"""Tests for the shared knowledge sidecar byte-lock discipline (REL-015).

Covers the contention-vs-fatal contract of
``knowledge.locking.try_lock_nb``:

  - CONTENTION (another writer holds the byte lock) still reports
    ``False`` — the callers' bounded retry loops keep their behavior,
    and the rekey fence keeps its busy refusal path
  - FATAL OSError (a descriptor that cannot be locked at all — e.g. a
    closed fd) raises the typed, content-free ``LockError`` instead of
    masquerading as busy: the old blanket ``except OSError: return
    False`` let fatal conditions spin the retry loops / refuse with the
    wrong reason
  - the happy path is unchanged (lock, contend, unlock, re-lock), and a
    fatal error propagates typed through the journal/delivery lock
    acquisition (never as a busy-shaped refusal)

Filesystem work happens in temp dirs; no store, no network.

Run: python tests/test_knowledge_locking.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-knlock-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.delivery import DeliveryError, KnowledgeDelivery  # noqa: E402
from knowledge.journal import JournalError, KnowledgeJournal  # noqa: E402
from knowledge.locking import LockError, try_lock_nb, unlock  # noqa: E402


class LockDisciplineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-knlock-case-"))
        self.lock = self.dir / "sidecar.json.lock"

    def _open(self) -> int:
        return os.open(str(self.lock), os.O_CREAT | os.O_RDWR, 0o600)

    def test_contention_reports_false_for_the_retry_loops(self):
        first = self._open()
        try:
            self.assertTrue(try_lock_nb(first))
            second = self._open()
            try:
                self.assertFalse(
                    try_lock_nb(second),
                    "a held byte lock is CONTENTION: False, never an "
                    "error, so the bounded retry loops keep spinning")
            finally:
                os.close(second)
        finally:
            unlock(first)
            os.close(first)
        # Released: the next attempt succeeds (the happy path stands).
        third = self._open()
        try:
            self.assertTrue(try_lock_nb(third))
        finally:
            unlock(third)
            os.close(third)

    def test_fatal_lock_failure_raises_typed_not_busy(self):
        # A closed descriptor cannot be locked at all: that is FATAL, not
        # contention — the typed LockError replaces the old busy-False.
        fd = self._open()
        os.close(fd)
        with self.assertRaises(LockError) as ctx:
            try_lock_nb(fd)
        self.assertIn("byte lock failed", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, OSError)

    def test_journal_and_delivery_locks_still_work(self):
        # First-use parent creation and the normal acquire cycle are
        # unchanged: a DIRECT constructor on a fresh nested path locks,
        # and a second handle on the same lock file contends (False).
        nested = self.dir / "deep" / "nested" / "journal.json"
        journal = KnowledgeJournal(nested)
        with journal._locked():
            fd = self._open_of(nested.with_name("journal.json.lock"))
            try:
                self.assertFalse(try_lock_nb(fd))
            finally:
                os.close(fd)
        delivery = KnowledgeDelivery(self.dir / "deep" / "delivery.json")
        with delivery._locked():
            fd = self._open_of(self.dir / "deep" / "delivery.json.lock")
            try:
                self.assertFalse(try_lock_nb(fd))
            finally:
                os.close(fd)

    def _open_of(self, path: Path) -> int:
        return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)

    def test_fatal_error_propagates_typed_through_the_callers(self):
        # A fatal LockError inside a caller's retry loop surfaces as the
        # TYPED error (never a busy-shaped DeliveryError/JournalError,
        # never a silent False) — patched at the journal/delivery module
        # bindings the loops actually call.
        from knowledge import delivery as delivery_mod
        from knowledge import journal as journal_mod

        journal = KnowledgeJournal(self.dir / "j.json")
        with mock.patch.object(journal_mod, "_try_lock_nb",
                               side_effect=LockError("boom")):
            with self.assertRaises(LockError):
                journal.record_intent(target_id="notes", logical_key="k",
                                      locator="k/%s.md" % "-".join(["a" * 16] * 4),
                                      digest="a" * 64)
        delivery = KnowledgeDelivery(self.dir / "d.json")
        with mock.patch.object(delivery_mod, "_try_lock_nb",
                               side_effect=LockError("boom")):
            with self.assertRaises(LockError):
                delivery.set_intent(ref="knowledge:notes:run/k",
                                    fields={"state": "staked"})
        # The busy refusal path (contention past the deadline) is
        # untouched: a always-False lock still refuses as BUSY.
        always_false = lambda fd: False  # noqa: E731
        with mock.patch.object(delivery_mod, "_try_lock_nb", always_false):
            with self.assertRaises(DeliveryError) as ctx:
                delivery.set_intent(ref="knowledge:notes:run/k2",
                                    fields={"state": "staked"})
        self.assertIn("busy", str(ctx.exception))
        quick = KnowledgeJournal(self.dir / "j2.json", lock_timeout_s=0.05)
        with mock.patch.object(journal_mod, "_try_lock_nb", always_false):
            with self.assertRaises(JournalError) as ctx:
                quick.record_intent(target_id="notes", logical_key="k",
                                    locator="k/%s.md" % "-".join(["a" * 16] * 4), digest="a" * 64)
        self.assertIn("busy", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
