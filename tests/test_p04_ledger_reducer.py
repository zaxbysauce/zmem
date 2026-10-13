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
