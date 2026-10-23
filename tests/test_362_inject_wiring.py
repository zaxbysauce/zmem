"""Supplementary (unfrozen) pin: the non-reserve selector records through
the admission API, not lock-free record() (issue #362 remainder, 0.118.0).

The frozen C16 pins the ledger-layer admission contract; this file pins the
ROUTING: after 0.118.0, select_and_budget_for_injection's non-reserve branch
must reach the ledger through record_undelivered — if it ever regresses to
plain record(), the row still records but without admission accounting, and
this test fails both ways (record() is sabotaged to raise, and the delivered
row must still land in the ledger through the admission path).

Run standalone: python tests/test_362_inject_wiring.py
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# storelib freezes STORE_PATH at FIRST import — pin the sandbox BEFORE any
# storelib import (the module-level import below is the first one in this
# process; CI runs each file standalone).
_WIRING_TMP = tempfile.mkdtemp(prefix="zmem-362-wiring-")
atexit.register(shutil.rmtree, _WIRING_TMP, True)
os.environ["ZMEM_STORE"] = os.path.join(_WIRING_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _WIRING_TMP
for _k in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
           "ZMEM_RESERVATION_TTL_S", "ZMEM_DELIVERY_LOCK_GRACE_S",
           "ZMEM_TEST_NOW"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "memory", "scripts"))

from storelib import delivery_ledger as dl  # noqa: E402
from storelib import inject as inject_mod  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402


class NonReserveRoutingTest(unittest.TestCase):
    def setUp(self):
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            conn.execute(
                "INSERT OR IGNORE INTO memory (id, namespace, type, content, "
                "confidence, signal, source_ref, ingestion_ts) VALUES "
                "('wiring-1', 'test', 'fact', 'routing probe fixture row', "
                "0.9, 'test', 'session:wiring', '2026-01-01T00:00:00Z')")
            conn.commit()
        finally:
            conn.close()
        dl.clear_delivery_state(_WIRING_TMP, "wiring-session")

    def tearDown(self):
        dl.end_delivery_state(_WIRING_TMP, "wiring-session")

    def test_nonreserve_selector_records_through_admission_api(self):
        conn = schema_mod.connect()
        try:
            # Sabotage the legacy lock-free writer: after 0.118.0 the
            # non-reserve branch must not reach it. A regression to record()
            # would raise here (swallowed by the selector's fail-open
            # boundary) AND leave the ledger empty — both caught below.
            # Two proofs: (a) record() must not be the writer (sabotage);
            # (b) the ADMISSION API must be the writer AND carry
            # refresh_delivered=True from the selector - a kwarg-drop mutant
            # at inject.py's call site fails this spy (review round 2,
            # PRR-016/ti-04).
            spy = mock.Mock(wraps=dl.record_undelivered)
            with mock.patch.object(dl, "record",
                                   side_effect=AssertionError(
                                       "non-reserve path must not use "
                                       "record()")),                     mock.patch.object(dl, "record_undelivered", spy):
                result = inject_mod.select_and_budget_for_injection(
                    conn,
                    query="routing probe fixture",
                    namespace="test",
                    moment="user_prompt",
                    session_id="wiring-session",
                    budget_tokens=2000,
                    reserve=False,
                    data_dir=_WIRING_TMP,
                )
            spy.assert_called_once()
            self.assertTrue(spy.call_args.kwargs.get("refresh_delivered"),
                            "the selector must route refresh_delivered=True "
                            "through the admission API")
        finally:
            conn.close()
        self.assertIsNotNone(result)
        ids = dl.delivered_ids(_WIRING_TMP, "wiring-session")
        self.assertIn("wiring-1", ids,
                      "the delivered row must land in the ledger through the "
                      "admission API when record() is sabotaged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
