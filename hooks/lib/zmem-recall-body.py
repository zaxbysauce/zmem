#!/usr/bin/env python3
"""Shared recall body for the injecting hooks (issue #58, 3.5/3.8/3.9).

Consumers (all invoke this file AS A SCRIPT — the hyphenated filename
cannot be imported):
  - zmem-recall.sh        (UserPromptSubmit)         mode "user_prompt"
  - zmem-precompact.sh    (PreCompact, Claude only)  mode "precompact"
  - zmem-subagent-recall.sh (SubagentStart)          mode "subagent"
    (task-text recall when the host event carries the delegated prompt,
    recent pull otherwise; "recent" remains accepted for back-compat)
  - zmem-pretool-recall.sh (PreToolUse, ZCode+Claude) mode "pretool"
    (issue #90 / #85 C: query derived from the tool input itself)

argv contract (see main()):
  argv[1] = absolute path to store.py (must exist or exit 0)
  argv[2] = canonical namespace
  argv[3] = budget in chars (optional, default 25000)
  argv[4] = mode — "user_prompt" | "precompact" | "recent"
  argv[5] = recent --limit      (recent/precompact modes; default 3)
  argv[6] = recent --global-limit (recent/precompact modes; default 2;
            subagent-recall passes 5/3 to preserve its pull width)

The body:
  1. Calls ``python store.py recall|recent ...`` with --no-bump, --json,
     and the per-mode query/limit set. Hooks never write the store.
  2. Reads the JSON dict list from stdout.
  3. Applies the selective-inject gate (signals test|compile|lint|reviewer
     above the prompt floor; signal=none above the gate-none floor;
     everything else omitted).
  4. Renders the rows through ``storelib._format_fenced_recall`` into a
     fenced, provenance-tagged block.
  5. Emits ``{"additionalContext": <ctx>}`` on stdout (the .sh wrappers
     wrap it in the <<<ZMEM_JSON>>>…<<<END>>> sentinel and neutralize
     sentinel/fence tokens as transport defense).
  6. If nothing is injected, names WHICH gate fired (issue #87 / #85
     direction 1) instead of always blaming the bar:
       - retrieval empty (or rows dropped by the passive injection-risk
         filter) → "no durable memories retrieved for this prompt."
       - rows reached the selective-inject gate and none passed →
         "no durable memories met the inject bar." (byte-identical to the
         pre-#87 one-liner so existing greps keep working)
       - the gate passed rows but the token budget emptied the set →
         "memories withheld: the injection token budget
         (ZMEM_INJECT_TOKEN_BUDGET) dropped every candidate row."
  7. Fail-open: any error path emits nothing (the wrapper's ``|| echo '{}'``
     handles it) and exits 0; a reason-classification error degrades to the
     retrieved-empty one-liner (never the bar) and still exits 0.

The selective-inject decision is logged to ``<data dir>/zmem-bg.log`` (the
dir resolved by ``_data_dir()``; I5 critic-fix: existing log file, not a new
one). Since issue #87 every line carries ``reason=`` (from schema_meta's
INJECT_SILENT_REASONS tuple, plus ``injected`` on the success line) and
``omitted=N`` when the passive injection-risk filter dropped rows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


# Selective-inject constants are imported from schema_meta (the documented
# single source of truth, PRR-017 fix) once the scripts dir is known — see
# _load_schema_meta(). The literals below are ONLY the import-failure
# fallback so a partially-deployed tree still runs with the documented
# defaults rather than crashing the hook (fail-open).
_FALLBACK_GROUNDED_SIGNALS = {"test", "compile", "lint", "reviewer", "user"}
_FALLBACK_FLOOR_PROMPT = 0.25
_FALLBACK_FLOOR_GATE_NONE = 0.4
_FALLBACK_FLOOR_RECENT = 0.5
# Issue #87 / #85 direction 1: import-failure fallbacks mirroring
# schema_meta.INJECT_SILENT_REASONS / INJECT_REASON_INJECTED (a
# partially-deployed tree still classifies with the documented set).
_FALLBACK_SILENT_REASONS = ("empty-pool", "omitted", "below-bar", "budget-drop")
_FALLBACK_REASON_INJECTED = "injected"

# User-visible silent one-liners (issue #87 / #85 direction 1). The below-bar
# string is byte-identical to the pre-#87 single one-liner on purpose —
# operator greps and muscle memory keep working for the one case it was true.
_SILENT_CTX_RETRIEVED_EMPTY = "no durable memories retrieved for this prompt."
_SILENT_CTX_BELOW_BAR = "no durable memories met the inject bar."
_SILENT_CTX_BUDGET_DROP = (
    "memories withheld: the injection token budget "
    "(ZMEM_INJECT_TOKEN_BUDGET) dropped every candidate row."
)

_schema_meta = None


def _load_schema_meta(store_py: str):
    """Import schema_meta from the scripts dir (next to store.py) so the
    gate reads the SAME constants every other surface imports (PRR-017).
    Returns None on import failure; callers then use the literals above.
    """
    global _schema_meta
    if _schema_meta is not None:
        return _schema_meta
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, scripts_dir)
        import schema_meta  # type: ignore
        _schema_meta = schema_meta
        return schema_meta
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _floor(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # Reject non-finite overrides (nan/inf parse but poison comparisons).
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def _gate_constants(store_py: str):
    """Resolve (floor, gate_none_floor, grounded_signals) from schema_meta
    when importable, else the documented literal defaults."""
    sm = _load_schema_meta(store_py)
    if sm is not None:
        # getattr fallbacks: a partially-updated deployment (older
        # schema_meta without a constant) degrades to the literal default
        # instead of crashing the hook (fail-open).
        return (
            _floor(
                getattr(sm, "INJECT_FLOOR_PROMPT_ENV", "ZMEM_INJECT_FLOOR_PROMPT"),
                getattr(sm, "INJECT_FLOOR_PROMPT_DEFAULT", _FALLBACK_FLOOR_PROMPT),
            ),
            _floor(
                getattr(sm, "INJECT_FLOOR_GATE_NONE_ENV", "ZMEM_INJECT_FLOOR_GATE_NONE"),
                getattr(sm, "INJECT_FLOOR_GATE_NONE_DEFAULT", _FALLBACK_FLOOR_GATE_NONE),
            ),
            set(getattr(sm, "INJECT_GROUNDED_SIGNALS", _FALLBACK_GROUNDED_SIGNALS)),
        )
    return (
        _floor("ZMEM_INJECT_FLOOR_PROMPT", _FALLBACK_FLOOR_PROMPT),
        _floor("ZMEM_INJECT_FLOOR_GATE_NONE", _FALLBACK_FLOOR_GATE_NONE),
        set(_FALLBACK_GROUNDED_SIGNALS),
    )


def _recent_floor(store_py: str) -> float:
    sm = _load_schema_meta(store_py)
    if sm is not None:
        return _floor(
            getattr(sm, "INJECT_FLOOR_RECENT_ENV", "ZMEM_INJECT_FLOOR_RECENT"),
            getattr(sm, "INJECT_FLOOR_RECENT_DEFAULT", _FALLBACK_FLOOR_RECENT),
        )
    return _floor("ZMEM_INJECT_FLOOR_RECENT", _FALLBACK_FLOOR_RECENT)


def _reason_constants(store_py: str):
    """Resolve (silent_reasons, injected_reason) from schema_meta (the
    single source of truth, PRR-017), with literal fallbacks for a
    partially-deployed tree (same discipline as _gate_constants)."""
    sm = _load_schema_meta(store_py)
    if sm is not None:
        return (
            tuple(getattr(sm, "INJECT_SILENT_REASONS", _FALLBACK_SILENT_REASONS)),
            getattr(sm, "INJECT_REASON_INJECTED", _FALLBACK_REASON_INJECTED),
        )
    return (_FALLBACK_SILENT_REASONS, _FALLBACK_REASON_INJECTED)


def _classify_silent_reason(rows, omitted=0, budget_emptied=False,
                            allowed=_FALLBACK_SILENT_REASONS):
    """Name WHY a silent inject is silent (issue #87 / #85 direction 1).

    Called only when nothing will be injected. Order matters and matches the
    #87 spec: budget-drop wins over below-bar (a budget wipe of a gate-passed
    set is a budget fact, not a gate fact); empty rows with omitted==0 is
    empty-pool even if the prompt was long — do not guess. ``allowed`` is the
    closed set from schema_meta; a drift/unknown value degrades to empty-pool
    rather than inventing a reason.
    """
    if budget_emptied:
        reason = "budget-drop"
    elif rows:
        reason = "below-bar"
    elif omitted > 0:
        reason = "omitted"
    else:
        reason = "empty-pool"
    if reason not in allowed:
        return "empty-pool"
    return reason


def _selective_inject_filter(rows, floor: float, gate_none_floor: float,
                             grounded_signals=None):
    """Apply the hook selective-inject gate (issue #58, 3.8).

    Issue spec: tighten ONLY ``signal=none`` (the agent's self-opinion)
    to ``gate_none_floor`` (default 0.4). Every GROUNDED signal
    (test/compile/lint/reviewer/user — the signal hierarchy's trusted
    tiers) injects at the prompt floor (default 0.25). The original
    draft omitted ``user`` from the trusted set, which silently dropped
    user-stated memories from every hook (caught by the pre-existing
    tests/test_launcher.js sentinel round-trip canary, seeded
    signal=user — a regression the Python-only local loop missed).

    Returns (selected, status) where status is 'injected' (anything
    qualified) or 'silent' (nothing passed).
    """
    if grounded_signals is None:
        grounded_signals = _FALLBACK_GROUNDED_SIGNALS
    selected = []
    for r in rows:
        try:
            conf = float(r.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        sig = (r.get("signal") or "none").lower()
        if sig == "none":
            if conf >= gate_none_floor:
                selected.append(r)
        elif sig in grounded_signals and conf >= floor:
            selected.append(r)
    status = "injected" if selected else "silent"
    return selected, status


# Log bound (PRR-023 fix): zmem-bg.log was maintenance-only (~lines/day)
# and is now appended per hook event. Cap it: past this size, truncate to
# empty before appending (operator can raise the cap via ZMEM_BG_LOG_MAX_BYTES).
_BG_LOG_DEFAULT_MAX_BYTES = 262144


def _log_inject_decision(rows, selected, status: str, reason: str,
                         omitted=0, tokens_used=None, tokens_budget=None,
                         ops_count=0) -> None:
    """Append the injected|silent decision to the existing bg log.

    Issue #87 / #85 direction 1: every line carries ``reason=`` (closed set
    from schema_meta plus ``injected``), and ``omitted=N`` when the passive
    injection-risk filter dropped rows — so an operator can tell an
    empty-pool silent (query construction problem) from a below-bar silent
    (scoring problem) from a budget-drop without log forensics. Field order:
    ``status``, ``reason``, optional ``omitted=N``, ``ids``, ``all``,
    optional ``tokens=used/budget`` (the ``tokens=\\d+/\\d+`` shape pinned by
    tests/test_token_budget.py is unchanged).

    PRR-016 fix: the log used to read a stale ``ZMEM_DATA_DIR`` var and so
    always landed in ~/.zmem even on store-overridden deployments; it now
    reads the live env. Superseded again by ring-reader parity (PRR-91-001
    follow-up): the resolution goes through ``_data_dir()`` so the log lands
    next to the ops ring it describes — ZMEM_STORE-first like the ring read
    path, with the same plugin-data steps for non-launcher environments.
    """
    log_path = os.path.join(_data_dir(), "zmem-bg.log")
    try:
        # PRR-023: bounded growth — truncate to empty when over the cap so
        # per-event appends cannot grow the log without limit.
        try:
            if os.path.getsize(log_path) > _bg_log_max_bytes():
                with open(log_path, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        ids_all = [r.get("id") for r in rows]
        ids_sel = [r.get("id") for r in selected]
        # v13 (issue #65, 10.9): tokens kept/budget ride on the same line so
        # budget behavior is auditable in the existing bg log.
        om = ""
        if omitted and omitted > 0:
            om = " omitted={0}".format(int(omitted))
        tok = ""
        if tokens_used is not None:
            tok = " tokens={used}/{budget}".format(
                used=tokens_used, budget=tokens_budget if tokens_budget is not None else "-"
            )
        # Issue #88 / #85 direction 2: when operation tokens augmented the
        # query, say how many — an invisible query lane cannot be debugged
        # (the #85 lesson). Additive; appended at line end.
        ops = ""
        if ops_count and ops_count > 0:
            ops = " ops={0}".format(int(ops_count))
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                "[{ts}] zmem-hook status={status} reason={reason}{om} "
                "ids={ids_sel} all={ids_all}{tok}{ops}\n".format(
                    ts=int(time.time()),
                    status=status,
                    reason=reason,
                    om=om,
                    ids_sel=ids_sel,
                    ids_all=ids_all,
                    tok=tok,
                    ops=ops,
                )
            )
    except OSError:
        # Fail-open: never let the audit log block the hook.
        pass


def _bg_log_max_bytes() -> int:
    raw = os.environ.get("ZMEM_BG_LOG_MAX_BYTES", "")
    try:
        value = int(raw) if raw else _BG_LOG_DEFAULT_MAX_BYTES
    except ValueError:
        return _BG_LOG_DEFAULT_MAX_BYTES
    return value if value > 0 else _BG_LOG_DEFAULT_MAX_BYTES


def _format_fence(rows, header: str, store_py: str = "") -> str:
    """Render the hook-text fence (issue #58, 3.5). Imports the
    Python helper from storelib so the constants stay in one place.

    ``store_py`` is the absolute path to the caller's store.py; its
    directory (skills/memory/scripts) is where both ``storelib`` and
    ``schema_meta`` are importable from. Deriving the path from this
    file's own location is WRONG — this file lives in hooks/lib, two
    levels away from the scripts dir (caught by the round-2 behavioral
    smoke, not by any source-text assertion).
    """
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if scripts_dir:
        sys.path.insert(0, scripts_dir)
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
    from storelib import _format_fenced_recall
    return _format_fenced_recall(rows, header)


def _inject_helpers(store_py: str):
    """Load storelib/inject.py (budget + envelope helpers, issue #65 10.9/10.8).

    Same path derivation as _format_fence (store.py's scripts dir). Returns
    (apply_token_budget, inject_token_budget, estimate_tokens, envelope_results)
    or None on import failure — callers fall back to no-budget/no-unwrap
    legacy behavior (fail-open hook discipline).
    """
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        import inject as _inject_mod
        return _inject_mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _emit_envelope(ctx: str) -> None:
    print(json.dumps({"additionalContext": ctx}))


def _pending_ops_path(session_id: str):
    """Path of the pending-inject sidecar for a session (issue #90 / #85 C).

    A pre-tool fence parked for hosts that may ignore pre-tool
    additionalContext (Claude: documented since 2.1.9 but honored only on
    newer builds) is consumed (and cleared) by the next user_prompt run —
    guaranteed delivery even if the field is ignored. None without a
    session id.
    """
    if not session_id:
        return None
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128]
    if not safe:
        return None
    return os.path.join(_data_dir(), "ops", safe + ".pending")


def _write_pending(session_id: str, ctx: str) -> None:
    path = _pending_ops_path(session_id)
    if not path or not ctx:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(ctx)
    except OSError:
        pass  # fail-open: delivery degrades to the pre-tool emit alone


def _consume_pending(session_id: str) -> str:
    path = _pending_ops_path(session_id)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            ctx = f.read()
    except OSError:
        return ""
    try:
        os.unlink(path)
    except OSError:
        pass
    return ctx if ctx.strip() else ""


def _ops_helpers(store_py: str):
    """Load storelib/ops_tokens.py (issue #88 / #85 direction 2 —
    operation-token derivation for the inject query). Same path derivation
    as _inject_helpers; returns None on import failure and the caller
    degrades to the prose-only query (fail-open)."""
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        import ops_tokens as _ops_mod
        return _ops_mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _data_dir() -> str:
    """Resolve the data dir for the ops ring and the bg log — the single
    resolver for every passive-lane read/write in this body.

    Chain: ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA >
    ~/.zmem. ZMEM_STORE-first matches the ring writer (convention-capture.sh)
    so a split ZMEM_STORE/ZMEM_DATA deployment cannot split reader from
    writer (review PRR-91-001); the plugin-data steps give the chain the same
    ORDER as the bash writer's four explicit-env cases (and
    host.resolve_store_path), so a non-launcher environment that only sets a
    plugin-data var still finds the ring instead of silently no-op'ing the
    lane. Normalization: expanduser applies to EVERY branch — host.py
    expands all four explicit-env values and both bash writers route any
    tilde-resolved DATA_DIR through expanduser (shared helper
    hooks/lib/zmem-tilde-expand.sh), so a tilde-valued var resolves to the
    same directory on every side of the lane (a tilde ZMEM_DATA or
    ZMEM_STORE previously split reader from writer — cubic round-2 finding).
    For non-tilde values expanduser is a no-op, so launcher deployments are
    unchanged. host.py's deeper legacy tail (~/.zcode/memory, plugin scan)
    stays approximated by ~/.zmem, as before. Launcher-spawned hooks are
    unaffected: zmem-launch.js always exports ZMEM_DATA."""
    store = os.environ.get("ZMEM_STORE", "")
    if store:
        return os.path.expanduser(os.path.dirname(store))
    data_dir = os.environ.get("ZMEM_DATA", "")
    if not data_dir:
        claude_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if claude_data:
            return os.path.expanduser(claude_data)
        zcode_data = os.environ.get("ZCODE_PLUGIN_DATA", "")
        if zcode_data:
            return os.path.expanduser(zcode_data)
        data_dir = os.path.join(os.path.expanduser("~"), ".zmem")
    return os.path.expanduser(data_dir)


def _ops_query_tokens(store_py: str, session_id: str, _ops_cache={}):
    """Derive operation tokens for this session's recent tool events
    (issue #88 / #85 direction 2). ZMEM_QUERY_CONTEXT=0 disables (kill
    switch, spec B). Fail-open: any error or missing ring degrades to []
    (prose-only query, byte-identical to the pre-#88 behavior)."""
    if not session_id:
        return []
    if "mod" in _ops_cache:
        ops_mod = _ops_cache["mod"]
    else:
        ops_mod = _ops_helpers(store_py)
        _ops_cache["mod"] = ops_mod
    if ops_mod is None:
        return []
    try:
        if not ops_mod.query_context_enabled():
            return []
        events = ops_mod.read_ops_ring(_data_dir(), session_id)
        return ops_mod.derive_ops_tokens(*events)
    except Exception:
        return []


def main() -> int:
    if len(sys.argv) < 4:
        return 0
    store_py = sys.argv[1]
    ns = sys.argv[2]
    try:
        budget = int(sys.argv[3])
    except (IndexError, ValueError):
        budget = 25000
    mode = sys.argv[4] if len(sys.argv) > 4 else "user_prompt"
    # Optional per-mode limits (issue #58 final-critic round 2): callers
    # that previously pulled wider recent windows (subagent-recall used
    # 5 project / 3 global) can pass them instead of forking the render.
    # Defaults 3/2 match session-start Tier 2 / PreCompact.
    try:
        recent_limit = sys.argv[5]
    except IndexError:
        recent_limit = "3"
    try:
        recent_global_limit = sys.argv[6]
    except IndexError:
        recent_global_limit = "2"
    # Optional agent-type label (SubagentStart consumers only): biases
    # the rendered header, preserving the pre-#58 header contract
    # ("... agent <type>") that tests/test_launcher.js pins.
    try:
        agent_label = sys.argv[7]
    except IndexError:
        agent_label = ""

    if not store_py or not os.path.isfile(store_py):
        return 0

    # PRR-017 fix: floors + grounded set come from schema_meta (single
    # source of truth) with literal fallbacks for a partially-deployed tree.
    floor, gate_none_floor, grounded_signals = _gate_constants(store_py)
    # issue #65, 10.9: budget helpers (None when storelib is not importable).
    _inj = None
    # issue #87: envelope omitted count (passive injection-risk drops) and the
    # closed reason set, resolved once for the classification below.
    omitted = 0
    ops_tokens = []
    session_id = ""
    pending_ctx = ""
    silent_reasons, injected_reason = _reason_constants(store_py)

    # Query selection per mode. `use_recent_pull` selects the query-less
    # recent lane; otherwise `query` drives the recall lane.
    use_recent_pull = False
    query = None

    try:
        raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
        try:
            stdin_obj = json.loads(raw_stdin)
        except (ValueError, TypeError):
            stdin_obj = None
        if isinstance(stdin_obj, dict):
            _sid = stdin_obj.get("session_id", "")
            session_id = _sid if isinstance(_sid, str) else ""

        if mode == "precompact" or mode == "recent":
            # PreCompact and subagent-recall: re-inject the
            # high-confidence recent payload. No prompt text.
            use_recent_pull = True
        elif mode == "pretool":
            # PreToolUse (issue #90 / #85 C): the query is derived from the
            # TOOL INPUT ITSELF — the command or file path about to run.
            # This is the only event that sees `git stash pop` before it
            # executes (the exact #85 failure shape). Non-operation events
            # derive to nothing and stay silent (fail-open, exit 0). The
            # kill switch is GLOBAL (review round 1): ZMEM_QUERY_CONTEXT=0
            # silences every query-context lane, this one included — an
            # operator flipping it expects silence, and this lane costs a
            # subprocess per matched tool call.
            _ops_mod = _ops_helpers(store_py)
            if _ops_mod is None:
                return 0
            try:
                if not _ops_mod.query_context_enabled():
                    return 0
            except Exception:
                pass  # degrade to enabled — the switch itself must not crash
            tool_desc = ""
            if isinstance(stdin_obj, dict):
                ti = stdin_obj.get("tool_input")
                if isinstance(ti, dict):
                    tool_desc = (ti.get("command") or ti.get("file_path")
                                 or ti.get("notebook_path") or ti.get("path")
                                 or "")
                    if not isinstance(tool_desc, str):
                        tool_desc = ""
            try:
                ops_tokens = _ops_mod.derive_ops_tokens(str(tool_desc))
            except Exception:
                ops_tokens = []
            if not ops_tokens:
                return 0
            query = " ".join(ops_tokens)
        elif mode == "subagent":
            # SubagentStart (issue #90 / #85 D): prefer the delegated task
            # text when the host event carries it (prompt/task/description),
            # so a "fix CI" subagent queries the ratchet lessons instead of
            # whatever recently landed; fall back to the recent pull when it
            # does not (the payload historically has no task text).
            task = ""
            if isinstance(stdin_obj, dict):
                for field in ("prompt", "task", "description"):
                    _v = stdin_obj.get(field, "")
                    if isinstance(_v, str) and len(_v.strip()) >= 5:
                        task = _v
                        break
            if task:
                query = task[:500]
            else:
                use_recent_pull = True
        else:
            # UserPromptSubmit: the prompt text is the QUERY.
            # PRR-003 fix: stdin carries the host's JSON EVENT
            # ({"prompt": ..., "session_id": ..., "cwd": ...}); parse out
            # the prompt field (the pre-#58 wrapper contract). Non-JSON
            # stdin (plain text) is used verbatim for manual invocation.
            if isinstance(stdin_obj, dict):
                prompt = stdin_obj.get("prompt", "")
                if not isinstance(prompt, str):
                    prompt = ""
            else:
                prompt = raw_stdin
            if not prompt or len(prompt.strip()) < 5:
                return 0
            # Issue #90 / #85 C: first consume any pending pre-tool fence
            # parked for a host that may not honor additionalContext
            # pre-tool (Claude) — deliver it even if this prompt's own
            # recall is silent, then clear the sidecar.
            pending_ctx = _consume_pending(session_id)
            # Issue #88 / #85 direction 2: decision-point prompts are prose
            # with zero lexical overlap with the operation-adjacent lessons
            # that matter; append this session's recent tool-operation tokens
            # (from the PostToolUse ring) to the query. Fail-open: no ring /
            # opt-out / derivation error ⇒ prose-only query, byte-identical
            # to the pre-#88 behavior (compose is the identity then).
            _ops_mod = _ops_helpers(store_py)
            ops_tokens = _ops_query_tokens(store_py, session_id)
            if _ops_mod is not None and ops_tokens:
                query = _ops_mod.compose_inject_query(prompt, " ".join(ops_tokens))
            else:
                query = prompt[:500]

        if use_recent_pull:
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recent",
                    "--namespace", ns,
                    "--limit", recent_limit,
                    "--min-confidence", str(_recent_floor(store_py)),
                    "--include-global",
                    "--global-limit", recent_global_limit,
                    "--no-bump",
                    "--json",
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            ).decode("utf-8", "replace")
        else:
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recall",
                    "--query", query,
                    "--namespace", ns,
                    "--limit", "5",
                    "--include-global",
                    "--global-limit", "3",
                    "--no-bump",
                    "--json",
                ],
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode("utf-8", "replace")
        rows = json.loads(out) if out.strip() else []
        # v13 (issue #65, 10.8): unwrap the read envelope ({"results": ...});
        # a bare list from a pre-v13 store.py still works. Issue #87: read the
        # envelope's omitted count BEFORE the unwrap discards it — it counts
        # rows the passive --no-bump filter dropped (injection-risk /
        # untrusted_web), the difference between "omitted" and "empty-pool".
        if isinstance(rows, dict):
            try:
                omitted = int(rows.get("omitted", 0) or 0)
            except (TypeError, ValueError):
                omitted = 0
        _inj = _inject_helpers(store_py)
        if _inj is not None:
            rows = _inj.envelope_results(rows)
        else:
            if isinstance(rows, dict):
                rows = rows.get("results", [])
            if not isinstance(rows, list):
                rows = []
    except Exception:
        rows = []
        omitted = 0

    selected, status = _selective_inject_filter(
        rows, floor=floor, gate_none_floor=gate_none_floor,
        grounded_signals=grounded_signals,
    )
    # v13 (issue #65, 10.9): token-budget admission BEFORE the fence. Protected
    # types (decision/constraint) are never dropped; lowest-score signal=none
    # rows drop first. ZMEM_CTX_BUDGET char truncation below stays as the hard
    # outer stop (the token estimate does not include every fence byte).
    tokens_budget = None
    tokens_used = None
    budget_emptied = False
    if selected and _inj is not None:
        selected, _est, _dropped = _inj.apply_token_budget(selected)
        tokens_budget = _inj.inject_token_budget()
        if not selected:
            status = "silent"
            budget_emptied = True

    if not selected:
        # Issue #87 / #85 direction 1: name WHY the inject is silent instead
        # of always blaming the bar. Fail-open on classification errors —
        # degrade to the retrieved-empty one-liner (never the bar: blaming the
        # bar for an empty pool is the exact misattribution #85 hit), still
        # exit 0. F18 survives: a budget wipe still logs the token fields.
        try:
            reason = _classify_silent_reason(
                rows, omitted=omitted, budget_emptied=budget_emptied,
                allowed=silent_reasons,
            )
        except Exception:
            reason = "empty-pool"
        if reason == "below-bar":
            ctx = _SILENT_CTX_BELOW_BAR
        elif reason == "budget-drop":
            ctx = _SILENT_CTX_BUDGET_DROP
        else:
            # empty-pool and omitted share the string: do not teach the model
            # that omitted injection-risk rows existed (#87 spec).
            ctx = _SILENT_CTX_RETRIEVED_EMPTY
        _log_inject_decision(
            rows, selected, status, reason,
            omitted=omitted,
            tokens_used=0 if budget_emptied else None,
            tokens_budget=tokens_budget if budget_emptied else None,
            ops_count=len(ops_tokens),
        )
        if mode == "pretool":
            # Issue #90 / #85 C: a per-tool-call one-liner would inject noise
            # on every unmatched operation — PreToolUse stays fully silent
            # when nothing qualified (the log line above carries the reason).
            # A parked pending fence is still delivered by the NEXT
            # user_prompt run, so nothing is lost.
            return 0
        if pending_ctx:
            # Issue #90 / #85 C: deliver the parked pre-tool fence even when
            # this prompt's own recall is silent — it was never seen.
            _emit_envelope(pending_ctx)
            return 0
        _emit_envelope(ctx)
        return 0

    header = (
        f"Relevant memories (zmem {mode}, namespace {ns}"
        + (f", agent {agent_label}" if agent_label else "")
        + "). Consider if they apply to this task; ignore if not."
    )
    ctx = _format_fence(selected, header, store_py=store_py)
    if budget > 0 and len(ctx) > budget:
        # PRR-015 fix: actually truncate. The previous branch reconstructed
        # the original string unchanged (no-op), so oversized memories
        # bypassed the budget. Cut the fence BODY at the budget (minus the
        # closer), then re-append the closer — the fence is never left
        # unclosed and the payload respects ZMEM_CTX_BUDGET.
        closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
        body_budget = max(0, budget - len(closer) - 1)
        ctx = ctx[:body_budget].rstrip() + "\n" + closer + "\n[recall truncated]"
    if pending_ctx and ctx:
        # Issue #90 / #85 C: prepend the parked pre-tool fence (same turn's
        # operation context) to this prompt's recall — then re-apply the
        # char budget to the COMBINED block (review round 1): the budget is
        # the outer stop for the emitted context, not per-recall.
        ctx = pending_ctx + "\n\n" + ctx
        if budget > 0 and len(ctx) > budget:
            closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
            body_budget = max(0, budget - len(closer) - 1)
            ctx = ctx[:body_budget].rstrip() + "\n" + closer + "\n[recall truncated]"
    # tokens_used is measured on the FINAL emitted context (post budget,
    # post char-truncation) - the honest number (issue #65, 10.9).
    if _inj is not None:
        tokens_used = _inj.estimate_tokens(ctx)
    _log_inject_decision(rows, selected, status, injected_reason,
                         omitted=omitted,
                         tokens_used=tokens_used, tokens_budget=tokens_budget,
                         ops_count=len(ops_tokens))
    if mode == "pretool" and os.environ.get("ZMEM_HOST", "") == "claude":
        # Issue #90 / #85 C: older Claude builds ignore pre-tool
        # additionalContext (documented since 2.1.9) — park the
        # fence so the next user_prompt run is REQUIRED to deliver it even
        # if the pre-tool emit was ignored. ZCode additionalContext is
        # documented honored, so no sidecar there; worst case on Claude is
        # one duplicate delivery, never a lost one.
        _write_pending(session_id, ctx)
    _emit_envelope(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())