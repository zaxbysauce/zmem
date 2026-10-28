"""Issue #252 follow-up (P-06) — compaction liveness: stale-break row
safety, backlog growth bound, and Windows unlink containment.

Arm's-length acceptance checks authored against base 41008fdd. Every check
isolates into a throwaway tmpdir (ledger state is ops-dir sidecars only; no
sqlite, no ~/.zmem access). ZMEM_DELIVER_WINDOW_S / ZMEM_LEDGER_CAP are
popped in-process around each test.

Checks (ids are final; each test's docstring carries its id):

- C9  DoubleCompactorTest::test_no_row_loss_under_stale_break
- C10 GrowthBoundTest::test_read_cost_bounded_over_500_segments
- C11 WindowsUnlinkTest::test_clear_partial_state_no_escape
- C12 CommitLabelTest::test_interrupted_marker_ack_reports_duplicate
- C13 LockLivenessTest::test_dead_holder_lock_is_broken_after_grace

C9 timing calibration (authoring host): compactor A's first segment read is
stalled 0.8 s (test-side monkeypatch of dl._read_seg_file, real read done
BEFORE the sleep so A provably holds row-2's content), the stale threshold
is 0.3 s, and compactor B runs 0.45 s after A's stall begins — so B sees
A's lock as stale (age >= 0.45 s > 0.3 s) and finishes well inside A's
0.8 s stall (0.35 s margin). If this proves brittle on a slower host,
widen A's sleep to 1.2 s and the stale threshold to 0.5 s.

Runs standalone: python tests/test_p06_compact_liveness.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import delivery_ledger as dl  # noqa: E402

SID = "sess-p06-liveness"


class _LedgerTmpTestCase(unittest.TestCase):
    """Fresh throwaway ops dir + stripped ledger knobs per test."""

    def setUp(self):
        for key in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_RESERVATION_TTL_S"):
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p06-liveness-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DoubleCompactorTest(_LedgerTmpTestCase):

    def test_no_row_loss_under_stale_break(self):
        """C9 — two REAL _try_compact passes overlapping through a stale
        lock break must lose no row. Compactor A holds the delivery-state
        lock and stalls mid-read (slow-disk simulation, test-side
        monkeypatch of dl._read_seg_file sleeping 0.8 s before RETURNING
        the already-read content, FIRST call only); while A is stalled, a
        segment with row-3 is published (strictly AFTER A's glob — proven
        by the stall event firing inside A's read), and compactor B (real
        _try_compact, run past the 0.3 s stale threshold) breaks A's lock,
        merges row-2+row-3 into the base, and unlinks both segments. When A
        wakes it must not clobber B's merge with its stale base snapshot:
        the final delivered() view must hold rows 1, 2 AND 3. Base-RED:
        A's last-writer-wins base write drops row-3 (its stale base had
        only row-1 and B already unlinked row-3's segment)."""
        dl._DELIVERY_LOCK_STALE_S = 0.3  # test-side (restored in finally)
        real_stale = 30.0
        real_read = dl._read_seg_file
        stalled = threading.Event()
        calls = {"n": 0}

        def slow_first_read(path):
            calls["n"] += 1
            entries = real_read(path)  # read the content FIRST
            if calls["n"] == 1:
                stalled.set()
                time.sleep(0.8)  # slow-disk stall before returning it
            return entries

        now = time.time()
        # Seed the base with row-1 (record auto-compacts into the base).
        dl.record(self.tmp, SID, [{"id": "row-1", "content": "c"}], "m",
                  now=now)
        self.assertFalse(dl._seg_paths(self.tmp, SID),
                         "precondition: base seeded, no live segments")

        dl._read_seg_file = slow_first_read
        compactor_a = threading.Thread(
            target=dl._try_compact, args=(self.tmp, SID, now), daemon=True)
        try:
            # row-2's segment exists BEFORE A starts, so A's glob provably
            # sees exactly it (row-3 is published only after A's stall
            # event proves the glob already happened).
            seg2 = dl._publish_segment(self.tmp, SID, [
                {"id": "row-2", "moment": "m", "ts": now, "text": "c"}])
            self.assertTrue(seg2)
            compactor_a.start()
            self.assertTrue(
                stalled.wait(5.0),
                "compactor A never reached its segment read — its glob must "
                "see row-2's segment (test calibration bug, not a product "
                "signal)")
            seg3 = dl._publish_segment(self.tmp, SID, [
                {"id": "row-3", "moment": "m", "ts": now, "text": "c"}])
            self.assertTrue(seg3)
            # Push A's lock mtime past the stale threshold, then run the
            # second REAL compactor (B) from this thread.
            time.sleep(0.45)
            dl._try_compact(self.tmp, SID, now)
            compactor_a.join(15.0)
            self.assertFalse(
                compactor_a.is_alive(),
                "compactor A did not finish within 15 s of B's pass")
        finally:
            dl._read_seg_file = real_read
            dl._DELIVERY_LOCK_STALE_S = real_stale

        final = set(dl.delivered_ids(self.tmp, SID, now=now + 60))
        self.assertEqual(
            final, {"row-1", "row-2", "row-3"},
            "a row was lost when compactor B broke stalled compactor A's "
            "aged lock and both wrote the base ledger — the final merged "
            f"view is {sorted(final)!r}; row-3 (published after A's glob, "
            "merged and unlinked by B) was clobbered by A's stale-base "
            "last-writer-wins write at base")


