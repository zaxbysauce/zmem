"""Issue #289 (Workstream T PR 10) frozen acceptance checks C1-C4. RED at
base 8dda16d, GREEN after the fix.

Issue #289: the CLI (skills/memory/scripts/store.py) and the Hermes
provider honor remote mode — on a box configured with ZMEM_REMOTE_URL the
store CLI must stop touching a local store.sqlite: writes enqueue into the
T-09 durable op queue (issue #288) with a receipt, reads answer from the
hub (plus the pending read-your-writes overlay) in the SAME JSON envelope
shape the local recall prints, the Hermes provider's explicit add follows
its transport mode (MCP mode -> the hub, never a local _run_store
subprocess), and local-only admin commands refuse loudly instead of
running against a store that must not exist here. Authored from the issue
text only, before the feature exists; every check names its standing at
both ends:

- AC1 ``test_cli_add_in_remote_mode_never_creates_local_store`` — with
  ZMEM_REMOTE_URL set (a bound-then-closed dead port: the enqueue is a
  LOCAL durable write, so no hub needs to answer) and the data dir pinned
  via ZCODE_PLUGIN_DATA (issue #289 makes explicit ZMEM_STORE/ZMEM_DATA
  together with ZMEM_REMOTE_URL a config ERROR, so the remote-mode env
  pins the data dir through the harness-provided channel
  host.resolve_store_path already honors), ``store.py add --content X
  --json`` must exit 0, print a receipt whose JSON carries ``op_id`` and
  ``status``, enqueue one ``{"op": "add", "status": "pending"}`` line in
  ``<data>/queue/ops-local.jsonl`` (fleet/opqueue.py queue_path under the
  resolved store's parent; principal "local" is what the hooks/broker use
  today), and create NO ``<data>/store.sqlite``. RED at base (add ignores
  ZMEM_REMOTE_URL: the receipt is today's ``{"id", "result", "warnings"}`
  with no op_id, no queue file exists, and the local store IS created),
  GREEN after (issue #289 AC1).
- AC2 ``test_cli_recall_in_remote_mode_reads_hub`` — against the real
  in-process hub (tests/support/t08_hub.py::build_server on an ephemeral
  port, seeded from tests/fixtures/fleet/seed.jsonl, token
  t08-hub-token — the tests/test_t08_broker_remote.js:250-290 handshake
  pattern), ``store.py recall --query Q --namespace project:fixture-280
  --json`` with ZMEM_REMOTE_URL at the hub must exit 0, print the SAME
  JSON envelope KEY SET a local recall prints (captured in the same test
  against a seeded local scratch store — storelib/cli.py's recall
  dispatch prints the storelib/recall.py envelope
  results/count/omitted/injection_risk/tokens_used/tokens_budget/arms),
  and return at least one HUB row (the seeded "Fleet parity seed" content
  under project:fixture-280 — nothing local holds it). The hub's
  ``recall`` tool (mcp_server.py:1988) takes exactly query/namespace/
  limit, so the CLI's ``--query``/``--namespace`` surface can serve this
  with no lane/moment args. A prior remote add (AC1's enqueue) must leave
  the read-your-writes overlay: "[PENDING]" (fleet/opqueue.render_pending)
  appears in stdout. RED at base (recall ignores ZMEM_REMOTE_URL, reads
  the fresh EMPTY local store: envelope parses but carries ZERO hub
  rows — the hub-results assertion is the one that fails), GREEN after
  (issue #289 AC2).
- AC3 ``test_hermes_explicit_add_follows_transport_mode`` — in-process
  (the tests/test_hermes_transport.py importlib pattern with the
  agent.memory_provider stub), the provider constructed under
  ZMEM_MCP_URL=http://127.0.0.1:9/mcp (mode selection only; port 9 is
  never contacted because the transport is replaced) is in MCP mode, and
  its ``add`` tool call must route the write to the broker/hub transport,
  NOT ``_run_store``: a failing ``_run_store`` recorder plus a transport
  recorder answering a plausible hub receipt
  ``{"ok": True, "id": <hex>, "result": "stored"}`` (shaped to also carry
  the stdout/returncode keys _structured_write_response reads,
  hermes-plugin/__init__.py:1668-1693) — the transport recorder must
  capture the probe content and ``_run_store`` must never run. RED at
  base (``_tool_add`` at hermes-plugin/__init__.py:3651-3732 calls
  ``_run_store`` today, so the failing recorder fires), GREEN after
  (issue #289 AC3).
- AC4 ``test_admin_commands_refuse_in_remote_mode`` — each local-only
  admin command (``consolidate``, ``organize``, ``reembed``, ``purge``,
  ``export-dataset``, ``init``) run as a subprocess with ZMEM_REMOTE_URL
  set (dead port) and minimal VALID argv (purge needs ``--id``,
  export-dataset needs its ``dir`` positional + ``--yes``; the others are
  flagless) must exit 2 with the exact phrase ``this command runs on the
  hub; ZMEM_REMOTE_URL is set`` in stdout+stderr combined, and must touch
  nothing (no ``<data>/store.sqlite`` ever appears). RED at base (every
  command runs locally against the scratch — no refusal phrase, and most
  create the store), GREEN after (issue #289 AC4).

Env discipline (load-bearing): ``storelib`` freezes STORE_PATH at first
import from ambient env, so every remote-mode CLI check runs store.py as
a SUBPROCESS whose env strips EVERY ambient ``ZMEM_*`` variable (prefix
sweep — plus CLAUDE_PLUGIN_DATA/ZCODE_PLUGIN_DATA so the plugin-data
chain cannot resolve into the real user profile) BEFORE pinning only what
the check needs; remote-mode subprocess envs deliberately set NEITHER
ZMEM_STORE NOR ZMEM_DATA (issue #289: that combination with
ZMEM_REMOTE_URL is a config error) and pin the data dir via
ZCODE_PLUGIN_DATA instead. No storelib/fleet/mcp_server import happens at
test module level; the in-process AC3 purges the cached storelib/fleet/
host/hermes modules inside its pinned context manager on entry AND exit
(the t09 store-path-freeze discipline) and restores everything in
finally.

Runs under pytest (``python -m pytest tests/test_t10_cli_remote.py``) and
standalone (``python tests/test_t10_cli_remote.py`` — no argument runs
ALL checks, ``C1``..``C4`` run one, an unknown selector prints ``NO CHECK
<arg>`` to stderr and exits 2). Skips loudly (visible message) only when
the ``mcp`` package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
HUB_PY = REPO_ROOT / "tests" / "support" / "t08_hub.py"
SEED_JSONL = REPO_ROOT / "tests" / "fixtures" / "fleet" / "seed.jsonl"

# tests/support/t08_hub.py's fixed single-token auth (the hub accepts
# exactly this token; the remote CLI subprocess sends it).
HUB_TOKEN = "t08-hub-token"

# The seeded fixture namespace (tests/fixtures/fleet/README.md reserves
# session/namespace project:fixture-280 for the fleet parity fixtures).
SEED_NAMESPACE = "project:fixture-280"
SEED_MARKER = "Fleet parity seed"

# The frozen issue #289 AC4 refusal phrase, verbatim.
REFUSAL_PHRASE = "this command runs on the hub; ZMEM_REMOTE_URL is set"

# Subprocess budgets: store.py spawns are cold-interpreter + storelib
# imports (t09 uses 180s); the hub handshake includes seeding the fixture
# store through store.py before the T08_HUB line, so it gets the same
# order of magnitude.
_SUBPROC_TIMEOUT_S = 180
_HUB_HANDSHAKE_TIMEOUT_S = 120.0

# Modules whose cached state is env-derived (STORE_PATH freezes at first
# import) — purged on entry AND exit of every pinned context (t06/t08/t09
# discipline), plus the by-path module names the Hermes plugin loader
# registers and the agent.memory_provider stand-in AC3 installs.
_PURGE_EXACT = (
    "mcp_server", "fleet", "host", "redaction", "schema_meta", "store",
    "auth", "bind_guard", "agent", "agent.memory_provider",
    "zmem_hermes_transport", "zmem_fleet_transport",
    "zmem_mcp_client_transport", "zmem_schema_meta",
)
_PURGE_PREFIXES = ("fleet.", "storelib.", "store.", "auth.", "bind_guard.")


def _purge_cached_modules() -> None:
    """Drop env-derived module singletons (the t09 store-path-freeze
    discipline) so no sibling check inherits a stale STORE_PATH/queue dir
    pinned under a previous scratch."""
    doomed = [name for name in sys.modules
              if name in _PURGE_EXACT
              or name.startswith(_PURGE_PREFIXES)
              or name.startswith("zmem_hermes_t10")]
    for name in doomed:
        del sys.modules[name]


def _strip_config_env(target: dict) -> None:
    """Remove every ambient var the store-resolution / remote-mode chain
    reads, so only what the check pins can influence a child: the full
    ZMEM_* prefix sweep plus BOTH plugin-data channels (they outrank the
    ~ fallbacks in host.resolve_store_path and would otherwise resolve an
    ambient real profile)."""
    for key in list(target):
        if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                              "ZCODE_PLUGIN_DATA"):
            del target[key]


def _pinned_scratch(prefix: str) -> str:
    """A fresh GUID scratch dir name (caller owns cleanup)."""
    return tempfile.mkdtemp(prefix=f"zmem-t10-{prefix}-{uuid.uuid4().hex}-")


def _remote_subproc_env(tmp: str, remote_url: str,
                        token: str | None = None) -> dict:
    """A stripped subprocess env for a REMOTE-mode store.py run.

    Every ambient ZMEM_* (prefix sweep) and both plugin-data channels are
    stripped first; then ONLY what remote mode needs is pinned. Remote
    mode deliberately sets NEITHER ZMEM_STORE NOR ZMEM_DATA — issue #289
    makes that combination a config error — so the data dir stays
    hermetic through ZCODE_PLUGIN_DATA, which the resolution chain
    (host.resolve_store_path / storelib.schema) already honors.
    """
    env = {k: v for k, v in os.environ.items()}
    _strip_config_env(env)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
        "ZMEM_REMOTE_URL": remote_url,
    })
    if token is not None:
        env["ZMEM_MCP_TOKEN"] = token
    return env


def _local_subproc_env(tmp: str) -> dict:
    """A stripped subprocess env pinning a LOCAL scratch store (the t09
    _store_subproc_env discipline; used only for AC2's local reference
    leg, where a local store is the point)."""
    env = {k: v for k, v in os.environ.items()}
    _strip_config_env(env)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_store_env(env: dict, *argv: str) -> subprocess.CompletedProcess:
    """Run the real store.py CLI with a caller-built env (one spawn per
    call; cwd pinned at the repo root like every t-series check)."""
    return subprocess.run(
        [sys.executable, str(STORE_PY), *argv],
        capture_output=True, text=True, env=env,
        cwd=str(REPO_ROOT), timeout=_SUBPROC_TIMEOUT_S)


def _dead_port_url() -> str:
    """A bound-then-closed loopback port (the t08 dead-port probe):
    nothing listens, so every hub contact fails to connect — remote mode
    without needing a hub to answer."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/mcp"


