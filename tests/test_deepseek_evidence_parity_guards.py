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

- AC8 NotesShaResolutionTest: every 40-hex token in the CURRENT release
  segment of either report's notes resolves via ``git cat-file -t`` as a
  commit (the fabrication catch); unresolvable HISTORICAL-segment tokens
  degrade with a visible PROVENANCE-TOKEN-SKIPPED line, not a failure. A
  fabricated token (real 12-char prefix, invented tail) appended to the
  current segment is named in the failure.
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

Two-mode provenance contract (shallow CI checkouts and squash-merged
main). The mode is selected from the CHECKOUT SHAPE alone, never from
the evidence under test: SKIP iff (a) ``git rev-parse
--is-shallow-repository`` prints true — a depth-2 CI checkout of ANY
head (the branch head included: the canary sits at HEAD~1 but the
historical notes shas and canary~1 do not exist in the window) cannot
see the objects the strict laws dereference — or (b) the commit
carrying the live canary report (``git log -1 --format=%H -- <canary
path>``) is already an ANCESTOR of origin/main — post-squash/shipped
evidence, where unreachable feature-branch objects are the expected
state (a missing origin/main counts as not-ancestor). Otherwise STRICT
(full clones where the canary's carrying commit is not yet on the
default branch — PR authoring/review, the frozen replays) runs every
assertion at full strength, and FAILS CLOSED when the live canary
git_sha does not resolve as a commit (strict_canary_resolution_problem,
final-critic round 2 finding 2): a typo'd or fabricated canary sha is
the #490 hand-edited-evidence shape and must not be able to select SKIP
for itself. In STRICT, notes-token resolvability is segment-scoped:
tokens in the CURRENT release segment must resolve (the AC9 derivation
governs that segment — the fabrication catch); unresolvable
HISTORICAL-segment tokens (old squash-merged-away branches) each print
a visible ``PROVENANCE-TOKEN-SKIPPED: <short-token>: <reason>`` line
and are recorded in the assertion counts, not failed. SKIP mode still
runs the git-object-dependent positive legs but asserts only their
non-git invariants, each printing a visible ``PROVENANCE-GUARDS-SKIPPED:
<reason>`` line — deliberately NOT unittest.skip, so a CI log shows the
marker and the file stays exit-0 rather than reporting 'OK (skipped=N)'.
The scratch-based negative controls keep running and passing in skip mode
(controlled copies substitute a depth-safe resolvable sha), so the guards
still prove discrimination; the full git-object laws are enforced at PR
authoring/review time in full clones.

Run: python -X utf8 tests/test_deepseek_evidence_parity_guards.py
     python -X utf8 tests/test_deepseek_evidence_parity_guards.py NotesShaResolutionTest
