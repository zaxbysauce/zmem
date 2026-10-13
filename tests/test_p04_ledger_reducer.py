"""Issue #252 — supplementary unit tests for the segment/reducer laws.

The frozen acceptance checks live in tests/test_p04_ledger_concurrent_writers.py
(authored at arm's length, checkpoint-frozen); this file pins the internal
laws the fix introduces on top of those:

- _reduce_entries: upsert by id with ts >= (equal-ts refresh from a
  later-written segment wins; an older-ts re-record does NOT overwrite —
  the documented drift from the old unconditional replace), window prune,
  oldest-first cap eviction.
- compaction unlinks STRICTLY the paths read into the merge: a segment
  published between the glob and the unlink survives with its rows (the
  plan-critic round-1 blocker 1/2 interleaving).
- the terminal-marker post-write re-check removes the just-published
  segment (no resurrection after end_delivery_state).
- purge.scrub_ledgers scrubs segments (drop_ids, folded needles, torn
  lines) and stages rewrites through <hash>.ledger.tmp.<hex>.

All stores are throwaway temp dirs; ZMEM_LEDGER_CAP / ZMEM_DELIVER_WINDOW_S
are popped in-process. Runs standalone: python tests/test_p04_ledger_reducer.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import delivery_ledger as dl  # noqa: E402
from storelib.purge import scrub_ledgers  # noqa: E402

SID = "sess-p04-reducer"


def _entry(rid: str, ts: float, text: str = "c") -> dict:
    return {"id": rid, "moment": "subagent", "ts": ts, "text": text}


def _write_seg(tmp: str, entries: list, mtime: float | None = None) -> str:
    path = dl._publish_segment(tmp, SID, entries)
    assert path is not None
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class ReducerLawTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-reducer-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_equal_ts_refresh_from_later_written_segment_wins(self):
        base = time.time()
        _write_seg(self.tmp, [_entry("row-1", base, "stale fuel")],
                   mtime=base)
        _write_seg(self.tmp, [_entry("row-1", base, "fresh fuel")],
                   mtime=base + 10)
        entries = dl.delivered(self.tmp, SID, now=base + 60)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "fresh fuel")

    def test_older_ts_re_record_does_not_overwrite(self):
        # Documented drift from the locked RMW (which unconditionally
        # replaced): ts dominates, so a re-record with an older now loses.
        now = time.time()
        _write_seg(self.tmp, [_entry("row-1", now, "newer")])
        _write_seg(self.tmp, [_entry("row-1", now - 5, "older")],
                   mtime=now + 10)
        entries = dl.delivered(self.tmp, SID, now=now + 60)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "newer")

    def test_window_prune_and_cap(self):
        now = 1_000_000.0
        base = [_entry("old", now - 999_999), _entry("r0", now),
                _entry("r1", now + 1), _entry("r2", now + 2),
                _entry("r3", now + 3), _entry("r4", now + 4)]
        os.environ["ZMEM_LEDGER_CAP"] = "3"
        try:
            merged = dl._reduce_entries(base, [], now + 60)
        finally:
            os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.assertEqual([e["id"] for e in merged], ["r2", "r3", "r4"])

    def test_unreadable_segment_is_skipped_silently(self):
        now = time.time()
        _write_seg(self.tmp, [_entry("row-1", now)])
        bad = os.path.join(self.tmp, "ops",
                           dl._hashed_name(SID, "") + ".ledger.seg.deadbeef")
        Path(bad).write_bytes(b"\xff\xfe not utf8")
        entries = dl.delivered(self.tmp, SID, now=now + 60)
        self.assertEqual([e["id"] for e in entries], ["row-1"],
                         "a corrupt segment must not break the read")

    def test_corrupt_ts_degrades_instead_of_raising(self):
        # Issue #252 review P-001: the reducer's final sort key must
        # tolerate the same corrupt values its sibling comparisons do.
        now = time.time()
        base = Path(dl.ledger_path(self.tmp, SID))
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps(
            {"entries": [{"id": "bad-base", "moment": "m",
                          "ts": "not-a-number", "text": "x"}]}),
            encoding="utf-8")
        _write_seg(self.tmp, [_entry("bad-seg", {"oops": 1},
                                     "c"),  # type: ignore[arg-type]
                               _entry("good", now)])
        entries = dl.delivered(self.tmp, SID, now=now + 60)
        # The guarded fallbacks deliberately KEEP corrupt-ts entries (they
        # cannot be window-dated) — the law pinned here is that the read
        # completes instead of raising, and the valid row still surfaces.
        self.assertEqual({e["id"] for e in entries},
                         {"bad-base", "bad-seg", "good"},
                         "corrupt-ts entries must degrade, not raise")

    def test_window_prune_applies_to_segment_sourced_rows(self):
        # Issue #252 review P-012/M2: the reducer's window prune is the
        # ONLY prune for segment-held rows (segments are never rewritten);
        # deleting it must fail this test — the cap alone masks the base
        # fixture shapes.
        now = time.time()
        with patch.object(dl, "_try_compact", lambda *a, **k: None):
            dl.record(self.tmp, SID,
                      [{"id": "stale", "content": "old"}], "subagent",
                      now=now - 10_000_000)
            dl.record(self.tmp, SID,
                      [{"id": "fresh", "content": "new"}], "subagent",
                      now=now)
        self.assertEqual(dl.delivered_ids(self.tmp, SID, now=now + 60),
                         ["fresh"],
                         "an out-of-window segment row must be pruned")


class SweepSegmentArmTest(unittest.TestCase):
    """Issue #252 review P-012/M3: deleting the backup sweep's
    `.ledger.seg.` containment arm must fail a test (it previously broke
    no test in the suite)."""

    def test_sweep_reaps_aged_segments(self):
        import time as _time

        from storelib import backup
        tmp = tempfile.mkdtemp(prefix="zmem-p04-sweep-")
        try:
            ops = Path(tmp) / "ops"
            ops.mkdir(parents=True)
            seg = ops / (dl._hashed_name(SID, "") + ".ledger.seg.abc123")
            seg.write_text('{"id": "old", "ts": 1}\n', encoding="utf-8")
            aged = _time.time() - 2 * 86400
            os.utime(seg, (aged, aged))
            rc = backup.cmd_sweep(marker_dir=tmp, max_age_days=1)
            self.assertEqual(rc, 0)
            self.assertFalse(seg.exists(),
                             "aged segment survived the backup sweep — "
                             "the `.ledger.seg.` sweep arm is gone")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sweep_reaps_aged_ledger_tmp_files(self):
        # Issue #252 review round 2 (P-007 killer): the `.ledger.tmp.`
        # family (publication staging orphans) must join the sweep.
        import time as _time

        from storelib import backup
        tmp = tempfile.mkdtemp(prefix="zmem-p04-sweep-tmp-")
        try:
            ops = Path(tmp) / "ops"
            ops.mkdir(parents=True)
            orphan = ops / (dl._hashed_name(SID, "") + ".ledger.tmp.abc123")
            orphan.write_bytes(b'{"entries": []}')
            aged = _time.time() - 2 * 86400
            os.utime(orphan, (aged, aged))
            rc = backup.cmd_sweep(marker_dir=tmp, max_age_days=1)
            self.assertEqual(rc, 0)
            self.assertFalse(orphan.exists(),
                             "aged .ledger.tmp. orphan survived the sweep — "
                             "the `.ledger.tmp.` sweep arm is gone")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Round2FixPinsTest(unittest.TestCase):
    """Kill pins for the round-2 review fixes (deleting the fixed line must
    fail the test): purge scrub race tolerance, ASCII-safe publication,
    torn-tail per-line decode, base-write share retry."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-round2-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_purge_skips_undecodable_segment_but_verifies_bytes(self):
        from storelib.purge import _verify_ledgers_clean, scrub_ledgers
        ops = Path(self.tmp) / "ops"
        ops.mkdir(parents=True)
        seg = ops / (dl._hashed_name(SID, "") + ".ledger.seg.abc123")
        # Valid JSONL with an ASCII needle, then a torn 0xFF tail byte that
        # makes the whole file undecodable as UTF-8.
        seg.write_bytes(b'{"id": "x", "ts": 1, "text": "NEEDLEZORG"}\n\xff')
        stats = scrub_ledgers(Path(self.tmp), set(), ["NEEDLEZORG"])
        self.assertGreaterEqual(stats.get("segments_skipped", 0), 1,
                                "undecodable segment was not skipped")
        self.assertTrue(seg.exists(),
                        "a skipped segment must not be deleted by the scrub")
        residue = _verify_ledgers_clean(Path(self.tmp), ["NEEDLEZORG"])
        self.assertTrue(residue,
                        "byte-level verify must still scan skipped segments")

    def test_publication_survives_surrogates_and_u2028(self):
        now = time.time()
        dl.record(self.tmp, SID, [
            {"id": f"sur-{now}", "content": "lone \ud800 surrogate"},
            {"id": f"sep-{now}", "content": "line sep \u2028 here"},
        ], "subagent", now=now)
        ids = set(dl.delivered_ids(self.tmp, SID, now=now + 60))
        self.assertEqual(len(ids), 2, "both rows must round-trip")

    def test_torn_multibyte_tail_keeps_intact_lines(self):
        ops = Path(self.tmp) / "ops"
        ops.mkdir(parents=True)
        seg = ops / (dl._hashed_name(SID, "") + ".ledger.seg.deadbeef")
        intact = json.dumps({"id": "intact", "moment": "m", "ts": 1,
                             "text": "c"}, ensure_ascii=False)
        # Genuinely torn: ends INSIDE the two-byte é sequence (0xC3 without
        # its continuation byte), so strict whole-file decode raises.
        torn = b'{"id": "torn", "ts": 1, "text": "caf\xc3'
        seg.write_bytes(intact.encode("utf-8") + b"\n" + torn)
        entries = dl.delivered(self.tmp, SID, now=10_000.0)
        self.assertEqual([e["id"] for e in entries], ["intact"],
                         "a torn multi-byte tail dropped the whole segment")

    def test_base_write_retries_sharing_violation(self):
        target = str(Path(dl.ledger_path(self.tmp, SID)))
        real_replace = os.replace
        attempts = {"n": 0}

        def flaky_replace(src, dst, **kw):
            if (".tmp." in str(src) and str(dst) == target
                    and attempts["n"] == 0):
                attempts["n"] += 1
                raise OSError(5, "Access is denied")
            return real_replace(src, dst, **kw)

        with patch.object(dl.os, "replace", side_effect=flaky_replace):
            dl._atomic_write_json(target, {"entries": []})
        self.assertEqual(attempts["n"], 1, "retry never engaged")
        self.assertTrue(Path(target).exists(), "base not written after retry")
        residue = [p.name for p in (Path(self.tmp) / "ops").iterdir()
                   if ".tmp." in p.name]
        self.assertEqual(residue, [], "tmp leaked after retried write")


