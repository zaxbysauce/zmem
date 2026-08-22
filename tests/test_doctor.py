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


def _make_store(path: Path, schema_version: int = CURRENT_SCHEMA_VERSION) -> None:
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

    # ------------------------------------------------------------------
    # E8 (#39): pending namespace-migration preview in doctor
    # ------------------------------------------------------------------
    def _make_store_with_rows(self, store_path: Path, rows: list[tuple[str, str]],
                              schema_version: int = CURRENT_SCHEMA_VERSION) -> None:
        """Create a minimal store with a meta + memory table populated with
        (namespace, content) rows. Used by the ns-migration preview tests."""
        store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(schema_version),),
            )
            conn.execute(
                "CREATE TABLE memory(id TEXT PRIMARY KEY, namespace TEXT, "
                "content TEXT, superseded_at TEXT)"
            )
            for i, (ns, content) in enumerate(rows):
                conn.execute(
                    "INSERT INTO memory(id, namespace, content, superseded_at) "
                    "VALUES (?, ?, ?, NULL)",
                    (f"row-{i}", ns, content),
                )
            conn.commit()
        finally:
            conn.close()

    def _disable_native_memory(self):
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

    def test_ns_migration_pass_when_no_map_configured(self):
        """No ZMEM_NS_MIGRATION_MAP -> the self-heal is inactive; doctor passes."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_store_with_rows(
            store_dir / "store.sqlite",
            [("project:foo", "some content")],
        )
        env = self._base_env()
        env.pop("ZMEM_NS_MIGRATION_MAP", None)
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project), env=env,
        )
        report = json.loads(result.stdout)
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "pass", nsm)

    def test_ns_migration_pass_when_map_set_but_no_stranded_rows(self):
        """Map configured but no rows carry old-style keys -> pass."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_store_with_rows(
            store_dir / "store.sqlite",
            [("project:github.com/Example/Widget", "content")],  # already re-keyed
        )
        env = self._base_env()
        env["ZMEM_NS_MIGRATION_MAP"] = json.dumps(
            {"project:oldwidget": str(self.project)}
        )
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project), env=env,
        )
        report = json.loads(result.stdout)
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "pass", nsm)

    def test_ns_migration_warn_when_stranded_rows_present(self):
        """Rows still carrying an old-style namespace key -> warn with count."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_store_with_rows(
            store_dir / "store.sqlite",
            [
                ("project:oldwidget", "content one"),
                ("project:oldwidget", "content two"),  # same old ns, 2 rows
                ("user:global", "unrelated"),          # not in the map
            ],
        )
        env = self._base_env()
        env["ZMEM_NS_MIGRATION_MAP"] = json.dumps(
            {"project:oldwidget": str(self.project)}
        )
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project), env=env,
        )
        report = json.loads(result.stdout)
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "warn", nsm)
        self.assertEqual(nsm["details"].get("stranded_count"), 1,
                         "count is DISTINCT namespaces, so 2 rows under one "
                         "old-style key count as 1")
        self.assertIn("oldwidget", nsm["summary"])

    def test_ns_migration_invalid_json_does_not_crash(self):
        """Invalid ZMEM_NS_MIGRATION_MAP JSON -> treated as unconfigured (pass),
        never a crash."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_store_with_rows(
            store_dir / "store.sqlite",
            [("project:foo", "content")],
        )
        env = self._base_env()
        env["ZMEM_NS_MIGRATION_MAP"] = "{not valid json"
        result = self._run(
            "--format", "json", "--repo-root", str(self.repo),
            "--project", str(self.project), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "pass", nsm)


