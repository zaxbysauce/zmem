"""Tests for skills/memory/scripts/doctor.py.

The doctor command must remain read-only: these tests run it only against temp
HOME/config/tooling fixtures and temp sqlite stores. They never touch the real
user config or the real shared store.

Run: python tests/test_doctor.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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
            [
                REAL_GIT,
                "remote",
                "add",
                "origin",
                "https://github.com/Example/Widget.git",
            ],
            cwd=str(self.project),
            check=True,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fake_tools(self):
        node = self.bin / "node.cmd"
        git = self.bin / "git.cmd"
        bash = self.bin / "Git" / "bin" / "bash.cmd"
        _write_text(node, _cmd_script("echo v20.11.0"))
        _write_text(git, _cmd_script("echo https://github.com/Example/Widget.git"))
        _write_text(bash, _cmd_script("echo GNU bash, version 5.2.0"))

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
        _write_text(self.repo / ".codex-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.codex.json", "{}\n")
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

    def test_missing_required_host_surface_fails_per_group(self):
        """F-003 (pr-review #142): every required-surface group's negative
        path must be pinned. Dropping an entry from doctor's required dict
        (e.g. the codex_plugin guard this PR adds for issue #108) must fail
        the suite, not pass silently."""
        groups = {
            "claude_plugin": [".claude-plugin/plugin.json", "hooks/hooks.claude.json"],
            "codex_plugin": [".codex-plugin/plugin.json", "hooks/hooks.codex.json"],
            "zcode_plugin": [".zcode-plugin/plugin.json", "hooks/hooks.zcode.json"],
            "memory_skill": ["skills/memory/SKILL.md"],
        }
        for group, rels in groups.items():
            with self.subTest(group=group):
                for rel in rels:
                    (self.repo / rel).unlink()
                try:
                    result = self._run(
                        "--format",
                        "json",
                        "--repo-root",
                        str(self.repo),
                        "--project",
                        str(self.project),
                    )
                finally:
                    self._write_repo_surfaces()
                self.assertNotEqual(
                    result.returncode, 0, "missing %s must fail doctor" % group
                )
                report = json.loads(result.stdout)
                self.assertFalse(report["ok"], group)
                check = next(c for c in report["checks"] if c["id"] == "host-surfaces")
                self.assertEqual(check["status"], "fail", group)
                self.assertIn(group, check["summary"])

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
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        self.assertEqual(schema["status"], "pass", schema)
        self.assertEqual(schema["details"]["actual"], CURRENT_SCHEMA_VERSION)

    def test_stale_older_schema_version_warns_not_fails(self):
        """A store at an OLDER schema version than current must WARN (migrate
        needed), not FAIL — and a store at a NEWER version must FAIL."""
        store_dir = self.home / ".zmem"
        # An older store (current-1) should warn, not pass-as-current.
        _make_store(
            store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION - 1
        )
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
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        # Older-than-current must be a warning (writable migration path), not pass.
        self.assertEqual(schema["status"], "warn", schema)

    def test_future_schema_version_fails_doctor(self):
        """A store NEWER than the forward-compat ceiling must FAIL — the
        checkout cannot safely operate on it (update, or the explicit
        ZMEM_ALLOW_NEWER_SCHEMA override). The band BETWEEN the checkout's
        supported version and the ceiling is covered by the in-process
        doctor grading tests in tests/test_schema_forward_compat.py."""
        store_dir = self.home / ".zmem"
        _make_store(
            store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION + 1
        )
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
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        schema = next(c for c in report["checks"] if c["id"] == "schema-version")
        self.assertEqual(schema["status"], "fail", schema)

    # ------------------------------------------------------------------
    # v11 (issue #61, 6.1): the link-tables check
    # ------------------------------------------------------------------
    def _make_v11_link_store(
        self, store_path: Path, *, with_link_table: bool, trust_values=(1.0,)
    ) -> None:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            conn.execute(
                "CREATE TABLE memory(id TEXT PRIMARY KEY, trust_score REAL "
                "NOT NULL DEFAULT 1.0)"
            )
            for i, t in enumerate(trust_values):
                conn.execute(
                    "INSERT INTO memory(id, trust_score) VALUES (?, ?)", (f"row-{i}", t)
                )
            if with_link_table:
                conn.execute(
                    "CREATE TABLE memory_link(src_id TEXT, dst_id TEXT, "
                    "relation TEXT, score REAL, created_at TEXT)"
                )
            conn.commit()
        finally:
            conn.close()

    def test_link_tables_check_passes_on_healthy_v11_store(self):
        store_dir = self.home / ".zmem"
        self._make_v11_link_store(store_dir / "store.sqlite", with_link_table=True)
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        check = next(c for c in report["checks"] if c["id"] == "link-tables")
        self.assertEqual(check["status"], "pass", check)
        self.assertIn("memory_link table present", check["summary"])

    def test_link_tables_check_warns_when_table_missing(self):
        store_dir = self.home / ".zmem"
        self._make_v11_link_store(store_dir / "store.sqlite", with_link_table=False)
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        check = next(c for c in report["checks"] if c["id"] == "link-tables")
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("memory_link table missing", check["summary"])

    def test_link_tables_check_warns_on_out_of_range_trust(self):
        """adjust_trust clamps in SQL, so an out-of-range value means a
        hand-edited store — doctor warns (read-only, never repairs)."""
        store_dir = self.home / ".zmem"
        self._make_v11_link_store(
            store_dir / "store.sqlite", with_link_table=True, trust_values=(1.0, 1.7)
        )
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        check = next(c for c in report["checks"] if c["id"] == "link-tables")
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("outside [0.0, 1.0]", check["summary"])

    # ------------------------------------------------------------------
    # v12 (issue #64): the voyager-counters check
    # ------------------------------------------------------------------
    def _make_v12_counter_store(
        self, store_path: Path, counters: list[tuple[int, int]]
    ) -> None:
        """Minimal v12-tagged store whose memory table carries ONLY the
        columns the voyager-counters check reads."""
        store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            conn.execute(
                "CREATE TABLE memory(id TEXT PRIMARY KEY, "
                "applied_count INTEGER NOT NULL DEFAULT 0, "
                "violated_count INTEGER NOT NULL DEFAULT 0)"
            )
            for i, (applied, violated) in enumerate(counters):
                conn.execute(
                    "INSERT INTO memory(id, applied_count, violated_count) "
                    "VALUES (?, ?, ?)",
                    (f"row-{i}", applied, violated),
                )
            conn.commit()
        finally:
            conn.close()

    def _voyager_check(self):
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        report = json.loads(result.stdout)
        check = next(c for c in report["checks"] if c["id"] == "voyager-counters")
        return result, check

    def test_voyager_counters_pass_on_healthy_v12_store(self):
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_v12_counter_store(
            store_dir / "store.sqlite", counters=[(0, 0), (3, 1), (2, 0)]
        )
        result, check = self._voyager_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["applied_max"], 3, check)
        self.assertEqual(check["details"]["violated_max"], 1, check)

    def test_voyager_counters_warn_on_store_missing_columns_without_failing(self):
        """A store whose memory table lacks the columns (the minimal shape the
        next writable run creates/migrates) must WARN, never fail the
        report — doctor recovers, it does not gate."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        _make_store(store_dir / "store.sqlite", schema_version=CURRENT_SCHEMA_VERSION)
        result, check = self._voyager_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("issue #64", check["summary"])

    def test_voyager_counters_warn_on_negative_counter(self):
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        self._make_v12_counter_store(
            store_dir / "store.sqlite", counters=[(0, 0), (1, -2)]
        )
        result, check = self._voyager_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("negative", check["summary"])

    def test_voyager_counters_warn_not_crash_on_non_integer_counter(self):
        """SQLite dynamic typing allows TEXT in an INTEGER column via manual
        SQL; the check must degrade to warn, never raise TypeError mid-run
        (doctor never crashes on a hand-edited store)."""
        self._disable_native_memory()
        store_dir = self.home / ".zmem"
        store = store_dir / "store.sqlite"
        self._make_v12_counter_store(store, counters=[(0, 0)])
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "INSERT INTO memory(id, applied_count, "
                "violated_count) VALUES ('row-x', 'many', 0)"
            )
            conn.commit()
        finally:
            conn.close()
        result, check = self._voyager_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("non-integer", check["summary"])

    # ------------------------------------------------------------------
    # E8 (#39): pending namespace-migration preview in doctor
    # ------------------------------------------------------------------
    def _make_store_with_rows(
        self,
        store_path: Path,
        rows: list[tuple[str, str]],
        schema_version: int = CURRENT_SCHEMA_VERSION,
    ) -> None:
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
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
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
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
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
                ("user:global", "unrelated"),  # not in the map
            ],
        )
        env = self._base_env()
        env["ZMEM_NS_MIGRATION_MAP"] = json.dumps(
            {"project:oldwidget": str(self.project)}
        )
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
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "warn", nsm)
        self.assertEqual(
            nsm["details"].get("stranded_count"),
            1,
            "count is DISTINCT namespaces, so 2 rows under one "
            "old-style key count as 1",
        )
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
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        nsm = next(c for c in report["checks"] if c["id"] == "ns-migration")
        self.assertEqual(nsm["status"], "pass", nsm)

    def test_miss_min_overlap_passes_through_to_false_injection(self):
        """Issue #129: --miss-min-overlap reaches the join — the report's
        false_injection.min_token_overlap equals the flag value, and the
        miss-rate summary prints BOTH directions (miss + false-injection)."""
        self._disable_native_memory()
        # A snapshot store (NOT the host default) with the tables the join
        # probes (memory + memory_fts), plus a #129 decisions log carrying
        # one injected decision line for the counter's denominator.
        snap = self.tmp / "snapshot"
        store = snap / "store.sqlite"
        self._make_store_with_rows(store, [("project:fi", "git stash pop")])
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("CREATE TABLE memory_fts(memory_rowid TEXT)")
            conn.commit()
        finally:
            conn.close()
        _write_text(
            snap / "zmem-decisions.log",
            "[1740000000] zmem-hook status=injected reason=injected "
            "ids=['row-0'] all=['row-0'] sid=sess-fi moment=user_prompt\n",
        )
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            "--store",
            str(store),
            "--miss-rate",
            "--miss-min-overlap",
            "3",
        )
        report = json.loads(result.stdout)
        check = next(c for c in report["checks"] if c["id"] == "miss-rate")
        fi = check["details"]["report"]["false_injection"]
        self.assertEqual(
            fi["min_token_overlap"],
            3,
            "the flag value must pass through to the counter",
        )
        self.assertEqual(
            fi["overall"]["injected"], 1, "the counter consumed the decisions-log line"
        )
        self.assertIn(
            "missed", check["summary"], "the summary keeps the miss direction"
        )
        self.assertIn(
            "false-injection",
            check["summary"],
            "the summary prints the false-injection direction "
            "alongside the miss rate",
        )
        self.assertIn("min_overlap 3", check["summary"])


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
        _write_text(self.repo / ".codex-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.codex.json", "{}\n")
        _write_text(self.repo / ".zcode-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.zcode.json", "{}\n")
        _write_text(self.repo / "skills" / "memory" / "SKILL.md", "# memory\n")
        node = self.bin / "node.cmd"
        _write_text(node, _cmd_script("echo v20.11.0"))
        subprocess.run([REAL_GIT, "init", "-q"], cwd=str(self.project), check=True)
        subprocess.run(
            [
                REAL_GIT,
                "remote",
                "add",
                "origin",
                "https://github.com/Example/Widget.git",
            ],
            cwd=str(self.project),
            check=True,
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
            "ZMEM_STORE",
            "ZMEM_DATA",
            "ZMEM_CORE_MD",
            "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA",
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            # issue #63 review round: the new embedding/CE knobs must not
            # leak from a developer shell into doctor's sandbox (PRR-013)
            "ZMEM_EMBED_PROFILE",
            "ZMEM_CROSS_ENCODER",
            "ZMEM_CROSS_ENCODER_MODEL",
            "ZMEM_MODELS_DIR",
            "OneDrive",
            "OneDriveConsumer",
            "OneDriveCommercial",
        ):
            env.pop(key, None)
        return env

    def _run_doctor(self):
        result = subprocess.run(
            [
                PYTHON,
                str(DOCTOR_PY),
                "--format",
                "json",
                "--repo-root",
                str(self.repo),
                "--project",
                str(self.project),
            ],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        if not result.stdout.strip():
            self.fail(
                "doctor wrote no JSON on stdout "
                f"(rc={result.returncode}) stderr={result.stderr[-400:]!r} "
                "— a crash here previously masked itself as a confusing "
                "JSONDecodeError (PRR-014 courtesy note)"
            )
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
        _write_text(
            self.home / ".zmem" / "core.md",
            "\n".join(f"line {i}" for i in range(10)) + "\n",
        )
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(len(check["details"]["files"]), 1)
        stats = check["details"]["files"][0]
        self.assertEqual(stats["lines"], 10)
        self.assertGreater(stats["bytes"], 0)

    def test_tier0_300_line_core_md_warns(self):
        _write_text(
            self.home / ".zmem" / "core.md",
            "\n".join(f"rule {i}" for i in range(300)) + "\n",
        )
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("exceed", check["summary"])
        self.assertIn("store", check["summary"])  # remediation names the store

    def test_tier0_oversized_agents_md_alone_warns(self):
        _write_text(self.home / ".zmem" / "core.md", "small\n")
        _write_text(
            self.project / "AGENTS.md",
            "\n".join(f"agent rule {i}" for i in range(250)) + "\n",
        )
        _, report = self._run_doctor()
        check = self._check(report, "tier0-size")
        self.assertEqual(check["status"], "warn", check)
        paths = [f["path"] for f in check["details"]["files"]]
        self.assertEqual(len(paths), 2)  # core.md AND AGENTS.md measured

    def test_tier0_bytes_threshold_independent_of_lines(self):
        # 60 lines but > 16KB (each line ~300 bytes): byte cap must trip alone.
        _write_text(
            self.home / ".zmem" / "core.md",
            "\n".join("x" * 300 for _ in range(60)) + "\n",
        )
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
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"autoMemoryEnabled": False}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])

    def test_retention_thirty_days_is_pass_default_like(self):
        # 30 is the CC default: info-shaped pass, never a warn.
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": 30}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertTrue(check["details"]["configured"])
        self.assertEqual(check["details"]["cleanup_period_days"], 30)

    def test_retention_large_value_passes_with_retains_summary(self):
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": 99999}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertIn("retains transcripts for 99999", check["summary"])

    def test_retention_bool_and_nonpositive_count_as_unset(self):
        """PR feedback PRR-019/PRR-027: booleans and non-positive ints read as
        unset (default-30 note), never echoed as valid configuration."""
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": True}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])

        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": -5}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertFalse(check["details"]["configured"])
        self.assertNotIn("-5", check["summary"])

    def test_retention_local_settings_override_shared(self):
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": 30}),
        )
        _write_text(
            self.home / ".claude" / "settings.local.json",
            json.dumps({"cleanupPeriodDays": 365}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["cleanup_period_days"], 365)
        self.assertTrue(check["details"]["configured"])

    def test_retention_invalid_local_does_not_clobber_shared(self):
        """Feedback-reviewer finding: an INVALID local override (e.g. -5) must
        fail to override — it must not silently discard a valid shared value."""
        _write_text(
            self.home / ".claude" / "settings.json",
            json.dumps({"cleanupPeriodDays": 60}),
        )
        _write_text(
            self.home / ".claude" / "settings.local.json",
            json.dumps({"cleanupPeriodDays": -5}),
        )
        _, report = self._run_doctor()
        check = self._check(report, "session-retention")
        self.assertEqual(check["status"], "pass", check)
        self.assertTrue(check["details"]["configured"])
        self.assertEqual(check["details"]["cleanup_period_days"], 60)

    def test_new_checks_never_contribute_a_fail(self):
        # Retention is informational and tier0 warns at most: neither may add
        # a fail (warn/skip/pass only), whatever the fixture.
        _write_text(
            self.home / ".zmem" / "core.md",
            "\n".join(f"rule {i}" for i in range(300)) + "\n",
        )
        (self.home / ".claude").mkdir()
        _, report = self._run_doctor()
        self.assertEqual(self._check(report, "tier0-size")["status"], "warn")
        self.assertEqual(self._check(report, "session-retention")["status"], "pass")
        statuses = {
            c["status"]
            for c in report["checks"]
            if c["id"] in ("tier0-size", "session-retention")
        }
        self.assertNotIn("fail", statuses)


class V13DoctorChecksTest(DoctorIssue49ChecksTest):
    """v13 (issue #65, 10.7/10.10): episode-tables + mcp-token checks.

    Inherits the Issue49 fixture (temp HOME/config, read-only doctor); only
    the two new checks' statuses are asserted.
    """

    def _run_json(self, env):
        result = subprocess.run(
            [
                PYTHON,
                str(DOCTOR_PY),
                "--format",
                "json",
                "--repo-root",
                str(self.repo),
                "--project",
                str(self.project),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # C39: same PRR-014 guard as _run_doctor — a doctor crash must
        # surface as its traceback here, not as a confusing
        # JSONDecodeError on empty stdout in the caller.
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertTrue(
            result.stdout.strip(),
            f"doctor wrote no JSON (rc={result.returncode}) "
            f"stderr={result.stderr[-400:]!r}",
        )
        return result

    @staticmethod
    def _check(report, check_id):
        return next(c for c in report["checks"] if c["id"] == check_id)

    def test_mcp_token_unconfigured_is_skip(self):
        env = self._env()
        env.pop("ZMEM_MCP_TOKEN", None)
        env.pop("ZMEM_MCP_TOKEN_FILE", None)
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "skip", check)

    def test_mcp_token_env_is_unscoped_warn(self):
        env = self._env()
        env["ZMEM_MCP_TOKEN"] = "doctor-test-secret"
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "warn", check)
        self.assertIs(check["details"]["unscoped_token"], True, check)
        # The token value never appears anywhere in the report.
        self.assertNotIn("doctor-test-secret", json.dumps(report))

    def test_mcp_token_scoped_json_is_pass(self):
        tok = self.tmp / "scoped-token.json"
        _write_text(
            tok,
            json.dumps(
                {"token": "doctor-scoped-secret", "namespaces": ["project:zmem"]}
            ),
        )
        env = self._env()
        env.pop("ZMEM_MCP_TOKEN", None)
        env["ZMEM_MCP_TOKEN_FILE"] = str(tok)
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "pass", check)
        self.assertIs(check["details"]["unscoped_token"], False, check)
        self.assertEqual(check["details"]["namespaces"], 1, check)
        self.assertIs(check["details"]["reads_require_namespace"], True, check)
        self.assertNotIn("doctor-scoped-secret", json.dumps(report))

    def test_mcp_token_malformed_json_is_fail(self):
        tok = self.tmp / "bad-token.json"
        _write_text(tok, '{"token": "x", ')
        env = self._env()
        env.pop("ZMEM_MCP_TOKEN", None)
        env["ZMEM_MCP_TOKEN_FILE"] = str(tok)
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "fail", check)

    def test_mcp_token_json_without_namespaces_is_unscoped_warn(self):
        # auth.py treats absent/null 'namespaces' as a VALID unscoped
        # operator token (final-critic B1): doctor must WARN, not fail.
        tok = self.tmp / "unscoped-token.json"
        _write_text(tok, json.dumps({"token": "json-unscoped-secret"}))
        env = self._env()
        env.pop("ZMEM_MCP_TOKEN", None)
        env["ZMEM_MCP_TOKEN_FILE"] = str(tok)
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "warn", check)
        self.assertIs(check["details"]["unscoped_token"], True, check)
        self.assertNotIn("json-unscoped-secret", json.dumps(report))

    def test_mcp_token_json_null_namespaces_is_unscoped_warn(self):
        tok = self.tmp / "null-ns-token.json"
        _write_text(tok, json.dumps({"token": "null-ns-secret", "namespaces": None}))
        env = self._env()
        env.pop("ZMEM_MCP_TOKEN", None)
        env["ZMEM_MCP_TOKEN_FILE"] = str(tok)
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "mcp-token")
        self.assertEqual(check["status"], "warn", check)
        self.assertIs(check["details"]["unscoped_token"], True, check)

    def test_episode_tables_absent_store_is_skip(self):
        env = self._env()
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "episode-tables")
        self.assertEqual(check["status"], "skip", check)

    def test_episode_tables_current_store_is_pass(self):
        store = self.home / ".zmem" / "store.sqlite"
        _make_store(store, schema_version=CURRENT_SCHEMA_VERSION)
        # _make_store builds only the meta table; add the v13 tables so the
        # fixture matches a real current store.
        conn = sqlite3.connect(str(store))
        try:
            conn.executescript(
                "CREATE TABLE episode (id TEXT PRIMARY KEY, namespace TEXT NOT NULL,"
                " started_at TEXT NOT NULL, ended_at TEXT NOT NULL DEFAULT '',"
                " summary_memory_id TEXT NOT NULL DEFAULT '',"
                " token_count INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE episode_memory (episode_id TEXT NOT NULL,"
                " memory_id TEXT NOT NULL, added_at TEXT NOT NULL DEFAULT '',"
                " PRIMARY KEY (episode_id, memory_id));"
            )
            conn.commit()
        finally:
            conn.close()
        env = self._env()
        report = json.loads(self._run_json(env).stdout)
        check = self._check(report, "episode-tables")
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["memberships"], 0, check)


