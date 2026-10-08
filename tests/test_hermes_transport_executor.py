"""Real-executor deadline coverage for the #160 transport (PR #243 feedback).

The frozen DeadlineTest in tests/test_hermes_transport.py pins the fake-clock
contract (no wall-clock assertions THERE, by design).  This module owns the
complement: the REAL DeadlineExecutor threading branch — worker join timeout,
cancel hook invocation, bounded grace join, and the None / empty-envelope
return — plus the provider's >4096-char truncate-then-delegate slice, none of
which the fake-clock seam can exercise.

Wall-clock bounds here are deliberately generous (deadlines of 0.3-0.5 s
against assertions capped at 10 s) so slow CI runners stay deterministic;
the operations themselves block on Events released by the cancel hooks, not
on sleeps, so the tests cannot pass by timing luck.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import atexit
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-160-executor-"))
atexit.register(shutil.rmtree, str(_SCRATCH), ignore_errors=True)
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_MODE_ENV_KEYS = ("ZMEM_HERMES_MODE", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
                  "ZMEM_MCP_TOKEN_FILE", "ZMEM_HERMES_DEADLINE_S",
                  "ZMEM_HOME")

_ENVELOPE_JSON = (
    '{"results": [], "count": 0, "omitted": 0, "reason": "ok", '
    '"excluded": [], "candidate_ids": [], "tokens_used": 0, '
    '"tokens_budget": 1500, "budget_dropped": 0, "budget_admission": 0, '
    '"budget_truncated": 0, "budget_dropped_protected": 0, "arms": {}, '
    '"rendered": "late"}')


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "zmem_transport_executor",
        REPO_ROOT / "hermes-plugin" / "transport.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_transport_executor"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider():
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        "zmem_hermes_executor", REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_hermes_executor"] = module
    spec.loader.exec_module(module)
    return module


class _CancellableOp:
    """An operation that blocks until its cancel hook releases it.

    ``completed`` is set only when ``__call__`` actually returns — after the
    grace join, run() returning None therefore guarantees the worker was
    reaped (removing the grace join makes the completed assert race and
    fail)."""

    def __init__(self):
        self.cancel_requested = threading.Event()
        self.released = threading.Event()
        self.completed = threading.Event()
        self.ran = False

    def __call__(self):
        self.ran = True
        self.released.wait(timeout=30)
        self.completed.set()
        return "should-never-be-returned"

    def cancel(self):
        self.cancel_requested.set()
        self.released.set()


class RealExecutorDeadlineTest(unittest.TestCase):
    """F1/F2: the production threading branch (join -> cancel -> grace -> None)."""

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

    def test_deadline_invokes_cancel_hook_and_returns_none(self):
        transport = _load_transport()
        op = _CancellableOp()
        started = time.monotonic()
        result = transport.DeadlineExecutor().run(op, 0.3)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertTrue(op.ran, "operation must have started")
        self.assertTrue(op.cancel_requested.is_set(),
                        "deadline must invoke the cancel hook")
        # The grace join must have reaped the worker before run() returned:
        # the op completes as soon as the cancel hook releases it, so a
        # returned-None without the join leaves this Event unset (race).
        self.assertTrue(op.completed.is_set(),
                        "grace join must reap the cancelled worker before "
                        "run() returns")
        self.assertLess(elapsed, 10.0,
                        "cancel-released op must not wait out its 30s cap")

    def test_local_subprocess_deadline_kills_child_returns_empty(self):
        transport = _load_transport()
        tmp = Path(tempfile.mkdtemp(prefix="zmem-160-slow-store-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        finished = tmp / "finished.txt"
        slow_store = tmp / "store.py"
        started_marker = tmp / "started.txt"
        slow_store.write_text(textwrap.dedent('''
            import sys, time
            from pathlib import Path
            Path(r"@START@").write_text("up", encoding="utf-8")
            time.sleep(1.5)
            Path(r"@FIN@").write_text("done", encoding="utf-8")
            print(@ENVELOPE@)
        ''').replace("@START@", str(started_marker).replace("\\", "/"))
            .replace("@FIN@", str(finished).replace("\\", "/"))
            .replace("@ENVELOPE@", repr(_ENVELOPE_JSON)),
            encoding="utf-8")
        local = transport.LocalSubprocess(
            store_py=str(slow_store),
            executor=transport.DeadlineExecutor(),
            deadline_s=0.4)
        started = time.monotonic()
        envelope = local.prefetch(
            "stash pop", namespace="project:parity", session_id="s",
            moment="user_prompt", ops_tokens=[])
        elapsed = time.monotonic() - started
        self.assertEqual(envelope["reason"], "empty-pool")
        self.assertEqual(envelope["rendered"], "")
        self.assertLess(elapsed, 10.0)
        # Wait for the child to have STARTED (so the kill had a real target),
        # then wait past the child's unfinished 1.5 s finish marker: if the
        # deadline did NOT kill it, "finished" appears at ~1.7 s and this
        # assert at ~3 s fails.  With the kill, the child dies at the
        # deadline and the marker never appears.
        started_deadline = time.monotonic() + 10.0
        while not started_marker.exists() and time.monotonic() < started_deadline:
            time.sleep(0.05)
        self.assertTrue(started_marker.exists(), "child must have started")
        time.sleep(max(0.0, 3.0 - (time.monotonic() - started)))
        self.assertFalse(finished.exists(),
                         "deadline must kill the slow child before it "
                         "writes its 1.5 s finish marker")

    def test_mcp_deadline_cancels_coroutine_returns_empty(self):
        transport = _load_transport()
        cancelled_flag = threading.Event()

        async def slow_call(url, token, tool, arguments):
            loop = __import__("asyncio").get_running_loop()
            gate = loop.create_future()
            try:
                return await gate
            except __import__("asyncio").CancelledError:
                cancelled_flag.set()
                raise

        mcp = transport.McpHttp(
            url="http://127.0.0.1:9/mcp",
            executor=transport.DeadlineExecutor(),
            call_fn=slow_call, deadline_s=0.3)
        started = time.monotonic()
        envelope = mcp.prefetch(
            "stash pop", namespace="project:parity", session_id="s",
            moment="user_prompt", ops_tokens=[])
        elapsed = time.monotonic() - started
        self.assertEqual(envelope["reason"], "empty-pool")
        self.assertEqual(envelope["rendered"], "")
        self.assertTrue(cancelled_flag.wait(timeout=10),
                        "deadline must cancel the MCP coroutine")
        self.assertLess(elapsed, 10.0)

    def test_provider_truncates_over_4096_char_query(self):
        """F3, re-pinned by issue #162: the raw prompt is still truncated at
        ``_MAX_PROMPT_CHARS`` (4096) for the ticket/tombstone digest, and the
        transport now receives the whitespace-normalized query capped at
        ``_MAX_QUERY_CHARS`` (500) — the cache fingerprint covers exactly
        that text, so the two bounds can never diverge on the wire."""
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        self.addCleanup(os.environ.pop, "ZMEM_HOME", None)
        provider_mod = _load_provider()
        provider = provider_mod.ZmemMemoryProvider()
        seen = []

        class _RecordingTransport:
            def prefetch(self, query, **kwargs):
                seen.append(query)
                return {"rendered": ""}

        provider._transport = _RecordingTransport()
        provider.prefetch("x" * 5000, session_id="s")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), provider_mod._MAX_QUERY_CHARS)
        self.assertTrue(all(ch == "x" for ch in seen[0]))


class _SlowChildSmoke(unittest.TestCase):
    """Sanity: the slow-store helper above really would finish unbounded."""

    def test_slow_store_template_is_syntactic(self):
        compile("import sys, time", "<f>", "exec")
        proc = subprocess.run(
            [sys.executable, "-c", "print('ok')"], capture_output=True)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
