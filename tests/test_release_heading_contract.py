"""Release-gate CHANGELOG heading-contract tests (issue #233).

The heading-contract problem this guards against: the gate resolved "the
newest release" as the FIRST `## [X.Y.Z]` heading in file order and compared
only that version against the manifests, silently trusting two structural
invariants nothing enforced — at most one heading per released version, and
strictly-descending newest-first heading order. Both were violated in
shipped history (#214's duplicate `## [0.43.0]` sections; PR #230's
retitle-in-place ordering failure). The fix under test:
scripts/release_gate.py `_changelog_heading_violations`, wired into the
default gate path before the manifest comparison.

Run: python tests/test_release_heading_contract.py  (no pytest; house
convention; CI runs each tests/test_*.py file directly, so this module
must keep its __main__ footer).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Spec-load (no sys.path pollution; the module lives outside the package).
_spec = importlib.util.spec_from_file_location("zmem_release_gate_headings", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _heading_line(text: str, match: re.Match) -> int:
    """1-based line number of a regex match."""
    return text.count("\n", 0, match.start()) + 1


class HeadingContractUnitTest(unittest.TestCase):
    """Pure-function coverage of the heading-contract validation."""

    def _duplicate_text(self) -> str:
        return (
            "# Changelog\n\n## [Unreleased]\n\n- nothing yet\n\n"
            "## [2.0.0] - 2026-01-02\n\nfirst body\n\n"
            "## [2.0.0] - 2026-01-03\n\nsecond body\n\n"
            "## [1.0.0] - 2026-01-01\n\nold body\n"
        )

    def test_duplicate_heading_diagnostic(self):
        text = self._duplicate_text()
        violations = gate._changelog_heading_violations(text)
        dupes = [v for v in violations if "duplicate" in v]
        self.assertEqual(len(dupes), 1, f"want exactly one duplicate diagnostic, got {violations}")
        first = _heading_line(text, list(gate.SECTION_RE.finditer(text))[0])
        second = _heading_line(text, list(gate.SECTION_RE.finditer(text))[1])
        self.assertIn(f"{first}", dupes[0], "diagnostic must name the first line number")
        self.assertIn(f"{second}", dupes[0], "diagnostic must name the second line number")
        self.assertIn("2.0.0", dupes[0], "diagnostic must name the version")

    def test_misordered_heading_diagnostic(self):
        text = (
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\nolder body\n\n"
            "## [2.0.0] - 2026-01-02\n\nnewer body, placed last\n"
        )
        violations = gate._changelog_heading_violations(text)
        ordered = [v for v in violations if "descending" in v or "newest" in v]
        self.assertEqual(len(ordered), 1, f"want exactly one ordering diagnostic, got {violations}")
        self.assertIn("1.0.0", ordered[0], "diagnostic must name the older version")
        self.assertIn("2.0.0", ordered[0], "diagnostic must name the newer version")

    def test_equal_adjacent_pair_is_both_defects(self):
        text = "## [0.1.0] - 2026-01-01\n\nbody\n\n## [0.1.0] - 2026-01-02\n\nbody\n"
        violations = gate._changelog_heading_violations(text)
        self.assertEqual(len(violations), 2,
                         "an equal adjacent pair is BOTH a duplicate and a "
                         f"non-descending pair, got {violations}")
        self.assertTrue(any("duplicate" in v for v in violations))
        self.assertTrue(any("descending" in v or "newest" in v for v in violations))

    def test_wellformed_changelog_has_no_violations(self):
        text = (
            "# Changelog\n\n## [Unreleased]\n\n- nothing yet\n\n"
            "## [2.1.0] - 2026-01-03\n\nnewest\n\n"
            "## [2.0.0] - 2026-01-02\n\nmid\n\n"
            "## [1.9.3] - 2026-01-01\n\nold\n"
        )
        self.assertEqual(gate._changelog_heading_violations(text), [])

    def test_nonadjacent_duplicate_gets_both_defects(self):
        # V, W, V with W < V: the duplicate diagnostic names both V lines
        # and the ordering diagnostic names the W->V pair (PR #237 review).
        text = (
            "## [2.0.0] - 2026-01-03\n\nnewest\n\n"
            "## [1.0.0] - 2026-01-02\n\nmid\n\n"
            "## [2.0.0] - 2026-01-01\n\noldest duplicate\n"
        )
        violations = gate._changelog_heading_violations(text)
        self.assertEqual(len([v for v in violations if "duplicate" in v]), 1)
        self.assertEqual(len([v for v in violations if "descending" in v]), 1)
        dup = next(v for v in violations if "duplicate" in v)
        self.assertIn("lines 1 and 9", dup)

    def test_triple_duplicate_lists_all_lines(self):
        # >=3-way cluster: the duplicate diagnostic enumerates every line.
        text = (
            "## [1.0.0] - 2026-01-01\n\na\n\n"
            "## [1.0.0] - 2026-01-02\n\nb\n\n"
            "## [1.0.0] - 2026-01-03\n\nc\n"
        )
        violations = gate._changelog_heading_violations(text)
        dup = next(v for v in violations if "duplicate" in v)
        self.assertIn("lines 1 and 5 and 9", dup)

    def test_zero_padded_version_pair_fires_ordering_only(self):
        # 1.02.0 and 1.2.0 are numerically equal but string-distinct, so the
        # duplicate diagnostic does not fire and the ordering diagnostic
        # does (pinned PR #237 review behavior; still exits 1 either way).
        text = (
            "## [1.02.0] - 2026-01-02\n\nfirst\n\n"
            "## [1.2.0] - 2026-01-01\n\nsecond\n"
        )
        violations = gate._changelog_heading_violations(text)
        self.assertEqual([], [v for v in violations if "duplicate" in v])
        self.assertEqual(len([v for v in violations if "descending" in v]), 1)


class HeadingContractGatePathTest(unittest.TestCase):
    """The default gate path rejects contract violations before the
    manifest comparison (issue #233: a heading violation makes 'the newest
    section' ill-defined, so the check must fire first)."""

    def _gate_rc(self, text: str) -> tuple[int, str]:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8", newline="\n")
        original_path = gate.CHANGELOG_PATH
        original_discover = gate.discover_manifests
        original_read_version = gate.read_version
        try:
            tmp.write(text)
            tmp.close()
            gate.CHANGELOG_PATH = Path(tmp.name)
            # Isolate from the developer's tree (PR #237 review): a broken or
            # half-bumped live manifest set must not confound the assertion.
            gate.discover_manifests = lambda repo_root=None: ["m1", "m2", "m3", "m4", "m5"]
            gate.read_version = lambda rel_path, repo_root=None: "9.9.9"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = gate.main([])
        finally:
            gate.CHANGELOG_PATH = original_path
            gate.discover_manifests = original_discover
            gate.read_version = original_read_version
            try:
                Path(tmp.name).unlink()
            except OSError:
                pass
        return rc, buffer.getvalue()

    def test_duplicate_heading_fails_the_gate(self):
        rc, out = self._gate_rc(
            "## [9.9.9] - 2026-01-02\n\nbody a\n\n"
            "## [9.9.9] - 2026-01-03\n\nbody b\n"
        )
        self.assertEqual(rc, 1, f"gate must exit 1 on a duplicated heading, got {rc}; out={out}")
        self.assertIn("duplicate", out.lower())
        self.assertIn("::error::", out)
        # Assertion precision (PR #237 review): the diagnostic must name the
        # duplicated version and BOTH heading line numbers.
        self.assertIn("[9.9.9]", out)
        self.assertIn("lines 1 and 5", out)

    def test_misordered_heading_fails_the_gate(self):
        rc, out = self._gate_rc(
            "## [1.0.0] - 2026-01-01\n\nolder body\n\n"
            "## [2.0.0] - 2026-01-02\n\nnewer body, placed last\n"
        )
        self.assertEqual(rc, 1, f"gate must exit 1 on newest-last placement, got {rc}; out={out}")
        self.assertIn("descending", out.lower())
        # Assertion precision (PR #237 review): both versions named, older
        # first (the pair is reported in file order).
        self.assertIn("[1.0.0] (line 1)", out)
        self.assertIn("[2.0.0] (line 5)", out)

    def test_unreadable_changelog_fails_clean(self):
        # PR #237 review R7: a missing/unreadable CHANGELOG must produce a
        # clean ::error:: + exit 1, not an uncaught traceback.
        original_path = gate.CHANGELOG_PATH
        original_discover = gate.discover_manifests
        original_read_version = gate.read_version
        try:
            gate.CHANGELOG_PATH = Path("N:/nonexistent/zmem/CHANGELOG.md")
            gate.discover_manifests = lambda repo_root=None: ["m1", "m2", "m3", "m4", "m5"]
            gate.read_version = lambda rel_path, repo_root=None: "9.9.9"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                rc = gate.main([])
        finally:
            gate.CHANGELOG_PATH = original_path
            gate.discover_manifests = original_discover
            gate.read_version = original_read_version
        self.assertEqual(rc, 1)
        self.assertIn("::error::", buffer.getvalue())
        self.assertIn("unreadable", buffer.getvalue().lower())


class RepairedRepoChangelogTest(unittest.TestCase):
    """AC3 guard: the real repo CHANGELOG (post-#214 repair) satisfies the
    heading contract and keeps exactly one 0.43.0 heading."""

    def test_repo_changelog_passes_heading_contract(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        violations = gate._changelog_heading_violations(text)
        self.assertEqual(violations, [], "repo CHANGELOG must satisfy the heading contract")

    def test_exactly_one_0430_heading(self):
        text = CHANGELOG.read_text(encoding="utf-8")
        count = len(re.findall(r"^## \[0\.43\.0\]", text, re.MULTILINE))
        self.assertEqual(count, 1, f"want exactly one 0.43.0 heading, found {count}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
