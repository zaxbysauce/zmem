from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

# Keep import-time store resolution away from the operator store when this
# module is run directly or loaded by unittest discovery. Storelib caches this
# temporary location, then the ambient process environment is restored before
# discovery can import another module.
_IMPORT_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_EMBED_PROFILE", "ZMEM_MODEL_AUTODOWNLOAD")
_IMPORT_ENV = {key: os.environ.get(key) for key in _IMPORT_KEYS}
_IMPORT_TMP = tempfile.TemporaryDirectory(prefix="zmem-training-exporter-import-")
_IMPORT_ROOT = Path(_IMPORT_TMP.name)
os.environ.update({
    "ZMEM_STORE": str(_IMPORT_ROOT / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_ROOT / "data"),
    "ZMEM_EMBED_PROFILE": "fake",
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
})
sys.path.insert(0, str(Path(__file__).parents[1] / "skills" / "memory" / "scripts"))

try:
    from storelib import schema
    from storelib import training as training_module
    from storelib.training import (
        PREFERENCE_COLUMNS,
        QUARANTINE_EVENT_MAX_BYTES,
        SFT_COLUMNS,
        TrainingExportError,
        _bounded_quarantine_event,
        _load_snapshot_rows,
        _lineage_groups,
        _opaque_project_label,
        _redact,
        _split_bucket,
        _split_key,
        _training_output_lock,
        build_preference_rows,
        build_sft_rows,
        write_training_views,
    )
finally:
    for _key, _value in _IMPORT_ENV.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value
    _IMPORT_TMP.cleanup()


class TrainingExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)
        self._path_fixture = tempfile.TemporaryDirectory(
            prefix="zmem-training-exporter-path-")
        self._path_root = Path(self._path_fixture.name)
        self._old_profile = os.environ.get("ZMEM_EMBED_PROFILE")
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"

    def tearDown(self) -> None:
        if self._old_profile is None:
            os.environ.pop("ZMEM_EMBED_PROFILE", None)
        else:
            os.environ["ZMEM_EMBED_PROFILE"] = self._old_profile
        self.conn.close()
        self._path_fixture.cleanup()

    def _seed_capture(self, *, capture_id: str = "cap-1", task_id: str = "task-1",
                      memory_id: str = "mem-1", evidence_id: str = "ev-1",
                      event_id: str = "event-1", applied: int = 3,
                      violated: int = 0, trust: float = 1.0,
                      evidence_kind: str = "test_result") -> None:
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
            (evidence_id, "session-1", "test", "stop", evidence_kind,
             "2026-01-01T00:00:00Z", "hash", "passed", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        if event_id != evidence_id:
            self.conn.execute(
                "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, "session-1", "test", "event", evidence_kind,
                 "2026-01-01T00:00:01Z", "event-hash", "event", "fixture"),
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
             json.dumps(["deploy --safe"]),
             hashlib.sha256(b"<context>safe</context>").hexdigest(),
             "transform-v1", "2026-01-01T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO training_capture_completion (capture_id, evidence_id, verifier_id, verified_at, "
            "outcome_kind, outcome_value, acknowledgement_attestation, export_consent_scope, "
            "export_content_license, reviewer_confirmed, correction_closeout, associated_memory_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, evidence_id, "verifier", "2026-01-01T00:00:00Z", "test", "passed",
             "{}", "fixture-export-scope", "fixture-export-license", 0, 0,
             json.dumps([memory_id])),
        )
        observation_payload = json.dumps({"source_event_id": event_id})
        self.conn.execute(
            "INSERT INTO training_capture_observation "
            "(observation_id, capture_id, observation_kind, payload, payload_sha256, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("obs-1", capture_id, "event", observation_payload,
             hashlib.sha256(observation_payload.encode("utf-8")).hexdigest(),
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
            self.assertEqual(manifest["format"], "parquet")
            self.assertEqual(manifest["transform_version"], "training-v1")
            self.assertFalse(manifest["governance"]["canonical_training_content_mutated"])
            self.assertTrue(manifest["governance"]["snapshot_binding_registry_written"])
            self.assertFalse(manifest["governance"]["canonical_training_content_mutated"])
            self.assertTrue(manifest["governance"]["snapshot_binding_registry_written"])
            self.assertEqual(manifest["row_counts"], {"preferences": 0, "sft": 0})
            self.assertEqual(
                pq.read_schema(training / "sft-000.parquet").metadata[b"created_by"],
                b"zmem-training-v1",
            )

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
            self.assertEqual(row["task_id"], "cap-1")
            self.assertEqual(row["source_memory_ids"], ["mem-1"])
            self.assertEqual(row["source_event_ids"], ["ev-1", "event-1"])
            self.assertEqual(row["label_status"], "candidate_positive")
            self.assertEqual(len(row["row_checksum"]), 64)
            self.assertEqual(before, self.conn.execute("SELECT count(*) FROM memory").fetchone()[0])

    def test_rendered_hash_corruption_is_excluded_before_materialization(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_delivery_snapshot SET rendered=? WHERE capture_id=?",
            ("tampered rendered context", "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            result = write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="corrupt-rendered", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            self.assertEqual(result["sft_count"], 0)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["excluded_counts"], {"snapshot_integrity": 1})

    def test_completed_row_binds_export_governance_and_full_projection(self) -> None:
        self._seed_capture()
        row = build_sft_rows(
            self.conn, namespace="project:demo", snapshot_id="projection",
            reviewer_confirmed=True,
        )[0]
        self.assertEqual(
            {key: row[key] for key in (
                "prompt", "context_fence", "ops_tokens", "assistant_response",
                "outcome_kind", "outcome_value", "evidence_ref",
                "consent_scope", "content_license", "redaction_status",
                "redaction_policy_version",
            )},
            {
                "prompt": "What command should I use?",
                "context_fence": "<context>safe</context>",
                "ops_tokens": ["deploy --safe"],
                "assistant_response": "Use the safe deploy command.",
                "outcome_kind": "test",
                "outcome_value": "passed",
                "evidence_ref": "ev-1",
                "consent_scope": "fixture-export-scope",
                "content_license": "fixture-export-license",
                "redaction_status": "redacted",
                "redaction_policy_version": "policy-v1",
            },
        )
        self.assertIn(row["split_key"], {"train", "validation", "test"})
        self.assertEqual(row["project_key"], _opaque_project_label("project:demo"))
        checksum_input = {key: row[key] for key in SFT_COLUMNS
                          if key != "row_checksum"}
        expected = hashlib.sha256(
            (json.dumps(checksum_input, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")) + "\n").encode("utf-8")
        ).hexdigest()
        self.assertEqual(row["row_checksum"], expected)

    def test_export_enforces_evidence_outcome_compatibility(self) -> None:
        self._seed_capture(evidence_kind="turn")
        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_kind=?, outcome_value=? "
            "WHERE capture_id=?",
            ("user_acceptance", "accepted", "cap-1"),
        )
        self.conn.commit()
        accepted = build_sft_rows(
            self.conn, namespace="project:demo", snapshot_id="user-accepted",
            reviewer_confirmed=True,
        )
        self.assertEqual(len(accepted), 1)

        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_kind=?, outcome_value=? "
            "WHERE capture_id=?",
            ("test", "passed", "cap-1"),
        )
        self.conn.commit()
        self.assertEqual(
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="mismatched",
                reviewer_confirmed=True,
            ),
            [],
        )

    def test_capture_uuid_fallback_exports_without_host_task_id(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture SET host_task_id=NULL WHERE capture_id=?",
            ("cap-1",),
        )
        self.conn.commit()
        row = build_sft_rows(
            self.conn, namespace="project:demo", snapshot_id="no-host-task",
            reviewer_confirmed=True,
        )[0]
        self.assertEqual(row["task_id"], "cap-1")

    def test_snapshot_binding_includes_output_affecting_thresholds(self) -> None:
        self._seed_capture(trust=0.25, applied=3)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ZMEM_INJECT_FLOOR_TRUST": "0.2"}):
                write_training_views(
                    self.conn, out_dir=tmp, namespace="project:demo",
                    snapshot_id="threshold-bound", reviewer_confirmed=True,
                )
            before = (Path(tmp) / "manifest.json").read_bytes()
            with patch.dict(os.environ, {"ZMEM_INJECT_FLOOR_TRUST": "0.3"}):
                with self.assertRaisesRegex(
                    TrainingExportError, "different export inputs"
                ):
                    write_training_views(
                        self.conn, out_dir=tmp, namespace="project:demo",
                        snapshot_id="threshold-bound", reviewer_confirmed=True,
                    )
            self.assertEqual((Path(tmp) / "manifest.json").read_bytes(), before)

    def test_missing_pyarrow_fails_before_creating_output(self) -> None:
        self._seed_capture()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "sys.modules", {"pyarrow": None, "pyarrow.parquet": None}
            ):
                with self.assertRaisesRegex(TrainingExportError, "pyarrow is required"):
                    write_training_views(
                        self.conn, out_dir=tmp, namespace="project:demo",
                        snapshot_id="without-pyarrow", reviewer_confirmed=True,
                    )
            self.assertFalse((Path(tmp) / "manifest.json").exists())
            self.assertFalse(any(Path(tmp).glob(".training-staging-*")))

    def test_observation_payload_digest_tampering_fails_closed(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture_observation SET payload=? WHERE observation_id=?",
            (json.dumps({"source_event_id": "forged-event"}), "obs-1"),
        )
        self.conn.commit()
        with self.assertRaisesRegex(TrainingExportError, "digest mismatch"):
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="tampered-observation",
                reviewer_confirmed=True,
            )

    def test_unknown_and_foreign_observation_ids_are_not_exported(self) -> None:
        self._seed_capture()
        foreign_payload = json.dumps({
            "source_event_ids": ["unknown-event", "foreign-event"]
        })
        self.conn.execute(
            "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("foreign-event", "another-session", "test", "event", "test_result",
             "2026-01-01T00:00:02Z", "foreign-hash", "foreign", "fixture"),
        )
        self.conn.execute(
            "UPDATE training_capture_observation SET payload=?, payload_sha256=? "
            "WHERE observation_id=?",
            (foreign_payload, hashlib.sha256(foreign_payload.encode("utf-8")).hexdigest(),
             "obs-1"),
        )
        self.conn.commit()
        row = build_sft_rows(
            self.conn, namespace="project:demo", snapshot_id="foreign-observation",
            reviewer_confirmed=True,
        )[0]
        self.assertEqual(row["source_event_ids"], ["ev-1"])

    def test_reviewer_acceptance_without_distinct_review_is_not_exportable(self) -> None:
        self._seed_capture(evidence_kind="correction")
        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_kind=?, outcome_value=?, "
            "reviewer_id=?, reviewer_confirmed=1, correction_closeout=1 WHERE capture_id=?",
            ("reviewer_acceptance", "accepted", "verifier", "cap-1"),
        )
        self.conn.commit()
        self.assertEqual(
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="self-reviewed",
                reviewer_confirmed=True,
            ),
            [],
        )

    def test_revoked_completion_is_reported_in_exclusion_counts(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture SET revoked_at=? WHERE capture_id=?",
            ("2026-01-02T00:00:00Z", "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="revoked-count", reviewer_confirmed=True,
            )
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(manifest["excluded_counts"].get("revoked"), 1)

    def test_foreign_associated_memory_is_excluded_by_namespace(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, trust_score, applied_count, violated_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem-foreign", "project:other", "fact", "Foreign source", "2026-01-01T00:00:00Z", 1.0, 3, 0),
        )
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            ("mem-foreign", "ev-1"),
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET associated_memory_ids_json=? WHERE capture_id=?",
            (json.dumps(["mem-1", "mem-foreign"]), "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            result = write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="foreign-associated-memory", reviewer_confirmed=True,
            )
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        self.assertEqual(result["sft_count"], 0)
        self.assertEqual(manifest["excluded_counts"], {"association_namespace_mismatch": 1})

    def test_snapshot_id_cannot_be_reused_for_a_second_destination(self) -> None:
        self._seed_capture()
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            write_training_views(
                self.conn, out_dir=str(first), namespace="project:demo",
                snapshot_id="destination-bound", reviewer_confirmed=True,
            )
            with self.assertRaisesRegex(TrainingExportError, "different export inputs"):
                write_training_views(
                    self.conn, out_dir=str(second), namespace="project:demo",
                    snapshot_id="destination-bound", reviewer_confirmed=True,
                )
            self.assertFalse((second / "manifest.json").exists())

    def test_project_split_is_stable_and_uses_frozen_byte_buckets(self) -> None:
        key_a = _split_key("demo", "caller-salt")
        key_b = _split_key("demo", "different-snapshot")
        self.assertEqual(key_a, key_b)
        self.assertEqual(
            _split_key("fixture", "ignored"),
            "7d68cc11d5ee2fa7bfa8d2e7933505a3080bd70a7f09caa9ba35b56fd067ce57",
        )
        self.assertEqual(_split_bucket("00" + "0" * 62), "train")
        self.assertEqual(_split_bucket("cb" + "0" * 62), "train")
        self.assertEqual(_split_bucket("cc" + "0" * 62), "validation")
        self.assertEqual(_split_bucket("e5" + "0" * 62), "validation")
        self.assertEqual(_split_bucket("e6" + "0" * 62), "test")
        self.assertEqual(_split_bucket("ff" + "0" * 62), "test")

    def test_update_of_joins_same_namespace_source_memory_lineage(self) -> None:
        groups = _lineage_groups([
            {
                "capture_id": "capture-source",
                "namespace": "project:demo",
                "session_id": "session-source",
                "episode_id": None,
                "source_memory_ids": ["mem-predecessor"],
                "memory_rows": [],
            },
            {
                "capture_id": "capture-update",
                "namespace": "project:demo",
                "session_id": "session-update",
                "episode_id": None,
                "source_memory_ids": ["mem-update"],
                "memory_rows": [{
                    "id": "mem-update",
                    "namespace": "project:demo",
                    "update_of": "mem-predecessor",
                }],
            },
        ])
        self.assertEqual(groups[0], groups[1])

    def test_update_of_does_not_join_memory_lineage_across_namespaces(self) -> None:
        groups = _lineage_groups([
            {
                "capture_id": "capture-source",
                "namespace": "project:one",
                "session_id": "shared-session",
                "episode_id": "shared-episode",
                "source_memory_ids": ["mem-predecessor"],
                "memory_rows": [],
            },
            {
                "capture_id": "capture-update",
                "namespace": "project:two",
                "session_id": "shared-session",
                "episode_id": "shared-episode",
                "source_memory_ids": ["mem-update"],
                "memory_rows": [{
                    "id": "mem-update",
                    "namespace": "project:two",
                    "update_of": "mem-predecessor",
                }],
            },
        ])
        self.assertNotEqual(groups[0], groups[1])

    def test_same_namespace_unrelated_sessions_and_episodes_remain_separate(self) -> None:
        groups = _lineage_groups([
            {
                "capture_id": "capture-one",
                "namespace": "project:demo",
                "session_id": "session-one",
                "episode_id": "episode-one",
                "source_memory_ids": ["mem-one"],
                "memory_rows": [],
            },
            {
                "capture_id": "capture-two",
                "namespace": "project:demo",
                "session_id": "session-two",
                "episode_id": "episode-two",
                "source_memory_ids": ["mem-two"],
                "memory_rows": [],
            },
        ])
        self.assertNotEqual(groups[0], groups[1])

    def test_same_session_id_is_scoped_by_namespace(self) -> None:
        groups = _lineage_groups([
            {
                "capture_id": "capture-one",
                "namespace": "project:one",
                "session_id": "shared-session",
                "episode_id": "shared-episode",
                "source_memory_ids": [],
                "memory_rows": [],
            },
            {
                "capture_id": "capture-two",
                "namespace": "project:two",
                "session_id": "shared-session",
                "episode_id": "shared-episode",
                "source_memory_ids": [],
                "memory_rows": [],
            },
        ])
        self.assertNotEqual(groups[0], groups[1])

    def test_export_rejects_missing_or_cross_namespace_update_predecessor(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
            "trust_score, applied_count, violated_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem-other-project", "project:other", "fact", "Other project memory",
             "2026-01-01T00:00:00Z", 1.0, 0, 0),
        )
        self.conn.execute(
            "UPDATE memory SET update_of=? WHERE id=?",
            ("mem-other-project", "mem-1"),
        )
        self.conn.commit()
        with self.assertRaisesRegex(TrainingExportError, "crosses project namespace"):
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="cross-project-update",
                reviewer_confirmed=True,
            )

        self.conn.execute(
            "UPDATE memory SET update_of=? WHERE id=?",
            ("missing-predecessor", "mem-1"),
        )
        self.conn.commit()
        with self.assertRaisesRegex(TrainingExportError, "missing predecessor"):
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="missing-update",
                reviewer_confirmed=True,
            )

    def test_tracked_training_goldens_regenerate_deterministically(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "training"
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [
                    sys.executable,
                    str(fixture_dir / "build_fixtures.py"),
                    "--output",
                    tmp,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            for name in (
                "expected-sft.json",
                "expected-preferences.json",
                "expected-deletion-map.json",
            ):
                self.assertEqual(
                    (Path(tmp) / name).read_bytes(),
                    (fixture_dir / name).read_bytes(),
                )

    def test_export_refuses_source_growth_beyond_bounded_limit(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "INSERT INTO training_capture (capture_id, host, session_id, namespace, "
            "created_at, updated_at, state, prompt, assistant_response, "
            "consent_scope, content_license, redaction_status, "
            "redaction_policy_version, governance_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("partial-growth", "fixture", "session-growth", "project:demo",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "partial",
             "partial prompt", "partial answer", "scope", "license",
             "redacted", "policy-v1", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO training_delivery_snapshot (delivery_snapshot_id, capture_id, "
            "rendered, effective_ops_json, rendered_hash, transform_version, emitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("delivery-growth", "partial-growth", "partial context", "[]",
             hashlib.sha256(b"partial context").hexdigest(),
             "transform-v1", "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        previous = os.environ.get("ZMEM_TRAINING_MAX_ROWS")
        os.environ["ZMEM_TRAINING_MAX_ROWS"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(TrainingExportError, "maximum of 1"):
                    write_training_views(
                        self.conn, out_dir=tmp, namespace="project:demo",
                        snapshot_id="bounded", reviewer_confirmed=True,
                    )
                self.assertFalse((Path(tmp) / "manifest.json").exists())
        finally:
            if previous is None:
                os.environ.pop("ZMEM_TRAINING_MAX_ROWS", None)
            else:
                os.environ["ZMEM_TRAINING_MAX_ROWS"] = previous

    def test_quarantine_requires_verified_completion(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "INSERT INTO training_capture (capture_id, host, session_id, namespace, "
            "created_at, updated_at, state, prompt, assistant_response, "
            "consent_scope, content_license, redaction_status, "
            "redaction_policy_version, governance_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("partial", "fixture", "session-partial", "project:demo",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "partial",
             "partial prompt", "partial answer", "scope", "license",
             "redacted", "policy-v1", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO training_delivery_snapshot (delivery_snapshot_id, capture_id, "
            "rendered, effective_ops_json, rendered_hash, transform_version, emitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("delivery-partial", "partial", "partial context", "[]",
             hashlib.sha256(b"partial context").hexdigest(),
             "transform-v1", "2026-01-01T00:00:00Z"),
        )
        partial_payload = json.dumps({"source_event_id": "partial-event"})
        self.conn.execute(
            "INSERT INTO training_capture_observation "
            "(observation_id, capture_id, observation_kind, payload, payload_sha256, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("obs-partial", "partial", "event", partial_payload,
             hashlib.sha256(partial_payload.encode("utf-8")).hexdigest(),
             "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="quarantine-gate", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            manifest = json.loads(
                (Path(tmp) / "quarantine-manifest.json").read_text()
            )
            self.assertEqual(manifest["event_ids"], ["ev-1", "event-1"])

    def test_quarantine_excludes_revoked_completion(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture SET revoked_at=? WHERE capture_id=?",
            ("2026-01-02T00:00:00Z", "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="quarantine-revoked", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            manifest = json.loads(
                (Path(tmp) / "quarantine-manifest.json").read_text()
            )
            self.assertEqual(manifest["event_ids"], [])

    def test_quarantine_excludes_capture_time_quarantine(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture SET quarantine_reason=? WHERE capture_id=?",
            ("operator-review", "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="quarantine-capture-time", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            manifest = json.loads(
                (Path(tmp) / "quarantine-manifest.json").read_text()
            )
            self.assertEqual(manifest["event_ids"], [])
            export_manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(export_manifest["excluded_counts"], {"quarantine": 1})

    def test_quarantine_excludes_governance_invalid_capture(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_capture SET governance_source=? WHERE capture_id=?",
            ("", "cap-1"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="quarantine-governance", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            manifest = json.loads(
                (Path(tmp) / "quarantine-manifest.json").read_text()
            )
            self.assertEqual(manifest["event_ids"], [])

    def test_quarantine_includes_gate_passing_negative_label(self) -> None:
        self._seed_capture(violated=2, applied=0)
        with tempfile.TemporaryDirectory() as tmp:
            write_training_views(
                self.conn, out_dir=tmp, namespace="project:demo",
                snapshot_id="quarantine-negative", reviewer_confirmed=True,
                quarantine_raw=True,
            )
            manifest = json.loads(
                (Path(tmp) / "quarantine-manifest.json").read_text()
            )
            self.assertEqual(manifest["event_ids"], ["ev-1", "event-1"])

    def test_persisted_ops_fail_closed_with_capture_id(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_delivery_snapshot SET effective_ops_json=? "
            "WHERE capture_id=?",
            (json.dumps(["valid", 17]), "cap-1"),
        )
        self.conn.commit()
        with self.assertRaisesRegex(
            TrainingExportError, "invalid persisted operations for capture cap-1"
        ):
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="bad-ops",
                reviewer_confirmed=True,
            )

    def test_oversized_persisted_ops_fail_closed_without_truncation(self) -> None:
        self._seed_capture()
        oversized = json.dumps(["safe-operation " * 30, "y"])
        self.conn.execute(
            "UPDATE training_delivery_snapshot SET effective_ops_json=? "
            "WHERE capture_id=?", (oversized, "cap-1")
        )
        self.conn.commit()
        with self.assertRaisesRegex(
            TrainingExportError, "invalid persisted operations for capture cap-1"
        ):
            build_sft_rows(
                self.conn, namespace="project:demo", snapshot_id="large-ops",
                reviewer_confirmed=True,
            )

    def test_quarantine_ops_failure_names_capture_id(self) -> None:
        self._seed_capture()
        self.conn.execute(
            "UPDATE training_delivery_snapshot SET effective_ops_json=? "
            "WHERE capture_id=?", ("{not-json", "cap-1")
        )
        self.conn.commit()
        item = _load_snapshot_rows(self.conn, "project:demo")[0]
        with self.assertRaisesRegex(
            TrainingExportError, "invalid persisted operations for capture cap-1"
        ):
            _bounded_quarantine_event(item, "event-1")

    def test_valid_many_ops_quarantine_event_is_byte_bounded(self) -> None:
        item = {
            "capture": {
                "capture_id": "capture-bounded",
                "prompt": "p" * 300,
                "assistant_response": "a" * 300,
                "rendered": "r" * 300,
                "effective_ops_json": json.dumps(["x"] * 80),
            },
        }
        event = _bounded_quarantine_event(item, "event-bounded")
        payload = json.loads(event)
        self.assertLessEqual(len(event), QUARANTINE_EVENT_MAX_BYTES)
        self.assertEqual(payload["capture_id"], "capture-bounded")
        self.assertEqual(payload["event_id"], "event-bounded")
        self.assertLess(len(payload["ops_tokens"]), 80)

    def test_training_output_lock_persists_and_ignores_legacy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out"
            lock_path = destination.parent / ".out.training-export.lock"
            lock_path.write_text("legacy-marker")
            with _training_output_lock(destination):
                self.assertTrue(lock_path.is_file())
                with self.assertRaisesRegex(
                    TrainingExportError, "already in progress"
                ):
                    with _training_output_lock(destination):
                        pass
            self.assertTrue(lock_path.is_file())
            self.assertEqual(lock_path.read_text(), "legacy-marker")
            with _training_output_lock(destination):
                self.assertTrue(lock_path.is_file())

    def test_training_output_lock_rejects_distinct_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out"
            result: list[BaseException | None] = []

            def contend() -> None:
                try:
                    with _training_output_lock(destination):
                        result.append(None)
                except BaseException as exc:  # captured for the main assertion
                    result.append(exc)

            with _training_output_lock(destination):
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], TrainingExportError)

    def test_training_output_lock_is_held_through_manifest_last_install(self) -> None:
        self._seed_capture()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp).resolve()
            result: list[BaseException | None] = []
            original_replace = training_module.os.replace

            def replace_with_contention(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
                if Path(target).resolve() == destination / "manifest.json":
                    def contend() -> None:
                        try:
                            with _training_output_lock(destination):
                                result.append(None)
                        except BaseException as exc:  # captured for the main assertion
                            result.append(exc)

                    thread = threading.Thread(target=contend)
                    thread.start()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                original_replace(source, target)

            with patch.object(training_module.os, "replace", replace_with_contention):
                write_training_views(
                    self.conn, out_dir=str(destination), namespace="project:demo",
                    snapshot_id="manifest-lock", reviewer_confirmed=True,
                )
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], TrainingExportError)
            self.assertTrue((destination / "manifest.json").is_file())

    def test_training_output_lock_releases_after_child_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out"
            child_code = (
                "import sys, time\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "from pathlib import Path\n"
                "from storelib.training import _training_output_lock\n"
                "with _training_output_lock(Path(sys.argv[1])):\n"
                " print('ready', flush=True)\n"
                " time.sleep(30)\n"
            )
            script_root = str(Path(__file__).parents[1] / "skills" / "memory" / "scripts")
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(destination), script_root],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(
                    TrainingExportError, "already in progress"
                ):
                    with _training_output_lock(destination):
                        pass
            finally:
                child.kill()
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()
            with _training_output_lock(destination):
                self.assertTrue(
                    (destination.parent / ".out.training-export.lock").is_file()
                )

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
        self._seed_capture(evidence_kind="correction")
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
            "UPDATE memory SET content=? WHERE id=?",
            (f"Use the new deploy command for alice@example.com at "
             f"{self._path_root / 'alice' / 'new'}", "mem-1"),
        )
        self.conn.execute(
            "UPDATE memory SET content=? WHERE id=?",
            (f"Use the old deploy command for bob@example.com at "
             f"{self._path_root / 'bob' / 'old'}", "mem-predecessor"),
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_kind=?, outcome_value=?, "
            "reviewer_id=?, reviewer_confirmed=1, correction_closeout=1 WHERE capture_id=?",
            ("reviewer_acceptance", "accepted", "reviewer-1", "cap-1"),
        )
        self.conn.execute(
            "INSERT INTO training_capture_review "
            "(capture_id, completion_evidence_id, reviewer_id, reviewed_at) "
            "VALUES (?, ?, ?, ?)",
            ("cap-1", "ev-1", "reviewer-1", "2026-01-01T00:00:01Z"),
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
        serialized = json.dumps(row, ensure_ascii=False)
        new_path = str(self._path_root / "alice" / "new")
        old_path = str(self._path_root / "bob" / "old")
        self.assertNotIn("alice@example.com", serialized)
        self.assertNotIn("bob@example.com", serialized)
        self.assertNotIn(new_path, serialized)
        self.assertNotIn(old_path, serialized)
        self.assertGreaterEqual(serialized.count("[REDACTED_EMAIL]"), 2)
        self.assertGreaterEqual(serialized.count("[REDACTED_PATH]"), 2)

    def test_training_export_redacts_before_byte_bound_and_hides_project_namespace(self) -> None:
        self._seed_capture()
        raw_namespace = (
            f"project:{self._path_root / 'alice' / 'private-project'}")
        current_email = "alice@example.com"
        current_path = str(self._path_root / "alice" / "private")
        current_bearer = "Bearer " + ("A" * 16) + "."
        predecessor_email = "bob@example.com"
        predecessor_path = str(self._path_root / "bob" / "old")
        predecessor_bearer = "Bearer " + ("B" * 16) + "."
        self.conn.execute(
            "UPDATE memory SET namespace=?, content=? WHERE id=?",
            (raw_namespace, f"contact {current_email} at {current_path} plus {current_bearer}", "mem-1"),
        )
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
            "trust_score, applied_count, violated_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mem-predecessor", raw_namespace, "fact",
             f"contact {predecessor_email} at {predecessor_path} plus {predecessor_bearer}",
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
            "UPDATE episode SET namespace=? WHERE id=?", (raw_namespace, "episode-1")
        )
        self.conn.execute(
            "UPDATE training_capture SET namespace=?, prompt=?, assistant_response=? "
            "WHERE capture_id=?",
            (raw_namespace, f"Deploy for {current_email} at {current_path}",
             current_bearer + "é" * 400, "cap-1"),
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET outcome_value=? WHERE capture_id=?",
            (f"passed for {current_email} at {current_path} with {current_bearer}", "cap-1"),
        )
        self.conn.commit()
        self.assertIn("[REDACTED_SECRET]", _redact(current_bearer, max_bytes=24) or "")
        rows = build_sft_rows(
            self.conn, namespace=raw_namespace, snapshot_id="privacy-boundary",
            reviewer_confirmed=True,
        )
        self.assertEqual(len(rows), 1)
        row_text = json.dumps(rows[0], ensure_ascii=False)
        self.assertTrue(row_text.startswith("{") and row_text.endswith("}"))
        self.assertNotIn(raw_namespace, row_text)
        self.assertNotIn(current_email, row_text)
        self.assertNotIn(current_path, row_text)
        self.assertNotIn(current_bearer, row_text)
        self.assertIn("[REDACTED_EMAIL]", row_text)
        self.assertIn("[REDACTED_PATH]", row_text)
        self.assertIn("[REDACTED_SECRET]", row_text)
        self.assertEqual(rows[0]["project_key"], _opaque_project_label(raw_namespace))

        # Switch this same capture through the real correction/reviewer gate so
        # the preference export contains both current and predecessor memory
        # objects.  The outcome above deliberately carried the same raw values
        # as the memory rows; its SFT readback proves it is redacted before the
        # context byte bound is applied.  Reviewer completion requires the
        # canonical accepted outcome, so the final materialized export uses the
        # governed reviewer value while retaining the PII-bearing source rows.
        self.conn.execute(
            "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ev-correction", "session-1", "test", "stop", "correction",
             "2026-01-01T00:00:01Z", "correction-hash", "accepted correction", "fixture"),
        )
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
            ("mem-1", "ev-correction"),
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET evidence_id=?, outcome_kind=?, outcome_value=?, "
            "reviewer_id=?, reviewer_confirmed=1, correction_closeout=1 WHERE capture_id=?",
            ("ev-correction", "reviewer_acceptance", "accepted", "reviewer-privacy", "cap-1"),
        )
        self.conn.execute(
            "INSERT INTO training_capture_review "
            "(capture_id, completion_evidence_id, reviewer_id, reviewed_at) "
            "VALUES (?, ?, ?, ?)",
            ("cap-1", "ev-correction", "reviewer-privacy", "2026-01-01T00:00:01Z"),
        )
        self.conn.commit()
        preferences = build_preference_rows(
            self.conn, namespace=raw_namespace, snapshot_id="privacy-preferences",
            reviewer_confirmed=True,
        )
        self.assertGreaterEqual(len(preferences), 1)
        preference_text = json.dumps(preferences[0], ensure_ascii=False)
        self.assertNotIn(current_email, preference_text)
        self.assertNotIn(current_path, preference_text)
        self.assertNotIn(current_bearer, preference_text)
        self.assertNotIn(predecessor_email, preference_text)
        self.assertNotIn(predecessor_path, preference_text)
        self.assertNotIn(predecessor_bearer, preference_text)
        self.assertGreaterEqual(preference_text.count("[REDACTED_EMAIL]"), 2)
        self.assertGreaterEqual(preference_text.count("[REDACTED_PATH]"), 2)
        self.assertGreaterEqual(preference_text.count("[REDACTED_SECRET]"), 2)
        with tempfile.TemporaryDirectory(prefix="training-privacy-artifacts-") as tmp:
            output = Path(tmp) / "export"
            result = write_training_views(
                self.conn,
                out_dir=str(output),
                namespace=raw_namespace,
                snapshot_id="privacy-artifacts",
                reviewer_confirmed=True,
            )
            self.assertGreaterEqual(result["preference_count"], 1)
            raw_split_digest = _split_key(raw_namespace[8:])
            for artifact in output.rglob("*"):
                if not artifact.is_file():
                    continue
                artifact_bytes = artifact.read_bytes()
                for raw_value in (
                    raw_namespace,
                    current_email,
                    current_path,
                    current_bearer,
                    predecessor_email,
                    predecessor_path,
                    predecessor_bearer,
                    raw_split_digest,
                ):
                    self.assertNotIn(raw_value.encode("utf-8"), artifact_bytes, artifact)

    def test_project_label_and_split_bucket_are_stable_for_same_namespace(self) -> None:
        alice_namespace = (
            f"project:{self._path_root / 'alice' / 'private-project'}")
        bob_namespace = (
            f"project:{self._path_root / 'bob' / 'private-project'}")
        self.assertEqual(
            _opaque_project_label(alice_namespace),
            _opaque_project_label(alice_namespace),
        )
        self.assertNotEqual(
            _opaque_project_label(alice_namespace),
            _opaque_project_label(bob_namespace),
        )

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
                TrainingExportError, "different export inputs"
            ):
                write_training_views(
                    self.conn, out_dir=tmp, namespace="project:demo",
                    snapshot_id="immutable-snapshot", reviewer_confirmed=True,
                )
            self.assertEqual(manifest_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
