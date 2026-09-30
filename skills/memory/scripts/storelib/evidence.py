"""Evidence storage primitives.

Evidence is intentionally a small side store.  Producers call the writer
inside their own transaction; the writer never commits.  A retention sweep
uses a savepoint inside a caller transaction, or owns and commits a transaction
when called outside one.  Evidence hashes intentionally cover the stable
semantic payload ``kind|ts|final_excerpt`` only; session/lane/moment/path are
provenance columns and remain queryable/exported separately without changing
content identity.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from storelib.write import redact_text


EVIDENCE_MAX_EXCERPT_CHARS = 400
# Bound untrusted input before redact_text runs. The stored value remains capped
# at EVIDENCE_MAX_EXCERPT_CHARS, while ordinary callers can still submit modest
# over-cap excerpts that are deterministically truncated.
EVIDENCE_INPUT_MAX_EXCERPT_CHARS = 4096
EVIDENCE_DEFAULT_RETENTION_DAYS = 30
EVIDENCE_DEFAULT_CAP = 50_000
# Keep one memory write's association list bounded at every local trust
# boundary. The CLI, writer, and direct association helper all use the
# canonical normalizer below so their count and ordering rules cannot drift.
MAX_EVIDENCE_IDS_PER_WRITE = 256
# Association reads can otherwise turn a single memory or evidence identifier
# into an unbounded response.  This is a read/output limit only: imports and
# persistence remain lossless, and callers can retrieve the next page.
EVIDENCE_ASSOCIATION_PAGE_MAX = 256
# Keep untrusted ``IN`` query inputs below SQLite's common host-parameter
# ceilings (999 in legacy builds, 32766 in newer builds).  These are
# conservative per-query limits; custom builds with a lower compiled cap may
# require smaller values.
EVIDENCE_LOOKUP_CHUNK_SIZE = 400
EVIDENCE_ASSOCIATION_LOOKUP_CHUNK_SIZE = 900
EVIDENCE_LANES = (
    "claude", "codex", "zcode", "hermes-provider", "hermes-compat",
)
EVIDENCE_MOMENTS = (
    "session_start", "user_prompt", "pretool", "subagent", "precompact",
)
EVIDENCE_KINDS = (
    "turn", "tool_call", "tool_failure", "edit", "test_result",
    "correction", "delegation",
)
_UTC_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_UUID_SHAPED_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_SQLITE_INT_MAX = 2**63 - 1


def normalize_evidence_ids(
    evidence_ids: Iterable[object] | None,
    *,
    error_type: type[Exception] = ValueError,
) -> list[str]:
    """Canonicalize and bound evidence IDs for one memory write.

    ``error_type`` lets the CLI retain its ``ArgumentTypeError`` contract while
    writer and direct association callers continue to raise ``ValueError``.
    Endpoint existence is checked separately at each write trust boundary.
    """
    if evidence_ids is None:
        raw_ids: list[object] = []
    elif isinstance(evidence_ids, (list, tuple)):
        # Reject before string coercion, sorting, or duplicate scans.
        if len(evidence_ids) > MAX_EVIDENCE_IDS_PER_WRITE:
            raise error_type(
                "at most 256 evidence ids may be attached to one memory write"
            )
        raw_ids = list(evidence_ids)
    else:
        raw_ids = []
        for value in evidence_ids:
            raw_ids.append(value)
            if len(raw_ids) > MAX_EVIDENCE_IDS_PER_WRITE:
                raise error_type(
                    "at most 256 evidence ids may be attached to one memory write"
                )
    ids = [str(value).strip() for value in raw_ids]
    for evidence_id in ids:
        if not evidence_id:
            raise error_type("evidence id is empty")
    seen: set[str] = set()
    for evidence_id in ids:
        if evidence_id in seen:
            raise error_type(f"duplicate evidence id: {evidence_id}")
        seen.add(evidence_id)
    return sorted(ids)


def _validate_ts(value: str, field: str = "ts") -> str:
    if not isinstance(value, str) or not _UTC_SECOND_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a second-precision UTC timestamp "
            "(YYYY-MM-DDTHH:MM:SSZ)"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid UTC timestamp") from exc
    return value


def _required_text(value: object, field: str, *, max_chars: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if max_chars is not None and len(value) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    return value


def _validated_limit(raw: str | None, field: str, default: int, *, minimum: int) -> tuple[int, bool]:
    value = default if raw is None or raw == "" else raw
    if isinstance(value, bool):
        return default, raw not in (None, "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default, raw not in (None, "")
    if parsed < minimum or parsed > _SQLITE_INT_MAX:
        return default, raw not in (None, "")
    return parsed, False


def write_evidence(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    lane: str | None,
    moment: str,
    kind: str,
    ts: str,
    excerpt: str,
    ref_path: str,
    ref_offset: int | None,
    id: str | None = None,
) -> str:
    """Validate, redact, cap, hash, and insert one evidence row.

    The caller owns the transaction.  In particular, this helper never calls
    ``commit``: duplicate IDs and SQL failures are therefore naturally part of
    the caller's rollback boundary.
    """
    session_id = _required_text(session_id, "session_id", max_chars=512)
    if lane is not None and lane not in EVIDENCE_LANES:
        raise ValueError(f"lane must be one of {', '.join(EVIDENCE_LANES)} or None")
    moment = _required_text(moment, "moment")
    if moment not in EVIDENCE_MOMENTS:
        raise ValueError(f"moment must be one of {', '.join(EVIDENCE_MOMENTS)}")
    kind = _required_text(kind, "kind")
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(EVIDENCE_KINDS)}")
    ts = _validate_ts(ts)
    if not isinstance(excerpt, str) or not excerpt:
        raise ValueError("excerpt must be a non-empty string")
    if not isinstance(ref_path, str) or not ref_path.strip():
        raise ValueError("ref_path must be a non-empty string")
    if len(ref_path) > 4096:
        raise ValueError("ref_path exceeds 4096 characters")
    if ref_offset is not None and (
        isinstance(ref_offset, bool)
        or not isinstance(ref_offset, int)
        or ref_offset < 0
        or ref_offset > _SQLITE_INT_MAX
    ):
        raise ValueError(
            "ref_offset must be a non-negative signed 64-bit integer or None"
        )
    if id is not None and (
        not isinstance(id, str) or not _UUID_SHAPED_RE.fullmatch(id)
    ):
        raise ValueError("id must be a 36-character UUID-shaped string")

    excerpt = excerpt[:EVIDENCE_INPUT_MAX_EXCERPT_CHARS]
    final_excerpt, _ = redact_text(excerpt)
    final_excerpt = final_excerpt[:EVIDENCE_MAX_EXCERPT_CHARS]
    digest = hashlib.sha256(
        f"{kind}|{ts}|{final_excerpt}".encode("utf-8")
    ).hexdigest()
    evidence_id = id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO evidence "
        "(id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path, ref_offset) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id, session_id, lane, moment, kind, ts, digest,
            final_excerpt, ref_path, ref_offset,
        ),
    )
    return evidence_id


def sweep_evidence(
    conn: sqlite3.Connection,
    *,
    now_ts: str,
) -> dict[str, int]:
    """Expire and cap evidence, deleting associations in the same transaction.

    Expiration is strict (``ts < cutoff``), and cap deletion is stable by
    ``ts,id``.  Orphan association rows are removed as a final repair pass;
    unassociated evidence itself is retained because it has no namespace
    attribution and remains part of unscoped exports.
    """
    now_ts = _validate_ts(now_ts, "now_ts")
    days, days_invalid = _validated_limit(
        os.environ.get("ZMEM_EVIDENCE_DAYS"), "retention_days",
        EVIDENCE_DEFAULT_RETENTION_DAYS, minimum=0,
    )
    cap_value, cap_invalid = _validated_limit(
        os.environ.get("ZMEM_EVIDENCE_CAP"), "cap",
        EVIDENCE_DEFAULT_CAP, minimum=1,
    )
    zero = {"expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0}
    if days_invalid:
        print(
            "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS",
            file=sys.stderr,
        )
        return zero
    if cap_invalid:
        print(
            "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP",
            file=sys.stderr,
        )
        return zero
    now = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    try:
        cutoff = (now - timedelta(days=days)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (OverflowError, ValueError):
        # A valid year-one clock cannot represent even the documented default
        # retention window. Treat the lower representable bound as "nothing
        # expires" rather than disabling the sweep or raising from a cadence.
        cutoff = datetime.min.replace(tzinfo=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    savepoint = "zmem_evidence_sweep"
    own_transaction = not conn.in_transaction
    try:
        if own_transaction:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
    except sqlite3.Error:
        print("evidence retention failed", file=sys.stderr)
        return zero
    try:
        expired = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE ts < ?", (cutoff,)
        ).fetchone()[0]
        episode_links = conn.execute(
            "DELETE FROM episode_evidence WHERE evidence_id IN "
            "(SELECT id FROM evidence WHERE ts < ?)", (cutoff,)
        ).rowcount
        memory_links = conn.execute(
            "DELETE FROM memory_evidence WHERE evidence_id IN "
            "(SELECT id FROM evidence WHERE ts < ?)", (cutoff,)
        ).rowcount
        conn.execute("DELETE FROM evidence WHERE ts < ?", (cutoff,))

        # Keep newest rows by ts DESC, id DESC; the subquery avoids a host
        # parameter list and remains safe above SQLite's variable limit.
        capped = conn.execute(
            "SELECT COUNT(*) FROM evidence"
        ).fetchone()[0] - cap_value
        capped = max(capped, 0)
        if capped:
            episode_links += conn.execute(
                "DELETE FROM episode_evidence WHERE evidence_id IN "
                "(SELECT id FROM evidence ORDER BY ts DESC, id DESC "
                "LIMIT -1 OFFSET ?)", (cap_value,)
            ).rowcount
            memory_links += conn.execute(
                "DELETE FROM memory_evidence WHERE evidence_id IN "
                "(SELECT id FROM evidence ORDER BY ts DESC, id DESC "
                "LIMIT -1 OFFSET ?)", (cap_value,)
            ).rowcount
            conn.execute(
                "DELETE FROM evidence WHERE id IN "
                "(SELECT id FROM evidence ORDER BY ts DESC, id DESC "
                "LIMIT -1 OFFSET ?)", (cap_value,)
            )

        # Repair stale association rows without treating unassociated evidence
        # as an orphan: unscoped exports intentionally retain those rows.
        episode_links += conn.execute(
            "DELETE FROM episode_evidence WHERE episode_id NOT IN "
            "(SELECT id FROM episode) OR evidence_id NOT IN (SELECT id FROM evidence)"
        ).rowcount
        memory_links += conn.execute(
            "DELETE FROM memory_evidence WHERE memory_id NOT IN "
            "(SELECT id FROM memory) OR evidence_id NOT IN (SELECT id FROM evidence)"
        ).rowcount
        result = {
            "expired": int(expired), "capped": int(capped),
            "episode_links": max(int(episode_links), 0),
            "memory_links": max(int(memory_links), 0),
        }
        if own_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
    except sqlite3.Error:
        if own_transaction:
            conn.rollback()
        else:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            finally:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        print("evidence retention failed", file=sys.stderr)
        return zero


def attach_memory_evidence(
    conn: sqlite3.Connection, *, memory_id: str, evidence_ids: list[str] | tuple[str, ...]
) -> int:
    """Attach evidence rows to one memory atomically and idempotently.

    Writer callers normally already own a transaction.  The standalone form is
    useful to administrative callers, while the savepoint keeps a failed
    association from partially changing a caller-owned transaction. Returns
    the number of newly inserted pairs; existing pairs contribute zero.
    """
    ids = normalize_evidence_ids(evidence_ids)
    if not ids:
        return 0

    own_transaction = not conn.in_transaction
    savepoint = "zmem_attach_memory_evidence"
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        if conn.execute("SELECT 1 FROM memory WHERE id=?", (memory_id,)).fetchone() is None:
            raise ValueError(f"memory id not found: {memory_id}")
        missing = _missing_evidence_ids(conn, ids)
        if missing:
            raise ValueError(f"evidence id not found: {missing[0]}")
        inserted = 0
        for evidence_id in ids:
            inserted += conn.execute(
                "INSERT OR IGNORE INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            ).rowcount
        if own_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return inserted
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            finally:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def evidence_ids_for_memory(conn, memory_id: str) -> list[str]:
    """Issue #124: association read for the operation-feedback loop — the
    evidence ids linked to one memory via the schema-14 memory_evidence
    table. Read-only; the association WRITE API and the MCP surfaces remain
    issue #171's scope. Sorted for deterministic membership checks."""
    return evidence_ids_for_memories(conn, [memory_id]).get(memory_id, [])


