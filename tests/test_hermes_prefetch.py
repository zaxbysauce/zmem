"""Issue #162 — Hermes provider prefetch cache, recall_status, and session
invalidation tests.

``HermesPrefetchTest`` carries the eight issue-named methods; the
``_ProviderBoundaryTest`` companion pins the definition-of-done boundary
(the no-storelib token list and the distinct scheduler/deadline seam).  All
provider tests inject a recording fake transport, the #160 ``FakeExecutor``
(one instance as the Scheduler, a separate instance as the DeadlineExecutor
— the provider never assigns one object to both), and the fake's ``now`` as
the provider clock, so ``advance`` drives cache freshness from one virtual
clock.  No test creates a real thread, socket, subprocess, or operator
store: the scratch environment is pinned at module import (the
tests/test_hermes_transport.py pattern) and the reset test's ledger-clear is
recorded through a patched module-level ``_run_store``.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import atexit
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-162-prefetch-"))
atexit.register(shutil.rmtree, str(_SCRATCH), ignore_errors=True)
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_NAMESPACE"] = "project:fixture"

_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hermes"
_SUPPORT_DIR = REPO_ROOT / "tests" / "support"


def _load_fake_executor():
    spec = importlib.util.spec_from_file_location(
        "zmem_fake_executor_162", _SUPPORT_DIR / "fake_executor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_fake_executor_162"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider(name="zmem_hermes_162"):
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_hermes_transport.py)
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


def _fixture_cases():
    return json.loads(
        (_FIXTURE_DIR / "prefetch_cases.json").read_text(encoding="utf-8"))


def _fixture_expected():
    return json.loads(
        (_FIXTURE_DIR / "prefetch_cases.expected.json").read_text(
            encoding="utf-8"))


def _case_envelopes():
    return {case["query"]: case["envelope"] for case in _fixture_cases()}


class RecordingExecutor(_load_fake_executor().FakeExecutor):
    """FakeExecutor that records every Scheduler submission."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submissions = []

    def submit(self, fn, delay_s=None):
        self.submissions.append(fn)
        return super().submit(fn, delay_s=delay_s)


class FakeTransport:
    """Recording fake transport with a simulated store-side ledger.

    The ledger holds one ``(session_id, fingerprint)`` row per delivering
    call, mirroring the #158 store-side delivery ledger at provider-test
    granularity: a repeat call for a delivered row returns the
    already-delivered envelope (the ``project-beta`` fixture shape).
    """

    def __init__(self, envelopes=None):
        self.envelopes = envelopes if envelopes is not None \
            else _case_envelopes()
        self.ledger = []
        self.calls = []

    def prefetch(self, query, *, namespace, session_id, moment, ops_tokens,
                 lane):
        self.calls.append({"query": query, "namespace": namespace,
                           "session_id": session_id, "moment": moment,
                           "ops_tokens": list(ops_tokens), "lane": lane})
        normalized = " ".join(str(query).split())[:500]
        payload = (b"zmem-prefetch-v1\0" + normalized.encode("utf-8")
                   + b"\0" + namespace.encode("utf-8")
                   + b"\0user_prompt\0hermes-provider")
        fingerprint = hashlib_sha256(payload)
        row = (session_id, fingerprint)
        if row in self.ledger:
            envelope = self.envelopes["project beta"]
        else:
            self.ledger.append(row)
            envelope = self.envelopes.get(
                query, self.envelopes["no match"])
        return copy.deepcopy(envelope)


