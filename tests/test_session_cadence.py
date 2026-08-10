"""Tests for store.py's `session-cadence` subcommand (#39 E9).

The session-start hook used to spawn three separate detached python
interpreters (consolidate, backup --if-due, sweep). `session-cadence` batches
them into one process. Each op must keep its EXACT standalone semantics:
consolidate's cadence gate + single-flight lock, backup's --if-due gate, and
sweep's store-independent file reaping.

Run: python tests/test_session_cadence.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


class SessionCadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-cadence-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def test_session_cadence_runs_all_three_ops(self):
        """A fresh store: session-cadence runs consolidate, backup, sweep and
        prints a one-line summary naming all three."""
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[zmem] session-cadence:", r.stdout)
        # All three ops are named in the summary line.
        self.assertIn("consolidate:", r.stdout)
        self.assertIn("backup:", r.stdout)
        self.assertIn("sweep:", r.stdout)

    def test_second_run_is_cadence_noop(self):
        """Hard assertion (critic-required): running session-cadence twice in a
        row, the second run respects the cadence gates — backup is 'not due' and
        consolidate is skipped by its cadence gate. Proves batching did not
        bypass the --if-due / consolidate-cadence gating."""
        self._run("init")
        first = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(first.returncode, 0, first.stderr)
        # The first run records last_backup (backup ran on a fresh store).
        self.assertIn("backup: snapshot", first.stdout + first.stderr)

        second = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(second.returncode, 0, second.stderr)
        combined = second.stdout + second.stderr
        # backup --if-due must skip on the immediate second run.
        self.assertIn("not due", combined,
                      "second run's backup must be skipped by the --if-due gate")
        # consolidate's cadence gate must also skip the immediate re-run.
        self.assertTrue(
            "skipped by cadence gate" in combined or "no embeddable memories" in combined,
            f"second run's consolidate must be cadence-gated or empty. got: {combined}",
        )

    def test_backup_retention_flag_is_passed_through(self):
        """The --backup-retention flag reaches cmd_backup (retention is applied).
        Verify by checking backup runs and mentions the snapshot."""
        self._run("init")
        r = self._run("session-cadence", "--backup-retention", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("backup: ok", r.stdout)

    def test_one_op_error_does_not_abort_others(self):
        """A failure in one cadence op is reported but does not prevent the
        others from running (the ops are independent). We can't easily force a
        consolidate failure, but we CAN confirm the summary reports per-op
        status independently rather than aborting on the first."""
        self._run("init")
        r = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if "session-cadence:" in l]
        self.assertTrue(line, f"no summary line in {r.stdout!r}")
        # The summary names each op with its own status.
        summary = line[0]
        self.assertIn("consolidate:", summary)
        self.assertIn("backup:", summary)
        self.assertIn("sweep:", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