def _missing_evidence_ids(
    conn: sqlite3.Connection, evidence_ids: list[str] | tuple[str, ...]
) -> list[str]:
    """Return missing evidence IDs in caller-supplied deterministic order.

    The writer validates association endpoints at its own trust boundary and
    the association helper validates them again inside its transaction. Keep
    those checks, but use bounded ``IN`` queries so large imports do not hold
    the write lock across one query per untrusted ID.
    """
    missing: list[str] = []
    for offset in range(0, len(evidence_ids), EVIDENCE_LOOKUP_CHUNK_SIZE):
        chunk = list(evidence_ids[offset:offset + EVIDENCE_LOOKUP_CHUNK_SIZE])
        placeholders = ",".join("?" for _ in chunk)
        found = {
            row[0] for row in conn.execute(
                f"SELECT id FROM evidence WHERE id IN ({placeholders})", chunk
            ).fetchall()
        }
        missing.extend(evidence_id for evidence_id in chunk if evidence_id not in found)
    return missing


def evidence_ids_for_memories(
    conn: sqlite3.Connection, memory_ids: list[str] | tuple[str, ...]
) -> dict[str, list[str]]:
    """Fetch associations for many memories with bounded queries.

    Pre-v14 stores have no association table. Probe the schema marker once per
    call when that table is absent, preserving the single-memory helper's
    fail-open legacy behavior while still surfacing damaged v14 stores.
    """
    result = {memory_id: [] for memory_id in memory_ids}
    if not memory_ids:
        return result
    unique_ids = list(dict.fromkeys(memory_ids))
    try:
        for offset in range(0, len(unique_ids), EVIDENCE_ASSOCIATION_LOOKUP_CHUNK_SIZE):
            chunk = unique_ids[offset:offset + EVIDENCE_ASSOCIATION_LOOKUP_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT memory_id, evidence_id FROM memory_evidence "
                f"WHERE memory_id IN ({placeholders}) "
                "ORDER BY memory_id, evidence_id",
                chunk,
            ).fetchall()
            for memory_id, evidence_id in rows:
                result[memory_id].append(evidence_id)
    except sqlite3.OperationalError as exc:
        # Older, schema-initialized stores can legitimately lack this v14
        # side table. Do not hide a damaged v14 schema or unrelated SQL error.
        if "no such table: memory_evidence" not in str(exc):
            raise
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        try:
            pre_v14 = version is not None and int(version[0]) < 14
        except (TypeError, ValueError):
            pre_v14 = False
        if not pre_v14:
            raise
        return result
    return result


