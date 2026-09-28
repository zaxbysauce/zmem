"""Governed SFT and preference views for training capture records.

The SQLite store is authoritative. This module reads canonical memory,
evidence and capture tables, then writes
disposable Parquet/JSON artifacts in a staged directory. It also records a
content-free snapshot-id binding in SQLite to prevent reuse for different inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from redaction import redact_secret_like_text
except ImportError:  # pragma: no cover - installed scripts layout
    from ..redaction import redact_secret_like_text  # type: ignore

try:
    from .training_capture import evidence_kind_compatible
except ImportError:  # pragma: no cover - direct script-layout imports
    from training_capture import evidence_kind_compatible  # type: ignore


SFT_COLUMNS = (
    "task_id", "project_key", "session_id", "episode_id", "prompt",
    "context_fence", "ops_tokens", "assistant_response", "outcome_kind",
    "outcome_value", "evidence_ref", "source_memory_ids", "source_event_ids",
    "consent_scope", "content_license", "redaction_status",
    "redaction_policy_version", "split_key", "transform_version",
    "label_status", "exclusion_reason", "row_checksum",
)

_PREFERENCE_OBJECT_FIELDS = (
    "memory_id", "content", "source_event_ids", "evidence_ref",
)
PREFERENCE_COLUMNS = (
    "task_id", "project_key", "session_id", "episode_id", "prompt",
    "context_fence", "chosen", "rejected", "update_of", "supersede_reason",
    "source_memory_ids", "source_event_ids", "evidence_ref", "consent_scope",
    "content_license", "redaction_status", "redaction_policy_version",
    "split_key", "transform_version", "row_checksum",
)

TRANSFORM_VERSION = "training-v1"
SPLIT_SALT_ENV = "ZMEM_TRAINING_SPLIT_SALT"
FIXED_SPLIT_SALT = "zmem-training-v1"
TRUST_FLOOR_ENV = "ZMEM_INJECT_FLOOR_TRUST"
DEDUP_THRESHOLD_ENV = "ZMEM_DEDUP_THRESHOLD"
MAX_EXPORT_ROWS_ENV = "ZMEM_TRAINING_MAX_ROWS"
DEFAULT_TRUST_FLOOR = 0.2
DEFAULT_DEDUP_THRESHOLD = 0.85
DEFAULT_MAX_EXPORT_ROWS = 10_000
_OUTCOME_KINDS = frozenset({
    "test", "compile", "lint", "user_acceptance", "reviewer_acceptance",
})
QUARANTINE_MAX_EVENTS = 50
QUARANTINE_EVENT_MAX_BYTES = 400
MAX_CONTEXT_BYTES = 16_000
MAX_OPS_BYTES = 400

_PROCESS_LOCK_GUARD = threading.Lock()
_ACTIVE_OUTPUT_LOCKS: set[str] = set()


class TrainingExportError(RuntimeError):
    """Raised when a training export cannot be produced safely."""


def _require_confirmation(reviewer_confirmed: bool) -> None:
    if reviewer_confirmed is not True:
        raise ValueError("reviewer confirmation required")


def _text(value: object, *, max_bytes: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if max_bytes is not None:
        raw = text.encode("utf-8")[:max_bytes]
        text = raw.decode("utf-8", errors="ignore")
    return text


def _redact(value: object, *, max_bytes: int | None = None) -> str | None:
    text = _text(value, max_bytes=max_bytes)
    if text is None:
        return None
    text, _ = redact_secret_like_text(text)
    if max_bytes is not None:
        text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return text


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _parse_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _max_export_rows() -> int:
    try:
        value = int(os.environ.get(MAX_EXPORT_ROWS_ENV,
                                   str(DEFAULT_MAX_EXPORT_ROWS)))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_EXPORT_ROWS
    if value < 1:
        value = DEFAULT_MAX_EXPORT_ROWS
    return value


def _embedding_identity() -> tuple[str, str]:
    """Return the local text-embedding model and immutable revision marker."""
    try:
        try:
            import embed_profiles
        except ImportError:  # pragma: no cover - installed scripts layout
            from .. import embed_profiles  # type: ignore
        profile = embed_profiles.resolve_active_profile()
        details = embed_profiles.get_profile(profile)
        model = str(details.get("hf_id") or profile)
        revision = str(details.get("sha256") or f"profile:{profile}")
        return model, revision
    except Exception:
        # A missing/invalid model is still rejected by _embed_for_dedup for a
        # nonempty export; retain an explicit diagnostic identity in empty
        # schema-only manifests.
        return "unknown", "unknown"


def _parse_ops(value: object, *, capture_id: str) -> list[str]:
    """Parse one persisted operation list without changing its contents.

    The delivery snapshot is an immutable input to an export.  Truncating a
    token or dropping trailing operations would make the derived view disagree
    with the acknowledged snapshot, so every malformed or oversized value is a
    hard, capture-specific export failure.
    """
    diagnostic = f"invalid persisted operations for capture {capture_id}"
    if value is None:
        raise TrainingExportError(f"{diagnostic}: missing value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise TrainingExportError(f"{diagnostic}: malformed JSON") from None
    if not isinstance(value, list):
        raise TrainingExportError(f"{diagnostic}: expected a JSON array")
    persisted = list(value)
    if any(not isinstance(item, str) for item in persisted):
        raise TrainingExportError(f"{diagnostic}: array items must be strings")
    persisted_encoded = json.dumps(
        persisted, ensure_ascii=False, separators=(",", ":")
    )
    if len(persisted_encoded.encode("utf-8")) > MAX_OPS_BYTES:
        raise TrainingExportError(
            f"{diagnostic}: encoded value exceeds {MAX_OPS_BYTES} bytes"
        )
    result: list[str] = []
    for item in persisted:
        result.append(_redact(item) or "")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OPS_BYTES:
        raise TrainingExportError(
            f"{diagnostic}: encoded value exceeds {MAX_OPS_BYTES} bytes"
        )
    return result


def _walk_event_ids(value: object, *, key_hint: str = "") -> list[str]:
    """Extract only explicit source/event id keys from an observation payload."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).casefold().replace("-", "_")
            if name in {"source_event_id", "source_event_ids", "event_id", "event_ids"}:
                if isinstance(child, (list, tuple)):
                    found.extend(str(item) for item in child if isinstance(item, str) and item.strip())
                elif isinstance(child, str) and child.strip():
                    found.append(child.strip())
            else:
                found.extend(_walk_event_ids(child, key_hint=name))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_walk_event_ids(child, key_hint=key_hint))
    elif key_hint in {"source_event_id", "event_id"} and isinstance(value, str):
        found.append(value.strip())
    return found


def _observation_event_ids(
    rows: Iterable[sqlite3.Row], *, capture_id: str
) -> list[str]:
    """Return event references only from payloads whose stored digest verifies."""
    found: set[str] = set()
    for row in rows:
        payload = row["payload"]
        if payload is None:
            continue
        if not isinstance(payload, str):
            raise TrainingExportError(
                f"invalid observation payload for capture {capture_id}"
            )
        expected = row["payload_sha256"] if "payload_sha256" in row.keys() else None
        actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
            raise TrainingExportError(
                f"observation payload digest mismatch for capture {capture_id}"
            )
        parsed: object = payload
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            pass
        ids = _walk_event_ids(parsed)
        kind = str(row["observation_kind"] or "").casefold()
        if not ids and kind in {"source_event_id", "event_id", "event"} and isinstance(parsed, str):
            ids = [parsed.strip()]
        found.update(item for item in ids if item)
    return sorted(found)


