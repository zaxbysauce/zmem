#!/usr/bin/env python3
"""Deterministic wrapper-to-payload generator (issue #177).

Reads the fixed wrapper object (host-payloads.json) and writes one raw
payload file per adapter (claude/codex/zcode), then prints each output
SHA-256 digest.  No clock, no network, no store — byte-stable by design.

Usage:
    python tests/fixtures/retain/generate_payloads.py \
        tests/fixtures/retain/host-payloads.json \
        tests/fixtures/retain/claude-payload.json \
        tests/fixtures/retain/codex-payload.json \
        tests/fixtures/retain/zcode-payload.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        sys.stderr.write(
            "usage: generate_payloads.py WRAPPER CLAUDE CODEX ZCODE\n")
        return 2
    wrapper_path, claude_path, codex_path, zcode_path = (
        Path(argv[1]), Path(argv[2]), Path(argv[3]), Path(argv[4]))
    wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))

    claude = wrapper["claude"]
    codex = wrapper["codex"]
    zcode = wrapper["zcode"]

    outputs = [
        (claude_path, json.dumps(claude, separators=(",", ":"),
                                 ensure_ascii=False) + "\n"),
        (codex_path, json.dumps(codex, separators=(",", ":"),
                                ensure_ascii=False) + "\n"),
        (zcode_path, json.dumps(zcode, separators=(",", ":"),
                                ensure_ascii=False) + "\n"),
    ]
    for path, text in outputs:
        data = text.encode("utf-8")
        path.write_bytes(data)
        print("%s %s" % (path, hashlib.sha256(data).hexdigest()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
