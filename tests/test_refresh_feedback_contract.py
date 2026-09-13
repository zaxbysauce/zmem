"""Focused regression tests for the issue #184 feedback contract.

These tests deliberately stay separate from the frozen acceptance suites.  They
pin the externally consumed report schema, the registry writer's filesystem
boundary, and the operator/documentation contracts called out during review.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import host_registry  # noqa: E402


COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
HOST_RECORD_KEYS = {
    "host",
    "cacheRoot",
    "registryPath",
    "marketplacePaths",
    "beforeDigest",
    "afterDigest",
    "status",
    "mismatches",
}


def _host_record(host: str, *, mismatches: list[str] | None = None) -> dict[str, object]:
    return {
        "host": host,
        "cacheRoot": f"/cache/{host}",
        "registryPath": None if host == "codex" else f"/registry/{host}.json",
        "marketplacePaths": [],
        "beforeDigest": None,
        "afterDigest": "a" * 64,
        "status": "refreshed",
        "mismatches": mismatches or [],
    }


def _report(hosts: list[str], *, ok: bool = True, mismatch_count: int = 0) -> dict[str, object]:
    return {
        "checkout": "/checkout",
        "version": "0.35.0",
        "gitCommitSha": COMMIT_SHA,
        "hosts": [_host_record(host) for host in hosts],
        "mismatchCount": mismatch_count,
        "ok": ok,
    }


def _claude_registry() -> dict[str, object]:
    return {
        "version": 2,
        "plugins": {
            "other@official": [{"installPath": "/other"}],
            "zmem@zmem": [
                {
                    "scope": "user",
                    "installPath": "/old/zmem",
                    "version": "0.34.0",
                    "gitCommitSha": "f" * 40,
                }
            ],
        },
    }


class RefreshFeedbackContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-refresh-feedback-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_external_schema_enforces_canonical_host_order(self) -> None:
        import jsonschema

        schema = json.loads(
            (SCRIPTS / "refresh-report-schema.json").read_text(encoding="utf-8")
        )
        jsonschema.Draft7Validator.check_schema(schema)
        validator = jsonschema.Draft7Validator(schema)

        for hosts in (
            ["codex"],
            ["claude"],
            ["zcode"],
            ["codex", "claude"],
            ["codex", "zcode"],
            ["claude", "zcode"],
            ["codex", "claude", "zcode"],
        ):
            with self.subTest(hosts=hosts):
                self.assertEqual(list(validator.iter_errors(_report(hosts))), [])

        for hosts in (["claude", "codex"], ["zcode", "codex"], ["codex", "zcode", "claude"]):
            with self.subTest(hosts=hosts):
                self.assertTrue(list(validator.iter_errors(_report(list(hosts)))))

    def test_external_schema_enforces_ok_and_mismatch_invariants(self) -> None:
        import jsonschema

        schema = json.loads(
            (SCRIPTS / "refresh-report-schema.json").read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft7Validator(schema)

        successful = _report(["codex", "claude", "zcode"])
        self.assertEqual(list(validator.iter_errors(successful)), [])

        bad_success = _report(["codex"], mismatch_count=1)
        self.assertTrue(list(validator.iter_errors(bad_success)))
        bad_failure = _report(["codex"], ok=False, mismatch_count=0)
        self.assertTrue(list(validator.iter_errors(bad_failure)))

        bad_success["hosts"][0]["mismatches"] = ["failed"]  # type: ignore[index]
        self.assertTrue(list(validator.iter_errors(bad_success)))

    def test_registry_rejects_raw_parent_reference_before_normalization(self) -> None:
        destination = self.tmp / "trusted" / "link" / ".." / "installed_plugins.json"
        with self.assertRaisesRegex(OSError, "parent reference"):
            host_registry.write_host_registry(destination, _claude_registry())
        self.assertFalse((self.tmp / "trusted" / "installed_plugins.json").exists())

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-specific")
    def test_registry_rejects_windows_junction_parent(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        junction = self.tmp / "junction"
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.skipTest(f"junction creation unavailable: {result.stderr.strip()}")
        self.addCleanup(
            lambda: subprocess.run(
                ["cmd.exe", "/c", "rmdir", str(junction)],
                capture_output=True,
                check=False,
            )
        )

        destination = junction / "installed_plugins.json"
        with self.assertRaisesRegex(OSError, "link or reparse point"):
            host_registry.write_host_registry(destination, _claude_registry())
        self.assertFalse((outside / "installed_plugins.json").exists())

    @unittest.skipUnless(os.name != "nt", "POSIX mode bits are not portable on Windows")
    def test_registry_preserves_existing_mode_before_atomic_replace(self) -> None:
        destination = self.tmp / "installed_plugins.json"
        destination.write_text(json.dumps(_claude_registry()), encoding="utf-8")
        os.chmod(destination, 0o640)
        updated = host_registry.update_host_registry(
            _claude_registry(),
            host="claude",
            version="0.35.0",
            git_commit_sha=COMMIT_SHA,
            install_path=self.tmp / "cache",
        )

        host_registry.write_host_registry(destination, updated)

        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8")),
            updated,
        )

    def test_ci_requires_schema_validator_and_docs_order_side_effects(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install --disable-pip-version-check \"jsonschema>=4,<5\"", workflow)

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        readme_section = readme[readme.index("#### Host refresh paths") :]
        self.assertLess(
            readme_section.index("refresh before any host"),
            readme_section.index("codex plugin add"),
        )
        self.assertNotIn("runs this refresh immediately\nafter its existing `codex plugin add`", readme_section)

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        changelog_section = changelog[changelog.index("## [0.35.0]") :]
        self.assertLess(
            changelog_section.index("refresh before `codex plugin add`"),
            changelog_section.index("`codex plugin add` or"),
        )

        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertNotIn("tests/fixtures/hosts/** -text", attributes)


if __name__ == "__main__":
    unittest.main()
