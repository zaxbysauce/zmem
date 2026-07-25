"""Tests for the READ-ONLY `--no-bump` flag on store.py `recall` and `recent`.

Hook-driven recall (UserPromptSubmit, SubagentStart) must not write to the shared
store, so a subagent dispatch fan-out does not turn N delegated agents into N
concurrent writers on the box-wide brain (PLAN.md §5). `--no-bump` suppresses the
retrieval_count / last_retrieved telemetry write; without it the default bump
still applies (explicit skill-invoked recall).

Drives the REAL store.py CLI via subprocess against a throwaway temp store — the
same path the hooks exercise — never the box store.

Run: python tests/test_no_bump.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable
NS = "project:nobumptest"


class NoBumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store}
        self._run("add", "--namespace", NS, "--type", "lesson",
                  "--content", "always run the linter before committing python code",
                  "--signal", "lint")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=30,
        )

    def _rc(self):
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT retrieval_count FROM memory WHERE superseded_at IS NULL"
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def _last_retrieved(self):
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT last_retrieved FROM memory WHERE superseded_at IS NULL"
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    # --- recall ------------------------------------------------------------
    def test_recall_no_bump_leaves_retrieval_count_unchanged(self):
        self.assertEqual(self._rc(), 0)
        before = self._last_retrieved()
        for _ in range(3):
            r = self._run("recall", "--query", "linter python", "--namespace", NS,
                          "--no-bump", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("linter", r.stdout)  # row IS returned, just not bumped
        self.assertEqual(self._rc(), 0)
        self.assertEqual(self._last_retrieved(), before)  # telemetry untouched

    def test_recall_default_bumps(self):
        self.assertEqual(self._rc(), 0)
        self._run("recall", "--query", "linter python", "--namespace", NS, "--json")
        self.assertEqual(self._rc(), 1)
        self.assertIsNotNone(self._last_retrieved())

    # --- recent ------------------------------------------------------------
    def test_recent_no_bump_leaves_retrieval_count_unchanged(self):
        self.assertEqual(self._rc(), 0)
        before = self._last_retrieved()
        for _ in range(3):
            r = self._run("recent", "--namespace", NS, "--no-bump", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("linter", r.stdout)
        self.assertEqual(self._rc(), 0)
        self.assertEqual(self._last_retrieved(), before)

    def test_recent_default_bumps(self):
        self.assertEqual(self._rc(), 0)
        self._run("recent", "--namespace", NS, "--json")
        self.assertEqual(self._rc(), 1)
        self.assertIsNotNone(self._last_retrieved())

    # --- mixed: no-bump reads never advance the count a later bump starts from
    def test_no_bump_then_bump(self):
        self._run("recall", "--query", "linter", "--namespace", NS, "--no-bump", "--json")
        self.assertEqual(self._rc(), 0)
        self._run("recall", "--query", "linter", "--namespace", NS, "--json")
        self.assertEqual(self._rc(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
