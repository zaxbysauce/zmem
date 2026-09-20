"""Issue #71 A + D: Hermes pre_llm_call — correction-capture parity and the
remote MCP prefetch.

D (correction capture): the reflect hook classifies the current user turn
with the SAME corrections.detect_patterns rules the Claude/ZCode/Codex
capture hook uses and appends to the SAME schema-versioned sidecar queue
(host="hermes"); closeout stays the store write authority. Pinned: the
payload shapes (top-level user_message; the upstream-#83281 extra-nested
shape; conversation_history fallback), the <5-char bail, zmem's own injected
context filtered out, per-session dedup, the ZMEM_HERMES_CORRECTIONS=0 kill
switch, and local-mode silence when no user message is present.

A (remote prefetch): with ZMEM_MCP_URL set and NO local store, the hook
fetches the passive session_start prefetch over MCP via the mcp_client.py
subprocess. Pinned against a REAL spawned mcp_server.py on an ephemeral
port: fenced context delivered, retrieval_count NOT bumped, bad token and
refused connection fail open ({}), and correction capture still works in
remote mode. Skip-guarded on the mcp package (CI runs stdlib-only).

All stores are throwaway temp stores (ZMEM_STORE/ZMEM_DATA pinned per
subprocess). Runs standalone:
python tests/test_hermes_correction_remote.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
REFLECT = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py"
MCP_SERVER = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

STRIP = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
         "ZMEM_HOST", "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_CAPTURE",
         "ZMEM_HERMES_CORRECTIONS",
         "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
         "ZMEM_MCP_NAMESPACE", "ZMEM_MCP_TIMEOUT",
         "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
         "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _run_reflect(env: dict, payload: dict) -> tuple[str, int]:
    r = subprocess.run([sys.executable, str(REFLECT)],
                       input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=120)
    return r.stdout.strip(), r.returncode


def _queue_items(tmp: str, ns: str = "user:global") -> list[dict]:
    # correction_queue encodes namespaces: ':' -> '_c' (queue_path_for).
    q = Path(tmp, "queue", ns.replace(":", "_c") + ".json")
    if not q.is_file():
        return []
    return json.loads(q.read_text(encoding="utf-8"))


CORRECTION = "No, use bun not npm for this project's installs from now on"


class HermesCorrectionCaptureTest(unittest.TestCase):
    """Issue #71 D: parity capture on the pre_llm_call path (since issue
    #122 the queue write itself happens inside the store bridge)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-hermes-corr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Pin the namespace: issue #122 derives it from the project dir via
        # host.resolve_namespace when unpinned, so an unpinned run on a git
        # checkout would queue under the project namespace, not user:global.
        self.env = _clean_env(self.tmp, ZMEM_HOME=str(REPO_ROOT),
                              ZMEM_NAMESPACE="user:global")

    def test_user_message_captured_with_host_hermes(self):
        out, rc = _run_reflect(self.env, {"session_id": "s1",
                                          "user_message": CORRECTION})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}", "capture is silent (context budget)")
        items = _queue_items(self.tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["host"], "hermes")
        self.assertEqual(items[0]["namespace"], "user:global")
        self.assertEqual(items[0]["schema_version"], 1)
        self.assertIn("bun", items[0]["message"])

    def test_extra_nested_payload_captured(self):
        # Upstream hermes-agent #83281: the shell-hook serializer nests
        # user_message under "extra".
        out, rc = _run_reflect(self.env, {
            "session_id": "s2",
            "extra": {"user_message": CORRECTION}})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")
        self.assertEqual(len(_queue_items(self.tmp)), 1)

    def test_conversation_history_fallback_captured(self):
        out, rc = _run_reflect(self.env, {
            "session_id": "s3",
            "conversation_history": [
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": CORRECTION}]})
        self.assertEqual(rc, 0)
        self.assertEqual(len(_queue_items(self.tmp)), 1)

    def test_short_message_bails(self):
        out, rc = _run_reflect(self.env, {"session_id": "s4",
                                          "user_message": "ok fine"})
        self.assertEqual(rc, 0)
        self.assertEqual(_queue_items(self.tmp), [])

    def test_zmem_injected_context_never_captured(self):
        out, rc = _run_reflect(self.env, {
            "session_id": "s5",
            "user_message": "<<<ZMEM_UNTRUSTED_FENCE>>> injected memory text "
                            "with correction-like words: never deploy on friday"})
        self.assertEqual(rc, 0)
        self.assertEqual(_queue_items(self.tmp), [],
                         "zmem's own injection must not become a candidate")

    def test_dedup_same_message_appends_once(self):
        payload = {"session_id": "s6", "user_message": CORRECTION}
        _run_reflect(self.env, payload)
        _run_reflect(self.env, payload)
        self.assertEqual(len(_queue_items(self.tmp)), 1)
        # A DIFFERENT correction appends again.
        _run_reflect(self.env, {"session_id": "s6",
                                "user_message":
                                "remember: the fleet store is canonical"})
        self.assertEqual(len(_queue_items(self.tmp)), 2)

    def test_kill_switch(self):
        env = _clean_env(self.tmp, ZMEM_HOME=str(REPO_ROOT),
                         ZMEM_HERMES_CORRECTIONS="0")
        out, rc = _run_reflect(env, {"session_id": "s7",
                                     "user_message": CORRECTION})
        self.assertEqual(rc, 0)
        self.assertEqual(_queue_items(self.tmp), [])

    def test_local_mode_no_user_message_stays_silent(self):
        out, rc = _run_reflect(self.env, {"session_id": "s8"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")
        self.assertEqual(_queue_items(self.tmp), [])


class HookHelperUnitTest(unittest.TestCase):
    """PRR-019 + PRR-016: the hook's timeout clamp and namespace chain, as
    pure functions (import via importlib — the hook file is stdlib-only and
    import-safe outside Hermes)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_hermes_reflect_hook", REFLECT)
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    def test_clamp_timeout_defaults_and_garbage(self):
        self.assertEqual(self.hook._clamp_timeout(""), 8.0)
        self.assertEqual(self.hook._clamp_timeout("   "), 8.0)
        self.assertEqual(self.hook._clamp_timeout("not-a-number"), 8.0)

    def test_clamp_timeout_bounds(self):
        self.assertEqual(self.hook._clamp_timeout("0.2"), 1.0,
                         "below-floor values clamp UP to 1s")
        self.assertEqual(self.hook._clamp_timeout("999"), 30.0,
                         "above-ceiling values clamp DOWN to 30s")
        self.assertEqual(self.hook._clamp_timeout("12"), 12.0)

    def test_namespace_chain_mcp_namespace_wins(self):
        with mock.patch.dict(os.environ, {"ZMEM_MCP_NAMESPACE": "project:cfg",
                                          "ZMEM_NAMESPACE": "user:z"},
                             clear=False):
            self.assertEqual(self.hook._resolve_hook_namespace(),
                             "project:cfg")

    def test_namespace_chain_fallbacks(self):
        # Issue #122: the chain is MCP_NAMESPACE → NAMESPACE → ZMEM_PROJECT →
        # ZCODE_PROJECT_DIR → CLAUDE_PROJECT_DIR → cwd, with the project-dir
        # sources resolved through host.resolve_namespace. With every
        # project source pointing at a NONEXISTENT dir, git resolution fails
        # and the documented fallback is exactly user:global.
        with mock.patch.dict(os.environ, {"ZMEM_NAMESPACE": "user:z"},
                             clear=False):
            os.environ.pop("ZMEM_MCP_NAMESPACE", None)
            for k in ("ZMEM_PROJECT", "ZCODE_PROJECT_DIR",
                      "CLAUDE_PROJECT_DIR"):
                os.environ.pop(k, None)
            self.assertEqual(self.hook._resolve_hook_namespace(), "user:z")
        env_backup = {k: os.environ.get(k) for k in
                      ("ZMEM_MCP_NAMESPACE", "ZMEM_NAMESPACE", "ZMEM_PROJECT",
                       "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR")}
        for k in env_backup:
            os.environ.pop(k, None)
        try:
            # Resolver FAILURE (host unimportable — e.g. a broken plugin
            # copy): the documented fallback is exactly user:global. A real
            # dir would derive a project:* key (see the test below); git
            # failure paths are not deterministically arrangeable.
            with mock.patch.dict(sys.modules, {"host": None}):
                self.assertEqual(self.hook._resolve_hook_namespace(),
                                 "user:global")
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    def test_namespace_chain_prefers_project_dir_sources(self):
        # Issue #122: ZCODE_PROJECT_DIR feeds host.resolve_namespace — a
        # real git checkout derives its project:* key instead of falling
        # back to user:global. Assert the derivation THROUGH host (the sole
        # producer of project:* keys) using this repo as the fixture.
        repo_root = str(REPO_ROOT)
        with mock.patch.dict(os.environ, {
                "ZMEM_MCP_NAMESPACE": "",
                "ZMEM_NAMESPACE": "",
                "ZCODE_PROJECT_DIR": repo_root,
        }, clear=False):
            os.environ.pop("ZMEM_PROJECT", None)
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            derived = self.hook._resolve_hook_namespace()
        self.assertTrue(derived.startswith("project:"), derived)
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import host as _host  # the sole producer — same derivation
        self.assertEqual(derived,
                         _host.resolve_namespace(repo_root))


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class HermesRemotePrefetchTest(unittest.TestCase):
    """Issue #71 A: passive prefetch over the real MCP server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-hermes-remote-")
        # PRR-022: register cleanup BEFORE anything can fail — unittest skips
        # tearDownClass when setUpClass raises, which would leak the spawned
        # server process and the temp dir.
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(cls._kill_server)
        cls.env = _clean_env(cls.tmp, ZMEM_HOME=str(REPO_ROOT))
        # Seed one canonical row so prefetch has something to surface.
        subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "add", "--namespace",
             "user:global", "--type", "lesson", "--content",
             "remotecanary: the fleet store is the canonical shared brain",
             "--signal", "test", "--confidence", "0.9"],
            capture_output=True, text=True, env=cls.env, check=True,
            timeout=120)
        # Ephemeral port + spawn the real server. PRR-022: hold the bound
        # socket OPEN until immediately before Popen — closing it earlier
        # leaves a window where another process can steal the port.
        holder = socket.socket()
        try:
            holder.bind(("127.0.0.1", 0))
            cls.port = holder.getsockname()[1]
            cls.server_env = {**cls.env, "ZMEM_MCP_TOKEN": "remote-test-token"}
            cls.server = subprocess.Popen(
                [sys.executable, str(MCP_SERVER), "--host", "127.0.0.1",
                 "--port", str(cls.port)],
                env=cls.server_env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        finally:
            holder.close()
        cls._wait_health()

    @classmethod
    def _kill_server(cls):
        if getattr(cls, "server", None) is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()

    @classmethod
    def _wait_health(cls, timeout=30.0):
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{cls.port}/health"
        last_exc: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:  # noqa: BLE001 — poll loop
                last_exc = exc
            time.sleep(0.3)
        raise RuntimeError(f"mcp_server /health never came up: {last_exc}")

    @classmethod
    def tearDownClass(cls):
        # PRR-022: process/tmp cleanup lives in addClassCleanup hooks
        # (registered before any failure point) — tearDownClass is skipped
        # entirely when setUpClass raises. Nothing to do here.
        pass

    def _remote_env(self, **extra: str) -> dict:
        # Remote Hermes box: NO local store file (ZMEM_STORE points nowhere);
        # the sidecar queue lands under the remote box's own data dir (which
        # exists — like a real home dir — or correction_queue's local-FS guard
        # would refuse).
        env = _clean_env(self.tmp, ZMEM_HOME=str(REPO_ROOT))
        env["ZMEM_MCP_URL"] = f"http://127.0.0.1:{self.port}/mcp"
        env["ZMEM_MCP_TOKEN"] = "remote-test-token"
        env["ZMEM_DATA"] = os.path.join(self.tmp, "remote-sidecar")
        os.makedirs(env["ZMEM_DATA"], exist_ok=True)
        env["ZMEM_STORE"] = os.path.join(self.tmp, "remote-sidecar",
                                         "absent.sqlite")
        env.update(extra)
        return env

    def test_remote_prefetch_delivers_fenced_context(self):
        env = self._remote_env()
        out, rc = _run_reflect(env, {"session_id": "r1"})
        self.assertEqual(rc, 0)
        self.assertIn("remotecanary", out)
        self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", out)

    def test_remote_prefetch_does_not_bump_retrieval_count(self):
        env = self._remote_env()
        _run_reflect(env, {"session_id": "r2"})
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        try:
            n = conn.execute(
                "SELECT retrieval_count FROM memory WHERE content LIKE "
                "'%remotecanary%'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0,
                         "session_start prefetch must never bump the counter")

    def test_remote_bad_token_fails_open(self):
        env = self._remote_env(ZMEM_MCP_TOKEN="wrong-token")
        out, rc = _run_reflect(env, {"session_id": "r3"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")

    def test_remote_connection_refused_fails_open(self):
        env = self._remote_env(ZMEM_MCP_URL="http://127.0.0.1:9/mcp")
        out, rc = _run_reflect(env, {"session_id": "r4"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")

    def test_remote_mode_still_captures_correction(self):
        env = self._remote_env()
        out, rc = _run_reflect(env, {"session_id": "r5",
                                     "user_message": CORRECTION})
        self.assertEqual(rc, 0)
        # Issue #122: the namespace is DERIVED (project:* on a git
        # checkout), so scan the REMOTE box's local queue dir rather than
        # assuming user:global — the pinned intent is that the capture
        # lands in the local SIDECAR, never on the remote store.
        queue_dir = Path(env["ZMEM_DATA"], "queue")
        self.assertTrue(queue_dir.is_dir(),
                        "capture must use the REMOTE box's local sidecar")
        hermed = []
        for q in sorted(queue_dir.glob("*.json")):
            items = json.loads(q.read_text(encoding="utf-8"))
            hermed.extend(i for i in items if i.get("host") == "hermes")
        self.assertEqual(len(hermed), 1)


    def test_remote_mode_ignores_stale_local_store(self):
        """Final-critic critical #2: remote mode branches on ZMEM_MCP_URL,
        BEFORE the local-store check — a stale/accidental local store file
        must never silently downgrade the box to local delivery."""
        # Stale local store with a row the MCP server does not know.
        stale_dir = os.path.join(self.tmp, "stale-local")
        os.makedirs(stale_dir, exist_ok=True)
        stale_env = _clean_env(stale_dir, ZMEM_HOME=str(REPO_ROOT))
        stale_env["ZMEM_STORE"] = os.path.join(stale_dir, "store.sqlite")
        r = subprocess.run([sys.executable, str(SCRIPTS / "store.py"), "stats"],
                           capture_output=True, text=True, env=stale_env,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "add", "--namespace",
             "user:global", "--type", "fact", "--content",
             "stalelocal row only in the leftover local store",
             "--signal", "test"],
            capture_output=True, text=True, env=stale_env, check=True)
        env = self._remote_env()
        env["ZMEM_STORE"] = os.path.join(stale_dir, "store.sqlite")
        env["ZMEM_DATA"] = stale_dir
        out, rc = _run_reflect(env, {"session_id": "r6"})
        self.assertEqual(rc, 0)
        self.assertIn("remotecanary", out,
                      "prefetch must come from the MCP server")
        self.assertNotIn("stalelocal", out,
                         "a stale local store must not take over delivery")
        # Final-critic: the MCP ENVELOPE must not be injected raw — the
        # client extracts the context field (the hook's own {"context": ...}
        # wrapper is expected; the server's envelope markers are not).
        self.assertNotIn("session_started", out)
        self.assertNotIn('"tokens_used"', out)

    def test_session_id_sanitized_for_sidecar_paths(self):
        """Final-critic: a crafted session id must not escape the ops dir in
        sidecar file names."""
        hostile = "../../evil"
        env = self._remote_env()
        out, rc = _run_reflect(env, {"session_id": hostile,
                                     "user_message": CORRECTION})
        self.assertEqual(rc, 0)
        ops_dir = Path(env["ZMEM_DATA"], "ops")
        names = [p.name for p in ops_dir.iterdir()] if ops_dir.is_dir() else []
        for n in names:
            self.assertNotIn("..", n, f"sidecar name must not traverse: {n}")
        escaped = [str(p) for p in Path(self.tmp).rglob("evil*")]
        self.assertEqual(escaped, [],
                         "no sidecar file may land outside the data dir")

    def test_remote_namespace_env_reaches_the_query(self):
        """Final-critic critical #1: ZMEM_MCP_NAMESPACE (not the session id)
        is the MCP namespace. Seed a row in the configured namespace only —
        the pre-critic bug sent the session id as the namespace, so this row
        could never surface."""
        # Seed a project-namespace row in the SERVER store.
        subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "add", "--namespace",
             "project:cfgns", "--type", "fact", "--content",
             "cfgnscanary row reachable only via the configured namespace",
             "--signal", "test"],
            capture_output=True, text=True, env=self.server_env, check=True,
            timeout=120)
        env = self._remote_env(ZMEM_MCP_NAMESPACE="project:cfgns")
        out, rc = _run_reflect(env, {"session_id": "r7"})
        self.assertEqual(rc, 0)
        self.assertIn("cfgnscanary", out,
                      "ZMEM_MCP_NAMESPACE must drive the prefetch query "
                      "(the session id must never be sent as the namespace)")

    # ------------------------------------------------------------------
    # Issue #122: compatibility-mode parity — fixture-driven, in-process
    # hook runs where ONLY the mcp_client subprocess is faked; the
    # hermes-context store bridge runs FOR REAL against a seeded scratch
    # store, so the hashed sidecars, nudge bytes, and cursor ordering are
    # exercised end to end.
    # ------------------------------------------------------------------

    _SID = "00000000-0000-4000-8000-000000000122"
    _NS = "project:github.com/acme/demo"
    _QUERY = "Please check the stash safety for this turn."
    _OPS_STEM = hashlib.sha256(_SID.encode("utf-8")).hexdigest()[:32]
    _COMPAT = REPO_ROOT / "tests" / "fixtures" / "hermes_compat"

    def _compat_env(self, tmp: str, **extra: str) -> dict:
        return _clean_env(
            tmp,
            ZMEM_HOME=str(REPO_ROOT),
            ZMEM_MCP_URL="http://127.0.0.1:9/mcp",
            ZMEM_MCP_TOKEN="compat-token",
            ZMEM_MCP_NAMESPACE=self._NS,
            **extra)

    def _seed_compat_store(self, tmp: str, *, arm_failure: bool = False,
                           seed_cursor: bool = False) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "init"],
            capture_output=True, text=True, env=_clean_env(tmp), check=True,
            timeout=120)
        ops_dir = Path(tmp) / "ops"
        ops_dir.mkdir(parents=True, exist_ok=True)
        ring = (self._COMPAT / "ops" / f"{self._OPS_STEM}.log").read_bytes()
        (ops_dir / f"{self._OPS_STEM}.log").write_bytes(ring)
        if arm_failure:
            conn = sqlite3.connect(str(Path(tmp) / "store.sqlite"))
            try:
                conn.executescript(
                    (self._COMPAT / "meta-seed.sql").read_text(encoding="utf-8"))
                conn.commit()
            finally:
                conn.close()
        if seed_cursor:
            (ops_dir / f"{self._OPS_STEM}.delivered").write_bytes(
                (self._COMPAT / "cursor-before.txt").read_bytes())

    def _drive_reflect_inprocess(self, tmp: str, payload: dict,
                                 fake_client: "subprocess.CompletedProcess | None",
                                 **env_extra: str):
        """Run the reflect hook in-process; fake ONLY the mcp_client
        subprocess (recorded), let the hermes-context bridge run for real
        against the scratch store pinned into os.environ.
        Returns (stdout, stderr, returncode, client_cmds)."""
        import io as _io
        mod = _load_reflect_module()
        real_run = mod.subprocess.run
        client_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if any("mcp_client.py" in str(part) for part in cmd):
                client_cmds.append([str(p) for p in cmd])
                return fake_client
            return real_run(cmd, **kwargs)

        pinned = self._compat_env(tmp, **env_extra)
        saved = {k: os.environ.get(k) for k in pinned}
        for k, v in pinned.items():
            os.environ[k] = v
        out, err, rc = _io.StringIO(), _io.StringIO(), None
        try:
            with mock.patch.object(mod.subprocess, "run", fake_run), \
                    mock.patch.object(sys, "stdin",
                                      _io.StringIO(json.dumps(payload))), \
                    mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys, "stderr", err):
                rc = mod.main()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return out.getvalue(), err.getvalue(), rc, client_cmds

    def _fail_client(self, stderr: str = "boom"):
        import types as _types
        return _types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    def test_compat_prefetch_forwards_exact_request(self):
        """AC1: the remote request equals tests/fixtures/hermes_compat/
        request.json field for field (namespace, UUID session id, query,
        user_prompt, hermes-compat, git/stash/pop token order)."""
        import types as _types
        tmp = tempfile.mkdtemp(prefix="zmem-compat-c2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self._seed_compat_store(tmp)
        request = json.loads(
            (self._COMPAT / "request.json").read_text(encoding="utf-8"))
        envelope_txt = (self._COMPAT / "remote-response.json").read_text(
            encoding="utf-8")
        fake = _types.SimpleNamespace(returncode=0, stdout=envelope_txt,
                                      stderr="")
        out, err, rc, cmds = self._drive_reflect_inprocess(
            tmp,
            {"session_id": self._SID, "user_message": self._QUERY}, fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(cmds), 1, cmds)
        cmd = cmds[0]
        for flag, value in (
                ("--url", "http://127.0.0.1:9/mcp"),
                ("--query", request["query"]),
                ("--namespace", request["namespace"]),
                ("--session-id", request["session_id"]),
                ("--moment", request["moment"]),
                ("--lane", request["lane"]),
        ):
            self.assertIn(flag, cmd, cmd)
            self.assertEqual(cmd[cmd.index(flag) + 1], value,
                             f"{flag} value mismatch: {cmd}")
        # repeated --ops-token flags preserve the ring order
        ops_values = [cmd[i + 1] for i, part in enumerate(cmd)
                      if part == "--ops-token"]
        self.assertEqual(ops_values, request["ops_tokens"], cmd)
        self.assertIn("call", cmd)
        self.assertEqual(cmd[cmd.index("call") + 1], "prefetch")
        # clean prepare (no armed failure): the emitted context is exactly
        # the selector's rendered fence
        emitted = json.loads(out)
        self.assertEqual(
            emitted.get("context"),
            (self._COMPAT / "expected-rendered.txt").read_text(
                encoding="utf-8"))

    def test_success_merges_failure_and_rendered_context(self):
        """AC2: context bytes equal expected-context.json (failure nudge
        first, two-LF join, then the selector's rendered fence); the real
        bridge commit moves the cursor before->after; no .tmp remains."""
        import types as _types
        tmp = tempfile.mkdtemp(prefix="zmem-compat-c3-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self._seed_compat_store(tmp, arm_failure=True)
        fake = _types.SimpleNamespace(
            returncode=0,
            stdout=(self._COMPAT / "remote-response.json").read_text(
                encoding="utf-8"),
            stderr="")
        out, err, rc, _cmds = self._drive_reflect_inprocess(
            tmp,
            {"session_id": self._SID, "user_message": self._QUERY}, fake)
        self.assertEqual(rc, 0, (out, err))
        emitted = json.loads(out)
        expected = json.loads(
            (self._COMPAT / "expected-context.json").read_text(
                encoding="utf-8"))
        self.assertEqual(emitted["context"], expected["context"])
        rendered = (self._COMPAT / "expected-rendered.txt").read_text(
            encoding="utf-8")
        failure_text = expected["context"][:expected["context"].index(
            "\n\n" + rendered)]
        self.assertLess(emitted["context"].index(failure_text),
                        emitted["context"].index("<<<ZMEM_UNTRUSTED_FENCE>>>"),
                        "failure bytes must precede the rendered fence")
        cursor = Path(tmp, "ops", f"{self._OPS_STEM}.delivered")
        self.assertEqual(cursor.read_bytes(),
                         (self._COMPAT / "cursor-after.txt").read_bytes())
        residue = [p.name for p in Path(tmp, "ops").iterdir()
                   if p.name.endswith(".tmp")]
        self.assertEqual(residue, [])
        # ack-failure really cleared the armed marker
        conn = sqlite3.connect(str(Path(tmp) / "store.sqlite"))
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"hermes_pending_failure_{self._SID}",)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_two_failed_prefetch_attempts_keep_cursor(self):
        """AC3: two deterministic failures — stdout {}, exit 0, cursor
        byte-identical to `1726000000.0 0\\n`, attempts file
        `1726000000.0 2\\n`, and the exact stderr line once."""
        tmp = tempfile.mkdtemp(prefix="zmem-compat-c4-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self._seed_compat_store(tmp, seed_cursor=True)
        out, err, rc, cmds = self._drive_reflect_inprocess(
            tmp,
            {"session_id": self._SID, "user_message": self._QUERY},
            self._fail_client())
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {})
        self.assertEqual(len(cmds), 2, "exactly initial + one retry")
        cursor = Path(tmp, "ops", f"{self._OPS_STEM}.delivered")
        self.assertEqual(cursor.read_bytes(),
                         (self._COMPAT / "cursor-before.txt").read_bytes())
        attempts = Path(tmp, "ops", f"{self._OPS_STEM}.attempts")
        self.assertEqual(
            attempts.read_bytes(),
            (self._COMPAT / "attempts-after-two-failures.txt").read_bytes())
        self.assertEqual(
            err, "zmem-reflect: prefetch failed after 2 attempts; "
                 "cursor unchanged\n")

    def test_malformed_envelope_fails_open(self):
        """AC (client boundary): a response missing the selector-envelope
        keys makes mcp_client exit 1 with the exact stderr line."""
        import io as _io
        client_path = REPO_ROOT / "hermes-plugin" / "server" / "mcp_client.py"
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_client_under_test", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        malformed = json.loads(
            (self._COMPAT / "malformed-response.json").read_text(
                encoding="utf-8"))

        async def fake_call(url, token, tool, arguments):
            return malformed

        argv = ["mcp_client.py", "--url", "http://127.0.0.1:9/mcp",
                "call", "prefetch",
                "--query", self._QUERY,
                "--namespace", self._NS,
                "--session-id", self._SID,
                "--moment", "user_prompt",
                "--lane", "hermes-compat"]
        err = _io.StringIO()
        with mock.patch.dict(os.environ, {"ZMEM_MCP_TOKEN": "compat-token"}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch.object(client, "_call", fake_call):
            rc = client.main()
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue(),
                         "mcp_client: invalid prefetch envelope\n")

    def test_correction_capture_uses_store_subprocess(self):
        """The hook has no direct store access and sends one JSON payload."""
        src = REFLECT.read_text(encoding="utf-8")
        for banned in ("import sqlite3", "sqlite3.", "from storelib",
                       "import storelib", "import correction_queue",
                       "from correction_queue"):
            self.assertNotIn(banned, src, banned)
        tmp = tempfile.mkdtemp(prefix="zmem-compat-c5-")
        self.addCleanup(shutil.rmtree, tmp, True)
        mod = _load_reflect_module()
        recorded: list[list[str]] = []

        def recorder(value):
            recorded.append(dict(value))
            return {}

        payload = {"session_id": self._SID, "user_message": self._QUERY}
        env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT), ZMEM_INJECT="0")
        with mock.patch.dict(os.environ, env, clear=True):
            out = _capture_main(mod, recorder, payload)
        self.assertEqual(json.loads(out), {})
        self.assertEqual(len(recorded), 1, "one capture/prepare bridge call")
        first = recorded[0]
        self.assertEqual(first["user_message"], self._QUERY)
        self.assertEqual(first["session_id"], self._SID)
        self.assertIsInstance(first["namespace"], str)
        self.assertTrue(first["namespace"])


