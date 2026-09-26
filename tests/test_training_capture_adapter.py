"""Focused tests for the host-side automatic partial-capture adapter."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "hooks" / "lib" / "zmem-training-capture.py"
SPEC = importlib.util.spec_from_file_location("zmem_training_capture_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


class _Conn:
    def close(self) -> None:
        return None


class TrainingCaptureAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-training-hook-")
        self.env = {"ZMEM_DATA": self.tmp.name}
        self.calls: list[tuple[str, dict]] = []
        self.capture_id = "11111111-1111-4111-8111-111111111111"
        self.api = {
            "connect": lambda: _Conn(),
            "prepare": None,
            "start": self._start,
            "observe": self._observe,
            "snapshot": self._snapshot,
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _start(self, conn, **kwargs):
        self.calls.append(("start", kwargs))
        return {
            "capture_id": self.capture_id,
            "state": "partial",
            "redaction_status": "metadata_only" if not kwargs["consent_scope"] else "redacted",
        }

    def _observe(self, conn, capture_id, **kwargs):
        self.calls.append(("observe", {"capture_id": capture_id, **kwargs}))
        return {"capture_id": capture_id, "observation_id": "obs"}

    def _snapshot(self, conn, capture_id, **kwargs):
        self.calls.append(("snapshot", {"capture_id": capture_id, **kwargs}))
        return {
            "capture_id": capture_id,
            "delivery_snapshot_id": "22222222-2222-4222-8222-222222222222",
            "state": "emitted_to_host",
        }

    def test_default_deny_forwards_no_content_governance(self):
        result = ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "session_id": "session-1",
            "namespace": "project:hook-test",
            "prompt": "Bearer super-secret-token-value",
        }, env=self.env, api=self.api)

        self.assertEqual(result["state"], "partial")
        call = self.calls[0][1]
        self.assertIsNone(call["consent_scope"])
        self.assertIsNone(call["content_license"])
        self.assertIsNone(call["redaction_policy_version"])
        self.assertEqual(call["prompt"], "Bearer super-secret-token-value")
        self.assertNotIn("acknowledge", " ".join(name for name, _ in self.calls))
        self.assertNotIn("complete", " ".join(name for name, _ in self.calls))

    def test_missing_session_and_namespace_still_records_metadata_partial(self):
        result = ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "prompt": "host did not provide correlation metadata",
        }, env=self.env, api=self.api)

        self.assertEqual(result["state"], "partial")
        self.assertIsNone(self.calls[0][1]["session_id"])
        self.assertIsNone(self.calls[0][1]["namespace"])
        observed = ADAPTER.run_action({
            "action": "observe",
            "host": "claude",
            "observation_kind": "stop",
            "observation": {"status": "unknown"},
        }, env=self.env, api=self.api)
        self.assertEqual(observed["capture_id"], self.capture_id)

    def test_opt_in_forwards_all_governance_values(self):
        env = {
            **self.env,
            "ZMEM_CAPTURE_CONSENT_SCOPE": "local-training",
            "ZMEM_CAPTURE_CONTENT_LICENSE": "CC-BY-4.0",
            "ZMEM_CAPTURE_REDACTION_POLICY_VERSION": "v1",
        }
        ADAPTER.run_action({
            "action": "start",
            "host": "codex",
            "session_id": "session-2",
            "namespace": "project:hook-test",
            "cwd": "C:/repo",
            "host_task_id": "native-task-7",
            "prompt": "bounded prompt",
        }, env=env, api=self.api)

        call = self.calls[0][1]
        self.assertEqual(call["consent_scope"], "local-training")
        self.assertEqual(call["content_license"], "CC-BY-4.0")
        self.assertEqual(call["redaction_policy_version"], "v1")
        self.assertEqual(call["host_task_id"], "native-task-7")

    def test_observation_and_snapshot_reuse_partial_without_finalizing(self):
        base = {
            "host": "zcode",
            "session_id": "session-3",
            "namespace": "project:hook-test",
        }
        ADAPTER.run_action({"action": "start", **base}, env=self.env, api=self.api)
        observed = ADAPTER.run_action({
            "action": "observe",
            **base,
            "observation_kind": "post_tool_failure",
            "observation": {"status": "failed", "error": "redact me"},
        }, env=self.env, api=self.api)
        snap = ADAPTER.run_action({
            "action": "snapshot",
            **base,
            "rendered": "<<<zmem>>>redacted fence",
            "effective_ops": ["run tests"],
        }, env=self.env, api=self.api)

        self.assertEqual(observed["capture_id"], self.capture_id)
        self.assertEqual(snap["state"], "emitted_to_host")
        self.assertEqual(self.calls[1][1]["capture_id"], self.capture_id)
        self.assertEqual(self.calls[2][1]["effective_ops"], ["run tests"])
        self.assertEqual(self.calls[2][1]["rendered"], "<<<zmem>>>redacted fence")

    def test_missing_sidecar_is_a_noop(self):
        result = ADAPTER.run_action({
            "action": "snapshot",
            "host": "claude",
            "session_id": "missing",
            "namespace": "project:hook-test",
            "rendered": "must not be stored",
        }, env=self.env, api=self.api)
        self.assertEqual(result, {})
        self.assertEqual(self.calls, [])

    def test_state_sidecar_contains_only_capture_correlation(self):
        ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "session_id": "session-4",
            "namespace": "project:hook-test",
            "prompt": "private prompt",
        }, env=self.env, api=self.api)
        state_files = list((Path(self.tmp.name) / "training-capture").glob("*.json"))
        self.assertEqual(len(state_files), 1)
        self.assertEqual(json.loads(state_files[0].read_text()), {"capture_id": self.capture_id})


if __name__ == "__main__":
    unittest.main()
