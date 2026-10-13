"""Issue #252 — concurrent ledger writers. RED at base (lost-update race),
GREEN after the concurrency-safe write path lands.

Pinned here:
- storelib.delivery_ledger.record(): four concurrent writers sharing one
  session ledger — threads in one process, and real child processes across
  process boundaries — must never silently lose a row. Every call records a
  distinct id under one fixed ``now``; anything short of 4 x 50 = 200
  delivered ids is a lost update (a contender missed the bounded
  delivery-state lock deadline and the caller's rows were swallowed with
  the DeliveryStateLockError).
- the wedged-lock primitive: while another holder sits on the
  delivery-state lock, record() must return inside the store-recall budget
  (hooks/timeout-budget.json) and the row must still be delivered.

All stores are throwaway temp dirs; the ledger knobs ZMEM_LEDGER_CAP /
ZMEM_DELIVER_WINDOW_S are stripped in-process and from every child env.
Runs standalone: python tests/test_p04_ledger_concurrent_writers.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BUDGET = REPO_ROOT / "hooks" / "timeout-budget.json"
sys.path.insert(0, str(SCRIPTS))

from storelib import delivery_ledger  # noqa: E402

WRITERS = 4
PER_WRITER = 50
SHARED_SID = "sess-p04-shared"

_CHILD_CODE = '''
import sys
import time

scripts_dir, data_dir, sid, base_raw, writer_raw = sys.argv[1:6]
sys.path.insert(0, scripts_dir)
from storelib import delivery_ledger

base = float(base_raw)
writer = int(writer_raw)
while time.time() < base:
    time.sleep(0.001)
for i in range(50):
    delivery_ledger.record(
        data_dir, sid, [{"id": f"{writer}-r{i}", "content": "c"}],
        "subagent", now=base)
'''


class ConcurrentThreadWritersTest(unittest.TestCase):
    """4 threads x 50 single-row record() calls against one shared ledger."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-threads-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_threaded_writers_lose_no_entries(self):
        self.assertNotIn("ZMEM_LEDGER_CAP", os.environ)
        self.assertNotIn("ZMEM_DELIVER_WINDOW_S", os.environ)
        self.assertGreaterEqual(delivery_ledger._cap(), 200)

        base = time.time()
        barrier = threading.Barrier(WRITERS)

        def writer(w: int) -> None:
            barrier.wait(timeout=30.0)
            for i in range(PER_WRITER):
                delivery_ledger.record(
                    self.tmp, SHARED_SID,
                    [{"id": f"{w}-r{i}", "content": "c"}],
                    "subagent", now=base)

        threads = [threading.Thread(target=writer, args=(w,), daemon=True)
                   for w in range(WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)

        n = len(set(delivery_ledger.delivered_ids(
            self.tmp, SHARED_SID, now=base)))
        self.assertEqual(n, 200)


class ConcurrentProcessWritersTest(unittest.TestCase):
    """4 real child processes x 50 record() calls against one shared ledger."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-procs-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_process_writers_lose_no_entries(self):
        self.assertNotIn("ZMEM_LEDGER_CAP", os.environ)
        self.assertNotIn("ZMEM_DELIVER_WINDOW_S", os.environ)
        self.assertGreaterEqual(delivery_ledger._cap(), 200)

        base = time.time() + 2.0
        child_env = {k: v for k, v in os.environ.items()
                     if k not in ("ZMEM_LEDGER_CAP", "ZMEM_DELIVER_WINDOW_S")}
        child_env["PYTHONPATH"] = str(SCRIPTS)
        child_env["PYTHONUTF8"] = "1"

        children = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_CODE, str(SCRIPTS), self.tmp,
                 SHARED_SID, repr(base), str(w)],
                env=child_env)
            for w in range(WRITERS)
        ]
        for w, child in enumerate(children):
            rc = child.wait(timeout=120)
            self.assertEqual(rc, 0, f"child writer {w} exited {rc}")

        n = len(set(delivery_ledger.delivered_ids(
            self.tmp, SHARED_SID, now=base)))
        self.assertEqual(n, 200)


class WedgedLockPrimitiveTest(unittest.TestCase):
    """record() under a wedged delivery-state lock: bounded, still delivered."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-p04-wedged-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_returns_within_budget_when_lock_held(self):
        sid = "sess-p04-wedged"
        base = time.time()
        acquired = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with delivery_ledger._delivery_state_lock(self.tmp):
                acquired.set()
                release.wait(10.0)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        if not acquired.wait(5.0):
            release.set()
            holder.join(timeout=5.0)
            self.fail("holder thread did not acquire the delivery-state "
                      "lock within 5s")

        t0 = time.perf_counter()
        delivery_ledger.record(
            self.tmp, sid, [{"id": "wedged-1", "content": "c"}],
            "subagent", now=base)
        elapsed = time.perf_counter() - t0

        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        store_recall_s = float(budget["store_recall_ms"]) / 1000.0
        # "well inside" the 8s store-recall budget, per the issue: the
        # design's worst case is ONE bounded compaction lock attempt, so 1s
        # pins the bound tightly enough to kill a hot-path-blocking "fix"
        # that waits ~6-7s on the lock before falling back (plan critic
        # round 1, blocker 6).
        tight_bound_s = min(1.0, store_recall_s)
        self.assertLess(
            elapsed, tight_bound_s,
            f"record() blocked {elapsed:.2f}s under a wedged "
            f"delivery-state lock, beyond the {tight_bound_s:.2f}s "
            f"design bound (store-recall budget {store_recall_s:.2f}s)")

        release.set()
        holder.join(timeout=5.0)

        rid = "wedged-1"
        self.assertIn(
            rid, delivery_ledger.delivered_ids(self.tmp, sid, now=base),
            f"{rid} not delivered after wedged lock (elapsed={elapsed:.2f}s)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
