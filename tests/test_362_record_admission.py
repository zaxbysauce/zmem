"""Issue #362 (via #284) — non-reserve record-path admission: admit-once
accounting across processes, lock wiring, and the escalation upsert parity
floor.

Arm's-length acceptance checks authored against base 41008fdd. Every check
isolates into a throwaway tmpdir (ledger state is ops-dir sidecars only; no
sqlite, no ~/.zmem access). Subprocess children get every ambient ZMEM_*
key stripped, then ZMEM_DATA=<tmp> / ZMEM_STORE=<tmp>/store.sqlite /
PYTHONUTF8=1 / PYTHONIOENCODING=utf-8; in-process tests pop the ledger
knobs around each case.

Checks (ids are final; each test's docstring carries its id):

- C1  RecordAdmissionRaceTest::
      test_concurrent_record_path_admits_one        (NEW-SURFACE, base RED)
- C16 RecordAdmissionWiringTest::
      test_nonreserve_selector_records_through_admission_api
                                                   (NEW-SURFACE, base RED)
- C2  EscalationUpsertParityTest::
      test_escalation_re_record_refreshes_entry     (PRESERVING, base GREEN)

Authored module contract for C1/C16 (the implementer must satisfy it):
``storelib.delivery_ledger.record_undelivered`` grows an upsert-mode
admission entry point used by the NON-RESERVE record path —
``record_undelivered(data_dir, session_id, rows, moment, ...,
refresh_delivered=True)`` — returning the ids THIS call admitted. Two
concurrent writers racing the SAME (session, memory id) resolve to exactly
one winner; the loser gets [] back and observes the id as already
delivered. At base the kwarg does not exist, so the child calls raise
TypeError — that TypeError IS the base RED signature.

Runs standalone: python tests/test_362_record_admission.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
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
SCRIPTS = str(REPO_ROOT / "skills" / "memory" / "scripts")
sys.path.insert(0, SCRIPTS)

from storelib import delivery_ledger as dl  # noqa: E402

ROW_ID = "mem-1"

# Child writer: START file barrier, then the upsert-mode admission call for
# the SAME (session, memory id) as the sibling, retrying until definite.
# Exit 3 + "API_ERROR ..." reports the missing surface at base (the stable
# TypeError); exit 0 + "RESULT {...}" reports a definite race outcome.
_CHILD_CODE = r'''
import json
import os
import sys
import time

scripts_dir, data_dir, sid, barrier_dir, writer = sys.argv[1:6]
sys.path.insert(0, scripts_dir)
from storelib import delivery_ledger as dl

# Deterministic START barrier: publish readiness, poll for the sibling, so
# both children are inside the admission window simultaneously.
with open(os.path.join(barrier_dir, "ready-" + writer), "w",
          encoding="utf-8") as f:
    f.write("1")
other = "ready-" + str(1 - int(writer))
deadline = time.time() + 60.0
while not os.path.exists(os.path.join(barrier_dir, other)):
    if time.time() > deadline:
        raise SystemExit("start barrier timeout")
    time.sleep(0.002)

rows = [{"id": "mem-1", "content": "non-reserve admission race row"}]
retry_deadline = time.time() + 5.0
while True:
    try:
        admitted = dl.record_undelivered(
            data_dir, sid, rows, "user_prompt", refresh_delivered=True)
    except TypeError as exc:
        print("API_ERROR " + type(exc).__name__ + ": " + str(exc))
        raise SystemExit(3)
    sees_delivered = "mem-1" in dl.delivered_ids(data_dir, sid)
    if admitted or sees_delivered or time.time() > retry_deadline:
        print("RESULT " + json.dumps({
            "admitted": [str(i) for i in (admitted or [])],
            "sees_delivered": bool(sees_delivered),
        }))
        raise SystemExit(0)
    time.sleep(0.025)
'''


def _child_env(tmp: str) -> dict:
    """Scrubbed child env: strip EVERY ambient ZMEM_* key, then pin the
    throwaway scratch (no ~/.zmem, no ambient store)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_DATA": tmp,
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