def _validated_source_event_ids(
    conn: sqlite3.Connection, item: Mapping[str, Any]
) -> list[str]:
    """Bind provenance to the completion and same-session store evidence.

    Host observations may suggest IDs, but only an ID that resolves to an
    evidence row in this capture's session is eligible for export. The
    completion's store-validated evidence ID is always included.
    """
    capture = item["capture"]
    completion = item.get("completion")
    if completion is None:
        return []
    session_id = str(capture["session_id"] or "")
    completion_evidence_id = str(completion["evidence_id"] or "")
    observed_ids = sorted({
        str(event_id) for event_id in item.get("source_event_ids", [])
        if str(event_id)
    })
    all_ids = sorted(set(observed_ids) | {completion_evidence_id})
    if not all_ids:
        return []
    placeholders = ",".join("?" for _ in all_ids)
    evidence_rows = conn.execute(
        f"SELECT id, session_id FROM evidence WHERE id IN ({placeholders})",
        all_ids,
    ).fetchall()
    evidence_by_id = {str(row["id"]): str(row["session_id"] or "")
                      for row in evidence_rows}
    if evidence_by_id.get(completion_evidence_id) != session_id:
        return []
    return sorted({
        event_id for event_id in all_ids
        if evidence_by_id.get(event_id) == session_id
    })


def _row_json(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    values: dict[str, object] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, bytes):
            value = value.hex()
        values[str(key)] = value
    return values


