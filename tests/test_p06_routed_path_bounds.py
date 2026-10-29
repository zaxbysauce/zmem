"""Supplementary (unfrozen) pins for the ROUTED record path's cost laws
(review round 2, PRR-003/004): production routes every injection through
record_undelivered(refresh_delivered=True), so the cost/retention invariants
must hold on THAT path, not on the compatibility record() API.

- Steady state: refresh-only admissions (escalation / bridged re-records)
  must NOT accumulate uncompacted segments — the drain runs on every routed
  call, so a burst of all-refresh fires keeps the live segment count bounded
  and the merged view exact.
- Wedge budget: under a wedged delivery-state lock with a 500-segment
  in-window backlog, one routed admission returns within the hook's 8 s
  store budget (structural worst case: the 2 s admission ladder + one
  O(backlog) merged read + one failed drain acquire + the 64-cap reaper;
  the honest O(backlog) listing cost is documented at _reap_dead_segments).

Run standalone: python tests/test_p06_routed_path_bounds.py
"""
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "memory", "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402

SID = "routed-path-bounds-session"


def _clear_pins():
    for key in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                "ZMEM_RESERVATION_TTL_S", "ZMEM_DELIVERY_LOCK_GRACE_S"):
        os.environ.pop(key, None)


class RoutedPathBoundsTest(unittest.TestCase):
    def setUp(self):
        _clear_pins()
        self.tmp = tempfile.mkdtemp(prefix="zmem-routed-bounds-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_refresh_burst_does_not_accumulate_segments(self):
        # Discrimination contract (review round 3): the burst runs PAST the
        # compaction cap (80 fires > _MAX_SEGMENTS_PER_COMPACT=64) and the
        # assertion is EXACT ZERO — a re-gated drain (compact only on
        # admissions) leaves one segment per refresh fire (~80) and fails
        # this; the shipped unconditional drain converges to zero every call.
        now = time.time()
        # First call admits; every later call is refresh-only (admits
        # nothing) — the escalation/bridged steady state.
        dl.record_undelivered(self.tmp, SID,
                              [{"id": "r0", "content": "seed"}],
                              "user_prompt", now=now,
                              refresh_delivered=True)
        for i in range(1, 81):
            dl.record_undelivered(self.tmp, SID,
                                  [{"id": "r0", "content": "seed"}],
                                  "pretool", now=now + i,
                                  refresh_delivered=True)
        live = len(dl._seg_paths(self.tmp, SID))
        self.assertEqual(
            live, 0,
            f"an all-refresh burst left {live} live segments after the "
            f"routed drain - the drain must compact on every call (one "
            f"fire = one segment; a gated drain accumulates ~80 here)")
        entries = dl.delivered(self.tmp, SID, now=now + 90)
        self.assertEqual([e["id"] for e in entries], ["r0"])
        self.assertEqual(entries[0]["moment"], "pretool",
                         "the last refresh must win the upsert")

    def test_wedged_routed_admission_with_backlog_stays_in_budget(self):
        now = time.time()
        for i in range(500):
            dl._publish_segment(self.tmp, SID,
                                [{"id": f"row-{i}", "moment": "m",
                                  "ts": now, "text": "filler"}])
        released = threading.Event()
        acquired = {"token": None}

        def holder():
            import host
            lock_path = os.path.join(self.tmp, "ops", ".delivery-state.lock")
            token = host.acquire_lock(lock_path, 30.0)
            acquired["token"] = token
            released.set()
            time.sleep(3.0)
            if token and token != "unlocked":
                host.release_lock(lock_path, token)

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        released.wait(2.0)
        self.assertNotIn(acquired["token"], (None, "unlocked"),
                         "precondition: the wedge holder acquired the lock")
        time.sleep(0.05)
        try:
            t0 = time.perf_counter()
            admitted = dl.record_undelivered(
                self.tmp, SID, [{"id": "row-new", "content": "fresh"}],
                "m", now=now)
            elapsed = time.perf_counter() - t0
        finally:
            th.join(6.0)
        self.assertEqual(admitted, ["row-new"])
        # Structural worst case: the 2 s admission ladder + one O(backlog)
        # merged read + one failed drain acquire + the 64-cap reaper. The
        # hook's own store budget is 8 s; pin strictly inside it.
        self.assertLess(
            elapsed, 8.0,
            f"a wedged routed admission over a 500-segment backlog took "
            f"{elapsed:.3f}s - the ladder (2s) plus one merged read plus "
            f"the fail-fast drain must stay inside the hook's 8 s budget")


if __name__ == "__main__":
    unittest.main(verbosity=2)
