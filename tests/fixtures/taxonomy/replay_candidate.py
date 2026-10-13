#!/usr/bin/env python3
"""Deterministic, read-only candidate-delta generator for the #172 taxonomy RFC.

Projects the #155 replay evidence (store snapshot + decision log + expected
report) into one observed-delta JSON file per taxonomy candidate. The generator
is deliberately hermetic: it never imports storelib, never opens SQLite, never
loads a model, and never reads an environment-selected store. Every input is
read as raw bytes; every input or output failure is fail-closed with a single
LF-terminated diagnostic line and exit 1, and no output file is replaced after
any failure. Argparse usage errors (missing flags, unknown --candidate) follow
the standard convention: exit 2 with argparse's usage text, per contract
Design step 4.

Contract: zaxbysauce/zmem issue #172, Design steps 3 and 4.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

CANDIDATES = (
    "state",
    "event",
    "observation",
    "runbook",
    "entity_card",
    "host",
    "service",
    "model",
    "repo",
    "file",
    "command",
    "error_class",
)


class TaxonomyError(Exception):
    """A fail-closed condition carrying the exact one-line diagnostic."""


def read_input_bytes(path: Path) -> bytes:
    """Read one input file as raw bytes, with no newline normalization."""
    if not path.is_file():
        raise TaxonomyError(f"[taxonomy] input error: missing file {path}")
    try:
        return path.read_bytes()
    except OSError:
        raise TaxonomyError(f"[taxonomy] input error: unreadable file {path}") from None


def canonical_input_digest(snapshot: Path, decisions: Path) -> str:
    """sha256(snapshot bytes + decisions bytes), no separator bytes."""
    snapshot_bytes = read_input_bytes(snapshot)
    decisions_bytes = read_input_bytes(decisions)
    return hashlib.sha256(snapshot_bytes + decisions_bytes).hexdigest()


def canonicalize(value: object) -> object:
    """Recursively sort object keys and sort array elements by canonical bytes."""
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        elements = [canonicalize(item) for item in value]
        return sorted(
            elements,
            key=lambda item: json.dumps(
                item, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ),
        )
    return value


def load_expected(path: Path, digest: str) -> dict[str, object]:
    """Parse expected.json and require its input_digest to equal `digest`."""
    raw = read_input_bytes(path)
    try:
        text = raw.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise TaxonomyError(f"[taxonomy] input error: invalid UTF-8 JSON in {path}") from None
    if not isinstance(document, dict):
        raise TaxonomyError(f"[taxonomy] input error: invalid UTF-8 JSON in {path}")
    expected_digest = document.get("input_digest")
    if not isinstance(expected_digest, str):
        raise TaxonomyError(
            "[taxonomy] input error: expected input_digest is missing or not a string"
        )
    if expected_digest != digest:
        raise TaxonomyError(
            f"[taxonomy] input error: digest mismatch: expected {expected_digest} computed {digest}"
        )
    return document


def build_delta(candidate: str, digest: str, expected: dict[str, object]) -> dict[str, object]:
    """Assemble the baseline evidence projection for one candidate."""
    return {
        "candidate": candidate,
        "input_digest": digest,
        "observed_delta": {
            "replay": canonicalize(expected),
            "runtime_change": "none",
            "schema_change": "none",
        },
    }


def write_delta(path: Path, payload: dict[str, object]) -> None:
    """Write canonical bytes through a sibling temp file and os.replace."""
    blob = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    parent = path.parent
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(dir=str(parent), prefix=path.name + ".", suffix=".tmp")
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
        os.replace(temp_path, path)
        temp_path = None
    except OSError:
        raise TaxonomyError(f"[taxonomy] output error: unable to write {path}") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="taxonomy candidate delta generator")
    parser.add_argument(
        "--candidate",
        dest="candidate",
        type=str,
        choices=CANDIDATES,
        required=True,
        help="taxonomy candidate",
    )
    parser.add_argument(
        "--snapshot", dest="snapshot", type=Path, required=True, help="#155 replay store"
    )
    parser.add_argument(
        "--decisions", dest="decisions", type=Path, required=True, help="#155 decision log"
    )
    parser.add_argument(
        "--expected", dest="expected", type=Path, required=True, help="#155 expected replay JSON"
    )
    parser.add_argument(
        "--out", dest="out", type=Path, required=True, help="candidate delta output"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        digest = canonical_input_digest(args.snapshot, args.decisions)
        expected = load_expected(args.expected, digest)
        payload = build_delta(args.candidate, digest, expected)
        write_delta(args.out, payload)
    except TaxonomyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
