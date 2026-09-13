"""Deterministic committed fixtures for issue #184 host refresh."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
DRIFT_SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "hosts"
GENERATOR = FIXTURE_ROOT / "generate.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(DRIFT_SCRIPTS))

import drift  # noqa: E402
import host_registry  # noqa: E402
import release_gate  # noqa: E402
import refresh_hosts  # noqa: E402


HOSTS = ("codex", "claude", "zcode")
VERSION = "0.34.0"
MARKETPLACE_PREIMAGE_VERSION = "0.14.0"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
CANONICAL_MANIFESTS = {
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".zcode-plugin/plugin.json",
    "hermes-plugin/plugin.yaml",
    "marketplace.json",
}
CODEX_CACHE_REL = Path(".codex/plugins/cache/personal/zmem") / VERSION
CLAUDE_CACHE_REL = Path(".claude/plugins/cache/zmem/zmem") / VERSION
ZCODE_CACHE_REL = Path(".zcode/cli/plugins/cache/zmem/zmem") / VERSION
ZCODE_MARKETPLACE_REL = Path(".zcode/cli/plugins/marketplaces/zmem")


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree(root: Path) -> dict[str, tuple[str, bytes | None]]:
    """Snapshot files and directories so a dry-run cannot hide mutations."""
    result: dict[str, tuple[str, bytes | None]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("dir", None)
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
    return result


def _materialize(template: Path, values: dict[str, str]) -> bytes:
    """Replace tokens inside JSON strings with JSON-escaped path content."""
    payload = template.read_bytes()
    for token, value in values.items():
        escaped = json.dumps(value, ensure_ascii=False)[1:-1].encode("utf-8")
        payload = payload.replace(token.encode("utf-8"), escaped)
    return payload


def _git(checkout: Path, *args: str) -> None:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=issue184@example.invalid",
            "-c",
            "user.name=issue184",
            *args,
        ],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr.strip() or f"git exited {result.returncode}")


class _FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-host-fixtures-"))
        self.checkout = self.tmp / "checkout"
        self.home = self.tmp / "home"
        shutil.copytree(FIXTURE_ROOT / "checkout", self.checkout)
        shutil.copytree(FIXTURE_ROOT / "home", self.home)
        self.report = self.tmp / "refresh-report.json"

        # A root-level .git sentinel is created only by the dedicated mirror
        # regression below.  Keep this real temporary checkout free of it so
        # Git can initialize the index used by release_gate discovery.
        git_sentinel = self.checkout / ".git"
        if git_sentinel.is_file():
            git_sentinel.unlink()

        # release_gate.discover_manifests intentionally uses tracked files;
        # this temporary index makes the committed fixture a real checkout.
        # The refresh implementation's only patched production boundary is
        # _git_head below, which supplies the approved deterministic SHA.
        _git(self.checkout, "init", "--quiet")
        _git(self.checkout, "add", ".")
        _git(self.checkout, "commit", "--quiet", "-m", "fixture")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_refresh(
        self, *, dry_run: bool = False, report_path: Path | None = None
    ) -> int:
        argv = [
            "--checkout",
            str(self.checkout),
            "--report",
            str(report_path or self.report),
        ]
        if dry_run:
            argv.append("--dry-run")
        with mock.patch.object(refresh_hosts.Path, "home", return_value=self.home):
            with mock.patch.object(refresh_hosts, "_git_head", return_value=COMMIT_SHA):
                return int(refresh_hosts.main(argv) or 0)

    def _report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def _materialized_values(self) -> dict[str, str]:
        home = self.home.resolve()
        checkout = self.checkout.resolve()
        return {
            "__CHECKOUT__": str(checkout),
            "__CODEX_CACHE__": str(home / CODEX_CACHE_REL),
            "__CLAUDE_CACHE__": str(home / CLAUDE_CACHE_REL),
            "__ZCODE_CACHE__": str(home / ZCODE_CACHE_REL),
            "__CLAUDE_REGISTRY__": str(
                home / ".claude/plugins/installed_plugins.json"
            ),
            "__ZCODE_REGISTRY__": str(
                home / ".zcode/cli/plugins/installed_plugins.json"
            ),
            "__ZCODE_MARKETPLACE__": str(home / ZCODE_MARKETPLACE_REL),
            "__ZCODE_MARKETPLACE_FILE__": str(
                home / ZCODE_MARKETPLACE_REL / "marketplace.json"
            ),
            "__ZCODE_CLAUDE_MARKETPLACE_FILE__": str(
                home / ZCODE_MARKETPLACE_REL / ".claude-plugin/marketplace.json"
            ),
        }

    def _expected_report(self) -> dict:
        payload = _materialize(
            FIXTURE_ROOT / "expected/report.json", self._materialized_values()
        )
        return json.loads(payload.decode("utf-8"))


class GeneratorReproducibilityTest(unittest.TestCase):
    def test_generator_reproduces_every_committed_byte_and_sorted_hashes(self):
        with tempfile.TemporaryDirectory(prefix="zmem-generator-a-") as first:
            with tempfile.TemporaryDirectory(prefix="zmem-generator-b-") as second:
                outputs = []
                for root in (Path(first), Path(second)):
                    result = subprocess.run(
                        [sys.executable, str(GENERATOR), "--output-root", str(root)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    outputs.append(result.stdout)
                    self.assertEqual(result.stderr, "")

                self.assertEqual(outputs[0], outputs[1])
                lines = outputs[0].splitlines()
                paths = [line.split(" ", 1)[0] for line in lines]
                self.assertEqual(paths, sorted(paths))
                self.assertEqual(len(paths), len(set(paths)))
                for line in lines:
                    relative, digest = line.rsplit(" ", 1)
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")
                    self.assertEqual(
                        digest,
                        hashlib.sha256(
                            (Path(first) / Path(relative)).read_bytes()
                        ).hexdigest(),
                    )

                for root in (Path(first), Path(second)):
                    for name in ("checkout", "home", "expected"):
                        self.assertEqual(
                            _files(root / name), _files(FIXTURE_ROOT / name)
                        )


class HostRefreshFixtureIntegrationTest(_FixtureCase):
    def test_release_gate_discovery_excludes_committed_fixture_manifests(self):
        self.assertEqual(
            set(release_gate.discover_manifests()), CANONICAL_MANIFESTS
        )

    def test_release_gate_disk_discovery_excludes_committed_fixture_manifests(self):
        with tempfile.TemporaryDirectory(prefix="zmem-disk-manifests-") as root:
            synthetic = Path(root)
            shutil.copytree(FIXTURE_ROOT / "checkout", synthetic, dirs_exist_ok=True)
            shutil.copytree(
                FIXTURE_ROOT, synthetic / "tests/fixtures/hosts", dirs_exist_ok=True
            )
            self.assertEqual(
                set(release_gate._discover_manifests_disk(synthetic)),
                CANONICAL_MANIFESTS,
            )

    def test_checkout_has_exact_release_manifests_runtime_and_excluded_sentinels(self):
        manifests = {
            path.relative_to(self.checkout).as_posix()
            for path in self.checkout.rglob("*")
            if path.is_file()
            and (
                path.name in {"plugin.json", "marketplace.json"}
                or path.name == "plugin.yaml"
            )
        }
        self.assertEqual(manifests, CANONICAL_MANIFESTS)
        for relative in CANONICAL_MANIFESTS:
            self.assertIn(VERSION, (self.checkout / relative).read_text(encoding="utf-8"))
        release_manifest = json.loads(
            (self.checkout / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(release_manifest["version"], VERSION)
        self.assertEqual(release_manifest["algorithm"], drift.ALGORITHM)
        self.assertEqual(
            release_manifest["files"], drift.tree_hashes(self.checkout)
        )
        self.assertEqual(
            release_manifest["digest"], drift.aggregate(release_manifest["files"])
        )
        for relative in (
            "graphify-out/should-not-copy.txt",
            "scripts/__pycache__/sentinel.pyc",
            "scripts/ignored.pyc",
            "scripts/ignored.pyo",
        ):
            self.assertTrue((self.checkout / relative).is_file())

    def test_root_git_regular_file_is_excluded_by_mirror_enumeration(self):
        with tempfile.TemporaryDirectory(prefix="zmem-git-sentinel-") as root:
            checkout = Path(root) / "checkout"
            shutil.copytree(FIXTURE_ROOT / "checkout", checkout)
            sentinel = checkout / ".git"
            sentinel.write_bytes(b"git metadata sentinel; never mirror this file\n")
            mirror = refresh_hosts._iter_mirror_files(checkout)
            self.assertTrue(sentinel.is_file())
            self.assertNotIn(
                ".git",
                {relative.as_posix() for _, relative in mirror},
            )

    def test_checkout_symlink_entries_are_skipped_by_mirror_enumeration(self):
        ordinary = self.checkout / "ordinary-source.txt"
        ordinary.write_bytes(b"ordinary source\n")
        linked_file_target = self.tmp / "linked-file-target.txt"
        linked_file_target.write_bytes(b"must not be mirrored\n")
        linked_file = self.checkout / "linked-file.txt"
        linked_dir_target = self.tmp / "linked-dir-target"
        linked_dir_target.mkdir()
        (linked_dir_target / "secret.txt").write_bytes(b"must not be mirrored\n")
        linked_dir = self.checkout / "linked-dir"
        try:
            linked_file.symlink_to(linked_file_target)
            linked_dir.symlink_to(linked_dir_target, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        mirrored = {
            relative.as_posix()
            for _, relative in refresh_hosts._iter_mirror_files(self.checkout)
        }
        self.assertIn("ordinary-source.txt", mirrored)
        self.assertNotIn("linked-file.txt", mirrored)
        self.assertNotIn("linked-dir/secret.txt", mirrored)

    def test_report_path_collision_with_registry_fails_closed(self):
        registry = self.home / ".claude/plugins/installed_plugins.json"
        before = _tree(self.home)
        status = self._run_refresh(report_path=registry)
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before)

    def test_all_destinations_validate_before_existing_cache_or_registry_reads(self):
        registry = self.home / ".claude/plugins/installed_plugins.json"
        registry_bytes = registry.read_bytes()
        registry.unlink()
        try:
            with mock.patch.object(
                refresh_hosts,
                "_runtime_digest",
                wraps=refresh_hosts._runtime_digest,
            ) as runtime_digest:
                status = self._run_refresh()
            self.assertNotEqual(status, 0)
            runtime_digest.assert_not_called()
        finally:
            registry.write_bytes(registry_bytes)

    def test_main_reports_refresh_failures_to_stderr(self):
        registry = self.home / ".claude/plugins/installed_plugins.json"
        before = registry.read_bytes()
        registry.unlink()
        stderr = io.StringIO()
        try:
            argv = [
                "--checkout",
                str(self.checkout),
                "--report",
                str(self.report),
            ]
            with contextlib.redirect_stderr(stderr):
                status = self._run_refresh()
            self.assertNotEqual(status, 0)
            self.assertEqual(stderr.getvalue().count("\n"), 1)
            self.assertIn("[refresh-hosts] refresh failed:", stderr.getvalue())
        finally:
            registry.write_bytes(before)

    def test_report_symlink_to_innocent_file_fails_closed(self):
        innocent = self.tmp / "innocent.txt"
        innocent.write_bytes(b"do not overwrite this target\n")
        report = self.tmp / "report-link.json"
        try:
            report.symlink_to(innocent)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        before_home = _tree(self.home)
        before_target = innocent.read_bytes()
        status = self._run_refresh(report_path=report)
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before_home)
        self.assertEqual(innocent.read_bytes(), before_target)
        self.assertTrue(report.is_symlink())

    def test_cache_destination_symlink_fails_closed(self):
        target = self.tmp / "cache-target"
        target.mkdir()
        destination = self.home / CODEX_CACHE_REL
        try:
            destination.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        before_home = _tree(self.home)
        before_target = _tree(target)
        status = self._run_refresh()
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before_home)
        self.assertEqual(_tree(target), before_target)
        self.assertTrue(destination.is_symlink())

    def test_cache_ancestor_symlink_fails_closed(self):
        target = self.tmp / "cache-ancestor-target"
        versioned = target / "personal" / "zmem" / VERSION
        versioned.mkdir(parents=True)
        (versioned / "outside.txt").write_bytes(b"must remain outside the home\n")
        cache_parent = self.home / ".codex" / "plugins" / "cache"
        shutil.rmtree(cache_parent)
        try:
            cache_parent.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        before_home = _tree(self.home)
        before_target = _tree(target)
        status = self._run_refresh()
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before_home)
        self.assertEqual(_tree(target), before_target)
        self.assertTrue(cache_parent.is_symlink())

    def test_symlinked_home_is_rejected_before_resolution(self):
        linked_home = self.tmp / "home-link"
        try:
            linked_home.symlink_to(self.home, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        with mock.patch.object(refresh_hosts, "_git_head", return_value=COMMIT_SHA):
            with self.assertRaisesRegex(refresh_hosts.RefreshError, r"home is a symlink"):
                refresh_hosts.refresh_host(
                    self.checkout,
                    "codex",
                    linked_home,
                    dry_run=True,
                )

    def test_cli_rejects_symlinked_home_before_resolution(self):
        linked_home = self.tmp / "home-link"
        try:
            linked_home.symlink_to(self.home, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        before_home = _tree(self.home)
        report = self.tmp / "cli-report.json"
        argv = [
            "--checkout",
            str(self.checkout),
            "--report",
            str(report),
            "--hosts",
            "codex",
            "--dry-run",
        ]
        with mock.patch.object(refresh_hosts.Path, "home", return_value=linked_home):
            with mock.patch.object(refresh_hosts, "_git_head", return_value=COMMIT_SHA):
                status = refresh_hosts.main(argv)
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before_home)
        self.assertFalse(report.exists())

    def test_registry_writer_rejects_symlinked_ancestor(self):
        target = self.tmp / "registry-ancestor-target"
        target.mkdir()
        link = self.tmp / "registry-parent-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            raise

        source = self.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        destination = link / "installed_plugins.json"
        before_target = _tree(target)
        with self.assertRaises(OSError):
            host_registry.write_host_registry(destination, data)
        self.assertEqual(_tree(target), before_target)
        self.assertFalse((target / "installed_plugins.json").exists())

    def test_public_refresh_host_dry_run_returns_row_without_report_with_fixture_home(self):
        before_home = _tree(self.home)
        with mock.patch.object(refresh_hosts, "_git_head", return_value=COMMIT_SHA):
            row = refresh_hosts.refresh_host(
                self.checkout,
                "codex",
                self.home,
                dry_run=True,
            )
        self.assertEqual(row["host"], "codex")
        self.assertEqual(row["status"], "dry-run")
        self.assertEqual(row["mismatches"], [])
        self.assertIsNone(row["registryPath"])
        self.assertEqual(row["marketplacePaths"], [])
        self.assertFalse(self.report.exists())
        self.assertFalse((self.home / CODEX_CACHE_REL).exists())
        self.assertEqual(_tree(self.home), before_home)

    def test_dry_run_from_drifted_home_changes_only_report_then_real_refresh_matches_expected(self):
        for relative in (
            ZCODE_MARKETPLACE_REL / "marketplace.json",
            ZCODE_MARKETPLACE_REL / ".claude-plugin/marketplace.json",
        ):
            preimage = json.loads((self.home / relative).read_text(encoding="utf-8"))
            self.assertEqual(
                preimage["plugins"][0]["version"], MARKETPLACE_PREIMAGE_VERSION
            )
            self.assertNotEqual(
                (self.home / relative).read_bytes(),
                (FIXTURE_ROOT / "expected/marketplace" / relative.relative_to(
                    ZCODE_MARKETPLACE_REL
                )).read_bytes(),
            )
        for relative in (
            Path(".claude/plugins/cache/zmem/zmem/0.33.0/old.txt"),
            Path(".codex/plugins/cache/personal/zmem/0.32.0/old.txt"),
            Path(".zcode/cli/plugins/cache/zmem/zmem/0.33.0/old.txt"),
        ):
            self.assertTrue((self.home / relative).is_file(), relative)
        workspace_before = _tree(self.tmp)
        self.assertEqual(self._run_refresh(dry_run=True), 0)
        self.assertTrue(self.report.is_file())
        workspace_after = _tree(self.tmp)
        report_relative = self.report.relative_to(self.tmp).as_posix()
        self.assertEqual(
            {
                key: value
                for key, value in workspace_after.items()
                if key != report_relative
            },
            workspace_before,
        )
        self.assertEqual(
            workspace_after[report_relative][0], "file"
        )
        dry_report = self._report()
        self.assertEqual([row["host"] for row in dry_report["hosts"]], list(HOSTS))
        dry_after_digests = [row["afterDigest"] for row in dry_report["hosts"]]
        self.assertEqual(
            [row["status"] for row in dry_report["hosts"]],
            ["dry-run", "dry-run", "dry-run"],
        )
        self.assertEqual(dry_report["gitCommitSha"], COMMIT_SHA)
        self.assertEqual(dry_report["mismatchCount"], 0)
        self.assertTrue(dry_report["ok"])

        # The dry-run's planned destinations remain absent or unchanged.
        self.assertFalse((self.home / CODEX_CACHE_REL).exists())
        self.assertFalse((self.home / ZCODE_CACHE_REL).exists())
        self.assertEqual(
            _tree(self.home / CLAUDE_CACHE_REL),
            _tree(FIXTURE_ROOT / "home" / CLAUDE_CACHE_REL),
        )
        self.assertFalse(
            any(path.name.startswith(".zmem-refresh-") for path in self.home.rglob("*"))
        )

        self.assertEqual(self._run_refresh(), 0)
        expected_report_bytes = (FIXTURE_ROOT / "expected/report.json").read_bytes()
        self.assertTrue(expected_report_bytes.endswith(b"\n"))
        self.assertEqual(expected_report_bytes.count(b"\n"), 1)
        self.assertNotIn(b"\n", expected_report_bytes[:-1])
        self.assertEqual(
            self.report.read_bytes(),
            _materialize(
                FIXTURE_ROOT / "expected/report.json", self._materialized_values()
            ),
        )
        report = self._report()
        self.assertEqual(report, self._expected_report())
        self.assertEqual(
            dry_after_digests,
            [row["afterDigest"] for row in report["hosts"]],
        )
        with self.subTest(report_schema=True):
            import jsonschema

            schema = json.loads(
                (SCRIPTS / "refresh-report-schema.json").read_text(encoding="utf-8")
            )
            jsonschema.validate(report, schema)

        expected_cache_root = FIXTURE_ROOT / "expected/cache"
        for host in HOSTS:
            actual = self.home / {
                "codex": CODEX_CACHE_REL,
                "claude": CLAUDE_CACHE_REL,
                "zcode": ZCODE_CACHE_REL,
            }[host]
            self.assertEqual(_files(actual), _files(expected_cache_root / host))

        self.assertEqual(
            (self.home / ZCODE_MARKETPLACE_REL / "marketplace.json").read_bytes(),
            (FIXTURE_ROOT / "expected/marketplace/marketplace.json").read_bytes(),
        )
        self.assertEqual(
            (
                self.home
                / ZCODE_MARKETPLACE_REL
                / ".claude-plugin/marketplace.json"
            ).read_bytes(),
            (
                FIXTURE_ROOT / "expected/marketplace/.claude-plugin/marketplace.json"
            ).read_bytes(),
        )
        self.assertEqual(
            (self.home / ZCODE_MARKETPLACE_REL / "unrelated.txt").read_bytes(),
            (FIXTURE_ROOT / "home" / ZCODE_MARKETPLACE_REL / "unrelated.txt").read_bytes(),
        )

        self._assert_claude_registry_preservation()
        self._assert_zcode_registry_preservation()
        self.assertFalse(
            any(path.name.startswith(".zmem-refresh-") for path in self.home.rglob("*"))
        )

    def test_staged_sources_and_backups_are_destination_siblings(self):
        real_backup = refresh_hosts._backup_operations
        real_replace = refresh_hosts.os.replace
        observed: list[tuple[Path, Path, Path | None]] = []
        replacements: list[tuple[Path, Path]] = []

        def inspect_operations(operations):
            real_backup(operations)
            observed.extend(
                (operation.source, operation.destination, operation.backup)
                for operation in operations
            )

        def inspect_replace(source, destination):
            replacements.append((Path(source), Path(destination)))
            return real_replace(source, destination)

        with mock.patch.object(refresh_hosts, "_backup_operations", side_effect=inspect_operations):
            with mock.patch.object(refresh_hosts.os, "replace", side_effect=inspect_replace):
                self.assertEqual(self._run_refresh(), 0)

        self.assertTrue(observed)
        for source, destination, backup in observed:
            self.assertEqual(source.parent, destination.parent)
            if backup is not None:
                self.assertEqual(backup.parent, destination.parent)
        self.assertTrue(replacements)
        for source, destination in replacements:
            self.assertEqual(source.parent, destination.parent)
        self.assertFalse(
            any(path.name.startswith(".zmem-refresh-") for path in self.home.rglob("*"))
        )

    def test_staging_failure_cleans_siblings_and_created_destination_parents(self):
        cache_parent = self.home / ".codex" / "plugins" / "cache" / "personal" / "zmem"
        shutil.rmtree(cache_parent)
        before_home = _tree(self.home)
        with mock.patch.object(
            refresh_hosts, "_copy_file", side_effect=OSError("injected staging failure")
        ):
            status = self._run_refresh()
        self.assertNotEqual(status, 0)
        self.assertEqual(_tree(self.home), before_home)
        self.assertFalse(cache_parent.exists())
        self.assertFalse(
            any(path.name.startswith(".zmem-refresh-") for path in self.home.rglob("*"))
        )

    def test_cold_home_dry_run_cleans_staging_before_created_parents(self):
        shutil.rmtree(self.home)
        self.home.mkdir()
        for relative in (
            Path(".claude/plugins/installed_plugins.json"),
            Path(".zcode/cli/plugins/installed_plugins.json"),
        ):
            destination = self.home / relative
            destination.parent.mkdir(parents=True)
            shutil.copy2(FIXTURE_ROOT / "home" / relative, destination)
        before_home = _tree(self.home)

        self.assertEqual(self._run_refresh(dry_run=True), 0)
        self.assertTrue(self.report.is_file())
        self.assertEqual(_tree(self.home), before_home)
        self.assertFalse(
            any(path.name.startswith(".zmem-refresh-") for path in self.home.rglob("*"))
        )

    def test_restore_failure_retains_recoverable_preimage(self):
        destination = self.home / CLAUDE_CACHE_REL
        before = _tree(destination)
        real_replace = refresh_hosts.os.replace
        real_copytree = refresh_hosts.shutil.copytree
        restore_started = False

        def fail_at_claude_cache(source, target):
            nonlocal restore_started
            if Path(target).resolve() == destination.resolve():
                restore_started = True
                raise OSError("injected replacement failure")
            return real_replace(source, target)

        def fail_restore(source, target, *args, **kwargs):
            if restore_started:
                raise OSError("injected restore failure")
            return real_copytree(source, target, *args, **kwargs)

        with mock.patch.object(refresh_hosts.shutil, "copytree", side_effect=fail_restore):
            with mock.patch.object(refresh_hosts.os, "replace", side_effect=fail_at_claude_cache):
                status = self._run_refresh()

        self.assertNotEqual(status, 0)
        self.assertFalse(destination.exists())
        marker = "preimage retained at "
        mismatches = [
            mismatch
            for row in self._report()["hosts"]
            for mismatch in row["mismatches"]
        ]
        retained = {
            Path(mismatch.split(marker, 1)[1])
            for mismatch in mismatches
            if marker in mismatch
        }
        self.assertEqual(len(retained), 1)
        backup = retained.pop()
        self.assertTrue(backup.is_dir())
        self.assertEqual(_tree(backup), before)
        self.assertEqual(backup.parent, destination.parent)
        residue = {
            path
            for path in self.home.rglob("*")
            if path.name.startswith(".zmem-refresh-")
        }
        self.assertEqual(residue, {backup})

    def _assert_claude_registry_preservation(self) -> None:
        before = json.loads(
            (
                FIXTURE_ROOT / "home/.claude/plugins/installed_plugins.json"
            ).read_text(encoding="utf-8")
        )
        actual_path = self.home / ".claude/plugins/installed_plugins.json"
        after = json.loads(actual_path.read_text(encoding="utf-8"))
        expected = json.loads(
            _materialize(
                FIXTURE_ROOT / "expected/registry/claude-installed.json",
                self._materialized_values(),
            ).decode("utf-8")
        )
        self.assertEqual(actual_path.read_bytes(), _materialize(
            FIXTURE_ROOT / "expected/registry/claude-installed.json",
            self._materialized_values(),
        ))
        self.assertEqual(list(before["plugins"]), list(after["plugins"]))
        self.assertEqual(set(before), set(after))
        for key in before["plugins"]:
            if key != "zmem@zmem":
                self.assertEqual(after["plugins"][key], before["plugins"][key])
                continue
            for previous, current in zip(before["plugins"][key], after["plugins"][key]):
                self.assertEqual(
                    set(previous) | set(current),
                    set(current),
                )
                changed = {
                    field
                    for field in current
                    if previous.get(field) != current.get(field)
                }
                self.assertEqual(changed, {"installPath", "version", "gitCommitSha"})
                self.assertEqual(current["version"], VERSION)
                self.assertEqual(current["gitCommitSha"], COMMIT_SHA)
        self.assertEqual(after, expected)

    def _assert_zcode_registry_preservation(self) -> None:
        before = json.loads(
            (
                FIXTURE_ROOT / "home/.zcode/cli/plugins/installed_plugins.json"
            ).read_text(encoding="utf-8")
        )
        actual_path = self.home / ".zcode/cli/plugins/installed_plugins.json"
        after = json.loads(actual_path.read_text(encoding="utf-8"))
        expected_bytes = _materialize(
            FIXTURE_ROOT / "expected/registry/zcode-installed.json",
            self._materialized_values(),
        )
        self.assertEqual(actual_path.read_bytes(), expected_bytes)
        self.assertEqual(
            [row["name"] for row in before["plugins"]],
            [row["name"] for row in after["plugins"]],
        )
        self.assertEqual(
            [row["scope"] for row in before["plugins"]],
            [row["scope"] for row in after["plugins"]],
        )
        self.assertEqual(len(before["plugins"]), len(after["plugins"]))
        for previous, current in zip(before["plugins"], after["plugins"]):
            if previous["name"] != "zmem":
                self.assertEqual(current, previous)
                continue
            changed = {
                field
                for field in current
                if previous.get(field) != current.get(field)
            }
            self.assertEqual(changed, {"installPath", "version", "gitCommitSha"})
            self.assertEqual(current["version"], VERSION)
            self.assertEqual(current["gitCommitSha"], COMMIT_SHA)


class TempAllocatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-temp-allocator-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_permission_denial_fails_fast_without_tempfile_retry(self):
        denied = PermissionError(13, "Access is denied")
        cases = (
            (
                "directory",
                lambda: refresh_hosts._new_temp_dir(".zmem-refresh-dir-", self.tmp),
                "mkdir",
                "mkdtemp",
                "cannot create staging directory",
            ),
            (
                "file",
                lambda: refresh_hosts._new_temp_file(".zmem-refresh-file-", self.tmp),
                "open",
                "mkstemp",
                "cannot create staging file",
            ),
            (
                "path",
                lambda: refresh_hosts._new_temp_path(".zmem-refresh-path-", self.tmp),
                "open",
                "mkstemp",
                "cannot create staging file",
            ),
        )
        for kind, create, os_creator, tempfile_creator, message in cases:
            with self.subTest(kind=kind):
                with mock.patch.object(
                    refresh_hosts.tempfile,
                    tempfile_creator,
                    side_effect=AssertionError("unbounded tempfile retry"),
                ):
                    with mock.patch.object(
                        refresh_hosts.os, os_creator, side_effect=denied
                    ) as mocked_creator:
                        with self.assertRaisesRegex(refresh_hosts.RefreshError, message):
                            create()
                self.assertEqual(mocked_creator.call_count, 1)

    def test_collision_retry_succeeds_for_each_sibling_allocator(self):
        real_mkdir = refresh_hosts.os.mkdir
        mkdir_calls: list[Path] = []

        def collide_once_mkdir(path, mode):
            mkdir_calls.append(Path(path))
            if len(mkdir_calls) == 1:
                raise FileExistsError(17, "collision")
            return real_mkdir(path, mode)

        with mock.patch.object(refresh_hosts.os, "mkdir", side_effect=collide_once_mkdir):
            directory = refresh_hosts._new_temp_dir(".zmem-refresh-dir-", self.tmp)
        self.assertEqual(len(mkdir_calls), 2)
        self.assertEqual(directory.parent, self.tmp)
        self.assertTrue(directory.is_dir())

        real_open = refresh_hosts.os.open
        open_calls: list[Path] = []

        def collide_once_open(path, flags, mode):
            open_calls.append(Path(path))
            if len(open_calls) == 1:
                raise FileExistsError(17, "collision")
            return real_open(path, flags, mode)

        with mock.patch.object(refresh_hosts.os, "open", side_effect=collide_once_open):
            file_path = refresh_hosts._new_temp_file(".zmem-refresh-file-", self.tmp)
        self.assertEqual(len(open_calls), 2)
        self.assertEqual(file_path.parent, self.tmp)
        self.assertTrue(file_path.is_file())

        open_calls.clear()
        with mock.patch.object(refresh_hosts.os, "open", side_effect=collide_once_open):
            reserved = refresh_hosts._new_temp_path(".zmem-refresh-path-", self.tmp)
        self.assertEqual(len(open_calls), 2)
        self.assertEqual(reserved.parent, self.tmp)
        self.assertFalse(reserved.exists())

    def test_collision_retry_is_bounded_for_each_sibling_allocator(self):
        collision = FileExistsError(17, "collision")
        with mock.patch.object(refresh_hosts, "_TEMP_CREATE_ATTEMPTS", 3):
            with mock.patch.object(refresh_hosts.os, "mkdir", side_effect=collision) as mkdir:
                with self.assertRaisesRegex(refresh_hosts.RefreshError, r"after 3 attempts"):
                    refresh_hosts._new_temp_dir(".zmem-refresh-dir-", self.tmp)
            self.assertEqual(mkdir.call_count, 3)

            with mock.patch.object(refresh_hosts.os, "open", side_effect=collision) as open_file:
                with self.assertRaisesRegex(refresh_hosts.RefreshError, r"after 3 attempts"):
                    refresh_hosts._new_temp_file(".zmem-refresh-file-", self.tmp)
            self.assertEqual(open_file.call_count, 3)

            with mock.patch.object(refresh_hosts.os, "open", side_effect=collision) as open_path:
                with self.assertRaisesRegex(refresh_hosts.RefreshError, r"after 3 attempts"):
                    refresh_hosts._new_temp_path(".zmem-refresh-path-", self.tmp)
            self.assertEqual(open_path.call_count, 3)

    def test_partial_fd_and_reserved_path_are_cleaned_on_failure(self):
        real_close = refresh_hosts.os.close

        def close_then_fail(fd):
            real_close(fd)
            raise OSError("close failed")

        with mock.patch.object(refresh_hosts.os, "close", side_effect=close_then_fail):
            with self.assertRaisesRegex(refresh_hosts.RefreshError, r"cannot finalize staging file"):
                refresh_hosts._new_temp_file(".zmem-refresh-close-", self.tmp)
        self.assertFalse(any(path.name.startswith(".zmem-refresh-close-") for path in self.tmp.iterdir()))

        real_unlink = refresh_hosts.os.unlink

        def unlink_then_fail(path):
            real_unlink(path)
            raise OSError("unlink failed")

        with mock.patch.object(refresh_hosts.os, "unlink", side_effect=unlink_then_fail):
            with self.assertRaisesRegex(refresh_hosts.RefreshError, r"cannot reserve temporary sibling"):
                refresh_hosts._new_temp_path(".zmem-refresh-unlink-", self.tmp)
        self.assertFalse(any(path.name.startswith(".zmem-refresh-unlink-") for path in self.tmp.iterdir()))


class RollbackSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-rollback-safety-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_preimage_does_not_delete_existing_destination(self):
        destination = self.tmp / "destination"
        destination.mkdir()
        marker = destination / "state.txt"
        marker.write_text("keep", encoding="utf-8")
        missing_backup = self.tmp / "missing-backup"
        operation = refresh_hosts._Operation(
            "claude",
            "cache",
            self.tmp / "staged",
            destination,
            backup=missing_backup,
            existed=True,
            mutated=True,
        )

        failures = refresh_hosts._rollback([operation], [])

        self.assertTrue(destination.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue(any("preimage backup is missing" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