class GrowthBoundTest(_LedgerTmpTestCase):

    # Backlog bound rationale: one in-lock compaction pass merges at most
    # _MAX_SEGMENTS_PER_COMPACT (64) segments, so a single pass over a
    # 501-segment backlog leaves 437 live segments. The bound below allows
    # at most TWO 64-segment passes of residue plus slack (2*64 + 2 = 130):
    # the write path must DRAIN backlogs across passes (or drain bigger
    # batches), never stop after one. A following delivered() read then
    # costs at most 130 segment-file opens under the same bound.
    BOUND = 130

    def test_read_cost_bounded_over_500_segments(self):
        """C10 — after 500 published one-row segments, ONE record() call
        must drain the backlog down to <= 130 live segments (a single
        64-segment compaction pass — the base end state, 437 remaining —
        is not acceptable), and a following delivered() call must read at
        most 130 segment files (counted via a test-side counting wrapper
        around dl._read_seg_file). No wall-clock assertions (flaky by
        design). Base-RED: 437 segments remain and delivered() reads ~437
        files."""
        now = time.time()
        for i in range(500):
            seg = dl._publish_segment(self.tmp, SID, [
                {"id": f"g-{i}", "moment": "m", "ts": now, "text": "c"}])
            self.assertTrue(seg)
        self.assertEqual(len(dl._seg_paths(self.tmp, SID)), 500,
                         "precondition: 500 live segments")

        dl.record(self.tmp, SID, [{"id": "g-new", "content": "c"}], "m",
                  now=now)

        remaining = dl._seg_paths(self.tmp, SID)
        self.assertLessEqual(
            len(remaining), self.BOUND,
            f"one record() call left {len(remaining)} live segments after a "
            f"500-segment backlog (bound {self.BOUND} = two 64-segment "
            "passes + slack): the write path must drain backlogs across "
            "passes, not stop after a single 64-segment compaction (base "
            "leaves 437)")

        real_read = dl._read_seg_file
        counts = {"n": 0}

        def counting_read(path):
            counts["n"] += 1
            return real_read(path)

        dl._read_seg_file = counting_read
        try:
            dl.delivered(self.tmp, SID, now=now + 60)
        finally:
            dl._read_seg_file = real_read
        self.assertLessEqual(
            counts["n"], self.BOUND,
            f"delivered() opened {counts['n']} segment files after one "
            f"record() call over a 500-segment backlog (bound {self.BOUND}) "
            "— read cost must stay bounded by the drained end state, not "
            "the full backlog")


