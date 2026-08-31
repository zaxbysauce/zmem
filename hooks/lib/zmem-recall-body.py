#!/usr/bin/env python3
"""Shared recall body for the injecting hooks (issue #58, 3.5/3.8/3.9).

Consumers (all invoke this file AS A SCRIPT — the hyphenated filename
cannot be imported):
  - zmem-recall.sh        (UserPromptSubmit)         mode "user_prompt"
  - zmem-precompact.sh    (PreCompact, Claude only)  mode "precompact"
  - zmem-subagent-recall.sh (SubagentStart)          mode "recent"

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

The selective-inject decision is logged to ``$DATA_DIR/zmem-bg.log``
(I5 critic-fix: existing log file, not a new one). Since issue #87 every
line carries ``reason=`` (from schema_meta's INJECT_SILENT_REASONS tuple,
plus ``injected`` on the success line) and ``omitted=N`` when the passive
injection-risk filter dropped rows.
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
                         omitted=0, tokens_used=None, tokens_budget=None) -> None:
    """Append the injected|silent decision to the existing bg log.

    Issue #87 / #85 direction 1: every line carries ``reason=`` (closed set
    from schema_meta plus ``injected``), and ``omitted=N`` when the passive
    injection-risk filter dropped rows — so an operator can tell an
    empty-pool silent (query construction problem) from a below-bar silent
    (scoring problem) from a budget-drop without log forensics. Field order:
    ``status``, ``reason``, optional ``omitted=N``, ``ids``, ``all``,
    optional ``tokens=used/budget`` (the ``tokens=\\d+/\\d+`` shape pinned by
    tests/test_token_budget.py is unchanged).

    PRR-016 fix: read the CANONICAL ``ZMEM_DATA`` env (what every hook
    wrapper and the launcher export); the previous ``ZMEM_DATA_DIR`` read
    never matched, so the log always landed in ~/.zmem even on
    store-overridden deployments. ``ZMEM_STORE``'s parent is the secondary
    resolution (host.resolve_store_path gives ZMEM_STORE top precedence).
    """
    data_dir = os.environ.get("ZMEM_DATA", "")
    if not data_dir:
        store = os.environ.get("ZMEM_STORE", "")
        data_dir = os.path.dirname(store) if store else ""
    if not data_dir:
        data_dir = os.path.join(os.path.expanduser("~"), ".zmem")
    log_path = os.path.join(data_dir, "zmem-bg.log")
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
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                "[{ts}] zmem-hook status={status} reason={reason}{om} "
                "ids={ids_sel} all={ids_all}{tok}\n".format(
                    ts=int(time.time()),
                    status=status,
                    reason=reason,
                    om=om,
                    ids_sel=ids_sel,
                    ids_all=ids_all,
                    tok=tok,
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
    silent_reasons, injected_reason = _reason_constants(store_py)

    try:
        if mode == "precompact" or mode == "recent":
            # PreCompact and subagent-recall: re-inject the
            # high-confidence recent payload. No prompt text.
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
            # UserPromptSubmit: the prompt text is the QUERY.
            # PRR-003 fix: stdin carries the host's JSON EVENT
            # ({"prompt": ..., "session_id": ..., "cwd": ...}); parse out
            # the prompt field (the pre-#58 wrapper contract). Non-JSON
            # stdin (plain text) is used verbatim for manual invocation.
            raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
            prompt = ""
            try:
                obj = json.loads(raw_stdin)
                prompt = obj.get("prompt", "") if isinstance(obj, dict) else ""
            except (ValueError, TypeError):
                prompt = raw_stdin
            if not prompt or len(prompt.strip()) < 5:
                return 0
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recall",
                    "--query", prompt[:500],
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
        )
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
    # tokens_used is measured on the FINAL emitted context (post budget,
    # post char-truncation) - the honest number (issue #65, 10.9).
    if _inj is not None:
        tokens_used = _inj.estimate_tokens(ctx)
    _log_inject_decision(rows, selected, status, injected_reason,
                         omitted=omitted,
                         tokens_used=tokens_used, tokens_budget=tokens_budget)
    _emit_envelope(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())