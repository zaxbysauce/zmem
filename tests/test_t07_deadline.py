"""Issue #286 (Workstream T PR 7) — caller deadlines + split read/write pools.

Phase 2.5 frozen acceptance checks AC1-AC3 (AC4's preserving check stays in
tests/test_hermes_transport.py -k Deadline, untouched). Every check names
its standing at both ends:

- AC1 ``test_queue_admission_honours_caller_deadline`` — with every
  concurrency slot held (``_MAX_CONCURRENT_STORE`` slow fake stores), an
  ``add`` carrying ``deadline_ms=500`` must return the stable error token
  ``deadline_exceeded`` within 500 ms + 100 ms slack (elapsed < 0.6 s),
  not wait on the fixed 60 s queue budget. RED at base (``deadline_ms``
  is silently dropped by arg validation — the SDK's generated argument
  model ignores extra keys — so the call queues behind the held slots,
  measures ~3.8 s, and returns a stored/stored SUCCESS), GREEN after the
  fix.
- AC2 ``test_expired_call_commits_nothing`` — a store subprocess that
  would outlive the remaining deadline is KILLED at the deadline: driving
  ``add(deadline_ms=500)`` against a FAKE store.py checkout that sleeps
  2 s, then writes a marker file (the would-be commit) and prints
  success JSON, the call must return ``deadline_exceeded`` and the marker
  must still be ABSENT after waiting past the would-write moment (3 s),
  with zero rows in the isolated store. RED at base (the kwarg is
  ignored, the subprocess runs its full 2 s, the call reports success
  and the marker IS written — a commit after the caller's deadline),
  GREEN after the fix.
- AC3 ``test_passive_reads_not_starved_by_writes`` — a saturated WRITE
  pool must not delay a passive read: with all slots held by slow fake
  writes, a ``prefetch`` (served by a fast fake returning the real
  selector envelope) must complete in under 1.5 s and return the real
  envelope shape (``results`` list + ``rendered``, no ``error``), drawing
  from the separate READ pool. RED at base (one shared semaphore — the
  prefetch queues behind the write burst and measures ~2.8 s), GREEN
  after the fix.

Supplementary (same file, not a frozen AC row):
- ``test_legacy_call_without_deadline_keeps_fixed_budget_shape`` — the
  control leg: an ``add`` WITHOUT ``deadline_ms`` against a fast fake
  store succeeds normally (result stored, never ``deadline_exceeded``) —
  legacy callers keep the fixed-budget behavior byte-for-byte. GREEN at
  BOTH ends.

Authored tool contract the implementation must satisfy (the issue's
constraints, made executable):
- Every tool gains an OPTIONAL ``deadline_ms`` (milliseconds — the
  caller's REMAINING budget). Absent → legacy behavior (fixed 60 s queue
  wait, 30 s subprocess), byte-identical.
- A call whose deadline expires while waiting for a concurrency slot
  returns ``{"error": "deadline_exceeded", ...}`` promptly.
- A store subprocess that would outlive the remaining deadline is killed
  at the deadline; the call leaves no row and no ledger record.
- Passive reads (prefetch, session_start) draw from a separate READ
  pool so a saturated WRITE pool cannot delay them.

Env discipline: every test pins a fresh GUID scratch dir (ZMEM_HOME=repo
root so the import-time constant loading uses the real checkout,
ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR, ZMEM_MODEL_AUTODOWNLOAD=0,
PYTHONIOENCODING=utf-8, a scratch ZMEM_MCP_TOKEN) BEFORE importing
mcp_server, purging any cached ``mcp_server`` module so constants reload
under the pinned env, and restores everything afterwards. AC1/AC3 drive
slot-holding load by monkeypatching the module-level ``_run_store`` that
``_run_store_async`` resolves at call time (restored in finally); AC2
flips ``ZMEM_HOME`` to a fake checkout just before the call (the server
resolves store.py per call) and restores it in finally. AC1/AC3 run
fillers and the measured call inside ONE ``asyncio.run`` — the store
semaphore is module-global and must not cross event loops.

Runs standalone (CI runs ``python tests/test_t07_deadline.py`` directly —
the repo's python lane is a direct-execution loop, NOT pytest): the
__main__ block invokes every check, prints failures by name, and exits 1
on any failure. Skips loudly (visible message, exit 0) only when the
``mcp`` package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt on both matrix legs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

try:
    import mcp  # noqa: F401  (the SDK import that gates the whole surface)
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_DEFAULT_NS",
    "ZMEM_EMBED_PROFILE", "ZMEM_HOST", "ZMEM_SESSION",
)

# AC1: the issue's own bound — deadline_ms=500 plus 100 ms slack.
_AC1_MAX_ELAPSED_S = 0.6
# AC3: fast-admission bound for the passive read under full write load.
_AC3_MAX_ELAPSED_S = 1.5
# How long the slot-holding fakes keep the (write/shared) pool saturated.
_SLOT_HOLD_S = 2.0
_WRITE_HOLD_S = 3.0
# AC2: wait PAST the fake checkout's would-write moment (2 s sleep).
_WOULD_WRITE_PAST_S = 3.0

# The fake store.py checkout for AC2: sleeps past the caller deadline,
# then writes the marker file (the would-be commit) and prints success
# JSON. Pure stdlib, no zmem imports — it stands in for the subprocess
# the hub would spawn for `add`.
_FAKE_STORE_PY = '''\
"""T07 fake store checkout (issue #286 AC2)."""
import json
import os
import time

time.sleep(2.0)
_marker = os.environ.get("ZMEM_T07_MARKER", "")
if _marker:
    with open(_marker, "w", encoding="utf-8") as fh:
        fh.write("t07-expired-commit")
print(json.dumps({"id": "t07-fake", "result": "stored"}))
'''

# The real selector envelope shape (the store-side empty-pool response),
# served instantly by the AC3 fast fake so the measured time is pure
# pool-admission latency, never store work.
_PREFETCH_EMPTY_ENVELOPE = {
    "results": [], "count": 0, "omitted": 0, "reason": "empty-pool",
    "excluded": [], "candidate_ids": [], "tokens_used": 0,
    "tokens_budget": 0, "budget_dropped": 0, "budget_admission": 0,
    "budget_truncated": 0, "budget_dropped_protected": 0,
    "arms": [], "rendered": "", "context": "",
}


@contextlib.contextmanager
def _pinned_server(prefix: str):
    """Fresh GUID scratch + pinned env + a real build_server() object.

    mcp_server is (re)imported under the pinned environment so its
    import-time constant loading follows THIS scratch; the module is purged
    from sys.modules on entry AND exit so sibling tests never inherit the
    pin (store-path-freeze discipline). Each test therefore gets its own
    store semaphore/executor, bound to its own event loop.
    """
    tmp = tempfile.mkdtemp(prefix=f"zmem-t07-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t07-check-token",
    })
    for mod in [k for k in list(sys.modules)
                if k == "mcp_server" or k.startswith("auth")
                or k.startswith("bind_guard")]:
        del sys.modules[mod]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        yield server, mcp_server
    finally:
        sys.path.remove(str(SERVER_DIR))
        for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _call(server, name: str, args: dict):
    """Invoke one tool through the real ToolManager (context-free)."""
    return asyncio.run(
        server._tool_manager.call_tool(name, args, context=None))


def _live_row_count(store_path: str) -> int:
    """Rows in the isolated store's memory table (0 when file absent)."""
    if not os.path.exists(store_path):
        return 0
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


async def _run_full_slot_scenario(server, mod, measured_call):
    """Saturate every store slot, then run ONE measured call, then unwind.

    Launches ``mod._MAX_CONCURRENT_STORE`` filler ``add`` calls (they hold
    a permit each for the fake's sleep), waits 0.3 s so every filler has
    acquired its slot, then times ``measured_call()`` — an async callable
    invoking the tool under measurement. Filler tasks are cancelled and
    gathered before the loop closes so the scenario never leaves pending
    tasks behind (the underlying worker sleeps run out in the background
    and release their permits via the thread-safe fallback path).
    """
    async def filler(i: int):
        return await server._tool_manager.call_tool(
            "add", {
                "type": "fact",
                "content": f"t07-filler-{i}",
                "namespace": "user:global",
            }, context=None)

    fillers = [asyncio.create_task(filler(i))
               for i in range(mod._MAX_CONCURRENT_STORE)]
    await asyncio.sleep(0.3)  # let every filler acquire a slot
    started = time.perf_counter()
    resp = await measured_call()
    elapsed = time.perf_counter() - started
    for task in fillers:
        task.cancel()
    await asyncio.gather(*fillers, return_exceptions=True)
    return resp, elapsed


# -- AC1 ----------------------------------------------------------------------

def test_queue_admission_honours_caller_deadline():
    with _pinned_server("ac1") as (server, mod):
        real_run_store = mod._run_store

        def slow_fake(args, input_text=None, timing=None):
            time.sleep(_SLOT_HOLD_S)
            return {"ok": True, "stdout": '{"id": "t07", "result": "stored"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = slow_fake
        try:
            async def measured():
                return await server._tool_manager.call_tool(
                    "add", {
                        "type": "fact",
                        "content": "t07-deadline-queued",
                        "namespace": "user:global",
                        "deadline_ms": 500,
                    }, context=None)

            resp, elapsed = asyncio.run(
                _run_full_slot_scenario(server, mod, measured))
        finally:
            mod._run_store = real_run_store
        assert elapsed < _AC1_MAX_ELAPSED_S, (
            f"a call carrying deadline_ms=500 must be answered within "
            f"500 ms + 100 ms slack even when all "
            f"{mod._MAX_CONCURRENT_STORE} slots are held; measured "
            f"{elapsed:.2f}s (queue admission ignored the caller's "
            f"deadline and waited on the fixed budget)")
        assert isinstance(resp, dict), f"expected a dict, got {resp!r}"
        assert resp.get("error") == "deadline_exceeded", (
            f"an expired queued call must return the stable error token "
            f"deadline_exceeded, got: {resp!r}")


# -- AC2 ----------------------------------------------------------------------

def test_expired_call_commits_nothing():
    marker = f"t07-ac2-marker-{uuid.uuid4().hex}.txt"
    with _pinned_server("ac2") as (server, _mod):
        scratch = os.environ["ZMEM_DATA"]
        marker_path = os.path.join(scratch, marker)
        # Build the FAKE checkout: <scratch>/skills/memory/scripts/store.py
        # that sleeps 2 s, writes the marker, prints success JSON.
        fake_home = os.path.join(scratch, "fake-checkout")
        scripts_dir = os.path.join(
            fake_home, "skills", "memory", "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        with open(os.path.join(scripts_dir, "store.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(_FAKE_STORE_PY)
        assert not os.path.exists(marker_path), (
            "fresh scratch must not carry a marker")
        saved_home = os.environ["ZMEM_HOME"]
        os.environ["ZMEM_T07_MARKER"] = marker_path
        # The server resolves store.py per call: point ZMEM_HOME at the
        # fake checkout BEFORE the add.
        os.environ["ZMEM_HOME"] = fake_home
        try:
            started = time.perf_counter()
            resp = _call(server, "add", {
                "type": "fact",
                "content": "t07 expired write",
                "namespace": "user:global",
                "deadline_ms": 500,
            })
            took = time.perf_counter() - started
            assert isinstance(resp, dict), f"expected a dict, got {resp!r}"
            assert resp.get("error") == "deadline_exceeded", (
                f"a store subprocess that would outlive deadline_ms=500 "
                f"must be killed at the deadline and report "
                f"deadline_exceeded; got {resp!r} after {took:.2f}s "
                f"(deadline_ms was ignored and the subprocess ran on)")
            # Wait PAST the fake's would-write moment (sleep 2 s → write):
            # a killed subprocess must never have committed.
            remaining = _WOULD_WRITE_PAST_S - (time.perf_counter() - started)
            if remaining > 0:
                time.sleep(remaining)
            assert not os.path.exists(marker_path), (
                "the expired store subprocess wrote its marker AFTER the "
                "caller's deadline — it must be killed at the deadline and "
                "leave no commit behind")
            assert _live_row_count(os.environ["ZMEM_STORE"]) == 0, (
                "an expired write must leave no row in the store")
        finally:
            os.environ["ZMEM_HOME"] = saved_home
            os.environ.pop("ZMEM_T07_MARKER", None)


# -- AC3 ----------------------------------------------------------------------

def test_passive_reads_not_starved_by_writes():
    session = f"t07-ac3-{uuid.uuid4().hex}"
    with _pinned_server("ac3") as (server, mod):
        real_run_store = mod._run_store

        def pool_fake(args, input_text=None, timing=None):
            if args and args[0] == "add":
                # Slow WRITE: saturates the shared/write pool.
                time.sleep(_WRITE_HOLD_S)
                return {"ok": True,
                        "stdout": '{"id": "t07", "result": "stored"}',
                        "stderr": "", "returncode": 0}
            # Fast READ: returns the real selector envelope instantly so
            # the measured time is pure pool-admission latency.
            return {"ok": True,
                    "stdout": json.dumps(dict(_PREFETCH_EMPTY_ENVELOPE)),
                    "stderr": "", "returncode": 0}

        mod._run_store = pool_fake
        try:
            async def measured():
                return await server._tool_manager.call_tool(
                    "prefetch", {
                        "query": "t07 read-pool probe",
                        "namespace": "user:global",
                        "session_id": session,
                        "moment": "user_prompt",
                    }, context=None)

            resp, elapsed = asyncio.run(
                _run_full_slot_scenario(server, mod, measured))
        finally:
            mod._run_store = real_run_store
        assert elapsed < _AC3_MAX_ELAPSED_S, (
            f"a passive prefetch must be admitted from the separate READ "
            f"pool while the WRITE pool is saturated; measured "
            f"{elapsed:.2f}s under {mod._MAX_CONCURRENT_STORE} held write "
            f"slots (one shared pool starved the read behind the burst)")
        assert isinstance(resp, dict), f"expected a dict, got {resp!r}"
        assert "error" not in resp, (
            f"a read-pool prefetch must not surface an error: {resp!r}")
        assert isinstance(resp.get("results"), list), (
            f"prefetch must return the real envelope (results list): "
            f"{resp!r}")
        assert "rendered" in resp, (
            f"prefetch must return the real envelope (rendered fence): "
            f"{resp!r}")


# -- supplementary (not a frozen AC row) ---------------------------------------

def test_legacy_call_without_deadline_keeps_fixed_budget_shape():
    with _pinned_server("legacy") as (server, mod):
        real_run_store = mod._run_store

        def fast_fake(args, input_text=None, timing=None):
            return {"ok": True,
                    "stdout": '{"id": "t07-legacy", "result": "stored"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = fast_fake
        try:
            resp = _call(server, "add", {
                "type": "fact",
                "content": "t07 legacy fixed budget",
                "namespace": "user:global",
            })
        finally:
            mod._run_store = real_run_store
        assert isinstance(resp, dict), f"expected a dict, got {resp!r}"
        assert not resp.get("error"), (
            f"a call WITHOUT deadline_ms keeps the legacy fixed-budget "
            f"behavior and succeeds normally: {resp!r}")
        assert resp.get("error") != "deadline_exceeded", resp
        assert resp.get("result") == "stored", (
            f"legacy add must report the stored result: {resp!r}")


# -- standalone runner (CI direct-execution lane) -------------------------------

def _all_checks():
    return (
        test_queue_admission_honours_caller_deadline,
        test_expired_call_commits_nothing,
        test_passive_reads_not_starved_by_writes,
        test_legacy_call_without_deadline_keeps_fixed_budget_shape,
    )


def _main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: tests/test_t07_deadline.py requires the 'mcp' package "
              "(hermes-plugin/server/requirements.txt); not installed here.")
        return 0
    failures = []
    for check in _all_checks():
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
