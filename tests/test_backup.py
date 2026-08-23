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
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:backuptest"

# Point the module-level STORE_PATH at a throwaway location BEFORE importing
# store.py: the real box store at ~/.zmem (i.e. %USERPROFILE%\.zmem) must never
# be the import-time default in a test process.
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
# restore — local-filesystem guard
# ---------------------------------------------------------------------------
# `restore` writes the same live WAL-mode store.sqlite that connect() refuses to
# open on a UNC/network/OneDrive path, so it applies the same guard, and it must
# fire before anything on the destination side is created or touched.
class RestoreLocalFsGuardTest(_StoreCase):
    def _snapshot_from_a_good_store(self) -> str:
        """A verified snapshot built in a normal temp store, to hand to a
        restore aimed at a rejected destination."""
        self.add("ALPHA original row")
        self.assertEqual(self.run_store("backup").returncode, 0)
        return str(self.backups / self.snapshots()[0])

    @unittest.skipUnless(os.name == "nt", "UNC paths are a Windows-only concept")
    def test_refuses_a_unc_destination(self):
        # Windows-gated for the same reason tests/test_host.py's
        # test_rejects_unc_path is: on POSIX `\\fileserver\share\...` is not a
        # UNC path at all, it is an ordinary RELATIVE filename whose every
        # character is legal, so assert_local_fs correctly does not raise and
        # the restore correctly succeeds. The guard itself is cross-platform
        # (test_rejects_forward_slash_unc covers the `//server/share` spelling
        # everywhere); only this Windows SPELLING of a UNC path is not.
        snap = self._snapshot_from_a_good_store()
        env = {**self.env, "ZMEM_STORE": r"\\fileserver\share\zmem\store.sqlite"}
        r = self.run_store("restore", "--from", snap, "--force", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("UNC", r.stderr)
        self.assertIn("restore FAILED", r.stderr)

    def test_refuses_a_onedrive_destination(self):
        snap = self._snapshot_from_a_good_store()
        onedrive = os.path.join(self.tmp, "OneDrive")
        os.makedirs(os.path.join(onedrive, "zmem"), exist_ok=True)
        env = {**self.env,
               "OneDrive": onedrive,
               "ZMEM_STORE": os.path.join(onedrive, "zmem", "store.sqlite")}
        r = self.run_store("restore", "--from", snap, "--force", env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("OneDrive", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(onedrive, "zmem", "store.sqlite")),
                         "the guard must fire before the destination is created")

    def test_a_local_destination_is_still_accepted(self):
        """Control: the guard must not reject an ordinary local temp path."""
        snap = self._snapshot_from_a_good_store()
        r = self.run_store("restore", "--from", snap, "--force")
        self.assertEqual(r.returncode, 0, r.stderr)


# ---------------------------------------------------------------------------
# restore — coordination with the automated background writers
# ---------------------------------------------------------------------------
# `restore` overwrites store.sqlite wholesale, and the SessionStart hook fires
# detached `backup --if-due` and `consolidate` runs that may be mid-flight
# against it. Both are single-flighted on their own lockfiles, so restore takes
# BOTH for its whole duration and refuses (exit 2, destination untouched) if it
# cannot. Exit 2, not 0: unlike a backup — whose lock loss genuinely means
# "someone else is already doing it" — a silently skipped restore would report
# success while the user's data is unchanged.
#
# NOT covered, by design: a live interactive session writing through the normal
# add/recall path takes neither lock. See SKILL.md ("run restore when no session
# is actively writing").
class RestoreSingleFlightTest(_StoreCase):
    def _hold(self, name: str) -> Path:
        p = Path(self.tmp) / f".zmem-{name}.lock"
        p.write_text("99999:handmade-by-test", encoding="utf-8")
        return p

    def _seed_and_snapshot(self) -> Path:
        self.add("ALPHA original row")
        self.assertEqual(self.run_store("backup").returncode, 0)
        return self.backups / self.snapshots()[0]

    def _live_contents(self) -> set:
        conn = sqlite3.connect(self.store)
        try:
            return {r[0] for r in conn.execute(
                "SELECT content FROM memory WHERE superseded_at IS NULL")}
        finally:
            conn.close()

    def _assert_refused_untouched(self, r, held: Path, expect: str):
        self.assertEqual(r.returncode, 2,
                         f"a skipped restore must not look like a completed one: "
                         f"{r.stdout}{r.stderr}")
        self.assertIn(expect, r.stderr)
        self.assertIn("destination untouched", r.stderr)
        self.assertEqual(self._live_contents(), {"ALPHA original row", "CHARLIE added later"})
        self.assertEqual(list(self.backups.glob("prerestore-*")), [],
                         "a refused restore must not take a pre-restore backup")
        self.assertTrue(held.exists(), "the other job's lock must survive untouched")
        self.assertEqual(held.read_text(encoding="utf-8").strip(),
                         "99999:handmade-by-test")

    def test_refuses_while_a_backup_holds_its_lock(self):
        snap = self._seed_and_snapshot()
        self.add("CHARLIE added later")
        lock = self._hold("backup")
        r = self.run_store("restore", "--from", str(snap), "--force")
        self._assert_refused_untouched(r, lock, "a backup is currently running")

    def test_refuses_while_a_consolidate_holds_its_lock(self):
        snap = self._seed_and_snapshot()
        self.add("CHARLIE added later")
        lock = self._hold("consolidate")
        r = self.run_store("restore", "--from", str(snap), "--force")
        self._assert_refused_untouched(r, lock, "a consolidation is currently running")

    def test_the_backup_lock_is_released_when_the_consolidate_lock_is_lost(self):
        """restore takes `backup` first; losing `consolidate` afterwards must
        not strand the one it already holds."""
        snap = self._seed_and_snapshot()
        self.add("CHARLIE added later")
        self._hold("consolidate")
        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((Path(self.tmp) / ".zmem-backup.lock").exists(),
                         "the half-acquired backup lock must be released")
        # Proof it is genuinely free: an ordinary backup still runs.
        self.assertEqual(self.run_store("backup").returncode, 0)

    def test_restore_proceeds_and_releases_both_locks_when_unlocked(self):
        snap = self._seed_and_snapshot()
        self.add("CHARLIE added later")
        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("restore: OK", r.stdout)
        self.assertEqual(self._live_contents(), {"ALPHA original row"})
        for name in ("backup", "consolidate"):
            self.assertFalse((Path(self.tmp) / f".zmem-{name}.lock").exists(),
                             f"restore must not leave the {name} lock behind")

    def test_a_stale_lock_does_not_block_restore_forever(self):
        """Same stale-lease recovery every other lock holder gets: a crashed
        backup must not wedge restore permanently."""
        snap = self._seed_and_snapshot()
        self.add("CHARLIE added later")
        lock = self._hold("backup")
        old = time.time() - 7200          # >> the 600s backup stale timeout
        os.utime(lock, (old, old))
        r = self.run_store("restore", "--from", str(snap), "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._live_contents(), {"ALPHA original row"})


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
# single-flight lock — the stale-break DOUBLE-BREAK race (regression)
# ---------------------------------------------------------------------------
# os.rename(path, victim) moves WHATEVER FILE IS AT `path` when the rename
# runs — it is not bound to the file instance the caller stat'ed and judged
# stale. Before the fix, two processes that both saw one stale lock could
# therefore both "break" it: the second one's rename landed on the FIRST one's
# freshly created, live lock, and both then believed they held the lock. Worse,
# the first one's release_lock (a token-compare against a file that no longer
# exists) silently no-opped, so once the second released, the path was left
# with no lock at all while the first was still running.
#
# The instance-identity check alone did NOT close it, which is what the second
# half of this class pins. A breaker that had moved a live lock aside still had
# to put it back, and between its rename-out and its rename-back the path was
# momentarily EMPTY — so a third acquirer could take the lock and coexist with
# the rightful holder. That leak was intermittent-but-common (2-3 simultaneous
# holders in 277 of 300 local 16-thread runs, and an intermittent ubuntu CI
# failure of test_many_concurrent_breakers_yield_exactly_one_holder). Breaks are
# now SERIALIZED behind a short-lived `<lock>.break` claim whose holder re-stats
# the lock before touching it, so a live lock is never renamed aside at all.
#
# These tests pin both halves. They drive host.acquire_lock directly and
# instrument os.rename / os.stat / os.open to force exact interleavings; every
# barrier wait is bounded so a regression fails the test instead of hanging the
# suite.
class StaleBreakRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-lockrace-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = Path(self.tmp) / ".zmem-race.lock"
        self.real_rename = os.rename
        self.real_link = os.link
        self.real_stat = os.stat
        self.real_open = os.open
        self.addCleanup(setattr, os, "rename", self.real_rename)
        self.addCleanup(setattr, os, "link", self.real_link)
        self.addCleanup(setattr, os, "stat", self.real_stat)
        self.addCleanup(setattr, os, "open", self.real_open)

    def _plant_stale_lock(self, token: str = "crashed-holder-token") -> int:
        """A lockfile left behind by a crashed holder. Returns its st_mtime_ns."""
        self.lock.write_text(token, encoding="utf-8")
        old = time.time() - 7200  # far past the 600s stale timeout used below
        os.utime(self.lock, (old, old))
        return self.lock.stat().st_mtime_ns

    def _claim(self) -> Path:
        """The short-lived file that serializes breaks (host._claim_path)."""
        return Path(str(self.lock) + host._BREAK_CLAIM_SUFFIX)

    def _residue(self) -> list:
        return sorted(p.name for p in Path(self.tmp).glob("*.stale.*"))

    def _rename_spy(self) -> list:
        """Install an os.rename that records every src it is handed. Returns
        the (live) list of recorded srcs."""
        seen: list = []
        real_rename = self.real_rename

        def spy(src, dst, *a, **kw):
            seen.append(str(src))
            return real_rename(src, dst, *a, **kw)

        os.rename = spy
        return seen

    # -- the reviewer's race, with a real second thread --------------------
    def test_two_breakers_of_one_stale_lock_cannot_both_hold(self):
        """A and B both judge the SAME stale instance breakable. B reaches the
        break first and is descheduled mid-rename while holding the break
        claim; A must be EXCLUDED by that claim and skip WITHOUT touching the
        lock. Exactly one of them (B) may end up holding.

        RE-CHOREOGRAPHED, NOT WEAKENED — read this before "fixing" it. The
        invariant under test is unchanged and is still the one in the name:
        two breakers of one stale lock cannot both hold. What changed is the
        protocol that enforces it. Before breaks were serialized, both A and B
        entered the break concurrently; A won, and B — having stat'ed the
        original stale mtime — renamed A's fresh LIVE lock aside and put it
        back. That put-back left the path momentarily EMPTY, and a third
        acquirer landing in that window ended up holding alongside A. That is
        the window the 8-thread stress test below kept catching on ubuntu CI
        (2-3 simultaneous holders in 277/300 local 16-thread runs).

        The assertions below are strictly discriminating against the old
        behavior: with identity-checking alone and no claim, A would acquire
        (assertIsNone(a) fails), A would rename the lock it never verified
        under a claim (assertNotIn(MainThread) fails), and B would come back
        None (assertIsNotNone(b) fails). Deleting the serialization cannot make
        this test pass.
        """
        self._plant_stale_lock()

        b_at_rename = threading.Event()   # B is inside the break, holding the claim
        a_attempted = threading.Event()   # A has had its turn
        state = {"barrier_used": False}
        real_rename = self.real_rename
        renamers: list = []

        def instrumented(src, dst, *a, **kw):
            if str(src) == str(self.lock):
                renamers.append(threading.current_thread().name)
                # Deschedule ONLY B's break-rename. The put-back rename has
                # src == the victim, so the barrier cannot fire twice.
                if (not state["barrier_used"]
                        and threading.current_thread().name == "breaker-B"):
                    state["barrier_used"] = True
                    b_at_rename.set()
                    a_attempted.wait(20)
            return real_rename(src, dst, *a, **kw)

        results = {}

        def run_b():
            results["b"] = host.acquire_lock(self.lock, 600)

        b = threading.Thread(target=run_b, name="breaker-B", daemon=True)
        os.rename = instrumented
        b.start()
        self.assertTrue(b_at_rename.wait(20), "B never reached its break rename")
        self.assertTrue(self._claim().exists(),
                        "B should be holding the break claim at this point")

        # A (this thread) tries to break the same stale instance mid-B-break.
        results["a"] = host.acquire_lock(self.lock, 600)
        a_attempted.set()
        b.join(20)
        os.rename = real_rename
        self.assertFalse(b.is_alive(), "breaker thread B hung")

        self.assertIsNone(
            results["a"],
            "BOTH PROCESSES HOLD THE LOCK: a second breaker entered a break "
            "that was already in progress",
        )
        self.assertNotIn(
            threading.main_thread().name, renamers,
            "A renamed a lock it never verified under the break claim — that "
            "rename is what opens the empty-path window",
        )

        tok_b = results.get("b")
        self.assertIsNotNone(tok_b, "B held the claim and should have won")
        self.assertNotEqual(tok_b, "unlocked", "B should hold a real lock")
        self.assertTrue(self.lock.exists(), "B's live lock was destroyed")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), tok_b)
        self.assertEqual(self._residue(), [], "victim file left behind")
        self.assertFalse(self._claim().exists(),
                         "the break claim must be released on every exit path")

        # And B's release still works — proof its token survived the round trip.
        host.release_lock(self.lock, tok_b)
        self.assertFalse(self.lock.exists())

    # -- the serialization itself, deterministically ------------------------
    def test_a_live_break_claim_leaves_the_stale_lock_untouched(self):
        """A break already in progress must be joined by nobody. The mtime and
        no-rename assertions are the load-bearing ones: returning None is not
        enough, the lock must not have been MOVED, because it is the move that
        empties the path."""
        planted = self._plant_stale_lock()
        claim = self._claim()
        claim.write_text("", encoding="utf-8")   # fresh => a breaker is mid-break
        renamed = self._rename_spy()
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.rename = self.real_rename

        self.assertIsNone(tok, "a break already in progress must not be joined")
        self.assertEqual(renamed, [],
                         "the lock was renamed by a breaker that never held the claim")
        self.assertEqual(self.lock.stat().st_mtime_ns, planted,
                         "the stale lock must be left exactly as it was found")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "crashed-holder-token")
        self.assertEqual(self._residue(), [])
        self.assertTrue(claim.exists(), "another breaker's live claim must survive")

    def test_a_lock_that_went_live_under_us_is_never_renamed(self):
        """The re-stat under the claim, deterministically. acquire_lock's
        staleness stat sees the crashed holder's lock; by the time the claim is
        held, another process has broken it and installed its own LIVE lock.
        The break must be abandoned with no rename at all — renaming here is
        exactly what used to empty the path and let a third party acquire
        alongside the rightful holder."""
        self._plant_stale_lock()
        real_stat = self.real_stat
        seen = {"n": 0}

        def stat_spy(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            if str(path) == str(self.lock):
                seen["n"] += 1
                if seen["n"] == 1:
                    # Between the staleness stat and the re-stat under the
                    # claim: someone else breaks it and takes it.
                    os.unlink(str(self.lock))
                    self.lock.write_text("live-successor-token", encoding="utf-8")
            return st

        renamed = self._rename_spy()
        os.stat = stat_spy
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.stat, os.rename = real_stat, self.real_rename

        self.assertGreaterEqual(seen["n"], 2,
                                "the re-stat under the break claim never happened")
        self.assertIsNone(tok, "must not acquire alongside the live successor")
        self.assertEqual(renamed, [],
                         "a LIVE lock was renamed aside — the empty-path window is back")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "live-successor-token")
        self.assertEqual(self._residue(), [])
        self.assertFalse(self._claim().exists(),
                         "the claim must be released when the break is abandoned")

    # -- the claim must never become a wedge --------------------------------
    def test_an_orphaned_break_claim_is_reclaimed(self):
        """A breaker killed mid-break parks its claim forever. The claim's own
        (much shorter) lease must expire and let stale recovery resume — the
        historical objection to a claim file was precisely that it could wedge
        the lock permanently."""
        self._plant_stale_lock()
        claim = self._claim()
        claim.write_text("", encoding="utf-8")
        dead = time.time() - 300          # >> the 30s claim lease
        os.utime(claim, (dead, dead))

        tok = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(tok, "an orphaned break claim must not wedge stale recovery")
        self.assertNotEqual(tok, "unlocked")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), tok)
        self.assertEqual(self._residue(), [])
        self.assertFalse(claim.exists(), "the reclaimed claim must be released")

    def test_a_transient_claim_create_error_is_not_read_as_unusable(self):
        """Windows leaves an unlinked name in a delete-pending state in which a
        concurrent O_EXCL create fails with EACCES rather than EEXIST — and a
        hot break loop unlinks this claim constantly. Reading that transient as
        "the claim mechanism is unusable" silently drops the caller back onto
        the UNSERIALIZED break. Every one of the residual double-holds measured
        while this retry was missing (8 per 300 16-thread runs) came from
        exactly that misclassification, so this is a correctness test, not a
        flake-suppressor: only a PERSISTENT failure may mean "unusable"."""
        calls = {"n": 0}
        real_open = self.real_open

        def flaky(path, *a, **kw):
            if str(path).endswith(host._BREAK_CLAIM_SUFFIX):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "unlink still pending")
            return real_open(path, *a, **kw)

        os.open = flaky
        try:
            state = host._create_break_claim(self._claim())
        finally:
            os.open = real_open

        self.assertEqual(state, host._CLAIM_ACQUIRED,
                         "a transient claim-create error must not disable serialization")
        self.assertGreaterEqual(calls["n"], 2, "the transient failure was never retried")

    def test_an_unusable_break_claim_degrades_instead_of_wedging(self):
        """Fail-open floor. If the claim file PERSISTENTLY cannot be created
        for a reason that is not contention (read-only fs), the break proceeds
        UNSERIALIZED — i.e. pre-change behavior, identity check still intact.
        Declining the break instead would leave a crashed holder's lock in
        place forever, and this API promises never to wedge."""
        self._plant_stale_lock()
        real_open = self.real_open

        def refuse_the_claim(path, *a, **kw):
            if str(path).endswith(host._BREAK_CLAIM_SUFFIX):
                raise PermissionError(13, "claim path is not writable")
            return real_open(path, *a, **kw)

        os.open = refuse_the_claim
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.open = real_open

        self.assertIsNotNone(tok, "an unusable claim must not wedge stale recovery")
        self.assertNotEqual(tok, "unlocked")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), tok)
        self.assertEqual(self._residue(), [])
        self.assertFalse(self._claim().exists())

    # -- same race, deterministic single-threaded form ---------------------
    def test_breaker_restores_a_lock_it_had_no_right_to_move(self):
        """Force the mismatch with no threads: between our stat and our rename,
        another breaker replaces the stale lock with its own live one."""
        self._plant_stale_lock()
        real_rename = self.real_rename
        swapped = {"done": False}

        def instrumented(src, dst, *a, **kw):
            if not swapped["done"] and str(src) == str(self.lock):
                swapped["done"] = True
                # Someone else broke the stale lock and installed a fresh one.
                os.unlink(str(self.lock))
                self.lock.write_text("other-holder-token", encoding="utf-8")
            return real_rename(src, dst, *a, **kw)

        os.rename = instrumented
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.rename = real_rename

        self.assertIsNone(tok, "must not acquire after moving a live lock")
        self.assertTrue(self.lock.exists(), "the live lock must be put back")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "other-holder-token")
        self.assertEqual(self._residue(), [])

    def test_failed_put_back_still_never_grants_the_lock(self):
        """Three-way: our put-back rename fails because a third process took
        the empty path in the gap. We must drop our victim copy and give up —
        never acquire on the strength of a break we could not confirm."""
        self._plant_stale_lock()
        real_rename, real_link = self.real_rename, self.real_link
        steps = {"stolen": False, "third_party": False}

        def third_party_grabs_the_free_path():
            if not steps["third_party"]:
                steps["third_party"] = True
                self.lock.write_text("third-party-token", encoding="utf-8")

        def instrumented_rename(src, dst, *a, **kw):
            if not steps["stolen"] and str(src) == str(self.lock):
                steps["stolen"] = True
                os.unlink(str(self.lock))
                self.lock.write_text("other-holder-token", encoding="utf-8")
            elif ".stale." in str(src):
                third_party_grabs_the_free_path()
            return real_rename(src, dst, *a, **kw)

        def instrumented_link(src, dst, *a, **kw):
            # _rename_noreplace's POSIX limb puts the lock back with os.link,
            # not os.rename, so the injection has to hook both to reach the
            # put-back on every platform CI runs.
            if ".stale." in str(src):
                third_party_grabs_the_free_path()
            return real_link(src, dst, *a, **kw)

        os.rename = instrumented_rename
        os.link = instrumented_link
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.rename, os.link = real_rename, real_link

        self.assertTrue(steps["third_party"], "put-back was never attempted")
        self.assertIsNone(tok, "must not acquire when the put-back failed")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "third-party-token",
                         "the third party's lock must not be clobbered")
        self.assertEqual(self._residue(), [], "victim copy must be dropped")

    def test_single_breaker_of_a_genuinely_stale_lock_still_wins(self):
        """The identity check must not break the ordinary crashed-holder path."""
        self._plant_stale_lock()
        tok = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(tok)
        self.assertNotEqual(tok, "unlocked")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), tok)
        self.assertEqual(self._residue(), [])
        host.release_lock(self.lock, tok)
        self.assertFalse(self.lock.exists())

    def test_many_concurrent_breakers_yield_exactly_one_holder(self):
        """Unsynchronized stress form of the same race."""
        self._plant_stale_lock()
        results = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def worker():
            start.wait(20)
            t = host.acquire_lock(self.lock, 600)
            with lock:
                results.append(t)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
            self.assertFalse(t.is_alive(), "a breaker thread hung")

        winners = [t for t in results if t is not None and t != "unlocked"]
        self.assertEqual(len(winners), 1,
                         f"exactly one breaker may win, got {len(winners)}")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), winners[0])
        self.assertEqual(self._residue(), [])

    # -- the no-clobber rename helper's contract ---------------------------
    def test_rename_noreplace_refuses_an_existing_destination(self):
        """os.rename silently REPLACES dst on POSIX; the put-back path must
        never do that to a third party's live lock. CI runs ubuntu-latest, so
        this contract is exercised on both platforms."""
        src = Path(self.tmp) / "src.tmp"
        dst = Path(self.tmp) / "dst.tmp"
        src.write_text("src", encoding="utf-8")
        dst.write_text("dst", encoding="utf-8")
        self.assertFalse(host._rename_noreplace(str(src), str(dst)))
        self.assertEqual(dst.read_text(encoding="utf-8"), "dst")
        self.assertTrue(src.exists(), "src must survive a refused rename")

        dst.unlink()
        self.assertTrue(host._rename_noreplace(str(src), str(dst)))
        self.assertEqual(dst.read_text(encoding="utf-8"), "src")
        self.assertFalse(src.exists(), "src must be gone after a real rename")


