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

# Shared WAL-safety guard (single copy across the three Hermes hooks — #37 L25).
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)
from _zmem_hook_common import assert_local_fs as _assert_local_fs  # noqa: E402

_CONVENTION_COUNT_KEY = "hermes_convention_count_{session}"
_VERIFY_PROMPTED_KEY = "hermes_verify_prompted_{session}"


def _parse_convention_count(value) -> int:
    """Counter count from a meta value: canonical JSON {"count": N,
    "last_seen": <exact canonical UTC timestamp>} first, legacy bare integer
    digits fallback, 0 for anything malformed — including a corrupt
    last_seen, which carries no age evidence (issue #128, release 0.144.0;
    impl-review round 1 finding 3).

    LOCAL TWIN of schema_meta.parse_convention_count: this hook must also run
    on a box where the storelib tree is not importable (see _resolve_store_path's
    fallback), so it carries its own copy instead of importing. A lockstep test
    (tests/test_meta_table_bound.py VerifyHookParityTest.test_parser_lockstep_matrix)
    pins this equal to schema_meta's over a value matrix -- change both together."""
    if value is None:
        return 0
    text = value if isinstance(value, str) else str(value)
    stripped = text.strip()
    if stripped.isdigit():
        return int(stripped)
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return 0
    if not isinstance(obj, dict):
        return 0
    count = obj.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    last_seen = obj.get("last_seen")
    if not isinstance(last_seen, str) or not last_seen:
        return 0
    # Exact canonical shape + a strftime round-trip: strptime alone ACCEPTS
    # second=60 (which the epoch parser resolves as 0.0), so the round-trip
    # is what catches normalizable-but-unresolvable shapes (impl-review r2).
    import re as _re
    import time as _time
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", last_seen):
        return 0
    try:
        _parsed = _time.strptime(last_seen, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return 0
    # strptime accepts leap seconds (tm_sec 60/61) that strftime renders
    # back verbatim; the explicit range gates catch what the round-trip
    # cannot (impl-review r2, twin of schema_meta._valid_convention_last_seen).
    if not (0 <= _parsed.tm_hour <= 23 and 0 <= _parsed.tm_min <= 59
            and 0 <= _parsed.tm_sec <= 59):
        return 0
    if _time.strftime("%Y-%m-%dT%H:%M:%SZ", _parsed) != last_seen:
        return 0
    return count


def _resolve_store_path() -> Path:
    """Resolve the store path via the SAME authoritative resolver as the
    provider and store.py (host.resolve_store_path). Previously this hook
    hand-rolled a TRUNCATED copy that omitted CLAUDE/ZCODE_PLUGIN_DATA, so on
    plugin-data-dir boxes it resolved a nonexistent ~/.zmem/store.sqlite and
    silently no-op'd all session (#36 M10).

    Imports the real resolver when the scripts dir is reachable (the normal
    case). The inline fallback below is a BEST-EFFORT subset (the env-var chain
    + ~/.zmem) reached only if host.py itself is unimportable; it does NOT
    include host.py's legacy probes (~/.zcode/memory, _legacy_plugin_store) —
    if you need those on a broken-import box, fix the import path instead."""
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
        count = _parse_convention_count(row[0]) if row else 0
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
