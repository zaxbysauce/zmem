#!/usr/bin/env python3
"""Deterministic generator for tests/fixtures/launcher/codex-cases.json.

Issue #188 (Workstream N PR 5 of 6): the committed fixture pins the ten
Codex hook verbs in manifest order with a fixed stdin object and a fixed
sentinel child_stdout per verb, so the adapter's Windows-manifest loop
(runWindowsManifestCase) can compare exact envelopes against
codex-expected.json with no ambient path, clock, store, or host data.

Usage:
    python tests/fixtures/launcher/generate_codex_cases.py [output_path]

With no argument the committed fixture path is written; an explicit
output_path writes the identical bytes elsewhere (used by the frozen
acceptance check to prove determinism). Output is UTF-8, one final LF,
key order preserved, no ASCII escape pass.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "hooks" / "hooks.codex.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "codex-cases.json"

NAMESPACE = "project:zmem-188"
SESSION_ID = "00000000-0000-4000-8000-000000000188"
PROJECT = "C:\\zmem-188\\project"
TIMESTAMP = "2026-09-10T00:00:00Z"

# Expected Codex hookEventName per verb (None where upstream drops
# additionalContext, i.e. session-start emits no sentinel and PreCompact is
# pass-through). capture-failure maps to PostToolUse because Codex exposes no
# PostToolUseFailure — the launcher's codex remap.
EXPECTED_EVENT = {
    "recall": "UserPromptSubmit",
    "capture-correction": "UserPromptSubmit",
    "pretool-recall": "PreToolUse",
    "convention-capture": "PostToolUse",
    "capture-failure": "PostToolUse",
    "reflect": "Stop",
    "subagent-recall": "SubagentStart",
    "subagent-reflect": "SubagentStop",
    "session-start": None,
    "precompact": None,
}
# Verbs whose stub emits no sentinel: the launcher fails open to {}.
NO_SENTINEL = {"session-start", "precompact"}

# Contract deviation (disclosed in the PR body): the issue's Design step 5
# fixes ONE stdin object for all ten cases, but the launcher's codex
# capture-failure path normalizes the payload BEFORE spawning the child
# (normalizeCodexFailurePayload) and fails open to {} when the stdin carries
# no failure signal — with the literal no-failure stdin the capture-failure
# case could never produce the PostToolUse envelope the same issue mandates
# in codex-expected.json and AC2. The capture-failure case therefore carries
# the four canonical keys PLUS the single field the normalizer requires (a
# meaningful `error`; tool_name/tool_input default and no status is needed —
# final-critic round 1); every other case keeps the exact canonical stdin.
FAILURE_SIGNAL_FIELDS = {
    "error": "Exit code 1",
}


def case_stdin(verb):
    stdin = {
        "session_id": SESSION_ID,
        "cwd": PROJECT,
        "namespace": NAMESPACE,
        "timestamp": TIMESTAMP,
    }
    if verb == "capture-failure":
        stdin.update(FAILURE_SIGNAL_FIELDS)
    return stdin


def manifest_verbs():
    spec = json.loads(MANIFEST.read_text(encoding="utf-8"))
    verbs = []
    for _event, groups in spec.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("commandWindows") or hook.get("command") or ""
                verbs.append(command.rsplit(" ", 1)[-1])
    return verbs


def build_cases():
    cases = []
    for verb in manifest_verbs():
        if verb in NO_SENTINEL:
            child_stdout = ""
        else:
            child_stdout = (
                '<<<ZMEM_JSON>>>{"additionalContext":"fixture-%s"}<<<END>>>\n'
                % verb
            )
        case = {
            "verb": verb,
            "stdin": case_stdin(verb),
            "child_stdout": child_stdout,
            "expected_event": EXPECTED_EVENT[verb],
        }
        cases.append(case)
    return {
        "schema": 1,
        "namespace": NAMESPACE,
        "session_id": SESSION_ID,
        "project": PROJECT,
        "timestamp": TIMESTAMP,
        "cases": cases,
    }


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    output = Path(args[0]) if args else DEFAULT_OUTPUT
    document = build_cases()
    text = json.dumps(document, ensure_ascii=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
