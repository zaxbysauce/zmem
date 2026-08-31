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

# Shared WAL-safety guard + store resolver (single copy across the three Hermes
# hooks — #37 L25). The hook's own dir must be on sys.path for the sibling
# import to resolve when run as a standalone script.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)
from _zmem_hook_common import assert_local_fs as _assert_local_fs  # noqa: E402

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
    """Resolve the store path via the SAME authoritative resolver as the
    provider and store.py (host.resolve_store_path). Previously this hook
    hand-rolled a TRUNCATED copy that omitted CLAUDE/ZCODE_PLUGIN_DATA, so on
    plugin-data-dir boxes it resolved a nonexistent ~/.zmem/store.sqlite and
    silently no-op'd all session (#36 M10).

    Imports the real resolver when the scripts dir is reachable. In a
    repo/symlink/junction install `__file__`'s `parents[2]` finds it; in a
    COPY install (`cp -r hermes-plugin …`, README-documented) the copy has no
    `skills/` tree, so we also probe `$ZMEM_HOME/skills/memory/scripts` (the
    env var copy users MUST set) before giving up (#36 M10 / cubic-3,5,8).

    The inline fallback below is a BEST-EFFORT subset (the env-var chain
    + ~/.zmem) reached only if host.py itself is unimportable from any probe;
    it does NOT include host.py's legacy probes (~/.zcode/memory,
    _legacy_plugin_store)."""
    _rel = Path("skills") / "memory" / "scripts"
    candidates = [
        Path(__file__).resolve().parents[2] / _rel,            # in-tree (repo/symlink/junction)
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,  # copy install
    ]
    for _scripts_dir in candidates:
        if (_scripts_dir / "host.py").is_file():
            sys.path.insert(0, str(_scripts_dir))
            try:
                import host  # type: ignore  # noqa: F811
                return host.resolve_store_path()
            except Exception:
                pass
    # Inline fallback: the env-var chain + ~/.zmem (host.py's legacy probes
    # omitted — see docstring).
    explicit = os.environ.get("ZMEM_STORE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        d = os.environ.get(var, "").strip()
        if d:
            return Path(d).expanduser() / "store.sqlite"
    return Path.home() / ".zmem" / "store.sqlite"


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


def _op_descriptor(payload: dict, extra: dict) -> str:
    """Best-effort operation descriptor for the query-context ring
    (issue #88 / #85 direction 2). post_tool_call payload fields beyond
    session_id / extra.status are gateway-defined (the emitter lives in the
    Hermes gateway's model_tools, not in this repo), so probe the plausible
    shapes defensively. append_ops_ring does the allowlisting — only the
    allowlisted tokens ever reach disk (spec B)."""
    for source in (extra, payload):
        for key in ("command", "cmd"):
            v = source.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        ti = source.get("tool_input")
        if isinstance(ti, dict):
            for key in ("command", "file_path", "notebook_path", "path"):
                v = ti.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def _append_query_context(store_path: Path, session: str,
                          payload: dict, extra: dict) -> None:
    """Record this tool event on the per-session ops ring (#85 spec B:
    prior-turn operation context for the prefetch query). The ring is the
    SAME one the coding hosts' convention-capture writes; the Hermes
    provider's prefetch() composes it into the next turn's query.
    Fail-open: ring health never affects this hook."""
    try:
        # Review PRR-91-004: the kill switch gates COLLECTION too.
        if os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0":
            return
        desc = _op_descriptor(payload, extra)
        if not desc:
            return
        _rel = Path("skills") / "memory" / "scripts"
        candidates = [
            Path(__file__).resolve().parents[2] / _rel,
            Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,
        ]
        scripts_dir = next((c for c in candidates if (c / "host.py").is_file()),
                           None)
        if scripts_dir is None:
            return
        sys.path.insert(0, str(scripts_dir / "storelib"))
        import ops_tokens  # noqa: E402  (stdlib-only, like this hook)
        ops_tokens.append_ops_ring(str(store_path.parent), session,
                                   str(extra.get("tool") or payload.get(
                                       "tool_name") or ""), desc)
    except Exception:
        pass


def _emit_empty() -> None:
    """Always silent — post_tool_call results are discarded by Hermes."""
    print("{}")


def main() -> int:
    payload = _read_payload()
    session = _session_id(payload)
    extra = payload.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}

    # Issue #88 / #85 direction 2: record this tool event on the query-
    # context ring BEFORE the store connect — the ring only needs the store
    # PATH (its sibling ops/ dir), so a missing/unwritable store must not
    # lose the verb. Fail-open either way.
    try:
        _append_query_context(_resolve_store_path(), session, payload, extra)
    except Exception:
        pass

    conn = _connect()
    if conn is None:
        _emit_empty()
        return 0

    try:
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
