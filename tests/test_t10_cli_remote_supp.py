"""Issue #289 (T PR 10) SUPPLEMENTARY (unfrozen) pins — the 05-fix-plan
REVISION 4 verification-plan rows beyond the frozen C1-C4 file.

Every pin here mirrors tests/test_t10_cli_remote.py's env discipline:
storelib freezes STORE_PATH at first import, so every remote-mode CLI leg
runs store.py as a SUBPROCESS whose env strips EVERY ambient ZMEM_*
variable (plus both plugin-data channels) BEFORE pinning only what the
pin needs; remote legs pin the data dir via ZCODE_PLUGIN_DATA (or, for
the HOOK-env band, ZMEM_DATA — legal alone in remote mode) and set
NEITHER ZMEM_STORE (that combination is a config error by design). The
in-process legs (Hermes routing, _parse_results, compat hooks) purge the
env-derived module singletons on entry AND exit and restore os.environ
in finally. No storelib/fleet/mcp_server import happens at module level.

Pins (one test function each, numbered per the supp spec):

1.  HOOK-ENV band — ZMEM_DATA + ZMEM_REMOTE_URL (the launcher exports
    ZMEM_DATA to every hook child): source-exists / ops-append /
    ledger-clear execute, none config-errors, none creates store.sqlite.
2.  Config error — ZMEM_STORE + ZMEM_REMOTE_URL exits 2 naming
    ZMEM_STORE AND ZMEM_DATA AND ZMEM_REMOTE_URL.
3.  REFUSE band beyond the frozen six — backup, evidence write,
    operation-feedback, apply-ops, prefetch, hermes-convention: exit 2
    with the exact refusal phrase, no store.sqlite.
4.  Write flag refusals — add --confidence, add --type state, update
    --taint, invalidate --expected-namespace, update --reason (explicit).
5.  LOCAL --reason semantics — the parser-default None sentinel keeps
    `update --reason ""` refused locally while a plain update still
    succeeds (default "updated").
6.  Read flag refusals (recall --min-confidence, recent --for-injection,
    search --exact) + the POSITIVE remote search pin against the real
    t08 hub (7-key envelope, >=1 hub row).
7.  `--content -` stdin — the piped text is the queued op's content.
8.  Capture-mode resolution — ZMEM_CAPTURE_MODE=manual lands on the
    queued op without the flag.
9.  Overlay ordering — pending add + reachable hub: first recall shows
    BOTH a hub row and [PENDING]; the second recall (queue flushed by
    the first) has no [PENDING].
10. Unreachable error contract — dead port: exit 1, stdout JSON
    {ok:false, reason:hub-unreachable}, non-empty stderr, no store.
11. Hermes MCP-mode routing — supersede/update/invalidate/search route
    through the transport recorder (never _run_store); session_start
    forwards the caller's limit kwarg; the add response mapping returns
    a JSON string carrying result/id.
12. _parse_results arms — arms passthrough + None→0 int normalization.
13. Compat-hook guards — reflect's _run_hermes_context ack-failure skip
    (commit-cursor still spawns) and convention's hermes-convention /
    evidence-write skips under ZMEM_REMOTE_URL.
14. Broker-fallback pin — the recall flag shape hooks/lib/
    zmem-recall-body.py uses is refused under ZMEM_REMOTE_URL (rc!=0 is
    the body's fail-open empty signal) and creates no store.
15. update --evidence refused in remote mode — the parser-dest fix: the
    write-route refusal names --evidence and enqueues nothing (before the
    dest alignment the flag silently queued).
16. POSITIVE remote list against the real t08 hub — `list --namespace
    project:fixture-280 --json` exits 0 with the exact 3-key envelope
    {namespace, count, memories} and >=1 row carrying
    id/namespace/type/preview.
17. get dead-port reason — the transport's connect-level reason survives
    the get error mapping: stdout JSON ok:false with reason
    hub-unreachable (hub-refused stays reserved for hub-ANSWERED
    refusals).
18. recall --delegation-prompt and recent --legacy-unscoped — both
    passive-marker reads refuse (exit 2) naming the flag.
19. HOOK-ENV band part 2 — retain (the correction-queue review sidecar
    writer; RETAIN_HOSTS has no "probe", so the minimal valid host is
    zcode) and delivery-clear execute under ZMEM_DATA+ZMEM_REMOTE_URL:
    exit 0, no config error, no store.sqlite.

Runs under pytest (``python -m pytest tests/test_t10_cli_remote_supp.py``)
and standalone (``python tests/test_t10_cli_remote_supp.py`` — no
argument runs EVERY pin; a name fragment selects one; unknown selector
exits 2). Hub-dependent pins skip loudly only when the mcp package is
absent (CI installs hermes-plugin/server/requirements.txt).
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
HOOKS_DIR = REPO_ROOT / "hermes-plugin" / "hooks"
HUB_PY = REPO_ROOT / "tests" / "support" / "t08_hub.py"
SEED_JSONL = REPO_ROOT / "tests" / "fixtures" / "fleet" / "seed.jsonl"

HUB_TOKEN = "t08-hub-token"
SEED_NAMESPACE = "project:fixture-280"
SEED_MARKER = "Fleet parity seed"
REFUSAL_PHRASE = "this command runs on the hub; ZMEM_REMOTE_URL is set"
REMOTE_READ_ENVELOPE_KEYS = {
    "results", "count", "omitted", "injection_risk",
    "tokens_used", "tokens_budget", "arms",
}

# Subprocess budgets: the supp spec pins 30s per store.py spawn (the
# enqueue legs are local durable writes and never wait on a hub); the
# hub handshake includes replaying the fleet fixture seed through
# store.py before the T08_HUB line, so it keeps the frozen file's
# magnitude.
_SUBPROC_TIMEOUT_S = 30
_HUB_HANDSHAKE_TIMEOUT_S = 120.0

# Modules whose cached state is env-derived (STORE_PATH freezes at first
# import) — purged on entry AND exit of every pinned in-process context
# (the t09/t10 frozen discipline), plus the by-path module names the
# Hermes plugin loader and this file's own loaders register.
_PURGE_EXACT = (
    "mcp_server", "fleet", "host", "redaction", "schema_meta", "store",
    "auth", "bind_guard", "agent", "agent.memory_provider",
    "zmem_hermes_transport", "zmem_fleet_transport",
    "zmem_mcp_client_transport", "zmem_schema_meta",
)
_PURGE_PREFIXES = ("fleet.", "storelib.", "store.", "auth.", "bind_guard.",
                   "zmem_t10_supp_")


def _purge_cached_modules() -> None:
    """Drop env-derived module singletons so no sibling pin inherits a
    stale STORE_PATH pinned under a previous scratch."""
    doomed = [name for name in sys.modules
              if name in _PURGE_EXACT
              or name.startswith(_PURGE_PREFIXES)]
    for name in doomed:
        del sys.modules[name]


def _strip_config_env(target: dict) -> None:
    """Remove every ambient var the store-resolution / remote-mode chain
    reads (the full ZMEM_* prefix sweep plus BOTH plugin-data channels,
    which outrank the ~ fallbacks in host.resolve_store_path)."""
    for key in list(target):
        if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                              "ZCODE_PLUGIN_DATA"):
            del target[key]


def _pinned_scratch(prefix: str) -> str:
    """A fresh GUID scratch dir name (caller owns cleanup)."""
    return tempfile.mkdtemp(prefix=f"zmem-t10s-{prefix}-{uuid.uuid4().hex}-")


def _base_env(tmp: str) -> dict:
    """The stripped common pin every subprocess leg starts from."""
    env = {k: v for k, v in os.environ.items()}
    _strip_config_env(env)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
    })
    return env


def _remote_subproc_env(tmp: str, remote_url: str, token: str | None = None,
                        extra: dict | None = None) -> dict:
    """A stripped subprocess env for a REMOTE-mode store.py run. Sets
    NEITHER ZMEM_STORE NOR ZMEM_DATA by default (that combination is a
    config error in remote mode); `extra` lets a pin add ZMEM_DATA (the
    hook band) or ZMEM_CAPTURE_MODE on top."""
    env = _base_env(tmp)
    env["ZMEM_REMOTE_URL"] = remote_url
    if token is not None:
        env["ZMEM_MCP_TOKEN"] = token
    if extra:
        env.update(extra)
    return env


def _local_subproc_env(tmp: str) -> dict:
    """A stripped subprocess env pinning a LOCAL scratch store (the t09
    discipline; only the local-semantics pins use it)."""
    env = _base_env(tmp)
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
    })
    return env


def _run_store_env(env: dict, *argv: str,
                   input_text: str | None = None) -> subprocess.CompletedProcess:
    """Run the real store.py CLI with a caller-built env (cwd pinned at
    the repo root like every t-series check; 30s budget per spawn)."""
    return subprocess.run(
        [sys.executable, str(STORE_PY), *argv],
        capture_output=True, text=True, env=env,
        input=input_text, cwd=str(REPO_ROOT), timeout=_SUBPROC_TIMEOUT_S)


def _dead_port_url() -> str:
    """A bound-then-closed loopback port: nothing listens, so every hub
    contact fails to connect (remote mode without a live hub)."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/mcp"


