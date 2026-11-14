"""Real isolated local MCP hub integration tests (knowledge companion PR1).

Unlike tests/test_mcp_server.py (which drives ``build_server`` in-process
through the ToolManager), this module starts the REAL hub as a subprocess —
``python hermes-plugin/server/mcp_server.py --host 127.0.0.1 --port <free>``
— on an ephemeral loopback port with a TEMP store (``ZMEM_STORE`` under a
per-run temp dir) and a generated TEST-ONLY bearer token (``zmem-hub-it-*``;
never read from or written to the operator's ``~/.zmem``).  Clients are
REAL network clients: ``fleet.transport.McpHttp`` (the same transport the
workflow uses) and ``store.py knowledge ...`` subprocesses running in remote
mode (``ZMEM_REMOTE_URL`` + ``ZMEM_DATA``; remote mode REFUSES ``ZMEM_STORE``).

Coverage (bounded, essential — no mock framework; the ONLY stub is
``_OldHubStub``, a stand-in for an OLD hub that lacks ``knowledge_link_v1``
and is explicitly NOT a live old-hub instance):

- hello over real HTTP advertises ``knowledge_link_v1``; wrong bearer denied.
- Cross-host flow, two INDEPENDENT data/journal dirs (a/ and b/) sharing one
  temp Markdown vault: A captures v1; B — empty journal, BEFORE any capture
  of its own — resolves the authoritative pointer through the hub's
  ``knowledge_pointer`` tool and reads/verifies the bytes from the shared
  vault; B captures changed content and the hub pointer advances to v2
  (B staked the HUB's row, not a local sidecar); A re-capturing its old
  bytes is an honest version-3 advance (``existed_match``, no silent
  supersede).
- Pending lane: capture against an unreachable hub is exit-3 PENDING with
  NOTHING enqueued (capability-before-queue); the sanctioned recovery
  (re-capture against the live hub) applies v1.  A genuinely queued entry
  (built with the delivery module's own ``enqueue``/``build_link_op``, in a
  real sidecar file) drains to the live hub via ``knowledge flush``.
- Namespace authz on a SECOND hub started with a scoped legacy token file:
  knowledge ops in a foreign namespace are denied with no pointer oracle
  (no row written, nothing disclosed), and a same-op-id replay against a
  denied namespace does NOT replay the committed receipt's canonical id.
- Engine-level probe (storelib.ops.apply_ops on a scratch store): the
  knowledge_link pre-PK gate (authorize namespace/capability BEFORE the
  applied_ops replay lookup) — denied-ns replay and unknown-principal
  replay reject without canonical-id disclosure; authorized replay stays an
  idempotent duplicate.
- Core lane health: a standard ``add`` and a core ``apply_ops`` batch still
  commit AFTER unsupported/pending knowledge attempts.
- Remote read/check/status never open or create a local store (no
  store.sqlite appears under any client data dir); incorrect knowledge
  subcommands are argparse-rejected (exit 2).

Execution form: every test method is self-contained (safe under
pytest-randomly shuffling and under plain ``python tests/…``); the ordered
cross-host story (A captures -> B discovers -> B advances -> A honest
re-capture) lives inside ONE end-to-end method by design.  No claim is
made about unordered execution of a split scenario because there is none.

Bounds/cleanup: every subprocess this module starts is its own child (PID
recorded); the hub is terminated (then killed if needed) in tearDownClass;
server stdout/stderr go to a temp log file (never an inherited pipe); every
client call has a hard timeout.  Nothing listens off-loopback, no commit,
no push, no config deploy, no production notes (all content strings are
synthetic literals).

Run (the ``mcp`` package is required — the interpreter must have it):
    py -3.11 -m unittest tests.test_knowledge_hub_integration -v
    py -3.11 -m pytest tests/test_knowledge_hub_integration.py -v
"""

from __future__ import annotations

import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

_CLIENT_TIMEOUT_S = 90      # store.py first-run may init the store schema
_HEALTH_TIMEOUT_S = 30.0    # bounded readiness poll for the hub subprocess
_SERVER_STOP_S = 8.0        # terminate->kill window for our own child


def _free_port() -> int:
    """One ephemeral loopback port (bind 0, read, close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_ok(url: str, timeout_s: float = 2.0) -> bool:
    """True when GET url answers 200 (stdlib, bounded)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class HubProc:
    """One REAL hub subprocess on loopback; we own its PID exclusively."""

    def __init__(self, tmp: Path, *, env_overrides: dict) -> None:
        self._tmp = tmp
        self._env_overrides = env_overrides
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.url = ""
        self.log_path = tmp / "server.log"

    def start(self) -> None:
        child_env = dict(os.environ)
        child_env.pop("ZMEM_REMOTE_URL", None)
        child_env.pop("ZMEM_DATA", None)
        child_env.pop("ZMEM_MCP_TOKEN", None)
        child_env.pop("ZMEM_MCP_TOKEN_FILE", None)
        child_env["ZMEM_HOME"] = str(REPO_ROOT)
        child_env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        child_env.update(self._env_overrides)
        last_err = ""
        for _attempt in range(3):
            port = _free_port()
            log = open(self.log_path, "ab")  # noqa: SIM115 - closed in stop()
            try:
                self.proc = subprocess.Popen(  # noqa: S603 - fixed argv
                    [sys.executable, "-u", str(SERVER_PY),
                     "--host", "127.0.0.1", "--port", str(port)],
                    stdout=log, stderr=subprocess.STDOUT, env=child_env,
                )
            finally:
                log.close()
            deadline = time.monotonic() + _HEALTH_TIMEOUT_S
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break  # died: read the log, retry on a fresh port
                if _http_ok(f"http://127.0.0.1:{port}/health"):
                    self.port = port
                    self.url = f"http://127.0.0.1:{port}/mcp"
                    return
                time.sleep(0.25)
            last_err = self._log_tail()
            self._reap()
        raise AssertionError(
            "hub subprocess did not become healthy on 3 ephemeral ports; "
            f"last server log tail:\n{last_err}")

    def _log_tail(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8",
                                           errors="replace")[-1500:]
        except OSError:
            return "<no log>"

    def _reap(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=_SERVER_STOP_S)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self.proc = None

    def stop(self) -> None:
        """Terminate (then kill) OUR OWN child PID only; bounded."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=_SERVER_STOP_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=_SERVER_STOP_S)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        self.proc = None


def _mcp_call():
    """The stdlib MCP client's raw ``_call`` (same loader the fleet
    transport uses) — direct tool access for assertions on exact verdicts."""
    path = REPO_ROOT / "hermes-plugin" / "server" / "mcp_client.py"
    spec = importlib.util.spec_from_file_location(
        "zmem_mcp_client_it", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_mcp_client_it"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module._call


def _call_tool(url: str, token: str, tool: str, arguments: dict) -> dict:
    """Run one REAL MCP tool call (fresh loop per call; bounded by caller)."""
    import asyncio

    call = _mcp_call()
    return asyncio.run(call(url, token, tool, arguments))


def _transport(url: str, token: str):
    """A real fleet.transport.McpHttp against the live hub."""
    from fleet import transport

    return transport.McpHttp(url=url, executor=transport.DeadlineExecutor(),
                             deadline_s=6.0, token=token)


def _write_client_config(data_dir: Path, vault: Path) -> None:
    """The PR1 knowledge config: ONE temp Markdown vault target."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump({"knowledge": {"enabled": True, "targets": {
            "notes": {"kind": "markdown", "root": str(vault)}}}}, f)


