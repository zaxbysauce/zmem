"""doctor served-drift check (issue #107, Workstream A PR 2) — AC1 + AC2.

AC1: doctor reports drift for a fixture tree with one modified hook file and
matched for an unmodified copy.
AC2: the drift check never fails the doctor path — a missing manifest
degrades to unknown/skip and doctor still exits 0.

Direct unit calls against doctor._check_served_drift plus subprocess-level
exit-code assertions (warn/skip never flip doctor's ok).

All stores are throwaway temp stores; ambient zmem env is stripped from every
child process. The operator's real store is never touched.

Runs standalone: python tests/test_doctor_drift.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
DOCTOR_PY = SCRIPTS / "doctor.py"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_doctor import (  # noqa: E402
    REAL_GIT, _cmd_script, _make_store, _write_text,
)
import doctor  # noqa: E402

from schema_meta import SUPPORTED_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import drift  # noqa: E402

PYTHON = sys.executable


def _write_manifest(root: Path, version: str = "9.9.9") -> None:
    files = drift.tree_hashes(root)
    (root / "release-manifest.json").write_text(
        json.dumps({"version": version, "algorithm": "sha256-crlf-norm",
                    "files": files, "digest": drift.aggregate(files)},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")


class DoctorServedDriftUnitTest(unittest.TestCase):
    """_check_served_drift against fixture trees (AC1)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-docdrift-"))
        self.root = self.tmp / "served"
        for rel in ("hooks/zmem-recall.sh", "hooks/hooks.claude.json",
                    "skills/memory/scripts/store.py",
                    "skills/memory/SKILL.md", "hermes-plugin/plugin.yaml"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rel.encode() + b"\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matched_tree_reports_pass(self):
        _write_manifest(self.root)
        check = doctor._check_served_drift(self.root)
        self.assertEqual(check["id"], "served-drift")
        self.assertEqual(check["status"], "pass", check["summary"])
        self.assertIn("matches", check["summary"])

    def test_modified_hook_file_reports_drift(self):
        """AC1: one modified hook file -> drift with the differing path."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        check = doctor._check_served_drift(self.root)
        self.assertEqual(check["status"], "warn", check["summary"])
        self.assertIn("DRIFTED", check["summary"])
        self.assertEqual(check["details"]["differing_count"], 1)
        self.assertEqual(check["details"]["differing"],
                         ["hooks/zmem-recall.sh"])

    def test_missing_manifest_degrades_to_skip(self):
        """AC2: no manifest -> unknown/skip, never a failure."""
        check = doctor._check_served_drift(self.root)
        self.assertEqual(check["status"], "skip", check["summary"])
        self.assertIn("No release-manifest.json", check["summary"])


class DoctorServedDriftCliTest(unittest.TestCase):
    """Exit-code contract end to end: warn/skip never flip doctor ok (AC2)."""

    def setUp(self):
        if not REAL_GIT:
            self.skipTest("git is required for the namespace fixture")
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-docdrift-cli-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.project = self.tmp / "project"
        self.bin = self.tmp / "bin"
        for d in (self.home, self.repo, self.project, self.bin):
            d.mkdir()
        self._write_fake_tools()
        self._write_repo_surfaces()
        _make_store(self.home / ".zmem" / "store.sqlite",
                    schema_version=CURRENT_SCHEMA_VERSION)
        # Same companion settings the test_doctor green path needs so the
        # native-memory / codex-trust checks do not fail the fixture report —
        # the exit-code assertion here is about served-drift, nothing else.
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}))
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join([
                "[features]",
                "memories = false",
                "",
                "[memories]",
                "use_memories = false",
                "generate_memories = false",
                "",
                f"[projects.'{str(self.project).lower()}']",
                'trust_level = "trusted"',
            ]))
        subprocess.run([REAL_GIT, "init", "-q"], cwd=str(self.project),
                       check=True)
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin",
             "https://github.com/Example/Widget.git"],
            cwd=str(self.project), check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fake_tools(self):
        _write_text(self.bin / "node.cmd", _cmd_script("echo v20.11.0"))
        _write_text(self.bin / "git.cmd",
                    _cmd_script("echo https://github.com/Example/Widget.git"))
        _write_text(self.bin / "Git" / "bin" / "bash.cmd",
                    _cmd_script("echo GNU bash, version 5.2.0"))

    def _write_repo_surfaces(self):
        _write_text(
            self.repo / ".claude-plugin" / "plugin.json",
            json.dumps({"name": "zmem", "userConfig": {
                "storeDirectory": {"default": "~/.zmem"}}}))
        _write_text(self.repo / "hooks" / "hooks.claude.json", "{}\n")
        _write_text(self.repo / ".zcode-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.zcode.json", "{}\n")
        _write_text(self.repo / "skills" / "memory" / "SKILL.md", "# memory\n")

    def _base_env(self) -> dict:
        env = {**os.environ}
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["ZMEM_BASH_PATH"] = str(self.bin / "Git" / "bin" / "bash.cmd")
        for key in ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
                    "ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "OneDrive",
                    "OneDriveConsumer", "OneDriveCommercial"):
            env.pop(key, None)
        return env

    def _run(self):
        return subprocess.run(
            [PYTHON, str(DOCTOR_PY), "--format", "json",
             "--repo-root", str(self.repo), "--project", str(self.project)],
            env=self._base_env(), capture_output=True, text=True, timeout=60)

    def _served_drift(self, report: dict) -> dict:
        matches = [c for c in report["checks"] if c["id"] == "served-drift"]
        self.assertEqual(len(matches), 1, "served-drift check missing")
        return matches[0]

    def test_cli_drifted_tree_warns_and_exits_zero(self):
        _write_manifest(self.repo)
        (self.repo / "hooks" / "hooks.claude.json").write_text("{} drifted\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        report = json.loads(r.stdout)
        check = self._served_drift(report)
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["details"]["differing"],
                         ["hooks/hooks.claude.json"])
        self.assertTrue(any("refresh" in note.lower()
                            for note in report["recommendations"]),
                        report["recommendations"])

    def test_cli_matched_tree_passes(self):
        _write_manifest(self.repo)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        self.assertEqual(self._served_drift(json.loads(r.stdout))["status"],
                         "pass")

    def test_cli_deleted_manifest_skips_and_exits_zero(self):
        """AC2 (the issue's exact prescribed shape): delete the manifest."""
        _write_manifest(self.repo)
        (self.repo / "release-manifest.json").unlink()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        self.assertEqual(self._served_drift(json.loads(r.stdout))["status"],
                         "skip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
