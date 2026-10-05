"""Issue #258 (Workstream R PR 2): the no-failure Stop nudge fires only when
a tracked signal changed.

At MAIN, hooks/zmem-reflect.sh's `count == 0` branch builds and emits the
"you may have learned something worth capturing" nudge unconditionally on
every quiet Stop — the condition is a state predicate, not a transition
predicate, so the nudge's cost is constant per Stop regardless of change.
These checks pin the gated contract:

  - AC1: a second Stop on an identical quiet transcript emits NO nudge,
  - AC2: a recognized runner signal appearing between Stops re-arms the
    nudge (a passing `python -m pytest` run, which capture_quality's
    infer_signal maps to `test`),

plus reviewed pins that travel in the same frozen file:

  - first Stop of a fresh session still nudges (AC3's end-to-end companion;
    also pins that the nudge-firing Stop records state),
  - the suppressed Stop still records the signal state for the closeout
    skill (AC6 substitute evidence: ops/<sha256(session)[:32]>.signals,
    version 1, per-signal last status; no additionalContext),
  - a `ruff check` run re-arms via the `lint` signal (vocabulary breadth),
  - a stop_hook_active payload writes no state ahead of the loop guard
    (AC5 companion).

Mirrors the harness of tests/test_reflect_hook.py /
tests/test_r01_subagent_details.py (Git-Bash drives hooks/zmem-reflect.sh
end-to-end; the <<<ZMEM_JSON>>> envelope is parsed).
Run: python tests/test_r02_nudge_on_signal_change.py
Skips when no bash is available (same policy as test_reflect_hook.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_GIT_BASHES = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
)
_BASH = next((str(path) for path in _GIT_BASHES if path.is_file()),
             shutil.which("bash"))

SESSION = "rtest"
NUDGE = "you may have learned something worth capturing"


def _write_transcript(path, commands):
    """Write a Claude-Code-style JSONL transcript: one Bash tool_use plus one
    tool_result per (command, is_error) pair, in order."""
    records = []
    for i, (cmd, is_error) in enumerate(commands):
        tid = "t%d" % i
        records.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash",
             "input": {"command": cmd}}]}})
        records.append({"type": "user", "message": {"content": [
            {"type": "tool_result",
             "content": "Exit code 1" if is_error else "ok",
             "is_error": is_error, "tool_use_id": tid}]}})
    Path(path).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class NudgeOnSignalChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        # Quiet transcript: one successful ls run (infer_signal -> none).
        _write_transcript(self.transcript, [("ls", False)])

    def _state_path(self):
        stem = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:32]
        return Path(self.tmp) / "ops" / (stem + ".signals")

    def _run_stop(self, stdin="{}"):
        env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_CAPTURE", "ZMEM_ZCODE_DB",
                    "ZMEM_TRANSCRIPT", "ZMEM_FAILURES_DB_TIMEOUT_S",
                    "ZMEM_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": SESSION,
            "ZMEM_NAMESPACE": "project:" + SESSION,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_TRANSCRIPT": self.transcript,
        })
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-reflect.sh")],
            input=stdin, text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=env, timeout=60,
        )
        raw = proc.stdout if proc.returncode == 0 else ""
        if "<<<ZMEM_JSON>>>" not in raw:
            return ""
        inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
        try:
            return json.loads(inner).get("additionalContext", "")
        except Exception:
            return ""

    def test_second_stop_without_signal_change_is_silent(self):
        # AC1: identical quiet transcript, second Stop must not re-nudge.
        if not _BASH:
            self.skipTest("no bash")
        msg1 = self._run_stop()
        self.assertIn(NUDGE, msg1, msg1)  # precondition: first Stop nudges
        msg2 = self._run_stop()
        self.assertEqual(msg2.count(NUDGE), 0, msg2)

    def test_nudge_rearms_after_signal_flip(self):
        # AC2: silenced Stop re-arms when a recognized runner signal appears
        # (a passing python -m pytest run -> infer_signal "test").
        if not _BASH:
            self.skipTest("no bash")
        self._run_stop()               # Stop 1: fresh session, nudges
        msg2 = self._run_stop()        # Stop 2: unchanged, must be quiet
        quiet = NUDGE in msg2
        _write_transcript(self.transcript,
                          [("ls", False), ("python -m pytest -q", False)])
        msg3 = self._run_stop()        # Stop 3: test signal flipped on
        flipped = NUDGE in msg3
        self.assertEqual((quiet, flipped), (False, True))

    def test_first_stop_still_nudges_and_records_state(self):
        # AC3 companion: the first Stop of a fresh session still nudges, and
        # the nudge-firing Stop records the signal state (record-then-nudge).
        if not _BASH:
            self.skipTest("no bash")
        msg1 = self._run_stop()
        self.assertIn(NUDGE, msg1, msg1)
        state = self._state_path()
        self.assertTrue(state.is_file(), "state file missing after Stop 1")
        doc = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(doc.get("version"), 1, doc)
        self.assertEqual(doc.get("signals"), {}, doc)  # only `ls` ran

    def test_state_file_records_signal_when_nudges_suppressed(self):
        # AC6 substitute evidence: a SUPPRESSED Stop still records the
        # signal for the closeout skill — no additionalContext, but the
        # state file carries the per-signal last status (test: pass).
        if not _BASH:
            self.skipTest("no bash")
        self._run_stop()               # Stop 1: nudge, state recorded
        _write_transcript(self.transcript,
                          [("ls", False), ("python -m pytest -q", False)])
        self._run_stop()               # Stop 2: flip, nudges, records
        msg3 = self._run_stop()        # Stop 3: unchanged -> suppressed
        self.assertEqual(msg3.count(NUDGE), 0, msg3)
        doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(doc.get("version"), 1, doc)
        self.assertEqual(doc.get("signals"), {"test": "pass"}, doc)

    def test_lint_signal_flip_rearms_too(self):
        # Vocabulary breadth: a `ruff check` run re-arms via the `lint`
        # signal exactly like a pytest run re-arms via `test`.
        if not _BASH:
            self.skipTest("no bash")
        self._run_stop()               # Stop 1: nudge
        msg2 = self._run_stop()        # Stop 2: quiet
        quiet = NUDGE in msg2
        _write_transcript(self.transcript,
                          [("ls", False), ("ruff check .", False)])
        msg3 = self._run_stop()        # Stop 3: lint signal flipped on
        flipped = NUDGE in msg3
        self.assertEqual((quiet, flipped), (False, True))
        doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(doc.get("signals"), {"lint": "pass"}, doc)

    def test_stop_hook_active_writes_no_state(self):
        # AC5 companion: the loop guard exits before any state access, so a
        # stop_hook_active payload must leave no state file behind.
        if not _BASH:
            self.skipTest("no bash")
        msg = self._run_stop(stdin='{"stop_hook_active": true}')
        self.assertEqual(msg.count(NUDGE), 0, msg)
        self.assertFalse(self._state_path().exists(),
                         "state written ahead of the loop guard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