def _client_env(data_dir: Path, url: str, token: str,
                *, remote_url: str | None = None) -> dict:
    """Env for a ``store.py knowledge`` subprocess in REMOTE mode.

    Remote mode refuses ZMEM_STORE; ZMEM_DATA names the client's own
    data/journal/sidecar dir (the two hosts' independence under test).
    """
    env = dict(os.environ)
    for key in ("ZMEM_STORE", "ZMEM_REMOTE_URL", "ZMEM_MCP_URL"):
        env.pop(key, None)
    env["ZMEM_HOME"] = str(REPO_ROOT)
    env["ZMEM_DATA"] = str(data_dir)
    env["ZMEM_MCP_TOKEN"] = token
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_MODELS_DIR"] = str(data_dir.parent / "no-models")
    if remote_url is not None:
        env["ZMEM_REMOTE_URL"] = remote_url
    else:
        env["ZMEM_REMOTE_URL"] = url
    return env


def _hex64() -> str:
    """A synthetic 64-hex digest for probe envelopes (never a secret)."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _run_client(data_dir: Path, url: str, token: str, args: list[str],
                *, remote_url: str | None = None,
                timeout_s: float = _CLIENT_TIMEOUT_S):
    """Run ``store.py <args>`` as a remote-mode client subprocess.

    Returns (rc, parsed_json_or_None, stderr).  No ``-I``: the remote
    client needs the same interpreter's site packages (``mcp``) the
    deployment uses; the script path is absolute repo code."""
    env = _client_env(data_dir, url, token, remote_url=remote_url)
    proc = subprocess.run(  # noqa: S603 - fixed interpreter + repo script
        [sys.executable, str(STORE_PY), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout_s, cwd=str(REPO_ROOT),
    )
    parsed = None
    out = (proc.stdout or "").strip()
    if out.startswith("{"):
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
    return proc.returncode, parsed, proc.stderr or ""


def _link_args(namespace: str, target: str, key: str, digest: str,
               version: int, *, expected_row_id: str | None = None) -> dict:
    """One valid knowledge_link op args envelope.

    The locator is the DERIVED deterministic form (``<key>/<4x16hex>.md``
    — knowledge/documents.version_locator); link_pointer refuses any other
    shape, and the digest travels as the storage-form 8x8-hex chunks.
    """
    chunks = [digest[i * 8:(i + 1) * 8] for i in range(8)]
    locator = "%s/%s.md" % (key, "-".join(
        digest[i:i + 16] for i in range(0, 64, 16)))
    return {
        "namespace": namespace, "target": target, "key": key,
        "version": version,
        "locator": locator,
        "fullhash": chunks, "summary": "integration probe summary",
        "date": "2026-11-13T00:00:00Z",
        "expected_row_id": expected_row_id,
    }


@unittest.skipUnless(
    MCP_AVAILABLE,
    "mcp package not installed — run the real-hub integration under an "
    "interpreter that has it (verified: py -3.11; py -3 currently resolves "
    "3.14 which lacks mcp). Not skipped for any other reason.")
class RealHubKnowledgeFlowTest(unittest.TestCase):
    """The real-hub flow; every method is order-independent (the ordered
    cross-host story is one end-to-end method — see module docstring)."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="zmem-hub-it-"))
        cls.hub_dir = cls._tmp / "hub"
        cls.hub_dir.mkdir(parents=True, exist_ok=True)
        cls.vault = cls._tmp / "vault"
        cls.vault.mkdir(parents=True, exist_ok=True)
        # Generated TEST-ONLY token: a literal the test mints per run; it is
        # never sourced from (or written to) the operator's ~/.zmem.
        cls.token = "zmem-hub-it-" + uuid.uuid4().hex
        cls.hub = HubProc(
            cls.hub_dir,
            env_overrides={
                "ZMEM_STORE": str(cls.hub_dir / "store.sqlite"),
                "ZMEM_MCP_TOKEN": cls.token,
                "ZMEM_MODELS_DIR": str(cls._tmp / "no-models"),
            })
        cls.hub.start()
        # Two INDEPENDENT client data/journal dirs, one shared vault.
        cls.dir_a = cls._tmp / "a"
        cls.dir_b = cls._tmp / "b"
        cls.dir_c = cls._tmp / "c"
        cls.dir_d = cls._tmp / "d"
        for d in (cls.dir_a, cls.dir_b, cls.dir_c, cls.dir_d):
            _write_client_config(d, cls.vault)
        cls.key = "guides/deploy-check"
        cls.ns = "project:hub-it"

    @classmethod
    def tearDownClass(cls):
        cls.hub.stop()  # our own child PID only
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- 01: handshake -----------------------------------------------------

    def test_01_hello_advertises_knowledge_capability(self):
        http = _transport(self.hub.url, self.token)
        payload = http.hello()
        self.assertIn("knowledge_link_v1", payload.get("capabilities", []),
                      payload)
        self.assertEqual(payload.get("protocol"), 1, payload)
        self.assertTrue(str(payload.get("zmem_version") or ""), payload)

    def test_02_wrong_bearer_token_denied(self):
        with self.assertRaises(Exception) as ctx:
            _call_tool(self.hub.url, "zmem-hub-it-wrong-" + uuid.uuid4().hex,
                       "hello", {})
        self.assertNotIn(self.token, str(ctx.exception))

    # -- the ordered cross-host story, ONE method (see module docstring) ---

    def test_cross_host_capture_discover_advance_end_to_end(self):
        # (1) Host A captures v1: applied, journal applied, vault file
        # created, delivery queue drained.
        rc, a1, err = _run_client(
            self.dir_a, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes", "--key", self.key,
             "--content", "Deploy checklist v1: run tests then ship.",
             "--summary", "deploy checklist", "--namespace", self.ns,
             "--json"])
        self.assertEqual(rc, 0, (a1, err))
        self.assertTrue(a1 and a1.get("ok"), a1)
        self.assertTrue(a1["completed"], a1)
        self.assertEqual(a1["pointer"]["status"], "applied", a1)
        self.assertEqual(a1["version"], 1, a1)
        self.assertFalse(a1["existed_match"], a1)
        self.assertEqual(a1["journal_state"], "applied", a1)
        locator = a1["locator"]
        self.assertTrue(locator.startswith(self.key + "/"), locator)
        self.assertTrue((self.vault / locator).is_file(), locator)
        sidecar = json.loads(
            (self.dir_a / "knowledge" / "delivery.json").read_text(
                encoding="utf-8"))
        self.assertEqual(sidecar["queue"], [], sidecar)

        # (2) Host B — empty sidecar dir, BEFORE any capture of its own —
        # resolves the AUTHORITATIVE pointer through the hub.
        self.assertFalse(
            (self.dir_b / "knowledge").exists(),
            "host B must start with no sidecar state")
        http = _transport(self.hub.url, self.token)
        answer = http.knowledge_pointer("notes", self.key, namespace=self.ns)
        self.assertTrue(answer.get("ok"), answer)
        pointer = answer.get("pointer") or {}
        self.assertEqual(pointer.get("version"), 1, pointer)
        self.assertTrue(pointer.get("row_id"), pointer)
        self.assertTrue(pointer.get("locator", "").startswith(
            self.key + "/"), pointer)
        digest = pointer.get("full_sha256")
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64, pointer)
        rc, read_b, err = _run_client(
            self.dir_b, self.hub.url, self.token,
            ["knowledge", "read", "--target", "notes", "--key", self.key,
             "--namespace", self.ns, "--locator", pointer["locator"],
             "--json"])
        self.assertEqual(rc, 0, (read_b, err))
        self.assertTrue(read_b.get("ok"), read_b)
        self.assertEqual(read_b["actual_digest"], digest, read_b)
        self.assertEqual(read_b["expected_digest"], digest, read_b)
        # Discover-read WITHOUT a locator on a host with no local state:
        # resolves through the HUB's authoritative pointer (the workflow's
        # remote read path) — same digest, same bytes as A published.
        rc2, read_nl, _ = _run_client(
            self.dir_b, self.hub.url, self.token,
            ["knowledge", "read", "--target", "notes", "--key", self.key,
             "--namespace", self.ns, "--json"])
        self.assertEqual(rc2, 0, read_nl)
        self.assertTrue(read_nl.get("ok"), read_nl)
        self.assertEqual(read_nl.get("provenance"), "hub-pointer", read_nl)
        self.assertEqual(read_nl["actual_digest"], digest, read_nl)
        self.assertEqual(read_nl["content"],
                         "Deploy checklist v1: run tests then ship.\n",
                         read_nl)

        # (3) Host B captures CHANGED content: version 2, applied — the
        # hub pointer now carries B's digest (B staked the HUB's row).
        rc, b2, err = _run_client(
            self.dir_b, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes", "--key", self.key,
             "--content",
             "Deploy checklist v2: run tests, gate, then ship.",
             "--summary", "deploy checklist v2", "--namespace", self.ns,
             "--json"])
        self.assertEqual(rc, 0, (b2, err))
        self.assertTrue(b2.get("ok"), b2)
        self.assertEqual(b2["version"], 2, b2)
        self.assertEqual(b2["pointer"]["status"], "applied", b2)
        answer2 = http.knowledge_pointer("notes", self.key, namespace=self.ns)
        self.assertEqual((answer2.get("pointer") or {}).get("version"), 2,
                         answer2)
        digest_b = b2["digest"]
        self.assertEqual((answer2.get("pointer") or {}).get("full_sha256"),
                         digest_b, answer2)
        rc2, read_b2, _ = _run_client(
            self.dir_b, self.hub.url, self.token,
            ["knowledge", "read", "--target", "notes", "--key", self.key,
             "--namespace", self.ns, "--json"])
        self.assertEqual(rc2, 0, read_b2)
        self.assertEqual(read_b2.get("provenance"), "hub-pointer", read_b2)
        self.assertEqual(read_b2.get("actual_digest"), digest_b, read_b2)

        # (4) Host A re-captures its OLD bytes after B advanced: the
        # immutable doc already existed (existed_match) and the pointer
        # honestly ADVANCED to v3 — no silent supersede of B's publication.
        rc, a3, err = _run_client(
            self.dir_a, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes", "--key", self.key,
             "--content", "Deploy checklist v1: run tests then ship.",
             "--summary", "deploy checklist", "--namespace", self.ns,
             "--json"])
        self.assertEqual(rc, 0, (a3, err))
        self.assertTrue(a3.get("ok"), a3)
        self.assertTrue(a3["existed_match"], a3)
        self.assertEqual(a3["version"], 3, a3)
        self.assertEqual(a3["pointer"]["status"], "applied", a3)

    # -- pending lane + flush (independent) ---------------------------------

    def test_unreachable_hub_capture_pending_then_recovery(self):
        dead = f"http://127.0.0.1:{_free_port()}/mcp"
        rc, payload, err = _run_client(
            self.dir_c, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes",
             "--key", "guides/pending-lane",
             "--content", "Pending lane probe: doc published, pointer not.",
             "--namespace", self.ns, "--json"],
            remote_url=dead)
        self.assertEqual(rc, 3, (rc, payload, err))
        self.assertFalse(payload.get("ok"), payload)
        self.assertFalse(payload["completed"], payload)
        self.assertTrue(payload["doc_published"], payload)
        self.assertEqual(payload["pointer"]["status"], "pending", payload)
        self.assertEqual(payload["pointer"]["reason"], "hub-unreachable",
                         payload)
        # Capability BEFORE queue: nothing was enqueued anywhere.
        sidecar = json.loads(
            (self.dir_c / "knowledge" / "delivery.json").read_text(
                encoding="utf-8"))
        self.assertEqual(sidecar["queue"], [], sidecar)
        # Sanctioned recovery: re-capture the same screened input against
        # the LIVE hub — applies as v1.
        rc2, payload2, err2 = _run_client(
            self.dir_c, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes",
             "--key", "guides/pending-lane",
             "--content", "Pending lane probe: doc published, pointer not.",
             "--namespace", self.ns, "--json"])
        self.assertEqual(rc2, 0, (payload2, err2))
        self.assertTrue(payload2.get("ok"), payload2)
        self.assertEqual(payload2["version"], 1, payload2)
        self.assertEqual(payload2["pointer"]["status"], "applied", payload2)

    def test_queued_entry_flushes_to_live_hub(self):
        from knowledge.delivery import KnowledgeDelivery, build_link_op

        delivery_path = self.dir_c / "knowledge" / "delivery.json"
        delivery = KnowledgeDelivery(delivery_path)
        digest = _hex64()
        entry = {
            "namespace": self.ns, "target": "notes",
            "key": "guides/flush-lane", "version": 1,
            "locator": "guides/flush-lane/%s.md" % "-".join(
                digest[i:i + 16] for i in range(0, 64, 16)),
            "fullhash": [digest[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "flush lane probe", "date": "2026-11-13T00:00:00Z",
            "expected_row_id": None,
        }
        queued = delivery.enqueue(entry)
        op_id = queued["op_id"]
        rc, payload, err = _run_client(
            self.dir_c, self.hub.url, self.token,
            ["knowledge", "flush", "--json"])
        self.assertEqual(rc, 0, (payload, err))
        self.assertTrue(payload.get("ok"), payload)
        self.assertIn(op_id, payload.get("applied") or [], payload)
        self.assertEqual(payload.get("pending"), 0, payload)
        # The queue drained and the HUB holds the pointer row.
        after = KnowledgeDelivery(delivery_path)
        self.assertEqual(after.pending(), [])
        http = _transport(self.hub.url, self.token)
        answer = http.knowledge_pointer("notes", "guides/flush-lane",
                                        namespace=self.ns)
        self.assertTrue(answer.get("ok"), answer)
        self.assertEqual((answer.get("pointer") or {}).get("full_sha256"),
                         digest, answer)

    # -- core lane health (independent) --------------------------------------

    def test_core_add_and_apply_ops_survive_knowledge_attempts(self):
        result = _call_tool(self.hub.url, self.token, "add", {
            "type": "fact", "content": "core add after knowledge pending",
            "namespace": self.ns, "signal": "test"})
        self.assertEqual(result.get("result"), "stored", result)
        http = _transport(self.hub.url, self.token)
        core = {"op_id": "it-core-" + uuid.uuid4().hex, "op": "add",
                "args": {"type": "lesson",
                         "content": "core apply_ops after knowledge attempts",
                         "namespace": self.ns}}
        batch = http.apply_ops([core])
        self.assertTrue(batch.get("ok"), batch)
        self.assertEqual(batch["receipts"][0]["status"], "committed", batch)

    # -- remote read surfaces never open local state (independent) ----------

    def test_remote_read_surfaces_never_create_local_store(self):
        # Self-contained (own data dir): capture a doc, then drive every
        # read-only surface and assert no local store appeared.
        rc, cap, err = _run_client(
            self.dir_d, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes",
             "--key", "guides/ro-surface", "--content",
             "Read-only surface probe document.",
             "--namespace", self.ns, "--json"])
        self.assertEqual(rc, 0, (cap, err))
        rc2, status_b, err2 = _run_client(
            self.dir_d, self.hub.url, self.token,
            ["knowledge", "status", "--namespace", self.ns, "--json"])
        self.assertEqual(rc2, 0, (status_b, err2))
        self.assertTrue(status_b.get("ok"), status_b)
        self.assertIsNone(status_b.get("pointers"), status_b)
        self.assertIn("hub", str(status_b.get("hub_note", "")), status_b)
        rc3, check_b, err3 = _run_client(
            self.dir_d, self.hub.url, self.token,
            ["knowledge", "check", "--target", "notes",
             "--key", "guides/ro-surface", "--namespace", self.ns,
             "--json"])
        self.assertEqual(rc3, 0, (check_b, err3))
        self.assertTrue(check_b.get("ok"), check_b)
        self.assertTrue(check_b.get("matches"), check_b)
        for d in (self.dir_a, self.dir_b, self.dir_c, self.dir_d):
            hits = list(d.rglob("store.sqlite"))
            self.assertEqual(hits, [],
                             "remote-mode knowledge surfaces must not open "
                             "or create a local store: %s" % hits)

    # -- incorrect subcommands rejected (independent) ------------------------

    def test_incorrect_subcommands_rejected(self):
        cases = [
            ["knowledge", "bogus", "--json"],
            ["knowledge", "capture", "--target", "notes", "--json"],
            ["knowledge", "read", "--key", self.key, "--json"],
            ["knowledge", "flush", "extra-positional", "--json"],
        ]
        for args in cases:
            rc, _payload, _err = _run_client(
                self.dir_b, self.hub.url, self.token, args)
            self.assertEqual(rc, 2, args)
        # Unknown TOOL on the wire is an MCP error, never a silent empty.
        with self.assertRaises(Exception):
            _call_tool(self.hub.url, self.token,
                       "knowledge_pointer_bogus", {})


@unittest.skipUnless(
    MCP_AVAILABLE,
    "mcp package not installed — run the real-hub integration under an "
    "interpreter that has it (verified: py -3.11).")
class ScopedHubNamespaceAuthzTest(unittest.TestCase):
    """A SECOND real hub whose legacy token is SCOPED to one namespace:
    knowledge ops in a foreign namespace are denied with no pointer oracle,
    and a denied-namespace REPLAY of a committed op_id never replays the
    committed receipt's canonical id."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="zmem-hub-scoped-it-"))
        cls.hub_dir = cls._tmp / "hub"
        cls.hub_dir.mkdir(parents=True, exist_ok=True)
        cls.vault = cls._tmp / "vault"
        cls.vault.mkdir(parents=True, exist_ok=True)
        cls.token = "zmem-hub-it-" + uuid.uuid4().hex
        token_file = cls.hub_dir / "scoped-token.json"
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump({"token": cls.token,
                       "namespaces": ["project:hub-it"]}, f)
        cls.hub = HubProc(
            cls.hub_dir, env_overrides={
                "ZMEM_STORE": str(cls.hub_dir / "store.sqlite"),
                "ZMEM_MCP_TOKEN_FILE": str(token_file),
                "ZMEM_MODELS_DIR": str(cls._tmp / "no-models"),
            })
        cls.hub.start()
        cls.dir_client = cls._tmp / "client"
        _write_client_config(cls.dir_client, cls.vault)

    @staticmethod
    def _seeded_vault_doc(vault: Path, key: str, digest: str,
                          text: str) -> str:
        """Write a real vault file at the DERIVED locator and return it."""
        locator = "%s/%s.md" % (key, "-".join(
            digest[i:i + 16] for i in range(0, 64, 16)))
        path = vault / Path(locator)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return locator

    def test_scoped_knowledge_pointer_read_denied_foreign_namespace(self):
        """The scoped token cannot read pointers outside its namespace and
        the denial discloses no pointer data (same shape as an HTTP 401:
        no data, refusal only)."""
        http = _transport(self.hub.url, self.token)
        answer = http.knowledge_pointer("notes", "guides/authz-probe",
                                        namespace="project:other")
        self.assertFalse(answer.get("ok"), answer)
        self.assertNotIn("pointer", answer)  # no pointer payload on denial
        # The exact hub verdict through the direct client call.
        with self.assertRaises(Exception) as ctx:
            _call_tool(self.hub.url, self.token, "knowledge_pointer",
                       {"target": "notes", "key": "guides/authz-probe",
                        "namespace": "project:other"})
        self.assertIn("namespace_not_allowed", str(ctx.exception))
        # A WRONG token (HTTP 401 class) is equally data-free.
        with self.assertRaises(Exception) as ctx2:
            _call_tool(self.hub.url, "zmem-hub-it-wrong-" + uuid.uuid4().hex,
                       "knowledge_pointer",
                       {"target": "notes", "key": "guides/authz-probe",
                        "namespace": "project:hub-it"})
        self.assertNotIn("pointer", str(ctx2.exception))

    def test_scoped_read_no_local_intent_fallback_in_denied_namespace(self):
        """Authorization may not be bypassed by a LOCAL sidecar: a seeded
        delivery intent (with real vault bytes behind it) in a namespace the
        token cannot touch must NOT satisfy ``knowledge read`` — the hub
        read is denied and the local fallback must not hand over content."""
        from knowledge.delivery import KnowledgeDelivery

        key = "guides/seeded-" + uuid.uuid4().hex[:8]
        digest = _hex64()
        locator = self._seeded_vault_doc(
            self.vault, key, digest, "Seeded local-intent probe bytes.\n")
        delivery = KnowledgeDelivery(
            self.dir_client / "knowledge" / "delivery.json")
        delivery.set_intent(ref="knowledge:notes:%s" % key,
                            namespace="project:other", fields={
                                "target": "notes", "key": key,
                                "namespace": "project:other",
                                "digest": digest, "locator": locator,
                                "version": 1, "expected_row_id": None,
                                "row_id": None, "state": "applied",
                                "op_id": None, "staked": True})
        rc, payload, err = _run_client(
            self.dir_client, self.hub.url, self.token,
            ["knowledge", "read", "--target", "notes", "--key", key,
             "--namespace", "project:other", "--json"])
        # SECURE CONTRACT: refuse — never the seeded local bytes.
        self.assertFalse(
            payload and payload.get("ok"),
            "local-intent fallback disclosed content in a namespace the "
            "token cannot touch: %r" % (payload,))
        self.assertNotIn("Seeded local-intent probe bytes",
                         json.dumps(payload or {}))

    def test_scoped_capture_foreign_ns_is_terminal_rejection(self):
        """A REAL network capture in a foreign namespace is a TERMINAL
        rejection: exit 1 (never pending-3, never a perpetual queue), the
        queue empties, the journal entry is quarantined, and the receipt is
        kept for the operator.  Whether the doc create is additionally
        refused BEFORE publication is a separate worker decision (probed
        and reported; not asserted here)."""
        rc, payload, err = _run_client(
            self.dir_client, self.hub.url, self.token,
            ["knowledge", "capture", "--target", "notes",
             "--key", "guides/foreign-capture", "--content",
             "Foreign namespace capture probe document.",
             "--namespace", "project:other", "--json"])
        self.assertEqual(rc, 1, (rc, payload, err))
        self.assertFalse(payload.get("ok"), payload)
        self.assertEqual(payload["pointer"]["status"], "rejected", payload)
        receipt = payload["pointer"].get("receipt") or {}
        self.assertTrue(receipt.get("op_id"), payload)
        # Terminal: queue drained (no perpetual pending), journal
        # quarantined, receipt retained.
        sidecar = json.loads(
            (self.dir_client / "knowledge" / "delivery.json").read_text(
                encoding="utf-8"))
        self.assertEqual(sidecar["queue"], [], sidecar)
        journal = json.loads(
            (self.dir_client / "knowledge" / "journal.json").read_text(
                encoding="utf-8"))
        states = [row.get("state") for row in journal.get("entries", [])
                  if row.get("ref") == "knowledge:notes:guides/foreign-capture"]
        self.assertIn("quarantined", states, journal)
        self.assertTrue(
            any(r.get("op_id") == receipt.get("op_id")
                for r in sidecar.get("receipts", {}).values()), sidecar)
        # Locals stay clean (no local store) and the pre-doc-refusal state
        # is RECORDED, not asserted (worker-pending): doc_published says
        # whether the immutable create ran before the rejection.
        self.assertIsInstance(payload.get("doc_published"), bool, payload)
        self.assertEqual(list(self.dir_client.rglob("store.sqlite")), [])

    @classmethod
    def tearDownClass(cls):
        cls.hub.stop()
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _knowledge_op(self, op_id: str, namespace: str) -> dict:
        # UNIQUE key per op: the hub store is shared across the class'
        # tests; a repeated key would hit the pointer CAS instead of the
        # namespace gate under test.
        digest = _hex64()
        key = "guides/authz-probe-" + uuid.uuid4().hex[:8]
        args = _link_args(namespace, "notes", key, digest, 1)
        return {"op_id": op_id, "op": "knowledge_link", "args": args}

    def _hub_pointer_rows(self, namespace: str) -> int:
        store = self.hub_dir / "store.sqlite"
        if not store.is_file():
            return 0  # no store was ever created: nothing can have leaked
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memory WHERE namespace=? AND "
                "source_ref LIKE 'knowledge:%'", (namespace,)).fetchone()[0]
        finally:
            conn.close()

    def test_scoped_knowledge_in_own_namespace_commits(self):
        http = _transport(self.hub.url, self.token)
        op = self._knowledge_op("it-authz-ok-" + uuid.uuid4().hex,
                                "project:hub-it")
        result = http.apply_knowledge_ops([op])
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["receipts"][0]["status"], "committed",
                         result)

    def test_scoped_knowledge_foreign_namespace_denied_no_oracle(self):
        http = _transport(self.hub.url, self.token)
        op = self._knowledge_op("it-authz-deny-" + uuid.uuid4().hex,
                                "project:other")
        # Exact hub verdict (direct client call): the structured refusal.
        with self.assertRaises(Exception) as ctx:
            _call_tool(self.hub.url, self.token, "apply_ops",
                       {"ops": [op]})
        self.assertIn("namespace_not_allowed", str(ctx.exception))
        # Through the transport wrapper the same refusal fails CLOSED.
        result = http.apply_knowledge_ops([op])
        self.assertFalse(result.get("ok"), result)
        # No pointer oracle: the denied namespace gained NO row, and the
        # denial string carries no canonical row id.
        self.assertEqual(self._hub_pointer_rows("project:other"), 0)

    def test_scoped_replay_same_op_id_denied_ns_no_canonical_leak(self):
        op_id = "it-authz-replay-" + uuid.uuid4().hex
        http = _transport(self.hub.url, self.token)
        good = self._knowledge_op(op_id, "project:hub-it")
        first = http.apply_knowledge_ops([good])
        self.assertTrue(first.get("ok"), first)
        canonical = first["receipts"][0].get("canonical_id")
        self.assertTrue(canonical, first)
        # Same op_id, now carrying a DENIED namespace: must NOT answer the
        # committed 'duplicate' receipt (that would disclose the authorized
        # commit's canonical id against the denied-namespace attempt).
        bad = self._knowledge_op(op_id, "project:other")
        with self.assertRaises(Exception) as ctx:
            _call_tool(self.hub.url, self.token, "apply_ops",
                       {"ops": [bad]})
        message = str(ctx.exception)
        self.assertIn("namespace_not_allowed", message)
        self.assertNotIn(canonical, message)
        # Authorized replay still idempotent: the good op replays duplicate.
        replay = http.apply_knowledge_ops([good])
        self.assertTrue(replay.get("ok"), replay)
        self.assertEqual(replay["receipts"][0]["status"], "duplicate", replay)
        self.assertEqual(replay["receipts"][0].get("canonical_id"),
                         canonical, replay)
        self.assertEqual(self._hub_pointer_rows("project:other"), 0)


@unittest.skipUnless(
    MCP_AVAILABLE,
    "mcp package not installed — run the real-hub integration under an "
    "interpreter that has it (verified: py -3.11).")
class EngineKnowledgeGateOrderingTest(unittest.TestCase):
    """Direct engine probe (storelib.ops.apply_ops on a scratch store):
    the knowledge_link admission gate — unknown principal, namespace
    scope, write capability — must run BEFORE the applied_ops PK replay
    lookup, so a replay can never disclose a canonical id across a
    namespace (or to an unknown/revoked principal) the caller cannot touch.
    No hub, no network; this pins the engine law the wire guard layers."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="zmem-engine-it-"))
        cls._saved_env = {k: os.environ.get(k) for k in (
            "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_REMOTE_URL")}
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = str(cls._tmp / "store.sqlite")
        os.environ.pop("ZMEM_DATA", None)
        os.environ.pop("ZMEM_REMOTE_URL", None)
        cls._saved_modules = {
            key: value for key, value in list(sys.modules.items())
            if key == "storelib" or key.startswith("storelib.")
        }
        for key in cls._saved_modules:
            del sys.modules[key]
        pkg = SCRIPTS_DIR / "storelib"
        spec = importlib.util.spec_from_file_location(
            "storelib", pkg / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["storelib"] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        cls.storelib = module
        spec_ops = importlib.util.spec_from_file_location(
            "storelib.ops", pkg / "ops.py")
        cls.ops = importlib.util.module_from_spec(spec_ops)
        sys.modules["storelib.ops"] = cls.ops
        spec_ops.loader.exec_module(cls.ops)  # type: ignore[union-attr]

    @classmethod
    def tearDownClass(cls):
        for key in [k for k in list(sys.modules)
                    if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[key]
        sys.modules.update(cls._saved_modules)
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    class _Principal:
        def __init__(self, pid, *, scoped=False, namespaces=(),
                     capabilities=None, revoked=False):
            self.principal_id = pid
            self.scoped = scoped
            self.namespaces = frozenset(namespaces)
            self.capabilities = capabilities
            self.revoked = revoked

    def _connect(self):
        conn = self.storelib.connect()
        self.storelib.init_db(conn)
        self.storelib.migrate(conn)  # applied_ops + pointer tables
        return conn

    def _apply(self, conn, ops, principal, principal_map):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            return self.ops.apply_ops(
                conn, ops, principal=principal,
                principal_lookup=lambda pid: principal_map.get(pid),
                default_principal=None)

    def _op(self, op_id, namespace, digest_hex):
        # UNIQUE key per probe: the scratch store is shared across the
        # class' tests and a repeated key would hit the pointer CAS
        # ("initial link refused") instead of the gate under test.
        key = "guides/engine-probe-" + uuid.uuid4().hex[:8]
        return {"op_id": op_id, "op": "knowledge_link",
                "args": _link_args(namespace, "notes", key, digest_hex, 1)}

    def test_pre_pk_gate_blocks_denied_ns_replay_without_canonical_leak(self):
        conn = self._connect()
        try:
            allowed = self._Principal("p1", scoped=True,
                                      namespaces=["project:hub-it"])
            denied_map = {"p1": allowed}
            op_id = "eng-" + uuid.uuid4().hex
            digest = _hex64()
            first = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p1", denied_map)
            receipt = first["receipts"][0]
            self.assertEqual(receipt["status"], "committed", first)
            canonical = receipt.get("canonical_id")
            self.assertTrue(canonical, first)
            # FRESH op in the denied namespace: rejected before anything.
            fresh = self._apply(
                conn, [self._op("eng2-" + uuid.uuid4().hex,
                                "project:other", digest)],
                "p1", denied_map)
            self.assertEqual(fresh["receipts"][0]["status"], "rejected",
                             fresh)
            self.assertEqual(fresh["receipts"][0]["error"],
                             "namespace_not_allowed", fresh)
            # The ordering law: SAME op_id replayed carrying the DENIED
            # namespace must NOT answer 'duplicate' with the committed
            # canonical id.
            replay = self._apply(
                conn, [self._op(op_id, "project:other", digest)],
                "p1", denied_map)
            r = replay["receipts"][0]
            self.assertEqual(r["status"], "rejected", replay)
            self.assertEqual(r["error"], "namespace_not_allowed", replay)
            self.assertFalse(r.get("canonical_id"), replay)
            # Authorized replay keeps idempotency.
            ok_replay = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p1", denied_map)
            self.assertEqual(ok_replay["receipts"][0]["status"],
                             "duplicate", ok_replay)
            self.assertEqual(ok_replay["receipts"][0].get("canonical_id"),
                             canonical, ok_replay)
        finally:
            conn.close()

    def test_pre_pk_gate_unknown_principal_replay_no_disclosure(self):
        conn = self._connect()
        try:
            allowed = self._Principal("p1", scoped=True,
                                      namespaces=["project:hub-it"])
            principal_map = {"p1": allowed}
            op_id = "eng-" + uuid.uuid4().hex
            digest = _hex64()
            first = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p1", principal_map)
            self.assertEqual(first["receipts"][0]["status"], "committed",
                             first)
            canonical = first["receipts"][0]["canonical_id"]
            # UNKNOWN principal: rejected even for a replay-shaped op id,
            # with no disclosure of p1's committed row.
            unknown = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p9", principal_map)
            self.assertEqual(unknown["receipts"][0]["error"],
                             "unknown_principal", unknown)
            self.assertFalse(unknown["receipts"][0].get("canonical_id"),
                             unknown)
            self.assertNotIn(canonical, json.dumps(unknown))
        finally:
            conn.close()

    def test_pre_pk_gate_revoked_principal_replay_denied(self):
        conn = self._connect()
        try:
            live = self._Principal("p2", scoped=True,
                                   namespaces=["project:hub-it"])
            op_id = "eng-" + uuid.uuid4().hex
            digest = _hex64()
            first = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p2", {"p2": live})
            self.assertEqual(first["receipts"][0]["status"], "committed",
                             first)
            canonical = first["receipts"][0]["canonical_id"]
            # Revocation between calls: p2's write capability is gone, so
            # even its OWN committed op_id replays as capability_denied —
            # never a duplicate receipt disclosing the canonical row.
            revoked = self._Principal("p2", scoped=True,
                                      namespaces=["project:hub-it"],
                                      revoked=True)
            replay = self._apply(
                conn, [self._op(op_id, "project:hub-it", digest)],
                "p2", {"p2": revoked})
            r = replay["receipts"][0]
            self.assertEqual(r["status"], "rejected", replay)
            self.assertEqual(r["error"], "capability_denied", replay)
            self.assertFalse(r.get("canonical_id"), replay)
            self.assertNotIn(canonical, json.dumps(replay))
        finally:
            conn.close()


class _OldHubStub:
    """A MINIMAL stand-in for an OLD hub that lacks ``knowledge_link_v1``.

    NOT a live old-hub instance (none can be run without source edits); it
    speaks just the ``hello``/``apply_knowledge_ops`` surface the delivery
    lane consults, and records whether any apply was attempted.
    """

    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable
        self.apply_attempts = 0

    def hello(self, client_protocol=1, deadline_ms=None):
        if self.unreachable:
            raise ConnectionError("old-hub stub: unreachable")
        return {
            "protocol": 1, "event_schema": 1,
            "capture_policy": "network-auto-redact-v1", "store_schema": 18,
            "zmem_version": "0.140.0-stub-NOT-LIVE-OLD-HUB",
            "supported_protocol_majors": [1],
            # No knowledge_link_v1: the old-hub shape under test.
            "capabilities": ["protocol-negotiation", "auth:legacy-token",
                             "namespace-scoping", "apply-ops", "snapshot"],
        }

    def apply_knowledge_ops(self, ops, principal="", client_protocol=1,
                            deadline_ms=None):
        self.apply_attempts += 1
        return {"ok": False, "reason": "knowledge-batch-refused"}


@unittest.skipUnless(
    MCP_AVAILABLE,
    "mcp package not installed — run the real-hub integration under an "
    "interpreter that has it (verified: py -3.11).")
class OldHubCapabilityNegativeTest(unittest.TestCase):
    """Old-hub negatives: nothing is enqueued or sent to a hub that does
    not advertise knowledge_link_v1 (mock stub — NOT a live old hub), and
    a genuinely unreachable endpoint answers the distinct reason."""

    maxDiff = None

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-oldhub-it-"))
        self.vault = self._tmp / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        self.data = self._tmp / "d"
        _write_client_config(self.data, self.vault)
        from knowledge.delivery import KnowledgeDelivery
        from knowledge.journal import KnowledgeJournal
        self.delivery = KnowledgeDelivery(
            self.data / "knowledge" / "delivery.json")
        self.journal = KnowledgeJournal(
            self.data / "knowledge" / "journal.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_stub_is_labeled_not_live(self):
        stub = _OldHubStub()
        self.assertIn("NOT-LIVE-OLD-HUB",
                      stub.hello()["zmem_version"])

    def test_oldhub_stub_capability_unsupported_and_queue_paused(self):
        from knowledge.delivery import flush_to_hub, hub_capability_status
        stub = _OldHubStub()
        status, detail = hub_capability_status(stub)
        self.assertEqual(status, "unsupported", (status, detail))
        self.assertIn("knowledge_link_v1", detail)
        digest = _hex64()
        self.delivery.enqueue({
            "namespace": "project:hub-it", "target": "notes",
            "key": "guides/old-hub", "version": 1,
            "locator": "guides/old-hub/%s.md" % "-".join(
                digest[i:i + 16] for i in range(0, 64, 16)),
            "fullhash": [digest[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "old hub probe", "date": "2026-11-13T00:00:00Z",
            "expected_row_id": None})
        result = flush_to_hub(self.delivery, stub)
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["paused"], result)
        self.assertEqual(len(self.delivery.pending()), 1,
                         "rollback pause must keep the entry queued")
        self.assertEqual(stub.apply_attempts, 0,
                         "nothing may be sent to an old hub")

    def test_capture_against_oldhub_stub_is_pending_not_enqueued(self):
        from knowledge.config import load_knowledge_config
        from knowledge.workflow import capture
        config = load_knowledge_config(
            path=self.data / "config.json", env={})
        stub = _OldHubStub()
        envelope = capture(
            target_id="notes", logical_key="guides/old-hub-cap",
            text="Old hub capability probe document.",
            namespace="project:hub-it", mode="remote", config=config,
            journal=self.journal, delivery=self.delivery, http=stub)
        self.assertFalse(envelope["ok"], envelope)
        self.assertFalse(envelope["completed"], envelope)
        self.assertTrue(envelope["doc_published"], envelope)
        self.assertEqual(envelope["pointer"]["status"], "pending", envelope)
        self.assertEqual(envelope["pointer"]["reason"], "hub-unsupported",
                         envelope)
        self.assertEqual(self.delivery.pending(), [],
                         "capability BEFORE queue: nothing enqueued")
        self.assertEqual(stub.apply_attempts, 0)
        # The immutable version DID land in the vault.
        self.assertTrue((self.vault / envelope["locator"]).is_file(),
                        envelope["locator"])

    def test_unreachable_endpoint_is_distinct_reason(self):
        from knowledge.delivery import hub_capability_status
        dead = _transport(
            f"http://127.0.0.1:{_free_port()}/mcp", "zmem-hub-it-dead")
        status, _detail = hub_capability_status(dead)
        self.assertEqual(status, "unreachable", status)


if __name__ == "__main__":
    unittest.main()
