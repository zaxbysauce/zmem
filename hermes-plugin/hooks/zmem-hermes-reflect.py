#!/usr/bin/env python3
"""Hermes shell hook: reflect nudge DELIVERY (pre_llm_call) — universal surface.

This is the DELIVERY side of the reflection loop. The convention hook
(``post_tool_call``, observational — its results are discarded by Hermes)
records pending-nudge flags in zmem's ``meta`` table; THIS hook fires on
``pre_llm_call`` (whose ``{"context": ...}`` results ARE consumed and injected
into the user message) and delivers them.

Priority (first match wins, each is one-shot per session):
  1. pending failure nudge → emit failure-capture context, clear flag
  2. pending convention nudge → emit convention-capture context, clear flag
  3. generic reflect: if convention counter > 0 and no lesson captured yet,
     emit a reflect nudge (once per session)

Works on ALL surfaces (CLI, TUI, Telegram, Discord, Slack) because
``pre_llm_call`` context is injected into the user message regardless of
platform — unlike ``pre_verify`` which Hermes gates on file mutations.

Emits raw JSON on stdout: ``{"context": "..."}`` to inject, ``{}`` silent.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_CONVENTION_COUNT_KEY = "hermes_convention_count_{session}"
_PENDING_CONVENTION_KEY = "hermes_pending_convention_{session}"
_PENDING_FAILURE_KEY = "hermes_pending_failure_{session}"
# Cleared alongside _PENDING_FAILURE_KEY on delivery so a LATER failure in the
# same session can re-arm (without this, the one-shot _FAILURE_CAPTURED_KEY in
# the convention hook would suppress every failure after the first delivered).
_FAILURE_CAPTURED_KEY = "hermes_failure_captured_{session}"
_REFLECT_PROMPTED_KEY = "hermes_reflect_prompted_{session}"


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

    Mirrors zmem's host.py assert_local_fs. Best-effort: falls back to a UNC
    prefix check if host.py can't be imported.
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
        return True


def _connect() -> sqlite3.Connection | None:
    p = _resolve_store_path()
    if not p.is_file():
        return None
    if not _assert_local_fs(p):
        return None
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


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


def _emit_context(text: str) -> None:
    print(json.dumps({"context": text}))


def _emit_empty() -> None:
    print("{}")


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_clear(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


def _has_lesson(conn: sqlite3.Connection, session: str) -> bool:
    """Has any (non-superseded) memory been captured for this session?"""
    try:
        return conn.execute(
            "SELECT 1 FROM memory WHERE source_ref = ? AND superseded_at IS NULL "
            "LIMIT 1",
            (f"session:{session}",),
        ).fetchone() is not None
    except sqlite3.Error:
        # Schema drift on an old/foreign store — treat as "no lesson" rather
        # than crashing the agent turn.
        return False


def _convention_nudge(session: str) -> str:
    return (
        "ZMem convention capture: you just completed several tool calls. If you "
        "discovered a reusable convention, pattern, or workaround during this "
        "session — something that would help a future session facing a similar "
        "task — capture it now by calling the zmem_add tool:\n"
        f'  zmem_add with type="convention", content="<the lesson>", '
        f'signal="<test|reviewer|user|none>", source_ref="session:{session}"\n'
        "If nothing generalizable applies, do nothing."
    )


def _failure_nudge(session: str) -> str:
    # The detailed tool+error context was available at post_tool_call time but
    # isn't forwarded here; the nudge is still actionable (the model saw the
    # failure in the same turn).
    return (
        f"ZMem auto-capture: a tool failed earlier this session "
        f"(source_ref=session:{session}). If a generalizable lesson can be "
        "derived from that failure — a gotcha, a misconfiguration, a wrong "
        "assumption — capture it now by calling the zmem_add tool:\n"
        f'  zmem_add with type="lesson", content="<the lesson, with the error '
        f'context>", signal="none", source_ref="session:{session}"\n'
        "If the failure was transient or not generalizable, do nothing."
    )


def _reflect_nudge(session: str, count: int) -> str:
    return (
        "ZMem reflection: this session has completed "
        f"{count} tool call(s) but no lesson has been captured yet "
        f"(source_ref=session:{session}). If you learned something this session "
        "that a future session would benefit from — a workaround, a corrected "
        "assumption, a project convention — capture it now by calling the "
        "zmem_add tool:\n"
        f'  zmem_add with type="lesson", content="<the lesson>", '
        f'signal="<test|reviewer|user|none>", source_ref="session:{session}"\n'
        "If nothing generalizable, do nothing."
    )


def main() -> int:
    payload = _read_payload()
    session = _session_id(payload)

    conn = _connect()
    if conn is None:
        _emit_empty()
        return 0

    try:
        # Priority 1: pending failure nudge (armed by the convention hook).
        fail_key = _PENDING_FAILURE_KEY.format(session=session)
        if _meta_get(conn, fail_key):
            _emit_context(_failure_nudge(session))
            _meta_clear(conn, fail_key)
            # Also clear the one-shot captured marker so a subsequent failure
            # in the same session can re-arm a nudge. Without this, the
            # convention hook's _FAILURE_CAPTURED_KEY would suppress every
            # failure after the first delivered one.
            _meta_clear(conn, _FAILURE_CAPTURED_KEY.format(session=session))
            return 0

        # Priority 2: pending convention nudge.
        conv_key = _PENDING_CONVENTION_KEY.format(session=session)
        if _meta_get(conn, conv_key):
            _emit_context(_convention_nudge(session))
            _meta_clear(conn, conv_key)
            return 0

        # Priority 3: generic reflect — once per session if there's substantial
        # signal (>= the convention interval) but no lesson captured yet. This
        # catches the case where the session is winding down and the convention
        # nudge was already delivered+cleared, or the interval boundary was
        # never hit. Gated on the interval so it doesn't fire after a single
        # tool call.
        try:
            _interval = max(1, int(os.environ.get("ZMEM_CONVENTION_INTERVAL", "10")))
        except ValueError:
            _interval = 10
        prompted_key = _REFLECT_PROMPTED_KEY.format(session=session)
        if _meta_get(conn, prompted_key):
            _emit_empty()
            return 0
        count_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (_CONVENTION_COUNT_KEY.format(session=session),),
        ).fetchone()
        count = int(count_row[0]) if count_row and str(count_row[0]).isdigit() else 0
        if count < _interval:
            _emit_empty()
            return 0
        if _has_lesson(conn, session):
            # Already captured — mark prompted so we don't re-query every turn.
            _meta_clear(conn, prompted_key)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (prompted_key,),
            )
            conn.commit()
            _emit_empty()
            return 0
        _emit_context(_reflect_nudge(session, count))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (prompted_key,),
        )
        conn.commit()
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(f"zmem-reflect: sqlite error: {exc}\n")
        _emit_empty()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
