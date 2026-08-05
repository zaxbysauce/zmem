#!/usr/bin/env python3
"""Hermes shell hook: convention + failure signal recorder (post_tool_call).

⚠️ IMPORTANT — ``post_tool_call`` results are OBSERVATIONAL in Hermes: the
shell-hook bridge parses ``{"context": ...}`` for any event, but the sole
emitter (``model_tools._emit_post_tool_call_hook``) **discards the return
value**. ``post_tool_call`` is documented as observational
(``model_tools.py`` ~line 1467: "post_tool_call which stays observational").

So this hook does NOT emit a nudge — it would be a dead letter. Instead it
**records signal** to zmem's ``meta`` table for the ``pre_llm_call`` reflect
hook (``zmem-hermes-reflect.py``, whose results ARE consumed) to act on:
  - increments the per-session convention counter (every successful matching call)
  - sets a ``pending_convention_nudge`` flag every Nth call
  - sets a ``pending_failure_nudge`` flag on the first failed tool call

Always emits ``{}`` (silent). The reflect hook reads the pending flags and
delivers the actual nudge on the next ``pre_llm_call``. This separation keeps
the observation (post_tool_call) and delivery (pre_llm_call) on the right
sides of Hermes' hook-consumption contract.

Stdlib only. Store path mirrors zmem: ``ZMEM_STORE`` env → ``~/.zmem/store.sqlite``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Fire a convention nudge every N successful tool calls.
try:
    _INTERVAL = max(1, int(os.environ.get("ZMEM_CONVENTION_INTERVAL", "10")))
except ValueError:
    _INTERVAL = 10

# Counter key (monotonic per session).
_CONVENTION_COUNT_KEY = "hermes_convention_count_{session}"
# Pending-nudge flags the reflect hook (pre_llm_call) consumes.
_PENDING_CONVENTION_KEY = "hermes_pending_convention_{session}"
_PENDING_FAILURE_KEY = "hermes_pending_failure_{session}"
# One-time-per-session markers so we don't re-arm after the reflect hook delivers.
_FAILURE_CAPTURED_KEY = "hermes_failure_captured_{session}"


def _resolve_store_path() -> Path:
    explicit = os.environ.get("ZMEM_STORE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("ZMEM_DATA", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "store.sqlite"
    return Path.home() / ".zmem" / "store.sqlite"


def _assert_local_fs(path: Path) -> bool:
    """Reject UNC/network/OneDrive store paths (WAL-corruption guard).

    Mirrors zmem's host.py ``assert_local_fs`` — the provider gets it for free
    (it shells out to store.py), but hooks open sqlite directly and would
    otherwise bypass the guard. Returns True if safe, False if the path is
    network-mounted (the hook silently no-ops rather than corrupting the store).
    Best-effort: if host.py can't be imported, fall back to a UNC prefix check.
    """
    s = str(path)
    if s.startswith("\\\\") or s.startswith("//"):
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "skills" / "memory" / "scripts"))
        import host  # type: ignore[import-not-found]
        host.assert_local_fs(path)
        return True
    except Exception:
        # host.py unavailable or raised (network path) — trust the UNC check above
        return s.startswith("\\\\") is False and s.startswith("//") is False


def _connect() -> sqlite3.Connection | None:
    """Open the store. None if absent, missing the meta table, or on a
    network-mounted path (WAL-safety guard).

    Hooks never create the store — the memory provider's initialize() owns
    that. If the store isn't there (or is on a network share), the hook
    silently no-ops.
    """
    p = _resolve_store_path()
    if not p.is_file():
        return None
    if not _assert_local_fs(p):
        # Network/OneDrive path — refuse to open (WAL corruption risk).
        return None
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _counter_bump(conn: sqlite3.Connection, key: str) -> int:
    """Atomically increment a counter, returning the new value.

    INSERT ... ON CONFLICT ... DO UPDATE is race-safe across concurrent
    writers. Initial value '1' (not '0') so the Nth nudge arms on call N.
    """
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (key,),
    )
    conn.commit()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row else 0


def _meta_set_if_absent(conn: sqlite3.Connection, key: str) -> bool:
    """Set a flag key only if it doesn't already exist. Returns True if set.

    Atomic: INSERT OR IGNORE leaves an existing row untouched.
    """
    cur = conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, '1')", (key,))
    conn.commit()
    return cur.rowcount > 0


def _read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _session_id(payload: dict) -> str:
    sid = (payload.get("session_id") or "").strip()
    return sid or "unknown"


def _emit_empty() -> None:
    """Always silent — post_tool_call results are discarded by Hermes."""
    print("{}")


def main() -> int:
    payload = _read_payload()
    session = _session_id(payload)

    conn = _connect()
    if conn is None:
        _emit_empty()
        return 0

    try:
        extra = payload.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        status = (extra.get("status") or "").strip().lower()

        if status == "error":
            # Arm a failure nudge (once per session). The reflect hook delivers it.
            captured = _meta_set_if_absent(
                conn, _FAILURE_CAPTURED_KEY.format(session=session)
            )
            if captured:
                _meta_set_if_absent(
                    conn, _PENDING_FAILURE_KEY.format(session=session)
                )
            _emit_empty()
            return 0

        # Success: bump counter; every Nth call arms a convention nudge.
        count_key = _CONVENTION_COUNT_KEY.format(session=session)
        n = _counter_bump(conn, count_key)
        if n > 0 and n % _INTERVAL == 0:
            _meta_set_if_absent(
                conn, _PENDING_CONVENTION_KEY.format(session=session)
            )
        _emit_empty()
        return 0
    except (sqlite3.Error, ValueError) as exc:
        # Fail-open: lock contention past busy_timeout, disk errors, or a
        # corrupted meta.value (CAST yields NULL → int(None) raises ValueError)
        # must NOT crash the agent turn. Emit {} and exit 0, matching the
        # sibling hooks (reflect.py, verify.py) which guard the same way.
        sys.stderr.write(f"zmem-convention: sqlite/value error: {exc}\n")
        _emit_empty()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
