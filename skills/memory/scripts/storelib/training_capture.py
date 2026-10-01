"""Governed local training-capture state for issue #135.

Host callbacks can create partial records and observations, but only this module
may move a capture through delivery acknowledgement and verified completion.
Text is redacted before it reaches SQLite and every mutating operation owns one
transaction when its caller did not already open one.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from storelib.schema import _commit, now_iso


def _load_training_redactor():
    """Load the sibling dependency-free training redactor without sys.path edits."""
    path = Path(__file__).resolve().parent.parent / "redaction.py"
    spec = importlib.util.spec_from_file_location(
        f"_zmem_training_redaction_{uuid.uuid4().hex}", path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load training redactor from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    redact = getattr(module, "redact_training_text", None)
    if not callable(redact):
        raise ImportError(f"training redactor missing from {path}")
    return redact


redact_training_text = _load_training_redactor()


MAX_CAPTURE_TEXT_BYTES = 16_000
MAX_OPS_JSON_BYTES = 400
MAX_OBSERVATION_BYTES = 4_000
MAX_OUTCOME_BYTES = 4_096
MAX_OBSERVATIONS_PER_CAPTURE = 256
MAX_EVENT_IDS = 128
MAX_EVENT_ID_BYTES = 128
OUTCOME_KINDS = frozenset({
    "test", "compile", "lint", "user_acceptance", "reviewer_acceptance",
})
_EVIDENCE_KINDS_BY_OUTCOME = {
    "test": frozenset({"test_result"}),
    "compile": frozenset({"test_result"}),
    "lint": frozenset({"test_result"}),
    "user_acceptance": frozenset({"turn"}),
    "reviewer_acceptance": frozenset({"turn", "correction"}),
}
FINAL_STATES = frozenset({"completed"})


def _capture_identity_keys(
    host: object, session_id: object, namespace: object,
    task_id: object | None = None, turn_id: object | None = None,
    *, require_turn: bool,
) -> tuple[str, str]:
    """Return opaque correlation and session keys from named canonical JSON.

    Every component is named before hashing.  Delimited concatenation would let
    distinct identity tuples alias one another when a host emits the delimiter.
    The session key intentionally omits task/turn so a clear invalidates every
    correlated turn in precisely one host/session/namespace scope.
    """
    normalized_host = _required_text(host, "host", max_bytes=80).lower()
    normalized_session = _required_text(session_id, "session_id", max_bytes=512)
    normalized_namespace = _required_text(namespace, "namespace", max_bytes=512)
    scope = {
        "host": normalized_host,
        "namespace": normalized_namespace,
        "session": normalized_session,
    }
    scope_bytes = json.dumps(
        scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    session_key = hashlib.sha256(scope_bytes).hexdigest()
    if not require_turn:
        return "", session_key
    identity = {
        "host": normalized_host,
        "namespace": normalized_namespace,
        "session": normalized_session,
        "task": _required_text(task_id, "task_id", max_bytes=512),
        "turn": _required_text(turn_id, "turn_id", max_bytes=512),
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), session_key


def training_capture_identity_keys(
    *, host: object, session_id: object, namespace: object,
    task_id: object | None = None, turn_id: object | None = None,
    require_turn: bool = True,
) -> tuple[str, str]:
    """Public canonical identity builder used by all host adapter actions."""
    return _capture_identity_keys(
        host, session_id, namespace, task_id, turn_id, require_turn=require_turn,
    )


class CaptureBusyError(RuntimeError):
    """A hook-safe signal that a SQLite writer could not acquire the lock."""


class TrainingCaptureConflict(ValueError):
    """An idempotency identity was replayed with different immutable data."""


class TrainingCaptureInputRefusal(ValueError):
    """A bounded capture input was refused before content persistence."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def evidence_kind_compatible(
    outcome_kind: object,
    evidence_kind: object,
    *,
    correction_closeout: bool = False,
) -> bool:
    """Validate the evidence type allowed to prove a completion outcome."""
    outcome = str(outcome_kind or "")
    kind = str(evidence_kind or "")
    if correction_closeout:
        return outcome == "reviewer_acceptance" and kind == "correction"
    return kind in _EVIDENCE_KINDS_BY_OUTCOME.get(outcome, frozenset())


