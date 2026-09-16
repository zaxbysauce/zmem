"""Acceptance checks for issue #182's release surfaces (AC7).

These checks intentionally pin the seven host-facing manifests and the
generated release manifest to the next unused minor release.  The score-margin
release note remains pinned to its historical 0.34.0 section after the host
attribution release advances the current release identity to 0.38.0.  The release
manifest is trusted only when the existing release gate verifies it
successfully; this test never regenerates it as a side effect.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_VERSION = "0.43.0"
SCORE_MARGIN_VERSION = "0.34.0"
_SCORE_MARGIN_VERSION_RE = re.escape(SCORE_MARGIN_VERSION)

RELEASE_SECTION_RE = re.compile(
    rf"^## \[{_SCORE_MARGIN_VERSION_RE}\][^\n]*[-—]\s*(\d{{4}}-\d{{2}}-\d{{2}})\s*$",
    re.MULTILINE,
)
RELEASE_GATE = REPO_ROOT / "scripts" / "release_gate.py"

_SPEC = importlib.util.spec_from_file_location("zmem_release_gate", RELEASE_GATE)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


class ScoreMarginReleaseAcceptanceTest(unittest.TestCase):
    """Issue #182 AC7: release metadata is complete and gate-valid."""

    def test_exact_host_facing_manifests_declare_next_unused_minor(self):
        manifests = getattr(gate, "HOST_MANIFESTS", None)
        if manifests is None:
            manifests = gate.discover_manifests()
        manifests = tuple(manifests)
        self.assertEqual(
            len(manifests),
            7,
            "exactly seven host-facing manifests must participate in the release",
        )

        for relative in manifests:
            path = REPO_ROOT / relative
            self.assertTrue(path.is_file(), f"required host-facing manifest is missing: {relative}")
            self.assertEqual(
                gate.read_version(relative),
                EXPECTED_VERSION,
                f"{relative} must declare the next unused minor {EXPECTED_VERSION}",
            )

    def test_changelog_has_dated_score_margin_opt_in_section(self):
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        match = RELEASE_SECTION_RE.search(changelog)
        self.assertIsNotNone(
            match,
            f"CHANGELOG.md must contain a dated ## [{SCORE_MARGIN_VERSION}] release section",
        )
        assert match is not None
        # PR #198 (issue #121) merged main and took the NEXT minor: the
        # score-margin feature shipped in ## [0.34.0] (issue #182), so the
        # opt-in documentation is anchored to THAT section, not to the
        # current release section.
        section_start = changelog.find("## [0.34.0]")
        assert section_start >= 0
        section_end = changelog.find("\n## ", section_start)
        body = (changelog[section_start:]
                if section_end < 0 else changelog[section_start:section_end])
        self.assertIn(
            "ZMEM_INJECT_MARGIN",
            body,
            f"the {SCORE_MARGIN_VERSION} CHANGELOG section must document the score-margin opt-in",
        )
        self.assertIn(
            "score-margin",
            body.lower(),
            f"the {SCORE_MARGIN_VERSION} CHANGELOG section must name the score-margin feature",
        )
        self.assertRegex(
            body.lower(),
            r"opt[- ]?in",
            f"the {SCORE_MARGIN_VERSION} CHANGELOG section must explain that score-margin is opt-in",
        )

    def test_release_manifest_is_current_and_verified_by_existing_gate(self):
        path = REPO_ROOT / "release-manifest.json"
        self.assertTrue(path.is_file(), "release-manifest.json is required for a release")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest.get("version"),
            EXPECTED_VERSION,
            f"release-manifest.json must declare {EXPECTED_VERSION}",
        )

        result = subprocess.run(
            [sys.executable, str(RELEASE_GATE), "--verify-manifest"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            "release-manifest.json is accepted only after the existing release "
            f"gate verifies the generated manifest:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
