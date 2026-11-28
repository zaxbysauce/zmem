"""Supp guardrails for the store-recovery work (#506, #504, #507).

Not frozen; authored by the lead in Phase 4 per the approved plan's Test
Plan. Every leg is a guardrail the frozen checks do not carry:

- the crash-window sweep: a kill immediately before EVERY early stamp
  statement still resumes (the frozen C1 pins the v3/v4 windows; this
  sweeps every version block that writes a stamp);
- a shadow table missing while the virtual table remains, on a POPULATED
  store and on an EMPTIED one — the recovery runs whatever the row count;
- an impostor ``memory`` without the index columns — the step is skipped;
- direct ``init_db`` + ``migrate`` on a healthy populated store is
  byte-identical (the zero-write pin);
- a snapshot with its own ``-wal`` — recovered on the staged copy, the
  live store never gains it;
- the compat-window and override stamp policy (unchanged): a
  newer-than-forward destination refuses with exit 2;
  ``ZMEM_ALLOW_NEWER_SCHEMA=1`` lets the operator override;
- the sidecar-free preflight: neither read creates a sidecar; a store with
  residue reads a private copy (arm B notice);
- a missing and an empty destination with orphan sidecars: the ruled
  success lines, no pre-restore backup, the orphan bytes kept in the
  forensic directory;
- the injected fault matrix: an installed-verification failure, a
  permission-hardening failure, a migration-connection failure, a rollback
  that itself fails, a rollback whose own ``integrity_check`` is not ok,
  a safety-copy failure (abort, destination untouched), the F1 wording pin
  (the empty-destination rollback prints the ruled line), and a stash
  that cannot be removed on the success path (reported, restore still OK);
- the dedup-fold branch of ``update`` under concurrent metadata drift
  (the fold merges onto the drifted values);
- a multi-neighbour cluster with a concurrent writer: the cluster
  transaction holds the write lock (a second writer's ``BEGIN IMMEDIATE``
  is refused while the first absorb is in flight);
- the conditional-tombstone CAS in ``_absorb_into_keeper`` (a dead
  neighbour at tombstone time raises and the cluster rolls back);
- the loop-bound regression cases: non-ASCII rows (accented + NFD)
  accepted by the staged proof, a client without sqlite-vec accepting a
  store with ``memory_vec``, an extra UNIQUE index refused, an extra
  trigger refused, an inline ``UNIQUE`` column constraint refused, and a
  table upgraded by ``ALTER TABLE ADD COLUMN`` accepted;
- doctor ``fts-index`` on a healthy store.

Hermetic: every subprocess and in-process leg pins a fresh throwaway store
via the module-top env pin (before any storelib import, per the guard).
"""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
PY = sys.executable

# ---- module-top env pin (before any storelib import) --------------------
_Tmp = Path(tempfile.mkdtemp(prefix="x-store-recovery-supp-"))
_STORE = _Tmp / "store.sqlite"
# TG-4 (the round-1 review comment): the pin must be restored at MODULE
# teardown, and the restore must be ORIGINAL-based: a per-module snapshot
# would capture whatever a sibling module had already pinned. The shared
# helper captures the original values once per process and refcounts the
# pins, so the environment is restored only after the LAST pinning module
# has torn down (a middle module's teardown must not strip a pin a later
# module's tests still need).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_env_pin  # noqa: E402  (shared pin/restore helper)

_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_SESSION",
                "ZMEM_SESSION_ID", "ZMEM_TIER0", "ZMEM_SKILLS_DIRS",
                "ZMEM_PYTHON")
_ENV_TOKEN = _store_env_pin.acquire(_PINNED_KEYS)


def tearDownModule():
    # TG-4 (the round-1 review comment): atexit is PROCESS exit, so a
    # sibling test module imported into the SAME process still inherited
    # these pins. Restore at MODULE teardown -- after this module's legs
    # have finished (they need the pin) and before any sibling runs.
    _store_env_pin.release(_ENV_TOKEN)


@atexit.register
def _release_env_at_exit():
    _store_env_pin.release(_ENV_TOKEN)


os.environ.update({
    "ZMEM_STORE": str(_STORE),
    "ZMEM_DATA": str(_Tmp),
    "ZMEM_BACKUP_DIR": str(_Tmp / "backups"),
    "ZMEM_MODELS_DIR": str(_Tmp / "no-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_TIER0",
            "ZMEM_SKILLS_DIRS", "ZMEM_PYTHON"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(SCRIPTS_DIR))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard; the
#                      wiring census requires it in every test file that
#                      names storelib/store.py — after the env pin, so the
#                      guard sees the scratch store, never the real one)

NS = "project:x-store-recovery-supp"
A_NOW = "2026-11-25T12:00:00Z"


def _child_env(store_dir: Path, store: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(store_dir),
        "ZMEM_BACKUP_DIR": str(store_dir / "backups"),
        "ZMEM_MODELS_DIR": str(store_dir / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_LAUNCHER_WATCHDOG_MS": "60000",
    })
    return env


class SuppBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="supp-leg-"))
        self.store = self.dir / "store.sqlite"
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _add(self, content, *extra, setup=False):
        argv = [PY, "-X", "utf8", str(STORE_PY), "add",
                "--namespace", NS, "--type", "fact", "--content", content,
                "--signal", "test", "--json", *extra]
        r = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True,
                           text=True, env=_child_env(self.dir, self.store),
                           timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return r

    def _store_conn(self):
        conn = sqlite3.connect(str(self.store))
        conn.row_factory = sqlite3.Row
        return conn

    def _live_rows(self, path=None):
        conn = sqlite3.connect(str(path or self.store))
        try:
            return {r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")}
        finally:
            conn.close()


class CrashWindowSweepTest(SuppBase):
    """Every early stamp statement survives a kill immediately before it."""

    def _seed_v1_store(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE memory (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
            "type TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', "
            "source_ref TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL DEFAULT '', "
            "confidence REAL NOT NULL DEFAULT 0.5, signal TEXT NOT NULL DEFAULT 'none', "
            "valid_from TEXT NOT NULL DEFAULT '', superseded_at TEXT, "
            "ingestion_ts TEXT NOT NULL, retrieval_count INTEGER NOT NULL DEFAULT 0, "
            "last_retrieved TEXT)")
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        conn.execute(
            "INSERT INTO memory VALUES ('r1', ?, 'fact', 'crash sweep row one', "
            "'', '', '', 0.9, 'test', '', NULL, ?, 0, NULL)", (NS, A_NOW))
        conn.commit()
        conn.close()

    def test_stamp_statement_sweep_resumes(self):
        # F-006 (swarm-pr-review TF-001): a FRESH v1 store per stamp, and the
        # kill must actually FIRE for every stamp the ladder reaches. The old
        # single-store loop migrated to stamp 19 on the first resume, so
        # iterations 3-19 killed nothing (1/18 legs) while the module
        # docstring claimed a whole-ladder sweep.
        fired, not_reached = [], []
        for i in range(2, 20):
            store_i = self.dir / ("sweep%d_store.sqlite" % i)
            data_i = self.dir / ("sweep%d_data" % i)
            data_i.mkdir(parents=True, exist_ok=True)
            self._seed_v1_store(store_i)
            child = self.dir / ("kill_before_stamp_%d.py" % i)
            child.write_text(
                "import os, sqlite3, sys\n"
                "sys.path.insert(0, %r)\n" % str(SCRIPTS_DIR) +
                "from storelib import schema\n"
                "conn = sqlite3.connect(os.environ['ZMEM_STORE'])\n"
                "target = sys.argv[1]\n"
                "def kill(sql):\n"
                "    if sql.strip().startswith('UPDATE meta SET value=') and target in sql:\n"
                "        os._exit(137)\n"
                "conn.set_trace_callback(kill)\n"
                "schema._prepare_store(conn)\n", encoding="utf-8")
            env = _child_env(data_i, store_i)
            r = subprocess.run([PY, "-X", "utf8", str(child), str(i)],
                               cwd=str(REPO_ROOT), capture_output=True,
                               text=True, env=env, timeout=180)
            if r.returncode == 137:
                fired.append(i)
            else:
                not_reached.append(i)  # stamp i not written on the v1 path
            r2 = subprocess.run(
                [PY, "-X", "utf8", str(STORE_PY), "add", "--namespace", NS,
                 "--type", "fact", "--content", ("resume probe %d" % i),
                 "--signal", "test", "--json"],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                env=env, timeout=180)
            self.assertEqual(r2.returncode, 0,
                             ("stamp %d: kill rc=%d then resume failed: %s"
                              % (i, r.returncode, r2.stderr[-300:])))
            conn = sqlite3.connect(str(store_i))
            try:
                stamp = conn.execute(
                    "SELECT value FROM meta WHERE key="
                    "'schema_version'").fetchone()[0]
            finally:
                conn.close()
            self.assertGreaterEqual(int(stamp), 2, (i, stamp))
        # Every consecutive stamp the ladder writes must have been
        # kill-proven; a gap means a block skipped a stamp or the sweep
        # broke — either is a coverage loss.
        self.assertTrue(fired, "no kill fired at all")
        self.assertEqual(fired, list(range(2, 2 + len(fired))),
                         ("kill gaps in the stamp ladder; fired=%r "
                          "not_reached=%r" % (fired, not_reached)))


class ShadowTableRecoveryTest(SuppBase):
    """Shadow table missing, virtual table present: recovery runs."""

    def _drop_shadow(self, shadow):
        self._add("populated row one for shadow recovery")
        self._add("populated row two for shadow recovery")
        conn = sqlite3.connect(str(self.store))
        conn.execute(f"DROP TABLE {shadow}")
        conn.commit()
        conn.close()

    def test_populated_store_shadow_missing_is_healed(self):
        self._drop_shadow("memory_fts_data")
        r = self._add("post heal row")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        conn = self._store_conn()
        try:
            # F-002/MX-001: a heal through the SURGERY arm (an unconstructible
            # vtable is exactly the dropped-_data shape) must leave the store
            # integrity-clean — the old sqlite_master DELETE of the shadow
            # rows stranded their pages and every later integrity_check
            # reported "Page N: never used", losing the backup safety net.
            integrity = conn.execute(
                "PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok",
                             "surgery-healed store must stay integrity-clean")
            for word in ("populated", "post"):
                hits = conn.execute(
                    "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                    (f'"{word}"',)).fetchone()[0]
                self.assertGreater(hits, 0, word)
        finally:
            conn.close()

    def test_emptied_store_shadow_missing_is_healed(self):
        # emptied: rows removed BEFORE the shadow is dropped (deleting rows
        # fires the search triggers, which would fail on a broken index)
        self._add("emptied heal row")
        conn = sqlite3.connect(str(self.store))
        conn.execute("DELETE FROM memory")
        conn.commit()
        conn.execute("DROP TABLE memory_fts_idx")
        conn.commit()
        conn.close()
        r = self._add("first row after empty heal")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        conn = self._store_conn()
        try:
            # F-002/MX-001: same integrity pin for the emptied-store leg.
            integrity = conn.execute(
                "PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok",
                             "surgery-healed store must stay integrity-clean")
        finally:
            conn.close()


class ImpostorMemoryTest(SuppBase):
    """A memory without the index columns: the step is skipped, no crash."""

    def test_impostor_memory_survives_open(self):
        conn = sqlite3.connect(str(self.store))
        conn.executescript(
            "CREATE TABLE memory (id TEXT PRIMARY KEY, content TEXT); "
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); "
            "INSERT INTO meta VALUES ('schema_version', '19')")
        conn.commit()
        conn.close()
        # NOT self._add(): the add is expected to FAIL here (an int exit
        # code with a message, never a traceback).
        argv = [PY, "-X", "utf8", str(STORE_PY), "add", "--namespace", NS,
                "--type", "fact", "--content", "impostor store row",
                "--signal", "test", "--json"]
        r = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True,
                           text=True, env=_child_env(self.dir, self.store),
                           timeout=180)
        self.assertIsInstance(r.returncode, int)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr, r.stderr[-300:])


    def test_impostor_recovery_step_is_reached_and_changes_nothing(self):
        # TG-3(b) (the round-1 review comment): the impostor leg above only
        # proves the ADD fails. Call the recovery helper DIRECTLY on the
        # impostor connection: it must be REACHED, raise nothing, and leave
        # the impostor's schema untouched (no rebuild attempted).
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executescript(
                "CREATE TABLE memory (id TEXT PRIMARY KEY, content TEXT); "
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); "
                "INSERT INTO meta VALUES ('schema_version', '19')")
            conn.commit()
            before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            cols_before = [r[1] for r in conn.execute("PRAGMA table_info(memory)")]
        finally:
            conn.close()
        import storelib.schema as schema_mod
        conn = sqlite3.connect(str(self.store))
        try:
            schema_mod._recover_search_index(conn)   # must not raise
            after = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            cols_after = [r[1] for r in conn.execute("PRAGMA table_info(memory)")]
        finally:
            conn.close()
        self.assertEqual(after, before,
                         "the impostor store's tables must be untouched")
        self.assertEqual(cols_after, cols_before,
                         "the impostor memory table's columns must be untouched")


    def test_impostor_recovery_is_reached_end_to_end_and_rebuilds_nothing(self):
        # TG-3(b): the direct call above cannot prove the OPEN path reaches
        # the recovery helper. Run the real CLI add against the impostor
        # store with the helper wrapped, and assert (a) the helper was
        # reached, (b) the impostor's schema is unchanged, (c) the add
        # failed cleanly.
        conn = sqlite3.connect(str(self.store))
        try:
            conn.executescript(
                "CREATE TABLE memory (id TEXT PRIMARY KEY, content TEXT); "
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL); "
                "INSERT INTO meta VALUES ('schema_version', '19')")
            conn.commit()
            before = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            cols_before = [r[1] for r in conn.execute("PRAGMA table_info(memory)")]
        finally:
            conn.close()
        import storelib.schema as schema_mod
        original = schema_mod._recover_search_index
        calls = []

        def wrapper(conn_):
            calls.append(1)
            return original(conn_)

        # The CLI runs in a CHILD, so an in-process wrapper is invisible to
        # it: drive the same open sequence the CLI uses, in-process.
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        import storelib
        storelib._refresh_env_state()
        schema_mod._recover_search_index = wrapper
        try:
            # The impostor's own schema makes the open fail cleanly (the
            # index DDL needs the columns the impostor lacks) -- that IS
            # the impostor contract: a controlled refusal, never a rebuild.
            with self.assertRaisesRegex(sqlite3.OperationalError, "namespace"):
                conn = storelib.connect()
                try:
                    storelib.init_db(conn)
                finally:
                    conn.close()
        finally:
            schema_mod._recover_search_index = original
        self.assertTrue(calls, "the open path never reached the recovery helper")
        conn = sqlite3.connect(str(self.store))
        try:
            after = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            cols_after = [r[1] for r in conn.execute("PRAGMA table_info(memory)")]
        finally:
            conn.close()
        # The open creates the (empty) vec0 shadow tables, so the impostor
        # contract is about the SEARCH index: no memory_fts and no search
        # shadow table may appear, and the impostor's own memory table must
        # be untouched.
        self.assertNotIn("memory_fts", after,
                         "the impostor store must not gain a search index")
        for shadow in ("memory_fts_config", "memory_fts_data",
                       "memory_fts_docsize", "memory_fts_idx"):
            self.assertNotIn(shadow, after,
                             "the impostor store must not gain %s" % shadow)
        self.assertEqual(cols_after, cols_before,
                         "the impostor memory table's columns must be untouched")


