"""Generate deterministic issue #99 checkpoint-query fixtures.

The fixture is intentionally data-only: generation never imports storelib or
reads a host environment.  Compact sorted UTF-8 JSON with one final LF keeps
the bytes stable across platforms and worktrees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
NAMESPACE = "project:fixture/zmem"
INGESTION_TS = "2026-09-10T00:00:00Z"

PHRASES = (
    "foreign-stash conflict verify stash list",
    "stale tree fetch main rebase verify diff",
    "stale tree fetched base force-with-lease",
    "stale tree fetched base force-with-lease",
    "base drift citation re-pin",
    "basename ratchet citation re-pin local battery",
)


def _id(number: int) -> str:
    return f"00000000-0000-4000-8000-000000000{number:03d}"


def _case(number: int, tool_input: dict, tokens: list[str], phrase: str) -> dict:
    return {
        "id": _id(number),
        "ingestion_ts": INGESTION_TS,
        "namespace": NAMESPACE,
        "tool_input": tool_input,
        "tokens": tokens,
        "checkpoint": phrase,
    }


def build_cases() -> dict:
    return {
        "ingestion_ts": INGESTION_TS,
        "namespace": NAMESPACE,
        "rows": [
            _case(991, {"command": "git stash pop"},
                  ["git", "stash", "pop"], PHRASES[0]),
            _case(992, {"command": "git reset --hard HEAD~1"},
                  ["git", "reset"], PHRASES[1]),
            _case(993, {"command": "git push --force-with-lease origin topic"},
                  ["git", "push"], PHRASES[2]),
            _case(994, {"command": "git push origin main"},
                  ["git", "push"], PHRASES[3]),
            _case(995, {"command": "git merge --squash topic"},
                  ["git", "merge"], PHRASES[4]),
            _case(996, {"file_path": "tests/test_checkpoint_queries.py"},
                  ["test_checkpoint_queries.py"], PHRASES[5]),
            _case(997, {"command": "git stash list"},
                  ["git", "stash"], ""),
            _case(998, {"command": "git status --short"},
                  ["git", "status"], ""),
        ],
    }


def build_expected() -> dict:
    cases = build_cases()["rows"]
    return {
        "namespace": NAMESPACE,
        "ingestion_ts": INGESTION_TS,
        "rows": [
            {"id": case["id"], "tokens": case["tokens"],
             "checkpoint": case["checkpoint"]}
            for case in cases
        ],
    }


def _write(path: Path, payload: dict) -> bytes:
    blob = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return blob


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate issue #99 fixtures")
    parser.add_argument("--out-dir", default=str(FIXTURE_DIR))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    cases = _write(out_dir / "cases.json", build_cases())
    expected = _write(out_dir / "expected.json", build_expected())
    print(hashlib.sha256(cases).hexdigest())
    print(hashlib.sha256(expected).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