# ---------------------------------------------------------------------------
# single-flight lock — a lock file whose token write failed
# ---------------------------------------------------------------------------
# The lock's EXISTENCE is the lock, but its CONTENT is the owner's identity.
# A create that succeeds while the token write fails leaves an EMPTY lock file
# that no release_lock token-compare can ever match — the "owner" holds a lock
# it can never release, and the path stays guarded until the (minutes-long)
# stale timeout elapses. _try_create_lock must instead drop the broken file and
# report the unexpected-OSError outcome (None => caller proceeds unlocked).
class LockTokenWriteFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-lockwrite-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = Path(self.tmp) / ".zmem-write.lock"
        self.real_write = os.write
        self.addCleanup(setattr, os, "write", self.real_write)

    def test_failed_token_write_returns_none_and_leaves_no_lock(self):
        def boom(fd, data, *a, **kw):
            raise OSError(28, "No space left on device")

        os.write = boom
        try:
            result = host._try_create_lock(self.lock, "tok")
        finally:
            os.write = self.real_write

        self.assertIsNone(result, "a failed token write must not report success")
        self.assertFalse(self.lock.exists(),
                         "an empty, unreleasable lock file must not be left behind")

    def test_acquire_degrades_to_unlocked_when_the_token_write_fails(self):
        def boom(fd, data, *a, **kw):
            raise OSError(28, "No space left on device")

        os.write = boom
        try:
            tok = host.acquire_lock(self.lock, 600)
        finally:
            os.write = self.real_write

        self.assertEqual(tok, "unlocked",
                         "caller must degrade to running unlocked, not hold a "
                         "token it can never release")
        self.assertFalse(self.lock.exists())

    def test_normal_create_still_writes_the_token(self):
        self.assertIs(host._try_create_lock(self.lock, "tok"), True)
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), "tok")


