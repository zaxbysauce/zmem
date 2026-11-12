"""Issue #389 AC8 — prompt-timestamp threading, behavioral suite.

The explicit-clock chain pinned leg by leg (F5 of the approved plan):
  HookLegTest          — the recall body forwards the payload's timestamp
                         (``prompt_timestamp`` with precedence over
                         ``timestamp``) verbatim as ``--prompt-timestamp``
                         on the store argv; absent/invalid forwards
                         nothing and never errors (the frozen
                         probe_389_prompt_timestamp_threading.py stub-store
                         mechanism).
  TemporalWindowTest   — an explicit prompt time beats a divergent
                         ZMEM_TEST_NOW at storelib.recall._temporal_window.
  HermesProviderTest   — _passive_store_args/_tool_search (local + remote)
                         forward the parameter; the #162 prefetch cache key
                         differentiates on it; queue_prefetch's background
                         closure carries the submit-time clock; pre_llm_call
                         threads it; _SEARCH_SCHEMA carries the property.
  FleetTransportTest   — McpHttp.search/prefetch payload fields and the
                         LocalSubprocess prefetch argv flag.
  BrokerTest           — _handle_read extracts (precedence + strict ISO)
                         and forwards into http.prefetch; invalid drops.
  StoreCliTest         — the prefetch parser accepts the flag end to end.
  InjectSeamTest       — select_and_budget_for_injection(prompt_timestamp=)
                         reaches recall_memory verbatim.

Run standalone: python tests/test_prompt_timestamp_threading.py
"""

import _store_guard  # noqa: F401  (issue #244 isolation guard)

# ---------------------------------------------------------------------------
# Env pin — BEFORE any storelib import (house contract, the
# tests/test_temporal.py pattern: storelib freezes STORE_PATH on first
# import; the operator's ~/.zmem is never a test target).
# ---------------------------------------------------------------------------
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
HERMES_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"

_TMP = tempfile.mkdtemp(prefix="zmem-389-promptts-test-")
atexit.register(shutil.rmtree, _TMP, True)
# Issue #389 AC9 / FC-5: capture the pre-pin ambient BEFORE the module-level
# mutations below so each carries an explicit unittest.addModuleCleanup
# restore (the tests/test_temporal.py pattern; standalone runs execute
# doModuleCleanups, one-process runs never depend on it).
_MODULE_ENV_PRIOR = {key: os.environ.get(key) for key in (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
    "ZMEM_LINK_THRESHOLD", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW")}
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_LINK_THRESHOLD"] = "1.01"
os.environ["ZMEM_EMBED_PROFILE"] = "minilm"  # model-absent shape
os.environ.pop("ZMEM_TEST_NOW", None)


def _restore_module_env():
    for key, value in _MODULE_ENV_PRIOR.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


unittest.addModuleCleanup(_restore_module_env)

sys.path.insert(0, str(SCRIPTS_DIR))

NAMESPACE = "project:pts389"
SESSION_ID = "pts389-session"
PROMPT_TS = "2026-09-10T15:30:00Z"        # the payload's prompt timestamp
ALT_TS = "2026-10-01T08:00:00Z"           # a second valid, divergent clock
DIVERGENT_NOW = "2027-03-01T00:00:00Z"    # a divergent ZMEM_TEST_NOW
E2E_DIVERGENT_NOW = "2030-01-01T00:00:00Z"  # e2e leg: years away from TS
TEMPORAL_PROMPT = "outage during last week in the alpha service"

# The stub store child (probe_389 mechanism): records argv to the capture
# file, then answers every command with a well-formed envelope so the hook
# body completes its fail-open path.
_STUB_STORE = """\
import json, os, sys
capture = os.environ.get("ZMEM_TEST_CAPTURE", "")
argv = sys.argv[1:]
if capture:
    with open(capture, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")
cmd = argv[0] if argv else ""
if cmd == "query-rewrite":
    val = ""
    for i, a in enumerate(argv):
        if a == "--prompt" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--prompt="):
            val = a[len("--prompt="):]
    print(json.dumps({"query": val, "rewrite": 0}))
else:
    print(json.dumps({
        "results": [], "rendered": "", "reason": "empty-pool",
        "count": 0, "omitted": 0, "candidate_ids": [],
        "tokens_used": 0, "tokens_budget": 1500, "arms": {},
    }))
"""


def _flag_value(argv, flag):
    """Value carried by `flag` in argv, either "flag V" or "flag=V"."""
    for i, item in enumerate(argv):
        if item == flag and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith(flag + "="):
            return item[len(flag) + 1:]
    return None


def _run_body(event: dict) -> tuple[int, list[list[str]]]:
    """Drive the recall body in user_prompt mode; return (rc, invocations)."""
    stub_path = Path(_TMP) / "stub-store.py"
    if not stub_path.is_file():
        stub_path.write_text(_STUB_STORE, encoding="utf-8")
    capture_path = Path(_TMP) / ("argv-%s.jsonl" % uuid.uuid4().hex)
    env = {k: v for k, v in os.environ.items()
           if k != "ZMEM_TEST_CAPTURE"}
    env.update({
        "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
        "ZMEM_DATA": _TMP,
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_NAMESPACE": NAMESPACE,
        "ZMEM_HOST": "claude",
        "ZMEM_QUERY_CONTEXT": "0",  # keep the rewrite child off the capture
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
        "ZMEM_TEST_CAPTURE": str(capture_path),
        "PYTHONUTF8": "1",
    })
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(BODY), str(stub_path),
         NAMESPACE, "25000", "user_prompt"],
        input=json.dumps(event), capture_output=True, text=True,
        env=env, timeout=120,
    )
    invocations = []
    if capture_path.is_file():
        for line in capture_path.read_text(encoding="utf-8").splitlines():
            try:
                invocations.append(json.loads(line))
            except ValueError:
                pass
    return result.returncode, invocations


