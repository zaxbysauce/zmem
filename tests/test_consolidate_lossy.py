"""Regression coverage for issue #19: `consolidate` used to be lossy (absorbed
rows' unique content was tombstoned and lost from live recall) and its keeper
selection contradicted its own documented rule (code used a lexicographic
ORDER BY while the docstring/comment claimed confidence * retrieval_count).

This file locks in three fixes:

  1. CONTENT PRESERVATION (AC1). An absorbed row's unique text is appended to
     the keeper under a provenance separator and its id recorded in the
     keeper's `merged_from` column, so it stays live-recallable + FTS-indexed.
  2. KEEPER ORDERING (AC2). The keeper is the highest confidence *
     retrieval_count row (ties broken by earliest ingestion_ts), matching the
     docstring.
  3. DRY-RUN PREVIEW (AC3). `--dry-run` prints the absorbed content and the
     real append decision (would-APPEND / already-represented / size-cap /
     empty), without mutating the store.

Plus critic-driven edge cases: write-time dedup must NOT grow content (it
shares the metadata-merge helper but not the content-preservation path); the
content-size cap keeps the JSONL round-trip contract; `merged_from` survives
export-jsonl -> ingest-jsonl; a stale keeper source still surfaces absorbed
text (staleness only demotes, never excludes); repeated consolidation is
stable.

Run: python tests/test_consolidate_lossy.py   (no pytest -- repo convention)
"""

from __future__ import annotations

import importlib.util
import io
import contextlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:consolidate-lossy-19"

# High-overlap near-duplicate pair (Jaccard well above the 0.60 lexical
# threshold), where the absorbed row carries a genuinely distinct clause.
KEEPER_BASE = ("pytest xdist workers share a tmpdir causing a race condition "
               "under flaky quarantine windows")
ABSORBED_EXTRA = (KEEPER_BASE + " and lane ordering must be deterministic "
                  "across lane ids")


