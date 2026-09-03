"""Release-contract tests (automated releases, version parity).

The release problem this guards against: version bumps merged to main
without a matching git tag / GitHub Release (v0.8.5/v0.8.6/v0.8.8 were never
tagged; v0.9.0/v0.10.0 shipped unpublished until retrofitted by hand), and
partial version bumps that leave host-facing manifests disagreeing. The
fix under test: scripts/release_gate.py (parity + CHANGELOG + tag decision)
driven by .github/workflows/release.yml on every push to main.

Run: python tests/test_release_parity.py  (no pytest; house convention)
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PY = REPO_ROOT / "scripts" / "release_gate.py"
WORKFLOW_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYTHON = sys.executable

# Spec-load (no sys.path pollution; the module lives outside the package).
_spec = importlib.util.spec_from_file_location("zmem_release_gate", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


class ManifestParityTest(unittest.TestCase):
    """Every tracked host-facing manifest agrees on one version, and the
    CHANGELOG's newest released section matches it — the two invariants
    whose rot made releases invisible."""

    def test_all_manifests_parse_and_agree(self):
        manifests = gate.discover_manifests()
        self.assertGreaterEqual(
            len(manifests), gate.MIN_MANIFESTS,
            f"only {len(manifests)} host-facing manifests tracked — did a "
            f"release surface get deleted? found: {manifests}",
        )
        versions = {p: gate.read_version(p) for p in manifests}
        for p, v in versions.items():
            self.assertIsNotNone(v, f"{p} declares no parseable version")
        distinct = set(versions.values())
        self.assertEqual(
            len(distinct), 1,
            f"host-facing manifests disagree on version (partial bump): "
            f"{ {p: v for p, v in versions.items()} }",
        )

    def test_changelog_newest_release_matches_manifests(self):
        manifests = gate.discover_manifests()
        version = gate.read_version(manifests[0])
        section = gate.latest_changelog_section(CHANGELOG.read_text(encoding="utf-8"))
        self.assertIsNotNone(section, "CHANGELOG has no released section")
        self.assertEqual(
            section[0], version,
            f"manifests declare {version} but CHANGELOG's newest released "
            f"section is [{section[0]}] — a version bump without its "
            f"changelog section is a release violation",
        )


class ReleaseGateUnitTest(unittest.TestCase):
    """Pure-function coverage of the gate's parsing and extraction."""

    def test_read_version_shapes(self):
        # Top-level JSON (plugin.json shape).
        p = gate.REPO_ROOT / ".zcode-plugin" / "plugin.json"
        self.assertEqual(gate.read_version(str(p.relative_to(gate.REPO_ROOT))),
                         json.loads(p.read_text(encoding="utf-8"))["version"])
        # Marketplace shape (plugins[].version).
        self.assertEqual(
            gate.read_version("marketplace.json"),
            json.loads((gate.REPO_ROOT / "marketplace.json").read_text(encoding="utf-8"))
            ["plugins"][0]["version"],
        )
        # YAML shape (version: line).
        yaml_v = gate.read_version("hermes-plugin/plugin.yaml")
        self.assertRegex(yaml_v or "", r"^\d+\.\d+\.\d+$")

    def test_read_version_negatives(self):
        self.assertIsNone(gate.read_version("LICENSE"))          # not a manifest
        self.assertIsNone(gate.read_version("does-not-exist.json"))

    def test_latest_changelog_section_extraction(self):
        text = (
            "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] — 2026-01-02\n\n"
            "Second release body.\n\n- item two\n\n## [0.1.0] — 2026-01-01\n\n"
            "First release body.\n"
        )
        version, body = gate.latest_changelog_section(text)
        self.assertEqual(version, "0.2.0", "newest released section wins")
        self.assertIn("Second release body.", body)
        self.assertIn("- item two", body)
        self.assertNotIn("First release body.", body,
                         "body must stop at the next ## header")
        self.assertIsNone(gate.latest_changelog_section("no sections here"))

    def test_body_terminates_at_any_h2_header_including_plain_ones(self):
        """Reviewer-round pin (deliberate semantics): a section's body stops
        at the NEXT `## ` header of ANY kind — including plain non-bracket
        headers like the file's trailing '## Prior development versions'.
        A bracket-only terminator would bleed that trailing prose into the
        oldest release's notes, so the broad stop is the correct choice."""
        text = (
            "## [0.2.0] — 2026-01-02\n\nbody of newest\n\n"
            "## Prior development versions (0.8.0 – 0.8.3)\n\nold prose\n"
        )
        version, body = gate.latest_changelog_section(text)
        self.assertEqual(version, "0.2.0")
        self.assertIn("body of newest", body)
        self.assertNotIn("old prose", body,
                         "plain ## headers terminate a section body")

    def test_yaml_version_quoting_is_tolerated(self):
        m = gate._VERSION_RE.search('name: x\nversion: "1.2.3"\n')
        self.assertEqual(m.group(1).strip("\"'"), "1.2.3")

    def test_section_regex_ignores_unreleased(self):
        self.assertIsNone(gate.latest_changelog_section(
            "## [Unreleased]\n\nonly unreleased content\n"))