def _recall_invocation(invocations):
    for argv in invocations:
        if argv and argv[0] == "recall" and _flag_value(argv, "--query"):
            return argv
    return None


_HERMES = None


def _hermes():
    """Import hermes-plugin/__init__.py once, with the Hermes ABC stubbed
    (the tests/test_hermes_prefetch.py _load_provider pattern)."""
    global _HERMES
    if _HERMES is not None:
        return _HERMES
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal Hermes host ABC stand-in
        pass

    memory_provider.MemoryProvider = MemoryProvider
    memory_provider.RecallStatus = types.SimpleNamespace  # name-only import
    agent.memory_provider = memory_provider
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", memory_provider)
    spec = importlib.util.spec_from_file_location(
        "zmem_pts389_hermes", HERMES_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_pts389_hermes"] = module
    spec.loader.exec_module(module)
    _HERMES = module
    return module


def _fixture_envelope() -> dict:
    """One complete valid #158 envelope from the committed prefetch cases."""
    cases = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "hermes" /
         "prefetch_cases.json").read_text(encoding="utf-8"))
    for case in cases:
        if case.get("query") == "project alpha":
            envelope = case["envelope"]
            if envelope.get("reason") != "already-delivered":
                return envelope
    raise AssertionError("prefetch_cases.json lacks a plain project-alpha "
                         "envelope")


class StubTransport:
    """Recording transport for the provider prefetch/search surfaces."""

    def __init__(self, envelope=None):
        self.envelope = envelope if envelope is not None \
            else _fixture_envelope()
        self.calls = []
        self.search_calls = []

    def prefetch(self, query, *, namespace, session_id, moment, ops_tokens,
                 lane, prompt_timestamp=None):
        import copy
        self.calls.append({
            "query": query, "namespace": namespace, "session_id": session_id,
            "moment": moment, "ops_tokens": list(ops_tokens), "lane": lane,
            "prompt_timestamp": prompt_timestamp,
        })
        return copy.deepcopy(self.envelope)

    def search(self, query, *, namespace=None, limit=None,
               prompt_timestamp=None):
        self.search_calls.append({
            "query": query, "namespace": namespace, "limit": limit,
            "prompt_timestamp": prompt_timestamp,
        })
        return {"ok": True, "results": []}


class _OldShapeTransport:
    """A transport predating ``prompt_timestamp`` (mixed-version pin): the
    kwarg is an unexpected keyword argument (TypeError, exactly like a real
    older signature); the historical call shape serves normally."""

    def __init__(self, envelope):
        import copy
        self._copy = copy.deepcopy
        self.envelope = envelope
        self.prefetch_calls = []
        self.search_calls = []

    def prefetch(self, query, *, namespace, session_id, moment, ops_tokens,
                 lane, **unexpected):
        self.prefetch_calls.append(dict(unexpected))
        if "prompt_timestamp" in unexpected:
            raise TypeError("prefetch() got an unexpected keyword "
                            "argument 'prompt_timestamp'")
        return self._copy(self.envelope)

    def search(self, query, *, namespace=None, limit=None, **unexpected):
        self.search_calls.append(dict(unexpected))
        if "prompt_timestamp" in unexpected:
            raise TypeError("search() got an unexpected keyword "
                            "argument 'prompt_timestamp'")
        return {"ok": True, "results": []}