class WindowsUnlinkTest(_LedgerTmpTestCase):

    def test_clear_partial_state_no_escape(self):
        """C11 — a reader holding a published segment OPEN (plain open(),
        no FILE_SHARE_DELETE on Windows) must not make the terminal clear
        raise: end_delivery_state must not let the sharing violation escape
        (marker atomicity + no exception only; best-effort segment residue
        is acceptable once the handle closes). clear() under the same held
        handle must not raise either. Base-RED on Windows:
        end_delivery_state raises PermissionError [WinError 32] out of the
        segment unlink (the marker, written first, does land). On POSIX the
        open handle does not block unlink, so this check passes trivially
        at base there — it is the Windows-leg check; both signatures are
        reported by the authoring run."""
        now = time.time()
        seg = dl._publish_segment(self.tmp, SID, [
            {"id": "u-1", "moment": "m", "ts": now, "text": "c"}])
        self.assertTrue(seg)
        handle = open(seg, "r")  # held open for the whole check
        end_exc = None
        clear_exc = None
        try:
            try:
                dl.end_delivery_state(self.tmp, SID)
            except Exception as exc:  # any escape fails the check
                end_exc = exc
            try:
                dl.clear(self.tmp, SID)
            except Exception as exc:
                clear_exc = exc
            self.assertTrue(
                os.path.exists(dl.delivery_ended_path(self.tmp, SID)),
                "the terminal marker must land (atomically, before any "
                "unlink) even when a segment unlink later fails")
        finally:
            handle.close()
        self.assertIsNone(
            end_exc,
            "end_delivery_state must not let a segment-unlink failure "
            "(e.g. a Windows sharing violation while a reader holds the "
            "segment open) escape toward the CLI: "
            f"{type(end_exc).__name__}: {end_exc}")
        self.assertIsNone(
            clear_exc,
            "clear() must not raise under a held-open segment either: "
            f"{type(clear_exc).__name__}: {clear_exc}")


class CommitLabelTest(_LedgerTmpTestCase):

    def test_interrupted_marker_ack_reports_duplicate(self):
        """C12 — the interrupted-marker ack must report the honest label.
        Simulate the killed-after-admission state: mint a reservation
        sidecar via dl.reserve (committed flag ABSENT), then land the
        admission the kill left behind via dl.record_undelivered on the
        SAME rows (the child died between publish and sidecar commit),
        then call dl.commit_reservation. The result must be committed==[]
        AND duplicate True — the ids are already delivered, so a
        fresh-empty + duplicate-False reply is the mislabel. GREEN
        condition is behavioral only: duplicate True; however the fix
        reconciles the ack with the ledger is not asserted. Base-RED:
        commit_reservation (delivery_ledger.py 1020-1031 at base) never
        reconciles with delivered state — the uncommitted, unexpired
        sidecar falls through to a re-record that admits nothing and
        returns {"committed": [], "duplicate": False, "expired": False}."""
        sid = "sess-p06-commit-label"
        rows = [{"id": "m-1", "content": "x"}]
        reservation_id = dl.reserve(self.tmp, sid, rows, "m")
        self.assertTrue(reservation_id,
                        "precondition: reservation sidecar minted")

        admitted = dl.record_undelivered(self.tmp, sid, rows, "m")
        self.assertEqual(
            admitted, ["m-1"],
            f"precondition: the interrupted admission landed in the ledger "
            f"(got {admitted!r})")

        result = dl.commit_reservation(self.tmp, reservation_id)
        self.assertEqual(
            result.get("committed"), [],
            f"the ack of an already-delivered reservation must commit "
            f"nothing: {result!r}")
        self.assertIs(
            result.get("duplicate"), True,
            f"the ack of a reservation whose ids are ALREADY delivered "
            f"(the killed-after-admission state) must report the honest "
            f"duplicate label, not a fresh empty commit: {result!r}")


