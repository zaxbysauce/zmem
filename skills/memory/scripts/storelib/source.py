"""Read-only, evidence-first local provenance resolution for issue #139."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from redaction import _redact_secret_like_text_with_positions
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


def _source_authorized_namespaces(conn: sqlite3.Connection) -> frozenset[str]:
    """Namespaces the read-only source resolver can authorize right now.

    Recall source hints call this exact helper so their advertised commands do
    not promise access that ``source`` will refuse.
    """
    return frozenset(_source_namespace_aliases(conn, _namespace()))


def _memory(conn: sqlite3.Connection, memory_id: str) -> tuple[sqlite3.Row, str]:
    namespace = _namespace()
    aliases = _source_namespace_aliases(conn, namespace)
    row = conn.execute(
        "SELECT id,namespace,source_ref FROM memory WHERE id=? AND namespace IN (" +
        ",".join("?" for _ in aliases) + ")", [memory_id, *aliases]).fetchone()
    if row is None:
        raise SourceRefusal("source unavailable")
    return row, namespace


def _evidence(
    conn: sqlite3.Connection, memory_id: str, evidence_id: str | None = None,
) -> tuple[list[sqlite3.Row], sqlite3.Row | None]:
    """Resolve a memory's evidence associations and the anchor to use.

    Issue #512: the FULL association list is always returned (never filtered
    to the selection); ``evidence_id`` only picks WHICH anchor resolves. The
    #139 invariants hold: an orphan association is an evidence failure, never
    a fallback trigger; a multi-anchor memory without an explicit selector
    refuses with a structured ambiguity naming every candidate (never a
    silent pick); a selector that is not associated with the memory refuses.
    A single-anchor memory keeps the historical behavior (its sole row is the
    anchor); an evidence-less memory returns ([], None).
    """
    rows = conn.execute(
        "SELECT me.evidence_id,e.id,e.session_id,e.lane,e.ts,e.excerpt,e.ref_path,e.ref_offset "
        "FROM memory_evidence me LEFT JOIN evidence e ON e.id=me.evidence_id "
        "WHERE me.memory_id=? ORDER BY me.evidence_id", (memory_id,)).fetchall()
    if any(row[1] is None for row in rows):
        raise SourceRefusal("source unavailable")
    if not rows:
        return [], None
    anchors = {(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows}
    if evidence_id is not None:
        # Membership FIRST (review 4.5 round 1): even when every row shares
        # one anchor tuple, an unassociated selector must refuse — repair
        # reports the requested id, so its provenance must be associated.
        for row in rows:
            if str(row[0]) == str(evidence_id):
                return rows, row
        named = ", ".join(str(r[0]) for r in rows)
        raise SourceRefusal(
            f"evidence id {evidence_id!r} is not associated with this memory "
            f"(associated: {named})")
    if len(anchors) == 1:
        return rows, rows[0]
    named = ", ".join(str(r[0]) for r in rows)
    raise SourceRefusal(
        "ambiguous anchors: pass --evidence-id one of " + named)


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


_SOURCE_ROOT_ENV = "ZMEM_SOURCE_ROOT"


def _source_root() -> Path:
    """The trusted project-file root for #512 passage verification."""
    raw = os.environ.get(_SOURCE_ROOT_ENV, "").strip()
    if not raw:
        raise SourceRefusal("source unavailable")
    return Path(raw)


def _safe_ref_allowed(ref: str) -> bool:
    """A bare relative filename that _safe_ref would accept (no separators,
    drives, schemes, or parent segments) — the shape a project-file ref must
    have."""
    try:
        _safe_ref(ref)
    except SourceRefusal:
        return False
    return True


def _project_file_path(ref: str) -> Path:
    """Resolve a bare relative evidence ref under the trusted source root."""
    root = _source_root()
    candidate = root / ref
    try:
        if candidate.parent.resolve(strict=True) != root.resolve(strict=True):
            raise SourceRefusal("source unavailable")
    except OSError as exc:
        raise SourceRefusal("source unavailable") from exc
    if not is_safe_regular_path(candidate):
        raise SourceRefusal("source unavailable")
    try:
        if candidate.stat().st_size > MAX_SOURCE_FILE_BYTES:
            raise SourceRefusal("source unavailable")
    except OSError as exc:
        raise SourceRefusal("source unavailable") from exc
    return candidate


