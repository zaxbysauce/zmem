"""Read-only, evidence-first local provenance resolution for issue #139."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from storelib.write import redact_text

MAX_CONTEXT = 20
MAX_SCAN_MATCHES = 50
MAX_HERMES_FILES = 50
MAX_HERMES_FILE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CLAUDE_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "summary", "tool", "progress",
    "file-history-snapshot", "queue-operation",
})


class SourceRefusal(RuntimeError):
    """A source boundary failure whose detail is safe to show to an operator."""


def _is_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def is_safe_regular_path(path: Path) -> bool:
    """Use lstat, including for dangling links, before opening a source."""
    try:
        if not os.path.lexists(path) or _is_reparse(path) or not path.is_file():
            return False
        current = path.parent
        while current != current.parent:
            if _is_reparse(current):
                return False
            current = current.parent
        return not _is_reparse(current)
    except OSError:
        return False


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _redact(text: str) -> str:
    try:
        return redact_text(text)[0]
    except Exception as exc:
        raise SourceRefusal("source unavailable") from exc


def _safe_ref(value: object) -> str:
    ref = str(value or "")
    if (not ref or ref.startswith(("/", "\\")) or "\\" in ref or
            _DRIVE_RE.match(ref) or "://" in ref or ".." in Path(ref).parts):
        raise SourceRefusal("source unavailable")
    if ":" in ref:
        if not re.fullmatch(r"(?:zcode:session/[A-Za-z0-9._-]+|hermes:state\.db/session/[A-Za-z0-9._-]+)", ref):
            raise SourceRefusal("source unavailable")
    elif Path(ref).name != ref:
        raise SourceRefusal("source unavailable")
    return ref


def _namespace() -> str:
    override = os.environ.get("ZMEM_NAMESPACE", "").strip()
    if override:
        return override
    import host
    return host.resolve_namespace(Path.cwd(), write_cache=False)


def _is_project_namespace(value: object) -> bool:
    return isinstance(value, str) and value.startswith("project:") and len(value) > len("project:")


def _source_namespace_aliases(conn: sqlite3.Connection, namespace: str) -> list[str]:
    """Return source-safe v5 compatibility aliases for ``namespace``."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='ns_migration_v5'"
        ).fetchone()
        migration_map = {}
        if row is not None:
            migration_map = json.loads(row[0])
            if (not isinstance(migration_map, dict) or
                    any(not isinstance(old, str) or not isinstance(new, str)
                        for old, new in migration_map.items())):
                raise SourceRefusal("source unavailable")
    except SourceRefusal:
        raise
    except (RecursionError, TypeError, ValueError, sqlite3.Error) as exc:
        raise SourceRefusal("source unavailable") from exc

    # Preserve the caller's exact scope. Compatibility applies only between
    # valid project namespaces, never to global or other legacy scopes.
    if not _is_project_namespace(namespace):
        return [namespace]
    aliases = [namespace]
    if namespace in migration_map:
        candidate = migration_map[namespace]
        if _is_project_namespace(candidate):
            aliases.append(candidate)
    else:
        aliases.extend(old for old, new in migration_map.items()
                       if new == namespace and _is_project_namespace(old))
    return aliases


def _memory(conn: sqlite3.Connection, memory_id: str) -> tuple[sqlite3.Row, str]:
    namespace = _namespace()
    aliases = _source_namespace_aliases(conn, namespace)
    row = conn.execute(
        "SELECT id,namespace,source_ref FROM memory WHERE id=? AND namespace IN (" +
        ",".join("?" for _ in aliases) + ")", [memory_id, *aliases]).fetchone()
    if row is None:
        raise SourceRefusal("source unavailable")
    return row, namespace


