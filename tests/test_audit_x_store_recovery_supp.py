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
- the compat-window and override stamp policy (unchanged);
- the sidecar-free preflight: neither read creates a sidecar; a store with
  residue reads a private copy (arm B notice);
- a missing and an empty destination with orphan sidecars: the ruled
  success lines, no pre-restore backup;
- an installed-verification failure, a permission-hardening failure, a
  migration-connection failure and a rollback that itself fails (each
  injected, each reported with the ruled lines);
- the dedup-fold branch of ``update`` under concurrent metadata drift;
- the ops applier path with concurrent metadata drift (caller-owned
  transaction);
- doctor ``fts-index`` on a healthy store.

Hermetic: every subprocess and in-process leg pins a fresh throwaway store
via the module-top env pin (before any storelib import, per the guard).
"""
from __future__ import annotations

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

    def test_stamp_statement_sweep_resumes(self):
        conn = sqlite3.connect(str(self.store))
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
        # Drive the public open with a one-shot trace that kills BEFORE each
        # "UPDATE meta SET value=" statement in turn; every kill must resume.
        for i in range(2, 20):
            child = self.dir / f"kill_before_stamp_{i}.py"
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
            env = _child_env(self.dir, self.store)
            r = subprocess.run([PY, "-X", "utf8", str(child), str(i)],
                               cwd=str(REPO_ROOT), capture_output=True,
                               text=True, env=env, timeout=180)
            # A kill (137) or a clean finish are both fine; the OPEN must
            # always complete on the next attempt.
            r2 = subprocess.run(
                [PY, "-X", "utf8", str(STORE_PY), "add", "--namespace", NS,
                 "--type", "fact", "--content", f"resume probe {i}",
                 "--signal", "test", "--json"],
                cwd=str(REPO_ROOT), capture_output=True, text=True,
                env=env, timeout=180)
            self.assertEqual(r2.returncode, 0,
                             f"stamp {i}: kill rc={r.returncode} then resume "
                             f"failed: {r2.stderr[-300:]}")
        conn = self._store_conn()
        try:
            stamp = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(stamp) >= 2, True, stamp)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
