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


def _map_owner_only(path) -> None:
    """#406 contract: a test map must not depend on the directory's
    inherited ACL (non-system drives inherit Users/Authenticated-Users
    allow ACEs, which the hub correctly refuses). POSIX uses chmod."""
    if os.name == "nt":
        subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
    else:
        os.chmod(os.fspath(path), 0o600)


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
    # Issue #286 review F-3: the pool knobs leak into module constants at
    # import — pin them so same-process sibling tests inherit a clean env.
    "ZMEM_MCP_MAX_CONCURRENT", "ZMEM_MCP_MAX_CONCURRENT_READ",
    "ZMEM_MCP_MAX_CONCURRENT_WRITE", "ZMEM_MCP_QUEUE_TIMEOUT_S",
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
        # F-3: the pool SIZES come from the matching knob — a construction
        # swap (read built from WRITE, write from READ) passes the constant
        # and distinctness checks but flips these values.
        assert read_sem._value == 3, read_sem._value
        assert write_sem._value == 5, write_sem._value
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
            before = mod._get_store_semaphore()._value
            raised = None
            try:
                _call(server, "add", {
                    "type": "fact", "content": "t07-supp submitfail",
                    "namespace": "user:global", "deadline_ms": 30000,
                })
            except Exception:
                raised = True
            assert raised, "a submit failure must propagate after the restore"
            after = mod._get_store_semaphore()._value
            assert after == before, (
                f"the write-pool permit must be restored after a submit "
                f"failure (before={before}, after={after} — a dropped "
                f"release shrinks the pool)")
        finally:
            mod._store_executor = real_executor


def test_recall_and_update_honour_the_deadline():
    """N-17: deadline coverage beyond add/prefetch — recall carries the
    client_protocol gate, and update's multi-leg span (map mode: the
    scoped get + the mutation) recomputes the budget so a later leg is
    killed inside what an earlier leg consumed."""
    # Map mode (principal map) makes update a genuine multi-leg tool:
    # the scoped get + attribution-tags get + mutation, all under ONE
    # _Deadline with per-leg remaining_s() recompute.
    tmp = tempfile.mkdtemp(prefix="zmem-t07s-map-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    token = "fb-map-token-" + uuid.uuid4().hex
    tokens_path = Path(tmp) / "tokens.json"
    tokens_path.write_text(json.dumps({
        token: {"principal": "fbhost", "host": "fbhost",
                "harness": "fbharness", "namespaces": ["user:global"]},
    }), encoding="utf-8")
    os.chmod(tokens_path, 0o600)  # #406 map permission contract
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKENS_FILE": str(tokens_path),
    })
    for mod_name in [k for k in list(sys.modules) if k == "mcp_server"]:
        del sys.modules[mod_name]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server as mod
        server = mod.build_server(host="127.0.0.1", port=0, use_tls=False)
        # Map mode resolves the caller from the request context — the direct
        # ToolManager call carries none, so set the SDK auth context var the
        # same way the transport would (the t02 test's pattern).
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        access = asyncio.run(server._token_verifier.verify_token(token))
        assert access is not None, "token verifier must accept the map token"

        def _call_scoped(name, args):
            ctx = auth_context_var.set(AuthenticatedUser(access))
            try:
                return _call(server, name, args)
            finally:
                auth_context_var.reset(ctx)

        real_run_store = mod._run_store

        seen_legs = []

        def two_leg_fake(args, input_text=None, timing=None, timeout=None):
            # Model subprocess.run's kill per leg: each leg has its own work
            # time; a timeout below it surfaces the TimeoutExpired envelope.
            # F-4: capture the per-leg timeouts — the recompute pin asserts
            # leg 2's clamp is BELOW leg 1's by the get leg's consumed time.
            seen_legs.append((args[0] if args else "?", timeout))
            work = 0.5 if (args and args[0] == "get") else 3.5
            if timeout is not None and timeout < work:
                return {"ok": False, "stdout": "",
                        "stderr": f"store.py timed out after {timeout}s",
                        "returncode": 124, "deadline_exceeded": True}
            time.sleep(work)
            if args and args[0] == "get":
                return {"ok": True, "stdout": '{"namespace": "user:global", "tags": ""}',
                        "stderr": "", "returncode": 0}
            return {"ok": True, "stdout": '{"id": "upd", "result": "updated"}',
                    "stderr": "", "returncode": 0}

        mod._run_store = two_leg_fake
        try:
            refused = _call_scoped("recall", {
                "query": "t07 supp gate", "namespace": "user:global",
                "client_protocol": 99})
            assert refused.get("error") == "protocol_incompatible", (
                f"recall must carry the client_protocol gate: {refused!r}")
            expired = _call_scoped("recall", {
                "query": "t07 supp gate", "namespace": "user:global",
                "deadline_ms": 0})
            assert expired.get("error") == "deadline_exceeded", expired
            resp = _call_scoped("update", {
                "id": "fb-target", "content": "t07 supp update",
                "deadline_ms": 3000})
            assert resp.get("error") == "deadline_exceeded", resp
            # F-4: the recompute discriminator — a later leg's subprocess
            # timeout must be clamped BELOW the first leg's by the consumed
            # work time. A mutant passing the gate-time budget to every leg
            # passes equal timeouts and fails here.
            assert len(seen_legs) >= 2, seen_legs
            first_t, last_t = seen_legs[0][1], seen_legs[-1][1]
            assert first_t is not None and last_t is not None, seen_legs
            assert last_t < first_t - 0.25, (
                f"a later leg's subprocess timeout must be recomputed below "
                f"the first leg's (first={first_t!r}, last={last_t!r}, "
                f"legs={seen_legs!r})")
        finally:
            mod._run_store = real_run_store
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