def _tail(text: str, limit: int = 400) -> str:
    return (text or "").strip()[-limit:]


def _last_json_line(text: str):
    last = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict):
            last = obj
    return last


def _first_json_object(text: str):
    """The first parseable JSON object in stdout (multi-line indented
    envelopes; raw_decode ignores whatever trails the object)."""
    lines = (text or "").splitlines()
    decoder = json.JSONDecoder()
    for index, line in enumerate(lines):
        if not line.strip().startswith("{"):
            continue
        remainder = "\n".join(lines[index:]).lstrip()
        try:
            obj, _end = decoder.raw_decode(remainder)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _queue_entries(data_dir: str) -> list[dict]:
    path = Path(data_dir) / "queue" / "ops-local.jsonl"
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _store_exists(data_dir: str) -> bool:
    return (Path(data_dir) / "store.sqlite").exists()


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:  # pragma: no cover - CI installs requirements.txt
        return False


class _Hub:
    """The real in-process MCP hub (tests/support/t08_hub.py) as a child
    process — the frozen file's launcher, copied verbatim in shape:
    spawn with stdin held open, poll the ONE ``T08_HUB <port>`` handshake
    line, shut down by closing stdin (then killing, belt and
    suspenders)."""

    def __init__(self, hub_dir: str):
        self._hub_dir = hub_dir
        self.url = ""
        self._proc: subprocess.Popen | None = None
        self._out_path = os.path.join(hub_dir, "hub-stdout.log")
        self._err_path = os.path.join(hub_dir, "hub-stderr.log")

    def __enter__(self) -> "_Hub":
        Path(self._hub_dir).mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()}
        _strip_config_env(env)  # the hub pins its own store; start clean
        out = open(self._out_path, "ab")
        err = open(self._err_path, "ab")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(HUB_PY),
                 "--store", os.path.join(self._hub_dir, "store.sqlite"),
                 "--seed", str(SEED_JSONL),
                 "--namespace", SEED_NAMESPACE],
                stdin=subprocess.PIPE, stdout=out, stderr=err,
                env=env, cwd=str(REPO_ROOT))
        finally:
            out.close()
            err.close()

        handshake = ""
        deadline = time.monotonic() + _HUB_HANDSHAKE_TIMEOUT_S
        import re
        while time.monotonic() < deadline:
            text = ""
            if os.path.exists(self._out_path):
                text = Path(self._out_path).read_text(
                    encoding="utf-8", errors="replace")
            nl = text.find("\n")
            if nl >= 0:
                handshake = text[:nl].strip()
                break
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        if not re.match(r"^T08_HUB \d+$", handshake or ""):
            stderr_tail = ""
            if os.path.exists(self._err_path):
                stderr_tail = _tail(Path(self._err_path).read_text(
                    encoding="utf-8", errors="replace"), 800)
            self._shutdown()
            raise AssertionError(
                f"the t08 hub helper never reached its T08_HUB handshake "
                f"(got {handshake!r}; hub exit="
                f"{self._proc.returncode if self._proc else '?'}; "
                f"stderr tail: {stderr_tail!r}) — test harness failure, "
                f"not an issue #289 verdict")
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
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    def __exit__(self, *exc) -> None:
        self._shutdown()


