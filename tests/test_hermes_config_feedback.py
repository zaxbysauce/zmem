"""Issue #161 feedback-round coverage (implementation-review round 1).

These assertions are deliberately OUTSIDE the checkpoint-frozen suite
(tests/test_hermes_config.py is byte-locked by the published anchor): the
Phase-4.5 reviewer mandated warm-sequence, token-reach, and precedence
pins that the frozen file cannot carry.  Run:
``python -m unittest tests/test_hermes_config_feedback.py``.

Same isolation discipline as the frozen suite: module-top env pin, mocked
``_run_store``, no ~/.zmem access.
"""

import atexit  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_PLUGIN_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"

_PIN_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD")
_prior_env = {k: os.environ.get(k) for k in _PIN_KEYS}
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-161-fb-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_MODE_ENV_KEYS = ("ZMEM_HERMES_MODE", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
                  "ZMEM_MCP_TOKEN_FILE", "ZMEM_HERMES_DEADLINE_S", "ZMEM_HOME",
                  "ZMEM_NAMESPACE")


def _restore_pin():
    for key, value in _prior_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(str(_SCRATCH), ignore_errors=True)


atexit.register(_restore_pin)

_provider_seq = 0


def _load_provider():
    global _provider_seq
    _provider_seq += 1
    name = f"zmem_hermes_161_fb_{_provider_seq}"
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(name, HERMES_PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _persisted_bytes(values):
    persisted = {k: v for k, v in values.items() if k != "token_file"}
    return (
        json.dumps(persisted, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
    ).encode("utf-8")


class WarmFailClosedTest(unittest.TestCase):
    """Reviewer round-1 Required Revision 1: the fail-closed postconditions
    hold on a WARM re-initialize (good config -> corrupt file -> re-init),
    not just on fresh providers."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _fresh_hermes_home(self) -> Path:
        home = Path(tempfile.mkdtemp(prefix="zmem-161-fb-home-"))
        self.addCleanup(shutil.rmtree, str(home), ignore_errors=True)
        return home

    def test_warm_reinit_after_corruption_fails_closed(self):
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        home = self._fresh_hermes_home()
        provider.save_config(
            {"mode": "mcp", "url": "http://127.0.0.1:9/mcp",
             "namespace_policy": "fixed",
             "fixed_namespace": "project:github.com/o/r",
             "auto_retain": True},
            str(home))
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s1", hermes_home=str(home))
        self.assertIs(provider._initialized, True)
        self.assertEqual(provider._namespace, "project:github.com/o/r")
        self.assertIs(provider.auto_retain_enabled(), True)

        # Corrupt the file behind initialize's back, then re-initialize:
        # the provider must drop to schema defaults and become
        # uninitialized -- exactly what the warning text and CHANGELOG
        # promise, on the warm path too.  The previously selected transport
        # is retained (fail-closed skips re-selection; it does not tear
        # down the running session's transport).
        transport_before = provider._transport
        config_path = home / "zmem" / "config.json"
        config_path.write_text("{not json any more", encoding="utf-8")
        with mock.patch.object(mod, "_run_store") as run_store:
            with self.assertLogs(mod.logger, level="WARNING") as logged:
                provider.initialize(session_id="s2", hermes_home=str(home))
            self.assertEqual(len(logged.records), 1)
            self.assertEqual(run_store.call_count, 0)
        self.assertIs(provider._initialized, False)
        self.assertIs(provider.auto_retain_enabled(), False)
        self.assertIs(provider._transport, transport_before)
        # The running session binding is deliberately retained: fail-closed
        # returns before the turn-epoch rotation, so no NEW session is bound
        # (plan-sanctioned consequence; the host sees uninitialized and must
        # re-initialize once the config is fixed).
        self.assertEqual(provider._namespace, "project:github.com/o/r")
        self.assertEqual(provider._session_id, "s1")
        defaults = provider._normalize_config({})
        self.assertEqual(provider._config, defaults)


class TokenFileReachTest(unittest.TestCase):
    """Reviewer round-1 Required Revision 4: an assertion that can actually
    fail if token_file is ever wired into transport construction."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_token_file_never_reaches_transport_nor_overlay(self):
        mod = _load_provider()
        home = Path(tempfile.mkdtemp(prefix="zmem-161-fb-token-"))
        self.addCleanup(shutil.rmtree, str(home), ignore_errors=True)
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        provider = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s", hermes_home=str(home),
                                token_file="Z:/nonexistent/token.txt")
        # The seam key mapping must not carry token_file at all.
        self.assertNotIn("token_file", mod._CONFIG_ENV_KEYS)
        # The constructed MCP transport must hold no explicit token: the
        # #160 McpHttp constructor stores it as _token (transport.py:355) —
        # an assertion that fails if token_file is ever wired into
        # construction.
        self.assertIsNone(getattr(provider._transport, "_token", None))
        # And the overlay built for selection must not carry the env key
        # from config: recompute and inspect.
        overlay = provider._config_overlay(provider._config,
                                           provider._config_explicit)
        self.assertEqual(overlay.get("ZMEM_MCP_TOKEN_FILE"),
                         os.environ.get("ZMEM_MCP_TOKEN_FILE"))


class NamespacePrecedencePinTest(unittest.TestCase):
    """Reviewer round-1 finding: env ZMEM_NAMESPACE beats
    namespace_policy=fixed was implemented but never pinned."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_namespace_beats_fixed_policy(self):
        home = Path(tempfile.mkdtemp(prefix="zmem-161-fb-ns-"))
        self.addCleanup(shutil.rmtree, str(home), ignore_errors=True)
        mod = _load_provider()
        mod.ZmemMemoryProvider().save_config(
            {"mode": "local", "namespace_policy": "fixed",
             "fixed_namespace": "project:github.com/o/r"},
            str(home))
        provider = mod.ZmemMemoryProvider()
        os.environ["ZMEM_NAMESPACE"] = "user:someone-else"
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s", hermes_home=str(home))
        self.assertEqual(provider._namespace, "user:someone-else")

    def test_invalid_schema_kwarg_raises_valueerror(self):
        # Recorded contract: initialize merges schema-key kwargs and
        # normalizes, so an invalid value raises ValueError the same way
        # save_config does (previously unknown kwargs were ignored).
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        home = Path(tempfile.mkdtemp(prefix="zmem-161-fb-kwarg-"))
        self.addCleanup(shutil.rmtree, str(home), ignore_errors=True)
        with self.assertRaises(ValueError):
            provider.initialize(session_id="s", hermes_home=str(home),
                                deadline_s=-3.0)


if __name__ == "__main__":
    unittest.main()
