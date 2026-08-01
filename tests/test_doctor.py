"""Tests for skills/memory/scripts/doctor.py.

The doctor command must remain read-only: these tests run it only against temp
HOME/config/tooling fixtures and temp sqlite stores. They never touch the real
user config or the real shared store.

Run: python tests/test_doctor.py
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"
PYTHON = sys.executable
REAL_GIT = shutil.which("git")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_store(path: Path, schema_version: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(schema_version),),
        )
        conn.commit()
    finally:
        conn.close()


def _cmd_script(body: str) -> str:
    return "@echo off\n" + body + "\n"


class DoctorCliTest(unittest.TestCase):
    def setUp(self):
        if not REAL_GIT:
            self.skipTest("git is required for the namespace fixture")
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.project = self.tmp / "project"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.repo.mkdir()
        self.project.mkdir()
        self.bin.mkdir()

        self._write_fake_tools()
        self._write_repo_surfaces()
        subprocess.run([REAL_GIT, "init", "-q"], cwd=str(self.project), check=True)
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin", "https://github.com/Example/Widget.git"],
            cwd=str(self.project),
            check=True,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fake_tools(self):
        node = self.bin / "node.cmd"
        git = self.bin / "git.cmd"
        bash = self.bin / "bash.cmd"
        _write_text(node, _cmd_script('echo v20.11.0'))
        _write_text(git, _cmd_script('echo https://github.com/Example/Widget.git'))
        _write_text(bash, _cmd_script('echo GNU bash, version 5.2.0'))

    def _write_repo_surfaces(self):
        _write_text(
            self.repo / ".claude-plugin" / "plugin.json",
            json.dumps(
                {
                    "name": "zmem",
                    "userConfig": {
                        "storeDirectory": {
                            "default": "~/.zmem",
                        }
                    },
                }
            ),
        )
        _write_text(self.repo / "hooks" / "hooks.claude.json", "{}\n")
        _write_text(self.repo / ".zcode-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.zcode.json", "{}\n")
        _write_text(self.repo / "skills" / "memory" / "SKILL.md", "# memory\n")

    def _base_env(self) -> dict:
        env = {**os.environ}
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["ZMEM_BASH_PATH"] = str(self.bin / "bash.cmd")
        for key in (
            "ZMEM_STORE",
            "ZMEM_DATA",
            "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA",
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            "OneDrive",
            "OneDriveConsumer",
            "OneDriveCommercial",
        ):
            env.pop(key, None)
        return env

    def _run(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(DOCTOR_PY), *args],
            env=env or self._base_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_json_report_clean_pass(self):
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=5)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                [
                    "[features]",
                    "memories = false",
                    "",
                    "[memories]",
                    "use_memories = false",
                    "generate_memories = false",
                    "",
                    f"[projects.'{str(self.project).lower()}']",
                    'trust_level = "trusted"',
                ]
            ),
        )

        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["namespace"], "project:github.com/example/widget")

        statuses = {c["id"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["store-resolution"], "pass")
        self.assertEqual(statuses["store-location-safety"], "pass")
        self.assertEqual(statuses["schema-version"], "pass")
        self.assertEqual(statuses["claude-native-memory"], "pass")
        self.assertEqual(statuses["codex-native-memory"], "pass")
        self.assertEqual(statuses["host-surfaces"], "pass")

        surfaces = next(c for c in report["checks"] if c["id"] == "host-surfaces")
        self.assertEqual(
            surfaces["details"]["surfaces"]["codex_adapter_optional"]["present"],
            [],
            "optional Codex adapter files missing must not fail the doctor",
        )

    def test_human_report_returns_nonzero_on_native_memory_blockers(self):
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=5)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": True}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                [
                    "[features]",
                    "memories = true",
                    "",
                    "[memories]",
                    "use_memories = true",
                    "generate_memories = true",
                ]
            ),
        )

        result = self._run(
            "--format",
            "human",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("claude-native-memory", result.stdout)
        self.assertIn("codex-native-memory", result.stdout)
        self.assertIn("BLOCKED", result.stdout)

    def test_conflicting_env_and_onedrive_path_are_reported_read_only(self):
        onedrive_root = self.home / "OneDrive"
        conflicting_store = onedrive_root / "zmem" / "store.sqlite"
        env = self._base_env()
        env["OneDrive"] = str(onedrive_root)
        env["ZMEM_STORE"] = str(conflicting_store)
        env["ZMEM_DATA"] = str(self.home / "shared-a")
        env["CLAUDE_PLUGIN_DATA"] = str(self.home / "shared-b")
        env["ZCODE_PLUGIN_DATA"] = str(self.home / "shared-c")
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                [
                    "[features]",
                    "memories = false",
                    "",
                    "[memories]",
                    "use_memories = false",
                    "generate_memories = false",
                ]
            ),
        )
        self.assertFalse(conflicting_store.exists(), "fixture must start absent")

        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        statuses = {c["id"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["store-resolution"], "fail")
        self.assertEqual(statuses["store-location-safety"], "fail")
        self.assertFalse(
            conflicting_store.exists(),
            "doctor must not create or mutate the store path during diagnostics",
        )

    def test_store_missing_yields_warning_not_write(self):
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                [
                    "[features]",
                    "memories = false",
                    "",
                    "[memories]",
                    "use_memories = false",
                    "generate_memories = false",
                ]
            ),
        )
        env = self._base_env()
        env["ZMEM_DATA"] = str(self.home / "fresh-store")
        target = Path(env["ZMEM_DATA"]) / "store.sqlite"
        self.assertFalse(target.exists())

        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
        )
        report = json.loads(result.stdout)
        access = next(c for c in report["checks"] if c["id"] == "store-access")
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        self.assertEqual(access["status"], "warn")
        self.assertEqual(schema["status"], "warn")
        self.assertFalse(target.exists(), "doctor must not initialize the store")


if __name__ == "__main__":
    unittest.main(verbosity=2)
