"""Issue #490 frozen acceptance checks (collated 489+490 trace): release-
evidence PROVENANCE guards over docs/host-gate-evidence/deepseek/ — the
three hand-maintained fields of the support report (notes SHAs, derived
current-segment sha, generated_at stamp) plus the dirty-tree disclosure
predicate. All three defect shapes shipped through PR #487's pre-correction
blob and passed the full gate ladder with green CI because no automated
guard validates them (the assertions module checks zmem_version, never
git_sha; the #469 stamp guard checks only support-after-canary).

Four classes, each with (i) a POSITIVE leg against the REAL committed
reports (green on the corrected baseline this file lands against) and
(ii) NEGATIVE-CONTROL legs that copy a report to a scratch dir, mutate it
into the named defect shape, and assert the guard helper flags it:

- AC8 NotesShaResolutionTest: every 40-hex token in either report's notes
  resolves via ``git cat-file -t`` as a commit; a fabricated token (real
  12-char prefix, invented tail) is named in the failure.
- AC9 CurrentSegmentDerivationTest: the current release segment's recorded
  runtime SHA equals ``git rev-parse`` of the canary report's git_sha
  (derivation, not string equality of hand-typed text).
- AC10 StampCarryingCommitTest: support generated_at is strictly after the
  canary's AND no later than the committer timestamp of the commit
  carrying the report (git log -1 --format=%cI -- path).
- AC11 WorkingTreeDisclosureTest: when ``git show <canary git_sha>:release-
  manifest.json`` reports a version different from the canary report's
  zmem_version, the current support notes segment must carry a
  working-tree disclosure (0.150.0/0.154.0 precedent vocabulary).

Current-segment identification: the notes field is a " | "-separated
provenance log. The primary rule is the segment whose LEADING version
label equals release-manifest.json's top-level version. On the committed
0.160.0 baseline that label runs MID-segment (the 0.160.0 record continues
the "FEEDBACK ROUND 2" segment without a separator), so a documented
fallback selects the LAST segment that mentions the release version AND
carries a 40-hex sha (verified to hold exactly the one corrected full
canary sha at the freeze anchor).

Pure helpers + tiny git subprocess wrappers; no env, no store. Historical
release sections must PASS unchanged (issue #490 AC5, owned by the
existing five tests in test_deepseek_evidence_version_parity.py).

Run: python -X utf8 tests/test_deepseek_evidence_parity_guards.py
     python -X utf8 tests/test_deepseek_evidence_parity_guards.py NotesShaResolutionTest
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = REPO_ROOT / "docs" / "host-gate-evidence" / "deepseek"
SUPPORT_PATH = EVIDENCE / "support-report.json"
CANARY_PATH = EVIDENCE / "canary-report.json"
RELEASE_MANIFEST = REPO_ROOT / "release-manifest.json"

# A 40-hex token with hex-lookaround guards so 64-hex manifest digests and
# 12-char prefixes never yield spurious matches. Case-WIDENED (critic
# round: a lowercase-only class silently passes an uppercase fabricated
# sha): extraction preserves the token AS WRITTEN so git cat-file receives
# the exact text (git rejects non-lowercase object names, which is itself
# a resolvability failure the guard must surface, not skip).
SHA40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def _git(args, timeout: int = 60):
    return subprocess.run(["git"] + list(args), cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout)


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _release_version() -> str:
    return _load(RELEASE_MANIFEST)["version"]


def _parse_iso(raw):
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


# --- AC8: every notes 40-hex token resolves as a commit ----------------------


def notes_sha_problems(report: dict, label: str) -> list:
    """Every 40-hex token in `report`'s notes must resolve via
    ``git cat-file -t`` as a commit (no label heuristic). Returns one
    problem string per offending token, naming the token."""
    notes = (report or {}).get("notes") or ""
    problems = []
    for tok in sorted(set(SHA40_RE.findall(notes))):
        r = _git(["cat-file", "-t", tok])
        if r.returncode != 0 or r.stdout.strip() != "commit":
            problems.append(
                f"{label} notes 40-hex token {tok} does not resolve as a "
                f"commit via git cat-file -t (rc={r.returncode}, "
                f"type={r.stdout.strip()!r}, err={r.stderr.strip()[-120:]!r})")
    return problems


# --- current-segment identification (AC9 / AC11) -----------------------------


def current_segment(notes: str, version: str):
    """(segment, how) for the current release's notes segment.

    Primary rule: the segment whose LEADING version label equals the
    release version. Fallback (documented deviation — the committed
    0.160.0 record runs mid-segment): the LAST " | "-separated segment
    that mentions the release version as a word token AND carries a
    40-hex sha. (None, "none") when nothing matches."""
    segs = str(notes or "").split(" | ")
    labeled = [s for s in segs
               if re.match(r"\s*" + re.escape(version) + r"\b", s)]
    if labeled:
        return labeled[-1], f"leading version label {version}"
    mentioning = [s for s in segs
                  if re.search(r"\b" + re.escape(version) + r"\b", s)
                  and SHA40_RE.search(s)]
    if mentioning:
        return mentioning[-1], (f"last segment mentioning {version} with "
                                "a 40-hex sha (mid-segment label fallback)")
    return None, "none"


def current_segment_derivation_problem(support_report: dict,
                                       canary_report: dict,
                                       version: str):
    """None when the current segment's recorded runtime SHA equals
    ``git rev-parse`` of the canary report's git_sha; else a problem
    naming the mismatched sha(s) and the expected full sha."""
    canary_sha = (canary_report or {}).get("git_sha")
    if not canary_sha:
        return "canary report carries no git_sha (fail closed)"
    r = _git(["rev-parse", str(canary_sha)])
    if r.returncode != 0:
        return (f"git rev-parse {canary_sha} failed "
                f"(rc={r.returncode}, err={r.stderr.strip()[-120:]!r})")
    expected = r.stdout.strip().lower()
    seg, how = current_segment((support_report or {}).get("notes"), version)
    if seg is None:
        return (f"current release segment for {version} not found in the "
                "support notes (fail closed)")
    shas = sorted(set(SHA40_RE.findall(seg)))
    if not shas:
        return (f"the current notes segment ({how}) records no 40-hex "
                f"runtime sha for {version} (fail closed)")
    bad = [s for s in shas if s.lower() != expected]
    if bad:
        return (f"derivation mismatch: current segment ({how}) sha(s) "
                f"{bad} != git rev-parse of canary git_sha {canary_sha} "
                f"(= {expected}); the notes law is that the full sha is "
                "derived mechanically from the canary report via git "
                "rev-parse, never retyped")
    return None


# --- AC10: stamp bounds -------------------------------------------------------


def stamp_bounds_problem(canary_stamp_raw, support_stamp_raw,
                         carrying_iso_raw):
    """None when canary_stamp < support_stamp <= carrying_stamp (aware
    datetimes); else a problem naming the offending stamps. Fails closed
    on missing/unparseable stamps — never raises."""
    if not canary_stamp_raw or not support_stamp_raw or not carrying_iso_raw:
        return (f"stamp missing: canary={canary_stamp_raw!r} "
                f"support={support_stamp_raw!r} "
                f"carrying={carrying_iso_raw!r} (fail closed)")
    try:
        canary = _parse_iso(canary_stamp_raw)
        support = _parse_iso(support_stamp_raw)
        carrying = _parse_iso(carrying_iso_raw)
    except (ValueError, TypeError) as exc:
        return (f"stamp unparseable: canary={canary_stamp_raw!r} "
                f"support={support_stamp_raw!r} "
                f"carrying={carrying_iso_raw!r} ({exc})")
    try:
        strictly_after = support > canary
        within_carrying = support <= carrying
    except TypeError as exc:
        return (f"stamps cannot be ordered (naive vs aware): "
                f"canary={canary_stamp_raw!r} support={support_stamp_raw!r} "
                f"carrying={carrying_iso_raw!r} ({exc})")
    if not strictly_after:
        return (f"support generated_at {support_stamp_raw} is not strictly "
                f"after canary generated_at {canary_stamp_raw} (PR #451 "
                "convention)")
    if not within_carrying:
        return (f"support generated_at {support_stamp_raw} postdates the "
                f"carrying commit's committer timestamp "
                f"{carrying_iso_raw} — a hand-typed future stamp cannot "
                "have been written by the commit that carries the file "
                "(the 0.160.0 pre-correction shape: +29 s past 6be1af40)")
    return None


# --- AC11: working-tree disclosure on version mismatch ------------------------


def working_tree_disclosure_problem(support_report: dict,
                                    canary_report: dict,
                                    version: str):
    """None when no dirty-tree run is indicated, or when one is indicated
    AND the current notes segment carries a working-tree disclosure
    (case-insensitive 'working tree' / 'working-tree'); else a problem
    naming the mismatched versions and the offending segment."""
    gs = (canary_report or {}).get("git_sha")
    if not gs:
        return "canary report carries no git_sha (fail closed)"
    r = _git(["show", f"{gs}:release-manifest.json"])
    if r.returncode != 0:
        return (f"git show {gs}:release-manifest.json failed "
                f"(rc={r.returncode}, err={r.stderr.strip()[-120:]!r})")
    m = re.search(r'"version"\s*:\s*"([^"]+)"', r.stdout)
    if not m:
        return (f"release-manifest.json at git show {gs} carries no "
                "parseable version field (fail closed)")
    tree_version = m.group(1)
    canary_version = (canary_report or {}).get("zmem_version")
    if tree_version == canary_version:
        return None  # the recorded tree already carries the canary version
    seg, how = current_segment((support_report or {}).get("notes"), version)
    if seg is None:
        return (f"canary zmem_version {canary_version} != release-manifest "
                f"version {tree_version} at git_sha {gs} (working-tree "
                f"run), but the current release segment for {version} was "
                "not found in the support notes (fail closed)")
    if not re.search(r"working[- ]tree", seg, re.IGNORECASE):
        return (f"canary zmem_version {canary_version} != release-manifest "
                f"version {tree_version} at git_sha {gs}, but the current "
                f"notes segment ({how}) carries no working-tree disclosure "
                "(0.150.0/0.154.0 precedent vocabulary); silent "
                f"dirty-tree runs are the flagged shape; segment head: "
                f"{seg[:160]!r}")
    return None


# --- scratch-copy helper for the negative controls ----------------------------


def _mutated_support_copy(testcase, mutator):
    """Copy the REAL support report to a scratch dir, mutate its notes
    with `mutator`, write the mutated JSON, and return (report_dict,
    scratch_dir). The testcase registers the scratch cleanup."""
    scratch = Path(tempfile.mkdtemp(prefix="zmem-490guard-"))
    testcase.addCleanup(shutil.rmtree, str(scratch), True)
    real = _load(SUPPORT_PATH)
    data = dict(real)
    data["notes"] = mutator(real.get("notes") or "")
    out = scratch / "support-report.json"
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return _load(out), scratch


class NotesShaResolutionTest(unittest.TestCase):
    """AC8: every 40-hex token in either report's notes resolves as a
    commit; a fabricated token is named in the failure."""

    def test_real_reports_notes_tokens_all_resolve(self):
        for path, label in ((SUPPORT_PATH, "support-report"),
                            (CANARY_PATH, "canary-report")):
            report = _load(path)
            notes = report.get("notes") or ""
            tokens = sorted(set(SHA40_RE.findall(notes)))
            problems = notes_sha_problems(report, label)
            self.assertEqual(
                problems, [],
                f"{label}: unresolved notes sha token(s): {problems} "
                f"(checked {len(tokens)} unique 40-hex token(s))")
            if label == "support-report":
                # non-vacuity pin: the shipped notes DO carry the
                # historical token set (10 unique at the freeze anchor)
                self.assertGreaterEqual(
                    len(tokens), 1,
                    "the support notes are expected to carry the "
                    "historical 40-hex provenance tokens")

    def test_fabricated_sha_negative_control_flagged(self):
        # The PR #487 pre-correction shape: real 12-char prefix
        # (217836598699 = the recorded canary git_sha), invented tail —
        # 40 hex total, differing from the real full sha (...415aa2e).
        real_full = _git(
            ["rev-parse", str(_load(CANARY_PATH)["git_sha"])])
        self.assertEqual(real_full.returncode, 0, real_full.stderr)
        fabricated = "217836598699a953351960c37134a898f415ffff"
        self.assertEqual(len(fabricated), 40)
        self.assertNotEqual(
            fabricated, real_full.stdout.strip().lower(),
            "the fabricated control token must differ from the real full "
            "sha (guard against the control degenerating into the truth)")
        report, _scratch = _mutated_support_copy(
            self, lambda notes: notes + " fabricated evidence sha "
                + fabricated)
        problems = notes_sha_problems(report, "scratch-support")
        self.assertTrue(
            problems,
            "the fabricated notes sha token must be flagged; got no "
            "problems (the guard is fail-open on this shape)")
        self.assertIn(
            fabricated, problems[0],
            f"the failure must name the offending token; got: {problems}")

    def test_uppercase_fabricated_sha_negative_control_flagged(self):
        # Critic round: a case-narrow [0-9a-f]{40} extractor never matches
        # an all-uppercase 40-hex fabrication, so it would pass silently.
        # The widened extractor must surface it, and the problem must name
        # the token AS WRITTEN (git rejects non-lowercase object names —
        # itself a resolvability failure).
        uppercase = "A1B2C3D4E5" * 4
        self.assertEqual(len(uppercase), 40)
        self.assertTrue(SHA40_RE.fullmatch(uppercase))
        report, _scratch = _mutated_support_copy(
            self, lambda notes: notes + " fabricated evidence sha "
                + uppercase)
        problems = notes_sha_problems(report, "scratch-support")
        self.assertTrue(
            problems,
            "the uppercase fabricated notes sha token must be flagged; "
            "got no problems (case-narrow extractor regression)")
        self.assertIn(
            uppercase, problems[0],
            f"the failure must name the offending uppercase token verbatim; "
            f"got: {problems}")


class CurrentSegmentDerivationTest(unittest.TestCase):
    """AC9: the current segment's runtime SHA is DERIVED from the canary
    report's git_sha (git rev-parse), never retyped."""

    def test_real_current_segment_sha_derives_from_canary(self):
        version = _release_version()
        problem = current_segment_derivation_problem(
            _load(SUPPORT_PATH), _load(CANARY_PATH), version)
        self.assertIsNone(
            problem,
            f"the live evidence pair must satisfy the derivation law "
            f"({problem})")

    def test_retyped_current_segment_sha_flagged(self):
        version = _release_version()
        canary = _load(CANARY_PATH)
        expected = _git(["rev-parse", str(canary["git_sha"])])
        self.assertEqual(expected.returncode, 0, expected.stderr)
        # a DIFFERENT resolvable commit's full sha (still a commit, so the
        # AC8 resolvability law alone cannot catch it — only the
        # derivation comparison can)
        other = _git(["rev-parse", "HEAD~5"])
        self.assertEqual(other.returncode, 0, other.stderr)
        other_full = other.stdout.strip().lower()
        self.assertNotEqual(other_full, expected.stdout.strip().lower())

        def _mutate(notes):
            seg, _how = current_segment(notes, version)
            assert seg is not None, "precondition: current segment found"
            shas = sorted(set(SHA40_RE.findall(seg)))
            assert shas, "precondition: current segment carries a sha"
            return notes.replace(shas[0], other_full, 1)

        report, _scratch = _mutated_support_copy(self, _mutate)
        problem = current_segment_derivation_problem(report, canary, version)
        self.assertTrue(
            problem,
            "a retyped current-segment sha (divergent tail, still "
            "cat-file-resolvable) must be flagged")
        self.assertIn("derivation mismatch", problem)
        self.assertIn(other_full, problem,
                      "the failure must name the offending sha")
        self.assertIn(expected.stdout.strip().lower(), problem,
                      "the failure must name the expected derived sha")


