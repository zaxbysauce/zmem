#!/usr/bin/env python3
"""Ingest a closeout-remote harvest JSON array into the zmem store.

Reads a harvest JSON file (the array format documented in
skills/closeout-remote/SKILL.md's "Output format" section), validates each
row's shape and enum values, and calls `store.py add` via subprocess for
each valid row.

This script does ONLY mechanical shape validation (required keys present,
enum values in range). It does NOT apply the capture bar and does NOT dedup
against the store — that judgment call belongs to the agent running
`commands/ingest-harvest.md`, which is expected to hand this script an
already-trimmed, already-recall-checked set of surviving rows. Feeding it a
raw, un-reviewed harvest will happily ingest everything that is well-formed.

Python 3.11, stdlib only, ASCII-only stdout/stderr.

Usage:
  python ingest_harvest.py <harvest.json> [--source-ref REF] [--store PATH]

Exit code: 0 if every row in the file was valid and ingested cleanly;
1 if any row was rejected (bad shape/enum) or failed to ingest.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_KEYS = ("namespace", "type", "content", "tags", "signal", "why")
ALLOWED_TYPES = ("fact", "lesson", "convention", "preference")
ALLOWED_SIGNALS = ("test", "compile", "lint", "reviewer", "user", "none")
# Same ceiling store.py's Tier 3 ingest validator enforces
# (INGEST_MAX_CONTENT_CHARS). A harvest is agent-authored text from a remote
# session; nothing legitimate is this long, and an oversized row bloats the
# store and every pack/prompt built from it.
MAX_CONTENT_CHARS = 65536


def _ascii(value: object) -> str:
    """Coerce to str and strip anything outside ASCII.

    Harvest content is arbitrary agent-authored text (em-dashes, curly
    quotes, etc. show up constantly), and a legacy Windows console/pipe can
    have a non-UTF-8 codepage with strict error handling — printing raw
    non-ASCII there raises UnicodeEncodeError mid-batch and kills the run
    before the summary line prints. Every non-ASCII codepoint is replaced
    with '?' rather than raising or silently dropping the row.
    """
    return str(value).encode("ascii", "replace").decode("ascii")


def _print(msg: str, *, err: bool = False) -> None:
    print(_ascii(msg), file=sys.stderr if err else sys.stdout)


def default_store_path() -> Path:
    """Resolve store.py relative to this script's location in the repo
    layout: <repo>/scripts/ingest_harvest.py -> <repo>/skills/memory/scripts/store.py.
    """
    here = Path(__file__).resolve().parent
    return here.parent / "skills" / "memory" / "scripts" / "store.py"


def validate_row(row: object, index: int) -> tuple[dict | None, str | None]:
    """Return (row, None) if valid, or (None, error-message) if not."""
    if not isinstance(row, dict):
        return None, f"row {index}: not a JSON object"

    missing = [k for k in REQUIRED_KEYS if k not in row]
    if missing:
        return None, f"row {index}: missing required key(s): {', '.join(missing)}"

    for k in REQUIRED_KEYS:
        if not isinstance(row[k], str):
            return None, f"row {index}: field '{k}' must be a string"

    if not row["namespace"].strip():
        return None, f"row {index}: namespace is empty"
    if not row["content"].strip():
        return None, f"row {index}: content is empty"
    if len(row["content"]) > MAX_CONTENT_CHARS:
        return None, (
            f"row {index}: content is {len(row['content'])} chars, over the "
            f"{MAX_CONTENT_CHARS} limit"
        )
    if row["type"] not in ALLOWED_TYPES:
        return None, (
            f"row {index}: invalid type '{row['type']}' "
            f"(allowed: {', '.join(ALLOWED_TYPES)})"
        )
    if row["signal"] not in ALLOWED_SIGNALS:
        return None, (
            f"row {index}: invalid signal '{row['signal']}' "
            f"(allowed: {', '.join(ALLOWED_SIGNALS)})"
        )

    return row, None


def ingest_row(store_py: Path, row: dict, source_ref: str) -> tuple[bool, str]:
    """Call `store.py add` for one row. Returns (ok, message).

    Deliberately does NOT pass --confidence: the signal-derived default in
    store.py's add_memory() is intentional, and hand-setting it here would
    let a harvest silently override the honesty check signal is supposed
    to encode.

    Encoding, both directions, because a harvest carries arbitrary non-ASCII:
      - PYTHONIOENCODING=utf-8 in the CHILD's env, so store.py's own success
        print (which echoes the namespace) cannot die with UnicodeEncodeError
        on a legacy Windows codepage AFTER the row was already committed --
        that made a landed row report as FAILED, the worst possible outcome
        (the operator re-runs and the store gets a near-duplicate).
      - encoding/errors on the PARENT's pipe decode, so a child byte sequence
        this console cannot represent is replaced rather than raising here.
    """
    cmd = [
        sys.executable, str(store_py), "add",
        "--namespace", row["namespace"],
        "--type", row["type"],
        "--content", row["content"],
        "--tags", row["tags"],
        "--signal", row["signal"],
        "--source-ref", source_ref,
    ]
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=child_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"store.py add exited {result.returncode}"
    return True, (result.stdout or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a closeout-remote harvest JSON array into the zmem store."
    )
    parser.add_argument("harvest_file", help="path to the harvest JSON file")
    parser.add_argument(
        "--source-ref", default=None,
        help="source-ref passed to every store.py add call "
             "(default: session:harvest-<file-stem>)",
    )
    parser.add_argument(
        "--store", default=None,
        help="explicit path to store.py "
             "(default: resolve relative to this script's repo layout, "
             "<repo>/skills/memory/scripts/store.py)",
    )
    args = parser.parse_args()

    harvest_path = Path(args.harvest_file)
    if not harvest_path.is_file():
        _print(f"[ingest-harvest] ERROR: harvest file not found: {harvest_path}", err=True)
        return 1

    try:
        raw = harvest_path.read_text(encoding="utf-8")
    except OSError as e:
        _print(f"[ingest-harvest] ERROR: could not read {harvest_path}: {e}", err=True)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _print(f"[ingest-harvest] ERROR: invalid JSON in {harvest_path}: {e}", err=True)
        return 1

    if not isinstance(data, list):
        _print(
            f"[ingest-harvest] ERROR: harvest file must contain a JSON array, "
            f"got {type(data).__name__}",
            err=True,
        )
        return 1

    store_py = Path(args.store) if args.store else default_store_path()
    if not store_py.is_file():
        _print(
            f"[ingest-harvest] ERROR: store.py not found at {store_py}. "
            f"Pass --store PATH to point at it explicitly (e.g. the path the "
            f"SessionStart hook injected into context this session).",
            err=True,
        )
        return 1

    source_ref = args.source_ref or f"session:harvest-{harvest_path.stem}"

    total = len(data)
    added = 0
    failed = 0

    for i, raw_row in enumerate(data, start=1):
        row, err = validate_row(raw_row, i)
        if err:
            _print(f"[ingest-harvest] REJECTED: {err}", err=True)
            failed += 1
            continue

        ok, message = ingest_row(store_py, row, source_ref)
        if ok:
            added += 1
            preview = row["content"][:80]
            _print(f"[ingest-harvest] added row {i}: [{row['namespace']}] {row['type']}: {preview}")
        else:
            failed += 1
            _print(f"[ingest-harvest] FAILED row {i}: {message}", err=True)

    _print(f"[ingest-harvest] summary: {total} row(s) in file, {added} added, {failed} failed/rejected")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