class StoreEnvPinHelperTest(unittest.TestCase):
    """TG-4: the shared pin helper's lifetime semantics."""

    def setUp(self):
        import _store_env_pin as pin
        self.pin = pin
        self._orig = dict(pin._ORIGINAL)
        self._active = set(pin._ACTIVE)
        self._next = pin._NEXT_TOKEN
        # Isolate the holder state: the modules that hold pins in THIS
        # process would otherwise keep the restore from happening, and the
        # point of the legs is the helper's own semantics.
        pin._ORIGINAL.clear()
        pin._ACTIVE.clear()

    def tearDown(self):
        self.pin._ORIGINAL.clear()
        self.pin._ORIGINAL.update(self._orig)
        self.pin._ACTIVE.clear()
        self.pin._ACTIVE.update(self._active)
        self.pin._NEXT_TOKEN = self._next

    def test_two_holders_of_the_same_keys_stay_independent(self):
        os.environ["ZMEM_PIN_PROBE"] = "original"
        self.addCleanup(os.environ.pop, "ZMEM_PIN_PROBE", None)
        first = self.pin.acquire(("ZMEM_PIN_PROBE",))
        second = self.pin.acquire(("ZMEM_PIN_PROBE",))
        os.environ["ZMEM_PIN_PROBE"] = "pinned"
        self.pin.release(first)
        self.assertEqual(os.environ.get("ZMEM_PIN_PROBE"), "pinned",
                         "the second holder must keep the pin")
        self.pin.release(second)
        self.assertEqual(os.environ.get("ZMEM_PIN_PROBE"), "original",
                         "the last release must restore the original")

    def test_a_new_cycle_captures_a_fresh_original(self):
        # TG-4: a second cycle must capture the CURRENT values, not the
        # first cycle's stale snapshot.
        os.environ["ZMEM_PIN_CYCLE"] = "first"
        self.addCleanup(os.environ.pop, "ZMEM_PIN_CYCLE", None)
        first = self.pin.acquire(("ZMEM_PIN_CYCLE",))
        os.environ["ZMEM_PIN_CYCLE"] = "pinned"
        self.pin.release(first)
        self.assertEqual(os.environ.get("ZMEM_PIN_CYCLE"), "first")
        # the environment legitimately changes between cycles
        os.environ["ZMEM_PIN_CYCLE"] = "second"
        second = self.pin.acquire(("ZMEM_PIN_CYCLE",))
        os.environ["ZMEM_PIN_CYCLE"] = "pinned-again"
        self.pin.release(second)
        self.assertEqual(os.environ.get("ZMEM_PIN_CYCLE"), "second",
                         "a new cycle must restore ITS OWN original")

    def test_the_restore_covers_every_captured_key(self):
        for key, value in (("ZMEM_PIN_A", "orig-a"), ("ZMEM_PIN_B", "orig-b")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        first = self.pin.acquire(("ZMEM_PIN_A",))
        second = self.pin.acquire(("ZMEM_PIN_B",))
        os.environ["ZMEM_PIN_A"] = "pinned-a"
        os.environ["ZMEM_PIN_B"] = "pinned-b"
        self.pin.release(first)
        self.pin.release(second)
        self.assertEqual(os.environ.get("ZMEM_PIN_A"), "orig-a")
        self.assertEqual(os.environ.get("ZMEM_PIN_B"), "orig-b")


class HealthyStoreByteIdentityTest(SuppBase):
    """Direct init_db + migrate on a healthy populated store: no byte write."""

    def test_second_open_is_byte_identical(self):
        self._add("healthy byte identity row")
        self._add("healthy byte identity row two")
        before = self.store.read_bytes()
        driver = self.dir / "second_open.py"
        driver.write_text(
            "import os, sqlite3, sys\n"
            "sys.path.insert(0, %r)\n" % str(SCRIPTS_DIR) +
            "from storelib import schema\n"
            "conn = sqlite3.connect(os.environ['ZMEM_STORE'])\n"
            "schema.init_db(conn)\n"
            "schema.migrate(conn)\n"
            "conn.close()\n", encoding="utf-8")
        r = subprocess.run([PY, "-X", "utf8", str(driver)], cwd=str(REPO_ROOT),
                           capture_output=True, text=True,
                           env=_child_env(self.dir, self.store), timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertEqual(self.store.read_bytes(), before,
                         "a healthy store's main file changed across an open")


class SnapshotOwnWalTest(SuppBase):
    """A snapshot with its own -wal: recovered on the staged copy only."""

    def test_snapshot_wal_never_reaches_the_live_store(self):
        self._add("live control row one")
        self._add("live control row two")
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "backup"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        backup_dir = self.dir / "backups"
        snap = sorted(backup_dir.glob("store-*.sqlite"))[-1]
        # Make a WAL-mode snapshot pair by hand: copy the store, open it RW,
        # write, and copy the files WHILE THE WRITER IS STILL OPEN (closing
        # the last connection checkpoints and deletes the -wal).
        donor = self.dir / "donor.sqlite"
        shutil.copyfile(snap, donor)
        pair = self.dir / "pair.sqlite"
        conn = sqlite3.connect(str(donor))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
            "source_hash, confidence, signal, valid_from, superseded_at, "
            "ingestion_ts, retrieval_count, last_retrieved) "
            "SELECT id || '-walrow', namespace, type, "
            "'content that lived only in the snapshot wal', tags, source_ref, "
            "source_hash, confidence, signal, valid_from, superseded_at, "
            "ingestion_ts, 0, NULL FROM memory LIMIT 1")
        conn.commit()
        shutil.copyfile(donor, pair)
        shutil.copyfile(Path(str(donor) + "-wal"), Path(str(pair) + "-wal"))
        self.assertTrue(Path(str(pair) + "-wal").exists())
        self.assertTrue(Path(str(pair) + "-wal").stat().st_size > 0)
        conn.close()
        restore = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(pair),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=300)
        self.assertEqual(restore.returncode, 0,
                         restore.stdout[-300:] + restore.stderr[-300:])
        self.assertFalse(Path(str(self.store) + "-wal").exists(),
                         "the snapshot's -wal reached the live store")
        rows = self._live_rows()
        self.assertIn("content that lived only in the snapshot wal", rows)


