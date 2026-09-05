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
            r = subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c",
                 "user.name=T", *args],
                cwd=str(repo), capture_output=True, text=True, timeout=60,
            )
            # PRR-010: a silently-failed fixture git call must surface as a
            # fixture error, not as a confusing gate assertion downstream.
            self.assertEqual(
                r.returncode, 0,
                f"fixture git {' '.join(args)} failed:\n{r.stderr}")
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
        for args in (["add", "-A"], ["commit", "-qm", subject]):
            r = subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
                 *args],
                cwd=str(repo), capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                r.returncode, 0,
                f"fixture git {' '.join(args)} failed:\n{r.stderr}")

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

    def test_drift_negative_paths(self):
        # PRR-006: the two failure paths the original sweep never exercised —
        # (a) CHANGELOG.md unreadable/absent -> error exit 1;
        # (b) --repo-root on a non-git directory carrying drift content ->
        # head_bumps_version finds no git history, treats it as NO bump
        # (strict), no marker -> exit 1. An empty-Unreleased non-git dir
        # passes at the history-free step (rc 0, no git needed).
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-drift-neg-") as td:
            root = Path(td)
            r = self._run_gate(root)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("::error::", r.stdout)
            self.assertIn("unreadable", r.stdout)

            nogit = root / "nogit"
            nogit.mkdir()
            (nogit / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [Unreleased]\n\n- drift content (#1)\n",
                encoding="utf-8")
            r = self._run_gate(nogit)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("::error::unreleased drift", r.stdout)

            (nogit / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] — x\n\nb\n",
                encoding="utf-8")
            r = self._run_gate(nogit)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("no unreleased drift", r.stdout)

    def test_live_repo_drift_mode_reports_a_sane_verdict(self):
        # X10: this runs in the every-PR CI loop, where the live repo MAY
        # legitimately carry unreleased content (any feature PR mid-flight).
        # The smoke value is "the mode runs and reports one of its two
        # documented verdicts" — NOT a pinned rc (that would break every
        # PR that accumulates [Unreleased] entries before its bump PR).
        # The default mode's no-drift-text contract is still pinned.
        r = subprocess.run(
            [PYTHON, str(GATE_PY), "--check-unreleased-drift"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        )
        self.assertIn(r.returncode, (0, 1),
                      f"drift mode crashed on the live repo:\n{r.stdout}")
        self.assertTrue(
            ("no unreleased drift" in r.stdout)
            or ("::error::unreleased drift" in r.stdout),
            f"neither documented verdict emitted:\n{r.stdout}")
        # The default mode never gained drift output (release.yml contract).
        d = subprocess.run(
            [PYTHON, str(GATE_PY)], capture_output=True, text=True,
            timeout=120, cwd=str(REPO_ROOT),
        )
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        self.assertNotIn("unreleased drift", d.stdout)


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
        # PRR-003: a red earlier step must not mute the drift check on a
        # main push — the condition must include !cancelled() (or always()).
        self.assertRegex(
            step.group(0), r"if:.*(!cancelled\(\)|always\(\))",
            "the drift gate is skipped whenever any earlier step fails — "
            "add !cancelled() to the condition")

    def test_freshness_step_fetches_base_ref_before_the_gate(self):
        # The PR checkout is shallow and carries no origin/main ref; the
        # freshness gate fails closed on an unreadable base. Pin that the
        # step fetches the base tip BEFORE invoking the gate, or every
        # release-prep PR goes red on an infra-shaped error.
        import re
        step = re.search(
            r"- name: Manifest freshness on release-prep changes"
            r".*?run: \|(.*?)\n      - ", self.yml, re.DOTALL)
        self.assertIsNotNone(
            step, "ci.yml lost the manifest-freshness step")
        body = step.group(1)
        fetch = body.find("git fetch")
        gate = body.find("--check-manifest-freshness --base origin/main")
        self.assertGreater(fetch, -1,
                           "the freshness step must fetch origin/main "
                           "(shallow PR checkouts have no base ref)")
        self.assertGreater(gate, -1,
                           "the freshness step must run the gate")
        self.assertLess(fetch, gate,
                        "the fetch must run BEFORE the gate")


class ReleaseManifestTest(unittest.TestCase):
    """Issue #107: --emit-manifest writes the content-hash manifest over the
    working tree, --verify-manifest passes fresh and fails stale/missing, and
    the manifest never joins the version-parity set."""

    def _make_repo(self, tmp: Path) -> Path:
        repo = tmp / "synthetic"
        repo.mkdir(parents=True)
        (repo / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (repo / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "zmem", "version": "0.17.0"}),
            encoding="utf-8")
        # Runtime-surface files across all four prefixes (the hermes manifest
        # carries a version: line — the emit parity check reads it).
        for rel in ("hooks/zmem-recall.sh", "hooks/lib/body.py",
                    "skills/memory/scripts/store.py",
                    "skills/memory/SKILL.md"):
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {rel}\n", encoding="utf-8")
        (repo / "hermes-plugin").mkdir(parents=True, exist_ok=True)
        (repo / "hermes-plugin" / "plugin.yaml").write_text(
            "name: zmem\nversion: 0.17.0\n", encoding="utf-8")
        # Off-surface file: must never be hashed.
        (repo / "README.md").write_text("# readme\n", encoding="utf-8")
        # Emit enumerates TRACKED files (git ls-files) — the fixture must be
        # a git repo with its surface files committed. The untracked surface
        # file below proves the tracked-only exclusion.
        def git(*args: str):
            r = subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c",
                 "user.name=T", *args],
                cwd=str(repo), capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"fixture git failed: {r.stderr}"
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "fixture")
        (repo / "hooks" / "untracked-junk.sh").write_text(
            "# untracked" + chr(10), encoding="utf-8")
        return repo

    def _run_gate(self, repo: Path, *args: str):
        return subprocess.run(
            [PYTHON, str(GATE_PY), *args, "--repo-root", str(repo)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        )

    def test_emit_writes_deterministic_manifest(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-rm107-") as td:
            repo = self._make_repo(Path(td))
            r1 = self._run_gate(repo, "--emit-manifest")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            self.assertIn("[release-gate] wrote release-manifest.json", r1.stdout)
            first = (repo / "release-manifest.json").read_text(encoding="utf-8")
            manifest = json.loads(first)
            self.assertEqual(manifest["version"], "0.17.0")
            self.assertEqual(manifest["algorithm"], "sha256-crlf-norm")
            self.assertIn("hooks/zmem-recall.sh", manifest["files"])
            self.assertIn("skills/memory/scripts/store.py", manifest["files"])
            self.assertIn("skills/memory/SKILL.md", manifest["files"])
            self.assertIn("hermes-plugin/plugin.yaml", manifest["files"])
            self.assertNotIn("README.md", manifest["files"])
            self.assertNotIn("release-manifest.json", manifest["files"])
            # Untracked surface file: emit describes the TRACKED surface —
            # unshippable scratch must never enter the release manifest.
            self.assertNotIn("hooks/untracked-junk.sh", manifest["files"])
            # Deterministic: a re-emit over an unchanged tree is byte-identical.
            r2 = self._run_gate(repo, "--emit-manifest")
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertEqual(
                first, (repo / "release-manifest.json").read_text(
                    encoding="utf-8"),
                "emit must be deterministic (no timestamp, sorted keys)")

    def test_verify_passes_fresh_fails_stale_and_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem-rm107-") as td:
            repo = self._make_repo(Path(td))
            # Missing manifest: rc 1 with a clear remediation.
            r = self._run_gate(repo, "--verify-manifest")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("::error::", r.stdout)
            self.assertIn("--emit-manifest", r.stdout)
            # Fresh manifest: rc 0.
            self._run_gate(repo, "--emit-manifest")
            r = self._run_gate(repo, "--verify-manifest")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("release manifest fresh", r.stdout)
            # Stale (one surface file edited after emit): rc 1 naming the file.
            (repo / "hooks" / "zmem-recall.sh").write_text(
                "# drifted\n", encoding="utf-8")
            r = self._run_gate(repo, "--verify-manifest")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("STALE", r.stdout)
            self.assertIn("hooks/zmem-recall.sh", r.stdout)

    def test_manifest_never_joins_the_version_parity_set(self):
        """release-manifest.json carries a digest, not a release version —
        the default gate mode (and its byte-identical stdout contract) must
        never see it as a host-facing manifest."""
        manifests = gate.discover_manifests()
        self.assertNotIn("release-manifest.json", manifests)


class ReleaseWorkflowManifestPinsTest(unittest.TestCase):
    """Issue #107 workflow pins: the publish step verifies the manifest
    before creating the release and attaches it as a release asset."""

    def setUp(self):
        self.yml = (REPO_ROOT / ".github" / "workflows" / "release.yml"
                    ).read_text(encoding="utf-8")

    def test_publish_step_verifies_manifest_before_create(self):
        import re
        verify = self.yml.find("release_gate.py --verify-manifest")
        create = self.yml.find("gh release create")
        self.assertGreater(
            verify, -1, "release.yml lost the --verify-manifest step")
        self.assertGreater(
            create, -1, "release.yml lost gh release create")
        self.assertLess(
            verify, create,
            "--verify-manifest must run BEFORE gh release create so a stale "
            "manifest aborts the release under set -euo pipefail")

    def test_manifest_attached_as_release_asset(self):
        import re
        create = re.search(
            r"gh release create \"v\$\{VERSION\}\".*?release-manifest\.json",
            self.yml, re.DOTALL)
        self.assertIsNotNone(
            create,
            "release-manifest.json must be attached to the GitHub Release "
            "(issue #107 scope item 1)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
