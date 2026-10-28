"""Census pin for the #244 store-isolation guard wiring.

PRR-005 (swarm-pr-review pr410-20261028): deleting ``import _store_guard``
from a store-touching test file was invisible to every test — the seam guard
only inspects files that still import it, and CI lanes run with the guard
silent (truthy CI carve-out). This test derives the store-touching census
the same way the original wiring round did (storelib import OR store.py /
store_py / STORE_PY reference) and asserts every such file carries the guard
import or the documented ``-m unittest`` sys.path shim.

Run: python tests/test_store_guard_wiring_census.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_RE = re.compile(r"^import _store_guard", re.M)
SHIM_MARKER = "_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))"
STORE_TOUCH_RE = re.compile(
    r"import storelib|from storelib import|sys\.modules\[.storelib"
    r"|spec_from_file_location\(.*store|store\.py|store_py|STORE_PY")
# Deliberately unwired (comment-only census matches, no store surface):
# a file is exempt when it carries this marker comment.
EXEMPT_MARKER = "#244-census-exempt"


class StoreGuardWiringCensusTest(unittest.TestCase):
    def test_every_store_touching_test_file_wires_the_guard(self):
        wired = 0
        missing = []
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            body = path.read_text(encoding="utf-8", errors="replace")
            if not STORE_TOUCH_RE.search(body):
                continue
            if GUARD_RE.search(body):
                wired += 1
                continue
            if SHIM_MARKER in body:
                # The -m unittest shim shape imports the guard after the
                # sys.path insert; the plain-import regex above misses it.
                wired += 1
                continue
            if EXEMPT_MARKER in body:
                continue
            missing.append(path.name)
        self.assertGreater(wired, 100,
                           "census collapsed — the derivation drifted")
        self.assertEqual(
            missing, [],
            "store-touching test files without the #244 isolation guard "
            "(wire them like tests/test_backup.py, or mark a comment-only "
            f"census match with {EXEMPT_MARKER}): {missing}")

    def test_conftest_carries_the_guard(self):
        body = (REPO_ROOT / "tests" / "conftest.py").read_text(
            encoding="utf-8")
        self.assertIn("import _store_guard", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