class PreflightSidecarFreeTest(SuppBase):
    """Neither preflight read creates a sidecar; residue reads a copy."""

    def test_preflight_creates_no_sidecar(self):
        self._add("preflight sidecar-free row")
        # quiesce: an ordinary clean close leaves no sidecars
        for suffix in ("-wal", "-shm", "-journal"):
            p = Path(str(self.store) + suffix)
            if p.exists():
                p.unlink()
        before = sorted(p.name for p in self.dir.iterdir())
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from",
             str(self._snapshot()), "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        # a successful restore ends with the success lines and leaves no
        # sidecar (its own verification closes them away); the assertion of
        # interest is the START: no sidecar existed before it began.
        self.assertIn("[zmem] restore: OK", r.stdout)
        # F-013 (TF-002): pin the sidecar claim itself — a restore whose
        # preflight manufactured a -wal/-shm beside THIS quiesced store
        # fails here even when it finished OK. `before` is the quiesced
        # listing; the only new names allowed are the snapshot artifacts.
        after = sorted(p.name for p in self.dir.iterdir())
        permitted = set(before) | {"backups", "snap.sqlite"}
        leaked = [n for n in after
                  if n not in permitted
                  and (n.startswith("store.sqlite-")
                       or ".restore-" in n)]
        self.assertEqual(leaked, [], f"preflight leaked: {leaked}")

    def _snapshot(self):
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "backup"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        return sorted((self.dir / "backups").glob("store-*.sqlite"))[-1]


class DoctorHealthyTest(SuppBase):
    """doctor fts-index passes on a healthy store; no fts check fails.

    The GLOBAL exit code is host state (claude-native-memory, served-drift on
    a dev tree), not store health — the frozen C1 leg already pins that the
    doctor's exit code is not part of the index contract. This supp pin is
    about the index lane only.
    """

    def test_healthy_store_fts_index_passes(self):
        self._add("doctor healthy row")
        r = subprocess.run(
            [PY, "-X", "utf8", str(DOCTOR_PY), "--format", "json"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=300)
        self.assertTrue(r.stdout.strip(), r.stderr[-300:])
        import json
        report = json.loads(r.stdout)
        fts_checks = [c for c in report.get("checks", [])
                      if "fts" in c.get("id", "").lower()]
        self.assertTrue(fts_checks, "no fts-named check in the report")
        for c in fts_checks:
            self.assertEqual(
                c.get("status"), "pass",
                f"{c.get('id')}={c.get('status')}: {c.get('summary')}")
        lanes = report.get("lanes") or report.get("lane_status") or {}
        fts_lane = None
        for key in ("fts",):
            if isinstance(lanes, dict) and key in lanes:
                fts_lane = lanes[key]
        if fts_lane is not None:
            self.assertEqual(
                (fts_lane or {}).get("status")
                if isinstance(fts_lane, dict) else fts_lane, "ok",
                f"fts lane: {fts_lane!r}")


class TombstoneCasPinTest(SuppBase):
    """Phase-4.5 finding 2: the conditional tombstone in _absorb_into_keeper
    is defense-in-depth behind the under-lock CAS (a single-writer BEGIN
    IMMEDIATE cannot normally make it fire), so NO frozen check kills the
    mutation "revert the conditional tombstone". This leg pins the hunk
    directly: a stale (already-superseded) absorbed row must raise and the
    caller's transaction must roll back, never merge."""

    def test_dead_neighbour_at_tombstone_time_raises_and_rolls_back(self):
        self._add("tombstone cas keeper content")
        self._add("tombstone cas neighbour content")
        # storelib/__init__ re-exports the consolidate() FUNCTION, shadowing
        # the submodule name — import the module object itself.
        import importlib
        consolidate_lib = importlib.import_module("storelib.consolidate")
        conn = self._store_conn()
        try:
            keeper = conn.execute(
                "SELECT * FROM memory WHERE superseded_at IS NULL"
                " AND content LIKE 'tombstone cas keeper%'").fetchone()
            nb = conn.execute(
                "SELECT * FROM memory WHERE superseded_at IS NULL"
                " AND content LIKE 'tombstone cas neighbour%'").fetchone()
            self.assertIsNotNone(keeper)
            self.assertIsNotNone(nb)
            # A concurrent writer tombstoned the neighbour between the
            # working-set read and this cluster's tombstone: the stale row
            # object still says live, the table does not.
            conn.execute(
                "UPDATE memory SET superseded_at=?, valid_until=?,"
                " supersede_reason='superseded by another writer'"
                " WHERE id=?", (A_NOW, A_NOW, nb["id"]))
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(RuntimeError) as raised:
                consolidate_lib._absorb_into_keeper(
                    conn, keeper, nb, now=A_NOW)
            self.assertIn("was not live at tombstone time", str(raised.exception))
            conn.rollback()
            # nothing merged: the keeper is untouched and carries no
            # provenance from the dead neighbour
            fresh = conn.execute(
                "SELECT * FROM memory WHERE id=?", (keeper["id"],)).fetchone()
            self.assertNotIn(nb["id"], fresh["merged_from"] or "")
            self.assertEqual(fresh["content"], keeper["content"])
        finally:
            conn.close()


class _SnapshotMixin:
    """take one CLI backup snapshot, return its path."""

    def _snapshot(self, store_dir, store):
        env = _child_env(store_dir, store)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "backup"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=180)
        assert r.returncode == 0, r.stderr[-300:]
        return sorted((Path(store_dir) / "backups").glob("store-*.sqlite"))[-1]


class RestoreSuccessLinesTest(SuppBase, _SnapshotMixin):
    """A missing or empty destination with orphan sidecars: the ruled
    success lines, no pre-restore backup (plan Test Plan item 2)."""

    def _leg(self, make_dest):
        self._add("success lines control row")
        snap = self._snapshot(self.dir, self.store)
        # the destination becomes the empty/missing variant
        for p in [self.store] + [Path(str(self.store) + s)
                                 for s in ("-wal", "-shm", "-journal")]:
            if p.exists():
                p.unlink()
        make_dest(self.store)
        # orphan sidecars with distinctive bytes
        for sfx, blob in (("-wal", b"WALJUNK"), ("-shm", b"SHMJUNK"),
                          ("-journal", b"JRNLJUNK")):
            Path(str(self.store) + sfx).write_bytes(blob)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=_child_env(self.dir, self.store), timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("pre-restore backup skipped", r.stdout + r.stderr)
        self.assertIn("removed stale", r.stdout + r.stderr)
        self.assertIn("restore: OK", r.stdout + r.stderr)
        # no sidecar and no stash remain beside the store
        for sfx in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + sfx).exists(), sfx)
        leftovers = [p.name for p in self.dir.iterdir()
                     if ".restore-" in p.name]
        self.assertEqual(leftovers, [], leftovers)
        # the orphan bytes survive in a forensic directory
        forensic = list((self.dir / "backups").glob("restore-forensic-*"))
        self.assertTrue(forensic, "no forensic directory for orphan sidecars")
        kept = {p.name: p.read_bytes() for p in forensic[0].iterdir()}
        self.assertEqual(kept.get(self.store.name + "-wal"), b"WALJUNK")
        # no pre-restore backup was taken (nothing to preserve)
        self.assertFalse(list((self.dir / "backups").glob(
            "prerestore-*")), "a pre-restore backup was taken")

    def test_empty_destination_with_orphan_sidecars(self):
        self._leg(lambda store: store.write_bytes(b""))

    def test_missing_destination_with_orphan_sidecars(self):
        self._leg(lambda store: None)


class _InProcessRestoreBase(SuppBase, _SnapshotMixin):
    """In-process cmd_restore harness: pins the process storelib at THIS
    leg's throwaway store, applies a patch for the call's duration, and
    captures stdout/stderr without letting an exception escape."""

    def _pinned(self):
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        os.environ["ZMEM_BACKUP_DIR"] = str(self.dir / "backups")
        import storelib
        storelib._refresh_env_state()
        return storelib

    def _run(self, snap, patch=None, unpatch=None):
        import io as _io
        import contextlib as _cl
        # F-012 (MT-001): snapshot the env BEFORE _pinned() mutates it, or
        # the finally-restore below reinstates the mutated values and leaves
        # the process pinned at this leg's (soon rmtree'd) directory.
        saved = {k: v for k, v in os.environ.items()
                 if k.startswith("ZMEM_")}
        storelib = self._pinned()
        import storelib.backup as backup_mod
        out, err = _io.StringIO(), _io.StringIO()
        rc, raised = None, None
        try:
            if patch:
                patch(backup_mod)
            try:
                with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                    rc = backup_mod.cmd_restore(
                        from_path=str(snap), force=True)
            except Exception as e:  # an escaping exception is a failure
                raised, rc = e, None
            finally:
                if unpatch:
                    unpatch(backup_mod)
        finally:
            os.environ.update(saved)
            storelib._refresh_env_state()
        return {"rc": rc, "raised": raised, "out": out.getvalue(),
                "err": err.getvalue()}

    def _no_leftovers(self):
        names = [p.name for p in self.dir.iterdir() if ".restore-" in p.name]
        for sfx in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.store) + sfx).exists(), sfx)
        self.assertEqual(names, [], names)

    @staticmethod
    def _patch_attr(module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)

        def unpatch(_mod=None):
            setattr(module, name, original)

        return unpatch


