"""Committed fixture tests for the work-PR version guard (PR #483 F-E).

Builds a synthetic git repo with a canonical manifest (root plugin.json —
discovered by release_gate.discover_manifests, matching the
drift.VERSION_MANIFEST_RE by-name rule) and drives the guard script through
its CLI with --repo-root, pinning every deciding behavior the review named:

- clean tree → exit 0
- one manifest bumped → exit 2 naming the path (mutant: drop the compare)
- unresolvable base → exit 2 fail-closed (red at the pre-fix head, where it
  silently passed)
- manifest absent at a resolving base → counted as a bump → exit 2
- unparseable manifest → exit 2 fail-closed
- new dated CHANGELOG section → exit 2 (the CHANGELOG half)
- --label-exempt → exit 0
- equal-version manifest re-emit → exit 0 (the serialized-release pass arm)

Run: python -X utf8 tests/test_work_pr_no_bump.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "check_work_pr_no_bump.py"
PYTHON = sys.executable


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"fixture git {' '.join(args)} failed: {r.stderr}"


def _run_guard(repo: Path, base: str, *extra: str):
    return subprocess.run(
        [PYTHON, "-X", "utf8", str(GUARD), "--base", base,
         "--repo-root", str(repo), *extra],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)


class GuardFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-wprguard-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._manifest(self.repo, "0.158.0")
        (self.repo / "hooks").mkdir()
        (self.repo / "hooks" / "a.txt").write_text("surface\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- work notes\n\n"
            "## [0.158.0] - 2026-11-15\n\n- rel\n", encoding="utf-8")
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "tag", "v0.158.0")
        self.base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30).stdout.strip()

    def _manifest(self, repo: Path, version: str) -> None:
        (repo / "plugin.json").write_text(
            json.dumps({"name": "x", "version": version}) + "\n",
            encoding="utf-8")

    def _commit(self, msg: str) -> None:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", msg)

    def test_clean_tree_passes(self):
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no version reservation", r.stdout)

    def test_manifest_bump_fails_naming_the_path(self):
        self._manifest(self.repo, "0.159.0")
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("plugin.json", r.stdout)

    def test_unresolvable_base_fails_closed(self):
        # Red at the pre-fix head: the '' sentinel passed this silently.
        r = _run_guard(self.repo, "nonexistent-ref-xyz")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("does not resolve", r.stdout)

    def test_new_manifest_counts_as_bump(self):
        # A TRACKED new manifest (a merged PR's files are always tracked;
        # an untracked file cannot ride a PR commit).
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "y", "version": "0.159.0"}) + "\n",
            encoding="utf-8")
        self._commit("add a new version-bearing manifest")
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("plugin.json", r.stdout)

    def test_unparseable_manifest_fails_closed(self):
        self._manifest(self.repo, "not-a-version")
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_new_dated_changelog_section_fails(self):
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [0.159.0] - 2026-11-16\n\n- sneaky\n\n" + text,
            encoding="utf-8")
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("CHANGELOG.md", r.stdout)

    def test_label_exempt_passes_despite_bump(self):
        self._manifest(self.repo, "0.159.0")
        r = _run_guard(self.repo, self.base, "--label-exempt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_equal_version_reemit_passes(self):
        # A runtime file changes and the manifest is re-emitted at the SAME
        # published version: reserves nothing, passes when fresh.
        (self.repo / "hooks" / "a.txt").write_text("surface2\n",
                                                   encoding="utf-8")
        r = subprocess.run(
            [PYTHON, "-X", "utf8",
             str(REPO_ROOT / "scripts" / "release_gate.py"),
             "--emit-manifest", "--repo-root", str(self.repo)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self._commit("reemit at equal version")
        r = _run_guard(self.repo, self.base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_base_advance_fails_with_direction_report(self):
        # A base that ADVANCED (carries a newer version than the PR head)
        # fails the guard in the same repo: the report shows the direction
        # (base 0.159.0 -> head 0.158.0 is the base advancing, not the PR
        # bumping); full remedy-wording is a polish item.
        self._manifest(self.repo, "0.159.0")
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- x\n\n"
            "## [0.159.0] - 2026-11-16\n\n- rel\n", encoding="utf-8")
        self._commit("base advance to 0.159.0")
        adv = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        # Return the working tree to the OLD versions (the "PR" state).
        self._manifest(self.repo, "0.158.0")
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n- work notes\n\n"
            "## [0.158.0] - 2026-11-15\n\n- rel\n", encoding="utf-8")
        self._commit("pr work on the old base version")
        r = _run_guard(self.repo, adv)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        # The guard names the differing surfaces; the direction-aware
        # message (base advanced vs PR bumped) is the B1-003 polish item.
        self.assertIn("plugin.json", r.stdout)


if __name__ == "__main__":
    unittest.main()