class HermesCaptureSwitchTest(unittest.TestCase):
    """Issue #123: capture policy precedes all Hermes bridge work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-hermes-capture-switch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_capture_switch_precedes_hermes_work(self):
        """A recording failing executable proves disabled capture spawns none."""
        import io as _io

        fake_store = Path(self.tmp, "store.py")
        marker = Path(self.tmp, "invoked")
        fake_store.write_text(
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['ZMEM_INVOCATION_MARKER']).write_text('invoked')\n"
            "raise SystemExit(17)\n",
            encoding="utf-8",
        )
        mod = _load_reflect_module()
        out, err = _io.StringIO(), _io.StringIO()
        env = {
            "ZMEM_CAPTURE": "0",
            "ZMEM_INVOCATION_MARKER": str(marker),
            "ZMEM_HOME": self.tmp,
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(mod, "_scripts_dir", return_value=Path(self.tmp)), \
                mock.patch.object(sys, "stdin", _io.StringIO("not-json")), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            rc = mod.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "{}\n")
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(marker.exists(), "disabled capture must invoke no subprocess")

    def test_inject_switch_keeps_one_capture_call(self):
        """ZMEM_INJECT controls delivery only, not capture opportunity."""
        import io as _io

        mod = _load_reflect_module()
        calls: list[dict] = []
        with mock.patch.dict(os.environ, {"ZMEM_CAPTURE": "1", "ZMEM_INJECT": "0"},
                             clear=False), \
                mock.patch.object(mod, "_run_hermes_reflect",
                                  side_effect=lambda payload: calls.append(payload) or {}), \
                mock.patch.object(sys, "stdin", _io.StringIO(json.dumps({
                    "session_id": "capture-once", "user_message": CORRECTION}))), \
                mock.patch.object(sys, "stdout", _io.StringIO()), \
                mock.patch.object(sys, "stderr", _io.StringIO()):
            self.assertEqual(mod.main(), 0)
        self.assertEqual(len(calls), 1)


def _load_reflect_module():
    spec = importlib.util.spec_from_file_location(
        "zmem_reflect_under_test", REFLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture_main(mod, recorder, payload):
    """Drive mod.main() with a recorded bridge and captured std streams."""
    import io as _io
    out, err = _io.StringIO(), _io.StringIO()
    with mock.patch.object(mod, "_run_hermes_reflect", recorder), \
            mock.patch.object(sys, "stdin", _io.StringIO(json.dumps(payload))), \
            mock.patch.object(sys, "stdout", out), \
            mock.patch.object(sys, "stderr", err):
        mod.main()
    return out.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
