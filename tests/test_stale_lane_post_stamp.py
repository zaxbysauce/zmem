"""Issue #533 acceptance: a STALE non-NULL content_norm row planted by a
raw external sqlite3 writer AFTER the content_norm_verify_163 stamp must
heal to the canonical normalize of its own content by the end of the next
open.

Combined lane (issues #527 + #533): _heal_content_norms_post_stamp heals
BOTH the NULL shape (#527) and the STALE non-NULL shape (#533) on every
open, with deliberately no marker of its own. The non-NULL shape was the
gap this test pins: _verify_content_norms_163 early-returns once its
one-time stamp exists, so a stale non-NULL norm inserted after the stamp
would otherwise survive every reopen — permanently wrong for every
content_norm-keyed dedup/re-rank window (the #486 ingest contract).

Second pin (green at base, must stay green): a clean store's open writes
NOTHING — after init_db+migrate has settled, one more migrate leaves the
store file byte-identical (the PR #525 zero-write discipline the new heal
lane must preserve).

Run: python -X utf8 tests/test_stale_lane_post_stamp.py  (no pytest required)
"""

from __future__ import annotations
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

# Hermeticity floor BEFORE any storelib import (the STORE_PATH import-time
# freeze hazard; same pattern as tests/test_zero_write_passive.py): pin the
# store env to a scratch area and strip ambient project/namespace pins.
_SCRATCH = os.path.abspath(tempfile.mkdtemp(prefix="zmem-533-stale-"))
_SAVED_ENV = {k: os.environ.get(k) for k in (
    "ZMEM_STORE", "ZMEM_DATA", "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
    "ZMEM_NAMESPACE")}
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _SCRATCH
for _marker in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZMEM_NAMESPACE"):
    os.environ.pop(_marker, None)

import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

# Repo root resolved from THIS file (never a hardcoded checkout path — the
# check must replay inside a disposable worktree).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "skills", "memory", "scripts"))

from storelib.schema import init_db, migrate  # noqa: E402
from schema_meta import normalize_content  # noqa: E402

# Column tuple mirrored from the trace probe
# (.agents/issue-traces/533-535-pr532-followups/repro/probe_stale_after_stamp.py):
# the minimal raw-writer INSERT shape that reaches content_norm.
COLS = ("id, namespace, type, content, tags, source_ref, confidence, signal,"
        " ingestion_ts, content_norm")
ROW1 = ("62700001-0000-4000-8000-000000000001", "ns", "lesson", "heal me",
        "[]", "p", 0.5, "t", "2026-01-01T00:00:00Z", "WRONG")
ROW2 = ("62700002-0000-4000-8000-000000000002", "ns", "lesson",
        "stale after stamp", "[]", "p", 0.5, "t", "2026-01-02T00:00:00Z",
        "WRONG-AGAIN")


def _insert_raw(store_path: str, row: tuple) -> None:
    """The raw external sqlite3 writer (the #491 writer class): plain
    connect, INSERT, commit, close — no storelib code path."""
    raw = sqlite3.connect(store_path)
    try:
        raw.execute(
            "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,?)" % COLS,
            row)
        raw.commit()
    finally:
        raw.close()


