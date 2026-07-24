"""Tests for P11: `store.py backup` / `store.py restore`, retention rotation,
and the single-flight advisory lock shared by `backup` and `consolidate`.

Covers, per PLAN.md §7 P11:
  - backup success path (integrity_check + row-count comparison)
  - REFUSAL to claim success: a snapshot that fails either check is deleted,
    `last_backup` is not written and retention does not run
  - retention: only the oldest `store-*.sqlite` beyond N are deleted; an
    unrelated file (and every `prerestore-*` safety copy) survives untouched
  - `--if-due` gating: skip inside the interval, run once it has elapsed
  - restore: refuses without --force, refuses a corrupt snapshot before
    touching the destination, round-trips, and leaves a `prerestore-*` copy
  - single-flight: the loser skips cleanly (exit 0) and a stale lock is broken

Drives the REAL store.py CLI via subprocess against throwaway temp stores —
never the box store — and imports store/host directly for the pure functions.

Run: python tests/test_backup.py   (no pytest required — repo convention)
"""

from __future__ import annotations

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
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:backuptest"

# Point the module-level STORE_PATH at a throwaway location BEFORE importing
# store.py: `C:\Users\Brett\.zmem` is the real box store and must never be the
# import-time default in a test process.
_IMPORT_TMP = tempfile.mkdtemp(prefix="zmem-import-")
os.environ["ZMEM_STORE"] = os.path.join(_IMPORT_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

sys.path.insert(0, str(SCRIPTS_DIR))

import host  # noqa: E402
import store  # noqa: E402


def _base_env(tmp: str) -> dict:
    """Env for a store.py subprocess pinned to a throwaway store, with the
    embedding model forced absent (fast + deterministic; repo convention from
    tests/test_model_fallback.py)."""
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


class _StoreCase(unittest.TestCase):
    """Common temp-store fixture: a fresh store dir per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-backup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.backups = Path(self.tmp) / "backups"
        self.env = _base_env(self.tmp)

    def run_store(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env or self.env, capture_output=True, text=True, timeout=120,
        )

    def add(self, content: str, signal: str = "test", type_: str = "fact") -> str:
        r = self.run_store("add", "--namespace", NS, "--type", type_,
                           "--content", content, "--signal", signal)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def query(self, sql: str, params=()):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def meta(self, key: str):
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def snapshots(self) -> list[str]:
        if not self.backups.is_dir():
            return []
        return sorted(p.name for p in self.backups.glob("store-*.sqlite"))


# ---------------------------------------------------------------------------
# backup: success path
# ---------------------------------------------------------------------------
class BackupSuccessTest(_StoreCase):
    def test_backup_writes_verified_snapshot_and_meta(self):
        self.add("alpha row one")
        self.add("bravo row two")
        r = self.run_store("backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("integrity_check=ok", r.stdout)
        self.assertIn("exact match", r.stdout)

        snaps = self.snapshots()
        self.assertEqual(len(snaps), 1, r.stdout)

        # The snapshot really is a usable, complete copy.
        snap_path = self.backups / snaps[0]
        conn = sqlite3.connect(str(snap_path))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM memory").fetchone()[0], 2)
        finally:
            conn.close()
        self.assertIsNotNone(self.meta("last_backup"))

    def test_snapshot_is_self_contained_no_sidecars(self):
        """A snapshot must not leave -wal/-shm orphans in the backup dir: a
        read-only reader cannot checkpoint them away, so they would persist."""
        self.add("alpha row one")
        self.assertEqual(self.run_store("backup").returncode, 0)
        stray = [p.name for p in self.backups.iterdir()
                 if p.name.endswith(("-wal", "-shm", "-journal"))]
        self.assertEqual(stray, [])

    def test_out_dir_override(self):
        self.add("alpha row one")
        alt = os.path.join(self.tmp, "elsewhere")
        r = self.run_store("backup", "--out-dir", alt)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list(Path(alt).glob("store-*.sqlite"))), 1)
        self.assertFalse(self.backups.exists())

    def test_env_backup_dir_override(self):
        self.add("alpha row one")
        env = dict(self.env)
        env["ZMEM_BACKUP_DIR"] = os.path.join(self.tmp, "envdir")
        r = self.run_store("backup", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            len(list((Path(self.tmp) / "envdir").glob("store-*.sqlite"))), 1)


# ---------------------------------------------------------------------------
# backup: refusal to claim success
# ---------------------------------------------------------------------------
class BackupRefusalTest(_StoreCase):
    def _good_snapshot(self) -> Path:
        self.add("alpha row one")
        self.assertEqual(self.run_store("backup").returncode, 0)
        return self.backups / self.snapshots()[0]

    def test_verify_deletes_snapshot_when_integrity_fails(self):
        snap = self._good_snapshot()
        # Corrupt the middle of the file so the header still parses but pages
        # do not — integrity_check must reject it.
        with open(snap, "r+b") as f:
            f.seek(2048)
            f.write(b"\xde\xad\xbe\xef" * 512)
        with self.assertRaises(store.SnapshotError):
            store.verify_snapshot(snap, (1, 1), (1, 1))
        self.assertFalse(snap.exists(), "bad snapshot must be deleted")

    def test_verify_deletes_snapshot_when_not_a_database(self):
        snap = self.backups / "store-garbage.sqlite"
        self.backups.mkdir(parents=True, exist_ok=True)
        snap.write_bytes(b"this is definitely not a sqlite database" * 50)
        with self.assertRaises(store.SnapshotError):
            store.verify_snapshot(snap, (0, 0), (0, 0))
        self.assertFalse(snap.exists())

    def test_verify_deletes_snapshot_when_row_counts_disagree(self):
        snap = self._good_snapshot()
        # Snapshot really holds 1 row; claim the source held 5.
        with self.assertRaises(store.SnapshotError) as cm:
            store.verify_snapshot(snap, (5, 5), (5, 5))
        self.assertIn("row count mismatch", str(cm.exception))
        self.assertFalse(snap.exists())

    def test_cli_failure_leaves_meta_and_retention_untouched(self):
        """A failing backup must exit non-zero, not advance `last_backup`, and
        not prune anything."""
        snap = self._good_snapshot()
        first_meta = self.meta("last_backup")
        self.assertIsNotNone(first_meta)

        # Make the backup dir un-creatable by putting a FILE where it must go.
        blocked = os.path.join(self.tmp, "blocked")
        Path(blocked).write_text("not a directory", encoding="utf-8")

        r = self.run_store("backup", "--out-dir", blocked, "--retention", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("backup FAILED", r.stderr)
        self.assertIn("last_backup NOT updated", r.stderr)
        self.assertEqual(self.meta("last_backup"), first_meta)
        self.assertTrue(snap.exists(), "retention must not run after a failure")
        self.assertEqual(len(self.snapshots()), 1)


# ---------------------------------------------------------------------------
# counts_agree: the concurrent-writer band (unreachable without a real race)
# ---------------------------------------------------------------------------
class CountsAgreeTest(unittest.TestCase):
    def test_quiescent_exact_match_passes(self):
        ok, note = store.counts_agree((10, 7), (10, 7), (10, 7))
        self.assertTrue(ok)
        self.assertIn("exact match", note)

    def test_quiescent_mismatch_fails(self):
        ok, note = store.counts_agree((10, 7), (10, 7), (9, 7))
        self.assertFalse(ok)
        self.assertIn("row count mismatch", note)

    def test_concurrent_insert_within_band_passes(self):
        # A writer added a row mid-copy; the snapshot caught an instant between.
        ok, note = store.counts_agree((10, 7), (11, 8), (10, 7))
        self.assertTrue(ok, note)
        ok, note = store.counts_agree((10, 7), (11, 8), (11, 8))
        self.assertTrue(ok, note)

    def test_concurrent_supersede_moves_live_down_and_still_passes(self):
        # `live` can decrease (tombstone) while `total` cannot — the band must
        # use min/max, not a direction assumption.
        ok, note = store.counts_agree((10, 7), (10, 6), (10, 6))
        self.assertTrue(ok, note)

    def test_outside_band_fails(self):
        ok, note = store.counts_agree((10, 7), (11, 8), (13, 9))
        self.assertFalse(ok)
        self.assertIn("OUTSIDE band", note)


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
class RetentionTest(_StoreCase):
    def test_keeps_newest_n_and_never_touches_anything_else(self):
        self.add("alpha row one")
        self.backups.mkdir(parents=True, exist_ok=True)
        unrelated = self.backups / "UNRELATED-important.txt"
        unrelated.write_text("do not delete me", encoding="utf-8")
        decoy = self.backups / "store-notmatching.txt"   # wrong extension
        decoy.write_text("also not a snapshot", encoding="utf-8")
        pre = self.backups / "prerestore-19990101T000000Z.sqlite"
        pre.write_text("safety copy placeholder", encoding="utf-8")

        for _ in range(9):
            r = self.run_store("backup", "--retention", "7")
            self.assertEqual(r.returncode, 0, r.stderr)

        self.assertEqual(len(self.snapshots()), 7)
        self.assertTrue(unrelated.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "do not delete me")
        self.assertTrue(decoy.exists())
        self.assertTrue(pre.exists(), "prerestore-* must be outside the retention glob")

    def test_oldest_are_the_ones_deleted(self):
        self.add("alpha row one")
        self.backups.mkdir(parents=True, exist_ok=True)
        # Hand-build five snapshots with known, distinct mtimes.
        made = []
        for i in range(5):
            p = self.backups / f"store-2026010{i + 1}T000000Z.sqlite"
            shutil.copyfile(self.store, p)
            os.utime(p, (1000000 + i * 100, 1000000 + i * 100))
            made.append(p)
        removed = store.apply_retention(self.backups, 2)
        self.assertEqual([p.name for p in removed], [p.name for p in made[:3]])
        self.assertEqual(self.snapshots(), sorted(p.name for p in made[3:]))

    def test_retention_zero_disables_pruning(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (self.backups / f"store-2026010{i}T000000Z.sqlite").write_bytes(b"x")
        self.assertEqual(store.apply_retention(self.backups, 0), [])
        self.assertEqual(len(self.snapshots()), 3)

    def test_pruned_snapshot_sidecars_go_with_it(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        made = []
        for i in range(3):
            p = self.backups / f"store-2026010{i}T000000Z.sqlite"
            p.write_bytes(b"x")
            os.utime(p, (1000000 + i * 100, 1000000 + i * 100))
            made.append(p)
        orphan = Path(str(made[0]) + "-wal")
        orphan.write_bytes(b"stale wal")
        store.apply_retention(self.backups, 2)
        self.assertFalse(made[0].exists())
        self.assertFalse(orphan.exists(), "a pruned snapshot's own sidecar must go too")


# ---------------------------------------------------------------------------
# --if-due gating
# ---------------------------------------------------------------------------
class IfDueTest(_StoreCase):
    def _set_last_backup(self, iso: str):
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_backup', ?)",
                (iso,))
            conn.commit()
        finally:
            conn.close()

    def test_first_run_is_always_due(self):
        self.add("alpha row one")
        r = self.run_store("backup", "--if-due")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.snapshots()), 1)

    def test_second_run_inside_interval_skips(self):
        self.add("alpha row one")
        self.assertEqual(self.run_store("backup", "--if-due").returncode, 0)
        r = self.run_store("backup", "--if-due")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not due", r.stdout)
        self.assertEqual(len(self.snapshots()), 1, "no second snapshot inside interval")

    def test_runs_again_once_the_interval_has_elapsed(self):
        self.add("alpha row one")
        self.assertEqual(self.run_store("backup", "--if-due").returncode, 0)
        # Backdate the meta row rather than sleeping for a day.
        self._set_last_backup(time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400)))
        r = self.run_store("backup", "--if-due")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("not due", r.stdout)
        self.assertEqual(len(self.snapshots()), 2)

    def test_interval_env_override(self):
        self.add("alpha row one")
        env = dict(self.env)
        env["ZMEM_BACKUP_INTERVAL_DAYS"] = "0"
        self.assertEqual(self.run_store("backup", "--if-due", env=env).returncode, 0)
        self.assertEqual(self.run_store("backup", "--if-due", env=env).returncode, 0)
        self.assertEqual(len(self.snapshots()), 2)

    def test_without_if_due_always_runs(self):
        self.add("alpha row one")
        self.assertEqual(self.run_store("backup").returncode, 0)
        r = self.run_store("backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("not due", r.stdout)
        self.assertEqual(len(self.snapshots()), 2)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------
class RestoreTest(_StoreCase):
    def _seed_and_snapshot(self) -> Path:
        self.add("ALPHA original row")
        self.add("BRAVO original row")
        self.assertEqual(self.run_store("backup").returncode, 0)
        return self.backups / self.snapshots()[0]

    def _live_contents(self, db: str | None = None) -> set[str]:
        conn = sqlite3.connect(db or self.store)
        try:
            return {r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")}
        finally:
            conn.close()

    def test_refuses_without_force_when_destination_exists(self):
        snap = self._seed_and_snapshot()
        before = self._live_contents()
        r = self.run_store("restore", "--from", str(snap))
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(self._live_contents(), before, "store must be untouched")
        self.assertEqual(list(self.backups.glob("prerestore-*")), [],
                         "a refused restore must not take a pre-restore backup")

    def test_refuses_corrupt_snapshot_before_touching_destination(self):
        snap = self._seed_and_snapshot()
        before = self._live_contents()
        with open(snap, "r+b") as f:
            f.seek(2048)
            f.write(b"\xde\xad\xbe\xef" * 512)
        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._live_contents(), before)
        self.assertEqual(list(self.backups.glob("prerestore-*")), [])

    def test_missing_snapshot_is_refused(self):
        self._seed_and_snapshot()
        r = self.run_store("restore", "--from",
                           os.path.join(self.tmp, "nope.sqlite"), "--force")
        self.assertEqual(r.returncode, 1)
        self.assertIn("snapshot not found", r.stderr)

    def test_round_trip_restores_pre_mutation_state(self):
        snap = self._seed_and_snapshot()
        bravo = self.query(
            "SELECT id FROM memory WHERE content LIKE 'BRAVO%'")[0][0]
        self.assertEqual(self.run_store(
            "supersede", "--id", bravo, "--reason", "mutation").returncode, 0)
        self.add("CHARLIE added after the snapshot")
        mutated = self._live_contents()
        self.assertIn("CHARLIE added after the snapshot", mutated)
        self.assertNotIn("BRAVO original row", mutated)

        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("post-restore integrity_check=ok", r.stdout)

        restored = self._live_contents()
        self.assertEqual(restored,
                         {"ALPHA original row", "BRAVO original row"})

        # The pre-restore safety copy exists and holds the MUTATED state.
        pres = list(self.backups.glob("prerestore-*.sqlite"))
        self.assertEqual(len(pres), 1, "exactly one pre-restore backup")
        self.assertEqual(self._live_contents(str(pres[0])), mutated)

        # No sidecars dragged over from the snapshot / left at the destination.
        self.assertFalse(Path(self.store + "-wal").exists())
        self.assertFalse(Path(self.store + "-shm").exists())

    def test_restore_into_missing_destination_needs_no_force(self):
        snap = self._seed_and_snapshot()
        shutil.copyfile(snap, os.path.join(self.tmp, "keep.sqlite"))
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.store + suffix)
            if p.exists():
                p.unlink()
        r = self.run_store("restore", "--from",
                           os.path.join(self.tmp, "keep.sqlite"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pre-restore backup skipped", r.stdout)
        self.assertEqual(self._live_contents(),
                         {"ALPHA original row", "BRAVO original row"})

    def test_stale_destination_wal_never_survives_a_restore(self):
        """With a live store present, the pre-restore backup opens it first and
        SQLite itself consumes/clears the sidecar; either way no `-wal` from the
        previous store may still be sitting next to the restored file."""
        snap = self._seed_and_snapshot()
        stale = Path(self.store + "-wal")
        stale.write_bytes(b"stale wal frames from the previous store")
        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(stale.exists())
        self.assertEqual(self._live_contents(),
                         {"ALPHA original row", "BRAVO original row"})

    def test_stale_sidecars_are_removed_when_no_pre_restore_backup_runs(self):
        """Empty destination store => pre-restore backup is skipped, so the
        explicit sidecar sweep in restore is the only thing standing between
        the previous store's WAL frames and the restored file."""
        snap = self._seed_and_snapshot()
        shutil.copyfile(snap, os.path.join(self.tmp, "keep.sqlite"))
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.store + suffix)
            if p.exists():
                p.unlink()
        Path(self.store).write_bytes(b"")           # empty destination store
        stale_wal = Path(self.store + "-wal")
        stale_shm = Path(self.store + "-shm")
        stale_wal.write_bytes(b"stale wal frames from the previous store")
        stale_shm.write_bytes(b"stale shm")

        r = self.run_store("restore", "--from",
                           os.path.join(self.tmp, "keep.sqlite"), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pre-restore backup skipped", r.stdout)
        self.assertIn("removed stale", r.stdout)
        self.assertFalse(stale_wal.exists())
        self.assertFalse(stale_shm.exists())
        self.assertEqual(self._live_contents(),
                         {"ALPHA original row", "BRAVO original row"})


# ---------------------------------------------------------------------------
# single-flight lock — host.py primitives
# ---------------------------------------------------------------------------
class LockPrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = Path(self.tmp) / ".zmem-test.lock"

    def test_second_acquire_is_refused_while_first_holds(self):
        t1 = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(t1)
        self.assertIsNone(host.acquire_lock(self.lock, 600))
        host.release_lock(self.lock, t1)
        self.assertFalse(self.lock.exists())
        t2 = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(t2)

    def test_stale_lock_is_broken(self):
        t1 = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(t1)
        old = time.time() - 7200
        os.utime(self.lock, (old, old))
        t2 = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(t2, "an over-age lock must be broken, not obeyed")
        self.assertNotEqual(t1, t2)
        # No `.stale.*` debris left behind by the break.
        self.assertEqual([p.name for p in Path(self.tmp).iterdir()
                          if ".stale." in p.name], [])

    def test_release_ignores_a_lock_it_no_longer_owns(self):
        t1 = host.acquire_lock(self.lock, 600)
        old = time.time() - 7200
        os.utime(self.lock, (old, old))
        t2 = host.acquire_lock(self.lock, 600)  # breaks t1, takes it
        host.release_lock(self.lock, t1)        # stale owner tries to release
        self.assertTrue(self.lock.exists(),
                        "the new owner's lock must survive the old owner's release")
        host.release_lock(self.lock, t2)
        self.assertFalse(self.lock.exists())

    def test_release_with_no_token_is_a_noop(self):
        t = host.acquire_lock(self.lock, 600)
        host.release_lock(self.lock, None)
        self.assertTrue(self.lock.exists())
        host.release_lock(self.lock, t)

    def test_unlocked_degraded_token_never_deletes_a_real_lock(self):
        t = host.acquire_lock(self.lock, 600)
        host.release_lock(self.lock, "unlocked")
        self.assertTrue(self.lock.exists())
        host.release_lock(self.lock, t)


# ---------------------------------------------------------------------------
# single-flight lock — CLI behavior for backup + consolidate
# ---------------------------------------------------------------------------
class SingleFlightCliTest(_StoreCase):
    def _hold(self, name: str) -> Path:
        p = Path(self.tmp) / f".zmem-{name}.lock"
        p.write_text("99999:handmade-by-test", encoding="utf-8")
        return p

    def test_backup_skips_cleanly_when_locked(self):
        self.add("alpha row one")
        lock = self._hold("backup")
        r = self.run_store("backup")
        self.assertEqual(r.returncode, 0, "the loser must exit 0, not crash")
        self.assertIn("another backup is already running", r.stdout)
        self.assertEqual(self.snapshots(), [])
        self.assertIsNone(self.meta("last_backup"))
        self.assertTrue(lock.exists(), "the loser must not release someone else's lock")

    def test_backup_breaks_a_stale_lock(self):
        self.add("alpha row one")
        lock = self._hold("backup")
        old = time.time() - 7200          # >> the 600s backup stale timeout
        os.utime(lock, (old, old))
        r = self.run_store("backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("already running", r.stdout)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertFalse(lock.exists(), "the breaker must release what it took")

    def test_consolidate_skips_cleanly_when_locked(self):
        self.add("always run the linter before committing python code changes")
        self.add("always run the linter before committing python code updates")
        lock = self._hold("consolidate")
        r = self.run_store("consolidate")
        self.assertEqual(r.returncode, 0, "the loser must exit 0, not crash")
        self.assertIn("another consolidation is already running", r.stdout)
        self.assertNotIn("merged", r.stdout)
        live = self.query(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL")[0][0]
        self.assertEqual(live, 2, "the loser must not have done the clustering work")
        self.assertTrue(lock.exists())

    def test_consolidate_breaks_a_stale_lock_and_does_the_work(self):
        self.add("always run the linter before committing python code changes")
        self.add("always run the linter before committing python code updates")
        lock = self._hold("consolidate")
        old = time.time() - 7200          # >> the 1800s consolidate stale timeout
        os.utime(lock, (old, old))
        r = self.run_store("consolidate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("already running", r.stdout)
        self.assertIn("merged", r.stdout)
        live = self.query(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL")[0][0]
        self.assertEqual(live, 1, "the near-duplicate should have been merged")
        self.assertFalse(lock.exists())

    def test_consolidate_dry_run_is_never_gated(self):
        """--dry-run writes nothing, so it must not be blocked by a live lock."""
        self.add("always run the linter before committing python code changes")
        self.add("always run the linter before committing python code updates")
        self._hold("consolidate")
        r = self.run_store("consolidate", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("already running", r.stdout)

    def test_backup_and_consolidate_do_not_block_each_other(self):
        self.add("alpha row one")
        self._hold("consolidate")
        r = self.run_store("backup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.snapshots()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