class WorkflowContractTest(unittest.TestCase):
    """Structural pins on the release workflow — the automation itself must
    not silently regress (a deleted workflow is exactly the old failure
    mode, one rename away)."""

    def setUp(self):
        self.assertTrue(WORKFLOW_YML.exists(),
                        f"{WORKFLOW_YML} is missing — releases are manual again")
        self.yml = WORKFLOW_YML.read_text(encoding="utf-8")

    def test_triggers_only_on_main_push(self):
        self.assertIn('branches: ["main"]', self.yml)
        # The TRIGGER must never be pull_request_target (that + a write
        # token is a privilege-escalation surface). Match the YAML key, not
        # any occurrence — comments may legitimately explain the ban.
        import re
        self.assertIsNone(
            re.search(r"^\s*pull_request_target\s*:", self.yml, re.MULTILINE),
            "pull_request_target trigger in a contents:write workflow is a "
            "privilege escalation surface",
        )

    def test_declares_contents_write(self):
        self.assertIn("contents: write", self.yml,
                      "creating the tag + Release needs contents: write")

    def test_actions_are_sha_pinned(self):
        """Unlike ci.yml's documented tag-pinning waiver (read-only token),
        this workflow holds a contents:write token — a repointed major tag
        must not execute with write scope. Both actions are SHA-pinned."""
        import re
        uses = re.findall(r"uses:\s*(\S+)", self.yml)
        self.assertGreaterEqual(len(uses), 2)
        for ref in uses:
            self.assertRegex(
                ref, r"@[0-9a-f]{40}",
                f"action ref {ref!r} must be SHA-pinned (write token)")

    def test_runs_the_gate(self):
        self.assertIn("scripts/release_gate.py", self.yml)

    def test_tags_the_triggering_main_commit_not_a_branch_head(self):
        # The squash-merge lesson: a tag at a PR-branch head is orphaned off
        # first-parent history. The workflow must target github.sha.
        self.assertIn("TARGET_SHA: ${{ github.sha }}", self.yml)
        self.assertIn('--target "${TARGET_SHA}"', self.yml)

    def test_publishes_via_gh_release_create(self):
        self.assertIn("gh release create", self.yml)

    def test_idempotency_is_release_existence_not_tag_existence(self):
        """Final-critic pin: a bare tag pushed without a Release (human
        `git push --tag`) must heal on a later merge, not be skipped
        forever — so the publish step checks `gh release view` first."""
        self.assertIn("gh release view", self.yml)

    def test_publish_step_runs_on_resolved_version(self):
        self.assertIn("steps.gate.outputs.version != ''", self.yml)