def _project_file_lines(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceRefusal("source unavailable") from exc
    return _normalise(raw.decode("utf-8")).splitlines()


def _verification_outcome(
    anchor: sqlite3.Row, ref_path: str,
) -> dict[str, Any] | None:
    """Verify the selected evidence row's quoted passage against the CURRENT
    bytes of its quoted project file (issue #512 AC3).

    Outcome vocabulary: ``exact`` (verbatim line present at the recorded
    0-based ``ref_offset``), ``relocated`` (present exactly once elsewhere),
    ``ambiguous`` (multiple distinct occurrences), ``missing`` (absent).
    Returns None for anchors that do not resolve to a project file under
    ZMEM_SOURCE_ROOT — no fabricated outcome for transcript/zcode/hermes
    kinds. Outcomes are REPORTS; only trust-boundary failures refuse.
    """
    try:
        path = _project_file_path(ref_path)
    except SourceRefusal:
        return None
    lines = _project_file_lines(path)
    excerpt = str(anchor[5] or "")
    # R4.5-3 + review 4.5 r2: verify the COMPLETE quoted passage — every
    # line of the excerpt (internal blank lines included) as one contiguous
    # block, whitespace-insensitive per line — so a change to ANY quoted
    # line, or to the block's line sequence, affects the outcome.
    quoted_lines = [ln.strip() for ln in excerpt.splitlines()]
    if not quoted_lines:
        return None
    stripped = [ln.strip() for ln in lines]
    n = len(quoted_lines)
    hits = [i for i in range(len(stripped) - n + 1)
            if stripped[i:i + n] == quoted_lines]
    selected = anchor[7] if anchor[7] is not None else -1
    if not hits:
        outcome = "missing"
    elif len(hits) > 1:
        outcome = "ambiguous"
    elif selected >= 0 and hits[0] == selected:
        outcome = "exact"
    else:
        outcome = "relocated"
    return {"outcome": outcome, "evidence_id": str(anchor[0])}


def _project_file_window(path: Path, anchor: sqlite3.Row, context: int) -> str:
    """Render the project file's lines around the recorded offset (issue
    #512 AC3): normalized, redacted, context-windowed."""
    lines = _project_file_lines(path)
    offset = anchor[7] if anchor[7] is not None else 0
    lo = max(0, offset - context)
    hi = min(len(lines), offset + context + 1)
    window = "\n".join(lines[lo:hi])
    shown, _count, _positions = _redact_secret_like_text_with_positions(window, [])
    return shown


def _resolved(
    conn: sqlite3.Connection, memory_id: str, context: int,
    evidence_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(context, int) or isinstance(context, bool) or not 0 <= context <= MAX_CONTEXT:
        raise ValueError("--context must be between 0 and 20")
    memory, namespace = _memory(conn, memory_id)
    evidence, anchor = _evidence(conn, memory_id, evidence_id=evidence_id)
    ref = _safe_ref(anchor[6] if anchor else memory[2])
    verification = None
    resolved_project_file = False
    excerpt = ""
    detail: dict[str, Any] = {}
    scan_rows: list[dict[str, Any]] = []
    if anchor is not None:
        raw_ref = str(anchor[6] or "")
        if os.environ.get(_SOURCE_ROOT_ENV, "").strip() and _safe_ref_allowed(raw_ref):
            try:
                path = _project_file_path(raw_ref)
            except SourceRefusal:
                path = None
            if path is not None:
                excerpt = _project_file_window(path, anchor, context)
                detail = {"session_id": None, "turn_start": None,
                          "turn_end": None, "byte_start": None,
                          "byte_end": None, "returned": 1, "truncated": False,
                          "kind": "project_file"}
                verification = _verification_outcome(anchor, raw_ref)
                resolved_project_file = True
    if not resolved_project_file:
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
    if verification is not None:
        result["verification"] = verification
    return result, scan_rows


def show(
    conn: sqlite3.Connection, memory_id: str, context: int = 2,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    return _resolved(conn, memory_id, context, evidence_id=evidence_id)[0]


def _normalise_with_byte_boundaries(original: str) -> tuple[str, list[int]]:
    """Normalise line endings while retaining original UTF-8 byte boundaries."""
    pieces: list[str] = []
    boundaries = [0]
    source_index = byte_count = 0
    while source_index < len(original):
        if original[source_index:source_index + 2] == "\r\n":
            pieces.append("\n")
            source_index += 2
            byte_count += 2
        else:
            char = original[source_index]
            pieces.append("\n" if char == "\r" else char)
            source_index += 1
            byte_count += len(char.encode("utf-8"))
        boundaries.append(byte_count)
    return "".join(pieces), boundaries


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
        normal, boundaries = _normalise_with_byte_boundaries(original)
        # Native stores do not preserve original transcript byte offsets. File
        # records do, so calculate offsets within each source record.
        # Calculate offsets against the original byte stream, while matching
        # the normalized display string.  ``boundaries`` bridges CRLF and
        # multibyte characters without changing the public native-null rule.
        retained_positions: list[int] = []
        at = 0
        while True:
            pos = normal.find(needle, at)
            if pos < 0:
                break
            total += 1
            if len(matches) + len(retained_positions) < MAX_SCAN_MATCHES:
                retained_positions.append(pos)
            at = pos + max(1, len(needle))
        if retained_positions:
            # Redact the complete record before slicing.  The canonical engine
            # maps only the retained positions, so redaction cannot change
            # which raw occurrence centers a snippet or make a marker collision
            # look like a source occurrence.
            safe_record, _redaction_count, safe_positions = (
                _redact_secret_like_text_with_positions(normal, retained_positions))
            for pos, safe_pos in zip(retained_positions, safe_positions):
                if row["start"] is None:
                    byte_start = byte_end = None
                else:
                    byte_start = int(row["start"]) + boundaries[pos]
                    byte_end = int(row["start"]) + boundaries[pos + len(needle)]
                matches.append({"turn": row["turn"], "byte_start": byte_start,
                                "byte_end": byte_end,
                                "excerpt": safe_record[max(0, safe_pos-80):min(len(safe_record), safe_pos+len(needle)+80)]})
    return {"memory_id": memory_id, "session_id": shown["session_id"], "source_path": shown["source_path"], "needle": needle, "matches": matches, "match_count": total, "truncated": total > MAX_SCAN_MATCHES, "truncated_reason": "match_limit" if total > MAX_SCAN_MATCHES else None}


def source_show(
    conn: sqlite3.Connection, *, memory_id: str, context: int = 2,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Public, keyword-only source show API retained by ``storelib``."""
    return show(conn, memory_id, context, evidence_id=evidence_id)


def source_scan(conn: sqlite3.Connection, *, memory_id: str, needle: str) -> dict[str, Any]:
    """Public, keyword-only literal scan API retained by ``storelib``."""
    return scan(conn, memory_id, needle)


# --- issue #512: scope-aware revision resolution --------------------------------

_GLOBAL_SCOPE_KEYS = ("global", "user:global")  # schema.GLOBAL_NAMESPACE


def _scope_key(scope: str) -> str:
    """Equal-breadth global aliases (issue #512 review round 4): the literal
    ``global`` and the store's real ``user:global`` normalize to ONE key so
    neither is narrower than the other."""
    if scope in _GLOBAL_SCOPE_KEYS:
        return "global"
    return scope


def _scope_narrower(scope: str, other: str) -> bool:
    """True iff ``scope`` is strictly NARROWER than ``other``: different
    (normalized) scopes where ``other`` is a global alias or ``scope``
    descends from ``other`` by the ``other:`` prefix convention."""
    s_key, o_key = _scope_key(scope), _scope_key(other)
    if s_key == o_key:
        return False
    return o_key == "global" or scope.startswith(other + ":")


def resolve_revision(
    conn: sqlite3.Connection, *, revision_of: str, scope: str,
    expected_namespace: str | None = None, reason: str = "",
) -> dict[str, Any]:
    """Scope-aware approved-revision tombstone (issue #512 AC4).

    Row-factory-agnostic: all reads/writes use positional tuples and explicit
    column lists, so a plain ``sqlite3.connect`` connection works. Transaction
    ownership mirrors ``supersede_memory``: ``BEGIN IMMEDIATE`` only when the
    caller's connection is not already in a transaction (owned — this call
    commits or rolls back); a caller-owned transaction is JOINED (this call
    never commits or rolls back the caller's work; the result reports what
    THIS call wrote). The tombstone preserves the full supersede invariant
    set, positionally: the knowledge-pointer refusal, the paired
    superseded_at/valid_until columns, an optional ``supersede_reason``, the
    mutation-generation bump, optional ``memory_vec`` cleanup, and expiry-row
    cleanup (``_clear_expiry``).

    Authorization: refused (no mutation, ``refused_ids=[target]``) when the
    target is missing, already superseded, a reserved knowledge-pointer row,
    or its namespace is strictly narrower than ``scope``. ``expected_namespace``
    (when given) joins the SAME conditional UPDATE — atomic with the
    tombstone, no read-then-write rekey race.

    Result: ``{"outcome": "retained_exception"|"superseded"|
    "refused_unauthorized", "superseded_ids": [...], "retained_ids": [...],
    "refused_ids": [...]}`` where ``retained_ids`` lists the LIVE rows whose
    namespace is strictly narrower than the TARGET's namespace (collected
    inside the transaction, atomic with the tombstone).
    """
    from storelib.generation import bump_mutation_generation

    def _refused(reason_text: str, owns_tx: bool) -> dict[str, Any]:
        if owns_tx and conn.in_transaction:
            conn.rollback()
        return {"outcome": "refused_unauthorized", "superseded_ids": [],
                "retained_ids": [], "refused_ids": [revision_of],
                "reason": reason_text}

    owns_tx = not conn.in_transaction
    if owns_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        target = conn.execute(
            "SELECT id, namespace, superseded_at, source_ref FROM memory "
            "WHERE id=?", (revision_of,)).fetchone()
        if target is None:
            return _refused("unknown memory id", owns_tx)
        t_id, t_ns, t_superseded, t_source_ref = (
            target[0], target[1], target[2], target[3])
        if t_superseded is not None:
            return _refused("already superseded", owns_tx)
        if _has_knowledge_prefix(t_source_ref or ""):
            return _refused("reserved knowledge-pointer row", owns_tx)
        if expected_namespace is not None and t_ns != expected_namespace:
            return _refused("namespace guard mismatch", owns_tx)
        if _scope_narrower(t_ns, scope):
            return _refused(
                "target scope is narrower than the revision's scope", owns_tx)

        now = _revision_now()
        cursor = conn.execute(
            "UPDATE memory SET superseded_at=?, valid_until=?, "
            "supersede_reason=? WHERE id=? AND superseded_at IS NULL "
            "AND namespace=?",
            (now, now, reason or "approved revision (issue #512)",
             revision_of, t_ns))
        if cursor.rowcount == 0:
            # Concurrent tombstone/rekey won the race: nothing written.
            return _refused("concurrent mutation won the race", owns_tx)

        bump_mutation_generation(conn)
        _clear_expiry(conn, revision_of)
        _clear_vec_row(conn, revision_of)

        retained: list[str] = []
        for row in conn.execute(
            "SELECT id, namespace FROM memory "
            "WHERE superseded_at IS NULL AND id != ?",
            (revision_of,)).fetchall():
            if _scope_narrower(row[1], t_ns):
                retained.append(str(row[0]))

        if owns_tx:
            conn.commit()
        outcome = "retained_exception" if retained else "superseded"
        return {"outcome": outcome, "superseded_ids": [str(t_id)],
                "retained_ids": retained, "refused_ids": []}
    except BaseException:
        if owns_tx and conn.in_transaction:
            conn.rollback()
        raise


def _revision_now() -> str:
    """UTC timestamp for tombstone columns (append-only history)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clear_vec_row(conn: sqlite3.Connection, memory_id: str) -> None:
    """Remove the optional ``memory_vec`` row (vector support is optional —
    an FTS-only store has no such table, and a store whose vec0 module is
    unloadable must not fail either; mirrors supersede_memory's guard)."""
    has_vec = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vec'"
    ).fetchone()
    if has_vec:
        try:
            conn.execute("DELETE FROM memory_vec WHERE memory_id=?",
                         (memory_id,))
        except sqlite3.OperationalError:
            pass  # vec0 table not available (module unloadable)


def _clear_expiry(conn: sqlite3.Connection, memory_id: str) -> None:
    """Clear the target's expiry rows (same cleanup ``supersede_memory``
    performs)."""
    has_expiry = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='memory_expiry'").fetchone()
    if has_expiry:
        conn.execute("DELETE FROM memory_expiry WHERE memory_id=?",
                     (memory_id,))


def _has_knowledge_prefix(source_ref: str) -> bool:
    """Reserved knowledge-pointer rows never tombstone through this path."""
    from storelib.write import _has_knowledge_prefix as _impl
    return _impl(source_ref)


# --- issue #502: evidence-backed repair -----------------------------------------

_HEX_40_64_RE = re.compile(r"\b[0-9a-fA-F]{40}\b|\b[0-9a-fA-F]{64}\b")
MARKER = "[REDACTED_SECRET]"


def _check_alignment(content: str, alignment_text: str) -> None:
    """The shared alignment rule (issue #502 AC2): the aligned quotation must
    occur EXACTLY ONCE in the stored content. Zero -> changed quotation;
    more than one -> ambiguous target."""
    occurrences = content.count(alignment_text)
    if occurrences == 0:
        raise SourceRefusal("changed quotation: evidence no longer "
                            "matches the stored redacted context")
    if occurrences > 1:
        raise SourceRefusal("ambiguous target: the aligned quotation "
                            "occurs more than once in the stored content")


def repair_from_evidence(
    conn: sqlite3.Connection, *, memory_id: str, evidence_id: str,
    mode: str,
) -> dict[str, Any]:
    """Restore a verified object identifier from linked evidence (issue #502
    AC2).

    ``mode`` is ``"preview"`` (no write of any kind) or ``"apply"`` (ONE
    transaction with the full derived-state set). Refusals raise
    :class:`SourceRefusal` with operator-safe detail; the caller maps them to
    ``[zmem] source repair: refused`` and NOTHING is written.

    Alignment (under the apply lock for apply; point-in-time for preview):
    the excerpt must carry EXACTLY ONE hex-run candidate occurrence; the
    aligned text (excerpt with the candidate replaced by the marker) must
    occur EXACTLY ONCE in the CURRENT stored content. Zero -> changed
    quotation; >1 candidates or occurrences -> ambiguous. The candidate must
    verify as an object in the evidence-selected local git repository
    (`rev-parse --show-toplevel` must equal the selected path; then
    `cat-file -t` must exit 0).
    """
    import subprocess as _subprocess

    if mode not in ("preview", "apply"):
        raise SourceRefusal(f"unknown repair mode: {mode!r}")

    memory_row, _namespace = _memory(conn, memory_id)
    if _has_knowledge_prefix(str(memory_row[2] or "")):
        # R6-4: pointer content is authoritative versioned payload mutated
        # only through the knowledge handler's CAS transaction. (Positional
        # index 2 = source_ref; _memory returns (row, namespace).)
        raise SourceRefusal("reserved knowledge-pointer row")
    _rows, anchor = _evidence(conn, memory_id, evidence_id=evidence_id)
    if anchor is None:
        raise SourceRefusal("memory has no evidence to repair from")
    excerpt = str(anchor[5] or "")
    ref_path = str(anchor[6] or "")
    # R4.5-2: the repair contract resolves a BARE relative name beneath the
    # trusted root — reject separators, drives, schemes, and parent traversal
    # BEFORE any git call (an absolute or ../ ref_path must never select a
    # repository outside ZMEM_REPAIR_ROOT).
    _safe_ref(ref_path)
    candidates = list(dict.fromkeys(_HEX_40_64_RE.findall(excerpt)))
    if len(candidates) != 1:
        raise SourceRefusal(
            "evidence excerpt must quote exactly one verifiable identifier")
    candidate = candidates[0]
    if MARKER in excerpt:
        # Review 4.5 r2: a pre-existing marker in the excerpt means the
        # evidence was itself auto-redacted — the original value is gone and
        # the marker substitution below would hit the WRONG position.
        raise SourceRefusal(
            "evidence excerpt was itself redacted; no original value to "
            "restore")
    alignment_text = excerpt.replace(candidate, MARKER)

    repair_root = os.environ.get("ZMEM_REPAIR_ROOT", "").strip()
    if not repair_root:
        raise SourceRefusal("ZMEM_REPAIR_ROOT is not set")
    root_path = Path(repair_root)
    repo_dir = root_path / ref_path
    # Review 4.5 r2: a bare-name child may still be a symlink/reparse point
    # to an outside location — neither the root nor the child may be a
    # reparse point, and the child must resolve strictly inside the resolved
    # trusted root before any git call.
    try:
        if _is_reparse(root_path) or _is_reparse(repo_dir):
            raise SourceRefusal("ref_path is not a safe directory")
    except OSError as exc:
        raise SourceRefusal("ref_path is not a safe directory") from exc
    try:
        if (not repo_dir.resolve().is_relative_to(root_path.resolve())
                and repo_dir.resolve() != root_path.resolve()):
            raise SourceRefusal("ref_path escapes the trusted repair root")
    except OSError as exc:
        raise SourceRefusal("ref_path is not a safe directory") from exc

    def _git(*args: str) -> subprocess.CompletedProcess:
        return _subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")

    probe = _git("rev-parse", "--show-toplevel")
    if probe.returncode != 0:
        raise SourceRefusal("ref_path is not a repository")
    try:
        toplevel = Path(probe.stdout.strip()).resolve()
    except OSError as exc:
        raise SourceRefusal("ref_path is not a repository") from exc
    if toplevel != repo_dir.resolve():
        raise SourceRefusal("ref_path is not a repository")

    cat = _git("cat-file", "-t", candidate)
    if cat.returncode != 0:
        raise SourceRefusal(
            "restored value is not an object in the evidence-linked "
            "repository")

    # Alignment (R4.5-4): PREVIEW checks it point-in-time too — a preview
    # must not report a verified restoration the stored row cannot accept.
    # APPLY re-runs it UNDER THE WRITE LOCK (Round-5 critic: a concurrent
    # first repair makes a same-span second repair refuse).
    if mode == "preview":
        content_now = conn.execute(
            "SELECT content FROM memory WHERE id=? AND superseded_at IS NULL",
            (memory_id,)).fetchone()
        if content_now is None:
            raise SourceRefusal("memory vanished")
        _check_alignment(content_now[0] or "", alignment_text)
        # Preview NEVER proceeds into the apply transaction (the arm-B
        # snapshot connection is query-only — a fall-through would fail with
        # "attempt to write a readonly database").
        return {"memory_id": memory_id, "evidence_id": evidence_id,
                "mode": "preview", "restore_value": candidate,
                "verified": True, "applied": False}

    owns_tx = not conn.in_transaction
    if owns_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT content FROM memory WHERE id=? AND superseded_at IS NULL",
            (memory_id,)).fetchone()
        if row is None:
            raise SourceRefusal("memory vanished before apply")
        content = row[0] or ""
        _check_alignment(content, alignment_text)
        # Replace ONLY the marker span inside the aligned occurrence: the
        # aligned text carries exactly one marker; swap it for the verified
        # value.
        first = content.find(alignment_text)
        repaired_span = alignment_text.replace(MARKER, candidate, 1)
        new_content = content[:first] + repaired_span + content[
            first + len(alignment_text):]
        if new_content == content:
            raise SourceRefusal("repair would not change the stored content")

        from schema_meta import normalize_content  # the store's own normalizer
        from storelib.generation import bump_mutation_generation
        now = _revision_now()
        conn.execute(
            "UPDATE memory SET content=?, content_norm=?, embedding=NULL, "
            "embedding_model=NULL, embedded_at=NULL WHERE id=?",
            (new_content, normalize_content(new_content), memory_id))
        _clear_vec_row(conn, memory_id)
        bump_mutation_generation(conn)
        # Accumulating provenance record (one row can need several
        # independent repairs — one evidence row per span).
        import json as _json
        rec = conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"source_repair:{memory_id}",)).fetchone()
        try:
            entries = _json.loads(rec[0]) if rec is not None else []
            if not isinstance(entries, list):
                entries = []
        except (TypeError, ValueError):
            entries = []
        entries.append({"memory_id": memory_id, "evidence_id": evidence_id,
                        "restored": candidate, "at": now})
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (f"source_repair:{memory_id}", _json.dumps(entries)))
        if owns_tx:
            conn.commit()
        return {"memory_id": memory_id, "evidence_id": evidence_id,
                "mode": "apply", "restore_value": candidate,
                "verified": True, "applied": True}
    except BaseException:
        if owns_tx and conn.in_transaction:
            conn.rollback()
        raise