def _load_snapshot_rows(conn: sqlite3.Connection, namespace: str | None,
                        *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read all delivery snapshots in the requested namespace for one view."""
    query = (
        "SELECT c.*, s.delivery_snapshot_id, s.rendered, s.effective_ops_json, "
        "s.rendered_hash, s.transform_version, s.emitted_at "
        "FROM training_capture c "
        "JOIN training_delivery_snapshot s ON s.capture_id=c.capture_id"
    )
    params: list[object] = []
    if namespace is not None:
        query += " WHERE c.namespace=?"
        params.append(namespace)
    query += " ORDER BY c.capture_id"
    params_with_limit = list(params)
    if limit is not None:
        query += " LIMIT ?"
        params_with_limit.append(limit + 1)
    snapshot_rows: list[dict[str, Any]] = []
    selected_rows = conn.execute(query, params_with_limit).fetchall()
    if limit is not None and len(selected_rows) > limit:
        raise TrainingExportError(
            f"training export exceeds maximum of {limit} source captures"
        )
    for snapshot_row in selected_rows:
        capture_id = str(snapshot_row["capture_id"])
        observations = conn.execute(
            "SELECT observation_kind, payload, payload_sha256, observed_at, observation_id "
            "FROM training_capture_observation "
            "WHERE capture_id=? ORDER BY observed_at, observation_id", (capture_id,)
        ).fetchall()
        snapshot_rows.append({
            "capture": snapshot_row,
            "completion": conn.execute(
                "SELECT * FROM training_capture_completion "
                "WHERE capture_id=?", (capture_id,)
            ).fetchone(),
            "review": conn.execute(
                "SELECT * FROM training_capture_review WHERE capture_id=?",
                (capture_id,),
            ).fetchone(),
            "source_event_ids": _observation_event_ids(observations, capture_id=capture_id),
            "observations": observations,
        })
    return snapshot_rows


def _selection_fingerprint(conn: sqlite3.Connection,
                           snapshot_rows: Sequence[Mapping[str, Any]],
                           namespace: str | None) -> str:
    """Hash every source input that can affect selection, labels or provenance."""
    rows: list[dict[str, object]] = []
    for item in snapshot_rows:
        capture = item["capture"]
        capture_id = str(capture["capture_id"])
        completion = conn.execute(
            "SELECT * FROM training_capture_completion WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        review = item.get("review")
        if "review" not in item:
            review = conn.execute(
                "SELECT * FROM training_capture_review WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
        evidence_id = str(completion["evidence_id"]) if completion is not None else None
        association = conn.execute(
            "SELECT memory_id FROM memory_evidence WHERE evidence_id=? ORDER BY memory_id",
            (evidence_id,),
        ).fetchall() if evidence_id is not None else []
        memory_ids = [str(row[0]) for row in association]
        memories = _memory_rows(conn, memory_ids)
        predecessor_ids = sorted({
            str(memory["update_of"])
            for memory in memories
            if memory["update_of"]
        })
        predecessors = _memory_rows(conn, predecessor_ids)
        predecessor_association = conn.execute(
            f"SELECT memory_id, evidence_id FROM memory_evidence "
            f"WHERE memory_id IN ({','.join('?' for _ in predecessor_ids)}) "
            "ORDER BY memory_id, evidence_id", predecessor_ids
        ).fetchall() if predecessor_ids else []
        predecessor_evidence_ids = sorted({str(row[1]) for row in predecessor_association})
        predecessor_evidence = conn.execute(
            f"SELECT * FROM evidence WHERE id IN ({','.join('?' for _ in predecessor_evidence_ids)}) "
            "ORDER BY id", predecessor_evidence_ids
        ).fetchall() if predecessor_evidence_ids else []
        evidence = conn.execute(
            "SELECT * FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone() if evidence_id is not None else None
        observation_ids = item.get("source_event_ids", [])
        observation_evidence: list[sqlite3.Row] = []
        if observation_ids:
            marks = ",".join("?" for _ in observation_ids)
            observation_evidence = conn.execute(
                f"SELECT * FROM evidence WHERE id IN ({marks}) ORDER BY id",
                list(observation_ids),
            ).fetchall()
        episode_memberships = conn.execute(
            f"SELECT episode_id, memory_id FROM episode_memory "
            f"WHERE memory_id IN ({','.join('?' for _ in memory_ids)}) "
            "ORDER BY episode_id, memory_id", memory_ids
        ).fetchall() if memory_ids else []
        episode_ids = sorted({str(row[0]) for row in episode_memberships})
        episodes = conn.execute(
            f"SELECT * FROM episode WHERE id IN ({','.join('?' for _ in episode_ids)}) "
            "ORDER BY id", episode_ids
        ).fetchall() if episode_ids else []
        memory_fingerprint_fields = (
            "id", "namespace", "content", "trust_score", "applied_count",
            "violated_count", "update_of", "supersede_reason", "superseded_at",
            "valid_from", "valid_until",
        )
        rows.append({
            "capture_snapshot": _row_json(capture),
            "completion": _row_json(completion),
            "review": _row_json(review),
            "evidence": _row_json(evidence),
            "observation_evidence": [_row_json(row) for row in observation_evidence],
            "association": memory_ids,
            # Keep the fingerprint bounded and avoid touching persisted
            # embedding/vector columns; semantic dedup is text-only and
            # vectors are never an exporter input.
            "memories": [
                {key: memory[key] for key in memory_fingerprint_fields
                 if key in memory.keys()}
                for memory in memories
            ],
            "predecessors": [
                {key: memory[key] for key in memory_fingerprint_fields
                 if key in memory.keys()}
                for memory in predecessors
            ],
            "predecessor_association": [_row_json(row) for row in predecessor_association],
            "predecessor_evidence": [_row_json(row) for row in predecessor_evidence],
            "episode_memberships": [_row_json(row) for row in episode_memberships],
            "episodes": [_row_json(row) for row in episodes],
            "observations": [_row_json(observation) for observation in item["observations"]],
        })
    return hashlib.sha256(_json_bytes({
        "namespace": namespace,
        "captures": rows,
    })).hexdigest()


def _memory_rows(conn: sqlite3.Connection, ids: Sequence[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT * FROM memory WHERE id IN ({marks}) ORDER BY id", list(ids)
    ).fetchall()


def _validate_update_lineage(conn: sqlite3.Connection,
                             memory_rows: Sequence[sqlite3.Row]) -> None:
    """Refuse missing or cross-namespace memory predecessors."""
    predecessor_ids = sorted({
        str(memory["update_of"])
        for memory in memory_rows
        if memory["update_of"]
    })
    if not predecessor_ids:
        return
    predecessors = _memory_rows(conn, predecessor_ids)
    predecessor_by_id = {str(row["id"]): row for row in predecessors}
    if len(predecessor_by_id) != len(predecessor_ids):
        raise TrainingExportError("memory update_of references a missing predecessor")
    for memory in memory_rows:
        predecessor_id = str(memory["update_of"] or "")
        if not predecessor_id:
            continue
        predecessor = predecessor_by_id[predecessor_id]
        if str(predecessor["namespace"] or "") != str(memory["namespace"] or ""):
            raise TrainingExportError("memory update_of crosses project namespace")


def _episode_for_memories(conn: sqlite3.Connection, memory_ids: Sequence[str]) -> tuple[str | None, str | None]:
    if not memory_ids:
        return None, "missing_association"
    marks = ",".join("?" for _ in memory_ids)
    rows = conn.execute(
        f"SELECT episode_id, memory_id FROM episode_memory WHERE memory_id IN ({marks}) "
        "ORDER BY episode_id, memory_id", list(memory_ids)
    ).fetchall()
    by_memory: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_memory[str(row["memory_id"])].add(str(row["episode_id"]))
    if any(mid not in by_memory for mid in memory_ids):
        return None, "missing_episode"
    common = set.intersection(*(by_memory[mid] for mid in memory_ids))
    if len(common) != 1:
        return None, "ambiguous_episode" if common else "missing_episode"
    return sorted(common)[0], None


def _project_key(memory_rows: Sequence[sqlite3.Row]) -> tuple[str | None, str | None]:
    namespaces = {str(row["namespace"]) for row in memory_rows}
    if len(namespaces) != 1:
        return None, "mixed_namespace"
    namespace = next(iter(namespaces))
    if not namespace.startswith("project:") or not namespace[8:].strip():
        return None, "invalid_namespace"
    return namespace[8:], None


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def _lineage_groups(candidates: Sequence[dict[str, Any]]) -> dict[int, str]:
    uf = _UnionFind(len(candidates))
    key_owner: dict[tuple[str, str], int] = {}
    for index, item in enumerate(candidates):
        namespace = str(item.get("namespace", ""))
        keys: set[tuple[str, str]] = set()
        session_id = str(item.get("session_id") or "")
        if session_id:
            keys.add(("session", f"{namespace}\0{session_id}"))
        episode_id = str(item.get("episode_id") or "")
        if episode_id:
            keys.add(("episode", f"{namespace}\0{episode_id}"))
        keys.update(
            ("memory", f"{namespace}\0{memory_id}")
            for memory_id in item.get("source_memory_ids", [])
        )
        for memory in item.get("memory_rows", []):
            predecessor = str(memory["update_of"] or "")
            if predecessor:
                # update_of and source_memory_ids describe the same memory
                # identity.  Use one key type so a predecessor links to the
                # candidate that sourced it, and scope it to the namespace so
                # malformed cross-project references cannot merge groups.
                memory_namespace = str(
                    memory["namespace"] or namespace
                    if "namespace" in memory.keys() else namespace
                )
                keys.add(("memory", f"{memory_namespace}\0{predecessor}"))
        for key in keys:
            if key[1] and key in key_owner:
                uf.union(index, key_owner[key])
            elif key[1]:
                key_owner[key] = index
    roots: dict[int, list[str]] = defaultdict(list)
    for index, item in enumerate(candidates):
        roots[uf.find(index)].append(str(item.get("capture_id", index)))
    result: dict[int, str] = {}
    for root, ids in roots.items():
        group_id = hashlib.sha256(("\0".join(sorted(ids))).encode("utf-8")).hexdigest()
        for index, item in enumerate(candidates):
            if uf.find(index) == root:
                result[index] = group_id
    return result


def _split_key(project_key: str, split_salt: str | None = None) -> str:
    """Return the stable split identity for one project.

    Split assignment is deliberately a function of the canonical project key
    only.  Capture ids, session labels, episode membership and export snapshot
    labels are mutable selection details and must never move an existing
    project's rows between buckets.  ``split_salt`` remains accepted for API
    compatibility, but the v1 contract pins the salt to the transform version.
    The compact sorted-key JSON is the frozen cross-runtime canonical form.
    """
    del split_salt
    payload = {
        "kind": "project",
        "value": str(project_key),
        "salt": FIXED_SPLIT_SALT,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _split_bucket(split_key: str) -> str:
    first_byte = int(str(split_key)[:2], 16)
    if first_byte <= 0xCB:
        return "train"
    if first_byte <= 0xE5:
        return "validation"
    return "test"


def _completion_is_exportable(conn: sqlite3.Connection,
                              item: Mapping[str, Any]) -> bool:
    """Apply the one completion gate shared by views and quarantine.

    Automatic hooks only create redacted partials.  A derived artifact may
    contain a capture after it has a verified completion, an acknowledgement
    attestation bound to that completion, valid export-time governance and a
    real same-session evidence row.  Capture-time quarantine and revocation
    remain hard exclusions from every training output.
    """
    capture = item["capture"]
    completion = item.get("completion")
    if completion is None:
        return False
    for key in (
        "namespace", "session_id", "acknowledged_at", "finalized_at", "prompt",
        "assistant_response", "consent_scope", "content_license",
        "redaction_policy_version", "governance_source",
        "acknowledgement_attestation",
    ):
        if not str(capture[key] or "").strip():
            return False
    if capture["state"] != "completed" or capture["revoked_at"] is not None:
        return False
    if capture["quarantine_reason"] is not None:
        return False
    if capture["redaction_status"] != "redacted":
        return False
    attestation = str(capture["acknowledgement_attestation"] or "")
    if not attestation or str(completion["acknowledgement_attestation"] or "") != attestation:
        return False
    if not all(str(completion[key] or "").strip() for key in (
            "evidence_id", "verifier_id", "verified_at", "outcome_kind",
            "outcome_value", "export_consent_scope",
            "export_content_license")):
        return False
    evidence = conn.execute(
        "SELECT session_id, kind FROM evidence WHERE id=?",
        (completion["evidence_id"],),
    ).fetchone()
    if (
        evidence is None
        or evidence["session_id"] != capture["session_id"]
        or not evidence_kind_compatible(
            completion["outcome_kind"],
            evidence["kind"],
            correction_closeout=bool(completion["correction_closeout"]),
        )
    ):
        return False
    if str(completion["outcome_kind"]) == "reviewer_acceptance":
        if "review" in item:
            review = item["review"]
        else:
            review = conn.execute(
                "SELECT completion_evidence_id, reviewer_id FROM training_capture_review "
                "WHERE capture_id=?",
                (capture["capture_id"],),
            ).fetchone()
        return (
            review is not None
            and str(review["completion_evidence_id"]) == str(completion["evidence_id"])
            and str(review["reviewer_id"]) == str(completion["reviewer_id"])
            and str(review["reviewer_id"]) != str(completion["verifier_id"])
            and bool(completion["reviewer_confirmed"])
        )
    return True


def _canonical_event_text(item: Mapping[str, object]) -> str:
    return "\n".join(str(item.get(key) or "") for key in (
        "prompt", "assistant_response", "outcome_kind", "outcome_value"))


def _cosine(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or len(left) % 4:
        return -1.0
    values_left = struct.unpack(f"<{len(left) // 4}f", left)
    values_right = struct.unpack(f"<{len(right) // 4}f", right)
    dot = sum(a * b for a, b in zip(values_left, values_right))
    norm_left = math.sqrt(sum(a * a for a in values_left))
    norm_right = math.sqrt(sum(b * b for b in values_right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else -1.0


def _embed_for_dedup(texts: Sequence[str]) -> list[bytes]:
    try:
        from embeddings import embed_text
    except ImportError:  # pragma: no cover - installed scripts layout
        from ..embeddings import embed_text  # type: ignore
    vectors = [embed_text(text) for text in texts]
    if any(vector is None for vector in vectors):
        raise TrainingExportError(
            "semantic deduplication requires an available local embedding model"
        )
    return [vector for vector in vectors if vector is not None]


def _deduplicate(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        by_group[str(item["lineage_group"])].append(item)
    threshold = _parse_float_env(DEDUP_THRESHOLD_ENV, DEFAULT_DEDUP_THRESHOLD)
    keepers: list[dict[str, Any]] = []
    deletion_map: list[dict[str, str]] = []
    for group, items in sorted(by_group.items()):
        items.sort(key=lambda row: (str(row["task_id"]), str(row["capture_id"])))
        exact: dict[str, dict[str, Any]] = {}
        survivors: list[dict[str, Any]] = []
        for item in items:
            digest = hashlib.sha256(_canonical_event_text(item).encode("utf-8")).hexdigest()
            item["dedup_digest"] = digest
            keeper = exact.get(digest)
            if keeper is not None:
                keeper["source_event_ids"] = sorted(set(keeper["source_event_ids"]) | set(item["source_event_ids"]))
                deletion_map.append({"keeper_id": str(keeper["capture_id"]), "deleted_id": str(item["capture_id"]), "reason": "exact_duplicate"})
            else:
                exact[digest] = item
                survivors.append(item)
        if survivors:
            vectors = _embed_for_dedup([_canonical_event_text(item) for item in survivors])
            for item, vector in zip(survivors, vectors):
                item["_dedup_vector"] = vector
            retained: list[dict[str, Any]] = []
            for index, item in enumerate(survivors):
                keeper = next((candidate for candidate in retained
                               if _cosine(vectors[index], candidate["_dedup_vector"]) >= threshold), None)
                if keeper is not None:
                    keeper["source_event_ids"] = sorted(set(keeper["source_event_ids"]) | set(item["source_event_ids"]))
                    deletion_map.append({"keeper_id": str(keeper["capture_id"]), "deleted_id": str(item["capture_id"]), "reason": "semantic_duplicate"})
                else:
                    retained.append(item)
            survivors = retained
        for item in survivors:
            item.pop("dedup_digest", None)
            item.pop("_dedup_vector", None)
        keepers.extend(survivors)
    deletion_map.sort(key=lambda row: (row["deleted_id"], row["keeper_id"], row["reason"]))
    return keepers, deletion_map


def _candidate_rows(conn: sqlite3.Connection, *, namespace: str | None,
                    snapshot_id: str, quarantine_raw: bool = False,
                    split_salt: str | None = None) -> tuple[list[dict[str, Any]], Counter, list[dict[str, Any]]]:
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("snapshot_id must be a non-empty string")
    completion_columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(training_capture_completion)"
        ).fetchall()
    }
    associated_select = (
        "cc.associated_memory_ids_json"
        if "associated_memory_ids_json" in completion_columns
        else "NULL AS associated_memory_ids_json"
    )
    query = (
        "SELECT c.*, s.delivery_snapshot_id, s.rendered, s.effective_ops_json, "
        "s.rendered_hash, s.transform_version, s.emitted_at, cc.evidence_id, cc.verifier_id, "
        "cc.verified_at, cc.outcome_kind, cc.outcome_value, cc.export_consent_scope, "
        "cc.export_content_license, cc.reviewer_id, cc.reviewer_confirmed, "
        f"cc.correction_closeout, cc.correction_chain_id, {associated_select} "
        "FROM training_capture c "
        "JOIN training_delivery_snapshot s ON s.capture_id=c.capture_id "
        "JOIN training_capture_completion cc ON cc.capture_id=c.capture_id "
        "WHERE c.state='completed' AND c.revoked_at IS NULL "
        "AND c.redaction_status='redacted'"
    )
    max_rows = _max_export_rows()
    params: list[object] = []
    if namespace is not None:
        query += " AND c.namespace=?"
        params.append(namespace)
    rows = conn.execute(query + " ORDER BY c.capture_id LIMIT ?",
                        [*params, max_rows + 1]).fetchall()
    if len(rows) > max_rows:
        raise TrainingExportError(
            f"training export exceeds maximum of {max_rows} eligible captures"
        )
    revoked_query = (
        "SELECT count(*) FROM training_capture "
        "WHERE state='completed' AND revoked_at IS NOT NULL"
    )
    revoked_params: list[object] = []
    if namespace is not None:
        revoked_query += " AND namespace=?"
        revoked_params.append(namespace)
    revoked_count = int(conn.execute(revoked_query, revoked_params).fetchone()[0])
    excluded: Counter = Counter()
    if revoked_count:
        excluded["revoked"] = revoked_count
    all_snapshot_rows = _load_snapshot_rows(conn, namespace, limit=max_rows)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        capture_id = str(row["capture_id"])
        completion = conn.execute(
            "SELECT * FROM training_capture_completion WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        review = conn.execute(
            "SELECT * FROM training_capture_review WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        observations = conn.execute(
            "SELECT observation_kind, payload, payload_sha256 FROM training_capture_observation "
            "WHERE capture_id=? ORDER BY observed_at, observation_id", (capture_id,)
        ).fetchall()
        source_event_ids = _observation_event_ids(
            observations, capture_id=capture_id
        )
        # The independent snapshot query above deliberately includes captures
        # that are incomplete or excluded; this join only builds eligible rows.
        reason: str | None = None
        if not _completion_is_exportable(
                conn, {"capture": row, "completion": completion, "review": review}):
            reason = "missing_governance_or_outcome"
        elif row["revoked_at"] is not None:
            reason = "revoked"
        elif row["quarantine_reason"]:
            reason = "quarantine"
        elif row["redaction_status"] != "redacted":
            reason = "redaction_status"
        elif not all(row[key] for key in (
            "namespace", "session_id", "prompt", "assistant_response", "rendered", "effective_ops_json",
            "rendered_hash", "transform_version", "emitted_at",
            "consent_scope", "content_license", "redaction_policy_version",
            "export_consent_scope", "export_content_license",
            "evidence_id", "verifier_id", "verified_at",
            "acknowledgement_attestation", "outcome_kind", "outcome_value",
        )):
            reason = "missing_governance_or_outcome"
        elif str(row["outcome_kind"]) not in _OUTCOME_KINDS:
            reason = "invalid_outcome_kind"
        ops_tokens = _parse_ops(
            row["effective_ops_json"], capture_id=capture_id
        )
        evidence = None
        if reason is None:
            evidence = conn.execute("SELECT * FROM evidence WHERE id=?", (row["evidence_id"],)).fetchone()
            if evidence is None:
                reason = "missing_evidence"
        source_memory_ids: list[str] = []
        memory_rows: list[sqlite3.Row] = []
        if reason is None:
            source_memory_ids = [str(item[0]) for item in conn.execute(
                "SELECT memory_id FROM memory_evidence WHERE evidence_id=? ORDER BY memory_id",
                (row["evidence_id"],),
            ).fetchall()]
            if not source_memory_ids:
                reason = "missing_association"
            else:
                memory_rows = _memory_rows(conn, source_memory_ids)
                if len(memory_rows) != len(source_memory_ids):
                    reason = "missing_association"
                else:
                    _validate_update_lineage(conn, memory_rows)
                associated_json = row["associated_memory_ids_json"] if "associated_memory_ids_json" in row.keys() else None
                if reason is None and "associated_memory_ids_json" in completion_columns:
                    try:
                        parsed_ids = json.loads(associated_json) if associated_json is not None else None
                        recorded_ids = sorted(str(item) for item in parsed_ids) if isinstance(parsed_ids, list) else []
                    except (TypeError, ValueError):
                        recorded_ids = []
                    if recorded_ids != source_memory_ids:
                        reason = "association_mismatch"
        project_key = episode_id = None
        if reason is None:
            project_key, reason = _project_key(memory_rows)
        if reason is None:
            episode_id, reason = _episode_for_memories(conn, source_memory_ids)
        if reason is None:
            source_event_ids = _validated_source_event_ids(
                conn, {
                    "capture": row,
                    "completion": completion,
                    "source_event_ids": source_event_ids,
                }
            )
            if not source_event_ids:
                reason = "missing_source_event"
        label_status = None
        exclusion_reason = reason
        if reason is None:
            trust_floor = _parse_float_env(TRUST_FLOOR_ENV, DEFAULT_TRUST_FLOOR)
            negative = any(int(memory["violated_count"] or 0) >= 2 for memory in memory_rows)
            below_trust = any(float(memory["trust_score"] if memory["trust_score"] is not None else 1.0) < trust_floor for memory in memory_rows)
            positive = all(int(memory["applied_count"] or 0) >= 3 and int(memory["violated_count"] or 0) == 0 for memory in memory_rows)
            if negative:
                label_status, exclusion_reason = "candidate_negative", "violated_count"
            elif below_trust:
                label_status, exclusion_reason = None, "trust_floor"
            elif positive:
                label_status, exclusion_reason = "candidate_positive", None
            else:
                label_status, exclusion_reason = "unlabeled", None
        if label_status is None:
            excluded[exclusion_reason] += 1
            continue
        row_item: dict[str, Any] = {
            "capture_id": capture_id,
            "task_id": capture_id,
            "namespace": str(row["namespace"]),
            "project_key": project_key,
            "session_id": str(row["session_id"]),
            "episode_id": episode_id,
            "prompt": _redact(row["prompt"], max_bytes=MAX_CONTEXT_BYTES) or "",
            "context_fence": _redact(row["rendered"], max_bytes=MAX_CONTEXT_BYTES) or "",
            "ops_tokens": ops_tokens,
            "assistant_response": _redact(row["assistant_response"], max_bytes=MAX_CONTEXT_BYTES) or "",
            "outcome_kind": str(row["outcome_kind"]),
            "outcome_value": _redact(row["outcome_value"], max_bytes=MAX_CONTEXT_BYTES) or "",
            "evidence_ref": str(row["evidence_id"]),
            "source_memory_ids": source_memory_ids,
            "source_event_ids": source_event_ids,
             # Export authorization is recorded at completion time.  Capture
             # opt-in fields remain in SQLite for audit but never advertise
             # the license under which a row was released.
             "consent_scope": str(completion["export_consent_scope"]),
             "content_license": str(completion["export_content_license"]),
             "export_consent_scope": str(completion["export_consent_scope"]),
             "export_content_license": str(completion["export_content_license"]),
            "redaction_status": str(row["redaction_status"]),
            "redaction_policy_version": str(row["redaction_policy_version"]),
            "transform_version": str(row["transform_version"]),
            "label_status": label_status,
            "exclusion_reason": exclusion_reason,
            "memory_rows": memory_rows,
             "completion": completion,
            "correction_closeout": bool(row["correction_closeout"]),
        }
        candidates.append(row_item)
    if candidates:
        groups = _lineage_groups(candidates)
        for index, item in enumerate(candidates):
            item["lineage_group"] = groups[index]
    return candidates, excluded, all_snapshot_rows


def _assign_stable_splits(rows: Sequence[dict[str, Any]]) -> None:
    """Assign one immutable project split after lineage deduplication."""
    for item in rows:
        item["split_key"] = _split_key(str(item["project_key"]))
        item["split_bucket"] = _split_bucket(str(item["split_key"]))


def _materialize_sft(item: Mapping[str, Any]) -> dict[str, Any]:
    row = {key: item.get(key) for key in SFT_COLUMNS if key != "row_checksum"}
    row["row_checksum"] = _sha256_json(row)
    return row


def _build_rows(conn: sqlite3.Connection, *, namespace: str | None,
                snapshot_id: str, quarantine_raw: bool = False,
                split_salt: str | None = None) -> tuple[list[dict[str, Any]], Counter, list[dict[str, str]], list[dict[str, Any]]]:
    candidates, excluded, snapshot_rows = _candidate_rows(
        conn, namespace=namespace, snapshot_id=snapshot_id,
        quarantine_raw=quarantine_raw,
        split_salt=split_salt,
    )
    deduped, deletion_map = _deduplicate(candidates)
    _assign_stable_splits(deduped)
    for deletion in deletion_map:
        excluded[deletion["reason"]] += 1
    return [_materialize_sft(item) for item in deduped], excluded, deletion_map, snapshot_rows


def build_sft_rows(conn: sqlite3.Connection, *, namespace: str | None,
                   snapshot_id: str, reviewer_confirmed: bool = False,
                   quarantine_raw: bool = False) -> list[dict]:
    _require_confirmation(reviewer_confirmed)
    rows, _, _, _ = _build_rows(conn, namespace=namespace,
                                 snapshot_id=snapshot_id,
                                 quarantine_raw=quarantine_raw)
    return rows


def _preference_object(memory: sqlite3.Row, *, evidence_ref: str | None,
                       source_event_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "memory_id": str(memory["id"]),
        "content": _redact(memory["content"], max_bytes=16_000) or "",
        "source_event_ids": sorted(set(source_event_ids)),
        "evidence_ref": evidence_ref,
    }


def _preference_from_sft_item(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    completion = item["completion"]
    if item.get("label_status") == "candidate_negative":
        return []
    if (completion["outcome_kind"] != "reviewer_acceptance" or
            str(completion["outcome_value"]).casefold() != "accepted" or
            not str(completion["reviewer_id"] or "").strip() or
            not bool(completion["reviewer_confirmed"]) or
            not bool(completion["correction_closeout"])):
        return []
    results: list[dict[str, Any]] = []
    memory_by_id = {str(row["id"]): row for row in item["memory_rows"]}
    for chosen in item["memory_rows"]:
        predecessor_id = str(chosen["update_of"] or "")
        if not predecessor_id:
            continue
        predecessor = None
        try:
            # The predecessor is loaded by the caller into `_predecessor_rows`.
            predecessor = item.get("_predecessor_rows", {}).get(predecessor_id)
        except AttributeError:
            predecessor = None
        if (predecessor is None or
                str(predecessor["namespace"] or "") != str(chosen["namespace"] or "") or
                " ".join(str(predecessor["supersede_reason"] or "").split()).casefold() != "explicit correction"):
            continue
        chosen_obj = _preference_object(chosen, evidence_ref=item["evidence_ref"], source_event_ids=item["source_event_ids"])
        rejected_event_ids = item.get("_predecessor_event_ids", {}).get(predecessor_id, [])
        rejected_evidence = item.get("_predecessor_evidence", {}).get(predecessor_id)
        rejected_obj = _preference_object(predecessor, evidence_ref=rejected_evidence,
                                          source_event_ids=rejected_event_ids)
        pref = {
            "task_id": item["task_id"],
            "project_key": item["project_key"],
            "session_id": item["session_id"],
            "episode_id": item["episode_id"],
            "prompt": item["prompt"],
            "context_fence": item["context_fence"],
            "chosen": chosen_obj,
            "rejected": rejected_obj,
            "update_of": predecessor_id,
            "supersede_reason": "explicit correction",
            "source_memory_ids": list(item["source_memory_ids"]),
            "source_event_ids": list(item["source_event_ids"]),
            "evidence_ref": item["evidence_ref"],
            "consent_scope": item["consent_scope"],
            "content_license": item["content_license"],
            "redaction_status": item["redaction_status"],
            "redaction_policy_version": item["redaction_policy_version"],
            "split_key": item["split_key"],
            "transform_version": item["transform_version"],
        }
        pref["row_checksum"] = _sha256_json(pref)
        results.append(pref)
    return results


def _build_preference_rows(conn: sqlite3.Connection, *, namespace: str | None,
                           snapshot_id: str,
                           reviewer_confirmed: bool = False,
                           split_salt: str | None = None) -> list[dict]:
    _require_confirmation(reviewer_confirmed)
    # Preference generation shares the exact eligibility and dedup pass.  Add
    # predecessor provenance only after all canonical source rows are read.
    candidates, _, _ = _candidate_rows(conn, namespace=namespace,
                                       snapshot_id=snapshot_id,
                                       split_salt=split_salt)
    groups = _lineage_groups(candidates) if candidates else {}
    for index, item in enumerate(candidates):
        item["lineage_group"] = groups[index]
        predecessor_rows: dict[str, sqlite3.Row] = {}
        predecessor_events: dict[str, list[str]] = {}
        predecessor_evidence: dict[str, str | None] = {}
        for memory in item["memory_rows"]:
            predecessor_id = str(memory["update_of"] or "")
            if not predecessor_id:
                continue
            predecessor = conn.execute("SELECT * FROM memory WHERE id=?", (predecessor_id,)).fetchone()
            if predecessor is None:
                continue
            predecessor_rows[predecessor_id] = predecessor
            evidence_row = conn.execute(
                "SELECT evidence_id FROM memory_evidence WHERE memory_id=? ORDER BY evidence_id LIMIT 1",
                (predecessor_id,),
            ).fetchone()
            predecessor_evidence[predecessor_id] = str(evidence_row[0]) if evidence_row else None
            predecessor_events[predecessor_id] = []
        item["_predecessor_rows"] = predecessor_rows
        item["_predecessor_events"] = predecessor_events
        item["_predecessor_event_ids"] = predecessor_events
        item["_predecessor_evidence"] = predecessor_evidence
    # Dedup is required before preference output as well.  The list returned by
    # `_deduplicate` retains all canonical provenance in its keeper.
    deduped, _ = _deduplicate(candidates)
    _assign_stable_splits(deduped)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in deduped:
        for pref in _preference_from_sft_item(item):
            key = (str(pref["chosen"]["memory_id"]), str(pref["rejected"]["memory_id"]))
            if key not in seen:
                seen.add(key)
                rows.append(pref)
    return rows


def build_preference_rows(conn: sqlite3.Connection, *, namespace: str | None,
                          snapshot_id: str,
                          reviewer_confirmed: bool = False) -> list[dict]:
    return _build_preference_rows(
        conn, namespace=namespace, snapshot_id=snapshot_id,
        reviewer_confirmed=reviewer_confirmed,
    )


def _parquet_schemas(pa):
    sft_fields = []
    list_string = pa.list_(pa.string())
    for name in SFT_COLUMNS:
        if name in {"ops_tokens", "source_memory_ids", "source_event_ids"}:
            sft_fields.append(pa.field(name, list_string))
        elif name == "exclusion_reason":
            sft_fields.append(pa.field(name, pa.string(), nullable=True))
        else:
            sft_fields.append(pa.field(name, pa.string()))
    object_fields = pa.struct([
        pa.field("memory_id", pa.string()),
        pa.field("content", pa.string()),
        pa.field("source_event_ids", list_string),
        pa.field("evidence_ref", pa.string(), nullable=True),
    ])
    preference_fields = []
    for name in PREFERENCE_COLUMNS:
        if name in {"source_memory_ids", "source_event_ids"}:
            preference_fields.append(pa.field(name, list_string))
        elif name in {"chosen", "rejected"}:
            preference_fields.append(pa.field(name, object_fields))
        else:
            preference_fields.append(pa.field(name, pa.string()))
    return pa.schema(sft_fields), pa.schema(preference_fields)


def _write_parquet(path: Path, rows: Sequence[Mapping[str, Any]], schema, pa, pq) -> None:
    columns = {field.name: [row.get(field.name) for row in rows]
               for field in schema}
    table = pa.table(columns, schema=schema)
    metadata = dict(table.schema.metadata or {})
    metadata[b"created_by"] = b"zmem-training-v1"
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, path, compression="zstd", use_dictionary=False,
                   row_group_size=max(1, len(rows)))


def _bounded_quarantine_event(item: Mapping[str, Any], event_id: str) -> bytes:
    capture_id = str(item["capture"]["capture_id"])
    payload: dict[str, Any] = {
        "event_id": event_id,
        "capture_id": capture_id,
        "task_id": capture_id,
        "prompt": _redact(item["capture"]["prompt"], max_bytes=64),
        "assistant_response": _redact(item["capture"]["assistant_response"], max_bytes=64),
        "context_fence": _redact(item["capture"]["rendered"], max_bytes=64),
        "ops_tokens": _parse_ops(
            item["capture"]["effective_ops_json"], capture_id=capture_id
        ),
    }
    raw = _json_bytes(payload)
    if len(raw) <= QUARANTINE_EVENT_MAX_BYTES:
        return raw
    # Preserve valid JSON while meeting the strict byte cap by dropping large
    # content fields in a deterministic order, then shrinking the remainder.
    for key in ("assistant_response", "context_fence", "prompt", "task_id"):
        payload[key] = ""
        raw = _json_bytes(payload)
        if len(raw) <= QUARANTINE_EVENT_MAX_BYTES:
            return raw
    # Fixed identity fields are tiny in normal use; if a hostile capture has
    # oversized identifiers, bound them and retain valid JSON.
    for key in ("capture_id", "event_id"):
        payload[key] = str(payload[key])[:48]
    raw = _json_bytes(payload)
    if len(raw) > QUARANTINE_EVENT_MAX_BYTES:
        raise TrainingExportError(
            f"unable to bound quarantine event for capture {capture_id}"
        )
    return raw


def _write_quarantine(conn: sqlite3.Connection, staging: Path,
                      snapshot_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    # Keep only the deterministic lowest 50 event ids as input is streamed.
    # This bounds memory even when every capture has its full observation cap.
    selected: list[tuple[str, str, Mapping[str, Any]]] = []
    selected_ids: set[str] = set()
    for item in snapshot_rows:
        # The user-facing export contract is completion-gated for every
        # derived artifact. In particular, --quarantine-raw is not a side
        # channel for automatic partial captures.
        if not _completion_is_exportable(conn, item):
            continue
        capture_id = str(item["capture"]["capture_id"])
        ids = _validated_source_event_ids(conn, item)
        for event_id_value in ids:
            event_id = str(event_id_value)
            if event_id in selected_ids:
                continue
            if (len(selected) >= QUARANTINE_MAX_EVENTS
                    and event_id >= selected[-1][0]):
                continue
            selected.append((event_id, capture_id, item))
            selected.sort(key=lambda entry: (entry[0], entry[1]))
            selected_ids.add(event_id)
            if len(selected) > QUARANTINE_MAX_EVENTS:
                removed = selected.pop()
                selected_ids.discard(removed[0])
    qdir = staging / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    event_ids: list[str] = []
    for index, (event_id, _capture_id, item) in enumerate(selected):
        safe = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]
        (qdir / f"{index:03d}-{safe}.json").write_bytes(_bounded_quarantine_event(item, event_id))
        event_ids.append(event_id)
    return sorted(event_ids)


def _bind_export_snapshot(conn: sqlite3.Connection, *, snapshot_id: str,
                          binding_sha256: str) -> None:
    """Persist a one-way snapshot identity binding using SQLite's clock."""
    owns_tx = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        owns_tx = True
    try:
        conn.execute(
            "INSERT OR IGNORE INTO training_export_snapshot_binding "
            "(snapshot_id, binding_sha256, created_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (snapshot_id, binding_sha256),
        )
        row = conn.execute(
            "SELECT binding_sha256 FROM training_export_snapshot_binding "
            "WHERE snapshot_id=?", (snapshot_id,),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            str(row["binding_sha256"]), binding_sha256
        ):
            raise TrainingExportError(
                "snapshot_id is already bound to different export inputs"
            )
        if owns_tx:
            conn.commit()
    except Exception:
        if owns_tx and conn.in_transaction:
            conn.rollback()
        raise


def _write_training_views_unlocked(conn: sqlite3.Connection, *, out_dir: str,
                                   namespace: str | None, snapshot_id: str,
                                   reviewer_confirmed: bool = False,
                                   quarantine_raw: bool = False,
                                   split_salt: str | None = None) -> dict:
    """Publish one immutable read view directly under ``out_dir``.

    ``snapshot_id`` is the caller-assigned identity of this export read, and
    is deliberately distinct from each delivery snapshot id in SQLite. All
    source rows for the view are selected and rechecked in bounded SQLite read
    transactions, then this id is immutably bound to the selected inputs and
    output configuration before artifacts are published.
    """
    _require_confirmation(reviewer_confirmed)
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise ValueError("out_dir must be a non-empty path")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("snapshot_id must be a non-empty string")
    if conn.in_transaction:
        raise TrainingExportError(
            "training export requires a connection without an active transaction"
        )
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception as exc:
        raise TrainingExportError("pyarrow is required for export-training") from exc
    read_transaction_started = False
    if not conn.in_transaction:
        conn.execute("BEGIN")
        read_transaction_started = True
    try:
        # v1 split identity is immutable across callers, snapshots and
        # capture-set growth.  Keep the argument for source compatibility but
        # record the fixed contract value in every manifest.
        effective_split_salt = FIXED_SPLIT_SALT
        sft_rows, excluded, deletion_map, snapshot_rows = _build_rows(
            conn, namespace=namespace, snapshot_id=snapshot_id,
            quarantine_raw=quarantine_raw,
            split_salt=effective_split_salt,
        )
        preference_rows = _build_preference_rows(
            conn, namespace=namespace, snapshot_id=snapshot_id,
            reviewer_confirmed=True, split_salt=effective_split_salt,
        )
        source_fingerprint = _selection_fingerprint(conn, snapshot_rows, namespace)
        if read_transaction_started:
            conn.commit()
            read_transaction_started = False
        sft_schema, preference_schema = _parquet_schemas(pa)
        destination = Path(out_dir).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        embedding_model, embedding_revision = _embedding_identity()
        binding_sha256 = hashlib.sha256(_json_bytes({
            "destination": os.path.normcase(str(destination)),
            "namespace": namespace,
            "quarantine_raw": bool(quarantine_raw),
            "selection_sha256": source_fingerprint,
            "split_salt": effective_split_salt,
            "transform_version": TRANSFORM_VERSION,
            "manifest_schema_version": 1,
            "max_export_rows": _max_export_rows(),
            "trust_floor": _parse_float_env(TRUST_FLOOR_ENV, DEFAULT_TRUST_FLOOR),
            "dedup_threshold": _parse_float_env(
                DEDUP_THRESHOLD_ENV, DEFAULT_DEDUP_THRESHOLD
            ),
            "embedding_model": embedding_model,
            "embedding_revision": embedding_revision,
        })).hexdigest()
        staging = Path(tempfile.mkdtemp(prefix=".training-staging-", dir=str(destination.parent)))
        try:
            _write_parquet(staging / "sft-000.parquet", sft_rows, sft_schema, pa, pq)
            _write_parquet(staging / "preferences-000.parquet", preference_rows,
                           preference_schema, pa, pq)
            deletion_bytes = _json_bytes(deletion_map)
            (staging / "deletion-map.json").write_bytes(deletion_bytes)
            sft_bytes = (staging / "sft-000.parquet").read_bytes()
            preference_bytes = (staging / "preferences-000.parquet").read_bytes()
            if quarantine_raw:
                # Quarantine is derived from the same bounded source snapshot,
                # and only store-validated same-session evidence IDs survive.
                event_ids = _write_quarantine(conn, staging, snapshot_rows)
                quarantine_manifest = {"event_ids": event_ids}
                (staging / "quarantine-manifest.json").write_bytes(
                    _json_bytes(quarantine_manifest)
                )
            # Re-read the complete bounded input set after all artifacts are
            # staged. Keep the writer transaction through comparison and
            # snapshot registration so no source write can slip between them.
            verify_started = False
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                verify_started = True
            try:
                current_snapshot_rows = _load_snapshot_rows(
                    conn, namespace, limit=_max_export_rows()
                )
                current_fingerprint = _selection_fingerprint(
                    conn, current_snapshot_rows, namespace
                )
                if current_fingerprint != source_fingerprint:
                    raise TrainingExportError("training source changed during export")
                _bind_export_snapshot(
                    conn, snapshot_id=snapshot_id, binding_sha256=binding_sha256
                )
                if verify_started:
                    conn.commit()
                    verify_started = False
            except Exception:
                if verify_started:
                    conn.rollback()
                raise
            split_counts = Counter(_split_bucket(str(row["split_key"]))
                                   for row in sft_rows)
            manifest: dict[str, Any] = {
                "schema_version": 1,
                "format": "parquet",
                "snapshot_id": snapshot_id,
                "selection_sha256": source_fingerprint,
                "namespace": namespace,
                "transform_version": TRANSFORM_VERSION,
                "embedding_model": embedding_model,
                "embedding_revision": embedding_revision,
                "split_salt": effective_split_salt,
                "row_counts": {"preferences": len(preference_rows), "sft": len(sft_rows)},
                "excluded_counts": dict(sorted(excluded.items())),
                "split_counts": dict(sorted(split_counts.items())),
                "deletion_count": len(deletion_map),
                "deletion_map_sha256": hashlib.sha256(deletion_bytes).hexdigest(),
                "sft_sha256": hashlib.sha256(sft_bytes).hexdigest(),
                "preferences_sha256": hashlib.sha256(preference_bytes).hexdigest(),
                "artifact_sha256": {
                    "sft-000.parquet": hashlib.sha256(sft_bytes).hexdigest(),
                    "preferences-000.parquet": hashlib.sha256(preference_bytes).hexdigest(),
                    "deletion-map.json": hashlib.sha256(deletion_bytes).hexdigest(),
                },
                "governance": {
                    "policy": "SQLite authoritative; Parquet disposable; export requires reviewer confirmation",
                    "semantic_dedup": "in-memory only",
                    "canonical_training_content_mutated": False,
                    "snapshot_binding_registry_written": True,
                },
            }
            manifest_bytes = _json_bytes(manifest)
            (staging / "manifest.json").write_bytes(manifest_bytes)
            if destination.exists() and not destination.is_dir():
                raise TrainingExportError("refusing to overwrite non-directory training output")
            existing_manifest = destination / "manifest.json"
            if existing_manifest.is_file():
                try:
                    existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    existing = None
                if isinstance(existing, dict) and existing.get("snapshot_id") == snapshot_id:
                    if existing.get("selection_sha256") != source_fingerprint:
                        raise TrainingExportError(
                            "snapshot_id is already bound to a different selection"
                        )
            destination.mkdir(parents=True, exist_ok=True)
            # A prior manifest is a completion marker for the previous
            # generation.  Remove it before replacing any data file so a
            # failed rerun cannot leave old metadata advertising new files.
            old_manifest = destination / "manifest.json"
            if old_manifest.exists():
                old_manifest.unlink()
            shutil.rmtree(destination / "quarantine", ignore_errors=True)
            stale_quarantine_manifest = destination / "quarantine-manifest.json"
            if stale_quarantine_manifest.exists():
                stale_quarantine_manifest.unlink()
            for filename in ("sft-000.parquet", "preferences-000.parquet", "deletion-map.json"):
                os.replace(staging / filename, destination / filename)
            if quarantine_raw:
                qdest = destination / "quarantine"
                qdest.mkdir(parents=True, exist_ok=True)
                for path in sorted((staging / "quarantine").iterdir()):
                    os.replace(path, qdest / path.name)
                os.replace(staging / "quarantine-manifest.json", destination / "quarantine-manifest.json")
            os.replace(staging / "manifest.json", destination / "manifest.json")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "sft_count": len(sft_rows),
            "preference_count": len(preference_rows),
            "deletion_count": len(deletion_map),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    except Exception:
        if read_transaction_started:
            conn.rollback()
        raise


@contextmanager
def _training_output_lock(destination: Path):
    """Take a persistent sibling advisory lock for one output directory.

    The marker is intentionally never unlinked.  Its byte contents are only a
    durable operator hint; ownership is the OS lock, which releases when a
    process exits.  This is designed for local filesystems.  The process guard
    is needed because some advisory-lock implementations are process-scoped
    and would otherwise allow duplicate callers in one interpreter.
    """
    lock_path = destination.parent / f".{destination.name}.training-export.lock"
    canonical = str(lock_path.resolve())
    fd: int | None = None
    acquired = False
    try:
        with _PROCESS_LOCK_GUARD:
            if canonical in _ACTIVE_OUTPUT_LOCKS:
                raise TrainingExportError("training export already in progress")
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            os.set_inheritable(fd, False)
            if os.name == "nt":
                import msvcrt

                # msvcrt.locking requires a byte range.  Preserve any legacy
                # marker contents and only seed an empty marker when needed.
                if os.fstat(fd).st_size == 0:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise TrainingExportError(
                        "training export already in progress"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise TrainingExportError(
                        "training export already in progress"
                    ) from exc
            _ACTIVE_OUTPUT_LOCKS.add(canonical)
            acquired = True
    except TrainingExportError:
        if fd is not None and not acquired:
            os.close(fd)
        raise
    except OSError as exc:
        if fd is not None and not acquired:
            os.close(fd)
        raise TrainingExportError(
            f"unable to lock training export destination: {lock_path}"
        ) from exc
    try:
        yield
    finally:
        if fd is not None:
            if os.name == "nt":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
        with _PROCESS_LOCK_GUARD:
            _ACTIVE_OUTPUT_LOCKS.discard(canonical)


def write_training_views(conn: sqlite3.Connection, *, out_dir: str,
                         namespace: str | None, snapshot_id: str,
                         reviewer_confirmed: bool = False,
                         quarantine_raw: bool = False,
                         split_salt: str | None = None) -> dict:
    """Publish a completion-gated, coherently locked derived view."""
    destination = Path(out_dir).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _training_output_lock(destination):
        return _write_training_views_unlocked(
            conn, out_dir=out_dir, namespace=namespace,
            snapshot_id=snapshot_id, reviewer_confirmed=reviewer_confirmed,
            quarantine_raw=quarantine_raw, split_salt=split_salt,
        )


__all__ = [
    "PREFERENCE_COLUMNS", "SFT_COLUMNS", "TrainingExportError",
    "build_preference_rows", "build_sft_rows", "write_training_views",
]
