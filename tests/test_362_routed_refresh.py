"""Supplementary (unfrozen) routed-refresh pins for the #362 remainder fix.

The frozen checks pin admission accounting (C1) and record()'s upsert law
(C2); this file pins the path the hooks actually drive after 0.118.0:
record_undelivered(..., refresh_delivered=True) must refresh an
already-delivered entry under the SAME upsert law the reducer implements
(_entry_replaces), while returning it as NOT admitted.

Run standalone: python tests/test_362_routed_refresh.py
"""
import _store_guard  # noqa: F401  (issue #244 isolation guard)
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "memory", "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402

SID = "routed-refresh-session"


def _row(rid, content="escalation fuel"):
    return {"id": rid, "content": content}


class RoutedRefreshLawTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k)
                           for k in ("ZMEM_DELIVER_WINDOW_S",
                                     "ZMEM_LEDGER_CAP",
                                     "ZMEM_RESERVATION_TTL_S",
                                     "ZMEM_DELIVERY_LOCK_GRACE_S")}
        for key in self._saved_env:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-routed-refresh-")

    def tearDown(self):
        import shutil
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_routed_refresh_updates_entry_and_returns_not_admitted(self):
        now1 = 1000.0
        first = dl.record_undelivered(self.tmp, SID, [_row("e1")], "prompt",
                                      now=now1, refresh_delivered=True)
        self.assertEqual(first, ["e1"])
        now2 = 2000.0
        second = dl.record_undelivered(self.tmp, SID, [_row("e1")], "pretool",
                                       now=now2, refresh_delivered=True)
        # The re-delivery is NOT a first admission...
        self.assertEqual(second, [])
        # ...but the entry DID refresh (the escalation suppression-extension
        # law): newer ts wins, moment rewritten, exactly one entry.
        entries = dl.delivered(self.tmp, SID, now=now2)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], now2)
        self.assertEqual(entries[0]["moment"], "pretool")

    def test_equal_ts_later_call_wins_older_ts_does_not(self):
        dl.record_undelivered(self.tmp, SID, [{"id": "e2", "content": "c"}],
                              "prompt", now=1000.0, refresh_delivered=True)
        dl.record_undelivered(self.tmp, SID,
                              [{"id": "e2", "content": "c"}],
                              "pretool", now=1000.0, refresh_delivered=True)
        entries = dl.delivered(self.tmp, SID, now=1001.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["moment"], "pretool")
        # An OLDER-ts re-record must not overwrite (the reducer's ts-dominant
        # drift, stated deliberately at #252).
        dl.record_undelivered(self.tmp, SID,
                              [{"id": "e2", "content": "c"}],
                              "subagent", now=999.0, refresh_delivered=True)
        entries = dl.delivered(self.tmp, SID, now=1001.0)
        self.assertEqual(entries[0]["moment"], "pretool")

    def test_coercion_failure_ts_is_kept_as_newer(self):
        dl.record_undelivered(self.tmp, SID, [{"id": "e3", "content": "c"}],
                              "prompt", now=1000.0, refresh_delivered=True)
        dl.record_undelivered(self.tmp, SID,
                              [{"id": "e3", "content": "c", "ts": "corrupt"}],
                              "pretool", now=1000.0, refresh_delivered=True)
        entries = [e for e in dl.delivered(self.tmp, SID, now=1001.0)
                   if e["id"] == "e3"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["moment"], "pretool")

    def test_default_mode_stays_admit_once_without_refresh(self):
        dl.record_undelivered(self.tmp, SID, [_row("e4")], "prompt", now=1000.0)
        again = dl.record_undelivered(self.tmp, SID, [_row("e4")], "pretool",
                                      now=2000.0)
        self.assertEqual(again, [])
        entries = dl.delivered(self.tmp, SID, now=2001.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["moment"], "prompt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
