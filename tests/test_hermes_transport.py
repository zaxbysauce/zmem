"""Issue #160 — provider transport abstraction tests.

TransportSelectionTest, TokenResolutionTest, DeadlineTest, RecordedCallTest
(the four classes the issue names).  Store tests set ZMEM_STORE/ZMEM_DATA/
ZMEM_MODELS_DIR to fresh scratch paths and ZMEM_MODEL_AUTODOWNLOAD=0 before
any provider import; no test deletes tracked files and no test asserts on
wall-clock time — the fake executor's virtual clock supplies all deadline
evidence.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import textwrap
import types
import unittest
import socket
import contextlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-160-transport-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_TRANSPORT_PATH = REPO_ROOT / "hermes-plugin" / "transport.py"
_SUPPORT_DIR = REPO_ROOT / "tests" / "support"

_MODE_ENV_KEYS = (
    "ZMEM_HERMES_MODE", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_HERMES_DEADLINE_S", "ZMEM_HOME",
)


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "zmem_transport_160", _TRANSPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_transport_160"] = module
    spec.loader.exec_module(module)
    return module


def _load_fake_executor():
    spec = importlib.util.spec_from_file_location(
        "zmem_fake_executor_160", _SUPPORT_DIR / "fake_executor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_fake_executor_160"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider(name="zmem_hermes_160"):
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_session_tools.py)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _clean_mode_env():
    for key in _MODE_ENV_KEYS:
        os.environ.pop(key, None)


class TransportSelectionTest(unittest.TestCase):
    """Configuration-only mode selection and availability."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        _clean_mode_env()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_mcp_url_makes_remote_provider_available(self):
        """AC1: no local store + nonempty URL + socket patched to raise."""
        empty_home = Path(tempfile.mkdtemp(prefix="zmem-160-empty-home-"))
        os.environ["ZMEM_HOME"] = str(empty_home)
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        mod = _load_provider("zmem_hermes_160_ac1")
        mod._resolve_store_py = lambda: None
        provider = mod.ZmemMemoryProvider()
        socket_calls = []

        def _no_socket(*args, **kwargs):
            socket_calls.append(1)
            raise AssertionError("availability must never open a socket")

        saved = (socket.socket, socket.create_connection, socket.getaddrinfo)
        socket.socket = _no_socket
        socket.create_connection = _no_socket
        socket.getaddrinfo = _no_socket
        try:
            available = provider.is_available()
        finally:
            (socket.socket, socket.create_connection,
             socket.getaddrinfo) = saved
        self.assertTrue(available)
        self.assertEqual(socket_calls, [])

    def test_neither_transport_is_unavailable(self):
        empty_home = Path(tempfile.mkdtemp(prefix="zmem-160-none-home-"))
        os.environ["ZMEM_HOME"] = str(empty_home)
        mod = _load_provider("zmem_hermes_160_ac2")
        mod._resolve_store_py = lambda: None
        provider = mod.ZmemMemoryProvider()
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.unavailable_reason(),
                         "mode=none unavailable")

    def test_explicit_mode_precedence(self):
        transport = _load_transport()
        # Explicit local wins over a present URL.
        os.environ["ZMEM_HERMES_MODE"] = "local"
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        mode, reason = transport.resolve_transport_mode()
        self.assertIs(mode, transport.TransportMode.local)

        # Explicit mcp wins over a present local store.
        os.environ["ZMEM_HERMES_MODE"] = "mcp"
        os.environ.pop("ZMEM_MCP_URL", None)
        mode, reason = transport.resolve_transport_mode()
        self.assertIs(mode, transport.TransportMode.mcp)

        # Invalid explicit value never falls back.
        os.environ["ZMEM_HERMES_MODE"] = "carrier-pigeon"
        mode, reason = transport.resolve_transport_mode()
        self.assertIsNone(mode)
        self.assertEqual(reason, "mode=carrier-pigeon invalid")

        # Explicit local without a store never falls back to MCP.
        os.environ["ZMEM_HERMES_MODE"] = "local"
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        empty_home = Path(tempfile.mkdtemp(prefix="zmem-160-local-missing-"))
        os.environ["ZMEM_HOME"] = str(empty_home)
        mode, reason = transport.resolve_transport_mode()
        self.assertIsNone(mode)
        self.assertEqual(reason,
                         "mode=local unavailable: local store missing")

    def test_resolver_matches_provider_home_semantics(self):
        """D3 parity pin: transport._resolve_store_py mirrors the provider's."""
        transport = _load_transport()
        empty_home = Path(tempfile.mkdtemp(prefix="zmem-160-parity-"))
        os.environ["ZMEM_HOME"] = str(empty_home)
        # A set-and-valid ZMEM_HOME short-circuits: no in-tree fallback.
        self.assertIsNone(transport._resolve_store_py())
        os.environ.pop("ZMEM_HOME", None)
        # ZMEM_HOME unset: the in-tree checkout resolves.
        self.assertIsNotNone(transport._resolve_store_py())
        # A ZMEM_HOME that does not name a directory falls through to the
        # in-tree checkout, exactly like the provider's own resolver
        # (implementation-review round 1, HIGH finding).
        os.environ["ZMEM_HOME"] = str(empty_home / "does-not-exist")
        self.assertIsNotNone(transport._resolve_store_py())
        # ~ in ZMEM_HOME expands like the provider's expanduser() call.
        os.environ["ZMEM_HOME"] = "~/zmem-160-no-such-home"
        self.assertIsNotNone(transport._resolve_store_py())


