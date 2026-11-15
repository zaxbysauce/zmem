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


# --- Issue #469 AC2 (appended): evidence stamp ordering guardrail ---------

from datetime import datetime  # noqa: E402


def _stamp_ordering_problem(canary_report, support_report):
    """Return None when the support report's generated_at lands strictly
    after the canary report's (the PR #451 convention), else a non-empty
    problem string. Reads ONLY generated_at from either report; fails
    closed on a missing, empty, or unparseable stamp, or a stamp pair
    that cannot be ordered (naive vs aware — PR #470 review) — never
    raises, never skips."""
    canary_raw = (canary_report or {}).get("generated_at")
    support_raw = (support_report or {}).get("generated_at")
    if not canary_raw or not support_raw:
        return (f"generated_at missing or empty: canary={canary_raw!r} "
                f"support={support_raw!r} (fail closed)")
    try:
        canary_stamp = datetime.fromisoformat(
            str(canary_raw).replace("Z", "+00:00"))
        support_stamp = datetime.fromisoformat(
            str(support_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        return (f"generated_at unparseable: canary={canary_raw!r} "
                f"support={support_raw!r} ({exc})")
    try:
        ordered = support_stamp > canary_stamp
    except TypeError as exc:
        # One stamp parsed offset-naive and the other offset-aware: the
        # pair cannot be compared, which is itself a fail-closed problem
        # (a report emitted naive stamps is not verifiable against an
        # aware one).
        return (f"generated_at stamps cannot be ordered (naive vs aware): "
                f"canary={canary_raw!r} support={support_raw!r} ({exc})")
    if not ordered:
        return (f"support generated_at {support_raw} is not strictly after "
                f"canary generated_at {canary_raw} (PR #451 convention: the "
                f"support report's stamp must land strictly AFTER the "
                f"canary write)")
    return None


class EvidenceStampOrderingTest(unittest.TestCase):
    """The parity suite mechanically enforces the support-strictly-after-
    canary generated_at ordering (PR #451), which was previously policed
    only by operator memory. The class does not exist at base (loader
    error), GREEN after the fix lands."""

    def test_support_stamp_strictly_after_canary(self):
        # LIVE leg: the committed artifacts must satisfy the convention
        # (canary carries millisecond precision, support seconds). Kept in
        # its own method so a live-leg failure never hides the synthetic
        # legs (PR #470 review, OFB-13).
        canary = _load(_EVIDENCE / "canary-report.json")
        support = _load(_EVIDENCE / "support-report.json")
        canary_stamp = datetime.fromisoformat(
            str(canary["generated_at"]).replace("Z", "+00:00"))
        support_stamp = datetime.fromisoformat(
            str(support["generated_at"]).replace("Z", "+00:00"))
        self.assertGreater(
            support_stamp, canary_stamp,
            f"support generated_at {support['generated_at']} must land "
            f"strictly AFTER canary generated_at "
            f"{canary['generated_at']} (PR #451 convention: the support "
            f"report's stamp must land strictly after the canary write)")
        self.assertIsNone(_stamp_ordering_problem(canary, support),
                          "the live evidence pair is correctly ordered")

    def test_stamp_ordering_fail_closed_legs(self):
        # SYNTHETIC fail-closed legs: the check bites. Separate method so
        # each leg reports independently (PR #470 review, OFB-13).
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "2026-11-14T04:05:32.358Z"},
            {"generated_at": "2026-11-14T04:05:32.358Z"}),
            "equal stamps are a problem (strictly-after, not >=)")
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "2026-11-14T04:06:02Z"},
            {"generated_at": "2026-11-14T04:05:32.358Z"}),
            "a support stamp BEFORE the canary stamp is a problem")
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "2026-11-14T04:05:32.358Z"}, {}),
            "a missing support generated_at fails closed")
        self.assertTrue(_stamp_ordering_problem(
            {}, {"generated_at": "2026-11-14T04:06:02Z"}),
            "a missing canary generated_at fails closed")
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "not-a-stamp"},
            {"generated_at": "2026-11-14T04:06:02Z"}),
            "an unparseable stamp fails closed (never skips)")
        # PR #470 review (OFB-01/CPB-1): a naive-vs-aware stamp pair must
        # return a problem string, never raise TypeError.
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "2026-11-14T04:05:32.358Z"},
            {"generated_at": "2026-11-14T04:06:02"}),
            "a naive support stamp against an aware canary stamp fails "
            "closed with a problem string (never raises)")
        self.assertTrue(_stamp_ordering_problem(
            {"generated_at": "2026-11-14T04:05:32"},
            {"generated_at": "2026-11-14T04:06:02Z"}),
            "a naive canary stamp against an aware support stamp fails "
            "closed with a problem string (never raises)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