class CompactionInterleavingTest(unittest.TestCase):
    """Plan-critic round-1 blockers 1+2: a segment published between the
    compaction glob and the unlink must survive with its rows."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-interleave-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_segment_published_during_compaction_survives(self):
        now = time.time()
        # Writer A's segment is on disk before the compaction pass.
        seg_a = _write_seg(self.tmp, [_entry("a-row", now)])
        real_load = dl._load_entries
        published: list[str] = []

        def sneaky_load(path, now_arg):
            # Fires inside the locked merge, AFTER the glob: a concurrent
            # writer B publishes its segment right now.
            if not published:
                published.append(
                    dl._publish_segment(self.tmp, SID, [_entry("b-row", now)]))
            return real_load(path, now_arg)

        with patch.object(dl, "_load_entries", side_effect=sneaky_load):
            dl.record(self.tmp, SID, [_entry("a-row", now)], "subagent",
                      now=now)
        self.assertTrue(os.path.exists(published[0]),
                        "segment published between glob and unlink was "
                        "destroyed (re-glob-at-unlink mutation)")
        self.assertFalse(os.path.exists(seg_a),
                         "the merge-input segment should have been compacted")
        ids = set(dl.delivered_ids(self.tmp, SID, now=now + 60))
        self.assertEqual(ids, {"a-row", "b-row"})


class TerminalMarkerSelfUnlinkTest(unittest.TestCase):
    """A segment published against a fresh terminal marker must not
    resurrect delivered rows (post-write re-check)."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-terminal-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_during_fresh_terminal_marker_leaves_nothing(self):
        now = time.time()
        dl.end_delivery_state(self.tmp, SID)
        dl.record(self.tmp, SID, [_entry("ghost", now)], "subagent", now=now)
        segs = dl._seg_paths(self.tmp, SID)
        self.assertEqual(segs, [],
                         "segment survived a fresh terminal marker")
        self.assertEqual(dl.delivered_ids(self.tmp, SID, now=now + 60), [])