class StampCarryingCommitTest(unittest.TestCase):
    """AC10: generated_at strictly after the canary's AND no later than
    the carrying commit's committer timestamp."""

    def test_real_stamp_bounds_satisfied(self):
        canary = _load(CANARY_PATH)
        support = _load(SUPPORT_PATH)
        r = _git(["log", "-1", "--format=%cI", "--", str(SUPPORT_PATH)])
        self.assertEqual(r.returncode, 0, r.stderr)
        carrying = r.stdout.strip()
        self.assertTrue(carrying, "no carrying commit found for the report")
        problem = stamp_bounds_problem(
            canary.get("generated_at"), support.get("generated_at"),
            carrying)
        self.assertIsNone(
            problem,
            f"the live evidence pair must satisfy both stamp bounds "
            f"(canary={canary.get('generated_at')} "
            f"support={support.get('generated_at')} "
            f"carrying={carrying}): {problem}")

    def test_canary_stamp_within_its_own_carrying_commit(self):
        # Issue #490 AC3 bounds EACH report's generated_at by the committer
        # timestamp of the most recent commit touching THAT file; the
        # canary report gets its own upper-bound leg (critic finding: the
        # support path alone covered only half the law's subject).
        canary = _load(CANARY_PATH)
        r = _git(["log", "-1", "--format=%cI", "--", str(CANARY_PATH)])
        self.assertEqual(r.returncode, 0, r.stderr)
        carrying = r.stdout.strip()
        self.assertTrue(
            carrying, "no carrying commit found for the canary report")
        self.assertTrue(
            canary.get("generated_at"),
            "canary report carries no generated_at (fail closed)")
        canary_stamp = _parse_iso(canary.get("generated_at"))
        carrying_stamp = _parse_iso(carrying)
        self.assertTrue(
            canary_stamp <= carrying_stamp,
            f"canary generated_at {canary.get('generated_at')} postdates "
            f"its own carrying commit's committer timestamp {carrying} — "
            "a stamp later than the commit carrying the file cannot have "
            "been written by that commit (issue #490 AC3)")

    def test_postdated_stamp_negative_control_flagged(self):
        # Pure negative control: carrying stamp computed from a REAL past
        # commit (git log of HEAD~5), support stamp set 29 s after it —
        # the exact PR #487 pre-correction shape (+29 s past 6be1af40).
        r = _git(["log", "-1", "--format=%cI", "HEAD~5"])
        self.assertEqual(r.returncode, 0, r.stderr)
        carrying = r.stdout.strip()
        past = _parse_iso(carrying)
        # the canary stamp must sit BEFORE the carrying commit so the only
        # violated law is the upper bound (the strictly-after half is
        # pinned by its own leg below)
        canary_stamp = (past - timedelta(hours=1)
                        ).isoformat()
        support_stamp = (past + timedelta(seconds=29)).isoformat()
        problem = stamp_bounds_problem(canary_stamp, support_stamp, carrying)
        self.assertTrue(
            problem,
            "a support stamp 29 s past the carrying commit must be flagged")
        self.assertIn("postdate", problem)
        self.assertIn(support_stamp, problem,
                      "the failure must name the offending stamp")
        self.assertIn(carrying, problem,
                      "the failure must name the carrying-commit stamp")

    def test_not_strictly_after_canary_flagged(self):
        # The other half of the law (owned jointly with the #469 guard):
        # equal stamps are a problem; the helper is pure so this leg needs
        # no git state.
        problem = stamp_bounds_problem(
            "2026-11-16T18:18:09.234Z", "2026-11-16T18:18:09.234Z",
            "2026-11-20T00:00:00Z")
        self.assertTrue(problem)
        self.assertIn("strictly after", problem)


