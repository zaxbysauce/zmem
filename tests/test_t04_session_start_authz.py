"""T-04 session_start authorization pins (issue #283, supplementary).

The frozen acceptance checks (tests/test_t04_authz_selection.py) cover the
prefetch surface. These two supplementary pins close the intake-D1 coverage
gap on the session_start surface, where the selector leg (--session-id) used
to union user:global rows for scoped principals because the selector
hardcoded include_global=True and the MCP-side conditional --include-global
was inert on that leg:

- test_scoped_session_start_without_global_delivers_no_user_global_rows:
  GREEN after the fix (RED at base — the reproduced leak). A principal
  scoped to one project, session_start with a session id, store holding a
  project row AND a user:global row: the rendered fence and ids contain the
  project row and ZERO user:global rows.
- test_scoped_session_start_with_global_delivers_globals_at_limit_2:
  GREEN at base and head (never-suppress pin). A principal scoped WITH
  user:global still receives the global row on the selector leg.

Env discipline mirrors tests/test_passive_prefetch.py: fresh GUID scratch per
test, ZMEM_STORE/ZMEM_DATA pinned before any storelib import or store.py
subprocess, ambient overrides stripped and restored. Runs standalone:
python tests/test_t04_session_start_authz.py  (CI's direct-execution loop).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

_PIN_ENV_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_NOW", "PYTHONIOENCODING",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S",
    "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE", "ZMEM_CROSS_PROJECT",
    "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
)


class _Env:
    """Pin the isolation env around one test; restore on exit."""

    def __init__(self) -> None:
        self.scratch = tempfile.mkdtemp(
            prefix=f"zmem-t04-ss-{uuid.uuid4().hex}-")
        self.saved = {k: os.environ.get(k) for k in _PIN_ENV_KEYS}
        os.environ.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_STORE": os.path.join(self.scratch, "store.sqlite"),
            "ZMEM_DATA": self.scratch,
            "ZMEM_MODELS_DIR": os.path.join(self.scratch, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        for key in ("ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
                    "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_NAMESPACE", "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
                    "ZMEM_MCP_TOKENS_FILE", "ZMEM_CROSS_PROJECT",
                    "ZMEM_CROSS_PROJECT_HAZARD_VERBS"):
            os.environ.pop(key, None)

    def token_file(self, namespaces: list[str]) -> str:
        path = os.path.join(self.scratch, f"token-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"token": "t04-ss-secret",
                       "namespaces": namespaces}, f)
        return path

    def close(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.scratch, ignore_errors=True)


def _run_store(env: _Env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STORE_PY), *args],
        env=dict(os.environ), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


def _seed(env: _Env, namespace: str, content: str) -> None:
    r = _run_store(env, "add", "--namespace", namespace, "--type", "lesson",
                   "--content", content, "--signal", "test",
                   "--confidence", "0.9", "--json")
    assert r.returncode == 0, (namespace, r.stderr)


def _session_start(namespaces: list[str], session_id: str) -> dict:
    env = _Env()
    try:
        _run_store(env, "init")
        _seed(env, "project:t04-ss",
              "t04 session start project row: deploy runbook")
        _seed(env, "user:global",
              "t04 session start global row: fleet deploy convention")
        os.environ["ZMEM_MCP_TOKEN_FILE"] = env.token_file(namespaces)
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        spec = importlib.util.spec_from_file_location(
            "zmem_t04_ss_" + uuid.uuid4().hex[:8], SERVER_DIR / "mcp_server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        server = mod.build_server(host="127.0.0.1", port=0, use_tls=False)
        return asyncio.run(server._tool_manager.call_tool(
            "session_start",
            {"namespace": "project:t04-ss", "limit": 3,
             "session_id": session_id},
            context=None))
    finally:
        env.close()


def test_scoped_session_start_without_global_delivers_no_user_global_rows():
    """RED at base, GREEN after the fix (the intake-D1 leak pin)."""
    if not MCP_AVAILABLE:
        print("SKIP test_scoped_session_start_without_global: no mcp package")
        return
    out = _session_start(["project:t04-ss"],
                         "c2830000-0000-4000-8000-000000000001")
    rendered = out.get("rendered", "") or out.get("context", "")
    ids = out.get("ids", [])
    ns_rows = out.get("results", [])
    assert "t04 session start project row" in rendered, out
    assert "t04 session start global row" not in rendered, out
    if ns_rows:
        assert all(r.get("namespace") != "user:global" for r in ns_rows), out
    assert ids, out


def test_scoped_session_start_with_global_delivers_globals_at_limit_2():
    """GREEN at base and head (never-suppress pin for allowed globals)."""
    if not MCP_AVAILABLE:
        print("SKIP test_scoped_session_start_with_global: no mcp package")
        return
    out = _session_start(["project:t04-ss", "user:global"],
                         "c2830000-0000-4000-8000-000000000002")
    rendered = out.get("rendered", "") or out.get("context", "")
    assert "t04 session start project row" in rendered, out
    assert "t04 session start global row" in rendered, out


if __name__ == "__main__":
    _failures = []
    for _name in (
            "test_scoped_session_start_without_global_delivers_no_user_global_rows",
            "test_scoped_session_start_with_global_delivers_globals_at_limit_2"):
        try:
            globals()[_name]()
            print(f"ok   {_name}")
        except AssertionError as exc:
            _failures.append((_name, exc))
            print(f"FAIL {_name}: {exc}")
    if _failures:
        for _name, _exc in _failures:
            print(f"collected: {_name}: AssertionError: {_exc}")
        sys.exit(1)
    print("2 tests completed (or skipped with a visible message)")