def _load_store_module(store_path: Path, models_dir: Path):
    """A fresh store.py module instance pinned to a throwaway store, with the
    embedding model forced absent (store.py resolves both at import time)."""
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_lossy_{id(store_path)}", STORE_PY
    )
    env = {
        "ZMEM_STORE": str(store_path),
        "ZMEM_MODELS_DIR": str(models_dir),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


def _make_store():
    """Return (mod, conn, tmpdir) for an isolated, model-absent store."""
    tmp = tempfile.mkdtemp(prefix="zmem-lossy19-")
    models = Path(tmp) / "no-models"
    models.mkdir()
    mod = _load_store_module(Path(tmp) / "store.sqlite", models)
    conn = mod.connect()
    mod.init_db(conn)
    mod.migrate(conn)
    return mod, conn, tmp


def _add(mod, conn, content, confidence, rc=0, signal="test"):
    mid = mod.add_memory(conn, namespace=NS, type_="fact", content=content,
                         signal=signal, confidence=confidence)
    if rc:
        conn.execute("UPDATE memory SET retrieval_count=? WHERE id=?", (rc, mid))
        conn.commit()
    return mid


def _add_raw(mod, conn, content, confidence, rc=0, signal="test"):
    """Insert a row DIRECTLY into the memory table, bypassing write-time dedup
    (which would otherwise collapse identical/near-identical rows before
    consolidate ever sees them). Used by tests that need two near-identical
    rows to coexist so they can exercise consolidate's clustering."""
    import uuid
    mid = str(uuid.uuid4())
    ts = mod.now_iso()
    conn.execute(
        """INSERT INTO memory
           (id, namespace, type, content, tags, source_ref, source_hash,
            confidence, signal, valid_from, superseded_at, ingestion_ts,
            retrieval_count, last_retrieved)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)""",
        (mid, NS, "fact", content, "", "", "", confidence, signal, ts, ts, rc),
    )
    conn.commit()
    return mid


class ContentPreservationTest(unittest.TestCase):
    """AC1: an absorbed row's unique content survives in the keeper and is
    live-recallable."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_absorbed_unique_content_survives_in_keeper(self):
        # Keeper is the higher-product row (0.9*50=45 > 0.85*1=0.85), so it is
        # the SURVIVOR; the absorbed row has the unique clause -> the append
        # path MUST run, migrating the unique text into the keeper.
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        absorbed = _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)

        self.mod.consolidate(self.conn)

        row = self.conn.execute(
            "SELECT content, merged_from, superseded_at FROM memory WHERE id=?",
            (keeper,),
        ).fetchone()
        self.assertIsNone(row["superseded_at"], "keeper must stay live")
        self.assertIn("lane ordering must be deterministic", row["content"])
        self.assertIn("deterministic", row["content"])
        self.assertIn("across lane ids", row["content"])
        # Provenance recorded.
        self.assertEqual(row["merged_from"], absorbed)
        # Absorbed row tombstoned.
        abs_row = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (absorbed,)
        ).fetchone()
        self.assertIsNotNone(abs_row["superseded_at"])

    def test_recall_surfaces_merged_text(self):
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        self.mod.consolidate(self.conn)

        results = self.mod.recall_memory(self.conn, query="lane ordering deterministic",
                                         namespace=NS, limit=5, no_bump=True)
        contents = [r.get("content", "") for r in results]
        self.assertTrue(any("deterministic" in c for c in contents),
                        f"recall did not surface merged text: {contents}")

    def test_exact_duplicate_does_not_duplicate_content(self):
        # Two byte-identical rows: the absorbed text is fully contained in the
        # keeper -> no append, but merged_from still recorded + superseded.
        # NB: cannot create two identical rows via add_memory (write-time dedup
        # collapses them), so insert the second directly.
        keeper = _add(self.mod, self.conn, "identical shared text here", 0.90, rc=50)
        absorbed = _add_raw(self.mod, self.conn, "identical shared text here", 0.85, rc=1)
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        self.assertEqual(row["content"], "identical shared text here")
        self.assertNotIn("--- merged from", row["content"],
                         "exact duplicate must not append a separator")
        self.assertEqual(row["merged_from"], absorbed)

    def test_multi_absorb_accumulates(self):
        # One keeper absorbs two distinct rows in one cluster.
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        a1 = _add(self.mod, self.conn, KEEPER_BASE + " and alpha clause one", 0.85, rc=1)
        a2 = _add(self.mod, self.conn, KEEPER_BASE + " and bravo clause two", 0.85, rc=1)
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        self.assertIn("alpha clause one", row["content"])
        self.assertIn("bravo clause two", row["content"])
        # Both ids recorded, comma-joined.
        ids = [x.strip() for x in row["merged_from"].split(",")]
        self.assertIn(a1, ids)
        self.assertIn(a2, ids)

    def test_repeated_consolidate_is_stable(self):
        # Run consolidate twice; the second run must not re-merge tombstoned
        # rows or produce degenerate clustering from separator tokens.
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        self.mod.consolidate(self.conn)
        live_after_1 = self.conn.execute(
            "SELECT content FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,),
        ).fetchall()
        # Second run. MUST truly re-run the clustering loop, not silently
        # return early: the growth cadence gate (store.py) short-circuits a
        # back-to-back call (days_since≈0, growth ≈0) into an announced no-op,
        # so the test would pass without ever exercising re-consolidation
        # (swarm-pr-review PRR-002). force=True intentionally bypasses the gate
        # and runs the loop over the post-first-run live set (the absorbed row
        # is already tombstoned), verifying re-clustering is idempotent.
        # (Previously a non-default `threshold` was the bypass vehicle; issue #26
        # removed that side-channel in favour of an explicit --force.)
        self.mod.consolidate(self.conn, force=True)
        live_after_2 = self.conn.execute(
            "SELECT content FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,),
        ).fetchall()
        self.assertEqual(len(live_after_1), len(live_after_2),
                         "second consolidate changed the live set")
        # Unique text still present.
        self.assertTrue(any("deterministic" in r["content"] for r in live_after_2))

    def test_short_numeric_difference_is_preserved(self):
        # Implementation-review finding #2: rows differing only in 1-2 digit
        # numbers (version codes, exit codes) must NOT be classified
        # "already-represented" and lost. The uniqueness check uses a
        # length-agnostic tokenizer, not the >=3 clustering tokenizer.
        keeper = _add(self.mod, self.conn,
                      "the service exits with code 42 on timeout", 0.90, rc=50)
        absorbed = _add(self.mod, self.conn,
                        "the service exits with code 47 on timeout", 0.85, rc=1)
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (keeper,)).fetchone()
        # The unique code "47" must survive in the keeper.
        self.assertIn("47", row["content"],
                      "numeric-only difference '47' was lost")

    def test_reordered_same_tokens_are_preserved(self):
        # swarm-pr-review PRR-001: two rows can share the same word SET in a
        # different ORDER -- and that ordering can carry meaning ("call foo
        # before bar" vs "call bar before foo"). The old token-set-emptiness
        # check classified these "already-represented" and tombstoned the
        # absorbed row, silently dropping the reordered phrasing from live
        # recall. A reorder must be APPENDED (over-preserve rather than lose).
        base = "call foo before bar during init"
        keeper = _add(self.mod, self.conn, base, 0.90, rc=50)
        # Same token set, subject/object order reversed -> different meaning.
        absorbed_content = "call bar before foo during init"
        absorbed = _add_raw(self.mod, self.conn, absorbed_content, 0.85, rc=1)
        # Sanity that the fixture is a true same-token, different-order pair.
        self.assertEqual(self.mod._unique_tokens(base),
                         self.mod._unique_tokens(absorbed_content),
                         "fixture must share an identical token set")
        self.assertNotEqual(self.mod._normalize_text(base),
                            self.mod._normalize_text(absorbed_content),
                            "fixture must differ in surface form (order)")
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        # The reordered phrasing survives in the keeper (appended).
        self.assertIn("call bar before foo during init", row["content"],
                      "reordered same-token content was silently dropped")
        self.assertIn("--- merged from", row["content"],
                      "reorder must append, not be treated as a duplicate")
        self.assertIn(absorbed, row["merged_from"])

    def test_punctuation_only_difference_is_preserved(self):
        # PRR-001 companion: a punctuation-only difference is a DISTINCT surface
        # form (normalization lowercases and collapses whitespace but keeps
        # punctuation), so it carries information (emphasis, sentence boundary)
        # and MUST be preserved by appending — not treated as a duplicate.
        keeper = _add(self.mod, self.conn, "hello, world! how are you?", 0.90, rc=50)
        absorbed = _add_raw(self.mod, self.conn, "hello world how are you",
                            0.85, rc=1)
        # Sanity: normalization does NOT erase the punctuation difference.
        self.assertNotEqual(self.mod._normalize_text("hello, world! how are you?"),
                            self.mod._normalize_text("hello world how are you"))
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        self.assertIn("hello world how are you", row["content"],
                      "punctuation-only difference was silently dropped")
        self.assertIn("--- merged from", row["content"],
                      "punctuation difference must be appended (preserved)")
        self.assertIn(absorbed, row["merged_from"])

    def test_case_only_difference_is_deduped(self):
        # PRR-001 companion: a case-only difference is a true duplicate (dedup).
        keeper = _add(self.mod, self.conn, "the quick brown fox jumps", 0.90, rc=50)
        absorbed = _add_raw(self.mod, self.conn, "The Quick Brown Fox Jumps",
                            0.85, rc=1)
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        self.assertNotIn("--- merged from", row["content"],
                         "case-only difference must NOT be appended")

    def test_multi_absorb_decides_against_grown_keeper_content(self):
        # Final-critic finding #1: in a multi-absorb cluster, the decision for
        # the 2nd+ absorb must run against the keeper's GROWN content (after
        # earlier absorbs), not the seed row's original content. Otherwise the
        # real merge can append text already present in the grown keeper
        # (duplicate) AND waste size-cap budget.
        #
        # Fixture: a1 (processed FIRST — higher product) appends a unique clause;
        # a2's text is a contiguous prefix-substring of a1's appended segment, so
        # against the GROWN keeper a2 is "already-represented" (not appended),
        # but against the stale SEED a2 yields new tokens -> appended -> the test
        # fails. a1 is guaranteed to process before a2 by giving a1 a strictly
        # higher confidence*retrieval_count product.
        base = "shared base text about pytest xdist race conditions windows"
        keeper = _add(self.mod, self.conn, base, 0.90, rc=50)
        # a1: higher product (0.85 * 5 = 4.25) -> processed first.
        a1 = _add(self.mod, self.conn, base + " unique alpha clause marker",
                  0.85, rc=5)
        # a2: lower product (0.85 * 1 = 0.85) -> processed second. Its text is a
        # prefix-substring of a1's appended segment but NOT of the stale seed.
        a2 = _add_raw(self.mod, self.conn, base + " unique alpha clause",
                      0.85, rc=1)
        # force=True bypasses the cadence gate so the merge runs on this fresh
        # store regardless of gate state (issue #26 made forcing explicit; the
        # old `threshold=0.3` was an incidental side-channel for the same goal).
        self.mod.consolidate(self.conn, force=True)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        # a1's unique clause must be present (a1 was appended).
        self.assertIn("unique alpha clause marker", row["content"])
        # Exactly ONE append (a1). a2 must NOT be appended — against the grown
        # keeper it is already-represented. (Pre-fix, a2 would be appended
        # against the stale seed -> sep_count==2 -> this assertion fails.)
        sep_count = row["content"].count("--- merged from")
        self.assertEqual(sep_count, 1,
                         f"expected exactly 1 append (a1); got {sep_count} "
                         f"(a2 was appended despite being already represented "
                         f"in the grown keeper)")
        # Both ids recorded in merged_from (a2 superseded even though not
        # appended — its content was already represented).
        ids = [x.split(":")[0].strip() for x in row["merged_from"].split(",")]
        self.assertIn(a1, ids)
        self.assertIn(a2, ids)


class KeeperOrderingTest(unittest.TestCase):
    """AC2: keeper is the highest confidence * retrieval_count row."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_keeper_is_highest_confidence_times_retrieval_count(self):
        # A: conf 0.90, rc 21 -> product 18.90
        # B: conf 0.85, rc 34 -> product 28.90 (HIGHER)
        # Under the OLD lexicographic ORDER BY, A (higher confidence) was keeper
        # and B was destroyed. Under the documented product rule, B is keeper.
        a = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=21)
        b = _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=34)
        self.mod.consolidate(self.conn)

        a_live = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (a,)).fetchone()["superseded_at"]
        b_live = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (b,)).fetchone()["superseded_at"]
        self.assertIsNotNone(a_live, "lower-product A should have been absorbed")
        self.assertIsNone(b_live, "higher-product B should be the keeper")

    def test_tiebreak_is_deterministic_on_ingestion_ts(self):
        # Equal confidence * retrieval_count -> earliest ingestion_ts wins.
        # Both rows identical text (so the merge is about WHICH survives).
        # NB: identical text via add_memory collapses at write-time, so insert
        # both directly.
        import time
        a = _add_raw(self.mod, self.conn, "shared identical tiebreak text alpha", 0.80, rc=10)
        # Force b's ingestion_ts strictly later.
        time.sleep(1.1)
        b = _add_raw(self.mod, self.conn, "shared identical tiebreak text alpha", 0.80, rc=10)
        self.mod.consolidate(self.conn)
        a_live = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (a,)).fetchone()["superseded_at"]
        b_live = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (b,)).fetchone()["superseded_at"]
        self.assertIsNone(a_live, "earlier ingestion_ts (A) should be keeper on tie")
        self.assertIsNotNone(b_live, "later ingestion_ts (B) should be absorbed on tie")