def _required_text(value: object, field: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return value.strip()


def _optional_text(value: object, field: str, *, max_bytes: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return value


def _timestamp(value: object | None, field: str) -> str:
    """Return the store clock for capture state, never a caller supplied time.

    The optional argument remains for direct-library compatibility.  CLI entry
    points reject timestamp keys, while old callers cannot backdate or advance
    the retention/state clock by passing one here.
    """
    del value, field
    return now_iso()


def _retention_timestamp(value: object, field: str) -> str:
    """Validate the maintenance scheduler's comparison clock.

    This is not persisted state and is intentionally separate from `_timestamp`.
    It keeps retention tests and the local purge scheduler deterministic without
    accepting a caller-owned finalization timestamp.
    """
    text = _required_text(value, field, max_bytes=64)
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{field} must be a second-precision UTC timestamp") from exc
    return text


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"{field} must be a canonical UUID")
    return str(parsed)


_SAFE_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _redact_training_text(value: str) -> tuple[str, int]:
    """Preserve the capture module API while using the canonical helper."""
    return redact_training_text(value)


def _opaque_identifier(value: str | None) -> str | None:
    """Keep a stable, non-reversible correlation token for host identifiers."""
    if value is None:
        return None
    return "opaque:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


_OPAQUE_OBSERVATION_IDENTIFIER_RE = re.compile(r"opaque:[0-9a-f]{32}\Z")
_OPAQUE_OBSERVATION_IDENTITY_KEYS = frozenset({
    "host_task_id", "hosttaskid", "task_id", "taskid", "turn_id", "turnid",
})


def _opaque_observation_identifier(value: str) -> str:
    """Canonicalize an already-sanitized callback identity without re-hashing it."""
    if _OPAQUE_OBSERVATION_IDENTIFIER_RE.fullmatch(value):
        return value
    return _opaque_identifier(value) or ""


def _redacted_bounded(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    # Reject absurd callback payloads before regex processing; normal captures
    # are then redacted before the persisted size cap is applied.
    if len(value.encode("utf-8")) > 65_536:
        raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")
    redacted, _ = _redact_training_text(value)
    raw = redacted.encode("utf-8")[:limit]
    return raw.decode("utf-8", errors="ignore")


def _redacted_delivery_text(value: object) -> str | None:
    """Redact an emitted payload without changing what the host received."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("rendered must be a string or null")
    if len(value.encode("utf-8")) > 65_536:
        raise TrainingCaptureInputRefusal("rendered_over_limit")
    redacted, _ = _redact_training_text(value)
    if len(redacted.encode("utf-8")) > MAX_CAPTURE_TEXT_BYTES:
        raise TrainingCaptureInputRefusal("rendered_over_limit")
    return redacted


def _redacted_observation_kind(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observation_kind must be a non-empty string")
    redacted, _ = _redact_training_text(value.strip())
    return _required_text(redacted, "observation_kind", max_bytes=128)


def _redacted_cwd(value: object) -> str | None:
    """Retain only the fact that a working directory was supplied."""
    if value is None:
        return None
    text = _optional_text(value, "cwd", max_bytes=4096)
    return "[REDACTED_PATH]" if text else None


def _canonical_json(value: object, field: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} UTF-8 bytes")
    return encoded


def _canonical_attestation(value: object) -> str:
    """Keep the acknowledgement proof to its reviewed, non-content identity."""
    if not isinstance(value, Mapping):
        raise ValueError("attestation must be an object")
    attested_by = _required_text(value.get("attested_by"), "attestation.attested_by", max_bytes=512)
    # Attestation payloads arrive from a caller boundary.  Persisting arbitrary
    # fields would turn this control record into a transcript bypass, so the
    # store retains only the identity needed to establish who attested.  The
    # timestamp is a separate capture column.
    return _canonical_json({"attested_by": attested_by}, "attestation")


def _redacted_ops(value: object) -> str:
    if value is None:
        values: list[object] = []
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("effective_ops must be a list of strings")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("effective_ops must be a list of strings")
        if len(item.encode("utf-8")) > 65_536:
            raise TrainingCaptureInputRefusal(
                "effective_ops_over_limit",
                f"effective_ops exceeds {MAX_OPS_JSON_BYTES} UTF-8 bytes",
            )
        candidate = _redact_training_text(item)[0]
        result.append(candidate)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OPS_JSON_BYTES:
        raise TrainingCaptureInputRefusal(
            "effective_ops_over_limit",
            f"effective_ops exceeds {MAX_OPS_JSON_BYTES} UTF-8 bytes",
        )
    return encoded


def _bounded_event_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > MAX_EVENT_ID_BYTES:
        return None
    redacted, _ = _redact_training_text(candidate)
    if redacted != candidate or not _SAFE_EVENT_ID_RE.fullmatch(candidate):
        return _opaque_identifier(candidate)
    return candidate


def _bounded_observation_json(value: str | None) -> str | None:
    """Redact JSON observations and cap event-id fanout before persistence."""
    if value is None:
        return None
    if len(value.encode("utf-8")) > 65_536:
        raise ValueError("observation payload exceeds 65536 UTF-8 bytes")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        return _redacted_bounded(value, "observation payload", MAX_OBSERVATION_BYTES)
    seen_ids = 0

    def visit(item: object) -> object:
        nonlocal seen_ids
        if isinstance(item, str):
            return _redact_training_text(item)[0]
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        result: dict[str, object] = {}
        for key, child in item.items():
            if not isinstance(key, str):
                continue
            key_lower = key.lower()
            stored_key = _redact_training_text(key)[0]
            if not stored_key:
                continue
            if key_lower in _OPAQUE_OBSERVATION_IDENTITY_KEYS:
                if isinstance(child, str):
                    result[stored_key] = _opaque_observation_identifier(child)
                continue
            if key_lower == "cwd":
                result[stored_key] = "[REDACTED_PATH]" if isinstance(child, str) and child else None
                continue
            if key_lower in {"event_id", "source_event_id", "source_event_ids"}:
                if isinstance(child, list):
                    bounded: list[str] = []
                    for candidate in child:
                        if seen_ids >= MAX_EVENT_IDS:
                            break
                        normalized = _bounded_event_id(candidate)
                        if normalized is not None:
                            bounded.append(normalized)
                            seen_ids += 1
                    result[stored_key] = bounded
                else:
                    if seen_ids >= MAX_EVENT_IDS:
                        continue
                    normalized = _bounded_event_id(child)
                    if normalized is not None:
                        result[stored_key] = normalized
                        seen_ids += 1
                continue
            result[stored_key] = visit(child)
        return result

    encoded = json.dumps(visit(decoded), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OBSERVATION_BYTES:
        return json.dumps({"truncated": True}, separators=(",", ":"))
    return encoded


def _begin(conn: sqlite3.Connection) -> bool | str:
    """Start an operation-owned transaction or savepoint.

    Store callers sometimes compose writers in a larger transaction.  A plain
    ``conn.in_transaction`` bypass would leak our association inserts if a
    later validation failed and the caller chose to commit its outer work.
    """
    if conn.in_transaction:
        savepoint = "zmem_training_capture_" + uuid.uuid4().hex
        conn.execute(f"SAVEPOINT {savepoint}")
        return savepoint
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise CaptureBusyError("capture_busy") from exc
        raise
    return True


def _finish(conn: sqlite3.Connection, ownership: bool | str) -> None:
    if ownership is True:
        try:
            _commit(conn)
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise CaptureBusyError("capture_busy") from exc
            raise
    elif isinstance(ownership, str):
        conn.execute(f"RELEASE SAVEPOINT {ownership}")


def _rollback(conn: sqlite3.Connection, ownership: bool | str) -> None:
    if ownership is True and conn.in_transaction:
        conn.rollback()
    elif isinstance(ownership, str):
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {ownership}")
        finally:
            conn.execute(f"RELEASE SAVEPOINT {ownership}")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _capture_row(conn: sqlite3.Connection, capture_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM training_capture WHERE capture_id=?", (capture_id,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown training capture")
    return row


def _expire_closed_training_sessions(conn: sqlite3.Connection, now_ts: str) -> None:
    conn.execute(
        "DELETE FROM training_capture_closed_session WHERE "
        "datetime(cleared_at, '+30 days') <= datetime(?)",
        (now_ts,),
    )


def _require_open_training_session(conn: sqlite3.Connection, session_key: str) -> None:
    if conn.execute(
        "SELECT 1 FROM training_capture_closed_session WHERE session_key=?",
        (session_key,),
    ).fetchone() is not None:
        raise TrainingCaptureInputRefusal("session_closed")


def _correlation_capture_id(
    conn: sqlite3.Connection, correlation_key: str, session_key: str,
) -> str | None:
    row = conn.execute(
        "SELECT capture_id FROM training_capture_correlation "
        "WHERE correlation_key=? AND session_key=?",
        (correlation_key, session_key),
    ).fetchone()
    return str(row[0]) if row is not None else None


def start_correlated_training_capture(
    conn: sqlite3.Connection, *, task_id: object, turn_id: object,
    **kwargs: Any,
) -> dict[str, Any]:
    """Atomically create or retrieve one correlated partial capture.

    The caller must already hold its stable per-session OS lock.  This helper
    still owns the SQLite immediate transaction, which is the durable barrier
    against a clear or a second process bypassing a stale sidecar.
    """
    host = kwargs.get("host")
    session_id = kwargs.get("session_id")
    namespace = kwargs.get("namespace")
    correlation_key, session_key = training_capture_identity_keys(
        host=host, session_id=session_id, namespace=namespace,
        task_id=task_id, turn_id=turn_id,
    )
    owns_tx = _begin(conn)
    try:
        now = now_iso()
        _expire_closed_training_sessions(conn, now)
        _require_open_training_session(conn, session_key)
        existing_id = _correlation_capture_id(conn, correlation_key, session_key)
        if existing_id is not None:
            assert_training_capture_replay_binding(
                conn, existing_id, _correlated_start_replay_identity(kwargs),
            )
            row = _row_dict(_capture_row(conn, existing_id))
            _finish(conn, owns_tx)
            return row
        capture = start_training_capture(conn, **kwargs)
        conn.execute(
            "INSERT INTO training_capture_correlation "
            "(correlation_key, session_key, capture_id, created_at) VALUES (?, ?, ?, ?)",
            (correlation_key, session_key, capture["capture_id"], now),
        )
        _finish(conn, owns_tx)
        return capture
    except Exception:
        _rollback(conn, owns_tx)
        raise


def _clear_training_capture_session_keys(
    conn: sqlite3.Connection, *, session_keys: Sequence[object],
    remove_sidecars: Any | None = None,
) -> list[str]:
    """Clear a bounded, already-opaque set of session-key digests.

    This internal host-adapter seam deliberately accepts only the 64-hex
    digests produced by :func:`training_capture_identity_keys`; it never
    accepts raw host, session, or namespace identifiers. ``remove_sidecars``
    runs inside the durable transaction. A failure leaves both the mappings
    and closed-session tombstones untouched so the caller can retry.
    """
    if isinstance(session_keys, (str, bytes)):
        raise ValueError("session_keys must be a bounded sequence")
    normalized: list[str] = []
    for value in session_keys:
        key = _required_text(value, "session_key", max_bytes=64)
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ValueError("session_key must be a 64-character lowercase SHA-256 digest")
        if key not in normalized:
            normalized.append(key)
    if not normalized or len(normalized) > 8:
        raise ValueError("session_keys must contain between 1 and 8 opaque digests")
    owns_tx = _begin(conn)
    try:
        now = now_iso()
        _expire_closed_training_sessions(conn, now)
        placeholders = ",".join("?" for _ in normalized)
        keys = [str(row[0]) for row in conn.execute(
            "SELECT correlation_key FROM training_capture_correlation "
            f"WHERE session_key IN ({placeholders})", normalized,
        ).fetchall()]
        if callable(remove_sidecars):
            remove_sidecars(tuple(keys))
        conn.executemany(
            "INSERT INTO training_capture_closed_session(session_key, cleared_at) VALUES (?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET cleared_at=excluded.cleared_at",
            ((key, now) for key in normalized),
        )
        conn.execute(
            f"DELETE FROM training_capture_correlation WHERE session_key IN ({placeholders})", normalized,
        )
        _finish(conn, owns_tx)
        return keys
    except Exception:
        _rollback(conn, owns_tx)
        raise


def clear_correlated_training_session(
    conn: sqlite3.Connection, *, host: object, session_id: object,
    namespace: object, remove_sidecars: Any | None = None,
) -> list[str]:
    """Tombstone one identity scope and remove its durable correlations."""
    _, session_key = training_capture_identity_keys(
        host=host, session_id=session_id, namespace=namespace, require_turn=False,
    )
    return _clear_training_capture_session_keys(
        conn, session_keys=(session_key,), remove_sidecars=remove_sidecars,
    )


def assert_training_capture_replay_binding(
    conn: sqlite3.Connection, capture_id: str, identity: Mapping[str, object],
) -> None:
    """Reject a replay whose supplied capture identity differs from storage.

    Delivery and completion identify a capture through its immutable delivery
    snapshot.  When they also supply original capture fields, those fields must
    describe that same redacted capture; otherwise a changed payload could be
    accepted as an idempotent replay after the capture is complete.
    """
    capture_id = _uuid(capture_id, "capture_id")
    capture = _capture_row(conn, capture_id)
    normalizers = {
        "host": lambda value: _required_text(value, "host", max_bytes=80).lower(),
        # The compatibility column is always NULL.  Host task IDs are never a
        # persisted capture identity, including in hashed form.
        "host_task_id": lambda value: None,
        "session_id": lambda value: (_required_text(value, "session_id", max_bytes=512)
                                    if value is not None else None),
        "namespace": lambda value: (_required_text(value, "namespace", max_bytes=512)
                                    if value is not None else None),
        "cwd": _redacted_cwd,
        "prompt": lambda value: _redacted_bounded(value, "prompt", MAX_CAPTURE_TEXT_BYTES),
        "assistant_response": lambda value: _redacted_bounded(
            value, "assistant_response", MAX_CAPTURE_TEXT_BYTES
        ),
        "consent_scope": lambda value: (_required_text(value, "consent_scope", max_bytes=512)
                                         if value is not None else None),
        "content_license": lambda value: (_required_text(value, "content_license", max_bytes=512)
                                           if value is not None else None),
        "redaction_policy_version": lambda value: (_required_text(
            value, "redaction_policy_version", max_bytes=512
        ) if value is not None else None),
    }
    for field, normalize in normalizers.items():
        if field in identity and normalize(identity[field]) != capture[field]:
            raise TrainingCaptureConflict("conflicting training capture replay")
    if "redaction_status" in identity:
        status = identity["redaction_status"]
        if not isinstance(status, str) or status != capture["redaction_status"]:
            raise TrainingCaptureConflict("conflicting training capture replay")


def _content_governance(
    consent_scope: object, content_license: object,
    redaction_policy_version: object,
) -> tuple[str | None, str | None, str | None, bool]:
    values = (consent_scope, content_license, redaction_policy_version)
    if all(value is None or (isinstance(value, str) and not value.strip())
           for value in values):
        return None, None, None, False
    if any(not isinstance(value, str) or not value.strip() for value in values):
        # Hooks are intentionally allowed to create an auditable partial even
        # when configuration is only partly present.  Treat that exactly like
        # absent governance: keep metadata only and deny export.
        return None, None, None, False
    return (
        _required_text(consent_scope, "consent_scope", max_bytes=512),
        _required_text(content_license, "content_license", max_bytes=512),
        _required_text(redaction_policy_version, "redaction_policy_version", max_bytes=512),
        True,
    )


def start_training_capture(
    conn: sqlite3.Connection, *, host: str, session_id: str | None,
    namespace: str | None,
    host_task_id: str | None = None, cwd: str | None = None,
    prompt: str | None = None, assistant_response: str | None = None,
    consent_scope: str | None = None, content_license: str | None = None,
    redaction_policy_version: str | None = None,
    governance_source: str = "configured_local_policy",
    redaction_status: str | None = None,
) -> dict[str, Any]:
    """Create one store-identified partial capture.

    Missing governance is deliberately default-deny metadata-only capture.  It
    is not a refusal, so host callbacks remain observable without retaining
    their prompt or response.
    """
    host = _required_text(host, "host", max_bytes=80).lower()
    host_task_id = _optional_text(host_task_id, "host_task_id", max_bytes=512)
    cwd = _optional_text(cwd, "cwd", max_bytes=4096)
    governance_source = _required_text(governance_source, "governance_source", max_bytes=512)
    scope, license_, policy, permitted = _content_governance(
        consent_scope, content_license, redaction_policy_version
    )
    if permitted:
        session_id = _required_text(session_id, "session_id", max_bytes=512)
        namespace = _required_text(namespace, "namespace", max_bytes=512)
        if not namespace.startswith("project:") or not namespace[8:].strip():
            raise ValueError("namespace must be a non-empty project: namespace")
    else:
        # Callback metadata can be incomplete; the store UUID remains the
        # partial's identity and raw optional correlation is never persisted.
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string or null")
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError("namespace must be a string or null")
    computed_status = "redacted" if permitted else "metadata_only"
    if redaction_status is not None and redaction_status != computed_status:
        raise ValueError("redaction_status conflicts with store-calculated status")
    persisted_prompt = _redacted_bounded(prompt, "prompt", MAX_CAPTURE_TEXT_BYTES) if permitted else None
    persisted_response = _redacted_bounded(
        assistant_response, "assistant_response", MAX_CAPTURE_TEXT_BYTES
    ) if permitted else None
    # A default-deny partial is deliberately a minimal audit marker.  Host task
    # IDs are never retained; session values and cwd can contain prompt-like
    # private data, so they are retained only under capture governance.
    persisted_session = session_id if permitted else None
    persisted_namespace = namespace if permitted else None
    persisted_host_task_id = None
    persisted_cwd = _redacted_cwd(cwd) if permitted else None
    quarantine_reason = None if permitted else "capture_governance_denied"
    ts = now_iso()
    capture_id = str(uuid.uuid4())
    # Direct and standalone starts have no durable turn mapping, but a complete
    # session scope must still honor a prior clear.  Incomplete metadata-only
    # callbacks retain their minimal audit marker without becoming correlatable.
    session_key = ""
    if isinstance(session_id, str) and session_id.strip() and isinstance(namespace, str) and namespace.strip():
        _, session_key = training_capture_identity_keys(
            host=host, session_id=session_id, namespace=namespace, require_turn=False,
        )
    owns_tx = _begin(conn)
    try:
        if session_key:
            _expire_closed_training_sessions(conn, ts)
            _require_open_training_session(conn, session_key)
        conn.execute(
            "INSERT INTO training_capture (capture_id, host, host_task_id, session_id, "
            "namespace, cwd, created_at, updated_at, state, prompt, assistant_response, "
            "consent_scope, content_license, redaction_status, redaction_policy_version, "
            "governance_source, quarantine_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'partial', ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, host, persisted_host_task_id, persisted_session, persisted_namespace, persisted_cwd, ts, ts,
             persisted_prompt, persisted_response, scope, license_, computed_status,
             policy, governance_source, quarantine_reason),
        )
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(_capture_row(conn, capture_id))


def _correlated_start_replay_identity(kwargs: Mapping[str, object]) -> dict[str, object]:
    """Normalize a correlated start exactly as ``start_training_capture`` does."""
    scope, license_, policy, permitted = _content_governance(
        kwargs.get("consent_scope"), kwargs.get("content_license"),
        kwargs.get("redaction_policy_version"),
    )
    identity: dict[str, object] = {
        "host": kwargs.get("host"), "host_task_id": None,
        "redaction_status": "redacted" if permitted else "metadata_only",
        "consent_scope": scope, "content_license": license_,
        "redaction_policy_version": policy,
    }
    if permitted:
        identity.update({key: kwargs.get(key) for key in (
            "session_id", "namespace", "cwd", "prompt", "assistant_response",
        )})
    else:
        identity.update({key: None for key in (
            "session_id", "namespace", "cwd", "prompt", "assistant_response",
        )})
    return identity


def append_training_capture_observation(
    conn: sqlite3.Connection, capture_id: str, *, observation_kind: str,
    payload: str | None = None, observed_at: str | None = None,
) -> dict[str, Any]:
    """Append a bounded redacted observation without changing capture state."""
    capture_id = _uuid(capture_id, "capture_id")
    observation_kind = _redacted_observation_kind(observation_kind)
    observed_at = _timestamp(observed_at, "observed_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["state"] == "completed" or capture["revoked_at"] is not None:
            raise ValueError("cannot append an observation to a final training capture")
        observation_count = conn.execute(
            "SELECT count(*) FROM training_capture_observation WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[0]
        if observation_count >= MAX_OBSERVATIONS_PER_CAPTURE:
            raise ValueError("training capture observation limit exceeded")
        stored_payload = None
        if capture["redaction_status"] == "redacted":
            stored_payload = _bounded_observation_json(payload)
        payload_sha256 = (
            hashlib.sha256(stored_payload.encode("utf-8")).hexdigest()
            if stored_payload is not None else None
        )
        observation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO training_capture_observation "
            "(observation_id, capture_id, observation_kind, payload, payload_sha256, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (observation_id, capture_id, observation_kind, stored_payload, payload_sha256, observed_at),
        )
        conn.execute("UPDATE training_capture SET updated_at=? WHERE capture_id=?", (observed_at, capture_id))
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return {"observation_id": observation_id, "capture_id": capture_id}


def append_correlated_training_capture_observation(
    conn: sqlite3.Connection, *, host: object, session_id: object,
    namespace: object, task_id: object, turn_id: object,
    observation_kind: str, payload: str | None = None,
) -> dict[str, Any]:
    """Append only while the exact live correlation remains open."""
    correlation_key, session_key = training_capture_identity_keys(
        host=host, session_id=session_id, namespace=namespace,
        task_id=task_id, turn_id=turn_id,
    )
    owns_tx = _begin(conn)
    try:
        now = now_iso()
        _expire_closed_training_sessions(conn, now)
        _require_open_training_session(conn, session_key)
        capture_id = _correlation_capture_id(conn, correlation_key, session_key)
        if capture_id is None:
            raise ValueError("unknown training capture correlation")
        row = append_training_capture_observation(
            conn, capture_id, observation_kind=observation_kind, payload=payload,
        )
        _finish(conn, owns_tx)
        return row
    except Exception:
        _rollback(conn, owns_tx)
        raise


def record_training_delivery_snapshot(
    conn: sqlite3.Connection, capture_id: str, *, rendered: str | None,
    effective_ops: Sequence[str] | None, transform_version: str = "v1",
    delivery_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Record the exact emitted injection payload and move partial -> emitted."""
    capture_id = _uuid(capture_id, "capture_id")
    if delivery_snapshot_id is not None:
        delivery_snapshot_id = _uuid(delivery_snapshot_id, "delivery_snapshot_id")
    transform_version = _required_text(transform_version, "transform_version", max_bytes=512)
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["revoked_at"] is not None:
            raise ValueError("training capture cannot receive a delivery snapshot in its current state")
        if capture["redaction_status"] == "redacted":
            stored_rendered = _redacted_delivery_text(rendered)
            stored_ops = _redacted_ops(effective_ops)
            rendered_hash = hashlib.sha256((stored_rendered or "").encode("utf-8")).hexdigest()
        else:
            # Default-deny captures can record that a delivery occurred, but
            # cannot retain any emitted content, token list, or content hash.
            stored_rendered = stored_ops = rendered_hash = None
        if delivery_snapshot_id is not None:
            identified = conn.execute(
                "SELECT capture_id FROM training_delivery_snapshot WHERE delivery_snapshot_id=?",
                (delivery_snapshot_id,),
            ).fetchone()
            if identified is not None and identified["capture_id"] != capture_id:
                raise TrainingCaptureConflict("delivery_snapshot_id belongs to another capture")
        existing = conn.execute(
            "SELECT * FROM training_delivery_snapshot WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if existing is not None:
            expected = (stored_rendered, stored_ops, rendered_hash, transform_version)
            actual = (existing["rendered"], existing["effective_ops_json"],
                      existing["rendered_hash"], existing["transform_version"])
            if actual != expected:
                raise TrainingCaptureConflict("conflicting delivery snapshot replay")
            _finish(conn, owns_tx)
            replay = _row_dict(existing)
            replay["state"] = "emitted_to_host"
            return replay
        if capture["state"] != "partial":
            raise ValueError("training capture cannot receive a delivery snapshot in its current state")
        snapshot_id = delivery_snapshot_id or str(uuid.uuid4())
        ts = now_iso()
        conn.execute(
            "INSERT INTO training_delivery_snapshot "
            "(delivery_snapshot_id, capture_id, rendered, effective_ops_json, rendered_hash, "
            "transform_version, emitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, capture_id, stored_rendered, stored_ops, rendered_hash,
             transform_version, ts),
        )
        changed = conn.execute(
            "UPDATE training_capture SET state='emitted_to_host', updated_at=? "
            "WHERE capture_id=? AND state='partial'", (ts, capture_id),
        )
        if changed.rowcount != 1:
            raise TrainingCaptureConflict("training capture state changed during delivery")
        _finish(conn, owns_tx)
    except TrainingCaptureInputRefusal as exc:
        _rollback(conn, owns_tx)
        if owns_tx:
            mark_training_delivery_refusal(conn, capture_id, exc.reason)
        raise
    except Exception:
        _rollback(conn, owns_tx)
        raise
    result = _row_dict(conn.execute(
        "SELECT * FROM training_delivery_snapshot WHERE delivery_snapshot_id=?", (snapshot_id,)
    ).fetchone())
    # Adapters need an explicit nonempty delivery result.  This describes the
    # persisted delivery event even if a later acknowledgement has advanced the
    # capture's lifecycle state.
    result["state"] = "emitted_to_host"
    return result


def mark_training_delivery_refusal(
    conn: sqlite3.Connection, capture_id: str, reason: str,
) -> None:
    """Durably mark a rejected delivery after its caller rolled back."""
    capture_id = _uuid(capture_id, "capture_id")
    reason = _required_text(reason, "reason", max_bytes=128)
    owns_tx = _begin(conn)
    try:
        changed = conn.execute(
            "UPDATE training_capture SET quarantine_reason=?, updated_at=? "
            "WHERE capture_id=? AND state='partial'",
            (reason, now_iso(), capture_id),
        )
        if changed.rowcount != 1:
            raise TrainingCaptureConflict("training capture cannot be marked refused")
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise


def record_correlated_training_delivery_snapshot(
    conn: sqlite3.Connection, *, host: object, session_id: object,
    namespace: object, task_id: object, turn_id: object,
    rendered: str | None, effective_ops: Sequence[str] | None,
    transform_version: str = "v1",
) -> dict[str, Any]:
    """Record a delivery only while its exact correlation is still live."""
    correlation_key, session_key = training_capture_identity_keys(
        host=host, session_id=session_id, namespace=namespace,
        task_id=task_id, turn_id=turn_id,
    )
    capture_id: str | None = None
    owns_tx = _begin(conn)
    try:
        now = now_iso()
        _expire_closed_training_sessions(conn, now)
        _require_open_training_session(conn, session_key)
        capture_id = _correlation_capture_id(conn, correlation_key, session_key)
        if capture_id is None:
            raise ValueError("unknown training capture correlation")
        row = record_training_delivery_snapshot(
            conn, capture_id, rendered=rendered, effective_ops=effective_ops,
            transform_version=transform_version,
        )
        _finish(conn, owns_tx)
        return row
    except TrainingCaptureInputRefusal as exc:
        _rollback(conn, owns_tx)
        if capture_id is not None:
            mark_training_delivery_refusal(conn, capture_id, exc.reason)
        raise
    except Exception:
        _rollback(conn, owns_tx)
        raise


def acknowledge_training_delivery(
    conn: sqlite3.Connection, capture_id: str, *, attestation: Mapping[str, object],
    acknowledged_at: str | None = None,
) -> dict[str, Any]:
    """Record a trusted delivery attestation after a store emission."""
    capture_id = _uuid(capture_id, "capture_id")
    encoded_attestation = _canonical_attestation(attestation)
    ts = _timestamp(acknowledged_at, "acknowledged_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["state"] in {"acknowledged", "completed"}:
            if capture["acknowledgement_attestation"] == encoded_attestation:
                _finish(conn, owns_tx)
                return _row_dict(capture)
            raise TrainingCaptureConflict("conflicting delivery acknowledgement replay")
        if capture["state"] != "emitted_to_host" or capture["revoked_at"] is not None:
            raise ValueError("training capture must be emitted before acknowledgement")
        snapshot = conn.execute(
            "SELECT 1 FROM training_delivery_snapshot WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if snapshot is None:
            raise ValueError("training capture has no delivery snapshot")
        conn.execute(
            "UPDATE training_capture SET state='acknowledged', acknowledged_at=?, "
            "acknowledgement_attestation=?, updated_at=? WHERE capture_id=?",
            (ts, encoded_attestation, ts, capture_id),
        )
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(_capture_row(conn, capture_id))


def _memory_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("memory_ids must be a non-empty list of UUIDs")
    parsed = [_uuid(item, "memory_ids item") for item in value]
    if len(set(parsed)) != len(parsed):
        raise ValueError("memory_ids must be unique")
    return sorted(parsed)


def complete_training_capture(
    conn: sqlite3.Connection, capture_id: str, *, evidence_id: str,
    memory_ids: Sequence[str], verifier_id: str, outcome_kind: str,
    outcome_value: str, export_consent_scope: str, export_content_license: str,
    reviewer_id: str | None = None, reviewer_confirmed: bool = False,
    correction_closeout: bool = False, correction_chain_id: str | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Atomically associate evidence and mark an acknowledged capture complete."""
    capture_id = _uuid(capture_id, "capture_id")
    evidence_id = _uuid(evidence_id, "evidence_id")
    ids = _memory_ids(memory_ids)
    verifier_id = _required_text(verifier_id, "verifier_id", max_bytes=512)
    outcome_kind = _required_text(outcome_kind, "outcome_kind", max_bytes=64)
    if outcome_kind not in OUTCOME_KINDS:
        raise ValueError("outcome_kind is not allowed")
    outcome_value = _redacted_bounded(
        _required_text(outcome_value, "outcome_value", max_bytes=65_536),
        "outcome_value", MAX_OUTCOME_BYTES,
    )
    assert outcome_value is not None
    export_consent_scope = _required_text(export_consent_scope, "export_consent_scope", max_bytes=512)
    export_content_license = _required_text(export_content_license, "export_content_license", max_bytes=512)
    if not isinstance(reviewer_confirmed, bool) or not isinstance(correction_closeout, bool):
        raise ValueError("reviewer_confirmed and correction_closeout must be booleans")
    reviewer_id = _optional_text(reviewer_id, "reviewer_id", max_bytes=512)
    correction_chain_id = _optional_text(correction_chain_id, "correction_chain_id", max_bytes=512)
    if outcome_kind == "reviewer_acceptance" and (reviewer_confirmed or reviewer_id is not None):
        raise ValueError("reviewer acceptance requires a separate local review transition")
    ts = _timestamp(verified_at, "verified_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["state"] == "completed":
            completion = conn.execute(
                "SELECT * FROM training_capture_completion WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if completion is None:
                raise TrainingCaptureConflict("completed capture lacks completion record")
            expected = (evidence_id, json.dumps(ids, separators=(",", ":")), verifier_id, outcome_kind, outcome_value,
                        export_consent_scope, export_content_license,
                        int(correction_closeout), correction_chain_id)
            actual = tuple(completion[key] for key in (
                "evidence_id", "associated_memory_ids_json", "verifier_id", "outcome_kind", "outcome_value",
                "export_consent_scope", "export_content_license",
                "correction_closeout", "correction_chain_id",
            ))
            if actual == expected:
                _finish(conn, owns_tx)
                return _row_dict(completion)
            raise TrainingCaptureConflict("conflicting completion replay")
        if capture["state"] != "acknowledged" or capture["revoked_at"] is not None:
            raise ValueError("training capture must be acknowledged before completion")
        if capture["redaction_status"] != "redacted" or capture["quarantine_reason"] is not None:
            raise ValueError("training capture is not exportable")
        if capture["prompt"] is None or capture["assistant_response"] is None:
            raise ValueError("training capture lacks a redacted prompt or assistant response")
        snapshot = conn.execute(
            "SELECT rendered, effective_ops_json FROM training_delivery_snapshot WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if snapshot is None or snapshot["rendered"] is None or snapshot["effective_ops_json"] is None:
            raise ValueError("training capture lacks a redacted delivery snapshot")
        evidence = conn.execute(
            "SELECT session_id, kind FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            raise ValueError("evidence_id does not exist")
        if evidence["session_id"] != capture["session_id"]:
            raise ValueError("evidence_id is outside the capture session")
        if not evidence_kind_compatible(
                outcome_kind, evidence["kind"],
                correction_closeout=correction_closeout):
            raise ValueError("evidence kind is incompatible with outcome_kind")
        placeholders = ",".join("?" for _ in ids)
        memory_rows = conn.execute(
            "SELECT id, namespace FROM memory WHERE id IN (" + placeholders + ")", ids
        ).fetchall()
        if len(memory_rows) != len(ids):
            raise ValueError("memory_ids contains an unknown memory")
        if any(row["namespace"] != capture["namespace"] for row in memory_rows):
            raise ValueError("memory_ids must belong to the capture project namespace")
        for memory_id in ids:
            conn.execute(
                "INSERT OR IGNORE INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
        linked = [row[0] for row in conn.execute(
            "SELECT memory_id FROM memory_evidence WHERE evidence_id=? ORDER BY memory_id",
            (evidence_id,),
        ).fetchall()]
        if linked != ids:
            raise ValueError("evidence already has a different memory association")
        conn.execute(
            "INSERT INTO training_capture_completion "
            "(capture_id, evidence_id, associated_memory_ids_json, verifier_id, verified_at, outcome_kind, outcome_value, "
            "acknowledgement_attestation, export_consent_scope, export_content_license, reviewer_id, "
            "reviewer_confirmed, correction_closeout, correction_chain_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, evidence_id, json.dumps(ids, separators=(",", ":")), verifier_id, ts, outcome_kind, outcome_value,
             capture["acknowledgement_attestation"], export_consent_scope,
             export_content_license, reviewer_id, int(reviewer_confirmed),
             int(correction_closeout), correction_chain_id),
        )
        changed = conn.execute(
            "UPDATE training_capture SET state='completed', finalized_at=?, updated_at=? "
            "WHERE capture_id=? AND state='acknowledged'", (ts, ts, capture_id),
        )
        if changed.rowcount != 1:
            raise TrainingCaptureConflict("training capture state changed during completion")
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(conn.execute(
        "SELECT * FROM training_capture_completion WHERE capture_id=?", (capture_id,)
    ).fetchone())


def review_training_capture(
    conn: sqlite3.Connection, capture_id: str, *, evidence_id: str,
    reviewer_id: str, allowed_reviewer_ids: Sequence[str],
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Record the independent local reviewer transition for one completion.

    This protects against accidental self-review in a local host process.  It
    is not an authentication boundary against a hostile local administrator,
    who can change that process's configured caller identity.
    """
    capture_id = _uuid(capture_id, "capture_id")
    evidence_id = _uuid(evidence_id, "evidence_id")
    reviewer_id = _required_text(reviewer_id, "reviewer_id", max_bytes=512)
    allowed = {
        _required_text(item, "allowed reviewer id", max_bytes=512)
        for item in allowed_reviewer_ids
    }
    if reviewer_id not in allowed:
        raise ValueError("trusted local caller is not an authorized reviewer")
    ts = _timestamp(reviewed_at, "reviewed_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["revoked_at"] is not None:
            raise ValueError("cannot review a revoked training capture")
        completion = conn.execute(
            "SELECT * FROM training_capture_completion WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if completion is None:
            raise ValueError("training capture has no completion to review")
        if completion["outcome_kind"] != "reviewer_acceptance":
            raise ValueError("only reviewer_acceptance completions require local review")
        if completion["evidence_id"] != evidence_id:
            raise TrainingCaptureConflict("review evidence does not match completion evidence")
        if completion["verifier_id"] == reviewer_id:
            raise ValueError("reviewer must differ from verifier")
        existing = conn.execute(
            "SELECT * FROM training_capture_review WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if existing is not None:
            if (existing["completion_evidence_id"], existing["reviewer_id"]) == (evidence_id, reviewer_id):
                _finish(conn, owns_tx)
                return _row_dict(existing)
            raise TrainingCaptureConflict("conflicting training review replay")
        conn.execute(
            "INSERT INTO training_capture_review "
            "(capture_id, completion_evidence_id, reviewer_id, reviewed_at) VALUES (?, ?, ?, ?)",
            (capture_id, evidence_id, reviewer_id, ts),
        )
        # The legacy exporter already gates on these completion columns.  They
        # are updated only after the durable independent-review record exists.
        conn.execute(
            "UPDATE training_capture_completion SET reviewer_id=?, reviewer_confirmed=1 "
            "WHERE capture_id=?", (reviewer_id, capture_id),
        )
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(conn.execute(
        "SELECT * FROM training_capture_review WHERE capture_id=?", (capture_id,)
    ).fetchone())


def revoke_training_capture(
    conn: sqlite3.Connection, capture_id: str, *, reason: str, revoked_by: str = "local_governance",
    revoked_at: str | None = None,
) -> dict[str, Any]:
    """Immediately exclude a capture while retaining its governed audit trail."""
    capture_id = _uuid(capture_id, "capture_id")
    reason = _redacted_bounded(
        _required_text(reason, "reason", max_bytes=65_536),
        "reason", 512,
    )
    if not reason:
        raise ValueError("reason must be a non-empty string")
    revoked_by = _required_text(revoked_by, "revoked_by", max_bytes=512)
    ts = _timestamp(revoked_at, "revoked_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["revoked_at"] is not None:
            if capture["revocation_reason"] == reason and capture["revoked_by"] == revoked_by:
                _finish(conn, owns_tx)
                return _row_dict(capture)
            raise TrainingCaptureConflict("conflicting capture revocation replay")
        conn.execute(
            "UPDATE training_capture SET revoked_at=?, revoked_by=?, revocation_reason=?, "
            "finalized_at=COALESCE(finalized_at, ?), updated_at=? WHERE capture_id=?",
            (ts, revoked_by, reason, ts, ts, capture_id),
        )
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(_capture_row(conn, capture_id))


def purge_expired_training_captures(
    conn: sqlite3.Connection, *, now_ts: str, retention_days: int = 30,
) -> dict[str, int]:
    """Purge every local capture after 30 days of finalization or inactivity.

    Automatic hooks are allowed to produce partial and emitted rows without a
    trusted completion callback.  Those rows therefore use ``updated_at`` as
    their retention clock; completed or revoked rows use ``finalized_at``.
    """
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days != 30:
        raise ValueError("training capture retention is fixed at 30 days")
    now_ts = _retention_timestamp(now_ts, "now_ts")
    owns_tx = _begin(conn)
    try:
        rows = conn.execute(
            "SELECT capture_id FROM training_capture WHERE "
            "datetime(COALESCE(finalized_at, updated_at), '+30 days') <= datetime(?)",
            (now_ts,),
        ).fetchall()
        ids = [row[0] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute("DELETE FROM training_capture_correlation WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture_observation WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture_review WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture_completion WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_delivery_snapshot WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture WHERE capture_id IN (" + placeholders + ")", ids)
        _expire_closed_training_sessions(conn, now_ts)
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return {"purged_captures": len(ids)}


def capture_id_for_delivery_snapshot(conn: sqlite3.Connection, delivery_snapshot_id: str) -> str:
    """Resolve the trusted CLI delivery identity without accepting host ids."""
    delivery_snapshot_id = _uuid(delivery_snapshot_id, "delivery_snapshot_id")
    row = conn.execute(
        "SELECT capture_id FROM training_delivery_snapshot WHERE delivery_snapshot_id=?",
        (delivery_snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown delivery_snapshot_id")
    return str(row[0])
