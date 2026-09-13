#!/usr/bin/env python
"""Deterministic timeout fixtures (issue #121).

Writes slow_store.json (the injected-clock schedule: Tier 0 completes at
clock 0, the store subprocess starts at 8000 ms, the launcher watchdog
deadline lands at 12000 ms) and expected_timeout.json (the outer-timeout
decision record the watchdog must produce) as compact sorted-key UTF-8 JSON
with a final LF, byte-exactly as the issue specifies. TimeoutBudgetTest.
test_fixture_digest pins the SHA-256 of both files to these bytes.

Usage: python tests/fixtures/timeout/generate.py
"""

import hashlib
import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

NAMESPACE = "project:fixture/zmem"
FIXED_TIMESTAMP = "2026-09-10T00:00:00Z"

SLOW_STORE = [
    {"clock_ms": 0, "event": "tier0_complete", "namespace": NAMESPACE,
     "timestamp": FIXED_TIMESTAMP},
    {"clock_ms": 8000, "event": "store_started", "namespace": NAMESPACE,
     "timestamp": FIXED_TIMESTAMP},
    {"clock_ms": 12000, "event": "watchdog_deadline", "namespace": NAMESPACE,
     "timestamp": FIXED_TIMESTAMP},
]

EXPECTED_TIMEOUT = {
    "namespace": NAMESPACE,
    "outer_timeout": 1,
    "reason": "omitted",
    "stage": "launcher",
    "tier0_emitted": 1,
    "tier2_rows": 0,
    "timeout_ms": 12000,
}


def _dump(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> int:
    # PR #198 review F-003: support generating into a scratch directory so
    # tests can verify generator parity WITHOUT overwriting the committed
    # fixture bytes in the source tree (a read-only checkout must work).
    out_dir = FIXTURE_DIR
    if "--out-dir" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--out-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "slow_store.json").write_text(
        _dump(SLOW_STORE), encoding="utf-8", newline="\n")
    (out_dir / "expected_timeout.json").write_text(
        _dump(EXPECTED_TIMEOUT), encoding="utf-8", newline="\n")
    for name in ("slow_store.json", "expected_timeout.json"):
        digest = hashlib.sha256((out_dir / name).read_bytes()).hexdigest()
        print("%s %s" % (name, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
