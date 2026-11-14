#!/usr/bin/env python
"""Deterministic probe fixtures for the OpenCode host adapter (issue #66 AC1).

Writes, from fixed in-source values only (no clock, no network, no host
probes at generation time):

  probe-v1.json        the SELECTED probe record (selected=true)
  probe-v2.json        the REJECTED probe record (selected=false)
  probe-expected.json  the paired expected-output fixture (exact bytes are
                       pinned by issue #66's fixture contract)

and prints the SHA-256 digests of both probe files. The fixture values model
the 2026-09-10 probe session the issue records (version strings 1.0.0,
callbacks system.transform / session.compacting / event); the LIVE runtime
wiring against the current SDK is recorded in the issue's probe comment and
in opencode-plugin/index.mjs (see the plan's live-API reconciliation).
"""
import hashlib
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

PROBE_DATE = "2026-09-10"
VERSION = "1.0.0"
CALLBACKS = ["system.transform", "session.compacting", "event"]
EVENT_NAME = "session.compacting"
SESSION_FIELDS = ["id", "worktree"]
TRANSCRIPT_SHAPE = {"info": {}, "parts": []}

EXPECTED_LITERAL = (
    '{"selected":"probe-v1","rejected":"probe-v2","probe_date":"2026-09-10",'
    '"version":"1.0.0","callbacks":["system.transform","session.compacting",'
    '"event"],"event_name":"session.compacting","session_fields":["id",'
    '"worktree"],"transcript_shape":{"info":{},"parts":[]}}\n'
)


def _probe_record(selected: bool) -> dict:
    return {
        "probe_date": PROBE_DATE,
        "version": VERSION,
        "callbacks": list(CALLBACKS),
        "event_name": EVENT_NAME,
        "session_fields": list(SESSION_FIELDS),
        "transcript_shape": dict(TRANSCRIPT_SHAPE),
        "selected": selected,
    }


def _dump(obj: dict) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def main() -> int:
    v1 = _dump(_probe_record(True)) + b"\n"
    v2 = _dump(_probe_record(False)) + b"\n"
    (FIXTURE_DIR / "probe-v1.json").write_bytes(v1)
    (FIXTURE_DIR / "probe-v2.json").write_bytes(v2)
    (FIXTURE_DIR / "probe-expected.json").write_bytes(EXPECTED_LITERAL.encode("utf-8"))
    print("probe-v1.json sha256 " + hashlib.sha256(v1).hexdigest())
    print("probe-v2.json sha256 " + hashlib.sha256(v2).hexdigest())
    print("probe-expected.json sha256 "
          + hashlib.sha256(EXPECTED_LITERAL.encode("utf-8")).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
