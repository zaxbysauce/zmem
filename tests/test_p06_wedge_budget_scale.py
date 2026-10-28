"""Supplementary (unfrozen) pin: record()'s wedge budget holds at BACKLOG
SCALE, not just on the frozen pins' tiny fixtures (review round 1, MAJOR-1).

The frozen wedge pins (test_p04_ledger_concurrent_writers, C16 leg 2, C6)
use <=2 segments. This file wedges the delivery-state lock with a 500-segment
in-window backlog and requires record() to return under 1.0 s AND still
publish its row — the write-path cost under a wedged lock must be O(1) in
the backlog (the bounded drain stops at the first failed acquire; the reaper
scans at most _MAX_SEGMENTS_PER_COMPACT oldest-first).

Run standalone: python tests/test_p06_wedge_budget_scale.py
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

SID = "wedge-budget-scale-session"


class WedgeBudgetScaleTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k)
                           for k in ("ZMEM_DELIVER_WINDOW_S",
                                     "ZMEM_LEDGER_CAP",
                                     "ZMEM_RESERVATION_TTL_S",
                                     "ZMEM_DELIVERY_LOCK_GRACE_S")}
        for key in self._saved_env:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-wedge-scale-")

    def tearDown(self):
        import shutil
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_under_wedge_with_500_segment_backlog_stays_bounded(self):
        now = time.time()
        for i in range(500):
            dl._publish_segment(self.tmp, SID,
                                [{"id": f"row-{i}", "moment": "m",
                                  "ts": now, "text": "filler"}])
        self.assertEqual(len(dl._seg_paths(self.tmp, SID)), 500)
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
        # Precondition (review round 2, PRR-016/ti-05): prove the wedge is
        # actually held, else the timing assert proves nothing.
        self.assertNotIn(acquired["token"], (None, "unlocked"),
                         "precondition: wedge holder acquired the lock")
        time.sleep(0.05)
        try:
            t0 = time.perf_counter()
            dl.record(self.tmp, SID,
                      [{"id": "row-new", "content": "fresh"}], "m", now=now)
            elapsed = time.perf_counter() - t0
        finally:
            th.join(6.0)
        self.assertLess(
            elapsed, 1.0,
            f"record() under a wedged lock with a 500-segment backlog took "
            f"{elapsed:.3f}s - the wedge budget must be O(1) in the backlog "
            f"(bounded drain stops at the first failed acquire; the reaper "
            f"scans <= _MAX_SEGMENTS_PER_COMPACT oldest-first)")
        self.assertIn("row-new", dl.delivered_ids(self.tmp, SID),
                      "the row must still be published under the wedge")


if __name__ == "__main__":
    unittest.main(verbosity=2)