class TokenResolutionTest(unittest.TestCase):
    """AC5/issue Tests: token precedence inside the MCP operation."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        _clean_mode_env()
        self._transport = _load_transport()
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-160-token-"))
        self._seen = []

        async def recording_call(url, token, tool, arguments):
            self._seen.append({"url": url, "token": token,
                               "tool": tool, "arguments": dict(arguments)})
            return _valid_envelope()

        self._recording_call = recording_call

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _prefetch(self, **ctor):
        executor = self._transport.DeadlineExecutor()
        options = {"url": "http://127.0.0.1:9/mcp", "executor": executor,
                   "call_fn": self._recording_call}
        options.update(ctor)
        transport = self._transport.McpHttp(**options)
        return transport.prefetch(
            "stash pop", namespace="project:parity",
            session_id="s", moment="user_prompt", ops_tokens=[])

    def test_env_and_token_file_resolution(self):
        token_file = self._tmp / "token.txt"
        json_file = self._tmp / "token.json"

        # 1. Explicit constructor token wins over everything.
        token_file.write_text("file-token", encoding="utf-8")
        os.environ["ZMEM_MCP_TOKEN_FILE"] = str(token_file)
        os.environ["ZMEM_MCP_TOKEN"] = "env-token"
        envelope = self._prefetch(token="explicit-token")
        self.assertEqual(self._seen[-1]["token"], "explicit-token")
        self.assertEqual(envelope["rendered"], _valid_envelope()["rendered"])

        # 2. Token file (bare) beats the environment token.
        self._prefetch()
        self.assertEqual(self._seen[-1]["token"], "file-token")

        # 3. Environment token when no file.
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        self._prefetch()
        self.assertEqual(self._seen[-1]["token"], "env-token")

        # 4. JSON token file with a nonempty string "token".
        json_file.write_text(json.dumps({"token": "json-token",
                                         "namespaces": ["user:global"]}),
                             encoding="utf-8")
        os.environ["ZMEM_MCP_TOKEN_FILE"] = str(json_file)
        self._prefetch()
        self.assertEqual(self._seen[-1]["token"], "json-token")

        # 5. Malformed JSON token file: one warning + the empty envelope,
        #    and the call never fires (no credentials, no request).
        json_file.write_text("{not json", encoding="utf-8")
        os.environ["ZMEM_MCP_TOKEN_FILE"] = str(json_file)
        calls_before = len(self._seen)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            envelope = self._prefetch()
        self.assertIn("transport: invalid token file", buffer.getvalue())
        self.assertEqual(envelope["reason"], "empty-pool")
        self.assertEqual(envelope["rendered"], "")
        self.assertEqual(len(self._seen), calls_before)

        # 6. All-empty values resolve to an empty token.
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        self._prefetch()
        self.assertEqual(self._seen[-1]["token"], "")


class DeadlineTest(unittest.TestCase):
    """AC4/AC6: fake-clock deadline evidence and ZMEM_HERMES_DEADLINE_S."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        _clean_mode_env()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_fake_executor_returns_empty_at_six_seconds(self):
        fake_executor = _load_fake_executor()
        transport = _load_transport()
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        mod = _load_provider("zmem_hermes_160_deadline")
        executor = fake_executor.FakeExecutor()
        executor.pending_completion = 7.0
        provider = mod.ZmemMemoryProvider()
        provider._transport = transport.LocalSubprocess(
            store_py=str(REPO_ROOT / "skills" / "memory" / "scripts" /
                         "store.py"),
            executor=executor, deadline_s=6.0)
        rendered = provider.prefetch("stash pop", session_id="deadline-sid")
        self.assertEqual(rendered, "")
        self.assertEqual(executor.now(), 6.0)

    def test_cancelled_operation_never_writes_late(self):
        fake_executor = _load_fake_executor()
        executor = fake_executor.FakeExecutor()
        executor.pending_completion = 7.0
        handle_calls = fake_executor.FakeCall(lambda: "late value")
        result = executor.run(handle_calls, 6.0)
        self.assertIsNone(result)
        self.assertEqual(executor.now(), 6.0)
        with self.assertRaises(fake_executor.FakeCall.Cancelled):
            handle_calls()

    def test_deadline_env_resolution(self):
        transport = _load_transport()
        warning = "transport: invalid ZMEM_HERMES_DEADLINE_S; using 6.0"
        for raw in ("banana", "nan", "inf", "0", "-3", "8.0", "100"):
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                value = transport.resolve_deadline_s(
                    {"ZMEM_HERMES_DEADLINE_S": raw})
            self.assertEqual(value, 6.0, raw)
            self.assertEqual(buffer.getvalue().count(warning), 1, raw)
        for raw, expected in (("1.5", 1.5), ("7.9", 7.9)):
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                value = transport.resolve_deadline_s(
                    {"ZMEM_HERMES_DEADLINE_S": raw})
            self.assertEqual(value, expected, raw)
            self.assertNotIn(warning, buffer.getvalue(), raw)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            value = transport.resolve_deadline_s({})
        self.assertEqual(value, 6.0)
        self.assertNotIn(warning, buffer.getvalue())


