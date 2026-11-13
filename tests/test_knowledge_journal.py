"""Tests for the knowledge publication intent journal (PR1).

Covers, per the approved plan's validation promises:
  - intent is durable BEFORE create: the entry hits the file with state
    doc_pending and survives process boundaries (fresh instance re-reads)
  - METADATA ONLY: no API accepts note content, and the persisted file
    never contains the note body that publication used (canary scan)
  - identity/digest/locator cross-validation refuses mismatched triples
  - state machine: the happy path doc_pending -> doc_verified ->
    pointer_queued -> applied, quarantined escapes, refused jumps, refused
    backward moves, idempotent same-state marks, unknown states
  - version advance: a NEW intent after a TERMINAL entry (applied /
    quarantined) with a different digest; the same version after terminal
    is idempotent; a different digest while an intent is still ACTIVE is
    an explicit conflict; mark targets the LATEST ACTIVE entry;
    entry_for returns the LATEST entry; history retained, bounded
  - bounded history (max_entries keeps the newest)
  - corrupt file refuses (no silent reset); empty file is empty
  - locking: an OS byte lock (msvcrt/fcntl) held by a REAL second process
    times out bounded with no write; a crashed holder's lock is released
    by the OS (no unlink, no age-based takeover); concurrent in-process
    writers serialize without lost updates

Run: python tests/test_knowledge_journal.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-knfjrn-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.documents import prepare_document  # noqa: E402
from knowledge.journal import (  # noqa: E402
    JournalEntry,
    JournalError,
    KnowledgeJournal,
    STATES,
    default_journal_path,
)

NOTE_BODY = "rotate the staging certs before the Friday deploy window"


def intent_args(target: str, key: str, text: str) -> dict:
    doc = prepare_document(target, key, text)
    return {"target_id": target, "logical_key": doc.logical_key,
            "locator": doc.locator, "digest": doc.digest}


class JournalBasics(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-knfjrn-"))
        self.path = self.dir / "journal.json"
        self.journal = KnowledgeJournal(self.path)

    def test_intent_durable_before_create(self):
        entry = self.journal.record_intent(**intent_args("notes", "run/a",
                                                          NOTE_BODY))
        self.assertEqual(entry.state, "doc_pending")
        self.assertEqual(entry.seq, 1)
        self.assertTrue(self.path.exists())           # durable immediately
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["entries"][0]["state"], "doc_pending")

    def test_survives_process_boundary(self):
        self.journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        fresh = KnowledgeJournal(self.path)
        entries = fresh.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].state, "doc_pending")
        self.assertEqual(entries[0].ref, "knowledge:notes:run/a")

    def test_no_note_body_persisted(self):
        args = intent_args("notes", "run/a", NOTE_BODY)
        self.journal.record_intent(**args)
        self.journal.mark(target_id="notes", logical_key="run/a",
                          state="doc_verified")
        raw = self.path.read_bytes()
        for canary in (NOTE_BODY.encode("utf-8"), b"rotate", b"Friday"):
            self.assertNotIn(canary, raw)
        obj = json.loads(raw.decode("utf-8"))
        allowed = {"seq", "target_id", "logical_key", "ref", "locator",
                   "digest", "state", "updated_at", "namespace"}
        self.assertTrue(set(obj["entries"][0]).issubset(allowed))

    def test_idempotent_same_version(self):
        args = intent_args("notes", "run/a", NOTE_BODY)
        first = self.journal.record_intent(**args)
        again = self.journal.record_intent(**args)
        self.assertEqual(first, again)
        self.assertEqual(len(self.journal.entries()), 1)

    def test_different_digest_same_identity_refused(self):
        self.journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        with self.assertRaises(JournalError) as ctx:
            self.journal.record_intent(
                **intent_args("notes", "run/a", "a different body entirely\n"))
        self.assertIn("ACTIVE", str(ctx.exception))
        # one entry only — the conflicting intent was not recorded
        self.assertEqual(len(self.journal.entries()), 1)

    def test_new_intent_after_applied(self):
        first = intent_args("notes", "run/a", NOTE_BODY)
        self.journal.record_intent(**first)
        for state in ("doc_verified", "pointer_queued", "applied"):
            self.journal.mark(target_id="notes", logical_key="run/a",
                              state=state)
        second = intent_args("notes", "run/a", "second version body\n")
        entry = self.journal.record_intent(**second)
        self.assertEqual(entry.state, "doc_pending")
        self.assertEqual(entry.seq, 2)
        self.assertNotEqual(entry.digest, self.journal.entries()[0].digest)
        # history retained: old applied + new pending
        self.assertEqual([e.state for e in self.journal.entries()],
                         ["applied", "doc_pending"])
        # entry_for returns the LATEST entry
        self.assertEqual(self.journal.entry_for("notes", "run/a").seq, 2)
        # at most one active intent per identity
        pending = self.journal.pending()
        self.assertEqual([e.seq for e in pending], [2])
        # mark advances the LATEST ACTIVE entry
        advanced = self.journal.mark(target_id="notes",
                                     logical_key="run/a",
                                     state="doc_verified")
        self.assertEqual(advanced.seq, 2)
        self.assertEqual(advanced.state, "doc_verified")
        self.assertEqual(self.journal.entries()[0].state, "applied")

    def test_new_intent_after_quarantined(self):
        self.journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        self.journal.mark(target_id="notes", logical_key="run/a",
                          state="quarantined")
        entry = self.journal.record_intent(
            **intent_args("notes", "run/a", "retry after quarantine\n"))
        self.assertEqual(entry.state, "doc_pending")
        self.assertEqual([e.state for e in self.journal.entries()],
                         ["quarantined", "doc_pending"])
        self.assertEqual(self.journal.entry_for("notes", "run/a").seq, 2)

    def test_same_version_after_terminal_is_idempotent(self):
        args = intent_args("notes", "run/a", NOTE_BODY)
        self.journal.record_intent(**args)
        for state in ("doc_verified", "pointer_queued", "applied"):
            self.journal.mark(target_id="notes", logical_key="run/a",
                              state=state)
        again = self.journal.record_intent(**args)
        self.assertEqual(again.state, "applied")
        self.assertEqual(len(self.journal.entries()), 1)

    def test_mark_after_terminal_without_new_intent_refused(self):
        self.journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        self.journal.mark(target_id="notes", logical_key="run/a",
                          state="quarantined")
        with self.assertRaises(JournalError) as ctx:
            self.journal.mark(target_id="notes", logical_key="run/a",
                              state="doc_verified")
        self.assertIn("no ACTIVE entry", str(ctx.exception))

    def test_version_history_is_bounded(self):
        journal = KnowledgeJournal(self.path, max_entries=2)
        for version in range(3):
            journal.record_intent(
                **intent_args("notes", "run/a", "body v%d\n" % version))
            journal.mark(target_id="notes", logical_key="run/a",
                         state="quarantined")   # terminal -> next version
        entries = journal.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual([e.state for e in entries],
                         ["quarantined", "quarantined"])
        # the newest version is the latest surviving entry
        latest = journal.entry_for("notes", "run/a")
        self.assertEqual(latest.seq, 3)
        self.assertEqual(
            latest.digest,
            prepare_document("notes", "run/a", "body v2\n").digest)

    def test_locator_digest_cross_validation(self):
        good = intent_args("notes", "run/a", NOTE_BODY)
        other = intent_args("notes", "run/b", "a distinct second body\n")
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good, "digest": other["digest"]})
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good, "locator": other["locator"]})
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good,
                                          "digest": "0" * 64})
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good, "locator": "run/a/note.md"})

    def test_bad_identity_refused(self):
        good = intent_args("notes", "run/a", NOTE_BODY)
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good, "target_id": "NOTES"})
        with self.assertRaises(JournalError):
            self.journal.record_intent(**{**good, "logical_key": "../escape"})


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-knfjrn-"))
        self.journal = KnowledgeJournal(self.dir / "journal.json")
        self.args = intent_args("notes", "run/a", NOTE_BODY)
        self.journal.record_intent(**self.args)

    def mark(self, state: str) -> JournalEntry:
        return self.journal.mark(target_id="notes", logical_key="run/a",
                                 state=state)

    def test_happy_path(self):
        for state in ("doc_verified", "pointer_queued", "applied"):
            entry = self.mark(state)
            self.assertEqual(entry.state, state)
        self.assertEqual(self.journal.pending(), ())

    def test_same_state_mark_idempotent_and_terminal_refused(self):
        first = self.mark("doc_verified")
        again = self.mark("doc_verified")
        self.assertEqual(first, again)
        self.assertEqual(len(self.journal.entries()), 1)
        self.mark("pointer_queued")
        self.mark("applied")
        with self.assertRaises(JournalError):
            self.mark("applied")   # terminal: no active entry to mark

    def test_quarantine_escape_from_each_nonterminal_state(self):
        for state in ("doc-pending", "doc-verified", "pointer-queued"):
            fresh = KnowledgeJournal(self.dir / ("journal-%s.json" % state))
            args = intent_args("notes", "run/%s" % state, NOTE_BODY)
            fresh.record_intent(**args)
            walk = {"doc-pending": (),
                    "doc-verified": ("doc_verified",),
                    "pointer-queued": ("doc_verified", "pointer_queued")}[state]
            for step in walk:
                fresh.mark(target_id="notes",
                           logical_key="run/%s" % state, state=step)
            entry = fresh.mark(target_id="notes",
                               logical_key="run/%s" % state,
                               state="quarantined")
            self.assertEqual(entry.state, "quarantined")
            self.assertEqual(fresh.pending(), ())

    def test_refused_jumps(self):
        for bad in ("pointer_queued", "applied"):
            with self.assertRaises(JournalError, msg=bad):
                self.mark(bad)

    def test_refused_backward_and_terminal_moves(self):
        self.mark("doc_verified")
        with self.assertRaises(JournalError):
            self.mark("doc_pending")
        self.mark("pointer_queued")
        self.mark("applied")
        for bad in ("doc_pending", "doc_verified", "pointer_queued",
                    "quarantined"):
            with self.assertRaises(JournalError, msg=bad):
                self.mark(bad)

    def test_unknown_state_and_missing_identity(self):
        with self.assertRaises(JournalError):
            self.mark("doc_flown")
        with self.assertRaises(JournalError):
            self.journal.mark(target_id="notes", logical_key="run/absent",
                              state="doc_verified")

    def test_pending_only_lists_nonterminal(self):
        self.assertEqual(len(self.journal.pending()), 1)
        self.mark("doc_verified")
        self.assertEqual(len(self.journal.pending()), 1)
        self.mark("pointer_queued")
        self.mark("applied")
        self.assertEqual(self.journal.pending(), ())


class RobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-knfjrn-"))
        self.path = self.dir / "journal.json"

    def test_empty_file_is_empty_journal(self):
        self.path.write_bytes(b"")
        journal = KnowledgeJournal(self.path)
        self.assertEqual(journal.entries(), ())

    def test_corrupt_file_refuses(self):
        self.path.write_text("{oops", encoding="utf-8")
        journal = KnowledgeJournal(self.path)
        with self.assertRaises(JournalError):
            journal.entries()

    def test_unknown_version_refuses(self):
        self.path.write_text(json.dumps({"version": 99, "entries": []}),
                             encoding="utf-8")
        with self.assertRaises(JournalError):
            KnowledgeJournal(self.path).entries()

    def test_multi_entry_history_loads(self):
        # Version history per identity is legal file shape (one entry per
        # published version); a forged same-identity pair loads in seq
        # order and entry_for reports the higher-seq one.
        entry = intent_args("notes", "run/a", NOTE_BODY)
        KnowledgeJournal(self.path).record_intent(**entry)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        second = dict(raw["entries"][0])
        second["seq"] = 2
        second["state"] = "applied"
        raw["entries"].append(second)
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        entries = KnowledgeJournal(self.path).entries()
        self.assertEqual([e.seq for e in entries], [1, 2])
        self.assertEqual(
            KnowledgeJournal(self.path).entry_for("notes", "run/a").seq, 2)

    def test_bounded_history(self):
        journal = KnowledgeJournal(self.path, max_entries=3)
        for index in range(5):
            journal.record_intent(
                **intent_args("notes", "run/doc-%d" % index, NOTE_BODY))
        entries = journal.entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.logical_key for e in entries],
                         ["run/doc-2", "run/doc-3", "run/doc-4"])

    def test_no_stray_temp_files(self):
        journal = KnowledgeJournal(self.path)
        journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        journal.mark(target_id="notes", logical_key="run/a",
                     state="doc_verified")
        leftovers = [p.name for p in self.dir.iterdir()
                     if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])


class LockTests(unittest.TestCase):
    """Locking is an OS byte lock (msvcrt/fcntl), non-blocking with a
    bounded retry.  A live lock is NEVER deleted or taken over by age;
    the kernel releases it when the holder exits — including a crash."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="zmem-knfjrn-"))
        self.path = self.dir / "journal.json"

    def _hold_lock_subprocess(self, hold_s: float):
        """Start a REAL second process that holds the journal lock for
        ``hold_s`` seconds; returns the Popen once it prints LOCKED."""
        code = (
            "import sys, time\n"
            "from knowledge.journal import KnowledgeJournal\n"
            "journal = KnowledgeJournal(sys.argv[1], lock_timeout_s=20.0)\n"
            "with journal._locked():\n"
            "    print('LOCKED', flush=True)\n"
            "    time.sleep(%r)\n" % hold_s
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
        return proc

    def test_other_process_lock_times_out_bounded(self):
        proc = self._hold_lock_subprocess(hold_s=1.5)
        try:
            journal = KnowledgeJournal(self.path, lock_timeout_s=0.4)
            start = time.monotonic()
            with self.assertRaises(JournalError) as ctx:
                journal.record_intent(
                    **intent_args("notes", "run/a", NOTE_BODY))
            self.assertLess(time.monotonic() - start, 5.0)
            self.assertIn("another writer", str(ctx.exception))
            # nothing was written while the lock was held elsewhere
            self.assertFalse(self.path.exists())
        finally:
            proc.wait(timeout=15)
        # after the other process exits the lock is free: the SAME write
        # succeeds without deleting or recreating anything.
        entry = KnowledgeJournal(self.path, lock_timeout_s=10.0).record_intent(
            **intent_args("notes", "run/a", NOTE_BODY))
        self.assertEqual(entry.state, "doc_pending")

    def test_crashed_holder_lock_released_by_os(self):
        proc = self._hold_lock_subprocess(hold_s=30.0)   # would hold long
        journal = KnowledgeJournal(self.path, lock_timeout_s=0.4)
        with self.assertRaises(JournalError):
            journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        proc.kill()                                       # simulated crash
        proc.wait(timeout=15)
        # The kernel dropped the dead process's lock; we never touched the
        # lock file itself (no unlink, no age heuristic).
        entry = KnowledgeJournal(self.path, lock_timeout_s=10.0).record_intent(
            **intent_args("notes", "run/a", NOTE_BODY))
        self.assertEqual(entry.state, "doc_pending")

    def test_lock_reusable_across_operations(self):
        journal = KnowledgeJournal(self.path)
        journal.record_intent(**intent_args("notes", "run/a", NOTE_BODY))
        journal.mark(target_id="notes", logical_key="run/a",
                     state="doc_verified")
        # The persistent lock file may remain (it is never unlinked); what
        # matters is that a second writer/process can lock it right away.
        entry = KnowledgeJournal(self.path, lock_timeout_s=5.0).record_intent(
            **intent_args("notes", "run/b", NOTE_BODY))
        self.assertEqual(entry.seq, 2)

    def test_concurrent_writers_serialize(self):
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                other = KnowledgeJournal(self.path, lock_timeout_s=20.0)
                other.record_intent(
                    **intent_args("notes", "run/w-%d" % index, NOTE_BODY))
            except Exception as exc:  # noqa: BLE001 - recorded for assert
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        entries = KnowledgeJournal(self.path).entries()
        self.assertEqual(len(entries), 6)
        seqs = [e.seq for e in entries]
        self.assertEqual(seqs, sorted(seqs))
        identities = {(e.target_id, e.logical_key) for e in entries}
        self.assertEqual(len(identities), 6)


class DefaultPathTests(unittest.TestCase):
    def test_default_path_beside_store(self):
        # The contract: the journal lives under <store_dir>/knowledge/,
        # derived from the SAME store-dir chain as the config file
        # (import-order tolerant: compare against config_path itself).
        from storelib.config import config_path

        path = default_journal_path()
        self.assertEqual(path.name, "journal.json")
        self.assertEqual(path.parent.name, "knowledge")
        self.assertEqual(path.parent.parent, config_path().parent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