def _evidence(conn: sqlite3.Connection, memory_id: str) -> list[sqlite3.Row]:
    """An orphan association is an evidence failure, never a fallback trigger."""
    rows = conn.execute(
        "SELECT me.evidence_id,e.id,e.session_id,e.lane,e.ts,e.excerpt,e.ref_path,e.ref_offset "
        "FROM memory_evidence me LEFT JOIN evidence e ON e.id=me.evidence_id "
        "WHERE me.memory_id=? ORDER BY me.evidence_id", (memory_id,)).fetchall()
    if any(row[1] is None for row in rows):
        raise SourceRefusal("source unavailable")
    if not rows:
        return []
    anchors = {(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows}
    if len(anchors) != 1:
        raise SourceRefusal("source unavailable")
    return rows


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False


def _hermes_home() -> Path:
    """Resolve the trusted Hermes home without inventing a platform default.

    An explicit operator override remains usable when Hermes is not importable.
    When it is unset, only Hermes itself can establish the canonical home: its
    resolver includes platform and context-local profile selection.  Refuse
    rather than guessing, because a guessed root cannot prove that a canonical
    database is absent before the JSONL fallback is considered.
    """
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit)
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    except Exception as exc:
        raise SourceRefusal("hermes_db_unavailable") from exc
    if not isinstance(home, Path):
        raise SourceRefusal("hermes_db_unavailable")
    return home


def _configured_file(ref: str) -> tuple[Path, str]:
    if ref == "raw_memories.md" or ref.endswith(".db"):
        raise SourceRefusal("source unavailable")
    # Explicit local knobs are approved roots.  Their bytes still must satisfy
    # the strict host parser below; the reference selects no sibling path.
    for key, kind in (("ZMEM_TRANSCRIPT", "claude_transcript"),
                      ("ZMEM_AGENT_TRANSCRIPT", "claude_transcript"),
                      ("ZMEM_CODEX_MEMORY", "codex_session")):
        raw = os.environ.get(key, "").strip()
        if raw:
            path = Path(raw)
            if path.name == ref and is_safe_regular_path(path):
                return path, kind
    if ref == "MEMORY.md":
        default = Path.home() / ".codex" / "MEMORY.md"
        if is_safe_regular_path(default):
            return default, "codex_session"
    home = _hermes_home()
    sessions = Path(os.environ.get("ZMEM_HERMES_SESSIONS", "").strip() or home / "sessions")
    if not os.path.lexists(home / "state.db"):
        candidate = sessions / ref
        if _inside(sessions, candidate) and is_safe_regular_path(candidate):
            try:
                files = sorted(
                    (p for p in sessions.glob("*.jsonl") if is_safe_regular_path(p)),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )[:MAX_HERMES_FILES]
                if candidate in files and candidate.stat().st_size <= MAX_HERMES_FILE_BYTES:
                    return candidate, "hermes_session"
            except OSError:
                pass
    raise SourceRefusal("source unavailable")


def _content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
    return ""


def _records(path: Path, kind: str) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        # Read at most one byte beyond the contract.  A size preflight alone
        # races a growing transcript and would still permit an unbounded read.
        with path.open("rb") as handle:
            raw = handle.read(MAX_SOURCE_FILE_BYTES + 1)
        if len(raw) > MAX_SOURCE_FILE_BYTES:
            raise OSError("source exceeds byte bound")
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceRefusal("source unavailable") from exc
    if kind == "codex_session":
        if path.name != "MEMORY.md" or not re.search(r"^#{1,3}\s+", raw.decode("utf-8"), re.M):
            raise SourceRefusal("source unavailable")
        return raw, [{"start": 0, "end": len(raw), "session": None, "time": None,
                      "turn": "0", "text": raw.decode("utf-8"), "raw": raw.decode("utf-8")}]
    records: list[dict[str, Any]] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceRefusal("source unavailable") from exc
        if not isinstance(obj, dict):
            raise SourceRefusal("source unavailable")
        if kind == "claude_transcript":
            if obj.get("type") not in _CLAUDE_RECORD_TYPES:
                raise SourceRefusal("source unavailable")
            message = obj.get("message")
            content = _content(message.get("content")) if isinstance(message, dict) else _content(obj.get("content"))
            session, stamp = obj.get("sessionId"), obj.get("timestamp")
        else:
            content, session, stamp = _content(obj.get("content")), obj.get("session_id"), obj.get("timestamp")
            if not isinstance(obj.get("role"), str):
                raise SourceRefusal("source unavailable")
        if not isinstance(session, str) or not session or not isinstance(stamp, str) or not stamp:
            raise SourceRefusal("source unavailable")
        records.append({"start": start, "end": offset, "session": session, "time": stamp,
                        "turn": str(len(records)), "text": content, "raw": line.decode("utf-8")})
    if not records:
        raise SourceRefusal("source unavailable")
    return raw, records


