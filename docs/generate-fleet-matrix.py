"""Deterministic generator for docs/fleet-capability-matrix.json (issue #280).

PRODUCED, never rewritten by hand: edit this file and re-run, or run with
--check to verify the committed artifact matches.  The wiring (which
launcher verb each manifest registers under which event) is DERIVED from
hooks/hooks.*.json on every run, so the matrix cannot drift from the
manifests without --check failing.

Usage:
    py -3 docs/generate-fleet-matrix.py            # regenerate in place
    py -3 docs/generate-fleet-matrix.py --check    # verify only, exit 1 on drift

Platform note: run with any Python 3.11+ interpreter (py -3 on Windows,
python3 on macOS/Linux).  Output is always LF-terminated UTF-8.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "fleet-capability-matrix.json"
HOOKS_DIR = REPO / "hooks"
PLUGIN_SRC = REPO / "hermes-plugin" / "__init__.py"

MIN_VERSION = {
    "claude": "2.1.283",
    "codex": "0.160.0",
    "zcode": "3.15.3",
    "hermes": "0.21.1",
    "opencode": "1.18.30",
    "deepseek": "0.2.1-rc.2",
}
SUPPORTED_HARNESSES = ("claude", "codex", "zcode", "hermes", "opencode",
                      "deepseek")

# Degraded-class texts.  These strings are the AC1 mapping verbatim as
# docs/FLEET.md renders it (ASCII quotes, `reason:` key prefix) so the doc
# and the machine artifact stay byte-comparable.
INJECT = (
    "Degrade, never block: inject from a valid, unexpired broker snapshot "
    "labelled [OFFLINE SNAPSHOT r<rev>, <age> old] (T-11); if no valid "
    "unexpired snapshot exists, inject nothing and record "
    "reason: \"hub-unreachable\"."
)
REFLECT = (
    "Degrade, never block: compute failure evidence locally from the "
    "transcript, enqueue it to the durable op queue (T-09), and render the "
    "reflection nudge locally."
)
CAPTURE = (
    "Degrade, never block: capture locally and enqueue to the durable op "
    "queue (T-09); acknowledge locally and replay on reconnect."
)
SESSION_END = (
    "Degrade, never block: flush the durable op queue best-effort; never "
    "block session teardown."
)
EXPLICIT_RECALL = (
    "Degrade, never block: answer from the valid, unexpired broker snapshot "
    "with the same [OFFLINE SNAPSHOT r<rev>, <age> old] label; if none, "
    "return no rows with reason: \"hub-unreachable\"."
)
OPENCUE_GAP = (
    "Degrade, never block: no OpenCode adapter ships yet (issue #66), so "
    "this moment has no delivery path and must not block the host."
)

CL, HU = "client_local", "hub_authoritative"
CODEX_DOUBLE_FIRE = (
    "openai/codex#15266: SessionStart + UserPromptSubmit first-prompt "
    "double-fire is handled unconditionally by ledger/reservation dedupe, "
    "never by version gating."
)
POSTCOMPACT_NOTE = (
    "Registered (hooks.claude.json wires PostCompact) but a deliberate "
    "no-op per issue #158: hooks/zmem-postcompact.sh prints {} and exits, "
    "and the verb is absent from TRANSLATED_HOOKS. No output channel exists "
    "until a later Workstream T PR adds one."
)
CODEX_NO_FAILURE_EVENT = (
    "Codex has no PostToolUseFailure event; failure capture is delivered and "
    "rides PostToolUse (see the registered capture-failure row), so this "
    "capability_gap row records the missing EVENT, not a missing behaviour."
)


def inp(name, locality):
    return {"name": name, "locality": locality}


def _manifest_wiring():
    """harness -> {verb: hook_event}, derived from the manifest commands."""
    wiring = {}
    for manifest in sorted(HOOKS_DIR.glob("hooks.*.json")):
        harness = manifest.name[len("hooks."):-len(".json")]
        data = json.loads(manifest.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict) or not hooks:
            raise SystemExit(f"{manifest.name} registers no events")
        verbs = {}
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    verb = None
                    args = hook.get("args")
                    if isinstance(args, list) and len(args) >= 2:
                        if str(args[-2]).endswith("zmem-launch.js"):
                            verb = str(args[-1])
                    if verb is None:
                        command = str(hook.get("command", ""))
                        parts = command.replace('"', " ").split()
                        if len(parts) >= 2 and parts[-2].endswith(
                                "zmem-launch.js"):
                            verb = parts[-1]
                    if verb:
                        verbs[verb] = event
        wiring[harness] = verbs
    return wiring


def _hermes_callbacks():
    """The callback names hermes-plugin actually registers (fail closed)."""
    if not PLUGIN_SRC.is_file():
        raise SystemExit("hermes-plugin/__init__.py is missing")
    text = PLUGIN_SRC.read_text(encoding="utf-8")
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("register_hook("):
            inner = stripped[len("register_hook("):].strip()
            if inner.startswith('"'):
                names.append(inner[1:inner.index('"', 1)])
            elif inner.startswith("'"):
                names.append(inner[1:inner.index("'", 1)])
    if not names:
        raise SystemExit("no register_hook(...) calls found in hermes-plugin")
    return sorted(set(names))


# Native dsh/Cordis plugin wiring (issue #374): the deepseek adapter
# registers these events in deepseek-plugin/index.mjs — no hooks.deepseek.json
# exists or may be fabricated; the verb->event map below mirrors the plugin's
# actual ctx.on registrations.
_NATIVE_WIRING = {
    "deepseek": {
        "session-start": "agent/created",
        "recall": "agent/pre-step",
        "pretool-recall": "tools/pre-execute",
        "capture-failure": "tools/result",
        "reflect": "agent/turn-stopping",
        # precompact is deliberately ABSENT (PR #409 review): the adapter
        # listens for compaction events but its handler acts only on
        # compaction/end (state reset) — no pre-compaction recall is
        # delivered, so the matrix records a capability_gap row instead of
        # an overclaimed registered row for compaction/start.
    },
}
_NATIVE_NOTE = ("Native dsh/Cordis plugin event "
                "(deepseek-plugin/index.mjs); no shell-hook manifest (#374).")

WIRING = _manifest_wiring()
WIRING.update(_NATIVE_WIRING)
HERMES_CALLBACKS = _hermes_callbacks()
UNION_EVENTS = sorted({ev for verbs in WIRING.values() for ev in verbs.values()})

# Canonical moment per launcher verb, and the hook_event each verb rides
# when a manifest registers it (derived above; this table only supplies the
# fallback event for capability_gap rows).
VERB_EVENT = {
    "session-start": "SessionStart",
    "recall": "UserPromptSubmit",
    "capture-correction": "UserPromptSubmit",
    "pretool-recall": "PreToolUse",
    "convention-capture": "PostToolUse",
    "capture-failure": "PostToolUseFailure",
    "posttoolbatch-recall": "PostToolBatch",
    "reflect": "Stop",
    "subagent-recall": "SubagentStart",
    "subagent-reflect": "SubagentStop",
    "precompact": "PreCompact",
    "postcompact": "PostCompact",
    "session-end": "SessionEnd",
}
LAUNCHER_MOMENTS = tuple(sorted(VERB_EVENT))
CALLBACK_MOMENTS = {cb: cb.replace("_", "-") for cb in HERMES_CALLBACKS}
EXPLICIT_MOMENTS = ("explicit-recall", "explicit-write")
ALL_MOMENTS = (LAUNCHER_MOMENTS + tuple(sorted(CALLBACK_MOMENTS.values()))
               + EXPLICIT_MOMENTS)

MOMENT_SPEC = {
    "session-start": dict(event="SessionStart", degraded=INJECT,
                          hub_ops=["select"],
                          inputs=[inp("Tier 0 files", CL), inp("git remote", CL),
                                  inp("namespace", CL), inp("cwd", CL),
                                  inp("transcript", CL),
                                  inp("rows", HU), inp("embeddings", HU),
                                  inp("ledger", HU), inp("policy", HU)]),
    "recall": dict(event="UserPromptSubmit", degraded=INJECT,
                   hub_ops=["select"],
                   inputs=[inp("transcript", CL), inp("git remote", CL),
                           inp("namespace", CL), inp("cwd", CL),
                           inp("rows", HU),
                           inp("embeddings", HU), inp("ledger", HU),
                           inp("policy", HU)]),
    "capture-correction": dict(event="UserPromptSubmit", degraded=CAPTURE,
                               hub_ops=["ingest"],
                               inputs=[inp("transcript", CL), inp("cwd", CL),
                                       inp("rows", HU), inp("ledger", HU)]),
    "pretool-recall": dict(event="PreToolUse", degraded=INJECT,
                           hub_ops=["select"],
                           inputs=[inp("transcript", CL), inp("tool_input", CL),
                                   inp("cwd", CL), inp("rows", HU),
                                   inp("embeddings", HU), inp("ledger", HU),
                                   inp("policy", HU)]),
    "convention-capture": dict(event="PostToolUse", degraded=CAPTURE,
                               hub_ops=["ingest"],
                               inputs=[inp("transcript", CL),
                                       inp("tool_input", CL),
                                       inp("tool_output", CL), inp("rows", HU),
                                       inp("ledger", HU)]),
    "capture-failure": dict(event="PostToolUseFailure", degraded=CAPTURE,
                            hub_ops=["ingest"],
                            inputs=[inp("transcript", CL),
                                    inp("tool_input", CL),
                                    inp("tool_output", CL), inp("rows", HU),
                                    inp("ledger", HU)]),
    "posttoolbatch-recall": dict(event="PostToolBatch", degraded=INJECT,
                                 hub_ops=["select"],
                                 inputs=[inp("transcript", CL), inp("rows", HU),
                                         inp("embeddings", HU),
                                         inp("ledger", HU),
                                         inp("policy", HU)]),
    "reflect": dict(event="Stop", degraded=REFLECT, hub_ops=["ingest"],
                    inputs=[inp("transcript", CL), inp("cwd", CL),
                            inp("rows", HU), inp("ledger", HU)]),
    "subagent-recall": dict(event="SubagentStart", degraded=INJECT,
                            hub_ops=["select"],
                            inputs=[inp("agent transcript", CL),
                                    inp("transcript", CL), inp("rows", HU),
                                    inp("embeddings", HU), inp("ledger", HU),
                                    inp("policy", HU)]),
    "subagent-reflect": dict(event="SubagentStop", degraded=REFLECT,
                             hub_ops=["ingest"],
                             inputs=[inp("agent transcript", CL),
                                     inp("transcript", CL), inp("rows", HU),
                                     inp("ledger", HU)]),
    "precompact": dict(event="PreCompact", degraded=INJECT,
                       hub_ops=["select"],
                       inputs=[inp("transcript", CL), inp("rows", HU),
                               inp("policy", HU)]),
    "postcompact": dict(event="PostCompact", degraded=INJECT,
                        hub_ops=["select"],
                        inputs=[inp("transcript", CL), inp("rows", HU),
                                inp("policy", HU)]),
    "session-end": dict(event="SessionEnd", degraded=SESSION_END,
                        hub_ops=["flush"],
                        inputs=[inp("transcript", CL), inp("ledger", HU)]),
    "pre-llm-call": dict(event="pre_llm_call", degraded=INJECT,
                         hub_ops=["select"],
                         inputs=[inp("transcript", CL),
                                 inp("git remote", CL), inp("namespace", CL),
                                 inp("rows", HU),
                                 inp("embeddings", HU), inp("ledger", HU),
                                 inp("policy", HU)]),
    "post-tool-call": dict(event="post_tool_call", degraded=CAPTURE,
                           hub_ops=["ingest"],
                           inputs=[inp("tool_input", CL),
                                   inp("tool_output", CL), inp("rows", HU),
                                   inp("ledger", HU)]),
    "pre-verify": dict(event="pre_verify", degraded=REFLECT,
                       hub_ops=["ingest"],
                       inputs=[inp("transcript", CL), inp("rows", HU),
                               inp("ledger", HU)]),
    "explicit-recall": dict(event=None, degraded=EXPLICIT_RECALL,
                            hub_ops=["select"],
                            inputs=[inp("query", CL), inp("git remote", CL),
                                    inp("namespace", CL), inp("rows", HU),
                                    inp("embeddings", HU),
                                    inp("policy", HU)]),
    "explicit-write": dict(event=None, degraded=CAPTURE,
                           hub_ops=["ingest"],
                           inputs=[inp("memory payload", CL),
                                   inp("git remote", CL), inp("namespace", CL),
                                   inp("rows", HU),
                                   inp("ledger", HU)]),
}
for _cb, _moment in CALLBACK_MOMENTS.items():
    MOMENT_SPEC[_moment]["notes"] = (
        f"Hermes memory-provider callback (register_hook "
        f"\"{_cb}\"), not a hooks/*.json manifest event."
    )


AUTHORIZATION_INPUT = {"name": "principal authorization",
                        "locality": "hub_authoritative"}


def _with_authorization_input(inputs, hub_ops):
    """Issue #363 AC1: every ingest row carries the authorization input."""
    if "ingest" not in hub_ops:
        return inputs
    for item in inputs:
        if "authorization" in str(item.get("name", "")):
            return inputs
    return list(inputs) + [dict(AUTHORIZATION_INPUT)]


