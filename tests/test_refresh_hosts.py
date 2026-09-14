"""Acceptance checks for issue #184: transactional host-cache refresh.

The fixture is a throwaway, committed checkout and every destination is under
an injected temporary home.  No test reads or writes the operator's real home,
plugin caches, registries, or marketplace clone.

This is intentionally a NEW-SURFACE check on the pre-fix tree: importing the
two production modules below must fail until the implementation exists.
"""

from __future__ import annotations

import json
import os
import contextlib
import io
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
DRIFT_SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(DRIFT_SCRIPTS))

# Unconditional imports are deliberate.  They produce the expected
# ModuleNotFoundError NEW-SURFACE result before issue #184 is implemented.
import host_registry  # noqa: E402
import refresh_hosts  # noqa: E402
from drift import aggregate, tree_hashes  # noqa: E402


HOSTS = ("codex", "claude", "zcode")
VERSION = "0.36.0"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _claude_v2() -> dict:
    """Claude's v2 installed_plugins.json: mapping of plugin -> list."""
    return {
        "version": 2,
        "plugins": {
            "other@official": [
                {
                    "scope": "user",
                    "installPath": "C:/operator/other",
                    "version": "9.9.9",
                    "gitCommitSha": "f" * 40,
                    "installedAt": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                }
            ],
            "zmem@zmem": [
                {
                    "scope": "user",
                    "installPath": "C:/operator/old-zmem",
                    "version": "0.32.0",
                    "gitCommitSha": "e" * 40,
                    "installedAt": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                }
            ],
        },
    }


def _zcode_v1() -> dict:
    """ZCode's v1 installed_plugins.json: an ordered plugins list."""
    return {
        "version": 1,
        "plugins": [
            {
                "name": "other",
                "installPath": "C:/operator/other",
                "version": "9.9.9",
                "gitCommitSha": "f" * 40,
            },
            {
                "name": "zmem",
                "installPath": "C:/operator/old-zmem",
                "version": "0.32.0",
                "gitCommitSha": "e" * 40,
            },
        ],
    }


def _commit(repo: Path) -> str:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.email=issue184@example.invalid",
             "-c", "user.name=issue184", *args],
            cwd=repo, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "fixture")
    return git("rev-parse", "HEAD")


def _make_checkout(root: Path) -> Path:
    """Build seven agreeing manifests plus a real drift release manifest."""
    checkout = root / "checkout"
    checkout.mkdir()

    # The seven host-facing manifests returned by release_gate discovery, all
    # at the next minor.  Do not add convenience package/plugin manifests:
    # release parity must bind to this exact set.
    manifests = {
        ".claude-plugin/plugin.json": {"name": "zmem", "version": VERSION},
        ".claude-plugin/marketplace.json": {
            "plugins": [{"name": "zmem", "version": VERSION}]
        },
        ".codex-plugin/plugin.json": {"name": "zmem", "version": VERSION},
        ".zcode-plugin/plugin.json": {"name": "zmem", "version": VERSION},
        ".agents/plugins/marketplace.json": {
            "plugins": [{"name": "zmem", "version": VERSION}]
        },
        "marketplace.json": {"plugins": [{"name": "zmem", "version": VERSION}]},
    }
    for rel, value in manifests.items():
        _write(checkout / rel, _json_bytes(value))
    _write(checkout / "hermes-plugin/plugin.yaml",
           f"name: zmem\nversion: {VERSION}\n".encode())

    # Minimal served surface copied into each host cache.  tree_hashes() is
    # the repository-owned CRLF-normalized release-manifest implementation;
    # no digest is hand-authored in the fixture.
    _write(checkout / "hooks/zmem-recall.sh", b"#!/bin/sh\n# fixture\n")
    _write(checkout / "skills/memory/SKILL.md", b"# fixture memory skill\n")
    _write(checkout / "skills/memory/scripts/store.py", b"# fixture store\n")
    _write(checkout / "scripts/host_canary.py", b"# fixture canary\n")
    files = tree_hashes(checkout)
    _write(
        checkout / "release-manifest.json",
        _json_bytes({
            "version": VERSION,
            "algorithm": "sha256-crlf-norm",
            "files": files,
            "digest": aggregate(files),
        }),
    )

    _commit(checkout)
    return checkout


