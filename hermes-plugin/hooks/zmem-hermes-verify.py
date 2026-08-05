#!/usr/bin/env python3
"""Hermes shell hook: coding-stop reflect nudge (pre_verify).

Fires only on coding turns where the agent edited files and is about to
verify/finish (Hermes gates ``pre_verify`` on ``_turn_file_mutation_paths``,
so non-coding gateway sessions never reach this hook). Emits
``{"action": "continue", "message": "..."}`` to keep the agent going one more
turn with a reflection prompt — the Stop-reflect analogue from zmem's
ZCode/Claude Code integration. Bounded by ``agent.max_verify_nudges`` so
re-loops are inherently capped; no ``stop_hook_active`` guard needed.

Complementary to ``zmem-hermes-reflect.py`` (pre_llm_call), which covers ALL
surfaces. This hook provides the extra "keep the agent going" behavior that
only makes sense on a coding turn. Same store, same dedup keys, same logic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_CONVENTION_COUNT_KEY = "hermes_convention_count_{session}"
_VERIFY_PROMPTED_KEY = "hermes_verify_prompted_{session}"


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


def _emit_continue(message: str) -> None:
    """Emit a keep-going directive (pre_verify shape)."""
    print(json.dumps({"action": "continue", "message": message}))


def _emit_empty() -> None:
    print("{}")


def _verify_nudge(session: str) -> str:
    return (
        "ZMem reflect-before-stop: you're about to finish a coding turn. "
        "Before you do, consider whether you discovered anything worth "
        "capturing for future sessions — a gotcha, a convention, a corrected "
        "assumption. If so, capture it now by calling the zmem_add tool:\n"
        f'  zmem_add with type="lesson", content="<the lesson>", '
        f'signal="<test|reviewer|user|none>", source_ref="session:{session}"\n'
        "If nothing generalizable, finish the turn."
    )


def main() -> int:
    payload = _read_payload()
    session = _session_id(payload)

    conn = _connect()
    if conn is None:
        _emit_empty()
        return 0

    try:
        prompted_key = _VERIFY_PROMPTED_KEY.format(session=session)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (prompted_key,)
        ).fetchone()
        if row:
            _emit_empty()
            return 0

        # Only nudge if there's signal (tool calls happened this session).
        count_key = _CONVENTION_COUNT_KEY.format(session=session)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (count_key,)
        ).fetchone()
        count = int(row[0]) if row and str(row[0]).isdigit() else 0
        if count <= 0:
            _emit_empty()
            return 0

        # Already captured a lesson for this session? Don't nag.
        try:
            already = conn.execute(
                "SELECT 1 FROM memory WHERE source_ref = ? AND superseded_at IS NULL "
                "LIMIT 1",
                (f"session:{session}",),
            ).fetchone() is not None
        except sqlite3.Error:
            already = False
        if already:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (prompted_key,),
            )
            conn.commit()
            _emit_empty()
            return 0

        _emit_continue(_verify_nudge(session))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (prompted_key,),
        )
        conn.commit()
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(f"zmem-verify: sqlite error: {exc}\n")
        _emit_empty()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