class HookLegTest(unittest.TestCase):
    """The recall body forwards the payload's prompt clock (probe legs)."""

    def _event(self, **extra):
        event = {"prompt": TEMPORAL_PROMPT, "session_id": SESSION_ID,
                 "cwd": str(REPO_ROOT)}
        event.update(extra)
        return event

    def test_forwards_payload_timestamp_despite_divergent_now(self):
        # Both spellings carry the SAME value (probe leg 1) while
        # ZMEM_TEST_NOW diverges: the payload's timestamp is what travels.
        os.environ["ZMEM_TEST_NOW"] = DIVERGENT_NOW
        try:
            rc, invocations = _run_body(self._event(
                timestamp=PROMPT_TS, prompt_timestamp=PROMPT_TS))
        finally:
            os.environ.pop("ZMEM_TEST_NOW", None)
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv, invocations)
        self.assertEqual(
            _flag_value(recall_argv, "--prompt-timestamp"), PROMPT_TS)
        self.assertNotIn(DIVERGENT_NOW, recall_argv)

    def test_timestamp_only_forwards(self):
        rc, invocations = _run_body(self._event(timestamp=PROMPT_TS))
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv)
        self.assertEqual(
            _flag_value(recall_argv, "--prompt-timestamp"), PROMPT_TS)

    def test_prompt_timestamp_only_forwards(self):
        rc, invocations = _run_body(self._event(prompt_timestamp=PROMPT_TS))
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv)
        self.assertEqual(
            _flag_value(recall_argv, "--prompt-timestamp"), PROMPT_TS)

    def test_prompt_timestamp_wins_over_timestamp(self):
        rc, invocations = _run_body(self._event(
            timestamp=ALT_TS, prompt_timestamp=PROMPT_TS))
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv)
        self.assertEqual(
            _flag_value(recall_argv, "--prompt-timestamp"), PROMPT_TS)

    def test_absent_payload_forwards_no_flag(self):
        rc, invocations = _run_body(self._event())
        self.assertEqual(rc, 0)
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv)
        self.assertIsNone(_flag_value(recall_argv, "--prompt-timestamp"))

    def test_invalid_payload_forwards_no_flag_no_error(self):
        rc, invocations = _run_body(self._event(
            timestamp="not-a-timestamp", prompt_timestamp="yesterday"))
        self.assertEqual(rc, 0)
        recall_argv = _recall_invocation(invocations)
        self.assertIsNotNone(recall_argv)
        self.assertIsNone(_flag_value(recall_argv, "--prompt-timestamp"))
        # A non-string value (e.g. a host epoch float) drops the same way.
        rc2, invocations2 = _run_body(self._event(prompt_timestamp=1757512200))
        self.assertEqual(rc2, 0)
        self.assertIsNone(_flag_value(
            _recall_invocation(invocations2), "--prompt-timestamp"))


class TemporalWindowTest(unittest.TestCase):
    """Explicit prompt time beats a divergent ZMEM_TEST_NOW (probe leg 2)."""

    def test_explicit_timestamp_beats_divergent_test_now(self):
        from storelib import recall as recall_mod
        try:
            os.environ["ZMEM_TEST_NOW"] = DIVERGENT_NOW
            threaded = recall_mod._temporal_window(TEMPORAL_PROMPT, PROMPT_TS)
            os.environ["ZMEM_TEST_NOW"] = PROMPT_TS
            env_ts = recall_mod._temporal_window(TEMPORAL_PROMPT, None)
            os.environ["ZMEM_TEST_NOW"] = DIVERGENT_NOW
            env_div = recall_mod._temporal_window(TEMPORAL_PROMPT, None)
        finally:
            os.environ.pop("ZMEM_TEST_NOW", None)
        self.assertEqual(threaded, env_ts)
        self.assertNotEqual(threaded, env_div)
        self.assertNotEqual(threaded, ("", ""))


