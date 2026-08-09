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

# The schema version doctor must agree with. Imported from the single source of
# truth (schema_meta) so this test fails loudly if doctor drifts again (#36 M11).
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
from schema_meta import SUPPORTED_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION  # noqa: E402


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_store(path: Path, schema_version: int = 7) -> None:
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
        bash = self.bin / "Git" / "bin" / "bash.cmd"
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
        env["ZMEM_BASH_PATH"] = str(self.bin / "Git" / "bin" / "bash.cmd")
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
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
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
        if os.name == "nt":
            self.assertEqual(statuses["windows-bash"], "pass")

        surfaces = next(c for c in report["checks"] if c["id"] == "host-surfaces")
        self.assertEqual(
            surfaces["details"]["surfaces"]["codex_adapter_optional"]["present"],
            [],
            "optional Codex adapter files missing must not fail the doctor",
        )

    def test_human_report_returns_nonzero_on_native_memory_blockers(self):
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
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

    def test_scalar_hooks_false_is_accepted_without_traceback(self):
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "hooks = false\n\n[features]\nmemories = false\n",
        )
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"], report)

    @unittest.skipUnless(os.name == "nt", "Windows shell classification only")
    def test_unrecognized_runnable_windows_shell_fails_closed(self):
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "[features]\nmemories = false\n",
        )
        env = self._base_env()
        env["ZMEM_BASH_PATH"] = str(self.bin / "node.cmd")
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
        shell = next(c for c in report["checks"] if c["id"] == "windows-bash")
        self.assertEqual(shell["status"], "fail")
        self.assertIn("not recognized", shell["summary"])

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

    def test_current_schema_version_store_passes_doctor(self):
        """Regression for #36 M11: a store at the CURRENT schema version must
        PASS doctor's schema-version check (not FAIL because doctor hardcoded a
        stale older version)."""
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                ["[features]", "memories = false", "", "[memories]",
                 "use_memories = false", "generate_memories = false"]
            ),
        )
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        self.assertEqual(schema["status"], "pass", schema)
        self.assertEqual(schema["details"]["actual"], CURRENT_SCHEMA_VERSION)

    def test_stale_older_schema_version_warns_not_fails(self):
        """A store at an OLDER schema version than current must WARN (migrate
        needed), not FAIL — and a store at a NEWER version must FAIL."""
        store_dir = self.home / ".zmem"
        # An older store (current-1) should warn, not pass-as-current.
        _make_store(store_dir / "store.sqlite",
                    schema_version=CURRENT_SCHEMA_VERSION - 1)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                ["[features]", "memories = false", "", "[memories]",
                 "use_memories = false", "generate_memories = false"]
            ),
        )
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project),
        )
        report = json.loads(result.stdout)
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        # Older-than-current must be a warning (writable migration path), not pass.
        self.assertEqual(schema["status"], "warn", schema)

    def test_future_schema_version_fails_doctor(self):
        """A store NEWER than the checkout's expected version must FAIL — the
        checkout is too old to safely read it."""
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite",
                    schema_version=CURRENT_SCHEMA_VERSION + 1)
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _write_text(
            self.home / ".codex" / "config.toml",
            "\n".join(
                ["[features]", "memories = false", "", "[memories]",
                 "use_memories = false", "generate_memories = false"]
            ),
        )
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project),
        )
        report = json.loads(result.stdout)
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        self.assertEqual(schema["status"], "fail", schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
