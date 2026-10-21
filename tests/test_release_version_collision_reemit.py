"""Equal-version re-emit collision + tag-sort discrimination (issue #354;
release-gate-integrity trace, implementation-review round 1 findings).

The frozen collision suite (test_release_version_collision.py) pins the
BUMPED collision shape; the #388 live incident shape — an equal version
whose release-manifest.json was re-emitted while that version is already
tagged — had no discriminating test, and the fixtures' single-digit tags
could not tell tuple sort from string sort. This file pins both.

Run: python tests/test_release_version_collision_reemit.py  (no pytest)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "release_gate.py"
PYTHON = sys.executable


def _git(cwd: Path, *args: str):
    r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        *args], cwd=str(cwd), capture_output=True, text=True,
                       timeout=60)
    # A silently-failing fixture step degrades the shape under test (the
    # gate would resolve "" refs against the index), so every step asserts.
    unittest.TestCase().assertEqual(r.returncode, 0,
                                    f"git {args[:2]} failed: {r.stdout} {r.stderr}")
    return r


def _gate(repo_root: Path, base: str):
    return subprocess.run(
        [PYTHON, str(GATE), "--check-manifest-freshness", "--base", base,
         "--repo-root", str(repo_root)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rgi-reemit-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "plugin.json").write_text(
            '{"name": "x", "version": "0.111.0"}\n', encoding="utf-8")
        (self.repo / "hooks").mkdir()
        (self.repo / "hooks" / "a.txt").write_text("surface\n", encoding="utf-8")
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        # The base commit MUST carry an emitted release-manifest.json (PR #390
        # review F-002): with no manifest at base, _manifest_blob(base) is
        # "absent" and ANY re-emit at HEAD reads as "changed" — a mutant that
        # only checks manifest PRESENCE would pass every test below. With the
        # manifest committed at base, the tests discriminate bytes-changed.
        self._reemit()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base manifest")
        self.base = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reemit(self):
        r = subprocess.run(
            [PYTHON, str(GATE), "--emit-manifest", "--repo-root", str(self.repo)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class EqualVersionReemitCollisionTest(_Fixture):
    """The #388 shape: equal tagged version + freshly re-emitted manifest."""

    def test_equal_version_reemit_at_tagged_version_fails_with_collision(self):
        _git(self.repo, "tag", "v0.111.0")
        # Same version, but the tree changes and the manifest is re-emitted:
        # the PR claims content under an already-published version.
        (self.repo / "hooks" / "a.txt").write_text("surface2\n", encoding="utf-8")
        self._reemit()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "reemit at equal version")
        r = _gate(self.repo, self.base)
        self.assertNotEqual(
            r.returncode, 0,
            f"equal-version re-emit at a tagged version must FAIL (the #388 "
            f"live-incident shape), got exit 0: {r.stdout}",
        )
        self.assertIn("collision", (r.stdout + r.stderr).lower())

    def test_untagged_equal_version_reemit_passes_when_fresh(self):
        # No v0.111.0 tag: the same re-emit is a legitimate mid-flight
        # refresh; the manifest is fresh, so the gate stays green.
        (self.repo / "hooks" / "a.txt").write_text("surface2\n", encoding="utf-8")
        self._reemit()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "reemit untagged")
        r = _gate(self.repo, self.base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("release manifest fresh", r.stdout)


class TagSortKeyTest(_Fixture):
    """Multi-digit tags must sort by version tuple, not by string."""

    def test_newest_tag_is_resolved_by_tuple_not_string(self):
        # String sort would rank "0.9.0" above "0.111.0"; a 0.105.0 target
        # is below the true newest and must fail with the ordering error.
        (self.repo / "plugin.json").write_text(
            '{"name": "x", "version": "0.105.0"}\n', encoding="utf-8")
        self._reemit()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "target 0.105.0")
        _git(self.repo, "tag", "v0.9.0")
        _git(self.repo, "tag", "v0.111.0")
        r = _gate(self.repo, self.base)
        self.assertNotEqual(
            r.returncode, 0,
            f"0.105.0 < newest tag 0.111.0 (with v0.9.0 also present) must "
            f"FAIL with an ordering error; string sort would pass it: "
            f"{r.stdout}",
        )
        combined = (r.stdout + r.stderr).lower()
        self.assertIn("ordering", combined)
        self.assertIn("v0.111.0", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