"""

from __future__ import annotations

import json
import os
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
# the exact text (an uppercase spelling of a REAL sha resolves — git hex
# object names are case-insensitive — and a fabricated one still fails
# a resolvability failure the guard must surface, not skip).
SHA40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def _git(args, timeout: int = 60):
    # GIT_TERMINAL_PROMPT=0 — the test_git_hygiene network ratchet: a
    # variable-argv git call cannot be statically proven local, so it must
    # carry the effective noninteractive guard.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git"] + list(args), cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=timeout,
                          env=env)


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _release_version() -> str:
    return _load(RELEASE_MANIFEST)["version"]


def _parse_iso(raw):
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _repo_is_shallow() -> bool:
    """True when ``git rev-parse --is-shallow-repository`` prints true —
    a CI depth-2 checkout of ANY head: the canary sha itself may sit at
    HEAD~1 inside the window, but the historical notes shas and the
    canary's parent do not exist there, so strict resolvability would
    mint false defect reports (final-critic round 2, finding 1)."""
    r = _git(["rev-parse", "--is-shallow-repository"])
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def _canary_carrying_commit() -> str:
    """Full sha of the last commit touching the live canary report ('' on
    failure). The CARRYING COMMIT — a property of the checkout, never of
    the report's contents — anchors the mode: once the evidence ships on
    the default branch, the feature-branch objects it cites are gone by
    design (squash merge), and that is not a defect."""
    r = _git(["log", "-1", "--format=%H", "--", str(CANARY_PATH)])
    return r.stdout.strip().lower() if r.returncode == 0 else ""


def _is_ancestor_of_origin_main(commit: str) -> bool:
    """True when ``git merge-base --is-ancestor commit origin/main``
    succeeds. A MISSING origin/main (no remote configured) is treated as
    not-ancestor — the authoring-context default, which fails closed."""
    if not commit:
        return False
    r = _git(["rev-parse", "--verify", "--quiet", "origin/main"])
    if r.returncode != 0:
        return False
    m = _git(["merge-base", "--is-ancestor", commit, "origin/main"])
    return m.returncode == 0


def _select_provenance_mode(is_shallow: bool,
                            carrying_on_main: bool) -> tuple[str, str]:
    """PURE two-input mode selector (never reads the evidence under test):
    SKIP iff (a) the repository is shallow or (b) the canary report's
    carrying commit is already an ancestor of origin/main
    (post-squash/shipped evidence). Otherwise STRICT. Returns
    ("strict"|"skip", reason)."""
    if is_shallow:
        return ("skip",
                "shallow repository (git rev-parse --is-shallow-repository "
                "true): provenance objects outside the fetched window do "
                "not exist in this checkout")
    if carrying_on_main:
        return ("skip",
                "the commit carrying canary-report.json is an ancestor of "
                "origin/main (shipped/squash-merged evidence): the cited "
                "feature-branch objects are expected to be unreachable "
                "here")
    return "strict", ""


# Mode for this run, evaluated once at import from the CHECKOUT SHAPE
# alone: STRICT in full checkouts whose canary-carrying commit is not yet
# on the default branch (PR authoring/review, the frozen replays), SKIP
# in shallow CI checkouts and on post-squash main.
_MODE, _MODE_REASON = _select_provenance_mode(
    _repo_is_shallow(),
    _is_ancestor_of_origin_main(_canary_carrying_commit()))
_PROVENANCE_OK = _MODE == "strict"
_PROVENANCE_REASON = _MODE_REASON


def strict_canary_resolution_problem(mode: str, sha: str,
                                     resolves_as_commit: bool):
    """STRICT-mode root-of-trust gate (final-critic round 2, finding 2):
    the canary git_sha anchors AC9's derivation and AC11's disclosure
    predicate, and the mode selector no longer reads it — so in STRICT
    mode a value that resolves NOWHERE (typo or fabrication, the #490
    hand-edited-evidence threat) FAILS CLOSED with a problem naming the
    sha. Pure function of (mode, sha-resolution result) so the negative
    control can exercise the strict branch in BOTH modes without
    mutating the live report. None when there is no strict-mode
    complaint."""
    if mode != "strict" or resolves_as_commit:
        return None
    return (f"strict mode: canary git_sha {sha} does not resolve as a "
            "commit via git cat-file -t (typo'd or fabricated canary sha "
            "— the #490 hand-edited-evidence shape; fail closed)")


def _skip_marker(leg: str) -> None:
    # Visible marker for a degraded leg — NOT unittest.skip: the file must
    # stay exit-0 WITH the marker lines in the log, never 'OK (skipped=N)'.
    print(f"PROVENANCE-GUARDS-SKIPPED: {leg}: {_PROVENANCE_REASON}",
          flush=True)


def _sha_argv_safe(sha) -> bool:
    """PR #491 feedback (N-06): canary git_sha values reach git argv
    (cat-file / rev-parse / show). A value beginning with '-' would be
    parsed as an OPTION (executed scratch repro: '--output=...' is taken
    as a git flag). Git object names are hex, so requiring the hex
    charset defeats every option-injection shape."""
    return bool(re.fullmatch(r"[0-9a-f]{6,40}", str(sha or "").lower()))


def _visible_commit_shas(count: int = 2) -> list:
    """Full shas of the newest `count` commits visible in THIS checkout,
    newest first ([] on failure). No relative-depth argv anywhere — a
    depth-relative lookup is exactly what a shallow checkout breaks;
    plain ``git log`` reports only what exists."""
    r = _git(["log", "-n", str(count), "--format=%H"])
    if r.returncode != 0:
        return []
    return [ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()]


def _past_commit_iso() -> tuple[bool, str]:
    """(True, %cI) of the OLDEST of the two newest visible commits — a
    real past commit's committer timestamp for the pure-helper stamp
    control, resolvable at any depth that shows two commits (falling back
    to the newest commit's own timestamp at depth 1). (False, stderr)
    when nothing is visible."""
    r = _git(["log", "-n", "2", "--format=%cI"])
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode != 0 or not lines:
        return False, (r.stderr.strip()[-160:] or "no visible commits")
    return True, lines[-1]


# --- AC8: every notes 40-hex token resolves as a commit ----------------------


def notes_sha_problems(report: dict, label: str):
    """Segment-scoped resolvability law (final-critic round 2 finding 1;
    PR #491 feedback round parallel-F-002): every 40-hex token in the
    CURRENT release segment of `report`'s notes — the segment the AC9
    derivation governs — AND in the LAST segment by position (the newest
    appended record; notes are append-only, so an unlabeled trailing
    segment is the normal authoring shape) must resolve via
    ``git cat-file -t`` as a commit; that is the fabrication catch.
    Tokens in HISTORICAL segments name commits from old
    squash-merged-away feature branches: an unresolvable one prints a
    visible ``PROVENANCE-TOKEN-SKIPPED: <short-token>: <reason>`` line
    and is NOT a failure. Fail-closed default: when no current segment
    can be identified, EVERY token is strict-scope. Returns
    (problems, skipped) — one problem string per offending strict-scope
    token (naming the token) plus the skipped historical tokens."""
    notes = (report or {}).get("notes") or ""
    tokens = sorted(set(SHA40_RE.findall(notes)))
    seg, _how = current_segment(notes, _release_version())
    segs = [s for s in str(notes or "").split(" | ") if s.strip()]
    strict_scope = (set(SHA40_RE.findall(seg)) if seg is not None
                    else set(tokens))
    if segs:
        strict_scope |= set(SHA40_RE.findall(segs[-1]))
    problems = []
    skipped = []
    for tok in tokens:
        r = _git(["cat-file", "-t", tok])
        if r.returncode == 0 and r.stdout.strip() == "commit":
            continue
        if tok in strict_scope:
            problems.append(
                f"{label} notes 40-hex token {tok} does not resolve as a "
                f"commit via git cat-file -t (rc={r.returncode}, "
                f"type={r.stdout.strip()!r}, err={r.stderr.strip()[-120:]!r})")
        else:
            reason = (f"git cat-file -t rc={r.returncode}, "
                      f"err={r.stderr.strip()[-100:]!r}")
            skipped.append(tok)
            print(f"PROVENANCE-TOKEN-SKIPPED: {tok[:12]}: historical "
                  f"segment token unresolvable in this checkout "
                  f"({reason})", flush=True)
    return problems, skipped


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
    if not _sha_argv_safe(canary_sha):
        # PR #491 feedback (N-06): option-injection guard (see
        # working_tree_disclosure_problem).
        return (f"canary git_sha {canary_sha!r} is not a hex object name "
                "(option-injection guard; fail closed)")
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
    if not _sha_argv_safe(gs):
        # PR #491 feedback (N-06): a non-hex value reaching git argv could
        # be parsed as an option — refuse instead of executing it.
        return (f"canary git_sha {gs!r} is not a hex object name "
                "(option-injection guard; fail closed)")
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
    """AC8: every 40-hex token in the CURRENT release segment of either
    report's notes resolves as a commit; a fabricated token appended to
    that segment is named in the failure. Unresolvable HISTORICAL-segment
    tokens degrade with a printed PROVENANCE-TOKEN-SKIPPED line."""

    def test_real_reports_notes_tokens_all_resolve(self):
        for path, label in ((SUPPORT_PATH, "support-report"),
                            (CANARY_PATH, "canary-report")):
            report = _load(path)
            notes = report.get("notes") or ""
            tokens = sorted(set(SHA40_RE.findall(notes)))
            if label == "support-report":
                # non-vacuity pin: the shipped notes DO carry the
                # historical token set (10 unique at the freeze anchor) —
                # a non-git invariant, enforced in BOTH modes
                self.assertGreaterEqual(
                    len(tokens), 1,
                    "the support notes are expected to carry the "
                    "historical 40-hex provenance tokens")
            if not _PROVENANCE_OK:
                # skip mode: the historical objects the resolvability law
                # cites do not exist in this checkout; assert only the
                # non-git invariants and mark the degraded leg.
                _skip_marker(f"AC8 real-notes resolvability ({label})")
                continue
            problems, skipped = notes_sha_problems(report, label)
            self.assertEqual(
                problems, [],
                f"{label}: unresolved notes sha token(s): {problems} "
                f"(strict scope: the current release segment's tokens; "
                f"{len(tokens)} unique 40-hex token(s) in the notes, "
                f"{len(skipped)} historical-segment token(s) skipped as "
                "unreachable — see the PROVENANCE-TOKEN-SKIPPED lines, "
                "not silent)")

    def test_fabricated_sha_negative_control_flagged(self):
        # The PR #487 pre-correction shape: real 12-char prefix
        # (217836598699 = the recorded canary git_sha at that anchor),
        # invented tail — 40 hex total, differing from the real full sha
        # (...415aa2e). Negative controls run at full strength in BOTH
        # modes (the scratch copies need no live historical objects).
        fabricated = "217836598699a953351960c37134a898f415ffff"
        self.assertEqual(len(fabricated), 40)
        if _PROVENANCE_OK:
            _live_sha = str(_load(CANARY_PATH)["git_sha"])
            self.assertTrue(
                _sha_argv_safe(_live_sha),
                "the canary manifest's git_sha must be a bare 40-hex "
                "object name before it reaches any Git argv")
            real_full = _git(["rev-parse", _live_sha])
            self.assertEqual(real_full.returncode, 0, real_full.stderr)
            self.assertNotEqual(
                fabricated, real_full.stdout.strip().lower(),
                "the fabricated control token must differ from the real "
                "full sha (guard against the control degenerating into "
                "the truth)")

            def _mutate(notes):
                # append INTO the current release segment: strict
                # resolvability is scoped to that segment, so the control
                # must land inside it to keep exercising the fabrication
                # catch (historical-segment tokens only degrade).
                seg, _how = current_segment(notes, _release_version())
                assert seg is not None, "precondition: current segment found"
                return notes.replace(
                    seg, seg + " fabricated evidence sha " + fabricated, 1)
        else:
            # skip mode degeneration guard, no git objects: the fabricated
            # token must differ from the RECORDED current-segment sha
            # text; the scratch copy carries ONLY the fabricated token so
            # the single problem entry deterministically names it.
            seg, _how = current_segment(
                (_load(SUPPORT_PATH) or {}).get("notes"),
                _release_version())
            for recorded in sorted(set(SHA40_RE.findall(seg or ""))):
                self.assertNotEqual(
                    fabricated, recorded.lower(),
                    "the fabricated control token must differ from the "
                    "recorded current-segment sha")

            def _mutate(notes):
                return "fabricated evidence sha " + fabricated
        report, _scratch = _mutated_support_copy(self, _mutate)
        problems, _skipped = notes_sha_problems(report, "scratch-support")
        self.assertTrue(
            problems,
            "the fabricated notes sha token must be flagged; got no "
            "problems (the guard is fail-open on this shape)")
        self.assertIn(
            fabricated, problems[0],
            f"the failure must name the offending token; got: {problems}")

    def test_unlabeled_trailing_segment_fabricated_token_flagged(self):
        # PR #491 feedback (parallel-F-002): notes are append-only and an
        # UNLABELED trailing segment is the normal authoring shape — the
        # strict scope must cover the LAST segment by position, not only
        # the version-labeled one, or a fabricated token appended after
        # the release segment escapes.
        _fab = "6de4ba2a7501" + "b" * 28  # 40 hex, does not resolve
        self.assertEqual(len(_fab), 40, "control must be 40 hex")

        def _append_fab(notes):
            return (notes + " | Feedback round 1 (0.162.0): canary re-run "
                    "at " + _fab)
        report, _scratch = _mutated_support_copy(self, _append_fab)
        problems, skipped = notes_sha_problems(report, "support-report")
        self.assertTrue(
            any(_fab in prob
                for prob in problems),
            f"a fabricated token in an unlabeled trailing segment must be "
            f"strict-scope flagged; problems={problems!r} "
            f"skipped={skipped!r}") if False else any(_fab in p for p in problems) or self.fail(f"not flagged; problems={problems!r} skipped={skipped!r}")

    def test_uppercase_fabricated_sha_negative_control_flagged(self):
        # Critic round: a case-narrow [0-9a-f]{40} extractor never matches
        # an all-uppercase 40-hex fabrication, so it would pass silently.
        # The widened extractor must surface it, and the problem must name
        # the token AS WRITTEN (an uppercase spelling of a real sha still
        # resolves — git hex names are case-insensitive — a fabricated one
        # itself a resolvability failure). Runs at full strength in BOTH
        # modes.
        uppercase = "A1B2C3D4E5" * 4
        self.assertEqual(len(uppercase), 40)
        self.assertTrue(SHA40_RE.fullmatch(uppercase))
        if _PROVENANCE_OK:
            # appended INTO the current release segment (strict scope) so
            # the widened-extractor discrimination keeps exercising the
            # fabrication catch under the segment-scoped law.
            def _mutate(notes):
                seg, _how = current_segment(notes, _release_version())
                assert seg is not None, "precondition: current segment found"
                return notes.replace(
                    seg, seg + " fabricated evidence sha " + uppercase, 1)
            report, _scratch = _mutated_support_copy(self, _mutate)
        else:
            # skip mode: the scratch notes carry ONLY the uppercase token
            # so the single problem entry deterministically names it.
            report, _scratch = _mutated_support_copy(
                self, lambda notes: "fabricated evidence sha " + uppercase)
        problems, _skipped = notes_sha_problems(report, "scratch-support")
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
        if not _PROVENANCE_OK:
            # skip mode: the canary sha cannot be derived here; assert the
            # leg's non-git invariants (fail-closed field presence, the
            # current segment exists and records a 40-hex runtime sha).
            canary = _load(CANARY_PATH)
            self.assertTrue(
                (canary or {}).get("git_sha"),
                "canary report carries no git_sha (fail closed)")
            seg, how = current_segment(
                (_load(SUPPORT_PATH) or {}).get("notes"), version)
            self.assertIsNotNone(
                seg,
                f"current release segment for {version} not found in the "
                "support notes (fail closed)")
            self.assertTrue(
                SHA40_RE.findall(seg or ""),
                f"the current notes segment ({how}) records no 40-hex "
                f"runtime sha for {version} (fail closed)")
            _skip_marker("AC9 real-pair derivation")
            return
        self.assertIsNone(
            problem,
            f"the live evidence pair must satisfy the derivation law "
            f"({problem})")

    def test_retyped_current_segment_sha_flagged(self):
        version = _release_version()
        canary = _load(CANARY_PATH)
        if _PROVENANCE_OK:
            self.assertTrue(_sha_argv_safe(canary["git_sha"]),
                            "canary git_sha must be a hex object name "
                            "(N-06 option-injection guard)")
            expected = _git(["rev-parse", str(canary["git_sha"])])
            self.assertEqual(expected.returncode, 0, expected.stderr)
            # a DIFFERENT resolvable commit's full sha (still a commit, so
            # the AC8 resolvability law alone cannot catch it — only the
            # derivation comparison can). The canary's own PARENT is used
            # instead of a stack-depth-relative pick: at release time the
            # canary commit sits exactly N deep and a relative-depth
            # choice can COLLAPSE onto the canary itself, failing this
            # control on a healthy tree (ancestry-deterministic; caught by
            # the implementation reviewer at the 6-commit stack).
            other = _git(["rev-parse", str(canary["git_sha"]) + "~1"])
            self.assertEqual(other.returncode, 0, other.stderr)
            other_full = other.stdout.strip().lower()
            expected_full = expected.stdout.strip().lower()
            self.assertNotEqual(other_full, expected_full)
            control_canary = canary
        else:
            # skip mode: the real canary sha is unreachable here, so the
            # control runs against a CONTROLLED COPY of the canary whose
            # git_sha is a commit that DOES resolve in this checkout (the
            # newest visible one), with the second-newest visible commit
            # as the retyped sha — both objects exist even at depth 2, so
            # the derivation-comparison discrimination is still exercised
            # end to end.
            visible = _visible_commit_shas(2)
            if len(visible) < 2 or visible[0] == visible[1]:
                _skip_marker("AC9 retyped-sha control (no second commit "
                             "resolves in this checkout)")
                return
            expected_full, other_full = visible[0], visible[1]
            control_canary = dict(canary)
            control_canary["git_sha"] = expected_full

        def _mutate(notes):
            seg, _how = current_segment(notes, version)
            assert seg is not None, "precondition: current segment found"
            shas = sorted(set(SHA40_RE.findall(seg)))
            assert shas, "precondition: current segment carries a sha"
            return notes.replace(shas[0], other_full, 1)

        report, _scratch = _mutated_support_copy(self, _mutate)
        problem = current_segment_derivation_problem(
            report, control_canary, version)
        self.assertTrue(
            problem,
            "a retyped current-segment sha (divergent tail, still "
            "cat-file-resolvable) must be flagged")
        self.assertIn("derivation mismatch", problem)
        self.assertIn(other_full, problem,
                      "the failure must name the offending sha")
        self.assertIn(expected_full, problem,
                      "the failure must name the expected derived sha")

    def test_unresolvable_canary_sha_fails_closed(self):
        # Final-critic round 2, finding 2: the canary git_sha is the root
        # of trust for AC9's derivation and AC11's disclosure predicate,
        # and the mode selector no longer reads it — so STRICT must FAIL
        # CLOSED on a value that resolves nowhere (typo/fabrication, the
        # exact hand-edited-evidence threat #490 guards). The control
        # loads a SCRATCH COPY of the canary report with git_sha set to a
        # syntactically valid 12-hex prefix that resolves nowhere and
        # exercises the helper's STRICT branch as a pure function of
        # (mode, sha-resolution result) — passing in BOTH modes, without
        # mutating the live file. The trailing live gate is the strict-run
        # instance of the same law against the REAL canary report.
        fabricated = "0000000decaf"
        self.assertEqual(len(fabricated), 12)
        self.assertRegex(fabricated, r"^[0-9a-f]{12}$")
        scratch = Path(tempfile.mkdtemp(prefix="zmem-490guard-"))
        self.addCleanup(shutil.rmtree, str(scratch), True)
        data = dict(_load(CANARY_PATH))
        data["git_sha"] = fabricated
        out = scratch / "canary-report.json"
        out.write_text(json.dumps(data, indent=1), encoding="utf-8")
        scratch_sha = str((_load(out) or {}).get("git_sha") or "")
        self.assertEqual(scratch_sha, fabricated)
        r = _git(["cat-file", "-t", scratch_sha])
        resolves = r.returncode == 0 and r.stdout.strip() == "commit"
        self.assertFalse(
            resolves,
            "control precondition: the scratch canary sha must resolve "
            "nowhere in this checkout")
        strict_problem = strict_canary_resolution_problem(
            "strict", scratch_sha, resolves)
        self.assertTrue(
            strict_problem,
            "the strict-mode canary-resolution branch must FAIL CLOSED on "
            "an unresolvable canary git_sha (the #490 threat)")
        self.assertIn(
            scratch_sha, strict_problem,
            f"the failure must name the offending sha; got: "
            f"{strict_problem}")
        self.assertIsNone(
            strict_canary_resolution_problem("skip", scratch_sha, resolves),
            "the skip-mode branch must stay quiet (mode-scoped helper)")
        # LIVE gate, bites in strict runs only: the real canary git_sha
        # must resolve as a commit via git cat-file -t.
        live_sha = str((_load(CANARY_PATH) or {}).get("git_sha") or "")
        self.assertTrue(_sha_argv_safe(live_sha),
                        "canary git_sha must be a hex object name "
                        "(N-06 option-injection guard)")
        live = _git(["cat-file", "-t", live_sha])
        live_resolves = (live.returncode == 0
                         and live.stdout.strip() == "commit")
        self.assertIsNone(
            strict_canary_resolution_problem(_MODE, live_sha, live_resolves),
            "in strict mode the LIVE canary git_sha must resolve as a "
            "commit via git cat-file -t (a typo'd or fabricated canary "
            "sha fails closed — it must not be able to select SKIP for "
            "itself)")


class StampCarryingCommitTest(unittest.TestCase):
    """AC10: generated_at strictly after the canary's AND no later than
    the carrying commit's committer timestamp."""

    def test_real_stamp_bounds_satisfied(self):
        canary = _load(CANARY_PATH)
        support = _load(SUPPORT_PATH)
        r = _git(["log", "-1", "--format=%cI", "--", str(SUPPORT_PATH)])
        carrying = r.stdout.strip() if r.returncode == 0 else ""
        if not _PROVENANCE_OK and not carrying:
            # skip mode: the carrying-commit bound degrades to the marker
            # when the lookup yields nothing at shallow depth; the
            # strictly-after half is a non-git invariant and stays
            # enforced.
            self.assertTrue(
                canary.get("generated_at") and support.get("generated_at"),
                "stamp fields must be present (fail closed)")
            self.assertGreater(
                _parse_iso(support.get("generated_at")),
                _parse_iso(canary.get("generated_at")),
                "support generated_at must stay strictly after the canary "
                "write even in skip mode (PR #451 convention; no git "
                "objects needed)")
            _skip_marker("AC10 support carrying-commit upper bound")
            return
        self.assertEqual(r.returncode, 0, r.stderr)
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
        carrying = r.stdout.strip() if r.returncode == 0 else ""
        if not _PROVENANCE_OK and not carrying:
            # skip mode: the canary carrying-commit bound degrades to the
            # marker; the non-git presence/parse invariant stays enforced.
            self.assertTrue(
                canary.get("generated_at"),
                "canary report carries no generated_at (fail closed)")
            _parse_iso(canary.get("generated_at"))
            _skip_marker("AC10 canary carrying-commit upper bound")
            return
        self.assertEqual(r.returncode, 0, r.stderr)
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
        # commit visible in this checkout (the OLDEST of the two newest,
        # via plain `git log -n 2` — no relative-depth argv, which is
        # exactly what a shallow checkout breaks), support stamp set 29 s
        # after it — the exact PR #487 pre-correction shape (+29 s past
        # 6be1af40). Runs at full strength in BOTH modes.
        ok, carrying_or_err = _past_commit_iso()
        self.assertTrue(ok, carrying_or_err)
        carrying = carrying_or_err
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
        if not _PROVENANCE_OK:
            # skip mode: the canary tree cannot be read here; assert only
            # the non-git invariants (both fail-closed canary fields are
            # present and the current support segment exists).
            canary = _load(CANARY_PATH)
            self.assertTrue(
                (canary or {}).get("git_sha"),
                "canary report carries no git_sha (fail closed)")
            self.assertTrue(
                (canary or {}).get("zmem_version"),
                "canary report carries no zmem_version (fail closed)")
            seg, _how = current_segment(
                (_load(SUPPORT_PATH) or {}).get("notes"), version)
            self.assertIsNotNone(
                seg,
                f"current release segment for {version} not found in the "
                "support notes (fail closed)")
            _skip_marker("AC11 real-pair disclosure predicate")
            return
        self.assertIsNone(
            problem,
            f"the live evidence pair must satisfy the disclosure "
            f"predicate ({problem})")

    def test_silent_version_mismatch_flagged(self):
        version = _release_version()
        # Build the mismatch shape from a CONTROLLED COPY of the canary so a
        # future committed-tree canary (tree version == canary zmem_version)
        # is a valid live pair rather than a precondition failure: when the
        # live pair already mismatches (working-tree run) it is used as-is;
        # otherwise the copy's zmem_version is forced to differ from the
        # recorded tree version, which drives exactly the same helper branch.
        # Runs at full strength in BOTH modes: in skip mode the copy's
        # git_sha moves to a commit that DOES resolve here (the newest
        # visible one) so `git show` reads a real tree and the forced
        # mismatch drives the same helper branch.
        canary = dict(_load(CANARY_PATH))
        if _PROVENANCE_OK:
            self.assertTrue(
                _sha_argv_safe(str(canary["git_sha"])),
                "the canary manifest's git_sha must be a bare 40-hex "
                "object name before it reaches any Git argv")
            show = _git(
                ["show", f"{canary['git_sha']}:release-manifest.json"])
        else:
            visible = _visible_commit_shas(1)
            self.assertTrue(
                visible, "no visible commit resolves in this checkout")
            canary["git_sha"] = visible[0]
            self.assertTrue(
                _sha_argv_safe(str(canary["git_sha"])),
                "report-derived commit shas are validated before their "
                "first Git invocation, in both modes")
            show = _git(
                ["show", f"{canary['git_sha']}:release-manifest.json"])
        self.assertEqual(show.returncode, 0, show.stderr)
        tree_version = re.search(
            r'"version"\s*:\s*"([^"]+)"', show.stdout).group(1)
        if canary.get("zmem_version") == tree_version:
            canary["zmem_version"] = str(tree_version) + "-mismatch-control"

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
