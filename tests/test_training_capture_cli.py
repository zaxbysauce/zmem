"""Focused CLI tests for governed delivery identity creation and replay."""

from __future__ import annotations

import json
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
# Keep import-time store resolution away from the operator store when this
# module is run directly or loaded by unittest discovery.
_IMPORT_TMP = tempfile.TemporaryDirectory(prefix="zmem-training-capture-cli-import-")
_IMPORT_ROOT = Path(_IMPORT_TMP.name)
os.environ["ZMEM_STORE"] = str(_IMPORT_ROOT / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_IMPORT_ROOT / "data")
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
from storelib import cli as store_cli  # noqa: E402
from storelib.evidence import write_evidence  # noqa: E402


class TrainingCaptureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-training-capture-cli-")
        self.root = Path(self.tmp.name)
        self.store = self.root / "store.sqlite"
        self.env = os.environ.copy()
        self.env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.root / "data"),
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        for key in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            self.env.pop(key, None)
        initialized = self._run("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STORE), *args], cwd=ROOT, env=self.env,
            text=True, encoding="utf-8", capture_output=True, check=False,
            timeout=120,
        )

    def _delivery(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        path = self.root / "delivery.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return self._run("capture-training-delivery", "--input", str(path))

    def _payload(self, delivery_snapshot_id: str) -> dict[str, object]:
        return {
            "delivery_snapshot_id": delivery_snapshot_id,
            "host": "trusted-test-service",
            "session_id": "training-cli-session",
            "namespace": "project:training-cli",
            "prompt": "bounded prompt",
            "assistant_response": "bounded response",
            "rendered": "rendered delivery fence",
            "effective_ops": ["run tests"],
            "consent_scope": "local-training",
            "content_license": "CC-BY-4.0",
            "redaction_policy_version": "policy-v1",
        }

    def _snapshot_row(self, delivery_snapshot_id: str) -> tuple[object, ...]:
        conn = sqlite3.connect(self.store)
        try:
            return tuple(conn.execute(
                "SELECT capture_id, rendered, effective_ops_json, transform_version "
                "FROM training_delivery_snapshot WHERE delivery_snapshot_id=?",
                (delivery_snapshot_id,),
            ).fetchone() or ())
        finally:
            conn.close()

    def test_explicit_delivery_id_is_accepted_for_first_creation(self) -> None:
        delivery_id = str(uuid.uuid4())
        result = self._delivery(self._payload(delivery_id))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["delivery_snapshot_id"], delivery_id)
        self.assertEqual(output["state"], "emitted_to_host")
        self.assertEqual(self._snapshot_row(delivery_id)[0], output["capture_id"])

        # A separate process must be able to resolve the committed delivery
        # identity immediately; this catches a successful print before commit.
        ack_path = self.root / "ack.json"
        ack_path.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id,
            "attestation": {"attested_by": "local-cli"},
        }), encoding="utf-8")
        acknowledged = self._run("capture-training-acknowledge", "--input", str(ack_path))
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)
        self.assertEqual(json.loads(acknowledged.stdout)["state"], "acknowledged")
        conn = sqlite3.connect(self.store)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM training_capture WHERE capture_id=?",
                    (output["capture_id"],),
                ).fetchone()[0],
                "acknowledged",
            )
        finally:
            conn.close()

    def test_identical_explicit_delivery_replay_is_idempotent(self) -> None:
        delivery_id = str(uuid.uuid4())
        payload = self._payload(delivery_id)
        first = self._delivery(payload)
        second = self._delivery(payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        conn = sqlite3.connect(self.store)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM training_capture").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM training_delivery_snapshot").fetchone()[0], 1)
        finally:
            conn.close()

    def test_conflicting_explicit_delivery_replay_is_rejected(self) -> None:
        delivery_id = str(uuid.uuid4())
        payload = self._payload(delivery_id)
        first = self._delivery(payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self._snapshot_row(delivery_id)

        changed = dict(payload)
        changed["rendered"] = "changed delivery fence"
        conflict = self._delivery(changed)
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("conflicting delivery snapshot replay", conflict.stderr)
        self.assertEqual(self._snapshot_row(delivery_id), original)

    def test_lazy_export_import_failure_is_concise_and_does_not_use_unbound_error(self) -> None:
        original_import = __import__

        def fail_export_import(name, *args, **kwargs):
            if name == "storelib.training_export":
                raise ImportError("optional training exporter unavailable")
            return original_import(name, *args, **kwargs)

        stderr = io.StringIO()
        old_argv = sys.argv
        sys.argv = [
            str(STORE), "export-training", str(self.root / "training"),
            "--snapshot-id", str(uuid.uuid4()), "--reviewer-confirmed",
            "--namespace", "project:training-cli",
        ]
        try:
            with patch.object(store_cli, "connect", return_value=sqlite3.connect(":memory:")), \
                 patch.object(store_cli, "_prepare_store"), \
                 patch.object(store_cli, "_wait_for_maintenance_clear"), \
                 patch("builtins.__import__", side_effect=fail_export_import), \
                 redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    store_cli.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(raised.exception.code, 1, stderr.getvalue())
        self.assertIn("optional training exporter unavailable", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_partial_governance_is_accepted_as_metadata_only_and_timestamps_refused(self) -> None:
        delivery_id = str(uuid.uuid4())
        partial = self._payload(delivery_id)
        partial.pop("content_license")
        result = self._delivery(partial)
        self.assertEqual(result.returncode, 0, result.stderr)
        capture_id = json.loads(result.stdout)["capture_id"]
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT redaction_status, prompt, consent_scope, content_license "
                "FROM training_capture WHERE capture_id=?", (capture_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("metadata_only", None, None, None))
        finally:
            conn.close()
        timestamped = self._payload(str(uuid.uuid4()))
        timestamped["observed_at"] = "2000-01-01T00:00:00Z"
        refused = self._delivery(timestamped)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("timestamps are assigned by the local store", refused.stderr)

    def test_revoke_is_terminal_and_idempotent_by_delivery_id(self) -> None:
        delivery_id = str(uuid.uuid4())
        delivered = self._delivery(self._payload(delivery_id))
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        raw_path = str(self.root / "private")
        raw_reason = (
            "Удаление пользователя — 用户请求撤回 — alice@example.com at "
            f"{raw_path} with Bearer " + ("R" * 16) + "."
        )
        revoked = self._run(
            "capture-training-revoke", "--delivery-snapshot-id", delivery_id,
            "--reason", raw_reason,
        )
        replay = self._run(
            "capture-training-revoke", "--delivery-snapshot-id", delivery_id,
            "--reason", raw_reason,
        )
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertEqual(json.loads(revoked.stdout), json.loads(replay.stdout))
        output = json.loads(revoked.stdout)
        persisted_reason = output["reason"]
        self.assertLessEqual(len(persisted_reason.encode("utf-8")), 512)
        self.assertNotIn("alice@example.com", persisted_reason)
        self.assertNotIn(raw_path, persisted_reason)
        self.assertNotIn("Bearer " + ("R" * 16) + ".", persisted_reason)
        self.assertIn("[REDACTED_EMAIL]", persisted_reason)
        self.assertIn("[REDACTED_PATH]", persisted_reason)
        self.assertIn("[REDACTED_SECRET]", persisted_reason)
        conflict = self._run(
            "capture-training-revoke", "--delivery-snapshot-id", delivery_id,
            "--reason", "different reason",
        )
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("conflicting capture revocation replay", conflict.stderr)
        ack_path = self.root / "revoked-ack.json"
        ack_path.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id,
            "attestation": {"attested_by": "local-cli"},
        }), encoding="utf-8")
        self.assertNotEqual(
            self._run("capture-training-acknowledge", "--input", str(ack_path)).returncode, 0,
        )

    def test_review_uses_persisted_local_identity_and_refuses_label_mismatch(self) -> None:
        delivery_id = str(uuid.uuid4())
        delivered = self._delivery(self._payload(delivery_id))
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        capture_id = json.loads(delivered.stdout)["capture_id"]
        ack_path = self.root / "review-ack.json"
        ack_path.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id,
            "attestation": {"attested_by": "verifier-a"},
        }), encoding="utf-8")
        verifier_env = {**self.env, "ZMEM_TRAINING_CALLER_ID": "verifier-a"}
        ack = subprocess.run(
            [sys.executable, str(STORE), "capture-training-acknowledge", "--input", str(ack_path)],
            cwd=ROOT, env=verifier_env, text=True, encoding="utf-8", capture_output=True, timeout=120,
        )
        self.assertEqual(ack.returncode, 0, ack.stderr)
        memory_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, ingestion_ts) "
                "VALUES (?, 'project:training-cli', 'fact', 'review memory', '2026-01-01T00:00:00Z')",
                (memory_id,),
            )
            evidence_id = write_evidence(
                conn, session_id="training-cli-session", lane="codex", moment="user_prompt",
                kind="turn", ts="2026-01-01T00:00:00Z", excerpt="accepted", ref_path="test",
                ref_offset=0, id=str(uuid.uuid4()),
            )
            conn.commit()
        finally:
            conn.close()
        completion_path = self.root / "review-completion.json"
        completion_path.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id, "evidence_id": evidence_id,
            "memory_ids": [memory_id], "outcome_kind": "reviewer_acceptance",
            "outcome_value": "accepted", "export_consent_scope": "export",
            "export_content_license": "CC-BY",
            "verifier_id": "verifier-a",
        }), encoding="utf-8")
        completion = subprocess.run(
            [sys.executable, str(STORE), "capture-training-completion", "--input", str(completion_path)],
            cwd=ROOT, env=verifier_env, text=True, encoding="utf-8", capture_output=True, timeout=120,
        )
        self.assertEqual(completion.returncode, 0, completion.stderr)
        review_path = self.root / "review.json"
        review_path.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id, "evidence_id": evidence_id,
            "reviewer_id": "reviewer-b",
        }), encoding="utf-8")
        reviewer_env = {**self.env, "ZMEM_TRAINING_CALLER_ID": "reviewer-b",
                        "ZMEM_TRAINING_REVIEWER_IDS": "reviewer-b"}
        first = subprocess.run(
            [sys.executable, str(STORE), "capture-training-review", "--input", str(review_path)],
            cwd=ROOT, env=reviewer_env, text=True, encoding="utf-8", capture_output=True, timeout=120,
        )
        second = subprocess.run(
            [sys.executable, str(STORE), "capture-training-review", "--input", str(review_path)],
            cwd=ROOT, env=reviewer_env, text=True, encoding="utf-8", capture_output=True, timeout=120,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        mismatched = self._run("capture-training-review", "--input", str(review_path))
        self.assertEqual(mismatched.returncode, 1)
        self.assertIn("reviewer identity must match", mismatched.stderr)
        conn = sqlite3.connect(self.store)
        try:
            self.assertEqual(conn.execute(
                "SELECT reviewer_id, reviewer_confirmed FROM training_capture_completion WHERE capture_id=?",
                (capture_id,),
            ).fetchone(), ("reviewer-b", 1))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
