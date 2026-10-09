"""Issue #163: provider-registered post_tool_call / pre_llm_call /
pre_verify hooks in provider mode.

Deterministic, thread-free, model-absent: the callback tests inject
``tests/support/fake_executor.py::FakeExecutor`` as the Scheduler seam,
inject the real ``hermes-plugin/transport.py`` ``DeadlineExecutor`` as the
deadline seam with its ``run`` method patched for synchronous completion
(and, per case, deterministic timeout/rejection), and patch
``threading.Thread`` to fail construction so a zero-real-thread guarantee
is asserted while ``FakeExecutor.advance(0.0)`` drives the scheduled work.
Store-touching paths run the real ``store.py`` subprocess against the
scratch store pinned below — never the operator's ``~/.zmem``.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Store isolation BEFORE any import that might reach storelib or spawn a
# store subprocess: every store-touching case resolves these pins.
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-163-hooks-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_MODE_ENV_KEYS = ("ZMEM_HERMES_MODE", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
                  "ZMEM_MCP_TOKEN_FILE", "ZMEM_HERMES_DEADLINE_S",
                  "ZMEM_NAMESPACE")

_HOOKS_ENV_KEYS = ("ZMEM_QUERY_CONTEXT", "ZMEM_INJECT",
                   "ZMEM_CONVENTION_INTERVAL")

_TRANSPORT_PATH = REPO_ROOT / "hermes-plugin" / "transport.py"
_SUPPORT_DIR = REPO_ROOT / "tests" / "support"
_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "hermes"

_PROVIDER_HOOK_NAMES = ("post_tool_call", "pre_llm_call", "pre_verify")
_LEGACY_HOOK_NAMES = ("prefetch", "queue_prefetch", "sync_turn",
                      "on_session_end", "on_pre_compress", "on_memory_write")
_FORBIDDEN_PROVIDER_STRINGS = (
    "register_hook(\"pre_tool_call\"",
    "import storelib", "from storelib", "sqlite3",
    "_LEDGER_MOD", "_OPS_TOKENS", "delivery_ledger", "correction_queue",
    "import inject", "import ops_tokens", "import delivery_ledger",
)
_HOOKS_DIR_FORBIDDEN = ("import storelib", "from storelib", "import inject",
                        "import ops_tokens", "import delivery_ledger")


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "zmem_transport_163", _TRANSPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_transport_163"] = module
    spec.loader.exec_module(module)
    return module


def _load_fake_executor():
    spec = importlib.util.spec_from_file_location(
        "zmem_fake_executor_163", _SUPPORT_DIR / "fake_executor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_fake_executor_163"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider(name="zmem_hermes_163"):
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed."""
    agent = types_module()
    sys.modules.setdefault("agent", agent["agent"])
    sys.modules.setdefault("agent.memory_provider", agent["mp"])
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def types_module():
    import types
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_hermes_transport.py)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    return {"agent": agent, "mp": mp}


def _load_doctor():
    scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import doctor  # noqa: paths pinned above
    return doctor


def _load_ops_tokens():
    scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import storelib.ops_tokens as ops_tokens
    return ops_tokens


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _expected_context():
    return _fixture("pre_llm_call.expected.json")["context"]


def _fence_from_expected():
    context = _expected_context()
    close = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
    cut = context.index(close) + len(close)
    return context[:cut] + "\n"


def _sync_deadline(transport, mode="sync"):
    """A real #160 DeadlineExecutor with its run seam patched: 'sync'
    executes inline, 'timeout' returns the None sentinel, 'raise' raises."""
    deadline = transport.DeadlineExecutor()

    def _run(fn, deadline_s):
        if mode == "timeout":
            return None
        if mode == "raise":
            raise RuntimeError("executor rejection")
        return fn()

    deadline.run = _run
    return deadline


