"""Issue #252 follow-up (P-05) — segment path safety, retention, drain,
corrupt-marker containment, and the compact-snapshot merged view.

Arm's-length acceptance checks authored against base 41008fdd. Every check
isolates into a throwaway tmpdir (ledger state is ops-dir sidecars only; no
sqlite, no ~/.zmem access). The ledger knobs ZMEM_DELIVER_WINDOW_S /
ZMEM_LEDGER_CAP / ZMEM_RESERVATION_TTL_S are popped in-process around each
test; subprocess children (none in this file) would get the same strip.

Checks (ids are final; each test's docstring carries its id):

- C4  SegPathSafetyTest::test_symlink_not_parsed
      (NEW-SURFACE: _seg_file_readable guard-predicate leg + the POSIX
      behavioral symlink arm / the Windows directory-plant substitute arm)
- C5  SegPathSafetyTest::test_fifo_skipped
      (NEW-SURFACE: _seg_file_readable guard-predicate leg + the POSIX
      behavioral mkfifo arm / the Windows directory-plant substitute arm)
- C6  RetentionTest::test_aged_segment_reaped_by_compaction_or_write_path
- C7  PublishFailureTest::test_compaction_drain_runs_after_publish_failure
- C8  EndedMarkerTest::test_corrupt_marker_does_not_raise
- C15 SnapshotViewTest::test_snapshot_covers_unmerged_segments

Windows authoring note (this host): os.symlink is REFUSED without
SeCreateSymbolicLinkPrivilege (OSError WinError 1314) and os.mkfifo does not
exist, so C4/C5 each carry a deterministic in-process substitute arm (a
plain DIRECTORY planted at the segment name — creatable unprivileged) that
RUNS on Windows; the behavioral (symlink/fifo) arm runs wherever the OS
permits planting those. No check skips on Windows. Amendment (settled fix
plan): both checks are NEW-SURFACE via a guard-predicate leg that runs on
BOTH platforms FIRST — delivery_ledger must expose a module-level
``_seg_file_readable(path)`` (True only for regular files; False for
symlinks/reparse points/directories/non-regular files) — so the checks are
deterministically RED at base on Windows too (the predicate does not exist
there), while POSIX keeps the behavioral RED (evil-1 parsed at base; fifo
open blocks at base) on top of the same missing-predicate RED.

Runs standalone: python tests/test_p05_seg_path_safety.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import delivery_ledger as dl  # noqa: E402

SID = "sess-p05-segpath"

# Documented on this authoring host (Windows, no symlink privilege):
# os.symlink raises OSError [WinError 1314]. The substitute arms embed this
# note in their assertion messages so the refusal stays observable.
_SYMLINK_REFUSED_NOTE = (
    "os.symlink was refused by the OS on this host "
    "(OSError [WinError 1314] A required privilege is not held by the "
    "client) — the deterministic Windows substitute arm (a plain directory "
    "planted at the segment name) ran instead"
)


def _seg_name(session_id: str, tail: str) -> str:
    return dl._hashed_name(session_id, "") + ".ledger.seg." + tail


def _delivered_bounded(case: unittest.TestCase, data_dir: str, sid: str,
                       limit_s: float = 5.0):
    """delivered() under a hard wall-clock bound so a hang (an open() that
    blocks on a fifo, or any future regression of the file-kind guard) fails
    LOUDLY instead of stalling the suite. Returns the id list; fails the
    case when the call blocks past ``limit_s`` or raises."""
    box: dict = {}

    def run():
        try:
            box["ids"] = dl.delivered_ids(data_dir, sid)
        except BaseException as exc:  # surfaced to the case below
            box["exc"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(limit_s)
    if worker.is_alive():
        case.fail(f"delivered() blocked longer than {limit_s}s on a "
                  "non-regular file planted at a segment name — the reader "
                  "must skip non-regular segment paths, not open them")
    if "exc" in box:
        raise box["exc"]
    return box["ids"]


class _LedgerTmpTestCase(unittest.TestCase):
    """Fresh throwaway ops dir + stripped ledger knobs per test."""

    def setUp(self):
        for key in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_RESERVATION_TTL_S"):
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p05-segpath-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ops(self) -> Path:
        ops = Path(self.tmp, "ops")
        ops.mkdir(parents=True, exist_ok=True)
        return ops


class SegPathSafetyTest(_LedgerTmpTestCase):

    # The guard leg uses its OWN session id so the real segment it
    # publishes cannot leak into the observable-contract arms' delivered()
    # view of SID (the Windows substitute arm asserts that view is empty).
    GUARD_SID = SID + "-guard"

    def _guard_predicate_leg(self) -> None:
        """NEW-SURFACE leg (settled fix plan; runs on BOTH platforms,
        before every other arm): delivery_ledger must expose the
        module-level guard predicate ``_seg_file_readable(path)`` — True
        only for regular files; False for symlinks/reparse points,
        directories and any other non-regular file. Pinned against a
        planted DIRECTORY at a segment name (False) and a real published
        segment file (True). At base the predicate does not exist, so this
        leg fails with a stable missing-surface RED on every platform."""
        ops = self._ops()
        dirseg = ops / _seg_name(self.GUARD_SID, "guarddir" * 3)
        dirseg.mkdir()
        real_seg = dl._publish_segment(self.tmp, self.GUARD_SID, [
            {"id": "guard-real-1", "moment": "m", "ts": time.time(),
             "text": "c"}])
        self.assertTrue(real_seg,
                        "precondition: a real segment file was published")

        guard = getattr(dl, "_seg_file_readable", None)
        self.assertTrue(
            callable(guard),
            "delivery_ledger must expose the module-level guard predicate "
            "_seg_file_readable(path) (True only for regular files; False "
            "for symlinks/reparse points/directories/non-regular files) — "
            f"missing at base: getattr(dl, '_seg_file_readable', None) -> "
            f"{guard!r}")
        self.assertFalse(
            guard(str(dirseg)),
            f"_seg_file_readable must return False for a non-regular file "
            f"planted at a segment name (a directory): {dirseg}")
        self.assertTrue(
            guard(real_seg),
            f"_seg_file_readable must return True for a real published "
            f"segment file: {real_seg}")

    def _windows_substitute_arm(self, seg: Path, refusal: str) -> None:
        """Deterministic Windows substitute for the symlink/fifo arm: plant a
        plain DIRECTORY at the segment name (creatable unprivileged) and pin
        the OBSERVABLE contract only — delivered() neither blocks (5 s bound)
        nor raises nor parses any entry from the planted path."""
        seg.mkdir(parents=True, exist_ok=False)
        ids = _delivered_bounded(self, self.tmp, SID)
        self.assertEqual(
            ids, [],
            f"a non-regular file (directory) planted at a segment name "
            f"must not be parsed as ledger entries; note: {refusal}")

    def test_symlink_not_parsed(self):
        """C4 — a symlink planted at a segment name
        (<sha256(session)[:32]>.ledger.seg.<hex> in <data>/ops) must NOT be
        parsed as ledger entries. NEW-SURFACE guard-predicate leg (both
        platforms, first): dl._seg_file_readable must exist and return
        False for a planted directory at a segment name / True for a real
        segment file — RED at base everywhere (the predicate is missing).
        POSIX arm (or a privileged Windows host): os.symlink a file holding
        a fake entry at the segment name and assert delivered() does not
        surface evil-1 — RED at base wherever the symlink can be planted
        (verified by reading _seg_paths/_read_seg_file: the glob stats
        follow symlinks and the reader opens through them, so base parses
        evil-1). Windows arm: directory-plant substitute + documented OS
        refusal of the symlink attempt."""
        self._guard_predicate_leg()
        ops = self._ops()
        seg = ops / _seg_name(SID, "evil" * 8)
        target = Path(self.tmp, "evil-target.jsonl")
        now = time.time()
        target.write_text(
            json.dumps({"id": "evil-1", "moment": "m", "ts": now,
                        "text": "x"}) + "\n", encoding="utf-8")
        try:
            os.symlink(str(target), str(seg))
        except OSError as exc:
            self._windows_substitute_arm(
                seg, f"{_SYMLINK_REFUSED_NOTE}; observed {type(exc).__name__}: "
                     f"{exc}")
            return
        # Behavioral arm: the symlink IS planted at the segment name.
        self.assertTrue(seg.is_symlink())
        ids = _delivered_bounded(self, self.tmp, SID)
        self.assertNotIn(
            "evil-1", ids,
            "a symlink planted at a segment name was parsed as ledger "
            "entries — the segment reader must refuse non-regular files "
            "(symlinks), not follow them")

    def test_fifo_skipped(self):
        """C5 — a FIFO planted at a segment name must be skipped without
        blocking. NEW-SURFACE guard-predicate leg (both platforms, first):
        dl._seg_file_readable must exist and return False for a planted
        directory at a segment name / True for a real segment file — RED at
        base everywhere (the predicate is missing). POSIX arm: os.mkfifo at
        a segment name; delivered() must return (never block on the fifo's
        open) and surface nothing from it; the call runs under a 5 s
        threading timeout so a base hang fails loudly. Windows arm (no
        os.mkfifo): the same directory-plant substitute as C4."""
        self._guard_predicate_leg()
        ops = self._ops()
        seg = ops / _seg_name(SID, "fifo" * 8)
        mkfifo = getattr(os, "mkfifo", None)
        if mkfifo is None:
            self._windows_substitute_arm(
                seg, "os.mkfifo does not exist on this host (Windows) — "
                     "the deterministic substitute arm ran instead")
            return
        try:
            mkfifo(str(seg), 0o600)
        except OSError as exc:
            self._windows_substitute_arm(
                seg, f"os.mkfifo was refused on this host ({type(exc).__name__}: "
                     f"{exc}) — the deterministic substitute arm ran instead")
            return
        self.assertTrue(os.path.exists(str(seg)) and
                        not os.path.isfile(str(seg)))
        ids = _delivered_bounded(self, self.tmp, SID)
        self.assertEqual(
            ids, [],
            "a FIFO planted at a segment name must be skipped by the "
            "segment reader without blocking (delivered() returned, but "
            f"parsed/surfaced: {ids!r})")


class RetentionTest(_LedgerTmpTestCase):

    def test_aged_segment_reaped_by_compaction_or_write_path(self):
        """C6 — an ALL-out-of-window segment (every entry ts older than the
        suppression window; entries crafted at ts = now - 10 days) must be
        reaped from disk by a record() call EVEN WHEN the delivery-state lock
        is wedged (ops/.delivery-state.lock held via host.acquire_lock in a
        thread for ~2.5 s). Mechanism-agnostic: however the fix reaps
        (locked compaction retry, lock-free stale-segment reap, phantom-lock
        break), the aged file must be gone (or hold no recoverable entry
        text) once record() returns and the wedge lifts. Base-RED: record()
        fails its single lock-bounded compaction attempt and the aged
        segment persists."""
        import host

        now = time.time()
        aged = dl._publish_segment(self.tmp, SID, [
            {"id": "aged-1", "moment": "m", "ts": now - 10 * 86400,
             "text": "aged probe row"}])
        self.assertTrue(aged, "precondition: aged segment published")
        self.assertEqual(dl._seg_paths(self.tmp, SID), [aged])

        lock_path = os.path.join(self.tmp, "ops", ".delivery-state.lock")
        token = host.acquire_lock(lock_path, 30.0)
        self.assertNotIn(token, (None, "unlocked"),
                         "precondition: wedge holder acquired the lock")
        released = threading.Event()

        def wedge() -> None:
            time.sleep(2.5)
            host.release_lock(lock_path, token)
            released.set()

        holder = threading.Thread(target=wedge, daemon=True)
        holder.start()
        try:
            t0 = time.perf_counter()
            dl.record(self.tmp, SID, [{"id": "fresh-1", "content": "c"}],
                      "m", now=now)
            elapsed = time.perf_counter() - t0
            self.assertLess(
                elapsed, 2.0,
                f"record() blocked {elapsed:.2f}s under the wedged "
                "delivery-state lock; it must stay bounded and still make "
                "progress on retention")
        finally:
            self.assertTrue(released.wait(10.0),
                            "wedge holder never released (test bug)")
            holder.join(10.0)

        if os.path.exists(aged):
            residue = dl._read_seg_file(aged) or []
            recovered = [e.get("id") for e in residue]
            self.fail(
                f"an ALL-out-of-window segment (every entry ts older than "
                f"the suppression window) survived a record() call made "
                f"while the delivery-state lock was wedged; file={aged}, "
                f"recoverable ids={recovered!r} — it must be reaped "
                f"(mechanism-agnostic: by compaction or by the write path)")


class PublishFailureTest(_LedgerTmpTestCase):

    def test_compaction_drain_runs_after_publish_failure(self):
        """C7 — when record()'s OWN segment publication fails, the drain
        must still run for segments already on disk. Seed a pending segment
        via dl._publish_segment, monkeypatch dl._publish_segment to return
        None (test-side), call dl.record(...) with a fresh row, restore.
        The seeded segment must end up MERGED (its id present in delivered()
        AND the segment file unlinked). Base-RED: record() returns before
        _try_compact (the `if seg is None: return` early-out), so the seeded
        segment file survives on disk."""
        now = time.time()
        seeded = dl._publish_segment(self.tmp, SID, [
            {"id": "seed-1", "moment": "m", "ts": now, "text": "c"}])
        self.assertTrue(seeded, "precondition: seeded pending segment")

        real_publish = dl._publish_segment
        dl._publish_segment = lambda *a, **k: None  # test-side failure
        try:
            dl.record(self.tmp, SID, [{"id": "r-1", "content": "x"}],
                      "m", now=now)
        finally:
            dl._publish_segment = real_publish

        self.assertIn(
            "seed-1", dl.delivered_ids(self.tmp, SID, now=now + 60),
            "the seeded segment's row must be in the merged view")
        self.assertFalse(
            os.path.exists(seeded),
            "the already-published pending segment was not drained after "
            "record()'s own publication failed — record() must still run "
            "the compaction/drain pass for segments already on disk (at "
            "base it returns before _try_compact when _publish_segment "
            "fails)")


class EndedMarkerTest(_LedgerTmpTestCase):

    def test_corrupt_marker_does_not_raise(self):
        """C8 — a corrupt terminal marker ({"ended_at": 10**400}) must not
        let an exception escape record(), and the module's existing
        corrupt-marker law must hold: the marker read falls back to st_mtime
        (the same posture as the ValueError/JSON-decode path). Base-RED:
        float(10**400) raises OverflowError, which _delivery_state_ended's
        except tuple does not catch, and record()'s post-compaction marker
        re-check is unwrapped — OverflowError escapes record()."""
        marker = dl.delivery_ended_path(self.tmp, SID)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ended_at": 10 ** 400}))

        raised = None
        try:
            dl.record(self.tmp, SID, [{"id": "r1", "content": "x"}], "m",
                      now=time.time())
        except Exception as exc:  # any escape fails the check
            raised = exc
        self.assertIsNone(
            raised,
            f"record() must not let a corrupt terminal marker escape: got "
            f"{type(raised).__name__}: {raised}")

        # The corrupt-marker law, consistent with the ValueError path: the
        # read degrades to st_mtime, and the freshly-written marker means
        # the session's delivery state reads as ended.
        self.assertTrue(
            dl._delivery_state_ended(self.tmp, SID),
            "the corrupt-marker law must hold: a marker whose ended_at "
            "cannot be coerced falls back to st_mtime (fresh marker => "
            "ended)")


class SnapshotViewTest(_LedgerTmpTestCase):

    def test_snapshot_covers_unmerged_segments(self):
        """C15 — snapshot_for_compact must snapshot the MERGED view (base
        ledger + live unmerged segments). Seed a base row via record +
        _try_compact, publish an unmerged segment holding row "s2", call
        snapshot_for_compact, then read the .compact sidecar via
        read_compact_context and assert s2 is present. Base-RED: only the
        base ledger is read (snapshot ids == ["base-row-1"])."""
        now = time.time()
        dl.record(self.tmp, SID, [{"id": "base-row-1", "content": "c"}],
                  "m", now=now)
        self.assertFalse(
            dl._seg_paths(self.tmp, SID),
            "precondition: the seed row was compacted into the base ledger")
        seg = dl._publish_segment(self.tmp, SID, [
            {"id": "s2", "moment": "m", "ts": now, "text": "c"}])
        self.assertTrue(seg, "precondition: unmerged segment published")

        dl.snapshot_for_compact(self.tmp, SID)
        summary, entries = dl.read_compact_context(self.tmp, SID)
        ids = [e.get("id") for e in entries]
        self.assertIn(
            "s2", ids,
            f"snapshot_for_compact must snapshot the MERGED view (base + "
            f"live segments); snapshot ids were {ids!r} — only the base "
            f"ledger was read")
        self.assertIn("base-row-1", ids,
                      "the seeded base row must survive the snapshot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
