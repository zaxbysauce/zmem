"""Issue #258: the no-failure nudge's per-session signal state file.

End-to-end companion pins for the STATE mechanics the frozen
tests/test_r02_nudge_on_signal_change.py deliberately does not cover
(its file is byte-frozen at the red checkpoint):

  - scan-failure posture: when `store.py signals` cannot read the substrate,
    the hook emits nothing AND leaves the persisted state file untouched
    (neither nags every Stop nor blanks the record),
  - atomic-write behavior: no `*.tmp.*` residue under <ZMEM_DATA>/ops after
    Stops, the state file always parses as complete JSON, and a flip Stop
    replaces the file whole (sentinel content gone, new content complete),
    never truncated in place,
  - a corrupt pre-existing state file degrades to the first-Stop path
    (nudge), not to a crash or permanent silence.

Run: python tests/test_r02_signal_state.py
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

SESSION = "rtest02s"
NUDGE = "you may have learned something worth capturing"


def _write_transcript(path, commands):
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


class SignalStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-r02-state-")
        self.transcript = os.path.join(self.tmp, "t.jsonl")
        _write_transcript(self.transcript, [("ls", False)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_path(self):
        stem = hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:32]
        return Path(self.tmp) / "ops" / (stem + ".signals")

    def _run_stop(self):
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
            input="{}", text=True, capture_output=True,
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

    def _ops_tmp_residue(self):
        ops = Path(self.tmp) / "ops"
        if not ops.is_dir():
            return []
        return [p.name for p in ops.iterdir() if ".tmp." in p.name]

    def test_scan_failure_leaves_state_untouched_and_emits_nothing(self):
        if not _BASH:
            self.skipTest("no bash")
        msg1 = self._run_stop()
        self.assertIn(NUDGE, msg1, msg1)  # first Stop records + nudges
        state = self._state_path()
        self.assertTrue(state.is_file())
        before = state.read_bytes()
        # Forcing a genuine scan failure requires an unreadable file; that
        # only forbids reads on POSIX (and non-root). If this platform
        # ignores chmod 0o000, the leg cannot run here — skip honestly.
        os.chmod(self.transcript, 0o000)
        try:
            try:
                with open(self.transcript, "rb"):
                    pass
                self.skipTest("platform ignores chmod 0o000; cannot force "
                              "an unreadable transcript here")
            except OSError:
                pass
            msg2 = self._run_stop()
            self.assertNotIn(NUDGE, msg2, msg2)
            # The record from the successful Stop must survive verbatim.
            self.assertEqual(state.read_bytes(), before)
        finally:
            os.chmod(self.transcript, 0o644)

    def test_no_tmp_residue_and_state_always_parseable(self):
        if not _BASH:
            self.skipTest("no bash")
        self._run_stop()                    # Stop 1: nudge
        self._run_stop()                    # Stop 2: suppressed
        _write_transcript(self.transcript,
                          [("ls", False), ("python -m pytest -q", False)])
        msg3 = self._run_stop()             # Stop 3: flip, nudge
        self.assertIn(NUDGE, msg3, msg3)
        self.assertEqual(self._ops_tmp_residue(), [])
        doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(doc.get("signals"), {"test": "pass"}, doc)

    def test_state_file_replaced_whole_not_truncated(self):
        if not _BASH:
            self.skipTest("no bash")
        ops = Path(self.tmp) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        # Sentinel pre-state: comparison-equal to the current quiet state
        # (signals {} + user_corrections 0) but carrying stale extras; the
        # compare ignores them, so the first Stop is SUPPRESSED...
        self._state_path().write_text(json.dumps({
            "version": 1, "signals": {}, "user_corrections": 0,
            "updated": "sentinel-timestamp", "sentinel": True,
        }) + "\n", encoding="utf-8")
        msg1 = self._run_stop()
        self.assertNotIn(NUDGE, msg1, msg1)
        # ...then a flip replaces the file whole: sentinel content gone.
        _write_transcript(self.transcript,
                          [("ls", False), ("ruff check .", False)])
        msg2 = self._run_stop()
        self.assertIn(NUDGE, msg2, msg2)
        doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertNotIn("sentinel-timestamp", doc)
        self.assertFalse(doc.get("sentinel"))
        self.assertEqual(doc.get("signals"), {"lint": "pass"}, doc)
        self.assertEqual(doc.get("user_corrections"), 0, doc)

    def test_corrupt_state_degrades_to_first_stop(self):
        if not _BASH:
            self.skipTest("no bash")
        ops = Path(self.tmp) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text("{not json\n", encoding="utf-8")
        msg = self._run_stop()
        self.assertIn(NUDGE, msg, msg)  # unreadable state == first Stop
        doc = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(doc.get("version"), 1, doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
