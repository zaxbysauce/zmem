"""Independent AC11 red checks for the native Hermes memory-provider seam.

The callback shape is pinned to the verified upstream Hermes observer
artifact (``model_tools.py``): ``post_tool_call`` dispatches keyword
arguments, and the memory plugin context exposes ``register_hook``. These
tests intentionally avoid depending on a private zmem callback name.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE_PY = ROOT / "skills" / "memory" / "scripts" / "store.py"


@contextmanager
def _plugin_context(active: str):
    """Load a fresh plugin while runtime API stubs remain installed."""
    plugins = types.ModuleType("plugins")
    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: active
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
            f"issue183_native_{uuid.uuid4().hex}", ROOT / "hermes-plugin" / "__init__.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
        "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST", "ZMEM_QUERY_CONTEXT",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(ROOT),
        "ZMEM_NAMESPACE": "project:native-183",
        "ZMEM_HOST": "hermes-provider",
        "ZMEM_QUERY_CONTEXT": "1",
        "PYTHONUTF8": "1",
    })
    return env


class NativeHermesAcceptance(unittest.TestCase):
    def test_ac11_registration_is_active_only_and_fail_open(self):
        """Provider registration survives non-zmem and reduced SDK contexts."""
        class Context:
            def __init__(self, with_hooks=True):
                self.provider = None
                self.hooks = {}
                if with_hooks:
                    self.register_hook = self._register_hook

            def register_memory_provider(self, provider):
                self.provider = provider

            def _register_hook(self, name, callback):
                self.hooks[name] = callback

        active = Context()
        with _plugin_context("zmem") as plugin:
            plugin.register(active)
            self.assertIsNotNone(active.provider)
            self.assertIn("post_tool_call", active.hooks)
            self.assertTrue(callable(active.hooks["post_tool_call"]))

        other = Context()
        with _plugin_context("other") as plugin:
            plugin.register(other)
            self.assertIsNotNone(other.provider)
            self.assertNotIn("post_tool_call", other.hooks)

        absent = Context(with_hooks=False)
        # An older Hermes SDK has no collector.register_hook; provider loading
        # must remain usable and must not make startup fail.
        try:
            with _plugin_context("zmem") as plugin:
                plugin.register(absent)
        except Exception as exc:  # explicit failure makes this red, not skipped
            self.fail(f"register(ctx) is not fail-open without register_hook: {exc}")
        self.assertIsNotNone(absent.provider)

    def test_ac11_edit_callback_accepts_real_keywords_and_feeds_rewrite(self):
        """A successful write-file observation is an edit evidence row."""
        with tempfile.TemporaryDirectory(prefix="zmem-183-native-") as raw:
            tmp = Path(raw)
            with mock.patch.dict(os.environ, _env(tmp), clear=True):
                ctx = type("Context", (), {})()
                ctx.hooks = {}
                ctx.provider = None
                ctx.register_memory_provider = lambda provider: setattr(ctx, "provider", provider)
                ctx.register_hook = lambda name, callback: ctx.hooks.__setitem__(name, callback)
                with _plugin_context("zmem") as plugin:
                    plugin.register(ctx)
                    callback = ctx.hooks["post_tool_call"]

                    calls = []
                    writer_called = threading.Event()

                    def fake_store(args, *, input_text=None, **kwargs):
                        calls.append((list(args), input_text, dict(kwargs)))
                        writer_called.set()
                        return {"ok": True, "stdout": "{}\n", "stderr": "", "returncode": 0}

                    # The current provider already owns the subprocess seam; a
                    # future callback may schedule an equivalent bounded writer.
                    # Keep the public seam patched until its worker runs.
                    self.assertTrue(callable(getattr(plugin, "_run_store", None)))
                    with mock.patch.object(plugin, "_run_store", side_effect=fake_store):
                        callback(
                            tool_name="write_file",
                            args={"path": "src/actual.py", "content": "edited"},
                            result="ok",
                            task_id="task-183",
                            session_id="session-183",
                            tool_call_id="call-183",
                            turn_id="turn-183",
                            api_request_id="api-183",
                            duration_ms=7,
                            status="ok",
                            error_type=None,
                            error_message=None,
                            middleware_trace=[],
                        )
                        self.assertTrue(writer_called.wait(5), "post_tool_call writer did not run")

                self.assertTrue(calls, "post_tool_call did not invoke a store writer seam")
                payload_text = next((text for _args, text, _kw in calls if text), None)
                self.assertIsNotNone(payload_text)
                evidence = json.loads(payload_text)
                self.assertEqual(evidence["kind"], "edit")
                self.assertEqual(evidence["ref_path"], "src/actual.py")
                self.assertEqual(evidence["session_id"], "session-183")
                self.assertEqual(evidence["lane"], "hermes-provider")

                # Prove the emitted payload is consumable by the real isolated
                # store boundary and supplies the basename used by rewrite.
                init = subprocess.run([sys.executable, str(STORE_PY), "init"], env=_env(tmp), capture_output=True, text=True, timeout=30)
                self.assertEqual(init.returncode, 0, init.stderr)
                write = subprocess.run([sys.executable, str(STORE_PY), "evidence", "write"], input=json.dumps(evidence), env=_env(tmp), capture_output=True, text=True, timeout=30)
                self.assertEqual(write.returncode, 0, write.stderr)
                rewrite = subprocess.run([sys.executable, str(STORE_PY), "query-rewrite", "--prompt", "continue", "--session-id", "session-183", "--namespace", "project:native-183", "--json"], env=_env(tmp), capture_output=True, text=True, timeout=30)
                self.assertEqual(rewrite.returncode, 0, rewrite.stderr)
                self.assertIn("actual.py", json.loads(rewrite.stdout)["query"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