class GateIntegrationTest(unittest.TestCase):
    """The gate as the workflow runs it: exit 0 against the live repo state
    (either 'will cut' or 'already tagged' — both are pass; violations are
    the only failures, and parity is separately pinned above)."""

    def test_gate_exits_zero_on_current_repo(self):
        r = subprocess.run(
            [PYTHON, str(GATE_PY)], capture_output=True, text=True,
            timeout=120, cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            r.returncode, 0,
            f"release gate failed on the current repo state:\n{r.stdout}\n{r.stderr}",
        )
        self.assertIn("[release-gate]", r.stdout,
                      "the gate must state its decision human-readably")
        # The default mode's stdout is the Release workflow's input surface
        # (GITHUB_OUTPUT + human lines); drift-mode text must never bleed in.
        self.assertNotIn("unreleased drift", r.stdout)


class UnreleasedDriftUnitTest(unittest.TestCase):
    """Issue #106: pure-function coverage of the drift-mode CHANGELOG
    parsing (unreleased_body / unreleased_has_content)."""

    def _text(self, unreleased_body: str) -> str:
        return (
            "# Changelog\n\n## [Unreleased]\n\n" + unreleased_body +
            "\n\n## [0.2.0] — 2026-01-02\n\nReleased body.\n"
        )

    def test_absent_section_is_empty(self):
        self.assertEqual(gate.unreleased_body("## [0.2.0] — 2026-01-02\n\nbody\n"), "")
        self.assertFalse(gate.unreleased_has_content(
            "## [0.2.0] — 2026-01-02\n\nbody\n"))

    def test_empty_and_whitespace_only_sections_have_no_content(self):
        for body in ("", "   \n\n\t\n"):
            text = self._text(body)
            self.assertTrue(gate.unreleased_body(text) != "" or body == "",
                            "section body extraction ran")
            self.assertFalse(
                gate.unreleased_has_content(text),
                f"blank-only Unreleased body {body!r} must not count as drift")

    def test_content_counts_as_drift(self):
        text = self._text("- a merged change (#86)\n- another (#89)")
        self.assertTrue(gate.unreleased_has_content(text))
        self.assertIn("(#86)", gate.unreleased_body(text))

    def test_body_stops_at_next_h2(self):
        text = self._text("- unreleased item\n\n## Notable other section\nshared prose")
        self.assertNotIn("shared prose", gate.unreleased_body(text))


