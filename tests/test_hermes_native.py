"""Native Hermes integration suite (issue #165, Workstream H PR 8 of 8).

Drives three merged layers together through their real contracts:

* the source-pinned Hermes manager fixture
  (``tests/fixtures/hermes/vendor/cdf4c76/agent/memory_manager.py``) with its
  declared test-only ``_deadline_executor`` seam,
* the real zmem plugin (``hermes-plugin/__init__.py``) through the provider
  discovery path ``register(ctx)`` and the ``plugins.memory`` mode gate, and
* the real store CLI (``store.py``) ``recall --for-injection`` envelope, the
  delivery-ledger two-turn dedup, and the flagged ``evidence write`` path.

Guarantees (asserted per test via the resource guards): zero real threads,
zero real joins, zero real sockets, zero model downloads.  Every subprocess
this suite spawns uses the single-pipe + stderr-file shape that CPython runs
without reader threads, so the thread counter can be strict.  Compatibility
hooks are never executed in provider mode.

Tier: test-only (no production code is imported for mutation; the vendored
fixtures are read-only inputs).
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import Future
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_AGENT_DIR = (REPO_ROOT / "tests" / "fixtures" / "hermes" / "vendor"
                    / "cdf4c76" / "agent")
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hermes"
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
REFLECT_HOOK = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py"

# Module-top store pinning BEFORE any storelib/plugin import (issue #61
# hazard: modules freeze env-derived paths at first import).
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-165-native-"))
atexit.register(shutil.rmtree, str(_SCRATCH), ignore_errors=True)
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_MODE_ENV_KEYS = (
    "ZMEM_HOME", "ZMEM_MCP_URL", "ZMEM_MCP_NAMESPACE", "ZMEM_NAMESPACE",
    "ZMEM_QUERY_CONTEXT", "ZMEM_BACKUP_DIR", "ZMEM_INJECT", "ZMEM_CORE_MD",
    # Operator-shell hermeticity (PR review PRR-001): these are read live
    # from os.environ by the cached plugin module and the store children at
    # call time, so ambient values silently change test outcomes. setUp
    # pops every key in-process (after snapshotting for restore) and
    # _child_env strips them from child envs.
    "ZMEM_CAPTURE", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_CAPTURE_MODE",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
)

with open(FIXTURE_DIR / "native_cases.json", encoding="utf-8") as _handle:
    CASES = json.load(_handle)
with open(FIXTURE_DIR / "native_cases.expected.json",
          encoding="utf-8") as _handle:
    EXPECTED = json.load(_handle)

_NAMESPACE = CASES["namespace"]
_PROMPT_NONTRIVIAL = CASES["prompts"]["nontrivial"]
_PROMPT_TRIVIAL = CASES["prompts"]["trivial"]
_SESSION_PRIMARY = CASES["sessions"]["primary"]
_SESSION_SECONDARY = CASES["sessions"]["secondary"]
_CHILD_SID = CASES["delegation"]["child_session_id"]
_PARENT_SID = CASES["delegation"]["parent_session_id"]
_ROW_IDS = sorted(row["id"] for row in CASES["rows"])
_TIMESTAMP = CASES["timestamp"]
_TIER0 = CASES["tier0_core_md"]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_module_under(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_VENDORED_HEADER = (b"# Vendored from hermes-agent cdf4c76; update only "
                    b"with an explicit source-pin change.")


def _assert_vendored_headers() -> None:
    """The source-pinned header is asserted byte-for-byte in both vendored
    files (issue Tests section): any mismatch fails the suite."""
    for name in ("memory_provider.py", "memory_manager.py"):
        path = VENDOR_AGENT_DIR / name
        with open(path, "rb") as handle:
            first = handle.readline()
        first = first[:-1] if first.endswith(b"\n") else first
        if first != _VENDORED_HEADER:
            raise AssertionError(
                "vendored %s header mismatch: %r != %r"
                % (name, first, _VENDORED_HEADER))


def _install_vendored_stubs(scratch: str) -> None:
    """sys.modules stubs for the pinned manager's non-vendored imports."""
    agent = types.ModuleType("agent")
    sys.modules["agent"] = agent
    provider = _load_module_under("agent.memory_provider",
                                  VENDOR_AGENT_DIR / "memory_provider.py")
    agent.memory_provider = provider

    skill_commands = types.ModuleType("agent.skill_commands")
    skill_commands.extract_user_instruction_from_skill_message = \
        lambda message: message
    agent.skill_commands = skill_commands
    sys.modules["agent.skill_commands"] = skill_commands

    tools = types.ModuleType("tools")
    sys.modules["tools"] = tools
    spill = types.ModuleType("tools.hook_output_spill")
    spill.get_spill_config = lambda *args, **kwargs: None
    spill.spill_if_oversized = lambda text, *args, **kwargs: text
    tools.hook_output_spill = spill
    sys.modules["tools.hook_output_spill"] = spill
    registry = types.ModuleType("tools.registry")
    registry.tool_error = lambda message: message
    tools.registry = registry
    sys.modules["tools.registry"] = registry

    toolsets = types.ModuleType("toolsets")
    toolsets._HERMES_CORE_TOOLS = frozenset()
    toolsets.resolve_toolset = lambda name: set()
    sys.modules["toolsets"] = toolsets

    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: scratch
    sys.modules["hermes_constants"] = hermes_constants