def _tail(text: str, limit: int = 400) -> str:
    return (text or "").strip()[-limit:]


def _last_json_line(text: str):
    """The LAST stdout line that parses as a JSON object (add --json
    prints its receipt as one JSON line; parse leniently — any advisory
    lines around it are ignored)."""
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
    """The first parseable JSON object in stdout, starting at any line
    that opens a brace (recall --json prints a MULTI-LINE indented
    envelope; json.JSONDecoder.raw_decode ignores whatever trails the
    object, so a following [PENDING] overlay cannot break the parse)."""
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


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:  # pragma: no cover - CI installs requirements.txt
        return False


class _Hub:
    """The real in-process MCP hub (tests/support/t08_hub.py) as a child
    process, per the tests/test_t08_broker_remote.js:250-290 consumer
    pattern: spawn with stdin held open, poll the ONE ``T08_HUB <port>``
    handshake line from a stdout file, and shut down by closing stdin
    (then killing, belt and suspenders)."""

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
                f"(got {handshake!r}; hub exit={self._proc.returncode if self._proc else '?'}; "
                f"stderr tail: {stderr_tail!r}) — test harness failure, not "
                f"an issue #289 verdict")
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


def _load_provider_module(name: str):
    """Import hermes-plugin/__init__.py the tests/test_hermes_transport.py
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
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# -- AC1 -------------------------------------------------------------------------

def test_cli_add_in_remote_mode_never_creates_local_store():
    """issue #289 AC1: a remote-mode add enqueues a T-09 op, prints an
    op_id/status receipt, and never creates a local store."""
    tmp = _pinned_scratch("c1")
    try:
        remote_url = _dead_port_url()
        proc = _run_store_env(
            _remote_subproc_env(tmp, remote_url),
            "add", "--namespace", "user:global", "--type", "fact",
            "--content", "t10 ac1 remote add probe: the canary cutover "
                         "finished and the write must queue, not land",
            "--json")
        assert proc.returncode == 0, (
            f"remote-mode store.py add must exit 0 (the enqueue is a local "
            f"durable write; the hub need not answer): rc={proc.returncode} "
            f"stderr={_tail(proc.stderr)!r}")
        # The receipt: the LAST JSON line of stdout must carry op_id and
        # status (issue #289 AC1's receipt contract). At base the add prints
        # today's {"id", "result", "warnings"} with no op_id — this is the
        # RED anchor.
        receipt = _last_json_line(proc.stdout)
        assert isinstance(receipt, dict), (
            f"remote-mode add --json must print a JSON receipt line; "
            f"stdout={_tail(proc.stdout)!r}")
        assert receipt.get("op_id"), (
            f"the add receipt must carry the queued op's op_id (issue #289 "
            f"AC1); got: {str(receipt)[:300]!r}")
        assert "status" in receipt, (
            f"the add receipt must carry the op's status (issue #289 AC1); "
            f"got: {str(receipt)[:300]!r}")

        # The T-09 queue: one pending add op under the resolved data dir's
        # queue/ops-local.jsonl (fleet/opqueue.queue_path; principal
        # "local" is what the hooks/broker use today).
        queue_path = Path(tmp) / "queue" / "ops-local.jsonl"
        assert queue_path.is_file(), (
            f"remote-mode add must enqueue into the T-09 op queue at "
            f"{queue_path} (issue #289 AC1 routes remote writes through "
            f"the durable queue); the file is absent — the write went "
            f"somewhere else (or nowhere)")
        entries = []
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        assert len(entries) == 1, (
            f"exactly one queued op expected for one add, got "
            f"{len(entries)}: {entries!r}")
        assert entries[0].get("op") == "add", (
            f"the queued op must be an 'add' (issue #289 AC1 / issue #288 "
            f"vocabulary), got: {entries[0]!r}")
        assert entries[0].get("status") == "pending", (
            f"a freshly enqueued op must be status 'pending' until the hub "
            f"receipts it, got: {entries[0]!r}")

        # And NO local store: remote mode must not create store.sqlite.
        store_path = Path(tmp) / "store.sqlite"
        assert not store_path.exists(), (
            f"remote-mode add created the local store {store_path} — "
            f"issue #289 AC1 forbids any local store creation when "
            f"ZMEM_REMOTE_URL is set")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- AC2 -------------------------------------------------------------------------

def test_cli_recall_in_remote_mode_reads_hub():
    """issue #289 AC2: a remote-mode recall answers from the hub (plus the
    pending overlay) in the same JSON envelope shape as local recall."""
    if not _mcp_available():  # pragma: no cover - CI installs requirements
        print("SKIP: mcp package absent — test_cli_recall_in_remote_mode_"
              "reads_hub needs the real hub (hermes-plugin/server/"
              "requirements.txt)")
        return
    tmp = _pinned_scratch("c2")
    try:
        # LOCAL reference leg: a seeded scratch store (the t09 pinning
        # discipline — ZMEM_STORE/ZMEM_DATA are correct HERE because this
        # leg is deliberately local) whose recall --json output defines
        # the reference envelope KEY SET.
        local_dir = os.path.join(tmp, "local")
        os.makedirs(local_dir, exist_ok=True)
        local_env = _local_subproc_env(local_dir)
        seed = _run_store_env(
            local_env, "add", "--namespace", SEED_NAMESPACE,
            "--type", "fact",
            "--content", "t10 ac2 local reference row for envelope keys",
            "--confidence", "0.9", "--json")
        assert seed.returncode == 0, (
            f"local reference seed failed ({seed.returncode}): "
            f"{_tail(seed.stderr)!r}")
        local = _run_store_env(
            local_env, "recall",
            "--query", "local reference row envelope keys",
            "--namespace", SEED_NAMESPACE, "--json")
        assert local.returncode == 0, (
            f"local reference recall failed ({local.returncode}): "
            f"{_tail(local.stderr)!r}")
        reference = _first_json_object(local.stdout)
        assert isinstance(reference, dict), (
            f"local recall --json must print its JSON envelope "
            f"(harness sanity for the reference leg); "
            f"stdout={_tail(local.stdout)!r}")
        reference_keys = set(reference)
        assert reference_keys, "the reference envelope must have keys"

        # HUB leg: the real hub, the fleet fixture seed, and ONE shared
        # client data dir so the pending overlay leg (a prior remote add)
        # and the recall resolve the same queue.
        hub_dir = os.path.join(tmp, "hub")
        client_dir = os.path.join(tmp, "client")
        os.makedirs(client_dir, exist_ok=True)
        with _Hub(hub_dir) as hub:
            # A prior remote add enqueues a pending op (AC1's queue) so the
            # recall below must overlay it (fleet/opqueue.render_pending).
            pending_add = _run_store_env(
                _remote_subproc_env(client_dir, hub.url, HUB_TOKEN),
                "add", "--namespace", SEED_NAMESPACE, "--type", "fact",
                "--content", "t10 ac2 pending overlay probe row",
                "--json")
            assert pending_add.returncode == 0, (
                f"the prior remote add (the pending-overlay enqueue) must "
                f"exit 0: rc={pending_add.returncode} "
                f"stderr={_tail(pending_add.stderr)!r}")

            remote = _run_store_env(
                _remote_subproc_env(client_dir, hub.url, HUB_TOKEN),
                "recall",
                "--query", "fleet parity seed delivery ledger dedupe",
                "--namespace", SEED_NAMESPACE, "--json")
            assert remote.returncode == 0, (
                f"remote-mode recall must exit 0 with a reachable hub; "
                f"rc={remote.returncode} stderr={_tail(remote.stderr)!r}")

            envelope = _first_json_object(remote.stdout)
            assert isinstance(envelope, dict), (
                f"remote-mode recall --json must print a JSON envelope; "
                f"stdout={_tail(remote.stdout)!r}")

            # THE base-RED anchor: at least one result row originates from
            # the HUB — the seeded "Fleet parity seed" content under
            # project:fixture-280 exists ONLY in the hub's store (the
            # client data dir starts empty). At base the CLI ignores
            # ZMEM_REMOTE_URL, reads its fresh EMPTY local store, and this
            # is exactly the assertion that fails.
            results = envelope.get("results")
            assert isinstance(results, list), (
                f"the envelope must carry a 'results' list, got: "
                f"{str(envelope)[:300]!r}")
            hub_rows = [row for row in results
                        if isinstance(row, dict)
                        and (SEED_MARKER in str(row.get("content", ""))
                             or row.get("namespace") == SEED_NAMESPACE)]
            assert hub_rows, (
                f"remote-mode recall (ZMEM_REMOTE_URL={hub.url}) returned "
                f"no hub rows — the CLI read the local store instead of "
                f"the hub (issue #289 AC2; the hub holds the seeded "
                f"'{SEED_MARKER}' rows under {SEED_NAMESPACE}): "
                f"{str(envelope)[:300]!r}")

            # The envelope KEY SET must equal the local recall's key set
            # (same JSON envelope shape; storelib/recall.py's keys —
            # results/count/omitted/injection_risk/tokens_used/
            # tokens_budget/arms).
            assert set(envelope) == reference_keys, (
                f"the remote recall envelope's key set must equal the local "
                f"recall envelope's key set (issue #289 AC2); "
                f"remote={sorted(envelope)} local={sorted(reference_keys)}")

            # The pending read-your-writes overlay: the earlier remote add
            # is still queued, so the recall output must carry the
            # render_pending "[PENDING]" marker (fleet/opqueue.py).
            assert "[PENDING]" in (remote.stdout or ""), (
                f"a recall with a pending queued op must render the "
                f"read-your-writes overlay ([PENDING], "
                f"fleet/opqueue.render_pending); stdout tail="
                f"{_tail(remote.stdout)!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- AC3 -------------------------------------------------------------------------

class _TransportRecorder:
    """Records EVERY method call made on a replaced transport object (or
    class-level McpHttp method) and answers a plausible hub receipt shaped
    for both consumption styles the add tool could use: the direct hub
    shape {"ok", "id", "result"} and the _structured_write_response shape
    ({"ok", "stdout", ...}, hermes-plugin/__init__.py:1668-1693)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.receipt_id = uuid.uuid4().hex
        self.response = {
            "ok": True,
            "id": self.receipt_id,
            "result": "stored",
            "stdout": json.dumps({"id": self.receipt_id,
                                  "result": "stored", "warnings": []}),
            "stderr": "",
            "returncode": 0,
        }

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        recorder = self

        def _record(*args, **kwargs):
            recorder.calls.append({"method": name, "args": args,
                                   "kwargs": kwargs})
            return recorder.response
        return _record


