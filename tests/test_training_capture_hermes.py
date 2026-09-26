"""Hermes lifecycle callbacks feed only the bounded partial-capture adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _plugin_context():
    plugins = types.ModuleType("plugins")
    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: "zmem"
    plugins.memory = memory
    agent = types.ModuleType("agent")
    provider_api = types.ModuleType("agent.memory_provider")
    provider_api.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = provider_api
    modules = {
        "plugins": plugins,
        "plugins.memory": memory,
        "agent": agent,
        "agent.memory_provider": provider_api,
    }
    with mock.patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            f"training_capture_hermes_{uuid.uuid4().hex}",
            ROOT / "hermes-plugin" / "__init__.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


class HermesTrainingCaptureTests(unittest.TestCase):
    def test_sync_turn_starts_governed_partial_without_asserting_outcome(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []

            def capture(action, payload):
                calls.append((action, payload))
                return {"capture_id": "capture-hermes", "state": "partial"}

            with mock.patch.object(plugin, "_run_training_capture", side_effect=capture):
                self.assertIsNone(provider.sync_turn(
                    "prompt text", "assistant text", task_id="task-hermes"
                ))

            self.assertEqual(len(calls), 1)
            action, payload = calls[0]
            self.assertEqual(action, "start")
            self.assertEqual(payload["host"], "hermes")
            self.assertEqual(payload["session_id"], "session-hermes")
            self.assertEqual(payload["host_task_id"], "task-hermes")
            self.assertEqual(payload["prompt"], "prompt text")
            self.assertEqual(payload["assistant_response"], "assistant text")

    def test_post_tool_callback_is_observation_only_and_preserves_empty_response(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []
            with mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))), \
                 mock.patch.object(plugin, "_enqueue_native_evidence"):
                self.assertEqual(provider.post_tool_call(
                    session_id="session-hermes", task_id="task-hermes",
                    tool_name="read_file", result="ok",
                ), {})

            self.assertEqual(len(calls), 1)
            action, payload = calls[0]
            self.assertEqual(action, "observe")
            self.assertEqual(payload["observation_kind"], "post_tool_call")
            self.assertEqual(payload["host_task_id"], "task-hermes")
            self.assertEqual(payload["observation"]["result"], "ok")

    def test_prefetch_snapshots_exact_store_rendering_and_ops(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []
            envelope = {
                "rendered": "<<<ZMEM_UNTRUSTED_FENCE>>>context<<<END>>>",
                "effective_ops": ["zmem_search"],
                "transform_version": "v2",
            }
            store_result = {"ok": True, "stdout": json.dumps(envelope)}
            with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "0", "ZMEM_INJECT": "1"}, clear=False), \
                 mock.patch.object(plugin, "_run_passive_store", return_value=store_result), \
                 mock.patch.object(plugin, "_run_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload)) or {}):
                self.assertEqual(provider.prefetch("prompt", session_id="session-hermes"),
                                 envelope["rendered"])

            self.assertEqual(len(calls), 1)
            action, payload = calls[0]
            self.assertEqual(action, "snapshot")
            self.assertEqual(payload["rendered"], envelope["rendered"])
            self.assertEqual(payload["effective_ops"], envelope["effective_ops"])
            self.assertEqual(payload["transform_version"], "v2")

    def test_capture_marker_is_scoped_to_the_store_child(self):
        with _plugin_context() as plugin:
            completed = types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
            with mock.patch.object(plugin, "_resolve_store_py", return_value=Path("store.py")), \
                 mock.patch.object(plugin, "_python_bin", return_value="python"), \
                 mock.patch.object(plugin.subprocess, "run", return_value=completed) as run:
                plugin._run_passive_store(["recent"], capture=True)
                child_env = run.call_args.kwargs["env"]
                self.assertEqual(child_env["ZMEM_CAPTURE"], "1")
                self.assertNotIn("ZMEM_CAPTURE", os.environ)


if __name__ == "__main__":
    unittest.main()