class DryRunPreviewTest(unittest.TestCase):
    """AC3: dry-run prints absorbed content + the real append decision and
    does not mutate the store."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_dry_run_previews_text_and_does_not_mutate(self):
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        live_before = self.conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,)).fetchone()[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # force=True keeps the dry-run preview robust against the cadence
            # gate (a fresh store has no last_consolidation so the gate is not
            # armed here, but --force makes the intent explicit). threshold=0.5
            # is a similarity value ensuring the test pair clusters.
            self.mod.consolidate(self.conn, dry_run=True, threshold=0.5, force=True)
        out = buf.getvalue()
        live_after = self.conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,)).fetchone()[0]
        self.assertEqual(live_before, live_after, "dry-run mutated the store")
        self.assertIn("DRY RUN: cluster", out)
        self.assertIn("would APPEND", out)
        # The absorbed content itself must be in the preview.
        self.assertIn("lane ordering", out)
        # Issue #44: the final SUMMARY line (not just the per-row preview) must
        # use the mode-dependent verb. "would merge" deliberately lacks the
        # substring "merged" (no trailing d), so a dry-run summary can never be
        # skimmed as a completed merge. The old code printed
        # "merged N memories + (dry run -- no changes)" -- past tense even though
        # nothing happened -- which produced false closeout reports.
        self.assertIn("would merge", out, out)
        self.assertNotIn("merged", out, out)

    def test_dry_run_prune_summary_uses_would_prune(self):
        # Issue #44 companion: the prune verb is mode-dependent in lockstep with
        # the merge verb. A dry run with --prune must report "would prune" (never
        # the past-tense "pruned"), even when zero rows are prune-eligible -- the
        # summary appends the prune clause whenever the --prune flag is set, so
        # this pins the WORDING rather than the prune-selection logic (which has
        # its own coverage). "would prune" lacks the substring "pruned".
        _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        live_before = self.conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,)).fetchone()[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.consolidate(self.conn, dry_run=True, prune=True,
                                 threshold=0.5, force=True)
        out = buf.getvalue()
        live_after = self.conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL AND namespace=?",
            (NS,)).fetchone()[0]
        self.assertEqual(live_before, live_after, "dry-run mutated the store")
        self.assertIn("would prune", out, out)
        self.assertNotIn("pruned", out, out)


class WriteTimeDedupTest(unittest.TestCase):
    """AC4 / critic #1 separation: write-time dedup must NOT grow content."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_write_time_dedup_does_not_grow_content(self):
        k = _add(self.mod, self.conn, "alpha beta gamma unique keeper text",
                 0.90, rc=0)
        before = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (k,)).fetchone()["content"]
        # Re-add the SAME text -> write-time dedup refreshes metadata only.
        self.mod.add_memory(self.conn, namespace=NS, type_="fact",
                            content="alpha beta gamma unique keeper text",
                            signal="test", confidence=0.85)
        after = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (k,)).fetchone()["content"]
        self.assertEqual(before, after,
                         "write-time dedup must not append content")


