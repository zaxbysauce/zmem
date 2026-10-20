"""Release-gate integrity: version collision / ordering acceptance (RED pins).

--check-manifest-freshness today knows nothing about git tags: a release-prep
change declaring a version that is ALREADY tagged (collision), or one that is
OLDER than the newest existing tag (ordering), passes cleanly. These tests
freeze the post-fix contract:

  - collision: HEAD declares 0.9.0 and tag v0.9.0 exists at that commit
    → exit 1, output contains "collision";
  - ordering: HEAD declares 0.8.5 while tags v0.9.0 and v0.8.0 exist (and no
    v0.8.5) → exit 1, output contains "ordering";
  - uncollided: HEAD declares 0.9.5, newest tag v0.9.0, fresh manifest
    → exit 0 (pin: the new checks must not over-trigger).

Run: python tests/test_release_version_collision.py  (no pytest; CI runs
each tests/test_*.py directly). Class-selective:
python tests/test_release_version_collision.py VersionOrderingTest
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
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"
PYTHON = sys.executable


class _CollisionFixtureTest(unittest.TestCase):
    """Shared plumbing: temp dir, pinned-identity git fixture, gate runner.

    _fixture builds a synthetic repo whose steps are (version, tag) pairs:
    step 0 seeds .claude-plugin/plugin.json + hooks/a.txt and becomes the
    `base-point` branch; every later step rewrites the manifest version,
    edits the surface file, folds a freshly emitted release-manifest.json
    into its commit, and optionally tags that commit.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-collision-"))
        self.addCleanup(
            lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _git(self, repo: Path, *args: str) -> None:
        r = subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=str(repo), capture_output=True, text=True, timeout=60)
        self.assertEqual(
            r.returncode, 0,
            f"fixture git {' '.join(args)} failed:\n{r.stderr}")

    def _emit(self, repo: Path) -> None:
        r = subprocess.run(
            [PYTHON, str(GATE_PY), "--emit-manifest",
             "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))
        self.assertEqual(
            r.returncode, 0,
            f"fixture --emit-manifest failed:\n{r.stdout}\n{r.stderr}")

    def _fixture(self, name: str, steps: list) -> Path:
        repo = self.tmp / name
        (repo / ".claude-plugin").mkdir(parents=True)
        (repo / "hooks").mkdir(parents=True)
        (repo / "hooks" / "a.txt").write_text("surface r1\n", encoding="utf-8")
        self._git(repo, "init", "-q")
        for i, (version, tag) in enumerate(steps):
            (repo / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "zmem", "version": version}),
                encoding="utf-8")
            if i > 0:
                (repo / "hooks" / "a.txt").write_text(
                    f"surface r{i + 1}\n", encoding="utf-8")
            if i == 0:
                self._git(repo, "add", "-A")
                self._git(repo, "commit", "-qm", f"seed {version}")
                self._git(repo, "branch", "base-point")
            else:
                self._emit(repo)
                self._git(repo, "add", "-A")
                self._git(repo, "commit", "-qm", f"declare {version}")
            if tag:
                self._git(repo, "tag", tag)
        return repo

    def _freshness(self, repo: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(GATE_PY), "--check-manifest-freshness",
             "--base", "base-point", "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))


class CollisionTest(_CollisionFixtureTest):
    """HEAD re-declares an ALREADY-TAGGED version → collision failure."""

    def test_already_tagged_version_fails_with_collision(self):
        repo = self._fixture("collision", [
            ("0.8.0", None),
            ("0.9.0", "v0.9.0"),  # fresh manifest + tag at HEAD
        ])
        r = self._freshness(repo)
        combined = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 1,
            "a release-prep change declaring a version that is already "
            f"tagged (v0.9.0) must fail the gate:\n{combined}")
        self.assertIn(
            "collision", combined.lower(),
            "the failure must name the defect: the declared version "
            "collides with an existing tag")


class VersionOrderingTest(_CollisionFixtureTest):
    """HEAD declares a version OLDER than the newest tag → ordering failure."""

    def test_version_below_newest_tag_fails_with_ordering(self):
        repo = self._fixture("ordering", [
            ("0.8.0", "v0.8.0"),
            ("0.9.0", "v0.9.0"),
            ("0.8.5", None),  # fresh manifest at HEAD, no v0.8.5 tag
        ])
        r = self._freshness(repo)
        combined = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 1,
            "a release-prep change declaring 0.8.5 while v0.9.0 is already "
            f"tagged must fail the gate (releasing backwards):\n{combined}")
        self.assertIn(
            "ordering", combined.lower(),
            "the failure must name the defect: the declared version is "
            "out of order versus the newest existing tag")


class UncollidedVersionTest(_CollisionFixtureTest):
    """Floor pin: a normal forward bump above the newest tag still passes.

    GREEN at base (today's gate passes a fresh manifest) and must stay
    green after the collision/ordering checks land — they may not
    over-trigger on an ordinary forward release.
    """

    def test_version_above_newest_tag_passes(self):
        repo = self._fixture("uncollided", [
            ("0.9.0", "v0.9.0"),
            ("0.9.5", None),  # fresh manifest at HEAD, above every tag
        ])
        r = self._freshness(repo)
        combined = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 0,
            "0.9.5 above the newest tag v0.9.0 with a fresh manifest is a "
            f"normal release-prep change and must pass:\n{combined}")
        self.assertNotIn(
            "::error::", combined,
            "no gate violation may be reported for an ordinary forward bump")


if __name__ == "__main__":
    unittest.main(verbosity=2)
