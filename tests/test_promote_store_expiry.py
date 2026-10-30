"""promote-store preserves memory_expiry side records (issue #324).

The v15 side table (issue #173) was wired into the writer/reader/CLI but not
into promote-store's copy: a promoted live state row landed WITHOUT its
memory_expiry record and the next update on it failed with "state memories
require an existing memory_expiry row". This driver pins the copy AND its
parent-outcome-aware boundaries: side records land only on live state
parents, never on tombstoned or dedup-remapped ids, and an existing
destination record wins on conflict.

CI runs this file directly (no pytest); the __main__ block executes every
test so a plain `python tests/test_promote_store_expiry.py` is a real gate.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import test_jsonl_sync as T  # noqa: E402  (Store/_TwoStoreCase fixture)


NS = "project:github.com/zaxbysauce/zmem"


def _side_rows(store) -> dict:
    conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        return dict(conn.execute(
            "select memory_id, (expiry_class || '/' || inject_until) "
            "from memory_expiry").fetchall())
    finally:
        conn.close()


def _live_state_ids(store) -> set:
    conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "select id from memory where type='state' "
            "and superseded_at is null")}
    finally:
        conn.close()


def _id_of(store, content_prefix: str) -> str:
    conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select id from memory where content like ? "
            "and superseded_at is null order by rowid desc limit 1",
            (content_prefix + "%",)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class PromoteExpiryTest(T._TwoStoreCase):
    def test_promote_copies_side_record_parent_keyed(self):
        sid = self.a.add(NS, "endpoint api host is example.internal",
                         type_="state", expiry_class="endpoint")
        r = self.b.run("promote-store", "--from", self.a.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        sides = _side_rows(self.b)
        self.assertIn(sid, sides, "destination lost the parent-keyed record")
        self.assertTrue(sides[sid].startswith("endpoint/"),
                        f"wrong record payload: {sides[sid]!r}")

    def test_promoted_row_updates_without_refusal(self):
        sid = self.a.add(NS, "metric query p95 is 120ms",
                         type_="state", expiry_class="metric")
        self.b.run("promote-store", "--from", self.a.path)
        r = self.b.run("update", "--id", sid, "--content",
                       "metric query p95 is 121ms")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_repromote_after_update_never_revives_tombstoned_parent(self):
        sid = self.a.add(NS, "setting pool size is 8",
                         type_="state", expiry_class="setting")
        self.b.run("promote-store", "--from", self.a.path)
        self.b.run("update", "--id", sid, "--content",
                   "setting pool size is 9")
        before_mem = self._count(self.b, "memory")
        before_sides = _side_rows(self.b)
        r = self.b.run("promote-store", "--from", self.a.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._count(self.b, "memory"), before_mem)
        self.assertEqual(_side_rows(self.b), before_sides)
        # No side row may outlive a live state parent.
        orphans = set(_side_rows(self.b)) - _live_state_ids(self.b)
        self.assertEqual(orphans, set(), f"orphan side rows: {orphans}")

    def test_dedup_keeps_keeper_record_and_skips_remapped_parent(self):
        # Destination already holds the same CONTENT under a different id; the
        # source row dedups into that keeper and never lands under its own id,
        # so the source's side record must be skipped (no orphan).
        keeper = self.b.add(NS, "setting pool size is 8",
                            type_="state", expiry_class="setting")
        self.a.add(NS, "setting pool size is 8",
                   type_="state", expiry_class="setting")
        r = self.b.run("promote-store", "--from", self.a.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        sides = _side_rows(self.b)
        self.assertIn(keeper, sides)
        self.assertEqual(set(sides), {keeper},
                         f"expected only the keeper's record: {sides}")

    def test_destination_record_wins_on_conflict(self):
        """PRR-004: the re-promote must hit the REAL INSERT OR IGNORE
        conflict branch (same destination id, differing source record) and
        keep the destination's record. Stage the collision directly: seed
        the destination side row for the source id via SQL (same shape the
        writer produces), so the source record collides on the primary key
        instead of being orphan-skipped."""
        sid = self.a.add(NS, "endpoint api host is example.internal",
                         type_="state", expiry_class="endpoint")
        src_rec = _side_rows(self.a)[sid]
        self.b.run("promote-store", "--from", self.a.path)
        # Repair the destination's record for the SAME id (simulates the
        # destination having moved on): metric instead of endpoint.
        conn = sqlite3.connect(f"file:{self.b.path}", uri=True)
        conn.execute(
            "UPDATE memory_expiry SET expiry_class='metric' "
            "WHERE memory_id=?", (sid,))
        conn.commit()
        conn.close()
        r = self.b.run("promote-store", "--from", self.a.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The summary must show a real conflict outcome: the source record
        # hit INSERT OR IGNORE against an existing row (counted as an
        # existing-skip, not a write).
        self.assertIn("memory_expiry_skipped_existing=1", r.stdout, r.stdout)
        self.assertNotIn("memory_expiry=1,", r.stdout)
        sides = _side_rows(self.b)
        self.assertTrue(sides.get(sid, "").startswith("metric/"),
                        f"dest-wins violated on the SAME id: {sides}")
        self.assertEqual(set(sides), {sid},
                         f"unexpected extra side rows: {sides}")

    def test_repromote_conflict_counts_would_skip(self):
        """FB-10/PRR-016: a re-promote where the destination already holds
        every record reports would-skip/wrote-nothing, not a fresh-copy
        count."""
        sid = self.a.add(NS, "setting pool size is 77",
                         type_="state", expiry_class="setting")
        self.b.run("promote-store", "--from", self.a.path)
        r = self.b.run("promote-store", "--from", self.a.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("memory_expiry_skipped_existing=1", r.stdout, r.stdout)
        self.assertNotIn("memory_expiry=1", r.stdout, r.stdout)

    def test_dry_run_dedup_remap_reports_orphan_skip(self):
        """PRR-005: a source parent that will dedup-remap in the
        destination is reported as an orphan skip by --dry-run, matching
        the real run's outcome class."""
        keeper = self.b.add(NS, "setting pool size is 78",
                            type_="state", expiry_class="setting")
        self.a.add(NS, "setting pool size is 78",
                   type_="state", expiry_class="setting")
        r = self.b.run("promote-store", "--from", self.a.path, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("memory_expiry_orphan_skipped=1", r.stdout, r.stdout)
        self.assertNotIn("memory_expiry=1", r.stdout, r.stdout)

    def test_dry_run_projects_fresh_destination_would_copy(self):
        self.a.add(NS, "setting pool size is 8",
                   type_="state", expiry_class="setting")
        r = self.b.run("promote-store", "--from", self.a.path, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("memory_expiry=1", r.stdout, r.stdout)
        # Nothing was written.
        self.assertEqual(_side_rows(self.b), {})

    @staticmethod
    def _count(store, table: str) -> int:
        conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
        try:
            return conn.execute(f"select count(*) from {table}").fetchone()[0]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
