"""Structural guard for the `python -m unittest` import seam (issue #244).

CI runs some suites via `python -m unittest tests/<file>.py`, which puts the
REPO ROOT on sys.path — a plain `import _store_guard` inside such a file
resolves under direct execution but NOT under the module seam (ModuleNotFoundError
kills the whole step; hit three times on PR #410's CI rounds). This test parses
ci.yml's own `-m unittest` invocations and fails when any file they name carries
the plain guard import without the file-location sys.path shim — so a fourth
seam can never land silently.

Run: python tests/test_unittest_seam_coverage.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAIN_GUARD = re.compile(r"^import _store_guard", re.M)
SHIM_MARKER = "_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))"


class UnittestSeamCoverageTest(unittest.TestCase):
    def test_every_m_unittest_file_is_seam_safe(self):
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        files = sorted(set(re.findall(
            r"-m unittest[^\n]*?((?:tests/test_[\w.]+\.py)(?:[^\n]*?tests/test_[\w.]+\.py)*)",
            ci.encode().decode())))
        named = set()
        for chunk in files:
            named.update(re.findall(r"tests/test_[\w.]+\.py", chunk))
        self.assertTrue(named, "no -m unittest steps found — parser drifted?")
        bad = []
        for rel in sorted(named):
            path = REPO_ROOT / rel
            if not path.exists():
                bad.append(f"{rel}: named in ci.yml but absent")
                continue
            body = path.read_text(encoding="utf-8")
            if PLAIN_GUARD.search(body) and SHIM_MARKER not in body:
                bad.append(
                    f"{rel}: plain `import _store_guard` without the "
                    "file-location shim — ModuleNotFoundError under "
                    "`python -m unittest` (repo root on sys.path)")
                continue
            if PLAIN_GUARD.search(body):
                # Order matters (critic round 3): the shim must EXECUTE
                # before the import, so its offset must precede it —
                # membership alone is order-blind.
                if body.find(SHIM_MARKER) > body.find("import _store_guard"):
                    bad.append(
                        f"{rel}: shim appears AFTER `import _store_guard` — "
                        "the import executes first and still dies under the "
                        "-m seam")
        self.assertEqual(
            bad, [],
            "Files run via `python -m unittest` need the shim shape "
            "(see tests/test_training_views.py):\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