class RestoreFaultMatrixTest(_InProcessRestoreBase):
    """The plan's injected fault legs: installed-verification failure,
    permission-hardening failure, migration-connection failure, a rollback
    that itself fails (including a rollback whose integrity_check is not
    ok), the F1 wording pin, and the safety-copy abort."""

    def test_f1_empty_destination_rollback_prints_the_ruled_line(self):
        # a populated source store, then an EMPTY destination
        self._add("f1 destination seed row")
        snap = self._snapshot(self.dir, self.store)
        self.store.write_bytes(b"")
        import storelib.backup as bm
        _orig = bm._row_counts
        counts = {"n": 0}

        def wrong_second(conn):
            counts["n"] += 1
            if counts["n"] == 1:
                return _orig(conn)
            return (999, 999)  # installed verification mismatch

        unpatch = self._patch_attr(bm, "_row_counts", wrong_second)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 1, result["err"][-300:])
        self.assertIn("restore FAILED", result["err"])
        self.assertIn("destination was missing/empty before the restore "
                      "and is again", result["err"])
        self.assertNotIn("automatic rollback also failed", result["err"])
        self.assertTrue(self.store.exists())
        self.assertEqual(self.store.stat().st_size, 0)
        self._no_leftovers()

    def test_empty_destination_rollback_not_empty_again_is_reported(self):
        # TG-1 (the round-1 review comment): the empty-destination rollback
        # FAILURE arm had no test. The rollback's postcondition check must
        # fire, be reported, and carry the EMPTY-destination manual
        # instructions. A generic "automatic rollback also failed" assertion
        # would not distinguish the arms.
        self._add("f1 empty rollback failure keeper row")
        snap = self._snapshot(self.dir, self.store)
        self.store.write_bytes(b"")
        import pathlib
        import storelib.backup as bm
        _orig = bm._row_counts
        counts = {"n": 0}

        def wrong_second(conn):
            counts["n"] += 1
            if counts["n"] == 1:
                return _orig(conn)
            return (999, 999)  # installed verification mismatch

        def _touch_writes_a_byte(path_self, *a, **k):
            path_self.write_bytes(b"x")   # the re-created file is not empty

        unpatch_row = self._patch_attr(bm, "_row_counts", wrong_second)
        unpatch_touch = self._patch_attr(pathlib.Path, "touch",
                                         _touch_writes_a_byte)
        try:
            result = self._run(snap, lambda m: None, unpatch_row)
        finally:
            unpatch_touch()
        self.assertEqual(result["rc"], 1, result["err"][-300:])
        self.assertIn("is not empty again", result["err"])
        self.assertIn("automatic rollback also failed", result["err"])
        self.assertIn("replace", result["err"],
                      "the empty-destination rollback must print the manual "
                      f"instructions: {result['err'][-400:]}")

    def test_missing_destination_rollback_survived_is_reported(self):
        # TG-1 (the round-1 review comment): the missing-destination rollback
        # FAILURE arm ("survived the rollback") had no test either.
        self._add("f1 missing rollback failure keeper row")
        snap = self._snapshot(self.dir, self.store)
        self.store.unlink()                    # the destination is MISSING
        import pathlib
        import storelib.backup as bm
        _orig = bm._row_counts
        counts = {"n": 0}

        def wrong_second(conn):
            counts["n"] += 1
            if counts["n"] == 1:
                return _orig(conn)
            return (999, 999)  # installed verification mismatch

        _real_unlink = pathlib.Path.unlink

        def _unlink_skips_the_destination(path_self, *a, **k):
            if path_self == self.store:
                return None        # the destination survives the rollback
            return _real_unlink(path_self, *a, **k)

        unpatch_row = self._patch_attr(bm, "_row_counts", wrong_second)
        unpatch_unlink = self._patch_attr(pathlib.Path, "unlink",
                                          _unlink_skips_the_destination)
        try:
            result = self._run(snap, lambda m: None, unpatch_row)
        finally:
            unpatch_unlink()
        self.assertEqual(result["rc"], 1, result["err"][-300:])
        self.assertIn("survived the rollback", result["err"])
        self.assertIn("automatic rollback also failed", result["err"])
        self.assertIn("delete", result["err"],
                      "the missing-destination rollback must print the manual "
                      f"instructions: {result['err'][-400:]}")

    def test_installed_verification_failure_rolls_back(self):
        self._add("verify keeper row one")
        self._add("verify keeper row two")
        snap = self._snapshot(self.dir, self.store)
        before_rows = self._live_rows()
        state = {"n": 0}

        def wrong_second(conn):
            state["n"] += 1
            if state["n"] == 1:
                return _orig(conn)
            return (424242, 424242)

        import storelib.backup as bm
        _orig = bm._row_counts
        unpatch = self._patch_attr(bm, "_row_counts", wrong_second)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 1)
        self.assertIn("restored row counts differ from the validated copy",
                      result["err"])
        self.assertIn("rolled back: live store re-installed from the "
                      "pre-restore backup", result["err"])
        self.assertEqual(self._live_rows(), before_rows)
        self._no_leftovers()

    def test_permission_failure_rolls_back(self):
        self._add("permission keeper row")
        snap = self._snapshot(self.dir, self.store)
        before_rows = self._live_rows()
        import storelib.backup as bm

        class _PermFailHost:
            """Proxy the REAL host: only the destination permission
            hardening fails (the lock and fs calls must keep working)."""

            def __init__(self, real):
                self._real = real

            _raised = False

            def set_owner_only_perms(self, path):
                if Path(path) == self._dest:
                    if not self._raised:
                        self._raised = True
                        raise OSError("icacls denied (injected)")
                    # the rollback's own hardening call passes through
                return self._real.set_owner_only_perms(path)

            def __getattr__(self, name):
                return getattr(self._real, name)

        stub = _PermFailHost(bm._host)
        stub._dest = self.store
        unpatch = self._patch_attr(bm, "_host", stub)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 1)
        self.assertIn("could not set owner-only permissions", result["err"])
        self.assertIn("rolled back: live store re-installed from the "
                      "pre-restore backup", result["err"])
        self.assertEqual(self._live_rows(), before_rows)
        self._no_leftovers()

    def test_migration_connection_failure_rolls_back(self):
        self._add("migration keeper row")
        snap = self._snapshot(self.dir, self.store)
        before_rows = self._live_rows()

        def boom(*a, **k):
            raise RuntimeError("connect refused (injected)")

        import storelib.schema as schema_mod
        unpatch = self._patch_attr(schema_mod, "connect", boom)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 1)
        self.assertIn("post-restore open/migrate failed", result["err"])
        self.assertIn("rolled back: live store re-installed from the "
                      "pre-restore backup", result["err"])
        self.assertEqual(self._live_rows(), before_rows)
        self._no_leftovers()

    def test_rollback_that_itself_fails(self):
        self._add("rollback-fail keeper row")
        snap = self._snapshot(self.dir, self.store)
        state = {"n": 0}
        import os as _os
        real_replace = _os.replace

        def replace_fails_second(src, dst):
            state["n"] += 1
            if state["n"] >= 2:
                raise OSError("replace denied (injected)")
            return real_replace(src, dst)

        import storelib.backup as bm
        counts = {"n": 0}

        def wrong_second(conn):
            counts["n"] += 1
            if counts["n"] == 1:
                return _orig(conn)
            return (424242, 424242)

        _orig = bm._row_counts
        u1 = self._patch_attr(bm, "_row_counts", wrong_second)
        u2 = self._patch_attr(bm.os, "replace", replace_fails_second)

        def unpatch_both(mod):
            u1(mod)
            u2(mod)

        result = self._run(snap, lambda m: None, unpatch_both)
        self.assertEqual(result["rc"], 1)
        self.assertIn("restored row counts differ", result["err"])
        self.assertIn("automatic rollback also failed", result["err"])
        self.assertIn("roll back by hand", result["err"])
        self.assertIn("prerestore-", result["err"])

    def test_rollback_integrity_not_ok(self):
        self._add("integrity keeper row")
        snap = self._snapshot(self.dir, self.store)
        import os as _os
        real_replace = _os.replace
        state = {"n": 0}

        def corrupt_then_swap(src, dst):
            if not state.get("corrupted"):
                cands = sorted(
                    (self.dir / "backups").glob("prerestore-*"))
                if cands:
                    state["corrupted"] = True
                    # REAL corruption: trailing appends are invisible to
                    # SQLite (the header owns the page count) — destroy
                    # page 2 instead.
                    victim = cands[-1]
                    size = victim.stat().st_size
                    with open(victim, "r+b") as fh:
                        fh.seek(min(4096, max(1, size // 2)))
                        fh.write(b"\x00" * 32)
            return real_replace(src, dst)

        import storelib.backup as bm
        counts = {"n": 0}

        def wrong_second(conn):
            counts["n"] += 1
            if counts["n"] == 1:
                return _orig(conn)
            return (424242, 424242)

        _orig = bm._row_counts
        u1 = self._patch_attr(bm, "_row_counts", wrong_second)
        u2 = self._patch_attr(bm.os, "replace", corrupt_then_swap)

        def unpatch_both(mod):
            u1(mod)
            u2(mod)

        result = self._run(snap, lambda m: None, unpatch_both)
        self.assertEqual(result["rc"], 1)
        self.assertIn("restored row counts differ", result["err"])
        self.assertIn("automatic rollback also failed", result["err"])
        # the rollback's verification of the re-installed file failed. The
        # corrupted page's wording is SQLite-build-dependent: the WAL pragma
        # raises "database disk image is malformed" first on some builds,
        # while others reach the integrity_check leg and report
        # "btreeInitPage() returns error code 11" (the ubuntu CI lane showed
        # the second). Pin that SOME corruption wording surfaced.
        self.assertTrue("malformed" in result["err"]
                        or "integrity_check" in result["err"],
                        result["err"][-300:])

    def test_safety_copy_failure_aborts_untouched(self):
        self._add("abort keeper row")
        snap = self._snapshot(self.dir, self.store)
        before = self.store.read_bytes()

        def boom(*a, **k):
            raise RuntimeError("snapshot refused (injected)")

        import storelib.backup as bm
        unpatch = self._patch_attr(bm, "create_snapshot", boom)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 1)
        self.assertIn("restore ABORTED", result["err"])
        self.assertIn("refusing to overwrite the only copy", result["err"])
        self.assertEqual(self.store.read_bytes(), before)
        self._no_leftovers()


class StashRemovalTest(_InProcessRestoreBase):
    """A stash that cannot be removed on the SUCCESS path is reported, and
    the restore still succeeds (plan: 'the fault matrix beyond the frozen
    legs, including a stash that cannot be removed on the success path')."""

    def test_success_path_stash_removal_failure_is_reported(self):
        self._add("stash keeper row")
        snap = self._snapshot(self.dir, self.store)
        # sidecars present => a stash directory is created at step 6. The
        # sidecar is planted AFTER the snapshot: the backup subprocess
        # checkpoints an existing -wal away when it closes.
        Path(str(self.store) + "-wal").write_bytes(b"STALEWAL")
        import shutil as _sh
        real_rmtree = _sh.rmtree

        def keep_stash(path, *a, **k):
            if ".restore-stash-" in str(path):
                return  # pretend removal failed silently
            return real_rmtree(path, *a, **k)

        import storelib.backup as bm
        unpatch = self._patch_attr(bm.shutil, "rmtree", keep_stash)
        result = self._run(snap, lambda m: None, unpatch)
        self.assertEqual(result["rc"], 0, result["err"][-300:])
        self.assertIn("restore: OK", result["out"] + result["err"])
        self.assertIn("is set aside at", result["err"])
        self.assertTrue(list(self.dir.glob(self.store.name +
                                           ".restore-stash-*")),
                        "expected the stash to survive the failed removal")


class CompatPolicyTest(SuppBase, _SnapshotMixin):
    """The compat-window and override stamp policy (unchanged by the new
    preflight): a newer-than-forward destination refuses with exit 2, and
    ZMEM_ALLOW_NEWER_SCHEMA=1 lets the operator override."""

    def test_newer_destination_refused_then_override_allows(self):
        self._add("compat source row")
        snap = self._snapshot(self.dir, self.store)
        # plant a too-new destination stamp
        conn = self._store_conn()
        try:
            from schema_meta import FORWARD_COMPAT_SCHEMA_VERSION
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('schema_version', ?)",
                (str(FORWARD_COMPAT_SCHEMA_VERSION + 1),))
            conn.commit()
        finally:
            conn.close()
        before = self.store.read_bytes()
        env = _child_env(self.dir, self.store)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=300)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertIn("FAILED", r.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        # the operator override lets it proceed
        env["ZMEM_ALLOW_NEWER_SCHEMA"] = "1"
        r2 = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=300)
        self.assertEqual(r2.returncode, 0, r2.stderr[-300:])


