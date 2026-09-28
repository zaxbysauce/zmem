"""Production hook adapter to SQLite/CLI integration checks for issue #135."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
ADAPTER = ROOT / "hooks" / "lib" / "zmem-training-capture.py"


class TrainingCaptureHostIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-training-host-")
        self.root = Path(self.tmp.name)
        self.store = self.root / "store.sqlite"
        self.data = self.root / "data"
        self.env = os.environ.copy()
        self.env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.data),
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_MODELS_DIR": str(self.root / "models"),
            "PYTHONUTF8": "1",
        })
        for key in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adapter(
        self,
        action: str,
        payload: dict[str, object],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(ADAPTER), "--action", action],
            cwd=ROOT,
            env={**self.env, **(extra_env or {})},
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STORE), *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=120,
        )

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        return conn

    def _launcher_capture(
        self,
        payload: dict[str, object],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Run the production JS launcher against the real Python adapter."""
        script = (
            "const launch = require(process.argv[1]);"
            "const meta = JSON.parse(process.argv[2]);"
            "const env = {...process.env};"
            "const started = launch.runTrainingCapture('codex', 'recall', meta, env, 'start');"
            "launch.snapshotTrainingDelivery('codex', 'recall', meta, env, "
            "{rendered: 'launcher context', effective_ops: ['run tests']});"
            "process.stdout.write(JSON.stringify(started));"
        )
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "hooks" / "zmem-launch.js"),
             json.dumps(payload)],
            cwd=ROOT,
            env={**self.env, **(extra_env or {})},
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(payload["host_task_id"]), result.stdout)
        return json.loads(result.stdout)

    def test_production_launcher_wires_real_adapter_and_preserves_delivery_sidecar(self) -> None:
        task_id = "launcher-native-task-secret"
        started = self._launcher_capture({
            "session_id": "session-launcher",
            "namespace": "project:integration",
            "capture_key": "turn-launcher",
            "host_task_id": task_id,
            "prompt": "launcher prompt",
        })
        self.assertEqual(started["state"], "partial")
        self.assertTrue(started["capture_id"])

        # The production launcher deliberately detaches observe/snapshot. Give
        # the real Python adapter/store child enough scheduler slack to commit.
        time.sleep(2.0)
        conn = self._db()
        try:
            capture = conn.execute(
                "SELECT state FROM training_capture WHERE capture_id=?",
                (started["capture_id"],),
            ).fetchone()
            delivery = conn.execute(
                "SELECT delivery_snapshot_id FROM training_delivery_snapshot "
                "WHERE capture_id=?", (started["capture_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(capture[0], "emitted_to_host")
        self.assertIsNotNone(delivery)
        # With an explicit ZMEM_STORE, the adapter co-locates its hashed
        # correlation sidecar beside that store.
        sidecars = list((self.store.parent / "training-capture").glob("*.json"))
        self.assertEqual(len(sidecars), 1)
        self.assertNotIn(task_id, sidecars[0].name)
        self.assertNotIn(task_id.encode("utf-8"), sidecars[0].read_bytes())
        sidecar = json.loads(sidecars[0].read_text(encoding="utf-8"))
        self.assertEqual(sidecar["capture_id"], started["capture_id"])
        self.assertEqual(sidecar["delivery_snapshot_id"], delivery[0])
        self.assertNotIn(task_id.encode("utf-8"), self.store.read_bytes())

    def _seed_verified_source(self, *, session_id: str, namespace: str) -> tuple[str, str]:
        initialized = self._cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        memory_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())
        episode_id = f"episode-{memory_id}"
        ts = "2026-01-01T00:00:00Z"
        evidence_hash = hashlib.sha256(
            f"test_result|{ts}|verified integration event".encode("utf-8")
        ).hexdigest()
        conn = self._db()
        try:
            conn.execute(
                "INSERT INTO memory "
                "(id, namespace, type, content, ingestion_ts, trust_score, applied_count, violated_count) "
                "VALUES (?, ?, 'fact', 'verified integration memory', ?, 1.0, 3, 0)",
                (memory_id, namespace, ts),
            )
            conn.execute(
                "INSERT INTO episode (id, namespace, started_at) VALUES (?, ?, ?)",
                (episode_id, namespace, ts),
            )
            conn.execute(
                "INSERT INTO episode_memory (episode_id, memory_id) VALUES (?, ?)",
                (episode_id, memory_id),
            )
            conn.execute(
                "INSERT INTO evidence "
                "(id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path, ref_offset) "
                "VALUES (?, ?, 'codex', 'user_prompt', 'test_result', ?, ?, "
                "'verified integration event', 'fixture', 0)",
                (evidence_id, session_id, ts, evidence_hash),
            )
            conn.execute(
                "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
            conn.commit()
        finally:
            conn.close()
        return memory_id, evidence_id

    @staticmethod
    def _capture_payload(
        *, session_id: str, namespace: str, capture_key: str, task_id: str,
    ) -> dict[str, object]:
        return {
            "host": "codex",
            "session_id": session_id,
            "namespace": namespace,
            "capture_key": capture_key,
            "host_task_id": task_id,
            "prompt": "Use the verified memory",
            "assistant_response": "The answer is verified.",
        }

    def test_default_deny_partial_is_not_exportable(self) -> None:
        session_id = "session-default-deny"
        namespace = "project:integration"
        key = "turn-default-deny"
        task_id = "default-deny-task"
        payload = self._capture_payload(
            session_id=session_id, namespace=namespace,
            capture_key=key, task_id=task_id,
        )
        started = self._adapter("start", payload)
        self.assertEqual(started["state"], "partial")
        observed = self._adapter("observe", {
            **payload,
            "observation_kind": "post_tool",
            "observation": {"status": "completed", "capture_key": key},
        })
        self.assertEqual(observed["capture_id"], started["capture_id"])
        snapshot = self._adapter("snapshot", {
            **payload,
            "rendered": "context withheld by default deny",
            "effective_ops": ["run tests"],
        })
        self.assertEqual(snapshot["capture_id"], started["capture_id"])

        conn = self._db()
        try:
            row = conn.execute(
                "SELECT state, redaction_status, prompt, assistant_response "
                "FROM training_capture WHERE capture_id=?",
                (started["capture_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            tuple(row), ("emitted_to_host", "metadata_only", None, None)
        )

        output = self.root / "before-completion"
        exported = self._cli(
            "export-training", str(output), "--snapshot-id", "before-completion",
            "--reviewer-confirmed", "--namespace", namespace,
        )
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads(exported.stdout)["sft_count"], 0)

    def test_verified_completion_exports_and_redacts_task_id(self) -> None:
        session_id = "session-verified"
        namespace = "project:integration"
        key = "turn-verified"
        task_id = "native-task-secret-123"
        memory_id, evidence_id = self._seed_verified_source(
            session_id=session_id, namespace=namespace
        )
        governance = {
            "ZMEM_CAPTURE_CONSENT_SCOPE": "local-training",
            "ZMEM_CAPTURE_CONTENT_LICENSE": "CC-BY-4.0",
            "ZMEM_CAPTURE_REDACTION_POLICY_VERSION": "policy-v1",
        }
        payload = self._capture_payload(
            session_id=session_id, namespace=namespace,
            capture_key=key, task_id=task_id,
        )
        started = self._adapter("start", payload, extra_env=governance)
        observed = self._adapter("observe", {
            **payload,
            "observation_kind": "source_event",
            "observation": {"source_event_id": evidence_id, "capture_key": key},
        }, extra_env=governance)
        self.assertEqual(observed["capture_id"], started["capture_id"])
        snapshot = self._adapter("snapshot", {
            **payload,
            "rendered": "Verified context",
            "effective_ops": ["run tests"],
        }, extra_env=governance)
        delivery_id = snapshot["delivery_snapshot_id"]
        self.assertTrue(delivery_id)

        conn = self._db()
        try:
            stored_task = conn.execute(
                "SELECT host_task_id FROM training_capture WHERE capture_id=?",
                (started["capture_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        # Host task ids are correlation metadata only.  The capture table may
        # leave the compatibility column NULL; either representation must not
        # persist the host supplied secret-shaped value.
        self.assertNotIn(task_id, str(stored_task))

        # Bind the trusted source event through the real CLI replay path.
        delivery_input = self.root / "delivery-replay.json"
        delivery_input.write_text(json.dumps({
            **payload,
            "delivery_snapshot_id": delivery_id,
            "rendered": "Verified context",
            "effective_ops": ["run tests"],
            "source_event_id": evidence_id,
            "consent_scope": "local-training",
            "content_license": "CC-BY-4.0",
            "redaction_policy_version": "policy-v1",
        }), encoding="utf-8")
        delivery = self._cli("capture-training-delivery", "--input", str(delivery_input))
        self.assertEqual(delivery.returncode, 0, delivery.stderr)

        ack_input = self.root / "ack.json"
        ack_input.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id,
            "attestation": {"attested_by": "local-cli"},
        }), encoding="utf-8")
        acknowledged = self._cli(
            "capture-training-acknowledge", "--input", str(ack_input)
        )
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)

        completion_input = self.root / "completion.json"
        completion_input.write_text(json.dumps({
            "delivery_snapshot_id": delivery_id,
            "evidence_id": evidence_id,
            "memory_ids": [memory_id],
            "verifier_id": "local-cli",
            "outcome_kind": "test",
            "outcome_value": "passed",
            "consent_scope": "local-training",
            "content_license": "CC-BY-4.0",
        }), encoding="utf-8")
        completed = self._cli(
            "capture-training-completion", "--input", str(completion_input)
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        output = self.root / "after-completion"
        exported = self._cli(
            "export-training", str(output), "--snapshot-id", "after-completion",
            "--reviewer-confirmed", "--namespace", namespace, "--quarantine-raw",
        )
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads(exported.stdout)["sft_count"], 1,
                         exported.stdout + exported.stderr)
        import pyarrow.parquet as pq
        row = pq.read_table(output / "sft-000.parquet").to_pylist()[0]
        self.assertNotIn(task_id, str(row["task_id"]))
        for artifact in output.rglob("*"):
            if artifact.is_file():
                self.assertNotIn(task_id.encode("utf-8"), artifact.read_bytes())

    def test_opt_out_creates_no_store_or_sidecar(self) -> None:
        payload = self._capture_payload(
            session_id="session-opt-out", namespace="project:integration",
            capture_key="turn-opt-out", task_id="opt-out-task",
        )
        self.assertEqual(
            self._launcher_capture(payload, extra_env={"ZMEM_CAPTURE": "0"}),
            {},
        )
        for action, extra in (
            ("start", {}),
            ("observe", {"observation": {"status": "ignored"}}),
            ("snapshot", {"rendered": "ignored", "effective_ops": []}),
        ):
            result = self._adapter(
                action, {**payload, **extra}, extra_env={"ZMEM_CAPTURE": "0"}
            )
            self.assertEqual(result, {})
        self.assertFalse(self.store.exists())
        self.assertFalse((self.data / "training-capture").exists())
        self.assertFalse((self.store.parent / "training-capture").exists())
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + suffix).exists())

    def test_malformed_delivery_id_leaves_no_orphan(self) -> None:
        initialized = self._cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        delivery_input = self.root / "malformed.json"
        delivery_input.write_text(json.dumps({
            "delivery_snapshot_id": "not-a-uuid",
            "host": "codex",
            "session_id": "session-malformed",
            "namespace": "project:integration",
            "capture_key": "turn-malformed",
            "prompt": "prompt",
            "assistant_response": "response",
            "rendered": "context",
            "effective_ops": [],
            "consent_scope": "local-training",
            "content_license": "CC-BY-4.0",
            "redaction_policy_version": "policy-v1",
        }), encoding="utf-8")
        failed = self._cli("capture-training-delivery", "--input", str(delivery_input))
        self.assertNotEqual(failed.returncode, 0)
        conn = self._db()
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM training_capture").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
