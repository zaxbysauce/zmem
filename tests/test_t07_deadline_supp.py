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
            for bad in (True, "500", -1, [500], {"ms": 500}, 1.5 + 0j):
                resp = _call(server, "add", {
                    "type": "fact", "content": "t07-supp normalize",
                    "namespace": "user:global", "deadline_ms": bad,
                })
                assert isinstance(resp, dict), resp
                assert resp.get("error") == "invalid argument", (
                    f"deadline_ms={bad!r} must yield the structured "
                    f"invalid-argument refusal, got: {resp!r}")
                assert resp.get("field") == "deadline_ms", resp
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
