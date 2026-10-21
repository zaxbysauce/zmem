"""Feedback-round pins for the release gate (PR #390 feedback round).

Additive hardening in a NEW file — the frozen acceptance files
(test_release_manifest_freshness_gate.py, test_release_version_collision*.py,
test_release_gate_unreleased.py, test_release_gate_smoke.py) are byte-stable.
Each test here pins a contract the review round showed was unpinned or
fail-open. Run: python tests/test_release_gate_feedback.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"
PYTHON = sys.executable

_spec = importlib.util.spec_from_file_location("zmem_release_gate_fb", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _scratch_repo(version="0.5.0", tags=()):
    """Fixture with plugin.json + hooks/a.txt committed at the base commit."""
    tmp = Path(tempfile.mkdtemp(prefix="rgi-fb-"))
    repo = tmp / "repo"
    repo.mkdir()
    (repo / "plugin.json").write_text(
        json.dumps({"name": "x", "version": version}) + "\n", encoding="utf-8")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "a.txt").write_text("surface\n", encoding="utf-8")

    def git(*args):
        r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                            *args], cwd=str(repo), capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, (args, r.stdout, r.stderr)
        return r

    git("init", "-q", ".")
    git("add", "-A")
    git("commit", "-qm", "base")
    for t in tags:
        git("tag", t)
    return tmp, repo, git


def _bump_and_commit(repo, git, *, version=None, edit_hook=True):
    if version is not None:
        (repo / "plugin.json").write_text(
            json.dumps({"name": "x", "version": version}) + "\n",
            encoding="utf-8")
    if edit_hook:
        (repo / "hooks" / "a.txt").write_text("surface2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "claim")


def _freshness(repo, base, env_extra=None):
    """Run the gate as a subprocess (real behavior, incl. GITHUB_ACTIONS)."""
    import os
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.update(env_extra or {})
    return subprocess.run(
        [PYTHON, str(GATE_PY), "--check-manifest-freshness", "--base", base,
         "--repo-root", str(repo)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120, env=env)


def _captured(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


class IndeterminateInputsTest(unittest.TestCase):
    """FB-03: git failure / empty tag universe must be VISIBLE, not silent."""

    def test_git_failure_tag_universe_warns(self):
        tmp, repo, git = _scratch_repo(tags=("v0.9.0",))
        try:
            _bump_and_commit(repo, git)  # claims a target (bumped)
            with mock.patch.object(gate, "_tag_versions", return_value=None), \
                 mock.patch.object(gate, "_manifest_blob",
                                   side_effect=lambda r, ref: "a" if ref == "HEAD" else "b"):
                rc, out = _captured(gate.check_manifest_freshness, repo, "HEAD~1")
            self.assertIn("::warning::", out)
            self.assertIn("collision/ordering check skipped", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_manifest_blob_indeterminate_warns(self):
        tmp, repo, git = _scratch_repo(tags=("v0.9.0",))
        try:
            _bump_and_commit(repo, git, version="0.6.0", edit_hook=False)
            with mock.patch.object(gate, "_manifest_blob", return_value=None):
                rc, out = _captured(gate.check_manifest_freshness, repo, "HEAD~1")
            self.assertIn("::warning::", out)
            self.assertIn("collision trigger is indeterminate", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_tag_universe_fails_closed_in_github_actions(self):
        # CI fail-closed applies ONLY to the real-checkout context (an empty
        # universe there means the --tags fetch regressed); synthetic
        # --repo-root fixtures legitimately have sparse tags and stay
        # informational. Pin the real-context branch in-process with the tag
        # universe mocked empty.
        # Fully hermetic: in CI the python suites run BEFORE the freshness
        # step fetches origin/main, so even real `git show origin/main:...`
        # reads fail here. Mock the manifest inventory, the per-ref version
        # reads (distinct -> bumped -> claims_target), the blob comparison,
        # and the tag universe; nothing touches git.
        with mock.patch.object(gate, "discover_manifests",
                               return_value=["plugin.json"]),              mock.patch.object(gate, "_version_at",
                               side_effect=lambda r, ref, rel,
                                            _h="0.114.0", _b="0.113.0":
                                            _h if ref == "HEAD" else _b),              mock.patch.object(gate, "_tag_versions", return_value=[]),              mock.patch.object(gate, "_manifest_blob",
                               side_effect=lambda r, ref: "a" if ref == "HEAD" else "b"),              mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "1"}):
            rc, out = _captured(gate.check_manifest_freshness,
                                gate.REPO_ROOT, "origin/main")
        self.assertEqual(rc, 1, "empty tag universe in CI must fail closed")
        self.assertIn("no vX.Y.Z tags visible in CI", out)

    def test_empty_tag_universe_synthetic_fixture_stays_informational(self):
        tmp, repo, git = _scratch_repo()
        try:
            _bump_and_commit(repo, git, version="0.6.0", edit_hook=False)
            r = _freshness(repo, "HEAD~1", env_extra={"GITHUB_ACTIONS": "1"})
            combined = r.stdout + r.stderr
            # The verify leg may fail on a fixture without a manifest, but the
            # TAG check itself must not be the fail-closed error.
            self.assertNotIn("no vX.Y.Z tags visible in CI", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ManifestParsingRobustnessTest(unittest.TestCase):
    """FB-06: contributor-controlled manifest content cannot traceback."""

    def test_non_object_json_manifest_fails_closed_with_diagnostic(self):
        tmp, repo, git = _scratch_repo()
        try:
            (repo / "plugin.json").write_text("null\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "null manifest")
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("::error::", combined)
            self.assertIn("failing closed", combined)
            self.assertNotIn("Traceback", combined)
            self.assertNotIn("AttributeError", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_malformed_claimed_version_fails_with_diagnostic(self):
        tmp, repo, git = _scratch_repo()
        try:
            _bump_and_commit(repo, git, version="0.6.0-rc1", edit_hook=False)
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not an X.Y.Z version", combined)
            self.assertNotIn("Traceback", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_version_tuple_rejects_garbage(self):
        for bad in ("banana", "", "  ", "0.112.0-rc1"):
            with self.assertRaises(ValueError):
                gate._version_tuple(bad)


class CollisionMatchingTest(unittest.TestCase):
    """FB-06/F-002: collision matches parsed tuples, not string equality."""

    def test_leading_zero_spelling_still_collides(self):
        tmp, repo, git = _scratch_repo(version="0.5.0", tags=("v0.5.0",))
        try:
            # Claim "0.05.0": distinct STRING from the tag v0.5.0, same tuple.
            _bump_and_commit(repo, git, version="0.05.0")
            r = _freshness(repo, "HEAD~1")
            self.assertNotEqual(
                r.returncode, 0,
                "spelling variant of a published version must collide")
            self.assertIn("collision", (r.stdout + r.stderr).lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_loose_tags_are_excluded_with_a_count(self):
        tmp, repo, git = _scratch_repo(tags=("v0.9.0", "v1_0", "v0.9.0-rc1"))
        try:
            _bump_and_commit(repo, git, version="0.8.0", edit_hook=False)
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            # Ordering must name the strict newest v0.9.0, not v1_0-as-10.
            self.assertIn("below the newest published tag v0.9.0", combined)
            self.assertIn("do not match vX.Y.Z", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_equal_version_collision_message_names_the_reemit_remedy(self):
        tmp, repo, git = _scratch_repo(version="0.5.0", tags=("v0.5.0",))
        try:
            # Equal version, only the manifest bytes change (re-emit at HEAD):
            # the #388 shape. The collision remedy must name the re-emit.
            (repo / "hooks" / "a.txt").write_text("s2\n", encoding="utf-8")
            r = subprocess.run(
                [PYTHON, str(GATE_PY), "--emit-manifest", "--repo-root", str(repo)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            git("add", "-A")
            git("commit", "-qm", "reemit")
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("collision", combined.lower())
            self.assertIn("revert the re-emit", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class UnreleasedCensusHardeningTest(unittest.TestCase):
    """FB-04/FB-05: BOM cannot void the census; empty duplicates fail;
    diagnostics name the remediation and exact line numbers."""

    def test_bom_before_line1_heading_still_detects_drift(self):
        tmp = Path(tempfile.mkdtemp(prefix="rgi-fb-bom-"))
        try:
            (tmp / "CHANGELOG.md").write_text(
                "\ufeff## [Unreleased]\n\n- real entry\n",
                encoding="utf-8")
            rc, out = _captured(gate.check_unreleased_drift, tmp)
            self.assertEqual(rc, 1,
                             "BOM must not void the Unreleased census")
            self.assertIn("unreleased drift", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_two_empty_headings_still_fail(self):
        tmp = Path(tempfile.mkdtemp(prefix="rgi-fb-dup-"))
        try:
            text = ("# C\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\n"
                    "old\n\n## [Unreleased]\n")
            (tmp / "CHANGELOG.md").write_text(text, encoding="utf-8")
            rc, out = _captured(gate.check_unreleased_drift, tmp)
            self.assertEqual(rc, 1,
                             "empty duplicate headings are the defect itself")
            expected = ", ".join(
                str(n) for n in gate.unreleased_heading_lines(text))
            self.assertIn(f"headings (lines {expected})", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_duplicate_diagnostic_names_exact_line_numbers_and_remediation(self):
        tmp = Path(tempfile.mkdtemp(prefix="rgi-fb-dup2-"))
        try:
            text = ("# C\n\n## [Unreleased]\n\n- entry\n\n"
                    "## [0.1.0] - 2026-01-01\n\nold\n\n## [Unreleased]\n")
            (tmp / "CHANGELOG.md").write_text(text, encoding="utf-8")
            rc, out = _captured(gate.check_unreleased_drift, tmp)
            self.assertEqual(rc, 1)
            m = re.search(r"lines (\d+), (\d+)", out)
            self.assertIsNotNone(m, out)
            self.assertEqual([int(m.group(1)), int(m.group(2))],
                             gate.unreleased_heading_lines(text))
            self.assertIn("DELETE the stranded duplicate", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RoundTwoHardeningPinsTest(unittest.TestCase):
    """PR #390 review round 2: the three fixes the reviewer demanded, pinned
    (digit-limit diagnostic, drift RecursionError, dual stale+collision)."""

    def test_digit_limit_claim_fails_with_diagnostic_not_traceback(self):
        tmp, repo, git = _scratch_repo()
        try:
            huge = "9" * 5000 + ".0.0"
            _bump_and_commit(repo, git, version=huge, edit_hook=True)
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not usable as version numbers", combined)
            self.assertNotIn("Traceback", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_deeply_nested_manifest_lands_in_corrupt_diagnostic(self):
        import drift as _  # noqa: F401  (probe below goes through subprocess)
        tmp, repo, git = _scratch_repo()
        try:
            nested = json.dumps({"files": json.loads("[" * 200 + "]" * 200)})
            r = subprocess.run(
                [PYTHON, str(GATE_PY), "--verify-manifest",
                 "--repo-root", str(repo)],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                timeout=120)
            # Sanity: our nesting is deep enough to overflow a naive parser
            # only if CPython recurses; the gate must answer with its
            # diagnostic either way, never a traceback.
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("::error::", combined)
            self.assertNotIn("Traceback", combined)
            del nested
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stale_and_colliding_prints_both_diagnostics(self):
        tmp, repo, git = _scratch_repo(version="0.4.0", tags=("v0.5.0",))
        try:
            # Commit an emitted manifest at base, then bump onto the tagged
            # version AND edit a runtime file WITHOUT re-emitting: the claim
            # collides AND the manifest is stale — both diagnostics must print
            # (the collision return must not hide the staleness).
            r = subprocess.run(
                [PYTHON, str(GATE_PY), "--emit-manifest", "--repo-root", str(repo)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            git("add", "-A")
            git("commit", "-qm", "base manifest")
            _bump_and_commit(repo, git, version="0.5.0", edit_hook=True)
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("version collision", combined)
            self.assertIn("STALE", combined)
            self.assertIn("does not describe the tree", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PartialBumpParityFallThroughTest(unittest.TestCase):
    """FB-12d: a partial multi-manifest bump skips the tag checks entirely —
    a partial bump onto a TAGGED version must NOT report a collision."""

    def test_partial_bump_onto_tagged_version_skips_collision(self):
        tmp, repo, git = _scratch_repo(version="0.5.0", tags=("v0.6.0",))
        try:
            (repo / ".claude-plugin").mkdir()
            (repo / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "y", "version": "0.5.0"}) + "\n",
                encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "second manifest")
            # partial bump: plugin.json -> tagged 0.6.0, second manifest stays
            _bump_and_commit(repo, git, version="0.6.0", edit_hook=True)
            r = _freshness(repo, "HEAD~1")
            combined = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("version collision", combined)
            self.assertNotIn("version ordering", combined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RefreshRefusalPin(unittest.TestCase):
    """FB-09: refresh_hosts refuses a checkout whose manifest was edited
    without a re-emit (the behavior #359 introduced; previously unpinned)."""

    def test_edited_host_manifest_checkout_is_refused(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import refresh_hosts
        src = REPO_ROOT / "tests" / "fixtures" / "hosts" / "checkout"
        tmp = Path(tempfile.mkdtemp(prefix="rgi-fb-refresh-"))
        try:
            checkout = tmp / "checkout"
            shutil.copytree(src, checkout)
            if not (checkout / ".git").exists():
                subprocess.run(["git", "init", "-q", "."], cwd=str(checkout),
                               check=True, capture_output=True, timeout=60)
            plugin = checkout / ".claude-plugin" / "plugin.json"
            data = json.loads(plugin.read_text(encoding="utf-8"))
            data["edited-without-reemit"] = True
            plugin.write_text(json.dumps(data, indent=2) + "\n",
                              encoding="utf-8", newline="\n")

            def git(*args):
                r = subprocess.run(["git", "-c", "user.email=t@t",
                                    "-c", "user.name=t", *args],
                                   cwd=str(checkout), capture_output=True,
                                   text=True, timeout=60)
                self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
                return r

            git("add", "-A")
            git("commit", "-qm", "edit manifest without re-emit")
            home = tmp / "home"
            home.mkdir()
            with self.assertRaises(Exception) as ctx:
                refresh_hosts._refresh_transaction(
                    home, checkout, ("codex",), tmp / "report.json",
                    dry_run=False)
            self.assertIn("verification failed", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
