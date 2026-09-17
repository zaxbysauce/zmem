"""Evidence storage primitives.

Evidence is intentionally a small side store.  Producers call the writer
inside their own transaction; the writer never commits.  A retention sweep
uses a savepoint inside a caller transaction, or owns and commits a transaction
when called outside one.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

from storelib.write import redact_text


EVIDENCE_MAX_EXCERPT_CHARS = 400
EVIDENCE_DEFAULT_RETENTION_DAYS = 30
EVIDENCE_DEFAULT_CAP = 50_000
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


def _validated_limit(raw: str | None, field: str, default: int, *, minimum: int) -> int | None:
    value = default if raw is None or raw == "" else raw
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        return None
    if parsed < minimum or parsed > _SQLITE_INT_MAX:
        return None
    return parsed


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
    days = _validated_limit(
        os.environ.get("ZMEM_EVIDENCE_DAYS"), "retention_days",
        EVIDENCE_DEFAULT_RETENTION_DAYS, minimum=0,
    )
    cap_value = _validated_limit(
        os.environ.get("ZMEM_EVIDENCE_CAP"), "cap",
        EVIDENCE_DEFAULT_CAP, minimum=1,
    )
    if days is None:
        print(
            "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS",
            file=sys.stderr,
        )
        return {"expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0}
    if cap_value is None:
        print(
            "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP",
            file=sys.stderr,
        )
        return {"expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0}
    now = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    try:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        print(
            "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS",
            file=sys.stderr,
        )
        return {
            "expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0,
        }

    zero = {"expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0}
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