class WriteDriftSuppTest(SuppBase):
    """The dedup-fold branch of update under concurrent metadata drift
    (plan Test Plan item 2): the fold merges onto the DRIFTED values, not
    the ones the pre-lock read saw."""

    def test_dedup_fold_uses_drifted_metadata(self):
        import sqlite3 as _sq
        # a store via the module pin: set env BEFORE storelib import
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        import storelib
        storelib._refresh_env_state()
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        other = storelib.connect()
        self.addCleanup(other.close)
        self.addCleanup(conn.close)
        import storelib.write as writelib
        # row B is the row the update's new content will fold into; row A
        # is the update target (different content, so it does not self-match)
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="dedup drift anchor one two three",
            tags="orig", confidence=0.5, signal="test")
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="dedup unrelated target row",
            tags="target", confidence=0.5, signal="test")
        b = conn.execute(
            "SELECT id FROM memory WHERE content LIKE 'dedup drift anchor%'"
        ).fetchone()
        a = conn.execute(
            "SELECT id FROM memory WHERE content LIKE 'dedup unrelated%'"
        ).fetchone()
        self.assertIsNotNone(b)
        self.assertIsNotNone(a)

        def fire():
            other.execute(
                "UPDATE memory SET tags='drifted', confidence=0.95 WHERE id=?",
                (b["id"],))
            other.commit()

        original = writelib._validate_namespace
        state = {"fired": False}

        def hooked(*args, **k):
            result = original(*args, **k)
            if not state["fired"]:
                state["fired"] = True
                fire()
            return result

        writelib._validate_namespace = hooked
        try:
            writelib.update_memory(
                conn, mid=a["id"], content="dedup drift anchor one two three",
                tags="second", confidence=0.6, signal="test")
        finally:
            writelib._validate_namespace = original
        kept = conn.execute(
            "SELECT tags, confidence FROM memory WHERE id=?",
            (b["id"],)).fetchone()
        self.assertIn("drifted", kept["tags"], kept["tags"])
        self.assertIn("second", kept["tags"], kept["tags"])
        self.assertGreaterEqual(kept["confidence"], 0.95, kept["confidence"])


class LockHoldSuppTest(SuppBase):
    """A multi-neighbour cluster with a concurrent writer: the write lock is
    HELD for the whole cluster transaction (a second writer's BEGIN
    IMMEDIATE is refused while the first absorb is in flight)."""

    def test_cluster_txn_holds_the_write_lock(self):
        import sqlite3 as _sq
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        import storelib
        storelib._refresh_env_state()
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        other = storelib.connect()
        self.addCleanup(other.close)
        import storelib.write as writelib
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="lock hold keeper alpha beta", confidence=0.9,
            signal="test")
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="lock hold neighbour alpha beta", confidence=0.8,
            signal="test")
        import importlib
        cons = importlib.import_module("storelib.consolidate")
        seen = {}

        def probe_while_absorbing(*a, **k):
            if "verdict" not in seen:
                probe = _sq.connect(str(self.store), timeout=0.2)
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    probe.execute(
                        "INSERT INTO meta(key, value) VALUES "
                        "('supp-lock-probe', 'landed')")
                    probe.commit()
                    seen["verdict"] = "second-writer-was-admitted"
                except _sq.OperationalError as exc:
                    seen["verdict"] = "blocked:%s" % exc
                finally:
                    probe.close()
            return original_absorb(*a, **k)

        original_absorb = cons._absorb_into_keeper
        cons._absorb_into_keeper = probe_while_absorbing
        try:
            report = cons.consolidate(
                conn, threshold=0.5, force=True, merge_contested=False)
        finally:
            cons._absorb_into_keeper = original_absorb
        self.assertIn("blocked", seen.get("verdict", "never-called"),
                      seen)
        self.assertEqual(report.get("merged"), 1, report)


class LoopBoundShapeTest(SuppBase, _SnapshotMixin):
    """The swarm-critic loop-bound regression cases: non-ASCII rows
    accepted; a client without sqlite-vec accepts a store with memory_vec;
    an extra UNIQUE index / extra trigger / inline UNIQUE column refused;
    an ALTER-upgraded table accepted."""

    def _dest_with_row(self):
        # a populated destination whose bytes must not change on refusal
        self._add("destination control row")
        return self.store.read_bytes()

    def _restore(self, snap, extra_env=None):
        env = _child_env(self.dir, self.store)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=300)

    def test_non_ascii_rows_are_accepted(self):
        self._add("accented row caf\u00e9 evaluation")
        self._add("nfd row cafe\u0301 evaluation")
        snap = self._snapshot(self.dir, self.store)
        for p in [self.store] + [Path(str(self.store) + s)
                                 for s in ("-wal", "-shm", "-journal")]:
            if p.exists():
                p.unlink()
        r = self._restore(snap)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("restore: OK", r.stdout)
        rows = self._live_rows()
        self.assertTrue(any("caf\u00e9" in c for c in rows), rows)
        self.assertTrue(any("cafe\u0301" in c for c in rows), rows)

    def test_client_without_sqlite_vec_accepts_vec_store(self):
        self._add("vec store row")
        blocker = self.dir / "novec"
        blocker.mkdir()
        (blocker / "sqlite_vec.py").write_text(
            "raise ImportError('sqlite-vec blocked for this leg')\n",
            encoding="utf-8")
        snap = self._snapshot(self.dir, self.store)
        # prove the block actually bites under this PYTHONPATH
        env_probe = _child_env(self.dir, self.store)
        env_probe["PYTHONPATH"] = str(blocker)
        probe = subprocess.run(
            [PY, "-X", "utf8", "-c", "import sqlite_vec"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env=env_probe, timeout=60)
        self.assertNotEqual(probe.returncode, 0, "blocker did not bite")
        for p in [self.store] + [Path(str(self.store) + s)
                                 for s in ("-wal", "-shm", "-journal")]:
            if p.exists():
                p.unlink()
        r = self._restore(snap, {"PYTHONPATH": str(blocker)})
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        conn = self._store_conn()
        try:
            names = {n for (n,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE name='memory_vec'")}
        finally:
            conn.close()
        self.assertIn("memory_vec", names)
        self.assertIn("vec store row", self._live_rows())

    def test_extra_unique_index_is_refused(self):
        self._add("extra index source row")
        conn = self._store_conn()
        try:
            conn.execute(
                "CREATE UNIQUE INDEX supp_extra_unique_idx "
                "ON memory(id, ingestion_ts)")
            conn.commit()
        finally:
            conn.close()
        snap = self._snapshot(self.dir, self.store)
        before = self._dest_with_row()
        r = self._restore(snap)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not be validated", r.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_extra_trigger_is_refused(self):
        self._add("extra trigger source row")
        conn = self._store_conn()
        try:
            conn.execute(
                "CREATE TRIGGER supp_extra_trigger AFTER INSERT ON meta "
                "BEGIN SELECT 1; END")
            conn.commit()
        finally:
            conn.close()
        snap = self._snapshot(self.dir, self.store)
        before = self._dest_with_row()
        r = self._restore(snap)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not be validated", r.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_inline_unique_column_is_refused(self):
        self._add("inline unique source row")
        conn = self._store_conn()
        try:
            # REAL DDL, not writable_schema surgery: a UNIQUE column implies
            # an autoindex, and a DDL-only mutation fails integrity_check
            # before the shape proof ever runs. Rebuild memory from its own
            # table_info with an inline UNIQUE on namespace, rows copied.
            cols = conn.execute("PRAGMA table_info(memory)").fetchall()
            names = [c[1] for c in cols]
            defs = []
            for cid, name, ctype, notnull, dflt, pk in cols:
                d = f"  {name} {ctype}"
                if pk:
                    d += " PRIMARY KEY"
                if name == "namespace":
                    d += " NOT NULL UNIQUE"
                elif notnull:
                    d += " NOT NULL"
                if dflt is not None:
                    d += f" DEFAULT {dflt}"
                defs.append(d)
            ddl = ("CREATE TABLE memory_rebuilt (\n"
                   + ",\n".join(defs) + "\n)")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(ddl)
            conn.execute(
                f"INSERT INTO memory_rebuilt ({', '.join(names)}) "
                f"SELECT {', '.join(names)} FROM memory")
            conn.execute("DROP TABLE memory")
            conn.execute("ALTER TABLE memory_rebuilt RENAME TO memory")
            conn.commit()
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(ok, "ok", "fixture must pass integrity")
        finally:
            conn.close()
        snap = self._snapshot(self.dir, self.store)
        # the mutated store IS the destination; a refusal must leave its
        # bytes untouched (no same-namespace control row is possible: the
        # fixture's namespace column is UNIQUE now)
        before = self.store.read_bytes()
        r = self._restore(snap)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not be validated", r.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_alter_upgraded_table_is_accepted(self):
        self._add("alter upgraded source row")
        conn = self._store_conn()
        try:
            conn.execute("DROP TABLE training_capture_observation")
            conn.commit()
        finally:
            conn.close()
        # rebuild the ORIGINAL five-column shape (the 8065f515 DDL), then
        # let a writable open upgrade it by ALTER TABLE ADD COLUMN
        env = _child_env(self.dir, self.store)
        conn = self._store_conn()
        try:
            conn.execute("""
                CREATE TABLE training_capture_observation (
                  observation_id TEXT PRIMARY KEY,
                  capture_id TEXT NOT NULL REFERENCES training_capture(capture_id),
                  observation_kind TEXT NOT NULL,
                  payload TEXT,
                  observed_at TEXT NOT NULL
                )""")
            conn.commit()
        finally:
            conn.close()
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "add", "--namespace", NS,
             "--type", "fact", "--content", "alter upgraded writable open",
             "--signal", "test", "--json"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        conn = self._store_conn()
        try:
            cols = [c[1] for c in conn.execute(
                "PRAGMA table_info(training_capture_observation)")]
        finally:
            conn.close()
        self.assertIn("payload_sha256", cols, cols)
        snap = self._snapshot(self.dir, self.store)
        before = self._dest_with_row()
        r = self._restore(snap)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("restore: OK", r.stdout)
        self.assertIn("alter upgraded source row", self._live_rows())


class NamespaceTrimSuppTest(SuppBase):
    """Final-critic finding 1: update stores a CALLER-supplied namespace in
    its canonicalized form (trimmed), through the library AND the public
    CLI — the locked-row rework had regressed update to the raw value
    (add, and update on main, always stored the trimmed one)."""

    def test_update_trims_caller_namespace_library(self):
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        import storelib
        storelib._refresh_env_state()
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        import storelib.write as writelib
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="namespace trim library row", signal="test")
        row = conn.execute(
            "SELECT id FROM memory WHERE content LIKE 'namespace trim%'"
        ).fetchone()
        self.assertIsNotNone(row)
        writelib.update_memory(
            conn, mid=row["id"],
            content="namespace trim library row two",
            namespace="  " + NS + "  ")
        successor = conn.execute(
            "SELECT namespace FROM memory WHERE update_of=? "
            "AND superseded_at IS NULL", (row["id"],)).fetchone()
        self.assertIsNotNone(successor)
        self.assertEqual(successor["namespace"], NS,
                         repr(successor["namespace"]))

    def test_update_trims_caller_namespace_cli(self):
        seeded = "namespace trim cli row"
        r = self._add(seeded)
        import json as _json
        mid = _json.loads(r.stdout)["id"]
        env = _child_env(self.dir, self.store)
        r2 = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "update", "--id", mid,
             "--namespace", "  " + NS + "  ", "--content",
             "namespace trim cli row two", "--json"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=180)
        self.assertEqual(r2.returncode, 0, r2.stderr[-300:])
        conn = self._store_conn()
        try:
            ns_rows = conn.execute(
                "SELECT namespace FROM memory WHERE update_of=? "
                "AND superseded_at IS NULL", (mid,)).fetchall()
        finally:
            conn.close()
        self.assertTrue(ns_rows, "no live successor after the CLI update")
        self.assertEqual(ns_rows[0]["namespace"], NS,
                         repr(ns_rows[0]["namespace"]))


class PurgeScrubWarnsTest(SuppBase, _SnapshotMixin):
    """F-005 (MV-003): --scrub-backups must loudly refuse to claim
    completeness while restore-forensic-*/restore-work-* dirs sit in the
    backup dir holding pre-restore bytes."""

    def test_scrub_warns_about_restore_artifacts(self):
        import json as _json
        r0 = self._add("purge scrub warn row")
        mid = _json.loads(r0.stdout)["id"]
        snap = self._snapshot(self.dir, self.store)
        forensic = self.dir / "backups" / "restore-forensic-test"
        forensic.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.store, forensic / "store.sqlite")
        env = _child_env(self.dir, self.store)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "purge", "--id", mid,
             "--scrub-backups", "--out-dir", str(self.dir / "backups")],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=180)
        self.assertIn("restore recovery artifacts", r.stderr,
                      (r.returncode, r.stdout[-200:], r.stderr[-300:]))
        self.assertIn(str(forensic), r.stderr)


