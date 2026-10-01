"""Focused tests for the host-side automatic partial-capture adapter."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr


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
        self.correlations: dict[str, str] = {}
        self.api = {
            "connect": lambda: _Conn(),
            "prepare": None,
            "start": self._start,
            "start_correlated": self._start_correlated,
            "observe": self._observe,
            "observe_correlated": self._observe_correlated,
            "snapshot": self._snapshot,
            "snapshot_correlated": self._snapshot_correlated,
            "clear_correlated": self._clear_correlated,
            "identity_keys": self._identity_keys,
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _capture_state_records(self) -> list[dict]:
        directory = Path(self.tmp.name) / "training-capture"
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in directory.glob("*.json")
            if "capture_id" in json.loads(path.read_text(encoding="utf-8"))
        ]

    def _start(self, conn, **kwargs):
        self.calls.append(("start", kwargs))
        return {
            "capture_id": self.capture_id,
            "state": "partial",
            "redaction_status": "metadata_only" if not kwargs["consent_scope"] else "redacted",
        }

    def _identity_keys(self, *, host, session_id, namespace, task_id=None,
                       turn_id=None, require_turn=True):
        scope = json.dumps({"host": host, "namespace": namespace, "session": session_id},
                           sort_keys=True, separators=(",", ":")).encode()
        session_key = hashlib.sha256(scope).hexdigest()
        if not require_turn:
            return "", session_key
        identity = json.dumps({"host": host, "namespace": namespace,
                               "session": session_id, "task": task_id, "turn": turn_id},
                              sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(identity).hexdigest(), session_key

    def _start_correlated(self, conn, *, task_id, turn_id, **kwargs):
        key, _ = self._identity_keys(host=kwargs["host"], session_id=kwargs["session_id"],
                                     namespace=kwargs["namespace"], task_id=task_id, turn_id=turn_id)
        row = self._start(conn, **kwargs)
        self.correlations[key] = row["capture_id"]
        return row

    def _observe_correlated(self, conn, *, host, session_id, namespace, task_id, turn_id, **kwargs):
        key, _ = self._identity_keys(host=host, session_id=session_id, namespace=namespace,
                                     task_id=task_id, turn_id=turn_id)
        capture_id = self.correlations.get(key)
        if not capture_id:
            raise ValueError("unknown correlation")
        return self._observe(conn, capture_id, **kwargs)

    def _snapshot_correlated(self, conn, *, host, session_id, namespace, task_id, turn_id, **kwargs):
        key, _ = self._identity_keys(host=host, session_id=session_id, namespace=namespace,
                                     task_id=task_id, turn_id=turn_id)
        capture_id = self.correlations.get(key)
        if not capture_id:
            raise ValueError("unknown correlation")
        return self._snapshot(conn, capture_id, **kwargs)

    def _clear_correlated(self, conn, *, host, session_id, namespace, remove_sidecars):
        _unused, session_key = self._identity_keys(host=host, session_id=session_id,
                                                    namespace=namespace, require_turn=False)
        keys = [key for key in self.correlations if key]
        remove_sidecars(keys)
        self.correlations.clear()

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
            "host_task_id": "task-default-deny",
            "turn_id": "turn-default-deny",
            "prompt": "Bearer super-secret-token-value",
        }, env=self.env, api=self.api)

        self.assertEqual(result["state"], "partial")
        call = self.calls[0][1]
        self.assertIsNone(call["consent_scope"])
        self.assertIsNone(call["content_license"])
        self.assertIsNone(call["redaction_policy_version"])
        # The adapter forwards content to the storage API; default-deny
        # redaction is enforced by the production capture writer when these
        # governance values are absent.
        self.assertEqual(call["prompt"], "Bearer super-secret-token-value")
        self.assertNotIn("acknowledge", " ".join(name for name, _ in self.calls))
        self.assertNotIn("complete", " ".join(name for name, _ in self.calls))

    def test_missing_session_and_namespace_still_records_metadata_partial(self):
        result = ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "turn_id": "turn-metadata-only",
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
        self.assertEqual(observed, {})

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
            "turn_id": "turn-opt-in",
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
            "host_task_id": "task-3",
            "turn_id": "turn-3",
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
            "host_task_id": "task-4",
            "turn_id": "turn-4",
            "prompt": "private prompt",
        }, env=self.env, api=self.api)
        state_records = self._capture_state_records()
        self.assertEqual(len(state_records), 1)
        state = state_records[0]
        self.assertEqual(state["capture_id"], self.capture_id)
        self.assertIsInstance(state["generation"], str)
        self.assertEqual(set(state), {"capture_id", "generation"})

    def test_snapshot_delivery_identity_is_preserved_in_the_sidecar(self):
        base = {
            "host": "claude", "session_id": "session-delivery",
            "namespace": "project:hook-test", "host_task_id": "task-delivery",
            "turn_id": "turn-delivery",
        }
        ADAPTER.run_action({"action": "start", **base}, env=self.env, api=self.api)
        result = ADAPTER.run_action({
            "action": "snapshot", **base, "rendered": "context",
        }, env=self.env, api=self.api)
        self.assertEqual(
            result["delivery_snapshot_id"],
            "22222222-2222-4222-8222-222222222222",
        )
        state_records = self._capture_state_records()
        self.assertEqual(len(state_records), 1)
        state = state_records[0]
        self.assertEqual(state["capture_id"], self.capture_id)
        self.assertEqual(state["delivery_snapshot_id"], result["delivery_snapshot_id"])

    def test_delayed_snapshot_cannot_publish_against_a_newer_generation(self):
        base = {
            "host": "claude", "session_id": "session-generation",
            "namespace": "project:hook-test", "host_task_id": "task-generation",
            "turn_id": "turn-generation",
        }
        ADAPTER.run_action({"action": "start", **base}, env=self.env, api=self.api)
        state_key, _session_key, _fields = ADAPTER._identity_keys(
            base, self.api, require_turn=True,
        )
        replacement_capture = "33333333-3333-4333-8333-333333333333"
        replacement_generation = "44444444-4444-4444-8444-444444444444"

        def delayed_snapshot(conn, capture_id, **kwargs):
            ADAPTER._write_state(
                state_key, replacement_capture, self.env,
                generation=replacement_generation,
            )
            return {
                "capture_id": capture_id,
                "delivery_snapshot_id": "22222222-2222-4222-8222-222222222222",
                "state": "emitted_to_host",
            }

        def delayed_snapshot_correlated(conn, **kwargs):
            return delayed_snapshot(conn, self.capture_id, **kwargs)

        self.api["snapshot_correlated"] = delayed_snapshot_correlated
        result = ADAPTER.run_action({
            "action": "snapshot", **base, "rendered": "old context",
        }, env=self.env, api=self.api)
        self.assertEqual(result["capture_id"], self.capture_id)
        state_records = self._capture_state_records()
        self.assertEqual(len(state_records), 1)
        state = state_records[0]
        self.assertEqual(state, {
            "capture_id": replacement_capture,
            "generation": replacement_generation,
        })

    def test_keyless_starts_are_fresh_and_never_create_reusable_sidecar(self):
        first = ADAPTER.run_action({
            "action": "start", "host": "claude", "session_id": "same-session",
        }, env=self.env, api=self.api)
        self.capture_id = "22222222-2222-4222-8222-222222222222"
        second = ADAPTER.run_action({
            "action": "start", "host": "claude", "session_id": "same-session",
        }, env=self.env, api=self.api)

        self.assertEqual(first["capture_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(second["capture_id"], "22222222-2222-4222-8222-222222222222")
        self.assertFalse((Path(self.tmp.name) / "training-capture").exists())
        self.assertEqual(ADAPTER.run_action({
            "action": "snapshot", "host": "claude", "session_id": "same-session",
            "capture_id": first["capture_id"], "rendered": "must not attach",
        }, env=self.env, api=self.api), {})

    def test_keyless_content_becomes_metadata_only_without_a_sidecar(self):
        result = ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "session_id": "keyless-content",
            "prompt": "Bearer keyless-secret-must-not-persist",
            "assistant_response": "assistant bytes must not persist",
        }, env=self.env, api=self.api)

        self.assertEqual(result["state"], "partial")
        self.assertIsNone(self.calls[0][1]["prompt"])
        self.assertIsNone(self.calls[0][1]["assistant_response"])
        self.assertFalse((Path(self.tmp.name) / "training-capture").exists())

    def test_oversized_identity_does_not_alias_its_prefix(self):
        base = {
            "action": "start", "host": "claude", "session_id": "session-prefix",
            "namespace": "project:hook-test", "host_task_id": "task-prefix",
            "turn_id": "turn-prefix",
        }
        oversized = {**base, "host_task_id": "task-prefix" + ("x" * 513)}
        self.assertEqual(ADAPTER.run_action(oversized, env=self.env, api=self.api), {})
        self.assertEqual(self.calls, [])
        normal = ADAPTER.run_action(base, env=self.env, api=self.api)
        self.assertEqual(normal["capture_id"], self.capture_id)

    def test_content_reaches_core_without_prefix_clipping(self):
        prompt = ("x" * 16_000) + " Bearer full-secret-value"
        result = ADAPTER.run_action({
            "action": "start", "host": "claude", "session_id": "content-boundary",
            "namespace": "project:hook-test", "host_task_id": "task-boundary",
            "turn_id": "turn-boundary", "prompt": prompt,
        }, env={
            **self.env, "ZMEM_CAPTURE_CONSENT_SCOPE": "local-training",
            "ZMEM_CAPTURE_CONTENT_LICENSE": "CC-BY-4.0",
            "ZMEM_CAPTURE_REDACTION_POLICY_VERSION": "v1",
        }, api=self.api)
        self.assertEqual(result["capture_id"], self.capture_id)
        self.assertEqual(self.calls[0][1]["prompt"], prompt)

    def test_lock_timeout_never_falls_back_to_an_unlocked_capture(self):
        class BusyLock:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                raise TimeoutError("capture_lock_timeout")

            def __exit__(self, *_args):
                return None

        payload = {
            "action": "start", "host": "claude", "session_id": "locked-session",
            "namespace": "project:hook-test", "host_task_id": "locked-task",
            "turn_id": "locked-turn",
        }
        with mock.patch.object(ADAPTER, "_SessionLock", BusyLock):
            diagnostic = io.StringIO()
            with redirect_stderr(diagnostic):
                self.assertEqual(ADAPTER.run_action(payload, env=self.env, api=self.api), {})
            self.assertEqual(ADAPTER.run_action(
                {**payload, "action": "clear"}, env=self.env, api=self.api,
            ), {"error": "capture_busy"})
        self.assertEqual(diagnostic.getvalue(), "capture_lock_timeout\n")
        self.assertNotIn("locked-session", diagnostic.getvalue())
        self.assertEqual(self.calls, [])

    def test_standalone_content_uses_no_correlation_sidecar(self):
        result = ADAPTER.run_action({
            "action": "start_standalone",
            "host": "hermes",
            "session_id": "standalone-session",
            "namespace": "project:hook-test",
            "capture_key": "provider-fresh-key",
            "prompt": "standalone prompt",
            "assistant_response": "standalone response",
        }, env=self.env, api=self.api)

        self.assertEqual(result["state"], "partial")
        self.assertEqual(self.calls[0][1]["prompt"], "standalone prompt")
        self.assertEqual(
            self.calls[0][1]["assistant_response"], "standalone response",
        )
        self.assertFalse(list((Path(self.tmp.name) / "training-capture").glob("*.json")))

    def test_caller_capture_id_cannot_override_keyed_sidecar(self):
        base = {
            "host": "claude", "session_id": "keyed",
            "namespace": "project:hook-test", "host_task_id": "task-keyed",
            "turn_id": "turn-a",
        }
        ADAPTER.run_action({"action": "start", **base}, env=self.env, api=self.api)
        observed = ADAPTER.run_action({
            "action": "observe", **base,
            "capture_id": "22222222-2222-4222-8222-222222222222",
            "observation": {"status": "wrong-target"},
        }, env=self.env, api=self.api)
        self.assertEqual(observed["capture_id"], self.capture_id)
        self.assertEqual(self.calls[-1][1]["capture_id"], self.capture_id)

    def test_kill_switch_is_authoritative_and_does_not_create_state(self):
        env = {**self.env, "ZMEM_CAPTURE": "0"}
        result = ADAPTER.run_action({
            "action": "start",
            "host": "claude",
            "session_id": "disabled",
            "turn_id": "turn-disabled",
            "prompt": "must not be captured",
        }, env=env, api=self.api)
        self.assertEqual(result, {})
        self.assertEqual(self.calls, [])
        self.assertFalse((Path(self.tmp.name) / "training-capture").exists())

    def test_immutable_turn_keys_keep_delayed_callbacks_on_their_capture(self):
        base = {
            "host": "claude",
            "session_id": "overlapping",
            "namespace": "project:hook-test",
        }
        first = ADAPTER.run_action({
            "action": "start", **base, "turn_id": "turn-a", "host_task_id": "task-shared",
        }, env=self.env, api=self.api)
        self.capture_id = "22222222-2222-4222-8222-222222222222"
        ADAPTER.run_action({
            "action": "start", **base, "turn_id": "turn-b", "host_task_id": "task-shared",
        }, env=self.env, api=self.api)
        observed = ADAPTER.run_action({
            "action": "observe", **base, "turn_id": "turn-a", "host_task_id": "task-shared",
            "capture_id": "malformed-capture-id",
            "observation_kind": "post_tool",
            "observation": {"status": "delayed"},
        }, env=self.env, api=self.api)
        self.assertEqual(observed["capture_id"], first["capture_id"])

    def test_repeated_turns_sharing_task_and_session_do_not_reuse_sidecar(self):
        base = {
            "host": "claude", "session_id": "shared-session",
            "namespace": "project:hook-test", "host_task_id": "task-shared",
        }
        first = ADAPTER.run_action({
            "action": "start", **base, "turn_id": "turn-one",
        }, env=self.env, api=self.api)
        self.capture_id = "22222222-2222-4222-8222-222222222222"
        second = ADAPTER.run_action({
            "action": "start", **base, "turn_id": "turn-two",
        }, env=self.env, api=self.api)
        self.assertNotEqual(first["capture_id"], second["capture_id"])
        self.assertEqual(ADAPTER.run_action({
            "action": "observe", **base,
            "observation": {"status": "no-turn-key"},
        }, env=self.env, api=self.api), {})


if __name__ == "__main__":
    unittest.main()