_VENDORED_MANAGER = None


def _vendored_manager_module():
    global _VENDORED_MANAGER
    if _VENDORED_MANAGER is None:
        _install_vendored_stubs(str(_SCRATCH))
        _VENDORED_MANAGER = _load_module_under(
            "zmem_165_vendored_manager",
            VENDOR_AGENT_DIR / "memory_manager.py")
    return _VENDORED_MANAGER


_PLUGIN_MODULE = None
_PLUGINS_MEMORY_STATE = {"active": ""}


def _plugin_module():
    """Load hermes-plugin/__init__.py with the Hermes ABC stubbed and a
    controllable plugins.memory gate (provider vs compatibility mode)."""
    global _PLUGIN_MODULE
    if _PLUGIN_MODULE is not None:
        return _PLUGIN_MODULE

    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_hermes_hooks.py)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)

    plugins = types.ModuleType("plugins")
    plugins_memory = types.ModuleType("plugins.memory")

    def _get_active_memory_provider():
        return _PLUGINS_MEMORY_STATE["active"]

    plugins_memory._get_active_memory_provider = _get_active_memory_provider
    plugins.memory = plugins_memory
    sys.modules["plugins"] = plugins
    sys.modules["plugins.memory"] = plugins_memory

    _PLUGIN_MODULE = _load_module_under(
        "zmem_hermes_165_native", REPO_ROOT / "hermes-plugin" / "__init__.py")
    return _PLUGIN_MODULE


_FAKE_EXECUTOR_MODULE = None


def _fake_executor_module():
    global _FAKE_EXECUTOR_MODULE
    if _FAKE_EXECUTOR_MODULE is None:
        _FAKE_EXECUTOR_MODULE = _load_module_under(
            "zmem_165_fake_executor",
            REPO_ROOT / "tests" / "support" / "fake_executor.py")
    return _FAKE_EXECUTOR_MODULE


# ---------------------------------------------------------------------------
# Resource guards (thread / join / socket / model-download counters)
# ---------------------------------------------------------------------------

