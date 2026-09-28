"""Focused state-machine tests for governed issue #135 capture storage."""

from __future__ import annotations

import os
import json
import hashlib
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


# Keep all package resolution and any accidental schema path lookup isolated
# before importing storelib.  These tests use in-memory connections, but the
# import itself binds STORE_PATH for the process.
_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))
os.environ["ZMEM_STORE"] = str(Path(__file__).resolve().parent / ".training-capture-test.sqlite")

from storelib.evidence import write_evidence  # noqa: E402
from storelib.schema import SUPPORTED_SCHEMA_VERSION, init_db, migrate  # noqa: E402
from storelib.training_capture import (  # noqa: E402
    MAX_EVENT_IDS,
    MAX_OBSERVATIONS_PER_CAPTURE,
    TrainingCaptureConflict,
    acknowledge_training_delivery,
    append_training_capture_observation,
    assert_training_capture_replay_binding,
    complete_training_capture,
    purge_expired_training_captures,
    record_training_delivery_snapshot,
    review_training_capture,
    revoke_training_capture,
    start_training_capture,
)
from storelib.write import update_memory  # noqa: E402


class TrainingCaptureCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        migrate(self.conn)
        self.namespace = "project:capture-test"
        self.session_id = "capture-session"
        self.memory_one = self._memory("one")
        self.memory_two = self._memory("two")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _memory(self, name: str) -> str:
        memory_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, ingestion_ts) "
            "VALUES (?, ?, 'fact', ?, '2026-01-01T00:00:00Z')",
            (memory_id, self.namespace, f"memory {name}"),
        )
        return memory_id

    def _evidence(self, *, kind: str = "test_result") -> str:
        return write_evidence(
            self.conn, session_id=self.session_id, lane="codex",
            moment="user_prompt", kind=kind, ts="2026-01-01T00:00:00Z",
            excerpt="verified test event", ref_path="fixture", ref_offset=0,
            id=str(uuid.uuid4()),
        )

    def _acknowledged_capture(self) -> tuple[str, str]:
        capture = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, prompt="Bearer abcdefghijklmnop is private",
            assistant_response="response", consent_scope="local",
            content_license="licensed", redaction_policy_version="v1",
        )
        self.assertEqual(capture["redaction_status"], "redacted")
        self.assertNotIn("abcdefghijklmnop", capture["prompt"])
        snapshot = record_training_delivery_snapshot(
            self.conn, capture["capture_id"], rendered="Bearer abcdefghijklmnop",
            effective_ops=["Bearer abcdefghijklmnop"], delivery_snapshot_id=str(uuid.uuid4()),
        )
        acknowledged = acknowledge_training_delivery(
            self.conn, capture["capture_id"],
            attestation={
                "attested_by": "trusted-test",
                "untrusted_note": "Bearer abcdefghijklmnop",
            },
        )
        self.assertEqual(acknowledged["state"], "acknowledged")
        self.assertEqual(snapshot["state"], "emitted_to_host")
        self.assertNotIn("abcdefghijklmnop", snapshot["rendered"])
        self.assertEqual(
            json.loads(acknowledged["acknowledgement_attestation"]),
            {"attested_by": "trusted-test"},
        )
        return capture["capture_id"], snapshot["delivery_snapshot_id"]

    def test_initializer_keeps_schema_version_and_completion_is_atomic(self) -> None:
        version = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(version, str(SUPPORTED_SCHEMA_VERSION))
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({
            "training_capture", "training_delivery_snapshot",
            "training_capture_completion", "training_capture_review",
            "training_export_snapshot_binding", "training_capture_observation",
        } <= tables)

        capture_id, _ = self._acknowledged_capture()
        evidence_id = self._evidence()
        completion = complete_training_capture(
            self.conn, capture_id, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="trusted-test",
            outcome_kind="test", outcome_value="passed Bearer abcdefghijklmnop",
            export_consent_scope="export-local", export_content_license="licensed",
        )
        self.assertEqual(completion["evidence_id"], evidence_id)
        self.assertNotIn("abcdefghijklmnop", completion["outcome_value"])
        self.assertIn("[REDACTED", completion["outcome_value"])
        self.assertEqual(
            self.conn.execute("SELECT state FROM training_capture WHERE capture_id=?", (capture_id,)).fetchone()[0],
            "completed",
        )
        self.assertEqual(
            self.conn.execute("SELECT memory_id FROM memory_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()[0],
            self.memory_one,
        )

    def test_user_acceptance_requires_and_accepts_turn_evidence(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        completion = complete_training_capture(
            self.conn, capture_id, evidence_id=self._evidence(kind="turn"),
            memory_ids=[self.memory_one], verifier_id="trusted-test",
            outcome_kind="user_acceptance", outcome_value="accepted",
            export_consent_scope="export-local", export_content_license="licensed",
        )
        self.assertEqual(completion["outcome_kind"], "user_acceptance")

    def test_completion_rejects_incompatible_evidence_kind(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        evidence_id = self._evidence(kind="turn")
        with self.assertRaisesRegex(ValueError, "evidence kind"):
            complete_training_capture(
                self.conn, capture_id, evidence_id=evidence_id,
                memory_ids=[self.memory_one], verifier_id="trusted-test",
                outcome_kind="test", outcome_value="passed",
                export_consent_scope="export-local", export_content_license="licensed",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM training_capture WHERE capture_id=?",
                (capture_id,),
            ).fetchone()[0],
            "acknowledged",
        )

    def test_correction_closeout_requires_reviewer_correction_evidence(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        with self.assertRaisesRegex(ValueError, "separate local review"):
            complete_training_capture(
                self.conn, capture_id, evidence_id=self._evidence(),
                memory_ids=[self.memory_one], verifier_id="trusted-test",
                outcome_kind="reviewer_acceptance", outcome_value="accepted",
                export_consent_scope="export-local", export_content_license="licensed",
                reviewer_id="reviewer", reviewer_confirmed=True,
                correction_closeout=True,
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM training_capture WHERE capture_id=?",
                (capture_id,),
            ).fetchone()[0],
            "acknowledged",
        )

    def test_reviewer_acceptance_requires_distinct_allowlisted_review_transition(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        evidence_id = self._evidence(kind="turn")
        completion = complete_training_capture(
            self.conn, capture_id, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="verifier-a",
            outcome_kind="reviewer_acceptance", outcome_value="accepted",
            export_consent_scope="export-local", export_content_license="licensed",
        )
        self.assertEqual(completion["reviewer_confirmed"], 0)
        with self.assertRaisesRegex(ValueError, "reviewer must differ"):
            review_training_capture(
                self.conn, capture_id, evidence_id=evidence_id,
                reviewer_id="verifier-a", allowed_reviewer_ids=["verifier-a"],
            )
        with self.assertRaisesRegex(ValueError, "authorized reviewer"):
            review_training_capture(
                self.conn, capture_id, evidence_id=evidence_id,
                reviewer_id="reviewer-b", allowed_reviewer_ids=["reviewer-c"],
            )
        review = review_training_capture(
            self.conn, capture_id, evidence_id=evidence_id,
            reviewer_id="reviewer-b", allowed_reviewer_ids=["reviewer-b"],
        )
        self.assertEqual(review["reviewer_id"], "reviewer-b")
        self.assertEqual(self.conn.execute(
            "SELECT reviewer_id, reviewer_confirmed FROM training_capture_completion WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[:], ("reviewer-b", 1))
        self.assertEqual(
            review_training_capture(
                self.conn, capture_id, evidence_id=evidence_id,
                reviewer_id="reviewer-b", allowed_reviewer_ids=["reviewer-b"],
            )["capture_id"], capture_id,
        )
        with self.assertRaisesRegex(TrainingCaptureConflict, "conflicting training review replay"):
            review_training_capture(
                self.conn, capture_id, evidence_id=evidence_id,
                reviewer_id="reviewer-c", allowed_reviewer_ids=["reviewer-c"],
            )
        # Completion replay remains semantic/idempotent after review mutates its
        # export gate columns; the review record is its own immutable identity.
        self.assertEqual(complete_training_capture(
            self.conn, capture_id, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="verifier-a",
            outcome_kind="reviewer_acceptance", outcome_value="accepted",
            export_consent_scope="export-local", export_content_license="licensed",
        )["capture_id"], capture_id)

    def test_preexisting_self_reviewed_completion_is_not_grandfathered(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        evidence_id = self._evidence(kind="turn")
        complete_training_capture(
            self.conn, capture_id, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="legacy-reviewer",
            outcome_kind="reviewer_acceptance", outcome_value="accepted",
            export_consent_scope="export-local", export_content_license="licensed",
        )
        self.conn.execute(
            "UPDATE training_capture_completion SET reviewer_id='legacy-reviewer', reviewer_confirmed=1 "
            "WHERE capture_id=?", (capture_id,),
        )
        # This is the v14 layout before independent-review records existed.
        self.conn.execute("DROP TABLE training_capture_review")
        self.conn.commit()
        migrate(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT reviewer_id, reviewer_confirmed FROM training_capture_completion WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[:], ("legacy-reviewer", 0))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM training_capture_review WHERE capture_id=?", (capture_id,),
        ).fetchone())

    def test_partial_governance_is_metadata_only_default_deny(self) -> None:
        capture = start_training_capture(
            self.conn, host="test", session_id=self.session_id, namespace=self.namespace,
            prompt="private prompt", assistant_response="private response",
            consent_scope="only-one-policy-field",
        )
        self.assertEqual(capture["redaction_status"], "metadata_only")
        self.assertEqual(capture["quarantine_reason"], "capture_governance_denied")
        self.assertIsNone(capture["prompt"])
        self.assertIsNone(capture["consent_scope"])

    def test_store_clock_ignores_transition_timestamp_arguments(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        append_training_capture_observation(
            self.conn, capture_id, observation_kind="turn", payload="{}",
            observed_at="2000-01-01T00:00:00Z",
        )
        observed = self.conn.execute(
            "SELECT observed_at, payload, payload_sha256 FROM training_capture_observation "
            "WHERE capture_id=?", (capture_id,),
        ).fetchone()
        self.assertNotEqual(observed["observed_at"], "2000-01-01T00:00:00Z")
        self.assertEqual(observed["payload_sha256"], hashlib.sha256(
            observed["payload"].encode("utf-8")).hexdigest())
        revoked = revoke_training_capture(
            self.conn, capture_id, reason="clock test", revoked_at="2099-01-01T00:00:00Z",
        )
        self.assertNotEqual(revoked["revoked_at"], "2099-01-01T00:00:00Z")
        # A prior acknowledgement replay cannot clear terminal revocation.
        replay = acknowledge_training_delivery(
            self.conn, capture_id, attestation={"attested_by": "trusted-test"},
            acknowledged_at="2001-01-01T00:00:00Z",
        )
        self.assertIsNotNone(replay["revoked_at"])

    def test_additive_capture_schema_upgrades_once_without_steady_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-training-schema-") as tmp:
            path = Path(tmp) / "store.sqlite"
            first = sqlite3.connect(path)
            first.row_factory = sqlite3.Row
            init_db(first)
            migrate(first)
            # Simulate a v14 store before the capture feature.  No version bump
            # is available for this additive set.
            first.execute("DROP TABLE training_export_snapshot_binding")
            first.execute("DROP TABLE training_capture_review")
            first.execute("DROP TABLE training_capture_observation")
            first.execute("DROP TABLE training_capture_completion")
            first.execute("DROP TABLE training_delivery_snapshot")
            first.execute("DROP TABLE training_capture")
            first.commit()
            first_sql: list[str] = []
            first.set_trace_callback(first_sql.append)
            migrate(first)
            self.assertTrue({"training_capture", "training_capture_review",
                             "training_export_snapshot_binding"} <= {
                row[0] for row in first.execute("SELECT name FROM sqlite_master WHERE type='table'")
            })
            self.assertTrue(any("BEGIN IMMEDIATE" in sql.upper() for sql in first_sql))
            first.close()

            writer = sqlite3.connect(path, timeout=0.2)
            writer.execute("BEGIN IMMEDIATE")
            steady = sqlite3.connect(path, timeout=0.2)
            steady_sql: list[str] = []
            steady.set_trace_callback(steady_sql.append)
            try:
                migrate(steady)
                self.assertEqual(steady.execute("SELECT count(*) FROM memory").fetchone()[0], 0)
                self.assertFalse(any("BEGIN IMMEDIATE" in sql.upper() for sql in steady_sql))
                self.assertFalse(any("TRAINING_" in sql.upper() and "CREATE" in sql.upper()
                                     for sql in steady_sql))
            finally:
                steady.close()
                writer.rollback()
                writer.close()

    def test_observation_digest_is_stored_and_backfilled_on_additive_upgrade(self) -> None:
        capture = start_training_capture(
            self.conn, host="test", session_id=self.session_id, namespace=self.namespace,
            prompt="prompt", assistant_response="response", consent_scope="local",
            content_license="licensed", redaction_policy_version="v1",
        )
        observation = append_training_capture_observation(
            self.conn, capture["capture_id"], observation_kind="turn", payload='{"event_id":"one"}',
        )
        row = self.conn.execute(
            "SELECT payload, payload_sha256 FROM training_capture_observation WHERE observation_id=?",
            (observation["observation_id"],),
        ).fetchone()
        self.assertEqual(row["payload_sha256"], hashlib.sha256(
            row["payload"].encode("utf-8")).hexdigest())
        # Emulate the immediately previous additive table shape.  The next
        # migrate must add and backfill exactly once without changing v14.
        self.conn.execute("ALTER TABLE training_capture_observation DROP COLUMN payload_sha256")
        self.conn.commit()
        migrate(self.conn)
        backfilled = self.conn.execute(
            "SELECT payload, payload_sha256 FROM training_capture_observation WHERE observation_id=?",
            (observation["observation_id"],),
        ).fetchone()
        self.assertEqual(backfilled["payload_sha256"], hashlib.sha256(
            backfilled["payload"].encode("utf-8")).hexdigest())

    def test_nested_completion_conflict_rolls_back_its_association_savepoint(self) -> None:
        # First capture establishes a valid association through the public
        # completion API.  The second completion uses the same evidence with a
        # different memory: it reaches the post-insert invariant, then must
        # roll back only its own nested savepoint.
        evidence_id = self._evidence()
        first_capture, _ = self._acknowledged_capture()
        complete_training_capture(
            self.conn, first_capture, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="trusted-test",
            outcome_kind="test", outcome_value="passed",
            export_consent_scope="export-local", export_content_license="licensed",
        )
        second_capture, _ = self._acknowledged_capture()
        self.conn.commit()
        self.conn.execute("BEGIN")
        try:
            with self.assertRaisesRegex(ValueError, "different memory association"):
                complete_training_capture(
                    self.conn, second_capture, evidence_id=evidence_id,
                    memory_ids=[self.memory_two], verifier_id="trusted-test",
                    outcome_kind="test", outcome_value="passed",
                    export_consent_scope="export-local", export_content_license="licensed",
                )
            self.conn.commit()
        finally:
            if self.conn.in_transaction:
                self.conn.rollback()
        links = [row[0] for row in self.conn.execute(
            "SELECT memory_id FROM memory_evidence WHERE evidence_id=? ORDER BY memory_id",
            (evidence_id,),
        )]
        self.assertEqual(links, [self.memory_one])
        self.assertEqual(
            self.conn.execute("SELECT state FROM training_capture WHERE capture_id=?", (second_capture,)).fetchone()[0],
            "acknowledged",
        )

    def test_conflicting_snapshot_replay_is_rejected(self) -> None:
        capture_id, snapshot_id = self._acknowledged_capture()
        with self.assertRaises(TrainingCaptureConflict):
            record_training_delivery_snapshot(
                self.conn, capture_id, rendered="changed", effective_ops=[],
                delivery_snapshot_id=snapshot_id,
            )

    def test_conflicting_capture_replay_binding_is_rejected_without_mutation(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        assert_training_capture_replay_binding(
            self.conn, capture_id,
            {
                "host": "TEST", "session_id": self.session_id,
                "namespace": self.namespace, "cwd": None,
                "assistant_response": "response", "consent_scope": "local",
                "content_license": "licensed", "redaction_policy_version": "v1",
            },
        )
        with self.assertRaises(TrainingCaptureConflict):
            assert_training_capture_replay_binding(
                self.conn, capture_id, {"consent_scope": "changed opt-in"},
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM training_capture WHERE capture_id=?", (capture_id,)
            ).fetchone()[0],
            "acknowledged",
        )

    def test_default_deny_capture_is_minimal_metadata_only(self) -> None:
        capture = start_training_capture(
            self.conn, host="codex", session_id="secret-session", namespace=self.namespace,
            host_task_id="Bearer abcdefghijklmnop", cwd="C:/private/project",
            prompt="Bearer abcdefghijklmnop", assistant_response="private response",
        )
        self.assertEqual(capture["redaction_status"], "metadata_only")
        self.assertEqual(capture["quarantine_reason"], "capture_governance_denied")
        for field in ("session_id", "namespace", "host_task_id", "cwd", "prompt", "assistant_response"):
            self.assertIsNone(capture[field], field)

    def test_default_deny_delivery_snapshot_drops_content_to_sql_null(self) -> None:
        capture = start_training_capture(
            self.conn, host="codex", session_id="secret-session", namespace=self.namespace,
            prompt="private prompt", assistant_response="private response",
        )
        snapshot = record_training_delivery_snapshot(
            self.conn, capture["capture_id"], rendered="rendered private fence",
            effective_ops=["run tests"], delivery_snapshot_id=str(uuid.uuid4()),
        )
        self.assertIsNone(snapshot["rendered"])
        self.assertIsNone(snapshot["effective_ops_json"])
        self.assertIsNone(snapshot["rendered_hash"])
        stored = self.conn.execute(
            "SELECT rendered, effective_ops_json, rendered_hash "
            "FROM training_delivery_snapshot WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()
        self.assertEqual(tuple(stored), (None, None, None))

    def test_oversized_ops_refuse_whole_snapshot_and_leave_auditable_partial(self) -> None:
        capture = start_training_capture(
            self.conn, host="codex", session_id=self.session_id,
            namespace=self.namespace, prompt="bounded prompt",
            assistant_response="bounded response", consent_scope="local",
            content_license="licensed", redaction_policy_version="v1",
        )
        with self.assertRaisesRegex(ValueError, "effective_ops exceeds 400 UTF-8 bytes"):
            record_training_delivery_snapshot(
                self.conn, capture["capture_id"], rendered="fence",
                effective_ops=["é" * 250],
            )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM training_delivery_snapshot WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone())
        row = self.conn.execute(
            "SELECT state, quarantine_reason FROM training_capture WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("partial", "effective_ops_over_limit"))

    def test_oversized_single_op_uses_the_same_auditable_refusal(self) -> None:
        capture = start_training_capture(
            self.conn, host="codex", session_id=self.session_id,
            namespace=self.namespace, prompt="bounded prompt",
            assistant_response="bounded response", consent_scope="local",
            content_license="licensed", redaction_policy_version="v1",
        )
        with self.assertRaisesRegex(ValueError, "effective_ops exceeds 400 UTF-8 bytes"):
            record_training_delivery_snapshot(
                self.conn, capture["capture_id"], rendered="fence",
                effective_ops=["x" * 65_537],
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT quarantine_reason FROM training_capture WHERE capture_id=?",
                (capture["capture_id"],),
            ).fetchone()[0],
            "effective_ops_over_limit",
        )

    def test_revocation_excludes_capture_and_retention_purges_after_30_days(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        revoked = revoke_training_capture(
            self.conn, capture_id, reason="user requested removal",
            revoked_by="trusted-test", revoked_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(revoked["revocation_reason"], "user requested removal")
        # Caller timestamps are ignored; age the durable store row directly to
        # exercise retention independently of the state-transition clock.
        self.conn.execute(
            "UPDATE training_capture SET finalized_at='2026-01-01T00:00:00Z' WHERE capture_id=?",
            (capture_id,),
        )
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "acknowledged before completion"):
            complete_training_capture(
                self.conn, capture_id, evidence_id=self._evidence(),
                memory_ids=[self.memory_one], verifier_id="trusted-test",
                outcome_kind="test", outcome_value="passed",
                export_consent_scope="export-local", export_content_license="licensed",
            )
        self.assertEqual(
            purge_expired_training_captures(
                self.conn, now_ts="2026-01-31T00:00:00Z",
            ),
            {"purged_captures": 1},
        )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
        ).fetchone())

    def test_retention_purges_expired_terminal_rows_only_and_keeps_associations(self) -> None:
        completed_id, _ = self._acknowledged_capture()
        evidence_id = self._evidence()
        complete_training_capture(
            self.conn, completed_id, evidence_id=evidence_id,
            memory_ids=[self.memory_one], verifier_id="trusted-test",
            outcome_kind="test", outcome_value="passed",
            export_consent_scope="export-local", export_content_license="licensed",
            verified_at="2026-01-01T00:00:00Z",
        )
        revoked_id, _ = self._acknowledged_capture()
        revoke_training_capture(
            self.conn, revoked_id, reason="expired revocation",
            revoked_by="trusted-test", revoked_at="2026-01-01T00:00:00Z",
        )
        recent_id, _ = self._acknowledged_capture()
        revoke_training_capture(
            self.conn, recent_id, reason="recent revocation",
            revoked_by="trusted-test", revoked_at="2026-01-02T00:00:00Z",
        )
        self.conn.execute(
            "UPDATE training_capture SET finalized_at='2026-01-01T00:00:00Z' WHERE capture_id IN (?, ?)",
            (completed_id, revoked_id),
        )
        self.conn.execute(
            "UPDATE training_capture SET finalized_at='2026-01-02T00:00:00Z' WHERE capture_id=?",
            (recent_id,),
        )
        self.conn.commit()
        partial_id = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, prompt="partial", assistant_response="partial",
            consent_scope="local", content_license="licensed",
            redaction_policy_version="v1",
        )["capture_id"]

        result = purge_expired_training_captures(
            self.conn, now_ts="2026-01-31T00:00:00Z",
        )

        self.assertEqual(result, {"purged_captures": 2})
        for capture_id in (completed_id, revoked_id):
            self.assertIsNone(self.conn.execute(
                "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
            ).fetchone())
        for capture_id in (recent_id, partial_id):
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
            ).fetchone())
        # The store-owned evidence association is independent of local capture
        # retention and therefore survives deleting its completed capture.
        self.assertEqual(self.conn.execute(
            "SELECT memory_id FROM memory_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()[0], self.memory_one)

    def test_retention_ages_out_abandoned_partial_emitted_and_acknowledged_rows(self) -> None:
        partial_id = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, prompt="partial", assistant_response="partial",
            consent_scope="local", content_license="licensed",
            redaction_policy_version="v1",
        )["capture_id"]
        emitted_id = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, prompt="emitted", assistant_response="emitted",
            consent_scope="local", content_license="licensed",
            redaction_policy_version="v1",
        )["capture_id"]
        record_training_delivery_snapshot(
            self.conn, emitted_id, rendered="emitted", effective_ops=[],
        )
        acknowledged_id, _ = self._acknowledged_capture()
        self.conn.execute(
            "UPDATE training_capture SET updated_at='2026-01-01T00:00:00Z' "
            "WHERE capture_id IN (?, ?, ?)",
            (partial_id, emitted_id, acknowledged_id),
        )
        self.conn.commit()

        self.assertEqual(
            purge_expired_training_captures(
                self.conn, now_ts="2026-02-01T00:00:00Z",
            ),
            {"purged_captures": 3},
        )
        for capture_id in (partial_id, emitted_id, acknowledged_id):
            self.assertIsNone(self.conn.execute(
                "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
            ).fetchone())

    def test_retention_keeps_capture_one_second_before_expiration(self) -> None:
        capture_id = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, prompt="partial", assistant_response="partial",
        )["capture_id"]
        self.conn.execute(
            "UPDATE training_capture SET updated_at='2026-01-01T00:00:00Z' "
            "WHERE capture_id=?", (capture_id,),
        )
        self.conn.commit()

        self.assertEqual(
            purge_expired_training_captures(
                self.conn, now_ts="2026-01-30T23:59:59Z",
            ),
            {"purged_captures": 0},
        )
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
        ).fetchone())
        self.assertEqual(
            purge_expired_training_captures(
                self.conn, now_ts="2026-01-31T00:00:00Z",
            ),
            {"purged_captures": 1},
        )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
        ).fetchone())

    def test_training_redaction_covers_host_identifiers_paths_and_event_ids(self) -> None:
        alice_home = "/" + "home/" + "alice"
        capture = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, host_task_id="alice@example.com",
            cwd="C:/Users/<user>/private-project",
            prompt=f"contact alice@example.com from {alice_home}/project",
            assistant_response="Bearer abcdefghijklmnop",
            consent_scope="local", content_license="licensed",
            redaction_policy_version="v1",
        )
        self.assertIsNone(capture["host_task_id"])
        self.assertNotIn("alice", capture["cwd"])
        self.assertNotIn("alice@example.com", capture["prompt"])
        self.assertNotIn("abcdefghijklmnop", capture["assistant_response"])
        append_training_capture_observation(
            self.conn, capture["capture_id"], observation_kind="turn",
            payload=json.dumps({
                "event_id": "alice@example.com",
                "source_event_ids": ["event-safe", "Bearer abcdefghijklmnop"],
                "path": f"{alice_home}/private-project",
            }),
        )
        payload = self.conn.execute(
            "SELECT payload FROM training_capture_observation WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()[0]
        self.assertNotIn("alice@example.com", payload)
        self.assertNotIn("abcdefghijklmnop", payload)
        self.assertNotIn(alice_home, payload)

    def test_training_observations_bound_count_and_event_ids(self) -> None:
        capture = start_training_capture(
            self.conn, host="test", session_id=self.session_id,
            namespace=self.namespace, consent_scope="local",
            content_license="licensed", redaction_policy_version="v1",
        )
        append_training_capture_observation(
            self.conn, capture["capture_id"], observation_kind="turn",
            payload=json.dumps({
                "source_event_ids": [f"event-{index}" for index in range(MAX_EVENT_IDS + 10)],
            }),
        )
        stored = self.conn.execute(
            "SELECT payload FROM training_capture_observation WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()[0]
        self.assertEqual(len(json.loads(stored)["source_event_ids"]), MAX_EVENT_IDS)
        for _ in range(MAX_OBSERVATIONS_PER_CAPTURE - 1):
            append_training_capture_observation(
                self.conn, capture["capture_id"], observation_kind="turn", payload="{}",
            )
        with self.assertRaisesRegex(ValueError, "observation limit"):
            append_training_capture_observation(
                self.conn, capture["capture_id"], observation_kind="turn", payload="{}",
            )

    def test_retention_failure_rolls_back_capture_children(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        append_training_capture_observation(
            self.conn, capture_id, observation_kind="test", payload="retained",
            observed_at="2026-01-01T00:00:00Z",
        )
        self.conn.execute(
            "UPDATE training_capture SET finalized_at='2026-01-01T00:00:00Z' "
            "WHERE capture_id=?", (capture_id,)
        )
        self.conn.execute(
            "CREATE TRIGGER fail_training_retention BEFORE DELETE ON training_capture "
            "BEGIN SELECT RAISE(ABORT, 'injected retention failure'); END"
        )
        self.conn.commit()

        with self.assertRaisesRegex(sqlite3.DatabaseError, "injected retention failure"):
            purge_expired_training_captures(
                self.conn, now_ts="2026-01-31T00:00:00Z",
            )

        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM training_capture WHERE capture_id=?", (capture_id,)
        ).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM training_delivery_snapshot WHERE capture_id=?", (capture_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM training_capture_observation WHERE capture_id=?", (capture_id,)
        ).fetchone()[0], 1)

    def test_update_reason_is_normalized_allowlisted_and_written_to_predecessor(self) -> None:
        with self.assertRaisesRegex(ValueError, "update reason"):
            update_memory(
                self.conn, mid=self.memory_one, content="replacement",
                supersede_reason="training-example",
            )
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (self.memory_one,)
        ).fetchone()[0])
        with patch("storelib.write._detect_duplicate", return_value=(None, 0.0, None)):
            _, created = update_memory(
                self.conn, mid=self.memory_one, content="replacement",
                supersede_reason="  EXPLICIT   CORRECTION  ",
            )
        self.assertTrue(created)
        self.assertEqual(self.conn.execute(
            "SELECT supersede_reason FROM memory WHERE id=?", (self.memory_one,)
        ).fetchone()[0], "explicit correction")


if __name__ == "__main__":
    unittest.main()