class MigrationTest(unittest.TestCase):
    """AC5+issue#21: schema v6 adds merged_from idempotently; v7 adds the
    passive-surface telemetry columns (surfaced_count / last_surfaced)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-mig19-")
        self.models = Path(self.tmp) / "no-models"
        self.models.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def test_fresh_store_has_v7_columns(self):
        mod = _load_store_module(Path(self.tmp) / "s.sqlite", self.models)
        conn = mod.connect()
        mod.init_db(conn)
        mod.migrate(conn)
        sv = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        # migrate() walks to the CURRENT supported version (now 8 after #39 E4).
        self.assertEqual(sv, "8")
        self.assertIn("merged_from", cols)
        self.assertIn("surfaced_count", cols)
        self.assertIn("last_surfaced", cols)
        self.assertIn("content_norm", cols)
        conn.close()

    def test_re_migrate_is_idempotent(self):
        mod = _load_store_module(Path(self.tmp) / "s2.sqlite", self.models)
        conn = mod.connect()
        mod.init_db(conn)
        mod.migrate(conn)
        mod.migrate(conn)  # second time
        mod.migrate(conn)  # third time
        sv = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        # Idempotent: re-migrate stays at the current version (now 8).
        self.assertEqual(sv, "8")
        conn.close()

    def test_v6_to_v7_populated_migration_preserves_rows(self):
        # swarm-pr-review PRR-006 + issue #21: migrate() must preserve existing rows
        # when upgrading a POPULATED v6 store to v7 (surfaced_count/last_surfaced
        # added, all data intact), not just work on an empty fresh store. Build a v6
        # store by creating the v7 schema, then removing the v7-only columns and
        # rewinding the recorded version.
        mod = _load_store_module(Path(self.tmp) / "s3.sqlite", self.models)
        conn = mod.connect()
        mod.init_db(conn)
        mod.migrate(conn)
        conn.execute("ALTER TABLE memory DROP COLUMN last_surfaced")
        conn.execute("ALTER TABLE memory DROP COLUMN surfaced_count")
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        conn.commit()
        # Populate while in v6 state (raw inserts avoid any surfaced dep).
        a = _add_raw(mod, conn, "preserve alpha content for migration", 0.90, rc=3)
        _add_raw(mod, conn, "preserve beta content for migration", 0.70, rc=1)
        cols_sel = ("SELECT id, content, tags, confidence, signal, "
                    "retrieval_count, superseded_at, ingestion_ts FROM memory "
                    "ORDER BY id")
        before = [tuple(r) for r in conn.execute(cols_sel)]
        # Upgrade — migrate() walks to the CURRENT version (v8 after #39 E4),
        # passing through the v7 block (re-adding surfaced_count/last_surfaced).
        mod.migrate(conn)
        sv = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(sv, "8")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        self.assertIn("surfaced_count", cols)
        self.assertIn("last_surfaced", cols)
        after = [tuple(r) for r in conn.execute(cols_sel)]
        self.assertEqual(before, after,
                         "v6->v7 migration altered existing row data")
        # Newly-added columns default correctly on populated rows.
        ma = conn.execute(
            "SELECT surfaced_count, last_surfaced FROM memory WHERE id=?", (a,)).fetchone()
        self.assertEqual(ma["surfaced_count"], 0,
                         "fresh surfaced_count column should default to 0")
        self.assertIsNone(ma["last_surfaced"],
                          "fresh last_surfaced column should default to NULL")
        conn.close()


class SizeCapAndJsonlRoundtripTest(unittest.TestCase):
    """Critic #2 + #3: size cap keeps content within the ingest limit, and
    merged_from survives JSONL export/import."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_merge_respects_content_size_cap(self):
        # Build a keeper within ~one phrase-length of the cap from HIGH-OVERLAP
        # repeated phrases so the lexical/Jaccard similarity is well above
        # threshold (a wall of one repeated char has near-zero token overlap and
        # won't cluster). The absorbed row's suffix must then push the
        # keeper+separator+absorbed projection OVER the cap.
        cap = self.mod.INGEST_MAX_CONTENT_CHARS
        phrase = ("pytest xdist workers race condition flaky quarantine "
                  "tmpdir deterministic lane ordering ")
        # Target keeper length so keeper + separator + absorbed > cap.
        target_len = cap - len(phrase) - 60
        repeats = target_len // len(phrase)
        keeper_text = (phrase * repeats).rstrip()
        # Pad to within ~one phrase of the cap if the repeat grid undershot.
        while len(keeper_text) < target_len:
            keeper_text += " " + phrase.strip()
        absorbed_text = keeper_text + " unique cap boundary suffix clause here"
        # SANITY: confirm appending absorbed would exceed the cap.
        sep_len = len(f"\n\n--- merged from x ---\n")
        self.assertGreater(len(keeper_text) + sep_len + len(absorbed_text), cap,
                           "test fixture does not exercise the size-cap path")
        # Confirm the pair actually clusters.
        sim = self.mod._lexical_similarity(
            self.mod._lexical_tokens(keeper_text),
            self.mod._lexical_tokens(absorbed_text),
        )
        self.assertGreaterEqual(sim, self.mod.CONSOLIDATE_LEXICAL_THRESHOLD,
                                f"test fixture does not cluster (sim={sim})")
        keeper = _add(self.mod, self.conn, keeper_text, 0.90, rc=50)
        a = _add(self.mod, self.conn, absorbed_text, 0.85, rc=1)
        self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        # Keeper content must NOT exceed the cap (append was skipped).
        self.assertLessEqual(len(row["content"]), cap,
                             "keeper exceeded the content cap")
        # The id is recorded with the truncated marker (append skipped, provenance kept).
        self.assertIsNotNone(row["merged_from"],
                             "merged_from not recorded at size-cap")
        self.assertIn(a, row["merged_from"])
        self.assertIn(":truncated", row["merged_from"])

    def test_multi_absorb_cannot_cumulatively_exceed_size_cap(self):
        # Implementation-review finding #1: absorbs that each individually pass
        # the size-cap check against the ORIGINAL keeper must not, when
        # accumulated, push the keeper over INGEST_MAX_CONTENT_CHARS (or the
        # JSONL round-trip contract breaks). The grown-content re-check in
        # _absorb_into_keeper must downgrade the over-cap absorb.
        cap = self.mod.INGEST_MAX_CONTENT_CHARS
        # Fixture math (verified): phrase*700 = 73500 chars; slice to cap/2-100
        # = 32668. Each absorbed = keeper (32668) + short suffix -> ~32693.
        # a1 alone: 32668 + ~62 (sep) + 32693 = ~65423 <= 65536 (FITS).
        # After a1 grows the keeper to ~65423, a2: 65423 + 62 + 32693 = ~98178
        # > 65536 (EXCEEDS) -> the grown re-check MUST downgrade a2 to size-cap.
        # NB: the `* 700` must apply to the WHOLE phrase (parenthesized) — Python
        # implicit string concatenation binds looser than `*`, so an unparenthe-
        # sized split literal would silently make the source shorter than the
        # slice and the test would pass trivially.
        phrase = ("pytest xdist workers race condition flaky quarantine "
                  "tmpdir deterministic lane ordering alpha beta gamma ")
        shared = phrase * 700
        keeper_text = shared[:cap // 2 - 100]
        self.assertEqual(len(keeper_text), cap // 2 - 100,
                         "fixture setup: slice did not land at the target length")
        a1_text = keeper_text + " unique alpha clause one"
        a2_text = keeper_text + " unique bravo clause two"
        keeper = _add(self.mod, self.conn, keeper_text, 0.90, rc=50)
        a1 = _add(self.mod, self.conn, a1_text, 0.85, rc=1)
        a2 = _add(self.mod, self.conn, a2_text, 0.85, rc=1)
        # force=True bypasses the cadence gate (issue #26); the threshold here
        # was only ever a gate-bypass vehicle, not a similarity assertion — the
        # three texts are near-identical and cluster at any threshold.
        self.mod.consolidate(self.conn, force=True)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)
        ).fetchone()
        # The keeper MUST NEVER exceed the cap, regardless of how many absorbs
        # individually passed the per-absorb check.
        self.assertLessEqual(len(row["content"]), cap,
                             f"keeper exceeded cap after multi-absorb: "
                             f"{len(row['content'])} > {cap}")
        # Exactly ONE absorb's content landed (the first processed, which fit);
        # the other was downgraded to size-cap. Either "alpha clause one" OR
        # "bravo clause two" must be present (depending on neighbor order, which
        # is UUID-dependent), but not both.
        self.assertTrue(
            "alpha clause one" in row["content"] or "bravo clause two" in row["content"],
            "no absorbed content survived multi-absorb"
        )
        # The over-cap absorb must have been downgraded to size-cap, so its id
        # carries the :truncated marker.
        self.assertIn(":truncated", row["merged_from"],
                      "over-cap absorb was not downgraded to size-cap")

    def test_merged_from_survives_jsonl_roundtrip(self):
        keeper = _add(self.mod, self.conn, KEEPER_BASE, 0.90, rc=50)
        absorbed = _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        self.mod.consolidate(self.conn)
        # Export (include superseded so the absorbed tombstone also round-trips).
        export_path = Path(self.tmp) / "export.jsonl"
        rc = self.mod.cmd_export_jsonl(self.conn, out=str(export_path), namespace=NS,
                                       include_superseded=True)
        self.assertEqual(rc, 0)
        # Parse the exported lines; merged_from must be present on the keeper.
        lines = [json.loads(l) for l in export_path.read_text().splitlines() if l.strip()]
        keeper_line = next(l for l in lines if l["id"] == keeper)
        self.assertEqual(keeper_line["merged_from"], absorbed)
        # Import into a fresh store via the real ingest path.
        tmp2 = tempfile.mkdtemp(prefix="zmem-rt19-")
        models2 = Path(tmp2) / "m"; models2.mkdir()
        mod2 = _load_store_module(Path(tmp2) / "s.sqlite", models2)
        conn2 = mod2.connect(); mod2.init_db(conn2); mod2.migrate(conn2)
        try:
            r = mod2.cmd_ingest_jsonl(conn2, in_path=str(export_path),
                                      source_ref=None, allow_tombstones=True)
            self.assertEqual(r, 0)
            imported = conn2.execute(
                "SELECT merged_from FROM memory WHERE id=?", (keeper,)).fetchone()
            self.assertIsNotNone(imported, "keeper not imported")
            self.assertEqual(imported["merged_from"], absorbed,
                             "merged_from did not survive JSONL round-trip")
        finally:
            conn2.close()
            shutil.rmtree(tmp2, True)