class UnusableBackupDirTest(SuppBase, _SnapshotMixin):
    """F-004 (RP-003 group incl. the round-2 critic's second regression):
    a failure in restore steps 3-4 (unusable ZMEM_BACKUP_DIR) must print a
    ruled ABORTED line, leave the destination untouched, and leak nothing —
    for BOTH a populated destination and a first-time restore into an empty
    location (the base behavior for the empty case was SUCCESS; the
    regression died raw on the backup dir it never needed)."""

    def _restore_with_bad_dir(self):
        self._add("unusable backup dir row")
        snap_in_backups = self._snapshot(self.dir, self.store)
        # move the snapshot OUT of the dir we are about to break
        snap = self.dir / "snap.sqlite"
        shutil.copyfile(snap_in_backups, snap)
        before = self.store.read_bytes()
        blocker = self.dir / "backups"
        if blocker.is_dir():
            shutil.rmtree(blocker)
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_bytes(b"not a directory")
        env = _child_env(self.dir, self.store)
        env["ZMEM_BACKUP_DIR"] = str(blocker)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=300)
        leftovers = sorted(
            p.name for p in self.dir.iterdir()
            if ".restore-" in p.name
            or (p.name.startswith("store.sqlite") and p.name != "store.sqlite"))
        return r, before, leftovers

    def test_populated_destination_unusable_backup_dir(self):
        r, before, leftovers = self._restore_with_bad_dir()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restore ABORTED", r.stderr)
        self.assertIn("destination untouched", r.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(leftovers, [], leftovers)

    def test_first_time_restore_unusable_backup_dir(self):
        # no store, no sidecars: the backup dir is never needed — the
        # restore must SUCCEED (base behavior), not die on ZMEM_BACKUP_DIR.
        snap_dir = Path(tempfile.mkdtemp(prefix="f004-empty-src-"))
        self.addCleanup(shutil.rmtree, snap_dir, ignore_errors=True)
        src = snap_dir / "store.sqlite"
        env0 = _child_env(snap_dir, src)
        r0 = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "add", "--namespace", NS,
             "--type", "fact", "--content", "first time source row",
             "--signal", "test", "--json"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env0,
            timeout=180)
        self.assertEqual(r0.returncode, 0, r0.stderr[-200:])
        snap = self._snapshot(snap_dir, src)
        # destination: empty location with an unusable ZMEM_BACKUP_DIR
        blocker = self.dir / "backups"
        if blocker.is_dir():
            shutil.rmtree(blocker)
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_bytes(b"not a directory")
        env = _child_env(self.dir, self.store)
        env["ZMEM_BACKUP_DIR"] = str(blocker)
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "restore", "--from", str(snap),
             "--force"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertIn("restore: OK", r.stdout)


