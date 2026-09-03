"""Operation-token (query-context) tests — issue #88 / #85 directions 2+4.

Proves the spec-B contract:
- derive_ops_tokens is LLM-free, bounded, and allowlist-only (no FTS operator
  can survive derivation; a raw command never lands on disk);
- compose_inject_query reserves the ops slice INSIDE the 500-char cap and is
  the BYTE-EXACT identity without ops material (the legacy-neutrality pin);
- the PostToolUse hooks write the ring (coding host + Hermes post_tool_call);
- the UserPromptSubmit hook body and the Hermes prefetch surface compose the
  ring into the query (kill switch ZMEM_QUERY_CONTEXT=0 restores prose-only);
- the eval gold's decision-point items hit WITH ops and MISS prose-only (so
  they measure the lane, not accidental prefix matches), and legacy items
  compose byte-identically;
- sweep collects stale ops rings.

All stores are throwaway temp stores; ambient zmem env is stripped from every
child process. Runs standalone: python tests/test_ops_tokens.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

import ops_tokens  # noqa: E402

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    # Hook dir-resolution chains consult the plugin-data vars (host.py:42-66,
    # convention-capture.sh:166-190); strip them like test_sweep's
    # DATA_DIR_ENV_VARS so a dev box's ambient values can never receive
    # subprocess writes. Tests that need them set them explicitly via extra.
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    # #93 A1: eval-runner pollution vars (test_eval_runner sets both at module
    # import; a single-process multi-file runner must not inherit them) plus
    # the #71 C auto-rekey switch (a stray value must never redirect the
    # remediation against an unintended store).
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
)


def _clean_env(tmp: str, **extra: str) -> dict:
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
         "--signal", "test", "--confidence", "0.9"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


class DeriveTokensTest(unittest.TestCase):
    def test_git_chain_and_test_runner(self):
        self.assertEqual(
            ops_tokens.derive_ops_tokens("git stash pop"),
            ["git", "stash", "pop"])
        self.assertEqual(
            ops_tokens.derive_ops_tokens("bun test pr-workflow-gate.test.ts"),
            ["bun", "test", "pr-workflow-gate.test.ts"])
        self.assertEqual(
            ops_tokens.derive_ops_tokens("git reset --soft origin/main"),
            ["git", "reset", "origin/main"])

    def test_edited_path_becomes_basename(self):
        self.assertEqual(
            ops_tokens.derive_ops_tokens("src/lib/pr-workflow-gate.ts"),
            ["pr-workflow-gate.ts"])

    def test_operators_and_garbage_yield_nothing(self):
        # FTS syntax characters, NEAR/AND operators, parens, quotes: none can
        # survive the allowlist.
        self.assertEqual(ops_tokens.derive_ops_tokens('near/3 ("foo AND bar'), [])
        self.assertEqual(ops_tokens.derive_ops_tokens("NEAR(x) OR (a AND b)"), [])
        self.assertEqual(ops_tokens.derive_ops_tokens("   "), [])
        self.assertEqual(ops_tokens.derive_ops_tokens("plainword"), [])

    def test_secret_shaped_tokens_dropped(self):
        # Review PRR-91-002: credential-prefix tokens never reach the ring.
        self.assertEqual(
            ops_tokens.derive_ops_tokens("git clone ghp_AbCdEf123456"),
            ["git", "clone"])
        self.assertEqual(
            ops_tokens.derive_ops_tokens("git push xoxb-123456"),
            ["git", "push"])
        self.assertEqual(
            ops_tokens.derive_ops_tokens("bun test sk-ant-api03-xxxx"),
            ["bun", "test"])

    def test_single_oversized_token_dropped_not_severed(self):
        # Review PRR-91-014: a token longer than the reserved slice cannot
        # fit whole — it is dropped, never severed mid-word downstream.
        long_tok = "a" * 200 + ".test.ts"
        self.assertEqual(ops_tokens.derive_ops_tokens("bun test " + long_tok),
                         ["bun", "test"])

    def test_caps_and_dedup(self):
        toks = ops_tokens.derive_ops_tokens(*(["git push origin HEAD"] * 30))
        self.assertLessEqual(len(toks), 12)
        self.assertEqual(len(toks), len(set(toks)))


class ComposeQueryTest(unittest.TestCase):
    def test_identity_without_ops_is_byte_exact(self):
        # The legacy-neutrality pin: every gold item without an ops field runs
        # the byte-identical query, so committed eval scores cannot move.
        for prompt in ("", "  abc def  ", "x" * 900, "use the skill on the PR"):
            self.assertEqual(
                ops_tokens.compose_inject_query(prompt, ""),
                (prompt or "").strip()[:500])
            self.assertEqual(
                ops_tokens.compose_inject_query(prompt, "plainword"),
                (prompt or "").strip()[:500])  # derives to nothing → identity

    def test_ops_slice_reserved_inside_cap(self):
        long_prose = "word " * 200  # 1000 chars
        composed = ops_tokens.compose_inject_query(long_prose, "git stash pop")
        self.assertLessEqual(len(composed), 500)
        self.assertTrue(composed.endswith("git stash pop"),
                        "the verbs must survive, not the prose tail")
        # Prose is truncated to make room INSIDE the cap, never extended past it.
        self.assertLess(len(composed), len(long_prose))

    def test_short_prompt_keeps_prose_and_appends_within_cap(self):
        composed = ops_tokens.compose_inject_query(
            "drive the review loop", "git stash pop")
        self.assertTrue(composed.startswith("drive the review loop"))
        self.assertTrue(composed.endswith("git stash pop"))
        self.assertLessEqual(len(composed), 500)

    def test_boundary_prose_349_tail_150_exact(self):
        # Review PRR-91-003: the separator shares the reserved budget, and
        # multi-token tails are cut at a space boundary, never mid-token.
        prose = "x" * 400
        tail = ("git stash pop " * 20).strip()
        composed = ops_tokens.compose_inject_query(prose, tail)
        self.assertLessEqual(len(composed), 500)
        self.assertIn(" ", composed[349:])

    def test_single_token_at_slice_cap_not_severed(self):
        tok = "b" * 147 + ".ts"  # 150 chars, file-shaped so derivation keeps it
        composed = ops_tokens.compose_inject_query("x" * 349, tok)
        self.assertEqual(composed, "x" * 349 + " " + tok)
        self.assertEqual(len(composed), 500)

    def test_kill_switch_env(self):
        # Review PRR-91-010: isolate from ambient env — a host exporting
        # ZMEM_QUERY_CONTEXT=0 must not fail the enabled-by-default assert.
        saved = os.environ.pop("ZMEM_QUERY_CONTEXT", None)
        try:
            self.assertTrue(ops_tokens.query_context_enabled())
        finally:
            if saved is not None:
                os.environ["ZMEM_QUERY_CONTEXT"] = saved
        saved = os.environ.get("ZMEM_QUERY_CONTEXT")
        try:
            os.environ["ZMEM_QUERY_CONTEXT"] = "0"
            self.assertFalse(ops_tokens.query_context_enabled())
        finally:
            if saved is None:
                os.environ.pop("ZMEM_QUERY_CONTEXT", None)
            else:
                os.environ["ZMEM_QUERY_CONTEXT"] = saved


class RingTest(unittest.TestCase):
    def test_append_stores_only_allowlisted_tokens_never_raw(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ring-")
        try:
            ok = ops_tokens.append_ops_ring(
                tmp, "sess-1", "Bash", "git stash pop --quiet 'hunter2 pw'")
            self.assertTrue(ok)
            raw = Path(tmp, "ops", "sess-1.log").read_text(encoding="utf-8")
            # The RAW command never lands on disk: flags, quotes, and the
            # out-of-window argument word are all absent.
            self.assertNotIn("hunter2", raw)
            self.assertNotIn("--quiet", raw)
            self.assertNotIn("'", raw)
            obj = json.loads(raw.strip())
            # Only allowlisted tokens are stored (spec B).
            for tok in obj["ops"].split():
                self.assertRegex(tok, r"^[a-z0-9._/+-]+$")
            # Ring read returns the stored (already-allowlisted) descriptor.
            self.assertEqual(ops_tokens.read_ops_ring(tmp, "sess-1"),
                             [obj["ops"]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_append_bare_command_exact_tokens(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ring-")
        try:
            self.assertTrue(ops_tokens.append_ops_ring(
                tmp, "s2", "Bash", "git stash pop"))
            obj = json.loads(
                Path(tmp, "ops", "s2.log").read_text(encoding="utf-8").strip())
            self.assertEqual(obj["ops"], "git stash pop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_append_skips_events_that_derive_to_nothing(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ring-")
        try:
            self.assertFalse(
                ops_tokens.append_ops_ring(tmp, "s", "Bash", "plainword"))
            self.assertFalse(
                Path(tmp, "ops", "s.log").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_fail_open(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ring-")
        try:
            self.assertEqual(ops_tokens.read_ops_ring(tmp, "no-session"), [])
            self.assertEqual(ops_tokens.read_ops_ring("", "s"), [])
            self.assertEqual(ops_tokens.read_ops_ring(tmp, ""), [])
            ring = Path(tmp, "ops", "torn.log")
            ring.parent.mkdir(parents=True)
            ring.write_text(
                '{"ops": "git push origin HEAD"}\n'
                '{"ops": "git stash pop", "ts": truncated...\n'  # torn line
                '{"ops": "bun test ratchet.test.ts"}\n',
                encoding="utf-8")
            events = ops_tokens.read_ops_ring(tmp, "torn")
            self.assertEqual(events, ["git push origin HEAD",
                                      "bun test ratchet.test.ts"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ring_capped(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ring-")
        try:
            ring = Path(tmp, "ops", "cap.log")
            ring.parent.mkdir(parents=True)
            with open(ring, "w", encoding="utf-8") as f:
                f.write('{"ops": "git push filler0"}\n')
                f.write(('{"ops": "x' + "y" * 200 + '"}\n') * 400)
            ops_tokens.append_ops_ring(tmp, "cap", "Bash", "git stash pop")
            lines = ring.read_text(encoding="utf-8").strip().splitlines()
            self.assertLessEqual(len(lines),
                                 ops_tokens._RING_TRIM_TO_LINES + 1)
            self.assertIn("git stash pop", lines[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ConventionCaptureRingTest(unittest.TestCase):
    """The coding hosts' PostToolUse hook writes the ring (behavioral)."""

    def _run_hook(self, tmp: str, tool: str, tool_input: dict) -> str:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        env = _clean_env(
            tmp, ZMEM_ROOT=str(REPO_ROOT), ZMEM_SESSION="sess-cap",
            ZMEM_CONVENTION_INTERVAL="10")
        event = json.dumps({"tool_name": tool, "tool_input": tool_input,
                            "session_id": "sess-cap"})
        r = subprocess.run(
            [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
            input=event, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_bash_event_appends_allowlisted_ring_line(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-cap-")
        try:
            out = self._run_hook(tmp, "Bash", {"command": "git stash pop"})
            self.assertIn("<<<ZMEM_JSON>>>", out)  # hook contract intact
            ring = Path(tmp, "ops", "sess-cap.log")
            self.assertTrue(ring.is_file(), "ring line must be written")
            body = ring.read_text(encoding="utf-8")
            self.assertNotIn("hush", body)
            self.assertNotIn("--quiet", body)
            obj = json.loads(body.strip())
            self.assertEqual(obj["ops"], "git stash pop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_stdin_fails_open_no_ring_write(self):
        # Review round 1: a corrupt payload must degrade to the silent
        # envelope with NO ring write. ("Ring on every convention-tool
        # event" holds whenever PYTHON_BIN resolves — the happy-path tests
        # above prove that path; this pins the corrupt-input fail-open.)
        tmp = tempfile.mkdtemp(prefix="zmem-ops-capbad-")
        try:
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            env = _clean_env(
                tmp, ZMEM_ROOT=str(REPO_ROOT), ZMEM_SESSION="sess-cap",
                ZMEM_CONVENTION_INTERVAL="10")
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
                input="not json at all {{{",
                capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("<<<ZMEM_JSON>>>", r.stdout)
            self.assertFalse(Path(tmp, "ops", "sess-cap.log").exists(),
                             "no ring line may be written for a corrupt event")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kill_switch_stops_convention_capture_collection(self):
        # Review PRR-91-004 follow-up: the kill switch gates the WRITER —
        # a valid Bash event with ZMEM_QUERY_CONTEXT=0 writes no ring line.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-capks-")
        try:
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            env = _clean_env(
                tmp, ZMEM_ROOT=str(REPO_ROOT), ZMEM_SESSION="sess-cap",
                ZMEM_CONVENTION_INTERVAL="10", ZMEM_QUERY_CONTEXT="0")
            event = json.dumps({"tool_name": "Bash",
                                "tool_input": {"command": "git stash pop"},
                                "session_id": "sess-cap"})
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
                input=event, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("<<<ZMEM_JSON>>>", r.stdout)
            self.assertFalse(Path(tmp, "ops", "sess-cap.log").exists(),
                             "kill switch must stop ring collection")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unmatched_tool_name_writes_no_ring_line(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-capskip-")
        try:
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            env = _clean_env(
                tmp, ZMEM_ROOT=str(REPO_ROOT), ZMEM_SESSION="sess-cap",
                ZMEM_CONVENTION_INTERVAL="10")
            event = json.dumps({"tool_name": "Read",
                                "tool_input": {"file_path": "x.ts"},
                                "session_id": "sess-cap"})
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
                input=event, capture_output=True, text=True, env=env,
                timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(Path(tmp, "ops", "sess-cap.log").exists(),
                             "the case gate must skip non-convention tools")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_edit_event_appends_path_basename(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-cap-")
        try:
            self._run_hook(
                tmp, "Edit",
                {"file_path": "src/lib/pr-workflow-gate.ts",
                 "old_string": "a", "new_string": "b"})
            ring = Path(tmp, "ops", "sess-cap.log")
            self.assertTrue(ring.is_file())
            obj = json.loads(ring.read_text(encoding="utf-8").strip())
            self.assertEqual(obj["ops"], "pr-workflow-gate.ts")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HermesConventionRingTest(unittest.TestCase):
    """The Hermes post_tool_call hook records the verb on the same ring."""

    def test_post_tool_call_kill_switch_stops_collection(self):
        # Review PRR-91-004 follow-up: the Hermes post_tool_call writer
        # honors the kill switch — no ring line, hook still silent-ok.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-hermes-ks-")
        env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT),
                         ZMEM_QUERY_CONTEXT="0")
        try:
            payload = json.dumps({
                "session_id": "s-ks",
                "extra": {"status": "ok", "tool": "Bash",
                          "command": "git stash pop"},
            })
            r = subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "hermes-plugin" / "hooks" /
                     "zmem-hermes-convention.py")],
                input=payload, capture_output=True, text=True, env=env,
                timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "{}")
            self.assertFalse(Path(tmp, "ops", "s-ks.log").exists(),
                             "kill switch must stop Hermes ring collection")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_post_tool_call_precedence_and_fallback(self):
        # Review PRR-91-012: extra.command wins over payload.tool_input;
        # payload.tool_input.command is the fallback when extra has none.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-hermes-prec-")
        env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT))
        try:
            ring = Path(tmp, "ops", "s-prec.log")
            payload = json.dumps({
                "session_id": "s-prec",
                "tool_input": {"command": "git stash pop"},
                "extra": {"status": "ok", "tool": "Bash",
                          "command": "bun test ratchet.test.ts"},
            })
            subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "hermes-plugin" / "hooks" /
                     "zmem-hermes-convention.py")],
                input=payload, capture_output=True, text=True, env=env,
                timeout=60)
            obj = json.loads(ring.read_text(encoding="utf-8").strip())
            self.assertEqual(obj["ops"], "bun test ratchet.test.ts")
            ring.unlink()
            payload2 = json.dumps({
                "session_id": "s-prec",
                "tool_input": {"command": "git stash pop"},
                "extra": {"status": "ok"},
            })
            subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "hermes-plugin" / "hooks" /
                     "zmem-hermes-convention.py")],
                input=payload2, capture_output=True, text=True, env=env,
                timeout=60)
            obj2 = json.loads(ring.read_text(encoding="utf-8").strip())
            self.assertEqual(obj2["ops"], "git stash pop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_post_tool_call_records_descriptor(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-hermes-")
        env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT))
        try:
            payload = json.dumps({
                "session_id": "sess-hermes",
                "extra": {"status": "ok", "tool": "Bash",
                          "command": "git stash pop"},
            })
            r = subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "hermes-plugin" / "hooks" /
                     "zmem-hermes-convention.py")],
                input=payload, capture_output=True, text=True, env=env,
                timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "{}")
            ring = Path(tmp, "ops", "sess-hermes.log")
            self.assertTrue(ring.is_file(),
                            "post_tool_call must record the verb")
            obj = json.loads(ring.read_text(encoding="utf-8").strip())
            self.assertEqual(obj["ops"], "git stash pop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HookBodyComposeTest(unittest.TestCase):
    """End-to-end: the UserPromptSubmit body composes ring into the query."""

    LESSON = ("ringcanary hazard: a later blind git stash pop can apply a "
              "foreign pre-existing stash; verify git stash list before any "
              "consuming command")

    def _run_body(self, tmp: str, prompt: str, session_id: str,
                  **extra: str) -> str:
        env = _clean_env(tmp, **extra)
        r = subprocess.run(
            [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
             "project:ops-e2e", "25000", "user_prompt"],
            input=json.dumps({"prompt": prompt, "session_id": session_id}),
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stdout)
        return r.stdout

    def test_ring_changes_retrieval_and_log_carries_ops(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-e2e-")
        try:
            env = _clean_env(tmp)
            _seed(env, "project:ops-e2e", self.LESSON)
            ring = Path(tmp, "ops", "sess-e2e.log")
            ring.parent.mkdir(parents=True)
            ring.write_text(
                json.dumps({"ts": 1, "tool": "Bash", "ops": "git stash pop"}),
                encoding="utf-8")
            log = Path(tmp, "zmem-bg.log")
            if log.exists():
                log.unlink()

            # Prose-only session: no ring → silent (retrieved nothing).
            out = self._run_body(tmp, "keep finalizing this work", "no-ring")
            ctx = json.loads(out.strip())["additionalContext"]
            self.assertEqual(ctx, "no durable memories retrieved for this prompt.")

            # Same prose WITH the ring session: the lesson injects.
            out = self._run_body(tmp, "keep finalizing this work", "sess-e2e")
            ctx = json.loads(out.strip())["additionalContext"]
            self.assertIn("ringcanary", ctx)
            line = [l for l in log.read_text(encoding="utf-8").splitlines()
                    if "zmem-hook" in l][-1]
            self.assertRegex(line, r"ops=\d+")

            # Kill switch restores the prose-only behavior.
            out = self._run_body(tmp, "keep finalizing this work", "sess-e2e",
                                 ZMEM_QUERY_CONTEXT="0")
            ctx = json.loads(out.strip())["additionalContext"]
            self.assertEqual(ctx, "no durable memories retrieved for this prompt.")

            # Review PRR-91-013: ring present + recall returns nothing → the
            # SILENT line still carries ops=N.
            ring2 = Path(tmp, "ops", "sess-empty2.log")
            ring2.write_text(
                json.dumps({"ts": 2, "tool": "Bash",
                            "ops": "kubectl rollout undo"}),
                encoding="utf-8")
            out = self._run_body(tmp, "keep finalizing this unrelated zebra",
                                 "sess-empty2")
            ctx = json.loads(out.strip())["additionalContext"]
            self.assertNotIn("ringcanary", ctx)
            line = [l for l in log.read_text(encoding="utf-8").splitlines()
                    if "zmem-hook" in l][-1]
            self.assertIn("status=silent", line)
            self.assertRegex(line, r"ops=\d+")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DataDirPrecedenceTest(unittest.TestCase):
    """Review PRR-91-001: the ring reader (hook body) must resolve the data
    dir ZMEM_STORE-first, matching the convention-capture writer — otherwise
    split-env deployments silently no-op the lane. The plugin-data cases
    below extend the same writer/reader-parity contract to the rest of the
    writer's chain (CLAUDE_PLUGIN_DATA / ZCODE_PLUGIN_DATA): a non-launcher
    environment that only sets a plugin-data var must still find the ring."""

    def _plugin_data_env(self, tmp: str) -> dict:
        # _clean_env seeds ZMEM_STORE/ZMEM_DATA; the plugin-data branch of
        # the chain is only reachable when BOTH are absent, so pop them.
        env = _clean_env(tmp)
        env.pop("ZMEM_STORE", None)
        env.pop("ZMEM_DATA", None)
        # Keep even the pre-fix home fallback inside the sandbox: a failing
        # reader must probe <sandbox-home>/.zmem, never the operator's.
        # exist_ok: tests may call this helper twice on one tmp (two
        # sub-scenarios sharing a sandbox).
        home = Path(tmp, "sandbox-home")
        home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        return env

    def _run_writer(self, env: dict, session_id: str, cwd: str = None) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        cenv = dict(env)
        cenv.update({"ZMEM_ROOT": str(REPO_ROOT), "ZMEM_SESSION": session_id,
                     "ZMEM_CONVENTION_INTERVAL": "10"})
        event = json.dumps({"tool_name": "Bash",
                            "tool_input": {"command": "git stash pop"},
                            "session_id": session_id})
        subprocess.run(
            [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
            input=event, capture_output=True, text=True, env=cenv,
            timeout=60, cwd=cwd)

    def _run_reader(self, env: dict, ns: str, session_id: str,
                    cwd: str = None) -> str:
        r = subprocess.run(
            [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
             ns, "25000", "user_prompt"],
            input=json.dumps({"prompt": "keep finalizing this work",
                              "session_id": session_id}),
            capture_output=True, text=True, env=env, timeout=120, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())["additionalContext"]

    def _last_hook_line(self, data_dir: Path) -> str:
        log = data_dir / "zmem-bg.log"
        self.assertTrue(log.is_file(),
                        "bg log must co-locate with the ring's data dir")
        lines = [l for l in log.read_text(encoding="utf-8").splitlines()
                 if "zmem-hook" in l]
        self.assertTrue(lines, "no zmem-hook line in the bg log")
        return lines[-1]

    def test_reader_finds_ring_written_under_plugin_data_var(self):
        # THE W1 regression catcher: with ZMEM_STORE/ZMEM_DATA absent and
        # only ZCODE_PLUGIN_DATA set, the writer lands the ring under the
        # plugin-data dir and the reader must compose from the SAME dir.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-plugdata-")
        try:
            plugdata = Path(tmp, "plugdata")
            plugdata.mkdir()
            env = self._plugin_data_env(tmp)
            env["ZCODE_PLUGIN_DATA"] = str(plugdata)
            _seed(env, "project:prec-plug", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-q")
            self.assertTrue((plugdata / "ops" / "sess-q.log").is_file(),
                            "writer must resolve the plugin-data dir")
            self.assertFalse((Path(tmp) / "ops").exists(),
                             "writer must not write under the stripped "
                             "_clean_env data dir")
            self.assertFalse(
                (Path(env["HOME"]) / ".zmem" / "ops").exists(),
                "writer must not fall through to the home fallback")
            ctx = self._run_reader(env, "project:prec-plug", "sess-q")
            self.assertIn("ringcanary", ctx,
                          "reader must resolve the ring from the "
                          "plugin-data dir like the writer")
            line = self._last_hook_line(plugdata)
            self.assertIn("status=injected", line)
            # Exact token count: one `git stash pop` event derives exactly
            # three allowlisted tokens, so the composed tokens provably came
            # from THIS ring (the sandbox has no other ring to probe).
            self.assertRegex(line, r"ops=3(?:\s|$)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_claude_plugin_data_precedes_zcode(self):
        # Precedence pin (control): with both plugin-data vars set, writer
        # and reader must agree on the CLAUDE dir and never touch the ZCODE
        # one (host.resolve_store_path checks CLAUDE first).
        tmp = tempfile.mkdtemp(prefix="zmem-ops-plugorder-")
        try:
            claude_loc = Path(tmp, "claude-loc")
            zcode_loc = Path(tmp, "zcode-loc")
            claude_loc.mkdir()
            zcode_loc.mkdir()
            env = self._plugin_data_env(tmp)
            env["CLAUDE_PLUGIN_DATA"] = str(claude_loc)
            env["ZCODE_PLUGIN_DATA"] = str(zcode_loc)
            _seed(env, "project:prec-order", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-o")
            self.assertTrue((claude_loc / "ops" / "sess-o.log").is_file(),
                            "writer must prefer CLAUDE_PLUGIN_DATA")
            self.assertFalse((zcode_loc / "ops").exists())
            ctx = self._run_reader(env, "project:prec-order", "sess-o")
            self.assertIn("ringcanary", ctx,
                          "reader must prefer CLAUDE_PLUGIN_DATA too")
            line = self._last_hook_line(claude_loc)
            self.assertIn("status=injected", line)
            self.assertRegex(line, r"ops=3(?:\s|$)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zmem_data_wins_over_plugin_data(self):
        # Precedence pin (PRR-101-008): ZMEM_DATA is the explicit operator
        # override — when set alongside a plugin-data var it must win for
        # BOTH the writer and the reader (the reader-side half of this
        # ordering had no coverage before).
        tmp = tempfile.mkdtemp(prefix="zmem-ops-zdata-")
        try:
            data_loc = Path(tmp, "data-loc")
            zcode_loc = Path(tmp, "zcode-loc")
            data_loc.mkdir()
            zcode_loc.mkdir()
            env = _clean_env(tmp)
            env.pop("ZMEM_STORE", None)
            env["ZMEM_DATA"] = str(data_loc)
            env["ZCODE_PLUGIN_DATA"] = str(zcode_loc)
            _seed(env, "project:prec-zdata", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-z")
            self.assertTrue((data_loc / "ops" / "sess-z.log").is_file(),
                            "writer must prefer ZMEM_DATA over plugin-data")
            self.assertFalse((zcode_loc / "ops").exists())
            ctx = self._run_reader(env, "project:prec-zdata", "sess-z")
            self.assertIn("ringcanary", ctx,
                          "reader must prefer ZMEM_DATA over plugin-data too")
            self._last_hook_line(data_loc)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_writer_expands_tilde_plugin_data_like_reader(self):
        # Cubic CLM-1 (PRR-101-001): bash copies a tilde-valued plugin-data
        # var verbatim while python expands it — the writer must expand too,
        # or the ring lands in a literal '~' directory and the lane silently
        # no-ops. The sandboxed HOME pins BOTH sides to the same expansion.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-tilde-")
        try:
            env = self._plugin_data_env(tmp)
            env["ZCODE_PLUGIN_DATA"] = "~/pd-t"
            pd_dir = Path(env["HOME"]) / "pd-t"
            pd_dir.mkdir(parents=True)
            _seed(env, "project:prec-tilde", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-t", cwd=tmp)
            self.assertTrue((pd_dir / "ops" / "sess-t.log").is_file(),
                            "writer must expand a tilde-valued plugin-data "
                            "var like the python readers do")
            self.assertFalse((Path(tmp) / "~").exists(),
                             "a literal '~' directory must never be created "
                             "in the process cwd")
            ctx = self._run_reader(env, "project:prec-tilde", "sess-t",
                                   cwd=tmp)
            self.assertIn("ringcanary", ctx,
                          "reader must find the ring under the expanded "
                          "tilde path")
            line = self._last_hook_line(pd_dir)
            self.assertIn("status=injected", line)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tilde_zmem_data_and_store_agree_across_lane(self):
        # Cubic round-2 (CLM-2): the bash writers expand a tilde-resolved
        # DATA_DIR from ANY chain branch, so the reader must expand
        # ZMEM_DATA / ZMEM_STORE too — otherwise a tilde-valued operator
        # override splits writer from reader (writer at the expanded home
        # path, reader still probing the literal '~' path).
        tmp = tempfile.mkdtemp(prefix="zmem-ops-tilde-zdata-")
        try:
            env = self._plugin_data_env(tmp)
            env["ZMEM_DATA"] = "~/pd-zd"
            zd_dir = Path(env["HOME"]) / "pd-zd"
            zd_dir.mkdir(parents=True)
            _seed(env, "project:prec-tilde-zd", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-zd", cwd=tmp)
            self.assertTrue((zd_dir / "ops" / "sess-zd.log").is_file(),
                            "writer must expand a tilde-valued ZMEM_DATA")
            self.assertFalse((Path(tmp) / "~").exists())
            ctx = self._run_reader(env, "project:prec-tilde-zd", "sess-zd",
                                   cwd=tmp)
            self.assertIn("ringcanary", ctx,
                          "reader must expand tilde ZMEM_DATA like the "
                          "writer")
            self._last_hook_line(zd_dir)

            # Session-start's inline Tier-2 block must expand too: the outer
            # bash re-exports ZMEM_DATA verbatim, so an unexpanded block
            # would fail its own isdir gate and silently drop the decision
            # line (reviewer round-3 finding). Pin the namespace the hook's
            # recent-pull queries (ambient ZMEM_PROJECT-family vars are not
            # stripped by _STRIP_ENV) so rows exist and the decision line is
            # actually emitted.
            env.update({"ZMEM_ROOT": str(REPO_ROOT), "ZMEM_SESSION": "sess-zd",
                        "ZMEM_HOST": "zcode", "ZMEM_CTX_BUDGET": "25000",
                        "ZMEM_NAMESPACE": "project:prec-tilde-zd"})
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-session-start.sh")],
                input=json.dumps({"session_id": "sess-zd"}),
                capture_output=True, text=True, env=env, timeout=180, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            # session-start's decision line is the one WITHOUT reason= (the
            # body's line carries it) — assert THAT line landed in the
            # expanded dir rather than being silently dropped by the block's
            # isdir gate.
            ss_lines = [ln for ln in (zd_dir / "zmem-bg.log").read_text(
                encoding="utf-8").splitlines()
                if "zmem-hook" in ln and "reason=" not in ln]
            self.assertTrue(
                ss_lines,
                "session-start's own Tier-2 decision line must land in the "
                "expanded tilde dir, not be silently dropped")

            env = self._plugin_data_env(tmp)
            env["ZMEM_STORE"] = "~/pz/store.sqlite"
            pz_dir = Path(env["HOME"]) / "pz"
            pz_dir.mkdir(parents=True)
            _seed(env, "project:prec-tilde-zs", HookBodyComposeTest.LESSON)
            self._run_writer(env, "sess-zs", cwd=tmp)
            self.assertTrue((pz_dir / "ops" / "sess-zs.log").is_file(),
                            "writer must expand a tilde-valued ZMEM_STORE")
            ctx = self._run_reader(env, "project:prec-tilde-zs", "sess-zs",
                                   cwd=tmp)
            self.assertIn("ringcanary", ctx,
                          "reader must expand tilde ZMEM_STORE like the "
                          "writer")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_session_start_bg_log_lands_in_plugin_data_dir(self):
        # V1 (PRR-101-002): session-start's own bg-log writers (the bash
        # BG_SINK block and the inline Tier-2 decision block) must resolve
        # the SAME chain as the body — in a plugin-data-only environment the
        # whole diagnostic log lands in ONE file instead of splitting across
        # the plugin-data dir and ~/.zmem. CLAUDE-only is the catcher env:
        # the pre-fix bash chain covered ZCODE but omitted CLAUDE entirely,
        # so a ZCODE-only env would mask the split.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-ssstart-")
        try:
            plugdata = Path(tmp, "plugdata")
            plugdata.mkdir()
            env = self._plugin_data_env(tmp)
            env["CLAUDE_PLUGIN_DATA"] = str(plugdata)
            env.update({"ZMEM_ROOT": str(REPO_ROOT), "ZMEM_SESSION": "sess-ss",
                        "ZMEM_HOST": "zcode", "ZMEM_CTX_BUDGET": "25000"})
            # The Tier-2 decision line is only written when the recent-pull
            # returns rows — seed one into the global namespace the hook
            # queries (the store resolves via the plugin-data chain too).
            _seed(env, "user:global", HookBodyComposeTest.LESSON)
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-session-start.sh")],
                input=json.dumps({"session_id": "sess-ss"}),
                capture_output=True, text=True, env=env, timeout=180, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            log = plugdata / "zmem-bg.log"
            self.assertTrue(
                log.is_file(),
                "session-start must write the bg log into the plugin-data "
                "dir like the shared body does")
            self.assertIn("zmem-hook", log.read_text(encoding="utf-8"))
            self.assertFalse(
                (Path(env["HOME"]) / ".zmem" / "zmem-bg.log").exists(),
                "the bg log must not split off to the home fallback")
            # End-to-end worker proof (reviewer gate finding): the detached
            # cadence worker starts through a python wrapper after a 15s
            # race-guard delay — assert its output ACTUALLY lands in this bg
            # log. A wrapper that fails to exec store.py through the
            # interpreter (ENOEXEC/WinError 193, no shebang) is fire-and-
            # forget with its traceback in BG_SINK, so nothing else would
            # ever notice it (CI was green with exactly that bug).
            import time as _t
            deadline = _t.time() + 60
            cadence_line = ""
            while _t.time() < deadline:
                text = log.read_text(encoding="utf-8")
                hits = [ln for ln in text.splitlines()
                        if "session-cadence:" in ln]
                if hits:
                    cadence_line = hits[-1]
                    break
                _t.sleep(2)
            self.assertTrue(
                cadence_line and "sweep: ok" in cadence_line,
                "the deferred session-cadence worker must actually run and "
                f"log here; tail was: {log.read_text(encoding='utf-8')[-400:]!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_session_start_expands_tilde_plugin_data(self):
        # Final-critic finding on the fix round: session-start's bash chain
        # must expand a tilde-valued plugin-data var exactly like the
        # convention-capture writer, or its core.md/markers/bg log land in a
        # literal '~' directory while the ring lane uses the expanded one.
        tmp = tempfile.mkdtemp(prefix="zmem-ops-sstilde-")
        try:
            env = self._plugin_data_env(tmp)
            env["CLAUDE_PLUGIN_DATA"] = "~/pd-ss"
            pd_dir = Path(env["HOME"]) / "pd-ss"
            pd_dir.mkdir(parents=True)
            env.update({"ZMEM_ROOT": str(REPO_ROOT), "ZMEM_SESSION": "sess-st",
                        "ZMEM_HOST": "zcode", "ZMEM_CTX_BUDGET": "25000"})
            _seed(env, "user:global", HookBodyComposeTest.LESSON)
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            r = subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-session-start.sh")],
                input=json.dumps({"session_id": "sess-st"}),
                capture_output=True, text=True, env=env, timeout=180, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            self.assertFalse((Path(tmp) / "~").exists(),
                             "a literal '~' directory must never be created "
                             "in the process cwd")
            log = pd_dir / "zmem-bg.log"
            self.assertTrue(
                log.is_file(),
                "session-start must expand a tilde-valued plugin-data var "
                "like the ring writer does")
            self.assertIn("zmem-hook", log.read_text(encoding="utf-8"))
            self.assertFalse(
                (Path(env["HOME"]) / ".zmem" / "zmem-bg.log").exists(),
                "no session-start artifact may split off to ~/.zmem")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reader_finds_ring_written_under_zmem_store_dir(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-precedence-")
        try:
            store_dir = Path(tmp, "store-loc")
            data_dir = Path(tmp, "data-loc")
            store_dir.mkdir()
            data_dir.mkdir()
            env = _clean_env(tmp)
            env["ZMEM_STORE"] = str(store_dir / "store.sqlite")
            env["ZMEM_DATA"] = str(data_dir)
            _seed(env, "project:prec", HookBodyComposeTest.LESSON)
            # Writer (convention-capture resolves ZMEM_STORE-first) writes
            # under store-loc/ops — NOT under data-loc/ops.
            bash = shutil.which("bash")
            if not bash:
                self.skipTest("no bash on PATH")
            cenv = dict(env)
            cenv.update({"ZMEM_ROOT": str(REPO_ROOT), "ZMEM_SESSION": "sess-p",
                         "ZMEM_CONVENTION_INTERVAL": "10"})
            event = json.dumps({"tool_name": "Bash",
                                "tool_input": {"command": "git stash pop"},
                                "session_id": "sess-p"})
            subprocess.run(
                [bash, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
                input=event, capture_output=True, text=True, env=cenv,
                timeout=60)
            self.assertTrue((store_dir / "ops" / "sess-p.log").is_file(),
                            "writer must resolve ZMEM_STORE-first")
            self.assertFalse((data_dir / "ops").exists())
            # Reader (hook body) must find the ring under the SAME dir.
            r = subprocess.run(
                [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
                 "project:prec", "25000", "user_prompt"],
                input=json.dumps({"prompt": "keep finalizing this work",
                                  "session_id": "sess-p"}),
                capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            ctx = json.loads(r.stdout.strip())["additionalContext"]
            self.assertIn("ringcanary", ctx,
                          "reader must resolve the ring from the "
                          "ZMEM_STORE-parent dir like the writer")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HermesPrefetchComposeTest(unittest.TestCase):
    def test_prefetch_composes_ring(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-prefetch-")
        saved = {k: os.environ.get(k) for k in _STRIP_ENV}
        try:
            os.environ.update(_clean_env(tmp, ZMEM_HOME=str(REPO_ROOT)))
            _seed(_clean_env(tmp), "project:prefetch-compose",
                  "prefetchcanary hazard: blind git stash pop applies a "
                  "foreign stash; verify git stash list first")
            ring = Path(tmp, "ops", "sess-pf.log")
            ring.parent.mkdir(parents=True)
            ring.write_text(
                json.dumps({"ts": 1, "tool": "Bash", "ops": "git stash pop"}),
                encoding="utf-8")

            import types
            agent = types.ModuleType("agent")
            mp = types.ModuleType("agent.memory_provider")

            class MemoryProvider:
                pass

            mp.MemoryProvider = MemoryProvider
            agent.memory_provider = mp
            sys.modules.setdefault("agent", agent)
            sys.modules.setdefault("agent.memory_provider", mp)

            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_ops_prefetch",
                REPO_ROOT / "hermes-plugin" / "__init__.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["zmem_hermes_ops_prefetch"] = mod
            spec.loader.exec_module(mod)
            provider = mod.ZmemMemoryProvider()
            provider.initialize("sess-pf-provider")

            provider._namespace = "project:prefetch-compose"
            out = provider.prefetch("keep finalizing this work",
                                    session_id="sess-pf")
            self.assertIn("prefetchcanary", out)
            out_nosid = provider.prefetch("keep finalizing this work")
            self.assertNotIn("prefetchcanary", out_nosid)

            # Review PRR-91-009: the kill switch must silence the Hermes
            # prefetch composition too.
            os.environ["ZMEM_QUERY_CONTEXT"] = "0"
            try:
                out_ks = provider.prefetch("keep finalizing this work",
                                           session_id="sess-pf")
            finally:
                os.environ.pop("ZMEM_QUERY_CONTEXT", None)
            self.assertNotIn("prefetchcanary", out_ks)

            # Review round 1: a checkout where ops_tokens cannot be
            # imported (copy install without skills/, #36 M10) must
            # degrade to the prose-only query — no crash, no composition.
            saved_mod = mod._OPS_TOKENS
            mod._OPS_TOKENS = None
            try:
                out_none = provider.prefetch("keep finalizing this work",
                                             session_id="sess-pf")
            finally:
                mod._OPS_TOKENS = saved_mod
            self.assertNotIn("prefetchcanary", out_none)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(tmp, ignore_errors=True)


class EvalGoldComposeTest(unittest.TestCase):
    """Decision-point gold items: hit WITH ops, miss prose-only; legacy items
    compose byte-identically (identity pin at the loader level)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-ops-eval-")
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests" / "fixtures" /
                                 "eval_store.py"),
             os.path.join(cls._tmp, "store.sqlite")],
            check=True, capture_output=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_decision_items_hit_with_ops(self):
        r = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "eval_runner.py"),
             "--store", os.path.join(self._tmp, "store.sqlite")],
            capture_output=True, text=True, timeout=300,
            env=_clean_env(self._tmp, ZMEM_HOME=str(REPO_ROOT)))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        report = json.loads(r.stdout)
        items = report.get("items") or report.get("per_item") or []
        dec = {i["id"]: i for i in items if i.get("bucket") == "decision-point"}
        self.assertEqual(len(dec), 6, "six decision-point gold items")
        for item_id, i in dec.items():
            self.assertTrue(i.get("hit"), f"{item_id} must hit with ops")
            # Issue #88's acceptance bar: the ops lane puts each decision
            # item at rank 1-2 (exact ranks pinned in test_eval_runner).
            self.assertLessEqual(i.get("first_hit_rank", 99), 2)

    def test_decision_items_miss_prose_only(self):
        import contextlib
        import io as _io
        import sqlite3
        from storelib.eval_gold import load_gold
        from storelib.recall import recall_memory
        conn = sqlite3.connect(os.path.join(self._tmp, "store.sqlite"))
        conn.row_factory = sqlite3.Row
        for item in (g for g in load_gold(str(REPO_ROOT / "eval" / "gold.jsonl"))
                     if g.bucket == "decision-point"):
            with contextlib.redirect_stdout(_io.StringIO()):
                res = recall_memory(conn, query=item.query,
                                    namespace=item.namespace, limit=5,
                                    no_bump=True, no_telemetry=True,
                                    link_hops=0, link_budget=2,
                                    include_global=False, no_mmr=True)
            ids = [r["id"] for r in res]
            self.assertNotIn(item.must_include_ids[0], ids,
                             f"{item.id}: prose-only query must MISS — the "
                             "item would not measure the ops lane otherwise")

    def test_legacy_items_compose_identity(self):
        from storelib.eval_gold import load_gold
        for item in load_gold(str(REPO_ROOT / "eval" / "gold.jsonl")):
            if item.bucket == "decision-point":
                continue
            self.assertEqual(item.ops, "",
                             "legacy items must not carry ops")
            self.assertEqual(
                ops_tokens.compose_inject_query(item.query, item.ops),
                item.query.strip()[:500])

    def test_runner_path_composes_legacy_identically_and_ops_via_shared_fn(self):
        """Runner-level pin (review round 1): the identity must hold on the
        path evaluate_items ACTUALLY takes — the runner bypasses compose for
        ops-less items and calls the shared compose for ops items. Record
        the queries it hands to recall_memory and compare both."""
        import sqlite3

        import storelib.recall as recall_mod
        from storelib.eval_gold import GoldItem, evaluate_items
        recorded = []
        real = recall_mod.recall_memory

        def recorder(conn, *, query, **kw):
            recorded.append(query)
            return []

        conn = sqlite3.connect(":memory:")
        try:
            recall_mod.recall_memory = recorder
            legacy = GoldItem(id="lg-1", bucket="fts",
                              query="  padded legacy query  ",
                              must_include_ids=["x"])
            ops_item = GoldItem(id="dp-1", bucket="decision-point",
                                query="drive the loop", ops="git stash pop",
                                must_include_ids=["y"])
            evaluate_items(conn, [legacy, ops_item])
        finally:
            recall_mod.recall_memory = real
        self.assertEqual(len(recorded), 2)
        # Legacy: the runner passes the raw gold query untouched — the
        # byte-identical pre-#88 behavior (no compose call at all).
        self.assertEqual(recorded[0], legacy.query)
        # Ops item: the EXACT string the shared composer produces — the
        # eval measures the hook's real query, not a fork.
        self.assertEqual(
            recorded[1],
            ops_tokens.compose_inject_query(ops_item.query, ops_item.ops))


class SweepOpsRingTest(unittest.TestCase):
    def test_sweep_removes_stale_ring_keeps_fresh(self):
        tmp = tempfile.mkdtemp(prefix="zmem-ops-sweep-")
        try:
            ops_dir = Path(tmp, "ops")
            ops_dir.mkdir()
            stale = ops_dir / "old-session.log"
            fresh = ops_dir / "live-session.log"
            stale.write_text('{"ops": "git push origin HEAD"}\n',
                             encoding="utf-8")
            fresh.write_text('{"ops": "git stash pop"}\n', encoding="utf-8")
            old = time.time() - 90 * 86400
            os.utime(stale, (old, old))
            env = _clean_env(tmp)
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "store.py"), "sweep",
                 "--marker-dir", tmp, "--max-age-days", "30"],
                capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