class _ClassMethodRecorder:
    """A class-level recorder for fleet.transport.McpHttp methods (the
    tests/test_t08_broker_events.py _StopEvidenceRecorder pattern): catches
    the write even if the fix builds a fresh McpHttp instead of using the
    provider's replaced instance."""

    def __init__(self, sink: list, response: dict):
        self._sink = sink
        self._response = response

    def __call__(self, _transport_self, *args, **kwargs):
        self._sink.append({"args": args, "kwargs": kwargs})
        return self._response


def test_hermes_explicit_add_follows_transport_mode():
    """issue #289 AC3: in MCP mode the Hermes add tool routes the write to
    the broker/hub transport, never to _run_store."""
    tmp = _pinned_scratch("c3")
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
    inserted_name = "zmem_hermes_t10"
    try:
        module = _load_provider_module(inserted_name)
        provider = module.ZmemMemoryProvider()
        assert provider.is_available(), (
            f"the provider must resolve MCP mode under ZMEM_MCP_URL "
            f"(harness sanity); reason={provider.unavailable_reason()!r}")

        probe = ("t10 ac3 hermes add probe: this write must reach the "
                 "broker/hub transport, never a local store subprocess "
                 + uuid.uuid4().hex)
        recorder = _TransportRecorder()
        run_store_calls: list[dict] = []

        def _fail_run_store(*args, **kwargs):
            run_store_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError(
                "issue #289 AC3: the Hermes add tool called _run_store in "
                "MCP mode — the write must be routed to the broker/hub "
                f"transport, not a local store.py subprocess "
                f"(argv={args!r})")

        real_run_store = module._run_store
        real_transport = provider._transport
        mcp_http_cls = type(real_transport) if real_transport is not None \
            else None
        saved_methods = {}
        method_names = [name for name in dir(mcp_http_cls)
                        if not name.startswith("_")] \
            if mcp_http_cls is not None else []
        for name in method_names:
            saved_methods[name] = getattr(mcp_http_cls, name)
            setattr(mcp_http_cls, name,
                    _ClassMethodRecorder(recorder.calls, recorder.response))
        module._run_store = _fail_run_store
        provider._transport = recorder
        try:
            response = provider.handle_tool_call(
                "zmem_add", {"type": "fact", "content": probe,
                             "namespace": "user:global"})
        finally:
            module._run_store = real_run_store
            provider._transport = real_transport
            for name, method in saved_methods.items():
                setattr(mcp_http_cls, name, method)

        assert recorder.calls, (
            f"the add tool in MCP mode must send the write through the "
            f"transport (broker/hub) — no transport call was recorded; "
            f"response={str(response)[:300]!r}")
        flat_calls = json.dumps(
            [{"args": c.get("args"), "kwargs": c.get("kwargs"),
              "method": c.get("method")} for c in recorder.calls],
            ensure_ascii=False, default=str)
        assert probe in flat_calls, (
            f"the recorded transport write must carry the probe content — "
            f"the routed op is not this add; calls="
            f"{flat_calls[:400]!r}")
        assert not run_store_calls, (
            "_run_store must never run for an MCP-mode add (issue #289 "
            "AC3)")
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


