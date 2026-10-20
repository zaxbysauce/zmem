"""Release-gate smoke wrappers for the real repository (issue-tracer
release-gate-integrity trace, PRESERVING checks C10/C11).

Runs the exact commands the issues pin — `python scripts/release_gate.py`
(#364 AC3) and `python scripts/release_gate.py --verify-manifest` — as
subprocesses against the real repo root, so the executable contract stays
byte-faithful while giving the frozen check a stable driver file.

Run: python tests/test_release_gate_smoke.py   (no pytest — repo convention)
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "release_gate.py"
PYTHON = sys.executable


class DefaultGateGreenTest(unittest.TestCase):
    """AC10 (#364 AC3): the default gate resolves the current version, exit 0."""

    def test_default_gate_resolves_version(self):
        r = subprocess.run(
            [PYTHON, str(GATE)], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        out = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 0,
            f"default release gate must stay green on the real repo: {out}",
        )
        self.assertIn("resolved", out)


class VerifyManifestFreshTest(unittest.TestCase):
    """AC11: --verify-manifest stays green with the re-emitted manifest."""

    def test_verify_manifest_reports_fresh(self):
        r = subprocess.run(
            [PYTHON, str(GATE), "--verify-manifest"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        out = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 0,
            f"--verify-manifest must stay green on the real repo: {out}",
        )
        self.assertIn("release manifest fresh", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