class HermesProviderTest(unittest.TestCase):
    """Provider surfaces accept and forward prompt_timestamp."""

    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("ZMEM_INJECT", "ZMEM_NAMESPACE", "ZMEM_HERMES_MODE",
                        "ZMEM_HERMES_DEADLINE_S", "ZMEM_MCP_URL")
        }
        os.environ["ZMEM_NAMESPACE"] = NAMESPACE
        for key in ("ZMEM_INJECT", "ZMEM_HERMES_MODE",
                    "ZMEM_HERMES_DEADLINE_S", "ZMEM_MCP_URL"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _provider(self, transport=None):
        mod = _hermes()
        spec = importlib.util.spec_from_file_location(
            "zmem_fake_executor_pts", REPO_ROOT / "tests" / "support" /
            "fake_executor.py")
        fake = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("zmem_fake_executor_pts", fake)
        spec.loader.exec_module(fake)
        scheduler = fake.FakeExecutor()
        provider = mod.ZmemMemoryProvider(
            scheduler=scheduler, deadline=fake.FakeExecutor(),
            clock=scheduler.now)
        provider._session_id = SESSION_ID
        provider._namespace = NAMESPACE
        provider._initialized = True
        if transport is not None:
            provider._transport = transport
        return mod, provider, scheduler

    def test_passive_store_args_forwards_valid_timestamp(self):
        mod = _hermes()
        args = mod._passive_store_args(
            "recall", query=TEMPORAL_PROMPT, namespace=NAMESPACE,
            limit=5, global_limit=3, session_id=SESSION_ID,
            moment="user_prompt", lane="hermes-provider",
            prompt_timestamp=PROMPT_TS)
        self.assertEqual(_flag_value(args, "--prompt-timestamp"), PROMPT_TS)

    def test_passive_store_args_drops_invalid_and_non_recall(self):
        mod = _hermes()
        for bad in (None, "not-a-timestamp", 1757512200, "  "):
            args = mod._passive_store_args(
                "recall", query="q", namespace=NAMESPACE, limit=5,
                global_limit=3, session_id=SESSION_ID,
                moment="user_prompt", lane="hermes-provider",
                prompt_timestamp=bad)
            self.assertIsNone(_flag_value(args, "--prompt-timestamp"), bad)
        # The recent parser has no flag: a valid value forwards nothing.
        recent = mod._passive_store_args(
            "recent", query="", namespace=NAMESPACE, limit=3, global_limit=2,
            session_id=SESSION_ID, moment="session_start",
            lane="hermes-provider", prompt_timestamp=PROMPT_TS)
        self.assertIsNone(_flag_value(recent, "--prompt-timestamp"))

    def test_search_schema_carries_property(self):
        mod = _hermes()
        schema = mod._SEARCH_SCHEMA
        self.assertEqual(schema["parameters"]["properties"]
                         ["prompt_timestamp"]["type"], "string")
        self.assertNotIn(
            "prompt_timestamp", schema["parameters"]["required"])
        # The exposed tool-schema list carries the same dict.
        listed = {tool["name"]: tool for tool in mod._TOOL_SCHEMAS}
        self.assertIn("prompt_timestamp",
                      listed["zmem_search"]["parameters"]["properties"])

    def test_tool_search_local_forwards_flag(self):
        mod, provider, _ = self._provider()
        captured = {}

        def _fake_run_store(cli_args, **kwargs):
            captured["argv"] = list(cli_args)
            return {"ok": True, "stdout": json.dumps({"results": []}),
                    "stderr": "", "returncode": 0}

        with mock.patch.object(mod, "_run_store", _fake_run_store):
            provider._tool_search({"query": "alpha lessons",
                                   "prompt_timestamp": PROMPT_TS})
        self.assertEqual(
            _flag_value(captured["argv"], "--prompt-timestamp"), PROMPT_TS)

    def test_tool_search_local_invalid_dropped(self):
        mod, provider, _ = self._provider()
        captured = {}

        def _fake_run_store(cli_args, **kwargs):
            captured["argv"] = list(cli_args)
            return {"ok": True, "stdout": json.dumps({"results": []}),
                    "stderr": "", "returncode": 0}

        with mock.patch.object(mod, "_run_store", _fake_run_store):
            provider._tool_search({"query": "alpha lessons",
                                   "prompt_timestamp": "garbage"})
        self.assertIsNone(_flag_value(captured["argv"], "--prompt-timestamp"))

    def test_tool_search_remote_forwards_parameter(self):
        mod, provider, _ = self._provider(transport=StubTransport())
        provider._mode = mod._transport.TransportMode.mcp
        provider._tool_search({"query": "alpha lessons",
                               "prompt_timestamp": PROMPT_TS})
        call = provider._transport.search_calls[-1]
        self.assertEqual(call["prompt_timestamp"], PROMPT_TS)

    def test_prefetch_threads_and_cache_key_differentiates(self):
        transport = StubTransport()
        mod, provider, _ = self._provider(transport=transport)
        provider.prefetch("project alpha", session_id=SESSION_ID,
                          prompt_timestamp=PROMPT_TS)
        self.assertEqual(transport.calls[0]["prompt_timestamp"], PROMPT_TS)
        # A differently-timed prefetch is a DIFFERENT cache entry: the live
        # transport runs again (a wrong-window cache hit is impossible).
        provider.prefetch("project alpha", session_id=SESSION_ID,
                          prompt_timestamp=ALT_TS)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]["prompt_timestamp"], ALT_TS)
        # The same timestamp twice is a fresh cache hit: no third call.
        provider.prefetch("project alpha", session_id=SESSION_ID,
                          prompt_timestamp=PROMPT_TS)
        self.assertEqual(len(transport.calls), 2)
        # No timestamp threads nothing (mixed-version call shape).
        provider._prefetch_cache._entries.clear()
        provider._prefetch_cache._inserted_at.clear()
        provider.prefetch("project alpha", session_id=SESSION_ID)
        self.assertIsNone(transport.calls[2]["prompt_timestamp"])

    def test_queue_prefetch_job_carries_submit_time_clock(self):
        mod = _hermes()
        spec = importlib.util.spec_from_file_location(
            "zmem_fake_executor_pts2", REPO_ROOT / "tests" / "support" /
            "fake_executor.py")
        fake = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("zmem_fake_executor_pts2", fake)
        spec.loader.exec_module(fake)

        submissions = []

        class RecordingExecutor(fake.FakeExecutor):
            def submit(self, fn, delay_s=None):
                submissions.append(fn)
                return super().submit(fn, delay_s=delay_s)

        transport = StubTransport()
        provider = mod.ZmemMemoryProvider(
            scheduler=RecordingExecutor(), deadline=fake.FakeExecutor(),
            clock=lambda: 0.0)
        provider._session_id = SESSION_ID
        provider._namespace = NAMESPACE
        provider._initialized = True
        provider._transport = transport
        provider.queue_prefetch("project alpha", session_id=SESSION_ID,
                                prompt_timestamp=PROMPT_TS)
        self.assertEqual(len(submissions), 1)
        submissions[0]()  # run the queued job: the closure carries the clock
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["prompt_timestamp"], PROMPT_TS)

    def test_pre_llm_call_threads_prompt_timestamp(self):
        transport = StubTransport()
        mod, provider, _ = self._provider(transport=transport)
        provider.pre_llm_call(session_id=SESSION_ID,
                              user_message="project alpha notes",
                              prompt_timestamp=PROMPT_TS)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["prompt_timestamp"], PROMPT_TS)
        # Absent → the kwarg threads nothing.
        provider.pre_llm_call(session_id=SESSION_ID,
                              user_message="project alpha notes")
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(transport.calls[1]["prompt_timestamp"])

    def test_pre_llm_call_offline_serves_snapshot_once_with_timestamp(self):
        # IR-1 pin: the hub-unreachable fallback serves the offline snapshot
        # EXACTLY ONCE and with the prompt timestamp threaded (the
        # double-serve first-without-then-with shape is dead).
        transport = StubTransport(
            envelope={"ok": False, "reason": "hub-unreachable"})
        mod, provider, _ = self._provider(transport=transport)
        serves = []

        def _counting_serve(query, namespace, session_id,
                            prompt_timestamp=None):
            serves.append({"query": query, "namespace": namespace,
                           "session_id": session_id,
                           "prompt_timestamp": prompt_timestamp})
            return None  # no snapshot: the degrade path, the serve counted

        with mock.patch.object(mod, "_offline_snapshot_envelope",
                               _counting_serve):
            provider.pre_llm_call(session_id=SESSION_ID,
                                  user_message="project alpha notes",
                                  prompt_timestamp=PROMPT_TS)
        self.assertEqual(len(serves), 1)
        self.assertEqual(serves[0]["prompt_timestamp"], PROMPT_TS)

    def test_mixed_version_transport_typeerror_retries_once_warned(self):
        # Mixed-version guard: a transport predating prompt_timestamp raises
        # TypeError on the kwarg; the guarded call sites retry once WITHOUT
        # it (the lane still serves) and warn exactly once per process.
        transport = _OldShapeTransport(_fixture_envelope())
        mod, provider, _ = self._provider(transport=transport)
        with mock.patch.object(mod, "_PROMPT_TS_TRANSPORT_WARNED", False), \
                mock.patch.object(mod.logger, "warning") as warned:
            provider.prefetch("project alpha", session_id=SESSION_ID,
                              prompt_timestamp=PROMPT_TS)
            # The retry served the envelope: kwarg attempt, then the
            # historical call shape.
            self.assertEqual(
                transport.prefetch_calls, [{"prompt_timestamp": PROMPT_TS},
                                           {}])
            # One-shot: a second guarded failure never re-warns.
            provider.prefetch("project alpha two", session_id=SESSION_ID,
                              prompt_timestamp=ALT_TS)
            self.assertEqual(len(transport.prefetch_calls), 4)
            # The remote search lane degrades the same way.
            provider._mode = mod._transport.TransportMode.mcp
            provider._tool_search({"query": "alpha lessons",
                                   "prompt_timestamp": PROMPT_TS})
        self.assertEqual(transport.search_calls, [{"prompt_timestamp":
                                                   PROMPT_TS}, {}])
        ts_warnings = [c for c in warned.call_args_list
                       if "prompt_timestamp" in str(c.args[0] if c.args
                                                    else c)]
        self.assertEqual(len(ts_warnings), 1)


