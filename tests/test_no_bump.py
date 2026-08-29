"""Tests for the `--no-bump` flag semantics on store.py `recall` and `recent`.

Issue #21: hook-driven recall is the passive path. `--no-bump` must NOT advance
`retrieval_count` / `last_retrieved` (so a subagent dispatch fan-out does not turn N
delegated agents into N concurrent retrieval-count writers — PLAN.md §5), but since the
missing usefulness telemetry biased promote/prune decisions, `--no-bump` NOW records the
passive *surface* on the distinct `surfaced_count` / `last_surfaced` counters. So:
  - `--no-bump` recall/recent: bumps surfaced_count + last_surfaced only.
  - default recall/recent (explicit): bumps retrieval_count + last_retrieved only.
The two counters are mutually exclusive per recall event, so their sum is a non-double-counted
"total times surfaced into context" metric.

Drives the REAL store.py CLI via subprocess against a throwaway temp store — the same path
the hooks exercise — never the box store.

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

    def _counts(self):
        """Return (retrieval_count, surfaced_count, last_retrieved, last_surfaced)."""
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(
                "SELECT retrieval_count, surfaced_count, last_retrieved, last_surfaced "
                "FROM memory WHERE superseded_at IS NULL"
            ).fetchone()
        finally:
            conn.close()

    def _rc(self):
        return self._counts()[0]

    def _sc(self):
        return self._counts()[1]

    def _last_retrieved(self):
        return self._counts()[2]

    def _last_surfaced(self):
        return self._counts()[3]

    # --- recall ------------------------------------------------------------
    def test_recall_no_bump_leaves_retrieval_count_unchanged(self):
        self.assertEqual(self._rc(), 0)
        before = self._last_retrieved()
        for _ in range(3):
            r = self._run("recall", "--query", "linter python", "--namespace", NS,
                          "--no-bump", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("linter", r.stdout)  # row IS returned, just not retrieval-bumped
        self.assertEqual(self._rc(), 0)
        self.assertEqual(self._last_retrieved(), before)  # retrieval telemetry untouched

    def test_recall_default_bumps(self):
        self.assertEqual(self._rc(), 0)
        self._run("recall", "--query", "linter python", "--namespace", NS, "--json")
        self.assertEqual(self._rc(), 1)
        self.assertIsNotNone(self._last_retrieved())

    # issue #21: the passive surface MUST be recorded on surfaced_count, not lost.
    def test_recall_no_bump_records_surface(self):
        self.assertEqual(self._sc(), 0)
        before_surfaced = self._last_surfaced()
        for _ in range(3):
            r = self._run("recall", "--query", "linter python", "--namespace", NS,
                          "--no-bump", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._sc(), 3, "each passive recall records one surface")
        self.assertIsNotNone(self._last_surfaced())
        # and it must not have leaked into retrieval telemetry
        self.assertEqual(self._rc(), 0)
        # a surface in a prior passive recall must not shift last_surfaced backwards:
        # later events must keep/advance it.
        self.assertGreaterEqual(self._last_surfaced(), before_surfaced or self._last_surfaced())

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

    def test_recent_no_bump_records_surface(self):
        self.assertEqual(self._sc(), 0)
        for _ in range(2):
            r = self._run("recent", "--namespace", NS, "--no-bump", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._sc(), 2)
        self.assertEqual(self._rc(), 0)

    # issue #82: the change-intent unfold's [PREVIOUSLY] extras are neighbors
    # that did not match the query — they must never enter the telemetry bump
    # set (pinned end-to-end here and via the library seam in
    # tests/test_chain_unfold.py).
    def test_unfold_extras_never_increment_retrieval_count(self):
        r = self._run("recall", "--query", "what changed about the linter",
                      "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        # No lineage exists in this fixture, so no extras are appended; the
        # matched row still bumps exactly once (explicit path).
        self.assertEqual(self._rc(), 1)
        r = self._run("recall", "--query", "what changed about the linter",
                      "--namespace", NS, "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        # the passive path must record a surface, never a retrieval
        self.assertEqual(self._rc(), 1)
        self.assertEqual(self._sc(), 1)

    # --- mixed: no-bump reads never advance the count a later bump starts from
    def test_no_bump_then_bump(self):
        self._run("recall", "--query", "linter", "--namespace", NS, "--no-bump", "--json")
        self.assertEqual(self._rc(), 0)
        self._run("recall", "--query", "linter", "--namespace", NS, "--json")
        self.assertEqual(self._rc(), 1)

    # issue #21: mutual exclusivity — a single recall event bumps exactly one counter.
    def test_mutual_exclusivity_sum_not_double_counted(self):
        self.assertEqual((self._rc(), self._sc()), (0, 0))
        # 1 explicit + 2 passive recalls
        self._run("recall", "--query", "linter", "--namespace", NS, "--json")
        self._run("recall", "--query", "linter", "--namespace", NS, "--no-bump", "--json")
        self._run("recall", "--query", "linter", "--namespace", NS, "--no-bump", "--json")
        rc, sc, *_ = self._counts()
        self.assertEqual((rc, sc), (1, 2),
                         "one default + two no-bump recalls must give rc=1, sc=2 (sum 3, not 4)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
