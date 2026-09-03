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
         "ZMEM_HOST", "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT",
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
    """Issue #71 D: parity capture on the pre_llm_call path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-hermes-corr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _clean_env(self.tmp, ZMEM_HOME=str(REPO_ROOT))

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
        with mock.patch.dict(os.environ, {"ZMEM_NAMESPACE": "user:z"},
                             clear=False):
            os.environ.pop("ZMEM_MCP_NAMESPACE", None)
            self.assertEqual(self.hook._resolve_hook_namespace(), "user:z")
        env_backup = {k: os.environ.get(k) for k in
                      ("ZMEM_MCP_NAMESPACE", "ZMEM_NAMESPACE")}
        for k in env_backup:
            os.environ.pop(k, None)
        try:
            self.assertEqual(self.hook._resolve_hook_namespace(),
                             "user:global")
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v


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
        sidecar_q = Path(env["ZMEM_DATA"], "queue", "user_cglobal.json")
        self.assertTrue(sidecar_q.is_file(),
                        "capture must use the REMOTE box's local sidecar")
        items = json.loads(sidecar_q.read_text(encoding="utf-8"))
        self.assertEqual(items[0]["host"], "hermes")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