def evidence_ids_for_memories_bounded(
    conn: sqlite3.Connection,
    memory_ids: list[str] | tuple[str, ...],
    *,
    limit: int = EVIDENCE_ASSOCIATION_PAGE_MAX,
) -> tuple[dict[str, list[str]], set[str]]:
    """Return a bounded, deterministic evidence-id prefix for each memory.

    This is deliberately separate from :func:`evidence_ids_for_memories`:
    maintenance and feedback callers retain their complete, lossless view,
    while recall/recent/explain can safely expose provenance on large stores.
    ``truncated`` identifies memories for which one look-ahead id was found.

    A window-function filter looks concise but still materializes every link in
    an oversized partition.  The nested UNION form instead performs an indexed
    ``LIMIT limit + 1`` lookup for each requested memory in one SQL round trip
    per bounded chunk.
    """
    if limit < 1 or limit > EVIDENCE_ASSOCIATION_PAGE_MAX:
        raise ValueError("evidence association page limit is out of range")
    result = {memory_id: [] for memory_id in memory_ids}
    truncated: set[str] = set()
    unique_ids = list(dict.fromkeys(memory_ids))
    if not unique_ids:
        return result, truncated
    # Each UNION arm has one id and one limit parameter.  Keep under both the
    # common SQLite variable limit and SQLITE_MAX_COMPOUND_SELECT.
    chunk_size = min(400, EVIDENCE_ASSOCIATION_LOOKUP_CHUNK_SIZE)
    try:
        for offset in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[offset:offset + chunk_size]
            arms = [
                "SELECT memory_id, evidence_id FROM ("
                "SELECT memory_id, evidence_id FROM memory_evidence "
                "WHERE memory_id=? ORDER BY evidence_id LIMIT ?"
                ")"
                for _ in chunk
            ]
            params: list[object] = []
            for memory_id in chunk:
                params.extend((memory_id, limit + 1))
            rows = conn.execute(
                "SELECT memory_id, evidence_id FROM ("
                + " UNION ALL ".join(arms)
                + ") ORDER BY memory_id, evidence_id",
                params,
            ).fetchall()
            counts: dict[str, int] = {memory_id: 0 for memory_id in chunk}
            for memory_id, evidence_id in rows:
                counts[memory_id] += 1
                if counts[memory_id] <= limit:
                    result[memory_id].append(evidence_id)
                else:
                    truncated.add(memory_id)
    except sqlite3.OperationalError as exc:
        if "no such table: memory_evidence" not in str(exc):
            raise
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        try:
            pre_v14 = version is not None and int(version[0]) < 14
        except (TypeError, ValueError):
            pre_v14 = False
        if not pre_v14:
            raise
    return result, truncated