class ClearStripsSegmentsTest(unittest.TestCase):
    """clear() (PreCompact re-admission) must remove un-compacted segments
    too — implementation-review finding: segments ARE ledger state, and a
    clear that leaves them behind keeps rows suppressed deterministically.
    The legacy pending sidecar is NOT clear()'s to remove."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-clear-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clear_removes_uncompacted_segments_but_not_pending(self):
        now = time.time()
        # A segment that never reached the base (simulated: compaction
        # skipped because the lock is wedged during record).
        with patch.object(dl, "_try_compact", lambda *a, **k: None):
            dl.record(self.tmp, SID, [_entry("row-1", now)], "subagent",
                      now=now)
        self.assertEqual(dl.delivered_ids(self.tmp, SID, now=now + 60),
                         ["row-1"], "precondition: row lives in a segment")
        dl.park_pending(self.tmp, SID, [{"id": "p"}], "F", "pretool",
                        now=now)
        dl.clear(self.tmp, SID)
        self.assertEqual(dl._seg_paths(self.tmp, SID), [],
                         "clear() left an un-compacted segment behind")
        self.assertEqual(
            dl.delivered_ids(self.tmp, SID, now=now + 60), [],
            "rows stayed suppressed after PreCompact re-admission")
        self.assertTrue(os.path.exists(dl.pending_path(self.tmp, SID)),
                        "clear() must not touch the legacy pending sidecar")


class PublicationFailureResidueTest(unittest.TestCase):
    """When segment publication fails (genuine OSError), record() fails
    open silently AND leaves no *.tmp.* residue (implementation-review
    round 2, NOTE 5)."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-pubfail-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publication_oserror_leaves_no_residue_and_no_raise(self):
        now = time.time()
        real_replace = os.replace

        def exploding_replace(src, dst, **kw):
            if ".ledger.tmp." in str(src):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, **kw)

        with patch.object(dl.os, "replace", side_effect=exploding_replace):
            dl.record(self.tmp, SID, [_entry("row-1", now)], "subagent",
                      now=now)  # must not raise
        residue = [p.name for p in
                   (Path(self.tmp) / "ops").iterdir() if ".tmp." in p.name]
        self.assertEqual(residue, [], "publication tmp leaked on failure")


