"""Read-only opens create no WAL/journal sidecars (issue #345).

Covers storelib.readonly.open_readonly (arm A immutable / arm B private
residue copy), the converted cli and doctor surfaces end to end, and the
load-bearing reason the deliberately-kept WAL-aware opens (query-rewrite,
source scans) are NOT converted: a plain ``mode=ro`` connection sees
committed-but-uncheckpointed WAL rows, and those surfaces need exactly that.

Scratch stores are built through the production cold-open path
(``schema.connect()`` + ``schema._prepare_store``), never the operator
store; ``ZMEM_STORE``/``ZMEM_DATA`` are pinned for subprocess legs and for
any import-time resolution. RED at base, GREEN after the fix.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
TESTS_DIR = Path(__file__).resolve().parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import _store_guard  # noqa: F401  (issue #244: never the operator's real store)


def _pin_env(store_dir: Path) -> dict:
    for key in [k for k in os.environ if k.startswith("ZMEM_")]:
        del os.environ[key]
    os.environ["ZMEM_STORE"] = str(store_dir / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(store_dir)
    os.environ["ZMEM_MODELS_DIR"] = str(store_dir / "no-such-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return dict(os.environ)


def _sidecars(directory: Path):
    return sorted(
        p.name for p in directory.iterdir()
        if p.is_file() and (p.name.endswith("-wal") or p.name.endswith("-shm"))
    )


_TEMPLATE: Path | None = None
_TEMPLATE_DIR: Path | None = None


def _template_store() -> Path:
    """Build ONE quiesced WAL store via the production cold-open path.

    storelib freezes STORE_PATH at import, so the production builder can
    only ever target one directory per process — every test copies this
    template instead of re-running the builder.
    """
    global _TEMPLATE, _TEMPLATE_DIR
    if _TEMPLATE is None:
        import sqlite3

        _TEMPLATE_DIR = Path(tempfile.mkdtemp(prefix="zmem-readonly-tpl-"))
        _pin_env(_TEMPLATE_DIR)
        from storelib import schema

        conn = schema.connect()
        try:
            schema._prepare_store(conn)
            conn.execute(
                "INSERT INTO memory (id, type, content, tags, confidence, "
                "signal, source_ref, namespace, valid_from, ingestion_ts) "
                "VALUES ('00000000-0000-4000-8000-000000000001', 'lesson', "
                "'committed row one', '', 0.9, 'grounded', 'test', "
                "'test:readonly', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')")
            conn.commit()
        finally:
            conn.close()
        _TEMPLATE = _TEMPLATE_DIR / "store.sqlite"
        assert _sidecars(_TEMPLATE_DIR) == [], "template must be quiesced"
    return _TEMPLATE


def _build_store(store_dir: Path, journal_wal: str = "wal") -> Path:
    """A fresh copy of the quiesced template; 'delete' flips journal mode."""
    import sqlite3

    store_dir.mkdir(parents=True, exist_ok=True)
    store = store_dir / "store.sqlite"
    shutil.copyfile(_template_store(), store)
    if journal_wal == "delete":
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
            conn.commit()
        finally:
            conn.close()
    return store


class ReadonlyNoSidecarsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # storelib freezes STORE_PATH at FIRST import, and several test
        # bodies import storelib.readonly at their top — so the environment
        # must be pinned and the template built BEFORE any test runs. This
        # is the single import-time-correct ordering for this file.
        _template_store()

    @classmethod
    def tearDownClass(cls):
        if _TEMPLATE_DIR is not None:
            shutil.rmtree(_TEMPLATE_DIR, ignore_errors=True)

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="zmem-test-readonly-"))

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_arm_a_quiesced_wal_creates_no_sidecars(self):
        import contextlib
        import io

        from storelib import readonly

        store = _build_store(self.scratch / "quiesced")
        self.assertEqual(_sidecars(store.parent), [])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            handle = readonly.open_readonly(store)
            try:
                rows = handle.conn.execute(
                    "SELECT count(*) FROM memory").fetchone()
                self.assertEqual(rows[0], 1)
            finally:
                handle.close()
        self.assertEqual(
            _sidecars(store.parent), [],
            "arm A must not materialize -wal/-shm beside the store")
        self.assertNotIn(
            "private snapshot", err.getvalue(),
            "a quiesced store must take arm A (immutable), not the copy arm")

    def test_arm_a_delete_journal_store_creates_no_sidecars(self):
        from storelib import readonly

        store = _build_store(self.scratch / "delete-mode", journal_wal="delete")
        handle = readonly.open_readonly(store)
        try:
            handle.conn.execute("SELECT count(*) FROM memory").fetchone()
        finally:
            handle.close()
        self.assertEqual(_sidecars(store.parent), [])

    def test_arm_b_wal_residue_reads_private_copy(self):
        from storelib import readonly

        store = _build_store(self.scratch / "residue")
        # Simulate residue: any -wal file (even zero-length) selects arm B.
        Path(str(store) + "-wal").write_bytes(b"")
        handle = readonly.open_readonly(store)
        try:
            rows = handle.conn.execute("SELECT count(*) FROM memory").fetchone()
            self.assertEqual(rows[0], 1)
        finally:
            handle.close()
        # The operator's directory gained nothing but the (pre-existing) wal.
        self.assertEqual(
            sorted(p.name for p in store.parent.iterdir()),
            ["store.sqlite", "store.sqlite-wal"])

    def test_arm_b_journal_residue_never_reads_immutable(self):
        import contextlib
        import io

        from storelib import readonly

        store = _build_store(self.scratch / "journal-residue")
        # A hot rollback journal (any bytes) must force the copy arm: with
        # immutable=1 SQLite would skip journal recovery and could expose
        # uncommitted pages already spilled into the main file (plan-critic
        # round-1 probe). A syntactically invalid journal is treated as
        # stale by recovery, so the copy read still returns committed data —
        # the arm choice is pinned by the stderr snapshot notice.
        Path(str(store) + "-journal").write_bytes(b"not-a-journal")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            handle = readonly.open_readonly(store)
            try:
                rows = handle.conn.execute(
                    "SELECT count(*) FROM memory").fetchone()
                self.assertEqual(rows[0], 1)
            finally:
                handle.close()
        self.assertEqual(
            _sidecars(store.parent), [],
            "journal residue must not be read in place")
        self.assertIn(
            "private snapshot", err.getvalue(),
            "journal residue must select arm B (the private copy), never an "
            "immutable in-place read")

    def test_arm_b_live_writer_snapshot_sees_committed_wal_rows(self):
        import sqlite3

        from storelib import readonly

        store_dir = self.scratch / "live-writer"
        store = _build_store(store_dir)
        writer = sqlite3.connect(str(store), timeout=5.0)
        try:
            writer.execute(
                "INSERT INTO memory (id, type, content, tags, confidence, "
                "signal, source_ref, namespace, valid_from, ingestion_ts) "
                "VALUES ('00000000-0000-4000-8000-000000000002', 'lesson', "
                "'committed row two', '', 0.9, 'grounded', 'test', "
                "'test:readonly', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')")
            writer.commit()
            self.assertTrue(Path(str(store) + "-wal").exists())
            handle = readonly.open_readonly(store)
            try:
                rows = handle.conn.execute(
                    "SELECT count(*) FROM memory").fetchone()
                # The private copy carries the committed WAL frames, so the
                # snapshot includes both rows.
                self.assertEqual(rows[0], 2)
            finally:
                handle.close()
            self.assertEqual(
                _sidecars(store_dir), ["store.sqlite-shm", "store.sqlite-wal"],
                "arm B must not add files beside a live writer's sidecars")
        finally:
            writer.close()

    def test_arm_b_copy_failure_refuses_and_leaves_no_original_sidecars(self):
        from storelib import readonly

        store = _build_store(self.scratch / "copy-fails")
        Path(str(store) + "-wal").write_bytes(b"")
        with mock.patch.object(
                readonly.shutil, "copyfile",
                side_effect=OSError("simulated copy failure")):
            with self.assertRaises(OSError):
                readonly.open_readonly(store)
        self.assertEqual(
            sorted(p.name for p in store.parent.iterdir()),
            ["store.sqlite", "store.sqlite-wal"])

    def test_classify_state_command_end_to_end_no_sidecars(self):
        store = _build_store(self.scratch / "cli-cmd")
        env = _pin_env(store.parent)
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "store.py"),
             "classify-state", "--dry-run", "--json"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertEqual(_sidecars(store.parent), [])

    def test_doctor_expiring_command_end_to_end_no_sidecars(self):
        store = _build_store(self.scratch / "doctor-cmd")
        env = _pin_env(store.parent)
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "doctor.py"),
             "--expiring", "7d", "--store", str(store)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertEqual(_sidecars(store.parent), [])

    def test_deliberate_mode_ro_open_still_sees_live_wal_rows(self):
        """PRESERVING pin for the kept WAL-aware surfaces (query-rewrite,
        source scans): a plain mode=ro connection observes committed WAL
        rows, which is exactly why those sites are not converted."""
        import sqlite3

        store = _build_store(self.scratch / "live-visibility")
        writer = sqlite3.connect(str(store), timeout=5.0)
        try:
            writer.execute(
                "INSERT INTO memory (id, type, content, tags, confidence, "
                "signal, source_ref, namespace, valid_from, ingestion_ts) "
                "VALUES ('00000000-0000-4000-8000-000000000003', 'lesson', "
                "'wal-visible row', '', 0.9, 'grounded', 'test', "
                "'test:readonly', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')")
            writer.commit()
            conn = sqlite3.connect(
                store.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("SELECT count(*) FROM memory").fetchone()
                self.assertEqual(rows[0], 2)
            finally:
                conn.close()
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