class UnreleasedDriftSyntheticRepoTest(unittest.TestCase):
    """Issue #106 acceptance: on a synthetic repo reproducing the exact
    history that produced the issue (content added under ## [Unreleased]
    while the manifest version stays static), --check-unreleased-drift
    MUST fail; the bump, the [skip release] marker, and an empty Unreleased
    must each pass. Case names cite the check step they exercise."""

    def _make_repo(self, tmp: Path) -> Path:
        repo = tmp / "synthetic"
        repo.mkdir(parents=True)
        def git(*args: str):
            subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c",
                 "user.name=T", *args],
                cwd=str(repo), capture_output=True, text=True, timeout=60,
            )
        git("init", "-q")
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "zmem", "version": "0.13.1"}), encoding="utf-8")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n## [0.13.1] — 2026-08-28\n\n"
            "Prior release.\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "chore: cut 0.13.1 (#84)")
        return repo

    def _merge(self, repo: Path, *, changelog: str, version: str | None,
               subject: str) -> None:
        (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        if version is not None:
            (repo / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "zmem", "version": version}),
                encoding="utf-8")
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
             "add", "-A"], cwd=str(repo), capture_output=True, timeout=60)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "-qm", subject],
            cwd=str(repo), capture_output=True, timeout=60)

    def _run_gate(self, repo: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(GATE_PY), "--check-unreleased-drift",
             "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        )

    def test_step5_drift_fails_on_the_exact_issue_history(self):
        # The #106 history: 8 merges appended under [Unreleased] while every
        # manifest sat static at 0.13.1 — no bump, no marker, content present.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-") as td:
            repo = self._make_repo(Path(td))
            self._merge(
                repo,
                changelog=(
                    "# Changelog\n\n## [Unreleased]\n\n"
                    "- merged feature (#86)\n- merged fix (#89)\n\n"
                    "## [0.13.1] — 2026-08-28\n\nPrior release.\n"),
                version=None,
                subject="feat(memory): something user-facing (#86)")
            r = self._run_gate(repo)
            self.assertEqual(r.returncode, 1,
                             f"drift must fail the #106 history:\n{r.stdout}")
            self.assertIn("::error::", r.stdout)

    def test_step2_conventional_bump_promotes_and_passes(self):
        # The repo-convention bump: content PROMOTED out of [Unreleased] into
        # the new dated section — Unreleased is empty, so the gate passes at
        # the history-free step 2 (the common clean path).
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-") as td:
            repo = self._make_repo(Path(td))
            self._merge(
                repo,
                changelog=(
                    "# Changelog\n\n## [Unreleased]\n\n"
                    "## [0.14.0] — 2026-09-03\n\n- merged feature (#86)\n\n"
                    "## [0.13.1] — 2026-08-28\n\nPrior release.\n"),
                version="0.14.0",
                subject="feat(memory): something user-facing; release 0.14.0 (#87)")
            r = self._run_gate(repo)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("no unreleased drift", r.stdout)

    def test_step4_bump_with_leftover_unreleased_passes(self):
        # HEAD carries a version bump even though [Unreleased] still holds
        # content (leftover, per the issue wording "HEAD does not carry a
        # version bump" — a bump makes it pass regardless).
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-") as td:
            repo = self._make_repo(Path(td))
            self._merge(
                repo,
                changelog=(
                    "# Changelog\n\n## [Unreleased]\n\n"
                    "- leftover follow-up work (#88)\n\n"
                    "## [0.14.0] — 2026-09-03\n\n- merged feature (#86)\n\n"
                    "## [0.13.1] — 2026-08-28\n\nPrior release.\n"),
                version="0.14.0",
                subject="feat(memory): something user-facing; release 0.14.0 (#87)")
            r = self._run_gate(repo)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("version bump", r.stdout)

    def test_step3_skip_release_marker_passes_without_bump(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-") as td:
            repo = self._make_repo(Path(td))
            self._merge(
                repo,
                changelog=(
                    "# Changelog\n\n## [Unreleased]\n\n"
                    "- docs correction (#103)\n\n"
                    "## [0.13.1] — 2026-08-28\n\nPrior release.\n"),
                version=None,
                subject="docs(memory): a docs-only change [skip release] (#103)")
            r = self._run_gate(repo)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("[skip release]", r.stdout)

    def test_step2_empty_unreleased_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-") as td:
            repo = self._make_repo(Path(td))
            # No second merge at all — [Unreleased] still empty at HEAD.
            r = self._run_gate(repo)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("no unreleased drift", r.stdout)

    def test_live_repo_passes_drift_mode(self):
        r = subprocess.run(
            [PYTHON, str(GATE_PY), "--check-unreleased-drift"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        )
        self.assertEqual(r.returncode, 0,
                         f"live repo has unreleased drift:\n{r.stdout}")
        # The default mode never gained drift output (release.yml contract).
        self.assertIn("no unreleased drift", r.stdout)


class CiWorkflowContractTest(unittest.TestCase):
    """Issue #106: the drift gate must stay wired into ci.yml for main
    pushes only — deleting the step (or letting it run on PRs, where
    HEAD~1 is branch history) is exactly the regression class this pins."""

    def setUp(self):
        self.yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")

    def test_checkout_depth_covers_head_parent(self):
        self.assertIn("fetch-depth: 2", self.yml,
                      "the bump comparison needs HEAD~1 on push events")

    def test_drift_step_runs_the_gate_on_main_pushes_only(self):
        import re
        step = re.search(
            r"- name: Release drift gate.*?run: python scripts/"
            r"release_gate\.py --check-unreleased-drift",
            self.yml, re.DOTALL)
        self.assertIsNotNone(step, "ci.yml lost the drift-gate step")
        self.assertIn(
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
            step.group(0),
            "the drift gate must run only on pushes to main")


if __name__ == "__main__":
    unittest.main(verbosity=2)
