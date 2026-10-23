"""Supplementary (unfrozen) pin for the non-finite ended_at law (#320).

The frozen C8 check pins the OverflowError leg ({"ended_at": 10**400});
this file pins the sibling that raises NOTHING: json parses 1e400 and
Infinity to float('inf'), and at base (time.time() - inf) <= ttl is always
True, wedging the session phantom-ended forever. After the fix a non-finite
parsed marker takes the same corrupt path as any other (st_mtime fallback;
a fresh marker keeps suppressing only for the marker TTL).

Run standalone: python tests/test_p05_marker_nonfinite.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "memory", "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402

SID = "nonfinite-marker-session"


class NonFiniteMarkerLawTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k)
                           for k in ("ZMEM_DELIVER_WINDOW_S",
                                     "ZMEM_LEDGER_CAP",
                                     "ZMEM_RESERVATION_TTL_S",
                                     "ZMEM_DELIVERY_LOCK_GRACE_S")}
        for key in self._saved_env:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-nonfinite-marker-")

    def tearDown(self):
        import shutil
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant_marker(self, raw):
        path = dl.delivery_ended_path(self.tmp, SID)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        return path

    def test_inf_ended_at_takes_the_corrupt_marker_law(self):
        marker = self._plant_marker({"ended_at": float("inf")})
        # record() must not raise, and the marker law matches the corrupt
        # ValueError path: st_mtime fallback (fresh) -> suppressing now, but
        # bounded by the marker TTL, never wedged forever by an inf ts.
        now = time.time()
        dl.record(self.tmp, SID,
                  [{"id": "r1", "content": "hello"}], "m", now=now)
        self.assertTrue(os.path.exists(marker))
        # An AGED corrupt marker expires exactly like a corrupt ValueError
        # marker: os.utime the marker past the TTL and the session reopens.
        old = time.time() - (dl._DELIVERY_END_MARKER_TTL_S + 10)
        os.utime(marker, (old, old))
        self.assertFalse(dl._delivery_state_ended(self.tmp, SID))

    def test_1e400_literal_is_nonfinite_not_overflow(self):
        # json.loads("1e400") -> inf WITHOUT raising; pin the distinction the
        # fix relies on (isfinite catches what the except tuple cannot).
        parsed = json.loads('{"ended_at": 1e400}')
        self.assertIsInstance(parsed["ended_at"], float)
        self.assertTrue(parsed["ended_at"] == float("inf"))
        self._plant_marker(parsed)
        marker = dl.delivery_ended_path(self.tmp, SID)
        # Non-finite takes the corrupt path: st_mtime fallback (fresh) reads
        # as suppressing-now, bounded by the marker TTL, never inf-wedged.
        self.assertTrue(dl._delivery_state_ended(self.tmp, SID))
        old = time.time() - (dl._DELIVERY_END_MARKER_TTL_S + 10)
        os.utime(marker, (old, old))
        self.assertFalse(dl._delivery_state_ended(self.tmp, SID))
        now = time.time()
        dl.record(self.tmp, SID,
                  [{"id": "r2", "content": "hello"}], "m", now=now)


if __name__ == "__main__":
    unittest.main(verbosity=2)
