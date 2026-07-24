"""Plain-unittest tests for skills/memory/scripts/host.py.

Run: python tests/test_host.py
No pytest / third-party test harness required — matches the repo convention
(no existing test infra) per PLAN.md P1.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import host  # noqa: E402


ENV_KEYS = ["ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_CORE_MD", "OneDrive"]


class TestStorePathPrecedence(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self._patcher.stop()

    def test_zmem_store_wins_over_everything(self):
        os.environ["ZMEM_STORE"] = r"C:\explicit\store.sqlite"
        os.environ["ZMEM_DATA"] = r"C:\zmemdata"
        os.environ["CLAUDE_PLUGIN_DATA"] = r"C:\claudedata"
        os.environ["ZCODE_PLUGIN_DATA"] = r"C:\zcodedata"
        self.assertEqual(host.resolve_store_path(), Path(r"C:\explicit\store.sqlite"))

    def test_zmem_data_wins_over_plugin_data(self):
        os.environ["ZMEM_DATA"] = r"C:\zmemdata"
        os.environ["CLAUDE_PLUGIN_DATA"] = r"C:\claudedata"
        os.environ["ZCODE_PLUGIN_DATA"] = r"C:\zcodedata"
        self.assertEqual(host.resolve_store_path(), Path(r"C:\zmemdata") / "store.sqlite")

    def test_claude_plugin_data_wins_over_zcode(self):
        os.environ["CLAUDE_PLUGIN_DATA"] = r"C:\claudedata"
        os.environ["ZCODE_PLUGIN_DATA"] = r"C:\zcodedata"
        self.assertEqual(host.resolve_store_path(), Path(r"C:\claudedata") / "store.sqlite")

    def test_zcode_plugin_data_used_alone(self):
        os.environ["ZCODE_PLUGIN_DATA"] = r"C:\zcodedata"
        self.assertEqual(host.resolve_store_path(), Path(r"C:\zcodedata") / "store.sqlite")

    def test_default_is_dot_zmem_when_nothing_set(self):
        # No env vars set, no legacy store present -> new box-neutral default.
        with mock.patch.object(Path, "exists", return_value=False):
            result = host.resolve_store_path()
        self.assertEqual(result, Path(os.path.expanduser("~")) / ".zmem" / "store.sqlite")

    def test_legacy_used_only_if_it_already_exists_and_new_default_does_not(self):
        home = Path(os.path.expanduser("~"))
        zmem_default = home / ".zmem" / "store.sqlite"
        legacy = home / ".zcode" / "memory" / "store.sqlite"

        def fake_exists(self):
            if self == zmem_default:
                return False
            if self == legacy:
                return True
            return False

        with mock.patch.object(Path, "exists", fake_exists):
            result = host.resolve_store_path()
        self.assertEqual(result, legacy)

    def test_new_default_wins_once_it_exists(self):
        home = Path(os.path.expanduser("~"))
        zmem_default = home / ".zmem" / "store.sqlite"

        def fake_exists(self):
            return self == zmem_default

        with mock.patch.object(Path, "exists", fake_exists):
            result = host.resolve_store_path()
        self.assertEqual(result, zmem_default)


class TestCoreMdPath(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        self._patcher.stop()

    def test_explicit_override(self):
        os.environ["ZMEM_CORE_MD"] = r"C:\somewhere\core.md"
        self.assertEqual(host.resolve_core_md_path(), Path(r"C:\somewhere\core.md"))

    def test_derives_from_store_dir(self):
        os.environ["ZMEM_DATA"] = r"C:\zmemdata"
        self.assertEqual(host.resolve_core_md_path(), Path(r"C:\zmemdata") / "core.md")


class TestLocalFsGuard(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop("OneDrive", None)

    def tearDown(self):
        self._patcher.stop()

    def test_rejects_unc_path(self):
        with self.assertRaises(ValueError):
            host.assert_local_fs(Path(r"\\server\share\x"))

    def test_rejects_forward_slash_unc(self):
        with self.assertRaises(ValueError):
            host.assert_local_fs(Path("//server/share/x"))

    def test_rejects_under_onedrive(self):
        os.environ["OneDrive"] = r"D:\Cloud\OneDrive"
        with self.assertRaises(ValueError):
            host.assert_local_fs(Path(r"D:\Cloud\OneDrive\zmem"))

    def test_allows_local_drive_letter(self):
        os.environ["OneDrive"] = r"D:\Cloud\OneDrive"
        # Should not raise.
        host.assert_local_fs(Path(r"C:\Users\Brett\.zmem"))

    def test_no_crash_when_onedrive_env_unset(self):
        os.environ.pop("OneDrive", None)
        os.environ.pop("OneDriveConsumer", None)
        os.environ.pop("OneDriveCommercial", None)
        host.assert_local_fs(Path(r"C:\Users\Brett\.zmem"))


@unittest.skipUnless(sys.platform == "win32", "icacls is Windows-only")
class TestSetOwnerOnlyPerms(unittest.TestCase):
    """Regression test for a real bug found during P6 verification: a bare
    `icacls DIR /inheritance:r /grant:r user:F` grants rights on the directory
    object only (no inheritable ACE), so a file created in that directory
    *after* hardening can end up with an effectively empty DACL and become
    unreadable to the very same user. Reproduced on a real (non-temp)
    C:\\Users\\<you>\\... path, not just under AppData\\Local\\Temp: on a
    fresh ZMEM_DATA dir, every session-start after the first silently lost
    core.md (PermissionError swallowed by the hook's fail-open error handling).
    Directories must get (OI)(CI) so children inherit read/write."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-ownerperms-"))

    def tearDown(self):
        import shutil
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def test_file_created_after_hardening_is_still_readable(self):
        host.set_owner_only_perms(self.tmp)
        # Simulate what happens next in the real flow: a file (core.md,
        # store.sqlite) gets created in the now-hardened directory.
        f = self.tmp / "core.md"
        f.write_text("hello from the hardened dir\n", encoding="utf-8")
        # Must be readable by this same process/user without raising.
        self.assertEqual(f.read_text(encoding="utf-8"), "hello from the hardened dir\n")

    def test_never_raises_on_missing_username(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USERNAME", None)
            os.environ.pop("USER", None)
            host.set_owner_only_perms(self.tmp)  # must not raise


class TestBusyRetry(unittest.TestCase):
    def test_retries_on_locked_then_succeeds(self):
        import sqlite3
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch("time.sleep", return_value=None):
            result = host.busy_retry(flaky, attempts=5)
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_reraises_non_lock_operational_error(self):
        import sqlite3

        def boom():
            raise sqlite3.OperationalError("no such table: x")

        with self.assertRaises(sqlite3.OperationalError):
            host.busy_retry(boom, attempts=3)

    def test_exhausts_attempts_and_raises(self):
        import sqlite3

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch("time.sleep", return_value=None):
            with self.assertRaises(sqlite3.OperationalError):
                host.busy_retry(always_locked, attempts=3)


class TestImportSmoke(unittest.TestCase):
    """End-to-end: build a tiny source store, import it, assert row-count
    parity and source-unchanged proof."""

    def test_import_preserves_row_count_and_source(self):
        import importlib.util

        # The module file is literally import-store.py; '-' isn't valid in a
        # module name for a plain import statement, so load it by path.
        spec = importlib.util.spec_from_file_location(
            "zmem_import_store", SCRIPTS_DIR / "import-store.py"
        )
        import_store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(import_store)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            dest_dir = tmp_path / "dest"
            source_dir.mkdir()
            source_store = source_dir / "store.sqlite"

            # Build a minimal real store via store.py's own schema so the
            # import script's row-count/integrity queries are meaningful.
            spec2 = importlib.util.spec_from_file_location(
                "zmem_store_for_test", SCRIPTS_DIR / "store.py"
            )
            # store.py resolves STORE_PATH at import time from the environment;
            # point it at our temp source before exec'ing the module.
            with mock.patch.dict(os.environ, {"ZMEM_STORE": str(source_store)}, clear=False):
                store_mod = importlib.util.module_from_spec(spec2)
                spec2.loader.exec_module(store_mod)
                conn = store_mod.connect()
                store_mod.init_db(conn)
                store_mod.migrate(conn)
                store_mod.add_memory(
                    conn, namespace="user:global", type_="fact",
                    content="test memory one", signal="test",
                )
                store_mod.add_memory(
                    conn, namespace="user:global", type_="fact",
                    content="test memory two", signal="test",
                )
                expected_live = conn.execute(
                    "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
                ).fetchone()[0]
                conn.close()

            (source_dir / "core.md").write_text("# core\n", encoding="utf-8")

            result = import_store.run_import(source_store, dest_dir, force=False)

            self.assertTrue(result["source_unchanged"])
            self.assertEqual(result["dest_integrity_check"], "ok")
            self.assertEqual(result["dest_live_rows"], expected_live)
            self.assertTrue((dest_dir / "core.md").exists())

            # Re-hash the source ourselves, independent of the script, as a
            # second source-untouched proof.
            after_hash = import_store._file_fingerprint(source_store)["sha256"]
            self.assertEqual(after_hash, result["source_sha256_after"])

            # A second import into the same non-empty dest without --force
            # must refuse rather than silently overwrite.
            with self.assertRaises(FileExistsError):
                import_store.run_import(source_store, dest_dir, force=False)

            # --force allows the overwrite.
            result2 = import_store.run_import(source_store, dest_dir, force=True)
            self.assertTrue(result2["source_unchanged"])


if __name__ == "__main__":
    unittest.main()
