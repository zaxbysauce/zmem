from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "skills" / "memory" / "scripts"))

from storelib import schema
from storelib.training import (
    PREFERENCE_COLUMNS,
    SFT_COLUMNS,
    TrainingExportError,
    build_preference_rows,
    build_sft_rows,
    write_training_views,
)


class TrainingExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)
        self._old_profile = os.environ.get("ZMEM_EMBED_PROFILE")
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"

    def tearDown(self) -> None:
        if self._old_profile is None:
            os.environ.pop("ZMEM_EMBED_PROFILE", None)
        else:
            os.environ["ZMEM_EMBED_PROFILE"] = self._old_profile
        self.conn.close()

    def _seed_capture(self, *, capture_id: str = "cap-1", task_id: str = "task-1",
                      memory_id: str = "mem-1", evidence_id: str = "ev-1",
                      event_id: str = "event-1", applied: int = 3,
                      violated: int = 0, trust: float = 1.0) -> None:
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
            "trust_score, applied_count, violated_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, "project:demo", "fact", "Use the safe deploy command",
             "2026-01-01T00:00:00Z", trust, applied, violated),
        )
        self.conn.execute(
            "INSERT INTO episode (id, namespace, started_at) VALUES (?, ?, ?)",
            ("episode-1", "project:demo", "2026-01-01T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO episode_memory (episode_id, memory_id) VALUES (?, ?)",
            ("episode-1", memory_id),
        )
        self.conn.execute(
            "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, "session-1", "test", "stop", "test_result",
             "2026-01-01T00:00:00Z", "hash", "passed", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        self.conn.execute(
            "INSERT INTO training_capture (capture_id, host, host_task_id, session_id, namespace, "
            "created_at, updated_at, finalized_at, acknowledged_at, acknowledgement_attestation, state, "
            "prompt, assistant_response, consent_scope, content_license, redaction_status, "
            "redaction_policy_version, governance_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, "fixture", task_id, "session-1", "project:demo",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z", "{}", "completed", "What command should I use?",
             "Use the safe deploy command.", "fixture-scope", "fixture-license", "redacted",
             "policy-v1", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO training_delivery_snapshot (delivery_snapshot_id, capture_id, rendered, "
            "effective_ops_json, rendered_hash, transform_version, emitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("delivery-1", capture_id, "<context>safe</context>",
             json.dumps(["deploy --safe"]), "hash", "transform-v1", "2026-01-01T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO training_capture_completion (capture_id, evidence_id, verifier_id, verified_at, "
            "outcome_kind, outcome_value, acknowledgement_attestation, export_consent_scope, "
            "export_content_license, reviewer_confirmed, correction_closeout, associated_memory_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, evidence_id, "verifier", "2026-01-01T00:00:00Z", "test", "passed",
             "{}", "fixture-export-scope", "fixture-export-license", 0, 0,
             json.dumps([memory_id])),
        )
        self.conn.execute(
            "INSERT INTO training_capture_observation (observation_id, capture_id, observation_kind, payload, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("obs-1", capture_id, "event", json.dumps({"source_event_id": event_id}),
             "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()

    def test_confirmation_is_checked_before_query(self) -> None:
        class NoQuery:
            def execute(self, *args, **kwargs):
                raise AssertionError("query should not run")

        with self.assertRaisesRegex(ValueError, "reviewer confirmation required"):
            build_sft_rows(NoQuery(), namespace=None, snapshot_id="snap")
        with self.assertRaisesRegex(ValueError, "reviewer confirmation required"):
            build_preference_rows(NoQuery(), namespace=None, snapshot_id="snap")

    def test_empty_export_has_exact_schema_and_no_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = write_training_views(
                self.conn, out_dir=tmp, namespace=None, snapshot_id="snapshot-empty",
                reviewer_confirmed=True,
            )
            self.assertEqual(result["sft_count"], 0)
            self.assertEqual(result["preference_count"], 0)
            training = Path(tmp)
            self.assertTrue((training / "manifest.json").is_file())
            self.assertFalse((training / "quarantine").exists())
            import pyarrow.parquet as pq

            self.assertEqual(pq.read_schema(training / "sft-000.parquet").names,
                             list(SFT_COLUMNS))
            self.assertEqual(pq.read_schema(training / "preferences-000.parquet").names,
                             list(PREFERENCE_COLUMNS))
            self.assertEqual(
                list(PREFERENCE_COLUMNS),
                [
                    "task_id", "project_key", "session_id", "episode_id", "prompt",
                    "context_fence", "chosen", "rejected", "update_of",
                    "supersede_reason", "source_memory_ids", "source_event_ids",
                    "evidence_ref", "consent_scope", "content_license",
                    "redaction_status", "redaction_policy_version", "split_key",
                    "transform_version", "row_checksum",
                ],
            )
            manifest = json.loads((training / "manifest.json").read_text())
            self.assertEqual(manifest["row_counts"], {"preferences": 0, "sft": 0})

    def test_completed_capture_exports_redacted_sft_row(self) -> None:
        self._seed_capture()
        before = self.conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        with tempfile.TemporaryDirectory() as tmp:
            result = write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo", snapshot_id="snapshot-1",
                reviewer_confirmed=True,
            )
            self.assertEqual(result["sft_count"], 1)
            self.assertEqual(result["preference_count"], 0)
            import pyarrow.parquet as pq

            row = pq.read_table(Path(tmp) / "sft-000.parquet").to_pylist()[0]
            self.assertEqual(row["task_id"], "task-1")
            self.assertEqual(row["source_memory_ids"], ["mem-1"])
            self.assertEqual(row["source_event_ids"], ["event-1"])
            self.assertEqual(row["label_status"], "candidate_positive")
            self.assertEqual(len(row["row_checksum"]), 64)
            self.assertEqual(before, self.conn.execute("SELECT count(*) FROM memory").fetchone()[0])

    def test_reexport_cleans_quarantine_and_keeps_process_split_salt(self) -> None:
        self._seed_capture()
        with tempfile.TemporaryDirectory() as tmp:
            old_salt = os.environ.get("ZMEM_TRAINING_SPLIT_SALT")
            os.environ["ZMEM_TRAINING_SPLIT_SALT"] = "caller-salt"
            try:
                write_training_views(
                    self.conn, out_dir=tmp, namespace="project:demo",
                    snapshot_id="snapshot-1", reviewer_confirmed=True,
                    quarantine_raw=True, split_salt="per-call-salt",
                )
                self.assertTrue((Path(tmp) / "quarantine").is_dir())
                self.assertEqual(os.environ["ZMEM_TRAINING_SPLIT_SALT"], "caller-salt")
                write_training_views(
                    self.conn, out_dir=tmp, namespace="project:demo",
                    snapshot_id="snapshot-2", reviewer_confirmed=True,
                    quarantine_raw=False, split_salt="per-call-salt-2",
                )
            finally:
                if old_salt is None:
                    os.environ.pop("ZMEM_TRAINING_SPLIT_SALT", None)
                else:
                    os.environ["ZMEM_TRAINING_SPLIT_SALT"] = old_salt
            training = Path(tmp)
            self.assertFalse((training / "quarantine").exists())
            self.assertFalse((training / "quarantine-manifest.json").exists())
            manifest = json.loads((training / "manifest.json").read_text())
            self.assertEqual(
                manifest["sft_sha256"],
                hashlib.sha256((training / "sft-000.parquet").read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["preferences_sha256"],
                hashlib.sha256((training / "preferences-000.parquet").read_bytes()).hexdigest(),
            )

    def test_negative_label_is_emitted_with_violation_reason(self) -> None:
        self._seed_capture(violated=2, applied=0)
        rows = build_sft_rows(
            self.conn, namespace="project:demo", snapshot_id="snapshot-negative",
            reviewer_confirmed=True,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label_status"], "candidate_negative")
        self.assertEqual(rows[0]["exclusion_reason"], "violated_count")

    def test_explicit_correction_reviewer_completion_emits_preference(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
            "trust_score, applied_count, violated_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem-predecessor", "project:demo", "fact", "Use the old deploy command",
             "2026-01-01T00:00:00Z", 1.0, 0, 0),
        )
        self.conn.execute(
            "INSERT INTO episode_memory (episode_id, memory_id) VALUES (?, ?)",
            ("episode-1", "mem-predecessor"),
        )
        self.conn.execute(
            "UPDATE memory SET update_of=? WHERE id=?",
            ("mem-predecessor", "mem-1"),
        )
        self.conn.execute(
            "UPDATE memory SET supersede_reason=? WHERE id=?",
            ("explicit correction", "mem-predecessor"),
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_kind=?, outcome_value=?, "
            "reviewer_id=?, reviewer_confirmed=1, correction_closeout=1 WHERE capture_id=?",
            ("reviewer_acceptance", "accepted", "reviewer-1", "cap-1"),
        )
        self.conn.commit()
        rows = build_preference_rows(
            self.conn, namespace="project:demo", snapshot_id="snapshot-preference",
            reviewer_confirmed=True,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["update_of"], "mem-predecessor")
        self.assertEqual(row["chosen"]["memory_id"], "mem-1")
        self.assertEqual(row["rejected"]["memory_id"], "mem-predecessor")
        self.assertEqual(row["rejected"]["source_event_ids"], [])
        self.assertIsNone(row["rejected"]["evidence_ref"])

    def test_trust_floor_is_excluded_from_sft(self) -> None:
        self._seed_capture(trust=0.1, applied=3)
        with tempfile.TemporaryDirectory() as tmp:
            result = write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="snapshot-trust", reviewer_confirmed=True,
            )
            self.assertEqual(result["sft_count"], 0)
            manifest = json.loads(
                (Path(tmp) / "manifest.json").read_text()
            )
            self.assertEqual(manifest["excluded_counts"].get("trust_floor"), 1)

    def test_snapshot_id_cannot_rebind_to_changed_source(self) -> None:
        self._seed_capture()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="immutable-snapshot", reviewer_confirmed=True,
            )
            manifest_path = Path(tmp) / "manifest.json"
            before = manifest_path.read_bytes()
            self.conn.execute(
                "UPDATE training_capture SET prompt=? WHERE capture_id=?",
                ("Changed after export", "cap-1"),
            )
            self.conn.commit()
            with self.assertRaisesRegex(
                TrainingExportError, "different selection"
            ):
                write_training_views(
                    self.conn, out_dir=tmp, namespace="project:demo",
                    snapshot_id="immutable-snapshot", reviewer_confirmed=True,
                )
            self.assertEqual(manifest_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
