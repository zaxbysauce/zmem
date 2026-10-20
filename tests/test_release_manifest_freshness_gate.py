"""Release-gate integrity: manifest-freshness acceptance criteria (RED pins).

Four verified defects at the branch base; a separate implementer must make
these true. This file freezes the acceptance criteria for the
--check-manifest-freshness equal-version blind spot and the two workflow
ordering regressions:

  1. EQUAL-VERSION STALE MANIFEST: a change that keeps every manifest
     version EQUAL to the base but re-emits release-manifest.json (bytes
     differ from base) and leaves the tree out of sync with the recorded
     digests exits 0 today via the "not a release-prep change" skip. It
     must exit 1.
  2. REAL REPO EQUAL-VERSION VERIFY: `--check-manifest-freshness --base
     HEAD` on the real repo must actually RUN the verify (print "release
     manifest fresh"), not take the skip path.
  3. release.yml runs --verify-manifest AFTER the `gh release view`
     release-exists early exit, so a stale manifest ships silently whenever
     the release already exists. Verify must come first.
  4. ci.yml's Manifest freshness step fetches with --no-tags, so any
     tag-based freshness logic in the PR lane can never see a tag. The
     fetch must fetch tags.

Run: python tests/test_release_manifest_freshness_gate.py  (no pytest;
CI runs each tests/test_*.py directly). Class-selective:
python tests/test_release_manifest_freshness_gate.py WorkflowOrderTest
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"
PYTHON = sys.executable

# Spec-load the gate exactly like tests/test_release_parity.py (the module
# lives at scripts/release_gate.py, not in a package).
_spec = importlib.util.spec_from_file_location(
    "zmem_release_gate_freshness", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


class _FixtureRepoTest(unittest.TestCase):
    """Shared synthetic-fixture plumbing: temp dir + pinned-identity git."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-freshness-"))
        self.addCleanup(
            lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _git(self, repo: Path, *args: str) -> None:
        r = subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=str(repo), capture_output=True, text=True, timeout=60)
        self.assertEqual(
            r.returncode, 0,
            f"fixture git {' '.join(args)} failed:\n{r.stderr}")

    def _git_out(self, repo: Path, *args: str) -> str:
        r = subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=str(repo), capture_output=True, text=True, timeout=60)
        self.assertEqual(
            r.returncode, 0,
            f"fixture git {' '.join(args)} failed:\n{r.stderr}")
        return r.stdout

    def _emit(self, repo: Path) -> None:
        # The emit runs from the REAL repo root (release_gate.py resolves
        # its own REPO_ROOT; --repo-root points the work at the fixture).
        r = subprocess.run(
            [PYTHON, str(GATE_PY), "--emit-manifest",
             "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))
        self.assertEqual(
            r.returncode, 0,
            f"fixture --emit-manifest failed:\n{r.stdout}\n{r.stderr}")

    def _freshness(self, repo: Path, base: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(GATE_PY), "--check-manifest-freshness",
             "--base", base, "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))


class EqualVersionStaleManifestTest(_FixtureRepoTest):
    """Equal-version release-prep change with a stale manifest must FAIL.

    Fixture (per the acceptance contract): commit A seeds
    .claude-plugin/plugin.json 0.5.0 + hooks/a.txt and folds an emitted
    release-manifest.json into the commit (base ref `base-point`); commit B
    keeps 0.5.0, edits hooks/a.txt and RE-EMITS the manifest (bytes differ
    from base); commit C re-edits hooks/a.txt WITHOUT re-emitting, so the
    recorded tree digests no longer describe the tree.
    """

    def test_equal_version_stale_manifest_fails_the_gate(self):
        repo = self.tmp / "eq-stale"
        (repo / ".claude-plugin").mkdir(parents=True)
        (repo / "hooks").mkdir(parents=True)
        (repo / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "zmem", "version": "0.5.0"}),
            encoding="utf-8")
        (repo / "hooks" / "a.txt").write_text("surface r1\n", encoding="utf-8")
        self._git(repo, "init", "-q")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "A: seed 0.5.0")
        self._emit(repo)  # manifest describes the r1 tree
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "--amend", "-qm",
                  "A: seed 0.5.0 + emitted manifest")
        self._git(repo, "branch", "base-point")

        # Commit B: equal version, edited surface, manifest re-emitted.
        (repo / "hooks" / "a.txt").write_text("surface r2\n", encoding="utf-8")
        self._emit(repo)
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "B: equal version, re-emit manifest")
        # Commit C: surface edited again, manifest NOT re-emitted.
        (repo / "hooks" / "a.txt").write_text("surface r3\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "C: edit WITHOUT re-emitting")

        # --- fixture sanity (must hold at base; these are NOT the gate) ---
        base_manifest = self._git_out(
            repo, "show", "base-point:release-manifest.json")
        head_manifest = (repo / "release-manifest.json").read_text(
            encoding="utf-8")
        self.assertNotEqual(
            base_manifest, head_manifest,
            "fixture rot: HEAD manifest bytes must differ from base")
        base_version = json.loads(self._git_out(
            repo, "show", "base-point:.claude-plugin/plugin.json"))["version"]
        head_version = json.loads(
            (repo / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"))["version"]
        self.assertEqual(base_version, head_version, "versions stay equal")
        recorded = json.loads(head_manifest)
        self.assertNotEqual(
            recorded["files"]["hooks/a.txt"],
            gate.drift.file_hash(repo / "hooks" / "a.txt"),
            "fixture rot: recorded digests must differ from the tree")

        # --- the acceptance criterion (RED at base: exits 0 via the skip) ---
        r = self._freshness(repo, "base-point")
        self.assertEqual(
            r.returncode, 1,
            "an equal-version change that re-emits release-manifest.json "
            "and leaves the tree out of sync with the recorded digests "
            f"must fail the gate:\n{r.stdout}\n{r.stderr}")


class RealRepoEqualVersionVerifyTest(unittest.TestCase):
    """`--base HEAD` on the real repo must RUN the verify, not skip it.

    Every manifest version equals itself at HEAD, so today's gate prints
    the "not a release-prep change" skip and never verifies. The contract:
    equal versions do not exempt the run from verifying that the committed
    manifest describes the tree (it is fresh on the real repo today, so the
    verify line must appear and the exit must be 0).
    """

    def test_equal_version_base_verifies_on_the_real_repo(self):
        r = subprocess.run(
            [PYTHON, str(GATE_PY), "--check-manifest-freshness",
             "--base", "HEAD"],
            capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT))
        combined = r.stdout + r.stderr
        self.assertEqual(
            r.returncode, 0,
            "the real repo's manifest is fresh, so the equal-version "
            f"verify must pass:\n{combined}")
        self.assertNotIn(
            "not a release-prep change", combined,
            "an equal-version base must not take the skip path — the "
            "verify must run")
        self.assertIn(
            "release manifest fresh", combined,
            "the equal-version run must actually verify the manifest and "
            "say so")


class WorkflowOrderTest(unittest.TestCase):
    """Workflow pins for the two ordering regressions.

    release.yml: --verify-manifest runs inside the publish step AFTER the
    `gh release view` release-exists early exit, so when the release
    already exists the verify is skipped and a stale manifest ships
    silently. Verify must come BEFORE the release-exists check.

    ci.yml: the Manifest freshness step fetches the base with
    --no-tags, so the PR lane can never see a tag. The fetch must fetch
    tags.
    """

    @staticmethod
    def _code_lines(text: str) -> str:
        # Comment-stripped text: comparisons measure CODE order, not the
        # position of a phrase inside an explanatory comment.
        return "\n".join(
            ln for ln in text.splitlines()
            if not ln.lstrip().startswith("#"))

    def test_release_yml_verifies_manifest_before_release_view(self):
        yml = (REPO_ROOT / ".github" / "workflows" / "release.yml"
               ).read_text(encoding="utf-8")
        code = self._code_lines(yml)
        verify = code.find("--verify-manifest")
        view = code.find("gh release view")
        self.assertGreater(
            verify, -1, "release.yml lost the --verify-manifest step")
        self.assertGreater(
            view, -1, "release.yml lost the gh release view check")
        self.assertLess(
            verify, view,
            "--verify-manifest must run BEFORE the `gh release view` "
            "release-exists early exit — after it, an existing release "
            "skips the verify and a stale manifest ships silently")

    def test_ci_freshness_step_fetches_tags(self):
        yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml"
               ).read_text(encoding="utf-8")
        m = re.search(
            r"- name: [^\n]*Manifest freshness[^\n]*\n(.*?)(?=\n[ ]*- name: |\Z)",
            yml, re.DOTALL)
        self.assertIsNotNone(m, "ci.yml lost the Manifest freshness step")
        step = m.group(1)
        run_at = step.find("run:")
        self.assertGreater(run_at, -1, "the step has no run block")
        block = step[run_at:]
        self.assertIn(
            "--tags", block,
            "the Manifest freshness step must fetch tags (its gate logic "
            "needs tag visibility in the PR lane)")
        self.assertNotIn(
            "--no-tags", block,
            "the Manifest freshness step fetches with --no-tags, so tag-"
            "based gate logic can never see a tag in the PR lane")


if __name__ == "__main__":
    unittest.main(verbosity=2)
