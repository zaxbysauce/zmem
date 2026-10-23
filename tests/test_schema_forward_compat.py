"""Forward-compat schema gate tests (issue #65 follow-up).

The gate: a client refuses a store ABOVE FORWARD_COMPAT_SCHEMA_VERSION,
proceeds with a one-time stderr NOTICE for the additive window between
SUPPORTED and FORWARD_COMPAT, and honors ZMEM_ALLOW_NEWER_SCHEMA=1 as an
explicit operator override above the ceiling. Defaults keep today's
behavior (FORWARD_COMPAT == SUPPORTED -> anything newer refuses), so the
window only opens when a maintenance release of an older line extends it.

Also pins the real-world scenario that motivated this: an OLDER client
(patch its SUPPORTED constant to 12) must still be able to store and recall
ordinary memories on the current v14 store.  The test does not claim that
every v14 surface is usable by that older client; it covers the additive
memory-table path only.

Runs standalone: python tests/test_schema_forward_compat.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        # v16 (#175) deliberately held a ONE-STEP gap (the entity-kinds
        # rebuild is not additive, so FORWARD stayed 15 while SUPPORTED was
        # 16). v18 (#178) is purely additive (new side table + index, no
        # memory column or constraint change), so the window REOPENS AT
        # PARITY: FORWARD == SUPPORTED and a store above SUPPORTED (v18+)
        # still refuses exactly as before.
        self.assertEqual(
            self.schema.FORWARD_COMPAT_SCHEMA_VERSION,
            self.schema.SUPPORTED_SCHEMA_VERSION)
        with self.assertRaises(RuntimeError) as cm:
            self._decide(self.schema.SUPPORTED_SCHEMA_VERSION + 1)
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", str(cm.exception))

    def test_additive_window_proceeds_with_notice(self):
        # Simulate an older client (SUPPORTED=12) on the additive v13 store.
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema.FORWARD_COMPAT_SCHEMA_VERSION = 13
        self.schema._schema_compat._warned = False
        self.assertEqual(self._decide(13), "compat")
        # One-time NOTICE per process.
        self.assertEqual(self._decide(13), "compat")

    def test_above_ceiling_refuses_without_override(self):
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema.FORWARD_COMPAT_SCHEMA_VERSION = 13
        self.schema._schema_compat._warned = False
        with self.assertRaises(RuntimeError):
            self._decide(14)

    def test_env_override_admits_above_ceiling(self):
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema.FORWARD_COMPAT_SCHEMA_VERSION = 13
        self.schema._schema_compat._warned = False
        os.environ["ZMEM_ALLOW_NEWER_SCHEMA"] = "1"
        try:
            self.assertEqual(self._decide(14), "compat")
        finally:
            os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

    def test_additive_window_resets_notice_guard(self):
        """Issue #137/#172: the additive-window NOTICE stays once-per-process
        (explicit reset-before AND reset-after, per the issue), and the
        belief side tables being present changes NEITHER schema constant —
        SUPPORTED and FORWARD_COMPAT both stay 14 (the belief DDL is an
        additive, version-neutral migration)."""
        import importlib
        import io
        from contextlib import redirect_stderr
        import schema_meta as meta
        importlib.reload(meta)
        # reset-BEFORE: isolate this test's NOTICE from any prior fire.
        self.schema._schema_compat._warned = False
        self.schema.SUPPORTED_SCHEMA_VERSION = 12
        self.schema.FORWARD_COMPAT_SCHEMA_VERSION = 13
        first, second = io.StringIO(), io.StringIO()
        with redirect_stderr(first):
            self.assertEqual(self._decide(13), "compat")
        with redirect_stderr(second):
            self.assertEqual(self._decide(13), "compat")
        self.assertEqual(first.getvalue().count("NOTICE"), 1,
                         "the additive-window NOTICE must print exactly once")
        self.assertEqual(second.getvalue(), "",
                         "a second decide must not re-notice in-process")
        # reset-AFTER: leave the shared guard clean for sibling tests.
        self.schema._schema_compat._warned = False
        # A migrated store (belief tables present) must still be a v14 store
        # with both constants pinned at 14.
        with tempfile.TemporaryDirectory(prefix="zmem-additive-137-") as tmp:
            conn = sqlite3.connect(os.path.join(tmp, "store.sqlite"))
            try:
                self.schema.init_db(conn)
                self.schema.migrate(conn)
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("belief_head", tables,
                              "additive belief tables exist without a "
                              "version bump")
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version, str(meta.SUPPORTED_SCHEMA_VERSION))
            finally:
                conn.close()
        importlib.reload(self.schema)
        # v16 (#175): SUPPORTED advanced 15 -> 16 (entity kinds v2 rebuild);
        # FORWARD deliberately stayed 15 — a v15 client refused a v16 store.
        # v18 (#178): the additive `event`/`memory_occurrence` release moves
        # SUPPORTED to 18 and REOPENS the additive window at parity
        # (FORWARD 15 -> 18) — an older maintenance client may raise
        # FORWARD_COMPAT (never SUPPORTED) to 18 and keep working on a v18
        # store; clients still at FORWARD=15 refuse a v18 store.
        # (Renumbered from 17 at rebase: #179 owns 17 as stamp-only.)
        self.assertEqual(self.schema.SUPPORTED_SCHEMA_VERSION, 19)
        self.assertEqual(self.schema.FORWARD_COMPAT_SCHEMA_VERSION, 19)
        self.assertEqual(meta.SUPPORTED_SCHEMA_VERSION, 19)
        self.assertEqual(meta.FORWARD_COMPAT_SCHEMA_VERSION, 19)


    def _build_pre_v18_store(self):
        """Build an isolated v16 store via the real CLI; returns its path."""
        tmp = tempfile.mkdtemp(prefix="zmem-v15onv16-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = os.path.join(tmp, "store.sqlite")
        saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")}
        os.environ["ZMEM_STORE"] = store
        os.environ["ZMEM_DATA"] = tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        # Reload AFTER the env pin: STORE_PATH freezes at import.
        import importlib
        importlib.reload(self.schema)
        self.schema._schema_compat._warned = False

        def _restore():
            importlib.reload(self.schema)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "init"],
            capture_output=True, text=True, timeout=180,
            env={**os.environ},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return store

    def _store_version(self, store) -> str:
        conn = sqlite3.connect(store)
        try:
            return conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_v15_client_refuses_v16_store_with_actionable_message(self):
        """Issue #175 AC4: a v15 client (SUPPORTED=15, FORWARD=15) refuses a
        newer upgrade-required store with the existing actionable message.
        Both imported bindings (storelib.schema and schema_meta) are patched,
        restored by the patch context managers. v18 (#178): the store the
        CURRENT code builds is v18 — still above a v15 client's ceiling, so
        the refusal contract is unchanged at the new boundary."""
        import schema_meta as meta
        store = self._build_pre_v18_store()
        self.assertEqual(self._store_version(store), "19")
        with patch.object(self.schema, "SUPPORTED_SCHEMA_VERSION", 15),              patch.object(self.schema, "FORWARD_COMPAT_SCHEMA_VERSION", 15),              patch.object(meta, "SUPPORTED_SCHEMA_VERSION", 15),              patch.object(meta, "FORWARD_COMPAT_SCHEMA_VERSION", 15):
            try:
                self.schema.connect()
                self.fail("a v15 client must refuse a v18 store")
            except RuntimeError as exc:
                self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA=1", str(exc))
                self.assertIn("schema_version", str(exc))
        self.assertEqual(self._store_version(store), "19")

    def test_allow_newer_schema_override_proceeds_without_downgrade(self):
        """Issue #175 AC4: ZMEM_ALLOW_NEWER_SCHEMA=1 proceeds on the newer
        store, warns exactly once, never downgrades or migrates it. v17
        (#178): the built store is v18; a v15 client under the override
        keeps it at 18."""
        import io
        import schema_meta as meta
        from contextlib import redirect_stderr
        store = self._build_pre_v18_store()
        self.assertEqual(self._store_version(store), "19")
        os.environ["ZMEM_ALLOW_NEWER_SCHEMA"] = "1"
        with patch.object(self.schema, "SUPPORTED_SCHEMA_VERSION", 15),              patch.object(self.schema, "FORWARD_COMPAT_SCHEMA_VERSION", 15),              patch.object(meta, "SUPPORTED_SCHEMA_VERSION", 15),              patch.object(meta, "FORWARD_COMPAT_SCHEMA_VERSION", 15):
            self.schema._schema_compat._warned = False
            buf = io.StringIO()
            with redirect_stderr(buf):
                conn = self.schema.connect()
            try:
                self.schema.migrate(conn)
            finally:
                conn.close()
        self.assertEqual(buf.getvalue().count("WARNING"), 1,
                         f"expected exactly one WARNING, got: {buf.getvalue()!r}")
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA=1", buf.getvalue())
        self.assertEqual(self._store_version(store), "19")


class OlderClientOnNewerStoreTest(unittest.TestCase):
    """A v12-lineage client (SUPPORTED=12) uses the current v14 store's
    additive memory-table path without downgrading the store."""

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
        # Build the store with CURRENT code (v14).
        self._run(["init"])
        self._run(["add", "--namespace", "project:fwd", "--type", "fact",
                   "--content", "current-schema-created row", "--signal", "test",
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

    def test_older_client_stores_and_recalls_on_v14_store(self):
        # Simulate the older client in-process: SUPPORTED pinned to 12 with
        # the forward-compat window at 13.
        # v16 (#175): the CURRENT store is now upgrade-required for this
        # 12-lineage client, so the additive-window premise is pinned on a
        # simulated v15 store — the newest version an older-lineage client
        # can still open. Build with current code, hold the marker at 15.
        _sim = sqlite3.connect(os.environ["ZMEM_STORE"])
        _sim.execute("UPDATE meta SET value='15' WHERE key='schema_version'")
        _sim.commit()
        _sim.close()
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
        # And the store stays at 15 (the older client must NOT downgrade it).
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, "15")
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

    def test_maintenance_v14_client_stores_and_recalls_on_v15_store(self):
        # Issue #173 AC4: a MAINTENANCE release of the v14 line (SUPPORTED=14,
        # FORWARD=15) opens the current v15 store, prints exactly one NOTICE,
        # reads and writes ordinary (non-state) rows, and never downgrades the
        # store. This is the additive-window contract at the new version
        # boundary — the same shape the v12-lineage test above pins one
        # boundary earlier.
        import io
        import sqlite3
        from contextlib import redirect_stderr
        import schema_meta
        schema = self._schema
        # v16 (#175): pin the premise on a simulated v15 store — the newest
        # ADDITIVE version. A real v16 store is upgrade-required for a
        # v14-lineage client and is refused at open by design.
        _sim = sqlite3.connect(os.environ["ZMEM_STORE"])
        _sim.execute("UPDATE meta SET value='15' WHERE key='schema_version'")
        _sim.commit()
        _sim.close()
        with patch.object(schema, "SUPPORTED_SCHEMA_VERSION", 14), \
             patch.object(schema, "FORWARD_COMPAT_SCHEMA_VERSION", 15), \
             patch.object(schema_meta, "SUPPORTED_SCHEMA_VERSION", 14), \
             patch.object(schema_meta, "FORWARD_COMPAT_SCHEMA_VERSION", 15):
            schema._schema_compat._warned = False
            buf = io.StringIO()
            with redirect_stderr(buf):
                conn = schema.connect()
                schema._prepare_store(conn)
                from storelib.write import WriteResult, add_memory
                from storelib.recall import recall_memory
                res = add_memory(
                    conn, namespace="project:fwd", type_="fact",
                    content="row stored by a simulated v14 maintenance client",
                    signal="test",
                )
                self.assertIsInstance(res, WriteResult)
                self.assertEqual(len(res), 36)
                self.assertFalse(res.deduped)
                row = conn.execute(
                    "SELECT content FROM memory WHERE id=?",
                    (str(res),)).fetchone()
                self.assertIn("v14 maintenance client", row["content"])
                # An ordinary pre-existing type can be RECALLED too — the
                # additive window is read/write, not read-only.
                rows = recall_memory(
                    conn, query="maintenance client", namespace="project:fwd")
                self.assertTrue(
                    any(str(r["id"]) == str(res) for r in rows),
                    "the v14 maintenance client must be able to recall the row "
                    "it just wrote on a v15 store")
                conn.close()
            notice = buf.getvalue()
        # Exactly ONE notice, and it names the v15-vs-v14 gap.
        self.assertEqual(notice.count("NOTICE"), 1,
                         f"expected exactly one NOTICE, got: {notice!r}")
        self.assertIn("store schema v15 is newer than this client's v14",
                      notice)
        self.assertIn("additive-only schema", notice)
        # #173 PR-review F27 / contract step 11: the notice bytes are pinned
        # EXACTLY (full line + one LF), and a second call under the same
        # captured stream adds nothing — the one-shot guard proven at this
        # 14/15 boundary, not just the older 12/13 one.
        exact = (
            "[zmem] NOTICE: store schema v15 is newer than this client's v14; "
            "memory read/write continues (additive-only schema), but "
            "newer-only features are unavailable until you update this plugin.\n"
        )
        with patch.object(schema, "SUPPORTED_SCHEMA_VERSION", 14), \
             patch.object(schema, "FORWARD_COMPAT_SCHEMA_VERSION", 15):
            schema._schema_compat._warned = False
            first_buf = io.StringIO()
            with redirect_stderr(first_buf):
                self.assertEqual(
                    schema._schema_compat(15, "exact-bytes-probe"), "compat")
            self.assertEqual(first_buf.getvalue(), exact,
                             "the NOTICE must be byte-exact to the contract line")
            # Within the SAME warned-cycle the block above opened, every
            # subsequent call must add NOTHING (the one-shot guard).
            second_buf = io.StringIO()
            with redirect_stderr(second_buf):
                self.assertEqual(
                    schema._schema_compat(15, "second-call-probe"), "compat")
                self.assertEqual(
                    schema._schema_compat(15, "third-call-probe"), "compat")
            self.assertEqual(second_buf.getvalue(), "",
                             "subsequent calls must add no bytes")
        # The store remains v15 — an older client never downgrades it.
        ver = sqlite3.connect(os.environ["ZMEM_STORE"]).execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, "15")


class OlderV16ClientOnV18StoreTest(unittest.TestCase):
    """Issue #178 AC1: a v16 MAINTENANCE client (SUPPORTED=16, FORWARD=17)
    opens the current v18 store through the reopened additive window — one
    NOTICE, ordinary read/write, never a downgrade. The four version
    bindings are patched with unittest.mock.patch.object in the required
    16/17 pattern (SUPPORTED may never be patched below FORWARD here)."""

    def setUp(self):
        import importlib
        import storelib.schema as schema
        # Reload first so a patched SUPPORTED from a prior test can never
        # leak into this class (the sibling-class convention).
        importlib.reload(schema)
        self._tmp = tempfile.mkdtemp(prefix="zmem-178-v16onv17-")
        self._saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")}
        os.environ["ZMEM_STORE"] = os.path.join(self._tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self._tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self._tmp, "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        # Re-load AFTER the env is set: the module binds STORE_PATH at
        # import and connect() must target THIS test's store.
        importlib.reload(schema)
        schema._schema_compat._warned = False
        self._schema = schema
        # Build the store with CURRENT code (v17).
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "init"],
            capture_output=True, text=True, timeout=180,
            env={**os.environ},
        )
        self.assertEqual(r.returncode, 0, r.stderr)

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

    def test_forward18_client_refuses_v19_store(self):
        """issue #288 review (ML5-10/PRR-011): the docs used to claim a
        FORWARD-18 client reads a v19 store with one NOTICE — false. The
        ceiling-equal pair (the real 0.116-era client vs this v19 store)
        is a HARD REFUSE with the standard override hint; pinned so the
        release-note claim can never drift back."""
        from contextlib import redirect_stderr
        import io

        import schema_meta
        schema = self._schema
        store = os.environ["ZMEM_STORE"]
        self.assertEqual(self._store_version(store), "19",
                         "precondition: the current code builds a v19 store")
        with patch.object(schema, "SUPPORTED_SCHEMA_VERSION", 18),              patch.object(schema, "FORWARD_COMPAT_SCHEMA_VERSION", 18),              patch.object(schema_meta, "SUPPORTED_SCHEMA_VERSION", 18),              patch.object(schema_meta, "FORWARD_COMPAT_SCHEMA_VERSION", 18):
            buf = io.StringIO()
            with redirect_stderr(buf):
                try:
                    schema._schema_compat(19, store)
                    self.fail("a FORWARD-18 client must refuse a v19 store")
                except RuntimeError as exc:
                    self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", str(exc))
                    self.assertIn("schema_version", str(exc))
        # Nothing was written to the store by the refused open.
        self.assertEqual(self._store_version(store), "19")

    def test_v16_maintenance_client_stores_and_recalls_on_v18_store(self):
        import io
        import sqlite3
        from contextlib import redirect_stderr

        import schema_meta
        schema = self._schema
        store = os.environ["ZMEM_STORE"]
        self.assertEqual(self._store_version(store), "19",
                         "precondition: the current code builds a v18 store")

        # issue #288: the store this test builds is now v19 (additive
        # applied_ops), so the simulated maintenance client carries a
        # FORWARD ceiling of 19 — the additive-window intent is
        # unchanged (ceiling == the current release).
        with patch.object(schema, "SUPPORTED_SCHEMA_VERSION", 16), \
             patch.object(schema, "FORWARD_COMPAT_SCHEMA_VERSION", 19), \
             patch.object(schema_meta, "SUPPORTED_SCHEMA_VERSION", 16), \
             patch.object(schema_meta, "FORWARD_COMPAT_SCHEMA_VERSION", 19):
            # Reset BEFORE: isolate this test's NOTICE from any prior fire.
            schema._schema_compat._warned = False
            buf = io.StringIO()
            with redirect_stderr(buf):
                # The compat verdict itself, pinned directly (this probe is
                # what fires the one-shot NOTICE into the captured stream;
                # every later call in this block must add nothing).
                self.assertEqual(schema._schema_compat(19, store), "compat")
                conn = schema.connect()
                schema._prepare_store(conn)
                from storelib.write import WriteResult, add_memory
                from storelib.recall import recall_memory
                res = add_memory(
                    conn, namespace="project:fwd178", type_="fact",
                    content="row stored by a simulated v16 maintenance client",
                    signal="test",
                )
                self.assertIsInstance(res, WriteResult)
                self.assertEqual(len(res), 36)
                self.assertFalse(res.deduped)
                row = conn.execute(
                    "SELECT content FROM memory WHERE id=?",
                    (str(res),)).fetchone()
                self.assertIn("v16 maintenance client", row["content"])
                # The additive window is read/write, not read-only.
                rows = recall_memory(
                    conn, query="maintenance client",
                    namespace="project:fwd178")
                self.assertTrue(
                    any(str(r["id"]) == str(res) for r in rows),
                    "the v16 maintenance client must be able to recall the "
                    "row it just wrote on a v18 store")
                conn.close()
            notice = buf.getvalue()
        # Exactly ONE NOTICE names the v17-vs-v16 gap.
        self.assertEqual(notice.count("NOTICE"), 1,
                         f"expected exactly one NOTICE, got: {notice!r}")
        self.assertIn("store schema v19 is newer than this client's v16",
                      notice)
        self.assertIn("additive-only schema", notice)
        # The store remains v18 — the older client never downgrades it.
        self.assertEqual(self._store_version(store), "19")

    @staticmethod
    def _store_version(store) -> str:
        conn = sqlite3.connect(store)
        try:
            return conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()


class Schema18MigrationTest(unittest.TestCase):
    """Issue #178 AC1 (atomicity): a failing v18 DDL statement leaves the
    schema_version marker and the pre-migration table set unchanged."""

    def test_failed_v18_migration_is_atomic(self):
        import storelib.schema as schema

        tmp = tempfile.mkdtemp(prefix="zmem-178-atomic-")
        self.addCleanup(shutil.rmtree, tmp, True)
        store = os.path.join(tmp, "store.sqlite")
        saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
            "ZMEM_MODEL_AUTODOWNLOAD")}
        os.environ["ZMEM_STORE"] = store
        os.environ["ZMEM_DATA"] = tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

        def _restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

        # Build a full store with current code, then hold it at the v17
        # surface (drop the v18 objects, marker back to 17) — the state a
        # pre-migration store is in when the v18 block runs. The pre-state
        # moved 16 -> 17 with the renumber: sibling #179 owns 17 as a
        # stamp-only step, so a v17 store is the real starting point here.
        conn = sqlite3.connect(store)
        try:
            schema.init_db(conn)
            schema.migrate(conn)
            conn.execute("DROP INDEX IF EXISTS idx_memory_occurrence_range")
            conn.execute("DROP TABLE IF EXISTS memory_occurrence")
            conn.execute(
                "UPDATE meta SET value='17' WHERE key='schema_version'")
            conn.commit()

            def snapshot():
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                tables = sorted(r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"))
                return version, tables

            before_version, before_tables = snapshot()
            self.assertEqual(before_version, "17")
            self.assertNotIn("memory_occurrence", before_tables)

            # Inject ONE failing v18 DDL: keep the table statement, break
            # the LAST statement (the index) against a nonexistent table.
            broken_ddl = (
                schema._MEMORY_OCCURRENCE_SCHEMA_DDL[0],
                "CREATE INDEX IF NOT EXISTS idx_memory_occurrence_range "
                "ON no_such_v18_table(occurred_start)",
            )
            with patch.object(schema, "_MEMORY_OCCURRENCE_SCHEMA_DDL",
                              broken_ddl):
                with self.assertRaises(sqlite3.OperationalError):
                    schema.migrate(conn)

            after_version, after_tables = snapshot()
            self.assertEqual(after_version, before_version,
                             "a failed v17 migration must leave the version "
                             "marker below 17")
            self.assertEqual(after_tables, before_tables,
                             "a failed v17 migration must leave the table "
                             "set unchanged (no partial memory_occurrence)")
            self.assertNotIn("memory_occurrence", after_tables)
        finally:
            conn.close()


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