class _LedgerTmpTestCase(unittest.TestCase):
    """Fresh throwaway ops dir + stripped ledger knobs per test."""

    def setUp(self):
        for key in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_RESERVATION_TTL_S"):
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-362-admit-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class RecordAdmissionRaceTest(_LedgerTmpTestCase):

    def test_concurrent_record_path_admits_one(self):
        """C1 — NEW-SURFACE: the non-reserve record path gets admit-once
        accounting. Two REAL subprocesses (process-START file barrier, the
        house pattern) each drive the upsert-mode admission entry point for
        the SAME session and SAME memory id ("mem-1"), retrying for up to
        5 s until both hold a definite result. Exactly ONE child must be
        admitted (["mem-1"]); the loser must get [] back AND observe the id
        in delivered(). The parent also fails when the entry point is
        missing — at base record_undelivered takes no refresh_delivered
        kwarg, so both children exit 3 with the TypeError (the base RED
        signature)."""
        barrier = os.path.join(self.tmp, "barrier")
        os.makedirs(barrier, exist_ok=True)
        sid = f"362-admit-{uuid.uuid4().hex}"

        children = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_CODE,
                 SCRIPTS, self.tmp, sid, barrier, str(w)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=_child_env(self.tmp))
            for w in range(2)
        ]
        results = []
        try:
            for w, child in enumerate(children):
                out, err = child.communicate(timeout=120)
                api_error = next((l for l in out.splitlines()
                                  if l.startswith("API_ERROR ")), "")
                if child.returncode == 3 or api_error:
                    self.fail(
                        "the non-reserve record path's admit-once surface is "
                        "missing at base: record_undelivered(..., "
                        "refresh_delivered=True) rejected the call — "
                        f"{api_error or 'child exited 3 without API_ERROR'}")
                self.assertEqual(
                    child.returncode, 0,
                    f"admission writer {w} exited {child.returncode}: "
                    f"{err.strip()[-1500:]}")
                line = next((l for l in out.splitlines()
                             if l.startswith("RESULT ")), "")
                self.assertTrue(
                    line,
                    f"admission writer {w} printed no RESULT line: "
                    f"{out} {err}")
                results.append(json.loads(line[len("RESULT "):]))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                for pipe in (child.stdout, child.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass

        winners = [i for i, r in enumerate(results)
                   if ROW_ID in r["admitted"]]
        self.assertEqual(
            len(results), 2, "both children must report a definite result")
        self.assertEqual(
            len(winners), 1,
            f"exactly one concurrent writer must win admission of {ROW_ID}; "
            f"results: {results}")
        loser = results[1 - winners[0]]
        self.assertEqual(
            loser["admitted"], [],
            f"the losing writer also claims admission: {results}")
        self.assertTrue(
            loser["sees_delivered"],
            "the losing writer must observe the pair as already-delivered "
            f"(delivered_ids after its call): {results}")

        self.assertEqual(
            dl.delivered_ids(self.tmp, sid), [ROW_ID],
            "the merged ledger view after the race must hold exactly the "
            "one admitted id")


class RecordAdmissionWiringTest(_LedgerTmpTestCase):

    def test_nonreserve_selector_records_through_admission_api(self):
        """C16 — in-process lock-wiring pin (the tests/test_t05_ledger_lock_
        wiring.py pattern), pinned at the delivery_ledger layer (the
        thinnest real surface; a store-seeded drive through inject's
        selector needs a full store fixture for the same signal).

        Pinned, precisely: (leg 1) the upsert-mode admission entry point
        (record_undelivered(..., refresh_delivered=True)) exists, admits
        the fresh row, and its _publish_segment fires with the
        delivery-state lock HELD (host.acquire_lock recorded and not yet
        released — the t05 wiring discriminator), with ops/
        .delivery-state.lock among the acquired paths; (leg 2) plain
        dl.record() STILL does not require the lock — under a wedged
        ops/.delivery-state.lock (raw host.acquire_lock held in a thread)
        record() returns within 1.0 s and its row still publishes (the
        test_p04_ledger_concurrent_writers wedge law).

        Base-RED: leg 1 — plain record() never returns admission and
        record_undelivered has no refresh_delivered kwarg, so the call
        raises TypeError (the stable base signature); leg 2 alone is GREEN
        at base (record() publishes lock-free and its single bounded
        compaction attempt times out under the wedge)."""
        import host

        calls = []
        state = {"held": False, "published_inside": None}
        real_acquire = host.acquire_lock
        real_release = host.release_lock
        real_publish = dl._publish_segment
        no_token = getattr(host, "_NO_LOCK_TOKEN", "unlocked")

        def recording_acquire(path, stale_s=None):
            calls.append(str(path))
            token = real_acquire(path, stale_s)
            if token is not None and token != no_token:
                state["held"] = True
            return token

        def recording_release(path, token):
            state["held"] = False
            return real_release(path, token)

        def checking_publish(data_dir, key, entries):
            if state["published_inside"] is None:
                state["published_inside"] = state["held"]
            return real_publish(data_dir, key, entries)

        host.acquire_lock = recording_acquire
        host.release_lock = recording_release
        dl._publish_segment = checking_publish
        try:
            sid = f"admit-wire-{uuid.uuid4().hex}"
            rows = [{"id": "wire-1", "content": "admission wiring probe"}]
            # Leg 1: the admission surface must exist and publish under the
            # lock (NEW-SURFACE; TypeError at base).
            try:
                admitted = dl.record_undelivered(
                    self.tmp, sid, rows, "user_prompt",
                    refresh_delivered=True)
            except TypeError as exc:
                self.fail(
                    "the non-reserve record path must record through the "
                    "upsert-mode admission API: record_undelivered(..., "
                    f"refresh_delivered=True) is missing at base — "
                    f"TypeError: {exc}")
            self.assertEqual(admitted, ["wire-1"],
                             f"admission must admit the fresh row: "
                             f"{admitted!r}")
            first_publish_state = state["published_inside"]

            # Leg 2: plain record() must not REQUIRE the delivery lock.
            sid2 = f"record-wedge-{uuid.uuid4().hex}"
            lock_path = os.path.join(self.tmp, "ops",
                                     ".delivery-state.lock")
            token = real_acquire(lock_path, 30.0)
            self.assertNotIn(token, (None, "unlocked"),
                             "precondition: wedge holder acquired the lock")
            released = threading.Event()

            def wedge() -> None:
                time.sleep(2.5)
                real_release(lock_path, token)
                released.set()

            holder = threading.Thread(target=wedge, daemon=True)
            holder.start()
            try:
                t0 = time.perf_counter()
                dl.record(self.tmp, sid2,
                          [{"id": "wedge-row-1", "content": "c"}], "m",
                          now=time.time())
                elapsed = time.perf_counter() - t0
            finally:
                self.assertTrue(released.wait(10.0),
                                "wedge holder never released (test bug)")
                holder.join(10.0)
        finally:
            host.acquire_lock = real_acquire
            host.release_lock = real_release
            dl._publish_segment = real_publish

        self.assertTrue(
            first_publish_state is True,
            "the admission path must publish WITH the delivery-state lock "
            "held (host.acquire_lock acquired and not yet released when "
            "_publish_segment runs) — acquire-then-publish-outside would "
            f"re-open the double-fire window; observed "
            f"published_inside={first_publish_state!r}, acquire_lock "
            f"calls: {calls}")
        lock_paths = [p for p in calls if p.endswith(".delivery-state.lock")]
        self.assertTrue(
            lock_paths,
            "the admission path must acquire the delivery-state lock "
            "(host.acquire_lock on ops/.delivery-state.lock) around its "
            f"check+publish; observed acquire_lock calls: {calls}")

        self.assertLess(
            elapsed, 1.0,
            f"plain record() blocked {elapsed:.2f}s under a wedged "
            "delivery-state lock — the non-admission record path must stay "
            "lock-independent (bounded single compaction attempt only)")
        self.assertIn(
            "wedge-row-1", dl.delivered_ids(self.tmp, sid2),
            "plain record() must still publish its row under the wedged "
            "lock")


class EscalationUpsertParityTest(_LedgerTmpTestCase):

    def test_escalation_re_record_refreshes_entry(self):
        """C2 — PRESERVING floor (GREEN at base and at head; guards the
        byte-compat upsert law at _reduce_entries): recording id "e1" at
        ts t1, then RE-RECORDING the same id with a NEWER now via
        dl.record(...), must leave EXACTLY ONE entry whose ts equals the
        newer value and whose moment refreshed to the re-record's moment
        (the escalation re-admission refresh)."""
        t1, t2 = 1_000.0, 2_000.0
        dl.record(self.tmp, "sess-362-parity",
                  [{"id": "e1", "content": "escalation parity row"}],
                  "user_prompt", now=t1)
        dl.record(self.tmp, "sess-362-parity",
                  [{"id": "e1", "content": "escalation parity row"}],
                  "pretool", now=t2)

        entries = dl.delivered(self.tmp, "sess-362-parity", now=t2 + 60)
        self.assertEqual(
            len(entries), 1,
            f"the re-recorded id must hold EXACTLY ONE entry (upsert law; "
            f"duplicates would double-suppress): {entries!r}")
        self.assertEqual(entries[0]["ts"], t2,
                         "the entry's ts must equal the NEWER re-record now")
        self.assertEqual(entries[0]["moment"], "pretool",
                         "the re-record must refresh the entry's moment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