class FleetTransportTest(unittest.TestCase):
    """The fleet transport surfaces carry the prompt clock."""

    def _capturing_http(self):
        from fleet import transport as transport_mod

        http = transport_mod.McpHttp.__new__(transport_mod.McpHttp)
        http.tool_calls = []
        http._run_tool = lambda tool, arguments: (
            http.tool_calls.append((tool, dict(arguments)))
            or {"ok": True, "results": []})
        return http, transport_mod

    def test_mcp_http_search_and_prefetch_payload(self):
        http, _ = self._capturing_http()
        http.search("q", prompt_timestamp=PROMPT_TS)
        tool, args = http.tool_calls[-1]
        self.assertEqual(tool, "search")
        self.assertEqual(args.get("prompt_timestamp"), PROMPT_TS)
        http.prefetch("q", namespace=NAMESPACE, session_id=SESSION_ID,
                      moment="user_prompt", ops_tokens=[],
                      prompt_timestamp=PROMPT_TS)
        tool, args = http.tool_calls[-1]
        self.assertEqual(tool, "prefetch")
        self.assertEqual(args.get("prompt_timestamp"), PROMPT_TS)

    def test_mcp_http_invalid_timestamp_not_sent(self):
        http, _ = self._capturing_http()
        http.search("q", prompt_timestamp="garbage")
        _, args = http.tool_calls[-1]
        self.assertNotIn("prompt_timestamp", args)
        http.prefetch("q", namespace=NAMESPACE, session_id=SESSION_ID,
                      moment="user_prompt", ops_tokens=[],
                      prompt_timestamp="garbage")
        _, args = http.tool_calls[-1]
        self.assertNotIn("prompt_timestamp", args)

    def test_local_subprocess_prefetch_argv(self):
        from fleet import transport as transport_mod

        class _CaptureExecutor:
            def __init__(self):
                self.commands = []

            def run(self, operation, deadline_s):
                self.commands.append(list(operation._cmd))
                return ""

        executor = _CaptureExecutor()
        local = transport_mod.LocalSubprocess(
            store_py=str(STORE_PY), executor=executor, deadline_s=6.0)
        local.prefetch("q", namespace=NAMESPACE, session_id=SESSION_ID,
                       moment="user_prompt", ops_tokens=[],
                       prompt_timestamp=PROMPT_TS)
        argv = executor.commands[0]
        self.assertEqual(_flag_value(argv, "--prompt-timestamp"), PROMPT_TS)
        # Absent/invalid forward nothing.
        local.prefetch("q", namespace=NAMESPACE, session_id=SESSION_ID,
                       moment="user_prompt", ops_tokens=[])
        self.assertIsNone(
            _flag_value(executor.commands[1], "--prompt-timestamp"))
        local.prefetch("q", namespace=NAMESPACE, session_id=SESSION_ID,
                       moment="user_prompt", ops_tokens=[],
                       prompt_timestamp="garbage")
        self.assertIsNone(
            _flag_value(executor.commands[2], "--prompt-timestamp"))


