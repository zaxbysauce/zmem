"""Read-only opens create no WAL/journal sidecars (issue #345).

Covers storelib.readonly.open_readonly (arm A immutable / arm B private
residue copy), the converted cli and doctor surfaces end to end (query-rewrite
on the default snapshot arm; the hook's source-exists probes shallow), and
the load-bearing reason the deliberately-kept
WAL-aware opens (source scans and the other documented keeps) are NOT
converted: a plain ``mode=ro`` connection sees
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

# Pin the store env BEFORE the isolation guard AND before storelib can be
# imported (storelib freezes STORE_PATH at first import — PR #422 review
# 422R-014/cubic: the guard import ran first and made the file exit 1 on
# operator boxes whose real default store exists).
_SHARED_PIN_DIR = Path(tempfile.mkdtemp(prefix="zmem-readonly-pin-"))
os.environ["ZMEM_STORE"] = str(_SHARED_PIN_DIR / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SHARED_PIN_DIR)
os.environ["ZMEM_MODELS_DIR"] = str(_SHARED_PIN_DIR / "no-such-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

import _store_guard  # noqa: F401,E402  (issue #244: never the operator's store)


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
_SAVED_ENV: dict | None = None


def _template_store() -> Path:
    """Build ONE quiesced WAL store via the production cold-open path.

    storelib freezes STORE_PATH at import, so the production builder can
    only ever target one directory per process — every test copies this
    template instead of re-running the builder.
    """
    global _TEMPLATE, _TEMPLATE_DIR, _SAVED_ENV
    if _TEMPLATE is None:
        if _SAVED_ENV is None:
            _SAVED_ENV = dict(os.environ)
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
        # Restore the process environment: _pin_env deletes every ZMEM_*
        # variable and pins STORE_PATH to the (now removed) template dir —
        # without this restore, subsequent in-process tests in the same
        # interpreter resolve a deleted store (PR #422 review, cubic
        # test_readonly_no_sidecars-2).
        if _SAVED_ENV is not None:
            os.environ.clear()
            os.environ.update(_SAVED_ENV)

    def test_staging_dir_removed_on_close_and_refusal_path(self):
        """PR #422 review (F-03/422R-010): the arm-B staging copy must be
        GONE after handle.close(), and a refusal (shallow on residue) must
        stage nothing — the mutation probe (no-op _discard_stage) that stayed
        10/10 green is closed by asserting the filesystem, not the return."""
        import glob
        import os as _os
        store = _build_store(self.scratch / "staged.sqlite")
        (Path(str(store) + "-wal")).write_bytes(b"")
        before = set(glob.glob(
            os.path.join(tempfile.gettempdir(), "zmem-readonly-copy-*")))
        from storelib import readonly
        handle = readonly.open_readonly(store)
        stage_dir = handle._stage
        self.assertIsNotNone(stage_dir, "arm B must stage a copy")
        handle.close()
        self.assertFalse(
            stage_dir.exists(),
            "the staging copy must be removed by close(): %s" % stage_dir)
        # Refusal path: shallow on residue raises and stages NOTHING.
        with self.assertRaises(readonly.ReadOnlyStoreError):
            readonly.open_readonly(store, shallow=True)
        after = set(glob.glob(
            os.path.join(tempfile.gettempdir(), "zmem-readonly-copy-*")))
        self.assertEqual(
            after - before, set(),
            "a refused shallow open must stage no directory")

    def test_torn_copy_refuses_with_readonly_store_error(self):
        """PR #422 review (F-03/422R-010): a torn (non-sqlite) residue copy
        refuses with ReadOnlyStoreError — the error class the cli.py catch
        maps to the documented "store unreadable" exit."""
        store = _build_store(self.scratch / "torn.sqlite")
        # A genuinely torn copy: truncate the main file mid-page (quick_check
        # raises "database disk image is malformed"). SQLite's recovery is
        # lenient with merely INVALID -wal content (it resets the header), so
        # the fixture corrupts the MAIN file the copy is staged from.
        raw = store.read_bytes()
        store.write_bytes(raw[: len(raw) // 2])
        (Path(str(store) + "-wal")).write_bytes(b"")
        from storelib import readonly
        try:
            handle = readonly.open_readonly(store)
            handle.close()
            self.fail("a torn residue copy must raise ReadOnlyStoreError")
        except readonly.ReadOnlyStoreError:
            pass

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
            # Snapshot verification must never materialize the entire database.
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
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
        # Hermeticity: doctor's claude-native-memory check reads the REAL
        # ~/.claude state and FAILS when it is absent (CI runners, sandboxed
        # HOME). The check's own documented override makes this leg depend
        # only on the scratch store.
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "doctor.py"),
             "--expiring", "7d", "--store", str(store)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertEqual(_sidecars(store.parent), [])

    def test_lock_aware_open_observes_non_wal_exclusive_lock(self):
        """Fail-closed surfaces (doctor's expiry scan, issue #342): on a
        NON-WAL store, lock_aware=True keeps a plain mode=ro open so an
        EXCLUSIVE holder fails the read instead of being silently bypassed
        by immutable/copy; on a quiesced WAL store it stays sidecar-free."""
        import contextlib
        import io
        import sqlite3

        from storelib import readonly

        store = _build_store(self.scratch / "lock-aware", journal_wal="delete")
        holder = sqlite3.connect(str(store), timeout=5.0)
        try:
            holder.execute("PRAGMA locking_mode=EXCLUSIVE")
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("SELECT COUNT(*) FROM memory").fetchone()
            with self.assertRaises(sqlite3.OperationalError):
                ro = readonly.open_readonly(store, lock_aware=True)
                try:
                    ro.conn.execute(
                        "SELECT count(*) FROM memory").fetchone()
                finally:
                    ro.close()
        finally:
            holder.rollback()
            holder.close()
        # Quiesced WAL store + lock_aware: still the sidecar-free arm A.
        wal_store = _build_store(self.scratch / "lock-aware-wal")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            handle = readonly.open_readonly(wal_store, lock_aware=True)
            try:
                handle.conn.execute("SELECT count(*) FROM memory").fetchone()
            finally:
                handle.close()
        self.assertEqual(_sidecars(wal_store.parent), [])
        self.assertNotIn("private snapshot", err.getvalue())

    def test_deliberate_mode_ro_open_still_sees_live_wal_rows(self):
        """PRESERVING pin for the kept WAL-aware surfaces (source scans and
        the other documented keeps): a plain mode=ro connection observes
        committed WAL rows, which is exactly why those sites are not
        converted."""
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


    def _open_with_late_residue(self, **kwargs):
        import contextlib
        import glob
        import io
        import os
        import sqlite3 as real_sqlite3

        from storelib import readonly

        store = _build_store(self.scratch / "late-residue")
        armed = {"fired": False}

        class _LateWalSqlite3:
            def __getattr__(self, name):
                return getattr(real_sqlite3, name)

            def connect(self, *args, **kwargs):
                if (not armed["fired"] and args and isinstance(args[0], str)
                        and "immutable=1" in args[0]):
                    armed["fired"] = True
                    Path(str(store) + "-wal").write_bytes(b"")
                return real_sqlite3.connect(*args, **kwargs)

        # Hermeticity (delta re-gate r2): assert ONLY the staging this call
        # performed — a spy on mkdtemp, never a glob of the global
        # zmem-readonly-copy-* namespace (a concurrent process's legitimate
        # staging dir must not false-fail these legs).
        made_stages = []
        real_mkdtemp = readonly.tempfile.mkdtemp

        def spy_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            made_stages.append(Path(path))
            return path

        shim = _LateWalSqlite3()
        err = io.StringIO()
        outcome = {}
        with mock.patch.object(readonly, "sqlite3", shim), \
                mock.patch.object(readonly.tempfile, "mkdtemp", spy_mkdtemp), \
                contextlib.redirect_stderr(err):
            try:
                handle = readonly.open_readonly(store, **kwargs)
                outcome["handle"] = handle
            except readonly.ReadOnlyStoreError as exc:
                outcome["error"] = exc
        outcome["stages"] = made_stages
        outcome["stderr"] = err.getvalue()
        return outcome

    def test_default_falls_through_to_arm_b(self):
        outcome = self._open_with_late_residue()
        self.assertNotIn("error", outcome,
                         "default callers fall through to arm B")
        handle = outcome["handle"]
        self.assertEqual(
            len(outcome["stages"]), 1,
            "arm B must stage exactly one copy for this call")
        self.assertIsNotNone(handle._stage, "arm B must have staged a copy")
        self.assertEqual(
            Path(handle._stage), outcome["stages"][0],
            "the recorded stage must be this call's own staging dir")
        self.assertIn("private snapshot", outcome["stderr"])
        stage_dir = handle._stage
        handle.close()
        self.assertFalse(
            stage_dir.exists(),
            "close() must remove the staging copy: %s" % stage_dir)

    def test_shallow_late_residue_refuses_and_stages_nothing(self):
        outcome = self._open_with_late_residue(shallow=True)
        self.assertIn("error", outcome, "shallow must refuse late residue")
        self.assertIn("shallow read refuses", str(outcome["error"]))
        self.assertEqual(
            outcome["stages"], [],
            "a refused shallow open must stage no directory")

    def test_lock_aware_late_residue_refuses(self):
        outcome = self._open_with_late_residue(lock_aware=True)
        self.assertIn("error", outcome, "lock_aware must refuse late residue")
        self.assertIn("lock-aware fail-closed", str(outcome["error"]))
        self.assertEqual(outcome["stages"], [])

    def test_stale_staging_sweep_reaps_aged_dirs_only(self):
        """Delta re-gate (final-critic pin a): the staging sweeper must reap
        an aged zmem-readonly-copy-* dir at open (the hard-kill residue the
        close/except cleanup never sees) while leaving fresh dirs alone —
        a no-op _sweep_stale_stages mutant went 12/12 green before this."""
        import os as _os
        import time
        import uuid
        store = _build_store(self.scratch / "sweep.sqlite")
        (Path(str(store) + "-wal")).write_bytes(b"")
        tmp = Path(tempfile.gettempdir())
        # Per-process unique names (delta re-gate r2): two concurrent runs
        # of this leg must not collide on the shared TEMP namespace.
        tag = uuid.uuid4().hex[:8]
        aged = tmp / ("zmem-readonly-copy-%s-aged" % tag)
        fresh = tmp / ("zmem-readonly-copy-%s-fresh" % tag)
        aged.mkdir(parents=True, exist_ok=True)
        fresh.mkdir(parents=True, exist_ok=True)
        old = time.time() - 2 * 3600
        _os.utime(aged, (old, old))
        try:
            from storelib import readonly
            handle = readonly.open_readonly(store)
            handle.close()
            self.assertFalse(
                aged.exists(),
                "the sweeper must reap aged staging dirs at open")
            self.assertTrue(
                fresh.exists(), "the sweeper must not reap fresh dirs")
        finally:
            shutil.rmtree(aged, ignore_errors=True)
            shutil.rmtree(fresh, ignore_errors=True)

    def test_copy_order_refuses_moving_store(self):
        """Delta re-gate (final-critic pin c): the copy-order consistency
        check must refuse (after bounded retries) when the main file keeps
        moving during the staged snapshot — a `moved = False` mutant served
        a pre-checkpoint snapshot 12/12 green before this. Deterministic
        seam: the .verify copy is dirtied so it always differs from the
        first main copy."""
        import shutil as real_shutil
        store = _build_store(self.scratch / "moving.sqlite")
        (Path(str(store) + "-wal")).write_bytes(b"")
        from storelib import readonly
        real_copyfile = real_shutil.copyfile

        def diverging_copyfile(src, dst, **kwargs):
            real_copyfile(src, dst, **kwargs)
            if str(dst).endswith(".verify"):
                with open(dst, "ab") as f:
                    f.write(b"\x00diverge")

        with mock.patch.object(readonly.shutil, "copyfile",
                               diverging_copyfile):
            with self.assertRaises(readonly.ReadOnlyStoreError) as ctx:
                handle = readonly.open_readonly(store)
                handle.close()
        self.assertIn(
            "kept changing while the private snapshot was staged",
            str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