class StaleLanePostStampTest(unittest.TestCase):
    """AC1 (discriminating, issue #533): heal the stale-after-stamp shape."""

    def test_stale_nonnull_row_after_stamp_heals_on_reopen(self):
        store_path = os.environ["ZMEM_STORE"]

        # (1) fresh store carrying one stale non-NULL norm row from a raw
        # writer (the pre-stamp shape the landed 163 pass already heals).
        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            init_db(conn)
            conn.execute(
                "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,?)" % COLS,
                ROW1)
            conn.commit()
        finally:
            conn.close()

        # (2) reopen: migrate() heals the pre-stamp stale row and lands the
        # one-time content_norm_verify_163 stamp (rides the actual heal).
        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            migrate(conn)
            healed = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?", (ROW1[0],)
            ).fetchone()[0]
            stamp = conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_verify_163'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            healed, normalize_content(ROW1[3]),
            "precondition: the pre-stamp stale row must heal on the first "
            "open (the 163 verification pass) — if this fails the fixture "
            "no longer models the issue")
        self.assertIsNotNone(
            stamp, "precondition: the content_norm_verify_163 one-time "
            "stamp must land with that first heal")

        # (3) a raw EXTERNAL writer plants a second stale non-NULL row
        # AFTER the stamp — the exact shape the 163 early-return strands.
        _insert_raw(store_path, ROW2)

        # (4) full production reopen (migrate is the open-time pass set).
        conn = sqlite3.connect(store_path)
        conn.row_factory = sqlite3.Row
        try:
            migrate(conn)
            val = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?", (ROW2[0],)
            ).fetchone()[0]
        finally:
            conn.close()

        # (5) THE assertion (RED at base: val stays 'WRONG-AGAIN').
        self.assertEqual(
            val, normalize_content(ROW2[3]),
            "a STALE non-NULL content_norm row INSERTed after the "
            "content_norm_verify_163 stamp by a raw external writer must "
            "heal to the canonical normalize of its own content by the end "
            "of that open (issue #533; the NULL-only post-stamp lane from "
            "#527 does not cover this sibling shape)")