class BrokerTest(unittest.TestCase):
    """_handle_read extracts (precedence + strict ISO) and forwards."""

    def _stub_http(self):
        stub = types.SimpleNamespace(prefetch_calls=[])

        def _prefetch(query, *, namespace, session_id, moment, ops_tokens,
                      lane, reserve=False, deadline_ms=None,
                      consume_tasktext=False, prompt_timestamp=None):
            stub.prefetch_calls.append({
                "query": query, "namespace": namespace,
                "session_id": session_id, "moment": moment,
                "prompt_timestamp": prompt_timestamp,
            })
            return {"rendered": "row", "reason": "ok"}

        stub.prefetch = _prefetch
        return stub

    def _handle(self, http, payload):
        from fleet import broker as broker_mod
        # A dedicated env dir with a FRESH snapshot meta keeps the
        # post-read best-effort refresh from firing on the stub http.
        broker_env = Path(_TMP) / "broker-env"
        meta_dir = broker_env / "broker"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "snapshot.meta.json").write_text(
            json.dumps({"issued_at": time.time()}), encoding="utf-8")
        env = {"ZMEM_HOST": "claude", "ZMEM_NAMESPACE": NAMESPACE,
               "ZMEM_DATA": str(broker_env),
               "ZMEM_SNAPSHOT_REFRESH_S": "3600",
               "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models")}
        return broker_mod._handle_read(
            http, env, "UserPromptSubmit", payload, time.monotonic())

    def test_forwards_valid_and_applies_precedence(self):
        http = self._stub_http()
        payload = {"prompt": TEMPORAL_PROMPT, "session_id": SESSION_ID,
                   "cwd": str(REPO_ROOT),
                   "timestamp": PROMPT_TS, "prompt_timestamp": PROMPT_TS}
        self._handle(http, payload)
        self.assertEqual(http.prefetch_calls[-1]["prompt_timestamp"],
                         PROMPT_TS)
        # prompt_timestamp wins when both are present and valid.
        http2 = self._stub_http()
        self._handle(http2, {"prompt": TEMPORAL_PROMPT,
                             "session_id": SESSION_ID,
                             "timestamp": ALT_TS,
                             "prompt_timestamp": PROMPT_TS})
        self.assertEqual(http2.prefetch_calls[-1]["prompt_timestamp"],
                         PROMPT_TS)

    def test_invalid_or_absent_forwards_none(self):
        http = self._stub_http()
        self._handle(http, {"prompt": TEMPORAL_PROMPT,
                            "session_id": SESSION_ID,
                            "timestamp": "garbage",
                            "prompt_timestamp": 1757512200})
        self.assertIsNone(http.prefetch_calls[-1]["prompt_timestamp"])
        http_absent = self._stub_http()
        self._handle(http_absent, {"prompt": TEMPORAL_PROMPT,
                                   "session_id": SESSION_ID})
        self.assertIsNone(
            http_absent.prefetch_calls[-1]["prompt_timestamp"])