class _RefreshFixtureMixin:
    def _fixture(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-refresh184-"))
        self.checkout = _make_checkout(self.tmp)
        self.home = self.tmp / "fake-home"
        self.home.mkdir()
        self.report = self.tmp / "refresh-report.json"

        # Seed both supported registries at host-derived default paths.  The
        # codec tests also exercise arbitrary temporary paths directly.
        _write(self.home / ".claude/plugins/installed_plugins.json",
               _json_bytes(_claude_v2()))
        _write(self.home / ".zcode/cli/plugins/installed_plugins.json",
               _json_bytes(_zcode_v1()))

    def _main_status(self, *args: str) -> int:
        argv = ["--checkout", str(self.checkout), "--report", str(self.report)]
        argv.extend(args)
        with mock.patch.object(refresh_hosts.Path, "home", return_value=self.home):
            try:
                result = refresh_hosts.main(argv)
            except SystemExit as exc:
                return int(exc.code or 0)
        return int(result or 0)

    def _refresh_args(self, *args: str) -> tuple[str, ...]:
        return ("--hosts", ",".join(HOSTS), *args)

    def _report(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))

    def _reported_paths(self, report: dict) -> list[Path]:
        paths: list[Path] = []
        for row in report["hosts"]:
            cache = Path(row["cacheRoot"])
            paths.extend(p for p in cache.rglob("*") if p.is_file())
            if row["registryPath"] is not None:
                paths.append(Path(row["registryPath"]))
            paths.extend(Path(p) for p in row["marketplacePaths"])
        return [p for p in paths if p.exists() and p.is_file()]

    def _state(self, report: dict | None = None) -> dict[str, bytes]:
        report = report or self._report()
        return {str(p): p.read_bytes() for p in self._reported_paths(report)}


