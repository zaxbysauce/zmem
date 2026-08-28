"""Forward-compat schema gate tests (issue #65 follow-up).

The gate: a client refuses a store ABOVE FORWARD_COMPAT_SCHEMA_VERSION,
proceeds with a one-time stderr NOTICE for the additive window between
SUPPORTED and FORWARD_COMPAT, and honors ZMEM_ALLOW_NEWER_SCHEMA=1 as an
explicit operator override above the ceiling. Defaults keep today's
behavior (FORWARD_COMPAT == SUPPORTED -> anything newer refuses), so the
window only opens when a maintenance release of an older line extends it.

Also pins the real-world scenario that motivated this: an OLDER client
(patch its SUPPORTED constant to 12, FORWARD_COMPAT at 13) must be able to
store and recall memories on a v13 store, because v13 is additive-only.

Runs standalone: python tests/test_schema_forward_compat.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))


class SchemaCompatGateTest(unittest.TestCase):
    """Unit matrix for _schema_compat + the end-to-end older-client case."""

    def setUp(self):
        import importlib

        import storelib.schema as schema

        importlib.reload(schema)
        self.schema = schema
        # Fresh warning flag per test.
        self.schema._schema_compat._warned = False

    def tearDown(self):
        import importlib

        importlib.reload(self.schema)
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

    def _decide(self, store_version: int) -> str:
        return self.schema._schema_compat(store_version, "store-under-test")

    def test_store_at_or_below_supported_is_ok(self):
        supported = self.schema.SUPPORTED_SCHEMA_VERSION
        self.assertEqual(self._decide(supported), "ok")
        self.assertEqual(self._decide(1), "ok")

    def test_default_ceiling_refuses_supported_plus_one(self):
        # FORWARD_COMPAT defaults to SUPPORTED: today's behavior preserved.
        self.assertEqual(
            self.schema.FORWARD_COMPAT_SCHEMA_VERSION,
            self.schema.SUPPORTED_SCHEMA_VERSION)
        with self.assertRaises(RuntimeError) as cm:
            self._decide(self.schema.SUPPORTED_SCHEMA_VERSION + 1)
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", str(cm.exception))

    def test_additive_window_proceeds_with_notice(self):
        # Simulate an older client (SUPPORTED=12) on the additive v13 store.
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema._schema_compat._warned = False
        self.assertEqual(self._decide(13), "compat")
        # One-time NOTICE per process.
        self.assertEqual(self._decide(13), "compat")

    def test_above_ceiling_refuses_without_override(self):
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema._schema_compat._warned = False
        with self.assertRaises(RuntimeError):
            self._decide(14)

    def test_env_override_admits_above_ceiling(self):
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema._schema_compat._warned = False
        os.environ["ZMEM_ALLOW_NEWER_SCHEMA"] = "1"
        try:
            self.assertEqual(self._decide(14), "compat")
        finally:
            os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)


class OlderClientOnNewerStoreTest(unittest.TestCase):
    """The motivating scenario: a v12-lineage client (SUPPORTED=12) stores
    and recalls on a v13 store, because v13 is additive-only."""

    def setUp(self):
        import importlib
        import storelib.schema as schema
        # C-review item 4: reload first so a patched SUPPORTED from a
        # prior test can never leak into this class.
        importlib.reload(schema)
        self._tmp = tempfile.mkdtemp(prefix="zmem-fwdcompat-")
        self._saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD")}
        os.environ["ZMEM_STORE"] = os.path.join(self._tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # Re-load AFTER the env is set: the module binds STORE_PATH at
        # import, and an in-process connect() must target THIS test's
        # store (a stale path polluted an earlier run).
        importlib.reload(schema)
        schema._schema_compat._warned = False
        assert str(schema.STORE_PATH) == os.path.abspath(os.environ["ZMEM_STORE"]) or \
            os.path.samefile(schema.STORE_PATH, os.environ["ZMEM_STORE"]), schema.STORE_PATH
        self._schema = schema
        # Build the store with CURRENT code (v13).
        self._run(["init"])
        self._run(["add", "--namespace", "project:fwd", "--type", "fact",
                   "--content", "v13-created row", "--signal", "test",
                   "--json"])

    def tearDown(self):
        import importlib
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        importlib.reload(self._schema)  # restore patched constants
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _run(args):
        import subprocess
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_older_client_stores_and_recalls_on_v13_store(self):
        # Simulate the older client in-process: SUPPORTED pinned to 12 with
        # the forward-compat window at 13.
        from storelib.schema import connect, _prepare_store
        from storelib.write import WriteResult, add_memory
        schema = self._schema

        schema.SUPPORTED_SCHEMA_VERSION = 12
        schema._schema_compat._warned = False
        conn = connect()
        _prepare_store(conn)
        res = add_memory(
            conn, namespace="project:fwd", type_="fact",
            content="row stored by a simulated v12-lineage client",
            signal="test",
        )
        # C-review item 3: pin the result shape explicitly.
        self.assertIsInstance(res, WriteResult)
        self.assertEqual(len(res), 36)
        self.assertFalse(res.deduped)
        self.assertEqual(res.warnings, [])
        row = conn.execute(
            "SELECT content FROM memory WHERE id=?", (str(res),)).fetchone()
        self.assertIn("simulated v12-lineage", row["content"])
        # And the store stays at 13 (the older client must NOT downgrade it).
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, "13")
        conn.close()

    def test_cli_refusal_message_is_actionable(self):
        # A store ABOVE the ceiling refuses with the update-or-override hint.
        import sqlite3
        store = os.environ["ZMEM_STORE"]
        conn = sqlite3.connect(store)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(self.schema_supported() + 1),))
        conn.commit()
        conn.close()
        r = self._run(["stats"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", r.stderr)
        self.assertIn("Update this plugin", r.stderr)

    @staticmethod
    def schema_supported():
        from schema_meta import SUPPORTED_SCHEMA_VERSION
        return SUPPORTED_SCHEMA_VERSION


class DoctorCompatGradingTest(unittest.TestCase):
    """Doctor grades the forward-compat band WARN, not fail (reviewer
    item 1: doctor must agree with the writer gate)."""

    def setUp(self):
        import importlib
        import storelib.schema as schema
        importlib.reload(schema)
        self._schema = schema
        schema._schema_compat._warned = False
        self._tmp = tempfile.mkdtemp(prefix="zmem-doctor-grade-")

    def tearDown(self):
        import importlib
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        importlib.reload(self._schema)
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

    def _grade(self, store_version: int) -> dict:
        import doctor as doctor_mod
        saved_cur = doctor_mod.CURRENT_SCHEMA_VERSION
        saved_ceiling = doctor_mod.COMPAT_CEILING
        doctor_mod.CURRENT_SCHEMA_VERSION = 12
        doctor_mod.COMPAT_CEILING = 13
        try:
            store = Path(self._tmp) / f"store-{store_version}.sqlite"
            conn = sqlite3.connect(str(store))
            try:
                conn.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?)",
                    (str(store_version),))
                conn.commit()
            finally:
                conn.close()
            return doctor_mod._check_schema(
                store, {"details": {"store_write": True}})
        finally:
            doctor_mod.CURRENT_SCHEMA_VERSION = saved_cur
            doctor_mod.COMPAT_CEILING = saved_ceiling

    def test_band_store_warns_not_fails(self):
        check = self._grade(13)  # SUPPORTED 12 < 13 <= ceiling 13
        self.assertEqual(check["status"], "warn", check)
        self.assertEqual(check["details"]["compat_ceiling"], 13)

    def test_above_ceiling_store_fails_with_actionable_hint(self):
        check = self._grade(14)
        self.assertEqual(check["status"], "fail", check)
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", check["summary"])

    def test_current_store_passes(self):
        check = self._grade(12)
        self.assertEqual(check["status"], "pass", check)


if __name__ == "__main__":
    unittest.main(verbosity=2)
