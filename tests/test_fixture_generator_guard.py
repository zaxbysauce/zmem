"""Fixture-generator guard tests (issue #360, plan-critic R6).

The migration-fixture generator must refuse --input/--store paths inside
the tracked tests/fixtures/ tree without the explicit --allow-tracked
unlock, and permit them with it (the documented regeneration workflow).

Run: python tests/test_fixture_generator_guard.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN = REPO_ROOT / "tests" / "fixtures" / "entity" / \
    "generate_migration_expected.py"


class GeneratorGuardTest(unittest.TestCase):
    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(GEN), *extra],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=120, env=dict(os.environ))

    def test_tracked_input_refused_without_flag(self):
        proc = self._run(
            "--input", "tests/fixtures/entity/migration-input.jsonl",
            "--out", os.devnull, "--phase", "seed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-tracked", proc.stderr)

    def test_tracked_store_refused_without_flag(self):
        proc = self._run(
            "--input", "nonexistent-scratch-input.jsonl",
            "--store", "tests/fixtures/entity/store.sqlite",
            "--out", os.devnull, "--phase", "seed")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--allow-tracked", proc.stderr)

    def test_tracked_paths_permitted_with_flag(self):
        # With the flag the guard itself passes; the run may still fail on
        # unrelated preconditions (e.g. the store already exists), but it
        # must NOT be the guard refusing.
        proc = self._run(
            "--input", "tests/fixtures/entity/migration-input.jsonl",
            "--out", os.devnull, "--phase", "seed",
            "--allow-tracked")
        self.assertNotIn("--allow-tracked", proc.stderr)


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
