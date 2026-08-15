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
import subprocess
import sys
import tempfile
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
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        [_BASH, str(hook)], input=stdin, text=True,
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


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
        }
        env.update(env_extra)
        return _run_hook(REPO_ROOT / "hooks" / hook_name, env, stdin=stdin)

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
