"""Issue #258: `store.py signals` — the Stop-hook gate's read command.

Pins the command's contract independently of the hook (the hook end-to-end
behavior is pinned by tests/test_r02_nudge_on_signal_change.py and
tests/test_r02_signal_state.py):

  - classification: Bash commands classify via capture_quality.infer_signal
    (test/compile/lint; everything else untracked), joining tool_result
    is_error flags with LAST status per signal (chronological order),
  - exit-code contract: checked results (including a missing transcript
    path, which falls to the db leg like `failures`) exit 0; a genuine
    substrate error (unreadable transcript / injected walker OSError)
    prints an error object and exits 2.

Run: python tests/test_signals_cmd.py
"""

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
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"


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


def _run_signals(env, *extra):
    return subprocess.run(
        [sys.executable, str(STORE_PY), "signals", *extra],
        capture_output=True, text=True, env=env, timeout=60)


def _env(tmp):
    env = dict(os.environ)
    for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
                "ZMEM_TRANSCRIPT"):
        env.pop(key, None)
    env.update({"ZMEM_DATA": tmp, "ZMEM_MODELS_DIR": os.path.join(tmp, "mm"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0"})
    return env


class SignalsClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-signals-")
        self.transcript = os.path.join(self.tmp, "t.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _signals(self, commands):
        _write_transcript(self.transcript, commands)
        r = _run_signals(_env(self.tmp), "--session", "sigtest",
                         "--transcript", self.transcript)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["signals"]

    def test_runner_families_classified_last_status_wins(self):
        signals = self._signals([
            ("ls", False),
            ("python -m pytest -q", True),    # failing pytest
            ("python -m pytest -q", False),   # then passing — last wins
            ("python -m compileall src", False),
            ("ruff check .", False),
            ("biome check src/", False),
            ("curl https://example.test", False),  # none: untracked
            ("git push", False),                     # none: untracked
        ])
        self.assertEqual(signals, {"test": "pass", "compile": "pass",
                                   "lint": "pass"}, signals)

    def test_fail_last_wins_and_unittest_is_test(self):
        signals = self._signals([
            ("python -m unittest tests/test_x.py", False),
            ("python -m unittest tests/test_x.py", True),
        ])
        self.assertEqual(signals, {"test": "fail"}, signals)

    def test_non_bash_tools_are_not_classified(self):
        # Only Bash tool_use blocks carry shell command text; an Edit block
        # reusing a pytest-looking input must not classify.
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "e1", "name": "Edit",
                 "input": {"command": "python -m pytest -q"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "ok",
                 "is_error": False, "tool_use_id": "e1"}]}},
        ]
        Path(self.transcript).write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        r = _run_signals(_env(self.tmp), "--session", "sigtest",
                         "--transcript", self.transcript)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["signals"], {})

    def test_malformed_lines_are_skipped(self):
        # Per-line parse errors are checked results, not failures.
        _write_transcript(self.transcript, [("pytest tests/x.py", False)])
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write("{not json at all\n")
        r = _run_signals(_env(self.tmp), "--session", "sigtest",
                         "--transcript", self.transcript)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["signals"], {"test": "pass"})


class SignalsExitContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-signals-x-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_transcript_is_checked_empty(self):
        r = _run_signals(_env(self.tmp), "--session", "sigtest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"signals": {}})

    def test_missing_transcript_path_is_checked_empty(self):
        # Falls to the db leg exactly like `failures`; the db substrate has
        # no command text, so the result is a checked empty, not an error.
        r = _run_signals(_env(self.tmp), "--session", "sigtest",
                         "--transcript", os.path.join(self.tmp, "gone.jsonl"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"signals": {}})

    def test_unreadable_transcript_exits_two(self):
        # Genuine substrate error. chmod 0o000 only forbids reads on POSIX
        # (and non-root); if the platform still lets us read it, the leg
        # cannot run here — skip rather than assert a vacuous pass.
        transcript = os.path.join(self.tmp, "t.jsonl")
        _write_transcript(transcript, [("pytest tests/x.py", False)])
        os.chmod(transcript, 0o000)
        try:
            try:
                with open(transcript, "rb"):
                    pass
                self.skipTest("platform ignores chmod 0o000; no way to "
                              "force an unreadable file here")
            except OSError:
                pass
            r = _run_signals(_env(self.tmp), "--session", "sigtest",
                             "--transcript", transcript)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            obj = json.loads(r.stdout)
            self.assertIn("error", obj)
        finally:
            os.chmod(transcript, 0o644)

    def test_injected_walker_oserror_exits_two(self):
        # Portable exit-2 pin: the walker's file-level OSError must reach
        # cmd_signals (not be swallowed into a checked empty).
        saved_env = dict(os.environ)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "mm")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        saved_modules = {k: sys.modules.pop(k)
                         for k in list(sys.modules)
                         if k == "storelib" or k.startswith("storelib.")}
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            import storelib.mine as mine
            def _boom(path):
                raise OSError("injected read failure")
            orig = mine._signals_from_transcript
            mine._signals_from_transcript = _boom
            try:
                import io, contextlib
                # The walker only runs for a transcript path that EXISTS
                # (cmd_signals falls to the db leg otherwise), so point the
                # command at a real file before injecting the failure.
                real_path = os.path.join(self.tmp, "real.jsonl")
                Path(real_path).write_text("{}\n", encoding="utf-8")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mine.cmd_signals(session="s", transcript=real_path,
                                          db="unused")
            finally:
                mine._signals_from_transcript = orig
            self.assertEqual(rc, 2)
            obj = json.loads(buf.getvalue())
            self.assertIn("error", obj)
        finally:
            sys.path.pop(0)
            for key in list(k for k in sys.modules
                            if k == "storelib" or k.startswith("storelib.")):
                del sys.modules[key]
            sys.modules.update(saved_modules)
            os.environ.clear()
            os.environ.update(saved_env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
