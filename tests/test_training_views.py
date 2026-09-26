"""CLI contract tests for the governed training export views.

The exporter reads the canonical SQLite store and writes a disposable view.
These tests exercise that boundary through ``store.py`` so the assertions cover
the refusal paths, selection filters, and completion marker together rather
than duplicating the row-building implementation.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# Pin the store before any package import.  The storelib modules resolve their
# SQLite path at import time, and an ambient ZMEM_STORE must never point these
# tests at the operator's canonical database.
_BOOTSTRAP_ROOT = Path(tempfile.mkdtemp(prefix="zmem-training-views-import-"))
os.environ["ZMEM_STORE"] = str(_BOOTSTRAP_ROOT / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_BOOTSTRAP_ROOT / "data")
for _key in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
    os.environ.pop(_key, None)
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["PYTHONUTF8"] = "1"
atexit.register(shutil.rmtree, _BOOTSTRAP_ROOT, True)


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"


class TrainingViewsContractTests(unittest.TestCase):
    """Verify the public export surface and its read-only artifact contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="zmem-training-views-")
        self.root = Path(self._tmp.name)
        self.store = self.root / "store.sqlite"
        self.data = self.root / "data"
        self.env = os.environ.copy()
        self.env.update(
            {
                "ZMEM_STORE": str(self.store),
                "ZMEM_DATA": str(self.data),
                "ZMEM_EMBED_PROFILE": "fake",
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "ZMEM_MODELS_DIR": str(self.root / "models"),
                "ZMEM_LINK_THRESHOLD": "1.01",
                "PYTHONUTF8": "1",
            }
        )
        for key in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
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

    def _init_store(self) -> None:
        result = self._run("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.store.is_file())

    def _seed_capture(
        self,
        *,
        capture_id: str,
        namespace: str,
        memory_id: str,
        evidence_id: str,
        event_id: str,
        task_id: str,
        session_id: str,
    ) -> None:
        """Seed one immutable completed capture in an isolated test store.

        The fixture represents canonical rows already written by the governed
        capture APIs.  It is intentionally small so these tests focus on the
        export boundary; capture writer transitions are covered by the capture
        contract suite.
        """
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, ingestion_ts, "
                "trust_score, applied_count, violated_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    namespace,
                    "fact",
                    f"Use the safe deploy command for {task_id}",
                    "2026-01-01T00:00:00Z",
                    1.0,
                    3,
                    0,
                ),
            )
            episode_id = f"episode-{capture_id}"
            conn.execute(
                "INSERT INTO episode (id, namespace, started_at) VALUES (?, ?, ?)",
                (episode_id, namespace, "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO episode_memory (episode_id, memory_id) VALUES (?, ?)",
                (episode_id, memory_id),
            )
            conn.execute(
                "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, hash, "
                "excerpt, ref_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    session_id,
                    "test",
                    "stop",
                    "test_result",
                    "2026-01-01T00:00:00Z",
                    f"hash-{evidence_id}",
                    f"verified evidence for {event_id}",
                    "fixture://training-views",
                ),
            )
            conn.execute(
                "INSERT INTO memory_evidence (memory_id, evidence_id) VALUES (?, ?)",
                (memory_id, evidence_id),
            )
            conn.execute(
                "INSERT INTO training_capture (capture_id, host, host_task_id, "
                "session_id, namespace, created_at, updated_at, finalized_at, "
                "acknowledged_at, acknowledgement_attestation, state, prompt, "
                "assistant_response, consent_scope, content_license, redaction_status, "
                "redaction_policy_version, governance_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    "fixture",
                    task_id,
                    session_id,
                    namespace,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "fixture-ack",
                    "completed",
                    f"What command should I use for {task_id}?",
                    f"Use the safe deploy command for {task_id}.",
                    "fixture-scope",
                    "fixture-license",
                    "redacted",
                    "policy-v1",
                    "fixture",
                ),
            )
            conn.execute(
                "INSERT INTO training_delivery_snapshot (delivery_snapshot_id, "
                "capture_id, rendered, effective_ops_json, rendered_hash, "
                "transform_version, emitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"delivery-{capture_id}",
                    capture_id,
                    f"<context>{memory_id}</context>",
                    json.dumps([f"deploy --safe --task {task_id}"]),
                    f"rendered-hash-{capture_id}",
                    "transform-v1",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO training_capture_completion (capture_id, evidence_id, "
                "verifier_id, verified_at, outcome_kind, outcome_value, "
                "acknowledgement_attestation, export_consent_scope, "
                "export_content_license, reviewer_confirmed, correction_closeout, "
                "associated_memory_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    evidence_id,
                    "fixture-verifier",
                    "2026-01-01T00:00:00Z",
                    "test",
                    "passed",
                    "fixture-ack",
                    "fixture-export-scope",
                    "fixture-export-license",
                    0,
                    0,
                    json.dumps([memory_id]),
                ),
            )
            conn.execute(
                "INSERT INTO training_capture_observation (observation_id, "
                "capture_id, observation_kind, payload, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"observation-{capture_id}",
                    capture_id,
                    "event",
                    json.dumps({"source_event_id": event_id}),
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _manifest(self, output: Path) -> dict[str, object]:
        return json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    def test_empty_export_is_schema_only_and_store_bytes_are_unchanged(self) -> None:
        self._init_store()
        before = self.store.read_bytes()
        output = self.root / "training-empty"
        result = self._run(
            "export-training",
            str(output),
            "--snapshot-id",
            "empty-contract",
            "--reviewer-confirmed",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.read_bytes(), before)

        import pyarrow.parquet as pq

        self.assertEqual(
            pq.read_schema(output / "sft-000.parquet").names,
            [
                "task_id",
                "project_key",
                "session_id",
                "episode_id",
                "prompt",
                "context_fence",
                "ops_tokens",
                "assistant_response",
                "outcome_kind",
                "outcome_value",
                "evidence_ref",
                "source_memory_ids",
                "source_event_ids",
                "consent_scope",
                "content_license",
                "redaction_status",
                "redaction_policy_version",
                "split_key",
                "transform_version",
                "label_status",
                "exclusion_reason",
                "row_checksum",
            ],
        )
        self.assertEqual(
            pq.read_schema(output / "preferences-000.parquet").names,
            [
                "task_id",
                "project_key",
                "session_id",
                "episode_id",
                "prompt",
                "context_fence",
                "chosen",
                "rejected",
                "update_of",
                "supersede_reason",
                "source_memory_ids",
                "source_event_ids",
                "evidence_ref",
                "consent_scope",
                "content_license",
                "redaction_status",
                "redaction_policy_version",
                "split_key",
                "transform_version",
                "row_checksum",
            ],
        )
        self.assertEqual(pq.read_table(output / "sft-000.parquet").num_rows, 0)
        self.assertEqual(pq.read_table(output / "preferences-000.parquet").num_rows, 0)
        self.assertEqual(
            self._manifest(output)["row_counts"], {"preferences": 0, "sft": 0}
        )
        self.assertTrue((output / "deletion-map.json").is_file())
        self.assertFalse((output / "quarantine").exists())

    def test_namespace_selection_preserves_source_id_provenance(self) -> None:
        self._init_store()
        self._seed_capture(
            capture_id="cap-selected",
            namespace="project:selected",
            memory_id="mem-selected",
            evidence_id="evidence-selected",
            event_id="event-selected",
            task_id="task-selected",
            session_id="session-selected",
        )
        self._seed_capture(
            capture_id="cap-other",
            namespace="project:other",
            memory_id="mem-other",
            evidence_id="evidence-other",
            event_id="event-other",
            task_id="task-other",
            session_id="session-other",
        )
        before = self.store.read_bytes()
        output = self.root / "training-selected"
        result = self._run(
            "export-training",
            str(output),
            "--snapshot-id",
            "selected-contract",
            "--reviewer-confirmed",
            "--namespace",
            "project:selected",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.read_bytes(), before)

        import pyarrow.parquet as pq

        rows = pq.read_table(output / "sft-000.parquet").to_pylist()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_id"], "task-selected")
        self.assertEqual(row["source_memory_ids"], ["mem-selected"])
        self.assertEqual(row["source_event_ids"], ["event-selected"])
        self.assertEqual(row["evidence_ref"], "evidence-selected")
        self.assertNotIn("event-other", row["source_event_ids"])
        self.assertEqual(self._manifest(output)["namespace"], "project:selected")

    def test_manifest_is_last_completion_marker_and_checksums_bind_artifacts(self) -> None:
        self._init_store()
        self._seed_capture(
            capture_id="cap-manifest",
            namespace="project:manifest",
            memory_id="mem-manifest",
            evidence_id="evidence-manifest",
            event_id="event-manifest",
            task_id="task-manifest",
            session_id="session-manifest",
        )
        output = self.root / "training-manifest"
        result = self._run(
            "export-training",
            str(output),
            "--snapshot-id",
            "manifest-contract",
            "--reviewer-confirmed",
            "--namespace",
            "project:manifest",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        manifest = self._manifest(output)
        artifacts = {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in (
                "sft-000.parquet",
                "preferences-000.parquet",
                "deletion-map.json",
            )
        }
        self.assertEqual(manifest["artifact_sha256"], artifacts)
        self.assertEqual(manifest["sft_sha256"], artifacts["sft-000.parquet"])
        self.assertEqual(
            manifest["preferences_sha256"], artifacts["preferences-000.parquet"]
        )
        self.assertEqual(manifest["deletion_map_sha256"], artifacts["deletion-map.json"])
        self.assertFalse(
            any(path.name.startswith(".training-staging-") for path in output.parent.iterdir())
        )

    def test_failed_manifest_install_leaves_no_completion_marker(self) -> None:
        self._init_store()
        self._seed_capture(
            capture_id="cap-failure",
            namespace="project:failure",
            memory_id="mem-failure",
            evidence_id="evidence-failure",
            event_id="event-failure",
            task_id="task-failure",
            session_id="session-failure",
        )
        output = self.root / "training-failure"

        # This probes the externally meaningful atomicity rule: the manifest is
        # the completion marker and is installed after every derived artifact.
        sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
        try:
            from storelib.training import write_training_views

            conn = sqlite3.connect(self.store)
            conn.row_factory = sqlite3.Row
            try:
                original_replace = os.replace
                installed: list[str] = []

                def fail_manifest(source: str, destination: str) -> None:
                    installed.append(Path(source).name)
                    if Path(source).name == "manifest.json":
                        raise OSError("fixture manifest install failure")
                    original_replace(source, destination)

                with mock.patch("storelib.training.os.replace", side_effect=fail_manifest):
                    with self.assertRaisesRegex(OSError, "manifest install failure"):
                        write_training_views(
                            conn,
                            out_dir=str(output),
                            namespace="project:failure",
                            snapshot_id="manifest-failure",
                            reviewer_confirmed=True,
                        )
            finally:
                conn.close()
        finally:
            sys.path.remove(str(ROOT / "skills" / "memory" / "scripts"))

        self.assertEqual(installed[-1], "manifest.json")
        self.assertFalse((output / "manifest.json").exists())

    def test_export_refuses_missing_confirmation_before_creating_store(self) -> None:
        output = self.root / "refused"
        missing_snapshot = self._run("export-training", str(output))
        self.assertEqual(missing_snapshot.returncode, 2)
        self.assertEqual(missing_snapshot.stderr, "--snapshot-id is required\n")
        self.assertFalse(self.store.exists())
        self.assertFalse(output.exists())

        missing_confirmation = self._run(
            "export-training", str(output), "--snapshot-id", "refused-confirmation"
        )
        self.assertEqual(missing_confirmation.returncode, 2)
        self.assertEqual(
            missing_confirmation.stderr, "--reviewer-confirmed is required\n"
        )
        self.assertFalse(self.store.exists())
        self.assertFalse(output.exists())

    def test_export_refuses_non_directory_without_overwriting_it(self) -> None:
        self._init_store()
        output = self.root / "existing-file"
        original = b"keep this caller file"
        output.write_bytes(original)
        result = self._run(
            "export-training",
            str(output),
            "--snapshot-id",
            "non-directory-output",
            "--reviewer-confirmed",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to overwrite non-directory training output", result.stderr)
        self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