class StoreCliTest(unittest.TestCase):
    """The prefetch parser accepts --prompt-timestamp end to end."""

    def _run_cli(self, *extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_TEST_")}
        scratch = tempfile.mkdtemp(prefix="zmem-389-cli-", dir=_TMP)
        env.update({
            "ZMEM_STORE": os.path.join(scratch, "store.sqlite"),
            "ZMEM_DATA": scratch,
            "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_EMBED_PROFILE": "minilm",
            "ZMEM_LINK_THRESHOLD": "1.01",
            "PYTHONUTF8": "1",
        })
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "prefetch",
             "--query", "outage during last week",
             "--namespace", NAMESPACE, "--session-id", SESSION_ID,
             "--moment", "user_prompt", "--json", *extra],
            capture_output=True, text=True, env=env, timeout=180)

    def test_prefetch_flag_parses_and_dispatch_runs(self):
        result = self._run_cli("--prompt-timestamp", PROMPT_TS)
        self.assertEqual(result.returncode, 0,
                         result.stderr[-800:] if result.returncode else "")
        try:
            envelope = json.loads(result.stdout)
        except ValueError:
            self.fail("prefetch stdout was not JSON: %r" % result.stdout[:200])
        self.assertIn("rendered", envelope)

    def test_prefetch_invalid_flag_is_argparse_error(self):
        result = self._run_cli("--prompt-timestamp", "garbage")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ISO-8601", result.stderr)