def _load_module_by_path(name: str, path: Path):
    """Import a repo file under a private module name (the frozen
    _load_provider_module pattern, generalized for the hook scripts)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_provider_module(name: str):
    """Import hermes-plugin/__init__.py the tests/test_t10_cli_remote.py
    way: by path, with the Hermes ``agent.memory_provider`` ABC stubbed as
    a minimal stand-in (the hyphenated plugin dir is not a package)."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # the tests/test_hermes_transport.py stand-in
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules["agent"] = agent
    sys.modules["agent.memory_provider"] = mp
    return _load_module_by_path(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")


# -- pin 1 ---------------------------------------------------------------------

def test_hook_env_band_allow_local_commands_execute_under_zmem_data():
    """ZMEM_DATA + ZMEM_REMOTE_URL (the launcher exports ZMEM_DATA to
    every hook child): the ALLOW-LOCAL commands execute and never
    config-error; none creates a store.sqlite."""
    tmp = _pinned_scratch("p1")
    try:
        remote_url = _dead_port_url()
        env = _remote_subproc_env(tmp, remote_url, extra={"ZMEM_DATA": tmp})

        # source-exists: read-only mode=ro probe; an absent store answers
        # {"exists":false} WITHOUT creating one.
        proc = _run_store_env(
            env, "source-exists", "--namespace", "user:global",
            "--source-ref", "session:x")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"source-exists under ZMEM_DATA+ZMEM_REMOTE_URL must execute "
            f"(issue #289 hook band), rc={proc.returncode} "
            f"stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"ZMEM_DATA alone is legal in remote mode — source-exists must "
            f"not config-error; output={_tail(combined)!r}")
        answer = _last_json_line(proc.stdout)
        assert isinstance(answer, dict) and answer.get("exists") is False, (
            f"source-exists on an absent store must answer exists:false, "
            f"got stdout={_tail(proc.stdout)!r}")
        assert not _store_exists(tmp), (
            "source-exists must not create store.sqlite (read-only probe)")

        # ops-append: writes the operation-ring SIDECAR (ops/<stem>.log),
        # never SQLite. A bare "bash" is NOT a runner head
        # (ops_tokens._RUNNER_HEADS), so it derives no token and the
        # append is a declared-successful no-op — the literal task shape
        # is pinned for exit 0, then a token-deriving runner command
        # ("git status") pins the actual sidecar write for session "s".
        proc = _run_store_env(
            env, "ops-append", "--namespace", "user:global",
            "--session-id", "s", "--tool", "Bash", "--operation", "bash")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"ops-append under ZMEM_DATA+ZMEM_REMOTE_URL must execute "
            f"(the no-token derivation is a declared successful no-op), "
            f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"ops-append must not config-error under ZMEM_DATA+remote; "
            f"output={_tail(combined)!r}")

        proc = _run_store_env(
            env, "ops-append", "--namespace", "user:global",
            "--session-id", "s", "--tool", "Bash",
            "--operation", "git status")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"ops-append under ZMEM_DATA+ZMEM_REMOTE_URL must execute, "
            f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"ops-append must not config-error under ZMEM_DATA+remote; "
            f"output={_tail(combined)!r}")
        stem = hashlib.sha256(b"s").hexdigest()[:32]
        ring = Path(tmp) / "ops" / f"{stem}.log"
        assert ring.is_file(), (
            f"ops-append must write the operation-ring sidecar at {ring} "
            f"(storelib/ops_tokens.append_ops_ring); it is absent")
        assert not _store_exists(tmp), (
            "ops-append must never create store.sqlite (sidecar-only)")

        # ledger-clear: the delivery-ledger sidecar command, exit 0.
        proc = _run_store_env(env, "ledger-clear", "--session-id", "old")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"ledger-clear under ZMEM_DATA+ZMEM_REMOTE_URL must execute, "
            f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"ledger-clear must not config-error under ZMEM_DATA+remote; "
            f"output={_tail(combined)!r}")
        assert not _store_exists(tmp), (
            "ledger-clear must never create store.sqlite (sidecar-only)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 2 ---------------------------------------------------------------------

def test_config_error_names_store_data_and_remote_url():
    """ZMEM_STORE + ZMEM_REMOTE_URL is a config error: exit 2 with a
    stderr message naming ZMEM_STORE AND ZMEM_DATA AND ZMEM_REMOTE_URL."""
    tmp = _pinned_scratch("p2")
    try:
        env = _remote_subproc_env(
            tmp, _dead_port_url(),
            extra={"ZMEM_STORE": os.path.join(tmp, "store.sqlite")})
        proc = _run_store_env(
            env, "add", "--namespace", "user:global", "--type", "fact",
            "--content", "t10 supp config-error probe row")
        assert proc.returncode == 2, (
            f"ZMEM_STORE + ZMEM_REMOTE_URL must exit 2 (config error), "
            f"got {proc.returncode}; stderr={_tail(proc.stderr)!r}")
        stderr = proc.stderr or ""
        for var in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_REMOTE_URL"):
            assert var in stderr, (
                f"the remote-mode config error must name {var} (issue #289 "
                f"R2-1); stderr={_tail(stderr)!r}")
        assert not _store_exists(tmp), (
            "the config-error refusal must fire before any store access")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 3 ---------------------------------------------------------------------

def test_refuse_band_beyond_frozen_six_refuses_and_never_creates_store():
    """backup / evidence write / operation-feedback / apply-ops /
    hermes-convention: exit 2 with the exact refusal phrase, no
    store.sqlite. prefetch left the band in #374 (offline serve)."""
    tmp = _pinned_scratch("p3")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        # Minimal VALID argv per command (read from storelib/cli.py's
        # argparse): backup is flagless-legal; evidence write takes its
        # subcommand (the gate refuses before the stdin read, input=""
        # keeps the pipe honest); operation-feedback requires
        # --session-id/--event-id/--outcome; apply-ops requires --in
        # (point it at an empty scratch file); prefetch requires
        # --query/--namespace/--session-id/--moment; hermes-convention is
        # flagless (stdin payload, also never reached).
        batch = os.path.join(tmp, "empty-ops.jsonl")
        Path(batch).write_text("", encoding="utf-8")
        cases = [
            ("backup", []),
            ("evidence", ["write"]),
            ("operation-feedback",
             ["--session-id", "s", "--event-id", "evt-1",
              "--outcome", "success"]),
            ("apply-ops", ["--in", batch]),
            ("hermes-convention", []),
        ]
        for command, extra in cases:
            proc = _run_store_env(env, command, *extra, input_text="")
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert REFUSAL_PHRASE in combined, (
                f"'{command}' in remote mode must refuse with the exact "
                f"phrase {REFUSAL_PHRASE!r}; rc={proc.returncode} "
                f"stdout={_tail(proc.stdout)!r} stderr={_tail(proc.stderr)!r}")
            assert proc.returncode == 2, (
                f"'{command}' in remote mode must exit 2, got "
                f"{proc.returncode}")
            assert not _store_exists(tmp), (
                f"'{command}' touched the local store ({tmp}/store.sqlite "
                f"exists) — a remote-mode refusal must happen before any "
                f"store access")
        # prefetch left the refuse band in #374: remote prefetch now
        # routes to the hub and, when the hub is unreachable, serves the
        # T-11 offline snapshot envelope (rc 0, reason hub-unreachable)
        # WITHOUT ever creating a local store.
        proc = _run_store_env(
            env, "prefetch", "--query", "q", "--namespace", "user:global",
            "--session-id", "s", "--moment", "user_prompt", input_text="")
        assert proc.returncode == 0, (
            f"prefetch against an unreachable hub must serve the offline "
            f"envelope (issue #374); rc={proc.returncode} "
            f"stdout={_tail(proc.stdout)!r} stderr={_tail(proc.stderr)!r}")
        assert "hub-unreachable" in (proc.stdout or ""), (
            f"the offline envelope must carry reason hub-unreachable; "
            f"stdout={_tail(proc.stdout)!r}")
        assert not _store_exists(tmp), (
            "the offline prefetch serve must never create a local store")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 4 ---------------------------------------------------------------------

def test_write_flag_refusals_name_the_refused_flag():
    """Remote write-route flag refusals: each exits 2 naming the flag
    (or the --type value) the hub cannot carry."""
    tmp = _pinned_scratch("p4")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        some_id = uuid.uuid4().hex
        cases = [
            ("add --confidence",
             ["add", "--namespace", "user:global", "--type", "fact",
              "--content", "flag refusal probe", "--confidence", "0.9"],
             "--confidence"),
            ("add --type state",
             ["add", "--namespace", "user:global", "--type", "state",
              "--content", "flag refusal probe"],
             "state"),
            ("update --taint",
             ["update", "--id", some_id, "--content",
              "replacement body for the taint refusal probe",
              "--taint", "trusted_internal"],
             "--taint"),
            ("invalidate --expected-namespace",
             ["invalidate", "--id", some_id, "--reason",
              "no longer true on the remote box",
              "--expected-namespace", "user:global"],
             "--expected-namespace"),
            ("update --reason explicit",
             ["update", "--id", some_id, "--content",
              "replacement body for the reason refusal probe",
              "--reason", "explicit correction"],
             "--reason"),
        ]
        for label, argv, needle in cases:
            proc = _run_store_env(env, *argv)
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert proc.returncode == 2, (
                f"remote {label} must exit 2 (flag refused up front), "
                f"got {proc.returncode}; stderr={_tail(proc.stderr)!r}")
            assert needle in combined, (
                f"remote {label} refusal must name {needle!r}; "
                f"output={_tail(combined)!r}")
            assert not _store_exists(tmp), (
                f"remote {label} refusal must happen before any store "
                f"access")
        # No flag refusal ever enqueued anything either.
        assert _queue_entries(tmp) == [], (
            "a refused write flag must refuse BEFORE enqueue (no queue "
            "side effects)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 5 ---------------------------------------------------------------------

def test_local_update_reason_empty_refusal_and_default_reason_survive():
    """LOCAL mode: the `is not None` parser sentinel preserves the
    historical `update --reason \"\"` refusal while a plain update (no
    --reason) still succeeds with the \"updated\" default."""
    tmp = _pinned_scratch("p5")
    try:
        local_dir = os.path.join(tmp, "local")
        os.makedirs(local_dir, exist_ok=True)
        env = _local_subproc_env(local_dir)

        seed = _run_store_env(
            env, "add", "--namespace", "user:global", "--type", "fact",
            "--content", "t10 supp local reason sentinel seed row",
            "--json")
        assert seed.returncode == 0, (
            f"local seed add failed ({seed.returncode}): "
            f"{_tail(seed.stderr)!r}")
        receipt = _last_json_line(seed.stdout)
        assert isinstance(receipt, dict) and receipt.get("id"), (
            f"local seed add must print an id receipt; "
            f"stdout={_tail(seed.stdout)!r}")
        row_id = receipt["id"]

        empty_reason = _run_store_env(
            env, "update", "--id", str(row_id), "--content",
            "replacement body that must be refused for a blank reason",
            "--reason", "")
        assert empty_reason.returncode == 2, (
            f"local `update --reason \"\"` must still exit 2 (the "
            f"`is not None` sentinel keeps the historical blank-reason "
            f"refusal); got {empty_reason.returncode}; "
            f"stderr={_tail(empty_reason.stderr)!r}")

        plain = _run_store_env(
            env, "update", "--id", str(row_id), "--content",
            "replacement body that must land under the default reason")
        assert plain.returncode == 0, (
            f"local plain update (no --reason) must still succeed with "
            f"the 'updated' default; rc={plain.returncode}; "
            f"stderr={_tail(plain.stderr)!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 6 ---------------------------------------------------------------------

def test_read_flag_refusals_and_positive_remote_search():
    """Remote read-route flag refusals (recall --min-confidence, recent
    --for-injection, search --exact) plus the POSITIVE hub search pin:
    the 7-key envelope with at least one hub row."""
    tmp = _pinned_scratch("p6")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        refused = [
            ("recall --min-confidence",
             ["recall", "--query", "fleet parity seed",
              "--min-confidence", "0.5"],
             "--min-confidence"),
            ("recent --for-injection",
             ["recent", "--for-injection"],
             "--for-injection"),
            ("search --exact",
             ["search", "--text", "fleet parity seed", "--exact"],
             "--exact"),
        ]
        for label, argv, needle in refused:
            proc = _run_store_env(env, *argv)
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert proc.returncode == 2, (
                f"remote {label} must exit 2 (read flag refused), got "
                f"{proc.returncode}; stderr={_tail(proc.stderr)!r}")
            assert needle in combined, (
                f"remote {label} refusal must name {needle!r}; "
                f"output={_tail(combined)!r}")
        assert not _store_exists(tmp), (
            "a remote read-flag refusal must happen before any store "
            "access")

        # POSITIVE leg: the real hub (fixture seed under
        # project:fixture-280) answers remote search --text --json with
        # the local search envelope's exact key set and >=1 hub row.
        if not _mcp_available():  # pragma: no cover - CI installs mcp
            print("SKIP: mcp package absent — the positive remote-search "
                  "pin needs the real hub")
            return
        hub_dir = os.path.join(tmp, "hub")
        client_dir = os.path.join(tmp, "client")
        os.makedirs(client_dir, exist_ok=True)
        with _Hub(hub_dir) as hub:
            proc = _run_store_env(
                _remote_subproc_env(client_dir, hub.url, HUB_TOKEN),
                "search", "--text", "fleet parity seed",
                "--namespace", SEED_NAMESPACE, "--json")
            assert proc.returncode == 0, (
                f"remote search with a reachable hub must exit 0; "
                f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
            envelope = _first_json_object(proc.stdout)
            assert isinstance(envelope, dict), (
                f"remote search --json must print a JSON envelope; "
                f"stdout={_tail(proc.stdout)!r}")
            results = envelope.get("results")
            assert isinstance(results, list) and len(results) >= 1, (
                f"remote search must return at least one hub row (the "
                f"seeded '{SEED_MARKER}' content exists only hub-side); "
                f"envelope={str(envelope)[:300]!r}")
            assert any(SEED_MARKER in str(row.get("content", ""))
                       for row in results if isinstance(row, dict)), (
                f"at least one hub row must carry the seeded "
                f"'{SEED_MARKER}' content; results={str(results)[:300]!r}")
            assert set(envelope) == REMOTE_READ_ENVELOPE_KEYS, (
                f"remote search must print the per-command 7-key envelope "
                f"(results/count/omitted/injection_risk/tokens_used/"
                f"tokens_budget/arms); got {sorted(envelope)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 7 ---------------------------------------------------------------------

def test_remote_add_content_dash_reads_stdin_into_queued_op():
    """`add --content -` under remote mode: the piped stdin text becomes
    the queued op's content verbatim (redaction-stable probe)."""
    tmp = _pinned_scratch("p7")
    try:
        piped = ("t10 supp stdin sentinel body for the queue route "
                 "round trip")
        proc = _run_store_env(
            _remote_subproc_env(tmp, _dead_port_url()),
            "add", "--namespace", "user:global", "--type", "fact",
            "--content", "-", "--json", input_text=piped)
        assert proc.returncode == 0, (
            f"remote add --content - must exit 0 (stdin resolved before "
            f"enqueue); rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        receipt = _last_json_line(proc.stdout)
        assert isinstance(receipt, dict) and receipt.get("op_id"), (
            f"the stdin add must still print an op_id receipt; "
            f"stdout={_tail(proc.stdout)!r}")
        entries = _queue_entries(tmp)
        assert len(entries) == 1, (
            f"exactly one queued op expected, got {len(entries)}")
        assert entries[0].get("args", {}).get("content") == piped, (
            f"the queued op's content must equal the piped stdin text "
            f"verbatim; got {entries[0].get('args', {}).get('content')!r}")
        assert not _store_exists(tmp), (
            "the stdin add must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_remote_update_content_dash_reads_stdin_into_queued_op():
    """PRR-001 (PR #400 review): `update --content -` under remote mode
    queues the piped stdin text verbatim — `_resolve_content` must run
    EXACTLY once per invocation (the double-read shape queued empty
    content behind a success receipt)."""
    tmp = _pinned_scratch("p7b")
    try:
        piped = ("t10 supp update stdin sentinel body: the piped "
                 "replacement must reach the queue verbatim")
        proc = _run_store_env(
            _remote_subproc_env(tmp, _dead_port_url()),
            "update", "--id", "prr001-target", "--content", "-",
            "--json", input_text=piped)
        assert proc.returncode == 0, (
            f"remote update --content - must exit 0 (stdin resolved before "
            f"enqueue); rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        receipt = _last_json_line(proc.stdout)
        assert isinstance(receipt, dict) and receipt.get("op_id"), (
            f"the stdin update must still print an op_id receipt; "
            f"stdout={_tail(proc.stdout)!r}")
        entries = _queue_entries(tmp)
        update_entries = [e for e in entries if e.get("op") == "update"]
        assert len(update_entries) == 1, (
            f"exactly one queued update expected, got "
            f"{len(update_entries)} (all: {[e.get('op') for e in entries]})")
        queued = update_entries[0].get("args", {}).get("content")
        assert queued == piped, (
            f"PRR-001: the queued update's content must equal the piped "
            f"stdin text verbatim (a second stdin read queues empty "
            f"content); got {queued!r}")
        assert not _store_exists(tmp), (
            "the stdin update must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 8 ---------------------------------------------------------------------

def test_remote_capture_mode_resolved_from_env_intent():
    """ZMEM_CAPTURE_MODE=manual with no --capture-mode flag: the queued
    op carries capture_mode "manual" (gate resolution, not the flag)."""
    tmp = _pinned_scratch("p8")
    try:
        proc = _run_store_env(
            _remote_subproc_env(tmp, _dead_port_url(),
                                extra={"ZMEM_CAPTURE_MODE": "manual"}),
            "add", "--namespace", "user:global", "--type", "fact",
            "--content", "capture mode resolution probe for the queue",
            "--json")
        assert proc.returncode == 0, (
            f"remote add under ZMEM_CAPTURE_MODE=manual must exit 0; "
            f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
        entries = _queue_entries(tmp)
        assert len(entries) == 1, (
            f"exactly one queued op expected, got {len(entries)}")
        assert entries[0].get("args", {}).get("capture_mode") == "manual", (
            f"the gate must resolve capture_mode from ZMEM_CAPTURE_MODE "
            f"when the flag is unset; got "
            f"{entries[0].get('args', {}).get('capture_mode')!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 9 ---------------------------------------------------------------------

def test_overlay_ordering_pending_then_flushed_recall():
    """A reachable hub + one pending add: the first recall prints BOTH a
    hub result and the pre-rendered [PENDING] overlay; the second recall
    (the first flushed the queue) has no [PENDING]."""
    if not _mcp_available():  # pragma: no cover - CI installs mcp
        print("SKIP: mcp package absent — the overlay ordering pin needs "
              "the real hub")
        return
    tmp = _pinned_scratch("p9")
    try:
        hub_dir = os.path.join(tmp, "hub")
        client_dir = os.path.join(tmp, "client")
        os.makedirs(client_dir, exist_ok=True)
        with _Hub(hub_dir) as hub:
            env = _remote_subproc_env(client_dir, hub.url, HUB_TOKEN)
            pending = _run_store_env(
                env, "add", "--namespace", SEED_NAMESPACE, "--type",
                "fact", "--content",
                "t10 supp overlay ordering pending probe row",
                "--json")
            assert pending.returncode == 0, (
                f"the pending-overlay enqueue must exit 0; "
                f"rc={pending.returncode} stderr={_tail(pending.stderr)!r}")

            first = _run_store_env(
                env, "recall", "--query",
                "fleet parity seed delivery ledger dedupe",
                "--namespace", SEED_NAMESPACE, "--json")
            assert first.returncode == 0, (
                f"the first remote recall must exit 0; rc="
                f"{first.returncode} stderr={_tail(first.stderr)!r}")
            envelope = _first_json_object(first.stdout)
            assert isinstance(envelope, dict) and \
                len(envelope.get("results") or []) >= 1, (
                    f"the first recall must carry a HUB result (the "
                    f"seeded fixture rows); stdout={_tail(first.stdout)!r}")
            assert "[PENDING]" in (first.stdout or ""), (
                f"the first recall must also render the pre-flush overlay "
                f"([PENDING]) for the queued add; "
                f"stdout tail={_tail(first.stdout)!r}")

            second = _run_store_env(
                env, "recall", "--query",
                "fleet parity seed delivery ledger dedupe",
                "--namespace", SEED_NAMESPACE, "--json")
            assert second.returncode == 0, (
                f"the second remote recall must exit 0; rc="
                f"{second.returncode} stderr={_tail(second.stderr)!r}")
            assert "[PENDING]" not in (second.stdout or ""), (
                f"the second recall (queue flushed empty by the first) "
                f"must have no [PENDING] marker; "
                f"stdout tail={_tail(second.stdout)!r}")
        assert not _store_exists(client_dir), (
            "the remote recall must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 10 --------------------------------------------------------------------

def test_unreachable_hub_error_contract():
    """ZMEM_REMOTE_URL at a dead port: remote recall exits 1 with stdout
    JSON {ok:false, reason:hub-unreachable}, non-empty stderr, and no
    store.sqlite."""
    tmp = _pinned_scratch("p10")
    try:
        proc = _run_store_env(
            _remote_subproc_env(tmp, _dead_port_url()),
            "recall", "--query", "fleet parity seed", "--json")
        assert proc.returncode == 1, (
            f"a connect-level hub failure must exit 1 (the read error "
            f"contract), got {proc.returncode}; "
            f"stdout={_tail(proc.stdout)!r} stderr={_tail(proc.stderr)!r}")
        envelope = _first_json_object(proc.stdout)
        assert isinstance(envelope, dict), (
            f"the failed read must print a JSON error envelope on "
            f"stdout; stdout={_tail(proc.stdout)!r}")
        assert envelope.get("ok") is False, (
            f"the error envelope must carry ok:false; got "
            f"{str(envelope)[:200]!r}")
        assert envelope.get("reason") == "hub-unreachable", (
            f"a connect-level failure must report reason "
            f"'hub-unreachable'; got {str(envelope)[:200]!r}")
        assert (proc.stderr or "").strip(), (
            "the error contract requires a non-empty stderr detail line")
        assert not _store_exists(tmp), (
            "a failed remote read must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 11 --------------------------------------------------------------------

class _MethodDispatchRecorder:
    """Records every method call on the replaced provider transport and
    answers a per-method canned response (the frozen _TransportRecorder
    shape for the write tools; a session-start-shaped envelope for the
    session_start pull)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.receipt_id = uuid.uuid4().hex
        write_receipt = {
            "ok": True,
            "id": self.receipt_id,
            "result": "stored",
            "stdout": json.dumps({"id": self.receipt_id,
                                  "result": "stored", "warnings": []}),
            "stderr": "",
            "returncode": 0,
        }
        self._responses = {
            "session_start": {
                # The REAL hub session_start return shape (mcp_server.py):
                # ids/omitted/rendered — no results/candidate_ids. The
                # provider's remap turns ids into the decision fields.
                "ids": ["sess-row-1"],
                "rendered": "t10 supp rendered fence for the session pull",
                "omitted": 1,
                "budget_dropped": 0,
                "tokens_used": 3,
                "tokens_budget": 1500,
                "reason": "",
            },
            "search": {
                "ok": True,
                "results": [{"id": "hub-row-1",
                             "content": "t10 supp hub search row",
                             "type": "fact"}],
                "count": 1,
            },
        }
        self._default = write_receipt

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        recorder = self

        def _record(*args, **kwargs):
            recorder.calls.append({"method": name, "args": args,
                                   "kwargs": kwargs})
            return recorder._responses.get(name, recorder._default)
        return _record


def test_hermes_mcp_mode_routes_explicit_tools_through_transport():
    """MCP mode: supersede/update/invalidate/search route through the
    transport recorder (never _run_store); session_start forwards the
    caller's limit kwarg; the add response mapping is a JSON string
    carrying result/id."""
    tmp = _pinned_scratch("p11")
    saved_env = {k: v for k, v in os.environ.items()
                 if k.startswith("ZMEM_") or k in ("CLAUDE_PLUGIN_DATA",
                                                   "ZCODE_PLUGIN_DATA")}
    _strip_config_env(os.environ)
    os.environ.update({
        # Mode selection only — port 9 (discard) is never contacted
        # because the transport is replaced below.
        "ZMEM_MCP_URL": "http://127.0.0.1:9/mcp",
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
    })
    _purge_cached_modules()
    module = None
    inserted_name = "zmem_t10_supp_hermes"
    try:
        module = _load_provider_module(inserted_name)
        provider = module.ZmemMemoryProvider()
        assert provider.is_available(), (
            f"the provider must resolve MCP mode under ZMEM_MCP_URL "
            f"(harness sanity); reason={provider.unavailable_reason()!r}")

        probe = ("t10 supp hermes routing probe " + uuid.uuid4().hex)
        some_id = uuid.uuid4().hex
        recorder = _MethodDispatchRecorder()
        run_store_calls: list[dict] = []

        def _fail_run_store(*args, **kwargs):
            run_store_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError(
                "issue #289 T-10: an MCP-mode explicit Hermes tool called "
                "_run_store — the op must route through the broker/hub "
                f"transport, not a local store.py subprocess "
                f"(argv={args!r})")

        real_run_store = module._run_store
        real_transport = provider._transport
        real_append_decision = module._append_session_decision
        decision_ids: list = []

        def _record_decision(*d_args, **d_kwargs):
            decision_ids.append(list(d_kwargs.get("ids") or []))
            return real_append_decision(*d_args, **d_kwargs)

        module._run_store = _fail_run_store
        provider._transport = recorder
        module._append_session_decision = _record_decision
        try:
            responses = {}
            responses["add"] = provider.handle_tool_call(
                "zmem_add", {"type": "fact", "content": probe,
                             "namespace": "user:global"})
            responses["supersede"] = provider.handle_tool_call(
                "zmem_supersede", {"id": some_id,
                                   "reason": "superseded by probe"})
            responses["update"] = provider.handle_tool_call(
                "zmem_update", {"id": some_id,
                                "content": "replacement body probe"})
            responses["invalidate"] = provider.handle_tool_call(
                "zmem_invalidate", {"id": some_id,
                                    "reason": "no longer true probe"})
            responses["search"] = provider.handle_tool_call(
                "zmem_search", {"query": "hub search row"})
            responses["session_start"] = provider.handle_tool_call(
                "zmem_session_start", {"limit": 7})
        finally:
            module._run_store = real_run_store
            provider._transport = real_transport
            module._append_session_decision = real_append_decision

        methods = {call["method"] for call in recorder.calls}
        for expected in ("add", "supersede", "update", "invalidate",
                         "search", "session_start"):
            assert expected in methods, (
                f"the MCP-mode {expected} tool must route through the "
                f"transport; recorded methods={sorted(methods)}")
        assert not run_store_calls, (
            "_run_store must never run for an MCP-mode explicit tool "
            "(issue #289 T-10)")

        # The routed write carried the probe content.
        flat = json.dumps(recorder.calls, ensure_ascii=False, default=str)
        assert probe in flat, (
            f"the recorded transport traffic must carry the probe "
            f"content; calls={flat[:400]!r}")

        # Response mapping: the add tool returns a JSON STRING carrying
        # result/id (the _transport_write_response contract).
        add_resp = json.loads(responses["add"])
        assert add_resp.get("result") == "stored" and \
            add_resp.get("id") == recorder.receipt_id, (
                f"the MCP-mode add response must map the hub receipt to "
                f"{{result, id}}; got {add_resp!r}")

        search_resp = json.loads(responses["search"])
        assert isinstance(search_resp.get("results"), list) and \
            search_resp.get("count") == len(search_resp["results"]), (
                f"the MCP-mode search response must project results/count; "
                f"got {search_resp!r}")

        # The session-start pull forwarded the caller's limit kwarg and
        # the envelope's rendered fence survived the remap.
        ss_calls = [c for c in recorder.calls
                    if c["method"] == "session_start"]
        assert ss_calls, "session_start must route through the transport"
        assert ss_calls[0]["kwargs"].get("limit") == 7, (
            f"the session-start pull must forward the caller's limit "
            f"(clamped 1..50); kwargs={ss_calls[0]['kwargs']!r}")
        ss_resp = json.loads(responses["session_start"])
        assert ss_resp.get("context") == \
            "t10 supp rendered fence for the session pull", (
                f"the hub envelope's rendered fence must survive the "
                f"remap as 'context'; got {ss_resp!r}")
        # Review round-2 R2-1: the remap must reach the DECISION LINE —
        # the client-side zmem-decisions.log record carries the hub's
        # delivered ids (the response object itself keeps the adapter's
        # intentional ids:[] shape; the ids live in the decision line).
        assert decision_ids and decision_ids[0] == ["sess-row-1"], (
            f"the MCP-mode session-start decision line must carry the "
            f"hub's delivered ids (remapped from envelope ids); "
            f"recorded decision ids={decision_ids!r}")
    finally:
        if module is not None:
            sys.modules.pop(inserted_name, None)
        _purge_cached_modules()
        for key in list(os.environ):
            if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                                  "ZCODE_PLUGIN_DATA"):
                del os.environ[key]
        for key, value in saved_env.items():
            os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 12 --------------------------------------------------------------------

def test_parse_results_passes_arms_and_normalizes_none_tokens():
    """mcp_server._parse_results: the arms dict passes through and
    tokens_used None normalizes to the int 0 (per-command key parity for
    remote reads)."""
    if not _mcp_available():  # pragma: no cover - CI installs mcp
        print("SKIP: mcp package absent — _parse_results lives in the "
              "hub's mcp_server module")
        return
    tmp = _pinned_scratch("p12")
    saved_env = {k: v for k, v in os.environ.items()
                 if k.startswith("ZMEM_") or k in ("CLAUDE_PLUGIN_DATA",
                                                   "ZCODE_PLUGIN_DATA")}
    _strip_config_env(os.environ)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
    })
    _purge_cached_modules()
    saved_path = list(sys.path)
    try:
        sys.path.insert(0, str(SERVER_DIR))
        import mcp_server  # noqa: PLC0415 - lazy by design (pinned env)
        hub_stdout = json.dumps({
            "results": [], "count": 0, "omitted": 0, "injection_risk": 0,
            "tokens_used": None, "tokens_budget": None,
            "arms": {"fts": {"pre": 0}},
        })
        envelope = mcp_server._parse_results(
            {"ok": True, "stdout": hub_stdout, "stderr": "",
             "returncode": 0})
        assert isinstance(envelope, dict), (
            f"_parse_results must return a dict; got {envelope!r}")
        assert envelope.get("arms") == {"fts": {"pre": 0}}, (
            f"a dict arms member must pass through unchanged; got "
            f"{envelope.get('arms')!r}")
        assert envelope.get("tokens_used") == 0 and \
            isinstance(envelope.get("tokens_used"), int) and \
            not isinstance(envelope.get("tokens_used"), bool), (
                f"tokens_used None must normalize to the INT 0; got "
                f"{envelope.get('tokens_used')!r}")
        assert envelope.get("tokens_budget") == 0, (
            f"tokens_budget None must normalize to 0; got "
            f"{envelope.get('tokens_budget')!r}")
    finally:
        sys.path[:] = saved_path
        _purge_cached_modules()
        for key in list(os.environ):
            if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                                  "ZCODE_PLUGIN_DATA"):
                del os.environ[key]
        for key, value in saved_env.items():
            os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 13 --------------------------------------------------------------------

class _FakeCompletedProcess:
    def __init__(self):
        self.returncode = 0
        self.stdout = "{}"
        self.stderr = ""


class _SpawnRecorderModule:
    """A stand-in `subprocess` module namespace that records every run /
    Popen and answers a benign CompletedProcess (so a code path that DOES
    spawn still returns parseable output)."""

    def __init__(self, real):
        self._real = real
        self.run_calls: list[dict] = []
        self.popen_calls: list[dict] = []

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        return _FakeCompletedProcess()

    def Popen(self, *args, **kwargs):  # noqa: N802 - subprocess API shape
        self.popen_calls.append({"args": args, "kwargs": kwargs})

        class _Child:
            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        return _Child()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_compat_hooks_skip_hub_side_calls_under_remote():
    """Per-call remote guards in the Hermes compat hooks: reflect's
    _run_hermes_context skips ack-failure (commit-cursor still spawns);
    convention's _run_convention and _write_post_tool_evidence return
    without spawning under ZMEM_REMOTE_URL."""
    reflect_name = "zmem_t10_supp_reflect_hook"
    convention_name = "zmem_t10_supp_convention_hook"
    saved_remote = os.environ.get("ZMEM_REMOTE_URL")
    try:
        os.environ["ZMEM_REMOTE_URL"] = "http://127.0.0.1:1/mcp"
        import subprocess as _real_subprocess

        # --- zmem-hermes-reflect.py: _run_hermes_context ---------------
        reflect = _load_module_by_path(
            reflect_name, HOOKS_DIR / "zmem-hermes-reflect.py")
        reflect_sub = _SpawnRecorderModule(_real_subprocess)
        saved_reflect_sub = reflect.subprocess
        reflect.subprocess = reflect_sub
        try:
            ack = reflect._run_hermes_context(
                ["--action", "ack-failure", "--namespace", "user:global",
                 "--session-id", "s1"])
            assert ack == {}, (
                f"_run_hermes_context must answer {{}} for ack-failure "
                f"under ZMEM_REMOTE_URL; got {ack!r}")
            assert not reflect_sub.run_calls, (
                "ack-failure must NOT spawn the store.py bridge in remote "
                "mode (hub-side only); spawns="
                f"{reflect_sub.run_calls!r}")

            cursor = reflect._run_hermes_context(
                ["--action", "commit-cursor", "--namespace", "user:global",
                 "--session-id", "s1", "--cursor-ts", "0.0",
                 "--cursor-count", "0"])
            assert len(reflect_sub.run_calls) == 1, (
                "commit-cursor is sidecar-only ALLOW-LOCAL — it must "
                f"STILL spawn locally; spawns={reflect_sub.run_calls!r}")
            assert cursor == {}, (
                "the spawned commit-cursor bridge returns the recorded "
                f"benign stdout; got {cursor!r}")
        finally:
            reflect.subprocess = saved_reflect_sub
            sys.modules.pop(reflect_name, None)

        # --- zmem-hermes-convention.py: convention + evidence write ----
        convention = _load_module_by_path(
            convention_name, HOOKS_DIR / "zmem-hermes-convention.py")
        conv_sub = _SpawnRecorderModule(_real_subprocess)
        saved_conv_sub = convention.subprocess
        convention.subprocess = conv_sub
        try:
            payload = {
                "tool_name": "Read",
                "args": {"file_path": "probe.txt"},
                "session_id": "s1",
                "task_id": "t1",
                "tool_call_id": "c1",
                "result": {"ok": True},
                "duration_ms": 5,
            }
            convention._run_convention(payload, {})
            assert not conv_sub.run_calls, (
                "hermes-convention must SKIP the local store.py call "
                "under ZMEM_REMOTE_URL; spawns="
                f"{conv_sub.run_calls!r}")

            wrote = convention._write_post_tool_evidence(payload, {})
            assert wrote is False, (
                f"the evidence write must return False (skip) under "
                f"ZMEM_REMOTE_URL; got {wrote!r}")
            assert not conv_sub.popen_calls and not conv_sub.run_calls, (
                "the evidence write must spawn NOTHING under "
                f"ZMEM_REMOTE_URL; spawns={conv_sub.run_calls!r} "
                f"popens={conv_sub.popen_calls!r}")
        finally:
            convention.subprocess = saved_conv_sub
            sys.modules.pop(convention_name, None)
    finally:
        if saved_remote is None:
            os.environ.pop("ZMEM_REMOTE_URL", None)
        else:
            os.environ["ZMEM_REMOTE_URL"] = saved_remote
        sys.modules.pop(reflect_name, None)
        sys.modules.pop(convention_name, None)


# -- pin 14 --------------------------------------------------------------------

def test_broker_fallback_recall_flag_shape_refused_in_remote_mode():
    """The broker-fallback family pin: with ZMEM_REMOTE_URL set, the
    recall flag shape hooks/lib/zmem-recall-body.py actually uses
    (--for-injection + the passive markers) is REFUSED (rc!=0 is the
    body's fail-open empty signal) and creates no store."""
    tmp = _pinned_scratch("p14")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        # The exact adapter argv shape (zmem-recall-body.py main():
        # command --query ... --namespace ... --limit 5 --include-global
        # --global-limit 3 --no-bump --for-injection --json
        # --session-id=... --moment user_prompt --lane zcode).
        argv = [
            "recall",
            "--query", "deploy the service safely",
            "--namespace", "user:global",
            "--limit", "5",
            "--include-global",
            "--global-limit", "3",
            "--no-bump",
            "--for-injection",
            "--json",
            "--session-id=fallback-session",
            "--moment", "user_prompt",
            "--lane", "zcode",
        ]
        proc = _run_store_env(env, *argv)
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 2, (
            f"the broker-fallback recall shape must be REFUSED (exit 2) "
            f"under ZMEM_REMOTE_URL — the hook body treats rc!=0 as an "
            f"empty envelope; got {proc.returncode}; "
            f"stderr={_tail(proc.stderr)!r}")
        assert "not supported in remote mode" in combined, (
            f"the refusal must name the refused flag explicitly; "
            f"output={_tail(combined)!r}")
        assert not _store_exists(tmp), (
            "the refused fallback recall must create no store.sqlite "
            "(strictly better than base, which created one)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 15 --------------------------------------------------------------------

def test_update_evidence_refused_in_remote_mode():
    """Remote `update --id <any> --content x --evidence ev-1` exits 2 with
    a stderr naming --evidence and enqueues NOTHING (the parser-dest fix:
    before the update parser's --evidence dest aligned with the write-
    refusal table the flag was silently dropped and the op queued)."""
    tmp = _pinned_scratch("p15")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        proc = _run_store_env(
            env, "update", "--id", uuid.uuid4().hex, "--content",
            "replacement body carrying evidence ids for the refusal probe",
            "--evidence", "ev-1")
        assert proc.returncode == 2, (
            f"remote update --evidence must exit 2 (the write-route "
            f"refusal table carries the evidence dest), got "
            f"{proc.returncode}; stderr={_tail(proc.stderr)!r}")
        assert "--evidence" in (proc.stderr or ""), (
            f"the refusal must name --evidence on stderr; "
            f"stderr={_tail(proc.stderr)!r}")
        assert _queue_entries(tmp) == [], (
            "the refused update must enqueue NOTHING (before the parser "
            "dest fix this shape silently queued a drop-evidence op)")
        assert not _store_exists(tmp), (
            "a refused remote write must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 16 --------------------------------------------------------------------

def test_positive_remote_list_against_hub():
    """POSITIVE hub list pin: remote `list --namespace project:fixture-280
    --json` against the real t08 hub exits 0 and prints the list --json
    envelope with keys EXACTLY {namespace, count, memories}; memories is a
    non-empty list of dicts each carrying id/namespace/type/preview."""
    if not _mcp_available():  # pragma: no cover - CI installs mcp
        print("SKIP: mcp package absent — the positive remote-list pin "
              "needs the real hub")
        return
    tmp = _pinned_scratch("p16")
    try:
        hub_dir = os.path.join(tmp, "hub")
        client_dir = os.path.join(tmp, "client")
        os.makedirs(client_dir, exist_ok=True)
        with _Hub(hub_dir) as hub:
            proc = _run_store_env(
                _remote_subproc_env(client_dir, hub.url, HUB_TOKEN),
                "list", "--namespace", SEED_NAMESPACE, "--json")
            assert proc.returncode == 0, (
                f"remote list with a reachable hub must exit 0; "
                f"rc={proc.returncode} stderr={_tail(proc.stderr)!r}")
            envelope = _first_json_object(proc.stdout)
            assert isinstance(envelope, dict), (
                f"remote list --json must print a JSON envelope; "
                f"stdout={_tail(proc.stdout)!r}")
            assert set(envelope) == {"namespace", "count", "memories"}, (
                f"remote list must print the exact 3-key list --json "
                f"envelope (namespace/count/memories — the hub "
                f"list_memories tool serves this shape); got "
                f"{sorted(envelope)}")
            memories = envelope.get("memories")
            assert isinstance(memories, list) and len(memories) >= 1, (
                f"the seeded fixture adds live rows under {SEED_NAMESPACE}, "
                f"so memories must be a non-empty list; "
                f"envelope={str(envelope)[:300]!r}")
            for row in memories:
                assert isinstance(row, dict), (
                    f"every memories entry must be an object; "
                    f"got {row!r}")
                missing = {"id", "namespace", "type", "preview"} - set(row)
                assert not missing, (
                    f"every memories entry must carry "
                    f"id/namespace/type/preview (missing {sorted(missing)}); "
                    f"row={str(row)[:200]!r}")
            assert any(SEED_MARKER in str(row.get("preview", ""))
                       for row in memories), (
                f"at least one preview must carry the seeded "
                f"'{SEED_MARKER}' text; "
                f"memories={str(memories)[:300]!r}")
        assert not _store_exists(client_dir), (
            "the remote list must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 17 --------------------------------------------------------------------

def test_get_dead_port_reason_is_hub_unreachable():
    """Dead-port ZMEM_REMOTE_URL: remote `get --id xyz` exits 1 with stdout
    JSON ok:false and reason hub-unreachable — the transport's connect-
    level reason survives the get error mapping (hub-refused is reserved
    for hub-ANSWERED refusals; before the fix every get failure said
    hub-refused)."""
    tmp = _pinned_scratch("p17")
    try:
        proc = _run_store_env(
            _remote_subproc_env(tmp, _dead_port_url()),
            "get", "--id", "xyz")
        assert proc.returncode == 1, (
            f"a connect-level get failure must exit 1 (the read error "
            f"contract), got {proc.returncode}; "
            f"stdout={_tail(proc.stdout)!r} stderr={_tail(proc.stderr)!r}")
        envelope = _first_json_object(proc.stdout)
        assert isinstance(envelope, dict), (
            f"the failed get must print a JSON error envelope on stdout; "
            f"stdout={_tail(proc.stdout)!r}")
        assert envelope.get("ok") is False, (
            f"the error envelope must carry ok:false; got "
            f"{str(envelope)[:200]!r}")
        assert envelope.get("reason") == "hub-unreachable", (
            f"a connect-level get failure must preserve the transport's "
            f"'hub-unreachable' reason (hub-refused is reserved for "
            f"hub-ANSWERED refusals); got {str(envelope)[:200]!r}")
        assert not _store_exists(tmp), (
            "a failed remote get must never create a local store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 18 --------------------------------------------------------------------

def test_delegation_prompt_and_legacy_unscoped_refused():
    """The passive-marker read flags refuse under remote mode: `recall
    --query q --delegation-prompt x` exits 2 naming --delegation-prompt;
    `recent --legacy-unscoped` exits 2 naming --legacy-unscoped."""
    tmp = _pinned_scratch("p18")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        cases = [
            ("recall --delegation-prompt",
             ["recall", "--query", "q", "--delegation-prompt", "x"],
             "--delegation-prompt"),
            ("recent --legacy-unscoped",
             ["recent", "--legacy-unscoped"],
             "--legacy-unscoped"),
        ]
        for label, argv, needle in cases:
            proc = _run_store_env(env, *argv)
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert proc.returncode == 2, (
                f"remote {label} must exit 2 (passive marker refused up "
                f"front), got {proc.returncode}; "
                f"stderr={_tail(proc.stderr)!r}")
            assert needle in combined, (
                f"remote {label} refusal must name {needle!r}; "
                f"output={_tail(combined)!r}")
        assert not _store_exists(tmp), (
            "a remote passive-marker refusal must happen before any store "
            "access")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- pin 19 --------------------------------------------------------------------

def test_hook_env_retain_and_delivery_clear_execute():
    """HOOK-ENV band part 2 — under ZMEM_DATA + ZMEM_REMOTE_URL: `retain`
    (the correction-queue review sidecar writer) executes with exit 0 and
    `delivery-clear --session-id` executes with exit 0; neither
    config-errors and neither creates store.sqlite. The retain parser
    (storelib/cli.py) requires --namespace/--session/--host where --host
    is choices-bound to RETAIN_HOSTS (claude/codex/zcode/hermes-provider/
    hermes-compat — no "probe"), so the minimal VALID host is zcode;
    ZMEM_AUTO_RETAIN=1 forces the retention gate on so the stdin evidence
    row really lands in the review sidecar."""
    tmp = _pinned_scratch("p19")
    try:
        env = _remote_subproc_env(
            tmp, _dead_port_url(),
            extra={"ZMEM_DATA": tmp, "ZMEM_AUTO_RETAIN": "1"})
        payload = json.dumps({
            "evidence_id": "ev-t10s-retain-1",
            "text": "remember: prefer pinned scratch directories for "
                    "t10 supp retain probes",
            "timestamp": "2026-10-01T00:00:00Z",
            "session": "s1",
            "namespace": "user:global",
            "host": "zcode",
            "kind": "explicit",
        })
        proc = _run_store_env(
            env, "retain", "--input", "-", "--namespace", "user:global",
            "--session", "s1", "--host", "zcode", input_text=payload)
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"retain under ZMEM_DATA+ZMEM_REMOTE_URL must execute (the "
            f"review-sidecar ALLOW-LOCAL band), rc={proc.returncode} "
            f"stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"ZMEM_DATA alone is legal in remote mode — retain must not "
            f"config-error; output={_tail(combined)!r}")
        review = Path(tmp) / "queue" / "user_cglobal.json"
        assert review.is_file(), (
            f"retain (ZMEM_AUTO_RETAIN=1, one explicit evidence row) must "
            f"write the correction-queue review sidecar at {review}; it "
            f"is absent (stdout={_tail(proc.stdout)!r})")
        assert not _store_exists(tmp), (
            "retain must never create store.sqlite (review sidecar only)")

        proc = _run_store_env(env, "delivery-clear", "--session-id", "old")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 0, (
            f"delivery-clear under ZMEM_DATA+ZMEM_REMOTE_URL must execute "
            f"(the delivery-state sidecar band), rc={proc.returncode} "
            f"stderr={_tail(proc.stderr)!r}")
        assert "configuration error" not in combined, (
            f"delivery-clear must not config-error under ZMEM_DATA+remote; "
            f"output={_tail(combined)!r}")
        assert not _store_exists(tmp), (
            "delivery-clear must never create store.sqlite "
            "(delivery-state sidecar only)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- standalone runner (CI runs `python tests/test_t10_cli_remote_supp.py`;
#    every test function executes directly — a silent exit 0 is theater) ------


def test_hub_run_store_strips_remote_url_from_children():
    """PR #400 feedback round 2: the hub's _run_store spawns store.py
    children with ZMEM_REMOTE_URL stripped — the T-10 gate would otherwise
    refuse every hub-side tool call when the hub process itself carries the
    variable (self-referential or stale operator env), wedging the hub.
    Verified wedge shape at the pre-fix head: rc=2 config-error refusal."""
    if not _mcp_available():  # pragma: no cover - CI installs mcp
        print("SKIP: mcp package absent — _run_store lives in the "
              "hub's mcp_server module")
        return
    tmp = _pinned_scratch("p21")
    saved_env = {k: v for k, v in os.environ.items()
                 if k.startswith("ZMEM_") or k in ("CLAUDE_PLUGIN_DATA",
                                                   "ZCODE_PLUGIN_DATA")}
    _strip_config_env(os.environ)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
        # The wedge condition: the HUB process carries the client var.
        "ZMEM_REMOTE_URL": "http://127.0.0.1:9/mcp",
    })
    _purge_cached_modules()
    saved_path = list(sys.path)
    try:
        sys.path.insert(0, str(SERVER_DIR))
        import mcp_server  # noqa: PLC0415 - lazy by design (pinned env)
        r = mcp_server._run_store(
            ["add", "--namespace", "user:global", "--type", "fact",
             "--content", "hub env pin: hub self-protection probe",
             "--json"])
        assert r["ok"], (
            f"PRR-2: the hub's own store.py child must run LOCAL under a "
            f"hub-carried ZMEM_REMOTE_URL (children are spawned with the "
            f"variable stripped); rc={r['returncode']} "
            f"stderr={_tail(r['stderr'])!r}")
        assert '"result": "stored"' in (r["stdout"] or ""), (
            f"the hub child must actually STORE (a pending-receipt or "
            f"refusal shape means the gate fired); "
            f"stdout={_tail(r['stdout'])!r}")
        assert (Path(tmp) / "store.sqlite").is_file(), (
            "the hub child's store must land inside the pinned scratch "
            "(isolation held — nothing leaked to the real ~/.zmem); "
            f"entries={os.listdir(tmp)!r}")
    finally:
        sys.path = saved_path
        for key in list(os.environ):
            if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                                  "ZCODE_PLUGIN_DATA"):
                del os.environ[key]
        for key, value in saved_env.items():
            os.environ[key] = value
        _purge_cached_modules()
        shutil.rmtree(tmp, ignore_errors=True)




# -- pin 5 (#326) -------------------------------------------------------------

def test_expiry_write_route_queues_state_add_with_metadata():
    """#326: the remote write route carries the expiry pair. A state add WITH
    --expiry-class queues (exit 0) with both fields in the op payload; an
    update with expiry flags queues them too. A state add WITHOUT
    --expiry-class keeps its up-front exit-2 refusal (the writer would only
    reject the pending receipt at apply time) — pin 4's `add --type state`
    case stays the refusal guard."""
    tmp = _pinned_scratch("p5")
    try:
        env = _remote_subproc_env(tmp, _dead_port_url())
        proc = _run_store_env(
            env, "add", "--namespace", "user:global", "--type", "state",
            "--content", "expiry queue probe: pool size is 8",
            "--expiry-class", "setting",
            "--inject-until", "2027-06-01T00:00:00Z")
        assert proc.returncode == 0, (
            f"remote state add with expiry metadata must queue, got "
            f"{proc.returncode}; stderr={_tail(proc.stderr)!r}")
        entries = _queue_entries(tmp)
        assert len(entries) == 1, f"expected one queued op, got {len(entries)}"
        args = entries[0].get("args") or {}
        assert args.get("expiry_class") == "setting", args
        assert args.get("inject_until") == "2027-06-01T00:00:00Z", args
        assert args.get("type") == "state", args

        some_id = str(uuid.uuid4())
        proc = _run_store_env(
            env, "update", "--id", some_id,
            "--content", "replacement body for the expiry queue probe",
            "--expiry-class", "metric")
        assert proc.returncode == 0, (
            f"remote update with expiry flags must queue, got "
            f"{proc.returncode}; stderr={_tail(proc.stderr)!r}")
        entries = _queue_entries(tmp)
        assert len(entries) == 2, f"expected two queued ops, got {len(entries)}"
        upd_args = entries[1].get("args") or {}
        assert upd_args.get("expiry_class") == "metric", upd_args
        assert not _store_exists(tmp), (
            "the remote route must never open a local store")

        # PRR-013: the state-add-without-expiry refusal message is pinned
        # EXACTLY (pin 4's substring "state" would pass any wording).
        proc = _run_store_env(
            env, "add", "--namespace", "user:global", "--type", "state",
            "--content", "expiry refusal probe: pool size is 9")
        combined = (proc.stdout or "") + (proc.stderr or "")
        assert proc.returncode == 2, proc.returncode
        assert ("--type state in remote mode requires --expiry-class"
                in combined), _tail(combined)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_ALL_TESTS = (
    test_hook_env_band_allow_local_commands_execute_under_zmem_data,
    test_config_error_names_store_data_and_remote_url,
    test_refuse_band_beyond_frozen_six_refuses_and_never_creates_store,
    test_write_flag_refusals_name_the_refused_flag,
    test_expiry_write_route_queues_state_add_with_metadata,
    test_local_update_reason_empty_refusal_and_default_reason_survive,
    test_read_flag_refusals_and_positive_remote_search,
    test_remote_add_content_dash_reads_stdin_into_queued_op,
    test_remote_update_content_dash_reads_stdin_into_queued_op,
    test_remote_capture_mode_resolved_from_env_intent,
    test_overlay_ordering_pending_then_flushed_recall,
    test_unreachable_hub_error_contract,
    test_hermes_mcp_mode_routes_explicit_tools_through_transport,
    test_parse_results_passes_arms_and_normalizes_none_tokens,
    test_compat_hooks_skip_hub_side_calls_under_remote,
    test_broker_fallback_recall_flag_shape_refused_in_remote_mode,
    test_update_evidence_refused_in_remote_mode,
    test_positive_remote_list_against_hub,
    test_get_dead_port_reason_is_hub_unreachable,
    test_delegation_prompt_and_legacy_unscoped_refused,
    test_hook_env_retain_and_delivery_clear_execute,
    test_hub_run_store_strips_remote_url_from_children,
)


def _main(argv: list[str]) -> int:
    selectors = argv[1:]
    selected = list(_ALL_TESTS)
    if selectors:
        by_name = {t.__name__: t for t in _ALL_TESTS}
        chosen = []
        for selector in selectors:
            matches = [t for t in _ALL_TESTS if selector in t.__name__]
            if not matches:
                print(f"NO TEST {selector}", file=sys.stderr)
                return 2
            chosen.extend(matches)
        selected = chosen
    failures = []
    for check in selected:
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
    if failures:
        print(f"{len(failures)} test(s) failed")
        return 1
    print(f"all {len(selected)} test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
