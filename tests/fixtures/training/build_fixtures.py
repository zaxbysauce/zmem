"""Regenerate deterministic training exporter golden fixtures.

The fixture input is a compact description of an isolated SQLite store. This
script seeds that store and asks the production exporter to write the actual
Parquet and JSON artifacts before copying those bytes to the tracked goldens.
Run from the repository root::

    python tests/fixtures/training/build_fixtures.py --output tests/fixtures/training
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_ROOT = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from storelib import schema  # noqa: E402
from storelib.training import write_training_views  # noqa: E402


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _insert_fixture(conn: sqlite3.Connection, cases: dict[str, Any]) -> None:
    """Seed only canonical store tables used by the real exporter."""
    for memory in cases["memories"]:
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
            "trust_score, applied_count, violated_count, update_of, supersede_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory["id"], memory["namespace"], memory.get("type", "fact"),
                memory["content"], memory.get("ingestion_ts", "2026-01-01T00:00:00Z"),
                memory.get("trust_score", 1.0), memory.get("applied_count", 3),
                memory.get("violated_count", 0), memory.get("update_of", ""),
                memory.get("supersede_reason", ""),
            ),
        )
    for episode in cases["episodes"]:
        conn.execute(
            "INSERT INTO episode (id, namespace, started_at) VALUES (?, ?, ?)",
            (episode["id"], episode["namespace"], episode["started_at"]),
        )
    conn.executemany(
        "INSERT INTO episode_memory (episode_id, memory_id) VALUES (?, ?)",
        ((row["episode_id"], row["memory_id"]) for row in cases["episode_memory"]),
    )
    for evidence in cases["evidence"]:
        conn.execute(
            "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, "
            "excerpt, ref_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence["id"], evidence["session_id"], evidence.get("lane", "fixture"),
                evidence.get("moment", "stop"), evidence["kind"], evidence["ts"],
                evidence.get("hash", evidence["id"]), evidence.get("excerpt", "fixture"),
                evidence.get("ref_path", "fixtures/training"),
            ),
        )
    conn.executemany(
        "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
        ((row["memory_id"], row["evidence_id"]) for row in cases["memory_evidence"]),
    )
    for capture in cases["captures"]:
        conn.execute(
            "INSERT INTO training_capture (capture_id, host, host_task_id, session_id, "
            "namespace, created_at, updated_at, finalized_at, acknowledged_at, "
            "acknowledgement_attestation, state, prompt, assistant_response, "
            "consent_scope, content_license, redaction_status, redaction_policy_version, "
            "governance_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                capture["capture_id"], capture.get("host", "fixture"),
                capture["host_task_id"], capture["session_id"], capture["namespace"],
                capture.get("created_at", "2026-01-01T00:00:00Z"),
                capture.get("updated_at", "2026-01-01T00:00:00Z"),
                capture.get("finalized_at", "2026-01-01T00:00:00Z"),
                capture.get("acknowledged_at", "2026-01-01T00:00:00Z"),
                capture.get("attestation", "fixture-attestation"),
                capture.get("state", "completed"), capture["prompt"],
                capture["assistant_response"], capture.get("consent_scope", "fixture-scope"),
                capture.get("content_license", "fixture-license"),
                capture.get("redaction_status", "redacted"),
                capture.get("redaction_policy_version", "policy-v1"),
                capture.get("governance_source", "fixture"),
            ),
        )
    for delivery in cases["deliveries"]:
        conn.execute(
            "INSERT INTO training_delivery_snapshot (delivery_snapshot_id, capture_id, "
            "rendered, effective_ops_json, rendered_hash, transform_version, emitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                delivery["delivery_snapshot_id"], delivery["capture_id"],
                delivery["rendered"], json.dumps(delivery["ops"], separators=(",", ":")),
                delivery.get("rendered_hash", delivery["delivery_snapshot_id"]),
                delivery.get("transform_version", "fixture-transform"),
                delivery.get("emitted_at", "2026-01-01T00:00:00Z"),
            ),
        )
    for completion in cases["completions"]:
        capture = next(item for item in cases["captures"]
                       if item["capture_id"] == completion["capture_id"])
        conn.execute(
            "INSERT INTO training_capture_completion (capture_id, evidence_id, verifier_id, "
            "verified_at, outcome_kind, outcome_value, acknowledgement_attestation, "
            "export_consent_scope, export_content_license, reviewer_id, reviewer_confirmed, "
            "correction_closeout, correction_chain_id, associated_memory_ids_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                completion["capture_id"], completion["evidence_id"],
                completion.get("verifier_id", "fixture-verifier"),
                completion.get("verified_at", "2026-01-01T00:00:00Z"),
                completion["outcome_kind"], completion["outcome_value"],
                capture.get("attestation", "fixture-attestation"),
                completion.get("export_consent_scope", "fixture-export"),
                completion.get("export_content_license", "fixture-license"),
                completion.get("reviewer_id"), int(completion.get("reviewer_confirmed", False)),
                int(completion.get("correction_closeout", False)),
                completion.get("correction_chain_id"),
                json.dumps(completion["memory_ids"], separators=(",", ":")),
            ),
        )
        if completion["outcome_kind"] == "reviewer_acceptance":
            conn.execute(
                "INSERT INTO training_capture_review "
                "(capture_id, completion_evidence_id, reviewer_id, reviewed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    completion["capture_id"], completion["evidence_id"],
                    completion["reviewer_id"],
                    completion.get("reviewed_at", "2026-01-01T00:00:01Z"),
                ),
            )
    capture_by_id = {item["capture_id"]: item for item in cases["captures"]}
    for observation in cases["observations"]:
        event_id = observation.get("payload", {}).get("source_event_id")
        capture = capture_by_id[observation["capture_id"]]
        if event_id:
            conn.execute(
                "INSERT OR IGNORE INTO evidence "
                "(id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event_id), capture["session_id"], "test", "event",
                    "test_result", "2026-01-01T00:00:05Z", "event-hash",
                    "fixture event", "fixture",
                ),
            )
        payload = json.dumps(observation["payload"], separators=(",", ":"))
        conn.execute(
            "INSERT INTO training_capture_observation "
            "(observation_id, capture_id, observation_kind, payload, payload_sha256, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                observation["observation_id"], observation["capture_id"],
                observation.get("observation_kind", "event"), payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                observation.get("observed_at", "2026-01-01T00:00:00Z"),
            ),
        )
    conn.commit()


def load_cases() -> dict[str, Any]:
    fixture_dir = Path(__file__).resolve().parent
    return json.loads((fixture_dir / "cases.json").read_text(encoding="utf-8"))


def generate(output: Path) -> None:
    cases = load_cases()
    output.mkdir(parents=True, exist_ok=True)
    old_profile = os.environ.get("ZMEM_EMBED_PROFILE")
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    try:
        with tempfile.TemporaryDirectory(prefix="training-fixture-") as tmp:
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            try:
                schema.init_db(conn)
                schema.migrate(conn)
                _insert_fixture(conn, cases)
                generated = Path(tmp) / "training"
                write_training_views(
                    conn, out_dir=str(generated), namespace=cases["namespace"],
                    snapshot_id=cases["snapshot_id"], reviewer_confirmed=True,
                )
                names = {
                    "sft-000.parquet": "expected-sft.parquet",
                    "preferences-000.parquet": "expected-preferences.parquet",
                    "deletion-map.json": "expected-deletion-map.json",
                }
                for source, target in names.items():
                    shutil.copyfile(generated / source, output / target)
                import pyarrow.parquet as pq
                (output / "expected-sft.json").write_bytes(
                    _json_bytes(pq.read_table(generated / "sft-000.parquet").to_pylist()))
                (output / "expected-preferences.json").write_bytes(
                    _json_bytes(pq.read_table(generated / "preferences-000.parquet").to_pylist()))
            finally:
                conn.close()
    finally:
        if old_profile is None:
            os.environ.pop("ZMEM_EMBED_PROFILE", None)
        else:
            os.environ["ZMEM_EMBED_PROFILE"] = old_profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    generate(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
