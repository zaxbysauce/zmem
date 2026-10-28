"""Frozen shared helpers for the DeepSeek adapter arm's-length checks
(tests/test_t13_deepseek_contract.py + tests/test_t14_deepseek_learning.py,
issues #374/#375). Authored with the checks before the implementation;
mirrors the t08/t10/t11/t12 harness discipline:

- every probe runs against a per-test GUID temp dir with ALL ambient
  ``ZMEM_*`` plus both plugin-data channels stripped BEFORE anything is
  pinned, restored afterwards (the store-path-freeze discipline; no
  in-process storelib import in the test files themselves);
- the node driver ``tests/fixtures/deepseek/scenario.mjs`` is spawned as a
  SUBPROCESS with a bounded 90 s budget (kill + fail on expiry) and its
  ONE stdout JSON line parsed;
- the dsh runtime resolves from ``deepseek-plugin/node_modules`` or env
  ``ZMEM_DEEPSEEK_RUNTIME``; when genuinely absent the runtime legs
  ``pytest.skip("dsh runtime not installed")`` (the frozen test-command
  contract — the claim-gate checks independently require the committed
  canary/support reports, so CI cannot vacuously pass);
- the reachable-hub legs spawn the real MCP hub child
  (tests/support/t08_hub.py) with the ``T08_HUB <port>`` handshake poll.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    import pytest
except ImportError:  # pragma: no cover - CI installs pytest; bare boxes skip
    pytest = None

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
DRIFT_PY = SCRIPTS_DIR / "drift.py"
HUB_PY = REPO_ROOT / "tests" / "support" / "t08_hub.py"
SCENARIO_MJS = REPO_ROOT / "tests" / "fixtures" / "deepseek" / "scenario.mjs"
PLUGIN_DIR = REPO_ROOT / "deepseek-plugin"
RELEASE_MANIFEST = REPO_ROOT / "release-manifest.json"
CANARY_REPORT = REPO_ROOT / "docs" / "host-gate-evidence" / "deepseek" / "canary-report.json"
SUPPORT_REPORT = REPO_ROOT / "docs" / "host-gate-evidence" / "deepseek" / "support-report.json"

HUB_TOKEN = "t08-hub-token"

# The canonical release-manifest set after the adapter lands (seven today
# per scripts/refresh_hosts.py CANONICAL_MANIFESTS plus the new eighth).
CANONICAL_MANIFESTS = (
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".zcode-plugin/plugin.json",
    "hermes-plugin/plugin.yaml",
    "marketplace.json",
    "deepseek-plugin/plugin.json",
)

STRIP_KEYS = ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
              "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT",
              "PLUGIN_DATA")

SUBPROC_TIMEOUT_S = 90      # kill the node driver child after 90 s
HUB_HANDSHAKE_TIMEOUT_S = 120.0
STORE_SUBPROC_TIMEOUT_S = 60


def release_version() -> str:
    data = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    return str(data["version"])


def node_binary() -> "str | None":
    return shutil.which("node")


def operator_store_fingerprint() -> "tuple[int | None, int | None]":
    """(size, row-count) of the operator's DEFAULT store — host.py's
    box-neutral ``~/.zmem/store.sqlite`` (resolve_store_path priority 5),
    resolved WITHOUT importing host.py (no storelib import in the test
    process; the path is the documented default). Tests compare the
    fingerprint around a run to prove the isolated pins never let the
    launcher's ~/.zmem default engage. (None, None) when the default
    store does not exist (nothing to protect, nothing written).

    Amended twice for host-coupled flapping on boxes running the LIVE
    ambient zmem daemon: (1) mtime_ns dropped — every unrelated hook
    fire moves it; (2) the size member dropped — ambient recall bumps
    counters and rewrites pages, so the byte size drifts without any row
    change. The remaining member is the memory ROW COUNT under a
    read-only connection: recall-only ambient activity never changes it,
    and any real leak's write path lands rows. Pair this with the
    state.storePathUsed binding (the resolved store must sit inside the
    isolated tmp) for path-level discrimination."""
    path = Path.home() / ".zmem" / "store.sqlite"
    try:
        st = path.stat()
    except OSError:
        return (None, None)
    rows = None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        rows = None
    return (rows,)


def _resolve_runtime_dir() -> "str | None":
    candidates = []
    env_val = os.environ.get("ZMEM_DEEPSEEK_RUNTIME")
    if env_val:
        candidates.append(Path(env_val))
        candidates.append(Path(env_val) / "node_modules")
    candidates.append(PLUGIN_DIR / "node_modules")
    for cand in candidates:
        if (cand / "@deepseek-ai" / "dsh-agent-loop").is_dir():
            return str(cand)
    return None


# Resolved ONCE at harness import — BEFORE any isolated_env strips the
# ambient ZMEM_* (the runtime legs must still resolve after the pin).
_RUNTIME_DIR = _resolve_runtime_dir()


def runtime_dir() -> "str | None":
    """The dsh runtime location (env ZMEM_DEEPSEEK_RUNTIME, either a
    node_modules dir itself or a dir containing one, else
    deepseek-plugin/node_modules); None when the @deepseek-ai packages are
    genuinely unavailable (the runtime legs then skip)."""
    return _RUNTIME_DIR or _resolve_runtime_dir()


def require_runtime() -> None:
    if node_binary() is None or runtime_dir() is None:
        raise runtime_skip("dsh runtime not installed")


class _RuntimeUnavailable(Exception):
    """Raised when the dsh runtime is absent and no pytest skip class is
    importable — the standalone runners translate this into a visible
    SKIP line, never a crash or a silent pass."""


def runtime_skip(message: str) -> Exception:
    """The skip exception for THIS interpreter: pytest.skip.Exception when
    pytest is importable (the class pytest.skip actually raises —
    pytest.Skipped does not exist), else the local fallback."""
    if pytest is not None:
        exc = getattr(getattr(pytest, "skip", None), "Exception", None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            return exc(message)
    return _RuntimeUnavailable(message)


def skip_exceptions() -> tuple:
    """The except-able skip classes for main_runner (never an empty tuple:
    ``except ((),)`` raises TypeError at match time)."""
    return (_RuntimeUnavailable, pytest.skip.Exception) \
        if pytest is not None else (_RuntimeUnavailable,)


def purge_cached_modules() -> None:
    """Drop env-derived module singletons (the t06..t11 discipline)."""
    prefixes = ("fleet.", "storelib.", "store.", "auth.", "bind_guard.")
    exact = ("mcp_server", "fleet", "host", "redaction", "schema_meta",
             "store", "auth", "bind_guard", "drift")
    for name in [n for n in list(sys.modules)
                 if n in exact or n.startswith(prefixes)]:
        del sys.modules[name]


@contextlib.contextmanager
def isolated_env(prefix: str):
    """Per-test GUID scratch + ambient ZMEM_* strip/restore (no storelib
    import happens in the test process; the pin protects any in-process
    importlib legs like the drift coverage check)."""
    tmp = tempfile.mkdtemp(prefix=f"zmem-ds-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: v for k, v in os.environ.items()
             if k.startswith("ZMEM_") or k in STRIP_KEYS}
    for key in list(saved):
        os.environ.pop(key, None)
    purge_cached_modules()
    try:
        yield Path(tmp)
    finally:
        purge_cached_modules()
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            os.environ[key] = value


def scenario_env(state_path: Path, extra: dict) -> dict:
    """The node driver env: ambient ZMEM_* stripped, the frozen pins set
    (PATH and system vars survive from the ambient copy). Every child env
    carries a NONEXISTENT ZMEM_MODELS_DIR plus ZMEM_MODEL_AUTODOWNLOAD=0
    (no model downloads, embeddings degrade to FTS — the store-path-freeze
    discipline). When the caller maps a plugin-data dir for a remote leg
    (ZCODE_PLUGIN_DATA / CLAUDE_PLUGIN_DATA), ZMEM_DATA is mirrored to the
    same dir so the launcher's ~/.zmem default can never engage."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_") and k not in STRIP_KEYS}
    env.update({
        "ZMEM_ROOT": str(REPO_ROOT),
        "ZMEM_HOME": str(REPO_ROOT),
        "PYTHON": sys.executable,
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_DEEPSEEK_STATE": str(state_path),
        "ZMEM_MODELS_DIR": str(Path(state_path).parent / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_NAMESPACE": "user:global",
    })
    rt = runtime_dir()
    if rt:
        env["ZMEM_DEEPSEEK_RUNTIME"] = rt
    env.update({k: str(v) for k, v in extra.items()})
    plugin_data = env.get("ZCODE_PLUGIN_DATA") or env.get("CLAUDE_PLUGIN_DATA")
    if plugin_data and not env.get("ZMEM_DATA"):
        env["ZMEM_DATA"] = plugin_data
    return env


def parse_driver_stdout(stdout: str) -> "dict | None":
    """The driver's ONE JSON stdout line (last line carrying an 'ok' key —
    tolerant of stray runtime chatter, though the contract is one line)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def run_scenario(scenario: str, state_path: Path, extra_env: dict | None = None,
                 timeout_s: int = SUBPROC_TIMEOUT_S) -> dict:
    """Spawn the frozen node driver; return its parsed stdout payload.

    Bounded: the child is killed at ``timeout_s`` (fail, never hang). A
    payload with ok=false (the base-tree plugin-import RED) is RETURNED —
    the caller's ``ok_or_red`` converts it to the failing assertion."""
    env = scenario_env(state_path, extra_env or {})
    env["SCENARIO"] = scenario
    node = node_binary()
    if node is None:
        raise AssertionError("node binary not found on PATH")
    try:
        proc = subprocess.run(
            [node, str(SCENARIO_MJS)], capture_output=True, text=True,
            env=env, cwd=str(REPO_ROOT), timeout=timeout_s,
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"the scenario driver exceeded its {timeout_s}s budget "
            f"(SCENARIO={scenario}) — killed; the run must fail fast, "
            f"never hang") from None
    payload = parse_driver_stdout(proc.stdout)
    if payload is None:
        tail = (proc.stderr or "")[-500:]
        raise AssertionError(
            f"the scenario driver wrote no JSON line (SCENARIO={scenario}, "
            f"rc={proc.returncode}); stderr tail: {tail!r}")
    return payload


def ok_or_red(payload: dict, scenario: str) -> dict:
    """Convert a fatal-boot payload to the failing assertion that names
    the intended RED (at base: deepseek-plugin/index.mjs missing)."""
    if payload.get("ok") is not True:
        raise AssertionError(
            f"SCENARIO={scenario} reported a fatal boot error (the frozen "
            f"driver never decides pass/fail — this is the arm's-length "
            f"RED): {payload.get('error')!r}")
    observed = payload.get("observed")
    if not isinstance(observed, dict):
        raise AssertionError(
            f"SCENARIO={scenario} reported ok without an observed dict: "
            f"{payload!r}")
    return observed


def read_state(state_path: Path) -> "dict | None":
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def queue_entries(queue: Path) -> list:
    """The durable op-queue JSONL entries (fleet/opqueue.py layout)."""
    return [json.loads(line) for line
            in queue.read_text(encoding="utf-8").splitlines() if line.strip()]


def deepseek_agent_state(state, needle: str):
    """Tolerant walk for the plugin state's per-session/per-agent section
    (the interface freeze pins the driver<->plugin RESULT contract, not
    the state file's inner nesting): the first dict that either is stored
    under the key ``needle`` (a session id) or carries a session-ish field
    equal to it."""
    if not isinstance(state, dict):
        return None
    stack = [state]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        if needle in cur:
            found = cur[needle]
            return found if isinstance(found, dict) else {"value": found}
        for key in ("session", "sessionId", "agent", "agentId"):
            if cur.get(key) == needle:
                return cur
        for value in cur.values():
            if isinstance(value, (dict, list)):
                stack.append(value)
    return None


def dead_port_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/mcp"


def store_env(store_path: Path, data_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_") and k not in STRIP_KEYS}
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": str(store_path),
        "ZMEM_DATA": str(data_dir),
        "ZMEM_MODELS_DIR": str(data_dir / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def seed_store(store_path: Path, data_dir: Path,
               rows: list[tuple[str, str, str]]) -> None:
    """Seed an isolated store via the public store.py add CLI ((ns, type,
    content) rows; confidence pinned 0.9 so every recall lane can see
    them — the t08/t11 seeding note)."""
    env = store_env(store_path, data_dir)
    for namespace, item_type, content in rows:
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "add", "--namespace", namespace,
             "--type", item_type, "--content", content,
             "--confidence", "0.9", "--json"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL, timeout=STORE_SUBPROC_TIMEOUT_S)
        if proc.returncode != 0:
            raise AssertionError(
                f"TEST HARNESS failure (not an adapter verdict): store.py "
                f"add seeding failed rc={proc.returncode}; stderr="
                f"{(proc.stderr or '')[-300:]!r}")


def build_snapshot(source_store: Path, data_dir: Path,
                   namespace: str = "user:global") -> Path:
    """Build the offline snapshot pair at ``<data>/broker/`` through the
    PUBLIC fleet/snapshot.py build_snapshot seam (a subprocess so the
    test process never imports storelib; the frozen T-11 builder surface).
    """
    broker_dir = data_dir / "broker"
    snippet = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from fleet.snapshot import build_snapshot; "
        "build_snapshot(source_store=r'%s', snapshot_dir=r'%s', "
        "namespace=r'%s', env=None)" % (SCRIPTS_DIR, source_store,
                                        broker_dir, namespace))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_") and k not in STRIP_KEYS}
    env.update({"PYTHONIOENCODING": "utf-8",
                "ZMEM_MODELS_DIR": str(data_dir / "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0"})
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", snippet],
                          capture_output=True, text=True, env=env,
                          cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
                          timeout=STORE_SUBPROC_TIMEOUT_S)
    if proc.returncode != 0:
        raise AssertionError(
            f"TEST HARNESS failure (not an adapter verdict): the T-11 "
            f"snapshot builder subprocess failed rc={proc.returncode}; "
            f"stderr={(proc.stderr or '')[-300:]!r}")
    return broker_dir / "snapshot.sqlite"


class Hub:
    """The real MCP hub child (tests/support/t08_hub.py) with the
    T08_HUB <port> handshake poll — the t11 _Hub pattern."""

    def __init__(self, hub_dir: Path):
        self._hub_dir = hub_dir
        self.url = ""
        self._proc: "subprocess.Popen | None" = None
        self._out_path = hub_dir / "hub-stdout.log"
        self._err_path = hub_dir / "hub-stderr.log"

    def __enter__(self) -> "Hub":
        self._hub_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_") and k not in STRIP_KEYS}
        env.update({"ZMEM_MODELS_DIR": str(self._hub_dir / "missing-models"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"})
        out = open(self._out_path, "ab")
        err = open(self._err_path, "ab")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(HUB_PY),
                 "--store", str(self._hub_dir / "store.sqlite"),
                 "--namespace", "user:global"],
                stdin=subprocess.PIPE, stdout=out, stderr=err,
                env=env, cwd=str(REPO_ROOT))
        finally:
            out.close()
            err.close()
        handshake = ""
        deadline = time.monotonic() + HUB_HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            text = (self._out_path.read_text(encoding="utf-8",
                                             errors="replace")
                    if self._out_path.exists() else "")
            nl = text.find("\n")
            if nl >= 0:
                handshake = text[:nl].strip()
                break
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        if not re.match(r"^T08_HUB \d+$", handshake or ""):
            self._shutdown()
            stderr_tail = (self._err_path.read_text(
                encoding="utf-8", errors="replace")
                if self._err_path.exists() else "")[-500:]
            raise AssertionError(
                f"TEST HARNESS failure (not an adapter verdict): the t08 "
                f"hub helper never reached its T08_HUB handshake (got "
                f"{handshake!r}); hub stderr tail: {stderr_tail!r}")
        port = handshake.split(" ")[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    def _shutdown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def __exit__(self, *exc) -> None:
        self._shutdown()


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:  # pragma: no cover - CI installs requirements.txt
        return False


def tail(text: str, limit: int = 300) -> str:
    return (text or "").strip()[-limit:]


def main_runner(checks: dict, argv: list[str]) -> int:
    """The t-series standalone runner convention: no argument runs all,
    a name runs one, unknown selector prints NO CHECK and exits 2."""
    selectors = argv[1:]
    for sel in selectors:
        if sel not in checks:
            print(f"NO CHECK {sel}", file=sys.stderr)
            return 2
    selected = [checks[s] for s in selectors] if selectors else list(
        checks.values())
    skip_exc = skip_exceptions()
    failures = []
    for check in selected:
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
        except skip_exc as exc:  # noqa: BLE001 - visible skip, not failure
            print(f"SKIP {check.__name__}: {exc}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0
