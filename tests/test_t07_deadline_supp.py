"""Issue #286 supplementary deadline checks (plan-critic round 1, F-3).

NOT frozen (the frozen checks live in tests/test_t07_deadline.py, whose
bytes are checkpoint-locked): these close the discrimination gap the plan
critic named — the frozen set alone cannot distinguish a correct
implementation from a degenerate "deadline_ms present → always
deadline_exceeded" one, and nothing pinned the raw-value normalization
contract.

- ``test_generous_deadline_succeeds`` — a call with a GENEROUS deadline
  (30 s) against a fast fake store SUCCEEDS through the full seam
  (protocol gate → deadline gate → queue admission → subprocess). Kills
  the degenerate implementation.
- ``test_deadline_ms_normalization_table`` — the raw-value contract:
  ``True``/``"500"``/``-1``/``1.5-as-string`` → the structured
  invalid-argument refusal naming ``deadline_ms``; ``0`` → the stable
  ``deadline_exceeded`` with NO subprocess driven (no marker file).
- ``test_expired_at_entry_touches_no_ledger_family`` — a prefetch with
  ``deadline_ms=1`` against a REAL seeded store drives no store
  subprocess at all (the scratch ops/ ledger family is absent), pinning
  the expiry-at-entry no-work contract behind the reservation
  disposition (plan F-1).

Runs standalone: python tests/test_t07_deadline_supp.py (CI direct lane;
__main__ block prints PASS/FAIL per check and exits 1 on failure).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
    "ZMEM_MCP_TOKENS_FILE", "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT",
)


@contextlib.contextmanager
def _pinned_server(prefix: str):
    tmp = tempfile.mkdtemp(prefix=f"zmem-t07s-{prefix}-{uuid.uuid4().hex}-")
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
        "ZMEM_MCP_TOKEN": "t07-supp-token",
    })
    for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
        del sys.modules[mod]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        yield server, mcp_server, tmp
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
    return asyncio.run(
        server._tool_manager.call_tool(name, args, context=None))


def test_generous_deadline_succeeds():
    with _pinned_server("generous") as (server, mod, tmp):
        calls = {"n": 0}
        real_run_store = mod._run_store

        def fast_fake(args, input_text=None, timing=None, timeout=None):
            calls["n"] += 1
            assert timeout is not None and timeout > 1.0, (
                f"a generous deadline must clamp the subprocess timeout to "
                f"the remaining budget, got {timeout!r}")
            return {"ok": True,
                    "stdout": '{"id": "supp", "result": "stored"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = fast_fake
        try:
            resp = _call(server, "add", {
                "type": "fact", "content": "t07-supp generous",
                "namespace": "user:global", "deadline_ms": 30000,
            })
        finally:
            mod._run_store = real_run_store
        assert isinstance(resp, dict) and not resp.get("error"), resp
        assert resp.get("result") == "stored", resp
        assert calls["n"] == 1, "the subprocess must have run exactly once"


def test_short_deadline_success_path_and_unit_scale():
    """N-5 (review F-2): a ~2 s budget on a fast fake store SUCCEEDS with
    the subprocess timeout clamped near the remaining budget — kills both
    demonstrated mutants (expired() = declared_ms < 30000 would refuse a
    2000 ms call; a divide-by-10000 unit error would clamp the timeout to
    0.2 s)."""
    with _pinned_server("short-ok") as (server, mod, tmp):
        seen = {}
        real_run_store = mod._run_store

        def fast_fake(args, input_text=None, timing=None, timeout=None):
            seen["timeout"] = timeout
            return {"ok": True,
                    "stdout": '{"id": "short-ok", "result": "stored"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = fast_fake
        try:
            resp = _call(server, "add", {
                "type": "fact", "content": "t07-supp short-ok",
                "namespace": "user:global", "deadline_ms": 2000,
            })
        finally:
            mod._run_store = real_run_store
        assert resp.get("result") == "stored", resp
        assert seen["timeout"] is not None and 1.5 <= seen["timeout"] <= 1.9, (
            f"the subprocess timeout must be the ~1.8 s remaining budget "
            f"(2 s minus the reserve), got: {seen['timeout']!r}")


def test_pool_knobs_are_distinct_and_swap_detectable():
    """N-21: with DIFFERENT read/write knob values, the module constants
    and both semaphores must reflect them (a construction swap would fail
    the value assertions)."""
    tmp = tempfile.mkdtemp(prefix="zmem-t07s-pools-")
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
        "ZMEM_MCP_TOKEN": "t07-supp-token",
        "ZMEM_MCP_MAX_CONCURRENT_READ": "3",
        "ZMEM_MCP_MAX_CONCURRENT_WRITE": "5",
    })
    for mod_name in [k for k in list(sys.modules) if k == "mcp_server"]:
        del sys.modules[mod_name]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        assert mcp_server._MAX_CONCURRENT_READ == 3, (
            mcp_server._MAX_CONCURRENT_READ)
        assert mcp_server._MAX_CONCURRENT_WRITE == 5, (
            mcp_server._MAX_CONCURRENT_WRITE)
        read_sem, write_sem = asyncio.run(_acquire_both(mcp_server))
        assert read_sem is not write_sem
    finally:
        sys.path.remove(str(SERVER_DIR))
        for mod_name in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod_name]
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _acquire_both(mod):
    """Acquire one permit from each pool (post-rebind) and return them."""
    r = mod._get_read_semaphore()
    w = mod._get_store_semaphore()
    await r.acquire()
    await w.acquire()
    r.release()
    w.release()
    return r, w


def test_submit_failure_releases_the_permit():
    """N-20: when executor.submit raises after a successful acquire, the
    submit-failure guard must restore the pool permit (a dropped release
    would permanently shrink the pool)."""
    with _pinned_server("submitfail") as (server, mod, tmp):
        class _BoomExecutor:
            def submit(self, *a, **k):
                raise RuntimeError("fb submit failure")

        real_executor = mod._store_executor
        mod._store_executor = _BoomExecutor()
        try:
            raised = None
            try:
                _call(server, "add", {
                    "type": "fact", "content": "t07-supp submitfail",
                    "namespace": "user:global", "deadline_ms": 30000,
                })
            except Exception as exc:
                raised = exc
            assert raised is not None, (
                "a submit failure must propagate (after the permit is "
                f"restored), got: {resp!r}" if False else
                "a submit failure must propagate after the permit restore")
            sem = mod._get_store_semaphore()
            assert sem._value >= 1, (
                f"the write-pool permit must be restored after a submit "
                f"failure (value={sem._value})")
        finally:
            mod._store_executor = real_executor


def test_recall_and_update_honour_the_deadline():
    """N-17: deadline coverage beyond add/prefetch — recall carries the
    client_protocol gate, and update's multi-leg span recomputes the
    budget so the mutation leg is killed inside what is left."""
    with _pinned_server("recall-update") as (server, mod, tmp):
        real_run_store = mod._run_store

        def two_leg_fake(args, input_text=None, timing=None, timeout=None):
            # Model subprocess.run's kill: a timeout below the fake's 2 s
            # work time surfaces the same envelope _run_store returns on
            # TimeoutExpired (flag set, stdout empty).
            if timeout is not None and timeout < 1.9:
                return {"ok": False, "stdout": "",
                        "stderr": f"store.py timed out after {timeout}s",
                        "returncode": 124, "deadline_exceeded": True}
            if args and args[0] == "get":
                return {"ok": True, "stdout": '{"namespace": "user:global", "tags": ""}',
                        "stderr": "", "returncode": 0}
            time.sleep(2.0)
            return {"ok": True, "stdout": '{"id": "upd", "result": "updated"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = two_leg_fake
        try:
            refused = _call(server, "recall", {
                "query": "t07 supp gate", "client_protocol": 99})
            assert refused.get("error") == "protocol_incompatible", (
                f"recall must carry the client_protocol gate: {refused!r}")
            expired = _call(server, "recall", {
                "query": "t07 supp gate", "deadline_ms": 0})
            assert expired.get("error") == "deadline_exceeded", expired
            started = time.perf_counter()
            resp = _call(server, "update", {
                "id": "fb-target", "content": "t07 supp update",
                "deadline_ms": 500})
            elapsed = time.perf_counter() - started
            assert resp.get("error") == "deadline_exceeded", resp
            assert elapsed < 2.0, (
                f"update's mutation leg must be killed inside the "
                f"recomputed budget, took {elapsed:.2f}s")
        finally:
            mod._run_store = real_run_store


def test_deadline_ms_normalization_table():
    with _pinned_server("normalize") as (server, mod, tmp):
        ran = {"n": 0}
        real_run_store = mod._run_store

        def never_fake(args, input_text=None, timing=None, timeout=None):
            ran["n"] += 1
            return {"ok": True, "stdout": "{}", "stderr": "",
                    "returncode": 0}

        mod._run_store = never_fake
        try:
            for bad in (True, "500", -1, [500], {"ms": 500}, 1.5 + 0j,
                        float("nan"), float("inf")):
                resp = _call(server, "add", {
                    "type": "fact", "content": "t07-supp normalize",
                    "namespace": "user:global", "deadline_ms": bad,
                })
                assert isinstance(resp, dict), resp
                assert resp.get("error") == "invalid argument", (
                    f"deadline_ms={bad!r} must yield the structured "
                    f"invalid-argument refusal, got: {resp!r}")
                assert resp.get("field") == "deadline_ms", resp
                # N-8/N-15: the echoed value is bounded (never the raw
                # unserializable float or an unbounded container).
                assert len(repr(resp.get("value"))) <= 80, resp
            # A valid float deadline is accepted (numbers are numbers).
            ok_resp = _call(server, "add", {
                "type": "fact", "content": "t07-supp float-ok",
                "namespace": "user:global", "deadline_ms": 30000.0,
            })
            assert ok_resp.get("result") == "stored", ok_resp
            # Zero is already expired: the stable token, no subprocess.
            zero = _call(server, "add", {
                "type": "fact", "content": "t07-supp zero",
                "namespace": "user:global", "deadline_ms": 0,
            })
            assert zero.get("error") == "deadline_exceeded", zero
            assert ran["n"] == 1, (
                f"only the float-deadline call may drive a subprocess; "
                f"drove {ran['n']}")
        finally:
            mod._run_store = real_run_store


def test_expired_at_entry_touches_no_ledger_family():
    with _pinned_server("ledger") as (server, mod, tmp):
        # Seed a REAL store first (init only — no rows needed for the
        # expiry path; the selector envelope only matters if a subprocess
        # runs, which is exactly what must NOT happen).
        seeded = subprocess.run(
            [sys.executable, str(REPO_ROOT / "skills/memory/scripts/store.py"),
             "init"],
            capture_output=True, text=True, timeout=120)
        assert seeded.returncode == 0, seeded.stderr
        ops_before = sorted(p.name for p in Path(tmp).glob("ops/*"))
        resp = _call(server, "prefetch", {
            "query": "t07 supp expiry", "namespace": "user:global",
            "session_id": f"t07-supp-{uuid.uuid4().hex}",
            "moment": "user_prompt", "deadline_ms": 1,
        })
        time.sleep(0.5)
        ops_after = sorted(p.name for p in Path(tmp).glob("ops/*"))
        assert resp.get("error") == "deadline_exceeded", resp
        assert ops_after == ops_before, (
            f"an expired-at-entry call must not create ledger/reservation "
            f"state: before={ops_before} after={ops_after}")


def _all_checks():
    return (
        test_generous_deadline_succeeds,
        test_short_deadline_success_path_and_unit_scale,
        test_pool_knobs_are_distinct_and_swap_detectable,
        test_submit_failure_releases_the_permit,
        test_recall_and_update_honour_the_deadline,
        test_deadline_ms_normalization_table,
        test_expired_at_entry_touches_no_ledger_family,
    )


def _main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: tests/test_t07_deadline_supp.py requires the 'mcp' "
              "package (hermes-plugin/server/requirements.txt).")
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
    print("all supplementary checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