class DoctorIssue49ChecksTest(unittest.TestCase):
    """The issue #49 C checks: Tier-0 size (core.md / project AGENTS.md) and
    Claude Code transcript retention (cleanupPeriodDays). Same isolation
    contract as DoctorCliTest: temp HOME/config/tooling, read-only doctor."""

    def setUp(self):
        if not REAL_GIT:
            self.skipTest("git is required for the namespace fixture")
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor49-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.project = self.tmp / "project"
        self.bin = self.tmp / "bin"
        for d in (self.home, self.repo, self.project, self.bin):
            d.mkdir()
        # Minimal surfaces so doctor can run to completion; only the two new
        # checks' statuses are asserted.
        _write_text(self.repo / ".claude-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.claude.json", "{}\n")
        _write_text(self.repo / ".zcode-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.zcode.json", "{}\n")
        _write_text(self.repo / "skills" / "memory" / "SKILL.md", "# memory\n")
        node = self.bin / "node.cmd"
        _write_text(node, _cmd_script("echo v20.11.0"))
        subprocess.run([REAL_GIT, "init", "-q"], cwd=str(self.project), check=True)
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin",
             "https://github.com/Example/Widget.git"],
            cwd=str(self.project), check=True,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict:
        env = {**os.environ}
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["ZMEM_BASH_PATH"] = str(self.bin / "Git" / "bin" / "bash.cmd")
        for key in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_CORE_MD",
            "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            "OneDrive", "OneDriveConsumer", "OneDriveCommercial",
        ):
            env.pop(key, None)
        return env

    def _run_doctor(self):
        result = subprocess.run(
            [PYTHON, str(DOCTOR_PY), "--format", "json",
             "--repo-root", str(self.repo), "--project", str(self.project)],
            env=self._env(), capture_output=True, text=True, timeout=60,
        )
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result, json.loads(result.stdout)

    def _check(self, report, check_id):
        return next(c for c in report["checks"] if c["id"] == check_id)

    # --- tier0-size ---------------------------------------------------------

    def test_tier0_absent_reports_skip(self):
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "skip", check)
        self.assertIn("No Tier-0", check["summary"])

    def test_tier0_small_core_md_passes_with_stats(self):
        _write_text(self.home / ".zmem" / "core.md",
                    "\n".join(f"line {i}" for i in range(10)) + "\n")
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(len(check["details"]["files"]), 1)
        stats = check["details"]["files"][0]
        self.assertEqual(stats["lines"], 10)
        self.assertGreater(stats["bytes"], 0)

    def test_tier0_300_line_core_md_warns(self):
        _write_text(self.home / ".zmem" / "core.md",
                    "\n".join(f"rule {i}" for i in range(300)) + "\n")
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("exceed", check["summary"])
        self.assertIn("store", check["summary"])  # remediation names the store

    def test_tier0_oversized_agents_md_alone_warns(self):
        _write_text(self.home / ".zmem" / "core.md", "small\n")
        _write_text(self.project / "AGENTS.md",
                    "\n".join(f"agent rule {i}" for i in range(250)) + "\n")
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "warn", check)
        paths = [f["path"] for f in check["details"]["files"]]
        self.assertEqual(len(paths), 2)  # core.md AND AGENTS.md measured

    def test_tier0_bytes_threshold_independent_of_lines(self):
        # 60 lines but > 16KB (each line ~300 bytes): byte cap must trip alone.
        _write_text(self.home / ".zmem" / "core.md",
                    "\n".join("x" * 300 for _ in range(60)) + "\n")
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "warn", check)

    # --- session-retention --------------------------------------------------

    def test_retention_no_claude_dir_reports_not_applicable(self):
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "skip", check)
        self.assertIn("not applicable", check["summary"])
        self.assertIn("no Claude Code installation", check["summary"])

    def test_retention_settings_absent_passes_with_default_note(self):
        (self.home / ".claude").mkdir()
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])
        self.assertEqual(check["details"]["default"], 30)
        self.assertIn("cleanupPeriodDays", check["summary"])

    def test_retention_malformed_settings_passes(self):
        _write_text(self.home / ".claude" / "settings.json", "{not valid json")
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])

    def test_retention_unset_key_passes(self):
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"autoMemoryEnabled": False}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])

    def test_retention_thirty_days_is_pass_default_like(self):
        # 30 is the CC default: info-shaped pass, never a warn.
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": 30}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertTrue(check["details"]["configured"])
        self.assertEqual(check["details"]["cleanup_period_days"], 30)

    def test_retention_large_value_passes_with_retains_summary(self):
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": 99999}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertIn("retains transcripts for 99999", check["summary"])

    def test_retention_bool_and_nonpositive_count_as_unset(self):
        """PR feedback PRR-019/PRR-027: booleans and non-positive ints read as
        unset (default-30 note), never echoed as valid configuration."""
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": True}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])

        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": -5}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])
        self.assertNotIn("-5", check["summary"])

    def test_retention_local_settings_override_shared(self):
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": 30}))
        _write_text(self.home / ".claude" / "settings.local.json",
                    json.dumps({"cleanupPeriodDays": 365}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["cleanup_period_days"], 365)
        self.assertTrue(check["details"]["configured"])

    def test_retention_invalid_local_does_not_clobber_shared(self):
        """Feedback-reviewer finding: an INVALID local override (e.g. -5) must
        fail to override — it must not silently discard a valid shared value."""
        _write_text(self.home / ".claude" / "settings.json",
                    json.dumps({"cleanupPeriodDays": 60}))
        _write_text(self.home / ".claude" / "settings.local.json",
                    json.dumps({"cleanupPeriodDays": -5}))
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertTrue(check["details"]["configured"])
        self.assertEqual(check["details"]["cleanup_period_days"], 60)

    def test_new_checks_never_contribute_a_fail(self):
        # Retention is informational and tier0 warns at most: neither may add
        # a fail (warn/skip/pass only), whatever the fixture.
        _write_text(self.home / ".zmem" / "core.md",
                    "\n".join(f"rule {i}" for i in range(300)) + "\n")
        (self.home / ".claude").mkdir()
        _, report = self._run_doctor()
        self.assertEqual(self._check(report, "tier0-size")["status"], "warn")
        self.assertEqual(self._check(report, "session-retention")["status"], "pass")
        statuses = {c["status"] for c in report["checks"]
                    if c["id"] in ("tier0-size", "session-retention")}
        self.assertNotIn("fail", statuses)


class DoctorUnitFailOpenTest(unittest.TestCase):
    """Import-level fail-open tests that cannot be driven through the CLI
    subprocess (PR feedback PRR-027)."""

    def test_tier0_size_survives_resolver_raise(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402

        with mock.patch.object(doctor.host, "resolve_core_md_path",
                               side_effect=RuntimeError("hostile store env")):
            check = doctor._check_tier0_size(Path("/nonexistent-project"))
        # The unresolvable core.md simply is not measured — doctor never
        # tracebacks on a hostile store env.
        self.assertEqual(check["status"], "skip", check)


class PythonFloorTest(unittest.TestCase):
    """Issue #56 / 1.6: the supported Python floor is 3.11 (CI and the Hermes
    lane both run 3.11). Doctor must WARN below the floor — not fail (a
    3.8–3.10 interpreter still runs most of the store; the floor is a support
    contract, not a hard ABI gate) and not silently pass (the old behavior on
    3.8–3.10)."""

    def test_below_floor_warns_and_names_the_floor(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402

        with mock.patch("sys.version_info", (3, 10, 9, "final", 0)):
            check = doctor._check_python()
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("3.11", check["summary"], check)

    def test_at_or_above_floor_passes(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402

        if sys.version_info < (3, 11):
            self.skipTest("interpreter is below the 3.11 floor")
        check = doctor._check_python()
        self.assertEqual(check["status"], "pass", check)


if __name__ == "__main__":
    unittest.main(verbosity=2)
