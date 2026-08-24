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
  6. If nothing passes the gate, emits the silent one-liner
     "no durable memories met the inject bar." as additionalContext.
  7. Fail-open: any error path emits nothing (the wrapper's ``|| echo '{}'``
     handles it) and exits 0.

The selective-inject decision is logged to ``$DATA_DIR/zmem-bg.log``
(I5 critic-fix: existing log file, not a new one).
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


def _log_inject_decision(rows, selected, status: str) -> None:
    """Append the injected|silent decision to the existing bg log.

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
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                "[{ts}] zmem-hook status={status} ids={ids_sel} all={ids_all}\n".format(
                    ts=int(time.time()),
                    status=status,
                    ids_sel=ids_sel,
                    ids_all=ids_all,
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
    except Exception:
        rows = []

    selected, status = _selective_inject_filter(
        rows, floor=floor, gate_none_floor=gate_none_floor,
        grounded_signals=grounded_signals,
    )
    _log_inject_decision(rows, selected, status)

    if not selected:
        _emit_envelope("no durable memories met the inject bar.")
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
    _emit_envelope(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())