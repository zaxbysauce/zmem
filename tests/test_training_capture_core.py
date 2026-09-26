"""Focused state-machine tests for governed issue #135 capture storage."""

from __future__ import annotations

import os
import json
import sqlite3
import sys
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
    TrainingCaptureConflict,
    acknowledge_training_delivery,
    assert_training_capture_replay_binding,
    complete_training_capture,
    purge_expired_training_captures,
    record_training_delivery_snapshot,
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

    def _evidence(self) -> str:
        return write_evidence(
            self.conn, session_id=self.session_id, lane="codex",
            moment="user_prompt", kind="test_result", ts="2026-01-01T00:00:00Z",
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
            "training_capture_completion", "training_capture_observation",
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

    def test_revocation_excludes_capture_and_retention_purges_after_30_days(self) -> None:
        capture_id, _ = self._acknowledged_capture()
        revoked = revoke_training_capture(
            self.conn, capture_id, reason="user requested removal",
            revoked_by="trusted-test", revoked_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(revoked["revocation_reason"], "user requested removal")
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