class HostRefreshFixtureTest(_RefreshFixtureMixin, unittest.TestCase):
    """Public registry codecs and transactional refresh behavior."""

    def setUp(self):
        self._fixture()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claude_v2_registry_updates_only_zmem_preserving_mapping_order(self):
        path = self.tmp / "claude-installed.json"
        original = _claude_v2()
        _write(path, _json_bytes(original))
        schema_version, loaded = host_registry.load_host_registry(path, "claude")
        self.assertEqual(schema_version, "v2")
        self.assertEqual(loaded, original)

        updated = host_registry.update_host_registry(
            loaded, host="claude", version=VERSION,
            git_commit_sha=COMMIT_SHA, install_path="C:/staged/zmem-claude",
        )
        self.assertEqual(list(updated["plugins"]), ["other@official", "zmem@zmem"])
        self.assertEqual(updated["plugins"]["other@official"],
                         original["plugins"]["other@official"])
        entry = updated["plugins"]["zmem@zmem"][0]
        self.assertEqual(entry["installPath"], "C:/staged/zmem-claude")
        self.assertEqual(entry["version"], VERSION)
        self.assertEqual(entry["gitCommitSha"], COMMIT_SHA)
        self.assertEqual(entry["installedAt"],
                         original["plugins"]["zmem@zmem"][0]["installedAt"])
        host_registry.write_host_registry(path, updated)
        self.assertEqual(host_registry.load_host_registry(path, "claude")[1], updated)

    def test_zcode_v1_registry_updates_only_zmem_preserving_list_order(self):
        path = self.tmp / "zcode-installed.json"
        original = _zcode_v1()
        _write(path, _json_bytes(original))
        schema_version, loaded = host_registry.load_host_registry(path, "zcode")
        self.assertEqual(schema_version, "v1")
        updated = host_registry.update_host_registry(
            loaded, host="zcode", version=VERSION,
            git_commit_sha=COMMIT_SHA, install_path="C:/staged/zmem-zcode",
        )
        self.assertEqual([row["name"] for row in updated["plugins"]],
                         ["other", "zmem"])
        self.assertEqual(updated["plugins"][0], original["plugins"][0])
        self.assertEqual(updated["plugins"][1]["installPath"], "C:/staged/zmem-zcode")
        self.assertEqual(updated["plugins"][1]["version"], VERSION)
        self.assertEqual(updated["plugins"][1]["gitCommitSha"], COMMIT_SHA)

    def test_unknown_registry_version_or_shape_raises_registry_schema_error(self):
        bad_claude = self.tmp / "bad-claude.json"
        _write(bad_claude, _json_bytes({"version": 9, "plugins": {}}))
        with self.assertRaises(host_registry.RegistrySchemaError):
            host_registry.load_host_registry(bad_claude, "claude")
        bad_zcode = self.tmp / "bad-zcode.json"
        _write(bad_zcode, _json_bytes({"version": 1, "plugins": {}}))
        with self.assertRaises(host_registry.RegistrySchemaError):
            host_registry.load_host_registry(bad_zcode, "zcode")

        # The refresh coordinator must reject the same bad shape before it
        # stages or replaces any fake-home destination.
        bad_home = self.tmp / "bad-home"
        _write(bad_home / ".claude/plugins/installed_plugins.json",
               _json_bytes({"version": 9, "plugins": {}}))
        _write(bad_home / ".zcode/cli/plugins/installed_plugins.json",
               _json_bytes(_zcode_v1()))
        before = {
            str(p): p.read_bytes()
            for p in bad_home.rglob("*")
            if p.is_file() and p.name != refresh_hosts._REFRESH_LOCK_NAME
        }
        argv = ["--checkout", str(self.checkout), "--report", str(self.report)]
        with mock.patch.object(refresh_hosts.Path, "home", return_value=bad_home):
            status = refresh_hosts.main(argv)
        self.assertNotEqual(int(status or 0), 0)
        self.assertEqual(
            {
                str(p): p.read_bytes()
                for p in bad_home.rglob("*")
                if p.is_file() and p.name != refresh_hosts._REFRESH_LOCK_NAME
            },
            before,
        )
        self.assertFalse((bad_home / ".codex").exists())
        self.assertFalse((bad_home / ".zcode/cli/plugins/cache").exists())

    def test_default_refresh_report_is_schema_valid_and_has_three_hosts(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        report = self._report()
        schema = json.loads((SCRIPTS / "refresh-report-schema.json").read_text(encoding="utf-8"))
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is unavailable")

        jsonschema.validate(report, schema)
        self.assertEqual(report["checkout"], str(self.checkout.resolve()))
        self.assertEqual(report["version"], VERSION)
        self.assertRegex(report["gitCommitSha"], r"^[0-9a-f]{40}$")
        self.assertEqual([row["host"] for row in report["hosts"]], list(HOSTS))
        self.assertEqual(report["mismatchCount"], 0)
        self.assertTrue(report["ok"])
        expected_caches = {
            "codex": (self.home / ".codex/plugins/cache/personal/zmem/0.36.0").resolve(),
            "claude": (self.home / ".claude/plugins/cache/zmem/zmem/0.36.0").resolve(),
            "zcode": (self.home / ".zcode/cli/plugins/cache/zmem/zmem/0.36.0").resolve(),
        }
        expected_registries = {
            "codex": None,
            "claude": (self.home / ".claude/plugins/installed_plugins.json").resolve(),
            "zcode": (self.home / ".zcode/cli/plugins/installed_plugins.json").resolve(),
        }
        for row in report["hosts"]:
            self.assertEqual(
                set(row), {"host", "cacheRoot", "registryPath", "marketplacePaths",
                           "beforeDigest", "afterDigest", "status", "mismatches"},
            )
            self.assertEqual(row["mismatches"], [])
            self.assertEqual(
                Path(row["cacheRoot"]).resolve(),
                expected_caches[row["host"]].resolve(),
            )
            self.assertEqual(
                Path(row["registryPath"]).resolve()
                if row["registryPath"] is not None
                else None,
                expected_registries[row["host"]].resolve()
                if expected_registries[row["host"]] is not None
                else None,
            )
            for path in row["marketplacePaths"]:
                self.assertTrue(
                    str(Path(path).resolve()).startswith(str(self.home.resolve()))
                )

    @unittest.skipUnless(os.name != "nt", "POSIX mode bits are not portable on Windows")
    def test_transaction_preserves_existing_registry_modes(self):
        claude_registry = self.home / ".claude/plugins/installed_plugins.json"
        zcode_registry = self.home / ".zcode/cli/plugins/installed_plugins.json"
        os.chmod(claude_registry, 0o640)
        os.chmod(zcode_registry, 0o604)

        self.assertEqual(self._main_status(*self._refresh_args()), 0)

        self.assertEqual(stat.S_IMODE(claude_registry.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(zcode_registry.stat().st_mode), 0o604)

    def test_dirty_or_untracked_checkout_is_rejected_before_mirroring(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before = {
            str(path): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }
        _write(self.checkout / "untracked.txt", b"must not be mirrored\n")

        status = self._main_status(*self._refresh_args())

        self.assertNotEqual(status, 0)
        self.assertEqual(
            {
                str(path): path.read_bytes()
                for path in self.home.rglob("*")
                if path.is_file()
            },
            before,
        )
        self.assertIn("checkout must be clean", " ".join(
            mismatch
            for row in self._report()["hosts"]
            for mismatch in row["mismatches"]
        ))
        self.assertFalse(any(path.name.startswith(".zmem-refresh-")
                             for path in self.home.rglob("*")))

    def test_ignored_mirrorable_checkout_file_is_rejected_before_staging(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before = {
            str(path): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }
        exclude = self.checkout / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\nignored-runtime.txt\n",
            encoding="utf-8",
        )
        _write(self.checkout / "ignored-runtime.txt", b"must not be mirrored\n")

        status = self._main_status(*self._refresh_args())

        self.assertNotEqual(status, 0)
        self.assertEqual(
            {
                str(path): path.read_bytes()
                for path in self.home.rglob("*")
                if path.is_file()
            },
            before,
        )
        mismatches = " ".join(
            mismatch
            for row in self._report()["hosts"]
            for mismatch in row["mismatches"]
        )
        self.assertIn("ignored-runtime.txt", mismatches)
        self.assertFalse(any(path.name.startswith(".zmem-refresh-")
                             for path in self.home.rglob("*")))

    def test_unknown_version_report_collision_with_prior_cache_fails_closed(self):
        prior_cache = self.home / ".codex/plugins/cache/personal/zmem/0.32.0"
        prior_cache.mkdir(parents=True)
        prior_report = prior_cache / "refresh-report.json"
        prior_report.write_bytes(b"previous report\n")

        with mock.patch.object(refresh_hosts.Path, "home", return_value=self.home):
            status = refresh_hosts.main([
                "--checkout", str(self.checkout),
                "--report", str(prior_report),
                "--hosts", "codex",
                "--dry-run",
            ])

        self.assertNotEqual(int(status or 0), 0)
        self.assertEqual(prior_report.read_bytes(), b"previous report\n")

    def test_lock_rejects_a_second_refresh_without_mutating_home(self):
        lock = self.home / refresh_hosts._REFRESH_LOCK_NAME
        with refresh_hosts._refresh_lock(self.home):
            before = {
                str(path): path.read_bytes()
                for path in self.home.rglob("*")
                if path.is_file() and path.name != refresh_hosts._REFRESH_LOCK_NAME
            }
            status = self._main_status(*self._refresh_args())

        self.assertNotEqual(status, 0)
        self.assertEqual(
            {
                str(path): path.read_bytes()
                for path in self.home.rglob("*")
                if path.is_file() and path.name != refresh_hosts._REFRESH_LOCK_NAME
            },
            before,
        )
        self.assertTrue(lock.is_file())

    def test_stale_lock_marker_does_not_block_a_new_refresh(self):
        lock = self.home / refresh_hosts._REFRESH_LOCK_NAME
        lock.write_bytes(b"stale owner marker\n")

        status = self._main_status(*self._refresh_args())

        self.assertEqual(status, 0)
        self.assertTrue(lock.is_file())
        self.assertTrue(self.report.is_file())

    def test_raw_parent_components_are_rejected_before_normalization(self):
        with mock.patch.object(refresh_hosts, "_git_head", return_value=COMMIT_SHA):
            with self.assertRaisesRegex(refresh_hosts.RefreshError, "parent traversal"):
                refresh_hosts.refresh_host(
                    self.checkout / ".." / self.checkout.name,
                    "codex",
                    self.home,
                    dry_run=True,
                )
            with self.assertRaisesRegex(refresh_hosts.RefreshError, "parent traversal"):
                refresh_hosts.refresh_host(
                    self.checkout,
                    "codex",
                    self.home / ".." / self.home.name,
                    dry_run=True,
                )
            with self.assertRaisesRegex(refresh_hosts.RefreshError, "parent traversal"):
                refresh_hosts._refresh_transaction(
                    self.home,
                    self.checkout,
                    ("codex",),
                    self.tmp / ".." / self.tmp.name / "report.json",
                    dry_run=True,
                )

    def test_checkout_destination_overlap_is_rejected_before_staging(self):
        plan = refresh_hosts._HostPlan(
            "codex", self.checkout / ".codex-cache", None, [], None, None
        )
        with self.assertRaisesRegex(refresh_hosts.RefreshError, "checkout overlaps"):
            refresh_hosts._validate_destinations([plan], checkout=self.checkout)

    def test_commit_does_not_delete_destinations_before_replacement(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        destinations = {
            Path(row["cacheRoot"]).resolve()
            for row in self._report()["hosts"]
        }
        destinations.update(
            Path(row["registryPath"]).resolve()
            for row in self._report()["hosts"]
            if row["registryPath"] is not None
        )
        destinations.update(
            Path(path).resolve()
            for row in self._report()["hosts"]
            for path in row["marketplacePaths"]
        )
        removed: list[Path] = []
        real_remove = refresh_hosts._remove_path

        def observe(path):
            removed.append(Path(path).resolve())
            return real_remove(path)

        with mock.patch.object(refresh_hosts, "_remove_path", side_effect=observe):
            self.assertEqual(self._main_status(*self._refresh_args()), 0)

        self.assertTrue(removed)
        self.assertTrue(destinations.isdisjoint(removed))

    def test_full_refresh_installs_expected_checkout_and_marketplace_bytes(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        report = self._report()
        expected = {
            "hooks/zmem-recall.sh": (self.checkout / "hooks/zmem-recall.sh").read_bytes(),
            "skills/memory/SKILL.md": (self.checkout / "skills/memory/SKILL.md").read_bytes(),
            "skills/memory/scripts/store.py": (self.checkout / "skills/memory/scripts/store.py").read_bytes(),
            "hermes-plugin/plugin.yaml": (self.checkout / "hermes-plugin/plugin.yaml").read_bytes(),
        }
        for row in report["hosts"]:
            cache = Path(row["cacheRoot"])
            for rel, data in expected.items():
                self.assertEqual((cache / rel).read_bytes(), data)
        marketplace_bytes = {
            "marketplace.json": (self.checkout / "marketplace.json").read_bytes(),
            ".claude-plugin/marketplace.json": (
                self.checkout / ".claude-plugin/marketplace.json"
            ).read_bytes(),
        }
        zcode = next(row for row in report["hosts"] if row["host"] == "zcode")
        marketplace_root = self.home / ".zcode/cli/plugins/marketplaces/zmem"
        for path in zcode["marketplacePaths"]:
            rel = Path(path).resolve().relative_to(marketplace_root.resolve()).as_posix()
            self.assertIn(rel, marketplace_bytes)
            self.assertEqual(Path(path).read_bytes(), marketplace_bytes[rel])

    def test_dry_run_only_writes_report_and_preserves_all_destination_preimages(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before_report = self._report()
        before = self._state(before_report)
        self.assertEqual(self._main_status(*self._refresh_args(), "--dry-run"), 0)
        self.assertEqual(self._state(self._report()), before)
        self.assertTrue(self.report.is_file())

    def test_copy_failure_is_nonzero_and_rolls_back_without_partial_install(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before_report = self._report()
        before = self._state(before_report)
        with mock.patch.object(
            refresh_hosts, "_copy_file", side_effect=OSError("injected copy failure")
        ):
            status = self._main_status(*self._refresh_args())
        self.assertNotEqual(status, 0)
        self.assertEqual(self._state(before_report), before)
        failure = self._report()
        self.assertFalse(failure["ok"])
        self.assertGreaterEqual(failure["mismatchCount"], 1)

    def test_injected_os_replace_failure_is_nonzero_and_rolls_back_every_destination(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before_report = self._report()
        before = self._state(before_report)
        before_tree = {
            str(p.relative_to(self.home)): ("dir", None) if p.is_dir()
            else ("file", p.read_bytes())
            for p in self.home.rglob("*")
        }
        claude_cache = Path(
            next(row["cacheRoot"] for row in before_report["hosts"]
                 if row["host"] == "claude")
        ).resolve()

        real_replace = os.replace

        def fail_at_claude_cache(src, dst):
            if Path(dst).resolve() == claude_cache:
                raise OSError("injected replacement failure")
            return real_replace(src, dst)

        # Checkout bytes and release-manifest remain untouched, so validation
        # succeeds; codex commits first, then the selected Claude replacement
        # fails and must roll the whole transaction back.
        with mock.patch.object(refresh_hosts.os, "replace", side_effect=fail_at_claude_cache):
            status = self._main_status(*self._refresh_args())
        self.assertNotEqual(status, 0)
        self.assertEqual(self._state(before_report), before)
        after_tree = {
            str(p.relative_to(self.home)): ("dir", None) if p.is_dir()
            else ("file", p.read_bytes())
            for p in self.home.rglob("*")
        }
        self.assertEqual(after_tree, before_tree,
                         "rollback must remove temp/backup residue as well")
        self.assertFalse(self._report()["ok"])

    def test_report_write_failure_is_nonzero_and_rolls_back_all_destinations(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        before_report = self._report()
        before = self._state(before_report)
        before_tree = {
            str(p.relative_to(self.home)): ("dir", None) if p.is_dir()
            else ("file", p.read_bytes())
            for p in self.home.rglob("*")
        }
        before_report_bytes = self.report.read_bytes()
        requested_report = self.report.resolve()
        real_replace = os.replace

        def fail_report_write(src, dst):
            if Path(dst).resolve() == requested_report:
                raise OSError("injected report-write failure")
            return real_replace(src, dst)

        with mock.patch.object(refresh_hosts.os, "replace", side_effect=fail_report_write):
            status = self._main_status(*self._refresh_args())
        self.assertNotEqual(status, 0)
        self.assertEqual(self._state(before_report), before)
        after_tree = {
            str(p.relative_to(self.home)): ("dir", None) if p.is_dir()
            else ("file", p.read_bytes())
            for p in self.home.rglob("*")
        }
        self.assertEqual(after_tree, before_tree,
                         "report failure must remove staged/backup residue")
        self.assertEqual(self.report.read_bytes(), before_report_bytes,
                         "failed atomic report write must preserve prior report")

    def test_post_commit_cleanup_failure_keeps_new_state_and_retains_preimage(self):
        """Cleanup errors must not roll back an already committed transaction."""
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        old_cache = (
            self.home / ".codex/plugins/cache/personal/zmem/0.36.0"
        ).resolve()
        old_bytes = (old_cache / "hooks/zmem-recall.sh").read_bytes()

        # Make the next committed checkout observably different from the
        # preimage installed by the first refresh.
        new_bytes = b"#!/bin/sh\n# committed second release\n"
        _write(self.checkout / "hooks/zmem-recall.sh", new_bytes)
        manifest_path = self.checkout / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = tree_hashes(self.checkout)
        manifest["files"] = files
        manifest["digest"] = aggregate(files)
        _write(manifest_path, _json_bytes(manifest))
        _commit(self.checkout)

        failed_cleanup: list[Path] = []
        real_remove = refresh_hosts._remove_path

        def fail_first_backup_cleanup(path):
            candidate = Path(path)
            if not failed_cleanup and "-backup-" in candidate.name:
                failed_cleanup.append(candidate)
                raise OSError("injected post-commit cleanup failure")
            return real_remove(path)

        with mock.patch.object(
            refresh_hosts, "_remove_path", side_effect=fail_first_backup_cleanup
        ):
            status = self._main_status(*self._refresh_args())

        self.assertNotEqual(status, 0)
        self.assertEqual(len(failed_cleanup), 1)
        retained = failed_cleanup[0]
        self.assertTrue(retained.exists(), "failed cleanup must retain the preimage")
        self.assertEqual(
            (retained / "hooks/zmem-recall.sh").read_bytes()
            if retained.is_dir()
            else retained.read_bytes(),
            old_bytes,
        )
        self.assertEqual((old_cache / "hooks/zmem-recall.sh").read_bytes(), new_bytes)

        report = self._report()
        self.assertFalse(report["ok"])
        self.assertGreaterEqual(report["mismatchCount"], 1)
        self.assertTrue(
            any(
                "post-commit cleanup failure" in mismatch
                for row in report["hosts"]
                for mismatch in row["mismatches"]
            )
        )
        refresh_hosts._validate_report(report)

    def test_cli_help_exposes_only_four_public_flags_and_default_hosts(self):
        output = io.StringIO()
        with mock.patch.object(refresh_hosts.Path, "home", return_value=self.home):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as ctx:
                    refresh_hosts.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = output.getvalue()
        listed = set(re.findall(r"--[a-z-]+", help_text))
        self.assertEqual(listed - {"--help"},
                         {"--checkout", "--report", "--hosts", "--dry-run"})
        self.assertRegex(help_text, r"default[^\n]*(codex[^\n]*claude[^\n]*zcode|codex,claude,zcode)",
                         "help must state the codex,claude,zcode default")
        self.assertNotIn("--home", help_text)

        # Black-box confirmation of the stated default, without passing
        # --hosts: the report must still contain the canonical three hosts.
        default_report = self.tmp / "default-hosts-report.json"
        with mock.patch.object(refresh_hosts.Path, "home", return_value=self.home):
            status = refresh_hosts.main([
                "--checkout", str(self.checkout), "--report", str(default_report),
            ])
        self.assertEqual(int(status or 0), 0)
        report = json.loads(default_report.read_text(encoding="utf-8"))
        self.assertEqual([row["host"] for row in report["hosts"]], list(HOSTS))

    def test_main_rejects_empty_duplicate_and_unknown_hosts_with_exit_two(self):
        for hosts in ("", "codex,codex", "codex,unknown"):
            with self.subTest(hosts=hosts):
                self.assertEqual(self._main_status("--hosts", hosts), 2)


class ReleaseParityTest(_RefreshFixtureMixin, unittest.TestCase):
    """The checkout validator consumes one agreeing release and git identity."""

    def setUp(self):
        self._fixture()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_has_seven_host_facing_manifests_at_next_minor(self):
        paths = (
            ".claude-plugin/plugin.json", ".codex-plugin/plugin.json",
            ".zcode-plugin/plugin.json", ".claude-plugin/marketplace.json",
            ".agents/plugins/marketplace.json", "marketplace.json",
            "hermes-plugin/plugin.yaml",
        )
        for rel in paths:
            self.assertTrue((self.checkout / rel).is_file(), rel)
            text = (self.checkout / rel).read_text(encoding="utf-8")
            self.assertIn(VERSION, text)

    def test_refresh_uses_committed_checkout_sha_in_report(self):
        self.assertEqual(self._main_status(*self._refresh_args()), 0)
        report = self._report()
        actual = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.checkout,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(report["gitCommitSha"], actual)
        self.assertRegex(actual, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
