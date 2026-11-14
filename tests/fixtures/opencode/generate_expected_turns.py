#!/usr/bin/env python
"""Deterministic transcript + expected-turns fixtures for the OpenCode
adapter (issue #66 AC1/AC4).

Writes four files with fixed in-source values only (no clock, no network):

  session.jsonl           RAW fixture-shape transcript, the five records
                          pinned byte-for-byte by issue #66 (compact JSON
                          separators, one record per line, trailing LF)
  expected-turns.json     canonical Turn JSONL the normalizer must emit for
                          those records (seq 0-4, UTC RFC3339 ts, null for
                          the invalid time, harness=opencode)
  live-session.jsonl      SDK-shaped transcript exercising the LIVE message
                          shapes the @opencode-ai/plugin events carry
                          (time.created object; tool parts with tool +
                          state.output) — plan R2 F1
  expected-live-turns.json  canonical Turn JSONL for the live-shaped records

and prints the SHA-256 digest of every file it writes. The normalization
embedded here is the byte-spec of the fixtures; tests/test_mine_history.py
and tests/test_opencode_plugin.js prove the REAL adapters (python + JS)
produce identical bytes from the same inputs (cross-language parity).
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
HARNESS = "opencode"
SOURCE_PATH = "session.jsonl"
LIVE_SOURCE_PATH = "live-session.jsonl"

SESSION_UUID = "00000000-0000-4000-8000-000000000660"
WORKTREE = "C:/oc-project"

SESSION_RECORDS = [
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000661",
            "sessionID": SESSION_UUID,
            "parentID": None,
            "role": "user",
            "time": "2026-09-10T12:00:00-05:00",
            "worktree": WORKTREE,
        },
        "parts": [{"type": "text", "text": "remember: store the test command"}],
    },
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000662",
            "sessionID": SESSION_UUID,
            "parentID": "00000000-0000-4000-8000-000000000661",
            "role": "assistant",
            "time": 1767225600000,
            "worktree": WORKTREE,
        },
        "parts": [
            {"type": "text", "text": "I ran it."},
            {"type": "tool", "name": "bash", "text": "ok"},
        ],
    },
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000663",
            "sessionID": SESSION_UUID,
            "parentID": "00000000-0000-4000-8000-000000000662",
            "role": "user",
            "time": "not-a-time",
            "worktree": WORKTREE,
        },
        "parts": [{"type": "text", "text": "Use the isolated store."}],
    },
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000664",
            "sessionID": SESSION_UUID,
            "parentID": "00000000-0000-4000-8000-000000000663",
            "role": "assistant",
            "time": "2026-09-10T18:00:00Z",
            "worktree": WORKTREE,
        },
        "parts": [{"type": "text", "text": "The store is isolated."}],
    },
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000665",
            "sessionID": SESSION_UUID,
            "parentID": "00000000-0000-4000-8000-000000000664",
            "role": "user",
            "time": "2026-09-10T13:00:00-05:00",
            "worktree": WORKTREE,
        },
        "parts": [{"type": "text", "text": "Keep the fixture deterministic."}],
    },
]

# SDK-shaped records (live @opencode-ai/plugin message shapes): time may be
# an object carrying `created` (epoch ms); tool parts carry `tool` and a
# `state` object with `status`/`output`/`error` instead of name/text.
LIVE_SESSION_RECORDS = [
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000671",
            "sessionID": SESSION_UUID,
            "parentID": None,
            "role": "user",
            "time": {"created": 1767225600000},
            "worktree": WORKTREE,
        },
        "parts": [{"type": "text", "text": "live shape: created time"}],
    },
    {
        "info": {
            "id": "00000000-0000-4000-8000-000000000672",
            "sessionID": SESSION_UUID,
            "parentID": "00000000-0000-4000-8000-000000000671",
            "role": "assistant",
            "time": {"created": 1767225660000},
            "worktree": WORKTREE,
        },
        "parts": [
            {"type": "text", "text": "live shape: tool part"},
            {"type": "tool", "tool": "bash",
             "state": {"status": "completed", "output": "ok"}},
        ],
    },
]


def _utc_rfc3339(epoch_ms: int) -> str:
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_ts(value):
    """Reference timestamp normalization (mirrors the adapters)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _utc_rfc3339(int(value))
    if isinstance(value, dict):
        created = value.get("created")
        return _normalize_ts(created)
    if isinstance(value, str):
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _normalize_blocks(parts):
    blocks = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "tool":
            name = part.get("name") or part.get("tool") or ""
            state = part.get("state")
            if isinstance(state, dict):
                text = state.get("output") or state.get("error") or ""
            else:
                text = part.get("text") or ""
            blocks.append({"type": "tool", "name": name, "text": text})
    return blocks


def normalize_opencode_message(message, source_path, seq):
    """Reference Turn normalization (the byte-spec of the fixtures)."""
    info = message.get("info") or {}
    return {
        "session_id": info.get("sessionID") or info.get("session_id") or "",
        "workdir": info.get("worktree") or info.get("workdir") or "",
        "turn_uuid": info.get("id") or "",
        "parent_uuid": info.get("parentID") or info.get("parent_id"),
        "seq": seq,
        "ts": _normalize_ts(info.get("time")),
        "role": info.get("role") or "",
        "blocks": _normalize_blocks(message.get("parts")),
        "source_path": source_path,
        "harness": HARNESS,
    }


def _dump_compact(records) -> bytes:
    out = [json.dumps(r, ensure_ascii=False, separators=(",", ":"))
           for r in records]
    return ("\n".join(out) + "\n").encode("utf-8")


def _dump_turns(records, source_path) -> bytes:
    turns = [normalize_opencode_message(r, source_path, seq)
             for seq, r in enumerate(records)]
    return _dump_compact(turns)


def _write(name: str, data: bytes) -> None:
    (FIXTURE_DIR / name).write_bytes(data)
    print("{} sha256 {}".format(name, hashlib.sha256(data).hexdigest()))


def main() -> int:
    _write("session.jsonl", _dump_compact(SESSION_RECORDS))
    _write("expected-turns.json", _dump_turns(SESSION_RECORDS, SOURCE_PATH))
    _write("live-session.jsonl", _dump_compact(LIVE_SESSION_RECORDS))
    _write("expected-live-turns.json",
           _dump_turns(LIVE_SESSION_RECORDS, LIVE_SOURCE_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