class _NoThreads:
    """Guard against provider-owned thread creation.

    Any ``threading.Thread`` construction whose CALLER is not the
    ``subprocess`` module raises.  The subprocess exemption is host-owned
    plumbing: on Windows ``subprocess.run(capture_output=True)`` builds two
    reader threads per call (Linux uses select and builds none), and the
    transport's store bridge legitimately relies on it.  A provider-frame
    attempt (scheduling, telemetry that escapes its fail-open wrapper,
    deadline work) still raises, so provider code cannot create threads.
    """

    def __init__(self):
        self._original = threading.Thread
        self.attempts = 0

    def __enter__(self):
        original = self._original
        guard = self

        def _thread_guard(*args, **kwargs):
            caller = sys._getframe(1)
            caller_file = Path(
                caller.f_globals.get("__file__") or "?").name
            if caller_file == "subprocess.py":
                return original(*args, **kwargs)
            guard.attempts += 1
            raise AssertionError(
                "provider frame %s created a real thread"
                % caller_file)

        threading.Thread = _thread_guard
        return self

    def __exit__(self, *exc):
        threading.Thread = self._original
        return False


class _FakeCtx:
    """Captures register_memory_provider + register_hook calls in order."""

    def __init__(self):
        self.providers = []
        self.hooks = []

    def register_memory_provider(self, provider):
        self.providers.append(provider)

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))


def _make_provider(mod, transport, *, deadline_mode="sync"):
    fake_executor = _load_fake_executor()
    scheduler = fake_executor.FakeExecutor()
    # The provider submits without an explicit delay; FakeExecutor only
    # queues handles given one, so default the delay to 0.0 — the contract's
    # "FakeExecutor.advance(0.0) completes the scheduled append".
    original_submit = scheduler.submit

    def _submit(fn, delay_s=None):
        return original_submit(fn, 0.0 if delay_s is None else delay_s)

    scheduler.submit = _submit
    deadline = _sync_deadline(transport, mode=deadline_mode)
    provider = mod.ZmemMemoryProvider(scheduler=scheduler, deadline=deadline)
    return provider, deadline