class CleanOpenZeroWriteTest(unittest.TestCase):
    """Floor (green at base, must stay green): a settled clean store's
    migrate writes nothing — the file stays byte-identical."""

    def test_clean_store_third_migrate_is_byte_identical(self):
        store_path = os.path.join(_SCRATCH, "clean.sqlite")
        for _ in range(2):
            conn = sqlite3.connect(store_path)
            try:
                init_db(conn)
                migrate(conn)
            finally:
                conn.close()
        with open(store_path, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        conn = sqlite3.connect(store_path)
        try:
            migrate(conn)
        finally:
            conn.close()
        with open(store_path, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(
            before, after,
            "a settled clean store's migrate() must be zero-write "
            "(byte-identical store file — the PR #525 discipline the #533 "
            "heal lane must preserve)")


class CombinedLaneInventoryTest(unittest.TestCase):
    """Plan-critic round-1 MAJOR-3 inventory: the combined post-stamp lane's
    batch/concurrency/decode contracts, beyond the single-row AC1 shape.

    Each test gets its own store file (fresh UNIQUE ids) and drives the
    production surface where possible (migrate()) or the lane function
    directly where the contract under test is the lane's OWN transaction
    discipline (the full migrate() pass set legitimately commits other
    lanes' work inside a caller transaction)."""

    def _store_path(self, name):
        return os.path.join(_SCRATCH, name)

    def _open(self, name):
        conn = sqlite3.connect(self._store_path(name))
        conn.row_factory = sqlite3.Row
        return conn

    def _seed(self, name):
        conn = self._open(name)
        try:
            init_db(conn)
            conn.commit()
        finally:
            conn.close()

    def _row(self, i, norm=None, content=None):
        content = content if content is not None else "inv row %d body" % i
        return ("%08d-0000-4000-8000-%012d" % (i, i), "ns", "lesson",
                content, "[]", "p", 0.5, "t", "2026-01-01T00:00:00Z", norm)

    def _insert_many(self, name, rows):
        raw = sqlite3.connect(self._store_path(name))
        try:
            raw.executemany(
                "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,?)" % COLS,
                rows)
            raw.commit()
        finally:
            raw.close()

    def test_mixed_batch_straddles_the_500_row_boundary(self):
        # >500-row mixed batch: NULL + stale + canonical rows spread across
        # the lane's keyset pagination boundary; every NULL and every stale
        # row heals on ONE reopen, canonical rows are untouched.
        name = "inv-mixed.sqlite"
        self._seed(name)
        rows = []
        for i in range(1200):
            kind = i % 3
            if kind == 0:
                rows.append(self._row(i, norm=None))            # NULL shape
            elif kind == 1:
                rows.append(self._row(i, norm="WRONG-%d" % i))  # stale shape
            else:
                rows.append(self._row(i, norm="inv row %d body" % i))  # canonical
        self._insert_many(name, rows)
        conn = self._open(name)
        try:
            migrate(conn)
            norms = conn.execute(
                "SELECT id, content_norm FROM memory").fetchall()
        finally:
            conn.close()
        by_id = {r["id"]: r["content_norm"] for r in norms}
        self.assertEqual(len(by_id), 1200)
        for i in range(1200):
            self.assertEqual(
                by_id[self._row(i)[0]], "inv row %d body" % i,
                "row %d (%s shape) must hold the canonical norm after one "
                "reopen" % (i, ("NULL", "stale", "canonical")[i % 3]))

    def test_update_planted_stale_norm_heals(self):
        # UPDATE-style staleness: a raw writer UPDATEs content leaving the
        # OLD norm — rowid unchanged, no new row (the shape a rowid
        # high-water design would miss; see the rejected Candidate B).
        name = "inv-update.sqlite"
        self._seed(name)
        self._insert_many(name, [self._row(1, norm="old content norm")])
        raw = sqlite3.connect(self._store_path(name))
        try:
            raw.execute(
                "UPDATE memory SET content='rewritten body' WHERE id=?",
                (self._row(1)[0],))
            raw.commit()
        finally:
            raw.close()
        conn = self._open(name)
        try:
            migrate(conn)
            val = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?",
                (self._row(1)[0],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(val, normalize_content("rewritten body"))

    def test_undecodable_content_and_norm_rows_are_skipped(self):
        # Decode-fallback contract: content that is not valid UTF-8 (BLOB)
        # and a non-UTF-8 norm are SKIPPED, never normalized, never crashed.
        # Drives the lane directly: the full migrate() pass set runs other
        # lanes (entity backfill) whose BLOB handling is a separate,
        # pre-existing surface outside this issue's scope.
        import storelib.schema as schema
        name = "inv-undecodable.sqlite"
        self._seed(name)
        rid = "62700003-0000-4000-8000-000000000003"
        rid_bad_norm = "62700004-0000-4000-8000-000000000004"
        rid_repairable = "62700005-0000-4000-8000-000000000005"
        raw = sqlite3.connect(self._store_path(name))
        try:
            raw.execute(
                "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,?)" % COLS,
                (rid, "ns", "lesson", sqlite3.Binary(b"\xff\xfe bad"),
                 "[]", "p", 0.5, "t", "2026-01-01T00:00:00Z", "WRONG"))
            # UNDECODABLE NON-NULL NORM (invalid UTF-8 TEXT) with valid
            # content: implementation-review round 1 — this shape must not
            # block repair of OTHER rows in the same batch.
            raw.execute(
                "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,cast(? as text))"
                % COLS,
                (rid_bad_norm, "ns", "lesson", "valid body",
                 "[]", "p", 0.5, "t", "2026-01-01T00:00:00Z",
                 sqlite3.Binary(b"\xff\xfe nope")))
            # An ordinary STALE row in the SAME batch: must still heal.
            raw.execute(
                "INSERT INTO memory (%s) VALUES (?,?,?,?,?,?,?,?,?,?)" % COLS,
                (rid_repairable, "ns", "lesson", "repair me",
                 "[]", "p", 0.5, "t", "2026-01-01T00:00:00Z", "WRONG-NEIGHBOR"))
            raw.commit()
        finally:
            raw.close()
        conn = self._open(name)
        try:
            schema._heal_content_norms_post_stamp(conn)  # must not raise
            # Verify through the bytes factory: one row's norm is invalid
            # UTF-8 by construction, which the str factory cannot decode.
            conn.text_factory = bytes
            vals = {
                r[0].decode(): r[1] for r in conn.execute(
                    "SELECT id, content_norm FROM memory").fetchall()}
        finally:
            conn.close()
        self.assertEqual(
            vals[rid], b"WRONG",
            "a row whose content is not verifiable text stays outside the "
            "content_norm contract (skip semantics shared with the 163 and "
            "NULL lanes)")
        self.assertEqual(
            vals[rid_bad_norm], b"\xff\xfe nope",
            "an undecodable NON-NULL norm stays outside the contract too — "
            "skipped, never conflated with SQL NULL")
        self.assertEqual(
            vals[rid_repairable], normalize_content("repair me").encode(),
            "the repairable stale row in the SAME batch still heals — an "
            "undecodable norm elsewhere in the batch must not defer it "
            "(implementation-review round 1 finding)")

    def test_caller_transaction_rollback_undoes_heal_batch(self):
        # Caller-owned transaction: the lane's savepoint rides inside it and
        # the caller's ROLLBACK undoes the heal (the store stays consistent;
        # the next open retries — no marker, nothing stranded). Drives the
        # lane function directly: the contract under test is the LANE's own
        # commit discipline, not the whole migrate() pass set.
        import storelib.schema as schema
        name = "inv-caller.sqlite"
        self._seed(name)
        self._insert_many(name, [self._row(7, norm="WRONG-CALLER")])
        conn = self._open(name)
        try:
            conn.execute("BEGIN")
            schema._heal_content_norms_post_stamp(conn)
            healed_in_tx = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?",
                (self._row(7)[0],)).fetchone()[0]
            self.assertEqual(
                healed_in_tx, "inv row 7 body",
                "the lane heals inside the caller's transaction")
            conn.execute("ROLLBACK")
            after = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?",
                (self._row(7)[0],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            after, "WRONG-CALLER",
            "the caller's rollback owns the commit decision — the heal "
            "batch is undone, and the next open retries it")
        conn = self._open(name)
        try:
            migrate(conn)
            final = conn.execute(
                "SELECT content_norm FROM memory WHERE id=?",
                (self._row(7)[0],)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(final, "inv row 7 body")

    def test_second_compare_miss_aborts_lane_without_raising(self):
        # Concurrency contract: a row that keeps changing under the lane
        # aborts the CURRENT batch (diagnostic on stderr, no exception out
        # of the lane) — the never-raise discipline of the NULL lane. Uses
        # a Connection subclass because sqlite3.Connection.execute cannot
        # be monkeypatched on the C object.
        import contextlib
        import io as _io
        import storelib.schema as schema
        name = "inv-race.sqlite"
        self._seed(name)
        self._insert_many(name, [self._row(9, norm="WRONG-RACE")])
        target = self._row(9)[0]
        raced = {"fired": False}

        class _RacingConn(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("UPDATE memory SET content_norm"):
                    raced["fired"] = True
                    # The row changes before EVERY lane attempt: the first
                    # compare-and-update misses, and the fresh-read retry
                    # misses again — the second unresolved mismatch is the
                    # documented abort-without-marker conflict.
                    raced["n"] = raced.get("n", 0) + 1
                    super().execute(
                        "UPDATE memory SET content_norm=? WHERE id=?",
                        ("MOVING-%d" % raced["n"], target))
                return super().execute(sql, *args)

        conn = sqlite3.connect(self._store_path(name), factory=_RacingConn)
        conn.row_factory = sqlite3.Row
        err = _io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                schema._heal_content_norms_post_stamp(conn)  # must not raise
        finally:
            conn.close()
        self.assertTrue(
            "heal deferred" in err.getvalue(),
            "the lane diagnoses and defers (stderr) instead of raising; "
            "stderr was %r" % err.getvalue())

    def test_nonempty_canonical_store_stays_byte_identical(self):
        # Round-2 critic leg: byte-identity on a NON-EMPTY all-canonical
        # store (the scan reads every row and writes nothing).
        name = "inv-canonical.sqlite"
        self._seed(name)
        self._insert_many(name, [self._row(i) for i in range(50)])
        conn = self._open(name)
        try:
            migrate(conn)
        finally:
            conn.close()
        with open(self._store_path(name), "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        conn = self._open(name)
        try:
            migrate(conn)
        finally:
            conn.close()
        with open(self._store_path(name), "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(
            before, after,
            "an all-canonical store's open is zero-write even though the "
            "combined scan examines every row")


def tearDownModule():
    for _key, _prior in _SAVED_ENV.items():
        if _prior is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _prior
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
