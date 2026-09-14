"""Query-aware passive prefetch tests (issue #159, Workstream H-2).

Freezes the prefetch contract across the three delivery surfaces and the
selector's passive-state guarantees:

- ``PrefetchParityTest`` — the CLI ``prefetch`` subcommand, the MCP
  ``prefetch`` tool (invoked through the real FastMCP ToolManager, no live
  server), and the Hermes provider's ``prefetch`` all render BYTE-IDENTICAL
  fences for the same deterministic inputs, matching the frozen fixture
  ``tests/fixtures/prefetch/expected-envelope.json`` by SHA-256.
- ``PrefetchStateTest`` — prefetch never bumps ``retrieval_count`` /
  ``last_retrieved`` (passive recall; only ``surfaced_count`` /
  ``last_surfaced`` and the delivery ledger may advance).
- ``PrefetchLedgerTest`` — a session whose delivery ledger already holds the
  two candidate rows gets the silent ``already-delivered`` turn.
- ``PrefetchValidationTest`` — bogus lanes are refused on both the CLI
  (argparse exit 2) and MCP surfaces without a store subprocess, an omitted
  lane never leaks ``--lane`` into the store argv, and a scoped token is
  refused the exact namespace-guard object.

Env discipline (every store-touching class): a fresh GUID scratch dir per
class pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR/ZMEM_MODEL_AUTODOWNLOAD
BEFORE any storelib import or store.py subprocess — never ~/.zmem — plus the
determinism seams ZMEM_TEST_NOW/PYTHONIOENCODING, and strips every
prefetch-affecting ambient override (kill switch, budget, ledger window,
namespace, MCP token config).  tearDownClass rmtree's the scratch and
restores the saved environment.

Runs standalone: python tests/test_passive_prefetch.py
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "prefetch"

# Same constants the fixture generator pins (tests/fixtures/prefetch/
# generate.py is imported for the store build so the test store and the
# frozen expected envelope are byte-equivalent by construction).
NAMESPACE = "project:parity"
QUERY = "stash pop"
MOMENT = "user_prompt"
SESSION_ID = "e0000000-0000-4000-8000-000000000001"
ROW_ONE = "e0000000-0000-4000-8000-000000000001"
ROW_TWO = "e0000000-0000-4000-8000-000000000002"
PIN_TS = "2026-06-01T00:00:00Z"
# 2026-06-01T00:00:00Z as epoch seconds — the seeded ledger's fixed ts.
SEEDED_TS = 1780272000

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

_PIN_ENV_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_NOW", "PYTHONIOENCODING",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S",
    "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
)


def _pin_class_env(cls, prefix: str) -> str:
    """Fresh GUID scratch + env pin; returns the scratch dir path.

    Sets the four isolation vars before any storelib import / store.py
    subprocess can observe the environment, mirrors
    McpSessionToolsTest.setUpClass and ProviderEnvelopeTest.setUpClass.
    """
    cls._tmp = tempfile.mkdtemp(
        prefix=f"zmem-prefetch-{prefix}-{uuid.uuid4().hex}-")
    cls._saved_env = {k: os.environ.get(k) for k in _PIN_ENV_KEYS}
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(cls._tmp, "store.sqlite"),
        "ZMEM_DATA": cls._tmp,
        "ZMEM_MODELS_DIR": os.path.join(cls._tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_TEST_NOW": PIN_TS,
        "PYTHONIOENCODING": "utf-8",
        # Bare env token = unscoped operator token (test_mcp_auth convention)
        # so the MCP legs run the store path rather than the scope guard.
        "ZMEM_MCP_TOKEN": "prefetch-test-token",
    })
    for key in ("ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
                "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE",
                "ZMEM_MCP_TOKEN_FILE"):
        os.environ.pop(key, None)
    return cls._tmp


def _restore_class_env(cls) -> None:
    shutil.rmtree(cls._tmp, ignore_errors=True)
    for key, value in cls._saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _store_env() -> dict:
    """Child env for store.py subprocesses (inherits the class pin)."""
    return dict(os.environ)


def _run_store(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STORE_PY), *args],
        env=_store_env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _ledger_path(data_dir: str, session_id: str) -> Path:
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return Path(data_dir) / "ops" / (name + ".ledger")


def _reset_ledger(data_dir: str, session_id: str) -> None:
    """Reset the session's delivery ledger to a fresh empty copy."""
    path = _ledger_path(data_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURE_DIR / "parity-ledger.json").read_bytes())