def hashlib_sha256(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


class HermesPrefetchTest(unittest.TestCase):
    """The eight issue-named acceptance tests."""

    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("ZMEM_INJECT", "ZMEM_NAMESPACE",
                        "ZMEM_HERMES_DEADLINE_S", "ZMEM_HERMES_MODE",
                        "ZMEM_MCP_URL")
        }
        os.environ["ZMEM_NAMESPACE"] = "project:fixture"
        for key in ("ZMEM_INJECT", "ZMEM_HERMES_DEADLINE_S",
                    "ZMEM_HERMES_MODE", "ZMEM_MCP_URL"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _provider(self, scheduler=None, deadline=None, transport=None):
        mod = _load_provider("zmem_hermes_162_%s" % self.id())
        scheduler = scheduler if scheduler is not None \
            else RecordingExecutor()
        deadline = deadline if deadline is not None else \
            _load_fake_executor().FakeExecutor()
        provider = mod.ZmemMemoryProvider(scheduler=scheduler,
                                          deadline=deadline,
                                          clock=scheduler.now)
        provider._session_id = "sid-a"
        provider._namespace = "project:fixture"
        provider._initialized = True
        if transport is not None:
            provider._transport = transport
        return mod, provider, scheduler, deadline

    def test_queue_prefetch_deduplicates_key(self):
        mod, provider, scheduler, deadline = self._provider(
            transport=FakeTransport())
        expected_fingerprint = next(
            case["fingerprint"] for case in _fixture_expected()
            if case["case"] == "project-alpha")

        provider.queue_prefetch("project alpha", session_id="sid-a")
        provider.queue_prefetch("project alpha", session_id="sid-a")

        self.assertEqual(len(scheduler.submissions), 1)
        self.assertEqual(set(provider._pending_keys),
                         {("sid-a", expected_fingerprint)})

        # Second AC1 half: after the job runs, a fresh cache hit returns the
        # cached rendered field with zero further transport calls.
        scheduler.submissions[0]()
        calls_after_job = len(provider._transport.calls)
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(rendered,
                         _fixture_expected()[1]["rendered"])
        self.assertEqual(len(provider._transport.calls), calls_after_job)
        self.assertEqual(provider.recall_status().count, 3)

    def test_fresh_cache_hit_skips_transport(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)
        seed = _case_envelopes()["project alpha"]
        provider._prefetch_cache.put(
            ("sid-a", _fixture_expected()[1]["fingerprint"]), seed)

        def _no_subprocess(*args, **kwargs):
            raise AssertionError("fresh cache hit must not spawn a subprocess")

        def _no_socket(*args, **kwargs):
            raise AssertionError("fresh cache hit must not open a socket")

        saved = (subprocess.Popen, socket.socket, socket.create_connection,
                 socket.getaddrinfo)
        subprocess.Popen = _no_subprocess
        socket.socket = _no_socket
        socket.create_connection = _no_socket
        socket.getaddrinfo = _no_socket
        try:
            rendered = provider.prefetch("project alpha", session_id="sid-a")
        finally:
            (subprocess.Popen, socket.socket, socket.create_connection,
             socket.getaddrinfo) = saved
        self.assertEqual(rendered, _fixture_expected()[1]["rendered"])
        self.assertEqual(transport.calls, [])

    def test_stale_cache_runs_live_recall(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)
        first = _case_envelopes()["project alpha"]
        key = ("sid-a", _fixture_expected()[1]["fingerprint"])
        provider._prefetch_cache.put(key, first)

        replacement = copy.deepcopy(first)
        replacement["rendered"] = first["rendered"].replace(
            "alpha one", "alpha one (live)")
        transport.envelopes["project alpha"] = replacement

        scheduler.advance(30.01)
        self.assertIsNone(provider._prefetch_cache.get(key))

        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(rendered, replacement["rendered"])
        cached = provider._prefetch_cache.get(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["rendered"], replacement["rendered"])

    def test_stalled_background_job_does_not_block_prefetch(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)
        deadline.pending_completion = 6.0

        thread_watcher = {"count": 0}
        real_thread = threading.Thread

        class CountingThread(real_thread):
            def __init__(self, *args, **kwargs):
                thread_watcher["count"] += 1
                super().__init__(*args, **kwargs)

        threading.Thread = CountingThread
        try:
            provider.queue_prefetch("project alpha", session_id="sid-a")
            rendered = provider.prefetch("project alpha",
                                         session_id="sid-a")
        finally:
            threading.Thread = real_thread

        self.assertEqual(rendered, _fixture_expected()[1]["rendered"])
        self.assertTrue(rendered.endswith("<<<END_ZMEM_UNTRUSTED_FENCE>>>\n"))
        self.assertEqual(deadline.now(), 6.0)
        self.assertEqual(thread_watcher["count"], 0)
        self.assertEqual(len(scheduler.submissions), 1)

    def test_recall_status_zero_then_three_then_zero(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)

        # Review TC-07: use the ACTIVE session ("sid-a") — a foreign
        # session_id is rejected by the pairing gate before any transport
        # call, which made this leg vacuous (the count-0 assertion read the
        # constructor default).
        first = provider.prefetch("no match", session_id="sid-a")
        self.assertEqual(first, "")
        self.assertEqual(provider.recall_status().count, 0)

        second = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(second, _fixture_expected()[1]["rendered"])
        self.assertEqual(provider.recall_status().count, 3)

        third = provider.prefetch("project beta", session_id="sid-a")
        self.assertEqual(third, "")
        self.assertEqual(provider.recall_status().count, 0)

    def test_rewind_clears_cache_preserves_store_ledger(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)
        store_calls = []
        mod._run_store = lambda argv, **kwargs: (
            store_calls.append(list(argv)) or {"ok": True, "stdout": "",
                                               "stderr": "", "returncode": 0})

        first = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(first, _fixture_expected()[1]["rendered"])
        self.assertEqual(len(transport.ledger), 1)

        provider.on_session_switch("sid-a", rewound=True)

        self.assertEqual(store_calls, [])
        self.assertEqual(provider.recall_status().count, 0)
        cached = provider._prefetch_cache.get(
            ("sid-a", _fixture_expected()[1]["fingerprint"]))
        self.assertIsNone(cached)

        second = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(second, "")
        self.assertEqual(provider.recall_status().count, 0)
        self.assertEqual(len(transport.ledger), 1)
        self.assertEqual(len(transport.calls), 2)
        beta = _case_envelopes()["project beta"]
        self.assertEqual(transport.calls[-1]["session_id"], "sid-a")

    def test_reset_clears_store_ledger_and_redelivers(self):
        transport = FakeTransport()
        mod, provider, scheduler, deadline = self._provider(
            transport=transport)
        store_calls = []

        def recording_store(argv, **kwargs):
            store_calls.append(list(argv))
            if argv == ["ledger-clear", "--session-id", "sid-a"]:
                transport.ledger = [row for row in transport.ledger
                                    if row[0] != "sid-a"]
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

        mod._run_store = recording_store

        first = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(first, _fixture_expected()[1]["rendered"])

        provider.on_session_switch("sid-b", reset=True)
        self.assertEqual(store_calls,
                         [["ledger-clear", "--session-id", "sid-a"]])
        self.assertEqual(provider.recall_status().count, 0)
        self.assertEqual(transport.ledger, [])

        provider.on_session_switch("sid-a")
        second = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(second, _fixture_expected()[1]["rendered"])
        self.assertEqual(provider.recall_status().count, 3)
        self.assertEqual(len(store_calls), 1)

    def test_envelope_shape_and_canonical_fence(self):
        mod, provider, scheduler, deadline = self._provider()
        required = mod._load_envelope_key_constants()[0]
        optional = mod._load_envelope_key_constants()[1]
        expected_cases = {case["case"]: case for case in _fixture_expected()}
        envelopes = {case["query"]: case["envelope"]
                     for case in _fixture_cases()}

        for case in _fixture_cases():
            projection = expected_cases[case["case"]]
            _, key_and_query = provider._prefetch_request(
                case["query"], case["session_id"], case["namespace"])
            self.assertEqual(
                provider._prefetch_request(case["query"],
                                           case["session_id"],
                                           case["namespace"])[0][1],
                projection["fingerprint"], case["case"])
            self.assertEqual(key_and_query,
                             " ".join(case["query"].split()))

        alpha = envelopes["project alpha"]
        self.assertTrue(provider._envelope_is_valid(alpha))
        beta = envelopes["project beta"]
        self.assertTrue(provider._envelope_is_valid(beta))
        self.assertIsInstance(beta["excluded"], list)
        self.assertEqual(len(beta["excluded"]), 2)
        self.assertEqual(set(alpha.keys()), set(required))
        self.assertEqual(set(beta.keys()), set(required))

        unknown = dict(alpha)
        unknown["mystery"] = 1
        self.assertFalse(provider._envelope_is_valid(unknown))
        missing = {k: v for k, v in alpha.items() if k != "rendered"}
        self.assertFalse(provider._envelope_is_valid(missing))
        optional_only = dict(alpha)
        optional_only["budget_note"] = "trimmed"
        self.assertTrue(provider._envelope_is_valid(optional_only))
        self.assertTrue(optional.issuperset({"budget_note"}))

        transport = FakeTransport()
        provider._transport = transport
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(rendered, expected_cases["project-alpha"]["rendered"])
        self.assertTrue(rendered.endswith("<<<END_ZMEM_UNTRUSTED_FENCE>>>\n"))


class _ProviderBoundaryTest(unittest.TestCase):
    """Definition-of-done boundary pins that are not issue-named methods."""

    def test_no_storelib_or_ledger_tokens_in_provider(self):
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(
            encoding="utf-8")
        for token in ("import storelib", "from storelib", "sqlite3",
                      "delivery_ledger", "_load_inject", "_load_ops_tokens",
                      "_OPS_TOKENS", "_fence_renderer",
                      "_local_fenced_recall"):
            self.assertNotIn(token, text, "provider must not contain " + token)

    def test_scheduler_and_deadline_are_distinct_seams(self):
        mod = _load_provider("zmem_hermes_162_boundary")
        fake = _load_fake_executor()
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        provider = mod.ZmemMemoryProvider(scheduler=scheduler,
                                          deadline=deadline)
        self.assertIsNot(provider._scheduler, provider._deadline)
        # A no-arg provider keeps both seams inert-safe: queue is a no-op.
        bare = mod.ZmemMemoryProvider()
        self.assertIsNone(bare._scheduler)
        self.assertIsNone(bare.queue_prefetch("project alpha",
                                              session_id="sid-a"))
        self.assertEqual(bare._pending_keys, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
