#!/usr/bin/env python3
"""Deterministic fixture generator for the native Hermes suite (issue #165).

Writes ``native_cases.json`` (inputs: prompts, rows, sessions, delegation
literals, one role-``tool`` result, Tier-0 core text) and
``native_cases.expected.json`` (the exact manager-boundary output projection
the suite must reproduce byte-for-byte).  Both files are sorted-key UTF-8 JSON
with one final LF; the generator prints each file's SHA-256 so the PR can pin
the digests.  Deterministic by construction: no clock, no store, no network,
no random ids — every literal is fixed here.

Run:  python tests/fixtures/hermes/build_native_fixture.py [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

NAMESPACE = "project:fixture"
TIMESTAMP = "2026-09-10T00:00:00Z"

PROMPT_NONTRIVIAL = "Explain the fixture recall"
PROMPT_TRIVIAL = "ok"

SESSION_PRIMARY = "session-a"
SESSION_SECONDARY = "session-b"

CHILD_SESSION_ID = "00000000-0000-4000-8000-000000000164"
PARENT_SESSION_ID = "00000000-0000-4000-8000-000000000165"

ROW_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
)

DELEGATION_TASK = "Delegate the native fixture task"
DELEGATION_RESULT = "Native fixture delegation completed"

TOOL_RESULT_ROLE = "tool"
TOOL_RESULT_CONTENT = "git status --short"

TIER0_CORE_MD = (
    "# Core Memory (native fixture)\n"
    "\n"
    "Tier-0 text pinned by the native Hermes suite (issue #165).\n"
    "Stable rules only; injected through system_prompt_block.\n"
)

WRAPPER_OPEN = "<memory-context>"
WRAPPER_CLOSE = "</memory-context>"
SYSTEM_NOTE_PREFIX = "[System note: The following is recalled memory context,"

PROVIDER_MODE_EVENTS = (
    "post_tool_call:zmem",
    "pre_llm_call:zmem",
    "pre_verify:zmem",
)
COMPAT_MODE_EVENTS = (
    "pre_llm_call:zmem-hermes-reflect.py",
)

ROWS = (
    {
        "id": ROW_IDS[0],
        "type": "fact",
        "content": "fixture fact one for native recall",
        "tags": ("fixture", "native"),
    },
    {
        "id": ROW_IDS[1],
        "type": "fact",
        "content": "fixture fact two for native recall",
        "tags": ("fixture", "native"),
    },
    {
        "id": ROW_IDS[2],
        "type": "lesson",
        "content": "fixture lesson three for native recall",
        "tags": ("fixture", "native"),
    },
)


def cases_payload() -> dict:
    return {
        "namespace": NAMESPACE,
        "timestamp": TIMESTAMP,
        "prompts": {
            "nontrivial": PROMPT_NONTRIVIAL,
            "trivial": PROMPT_TRIVIAL,
        },
        "sessions": {
            "primary": SESSION_PRIMARY,
            "secondary": SESSION_SECONDARY,
        },
        "delegation": {
            "task": DELEGATION_TASK,
            "result": DELEGATION_RESULT,
            "child_session_id": CHILD_SESSION_ID,
            "parent_session_id": PARENT_SESSION_ID,
        },
        "tool_result": {
            "role": TOOL_RESULT_ROLE,
            "content": TOOL_RESULT_CONTENT,
        },
        "rows": [dict(row) for row in ROWS],
        "tier0_core_md": TIER0_CORE_MD,
    }


def expected_payload() -> dict:
    return {
        "candidate_ids_sorted": sorted(ROW_IDS),
        "compat_mode_events": list(COMPAT_MODE_EVENTS),
        "compat_mode_provider_callbacks": 0,
        "delegation_ref_path": "delegation:%s:%s" % (
            PARENT_SESSION_ID, CHILD_SESSION_ID),
        "mode_exclusivity_note": (
            "provider mode registers exactly one provider and the three "
            "provider hooks; compatibility mode registers zero plugin hooks "
            "and delivers exactly one shell-hook context"),
        "nontrivial_context_blocks": 1,
        "provider_mode_compatibility_hook_calls": 0,
        "provider_mode_events": list(PROVIDER_MODE_EVENTS),
        "system_note_prefix": SYSTEM_NOTE_PREFIX,
        "tier0_core_md": TIER0_CORE_MD,
        "tool_role": TOOL_RESULT_ROLE,
        "trivial_context_blocks": 0,
        "two_turn_delivery": {
            "turn1_reason": "injected",
            "turn2_reason": "already-delivered",
        },
        "wrapper_close": WRAPPER_CLOSE,
        "wrapper_open": WRAPPER_OPEN,
    }


def write_json(path: Path, payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True, indent=2,
                      ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", default=None,
        help="directory to write the fixture pair into "
             "(default: alongside this generator)")
    args = parser.parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else FIXTURE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    digests = {}
    for name, payload in (("native_cases.json", cases_payload()),
                          ("native_cases.expected.json", expected_payload())):
        digest = write_json(out_dir / name, payload)
        digests[name] = digest
        print("%s %s" % (out_dir / name, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
