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


PROMPT_HIGH_SIGNALS = {"test", "compile", "lint", "reviewer"}


def _floor(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _selective_inject_filter(rows, floor: float, gate_none_floor: float):
    """Apply the hook selective-inject gate (issue #58, 3.8).

    Returns (selected, status) where status is 'injected' (anything
    qualified) or 'silent' (nothing passed).
    """
    selected = []
    for r in rows:
        try:
            conf = float(r.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        sig = (r.get("signal") or "none").lower()
        if sig in PROMPT_HIGH_SIGNALS and conf >= floor:
            selected.append(r)
        elif sig == "none" and conf >= gate_none_floor:
            selected.append(r)
    status = "injected" if selected else "silent"
    return selected, status


def _log_inject_decision(rows, selected, status: str) -> None:
    """Append the injected|silent decision to the existing bg log."""
    data_dir = os.environ.get("ZMEM_DATA_DIR", "") or os.path.join(
        os.path.expanduser("~"), ".zmem",
    )
    if not data_dir:
        return
    log_path = os.path.join(data_dir, "zmem-bg.log")
    try:
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

    if not store_py or not os.path.isfile(store_py):
        return 0

    floor = _floor("ZMEM_INJECT_FLOOR_PROMPT", 0.25)
    gate_none_floor = _floor("ZMEM_INJECT_FLOOR_GATE_NONE", 0.4)

    try:
        if mode == "precompact" or mode == "recent":
            # PreCompact and subagent-recall: re-inject the
            # high-confidence recent payload. No prompt text.
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recent",
                    "--namespace", ns,
                    "--limit", recent_limit,
                    "--min-confidence", str(_floor("ZMEM_INJECT_FLOOR_RECENT", 0.5)),
                    "--include-global",
                    "--global-limit", recent_global_limit,
                    "--no-bump",
                    "--json",
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            ).decode("utf-8", "replace")
        else:
            # UserPromptSubmit: prompt text is the QUERY.
            # Read prompt from stdin if available; otherwise empty.
            prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
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
    )
    _log_inject_decision(rows, selected, status)

    if not selected:
        _emit_envelope("no durable memories met the inject bar.")
        return 0

    header = (
        f"Relevant memories (zmem {mode}, namespace {ns}). "
        f"Consider if they apply to this task; ignore if not."
    )
    ctx = _format_fence(selected, header, store_py=store_py)
    if budget > 0 and len(ctx) > budget:
        # Truncate from the END (preserve fence closer), but only inside
        # the body — never break an unclosed fence.
        marker = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
        idx = ctx.rfind(marker)
        if idx > 0:
            ctx = ctx[:idx].rstrip() + "\n" + marker
    _emit_envelope(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())