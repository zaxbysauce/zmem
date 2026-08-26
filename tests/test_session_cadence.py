"""Tests for store.py's `session-cadence` subcommand (#39 E9).

The session-start hook used to spawn three separate detached python
interpreters (consolidate, backup --if-due, sweep). `session-cadence` batches
them into one process. Each op must keep its EXACT standalone semantics:
organize's shared cadence gate + single-flight lock (the sleep-time op is
ORGANIZE since issue #62 7.7), backup's --if-due gate, and sweep's
store-independent file reaping.

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


# Post-split (issue #57) `main()` dispatch calls cli's own by-value
# `organize` import; patch that namespace, not the store shim.
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
import importlib as _ii
_cli_mod = _ii.import_module("storelib.cli")
PYTHON = sys.executable


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    # Hermetic organize/gate knobs (Claude Code round 4 env-isolation gap): a
    # host-leaked value here would silently flip cadence-gate expectations —
    # the two gate tests below REQUIRE the documented defaults.
    for k in ("ZMEM_ORGANIZE_IDLE_HOURS", "ZMEM_ORGANIZE_EPISODE_BOUND",
              "ZMEM_KEEPER_COMPRESS_CHARS", "ZMEM_UNRECALLED_DAYS",
              "ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS",
              "ZMEM_CONSOLIDATE_GROWTH_THRESHOLD"):
        env.pop(k, None)
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
        """A fresh store: session-cadence runs organize, backup, sweep and
        prints a one-line summary naming all three (the sleep-time op is
        ORGANIZE since issue #62 7.7, not consolidate)."""
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[zmem] session-cadence:", r.stdout)
        # All three ops are named in the summary line.
        self.assertIn("organize:", r.stdout)
        self.assertIn("backup:", r.stdout)
        self.assertIn("sweep:", r.stdout)

    def test_second_run_is_cadence_noop(self):
        """Hard assertion (critic-required; cubic#76 + Claude Code round 4):
        on a store WITH a live memory, run 1's organize does real work and its
        step-6 consolidate writes the shared clock; run 2's organize must then
        be GATED — "skipped by cadence gate", never the empty-episode no-op
        fallback. (The retired empty-store variant short-circuited at the
        empty-episode check before the gate was ever exercised — deleting the
        entire gate block left it green, i.e. it proved nothing.)"""
        self._run("init")
        r = self._run("add", "--namespace", "project:cadence-probe",
                      "--type", "fact", "--content", "cadence gate probe row")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(first.returncode, 0, first.stderr)
        # The first run records last_backup (backup ran on a fresh store) and
        # organize's internal consolidate wrote the shared cadence clock.
        self.assertIn("backup: snapshot", first.stdout + first.stderr)

        second = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(second.returncode, 0, second.stderr)
        combined = second.stdout + second.stderr
        # backup --if-due must skip on the immediate second run.
        self.assertIn("not due", combined,
                      "second run's backup must be skipped by the --if-due gate")
        # organize's shared cadence gate must skip the immediate re-run — the
        # clock run 1 armed is armed, days~0 < 7 and growth ~0 < 20%.
        self.assertIn("skipped by cadence gate", combined,
                      "second run's organize MUST hit the shared cadence gate "
                      f"(run 1 armed the clock). got: {combined}")

    def test_consolidate_arms_clock_gates_session_cadence_organize(self):
        """REVERSE gate direction (Claude Code round 4 coverage asymmetry:
        only organize→consolidate was ever tested, never consolidate→organize
        — the same direction as the F-009 Hermes gap this round fixes). An
        explicit ``consolidate --force`` writes the shared meta key; the next
        session-cadence's organize must respect it: two entry points, one
        clock."""
        self._run("init")
        r = self._run("add", "--namespace", "project:cadence-probe",
                      "--type", "fact", "--content", "reverse gate probe row")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("consolidate", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc = self._run("session-cadence", "--backup-retention", "7")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        combined = rc.stdout + rc.stderr
        self.assertIn("skipped by cadence gate", combined,
                      f"a just-run consolidate must gate session-cadence's "
                      f"organize (shared clock, one maintenance act); "
                      f"got: {combined}")

    def test_backup_retention_flag_actually_prunes(self):
        """The --backup-retention flag reaches cmd_backup THROUGH session-cadence
        AND is applied: with retention=1 and the if-due gate forced open
        (ZMEM_BACKUP_INTERVAL_DAYS=0), only 1 backup is retained after
        session-cadence runs. This exercises the argv→cmd_backup plumbing for
        real, not just the summary string (PRR-005)."""
        self._run("init")
        # Seed 2 snapshots with a high retention so they all survive seeding.
        self._run("backup", "--retention", "9")
        self._run("backup", "--retention", "9")
        backup_dir = os.path.join(self.tmp, "backups")
        seeded = [f for f in os.listdir(backup_dir) if f.endswith(".sqlite")] \
            if os.path.isdir(backup_dir) else []
        self.assertGreaterEqual(len(seeded), 2,
                                f"expected >= 2 seeded snapshots, got {seeded}")
        # Now run session-cadence with retention=1 and the if-due gate forced
        # open (interval 0 = always due), so the cadence backup actually runs
        # and prunes to 1. This is the path PRR-005 targets.
        env = {**self.env, "ZMEM_BACKUP_INTERVAL_DAYS": "0"}
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "session-cadence", "--backup-retention", "1"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        # After retention=1 via session-cadence, at most 1 snapshot remains.
        snapshots = [f for f in os.listdir(backup_dir) if f.endswith(".sqlite")] \
            if os.path.isdir(backup_dir) else []
        self.assertLessEqual(len(snapshots), 1,
                             f"session-cadence --backup-retention 1 should prune "
                             f"to <= 1 snapshot, got {len(snapshots)}: {snapshots}")

    def test_one_op_error_does_not_abort_others_inprocess(self):
        """Inject a REAL error into organize, drive main() in-process (so the
        production dispatch, failure counter, and exit code are the code under
        test — not a re-typed replica), and confirm: (a) organize reports
        'error' in the summary, (b) backup still runs, (c) the process exits
        nonzero (SystemExit code 1) per PRR-003/006."""
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location(
            "_zmem_cadence_err", str(STORE_PY))
        mod = importlib.util.module_from_spec(spec)

        # Scope env mutations so they are restored after the test (cubic-re #4:
        # never leak process-global os.environ changes to sibling tests in a
        # shared-process run).
        env_overrides = {
            "ZMEM_STORE": self.store,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            spec.loader.exec_module(mod)

            # Initialize the store.
            conn = mod.connect()
            try:
                mod.init_db(conn)
                mod.migrate(conn)
            finally:
                conn.close()

            # Monkeypatch organize to raise — simulating a real op failure.
            original_organize = _cli_mod.organize

            def _boom(*a, **kw):
                raise RuntimeError("injected organize failure for test")
            _cli_mod.organize = _boom

            # Drive main() in-process via sys.argv so the REAL dispatch + failure
            # counter + sys.exit(1) path are exercised (PRR-003/006).
            import io
            orig_argv = sys.argv
            captured = io.StringIO()
            sys.argv = ["store.py", "session-cadence", "--backup-retention", "7"]
            exit_code = None
            try:
                with __import__("contextlib").redirect_stdout(captured), \
                     __import__("contextlib").redirect_stderr(captured):
                    try:
                        mod.main()
                    except SystemExit as e:
                        exit_code = e.code
            finally:
                sys.argv = orig_argv
                _cli_mod.organize = original_organize

            output = captured.getvalue()
            # (a) organize error reported in the summary
            self.assertIn("organize: error", output,
                          f"organize error not reported in: {output!r}")
            # (b) backup still ran despite the organize failure
            self.assertIn("backup:", output,
                          f"backup did not run after organize error: {output!r}")
            # (c) the process exited nonzero (PRR-003: failures → sys.exit(1))
            self.assertEqual(exit_code, 1,
                             f"expected exit code 1 on op failure, got {exit_code}. "
                             f"output: {output!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