class ContentionRuledRetryTest(SuppBase):
    """F-003 (swarm-pr-review RP-001): when BEGIN IMMEDIATE fails on write
    contention, the cluster except path must not itself raise ('cannot
    rollback - no transaction is active') — the run continues and prints
    the ruled 'rolled back, will retry next run' line.

    F-011 (swarm-pr-review round 2): the two guards are DIFFERENT code
    paths and one test cannot cover both. The run-start write is the FIRST
    write, so a blocker taken before the call makes the RUN-START handler
    return before the cluster loop is ever reached — the old single leg
    asserted an OR over both ruled lines and therefore pinned NEITHER
    guard. The two legs below are split accordingly, and the run-start leg
    also pins the transaction state (F-003: the handler must roll the
    implicit transaction back before returning).

    The #542 AC2 cross-reference stands: that issue carries the
    writer-lease liveness AC, and the contention seam exercised here is the
    same seam AC2's contention tests will use.
    """

    def _seeded_conn(self):
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        import storelib
        storelib._refresh_env_state()
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        import storelib.write as writelib
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="contention keeper one two", confidence=0.9,
            signal="test")
        writelib.add_memory(
            conn, namespace=NS, type_="fact",
            content="contention neighbour one two", confidence=0.8,
            signal="test")
        return conn

    def test_run_start_contention_leaves_no_open_transaction(self):
        # F-003 (swarm-pr-review round 2): the run-start handler returns
        # after a contended write; pysqlite's implicit BEGIN succeeds
        # WITHOUT taking a lock and SQLite does not end the transaction on
        # SQLITE_BUSY, so without the rollback guard the connection comes
        # back still inside a transaction and the next caller (organize.py
        # reuses it) trips over 'cannot start a transaction within a
        # transaction'.
        conn = self._seeded_conn()
        before = conn.execute(
            "SELECT value FROM meta WHERE key='last_consolidation'").fetchone()
        blocker = sqlite3.connect(str(self.store), timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        import io as _io
        import contextlib as _cl
        import importlib
        cons = importlib.import_module("storelib.consolidate")
        out, err = _io.StringIO(), _io.StringIO()
        try:
            with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                report = cons.consolidate(
                    conn, threshold=0.05, force=True, namespace=NS)
        except Exception as e:
            self.fail(f"consolidate let the contention escape as "
                      f"{type(e).__name__}: {e}")
        finally:
            blocker.rollback()
        self.assertIn("run-start write failed", err.getvalue())
        self.assertNotIn("rolled back, will retry next run", err.getvalue())
        self.assertTrue(report.get("skipped_by_contention"), report)
        self.assertFalse(conn.in_transaction,
                         "the contended run-start write left the connection "
                         "inside an open transaction")
        after = conn.execute(
            "SELECT value FROM meta WHERE key='last_consolidation'").fetchone()
        self.assertEqual(before, after,
                         "a contended run-start write must not change the "
                         "last_consolidation stamp")

    def test_cluster_loop_contention_prints_ruled_retry(self):
        # The cluster-loop guard: the BEGIN IMMEDIATE inside the loop
        # fails (write-lock contention past busy_timeout). No transaction
        # is open, so a bare ROLLBACK would raise 'cannot rollback - no
        # transaction is active' OUT of the handler and kill the run with
        # an unhandled traceback. The guard must skip the rollback and
        # print the ruled retry line.
        conn = self._seeded_conn()

        class _BeginLocked:
            """Fails ONLY the cluster loop's explicit BEGIN IMMEDIATE."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **k):
                if "BEGIN IMMEDIATE" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self._inner.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        import io as _io
        import contextlib as _cl
        import importlib
        cons = importlib.import_module("storelib.consolidate")
        out, err = _io.StringIO(), _io.StringIO()
        try:
            with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                report = cons.consolidate(
                    _BeginLocked(conn), threshold=0.05, force=True,
                    namespace=NS)
        except Exception as e:
            self.fail(f"consolidate let the cluster-loop contention escape as "
                      f"{type(e).__name__}: {e}")
        self.assertIn("rolled back, will retry next run", err.getvalue(),
                      err.getvalue()[-400:])
        self.assertNotIn("run-start write failed", err.getvalue())

    def test_rollback_failure_is_reported(self):
        # F-003 (swarm-pr-review round 2, Stage-B review): a rollback that
        # ITSELF fails must be REPORTED — the handler must not return a
        # clean-looking retry report while the connection may still hold an
        # open transaction.
        conn = self._seeded_conn()

        class _RollbackFails:
            """The write fails (lock held) and the rollback fails too."""

            def __init__(self, inner):
                self._inner = inner

            def rollback(self):
                raise sqlite3.OperationalError("rollback refused")

            def execute(self, sql, *a, **k):
                return self._inner.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        blocker = sqlite3.connect(str(self.store), timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        import io as _io
        import contextlib as _cl
        import importlib
        cons = importlib.import_module("storelib.consolidate")
        out, err = _io.StringIO(), _io.StringIO()
        try:
            with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                cons.consolidate(_RollbackFails(conn), threshold=0.05,
                                 force=True, namespace=NS)
        except Exception as e:
            self.fail(f"consolidate let the rollback failure escape as "
                      f"{type(e).__name__}: {e}")
        finally:
            blocker.rollback()
        self.assertIn("run-start rollback failed", err.getvalue(),
                      err.getvalue()[-400:])


class Vec0RekeyCasSuppTest(SuppBase):
    """F-001 (swarm-pr-review CS-001): the vec0 gather path must carry the
    decision fields (namespace included) so the per-neighbour CAS refuses a
    concurrent rekey committed in the gather->refresh window. The frozen C5
    checks hook only _admitted_neighbors (post-refresh); this pins the
    window before it, on the REAL fake-embed vec0 lane."""

    def test_vec0_gather_carries_namespace(self):
        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(self.dir)
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        import storelib
        storelib._refresh_env_state()
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        other = storelib.connect()
        self.addCleanup(other.close)
        import storelib.write as writelib
        writelib.add_memory(conn, namespace="project:alpha", type_="fact",
                            content="vec0 rekey keeper alpha shared tokens",
                            confidence=0.9, signal="test")
        writelib.add_memory(conn, namespace="project:alpha", type_="fact",
                            content="vec0 rekey neighbour alpha shared tokens",
                            confidence=0.8, signal="test")
        nb = conn.execute(
            "SELECT id FROM memory WHERE content LIKE "
            "'vec0 rekey neighbour%'").fetchone()
        self.assertIsNotNone(nb)
        vec_n = conn.execute("SELECT count(*) FROM memory_vec").fetchone()[0]
        self.assertGreaterEqual(vec_n, 2, vec_n)
        import io as _io
        import contextlib as _cl
        import importlib
        cons = importlib.import_module("storelib.consolidate")

        def fire():
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "UPDATE memory SET namespace='project:beta' WHERE id=?",
                (nb["id"],))
            other.commit()

        original = cons._refresh_live_row
        state = {"fired": False}

        def hooked(c, row_id):
            result = original(c, row_id)
            if not state["fired"]:
                state["fired"] = True
                fire()
            return result

        cons._refresh_live_row = hooked
        try:
            out, err = _io.StringIO(), _io.StringIO()
            with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                report = cons.consolidate(conn, threshold=0.05, force=True)
        finally:
            cons._refresh_live_row = original
            os.environ.pop("ZMEM_EMBED_PROFILE", None)
            storelib._refresh_env_state()
        self.assertTrue(state["fired"], "seam never fired")
        self.assertEqual(report.get("merged"), 0,
                         f"cross-namespace absorb: {report}")
        self.assertIn("changed on a decision field", err.getvalue(),
                      err.getvalue()[-400:])
        beta_live = conn.execute(
            "SELECT count(*) FROM memory WHERE id=? AND namespace="
            "'project:beta' AND superseded_at IS NULL",
            (nb["id"],)).fetchone()[0]
        self.assertEqual(beta_live, 1)


class SurgeryAtomicityTest(SuppBase):
    """F-001 (swarm-pr-review round 2): the FTS surgery's recreate must be
    ONE transaction.

    With pysqlite's default isolation level the DROP/CREATE DDL auto-commits
    one statement at a time while the rebuild INSERT commits later, so a
    failure (or a kill) between the DDL and the rebuild left all five FTS
    objects PRESENT with an EMPTY index. `_recover_search_index` only
    rebuilds when an object is MISSING, so the next open never healed it and
    every pre-existing row stayed unsearchable until a manual rebuild-fts.
    With the recreate in one explicit transaction the failure rolls the
    whole recreate back, and the plain recovery arm heals the store.
    """

    def _unconstructible_store(self):
        self._add("surgery atomicity row one alpha")
        self._add("surgery atomicity row two beta")
        conn = sqlite3.connect(str(self.store))
        conn.execute("DROP TABLE memory_fts_data")  # -> unconstructible vtable
        conn.commit()
        conn.close()

    def test_failed_surgery_leaves_a_healable_store(self):
        self._unconstructible_store()
        import storelib.schema as schema
        real_connect = sqlite3.connect

        class _RebuildFails:
            """Fails the rebuild INSERT (after the DDL) and nothing else."""

            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **k):
                if "VALUES('rebuild')" in sql:
                    raise sqlite3.OperationalError("injected rebuild failure")
                return self._inner.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def _boom_connect(*args, **kwargs):
            return _RebuildFails(real_connect(*args, **kwargs))

        sqlite3.connect = _boom_connect
        try:
            with self.assertRaises(sqlite3.OperationalError):
                schema._fts_schema_surgery(str(self.store))
        finally:
            sqlite3.connect = real_connect

        # The next open must heal the index and every PRE-EXISTING row must
        # be MATCH-findable again (pre-fix: the objects were all present, so
        # the recovery gate skipped the rebuild and the rows stayed
        # unsearchable).
        r = self._add("post surgery failure row")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        conn = self._store_conn()
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok",
                             "a rolled-back surgery must leave the store "
                             "integrity-clean")
            for word in ("alpha", "beta"):
                hits = conn.execute(
                    "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                    (f'"{word}"',)).fetchone()[0]
                self.assertGreater(hits, 0, word)
        finally:
            conn.close()

    def _surgery_must_propagate(self, message):
        self._unconstructible_store()
        import storelib.generation as gen_mod
        import storelib.schema as schema
        original = gen_mod.bump_mutation_generation

        def _boom(_conn):
            raise sqlite3.OperationalError(message)

        gen_mod.bump_mutation_generation = _boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                schema._fts_schema_surgery(str(self.store))
        finally:
            gen_mod.bump_mutation_generation = original

    def test_bump_failure_with_a_different_table_is_not_swallowed(self):
        # "no such table: meta_extra" is NOT the benign case: a substring
        # test would swallow it.
        self._surgery_must_propagate("no such table: meta_extra")

    def test_bump_failure_ending_with_the_benign_phrase_is_not_swallowed(self):
        # Only the EXACT message is the benign case: an unrelated error
        # whose text merely ends with "no such table: meta" must propagate
        # too, so the recreate rolls back.
        self._surgery_must_propagate("boom: no such table: meta")

    def test_failed_generation_bump_rolls_the_recreate_back(self):
        # The mutation-generation bump IS the #474 recall-cache
        # invalidation: generation.py is explicit that a bump which cannot
        # run inside the write transaction is a caller bug, not a
        # degraded-ok state. A swallowed bump would commit a healed store
        # whose cached recall survives the heal, so the bump failure must
        # roll the whole recreate back.
        self._unconstructible_store()
        import storelib.generation as gen_mod
        import storelib.schema as schema
        original = gen_mod.bump_mutation_generation

        def _boom(_conn):
            raise sqlite3.OperationalError("injected bump failure")

        gen_mod.bump_mutation_generation = _boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                schema._fts_schema_surgery(str(self.store))
        finally:
            gen_mod.bump_mutation_generation = original

        # The recreate rolled back, so the next open heals the store and
        # every pre-existing row is MATCH-findable again.
        r = self._add("post bump failure row")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        conn = self._store_conn()
        try:
            for word in ("alpha", "beta"):
                hits = conn.execute(
                    "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                    (f'"{word}"',)).fetchone()[0]
                self.assertGreater(hits, 0, word)
        finally:
            conn.close()


class RoundOneCommentLegsTest(_InProcessRestoreBase):
    """Legs for the round-1 review comment's code findings (which the
    round-2 inventory did not cover until the closeout critic found the gap).

    - R1-F-002: the surgery's `meta` READ is gated on the meta table's
      presence, so a store with no meta table cannot fail mid-recreate.
    - R1-F-004: the staged copy is made INSIDE the guarded block, so a copy
      failure is contained (a ruled message, no traceback, no leftover
      `.restore-stage-*` file).
    - R1-F-005: the forensic dir is hardened BEFORE the pre-restore copies
      land in it.
    - R1-F-010: init_db recreates a dropped `memory_au` with the form the
      store's stamp implies (the guarded v2+ form on a v2+ store).
    """

    def test_surgery_heals_a_store_without_a_meta_table(self):
        self._add("no meta table row alpha")
        conn = sqlite3.connect(str(self.store))
        conn.execute("DROP TABLE memory_fts_data")   # -> unconstructible vtable
        conn.execute("DROP TABLE meta")
        conn.commit()
        conn.close()
        import storelib.schema as schema
        schema._fts_schema_surgery(str(self.store))   # must NOT raise
        conn = sqlite3.connect(str(self.store))
        try:
            hits = conn.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                ('"alpha"',)).fetchone()[0]
            self.assertGreater(hits, 0, "the healed index must find the row")
        finally:
            conn.close()

    def test_staged_copy_failure_is_contained(self):
        self._add("staged copy failure row")
        snap = self._snapshot(self.dir, self.store)
        original = {}

        def _patch(_mod):
            import shutil as sh
            original["copy2"] = sh.copy2

            def _boom(src, dst, *a, **k):
                if ".restore-stage-" in str(dst):
                    raise OSError("injected staged copy failure")
                return original["copy2"](src, dst, *a, **k)

            sh.copy2 = _boom

        def _unpatch(_mod):
            import shutil as sh
            sh.copy2 = original["copy2"]

        result = self._run(snap, patch=_patch, unpatch=_unpatch)
        self.assertEqual(result["rc"], 1, result)
        self.assertIn("destination untouched", result["err"])
        self.assertNotIn("Traceback", result["err"])
        leftovers = [p.name for p in self.dir.iterdir()
                     if ".restore-stage-" in p.name]
        self.assertEqual(leftovers, [],
                         "a failed staged copy must not leave a stage file")

    def test_forensic_dir_is_hardened_before_the_copies(self):
        self._add("forensic hardening order row")
        snap = self._snapshot(self.dir, self.store)
        # sidecars beside the store make the restore take the forensic-copy
        # path (step 3+4).
        for p in [self.store] + [Path(str(self.store) + s)
                                 for s in ("-wal", "-shm", "-journal")]:
            if p.exists():
                p.unlink()
        Path(str(self.store) + "-wal").write_bytes(b"WALJUNK")
        Path(str(self.store) + "-shm").write_bytes(b"SHMJUNK")
        events = []
        original = {}

        def _patch(_mod):
            import shutil as sh
            original["copy2"] = sh.copy2
            original["host"] = _mod._host

            class _RecordingHost:
                def __init__(self, real):
                    self._real = real

                def set_owner_only_perms(self, path):
                    events.append(("harden", str(path)))
                    if self._real is not None:
                        return self._real.set_owner_only_perms(path)

                def __getattr__(self, name):
                    if self._real is None:
                        raise AttributeError(name)
                    return getattr(self._real, name)

            def _spy(src, dst, *a, **k):
                events.append(("copy", str(dst)))
                return original["copy2"](src, dst, *a, **k)

            sh.copy2 = _spy
            _mod._host = _RecordingHost(_mod._host)

        def _unpatch(mod):
            import shutil as sh
            sh.copy2 = original["copy2"]
            mod._host = original["host"]

        result = self._run(snap, patch=_patch, unpatch=_unpatch)
        self.assertEqual(result["rc"], 0, result["err"][-400:])
        hardens = [i for i, (kind, path) in enumerate(events)
                   if kind == "harden" and "restore-forensic-" in path]
        copies = [i for i, (kind, path) in enumerate(events)
                  if kind == "copy" and "restore-forensic-" in path]
        self.assertTrue(hardens, f"the forensic dir was never hardened: {events}")
        self.assertTrue(copies, f"no forensic copy happened: {events}")
        self.assertLess(hardens[0], copies[0],
                        "the forensic dir must be hardened BEFORE the "
                        f"pre-restore copies land in it: {events}")

    def test_init_db_recreates_the_guarded_update_trigger(self):
        self._add("trigger parity row")
        conn = sqlite3.connect(str(self.store))
        conn.execute("DROP TRIGGER memory_au")
        conn.commit()
        conn.close()
        r = self._add("post parity row")
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        conn = sqlite3.connect(str(self.store))
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='memory_au'").fetchone()
            self.assertIsNotNone(row, "memory_au was not recreated")
            self.assertIn("WHEN", row[0].upper(),
                          "a v2+ store must get the GUARDED update trigger")
        finally:
            conn.close()


class RestoreOutputEscapingTest(_InProcessRestoreBase):
    """F-002 (swarm-pr-review round 2): every restore-path line that echoes
    snapshot- or store-derived text goes through the _esc() helper, so a
    crafted snapshot (or a crafted SQLite error) cannot inject terminal
    control bytes into the operator's terminal."""

    def test_preparation_failure_text_is_escaped(self):
        # The preparation-failure line (step 3+4) echoed the raw exception
        # text; pre-fix this leg fails because the ESC byte reaches the
        # output unescaped.
        self._add("escaping control row")
        snap = self._snapshot(self.dir, self.store)
        original = {}

        def _raise(_path):
            raise RuntimeError("incarnation rotation failed \x1b[31mRED\x1b[0m")

        def _patch(_mod):
            import storelib.generation as gen_mod
            original["fn"] = gen_mod.rotate_incarnation_on_staging
            gen_mod.rotate_incarnation_on_staging = _raise

        def _unpatch(_mod):
            import storelib.generation as gen_mod
            gen_mod.rotate_incarnation_on_staging = original["fn"]

        result = self._run(snap, patch=_patch, unpatch=_unpatch)
        self.assertEqual(result["rc"], 1, result)
        self.assertIn("restore ABORTED", result["err"])
        out = result["out"] + result["err"]
        self.assertNotIn("\x1b", out,
                         "a raw ESC byte reached the restore output")
        self.assertIn("\\x1b[31m", out,
                      "the control bytes must appear in escaped form")

    def test_esc_helper_escapes_a_control_bearing_name(self):
        # The reviewer asked for a leg over an ESC-bearing SNAPSHOT NAME.
        # Windows forbids ASCII control characters in a file name, so such a
        # file cannot exist on this host (the ubuntu CI lane could create
        # one); the reachable carrier of the same bytes is the text the
        # restore echoes (the SQLite error text, the snapshot-derived
        # identifiers), which the leg above drives end to end. This leg pins
        # the helper itself on an ESC-bearing NAME string, so the escaping
        # contract of a path-shaped value is pinned on every platform.
        import storelib.backup as backup_mod
        escaped = backup_mod._esc("snap\x1b[31mshot.sqlite")
        self.assertNotIn("\x1b", escaped)
        self.assertIn("\\x1b[31m", escaped)
        # clean text passes through unchanged, so ordinary names stay
        # greppable in the ruled output.
        self.assertEqual(backup_mod._esc("store-2026.sqlite"),
                         "store-2026.sqlite")

    @unittest.skipUnless(os.name != "nt",
                         "Windows forbids ASCII control characters in a file "
                         "name, so an ESC-bearing snapshot file cannot exist "
                         "on this platform (the ubuntu CI lane runs it)")
    def test_esc_bearing_snapshot_filename_is_escaped(self):
        # The end-to-end filename leg: the restore echoes the snapshot path
        # it was given, and a snapshot whose NAME carries terminal control
        # bytes must reach the operator escaped.
        self._add("esc filename row")
        snap = self._snapshot(self.dir, self.store)
        esc_name = snap.parent / ("snap\x1b[31mshot.sqlite")
        shutil.copy2(snap, esc_name)
        result = self._run(esc_name)
        out = result["out"] + result["err"]
        self.assertNotIn("\x1b", out,
                         "a raw ESC byte from the snapshot name reached the "
                         "restore output")
        self.assertIn("\\x1b[31m", out,
                      "the snapshot name must appear in escaped form")