class WorkingTreeDisclosureTest(unittest.TestCase):
    """AC11: a version mismatch between the canary git_sha's tree manifest
    and the canary zmem_version requires a working-tree disclosure in the
    current notes segment; silence is flagged."""

    def test_real_pair_disclosure_satisfied(self):
        version = _release_version()
        problem = working_tree_disclosure_problem(
            _load(SUPPORT_PATH), _load(CANARY_PATH), version)
        self.assertIsNone(
            problem,
            f"the live evidence pair must satisfy the disclosure "
            f"predicate ({problem})")

    def test_silent_version_mismatch_flagged(self):
        version = _release_version()
        canary = _load(CANARY_PATH)
        # precondition: the live pair IS in the mismatch shape (tree
        # manifest 0.159.0 vs canary zmem_version 0.160.0 at the freeze
        # anchor) — otherwise this negative control no longer tests the
        # disclosure branch.
        show = _git(["show", f"{canary['git_sha']}:release-manifest.json"])
        self.assertEqual(show.returncode, 0, show.stderr)
        tree_version = re.search(
            r'"version"\s*:\s*"([^"]+)"', show.stdout).group(1)
        self.assertNotEqual(
            tree_version, canary.get("zmem_version"),
            "precondition failed: the live pair is no longer in the "
            "version-mismatch shape; update this negative control's "
            "fixture strategy (mutate a copy of the canary report too)")

        def _scrub(notes):
            seg, _how = current_segment(notes, version)
            assert seg is not None, "precondition: current segment found"
            scrubbed = re.sub(r"working[- ]tree", "release-run", seg,
                              flags=re.IGNORECASE)
            return notes.replace(seg, scrubbed, 1)

        report, _scratch = _mutated_support_copy(self, _scrub)
        self.assertFalse(
            re.search(r"working[- ]tree",
                      current_segment(report.get("notes"), version)[0] or "",
                      re.IGNORECASE),
            "precondition failed: the scrub did not remove the disclosure "
            "marker from the current segment")
        problem = working_tree_disclosure_problem(report, canary, version)
        self.assertTrue(
            problem,
            "a version mismatch with the disclosure sentence removed must "
            "be flagged (the pre-correction 0.160.0 segment shape)")
        self.assertIn("working-tree disclosure", problem)
        self.assertIn(str(canary.get("zmem_version")), problem,
                      "the failure must name the canary zmem_version")
        self.assertIn(tree_version, problem,
                      "the failure must name the tree-manifest version")


if __name__ == "__main__":
    unittest.main(verbosity=2)
