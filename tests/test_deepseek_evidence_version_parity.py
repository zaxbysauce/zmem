"""AC6 (#416-3): the deepseek host-gate harness must cross-check the
EVIDENCE artifact versions (frozen acceptance check C6).

Contract this check pins (issue #416 item 3, to be implemented in
tests/support/deepseek_assertions.py — this file authors AGAINST the
contract only and never modifies the support module):

(i) ``support_report_problems`` gains an ``expected_version=None``
    keyword: None preserves today's checks; a STRING makes a mismatching
    ``report["zmem_version"]`` append a problem naming ``zmem_version``
    and the expected version.
(ii) a new ``evidence_version_problems(canary_report, support_report,
    release_version)`` returns a list of divergence descriptions
    (canary vs support, support vs release, canary vs release as
    applicable).

Legs (version facts at the freeze anchor; the LIVE leg reads whatever the
committed artifacts + release-manifest actually hold — it pins the DETECTION
contract, not a specific stale triple; the t14 disclosure-ratchet owns the
tolerated state and its retirement):
1. LIVE divergence — the COMMITTED canary-report, support-report and
   release-manifest disagree; the new bindings must report it.  At base
   neither the kwarg nor the function exists -> TypeError/AttributeError
   -> RED (nothing caught).
2. synthetic agreement — fixture reports whose versions all equal the
   release version report NO version problems.
3. synthetic divergence in each direction — the odd version out is
   reported, and the support kwarg names zmem_version + expected version.

Pure file reads + pure functions; no env, no store, no network.

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_deepseek_evidence_version_parity.py``
"""

from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SUPPORT_DIR = REPO_ROOT / "tests" / "support"
if str(_SUPPORT_DIR) not in sys.path:
    sys.path.insert(0, str(_SUPPORT_DIR))

import deepseek_assertions as da  # noqa: E402

_EVIDENCE = REPO_ROOT / "docs" / "host-gate-evidence" / "deepseek"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _support_fixture(version: str) -> dict:
    """A support report that passes today's checks EXCEPT version parity
    is layered on top via expected_version (claimed evidence is an issue
    ref; floors and the three frozen gap disclosures are present)."""
    return {
        "schema": da.SUPPORT_SCHEMA,
        "zmem_version": version,
        "generated_at": "2026-10-30T00:00:00Z",
        "claimed": [{"carrier": "headless", "platform": "linux",
                     "evidence": "#416"}],
        "tested_floors": {"node": "v24.16.0"},
        "capability_gaps": list(da.SUPPORT_GAP_DISCLOSURES),
    }


def _version_problems(problems) -> list:
    return [p for p in problems if "zmem_version" in str(p)]


class DeepseekEvidenceVersionParityTest(unittest.TestCase):
    """The harness reports evidence version divergence."""

    def test_leg1_live_agreement_and_detection(self):
        """Post-#420 contract: the LIVE committed artifacts agree with the
        release — the 0.134.0 regeneration closed the disclosed
        support-report lag, so the standing contract is agreement with NO
        tolerance, and the detection legs run against a synthetically
        drifted copy of the LIVE support report."""
        canary = _load(_EVIDENCE / "canary-report.json")
        support = _load(_EVIDENCE / "support-report.json")
        release = _load(REPO_ROOT / "release-manifest.json")["version"]
        # (0) the standing post-regeneration contract: live agreement.
        self.assertEqual(
            da.evidence_version_problems(canary, support, release), [],
            "the live evidence reports must agree with the release "
            "(the #420 regeneration closed the disclosed lag; no "
            "tolerance is tolerated)")
        self.assertEqual(
            da.support_report_problems(support, expected_version=release),
            [],
            "the live support report must validate against the release "
            "version with no problems")
        # (i) the expected_version kwarg fires on a drifted COPY.
        drifted_support = dict(support, zmem_version="0.0.0-drifted")
        kwarg_problems = da.support_report_problems(
            drifted_support, expected_version=release)
        self.assertTrue(
            _version_problems(kwarg_problems),
            "support_report_problems(expected_version=...) reported no "
            f"zmem_version problem for support-report zmem_version="
            f"{drifted_support.get('zmem_version')!r} vs release "
            f"{release!r}: {kwarg_problems}")
        # (ii) the cross-check binding fires on the same drifted copy.
        divergences = da.evidence_version_problems(
            canary, drifted_support, release)
        self.assertTrue(
            divergences,
            "evidence_version_problems reported nothing for canary="
            f"{canary.get('zmem_version')!r} support="
            f"{drifted_support.get('zmem_version')!r} release={release!r}")
        joined = " ".join(str(d) for d in divergences)
        self.assertTrue(
            "version" in joined.lower(),
            f"divergence descriptions must name the disagreeing versions: "
            f"{divergences}")

    def test_leg2_synthetic_agreement_reports_nothing(self):
        version = "9.9.8"
        canary = {"zmem_version": version}
        support = _support_fixture(version)
        self.assertEqual(
            da.evidence_version_problems(canary, support, version), [],
            "agreement (canary == support == release) must report no "
            "divergence")
        self.assertEqual(
            da.support_report_problems(support, expected_version=version),
            [],
            "a matching expected_version must add no problem to an "
            "otherwise-valid support report")

    def test_leg3_synthetic_divergence_each_direction(self):
        agreement = "9.9.8"
        cases = [  # (canary, support, release, offending value)
            ("9.9.7", agreement, agreement, "9.9.7"),
            (agreement, "9.9.7", agreement, "9.9.7"),
            (agreement, agreement, "9.9.7", "9.9.7"),
        ]
        for canary_v, support_v, release_v, offender in cases:
            problems = da.evidence_version_problems(
                {"zmem_version": canary_v}, _support_fixture(support_v),
                release_v)
            self.assertTrue(
                problems,
                f"divergence canary={canary_v!r} support={support_v!r} "
                f"release={release_v!r} must be reported")
            joined = " ".join(str(p) for p in problems)
            self.assertIn(
                offender, joined,
                f"divergence descriptions must name the offending value "
                f"{offender!r}: {problems}")
        kwarg_problems = da.support_report_problems(
            _support_fixture("9.9.7"), expected_version=agreement)
        self.assertTrue(
            any("zmem_version" in p and agreement in p
                for p in kwarg_problems),
            f"the support kwarg problem must name zmem_version and the "
            f"expected version {agreement!r}: {kwarg_problems}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
