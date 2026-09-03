"""ZMEM_INJECT=0 — the passive-injection kill switch (issue #110 / P0-5).

Parameterized over every passive surface the issue names:
- the shared hook body (user_prompt / pretool / precompact modes);
- the SessionStart bash hook, driven END TO END through the .sh (an
  apostrophe in the added inline-python lines would kill the whole hook —
  the PR #105 lesson; bash -n is asserted too);
- the Hermes provider (prefetch, _tool_session_start, system_prompt_block)
  via importlib + stubbed agent modules (works without the mcp package);
- the Hermes reflect hook as a subprocess (delivery silenced, correction
  capture still writes);
- MCP session_start (guarded on the optional mcp package — CI installs no
  deps — plus an always-running source-contract pin so the branch is pinned
  everywhere);
- doctor's inject-switch line.

Under the switch each surface emits its EMPTY envelope and logs
status=silent reason=disabled; a seeded store row proves the enabled path
still injects (control cases), the literal-"0"-only convention is pinned,
and a parked pre-tool fence survives the switch and delivers on re-enable.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process. The operator's real store is never touched.

Runs standalone: python tests/test_inject_kill_switch.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SESSION_START = REPO_ROOT / "hooks" / "zmem-session-start.sh"
HERMES_REFLECT = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py"
MCP_SERVER = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"
HERMES_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False

DISABLED_LINE = "status=silent reason=disabled"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    "ZMEM_HERMES_CORRECTIONS", "ZMEM_MCP_URL",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    """Child env with every ambient zmem var stripped and a throwaway
    store (ZMEM_STORE outranks ZMEM_DATA — strip first, then set it)."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _seed(env: dict, ns: str, content: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "store.py"), "add",
         "--namespace", ns, "--type", "lesson", "--content", content,
         "--signal", "test"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


def _hook_lines(tmp: str) -> list:
    path = Path(tmp) / "zmem-bg.log"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if "zmem-hook" in ln]


