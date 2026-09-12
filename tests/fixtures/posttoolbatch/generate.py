#!/usr/bin/env python3
"""Generate the deterministic PostToolBatch fixtures (issue #120).

Writes, next to this file:
  - batch.json     the exact hook payload a Claude PostToolBatch event
                   carries (namespace project:fixture/zmem, fixed timestamp,
                   session 00000000-0000-4000-8000-000000001120, three tool
                   uses — Edit, Write, Bash — plus an ignored tool_response
                   on the Bash use and an ignored top-level result).
  - expected.json  the parser projection those bytes must produce: ordered
                   events, basenames, tool_count, names, the Bash command's
                   operation-token slug, the bounded query, and the
                   reason=empty-pool silent classification for the parser
                   projection (no session store behind the fixture).

Both files are compact sorted-key UTF-8 JSON with one final LF. The
generator imports NOTHING from the plugin (no storelib, no hook code) —
its specification is literal, so the committed bytes are stable and the
digests pinned by tests/test_posttoolbatch.py stay reproducible:

    python tests/fixtures/posttoolbatch/generate.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

SESSION_ID = "00000000-0000-4000-8000-000000001120"
NAMESPACE = "project:fixture/zmem"
TIMESTAMP = "2026-09-10T00:00:00Z"

BATCH = {
    "session_id": SESSION_ID,
    "namespace": NAMESPACE,
    "timestamp": TIMESTAMP,
    "tool_uses": [
        {"name": "Edit", "input": {"file_path": "src/a.py"}},
        {"name": "Write", "input": {"path": "docs/guide.md"}},
        {
            "name": "Bash",
            "input": {"command": "git stash pop"},
            "tool_response": "IGNORED-RESPONSE",
        },
    ],
    "result": "IGNORED-RESULT",
}

EXPECTED = {
    "session_id": SESSION_ID,
    "namespace": NAMESPACE,
    "timestamp": TIMESTAMP,
    "events": ["Edit src/a.py", "Write docs/guide.md", "Bash git stash pop"],
    "tool_count": 3,
    "names": ["Edit", "Write", "Bash"],
    "basenames": ["a.py", "guide.md"],
    "ops_token": "git-stash-pop",
    "query": "Edit src/a.py\nWrite docs/guide.md\nBash git stash pop",
    "reason": "empty-pool",
}


def _write(path: Path, obj: dict) -> None:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


def main() -> int:
    _write(HERE / "batch.json", BATCH)
    _write(HERE / "expected.json", EXPECTED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