def _row(harness, moment, hook_event, status, launcher, notes=None):
    spec = MOMENT_SPEC[moment]
    row = {
        "harness": harness,
        "min_version": MIN_VERSION[harness],
        "moment": moment,
        "hook_event": hook_event,
        "status": status,
        "launcher": launcher,
        "inputs": _with_authorization_input(spec["inputs"],
                                            spec["hub_ops"]),
        "hub_ops": spec["hub_ops"],
        "degraded": spec["degraded"],
    }
    note = notes if notes is not None else spec.get("notes")
    if harness in _NATIVE_WIRING:
        note = _NATIVE_NOTE
    if harness == "opencode" and status == "capability_gap":
        note = OPENCUE_GAP if note is None else note
    if note:
        row["notes"] = note
    return row


def build_rows():
    rows = []
    for harness in SUPPORTED_HARNESSES:
        wired = WIRING.get(harness, {})
        # 1. Launcher moments: registered where the manifest wires the verb,
        #    capability_gap otherwise (with the canonical event).
        for moment in LAUNCHER_MOMENTS:
            if moment in wired:
                notes = None
                if harness == "codex" and moment in ("session-start", "recall"):
                    notes = CODEX_DOUBLE_FIRE
                if moment == "postcompact" and harness == "claude":
                    notes = POSTCOMPACT_NOTE
                rows.append(_row(harness, moment, wired[moment], "registered",
                                 moment, notes))
            else:
                notes = None
                if harness == "codex" and moment == "capture-failure":
                    notes = CODEX_NO_FAILURE_EVENT
                rows.append(_row(harness, moment, VERB_EVENT[moment],
                                 "capability_gap", None, notes))
        # 2. Event-level completeness: every union event a harness does NOT
        #    register still gets a row, so the gap is visible.  When the
        #    moment already has a registered row under a different event
        #    (codex capture-failure), this is the one deliberate second row
        #    for a (harness, moment) pair.
        for event in UNION_EVENTS:
            if event in wired.values():
                continue
            for moment in LAUNCHER_MOMENTS:
                if VERB_EVENT[moment] != event:
                    continue
                if wired.get(moment) == event:
                    continue
                already = any(r["harness"] == harness and r["moment"] == moment
                              and r["hook_event"] == event for r in rows)
                if already:
                    continue
                notes = CODEX_NO_FAILURE_EVENT if (
                    harness == "codex" and moment == "capture-failure") else None
                rows.append(_row(harness, moment, event, "capability_gap",
                                 None, notes))
        # 3. Hermes callback moments.
        for moment in sorted(CALLBACK_MOMENTS.values()):
            if harness == "hermes":
                rows.append(_row(harness, moment, MOMENT_SPEC[moment]["event"],
                                 "registered", MOMENT_SPEC[moment]["event"]))
            else:
                rows.append(_row(harness, moment, MOMENT_SPEC[moment]["event"],
                                 "capability_gap", None))
        # 4. Explicit-tool moments (the CLI/skill surface every harness
        #    shares; OpenCode has no adapter yet).
        for moment in EXPLICIT_MOMENTS:
            status = "capability_gap" if harness == "opencode" else "registered"
            launcher = None if status == "capability_gap" else (
                "store.py recall" if moment == "explicit-recall"
                else "store.py add")
            rows.append(_row(harness, moment, None, status, launcher))
    return rows


def render(rows):
    return json.dumps({
        "$schema": "./fleet-capability-matrix.schema.json",
        "matrix_version": 1,
        "generator": "docs/generate-fleet-matrix.py",
        "generator_note": (
            "PRODUCED by the generator named above; run it with --check to "
            "verify. Wiring is derived from hooks/hooks.*.json and the "
            "hermes register_hook calls on every run."
        ),
        "rows": rows,
    }, indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="generate-fleet-matrix.py",
        description="Generate docs/fleet-capability-matrix.json (issue #280).")
    parser.add_argument("--check", action="store_true",
                        help="verify the committed artifact matches; no write")
    args = parser.parse_args(argv)

    rows = build_rows()
    text = render(rows)
    if args.check:
        try:
            current = OUT.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"generate-fleet-matrix: {OUT} is missing", file=sys.stderr)
            return 1
        if current != text:
            print("generate-fleet-matrix: DRIFT — the committed matrix does "
                  "not match the generator output; re-run without --check",
                  file=sys.stderr)
            return 1
        print(f"generate-fleet-matrix: {OUT} is up to date "
              f"({len(rows)} rows)")
        return 0
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {OUT} with {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