# -- AC4 -------------------------------------------------------------------------

def test_admin_commands_refuse_in_remote_mode():
    """issue #289 AC4: local-only admin commands refuse loudly in remote
    mode (exit 2, the exact phrase, nothing touched)."""
    tmp = _pinned_scratch("c4")
    try:
        remote_url = _dead_port_url()
        env = _remote_subproc_env(tmp, remote_url)
        # Minimal VALID argv per command (read from storelib/cli.py's
        # argparse): consolidate/organize/reembed/init are flagless-legal;
        # purge requires --id (repeatable); export-dataset requires its
        # dir positional (+ --yes so an unscoped export is confirmed) and
        # the dir lives INSIDE the scratch so a base-mode run stays
        # hermetic. The refusal must fire on MODE grounds BEFORE any store
        # access.
        cases = [
            ("consolidate", []),
            ("organize", []),
            ("reembed", []),
            ("purge", ["--id", str(uuid.uuid4())]),
            ("export-dataset", [os.path.join(tmp, "ds"), "--yes"]),
            ("init", []),
        ]
        for command, extra in cases:
            proc = _run_store_env(env, command, *extra)
            combined = (proc.stdout or "") + (proc.stderr or "")
            assert REFUSAL_PHRASE in combined, (
                f"'{command}' in remote mode must refuse with the exact "
                f"phrase {REFUSAL_PHRASE!r} (issue #289 AC4); "
                f"rc={proc.returncode} stdout={_tail(proc.stdout)!r} "
                f"stderr={_tail(proc.stderr)!r}")
            assert proc.returncode == 2, (
                f"'{command}' in remote mode must exit 2, got "
                f"{proc.returncode}")
            store_path = Path(tmp) / "store.sqlite"
            assert not store_path.exists(), (
                f"'{command}' touched the local store ({store_path} "
                f"exists) — a remote-mode admin refusal must happen "
                f"before any store access (issue #289 AC4)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- standalone runner (manual lane; CI/pytest runs the functions) ----------------

_CHECKS = {
    "C1": test_cli_add_in_remote_mode_never_creates_local_store,
    "C2": test_cli_recall_in_remote_mode_reads_hub,
    "C3": test_hermes_explicit_add_follows_transport_mode,
    "C4": test_admin_commands_refuse_in_remote_mode,
}


def _all_checks():
    return tuple(_CHECKS[selector] for selector in ("C1", "C2", "C3", "C4"))


def _main(argv: list[str]) -> int:
    selectors = argv[1:]
    for selector in selectors:
        if selector not in _CHECKS:
            print(f"NO CHECK {selector}", file=sys.stderr)
            return 2
    selected = ([_CHECKS[s] for s in selectors] if selectors
                else list(_all_checks()))
    failures = []
    for check in selected:
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
    sys.exit(_main(sys.argv))
