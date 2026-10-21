"""Multi-section [Unreleased] semantics for release_gate.unreleased_body
(release-gate-integrity trace, Phase 4 named-mutation finding M6).

The frozen C8/C9 checks drive check_unreleased_drift and
unreleased_has_content; the mutation probe M6 showed that
unreleased_body's first-NON-EMPTY selection across multiple headings was
otherwise unpinned. This file pins it (additive hardening in a NEW file per
the frozen-file discipline — tests/test_release_gate_unreleased.py is
byte-frozen).

Run: python tests/test_release_unreleased_multisection.py  (no pytest)
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"

_spec = importlib.util.spec_from_file_location("zmem_release_gate_ms", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

TWO_SECTIONS_SECOND_CARRIES = (
    "# Changelog\n\n"
    "## [Unreleased]\n\n"
    "## [0.9.0] - 2026-01-01\n\n"
    "old release\n\n"
    "## [Unreleased]\n\n"
    "- second-section entry\n"
)
TWO_SECTIONS_FIRST_CARRIES = (
    "# Changelog\n\n"
    "## [Unreleased]\n\n"
    "- first-section entry\n\n"
    "## [0.9.0] - 2026-01-01\n\n"
    "old release\n\n"
    "## [Unreleased]\n\n"
)


class UnreleasedBodyMultiSectionTest(unittest.TestCase):
    def test_body_comes_from_first_section_that_carries_one(self):
        body = gate.unreleased_body(TWO_SECTIONS_SECOND_CARRIES)
        self.assertIn("second-section entry", body)
        self.assertNotIn("old release", body)

    def test_first_carrying_section_wins_when_earlier_one_has_content(self):
        body = gate.unreleased_body(TWO_SECTIONS_FIRST_CARRIES)
        self.assertIn("first-section entry", body)
        self.assertNotIn("old release", body)

    def test_all_empty_sections_yield_first_raw_body(self):
        # No section carries a non-blank line: the FIRST section's raw body
        # is returned (structural newline included), preserving the frozen
        # single-heading whitespace-raw contract; "" only when no heading.
        text = "# C\n\n## [Unreleased]\n"  # trailing heading, raw body "\n"
        self.assertEqual(gate.unreleased_body(text), "\n")
        self.assertEqual(gate.unreleased_body("# C\n\n## [0.1.0] - x\n"), "")

    def test_single_heading_whitespace_body_returned_raw(self):
        # The pre-existing single-heading contract (test_release_parity
        # pins the same shape): a whitespace-only body is NOT collapsed.
        text = "# C\n\n## [Unreleased]\n\n   \n\t\n\n## [0.1.0] - 2026-01-01\n"
        body = gate.unreleased_body(text)
        self.assertNotEqual(body, "")
        self.assertTrue(body.strip() == "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