class PurgeSegScrubTest(unittest.TestCase):
    """Segments join the purge scrub laws: drop_ids, folded needles, torn
    lines; rewrites stage through <hash>.ledger.tmp.<hex>."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-scrub-")
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scrub_drops_ids_needles_and_torn_lines(self):
        now = time.time()
        # Concatenated like tests/test_q01_purge.py's FAKE so the literal
        # token shape never appears verbatim in source (push protection).
        needle = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        seg = _write_seg(self.tmp, [
            _entry("victim-id", now, "innocent text"),
            _entry("keeper", now, "safe"),
            _entry("needle-row", now, f"carry the {needle} away"),
        ])
        with open(seg, "a", encoding="utf-8") as f:
            f.write('{"id": "torn", "ts": 1')  # crash-torn tail
        stats = scrub_ledgers(Path(self.tmp), {"victim-id"}, [needle])
        self.assertEqual(stats["entries_dropped"], 2)
        self.assertEqual(stats["files_scrubbed"], 1)
        lines = [json.loads(l) for l in
                 Path(seg).read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual([e["id"] for e in lines], ["keeper"])
        residue = [p.name for p in
                   (Path(self.tmp) / "ops").iterdir() if ".tmp." in p.name]
        self.assertEqual(residue, [])

    def test_scrub_unlinks_fully_dropped_segment(self):
        now = time.time()
        seg = _write_seg(self.tmp, [_entry("victim-id", now)])
        stats = scrub_ledgers(Path(self.tmp), {"victim-id"}, [])
        self.assertEqual((stats["files_scrubbed"], stats["entries_dropped"]),
                         (1, 1))
        self.assertFalse(os.path.exists(seg))

    def test_verify_ledgers_clean_sees_segments(self):
        now = time.time()
        needle = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        _write_seg(self.tmp, [_entry("keeper", now, f"has {needle} inside")])
        from storelib.purge import _verify_ledgers_clean
        residue = _verify_ledgers_clean(Path(self.tmp), [needle])
        self.assertTrue(residue, "segments must be covered by *.ledger*")


if __name__ == "__main__":
    unittest.main(verbosity=2)