def _valid_envelope() -> dict:
    return {
        "results": [{"id": "e0000000-0000-4000-8000-000000000001",
                     "type": "fact", "content": "stash pop keeps the stash"}],
        "count": 1,
        "omitted": 0,
        "reason": "ok",
        "excluded": [],
        "candidate_ids": ["e0000000-0000-4000-8000-000000000001"],
        "tokens_used": 42,
        "tokens_budget": 1500,
        "budget_dropped": 0,
        "budget_admission": 1,
        "budget_truncated": 0,
        "budget_dropped_protected": 0,
        "arms": {"hermes-provider": {"surfaced": 1}},
        "rendered": "<<<ZMEM_UNTRUSTED_FENCE>>>\nstash pop keeps the stash",
    }


_EMPTY_EXPECTED = {
    "results": [], "count": 0, "omitted": 0, "reason": "empty-pool",
    "excluded": [], "candidate_ids": [], "tokens_used": 0,
    "tokens_budget": 0, "budget_dropped": 0, "budget_admission": 0,
    "budget_truncated": 0, "budget_dropped_protected": 0, "arms": {},
    "rendered": "",
}

_FAKE_STORE_TEMPLATE = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    argv = sys.argv[1:]
    Path("@RECORD@").write_text(json.dumps(sys.argv), encoding="utf-8")
    mode = "@MODE@"
    if mode == "fail-exit":
        sys.stderr.write("boom")
        sys.exit(3)
    if mode == "empty":
        sys.exit(0)
    if mode == "bad-json":
        print("this is not json at all")
        sys.exit(0)
    if mode == "missing-rendered":
        print(json.dumps({"results": [], "count": 0}))
        sys.exit(0)
    print(json.dumps(@ENVELOPE@, indent=2))