def _load_generator():
    """Importlib-load the fixture generator (its build is the spec'd store)."""
    spec = importlib.util.spec_from_file_location(
        "zmem_prefetch_fixture_gen", FIXTURE_DIR / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["zmem_prefetch_fixture_gen"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_mcp_server(module_name: str):
    """Importlib-load hermes-plugin/server/mcp_server.py standalone."""
    spec = importlib.util.spec_from_file_location(
        module_name, SERVER_DIR / "mcp_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_provider(module_name: str):
    """Load hermes-plugin/__init__.py with the agent ABC stubbed."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (HermesSessionToolsTest pattern)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class PrefetchParityTest(unittest.TestCase):
    """CLI prefetch == MCP prefetch tool == Hermes provider prefetch.

    All three surfaces must hand the agent the byte-identical store-rendered
    fence for the same deterministic inputs, and that fence must equal the
    frozen fixture envelope's ``rendered`` (tests/fixtures/prefetch/
    expected-envelope.json, regenerated by the fixture generator).
    """

    @classmethod
    def setUpClass(cls):
        _pin_class_env(cls, "parity")
        # The provider leg resolves its recall namespace from ZMEM_NAMESPACE
        # at initialize() time (ProviderEnvelopeTest convention).
        os.environ["ZMEM_NAMESPACE"] = NAMESPACE
        cls.store_path = os.path.join(cls._tmp, "store.sqlite")

        gen = _load_generator()
        gen.build_parity_store(cls.store_path, gen.isolation_env(cls.store_path))

        cls.fixture = json.loads(
            (FIXTURE_DIR / "expected-envelope.json").read_text(encoding="utf-8"))

        if MCP_AVAILABLE:
            cls.mcp_server = _load_mcp_server("zmem_mcp_prefetch_server")
            cls.server = cls.mcp_server.build_server(
                host="127.0.0.1", port=0, use_tls=False)

        cls.provider_mod = _load_provider("zmem_prefetch_parity_provider")
        cls.provider = cls.provider_mod.ZmemMemoryProvider()
        cls.provider.initialize(SESSION_ID)

    @classmethod
    def tearDownClass(cls):
        _restore_class_env(cls)
        sys.modules.pop("zmem_prefetch_parity_provider", None)
        sys.modules.pop("zmem_mcp_prefetch_server", None)
        sys.modules.pop("zmem_prefetch_fixture_gen", None)

    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_hook_cli_mcp_rendered_sha_match(self):
        data_dir = str(Path(self.store_path).parent)

        # Leg 1 — CLI: fresh empty ledger, one prefetch subprocess.
        _reset_ledger(data_dir, SESSION_ID)
        r = _run_store("prefetch", "--query", QUERY, "--namespace", NAMESPACE,
                       "--session-id", SESSION_ID, "--moment", MOMENT)
        self.assertEqual(r.returncode, 0, r.stderr)
        cli_env = json.loads(r.stdout)
        cli_rendered = cli_env["rendered"]
        self.assertGreaterEqual(cli_env["count"], 1, cli_env)

        # Leg 2 — MCP tool via the real ToolManager (no live server): the
        # ledger is reset so the selector re-delivers instead of deduping.
        mcp_rendered = None
        if MCP_AVAILABLE:
            _reset_ledger(data_dir, SESSION_ID)
            result = asyncio.run(self.server._tool_manager.call_tool(
                "prefetch",
                {"query": QUERY, "namespace": NAMESPACE,
                 "session_id": SESSION_ID, "moment": MOMENT},
                context=None))
            self.assertNotIn("error", result, result)
            self.assertEqual(result.get("context"), result.get("rendered"))
            mcp_rendered = result["rendered"]

        # Leg 3 — Hermes provider surface (store.py recall selector dispatch).
        _reset_ledger(data_dir, SESSION_ID)
        provider_rendered = self.provider.prefetch(QUERY, session_id=SESSION_ID)

        expected_rendered = self.fixture["rendered"]
        self.assertTrue(expected_rendered, "fixture rendered must be non-empty")
        self.assertTrue(cli_rendered, "CLI rendered must be non-empty")
        self.assertTrue(provider_rendered,
                        "provider rendered must be non-empty")
        self.assertEqual(self._sha(cli_rendered), self._sha(expected_rendered))
        self.assertEqual(self._sha(provider_rendered),
                         self._sha(expected_rendered))
        if mcp_rendered is not None:
            self.assertTrue(mcp_rendered, "MCP rendered must be non-empty")
            self.assertEqual(self._sha(mcp_rendered),
                             self._sha(expected_rendered))


class PrefetchStateTest(unittest.TestCase):
    """Prefetch surfaces a row without bumping its retrieval telemetry."""

    SESSION = "state-probe-session-159"

    @classmethod
    def setUpClass(cls):
        _pin_class_env(cls, "state")
        cls.store_path = os.path.join(cls._tmp, "store.sqlite")
        r = _run_store("init")
        assert r.returncode == 0, r.stderr
        r = _run_store("add", "--namespace", "project:state", "--type", "fact",
                       "--content", "stash pop state probe row one",
                       "--signal", "test", "--confidence", "0.9", "--json")
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        _restore_class_env(cls)

    def _telemetry(self) -> dict:
        conn = sqlite3.connect(self.store_path)
        try:
            rows = conn.execute(
                "SELECT id, retrieval_count, last_retrieved, surfaced_count, "
                "last_surfaced FROM memory").fetchall()
            return {r[0]: r[1:] for r in rows}
        finally:
            conn.close()

    def test_prefetch_does_not_bump_retrieval_count(self):
        before = self._telemetry()
        self.assertEqual(len(before), 1, before)

        _reset_ledger(self._tmp, self.SESSION)
        r = _run_store("prefetch", "--query", "stash pop",
                       "--namespace", "project:state",
                       "--session-id", self.SESSION, "--moment", MOMENT)
        self.assertEqual(r.returncode, 0, r.stderr)
        envelope = json.loads(r.stdout)
        # The row must actually be surfaced, otherwise the no-bump claim is
        # vacuous.
        self.assertGreaterEqual(envelope["count"], 1, envelope)
        self.assertIn("stash pop state probe row one", envelope["rendered"])

        after = self._telemetry()
        self.assertEqual(set(before), set(after))
        for mid, (retr_b, lr_b, _surf_b, _ls_b) in before.items():
            retr_a, lr_a, _surf_a, _ls_a = after[mid]
            self.assertEqual(
                retr_a, retr_b,
                f"prefetch must never bump retrieval_count ({mid})")
            self.assertEqual(
                lr_a, lr_b,
                f"prefetch must never bump last_retrieved ({mid})")
        # surfaced_count/last_surfaced MAY advance — explicitly tolerated.


class PrefetchLedgerTest(unittest.TestCase):
    """A session that already received both rows gets the silent turn."""

    @classmethod
    def setUpClass(cls):
        _pin_class_env(cls, "ledger")
        cls.store_path = os.path.join(cls._tmp, "store.sqlite")
        gen = _load_generator()
        gen.build_parity_store(cls.store_path, gen.isolation_env(cls.store_path))

    @classmethod
    def tearDownClass(cls):
        _restore_class_env(cls)
        sys.modules.pop("zmem_prefetch_fixture_gen", None)

    def test_session_id_excludes_second_turn(self):
        # The isolated two-turn copy: the exact entries the ledger writer
        # itself produces ({"id", "moment", "ts", "text"} — see
        # storelib/delivery_ledger.py record()), with the fixture's fixed
        # epoch ts. The seeded ts is 2026-06-01 while the suppression window
        # defaults to 6h, so the window is widened via ZMEM_DELIVER_WINDOW_S
        # for this leg only (the env knob the ledger module documents).
        entries = [
            {"id": ROW_ONE, "moment": MOMENT, "ts": SEEDED_TS,
             "text": "stash pop recovery note one"},
            {"id": ROW_TWO, "moment": MOMENT, "ts": SEEDED_TS,
             "text": "stash pop recovery note two"},
        ]
        ledger = _ledger_path(self._tmp, SESSION_ID)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps({"entries": entries}), encoding="utf-8", newline="")

        os.environ["ZMEM_DELIVER_WINDOW_S"] = "999999999"
        try:
            r = _run_store("prefetch", "--query", QUERY,
                           "--namespace", NAMESPACE,
                           "--session-id", SESSION_ID, "--moment", MOMENT)
        finally:
            os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        envelope = json.loads(r.stdout)
        self.assertEqual(envelope["reason"], "already-delivered", envelope)
        self.assertEqual(len(envelope["excluded"]), 2, envelope)
        self.assertEqual(set(envelope["excluded"]), {ROW_ONE, ROW_TWO})
        self.assertEqual(envelope["results"], [], envelope)
        self.assertEqual(envelope["rendered"], "", envelope)
        self.assertEqual(envelope["count"], 0, envelope)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed (MCP legs)")
class PrefetchValidationTest(unittest.TestCase):
    """Boundary validation: lane tuple, lane omission, namespace scope."""

    @classmethod
    def setUpClass(cls):
        _pin_class_env(cls, "validation")
        cls.store_path = os.path.join(cls._tmp, "store.sqlite")
        r = _run_store("init")
        assert r.returncode == 0, r.stderr
        cls.mcp_server = _load_mcp_server("zmem_mcp_prefetch_validation")
        cls.server = cls.mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)

    @classmethod
    def tearDownClass(cls):
        _restore_class_env(cls)
        sys.modules.pop("zmem_mcp_prefetch_validation", None)

    def _call(self, **args):
        return asyncio.run(self.server._tool_manager.call_tool(
            "prefetch", args, context=None))

    def _patched_run_store(self, delegate: bool):
        """Swap mcp_server._run_store for a recorder; returns (calls, restore).

        ``_run_store_async`` resolves the module-global ``_run_store`` at
        submit time, so patching the module attribute is observed by the
        tool path. With delegate=True the recorder forwards to the real
        subprocess helper (the tool must still complete).
        """
        calls: list[list[str]] = []
        real = self.mcp_server._run_store

        def recorder(args, input_text=None):
            calls.append(list(args))
            if delegate:
                return real(args, input_text=input_text)
            return {"ok": True, "stdout": "{}", "stderr": "",
                    "returncode": 0}

        self.mcp_server._run_store = recorder

        def restore():
            self.mcp_server._run_store = real

        return calls, restore

    def test_bogus_lane_is_refused(self):
        # CLI leg: argparse owns the five-value lane tuple — exit 2.
        r = _run_store("prefetch", "--query", QUERY, "--namespace", NAMESPACE,
                       "--session-id", SESSION_ID, "--moment", MOMENT,
                       "--lane", "bogus")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("argument --lane: invalid choice", r.stderr)

        # MCP leg: the tool validates the lane BEFORE any store subprocess.
        calls, restore = self._patched_run_store(delegate=False)
        try:
            result = self._call(query=QUERY, namespace=NAMESPACE,
                                session_id=SESSION_ID, moment=MOMENT,
                                lane="bogus")
        finally:
            restore()
        self.assertIn("error", result, result)
        self.assertIn("invalid lane", result["error"], result)
        self.assertEqual(calls, [],
                         "a refused lane must not spawn a store subprocess")

    def test_missing_lane_is_omitted(self):
        # lane=None (the tool default) must stay None: the store argv never
        # carries --lane, and the tool still completes against the store.
        calls, restore = self._patched_run_store(delegate=True)
        try:
            result = self._call(query=QUERY, namespace=NAMESPACE,
                                session_id=SESSION_ID, moment=MOMENT,
                                lane=None)
        finally:
            restore()
        self.assertNotIn("error", result, result)
        self.assertIn("rendered", result, result)
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertEqual(argv[0], "prefetch", argv)
        self.assertNotIn("--lane", argv,
                         "lane=None must be omitted from the store argv")

    def test_scoped_namespace_refusal(self):
        # Scoped file token (test_mcp_auth.ScopedTokenToolSurfaceTest setup):
        # a prefetch for a namespace outside the allow-list returns the exact
        # namespace-guard refusal object and never reaches the store.
        token_file = os.path.join(self._tmp, "scoped-token.json")
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump({"token": "scoped-secret",
                       "namespaces": ["project:x"]}, f)
        saved = {k: os.environ.get(k)
                 for k in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE")}
        os.environ["ZMEM_MCP_TOKEN_FILE"] = token_file
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        try:
            scoped = self.mcp_server.build_server(
                host="127.0.0.1", port=0, use_tls=False)

            async def _scoped_call():
                return await scoped._tool_manager.call_tool(
                    "prefetch",
                    {"query": QUERY, "namespace": "project:y",
                     "session_id": SESSION_ID, "moment": MOMENT},
                    context=None)

            calls, restore = self._patched_run_store(delegate=False)
            try:
                result = asyncio.run(_scoped_call())
            finally:
                restore()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result, {
            "error": "namespace_not_allowed",
            "namespace": "project:y",
            "detail": (
                "this token is scoped; pass one of its allowed namespaces "
                "explicitly (reads without a namespace span every "
                "namespace and are denied for scoped tokens)"
            ),
        }, result)
        self.assertEqual(calls, [],
                         "a scoped-token refusal must not spawn a store "
                         "subprocess")


if __name__ == "__main__":
    unittest.main(verbosity=2)