def _run_body(tmp: str, event: dict, ns: str, mode: str = "user_prompt",
              **extra_env: str) -> "subprocess.CompletedProcess":
    env = _clean_env(tmp, **extra_env)
    return subprocess.run(
        [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
         ns, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=120,
    )


class KillSwitchBodyTest(unittest.TestCase):
    """The shared hook body — every mode short-circuits identically."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-killsw-")
        self.ns = "project:killswitch"
        _seed(_clean_env(self._tmp), self.ns,
              "killswitchcanary: git stash pop conflicts need stash drop "
              "after resolve, verified by test")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _assert_disabled(self, r: "subprocess.CompletedProcess") -> None:
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "{}",
                         "the empty envelope is the crash-fallback shape")
        self.assertNotIn("killswitchcanary", r.stdout)
        self.assertNotIn("additionalContext", r.stdout)
        lines = _hook_lines(self._tmp)
        self.assertEqual(len(lines), 1,
                         f"exactly one decision line (no recall ran): {lines}")
        self.assertIn(DISABLED_LINE, lines[0])
        self.assertIn("ids=[]", lines[0])
        self.assertIn("sid=", lines[0])

    def test_user_prompt_mode_silenced_with_env_sid(self):
        r = _run_body(
            self._tmp,
            {"prompt": "how do I handle git stash pop conflicts here?",
             "session_id": "sess-abc"},
            self.ns, ZMEM_INJECT="0")
        self._assert_disabled(r)
        self.assertIn("sid=sess-abc", _hook_lines(self._tmp)[0],
                      "the env/stdin session id threads into the log line")

    def test_pretool_mode_silenced(self):
        r = _run_body(
            self._tmp,
            {"tool_input": {"command": "git stash pop"},
             "session_id": "sess-pt"},
            self.ns, mode="pretool", ZMEM_INJECT="0")
        self._assert_disabled(r)

    def test_precompact_mode_silenced(self):
        r = _run_body(
            self._tmp, {"session_id": "sess-pc"}, self.ns,
            mode="precompact", ZMEM_INJECT="0")
        self._assert_disabled(r)

    def test_control_without_switch_still_injects(self):
        r = _run_body(
            self._tmp,
            {"prompt": "how do I handle git stash pop conflicts here?",
             "session_id": "sess-ctl"},
            self.ns)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("killswitchcanary", r.stdout,
                      "guard: the switch must not silence by default")

    def test_literal_zero_only_convention(self):
        # Only the literal 0 disables (the ZMEM_QUERY_CONTEXT convention):
        # empty, false-y, and NEAR-MISS spellings (0.0 / 00 / False / off)
        # all leave injection ENABLED — pinned so the documented footgun
        # stays a documented fact, not a silent behavior change.
        for value in ("", "false", "no", "1", "0.0", "00", "False", "off"):
            shutil.rmtree(self._tmp, ignore_errors=True)
            os.makedirs(self._tmp, exist_ok=True)
            _seed(_clean_env(self._tmp), self.ns,
                  "killswitchcanary: git stash pop conflicts need stash "
                  "drop after resolve, verified by test")
            r = _run_body(
                self._tmp,
                {"prompt": "how do I handle git stash pop conflicts here?",
                 "session_id": "sess-conv"},
                self.ns, ZMEM_INJECT=value)
            self.assertNotEqual(
                r.stdout.strip(), "{}",
                f"ZMEM_INJECT={value!r} must leave injection enabled")

    def test_parked_fence_survives_and_delivers_on_reenable(self):
        # Arm the pending sidecar on the enabled path (pretool + claude
        # parks the fence), run one disabled user_prompt (fence untouched,
        # nothing delivered), then re-enable and prove the fence delivers.
        sid = "sess-park"
        arm = _run_body(
            self._tmp,
            {"tool_input": {"command": "git stash pop"}, "session_id": sid},
            self.ns, mode="pretool", ZMEM_HOST="claude")
        self.assertEqual(arm.returncode, 0, arm.stderr)
        pending = Path(self._tmp) / "ops" / (sid + ".pending")
        self.assertTrue(pending.is_file(), "control: the fence parked")

        silenced = _run_body(
            self._tmp, {"prompt": "unrelated prompt text", "session_id": sid},
            self.ns, ZMEM_INJECT="0")
        self.assertEqual(silenced.stdout.strip(), "{}")
        self.assertTrue(pending.is_file(),
                        "the kill switch must NOT consume or drop the fence")

        resumed = _run_body(
            self._tmp, {"prompt": "unrelated prompt text", "session_id": sid},
            self.ns)
        self.assertIn("killswitchcanary", resumed.stdout,
                      "re-enabled: the parked fence is delivered")
        self.assertFalse(pending.is_file(), "and then consumed")


class KillSwitchSessionStartTest(unittest.TestCase):
    """The SessionStart bash hook — driven end to end through the .sh."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-killsw-ss-")
        self.ns = "project:killswitch-ss"
        _seed(_clean_env(self._tmp), self.ns,
              "killswitchcanary: git stash pop conflicts need stash drop "
              "after resolve, verified by test")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, **extra_env: str) -> "subprocess.CompletedProcess":
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        env = _clean_env(
            self._tmp,
            ZMEM_ROOT=str(REPO_ROOT),
            ZMEM_HOST="zcode",
            ZMEM_CTX_BUDGET="25000",
            ZMEM_NAMESPACE=self.ns,
            ZMEM_SESSION="sess-ss",
            **extra_env)
        return subprocess.run(
            [bash, str(SESSION_START)],
            input=json.dumps({"session_id": "sess-ss"}),
            capture_output=True, text=True, env=env, timeout=180,
            cwd=self._tmp,
        )

    def test_bash_n_clean(self):
        # The PR #105 lesson: an apostrophe in the inline python breaks the
        # whole hook. bash -n is the cheap canary; the drive below is the
        # real one.
        bash = shutil.which("bash") or self.skipTest("no bash on PATH")
        r = subprocess.run([bash, "-n", str(SESSION_START)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_disabled_emits_empty_envelope_and_logs(self):
        r = self._run(ZMEM_INJECT="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", r.stdout,
                      "the sentinel-wrapped EMPTY envelope")
        self.assertNotIn("additionalContext", r.stdout)
        self.assertNotIn("killswitchcanary", r.stdout)
        self.assertNotIn("Loaded from memory", r.stdout,
                         "Tier 0 is suppressed too")
        lines = _hook_lines(self._tmp)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(DISABLED_LINE, lines[0])
        self.assertIn("sid=sess-ss", lines[0])

    def test_disabled_whitespace_variants_still_disable(self):
        # The inline-python parser is ".strip() == '0'" — whitespace-tolerated
        # on both sides; pin the session-start twin of the recall-body
        # literal-0 test so a future parser drift on THIS surface is caught.
        for value in (" 0", "0 "):
            lines_before = len(_hook_lines(self._tmp))
            r = self._run(ZMEM_INJECT=value)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", r.stdout)
            self.assertNotIn("additionalContext", r.stdout)
            lines = _hook_lines(self._tmp)
            self.assertEqual(len(lines), lines_before + 1, lines)
            self.assertIn(DISABLED_LINE, lines[-1])

    def test_control_injects_tier0_and_recall(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("additionalContext", r.stdout)
        self.assertIn("Loaded from memory", r.stdout,
                      "Tier 0 core.md injects when enabled")


class KillSwitchHermesReflectTest(unittest.TestCase):
    """The Hermes reflect hook: delivery silenced, capture still writes."""

    def _run(self, tmp: str, payload: dict, **extra_env: str):
        env = _clean_env(
            tmp, ZMEM_HOME=str(REPO_ROOT), ZMEM_HERMES_CORRECTIONS="1",
            **extra_env)
        return subprocess.run(
            [sys.executable, str(HERMES_REFLECT)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=env, timeout=120, cwd=tmp,
        )

    def test_capture_still_writes_and_delivery_silenced(self):
        tmp = tempfile.mkdtemp(prefix="zmem-killsw-hr-")
        try:
            correction = ("No, use bun not npm for this project installs "
                          "from now on")
            r = self._run(
                tmp, {"session_id": "sess-hr", "user_message": correction},
                ZMEM_INJECT="0")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "{}",
                             "delivery is silenced (empty envelope)")
            self.assertIn(DISABLED_LINE, r.stderr,
                          "the stderr marker line carries the reason")
            # Derive the queue path from the encoder itself (single source of
            # truth for the ':' -> '_c' scheme), not a hardcoded filename.
            sys.path.insert(0, str(SCRIPTS))
            try:
                import correction_queue
            finally:
                sys.path.pop(0)
            queue = correction_queue.queue_path_for(
                "user:global", Path(tmp) / "queue")
            self.assertTrue(queue.is_file(),
                            "capture ran BEFORE the switch: the sidecar "
                            f"queue must still be written ({queue})")
            items = json.loads(queue.read_text(encoding="utf-8"))
            self.assertEqual(len(items), 1)
            self.assertIn("bun", items[0]["message"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_remote_mode_disabled_skips_lan_call(self):
        # PRR-005: the disabled gate must precede the _remote_enabled()
        # branch. ZMEM_MCP_URL points at a closed port: if the gate ever
        # moved below the remote branch, the remote path would dial it, fail,
        # and add its own "remote prefetch unavailable" stderr line. The
        # pin: the disabled marker is the ONLY stderr output — a reordered
        # gate cannot satisfy it.
        tmp = tempfile.mkdtemp(prefix="zmem-killsw-hr-remote-")
        try:
            r = self._run(
                tmp, {"session_id": "sess-remote",
                      "user_message": "harmless prompt text"},
                ZMEM_INJECT="0", ZMEM_MCP_URL="http://127.0.0.1:1/mcp")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "{}")
            stderr_lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
            self.assertEqual(
                len(stderr_lines), 1, r.stderr)
            self.assertIn(DISABLED_LINE, stderr_lines[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class KillSwitchHermesProviderTest(unittest.TestCase):
    """The Hermes provider surfaces, exercised without the mcp package
    (importlib + stubbed agent modules — the sanctioned twin pattern)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-killsw-prov-")
        # Pin the provider to an ISOLATED seeded store: the enabled-path
        # control below runs a real recall subprocess, and an unpinned
        # store resolution could reach the host-default store.
        _seed(_clean_env(cls._tmp), "user:global",
              "killswitchcanary: git stash pop conflicts need stash drop "
              "after resolve, verified by test")
        agent = types.ModuleType("agent")
        mp = types.ModuleType("agent.memory_provider")
        class MemoryProvider:  # noqa: D401 - minimal ABC stub
            pass
        mp.MemoryProvider = MemoryProvider
        agent.memory_provider = mp
        sys.modules.setdefault("agent", agent)
        sys.modules.setdefault("agent.memory_provider", mp)
        cls._saved = {k: os.environ.get(k) for k in (
            "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_INJECT",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_MCP_TOKEN",
        )}
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(cls._tmp, "no-models")
        os.environ.pop("ZMEM_DATA", None)
        os.environ.pop("ZMEM_INJECT", None)
        try:
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_killsw", HERMES_INIT)
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        finally:
            pass  # env restored in tearDownClass (provider holds no import)
        cls.provider = cls.mod.ZmemMemoryProvider()
        cls.provider.initialize("sess-killsw")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_prefetch_returns_empty_string(self):
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0"}):
            self.assertEqual(self.provider.prefetch("anything"), "")

    def test_prefetch_enabled_control_finds_the_seeded_row(self):
        # No switch → the normal recall path runs against the isolated
        # seeded store and RETURNS the row (contrast to the disabled twin,
        # which returns "" before any subprocess). Asserting the content
        # (not just non-empty) proves the switch is not silently always-on
        # and the recall actually surfaced the seeded row.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_INJECT", None)
            out = self.provider.prefetch("git stash pop conflicts")
            self.assertIn("killswitchcanary", out,
                          "enabled prefetch must recall the seeded row")

    def test_prefetch_whitespace_zero_still_disables(self):
        # Near-miss drift pin (X6/PRR-004 follow-up): the provider's own
        # predicate is whitespace-tolerated literal-"0" — pin it here so a
        # divergence from the other five sites is caught behaviorally.
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": " 0"}):
            self.assertEqual(self.provider.prefetch("anything"), "")

    def test_session_start_tool_returns_disabled_envelope(self):
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0"}):
            out = self.provider.handle_tool_call(
                "zmem_session_start", {})
        env = json.loads(out)
        self.assertEqual(env["result"], "session_started")
        self.assertEqual(env["reason"], "disabled")
        self.assertEqual(env["context"], "")
        self.assertEqual(env["ids"], [])
        self.assertEqual(
            sorted(env.keys()),
            sorted(["result", "namespace", "ids", "omitted",
                    "budget_dropped", "reason", "context",
                    "tokens_used", "tokens_budget"]),
            "the disabled envelope keeps the exact 9-key shape")

    def test_system_prompt_block_returns_empty(self):
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0"}):
            self.assertEqual(self.provider.system_prompt_block(), "")


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed (CI: no deps)")
class KillSwitchMcpSessionStartTest(unittest.TestCase):
    """MCP session_start through the real tool manager (runs wherever the
    optional mcp package exists; the source pin below covers the rest)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-killsw-mcp-")
        cls._saved = {k: os.environ.get(k) for k in (
            "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_INJECT",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_MCP_TOKEN",
        )}
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(cls._tmp, "no-models")
        os.environ["ZMEM_MCP_TOKEN"] = "kill-switch-test-token"
        os.environ.pop("ZMEM_DATA", None)
        os.environ.pop("ZMEM_INJECT", None)
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_killsw", MCP_SERVER)
        cls.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_killsw"] = cls.mcp_server
        spec.loader.exec_module(cls.mcp_server)
        cls.server = cls.mcp_server.build_server(host="127.0.0.1", port=0,
                                                 use_tls=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_session_start_disabled_envelope(self):
        import asyncio
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0"}):
            result = asyncio.run(self.server._tool_manager.call_tool(
                "session_start", {"namespace": "user:global"},
                context=None))
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(result["context"], "")
        self.assertEqual(result["ids"], [])
        self.assertEqual(result["result"], "session_started")
        self.assertEqual(
            sorted(result.keys()),
            sorted(["result", "namespace", "ids", "omitted",
                    "budget_dropped", "reason", "context",
                    "tokens_used", "tokens_budget"]),
            "the disabled envelope keeps the exact 9-key shape")


class KillSwitchSourceContractTest(unittest.TestCase):
    """Always-running pins (no optional deps): the switch branches exist on
    the surfaces that cannot be driven everywhere (mcp_server.py in CI),
    and schema_meta carries the constant. Presence needles only."""

    def test_mcp_server_has_disabled_branch(self):
        text = MCP_SERVER.read_text(encoding="utf-8")
        for needle in ('if _inject_disabled():',
                       '"reason": _INJECT_REASON_DISABLED,',
                       '_INJECT_REASON_DISABLED = "disabled"',
                       'INJECT_REASON_DISABLED'):
            self.assertIn(needle, text, f"mcp_server.py lost {needle!r}")

    def test_schema_meta_has_disabled_reason_constant(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import schema_meta  # noqa: PLC0415
        finally:
            sys.path.pop(0)
        self.assertEqual(schema_meta.INJECT_REASON_DISABLED, "disabled")
        self.assertNotIn(
            "disabled", schema_meta.INJECT_SILENT_REASONS,
            "the classifier closed set must stay untouched (issue #110 "
            "kept INJECT_REASON_DISABLED separate deliberately)")


class KillSwitchDoctorTest(unittest.TestCase):
    """doctor reports the switch state (pass/warn)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "zmem_doctor_killsw", SCRIPTS / "doctor.py")
        cls.doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.doctor)

    def test_pass_when_enabled(self):
        env = {k: v for k, v in os.environ.items() if k != "ZMEM_INJECT"}
        with mock.patch.dict(os.environ, env, clear=True):
            check = self.doctor._check_inject_switch()
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["id"], "inject-switch")

    def test_warn_when_disabled(self):
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0"}, clear=True):
            check = self.doctor._check_inject_switch()
        self.assertEqual(check["status"], "warn")
        self.assertIn("DISABLED", check["summary"])
        self.assertIn("capture", check["summary"],
                      "the warn line says capture still writes")

    def test_warn_when_whitespace_zero(self):
        # Near-miss drift pin: doctor parses with the same literal-"0"
        # whitespace-tolerated rule as every kill-switch caller.
        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "0 "}, clear=True):
            check = self.doctor._check_inject_switch()
        self.assertEqual(check["status"], "warn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