# ---------------------------------------------------------------------------
# single-flight lock — release racing a stale break (regression)
# ---------------------------------------------------------------------------
# release_lock used to read the file's content, compare it to the caller's
# token, and then unlink THE PATH in a separate, later syscall. Between the two,
# our lock can be judged stale and broken (possible whenever a releaser runs
# past its own stale timeout while genuinely still working) and a fresh, LIVE
# lock installed at the same path by another process — which the unlink then
# destroyed. Same class of bug as the acquire-path double-break, so the same
# rename-then-confirm-identity pattern applies, with the token itself as the
# identity witness (strictly stronger than _break_stale_lock's mtime).
class ReleaseBreakRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-relrace-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = Path(self.tmp) / ".zmem-rel.lock"
        self.real_rename = os.rename
        self.addCleanup(setattr, os, "rename", self.real_rename)

    def _residue(self) -> list:
        return sorted(p.name for p in Path(self.tmp).iterdir()
                      if ".stale." in p.name or ".release." in p.name)

    def test_release_never_deletes_a_live_lock_installed_after_our_check(self):
        """Our lock is broken as stale and replaced by another process's LIVE
        lock in the window between release_lock's identity check and its
        unlink. The stranger's lock must survive, untouched."""
        tok = host.acquire_lock(self.lock, 600)
        self.assertIsNotNone(tok)
        swapped = {"done": False}
        real_rename = self.real_rename

        def instrumented(src, dst, *a, **kw):
            if not swapped["done"] and str(src) == str(self.lock):
                swapped["done"] = True
                # Another process judged our lock stale, broke it, and took it.
                os.unlink(str(self.lock))
                self.lock.write_text("other-holder-token", encoding="utf-8")
            return real_rename(src, dst, *a, **kw)

        os.rename = instrumented
        try:
            host.release_lock(self.lock, tok)
        finally:
            os.rename = real_rename

        self.assertTrue(swapped["done"], "the release never renamed anything")
        self.assertTrue(self.lock.exists(),
                        "THE OTHER PROCESS'S LIVE LOCK WAS DELETED by a stale "
                        "releaser's unlink")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "other-holder-token")
        self.assertEqual(self._residue(), [], "aside copy left behind")

    def test_release_of_an_already_broken_lock_moves_nothing(self):
        """The common already-broken case must stay a pure read-only no-op: no
        rename at all, so it never opens a momentarily-empty-path window on
        somebody else's live lock."""
        tok = host.acquire_lock(self.lock, 600)
        old = time.time() - 7200
        os.utime(self.lock, (old, old))
        tok2 = host.acquire_lock(self.lock, 600)  # breaks ours, takes it
        self.assertIsNotNone(tok2)

        renames = []

        def instrumented(src, dst, *a, **kw):
            renames.append((str(src), str(dst)))
            return self.real_rename(src, dst, *a, **kw)

        os.rename = instrumented
        try:
            host.release_lock(self.lock, tok)
        finally:
            os.rename = self.real_rename

        self.assertEqual(renames, [], "a no-op release must not rename anything")
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(), tok2)
        host.release_lock(self.lock, tok2)
        self.assertFalse(self.lock.exists())

    def test_failed_put_back_drops_the_aside_copy_and_never_clobbers(self):
        """A third process takes the free path while we are putting the
        stranger's lock back. Its lock must win and no debris may remain."""
        tok = host.acquire_lock(self.lock, 600)
        real_rename, real_link = self.real_rename, os.link
        self.addCleanup(setattr, os, "link", real_link)
        steps = {"swapped": False, "third_party": False}

        def third_party_grabs_the_free_path():
            if not steps["third_party"]:
                steps["third_party"] = True
                self.lock.write_text("third-party-token", encoding="utf-8")

        def instrumented_rename(src, dst, *a, **kw):
            if not steps["swapped"] and str(src) == str(self.lock):
                steps["swapped"] = True
                os.unlink(str(self.lock))
                self.lock.write_text("other-holder-token", encoding="utf-8")
            elif ".release." in str(src):
                third_party_grabs_the_free_path()
            return real_rename(src, dst, *a, **kw)

        def instrumented_link(src, dst, *a, **kw):
            # _rename_noreplace's POSIX limb puts the file back with os.link.
            if ".release." in str(src):
                third_party_grabs_the_free_path()
            return real_link(src, dst, *a, **kw)

        os.rename = instrumented_rename
        os.link = instrumented_link
        try:
            host.release_lock(self.lock, tok)
        finally:
            os.rename, os.link = real_rename, real_link

        self.assertTrue(steps["third_party"], "put-back was never attempted")
        self.assertEqual(self.lock.read_text(encoding="utf-8").strip(),
                         "third-party-token",
                         "the third party's lock must not be clobbered")
        self.assertEqual(self._residue(), [], "aside copy must be dropped")

    def test_ordinary_release_still_removes_our_own_lock(self):
        tok = host.acquire_lock(self.lock, 600)
        host.release_lock(self.lock, tok)
        self.assertFalse(self.lock.exists())
        self.assertEqual(self._residue(), [])

    def test_release_survives_a_rename_that_fails(self):
        """If the aside-rename itself fails (sharing violation, already gone),
        release is a silent no-op — never raises, never falls back to a blind
        unlink."""
        tok = host.acquire_lock(self.lock, 600)

        def boom(src, dst, *a, **kw):
            raise OSError(13, "denied")

        os.rename = boom
        try:
            host.release_lock(self.lock, tok)
        finally:
            os.rename = self.real_rename

        self.assertTrue(self.lock.exists())
        self.assertEqual(self._residue(), [])


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
