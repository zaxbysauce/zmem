"""sid= on every bg-log decision line (issue #94, task 1).

Proves the session-key contract end to end on both writers:
- the shared hook body (user_prompt / pretool modes) appends
  ``sid=<sanitized session id>`` at line end on injected AND silent lines;
- the session-start hook (bash) threads ZMEM_SESSION/CLAUDE_SESSION_ID/
  ZCODE_SESSION_ID into its inline python and appends the same field;
- a hostile session id cannot forge log structure (charset, one field,
  one line);
- a missing session id logs ``sid=unknown``;
- the pre-existing ``tokens=\\d+/\\d+`` pin still passes on sid-carrying
  lines (the field is additive, appended at line end);
- both docstrings (module + _log_inject_decision) document the field.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process. The operator's real store is never touched.

Runs standalone: python tests/test_bg_log_sid.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _remove_log(tmp: str) -> None:
    path = Path(tmp) / "zmem-bg.log"
    try:
        path.unlink()
    except OSError:
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SESSION_START = REPO_ROOT / "hooks" / "zmem-session-start.sh"

HOSTILE_SID = 'sess-<>hostile&id "x"'
# canonical ops-lane rule, applied to HOSTILE_SID:
#   sess-<>hostile&id "x"  ->  sess-__hostile_id__x_
SID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
)


def _sanitize(sid: str) -> str:
    return SID_SAFE_RE.sub("_", sid)[:128] or "unknown"


def _clean_env(tmp: str, **extra: str) -> dict:
    """Child env with every ambient zmem var stripped and a throwaway store
    (ZMEM_STORE outranks ZMEM_DATA — strip first, then set the sandbox)."""
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


def _run_body(tmp: str, mode: str, event: dict, ns: str) -> str:
    env = _clean_env(tmp)
    r = subprocess.run(
        [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
         ns, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stdout
    return r.stdout


class BgLogSidBodyTest(unittest.TestCase):
    """Writer A — the shared hook body (all modes route through it)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-sid94-")
        self.ns = "project:sid94"
        _seed(_clean_env(self._tmp), self.ns,
              "sidcanary: git stash pop conflicts need stash drop after "
              "resolve, verified by test")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_injected_line_carries_sanitized_sid(self):
        out = _run_body(
            self._tmp, "user_prompt",
            {"prompt": "how do I handle git stash pop conflicts here?",
             "session_id": HOSTILE_SID},
            self.ns)
        self.assertIn("sidcanary", out)  # the row actually injected
        lines = _hook_lines(self._tmp)
        self.assertTrue(lines, "bg log decision line missing")
        line = lines[-1]
        self.assertIn("status=injected", line)
        self.assertIn(f" sid={_sanitize(HOSTILE_SID)}", line)
        self.assertRegex(line, r" sid=\S+$")
        # exactly one sid field, one physical line, hostile charset gone
        self.assertEqual(line.count(" sid="), 1)
        self.assertNotIn("<", line)
        self.assertNotIn(">", line)
        self.assertNotIn("&", line)

    def test_silent_line_without_session_logs_sid_unknown(self):
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "completely unrelated prompt about llamas"},
                  "project:sid94-nothing")
        line = _hook_lines(self._tmp)[-1]
        self.assertIn("status=silent", line)
        self.assertIn("reason=empty-pool", line)
        self.assertIn(" sid=unknown", line)
        self.assertRegex(line, r" sid=\S+$")

    def test_silent_line_with_session_carries_sanitized_sid(self):
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "completely unrelated prompt about llamas",
                   "session_id": HOSTILE_SID},
                  "project:sid94-nothing")
        line = _hook_lines(self._tmp)[-1]
        self.assertIn(f" sid={_sanitize(HOSTILE_SID)}", line)

    def test_tokens_regex_still_passes_alongside_sid(self):
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "how do I handle git stash pop conflicts?",
                   "session_id": "sess-tok"},
                  self.ns)
        line = _hook_lines(self._tmp)[-1]
        self.assertRegex(line, r"tokens=\d+/\d+")
        self.assertRegex(line, r" sid=\S+$")
        # sid is the LAST field: tokens= must not be swallowed by it
        self.assertLess(line.index("tokens="), line.index(" sid="))

    def test_pretool_line_carries_sid(self):
        _run_body(self._tmp, "pretool",
                  {"session_id": "sess-pretool",
                   "tool_input": {"command": "git stash pop"}},
                  self.ns)
        line = _hook_lines(self._tmp)[-1]
        self.assertIn(" sid=sess-pretool", line)
        self.assertRegex(line, r" sid=\S+$")

    def test_env_fallback_when_stdin_omits_session(self):
        # Bot round (cubic #3): a host that omits session_id from the event
        # JSON but launches through the adapter still carries it in the env
        # — writer A must fall back to the SAME env chain writer B uses so
        # both decision-line writers attribute to the same session.
        env = _clean_env(self._tmp, ZMEM_SESSION="sess-env-chain")
        r = subprocess.run(
            [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
             self.ns, "25000", "user_prompt"],
            input=json.dumps({"prompt": "how do I handle git stash pop?"}),
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        line = _hook_lines(self._tmp)[-1]
        self.assertIn(" sid=sess-env-chain", line)
        # stdin session_id still wins over the env when both exist.
        _remove_log(self._tmp)
        r = subprocess.run(
            [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
             self.ns, "25000", "user_prompt"],
            input=json.dumps({"prompt": "how do I handle git stash pop?",
                              "session_id": "sess-stdin-wins"}),
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        line = _hook_lines(self._tmp)[-1]
        self.assertIn(" sid=sess-stdin-wins", line)

    def test_both_docstrings_document_sid(self):
        # Swarm-review PRR-011: prove the needles live in EACH docstring
        # (module + _log_inject_decision) via the AST — a hit in a comment
        # or the log-format code must not satisfy this test.
        import ast
        tree = ast.parse(BODY.read_text(encoding="utf-8"))
        mod_doc = ast.get_docstring(tree) or ""
        fn_doc = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_log_inject_decision"):
                fn_doc = ast.get_docstring(node) or ""
                break
        self.assertTrue(mod_doc, "module docstring missing")
        self.assertTrue(fn_doc, "_log_inject_decision docstring missing")
        for needle in ("sid=<sanitized session id>", "sid=unknown"):
            self.assertIn(needle, mod_doc,
                          "the module docstring must document the field; "
                          "keep the needle on ONE physical line")
            self.assertIn(needle, fn_doc,
                          "the _log_inject_decision docstring must document "
                          "the field; keep the needle on ONE physical line")


class BgLogSidSessionStartTest(unittest.TestCase):
    """Writer B — the session-start hook's inline Tier-2 decision line."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-sid94-ss-")
        self.ns = "project:sid94ss"
        self.ns_empty = "project:sid94ss-empty"
        self._drove_hook = False
        _seed(_clean_env(self._tmp), self.ns,
              "sscanary: recent high confidence row for session start")

    def tearDown(self):
        # Swarm-review PRR-010: the hook spawns a detached cadence worker
        # (~15s delayed) that writes into this temp dir. Wait for its
        # completion line (mirrors test_ops_tokens' poll) so the worker
        # never races the rmtree on slow/Windows runners. Bounded: after
        # the deadline the (ignore_errors) rmtree proceeds regardless.
        log = Path(self._tmp) / "zmem-bg.log"
        if self._drove_hook and log.is_file():
            import time as _t
            deadline = _t.time() + 45
            while _t.time() < deadline:
                try:
                    if "session-cadence:" in log.read_text(
                            encoding="utf-8"):
                        break
                except OSError:
                    # A transient Windows sharing violation mid-append is
                    # the live-race case — keep polling, never abort early.
                    pass
                _t.sleep(2)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_session_start(self, extra_env: dict,
                           ns: str | None = None) -> "subprocess.CompletedProcess":
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        env = _clean_env(
            self._tmp,
            ZMEM_ROOT=str(REPO_ROOT),
            ZMEM_HOST="zcode",
            ZMEM_CTX_BUDGET="25000",
            ZMEM_NAMESPACE=ns or self.ns,
            **extra_env)
        r = subprocess.run(
            [bash, str(SESSION_START)],
            input=json.dumps({"session_id": "sess-ss"}),
            capture_output=True, text=True, env=env, timeout=180,
            cwd=self._tmp)
        self._drove_hook = True
        return r

    def _ss_line(self) -> str:
        """The session-start decision line: the one WITHOUT reason=."""
        lines = [ln for ln in _hook_lines(self._tmp) if "reason=" not in ln]
        self.assertTrue(lines,
                        "session-start decision line missing (check the "
                        "inline python ran; stderr above)")
        return lines[-1]

    def test_session_start_line_carries_sanitized_sid(self):
        r = self._run_session_start({"ZMEM_SESSION": HOSTILE_SID})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        line = self._ss_line()
        self.assertIn(f" sid={_sanitize(HOSTILE_SID)}", line)
        self.assertRegex(line, r" sid=\S+$")
        self.assertEqual(line.count(" sid="), 1)
        self.assertNotIn("<", line)

    def test_session_start_without_session_logs_sid_unknown(self):
        r = self._run_session_start({})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        line = self._ss_line()
        self.assertIn(" sid=unknown", line)

    def test_session_start_legacy_env_fallback_chain(self):
        # CLAUDE_SESSION_ID is the documented legacy fallback when the
        # launcher (ZMEM_SESSION) is absent.
        r = self._run_session_start({"CLAUDE_SESSION_ID": "sess-legacy-cc"})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        line = self._ss_line()
        self.assertIn(" sid=sess-legacy-cc", line)

    def test_session_start_silent_line_carries_real_sid(self):
        # Swarm-review PRR-012: the SILENT decision line with a REAL
        # session id must carry the sanitized sid — silent+sid=unknown was
        # already covered; this pins the silent+sid half of the writer-B
        # contract. (A decision line is only written when the recent pull
        # returns rows, so silence is produced the way the hook produces
        # it: rows exist but the token budget wipes them.)
        _seed(_clean_env(self._tmp), self.ns_empty,
              "quietcanary: budget-wipe row for the silent-line test")
        r = self._run_session_start({"ZMEM_SESSION": "sess-quiet",
                                     "ZMEM_INJECT_TOKEN_BUDGET": "10"},
                                    ns=self.ns_empty)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        line = self._ss_line()
        self.assertIn("status=silent", line)
        self.assertIn(" sid=sess-quiet", line)
        self.assertRegex(line, r" sid=\S+$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