class InjectSeamTest(unittest.TestCase):
    """select_and_budget_for_injection threads the clock to recall_memory."""

    def test_prompt_timestamp_reaches_recall_memory(self):
        from storelib import inject as inject_mod
        from storelib import recall as recall_mod
        from storelib import schema as schema_mod
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            captured = {}

            def _capture(conn, **kwargs):
                captured.update(kwargs)
                return []

            with mock.patch.object(recall_mod, "recall_memory", _capture):
                inject_mod.select_and_budget_for_injection(
                    conn, query="outage during last week",
                    namespace="test", moment="user_prompt",
                    session_id=SESSION_ID, data_dir=_TMP,
                    prompt_timestamp=PROMPT_TS)
            self.assertEqual(captured.get("prompt_timestamp"), PROMPT_TS)
            # The default threads None (the ZMEM_TEST_NOW/now seam).
            with mock.patch.object(recall_mod, "recall_memory", _capture):
                inject_mod.select_and_budget_for_injection(
                    conn, query="outage during last week",
                    namespace="test", moment="user_prompt",
                    session_id=SESSION_ID, data_dir=_TMP)
            self.assertIsNone(captured.get("prompt_timestamp"))
        finally:
            conn.close()


class StoreChildEndToEndTest(unittest.TestCase):
    """FC-1 part 2 — the committed end-to-end pin: the REAL store child,
    driven with the hook's exact for-injection argv shape, resolves the
    temporal window from the threaded ``--prompt-timestamp`` even while the
    child's own ZMEM_TEST_NOW points years away."""

    def _child_env(self, scratch):
        # The StoreCliTest._run_cli subprocess-env pattern (ambient minus
        # ZMEM_TEST_*), plus the model-absent pins and the divergent child
        # clock the threaded timestamp must beat.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_TEST_")}
        env.update({
            "ZMEM_STORE": os.path.join(scratch, "store.sqlite"),
            "ZMEM_DATA": scratch,
            "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_EMBED_PROFILE": "minilm",
            "ZMEM_LINK_THRESHOLD": "1.01",
            "ZMEM_TEST_NOW": E2E_DIVERGENT_NOW,
            "PYTHONUTF8": "1",
        })
        return env

    def test_threaded_timestamp_beats_divergent_child_now(self):
        scratch = tempfile.mkdtemp(prefix="zmem-389-promptts-e2e-", dir=_TMP)
        env = self._child_env(scratch)
        # Seed one event whose occurrence window (2026-09-01..03) overlaps
        # "last week" ONLY relative to PROMPT_TS (2026-09-10) — never
        # relative to the divergent 2030 child clock.
        seed = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "add",
             "--namespace", NAMESPACE, "--type", "event",
             "--content", TEMPORAL_PROMPT, "--signal", "test",
             "--occurred-start", "2026-09-01T08:00:00Z",
             "--occurred-end", "2026-09-03T08:00:00Z"],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(seed.returncode, 0, seed.stderr[-800:])
        base_argv = [
            sys.executable, "-X", "utf8", str(STORE_PY),
            "recall",
            "--query", TEMPORAL_PROMPT,
            "--namespace", NAMESPACE,
            "--no-bump", "--for-injection", "--json",
            "--session-id", SESSION_ID,
            "--moment", "user_prompt",
            "--lane", "claude",
            "--limit", "5",
        ]
        # Threaded leg: the payload's prompt clock wins over the child's
        # divergent ZMEM_TEST_NOW — the temporal arm finds the event.
        threaded = subprocess.run(
            base_argv + ["--prompt-timestamp", PROMPT_TS],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(threaded.returncode, 0, threaded.stderr[-800:])
        envelope = json.loads(threaded.stdout)
        temporal_arm = envelope["arms"]["temporal"]
        self.assertGreaterEqual(temporal_arm["pre"], 1)
        self.assertGreaterEqual(temporal_arm["post"], 1)
        # Control leg: the SAME divergent now WITHOUT --prompt-timestamp
        # resolves "last week" to Dec 2029 — the event is out of window and
        # the temporal arm stays empty.
        control = subprocess.run(
            base_argv, capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(control.returncode, 0, control.stderr[-800:])
        control_envelope = json.loads(control.stdout)
        self.assertEqual(control_envelope["arms"]["temporal"]["pre"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
