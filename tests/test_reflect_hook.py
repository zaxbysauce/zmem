"""Hook-level tests for the reflection prompts (issue #46).

Drives hooks/zmem-reflect.sh and hooks/zmem-subagent-reflect.sh end-to-end
(via bash) against synthetic Claude Code transcripts containing user
rejections, asserting the rendered additionalContext prompt:

  - the failure count line reports GENUINE failures only (a rejection is not
    miscounted as a failure),
  - user-rejection reasons render in a distinct fenced section with the
    `--signal user` hint,
  - when there are no rejections the prompt renders as before (no rejection
    section), and
  - the subagent hook surfaces a rejection-only transcript instead of no-op'ing.

These are the acceptance behaviours the issue's Tests list calls for at the
prompt level, which the store.py-unit tests cannot reach.

Run: python tests/test_reflect_hook.py
Requires a POSIX-ish `bash` on PATH (as CI provides on both runners); skipped
if unavailable. If the hook under test stops emitting when it should not, the
test FAILS rather than silently passing."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The hooks are bash scripts (they use bash-only constructs). Require `bash`
# specifically; falling back to `sh` (usually dash on Debian/Ubuntu) would make
# the tests fail spuriously instead of skipping. Test bodies skip when unset.
_BASH = shutil.which("bash")


def _write_transcript(records) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _tool_result(tid, content, is_error=True):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": content, "is_error": is_error, "tool_use_id": tid}]}}


def _rejection(tid, name, reason):
    return [
        _tool_use(tid, name),
        _tool_result(tid, "The user doesn't want to proceed.\nthe user said:\n" + reason),
    ]


def _run_hook(hook, env_extra, stdin="{}"):
    """Run a hook script with `stdin` on stdin and env_extra merged, returning
    the raw stdout (the <<<ZMEM_JSON>>>…<<<END>>> envelope)."""
    rc, stdout = _run_hook_rc(hook, env_extra, stdin=stdin)
    if rc != 0:
        return ""
    return stdout


def _run_hook_rc(hook, env_extra, stdin="{}"):
    """Like _run_hook but returns (returncode, stdout) so tests can assert the
    hook's exit code itself (#194: the kill switch and lock fail-open paths
    must exit 0, not merely print an empty envelope). Strips the hook-sensitive
    ZMEM_* vars from the ambient environment first (PRR-005): an operator's
    exported ZMEM_REFLECT=0 or ZMEM_ZCODE_DB must not flip test outcomes."""
    env = dict(os.environ)
    for key in ("ZMEM_REFLECT", "ZMEM_ZCODE_DB", "ZMEM_TRANSCRIPT",
                "ZMEM_FAILURES_DB_TIMEOUT_S"):
        env.pop(key, None)
    env.update(env_extra)
    proc = subprocess.run(
        [_BASH, str(hook)], input=stdin, text=True,
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    return proc.returncode, proc.stdout


def _extract_ctx(raw):
    """Pull the JSON object out of the sentinel envelope; return {} on failure."""
    if "<<<ZMEM_JSON>>>" not in raw:
        return {}
    inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
    try:
        return json.loads(inner)
    except Exception:
        return {}


class TestReflectHookMessaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, hook_name, env_extra, stdin="{}"):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_NAMESPACE": "project:hooktest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        env.update(env_extra)
        return _run_hook(REPO_ROOT / "hooks" / hook_name, env, stdin=stdin)

    def _run_rc(self, hook_name, env_extra, stdin="{}"):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_NAMESPACE": "project:hooktest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        env.update(env_extra)
        return _run_hook_rc(REPO_ROOT / "hooks" / hook_name, env, stdin=stdin)

    def test_failures_plus_rejection_not_miscounted_and_reason_shown(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),   # genuine failure (no marker)
            *_rejection("t2", "Edit", "don't touch the CI config"),  # rejection
        ])
        try:
            raw = self._run("zmem-reflect.sh", {"ZMEM_TRANSCRIPT": os.path.abspath(trans)})
            ctx = _extract_ctx(raw)
            msg = ctx.get("additionalContext", "")
            # Genuine failure count, not inflated to 2 by the rejection.
            self.assertIn("1 failed tool call(s)", msg, msg)
            self.assertNotIn("2 failed tool call(s)", msg, msg)
            # Rejection surfaced in a distinct, fenced section with its reason.
            self.assertIn("User rejected 1 tool call(s)", msg, msg)
            self.assertIn("don't touch the CI config", msg, msg)
            self.assertIn("--signal user", msg, msg)
            # Marker stripped; reason newline-free → appears on exactly one line
            # and cannot open its own fence line (fence-integrity composition).
            self.assertNotIn("the user said:", msg)
            self.assertEqual(msg.count("don't touch the CI config"), 1)
        finally:
            os.remove(trans)

    def test_renders_like_before_when_no_rejections(self):
        if not _BASH:
            self.skipTest("no bash")
        # db path, no transcript, no rejection substrate → must render as before
        # (no rejection section at all).
        raw = self._run("zmem-reflect.sh", {})
        ctx = _extract_ctx(raw)
        msg = ctx.get("additionalContext", "")
        self.assertIn("had no tool failures", msg, msg)   # success nudge kept
        self.assertNotIn("User rejected", msg, msg)       # no rejection section
        self.assertNotIn("Stated reasons", msg, msg)
        self.assertNotIn("--signal user", msg, msg)

    def test_stop_hook_active_loop_guard(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Bash", "stop"))
        try:
            raw = self._run("zmem-reflect.sh", {"ZMEM_TRANSCRIPT": os.path.abspath(trans)},
                            stdin='{"stop_hook_active": true}')
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    def test_reflect_kill_switch_emits_empty(self):
        # #194: ZMEM_REFLECT=0 disables the hook entirely — empty sentinel
        # envelope, exit 0 — even when the transcript contains a failure.
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            rc, raw = self._run_rc("zmem-reflect.sh", {
                "ZMEM_TRANSCRIPT": os.path.abspath(trans),
                "ZMEM_REFLECT": "0",
            })
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    @staticmethod
    def _make_zcode_db(path):
        conn = sqlite3.connect(path)
        # Pin rollback-journal mode: the BEGIN EXCLUSIVE lock below only blocks
        # readers on a delete-journal db (a WAL db would let the reader proceed
        # instantly and the timing assertion would be meaningless).
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("""CREATE TABLE tool_usage(
            session_id TEXT, tool_name TEXT, read_only INT, status TEXT,
            exit_code INT, error_message TEXT, error_type TEXT,
            retry_count INT, destructive INT, completed_at TEXT)""")
        conn.execute("INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("hooktest", "Bash", 0, "error", 1, "compile failed",
                      "BuildError", 0, 0, "2026-01-01"))
        conn.commit()
        conn.close()
        return path

    def test_locked_zcode_db_fails_open(self):
        # #194: a busy ZCode db (BEGIN EXCLUSIVE holder) must fail open inside
        # the bounded budget — hook exits 0 and never claims failed tool calls.
        # Substrate note: the production db is WAL-mode, where readers do not
        # block on a writer's BEGIN EXCLUSIVE at all; this fixture pins
        # rollback-journal mode as the deterministic substrate that engages
        # sqlite's busy handler, which is the mechanism the timeout kwarg
        # controls. The differential-timing assertion (PRR-002) proves the
        # env-driven bound is real end-to-end: with the lock held, the
        # 0.2s-budget run must finish well over 2s faster than the 3.0s-budget
        # run (sqlite's busy handler overshoots ~1.5x, so the slow run waits
        # ~4.5-5s — still inside the hook's 10s subprocess budget). If the env
        # var stopped reaching the reader, both runs would take the same
        # default wait and this assertion would fail.
        if not _BASH:
            self.skipTest("no bash")
        path = os.path.join(self.tmp, "zcode-db.sqlite")
        self._make_zcode_db(path)
        holder = sqlite3.connect(path)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            t0 = time.perf_counter()
            rc, raw = self._run_rc("zmem-reflect.sh", {
                "ZMEM_ZCODE_DB": path,
                "ZMEM_FAILURES_DB_TIMEOUT_S": "0.2",
            })
            fast = time.perf_counter() - t0
            self.assertEqual(rc, 0)
            self.assertNotIn("failed tool call(s)", raw, raw)
            t0 = time.perf_counter()
            rc2, raw2 = self._run_rc("zmem-reflect.sh", {
                "ZMEM_ZCODE_DB": path,
                "ZMEM_FAILURES_DB_TIMEOUT_S": "3.0",
            })
            slow = time.perf_counter() - t0
            self.assertEqual(rc2, 0)
            self.assertNotIn("failed tool call(s)", raw2, raw2)
            self.assertLess(
                fast + 2.0, slow,
                f"bounded wait not honored end-to-end: fast={fast:.2f}s slow={slow:.2f}s")
        finally:
            holder.rollback()
            holder.close()

    def test_zcode_db_override_is_used(self):
        # #194: ZMEM_ZCODE_DB points the db substrate at a scratch copy; a
        # qualifying row there must be detected (override honored end-to-end).
        if not _BASH:
            self.skipTest("no bash")
        path = os.path.join(self.tmp, "zcode-db.sqlite")
        self._make_zcode_db(path)
        rc, raw = self._run_rc("zmem-reflect.sh", {"ZMEM_ZCODE_DB": path})
        self.assertEqual(rc, 0)
        self.assertIn("1 failed tool call(s)", raw, raw)


class TestSubagentReflectMessaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, env_extra):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_AGENT_ID": "agent-1",
            "ZMEM_AGENT_TYPE": "explorer",
            "ZMEM_NAMESPACE": "project:hooktest",
        }
        env.update(env_extra)
        return _extract_ctx(_run_hook(REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh", env))

    def test_rejection_only_is_surfaced_not_noop(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Edit", "leave the schema alone"))
        try:
            ctx = self._run({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            msg = ctx.get("additionalContext", "")
            self.assertIn("had tool rejections but no tool failures", msg, msg)
            self.assertIn("leave the schema alone", msg, msg)
            self.assertIn("--signal user", msg, msg)
        finally:
            os.remove(trans)

    def test_failures_plus_rejection(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "boom"),   # genuine failure
            *_rejection("t2", "Read", "not that file"),  # rejection
        ])
        try:
            ctx = self._run({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            msg = ctx.get("additionalContext", "")
            self.assertIn("1 failed tool call(s)", msg, msg)
            self.assertIn("User rejected 1 tool call(s)", msg, msg)
            self.assertIn("not that file", msg, msg)
        finally:
            os.remove(trans)

    def test_no_failures_no_rejections_is_noop(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "all good", is_error=False),
        ])
        try:
            ctx = self._run({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            self.assertEqual(ctx, {}, ctx)
        finally:
            os.remove(trans)

    def test_stop_hook_active_loop_guard(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Edit", "stop"))
        try:
            env = {
                "ZMEM_DATA": self.tmp,
                "ZMEM_SESSION": "hooktest",
                "ZMEM_AGENT_ID": "agent-1",
                "ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans),
            }
            raw = _run_hook(REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh",
                            env, stdin='{"stop_hook_active": true}')
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)


if __name__ == "__main__":
    unittest.main(verbosity=2)
