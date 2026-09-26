"""Governed local training-capture state for issue #135.

Host callbacks can create partial records and observations, but only this module
may move a capture through delivery acknowledgement and verified completion.
Text is redacted before it reaches SQLite and every mutating operation owns one
transaction when its caller did not already open one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from storelib.schema import _commit, now_iso

try:
    from redaction import redact_secret_like_text
except ImportError:  # pragma: no cover - installed scripts layout
    # ``store.py`` normally adds its scripts directory to sys.path.  Keep the
    # library importable in isolated callers without relying on storelib being
    # a child package of that directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from redaction import redact_secret_like_text


MAX_CAPTURE_TEXT_BYTES = 16_000
MAX_OPS_JSON_BYTES = 400
MAX_OBSERVATION_BYTES = 4_000
MAX_OUTCOME_BYTES = 4_096
OUTCOME_KINDS = frozenset({
    "test", "compile", "lint", "user_acceptance", "reviewer_acceptance",
})
FINAL_STATES = frozenset({"completed"})


class CaptureBusyError(RuntimeError):
    """A hook-safe signal that a SQLite writer could not acquire the lock."""


class TrainingCaptureConflict(ValueError):
    """An idempotency identity was replayed with different immutable data."""


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
    if value is None:
        return now_iso()
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


def _redacted_bounded(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    # Reject absurd callback payloads before regex processing; normal captures
    # are then redacted before the persisted size cap is applied.
    if len(value.encode("utf-8")) > 65_536:
        raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")
    redacted, _ = redact_secret_like_text(value)
    raw = redacted.encode("utf-8")[:limit]
    return raw.decode("utf-8", errors="ignore")


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
        candidate = _redacted_bounded(item, "effective_ops item", 320) or ""
        candidate_values = [*result, candidate]
        encoded = json.dumps(candidate_values, ensure_ascii=False,
                             separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= MAX_OPS_JSON_BYTES:
            result.append(candidate)
        else:
            break
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


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
        "host_task_id": lambda value: _optional_text(value, "host_task_id", max_bytes=512),
        "session_id": lambda value: _required_text(value, "session_id", max_bytes=512),
        "namespace": lambda value: _required_text(value, "namespace", max_bytes=512),
        "cwd": lambda value: _optional_text(value, "cwd", max_bytes=4096),
        "prompt": lambda value: _redacted_bounded(value, "prompt", MAX_CAPTURE_TEXT_BYTES),
        "assistant_response": lambda value: _redacted_bounded(
            value, "assistant_response", MAX_CAPTURE_TEXT_BYTES
        ),
        "consent_scope": lambda value: _required_text(value, "consent_scope", max_bytes=512),
        "content_license": lambda value: _required_text(value, "content_license", max_bytes=512),
        "redaction_policy_version": lambda value: _required_text(
            value, "redaction_policy_version", max_bytes=512
        ),
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
        missing = next(name for name, value in zip(
            ("consent_scope", "content_license", "redaction_policy_version"), values
        ) if not isinstance(value, str) or not value.strip())
        raise ValueError(f"missing governance field: {missing}")
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
    # A default-deny partial is deliberately a minimal audit marker.  Opaque
    # host task/session values and cwd can contain prompt-like private data, so
    # they are not retained unless capture governance explicitly permits text.
    persisted_session = session_id if permitted else None
    persisted_namespace = namespace if permitted else None
    persisted_host_task_id = host_task_id if permitted else None
    persisted_cwd = cwd if permitted else None
    quarantine_reason = None if permitted else "capture_governance_denied"
    ts = now_iso()
    capture_id = str(uuid.uuid4())
    owns_tx = _begin(conn)
    try:
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


def append_training_capture_observation(
    conn: sqlite3.Connection, capture_id: str, *, observation_kind: str,
    payload: str | None = None, observed_at: str | None = None,
) -> dict[str, Any]:
    """Append a bounded redacted observation without changing capture state."""
    capture_id = _uuid(capture_id, "capture_id")
    observation_kind = _required_text(observation_kind, "observation_kind", max_bytes=128)
    observed_at = _timestamp(observed_at, "observed_at")
    owns_tx = _begin(conn)
    try:
        capture = _capture_row(conn, capture_id)
        if capture["state"] == "completed" or capture["revoked_at"] is not None:
            raise ValueError("cannot append an observation to a final training capture")
        stored_payload = None
        if capture["redaction_status"] == "redacted":
            stored_payload = _redacted_bounded(payload, "observation payload", MAX_OBSERVATION_BYTES)
        observation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO training_capture_observation "
            "(observation_id, capture_id, observation_kind, payload, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (observation_id, capture_id, observation_kind, stored_payload, observed_at),
        )
        conn.execute("UPDATE training_capture SET updated_at=? WHERE capture_id=?", (observed_at, capture_id))
        _finish(conn, owns_tx)
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return {"observation_id": observation_id, "capture_id": capture_id}


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
            stored_rendered = _redacted_bounded(rendered, "rendered", MAX_CAPTURE_TEXT_BYTES)
            stored_ops = _redacted_ops(effective_ops)
            rendered_hash = hashlib.sha256((stored_rendered or "").encode("utf-8")).hexdigest()
        else:
            # Default-deny captures can record that a delivery occurred, but
            # cannot retain any emitted content, token list, or content hash.
            stored_rendered = stored_ops = rendered_hash = None
        existing = conn.execute(
            "SELECT * FROM training_delivery_snapshot WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if existing is None and delivery_snapshot_id is not None:
            existing = conn.execute(
                "SELECT * FROM training_delivery_snapshot WHERE delivery_snapshot_id=?",
                (delivery_snapshot_id,),
            ).fetchone()
            if existing is not None and existing["capture_id"] != capture_id:
                raise TrainingCaptureConflict("delivery_snapshot_id belongs to another capture")
        if existing is not None:
            expected = (stored_rendered, stored_ops, rendered_hash, transform_version)
            actual = (existing["rendered"], existing["effective_ops_json"],
                      existing["rendered_hash"], existing["transform_version"])
            if actual != expected:
                raise TrainingCaptureConflict("conflicting delivery snapshot replay")
            _finish(conn, owns_tx)
            return _row_dict(existing)
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
    except Exception:
        _rollback(conn, owns_tx)
        raise
    return _row_dict(conn.execute(
        "SELECT * FROM training_delivery_snapshot WHERE delivery_snapshot_id=?", (snapshot_id,)
    ).fetchone())


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
    if outcome_kind == "reviewer_acceptance" and (not reviewer_confirmed or not reviewer_id):
        raise ValueError("reviewer_acceptance requires reviewer_id and reviewer_confirmed=true")
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
                        export_consent_scope, export_content_license, reviewer_id,
                        int(reviewer_confirmed), int(correction_closeout), correction_chain_id)
            actual = tuple(completion[key] for key in (
                "evidence_id", "associated_memory_ids_json", "verifier_id", "outcome_kind", "outcome_value",
                "export_consent_scope", "export_content_license", "reviewer_id",
                "reviewer_confirmed", "correction_closeout", "correction_chain_id",
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
            "SELECT session_id FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            raise ValueError("evidence_id does not exist")
        if evidence["session_id"] != capture["session_id"]:
            raise ValueError("evidence_id is outside the capture session")
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


def revoke_training_capture(
    conn: sqlite3.Connection, capture_id: str, *, reason: str, revoked_by: str = "local_governance",
    revoked_at: str | None = None,
) -> dict[str, Any]:
    """Immediately exclude a capture while retaining its governed audit trail."""
    capture_id = _uuid(capture_id, "capture_id")
    reason = _required_text(reason, "reason", max_bytes=4096)
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
    """Purge final/revoked local capture rows after the fixed retention window."""
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days != 30:
        raise ValueError("training capture retention is fixed at 30 days")
    now_ts = _timestamp(now_ts, "now_ts")
    owns_tx = _begin(conn)
    try:
        rows = conn.execute(
            "SELECT capture_id FROM training_capture WHERE finalized_at IS NOT NULL "
            "AND datetime(finalized_at, '+30 days') <= datetime(?)", (now_ts,)
        ).fetchall()
        ids = [row[0] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute("DELETE FROM training_capture_observation WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture_completion WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_delivery_snapshot WHERE capture_id IN (" + placeholders + ")", ids)
            conn.execute("DELETE FROM training_capture WHERE capture_id IN (" + placeholders + ")", ids)
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