class _ResourceGuards:
    """In-process counters for APPLICATION-level concurrency and I/O: every
    threading.Thread construction, every Thread.join call, and every
    socket.socket construction count — EXCEPT CPython's own subprocess reader
    threads (``Popen._readerthread``, module ``subprocess``), which Windows
    starts for each piped stream regardless of timeout.  Those are
    interpreter plumbing present in any repo helper that captures CLI output;
    the zero counters this suite asserts are about the Hermes machinery the
    deadline seam replaces: the vendored manager's prefetch thread and the
    transport's DeadlineExecutor worker.  All suite subprocesses use the
    single-pipe + stderr-file shape so at most the one plumbing reader is
    ever seen, and it is excluded by identity, not by disabling the guard."""

    def __init__(self):
        self.thread_count = 0
        self.join_count = 0
        self.socket_count = 0
        self._model_dirs = []
        module_models_dir = os.environ.get("ZMEM_MODELS_DIR")
        if module_models_dir:
            self._model_dirs.append(Path(module_models_dir))
        self._real_thread = threading.Thread
        self._real_join = threading.Thread.join
        self._real_socket = None

    def add_model_dir(self, path) -> None:
        """Register an additional per-test models dir to police (PR review
        PRR-007: children get a per-store dir, not the module-level one)."""
        self._model_dirs.append(Path(path))

    @staticmethod
    def _is_plumbing(kwargs) -> bool:
        target = kwargs.get("target")
        return getattr(target, "__module__", "") == "subprocess"

    def __enter__(self):
        guards = self

        class _CountingThread(self._real_thread):
            _app_counted = False

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if not guards._is_plumbing(kwargs):
                    self._app_counted = True
                    guards.thread_count += 1

        def _counting_join(inner_self, *args, **kwargs):
            if getattr(inner_self, "_app_counted", False):
                guards.join_count += 1
            return guards._real_join(inner_self, *args, **kwargs)

        threading.Thread = _CountingThread
        threading.Thread.join = _counting_join

        import socket as _socket
        self._real_socket = _socket.socket

        class _CountingSocket(self._real_socket):
            def __init__(self, *args, **kwargs):
                guards.socket_count += 1
                super().__init__(*args, **kwargs)

        _socket.socket = _CountingSocket
        return self

    def __exit__(self, *exc):
        import socket as _socket
        _socket.socket = self._real_socket
        threading.Thread = self._real_thread
        threading.Thread.join = self._real_join
        return False

    def assert_zero(self, test: unittest.TestCase) -> None:
        test.assertEqual(self.thread_count, 0,
                         "real threads created: %d" % self.thread_count)
        test.assertEqual(self.join_count, 0,
                         "real joins performed: %d" % self.join_count)
        test.assertEqual(self.socket_count, 0,
                         "real sockets created: %d" % self.socket_count)
        for models_dir in self._model_dirs:
            test.assertFalse(
                models_dir.exists(),
                "model download attempted: %s exists" % models_dir)


# ---------------------------------------------------------------------------
# Thread-free CLI helper + store seeding
# ---------------------------------------------------------------------------

def _child_env(extra=None) -> dict:
    env = dict(os.environ)
    for key in _MODE_ENV_KEYS:
        env.pop(key, None)
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def _unlink_quiet(path) -> None:
    """Remove a temp file, retrying once: on Windows a just-killed child can
    still hold the handle briefly after TimeoutExpired (PR review PRR-013)."""
    try:
        os.unlink(path)
    except OSError:
        time.sleep(0.05)
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_cli(argv, extra_env=None, input_text=None):
    """Run a repo CLI with the thread-free subprocess shape (one pipe +
    stderr to a real file)."""
    err_fd, err_path = tempfile.mkstemp(prefix="zmem-165-cli-err-")
    try:
        with os.fdopen(err_fd, "wb") as err_file:
            proc = subprocess.run(
                [sys.executable, str(STORE_PY)] + list(argv),
                cwd=str(REPO_ROOT), env=_child_env(extra_env),
                stdout=subprocess.PIPE, stderr=err_file,
                input=input_text, text=True, encoding="utf-8",
                errors="replace", timeout=180)
        stderr_text = Path(err_path).read_text(encoding="utf-8",
                                               errors="replace")
        return proc.returncode, proc.stdout or "", stderr_text
    finally:
        _unlink_quiet(err_path)


