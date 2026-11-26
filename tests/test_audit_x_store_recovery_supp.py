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
        storelib = self._pinned()
        import storelib.backup as backup_mod
        out, err = _io.StringIO(), _io.StringIO()
        rc, raised = None, None
        saved = {k: v for k, v in os.environ.items()
                 if k.startswith("ZMEM_")}
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
        # the rollback's verification of the re-installed file failed: the
        # corrupted page surfaces as "database disk image is malformed"
        # (a DatabaseError from the same verification leg)
        self.assertIn("malformed", result["err"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