class StaleSourceTest(unittest.TestCase):
    """Critic #1: a stale keeper source must still surface absorbed text
    (recall staleness only demotes, never excludes)."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_stale_source_keeper_still_surfaces_absorbed_content(self):
        # Give the keeper a file: source_ref whose content differs from its
        # source_hash -> recall marks it stale but MUST still return it.
        src = Path(self.tmp) / "src.md"
        src.write_text("original source content", encoding="utf-8")
        keeper = self.mod.add_memory(self.conn, namespace=NS, type_="fact",
                                     content=KEEPER_BASE, signal="test",
                                     confidence=0.90, source_ref=f"file:{src}")
        self.conn.execute("UPDATE memory SET retrieval_count=50 WHERE id=?", (keeper,))
        _add(self.mod, self.conn, ABSORBED_EXTRA, 0.85, rc=1)
        # Now mutate the source file so the hash disagrees -> stale.
        src.write_text("CHANGED source content", encoding="utf-8")
        self.conn.commit()
        self.mod.consolidate(self.conn)
        results = self.mod.recall_memory(self.conn, query="lane ordering deterministic",
                                         namespace=NS, limit=5, no_bump=True)
        contents = [r.get("content", "") for r in results]
        self.assertTrue(any("deterministic" in c for c in contents),
                        "stale keeper did not surface absorbed text")


@unittest.skipUnless(_sqlite_vec_available(),
                     "sqlite-vec not installed — the cosine path cannot be exercised")
class CosinePathTest(unittest.TestCase):
    """The cosine (vec0 KNN) path shares the same merge helper + ORDER BY, so
    the fix applies there too. Exercised in-process with hand-written vectors
    when sqlite-vec is available (CI runs without the model, so embeddings are
    stubbed available instead)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-cos19-")
        models = Path(self.tmp) / "no-models"; models.mkdir()
        self.mod = _load_store_module(Path(self.tmp) / "s.sqlite", models)
        self.conn = self.mod.connect()
        self.mod.init_db(self.conn)
        self.mod.migrate(self.conn)
        have_vec = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_vec'"
        ).fetchone()
        if not have_vec:
            self.skipTest("memory_vec (vec0) unavailable in this sqlite build")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def _vec(self, *values):
        dim = 384
        vals = list(values) + [0.0] * (dim - len(values))
        return struct.pack(f"{dim}f", *vals)

    def _add_embedded(self, content, confidence, embedding, rc=0):
        mid = self.mod.add_memory(self.conn, namespace=NS, type_="fact",
                                  content=content, signal="test",
                                  confidence=confidence)
        self.conn.execute(
            "UPDATE memory SET embedding=?, embedding_model='test-stub', retrieval_count=? WHERE id=?",
            (embedding, rc, mid))
        self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
        self.conn.execute(
            "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)", (embedding, mid))
        self.conn.commit()
        return mid

    def test_cosine_path_preserves_absorbed_content_and_picks_product_keeper(self):
        seed_vec = self._vec(1.0)
        near_vec = self._vec(1.0, 0.4)  # cos ~0.93, above the 0.80 default
        # near row has the unique clause + LOWER product -> it gets absorbed,
        # and its unique text must migrate to the keeper.
        keeper = self._add_embedded(KEEPER_BASE, 0.90, seed_vec, rc=50)
        absorbed = self._add_embedded(ABSORBED_EXTRA, 0.85, near_vec, rc=1)
        stub = mock.Mock()
        stub.is_available.return_value = True
        with mock.patch.object(self.mod, "_embeddings", stub):
            self.mod.consolidate(self.conn)
        row = self.conn.execute(
            "SELECT content, merged_from FROM memory WHERE id=?", (keeper,)).fetchone()
        self.assertIn("deterministic", row["content"])
        self.assertEqual(row["merged_from"], absorbed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