class _ScratchStore:
    """A per-test seeded store: three rows via the real add CLI, then id and
    timestamp pinning by rowid (store_builder.py precedent; test-side only).

    Construction only creates the directory; setUp registers the cleanup
    BEFORE calling seed(), so a seed failure cannot leak the temp dir
    (PR review PRR-013)."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-165-store-"))
        self.path = self.dir / "store.sqlite"

    def seed(self):
        for row in CASES["rows"]:
            code, out, err = _run_cli([
                "add", "--namespace", _NAMESPACE, "--type", row["type"],
                "--content", row["content"], "--tags", ",".join(row["tags"]),
                "--signal", "test", "--source-ref", "session:seed-native",
            ], extra_env=self.env())
            if code != 0:
                raise RuntimeError("seed add failed (%d): %s%s"
                                   % (code, out[-400:], err[-400:]))
        self._pin()

    def env(self) -> dict:
        return _child_env({
            "ZMEM_STORE": str(self.path),
            "ZMEM_DATA": str(self.dir),
            "ZMEM_MODELS_DIR": str(self.dir / "missing-models"),
            "ZMEM_NAMESPACE": _NAMESPACE,
            "ZMEM_LINK_THRESHOLD": "1.01",
        })

    def _pin(self) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            rows = conn.execute(
                "SELECT id, rowid FROM memory ORDER BY rowid").fetchall()
            pinned = sorted(row["id"] for row in CASES["rows"])
            for (old_id, _rowid), new_id in zip(rows, pinned):
                conn.execute("UPDATE memory SET id=? WHERE id=?",
                             (new_id, old_id))
            for column in ("created_at", "last_retrieved"):
                try:
                    conn.execute(
                        "UPDATE memory SET %s=? WHERE %s IS NOT NULL"
                        % (column, column), (_TIMESTAMP,))
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()

    def cleanup(self) -> None:
        shutil.rmtree(str(self.dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# Deterministic provider stub + thread-free transport
# ---------------------------------------------------------------------------

class _NativeStubProvider:
    """A MemoryProvider duck-type that routes recall through #158's single
    envelope via the real store CLI (issue Design step 4): one subprocess,
    one parsed envelope, ``rendered`` returned.  It never imports storelib,
    never opens SQLite, and never reads an operation-token loader."""

    name = "zmem"

    def __init__(self, store: _ScratchStore):
        self._store = store
        self.cli_calls = []
        self.envelope_reasons = []
        self.trivial_skips = []
        self.session_switch_calls = []
        self.sync_turn_calls = []
        self.delegation_calls = []

    def get_tool_schemas(self):
        return []

    def prefetch(self, query, *, session_id=""):
        vendored_provider = sys.modules["agent.memory_provider"]
        normalized = " ".join(str(query or "").split())
        if (not normalized
                or vendored_provider.is_trivial_prompt(normalized)):
            self.trivial_skips.append((normalized, session_id))
            return ""
        argv = ["recall", "--query", normalized, "--for-injection", "--json",
                "--namespace", _NAMESPACE, "--session-id", session_id,
                "--moment", "user_prompt", "--lane", "hermes-provider"]
        code, out, err = _run_cli(argv, extra_env=self._store.env())
        if code != 0:
            raise RuntimeError("stub recall failed (%d): %s%s"
                               % (code, out[-400:], err[-400:]))
        envelope = json.loads(out)
        self.cli_calls.append({"argv": argv, "session_id": session_id,
                               "reason": envelope.get("reason")})
        self.envelope_reasons.append(envelope.get("reason"))
        return envelope.get("rendered", "")

    def system_prompt_block(self):
        return _TIER0

    def on_session_switch(self, new_session_id, *, parent_session_id="",
                          reset=False, rewound=False, **kwargs):
        self.session_switch_calls.append(
            {"new": new_session_id, "parent": parent_session_id,
             "reset": reset, "rewound": rewound})

    def sync_turn(self, user_content, assistant_content, *, session_id="",
                  messages=None, **kwargs):
        self.sync_turn_calls.append(
            {"user": user_content, "assistant": assistant_content,
             "session_id": session_id, "messages": messages})

    def on_delegation(self, task, result, *, child_session_id="", **kwargs):
        self.delegation_calls.append(
            {"task": task, "result": result,
             "child_session_id": child_session_id, "kwargs": dict(kwargs)})

    def shutdown(self):
        return None


class _ThreadFreeTransport:
    """Test-owned transport duck-type: runs the provider's real prefetch argv
    through the thread-free CLI helper.  Used only where the REAL plugin
    provider must run its callback chain (mode exclusivity)."""

    def __init__(self, store: _ScratchStore):
        self._store = store

    def prefetch(self, query, *, namespace, session_id, moment, ops_tokens,
                 lane="hermes-provider"):
        # Production parity (PR review PRR-008): leading-dash queries ride
        # the `--query=` form (transport._query_args), and the child gets
        # ZMEM_CAPTURE defaulted on exactly like LocalSubprocess.
        if query.startswith("-"):
            query_args = ["--query=" + query]
        else:
            query_args = ["--query", query]
        argv = ["prefetch"] + query_args + ["--namespace", namespace,
                "--session-id", session_id, "--moment", moment,
                "--lane", lane]
        for token in ops_tokens:
            argv += ["--ops-token", token]
        argv.append("--json")
        child_env = self._store.env()
        child_env.setdefault("ZMEM_CAPTURE", "1")
        code, out, err = _run_cli(argv, extra_env=child_env)
        if code != 0:
            raise RuntimeError("transport prefetch failed (%d): %s%s"
                               % (code, out[-400:], err[-400:]))
        envelope = json.loads(out)
        envelope.pop("context", None)  # strip the additive MCP alias
        return envelope


class _InlineExecutor:
    """submit() runs inline and returns a completed Future (the vendored
    manager already supports executor injection via _sync_executor)."""

    def submit(self, fn):
        future = Future()
        try:
            future.set_result(fn())
        except Exception as exc:  # provider hooks must not break the worker
            future.set_exception(exc)
        return future


class _RecordingCtx:
    """Hermes plugin-context stand-in: records provider registrations, hook
    registrations, and hook invocations as <event>:<command-basename>."""

    def __init__(self):
        self.providers = []
        self.hooks = []
        self.events = []

    def register_memory_provider(self, provider):
        self.providers.append(provider)

    def register_hook(self, event, fn):
        self.hooks.append((event, fn))

    def record_event(self, event, basename):
        self.events.append("%s:%s" % (event, basename))


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------

class HermesNativeIntegrationTest(unittest.TestCase):
    """Ten deterministic native methods (issue #165 Design step 6)."""

    @classmethod
    def setUpClass(cls):
        _assert_vendored_headers()

    def setUp(self):
        # Snapshot then POP the mode keys in-process (PR review PRR-001):
        # the cached plugin module and the store children read these from
        # os.environ at call time, so ambient operator values would silently
        # change outcomes. _restore_env puts the operator's values back.
        self._prior_env = {key: os.environ.get(key)
                           for key in _MODE_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)
        # Guards entered BEFORE store creation so the seeding subprocesses
        # are covered by the counters too (the subprocess plumbing exclusion
        # keeps the counts at zero); cleanup is registered before seed() so
        # a seed failure cannot leak the temp dir (PRR-013).
        self._guards = _ResourceGuards()
        self._guards.__enter__()
        self.addCleanup(self._guards.__exit__, None, None, None)
        self.store = _ScratchStore()
        self.addCleanup(self.store.cleanup)
        self._guards.add_model_dir(self.store.dir / "missing-models")
        # Restore registration precedes seed() so a seed failure also
        # unwinds the in-process env pops (review round 2 nit).
        self.addCleanup(self._restore_env)
        self.store.seed()

    def _restore_env(self):
        for key, value in self._prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _PLUGINS_MEMORY_STATE["active"] = ""

    def _manager(self, pending_completion=None):
        module = _vendored_manager_module()
        manager = module.MemoryManager()
        manager._sync_executor = _InlineExecutor()
        fake = _fake_executor_module().FakeExecutor()
        fake.pending_completion = pending_completion
        manager._deadline_executor = fake
        return manager, module

    # -- 1 ------------------------------------------------------------------

    def test_discovery_by_provider_name(self):
        plugin = _plugin_module()
        _PLUGINS_MEMORY_STATE["active"] = "zmem"
        ctx = _RecordingCtx()
        plugin.register(ctx)
        self._guards.assert_zero(self)
        self.assertEqual(len(ctx.providers), 1)
        self.assertEqual(ctx.providers[0].name, "zmem")
        discovered = ctx.providers[0]
        self.assertEqual(
            discovered.__class__.__name__, "ZmemMemoryProvider")

    # -- 2 ------------------------------------------------------------------

    def test_tier0_in_system_prompt(self):
        core_path = self.store.dir / "core.md"
        core_path.write_text(_TIER0, encoding="utf-8")
        os.environ["ZMEM_CORE_MD"] = str(core_path)
        plugin = _plugin_module()
        provider = plugin.ZmemMemoryProvider()
        block = provider.system_prompt_block()
        self.assertEqual(block, _TIER0.strip())

        manager, module = self._manager()
        manager.add_provider(provider)
        system_prompt = manager.build_system_prompt()
        self._guards.assert_zero(self)
        self.assertIn(_TIER0.strip(), system_prompt)

    # -- 3 ------------------------------------------------------------------

    def test_prefetch_in_memory_context_block(self):
        manager, module = self._manager()
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)
        raw = manager.prefetch_all(_PROMPT_NONTRIVIAL,
                                   session_id=_SESSION_PRIMARY)
        wrapper = module.build_memory_context_block(raw)
        self._guards.assert_zero(self)
        self.assertTrue(wrapper.startswith(
            EXPECTED["wrapper_open"] + "\n" + EXPECTED["system_note_prefix"]))
        self.assertTrue(wrapper.endswith(EXPECTED["wrapper_close"]))
        self.assertEqual(wrapper.count(EXPECTED["wrapper_open"]), 1)
        self.assertEqual(wrapper.count(EXPECTED["wrapper_close"]), 1)
        for row_id in _ROW_IDS:
            self.assertIn(row_id, wrapper)
        self.assertEqual(stub.trivial_skips, [])
        self.assertEqual(len(stub.cli_calls), 1)

    # -- 4 ------------------------------------------------------------------

    def test_trivial_prompt_skipped(self):
        manager, module = self._manager()
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)
        raw = manager.prefetch_all(_PROMPT_TRIVIAL,
                                   session_id=_SESSION_PRIMARY)
        wrapper = module.build_memory_context_block(raw)
        self._guards.assert_zero(self)
        self.assertEqual(raw, "")
        self.assertEqual(wrapper, "")
        self.assertEqual(stub.cli_calls, [])
        self.assertEqual(len(stub.trivial_skips), 1)
        self.assertEqual(stub.trivial_skips[0][0], _PROMPT_TRIVIAL)

    # -- 5 ------------------------------------------------------------------

    def test_manager_join_8s_stalled_provider(self):
        manager, module = self._manager(pending_completion=100.0)
        fake = manager._deadline_executor
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)

        result = manager.prefetch_all(_PROMPT_NONTRIVIAL,
                                      session_id=_SESSION_PRIMARY)
        self._guards.assert_zero(self)
        self.assertEqual(result, "")
        self.assertEqual(fake.now(), 8.0)
        self.assertEqual(len(fake.cancellations), 1)
        abandoned = fake.cancellations[0]
        self.assertEqual(abandoned.run_count, 0)
        self.assertEqual(stub.cli_calls, [])

    # -- 6 ------------------------------------------------------------------

    def test_session_switch_resets(self):
        manager, module = self._manager()
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)
        manager.on_session_switch(
            _SESSION_SECONDARY, parent_session_id=_SESSION_PRIMARY,
            reset=True)
        manager.on_session_switch(_SESSION_PRIMARY, rewound=True)
        self._guards.assert_zero(self)
        self.assertEqual(len(stub.session_switch_calls), 2)
        first, second = stub.session_switch_calls
        self.assertEqual(first["new"], _SESSION_SECONDARY)
        self.assertEqual(first["parent"], _SESSION_PRIMARY)
        self.assertTrue(first["reset"])
        self.assertFalse(first["rewound"])
        self.assertEqual(second["new"], _SESSION_PRIMARY)
        self.assertFalse(second["parent"])
        self.assertFalse(second["reset"])
        self.assertTrue(second["rewound"])

    # -- 7 ------------------------------------------------------------------

    def test_tool_result_role_tool(self):
        manager, module = self._manager()
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)
        tool_result = dict(CASES["tool_result"])
        messages = [{"role": "user", "content": _PROMPT_NONTRIVIAL},
                    tool_result]
        manager.sync_all(_PROMPT_NONTRIVIAL, "assistant reply",
                         session_id=_SESSION_PRIMARY, messages=messages)
        self._guards.assert_zero(self)
        self.assertEqual(len(stub.sync_turn_calls), 1)
        seen = stub.sync_turn_calls[0]
        self.assertEqual(seen["session_id"], _SESSION_PRIMARY)
        self.assertIsNotNone(seen["messages"])
        roles = [m["role"] for m in seen["messages"]]
        self.assertIn("tool", roles)
        tool_entries = [m for m in seen["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_entries), 1)
        self.assertEqual(tool_entries[0]["content"],
                         CASES["tool_result"]["content"])

    # -- 8 ------------------------------------------------------------------

    def test_two_turn_dedup(self):
        manager, module = self._manager()
        stub = _NativeStubProvider(self.store)
        manager.add_provider(stub)

        first_raw = manager.prefetch_all(_PROMPT_NONTRIVIAL,
                                         session_id=_SESSION_PRIMARY)
        first_block = module.build_memory_context_block(first_raw)
        second_raw = manager.prefetch_all(_PROMPT_NONTRIVIAL,
                                          session_id=_SESSION_PRIMARY)
        second_block = module.build_memory_context_block(second_raw)
        self._guards.assert_zero(self)

        self.assertTrue(first_block.startswith(EXPECTED["wrapper_open"]))
        self.assertEqual(second_block, "")
        self.assertEqual(stub.envelope_reasons,
                         [EXPECTED["two_turn_delivery"]["turn1_reason"],
                          EXPECTED["two_turn_delivery"]["turn2_reason"]])
        self.assertEqual(len(stub.cli_calls), 2)
        blocks = sum(1 for block in (first_block, second_block) if block)
        self.assertEqual(blocks, EXPECTED["nontrivial_context_blocks"])

    # -- 9 ------------------------------------------------------------------

    def test_delegation_evidence_row(self):
        plugin = _plugin_module()
        provider = plugin.ZmemMemoryProvider()
        calls = []
        real_run_store = plugin._run_store

        def _recording_run_store(argv, *args, **kwargs):
            calls.append({"argv": list(argv),
                          "input_text": kwargs.get("input_text")})
            return {"ok": True, "returncode": 0,
                    "stdout": '{"ok": true, "id": "fixture-evidence-id"}',
                    "stderr": ""}

        plugin._run_store = _recording_run_store
        try:
            provider.on_delegation(
                CASES["delegation"]["task"], CASES["delegation"]["result"],
                child_session_id=_CHILD_SID, parent_session_id=_PARENT_SID)
        finally:
            plugin._run_store = real_run_store
        self._guards.assert_zero(self)

        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[:2], ["evidence", "write"])
        self.assertIn("--kind", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "delegation")
        self.assertIn("--lane", argv)
        self.assertEqual(argv[argv.index("--lane") + 1], "hermes-provider")
        self.assertIn("--moment", argv)
        self.assertEqual(argv[argv.index("--moment") + 1], "subagent")
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], _PARENT_SID)
        self.assertIn("--ref-path", argv)
        self.assertEqual(
            argv[argv.index("--ref-path") + 1],
            EXPECTED["delegation_ref_path"])
        payload = json.loads(calls[0]["input_text"])
        self.assertEqual(payload["child_session_id"], _CHILD_SID)
        self.assertEqual(payload["parent_session_id"], _PARENT_SID)
        self.assertEqual(payload["task_sha256"],
                         plugin._delegation_hash(CASES["delegation"]["task"]))
        self.assertEqual(payload["result_sha256"],
                         plugin._delegation_hash(
                             CASES["delegation"]["result"]))
        self.assertNotIn(CASES["delegation"]["task"],
                         calls[0]["input_text"])

    # -- 10 -----------------------------------------------------------------

    def test_mode_exclusivity(self):
        plugin = _plugin_module()

        # Provider mode: active provider is zmem.
        _PLUGINS_MEMORY_STATE["active"] = "zmem"
        provider_ctx = _RecordingCtx()
        plugin.register(provider_ctx)
        provider_events = list(provider_ctx.events)
        self.assertEqual(len(provider_ctx.providers), 1)
        self.assertEqual(
            [event for event, _fn in provider_ctx.hooks],
            ["post_tool_call", "pre_llm_call", "pre_verify"])
        for event, fn in provider_ctx.hooks:
            provider_ctx.record_event(event, "zmem")
        self.assertNotIn("pre_tool_call",
                         [e for e, _f in provider_ctx.hooks])

        hooks = dict(provider_ctx.hooks)
        real_provider = provider_ctx.providers[0]
        fake_module = _fake_executor_module()
        fake = fake_module.FakeExecutor()
        real_provider._deadline = fake
        real_provider._transport = _ThreadFreeTransport(self.store)
        # pre_llm_call's own gates: a session id (kwargs or initialize()),
        # an in-process namespace, and the documented user_message shape.
        os.environ["ZMEM_NAMESPACE"] = _NAMESPACE
        # Spawn recorder (AC3 "zero compatibility-hook calls"): every
        # subprocess this leg spawns — through run OR Popen (the transport
        # bridge constructs Popen directly) — is captured; none may name a
        # compat hook script.
        spawned_argv = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def _recording_run(argv, *args, **kwargs):
            spawned_argv.append(list(argv))
            return real_run(argv, *args, **kwargs)

        def _recording_popen(argv, *args, **kwargs):
            spawned_argv.append(list(argv))
            return real_popen(argv, *args, **kwargs)

        subprocess.run = _recording_run
        subprocess.Popen = _recording_popen
        try:
            result = hooks["pre_llm_call"](
                session_id=_SESSION_PRIMARY, user_message=_PROMPT_NONTRIVIAL,
                tool_count=0, remaining_tokens=8000, model="fixture-model",
                platform="test")
        finally:
            subprocess.run = real_run
            subprocess.Popen = real_popen
        provider_blocks = 0
        context = ""
        if isinstance(result, dict) and result.get("context"):
            context = result["context"]
            provider_blocks = 1
        self._guards.assert_zero(self)

        self.assertEqual(provider_blocks, 1)
        self.assertTrue(context)
        self.assertNotIn(EXPECTED["wrapper_open"], context)
        compat_basenames = ("zmem-hermes-reflect", "zmem-hermes-verify",
                            "zmem-hermes-convention")
        for argv in spawned_argv:
            joined = " ".join(str(part) for part in argv)
            for basename in compat_basenames:
                self.assertNotIn(
                    basename, joined,
                    "compat hook spawned in provider mode: %s" % joined)
        for event in provider_ctx.events:
            self.assertFalse(any(event.endswith(":" + name)
                                 for name in compat_basenames),
                             "compat hook fired in provider mode: %s"
                             % event)

        # Compatibility mode: active provider is NOT zmem.
        _PLUGINS_MEMORY_STATE["active"] = "builtin"
        compat_ctx = _RecordingCtx()
        plugin.register(compat_ctx)
        self.assertEqual(compat_ctx.hooks, [])
        self.assertEqual(len(compat_ctx.providers), 1)  # registered but inert

        payload = json.dumps({
            "session_id": _SESSION_SECONDARY,
            "user_message": _PROMPT_NONTRIVIAL,
        })
        err_fd, err_path = tempfile.mkstemp(prefix="zmem-165-compat-err-")
        try:
            with os.fdopen(err_fd, "wb") as err_file:
                proc = subprocess.run(
                    [sys.executable, str(REFLECT_HOOK)],
                    cwd=str(REPO_ROOT), env=self.store.env(),
                    stdout=subprocess.PIPE, stderr=err_file,
                    input=payload, text=True, encoding="utf-8",
                    errors="replace", timeout=180)
            hook_stderr = Path(err_path).read_text(encoding="utf-8",
                                                   errors="replace")
        finally:
            _unlink_quiet(err_path)
        self._guards.assert_zero(self)

        self.assertEqual(proc.returncode, 0, hook_stderr[-500:])
        hook_payload = json.loads(proc.stdout)
        hook_context = hook_payload.get("context", "")
        self.assertTrue(hook_context)
        self.assertNotIn(EXPECTED["wrapper_open"], hook_context)
        compat_ctx.record_event("pre_llm_call", "zmem-hermes-reflect.py")

        recorded = sorted(provider_ctx.events + compat_ctx.events)
        self.assertEqual(recorded, sorted(EXPECTED["provider_mode_events"]
                                          + EXPECTED["compat_mode_events"]))
        self.assertNotIn("pre_tool_call:zmem", recorded)
        for row_id in _ROW_IDS:
            self.assertIn(row_id, hook_context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