class RestoreWorkDirHardeningTest(_InProcessRestoreBase):
    """F-005 (swarm-pr-review round 2): the restore's WORK directory (a byte
    copy of the live store) must be owner-only hardened like the forensic
    directory beside it, so the guarantee does not depend on how the backup
    directory was created (an operator-supplied --out-dir is not hardened by
    _ensure_backup_dir). The leg pins the CALL, not the identifier: the
    hardening helper must be invoked with the work directory."""

    def test_work_dir_is_hardened(self):
        self._add("work dir hardening row")
        snap = self._snapshot(self.dir, self.store)
        calls = []

        class _RecordingHost:
            def __init__(self, real):
                self._real = real

            def set_owner_only_perms(self, path):
                calls.append(Path(path))
                if self._real is not None:
                    return self._real.set_owner_only_perms(path)

            def __getattr__(self, name):
                if self._real is None:
                    raise AttributeError(name)
                return getattr(self._real, name)

        original = {}

        def _patch(mod):
            original["host"] = mod._host
            mod._host = _RecordingHost(mod._host)

        def _unpatch(mod):
            mod._host = original["host"]

        result = self._run(snap, patch=_patch, unpatch=_unpatch)
        self.assertEqual(result["rc"], 0, result["err"][-400:])
        hardened = [p for p in calls if p.name.startswith("restore-work-")]
        self.assertTrue(hardened,
                        f"the restore work dir was never hardened: {calls}")


class CliEarlyErrorShapeTest(SuppBase):
    """F-019 (swarm-pr-review round 2): the early connect/prepare failure
    path prints the same command prefix as the general writer-lease path, so
    an operator sees WHICH command failed (and never a traceback)."""

    def test_early_failure_prints_the_command_prefix(self):
        blocker = self.dir / "not-a-directory"
        blocker.write_bytes(b"x")
        env = _child_env(self.dir, blocker / "store.sqlite")
        r = subprocess.run(
            [PY, "-X", "utf8", str(STORE_PY), "recall", "--query", "probe",
             "--no-bump", "--json"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
            timeout=180)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertIn("[zmem] recall: ", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ShapeTokenizerFidelityTest(SuppBase):
    """R1-F-007 / #542 AC6: the shape proof must parse what SQLite parses.

    The shape proof is NEW in this PR, and its tokenizer had two gaps
    that let a snapshot of the WRONG shape past it:

    * a numeric DEFAULT was normalised through ``float``, so ``DEFAULT 0``
      (an INTEGER) and ``DEFAULT 0.0`` (a REAL) compared EQUAL -- and
      SQLite stores them as different values -- and ``9007199254740992``
      and ``9007199254740993`` collided as well (one ULP apart);
    * a backtick-quoted identifier with a DOUBLED backtick escape is ONE
      identifier to SQLite (`` `a``b` `` is ``a`b``), but the pattern
      ended the identifier at the first backtick, so the impostor's single
      column read as the two columns the fresh store has.

    The numeric one is the material accept: the proof compares a column's
    DEFINITION token for token, and only the column NAME is cross-checked
    against ``table_xinfo`` -- so a required column whose default is a
    REAL where the fresh store's is an INTEGER was ACCEPTED, and the
    store that replaced the destination was not the required shape.
    """

    def _tokens(self, sql):
        import storelib.backup as backup_mod
        return backup_mod._shape_tokens(sql)

    def test_the_numeric_default_kind_is_part_of_the_shape(self):
        # R1-F-007: an INTEGER default and a REAL default are DIFFERENT
        # values to SQLite, so the shape proof must not call them equal.
        integer = self._tokens("c INTEGER NOT NULL DEFAULT 0")
        real = self._tokens("c INTEGER NOT NULL DEFAULT 0.0")
        self.assertNotEqual(
            integer, real,
            "AC6: `DEFAULT 0` (an INTEGER) and `DEFAULT 0.0` (a REAL) "
            "compare EQUAL in the shape proof; SQLite stores them as "
            "different values, so a store of the wrong shape is accepted")

    def test_the_numeric_default_value_is_exact(self):
        # R1-F-007: 2**53 and 2**53+1 differ by one ULP; a float cannot
        # tell them apart, so the proof must not.
        low = self._tokens("c INTEGER NOT NULL DEFAULT 9007199254740992")
        high = self._tokens("c INTEGER NOT NULL DEFAULT 9007199254740993")
        self.assertNotEqual(
            low, high,
            "AC6: the numeric default 9007199254740992 and 9007199254740993 "
            "compare EQUAL in the shape proof; they differ by one ULP, so a "
            "store whose required default is a different value is accepted")

    def test_the_value_tolerance_of_the_proof_is_kept(self):
        # The proof normalizes numbers BY VALUE (its own documented
        # contract), so two spellings of the same value still compare
        # equal -- a legitimate older backup must not be refused for it.
        self.assertEqual(
            self._tokens("c INTEGER NOT NULL DEFAULT 007"),
            self._tokens("c INTEGER NOT NULL DEFAULT 7"),
            "the shape proof must keep normalizing an INTEGER by value")
        self.assertEqual(
            self._tokens("c REAL NOT NULL DEFAULT 1e3"),
            self._tokens("c REAL NOT NULL DEFAULT 1000.0"),
            "the shape proof must keep normalizing a REAL by value")

    def test_a_doubled_backtick_is_one_identifier(self):
        # R1-F-007: SQLite reads `a``b` as the ONE identifier `a`b`.
        tokens = self._tokens("`a``b`")
        self.assertEqual(
            tokens, [("w", "a`b")],
            "AC6: a backtick-quoted identifier with a doubled-backtick "
            "escape is not read as one identifier; the tokenizer split it")

    def test_the_proof_refuses_a_real_default_impostor(self):
        # THE CONSEQUENCE: a staged store whose required column's default
        # is a REAL where the fresh store's is an INTEGER must be
        # REFUSED by the proof -- not accepted.
        import shutil as _shutil
        import storelib.backup as backup_mod

        self._add("shape impostor row")
        fresh = self.dir / "fresh.sqlite"
        staged = self.dir / "staged.sqlite"
        _shutil.copyfile(self.store, fresh)
        _shutil.copyfile(self.store, staged)

        # tamper the staged copy's `memory` DDL: the required column's
        # default becomes a REAL where the fresh store's is an INTEGER.
        conn = sqlite3.connect(str(staged))
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
            ).fetchone()
            self.assertIsNotNone(row, "the store has no `memory` table")
            ddl = row[0]
            self.assertIn("retrieval_count INTEGER NOT NULL DEFAULT 0", ddl,
                          "the memory DDL no longer carries the expected "
                          "retrieval_count default: %r" % ddl[:400])
            tampered = ddl.replace(
                "retrieval_count INTEGER NOT NULL DEFAULT 0",
                "retrieval_count INTEGER NOT NULL DEFAULT 0.0")
            self.assertNotEqual(tampered, ddl, "the tamper did not apply")
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_master SET sql=? WHERE type='table' "
                         "AND name='memory'", (tampered,))
            conn.execute("PRAGMA writable_schema=OFF")
            conn.commit()
        finally:
            conn.close()

        refused = False
        try:
            backup_mod.prove_required_shape(staged, fresh)
        except Exception:
            refused = True
        self.assertTrue(
            refused,
            "AC6: the shape proof ACCEPTED a staged store whose required "
            "column's default is a REAL where the fresh store's is an "
            "INTEGER -- a store of the wrong shape reached the destination")