class _FakeStore:
    """A temporary fake store.py executable the local transport can point
    at: records argv, then emits the declared JSON payload and exit code."""

    def __init__(self, tmp: Path, payload_obj=None, mode="ok"):
        self.path = tmp / "store.py"
        self.record = tmp / "argv.json"
        payload = "None" if payload_obj is None else repr(payload_obj)
        script = f'''
import json, sys
from pathlib import Path
Path({str(self.record)!r}).write_text(json.dumps(sys.argv), encoding="utf-8")
mode = {mode!r}
if mode == "fail-exit":
    sys.stderr.write("boom")
    sys.exit(3)
if mode == "empty":
    sys.exit(0)
if mode == "bad-json":
    print("this is not json at all")
    sys.exit(0)
print(json.dumps({payload}, indent=2))
'''
        self.path.write_text(script, encoding="utf-8")

    def argv(self):
        try:
            return json.loads(self.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def _ops_tokens_module():
    return _load_ops_tokens()


def _ring_stem(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _clean_hook_env():
    for key in _HOOKS_ENV_KEYS:
        os.environ.pop(key, None)


class HermesProviderHooksTest(unittest.TestCase):
    """The seven contract-mandated provider-callback tests (issue #163)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS
                       + _HOOKS_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)
        # Deterministic namespace for _resolve_namespace (same pin the #162
        # provider tests use).
        os.environ["ZMEM_NAMESPACE"] = "user:global"
        _clean_hook_env()
        self._transport = _load_transport()
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-163-case-"))

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- AC1 ---------------------------------------------------------------

    def test_provider_registers_exact_three_hooks(self):
        mod = _load_provider("zmem_hermes_163_ac1")
        mod._active_memory_provider_name = lambda: "zmem"
        ctx = _FakeCtx()
        mod.register(ctx)
        self.assertEqual(len(ctx.providers), 1)
        self.assertEqual([name for name, _ in ctx.hooks],
                         list(_PROVIDER_HOOK_NAMES))
        provider = ctx.providers[0]
        for name, callback in ctx.hooks:
            self.assertEqual(callback, getattr(provider, name))
        # The doctor allowlist contains exactly the same nine manifest names.
        doctor = _load_doctor()
        manifest_hooks = doctor._parse_simple_yaml(
            REPO_ROOT / "hermes-plugin" / "plugin.yaml").get("hooks") or []
        self.assertEqual(list(manifest_hooks),
                         list(_LEGACY_HOOK_NAMES) + list(_PROVIDER_HOOK_NAMES))
        self.assertEqual(set(doctor._HERMES_PROVIDER_HOOKS),
                         set(manifest_hooks))
        self.assertEqual(len(doctor._HERMES_PROVIDER_HOOKS), 9)

    def test_compatibility_mode_registers_zero_hooks(self):
        mod = _load_provider("zmem_hermes_163_compat")
        for active in ("", "hindsight", "other-provider", "zmem-old"):
            mod._active_memory_provider_name = lambda active=active: active
            ctx = _FakeCtx()
            mod.register(ctx)
            self.assertEqual(len(ctx.providers), 1)
            self.assertEqual(ctx.hooks, [], active)

    def test_pre_tool_call_is_never_registered(self):
        # The registration scan: every file under hermes-plugin/ is free of
        # the banned registration call.
        banned = 'register_hook("pre_tool_call"'
        for path in (REPO_ROOT / "hermes-plugin").rglob("*"):
            if not path.is_file():
                continue
            self.assertNotIn(banned, path.read_text(encoding="utf-8",
                                                    errors="replace"),
                             str(path))
        # The process-boundary scan: __init__.py carries the full forbidden
        # list; the compatibility hooks only the storelib-import family
        # (they legitimately own sqlite3 as separate store processes).
        provider_source = (REPO_ROOT / "hermes-plugin" / "__init__.py"
                           ).read_text(encoding="utf-8")
        for needle in _FORBIDDEN_PROVIDER_STRINGS:
            self.assertNotIn(needle, provider_source, needle)
        hooks_dir = REPO_ROOT / "hermes-plugin" / "hooks"
        for path in hooks_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8", errors="replace")
            for needle in _HOOKS_DIR_FORBIDDEN:
                self.assertNotIn(needle, source, "%s: %s" % (path, needle))

    # -- AC3 ---------------------------------------------------------------

    def test_provider_post_tool_call_arms_ring(self):
        mod = _load_provider("zmem_hermes_163_ring")
        provider, _deadline = _make_provider(mod, self._transport)
        # Sibling tests in this module share the per-process scratch ring;
        # this case owns exactly-one-event for its session.
        # The ring path must track the env the spawned child actually
        # inherits: in a shared-process run a sibling module's import-time
        # pin may win over this module's, so resolve from os.environ.
        ring = (Path(os.environ["ZMEM_DATA"]) / "ops"
                / (_ring_stem("session-163") + ".log"))
        ring.unlink(missing_ok=True)
        payload = _fixture("pre_llm_call.input.json")["post_tool_call"]
        with _NoThreads():
            self.assertEqual(provider.post_tool_call(**payload), {})
        provider._scheduler.advance(0.0)
        self.assertTrue(ring.is_file())
        lines = ring.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["tool"], "bash")
        self.assertEqual(event["ops"], "git status")
        self.assertIsInstance(event["ts"], int)
        raw = ring.read_text(encoding="utf-8")
        self.assertNotIn("--short", raw)
        self.assertNotIn("git status --short", raw)
        # Cap subcase: 65 deterministic events through the real writer leave
        # at most 64 complete events and at most 65536 UTF-8 bytes.
        ops_tokens = _ops_tokens_module()
        cap_session = "session-163-cap"
        for i in range(65):
            self.assertTrue(ops_tokens.append_ops_ring(
                str(_SCRATCH), cap_session, "Bash", "git push event-%d" % i))
        cap_lines = (_SCRATCH / "ops" / (_ring_stem(cap_session) + ".log")
                     ).read_text(encoding="utf-8").strip().splitlines()
        self.assertLessEqual(len(cap_lines), ops_tokens._RING_TRIM_TO_LINES)
        self.assertLessEqual(
            len("\n".join(cap_lines).encode("utf-8")),
            ops_tokens._RING_MAX_BYTES)

    # -- AC2 ---------------------------------------------------------------

    def test_provider_pre_llm_call_matches_fixture(self):
        mod = _load_provider("zmem_hermes_163_fixture")
        provider, deadline = _make_provider(mod, self._transport)
        input_fixture = _fixture("pre_llm_call.input.json")
        # 1. The real Hermes-shaped post_tool_call arms the token list.
        with _NoThreads():
            self.assertEqual(
                provider.post_tool_call(**input_fixture["post_tool_call"]), {})
        with provider._callback_state_lock:
            tokens = list(
                provider._callback_state["session-163"]["ops_tokens"])
        self.assertIn("git", tokens)
        self.assertIn("status", tokens)
        # 2. The test-only seam arms the nudge (Hermes never supplies one).
        provider._set_pending_nudge_for_test(
            "session-163", input_fixture["pending_nudge"])
        # 3. The complete 14-key envelope the fake store executable emits;
        #    excluded is a list of 36-character UUID-shaped strings.
        envelope = {
            "results": [{
                "id": "00000000-0000-4000-8000-000000000163",
                "content": "fixture rendered recall",
                "confidence": 0.9,
                "signal": "test",
                "namespace": "user:global",
                "type": "lesson",
                "source_ref": "session:session-163",
            }],
            "count": 1,
            "omitted": 0,
            "reason": "injected",
            "excluded": [],
            "candidate_ids": ["00000000-0000-4000-8000-000000000163"],
            "tokens_used": 4,
            "tokens_budget": 1500,
            "budget_dropped": 0,
            "budget_admission": 4,  # real wire shape is an int (PRR-001)
            "budget_truncated": 0,
            "budget_dropped_protected": 0,
            "arms": {},
            "rendered": _fence_from_expected(),
        }
        for ident in envelope["excluded"] + envelope["candidate_ids"]:
            self.assertIsInstance(ident, str)
            self.assertEqual(len(ident), 36)
            self.assertRegex(ident, r"^[0-9a-f-]{36}$")
            uuid.UUID(ident)
        fake_store = _FakeStore(self._tmp, payload_obj=envelope)
        provider._transport = self._transport.LocalSubprocess(
            store_py=str(fake_store.path), executor=deadline, deadline_s=6.0)
        # 4. Only Hermes-shaped keyword fields reach pre_llm_call.
        with _NoThreads():
            result = provider.pre_llm_call(**input_fixture["pre_llm_call"])
        expected_bytes = (_FIXTURES / "pre_llm_call.expected.json"
                          ).read_bytes()
        actual_bytes = json.dumps(
            result, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8") + b"\n"
        self.assertEqual(actual_bytes, expected_bytes)
        # The fake transport received the bounded tokens and the lane.
        argv = fake_store.argv()
        self.assertIsNotNone(argv)
        self.assertIn("--lane", argv)
        self.assertEqual(argv[argv.index("--lane") + 1], "hermes-provider")
        token_pairs = [argv[i + 1] for i, a in enumerate(argv)
                       if a == "--ops-token"]
        self.assertEqual(token_pairs, ["git", "status"])
        # 5. A successful delivery clears the pending nudge.
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["session-163"]["pending_nudge"], "")

    # -- AC5 ---------------------------------------------------------------

    def test_provider_pre_verify_returns_nudge(self):
        mod = _load_provider("zmem_hermes_163_verify")
        provider, _deadline = _make_provider(mod, self._transport)
        session = "session-163-verify"
        # Zero tool count -> {} without a store call.
        self.assertEqual(provider.pre_verify(session_id=session), {})
        # One accepted tool event arms tool_count.
        with _NoThreads():
            self.assertEqual(provider.post_tool_call(
                session_id=session, tool_name="bash",
                args={"command": "git push"}, status="ok"), {})
        provider._scheduler.advance(0.0)
        # No live lesson for session:<sid> (scratch store has none) ->
        # exactly one continue object with the copied verify nudge.
        result = provider.pre_verify(session_id=session)
        self.assertEqual(result, {"action": "continue",
                                  "message": mod._provider_verify_nudge(
                                      session)})
        # The next call returns {} from the persistent prompted marker.
        self.assertEqual(provider.pre_verify(session_id=session), {})
        with provider._callback_state_lock:
            self.assertTrue(provider._callback_state[session]["prompted"])

    # -- thread-freedom (AC6 adjacent, contract Design 7) ------------------

    def test_callbacks_use_fake_executor_without_threads(self):
        mod = _load_provider("zmem_hermes_163_threads")
        provider, deadline = _make_provider(mod, self._transport)
        envelope = {
            "results": [], "count": 0, "omitted": 0, "reason": "empty-pool",
            "excluded": [], "candidate_ids": [], "tokens_used": 0,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 0,
            "budget_truncated": 0, "budget_dropped_protected": 0,
            "arms": {}, "rendered": "",
        }
        fake_store = _FakeStore(self._tmp, payload_obj=envelope)
        provider._transport = self._transport.LocalSubprocess(
            store_py=str(fake_store.path), executor=deadline, deadline_s=6.0)
        session = "session-163-threads"
        # threading.Thread is patched to raise, so zero real threads can be
        # created structurally; the callbacks still complete with exactly
        # the expected results, which proves no provider-essential path
        # needs a thread: all scheduled work rides the FakeExecutor and the
        # patched (synchronous) DeadlineExecutor seam.
        with _NoThreads() as guard:
            self.assertEqual(provider.post_tool_call(
                session_id=session, tool_name="bash",
                args={"command": "git status"}, status="ok"), {})
            provider._scheduler.advance(0.0)
            result = provider.pre_llm_call(
                session_id=session, user_message="Summarize the recent work.")
            self.assertEqual(result, {})
            # Tool activity + no live lesson in the scratch store -> the
            # exact continue object, delivered without any real thread.
            self.assertEqual(provider.pre_verify(session_id=session),
                             {"action": "continue",
                              "message": mod._provider_verify_nudge(session)})
        # The only Thread attempts (if any) came from the fail-open
        # telemetry prelude, which is allowed to be blocked; zero real
        # threads were created (the patch raises before construction) and
        # the ring write still landed via the fake scheduler.
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state[session]["tool_count"], 1)
        ring = (Path(os.environ["ZMEM_DATA"]) / "ops"
                / (_ring_stem(session) + ".log"))
        self.assertTrue(ring.is_file())


class HermesProviderFailOpenTest(unittest.TestCase):
    """AC4: every failure class returns exactly {} and never raises into
    Hermes.  Method names carry the ``fail_open`` substring that the frozen
    acceptance check filters on."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS
                       + _HOOKS_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)
        _clean_hook_env()
        self._transport = _load_transport()
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-163-failopen-"))

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _provider(self, deadline_mode="sync"):
        mod = _load_provider("zmem_hermes_163_failopen_%s" % deadline_mode)
        provider, _deadline = _make_provider(mod, self._transport,
                                             deadline_mode=deadline_mode)
        return mod, provider

    def _armed(self, provider, session="session-163"):
        provider.post_tool_call(session_id=session, tool_name="bash",
                                args={"command": "git status"}, status="ok")
        provider._scheduler.advance(0.0)

    def _envelope_transport(self, mod, provider, payload_obj, mode="ok"):
        deadline = provider._deadline
        fake_store = _FakeStore(self._tmp, payload_obj=payload_obj,
                                mode=mode)
        provider._transport = self._transport.LocalSubprocess(
            store_py=str(fake_store.path), executor=deadline, deadline_s=6.0)

    # -- post_tool_call cases (1-6) ----------------------------------------

    def test_fail_open_missing_session(self):
        _mod, provider = self._provider()
        self.assertEqual(provider.post_tool_call(tool_name="bash",
                                                 args={"command": "ls"},
                                                 status="ok"), {})
        # Discriminator (PRR-020): with the session guard deleted, the
        # callback still returns {} but creates a callback-state entry for
        # the blank id and schedules an append.  State absence, not the
        # universal {} return, is the observable.
        with provider._callback_state_lock:
            self.assertEqual(len(provider._callback_state), 0)

    def test_fail_open_missing_tool(self):
        _mod, provider = self._provider()
        self.assertEqual(provider.post_tool_call(session_id="session-163",
                                                 args={"command": "ls"},
                                                 status="ok"), {})
        with provider._callback_state_lock:
            self.assertNotIn("session-163", provider._callback_state)

    def test_fail_open_missing_operation(self):
        _mod, provider = self._provider()
        self.assertEqual(provider.post_tool_call(session_id="session-163",
                                                 tool_name="bash",
                                                 args={"flags": ["-l"]},
                                                 status="ok"), {})
        with provider._callback_state_lock:
            entry = provider._callback_state.get("session-163")
            self.assertTrue(entry is None or entry["tool_count"] == 0)

    def test_fail_open_query_context_zero(self):
        _mod, provider = self._provider()
        os.environ["ZMEM_QUERY_CONTEXT"] = "0"
        self.assertEqual(provider.post_tool_call(
            session_id="session-163", tool_name="bash",
            args={"command": "git status"}, status="ok"), {})
        with provider._callback_state_lock:
            self.assertNotIn("session-163", provider._callback_state)
        # A whitespace-padded value still disables (repo convention).  The
        # discriminator is state absence, not the {} return (post_tool_call
        # returns {} on every path).
        os.environ["ZMEM_QUERY_CONTEXT"] = " 0 "
        self.assertEqual(provider.post_tool_call(
            session_id="session-163", tool_name="bash",
            args={"command": "git status"}, status="ok"), {})
        with provider._callback_state_lock:
            self.assertNotIn("session-163", provider._callback_state)

    def test_fail_open_missing_namespace(self):
        mod, provider = self._provider()
        provider._resolve_namespace = lambda **kwargs: ""
        # Discriminator (PRR-020): with the namespace guard deleted the
        # callback proceeds with namespace="" and a working transport WOULD
        # deliver context — install that transport so the {} is load-bearing.
        envelope = self._complete_envelope(rendered="fence text")
        self._envelope_transport(mod, provider, envelope)
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_disabled_injection(self):
        mod, provider = self._provider()
        self._armed(provider)
        # A working fake transport would deliver context when enabled, so
        # the disable path (not the incidental {}) is what this pins.
        envelope = self._complete_envelope(rendered="fence text")
        self._envelope_transport(mod, provider, envelope)
        os.environ["ZMEM_INJECT"] = "0"
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})
        self.assertEqual(provider.pre_verify(session_id="session-163"), {})
        # Whitespace-padded " 0 " also disables; without the disable both
        # calls would deliver the fence.
        os.environ["ZMEM_INJECT"] = " 0 "
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})
        del os.environ["ZMEM_INJECT"]
        delivered = provider.pre_llm_call(
            session_id="session-163", user_message="hello")
        self.assertEqual(delivered, {"context": "fence text"})

    def test_fail_open_missing_llm_session(self):
        mod, provider = self._provider()
        # Discriminator (PRR-020): a working transport would deliver context
        # for whatever session id reached it — only the session guard keeps
        # the response empty.
        envelope = self._complete_envelope(rendered="fence text")
        self._envelope_transport(mod, provider, envelope)
        self.assertEqual(provider.pre_llm_call(user_message="hello"), {})

    # -- pre_llm_call envelope cases (7-12) ---------------------------------

    def _complete_envelope(self, **overrides):
        envelope = {
            "results": [], "count": 0, "omitted": 0, "reason": "injected",
            "excluded": [], "candidate_ids": [], "tokens_used": 0,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 0,
            "budget_truncated": 0, "budget_dropped_protected": 0,
            "arms": {}, "rendered": "",
        }
        envelope.update(overrides)
        return envelope

    def test_fail_open_malformed_envelope(self):
        _mod, provider = self._provider()
        self._armed(provider)
        # PRR-020 discriminator: a COMPLETE envelope whose `count` has a
        # non-int TYPE — only the int shape check rejects this (the
        # required-key loop passes); a wrong-shaped envelope that slipped
        # through would deliver "fence text".
        bad = self._complete_envelope(rendered="fence text", count="7")
        self._envelope_transport(_mod, provider, bad)
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_missing_envelope_key(self):
        _mod, provider = self._provider()
        self._armed(provider)
        # PRR-020 discriminator: drop `reason` — presence-checked ONLY by
        # the required-key loop (no later shape check reads it), with a
        # non-empty rendered that would otherwise deliver.
        partial = self._complete_envelope(rendered="fence text")
        del partial["reason"]
        self._envelope_transport(_mod, provider, partial)
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_unknown_envelope_key(self):
        _mod, provider = self._provider()
        self._armed(provider)
        extra = self._complete_envelope(rendered="fence",
                                        surprise_key=True)
        self._envelope_transport(_mod, provider, extra)
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_empty_rendered(self):
        _mod, provider = self._provider()
        self._armed(provider)
        self._envelope_transport(
            _mod, provider, self._complete_envelope(rendered=""))
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_nonzero_store_exit(self):
        _mod, provider = self._provider()
        self._armed(provider)
        self._envelope_transport(_mod, provider, None, mode="fail-exit")
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})

    def test_fail_open_timeout(self):
        mod, provider = self._provider(deadline_mode="timeout")
        self._armed(provider)
        provider._set_pending_nudge_for_test("session-163",
                                             "Capture the lesson before "
                                             "finishing.")
        self._envelope_transport(
            mod, provider, self._complete_envelope(rendered="fence"))
        self.assertEqual(provider.pre_llm_call(
            session_id="session-163", user_message="hello"), {})
        # Negative-path pin: the timeout sentinel check returns BEFORE the
        # clear-on-success block, so the armed nudge must survive (PRR-005
        # family — preservation is the contract, even though {} is
        # returned on every path).
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["session-163"]["pending_nudge"],
                "Capture the lesson before finishing.")

    def test_fail_open_executor_rejection(self):
        _mod, provider = self._provider()
        # post_tool_call: scheduler.submit raising is swallowed (fail-open).
        class _RaisingScheduler:
            def submit(self, fn):
                raise RuntimeError("scheduler rejection")

        provider._scheduler = _RaisingScheduler()
        self.assertEqual(provider.post_tool_call(
            session_id="session-163", tool_name="bash",
            args={"command": "git status"}, status="ok"), {})
        # pre_llm_call: deadline.run raising is swallowed.
        _mod2, provider2 = self._provider(deadline_mode="raise")
        self.assertEqual(provider2.pre_llm_call(
            session_id="session-163", user_message="hello"),
            {})

    # -- pre_verify cases (13-14) -------------------------------------------

    def test_fail_open_malformed_verify_output(self):
        mod, provider = self._provider()
        session = "session-163-verify-bad"
        self._armed(provider, session)
        real_run_store = mod._run_store

        def _bad_store(args, input_text=None, **kwargs):
            return {"ok": True, "stdout": "not json at all",
                    "stderr": "", "returncode": 0}

        mod._run_store = _bad_store
        try:
            self.assertEqual(provider.pre_verify(session_id=session), {})
            with provider._callback_state_lock:
                self.assertFalse(
                    provider._callback_state[session]["prompted"])
        finally:
            mod._run_store = real_run_store

    def test_fail_open_store_failure(self):
        mod, provider = self._provider()
        session = "session-163-verify-fail"
        self._armed(provider, session)
        real_run_store = mod._run_store

        def _failing_store(args, input_text=None, **kwargs):
            return {"ok": False, "stdout": "", "stderr": "boom",
                    "returncode": 1}

        mod._run_store = _failing_store
        try:
            self.assertEqual(provider.pre_verify(session_id=session), {})
            with provider._callback_state_lock:
                self.assertFalse(
                    provider._callback_state[session]["prompted"])
        finally:
            mod._run_store = real_run_store
        # Zero tool count and an existing prompted marker also answer {}.
        self.assertEqual(provider.pre_verify(session_id="never-armed"), {})
        with provider._callback_state_lock:
            provider._callback_entry(session)["prompted"] = True
        mod._run_store = real_run_store
        self.assertEqual(provider.pre_verify(session_id=session), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
