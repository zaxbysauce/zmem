"""Issue #527 regression: NULL content_norm rows written AFTER the repair
markers heal on the next open, while clean stores stay zero-write.

Root cause (fixed in storelib/schema.py): the 162 repair and the 163
verification are one-shot marker lanes, so a NULL content_norm row planted
later by a raw external sqlite3 writer (the #491 writer class; every
supported writer materializes the norm at admission) was skipped by both
lanes forever. The unmarked post-stamp NULL lane
(_heal_null_content_norms_post_stamp) probes for NULLs on every open and
heals what it finds.

Legs:
- (a) end-to-end #527 shape: stale row heals + markers land, raw-external
  NULL row planted, reopen heals it to the CANONICAL normalize of a row
  that NEEDS normalization ('  Raw   NULL\\nBody  ' -> 'raw null body'),
  not a copy of content (closes the content==norm tautology).
- (b) zero-write floor: a clean store's steady-state open leaves the store
  file byte-identical (the first open lands the one-time markers and is
  snapshotted AFTER it, mirroring the byte-identical contracts).
- (c) undecodable-content NULL keeps NULL (skip semantics).
- (d) tombstoned NULL heals (both existing lanes heal tombstoned rows).
- (e) caller-owned transaction: a caller rollback after a successful heal
  undoes the heal (the lane rides the caller's transaction, savepoint only).
- (f) own-transaction lane failure releases the write lock (no stranded
  BEGIN) — a RAISE(ABORT) trigger on the memory table aborts the batch,
  the diagnostic goes to stderr, and migrate() returns normally.
- (g) second-compare-miss conflict (RAISE(IGNORE)-style): the CURRENT batch
  rolls back (its own earlier updates included — asserted on a same-batch
  row), EARLIER committed batches stay healed (observed from a second
  connection), one diagnostic line on stderr, no raise out of migrate().
- (h) caller-owned mid-batch failure (RAISE(ABORT) after ≥1 update inside
  the failing batch): the lane's savepoint bounds the undo, the caller's
  own pending work survives, migrate() returns, and the caller's rollback
  still governs everything (implementation-review round 1).

Hermetic discipline: ZMEM_STORE/ZMEM_DATA pinned BEFORE the first storelib
import (setUpModule), restored in tearDownModule.

Run (the file is its own runner):
    python -X utf8 tests/test_null_lane_post_marker.py
Prints a final CHECK-PASS / CHECK-FAIL line.
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "skills", "memory", "scripts")

_SAVED_ENV = {}


def setUpModule():
    scratch = tempfile.mkdtemp(prefix="zw-527-null-")
    _SAVED_ENV["scratch"] = scratch
    for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_NAMESPACE",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        _SAVED_ENV[key] = os.environ.pop(key, None)
    os.environ["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
    os.environ["ZMEM_DATA"] = scratch
    sys.path.insert(0, SCRIPTS)


def tearDownModule():
    for key, value in _SAVED_ENV.items():
        if key == "scratch":
            shutil.rmtree(value, ignore_errors=True)
        elif value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _norm(text):
    import re
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class NullLanePostMarkerTest(unittest.TestCase):

    def _build_marked_store(self, path):
        """init + a stale row so the first migrate heals it and lands both
        markers, then close. Returns the store path."""
        from storelib.schema import init_db, migrate
        conn = _connect(path)
        init_db(conn)
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags,"
            " source_ref, confidence, signal, ingestion_ts, content_norm)"
            " VALUES ('527000a1-0000-4000-8000-000000000001','ns','lesson',"
            " 'stale body','[]','probe',0.5,'test','2026-01-01T00:00:00Z',"
            " 'WRONG-NORM')")
        conn.commit()
        conn.close()
        conn = _connect(path)
        migrate(conn)
        markers = {r[0] for r in conn.execute("SELECT key FROM meta")}
        conn.close()
        assert "content_norm_repair_162" in markers
        assert "content_norm_verify_163" in markers
        return path

    def test_a_post_marker_null_heals_canonically(self):
        from storelib.schema import migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-a-")
        try:
            store = self._build_marked_store(os.path.join(scratch, "s.sqlite"))
            raw = sqlite3.connect(store)  # the external writer class
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " content_norm) VALUES ("
                "'527000a2-0000-4000-8000-000000000002','ns','lesson',"
                " ?,'[]','probe',0.5,'test','2026-01-02T00:00:00Z',NULL)",
                ("  Raw   NULL\nBody  ",))
            raw.commit()
            raw.close()
            conn = _connect(store)
            migrate(conn)
            val = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'527000a2-0000-4000-8000-000000000002'").fetchone()[0]
            conn.close()
            self.assertEqual(
                val, "raw null body",
                "post-marker NULL must heal to the CANONICAL normalize of "
                f"its content, got {val!r}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_b_clean_store_open_is_byte_identical(self):
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-b-")
        try:
            store = os.path.join(scratch, "s.sqlite")
            conn = _connect(store)
            init_db(conn)
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " content_norm) VALUES ("
                "'527000b1-0000-4000-8000-000000000001','ns','lesson',"
                " 'clean body','[]','probe',0.5,'test',"
                " '2026-01-01T00:00:00Z','clean body')")
            conn.commit()
            conn.close()
            # First open lands the one-time markers; snapshot AFTER it so
            # the measured reopen is the steady state (mirrors the
            # byte-identical contracts the way PR #525's CI round 1 did).
            conn = _connect(store)
            migrate(conn)
            conn.close()
            before = open(store, "rb").read()
            conn = _connect(store)
            migrate(conn)
            conn.close()
            after = open(store, "rb").read()
            self.assertEqual(
                before, after,
                "a clean store's steady-state open must write nothing "
                "(zero-write floor)")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_c_undecodable_null_keeps_null(self):
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-c-")
        try:
            store = os.path.join(scratch, "s.sqlite")
            conn = _connect(store)
            init_db(conn)
            conn.close()
            conn = _connect(store)
            migrate(conn)  # land markers on the empty store
            conn.close()
            raw = sqlite3.connect(store)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " content_norm) VALUES ("
                "'527000c1-0000-4000-8000-000000000001','ns','lesson',"
                " ?,'[]','probe',0.5,'test','2026-01-01T00:00:00Z',NULL)",
                (b"\xff\xfe undecodable",))
            raw.commit()
            raw.close()
            conn = _connect(store)
            conn.text_factory = str
            migrate(conn)
            row = conn.execute(
                "SELECT typeof(content_norm) FROM memory WHERE id="
                "'527000c1-0000-4000-8000-000000000001'").fetchone()
            conn.close()
            self.assertEqual(
                row[0], "null",
                "undecodable-content NULL rows must keep NULL (skip "
                "semantics, pinned since PR #525)")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_d_tombstoned_null_heals(self):
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-d-")
        try:
            store = self._build_marked_store(os.path.join(scratch, "s.sqlite"))
            raw = sqlite3.connect(store)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, valid_from, valid_until,"
                " superseded_at, ingestion_ts, content_norm) VALUES ("
                "'527000d1-0000-4000-8000-000000000001','ns','lesson',"
                " 'tombstoned body','[]','probe',0.5,'test',"
                " '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z',"
                " '2026-01-02T00:00:00Z','2026-01-01T00:00:00Z',NULL)")
            raw.commit()
            raw.close()
            conn = _connect(store)
            migrate(conn)
            val = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'527000d1-0000-4000-8000-000000000001'").fetchone()[0]
            conn.close()
            self.assertEqual(
                val, "tombstoned body",
                "tombstoned NULL rows heal too (both existing lanes heal "
                "them; pinned at test_followup_n493_supp.py:1701-1725)")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_e_caller_rollback_undoes_heal(self):
        from storelib.schema import migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-e-")
        try:
            store = self._build_marked_store(os.path.join(scratch, "s.sqlite"))
            raw = sqlite3.connect(store)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " content_norm) VALUES ("
                "'527000e1-0000-4000-8000-000000000001','ns','lesson',"
                " 'caller body','[]','probe',0.5,'test',"
                " '2026-01-01T00:00:00Z',NULL)")
            raw.commit()
            raw.close()
            conn = _connect(store)
            conn.execute("BEGIN")
            conn.execute("SAVEPOINT caller_work")
            err = io.StringIO()
            with redirect_stderr(err):
                migrate(conn)  # heal rides the caller transaction
            healed = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'527000e1-0000-4000-8000-000000000001'").fetchone()[0]
            self.assertEqual(healed, "caller body")
            conn.rollback()  # caller rolls back its OWN transaction
            val = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'527000e1-0000-4000-8000-000000000001'").fetchone()[0]
            conn.close()
            self.assertIsNone(
                val,
                "a caller rollback must undo the heal — the lane never "
                "commits caller work itself")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_f_own_tx_failure_releases_lock(self):
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-f-")
        try:
            store = os.path.join(scratch, "s.sqlite")
            conn = _connect(store)
            init_db(conn)
            conn.close()
            conn = _connect(store)
            migrate(conn)
            conn.close()
            raw = sqlite3.connect(store)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " content_norm) VALUES ("
                "'527000f1-0000-4000-8000-000000000001','ns','lesson',"
                " 'boom body','[]','probe',0.5,'test',"
                " '2026-01-01T00:00:00Z',NULL)")
            raw.execute(
                "CREATE TRIGGER fail_null_heal BEFORE UPDATE ON memory "
                "WHEN NEW.content_norm IS NOT NULL AND OLD.content_norm "
                "IS NULL BEGIN SELECT RAISE(ABORT, 'boom'); END")
            raw.commit()
            raw.close()
            conn = _connect(store)
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    migrate(conn)  # must NOT raise
            finally:
                in_tx = conn.in_transaction
                conn.close()
            self.assertFalse(
                in_tx,
                "a lane failure on the own-transaction path must release "
                "the write lock (no stranded BEGIN)")
            self.assertIn("NULL content_norm heal deferred", err.getvalue())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_g_second_miss_conflict_keeps_earlier_batches(self):
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-g-")
        try:
            store = os.path.join(scratch, "s.sqlite")
            conn = _connect(store)
            init_db(conn)
            conn.close()
            conn = _connect(store)
            migrate(conn)
            conn.close()
            # Seed 600 NULL rows -> the lane's batch size (500) makes TWO
            # batches: rows 0-499 (batch 1) and rows 500-599 (batch 2). A
            # RAISE(IGNORE) trigger on row 599 makes every UPDATE of that
            # row a silent zero-row miss; the fresh-read retry hits the
            # same IGNORE, and the second miss is the conflict path —
            # which must roll back ONLY batch 2 (rows 500-598 that healed
            # inside it before the trigger) while batch 1 stays healed.
            raw = sqlite3.connect(store)
            for i in range(600):
                raw.execute(
                    "INSERT INTO memory (id, namespace, type, content,"
                    " tags, source_ref, confidence, signal, ingestion_ts,"
                    " content_norm) VALUES (?, 'ns','lesson', ?, '[]',"
                    " 'probe',0.5,'test','2026-01-01T00:00:00Z',NULL)",
                    (f"5270007{i:03d}-0000-4000-8000-00000000000{i % 10}",
                     f"row number {i} body"))
            raw.commit()
            raw.close()
            raw = sqlite3.connect(store)
            raw.execute(
                "CREATE TRIGGER ignore_last_null BEFORE UPDATE ON memory "
                "WHEN NEW.id = '5270007599-0000-4000-8000-000000000009' "
                "AND OLD.content_norm IS NULL BEGIN SELECT RAISE(IGNORE);"
                " END")
            raw.commit()
            raw.close()
            conn = _connect(store)
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    migrate(conn)  # must NOT raise
            finally:
                conn.close()
            self.assertIn(
                "NULL content_norm heal deferred", err.getvalue(),
                "the conflict path emits its one-line diagnostic")
            # Observe from a fresh connection: batch-1 rows stay healed
            # (per-batch durability) AND the failing batch's own earlier
            # updates were rolled back (implementation-review round 1:
            # the oracle must discriminate, not just prove healed_early>0).
            check = _connect(store)
            batch1_first = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'5270007000-0000-4000-8000-000000000000'").fetchone()[0]
            batch2_before_trigger = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'5270007500-0000-4000-8000-000000000000'").fetchone()[0]
            trigger_row = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'5270007599-0000-4000-8000-000000000009'").fetchone()[0]
            healed_total = check.execute(
                "SELECT COUNT(*) FROM memory WHERE content_norm IS NOT "
                "NULL AND id LIKE '527000%'").fetchone()[0]
            check.close()
            self.assertEqual(
                batch1_first, "row number 0 body",
                "batch 1 stays healed (per-batch durability)")
            self.assertIsNone(
                batch2_before_trigger,
                "the FAILING batch's own earlier updates roll back with it")
            self.assertIsNone(
                trigger_row, "the conflicted row itself stays NULL")
            self.assertGreater(
                healed_total, 0,
                "earlier committed batches REMAIN healed after the "
                "conflict abort")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_h_caller_tx_midbatch_failure_preserves_caller_work(self):
        """Implementation-review round 1: under a CALLER-owned transaction,
        a mid-batch lane failure (after at least one update INSIDE the
        failing batch) must undo only the lane's batch savepoint while the
        caller's own prior work stays pending and the caller's rollback
        still governs everything."""
        from storelib.schema import init_db, migrate
        scratch = tempfile.mkdtemp(prefix="zw-527-h-")
        try:
            store = os.path.join(scratch, "s.sqlite")
            conn = _connect(store)
            init_db(conn)
            conn.close()
            conn = _connect(store)
            migrate(conn)
            conn.close()
            raw = sqlite3.connect(store)
            for i in range(3):
                raw.execute(
                    "INSERT INTO memory (id, namespace, type, content,"
                    " tags, source_ref, confidence, signal, ingestion_ts,"
                    " content_norm) VALUES (?, 'ns','lesson', ?, '[]',"
                    " 'probe',0.5,'test','2026-01-01T00:00:00Z',NULL)",
                    (f"5270008{i:01d}-0000-4000-8000-00000000000{i}",
                     f"caller batch row {i} body"))
            # Row 1 heals, then row 1's... no: trigger fires on row 2, so
            # row 0 and row 1 update INSIDE the failing batch first.
            raw.execute(
                "CREATE TRIGGER abort_third_null BEFORE UPDATE ON memory "
                "WHEN NEW.id = '52700082-0000-4000-8000-000000000002' "
                "AND OLD.content_norm IS NULL BEGIN SELECT RAISE(ABORT,"
                " 'boom'); END")
            raw.commit()
            raw.close()
            conn = _connect(store)
            err = io.StringIO()
            try:
                conn.execute("BEGIN")
                # caller's OWN prior work inside its transaction
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES "
                    "('caller_marker_527h', 'pending')")
                with redirect_stderr(err):
                    migrate(conn)  # must NOT raise
                # caller work still pending and visible
                marker = conn.execute(
                    "SELECT value FROM meta WHERE key="
                    "'caller_marker_527h'").fetchone()
                self.assertEqual(marker[0], "pending")
                # the failing batch's earlier updates were rolled back to
                # the lane savepoint: rows 0/1 stay NULL
                row0 = conn.execute(
                    "SELECT content_norm FROM memory WHERE id="
                    "'52700080-0000-4000-8000-000000000000'").fetchone()[0]
                row1 = conn.execute(
                    "SELECT content_norm FROM memory WHERE id="
                    "'52700081-0000-4000-8000-000000000001'").fetchone()[0]
                self.assertIsNone(row0)
                self.assertIsNone(row1)
                self.assertIn("NULL content_norm heal deferred",
                              err.getvalue())
                # caller rollback governs everything
                conn.rollback()
                gone = conn.execute(
                    "SELECT value FROM meta WHERE key="
                    "'caller_marker_527h'").fetchone()
                self.assertIsNone(gone,
                                  "caller rollback undoes the caller's own "
                                  "work (untouched by the lane)")
            finally:
                conn.close()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(NullLanePostMarkerTest))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("CHECK-PASS null-lane post-marker regression")
        return 0
    print("CHECK-FAIL null-lane post-marker regression")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