class DoctorUnitFailOpenTest(unittest.TestCase):
    """Import-level fail-open tests that cannot be driven through the CLI
    subprocess (PR feedback PRR-027)."""

    def test_tier0_size_survives_resolver_raise(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402

        with mock.patch.object(
            doctor.host,
            "resolve_core_md_path",
            side_effect=RuntimeError("hostile store env"),
        ):
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

        # Patch BOTH version surfaces: _check_python reads sys.version_info
        # for the threshold and sys.version.split()[0] for the message.
        # Patching only version_info left the REAL interpreter version in the
        # message ("Python 3.11.9 is below the supported floor") — a
        # self-contradictory line, and assertIn("3.11") then passed via the
        # leaked version instead of the floor being named (PRR-004).
        fake_version = "3.10.9 (tags/v3.10.9:b694321) [MSC v.1929 64 bit (AMD64)]"
        with mock.patch("sys.version_info", (3, 10, 9, "final", 0)), mock.patch(
            "sys.version", fake_version
        ):
            check = doctor._check_python()
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("3.10.9", check["summary"], check)
        self.assertIn("3.11", check["summary"], check)

    def test_at_or_above_floor_passes(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402

        if sys.version_info < (3, 11):
            self.skipTest("interpreter is below the 3.11 floor")
        check = doctor._check_python()
        self.assertEqual(check["status"], "pass", check)


class TrainingDependencyCheckTest(unittest.TestCase):
    """Issue #135: doctor gives a precise, warning-only PyArrow remediation."""

    @classmethod
    def setUpClass(cls):
        cls.scripts = REPO_ROOT / "skills" / "memory" / "scripts"
        sys.path.insert(0, str(cls.scripts))

    def test_missing_pyarrow_names_declared_install_command(self):
        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402

        with mock.patch.object(doctor.importlib.util, "find_spec", return_value=None):
            check = doctor._check_training_dependency()
        self.assertEqual(check["status"], "warn", check)
        self.assertIn("PyArrow is unavailable", check["summary"], check)
        command = check["details"]["install_command"]
        self.assertIn("-r", command)
        self.assertIn("requirements-training.txt", command)
        self.assertIn(command, " ".join(doctor._recommendations([check])))

    def test_available_pyarrow_passes(self):
        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402

        with mock.patch.object(doctor.importlib.util, "find_spec", return_value=object()):
            check = doctor._check_training_dependency()
        self.assertEqual(check["status"], "pass", check)

    def test_missing_capture_tables_warn_without_creating_them(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-training-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "store.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.commit()
        conn.close()
        before = path.read_bytes()

        check = doctor._check_training_capture_health(path)

        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(set(check["details"]["missing_tables"]), {
            "training_capture",
            "training_delivery_snapshot",
            "training_capture_completion",
            "training_capture_observation",
        })
        self.assertEqual(path.read_bytes(), before)

    def test_healthy_capture_tables_pass(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-training-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "store.sqlite"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE training_capture(
                capture_id TEXT PRIMARY KEY, state TEXT,
                redaction_status TEXT, redaction_policy_version TEXT,
                acknowledged_at TEXT, revoked_at TEXT
            );
            CREATE TABLE training_delivery_snapshot(
                delivery_snapshot_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE training_capture_completion(
                capture_id TEXT PRIMARY KEY, evidence_id TEXT,
                associated_memory_ids_json TEXT
            );
            CREATE TABLE training_capture_observation(
                observation_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE memory_evidence(memory_id TEXT, evidence_id TEXT);
            """
        )
        conn.commit()
        conn.close()

        check = doctor._check_training_capture_health(path)

        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["issues"], {})

    def test_metadata_only_partial_without_policy_version_passes(self):
        """Default-deny metadata-only partials do not need a policy version."""
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-metadata-only-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "store.sqlite"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE training_capture(
                capture_id TEXT PRIMARY KEY, state TEXT,
                redaction_status TEXT, redaction_policy_version TEXT,
                acknowledged_at TEXT, revoked_at TEXT
            );
            CREATE TABLE training_delivery_snapshot(
                delivery_snapshot_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE training_capture_completion(
                capture_id TEXT PRIMARY KEY, evidence_id TEXT,
                associated_memory_ids_json TEXT
            );
            CREATE TABLE training_capture_observation(
                observation_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE memory_evidence(memory_id TEXT, evidence_id TEXT);
            INSERT INTO training_capture(
                capture_id, state, redaction_status, redaction_policy_version
            ) VALUES ('capture-denied', 'partial', 'metadata_only', NULL);
            """
        )
        conn.commit()
        conn.close()

        check = doctor._check_training_capture_health(path)

        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["issues"], {})
        self.assertEqual(check["details"]["revoked_records"], 0)

    def test_revoked_capture_is_informational(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-revoked-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "store.sqlite"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE training_capture(
                capture_id TEXT PRIMARY KEY, state TEXT,
                redaction_status TEXT, redaction_policy_version TEXT,
                acknowledged_at TEXT, revoked_at TEXT
            );
            CREATE TABLE training_delivery_snapshot(
                delivery_snapshot_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE training_capture_completion(
                capture_id TEXT PRIMARY KEY, evidence_id TEXT,
                associated_memory_ids_json TEXT
            );
            CREATE TABLE training_capture_observation(
                observation_id TEXT PRIMARY KEY, capture_id TEXT
            );
            CREATE TABLE memory_evidence(memory_id TEXT, evidence_id TEXT);
            INSERT INTO training_capture(
                capture_id, state, redaction_status, redaction_policy_version,
                revoked_at
            ) VALUES ('capture-revoked', 'partial', 'metadata_only', NULL,
                      '2026-09-26T12:00:00Z');
            """
        )
        conn.commit()
        conn.close()

        check = doctor._check_training_capture_health(path)

        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["details"]["issues"], {})
        self.assertEqual(check["details"]["revoked_records"], 1)
        self.assertIn("revoked", check["summary"])

    def test_abandoned_staging_directory_is_reported(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-project-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        staging = tmp / ".training-staging-test"
        staging.mkdir()

        check = doctor._check_training_staging(tmp)

        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(check["details"]["count"], 1, check)
        self.assertIn(".training-staging-test", check["details"]["paths"][0])

    def test_staging_check_uses_explicit_output_parent(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-output-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        output_parent = tmp / "nested" / "exports"
        output_parent.mkdir(parents=True)
        output = output_parent / "training"
        staging = output_parent / ".training-staging-nested"
        staging.mkdir()
        root_staging = tmp / ".training-staging-root"
        root_staging.mkdir()

        check = doctor._check_training_staging(tmp, output)

        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(check["details"]["count"], 1, check)
        self.assertIn(".training-staging-nested", check["details"]["paths"][0])
        self.assertNotIn(".training-staging-root", check["details"]["paths"])
        self.assertIn("exports", check["details"]["parent"])

    def test_explicit_cleanup_removes_only_stale_direct_candidates(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-cleanup-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        output_parent = tmp / "exports"
        output_parent.mkdir()
        output = output_parent / "training"
        stale = output_parent / ".training-staging-stale"
        stale.mkdir()
        (stale / "partial.parquet").write_bytes(b"partial")
        old = time.time() - (48 * 60 * 60)
        os.utime(stale / "partial.parquet", (old, old))
        os.utime(stale, (old, old))
        recent = output_parent / ".training-staging-recent"
        recent.mkdir()
        (recent / "partial.parquet").write_bytes(b"active")
        keep = output_parent / "keep.txt"
        keep.write_text("retain", encoding="utf-8")
        outside = tmp / ".training-staging-outside"
        outside.mkdir()

        result = doctor._cleanup_training_staging(
            tmp,
            output,
            max_age_hours=24.0,
            confirm_no_training_export=True,
        )

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(len(result["removed"]), 1, result)
        self.assertEqual(len(result["skipped_recent"]), 1, result)
        self.assertFalse(stale.exists())
        self.assertTrue(recent.is_dir())
        self.assertTrue(keep.is_file())
        self.assertTrue(outside.is_dir())

    def test_explicit_cleanup_requires_no_export_confirmation(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-confirm-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        output_parent = tmp / "exports"
        output_parent.mkdir()
        output = output_parent / "training"
        staging = output_parent / ".training-staging-confirm"
        staging.mkdir()
        old = time.time() - (48 * 60 * 60)
        os.utime(staging, (old, old))

        result = doctor._cleanup_training_staging(tmp, output, max_age_hours=24.0)

        self.assertEqual(result["status"], "warn", result)
        self.assertTrue(staging.is_dir())
        self.assertIn("confirmation", result["errors"][0])

    def test_default_staging_check_does_not_delete(self):
        import doctor  # noqa: E402

        tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor135-readonly-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        staging = tmp / ".training-staging-readonly"
        staging.mkdir()

        check = doctor._check_training_staging(tmp)

        self.assertEqual(check["status"], "warn", check)
        self.assertTrue(staging.is_dir())


class EmbeddingsHealthCheckTest(unittest.TestCase):
    """Issue #63, 8.1/8.4: the new embeddings/embeddings_health surfaces."""

    @classmethod
    def setUpClass(cls):
        cls.scripts = REPO_ROOT / "skills" / "memory" / "scripts"
        sys.path.insert(0, str(cls.scripts))

    def _report(self, env_extra):
        import json as _json
        import subprocess as _sub

        tmp = tempfile.mkdtemp(prefix="zmem-doctor63-")
        self.addCleanup(shutil.rmtree, tmp, True)
        data = Path(tmp) / "data"
        data.mkdir()
        (data / "store.sqlite").write_bytes(b"")  # empty file: doctor skips
        env = dict(os.environ)
        env["ZMEM_DATA"] = str(data)
        env.pop("ZMEM_EMBED_PROFILE", None)
        env.update(env_extra)
        # Issue #185 isolation fix: ambient store vars outrank the fixture's
        # ZMEM_DATA in host.resolve_store_path (ZMEM_STORE first, then the
        # plugin-data vars), so drop them unless this test set them
        # deliberately — otherwise the doctor subprocess resolves an
        # ambient store and live_memories reads as None.
        for var in ("ZMEM_STORE", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            if var not in env_extra:
                env.pop(var, None)
        r = _sub.run(
            [sys.executable, str(self.scripts / "doctor.py"), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(self.scripts),
            env=env,
        )
        rep = _json.loads(r.stdout) if r.stdout.strip() else {"checks": []}
        return rep

    def test_health_check_always_present(self):
        rep = self._report({})
        ids = [c["id"] for c in rep["checks"]]
        self.assertIn("embeddings", ids)
        self.assertIn("embeddings_health", ids)

    def test_details_shape_on_minimal_fixture_store(self):
        sys.path.insert(0, str(self.scripts))
        import tempfile as _tf
        import sqlite3 as _sq

        store_dir = Path(_tf.mkdtemp(prefix="zmem-doctor63b-"))
        self.addCleanup(shutil.rmtree, store_dir, True)
        db = store_dir / "store.sqlite"
        c = _sq.connect(db)
        c.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES('schema_version','11');"
            "CREATE TABLE memory(id TEXT PRIMARY KEY, namespace TEXT,"
            " type TEXT, content TEXT, tags TEXT DEFAULT '',"
            " source_ref TEXT DEFAULT '', source_hash TEXT DEFAULT '',"
            " confidence REAL DEFAULT 0.5, signal TEXT DEFAULT 'none',"
            " valid_from TEXT, superseded_at TEXT,"
            " supersede_reason TEXT DEFAULT '', consolidated_at TEXT,"
            " merged_from TEXT, ingestion_ts TEXT,"
            " retrieval_count INTEGER DEFAULT 0, last_retrieved TEXT,"
            " embedding BLOB, embedding_model TEXT DEFAULT '',"
            " embedded_at TEXT, content_norm TEXT DEFAULT '',"
            " valid_until TEXT DEFAULT '', update_of TEXT,"
            " taint TEXT DEFAULT '', trust_score REAL);"
            "INSERT INTO memory(id,content,valid_from,ingestion_ts)"
            " VALUES('r1','hello world','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z');"
        )
        c.commit()
        c.close()
        env = {
            "ZMEM_DATA": str(store_dir),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_MODELS_DIR": str(store_dir / "no-models"),
        }
        rep = self._report(env)
        eh = next(c for c in rep["checks"] if c["id"] == "embeddings_health")
        d = eh["details"]
        for key in (
            "rows_with_embedding",
            "rows_without_embedding",
            "shipped_profiles",
            "active_profile",
            "matches_store",
        ):
            self.assertIn(key, d, key)
        self.assertEqual(d["live_memories"], 1)
        names = {p["name"] for p in d["shipped_profiles"]}
        self.assertEqual(names, {"minilm", "fake"})
        # zax-L3/PRR-005: cross-encoder visibility must exist in the
        # health payload even when unset (issue #126: unset = enabled
        # by default; unconfigured model still degrades fail-open)
        ce = d.get("cross_encoder") or {}
        self.assertIn("enabled", ce)
        self.assertEqual(ce.get("enabled"), True)

    def test_warning_decision_core_unit(self):
        sys.path.insert(0, str(self.scripts))
        import doctor

        w = doctor._embedding_health_warnings(
            active_profile="fake",
            embeddings_available=True,
            matches_store=None,
            total_live=5,
            with_emb=5,
            store_is_temp=False,
        )
        self.assertEqual(len(w), 1)
        self.assertIn("NON-temporary", w[0])

        w2 = doctor._embedding_health_warnings(
            active_profile="minilm",
            embeddings_available=True,
            matches_store=True,
            total_live=9,
            with_emb=0,
            store_is_temp=True,
        )
        self.assertEqual(len(w2), 1)
        self.assertIn("ZERO embedded", w2[0])

        # fake inside a temp sandbox stays quiet; unavailable runtime mutes
        # the zero-embedded advisory
        self.assertEqual(
            doctor._embedding_health_warnings(
                active_profile="fake",
                embeddings_available=True,
                matches_store=None,
                total_live=5,
                with_emb=5,
                store_is_temp=True,
            ),
            [],
        )
        self.assertEqual(
            doctor._embedding_health_warnings(
                active_profile="minilm",
                embeddings_available=False,
                matches_store=None,
                total_live=9,
                with_emb=0,
                store_is_temp=True,
            ),
            [],
        )


INSTALL_SKEW_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "doctor" / "install-skew"
INSTALL_SKEW_CHECK_IDS = (
    "duplicate-install",
    "marketplace-skew",
    "project-pin",
    "untrusted-hook",
    "zcode-native-memory",
    "orphan-store",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fixture_tree_digest(root: Path) -> str:
    """Issue #185 fixture-tree digest: SHA-256 over the sorted POSIX relative
    paths of every regular file, each framed by NUL bytes. Symlinks fail the
    fixture validation instead of being hashed through their target."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or os.path.islink(path):
            raise ValueError(f"symlink found in fixture tree: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class DoctorInstallSkewTest(unittest.TestCase):
    """Issue #185: host install-skew diagnostics (duplicate installs,
    marketplace skew, project pins), ZCode native memory, Codex manifest hook
    trust, and the read-only orphan-store inventory. Unit calls import doctor
    directly; every CLI run points HOME and the ZMEM_* store variables at
    temp/scratch paths so the operator's real store is never resolved and
    storelib is never imported through this class."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor185-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.project = self.tmp / "project"
        self.bin = self.tmp / "bin"
        for d in (self.home, self.repo, self.project, self.bin):
            d.mkdir()
        self._write_fake_tools()
        self._write_repo_surfaces()
        if REAL_GIT:
            subprocess.run([REAL_GIT, "init", "-q"], cwd=str(self.project), check=True)
            subprocess.run(
                [
                    REAL_GIT,
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/Example/Widget.git",
                ],
                cwd=str(self.project),
                check=True,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fake_tools(self):
        node = self.bin / "node.cmd"
        git = self.bin / "git.cmd"
        bash = self.bin / "Git" / "bin" / "bash.cmd"
        _write_text(node, _cmd_script("echo v20.11.0"))
        _write_text(git, _cmd_script("echo https://github.com/Example/Widget.git"))
        _write_text(bash, _cmd_script("echo GNU bash, version 5.2.0"))

    def _write_repo_surfaces(self):
        _write_text(self.repo / ".claude-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.claude.json", "{}\n")
        _write_text(self.repo / ".codex-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.codex.json", "{}\n")
        _write_text(self.repo / ".zcode-plugin" / "plugin.json", "{}\n")
        _write_text(self.repo / "hooks" / "hooks.zcode.json", "{}\n")
        _write_text(self.repo / "skills" / "memory" / "SKILL.md", "# memory\n")

    def _install_fixture_home(self):
        """Copy the whole install-skew fixture tree into the temp HOME, then
        map the claude/zcode/codex subtrees onto the dotted host directories
        (.claude, .zcode, .codex) the doctor inspects."""
        self._install_fixture_home_at(self.home)

    def _install_fixture_home_at(self, home):
        shutil.copytree(INSTALL_SKEW_FIXTURES, home, dirs_exist_ok=True)
        for name in ("claude", "zcode", "codex"):
            shutil.move(str(home / name), str(home / ("." + name)))

    def _base_env(self):
        env = {**os.environ}
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["ZMEM_BASH_PATH"] = str(self.bin / "Git" / "bin" / "bash.cmd")
        for key in (
            "ZMEM_STORE",
            "ZMEM_DATA",
            "ZMEM_CORE_MD",
            "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA",
            "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            "ZMEM_MODELS_DIR",
            "ZMEM_MODEL_AUTODOWNLOAD",
            "ZMEM_EMBED_PROFILE",
            "ZMEM_CROSS_ENCODER",
            "ZMEM_CROSS_ENCODER_MODEL",
            "OneDrive",
            "OneDriveConsumer",
            "OneDriveCommercial",
        ):
            env.pop(key, None)
        return env

    def _scratch_env(self):
        """_base_env plus the four isolated-store variables pointing at a
        scratch directory that never holds a real store."""
        env = self._base_env()
        scratch = self.tmp / "scratch"
        env["ZMEM_STORE"] = str(scratch / "store.sqlite")
        env["ZMEM_DATA"] = str(scratch / "data")
        env["ZMEM_MODELS_DIR"] = str(scratch / "missing-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        return env

    def _run(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(DOCTOR_PY), *args],
            env=env or self._scratch_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _doctor(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import doctor  # noqa: E402

        return doctor

    def _relative_to_home(self, raw) -> str:
        text = str(raw).replace("\\", "/")
        prefix = str(self.home).replace("\\", "/").rstrip("/") + "/"
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix) :]
        return text.lstrip("/")

    # --- _host_install_checks ----------------------------------------------

    def test_duplicate_install_is_fail_duplicate_install(self):
        doctor = self._doctor()
        self._install_fixture_home()
        checks = doctor._host_install_checks(self.home, self.project, self.repo)
        dupes = [c for c in checks if c.get("id") == "duplicate-install"]
        self.assertEqual(len(dupes), 1, checks)
        check = dupes[0]
        self.assertEqual(check["status"], "fail", check)
        self.assertEqual(
            check["summary"],
            "duplicate-install host=claude ids=zmem@primary,zmem@secondary",
            check,
        )
        # The project-scoped entry belongs to project-pin, not this check.
        self.assertNotIn("zmem@project-pin", check["summary"])
        # Issue #185 review PRR-001: a foreign enabled user-scope plugin in
        # a real registry must NOT count as a zmem install (and a
        # non-semver foreign version must not crash the pin comparison).
        foreign_home = self.tmp / "foreign-home"
        foreign_registry = (
            foreign_home / ".claude" / "plugins" / "installed_plugins.json"
        )
        _write_text(
            foreign_registry,
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "zmem@solo": [
                            {
                                "scope": "user",
                                "version": "0.27.0",
                                "enabled": True,
                            }
                        ],
                        "other-author@other-plugin": [
                            {
                                "scope": "user",
                                "version": "1.2.3",
                                "enabled": True,
                            }
                        ],
                    },
                }
            )
            + "\n",
        )
        foreign_checks = doctor._host_install_checks(
            foreign_home, self.project, self.repo
        )
        foreign_dupes = [
            c for c in foreign_checks if c.get("id") == "duplicate-install"
        ]
        self.assertEqual(
            len(foreign_dupes),
            0,
            "non-zmem plugins must not produce duplicate-install: %r"
            % (foreign_checks,),
        )

    def test_marketplace_skew_is_warn(self):
        doctor = self._doctor()
        self._install_fixture_home()
        checks = doctor._host_install_checks(self.home, self.project, self.repo)
        skew = [c for c in checks if c.get("id") == "marketplace-skew"]
        self.assertEqual(len(skew), 1, checks)  # ONE check across both hosts
        check = skew[0]
        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(
            check["summary"],
            "marketplace-skew installed=0.27.0 marketplace=0.14.0",
            check,
        )
        rendered = json.dumps(check.get("details", {})).replace("\\", "/")
        self.assertIn("0.27.0", rendered)
        self.assertIn("0.14.0", rendered)
        self.assertIn("marketplace/claude/plugin.json", rendered)
        self.assertIn("marketplace/zcode/plugin.json", rendered)

    def test_project_pin_is_warn(self):
        doctor = self._doctor()
        self._install_fixture_home()
        checks = doctor._host_install_checks(self.home, self.project, self.repo)
        pins = [c for c in checks if c.get("id") == "project-pin"]
        self.assertEqual(len(pins), 1, checks)
        check = pins[0]
        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(
            check["summary"], "project-pin project=0.14.0 user=0.27.0", check
        )
        rendered = json.dumps(check.get("details", {}))
        self.assertIn("zmem@project-pin", rendered)
        self.assertIn("project", rendered)
        self.assertIn("user", rendered)

    # --- codex manifest trust ----------------------------------------------

    def test_codex_missing_manifest_hook_is_warn(self):
        doctor = self._doctor()
        self._install_fixture_home()
        hook_ids = doctor._codex_manifest_hook_ids(
            INSTALL_SKEW_FIXTURES / "hooks" / "hooks.codex.json"
        )
        self.assertIn("session_start", hook_ids, hook_ids)
        self.assertIn("pre_tool_use", hook_ids, hook_ids)
        # The fixture config trusts only session_start for C:/fixture/repo;
        # matching is a string comparison and never requires that key to
        # exist as a real directory.
        check = doctor._check_codex_manifest_trust(self.home, Path("C:/fixture/repo"))
        self.assertEqual(check["id"], "untrusted-hook", check)
        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(check["summary"], "untrusted-hook pre_tool_use", check)
        rendered = json.dumps(check.get("details", {})).replace("\\", "/")
        self.assertIn("hooks.codex.json", rendered)
        self.assertIn("config.toml", rendered)
        # Issue #185 review PRR-005: valid JSON with the wrong shape must
        # WARN, never read as "nothing registered" (silent PASS). The
        # manifest resolves from repo_root when present, so point repo_root
        # at a scratch repo holding each bad payload.
        for payload in ("[]", '{"hooks": "x"}', "{}"):
            shape_repo = self.tmp / "shape-repo"
            shape_repo_hooks = shape_repo / "hooks"
            if shape_repo_hooks.exists():
                shutil.rmtree(shape_repo_hooks)
            shape_repo_hooks.mkdir(parents=True)
            (shape_repo_hooks / "hooks.codex.json").write_text(
                payload, encoding="utf-8", newline="\n"
            )
            check = doctor._check_codex_manifest_trust(self.home, shape_repo)
            if payload == "{}":
                # An empty manifest registers nothing: PASS is correct.
                self.assertEqual(check["status"], "pass", (payload, check))
            else:
                self.assertEqual(check["status"], "warn", (payload, check))
            shutil.rmtree(shape_repo)
        # Issue #185 review PRR-004: even with host_registry unavailable,
        # the manifest-trust check must still run.
        from unittest import mock

        with mock.patch.object(doctor, "host_registry", None):
            checks = doctor._host_install_checks(self.home, self.project, self.repo)
        self.assertTrue(
            [c for c in checks if c.get("id") == "untrusted-hook"],
            checks,
        )

    # --- zcode native memory -------------------------------------------------

    def test_zcode_native_memory_false_is_pass(self):
        doctor = self._doctor()
        setting = self.home / ".zcode" / "v2" / "setting.json"
        setting.parent.mkdir(parents=True, exist_ok=True)
        setting.write_bytes(
            (INSTALL_SKEW_FIXTURES / "zcode" / "v2" / "setting.json").read_bytes()
        )
        check = doctor._check_zcode_native_memory(self.home)
        self.assertEqual(check["id"], "zcode-native-memory", check)
        self.assertEqual(check["status"], "pass", check)
        self.assertEqual(check["summary"], "ZCode native memory is disabled.", check)
        rendered = json.dumps(check.get("details", {})).replace("\\", "/")
        self.assertIn(".zcode", rendered)
        self.assertIn("setting.json", rendered)
        # Issue #185 review PRR-007: pin the remaining branches — explicit
        # true FAILs, a missing key warns, and unreadable JSON warns.
        setting.write_text('{"memoryEnabled": true}\n', encoding="utf-8", newline="\n")
        check = doctor._check_zcode_native_memory(self.home)
        self.assertEqual(check["status"], "fail", check)
        setting.write_text("{}\n", encoding="utf-8", newline="\n")
        check = doctor._check_zcode_native_memory(self.home)
        self.assertEqual(check["status"], "warn", check)
        setting.write_text("{broken\n", encoding="utf-8", newline="\n")
        check = doctor._check_zcode_native_memory(self.home)
        self.assertEqual(check["status"], "warn", check)

    # --- orphan-store inventory ----------------------------------------------

    def test_orphan_store_reports_schema_and_rows(self):
        from unittest import mock

        doctor = self._doctor()
        orphan = INSTALL_SKEW_FIXTURES / "orphan" / "store.sqlite"
        resolved = self.tmp / "scratch" / "store.sqlite"  # absent scratch store
        decoy = self.tmp / "decoy" / "store.sqlite"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_bytes(b"not a sqlite database")
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            Path, "home", return_value=self.home
        ):
            for key in ("ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
                os.environ.pop(key, None)
            candidates = doctor._orphan_store_candidates(
                resolved, self.home, extra_candidates=[orphan]
            )
            checks = doctor._check_orphan_stores(resolved, extra_candidates=[orphan])
        # Only the injected fixture is a candidate: no env vars point
        # anywhere, home has no .zcode/memory store, and unrelated decoy
        # directories are never scanned.
        self.assertEqual(len(candidates), 1, candidates)
        self.assertEqual(
            os.path.normcase(str(Path(candidates[0]).resolve())),
            os.path.normcase(str(orphan.resolve())),
        )
        found = [c for c in checks if c.get("id") == "orphan-store"]
        self.assertEqual(len(found), 1, checks)
        check = found[0]
        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(check["summary"], "orphan-store schema=9 rows=362", check)
        details = check.get("details", {})
        self.assertEqual(details["schema_version"], 9, details)
        self.assertEqual(details["rows"], 362, details)
        self.assertIsInstance(details["schema_version"], int, details)
        self.assertIsInstance(details["rows"], int, details)
        self.assertIn("path", details, details)
        self.assertTrue(
            str(details["path"]).replace("\\", "/").endswith("orphan/store.sqlite"),
            details,
        )

    # --- malformed registries never crash the report -------------------------

    def test_malformed_registry_is_nonfatal(self):
        doctor = self._doctor()
        bad = self.home / ".claude" / "plugins" / "installed_plugins.json"
        _write_text(bad, "{not json")
        checks = doctor._host_install_checks(self.home, self.project, self.repo)
        self.assertIsInstance(checks, list, checks)
        self.assertFalse([c for c in checks if c["status"] == "fail"], checks)
        warned = [
            c
            for c in checks
            if c["status"] == "warn"
            and "installed_plugins.json" in json.dumps(c).replace("\\", "/")
        ]
        self.assertTrue(warned, checks)
        result = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
        )
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertTrue(result.stdout.strip(), result.stderr)
        report = json.loads(result.stdout)
        statuses = {}
        for check in report["checks"]:
            statuses.setdefault(check["id"], set()).add(check["status"])
        for check_id in ("duplicate-install", "marketplace-skew", "project-pin"):
            self.assertNotIn("fail", statuses.get(check_id, set()), report)
        # Issue #185 review PRR-002: an over-long digit version component
        # must take the malformed-WARN path, never abort the doctor run
        # (CPython 3.11+ int() raises beyond 4300 digits).
        big_home = self.tmp / "big-version-home"
        self._install_fixture_home_at(big_home)
        big_registry = big_home / ".claude" / "plugins" / "installed_plugins.json"
        registry = json.loads(big_registry.read_text(encoding="utf-8"))
        registry["plugins"]["zmem@big"] = [
            {
                "scope": "user",
                "version": "1." + "9" * 5000 + ".3",
                "enabled": True,
            }
        ]
        big_registry.write_text(
            json.dumps(registry) + "\n", encoding="utf-8", newline="\n"
        )
        big_checks = doctor._host_install_checks(big_home, self.project, self.repo)
        big_warned = [
            c
            for c in big_checks
            if c.get("id") == "host-registry" and c.get("status") == "warn"
        ]
        self.assertTrue(big_warned, big_checks)

    # --- fixture CLI end-to-end + read-only digests ---------------------------

    def test_fixture_tree_sha256_is_unchanged(self):
        if not REAL_GIT:
            self.skipTest("git is required for the namespace fixture")
        # Issue #185 review PRR-009: the fixture manifest is a byte-copy of
        # the shipped hooks/hooks.codex.json — assert the copy stays in
        # sync, otherwise the install-skew tests silently diverge from real
        # behavior when the shipped manifest changes.
        self.assertEqual(
            (INSTALL_SKEW_FIXTURES / "hooks" / "hooks.codex.json").read_bytes(),
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_bytes(),
            "install-skew hooks fixture must stay a byte-copy of the "
            "shipped hooks/hooks.codex.json",
        )
        self._install_fixture_home()
        (self.repo / "hooks" / "hooks.codex.json").write_bytes(
            (INSTALL_SKEW_FIXTURES / "hooks" / "hooks.codex.json").read_bytes()
        )
        env = self._scratch_env()
        env["CLAUDE_PLUGIN_DATA"] = str(self.home / "orphan")
        scratch_store = Path(env["ZMEM_STORE"])

        fixture_files = sorted(
            p for p in INSTALL_SKEW_FIXTURES.rglob("*") if p.is_file()
        )
        before = {p: _sha256(p) for p in fixture_files}
        digest_before = _fixture_tree_digest(INSTALL_SKEW_FIXTURES)
        self.assertRegex(digest_before, r"^[0-9a-f]{64}$")
        home_orphan = self.home / "orphan" / "store.sqlite"
        repo_manifest = self.repo / "hooks" / "hooks.codex.json"
        copies_before = {
            "home-orphan": _sha256(home_orphan),
            "repo-manifest": _sha256(repo_manifest),
        }

        json_run = self._run(
            "--format",
            "json",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
        )
        human_run = self._run(
            "--format",
            "human",
            "--repo-root",
            str(self.repo),
            "--project",
            str(self.project),
            env=env,
        )
        self.assertEqual(json_run.returncode, 1, json_run.stdout + json_run.stderr)
        self.assertEqual(human_run.returncode, 1, human_run.stdout + human_run.stderr)

        report = json.loads(json_run.stdout)
        picked = {}
        for check in report["checks"]:
            if check["id"] in INSTALL_SKEW_CHECK_IDS:
                picked.setdefault(check["id"], []).append(check)
        extracted = []
        for check_id in INSTALL_SKEW_CHECK_IDS:
            entries = picked.get(check_id, [])
            self.assertEqual(len(entries), 1, (check_id, report["summary"]))
            check = entries[0]
            entry = {
                "id": check["id"],
                "status": check["status"],
                "summary": check["summary"],
            }
            if check_id == "orphan-store":
                details = check["details"]
                entry["details"] = {
                    "path": self._relative_to_home(details["path"]),
                    "schema_version": details["schema_version"],
                    "rows": details["rows"],
                }
                self.assertEqual(entry["details"]["schema_version"], 9, check)
                self.assertEqual(entry["details"]["rows"], 362, check)
            extracted.append(entry)
        expected_line = (
            (INSTALL_SKEW_FIXTURES / "expected.json").read_bytes().rstrip(b"\n")
        )
        self.assertEqual(
            json.dumps({"checks": extracted}, separators=(",", ":")).encode("utf-8"),
            expected_line,
            "the extracted install-skew checks must byte-match expected.json",
        )

        # JSON/human parity: the same six ids render with the same tokens.
        self.assertIn(
            "[FAIL] duplicate-install: duplicate-install host=claude "
            "ids=zmem@primary,zmem@secondary",
            human_run.stdout,
        )
        self.assertIn(
            "[WARN] marketplace-skew: marketplace-skew "
            "installed=0.27.0 marketplace=0.14.0",
            human_run.stdout,
        )
        self.assertIn(
            "[WARN] project-pin: project-pin project=0.14.0 user=0.27.0",
            human_run.stdout,
        )
        self.assertIn(
            "[WARN] untrusted-hook: untrusted-hook pre_tool_use", human_run.stdout
        )
        self.assertIn(
            "[PASS] zcode-native-memory: ZCode native memory is disabled.",
            human_run.stdout,
        )
        self.assertIn(
            "[WARN] orphan-store: orphan-store schema=9 rows=362", human_run.stdout
        )

        # Read-only proof: doctor never creates or rewrites a single byte.
        self.assertFalse(scratch_store.exists(), "doctor must not create the store")
        self.assertEqual(_fixture_tree_digest(INSTALL_SKEW_FIXTURES), digest_before)
        for path, digest in before.items():
            self.assertEqual(_sha256(path), digest, path)
        self.assertEqual(_sha256(home_orphan), copies_before["home-orphan"])
        self.assertEqual(_sha256(repo_manifest), copies_before["repo-manifest"])

        # The digest contract rejects symlinks outright (platform permitting).
        sym_root = self.tmp / "symtree"
        sym_root.mkdir()
        (sym_root / "real.txt").write_bytes(b"real")
        try:
            os.symlink(str(sym_root / "real.txt"), str(sym_root / "link.txt"))
        except OSError:
            self.skipTest("os.symlink is unavailable on this platform")
        with self.assertRaises(ValueError):
            _fixture_tree_digest(sym_root)


class FeedbackDoctorTest(unittest.TestCase):
    """Issue #124: the voyager-counters check reports the observational
    feedback totals (SQL aggregates + sidecar counts) with the seven stable
    labels, in order, in details and human summary."""

    FB_SESS = "00000000-0000-4000-8000-000000000124"
    FB_M125 = "00000000-0000-4000-8000-000000000125"
    FB_M126 = "00000000-0000-4000-8000-000000000126"
    _LABELS = (
        "total_applied",
        "total_violated",
        "nonzero_applied",
        "nonzero_violated",
        "matched_applied",
        "matched_violated",
        "unmatched_operations",
    )

    def test_counter_totals_and_ranges(self):
        import doctor
        from unittest import mock

        tmp = tempfile.mkdtemp(prefix="zmem-fbdoctor-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = Path(tmp) / "store.sqlite"
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            conn.execute(
                "CREATE TABLE memory(id TEXT PRIMARY KEY, superseded_at TEXT,"
                " applied_count INTEGER NOT NULL DEFAULT 0,"
                " violated_count INTEGER NOT NULL DEFAULT 0)"
            )
            # Fixture semantics: one row violated once, one row applied once
            # (both live — the totals aggregate over live rows only).
            conn.executemany(
                "INSERT INTO memory(id, superseded_at, applied_count,"
                " violated_count) VALUES (?, NULL, ?, ?)",
                [(self.FB_M125, 0, 1), (self.FB_M126, 1, 0)],
            )
            conn.commit()
        finally:
            conn.close()
        # The committed sidecar fixture verbatim, in the store's parent dir
        # (the data dir doctor derives from resolved_store.parent): one
        # violated + one applied + one unmatched record.
        ops = Path(tmp) / "ops"
        ops.mkdir()
        sidecar = ops / (
            hashlib.sha256(self.FB_SESS.encode("utf-8")).hexdigest()[:32]
            + ".feedback.jsonl"
        )
        sidecar.write_bytes(
            (
                REPO_ROOT / "tests" / "fixtures" / "feedback_sidecar_expected.jsonl"
            ).read_bytes()
        )

        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "ZMEM_STORE",
                "ZMEM_DATA",
                "ZMEM_BACKUP_DIR",
                "CLAUDE_PLUGIN_DATA",
                "ZCODE_PLUGIN_DATA",
            )
        }
        env.update(
            {
                "ZMEM_STORE": str(store),
                "ZMEM_DATA": tmp,
                "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
            }
        )
        with mock.patch.dict(os.environ, env):
            check = doctor._check_voyager_counters(store)
        self.assertEqual(check["status"], "pass", check)
        details = check["details"]
        self.assertEqual(details["total_applied"], 1, check)
        self.assertEqual(details["total_violated"], 1, check)
        self.assertEqual(details["nonzero_applied"], 1, check)
        self.assertEqual(details["nonzero_violated"], 1, check)
        self.assertEqual(details["matched_applied"], 1, check)
        self.assertEqual(details["matched_violated"], 1, check)
        self.assertEqual(details["unmatched_operations"], 1, check)
        self.assertEqual(details["applied_max"], 1, check)
        self.assertEqual(details["violated_max"], 1, check)
        # The human summary names the same seven labels, in contract order.
        summary = check["summary"]
        positions = []
        for label in self._LABELS:
            self.assertIn(
                label + "=", summary, f"summary must name {label} (got: {summary!r})"
            )
            positions.append(summary.index(label + "="))
        self.assertEqual(
            positions, sorted(positions), f"labels out of order in summary: {summary!r}"
        )
        self.assertEqual(len(set(positions)), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