''')


class RecordedCallTest(unittest.TestCase):
    """AC5: lane on every call, the complete envelope, and failure classes."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        _clean_mode_env()
        self._transport = _load_transport()
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-160-recorded-"))
        self._fake_store = self._tmp / "store.py"
        self._record = self._tmp / "argv.json"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_fake_store(self, mode="ok"):
        envelope = _valid_envelope()
        script = (_FAKE_STORE_TEMPLATE
                  .replace("@RECORD@", self._record.as_posix())
                  .replace("@MODE@", mode)
                  .replace("@ENVELOPE@", repr(envelope)))
        self._fake_store.write_text(script, encoding="utf-8")

    def _local(self):
        executor = self._transport.DeadlineExecutor()
        return self._transport.LocalSubprocess(
            store_py=str(self._fake_store), executor=executor,
            deadline_s=6.0)

    def _recorded_argv(self) -> list:
        return json.loads(self._record.read_text(encoding="utf-8"))

    def test_every_transport_call_has_hermes_provider_lane(self):
        # Local: the recorded argv carries the lane and the full flag set.
        self._write_fake_store()
        envelope = self._local().prefetch(
            "stash pop", namespace="project:parity", session_id="s1",
            moment="user_prompt", ops_tokens=["op-one", "op-two"])
        argv = self._recorded_argv()
        self.assertIn("prefetch", argv)
        self.assertEqual(argv[argv.index("--lane") + 1], "hermes-provider")
        self.assertEqual(argv[argv.index("--namespace") + 1], "project:parity")
        self.assertEqual(argv[argv.index("--session-id") + 1], "s1")
        self.assertEqual(argv[argv.index("--moment") + 1], "user_prompt")
        self.assertEqual(argv.count("--ops-token"), 2)
        self.assertEqual(argv[argv.index("--ops-token") + 1], "op-one")
        # The transport returns the COMPLETE envelope dict.
        self.assertEqual(envelope, _valid_envelope())

        # A leading-dash query rides the --query= form (dash-safe argv).
        self._local().prefetch("--dangerous", namespace="project:parity",
                               session_id="s1", moment="user_prompt",
                               ops_tokens=[])
        argv = self._recorded_argv()
        self.assertTrue(
            any(item.startswith("--query=--dangerous") for item in argv))

        # MCP: the call_fn arguments carry the lane.
        seen = []

        async def recording_call(url, token, tool, arguments):
            seen.append((tool, dict(arguments)))
            return _valid_envelope()

        executor = self._transport.DeadlineExecutor()
        mcp = self._transport.McpHttp(
            url="http://127.0.0.1:9/mcp", executor=executor,
            call_fn=recording_call)
        envelope = mcp.prefetch(
            "stash pop", namespace="project:parity", session_id="s2",
            moment="user_prompt", ops_tokens=[])
        tool, arguments = seen[-1]
        self.assertEqual(tool, "prefetch")
        self.assertEqual(arguments["lane"], "hermes-provider")
        self.assertEqual(arguments["ops_tokens"], [])
        self.assertEqual(envelope, _valid_envelope())

    def test_failure_classes_return_the_empty_envelope(self):
        for mode in ("fail-exit", "empty", "bad-json", "missing-rendered"):
            with self.subTest(mode=mode):
                self._write_fake_store(mode=mode)
                envelope = self._local().prefetch(
                    "stash pop", namespace="project:parity", session_id="s1",
                    moment="user_prompt", ops_tokens=[])
                self.assertEqual(envelope, _EMPTY_EXPECTED, mode)

    def test_missing_store_py_fails_open(self):
        executor = self._transport.DeadlineExecutor()
        broken = self._transport.LocalSubprocess(
            store_py=str(self._tmp / "does-not-exist.py"),
            executor=executor, deadline_s=6.0)
        envelope = broken.prefetch(
            "stash pop", namespace="project:parity", session_id="s1",
            moment="user_prompt", ops_tokens=[])
        self.assertEqual(envelope, _EMPTY_EXPECTED)

    def test_no_storelib_or_ledger_access_in_transport(self):
        """Guardrail: the transport boundary never touches store state."""
        for rel in ("hermes-plugin/transport.py",
                    "hermes-plugin/__init__.py"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for token in ("import storelib", "from storelib", "sqlite3",
                          "delivery_ledger", "_load_inject",
                          "_load_ops_tokens", "_OPS_TOKENS",
                          "_fence_renderer", "_local_fenced_recall"):
                self.assertNotIn(token, text, rel + " must not contain "
                                 + token)
        lane_grammar = (REPO_ROOT / "skills" / "memory" / "scripts" /
                        "schema_meta.py").read_text(encoding="utf-8")
        self.assertIn("hermes-provider", lane_grammar)


if __name__ == "__main__":
    unittest.main(verbosity=2)