class LockLivenessTest(_LedgerTmpTestCase):

    def test_dead_holder_lock_is_broken_after_grace(self):
        """C13 — a delivery-state lock whose recorded holder PID is
        provably DEAD must be broken (via the host's existing
        phantom-break machinery) once its age exceeds the env-tunable
        grace, instead of waiting out the full stale window. The fix-plan
        knob ZMEM_DELIVERY_LOCK_GRACE_S (default 10) is pinned to 1 for
        this check.

        Lock fixture: ops/.delivery-state.lock holding
        f"{dead_pid}:{uuid4().hex}", where dead_pid comes from a
        subprocess.Popen([sys.executable, "-c", "pass"]) that was joined;
        host.process_alive(dead_pid) must return False (up to 5 candidate
        pids; none verifying dead fails the test LOUDLY — no skip).

        AGE DEVIATION from the commissioning note, documented: the note
        said "age the lock 120 s in the past", but the base stale window
        (_DELIVERY_LOCK_STALE_S) is 30 s and the base break path
        (host._break_stale_lock) is purely mtime-age-based — a 120 s-aged
        lock is ALREADY breakable at base, which would make this check
        green/vacuous at base and contradict the note's own required
        signature ("DeliveryStateLockError after the 0.25 s bounded
        wait"). The lock is therefore aged 5 s: strictly above the pinned
        1 s grace (head breaks it) and strictly below the 30 s stale
        window (base must treat the holder as live and time out).

        Assert: dl._delivery_state_lock(data_dir) YIELDS within 5 s
        (acquire succeeds via the dead-holder break) and the context
        manager completes with no exception (its release path included).
        Base-RED: DeliveryStateLockError after the 0.25 s bounded wait
        (base has no liveness wiring in the delivery lock)."""
        import host

        dead_pid = None
        for _ in range(5):
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            child.wait(timeout=30)
            if host.process_alive(child.pid) is False:
                dead_pid = child.pid
                break
        self.assertIsNotNone(
            dead_pid,
            "could not produce a provably-dead holder pid within 5 "
            "candidates (host.process_alive refused to classify any "
            "reaped child as dead) — failing loudly rather than skipping")

        ops = os.path.join(self.tmp, "ops")
        os.makedirs(ops, exist_ok=True)
        lock_path = os.path.join(ops, ".delivery-state.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(f"{dead_pid}:{uuid.uuid4().hex}")
        aged = time.time() - 5.0  # above grace (1 s), below stale (30 s)
        os.utime(lock_path, (aged, aged))
        self.assertIs(host.process_alive(dead_pid), False,
                      "precondition: the recorded holder is still provably "
                      "dead at fixture time")

        saved_grace = os.environ.get("ZMEM_DELIVERY_LOCK_GRACE_S")
        os.environ["ZMEM_DELIVERY_LOCK_GRACE_S"] = "1"
        try:
            raised = None
            t0 = time.perf_counter()
            try:
                with dl._delivery_state_lock(self.tmp):
                    pass  # yielded: the dead-holder lock was broken
            except Exception as exc:  # any escape/timeout fails the check
                raised = exc
            elapsed = time.perf_counter() - t0
        finally:
            if saved_grace is None:
                os.environ.pop("ZMEM_DELIVERY_LOCK_GRACE_S", None)
            else:
                os.environ["ZMEM_DELIVERY_LOCK_GRACE_S"] = saved_grace

        self.assertIsNone(
            raised,
            "a delivery-state lock whose recorded holder PID is provably "
            "dead and whose age exceeds ZMEM_DELIVERY_LOCK_GRACE_S must be "
            "broken instead of waited out — the context manager raised "
            f"{type(raised).__name__}: {raised}")
        self.assertLess(
            elapsed, 5.0,
            f"the dead-holder break took {elapsed:.2f}s — acquire must "
            f"succeed well inside 5 s once the 1 s grace has passed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