def _file_window(path: Path, kind: str, *, anchor: sqlite3.Row | None, context: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    raw, records = _records(path, kind)
    if anchor is None:
        sessions = {r["session"] for r in records}
        if len(sessions) > 1:
            raise SourceRefusal("source unavailable")
        center = 0
    else:
        candidates = [i for i, r in enumerate(records) if r["session"] == anchor[2]]
        if anchor[4]:
            candidates = [i for i in candidates if records[i]["time"] == anchor[4]]
        if anchor[7] is not None:
            candidates = [i for i in candidates if records[i]["start"] <= anchor[7] < records[i]["end"]]
        if len(candidates) != 1:
            raise SourceRefusal("source unavailable")
        center = candidates[0]
    # A same-session row after an intervening session is a separate physical
    # run.  Never join those ranges: that would display the intervening bytes
    # while falsely reporting one contiguous original byte window.
    run_start = center
    while run_start > 0 and records[run_start - 1]["session"] == records[center]["session"]:
        run_start -= 1
    run_end = center + 1
    while run_end < len(records) and records[run_end]["session"] == records[center]["session"]:
        run_end += 1
    session_rows = records[run_start:run_end]
    local = center - run_start
    lo, hi = max(0, local - context), min(len(session_rows), local + context + 1)
    selected = session_rows[lo:hi]
    try:
        shown = _redact(_normalise(raw[selected[0]["start"]:selected[-1]["end"]].decode("utf-8")))
    except UnicodeDecodeError as exc:
        raise SourceRefusal("source unavailable") from exc
    return shown, {"session_id": records[center]["session"], "turn_start": selected[0]["turn"],
                   "turn_end": selected[-1]["turn"], "byte_start": selected[0]["start"],
                   "byte_end": selected[-1]["end"], "returned": len(selected),
                   "truncated": len(selected) < context * 2 + 1, "kind": kind}, session_rows


def _external_db(path: Path) -> sqlite3.Connection:
    if not is_safe_regular_path(path):
        raise SourceRefusal("source unavailable")
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError as exc:
        raise SourceRefusal("source unavailable") from exc
    if len(header) >= 19 and header[18] == 2:
        if not all(is_safe_regular_path(Path(str(path) + suffix)) for suffix in ("-wal", "-shm")):
            raise SourceRefusal("source unavailable")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        conn.execute("PRAGMA query_only=1")
        return conn
    except sqlite3.Error as exc:
        raise SourceRefusal("source unavailable") from exc


def _zcode(ref: str, anchor: sqlite3.Row, context: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    db = Path(os.environ.get("ZMEM_ZCODE_DB", "")); session = ref.split("/", 1)[1]
    if anchor[2] != session or not isinstance(anchor[5], str) or not anchor[5]:
        raise SourceRefusal("source unavailable")
    conn = _external_db(db)
    try:
        rows = conn.execute("SELECT p.message_id,p.sequence,p.data FROM part p WHERE p.session_id=? ORDER BY p.sequence", (session,)).fetchall()
    except sqlite3.Error as exc:
        raise SourceRefusal("source unavailable") from exc
    finally:
        conn.close()
    try:
        parsed = []
        for row in rows:
            payload = json.loads(row[2])
            if not isinstance(payload, dict):
                raise ValueError("part payload is not an object")
            parsed.append((row[0], row[1], _content(payload.get("text", payload.get("content", "")))))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError) as exc:
        raise SourceRefusal("source unavailable") from exc
    candidates = [i for i, row in enumerate(parsed) if anchor[5] in _redact(_normalise(row[2]))]
    if len(candidates) != 1:
        raise SourceRefusal("source unavailable")
    idx = candidates[0]; lo, hi = max(0, idx-context), min(len(parsed), idx+context+1)
    excerpt = _redact(_normalise("\n".join(row[2] for row in parsed[lo:hi])))
    scan_rows = [{"turn": row[0], "text": row[2], "raw": row[2], "start": None, "end": None} for row in parsed]
    return excerpt, {"session_id": session, "turn_start": parsed[lo][0], "turn_end": parsed[hi-1][0], "byte_start": None, "byte_end": None, "returned": hi-lo, "truncated": (hi-lo) < context*2+1, "kind": "zcode_session"}, scan_rows


def _hermes(ref: str, anchor: sqlite3.Row, context: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    home = _hermes_home()
    db_path, session = home / "state.db", ref.rsplit("/", 1)[1]
    if anchor[2] != session or not isinstance(anchor[5], str) or not anchor[5]:
        raise SourceRefusal("hermes_db_unavailable")
    if not all(is_safe_regular_path(p) for p in (db_path, Path(str(db_path)+"-wal"), Path(str(db_path)+"-shm"))):
        raise SourceRefusal("hermes_db_unavailable")
    try:
        from hermes_state import SessionDB
        db = SessionDB(db_path=db_path, read_only=True)
        try:
            session = db.resolve_resume_session_id(session)
            available = db.get_messages(session)
            candidates = [r for r in available if anchor[5] in _redact(_normalise(_content(r.get("content", r.get("text", "")))))]
            if len(candidates) != 1 or not isinstance(candidates[0].get("id"), int):
                raise SourceRefusal("hermes_db_unavailable")
            window = db.get_messages_around(session, around_message_id=candidates[0]["id"], window=context)
        finally:
            db.close()
    except SourceRefusal:
        raise
    except Exception as exc:
        raise SourceRefusal("hermes_db_unavailable") from exc
    rows = window.get("window", []) if isinstance(window, dict) else []
    if not rows or any(not isinstance(r, dict) or not isinstance(r.get("id"), int) for r in rows):
        raise SourceRefusal("hermes_db_unavailable")
    excerpt = _redact(_normalise("\n".join(_content(r.get("content", r.get("text", ""))) for r in rows)))
    before, after = int(window.get("messages_before", 0)), int(window.get("messages_after", 0))
    scan_rows = [{"turn": row["id"], "text": _content(row.get("content", row.get("text", ""))), "raw": _content(row.get("content", row.get("text", ""))), "start": None, "end": None} for row in available]
    return excerpt, {"session_id": session, "turn_start": rows[0]["id"], "turn_end": rows[-1]["id"], "byte_start": None, "byte_end": None, "returned": len(rows), "truncated": before < context or after < context, "kind": "hermes_session"}, scan_rows


def _resolved(conn: sqlite3.Connection, memory_id: str, context: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(context, int) or isinstance(context, bool) or not 0 <= context <= MAX_CONTEXT:
        raise ValueError("--context must be between 0 and 20")
    memory, namespace = _memory(conn, memory_id); evidence = _evidence(conn, memory_id); anchor = evidence[0] if evidence else None
    ref = _safe_ref(anchor[6] if anchor else memory[2])
    if ref.startswith("zcode:"):
        if anchor is None: raise SourceRefusal("source unavailable")
        excerpt, detail, scan_rows = _zcode(ref, anchor, context)
    elif ref.startswith("hermes:"):
        if anchor is None: raise SourceRefusal("hermes_db_unavailable")
        excerpt, detail, scan_rows = _hermes(ref, anchor, context)
    else:
        path, kind = _configured_file(ref); excerpt, detail, scan_rows = _file_window(path, kind, anchor=anchor, context=context)
        detail["kind"] = kind if kind == "hermes_session" else ("evidence" if anchor else kind)
    result = {"memory_id": str(memory[0]), "namespace": str(memory[1]), "source_ref": str(memory[2]), "evidence_ids": [str(r[0]) for r in evidence], "source_kind": detail["kind"], "source_path": ref, "session_id": detail["session_id"], "capture_time": anchor[4] if anchor else None, "turn_start": detail["turn_start"], "turn_end": detail["turn_end"], "byte_start": detail["byte_start"], "byte_end": detail["byte_end"], "context_requested": context, "context_returned": detail["returned"], "truncated": bool(detail["truncated"]), "truncated_reason": "context_bound" if detail["truncated"] else None, "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(), "excerpt": excerpt}
    return result, scan_rows


def show(conn: sqlite3.Connection, memory_id: str, context: int = 2) -> dict[str, Any]:
    return _resolved(conn, memory_id, context)[0]


def scan(conn: sqlite3.Connection, memory_id: str, needle: str) -> dict[str, Any]:
    if not isinstance(needle, str) or not needle:
        raise ValueError("--needle must not be empty")
    # The output schema includes needle, so accepting a secret-shaped literal
    # would echo it verbatim. Refuse rather than weakening the output contract.
    if _redact(needle) != needle:
        raise SourceRefusal("source unavailable")
    shown, rows = _resolved(conn, memory_id, 0)
    matches: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        original = str(row["raw"])
        normal = _normalise(original)
        # Native stores do not preserve original transcript byte offsets. File
        # records do, so calculate offsets within each source record.
        boundaries = [0]
        source_index = byte_count = 0
        while source_index < len(original):
            if original[source_index:source_index + 2] == "\r\n":
                source_index += 2; byte_count += 2
            else:
                char = original[source_index]
                source_index += 1; byte_count += len(char.encode("utf-8"))
            boundaries.append(byte_count)
        at = 0
        while True:
            pos = normal.find(needle, at)
            if pos < 0:
                break
            total += 1
            if len(matches) < MAX_SCAN_MATCHES:
                if row["start"] is None:
                    byte_start = byte_end = None
                else:
                    byte_start = int(row["start"]) + boundaries[pos]
                    byte_end = int(row["start"]) + boundaries[pos + len(needle)]
                # Redact the whole record before slicing around a match. A
                # partial secret can evade a whole-secret pattern if clipping
                # happens first.
                safe_record = _redact(normal)
                safe_pos = safe_record.find(needle)
                if safe_pos < 0:
                    safe_pos = 0
                matches.append({"turn": row["turn"], "byte_start": byte_start,
                                "byte_end": byte_end,
                                "excerpt": safe_record[max(0, safe_pos-80):min(len(safe_record), safe_pos+len(needle)+80)]})
            at = pos + max(1, len(needle))
    return {"memory_id": memory_id, "session_id": shown["session_id"], "source_path": shown["source_path"], "needle": needle, "matches": matches, "match_count": total, "truncated": total > MAX_SCAN_MATCHES, "truncated_reason": "match_limit" if total > MAX_SCAN_MATCHES else None}


def source_show(conn: sqlite3.Connection, *, memory_id: str, context: int = 2) -> dict[str, Any]:
    """Public, keyword-only source show API retained by ``storelib``."""
    return show(conn, memory_id, context)


def source_scan(conn: sqlite3.Connection, *, memory_id: str, needle: str) -> dict[str, Any]:
    """Public, keyword-only literal scan API retained by ``storelib``."""
    return scan(conn, memory_id, needle)
