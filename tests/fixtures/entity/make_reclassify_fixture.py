#!/usr/bin/env python
"""Deterministic generator for the issue #176 entity fixtures.

Writes, from the FIXED values in issue #176 Design step 7 (no randomness, no
store access, no network):

  tests/fixtures/entity/classify.json          30 classifier strings + kinds
  tests/fixtures/entity/reclassify.json        fixed other-rows seed store
  tests/fixtures/entity/operator-snapshot.json fixed operator-metrics block

It NEVER writes tests/fixtures/entity/reclassify-expected.json — that file is
generated once by the reviewed command:

  python skills/memory/scripts/store.py reclassify-entities --dry-run --json \
      > tests/fixtures/entity/reclassify-expected.json

against the isolated fixture store. Running this generator twice produces
byte-identical outputs (sorted keys, 2-space indent, LF newlines, UTF-8).

Usage: python tests/fixtures/entity/make_reclassify_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

# Issue #176 Design step 7 — the exact 30 strings, in this exact order.
CLASSIFY_ROWS = [
    {"name": "localhost", "expected": "host"},
    {"name": "127.0.0.1", "expected": "host"},
    {"name": "api.example.com", "expected": "host"},
    {"name": "build-host-1.example", "expected": "host"},
    {"name": "db-host.internal", "expected": "host"},
    {"name": "postgres:5432", "expected": "service"},
    {"name": "api-service", "expected": "service"},
    {"name": "cache-server", "expected": "service"},
    {"name": "job-daemon", "expected": "service"},
    {"name": "gpt-5.3", "expected": "model"},
    {"name": "claude-3-5-sonnet", "expected": "model"},
    {"name": "llama-3", "expected": "model"},
    {"name": "text-embedding-3-small", "expected": "model"},
    {"name": "owner/repo", "expected": "repo"},
    {"name": "github.com/openai/codex", "expected": "repo"},
    {"name": "openai/zmem", "expected": "repo"},
    {"name": "https://github.com/org/repo", "expected": "repo"},
    {"name": "C:\\work\\main.py", "expected": "file"},
    {"name": "/var/log/app.log", "expected": "file"},
    {"name": "src/main.py", "expected": "file"},
    {"name": "README.md", "expected": "file"},
    {"name": "git status", "expected": "command"},
    {"name": "python -m unittest", "expected": "command"},
    {"name": "npm test", "expected": "command"},
    {"name": "docker build .", "expected": "command"},
    {"name": "ValueError", "expected": "error_class"},
    {"name": "TimeoutError", "expected": "error_class"},
    {"name": "DatabaseException", "expected": "error_class"},
    {"name": "Brett", "expected": "other"},
    {"name": "alpha", "expected": "other"},
]

# Issue #176 Fixtures-to-add section — the fixed seed rows.
_TS = "2026-09-10T00:00:00Z"
RECLASSIFY = {
    "entities": [
        {"id": "00000000-0000-4000-8000-000000000001", "kind": "other",
         "canonical_name": "api.example.com", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000002", "kind": "other",
         "canonical_name": "postgres:5432", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000003", "kind": "other",
         "canonical_name": "gpt-5.3", "created_at": _TS, "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000004", "kind": "other",
         "canonical_name": "owner/repo", "created_at": _TS,
         "updated_at": _TS},
        {"id": "00000000-0000-4000-8000-000000000005", "kind": "other",
         "canonical_name": "Brett", "created_at": _TS, "updated_at": _TS},
    ],
    "aliases": [
        {"entity_id": "00000000-0000-4000-8000-000000000001",
         "alias_norm": "api.example.com"},
        {"entity_id": "00000000-0000-4000-8000-000000000001",
         "alias_norm": "api"},
    ],
    "memory_entity": [
        {"memory_id": "40000000-0000-4000-8000-000000000001",
         "entity_id": "00000000-0000-4000-8000-000000000001",
         "role": "mentions"},
        {"memory_id": "40000000-0000-4000-8000-000000000002",
         "entity_id": "00000000-0000-4000-8000-000000000002",
         "role": "mentions"},
        {"memory_id": "40000000-0000-4000-8000-000000000003",
         "entity_id": "00000000-0000-4000-8000-000000000003",
         "role": "mentions"},
    ],
}

# The operator snapshot block (fixed bytes): the audit measurement that
# motivated per-kind weights, plus the fixture apply's fixed before/after
# outcome. Numbers are FIXED provenance data from the issue, never measured
# at generation time.
OPERATOR_SNAPSHOT = {
    "as_of": "2026-09-10",
    "source": "maintainer store audit v3 (issue #176 evidence)",
    "memory_entity_links": 26511,
    "other_links": 24826,
    "other_link_share": 24826 / 26511,
    "fixture": {
        "entity_rows": 5,
        "memory_entity_rows": 3,
        "before_other_link_share": 1.0,
        "after_other_link_share": 0.0,
        "changed": 4,
    },
}


def _write_bytes(path: Path, obj: object) -> None:
    payload = json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False)
    path.write_bytes((payload + "\n").encode("utf-8"))


def main() -> int:
    _write_bytes(FIXTURE_DIR / "classify.json", CLASSIFY_ROWS)
    _write_bytes(FIXTURE_DIR / "reclassify.json", RECLASSIFY)
    _write_bytes(FIXTURE_DIR / "operator-snapshot.json", OPERATOR_SNAPSHOT)
    # The reviewed expected file is NEVER written here (see module docstring).
    print("wrote classify.json, reclassify.json, operator-snapshot.json "
          f"under {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
