#!/usr/bin/env python3
"""Generate and validate the deterministic issue #130 expected fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
AS_OF = "2026-02-01T12:00:00Z"
NAMESPACE = "project:issue130"
MAIN = "00000000-0000-4000-8000-000000000130"
PRE = "00000000-0000-4000-8000-000000000131"
EQUAL = "00000000-0000-4000-8000-000000000132"
POST = "00000000-0000-4000-8000-000000000133"


def _expected_input() -> dict:
    return {
        "as_of": AS_OF,
        "namespace": NAMESPACE,
        "memories": [
            {"id": MAIN, "content": "seed main"},
            {"id": PRE, "content": "pre-time neighbor"},
            {"id": EQUAL, "content": "equal-time neighbor"},
            {"id": POST, "content": "post-time neighbor"},
        ],
        "edges": [
            {"src_id": MAIN, "dst_id": PRE, "relation": "related",
             "score": 0.9, "created_at": "2026-02-01T00:00:00Z"},
            {"src_id": MAIN, "dst_id": EQUAL, "relation": "related",
             "score": 0.9, "created_at": AS_OF},
            {"src_id": MAIN, "dst_id": POST, "relation": "related",
             "score": 0.9, "created_at": "2026-02-02T00:00:00Z"},
        ],
    }


def _validate_input(raw: bytes, obj: object) -> None:
    if b"\r" in raw or not raw.endswith(b"\n") or raw[:-1].endswith(b"\n"):
        raise ValueError("input must be UTF-8 JSON with one final LF")
    if obj != _expected_input():
        raise ValueError("input does not match the fixed issue #130 contract")
    assert isinstance(obj, dict)
    for memory in obj["memories"]:
        if not UUID_RE.fullmatch(memory["id"]):
            raise ValueError(f"invalid memory id: {memory['id']!r}")
    for edge in obj["edges"]:
        for key in ("src_id", "dst_id"):
            if not UUID_RE.fullmatch(edge[key]):
                raise ValueError(f"invalid edge id: {edge[key]!r}")
    if obj["as_of"] != AS_OF or obj["namespace"] != NAMESPACE:
        raise ValueError("unexpected issue #130 namespace or as_of")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    raw = args.input.read_bytes()
    obj = json.loads(raw.decode("utf-8"))
    _validate_input(raw, obj)
    expected = {
        "historical": [PRE, EQUAL],
        "present": [PRE, EQUAL, POST],
        "explain": [PRE, EQUAL],
    }
    output = (json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
              + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(output)
    temporary.replace(args.output)
    print(
        f"make_edge_expected: wrote {args.output} "
        f"sha256={hashlib.sha256(output).hexdigest()}"
    )
    print(
        f"edge-input.json sha256={hashlib.sha256(raw).hexdigest()}"
    )
    print(
        f"edge-expected.json sha256={hashlib.sha256(output).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
