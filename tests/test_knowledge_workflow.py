"""Knowledge workflow tests (PR1, plan 2026-11-13).

Covers the capture pipeline end to end against a REAL temp store and a
REAL markdown vault (the same admission/target/journal/CAS pieces the CLI
subprocess drives), per the approved plan's validation promises:

  - capture order: screen FIRST (a secret-like body rejects the
    publication BEFORE any journal entry, file, or pointer row exists);
    journal intent BEFORE create; predecessor observed BEFORE create
  - local capture: created -> read -> check(digest match) -> idempotent
    recapture (noop) with the journal reaching `applied`
  - changed-version publication uses the CORRECTED journal verbatim — no
    workflow-side bypass (coordinator direction): a journal that still
    refuses a second digest for one identity makes capture fail honestly
    BEFORE the create; a corrected journal (probe-detected) advances the
    pointer to v2
  - crash recovery: resume from doc_pending / doc_verified /
    pointer_queued by re-running capture with the same screened input
  - remote-mode law: an old/unreachable hub means an explicit pending
    pointer, NO enqueue, and NEVER a local store created
  - slash-segment logical keys and digit-leading target ids (the
    knowledge.documents grammar) round-trip through the reserved ref
  - the CLI subprocess contract: --json stdout is PURE JSON (one line),
    read prints the content, check exits by verdict

Run: python tests/test_knowledge_workflow.py
"""

from __future__ import annotations

import os
import tempfile

# Scratch store BEFORE the isolation guard import (the guard resolves the
# store env chain at import time; this file drives real store writes).
_TMP = tempfile.mkdtemp(prefix="zmem-knwf-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import json
import sqlite3
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.config import load_knowledge_config  # noqa: E402
from knowledge.delivery import KnowledgeDelivery  # noqa: E402
from knowledge.documents import prepare_document  # noqa: E402
from knowledge.journal import JournalError, KnowledgeJournal  # noqa: E402
from knowledge import workflow  # noqa: E402
from storelib import knowledge as store_knowledge  # noqa: E402

NOTE_V1 = ("Pipefail breaks grep pipelines: a no-match grep under "
           "set -o pipefail kills the stage.\n")
NOTE_V2 = NOTE_V1 + "\nUpdated: quote '|| true' consciously when grepping.\n"
SECRET_NOTE = "deploy token: abcdef0123456789abcdef0123456789abcdef01\n"


def _init_store(tmp: str) -> str:
    """Create + migrate a scratch store through the real CLI (the same
    lifecycle every other subprocess test uses)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, str(STORE_PY), "init"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    return os.path.join(tmp, "store.sqlite")


def _open_rw(store_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(store_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class _Env:
    """One scratch data dir + vault + config + journal + delivery."""

    def __init__(self, *, target_id: str = "notes") -> None:
        self.tmp = tempfile.mkdtemp(prefix="zmem-knwf-env-")
        self.vault = Path(self.tmp) / "vault"
        self.vault.mkdir()
        self.target_id = target_id
        self.config_path = Path(self.tmp) / "config.json"
        self.config_path.write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                target_id: {"kind": "markdown", "root": str(self.vault)},
            }},
        }), encoding="utf-8")
        self.store_path = _init_store(self.tmp)
        # The sidecar dir exists in any real data dir; the journal's lock
        # opens before its first write would mkdir the parent (owner's
        # OSlock fix pending), so the fixture creates it up front.
        (Path(self.tmp) / "knowledge").mkdir(exist_ok=True)
        self.journal = KnowledgeJournal(
            Path(self.tmp) / "knowledge" / "journal.json")
        self.delivery = KnowledgeDelivery(
            Path(self.tmp) / "knowledge" / "delivery.json")

    def config(self):
        return load_knowledge_config(self.config_path)


class _UnsupportedHub:
    """A reachable hub WITHOUT the knowledge capability (an old hub)."""

    def hello(self):
        return {"protocol": 1, "capabilities": ["apply-ops"],
                "zmem_version": "0.152.0"}


class _UnreachableHub:
    def hello(self):
        return {"ok": False, "reason": "hub-unreachable"}


def _journal_supports_progression(tmp: str) -> bool:
    """Probe whether the journal allows a SECOND digest for an identity
    whose first attempt reached a terminal state (the corrected journal).
    Until that lands these cases SKIP with the blocker named — they
    activate automatically when the correction lands."""
    journal = KnowledgeJournal(
        Path(tmp) / "probe-journal.json")
    one = prepare_document("notes", "probe", "first body\n")
    two = prepare_document("notes", "probe", "second body\n")
    journal.record_intent(target_id="notes", logical_key="probe",
                          locator=one.locator, digest=one.digest)
    journal.mark(target_id="notes", logical_key="probe", state="doc_verified")
    journal.mark(target_id="notes", logical_key="probe",
                 state="pointer_queued")
    journal.mark(target_id="notes", logical_key="probe", state="applied")
    try:
        journal.record_intent(target_id="notes", logical_key="probe",
                              locator=two.locator, digest=two.digest)
    except JournalError:
        return False
    return True


class LocalCaptureTests(unittest.TestCase):
    def test_capture_read_check_idempotent(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            first = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(first["ok"])
            self.assertEqual(first["pointer"]["status"], "created")
            self.assertEqual(first["version"], 1)
            self.assertEqual(first["journal_state"], "applied")
            # The vault carries the canonical bytes at the derived locator.
            note_file = env.vault.joinpath(*first["locator"].split("/"))
            self.assertTrue(note_file.is_file())
            self.assertEqual(note_file.read_bytes(),
                             (NOTE_V1.rstrip("\n") + "\n").encode())
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertIsNotNone(pointer)
            self.assertEqual(pointer.full_sha256, first["digest"])
            # read round-trips through the target.
            read = workflow.read(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertEqual(read["content"],
                             NOTE_V1.rstrip("\n") + "\n")
            self.assertEqual(read["provenance"], "pointer")
            # check verifies the readback digest.
            check = workflow.check(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(check["matches"])
            # Idempotent recapture: same screened input -> noop everywhere.
            again = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(again["ok"])
            self.assertEqual(again["pointer"]["status"], "noop")
            self.assertTrue(again["existed_match"])
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)
        finally:
            conn.close()

    def test_screening_rejects_before_any_write(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            with self.assertRaises(workflow.WorkflowError) as ctx:
                workflow.capture(
                    target_id="notes", logical_key="run-notes",
                    text=SECRET_NOTE, mode="local", conn=conn,
                    config=env.config(), journal=env.journal,
                    delivery=env.delivery)
            self.assertIn("secret-like", str(ctx.exception))
            self.assertIsNone(env.journal.entry_for(
                "notes", "run-notes", namespace="user:global"))
            files = list(env.vault.rglob("*.md"))
            self.assertEqual(files, [])
            n = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%'").fetchone()
            self.assertEqual(n["c"], 0)
        finally:
            conn.close()

    def test_disabled_and_unknown_target(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            with self.assertRaises(workflow.WorkflowError):
                workflow.capture(
                    target_id="ghost", logical_key="k", text="x\n",
                    mode="local", conn=conn, config=env.config(),
                    journal=env.journal, delivery=env.delivery)
            disabled = load_knowledge_config(None)  # no knowledge object
            self.assertFalse(disabled.enabled)
            with self.assertRaises(workflow.WorkflowError):
                workflow.capture(
                    target_id="notes", logical_key="k", text="x\n",
                    mode="local", conn=conn, config=disabled,
                    journal=env.journal, delivery=env.delivery)
        finally:
            conn.close()

    def test_documents_grammar_round_trip(self):
        """Slash-segment keys and digit-leading target ids (the
        knowledge.documents grammar the reserved ref reuses)."""
        env = _Env(target_id="0notes")
        conn = _open_rw(env.store_path)
        try:
            result = workflow.capture(
                target_id="0notes", logical_key="projects/deep/run-7",
                text="grammar note\n", mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(result["ok"])
            self.assertEqual(
                result["ref"], "knowledge:0notes:projects/deep/run-7")
            read = workflow.read(
                target_id="0notes", logical_key="projects/deep/run-7",
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertEqual(read["content"], "grammar note\n")
        finally:
            conn.close()

    def test_changed_version_uses_corrected_journal_only(self):
        """Changed-digest publication goes through the journal verbatim —
        no workflow bypass. Old journal: honest refusal BEFORE the create
        (nothing published). Corrected journal (probe-detected): the
        pointer advances to v2."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            first = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(first["ok"])
            if not _journal_supports_progression(env.tmp):
                # Blocker (reported): the shipped journal refuses a second
                # digest for one identity forever. The workflow must fail
                # HONESTLY before the create — never bypass to a sidecar.
                with self.assertRaises(workflow.WorkflowError) as ctx:
                    workflow.capture(
                        target_id="notes", logical_key="run-notes",
                        text=NOTE_V2, mode="local", conn=conn,
                        config=env.config(), journal=env.journal,
                        delivery=env.delivery)
                self.assertIn("corrected multi-version journal",
                              str(ctx.exception))
                # Nothing was published for v2 (refusal preceded create):
                # exactly the v1 note exists in the vault.
                self.assertEqual(len(list(env.vault.rglob("*.md"))), 1)
                return
            second = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V2,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(second["ok"])
            self.assertEqual(second["pointer"]["status"], "advanced")
            self.assertEqual(second["version"], 2)
            read = workflow.read(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertEqual(read["content"],
                             NOTE_V2.rstrip("\n") + "\n")
        finally:
            conn.close()


class CrashRecoveryTests(unittest.TestCase):
    def _stopped_at(self, env, conn, state):
        """Publish NOTE_V1's intent/create up to (not including) `state`'s
        successor work — the crash point — by driving the pieces the
        workflow itself uses."""
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        env.journal.record_intent(target_id="notes",
                                  logical_key="run-notes",
                                  locator=doc.locator, digest=doc.digest,
                                  namespace="user:global")
        if state in ("doc_verified", "pointer_queued"):
            from knowledge.documents import open_target
            open_target(env.config().targets["notes"]).create(doc)
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state="doc_verified", namespace="user:global")
        if state == "pointer_queued":
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state="pointer_queued",
                             namespace="user:global")
        return doc

    def test_resume_from_each_pending_state(self):
        for state in ("doc_pending", "doc_verified", "pointer_queued"):
            with self.subTest(state=state):
                env = _Env()
                conn = _open_rw(env.store_path)
                try:
                    doc = self._stopped_at(env, conn, state)
                    resumed = workflow.capture(
                        target_id="notes", logical_key="run-notes",
                        text=NOTE_V1, mode="local", conn=conn,
                        config=env.config(), journal=env.journal,
                        delivery=env.delivery)
                    self.assertTrue(resumed["ok"], resumed)
                    self.assertIn(resumed["pointer"]["status"],
                                  ("created", "noop"))
                    self.assertEqual(resumed["digest"], doc.digest)
                    entry = env.journal.entry_for("notes", "run-notes",
                                                  namespace="user:global")
                    self.assertEqual(entry.state, "applied")
                finally:
                    conn.close()

    def test_crash_after_link_before_applied(self):
        """Pointer landed but the verdict never reached the records —
        with the REALISTIC full staked intent (the probe's crash window):
        every retry reconciles (noop + applied); the stale stake is never
        replayed against the pointer its own attempt already moved."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            doc = prepare_document("notes", "run-notes", NOTE_V1)
            env.journal.record_intent(target_id="notes",
                                      logical_key="run-notes",
                                      locator=doc.locator,
                                      digest=doc.digest,
                                      namespace="user:global")
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state="doc_verified", namespace="user:global")
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state="pointer_queued",
                             namespace="user:global")
            env.delivery.set_intent(
                ref="knowledge:notes:run-notes", namespace="user:global",
                fields={"target": "notes", "key": "run-notes",
                        "namespace": "user:global", "digest": doc.digest,
                        "locator": doc.locator, "version": 1,
                        "expected_row_id": None, "row_id": None,
                        "state": "staked", "op_id": None, "staked": True})
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=1, full_sha256=doc.digest,
                summary="s", locator=doc.locator, expected_row_id=None)
            for attempt in (1, 2):
                resumed = workflow.capture(
                    target_id="notes", logical_key="run-notes",
                    text=NOTE_V1, mode="local", conn=conn,
                    config=env.config(), journal=env.journal,
                    delivery=env.delivery)
                self.assertTrue(resumed["ok"], "retry %d: %r"
                                % (attempt, resumed))
                self.assertEqual(resumed["pointer"]["status"], "noop")
                self.assertEqual(resumed["journal_state"], "applied")
            self.assertEqual(
                env.journal.entry_for("notes", "run-notes",
                                      namespace="user:global").state,
                "applied")
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)  # exactly one live pointer row
        finally:
            conn.close()


class LocalCasTests(unittest.TestCase):
    """The pointer CAS laws, driven through the workflow's own handler."""

    def test_initial_create_refused_when_pointer_exists(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            doc = prepare_document("notes", "run-notes", NOTE_V1)
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=1, full_sha256=doc.digest,
                summary="s", locator=doc.locator, expected_row_id=None)
            # A workflow first-create with a DIFFERENT key digest staked
            # against the existing pointer must surface cas_stale, not
            # silently win. (Same identity, same digest would be noop.)
            with self.assertRaises(
                    store_knowledge.KnowledgePointerCasError):
                store_knowledge.link_pointer(
                    conn, namespace="user:global", target="notes",
                    key="run-notes", version=2,
                    full_sha256=prepare_document(
                        "notes", "run-notes", NOTE_V2).digest,
                    summary="s2", expected_row_id=None)
        finally:
            conn.close()

    def test_lost_cas_race_only_one_advance_wins(self):
        """Two writers stake the SAME expected row: exactly one advance
        lands; the loser gets a stale CAS with nothing written."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            v1 = prepare_document("notes", "run-notes", NOTE_V1)
            created = store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=1, full_sha256=v1.digest,
                summary="s", locator=v1.locator, expected_row_id=None)
            self.assertEqual(created["status"], "created")
            stake = created["row_id"]
            a = prepare_document("notes", "run-notes",
                                 "advance A body\n")
            b = prepare_document("notes", "run-notes",
                                 "advance B body\n")
            first = store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=2, full_sha256=a.digest,
                summary="a", locator=a.locator, expected_row_id=stake)
            self.assertEqual(first["status"], "advanced")
            with self.assertRaises(
                    store_knowledge.KnowledgePointerCasError):
                store_knowledge.link_pointer(
                    conn, namespace="user:global", target="notes",
                    key="run-notes", version=3, full_sha256=b.digest,
                    summary="b", locator=b.locator,
                    expected_row_id=stake)
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, a.digest)
        finally:
            conn.close()


class RemoteModeLawTests(unittest.TestCase):
    def test_old_hub_no_enqueue_no_local_store(self):
        env = _Env()
        result = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=_UnsupportedHub())
        # Honesty law: the version IS published, but the WORKFLOW is not
        # complete — ok/completed stay false for a pending pointer.
        self.assertFalse(result["ok"])
        self.assertFalse(result["completed"])
        self.assertTrue(result["doc_published"])
        self.assertEqual(result["pointer"]["status"], "pending")
        self.assertEqual(result["pointer"]["reason"], "hub-unsupported")
        self.assertFalse(result["pointer"]["enqueued"])
        # Capability BEFORE queue: nothing was enqueued.
        self.assertEqual(env.delivery.pending(), [])
        # The target-side publication happened (immutable version exists).
        self.assertTrue(list(env.vault.rglob("*.md")))
        # Journal holds the in-flight attempt at doc_verified.
        self.assertEqual(
            env.journal.entry_for("notes", "run-notes",
                                  namespace="user:global").state,
            "doc_verified")
        # MODE LAW: no local store file was created by the remote capture.
        # (env.store_path exists because _Env initialized it for OTHER
        # tests' isolation; remote capture itself never opened it — pinned
        # by the CLI test below against a clean data dir.)

    def test_unreachable_hub_pending(self):
        env = _Env()
        result = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=_UnreachableHub())
        self.assertFalse(result["ok"])
        self.assertFalse(result["completed"])
        self.assertTrue(result["doc_published"])
        self.assertEqual(result["pointer"]["status"], "pending")
        self.assertEqual(result["pointer"]["reason"], "hub-unreachable")
        self.assertEqual(env.delivery.pending(), [])


class CliContractTests(unittest.TestCase):
    """The real store.py subprocess against a scratch data dir + vault."""

    def _run(self, tmp: str, *argv: str, stdin: str | None = None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
            "ZMEM_DATA": tmp,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        return subprocess.run(
            [sys.executable, str(STORE_PY), "knowledge", *argv],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=180,
            input=stdin)

    def test_end_to_end_cli(self):
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-cli-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "init"],
            capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items()
                    if not k.startswith("ZMEM_")},
                 "ZMEM_HOME": str(REPO_ROOT),
                 "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                 "ZMEM_DATA": tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                 "PYTHONIOENCODING": "utf-8"},
            cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

        cap = self._run(tmp, "capture", "--target", "notes",
                        "--key", "run-notes", "--content", NOTE_V1.strip(),
                        "--json")
        self.assertEqual(cap.returncode, 0, cap.stderr[-400:])
        lines = [ln for ln in cap.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1,
                         "capture --json stdout must be exactly one JSON "
                         "line (human progress goes to stderr): %r"
                         % cap.stdout)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pointer"]["status"], "created")

        read = self._run(tmp, "read", "--target", "notes",
                         "--key", "run-notes")
        self.assertEqual(read.returncode, 0, read.stderr[-400:])
        self.assertEqual(read.stdout, NOTE_V1.rstrip("\n") + "\n")

        check = self._run(tmp, "check", "--target", "notes",
                          "--key", "run-notes", "--json")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertTrue(json.loads(check.stdout)["matches"])

        again = self._run(tmp, "capture", "--target", "notes",
                          "--key", "run-notes", "--content", NOTE_V1.strip(),
                          "--json")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(
            again.stdout.splitlines()[0])["pointer"]["status"], "noop")

        status = self._run(tmp, "status", "--json")
        self.assertEqual(status.returncode, 0)
        doc = json.loads(status.stdout)
        self.assertEqual(doc["mode"], "local")
        self.assertEqual(doc["journal"][0]["state"], "applied")
        self.assertEqual(doc["pointers"][0]["pointer"]["version"], 1)
        # The hub's knowledge_pointer tool wraps exactly this surface
        # (status --target --key --json): the CURRENT pointer row carries
        # the FULL digest, unredacted, so a remote stake/read can compare.
        scoped = self._run(tmp, "status", "--target", "notes",
                           "--key", "run-notes", "--json")
        self.assertEqual(scoped.returncode, 0)
        row = json.loads(scoped.stdout)["pointers"][0]
        self.assertEqual((row["target"], row["key"]), ("notes", "run-notes"))
        self.assertEqual(row["pointer"]["full_sha256"], payload["digest"])
        self.assertEqual(row["pointer"]["locator"], payload["locator"])

    def test_remote_mode_never_creates_local_store(self):
        """In remote mode (ZMEM_REMOTE_URL) the knowledge command runs as
        a helper sidecar and must not create/open store.sqlite."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-rmt-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_DATA": tmp,  # data dir only: sidecars live here
            "ZMEM_REMOTE_URL": "http://127.0.0.1:9/",  # nothing listens
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "knowledge", "capture",
             "--target", "notes", "--key", "run-notes",
             "--content", "remote note body", "--json"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=180)
        # The version IS published, but the pointer is pending: the CLI
        # maps that to the DISTINCT exit 3 (not success, not failure=1).
        self.assertEqual(proc.returncode, 3,
                         "remote capture with a pending pointer must exit "
                         "3: %r %r" % (proc.stdout[-300:],
                                       proc.stderr[-300:]))
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["completed"])
        self.assertTrue(payload["doc_published"])
        self.assertEqual(payload["pointer"]["status"], "pending")
        self.assertEqual(payload["pointer"]["reason"], "hub-unreachable")
        # PENDING advice: nothing was enqueued, so 'knowledge flush' is
        # NOT advised (re-capture with the same screened input is).
        self.assertNotIn("knowledge flush", proc.stderr)
        self.assertNotIn("knowledge flush", proc.stdout)
        self.assertFalse(
            (Path(tmp) / "store.sqlite").exists(),
            "remote mode must never create a local store")
        # The immutable version WAS published to the vault target.
        self.assertTrue(list(vault.rglob("*.md")))
        # No core op queue was created for the knowledge capture.
        self.assertFalse((Path(tmp) / "ops.jsonl").exists())

    def test_cli_json_failure_is_json(self):
        """Under --json even a hard failure prints ONE JSON error object
        to stdout (an unknown target), never an empty stdout."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-jerr-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        proc = self._run(tmp, "capture", "--target", "ghost", "--key", "k",
                         "--content", "x", "--json")
        self.assertEqual(proc.returncode, 2)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1,
                         "the JSON error contract is one stdout line: %r"
                         % proc.stdout)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertIn("ghost", payload["error"])

    def test_cli_flush_pending_exit_3(self):
        """Local flush with undrained remote-mode leftovers exits 3
        (pending — distinct from success 0 and failure 1)."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-flh-")
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": tmp},
            }},
        }), encoding="utf-8")
        (Path(tmp) / "knowledge").mkdir()
        (Path(tmp) / "knowledge" / "delivery.json").write_text(json.dumps({
            "version": 1,
            "queue": [{
                "op_id": "kl-fixed", "namespace": "user:global",
                "target": "notes", "key": "run-notes", "version": 1,
                "locator": "run-notes/x.md",
                "fullhash": ["aaaaaaaa"] * 8, "summary": "s",
                "date": "2026-11-13T00:00:00Z",
                "expected_row_id": None,
                "enqueued_at": "2026-11-13T00:00:00Z",
                "attempts": 0, "last_error": None, "status": "queued"}],
            "receipts": {}, "intents": {}}), encoding="utf-8")
        proc = self._run(tmp, "flush", "--json")
        self.assertEqual(proc.returncode, 3,
                         "undrained entries are pending: distinct exit 3 "
                         "(%r %r)" % (proc.stdout[-200:], proc.stderr[-200:]))
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(payload["pending"], 1)

    def test_cli_oversized_summary_is_clean_exit_2(self):
        """The cap refusal through the CLI: exit 2 with ONE JSON error
        line under --json and no traceback anywhere."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-cap-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        init = subprocess.run(
            [sys.executable, str(STORE_PY), "init"],
            capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items()
                    if not k.startswith("ZMEM_")},
                 "ZMEM_HOME": str(REPO_ROOT),
                 "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                 "ZMEM_DATA": tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                 "PYTHONIOENCODING": "utf-8"},
            cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(init.returncode, 0, init.stderr[-400:])
        proc = self._run(tmp, "capture", "--target", "notes",
                         "--key", "k", "--content", "x",
                         "--summary", "word " * 200, "--json")
        self.assertEqual(proc.returncode, 2,
                         "%r %r" % (proc.stdout[-200:], proc.stderr[-200:]))
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertIn("at most", payload["error"])
        self.assertNotIn("Traceback", proc.stderr)

    def test_cli_read_unknown_target_is_clean_json(self):
        """The read-only surfaces fail fast on an unknown target too —
        before the store connect can mask it with a store error."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-rderr-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        proc = self._run(tmp, "read", "--target", "ghost", "--key", "k",
                         "--json")
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertIn("ghost", payload["error"])
        self.assertNotIn("Traceback", proc.stderr)

    def test_cli_corrupt_delivery_sidecar_is_clean_json(self):
        """A corrupt sidecar is an operator-facing exit 2 with one JSON
        error object — never a traceback (remote mode: no store needed)."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-corrupt-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        (Path(tmp) / "knowledge").mkdir()
        (Path(tmp) / "knowledge" / "delivery.json").write_text(
            "{definitely not json", encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_DATA": tmp,  # data dir only (remote mode: no ZMEM_STORE)
            "ZMEM_REMOTE_URL": "http://127.0.0.1:9/",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "knowledge", "status",
             "--json"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=180)
        self.assertEqual(proc.returncode, 2,
                         "%r %r" % (proc.stdout[-200:], proc.stderr[-200:]))
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, proc.stdout)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertIn("delivery sidecar", payload["error"])
        self.assertNotIn("Traceback", proc.stderr)


class _PointerHub:
    """A capable hub WITH the authoritative pointer read route.

    A MOCK of the transport's knowledge_pointer wrapper over the hub tool
    (hello advertises knowledge_link_v1; knowledge_pointer returns the
    scripted authoritative row) — live-hub transport tests are a separate
    later pass and are NOT claimed here."""

    def __init__(self, pointer=None, *, apply_ok=True):
        self.pointer = pointer          # the authoritative row, or None
        self.apply_ok = apply_ok
        self.ops_seen: list[list[dict]] = []
        self.pointer_reads = 0

    def hello(self):
        return {"protocol": 1,
                "capabilities": ["apply-ops", "knowledge_link_v1"],
                "zmem_version": "test-hub"}

    def knowledge_pointer(self, target, key, *, namespace=None, **_kw):
        self.pointer_reads += 1
        return {"ok": True, "namespace": namespace or "",
                "target": target, "key": key, "pointer": self.pointer}

    def apply_knowledge_ops(self, ops, principal="", **_kw):
        self.ops_seen.append([dict(op) for op in ops])
        if not self.apply_ok:
            return {"ok": False, "reason": "hub-unreachable"}
        return {"ok": True, "receipts": [
            {"op_id": op["op_id"], "status": "committed",
             "link_status": "advanced", "canonical_id": "row-2"}
            for op in ops]}


class _EnginePointerHub:
    """A fake hub whose apply lane runs the REAL ops engine on a scratch
    store AND whose pointer read is backed by that same store — a mock
    transport surface over the real engine/store (live-hub transport
    tests remain a separate pass and are NOT claimed here)."""

    def __init__(self, store_path: str, *, apply_ok: bool = True):
        self.store_path = store_path
        self.apply_ok = apply_ok

    def hello(self):
        return {"protocol": 1,
                "capabilities": ["apply-ops", "knowledge_link_v1"],
                "zmem_version": "test-hub"}

    def knowledge_pointer(self, target, key, *, namespace=None, **_kw):
        conn = _open_rw(self.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, namespace or "user:global", target, key)
        finally:
            conn.close()
        if pointer is None:
            return {"ok": True, "pointer": None}
        return {"ok": True, "pointer": {
            "row_id": pointer.row_id, "version": pointer.version,
            "locator": pointer.locator, "full_sha256": pointer.full_sha256,
            "summary": pointer.summary}}

    def apply_knowledge_ops(self, ops, principal="", **_kw):
        if not self.apply_ok:
            return {"ok": False, "reason": "hub-unreachable"}
        import copy as _copy

        from storelib import ops as opslib
        from storelib.ops import LegacyPrincipal

        conn = _open_rw(self.store_path)
        try:
            return opslib.apply_ops(
                conn, _copy.deepcopy(list(ops)), principal=principal,
                default_principal=LegacyPrincipal())
        finally:
            conn.close()


class FlushReconcileTests(unittest.TestCase):
    """Reviewer regression pair: flush must reconcile BOTH recovery
    records (journal AND delivery intent with the hub receipt's
    row_id/version), and a rejected attempt must quarantine its journal
    entry (terminal) so new content recovers."""

    def test_queued_flush_applies_reconciles_intent_then_v2(self):
        env = _Env()
        hub = _EnginePointerHub(env.store_path, apply_ok=False)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(first["pointer"]["status"], "queued")
        self.assertFalse(first["ok"])
        self.assertTrue(first["doc_published"])
        # The hub heals: flush drains the queue through the REAL engine.
        hub.apply_ok = True
        flush = workflow.flush(journal=env.journal, delivery=env.delivery,
                               http=hub)
        self.assertTrue(flush["ok"], flush)
        self.assertEqual(flush["pending"], 0)
        self.assertEqual(len(flush["applied"]), 1)
        # Journal reconciled to applied ...
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "applied")
        # ... AND the delivery intent carries the hub receipt's row id /
        # version / applied state (the next capture stakes what the hub
        # holds even if the authoritative read route were unavailable).
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "applied")
        receipt = env.delivery.receipts().get(flush["applied"][0])
        self.assertEqual(intent["row_id"], receipt.get("row_id"))
        self.assertEqual(intent["version"], receipt.get("version"))
        # v2 captures cleanly on top of the reconciled state.
        second = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V2,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["pointer"]["status"], "applied")
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, second["digest"])
            self.assertEqual(pointer.version, 2)
        finally:
            conn.close()

    def test_cas_rejection_quarantines_then_new_content_recovers(self):
        env = _Env()
        hub = _EnginePointerHub(env.store_path)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(first["ok"], first)
        # A STALE v2 op (bogus stake) is enqueued directly; the hub's real
        # engine rejects it on flush.
        doc2 = prepare_document("notes", "run-notes", NOTE_V2)
        env.journal.record_intent(
            target_id="notes", logical_key="run-notes",
            locator=doc2.locator, digest=doc2.digest,
            namespace="user:global")
        env.journal.mark(target_id="notes", logical_key="run-notes",
                         state="doc_verified", namespace="user:global")
        env.journal.mark(target_id="notes", logical_key="run-notes",
                         state="pointer_queued", namespace="user:global")
        env.delivery.enqueue({
            "namespace": "user:global", "target": "notes",
            "key": "run-notes", "version": 2, "locator": doc2.locator,
            "fullhash": [doc2.digest[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "stale stake", "date": "2026-11-13T00:00:00Z",
            "expected_row_id": "bogus-stale-row"})
        flush = workflow.flush(journal=env.journal, delivery=env.delivery,
                               http=hub)
        self.assertFalse(flush["ok"])
        self.assertEqual(len(flush["rejected"]), 1)
        receipt = flush["rejected"][0]
        self.assertIn(receipt.get("error"),
                      ("pointer_cas_stale", "pointer_conflict"))
        # The rejected attempt's journal entry is QUARANTINED (terminal):
        # the identity is not stuck ACTIVE forever.
        self.assertEqual(flush.get("journal_quarantined"),
                         [receipt["op_id"]])
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "quarantined")
        # The intent KEEPS the prior APPLIED record (the last pointer row
        # this host knows landed): the directly-enqueued stale op never
        # owned the intent, and a rejection must not clobber an applied
        # stake.
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "applied")
        self.assertEqual(intent["digest"], first["digest"])
        self.assertEqual(intent["op_id"], first["pointer"]["op_id"])
        self.assertIsNotNone(intent["row_id"])
        self.assertNotEqual(intent["expected_row_id"], "bogus-stale-row")
        # NEW content (different digest) recovers: fresh journal entry,
        # fresh stake, applied — history retained (one quarantined).
        third = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="recovered v2 body\n", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(third["ok"], third)
        self.assertEqual(third["version"], 2)
        states = [e.state for e in env.journal.entries()]
        self.assertEqual(states.count("quarantined"), 1)
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "applied")


class _RejectingHub(_PointerHub):
    """A capable hub whose apply lane REJECTS every knowledge_link op."""

    def apply_knowledge_ops(self, ops, principal="", **_kw):
        self.ops_seen.append([dict(op) for op in ops])
        return {"ok": True, "receipts": [
            {"op_id": op["op_id"], "status": "rejected",
             "error": "pointer_cas_stale"} for op in ops]}


class _DeniedHub(_PointerHub):
    """A capable hub whose pointer read is DENIED (namespace guard)."""

    def knowledge_pointer(self, target, key, *, namespace=None, **_kw):
        self.pointer_reads += 1
        return {"ok": False, "reason": "namespace_not_allowed"}


class RejectionTerminalTests(unittest.TestCase):
    """A rejection is TERMINAL for its attempt wherever it is observed —
    capture-time (queue entry already removed by the inner flush) and
    flush-time — and the recovery records keep the prior applied stake."""

    def test_capture_rejected_quarantines_and_recovers(self):
        env = _Env()
        hub = _RejectingHub(pointer=None)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertFalse(first["ok"])
        self.assertEqual(first["pointer"]["status"], "rejected")
        self.assertEqual(first["pointer"]["receipt"]["error"],
                         "pointer_cas_stale")
        # The journal entry is TERMINAL here — a later flush can never
        # quarantine it (the queue entry left with its receipt).
        self.assertEqual(first["journal_state"], "quarantined")
        self.assertEqual(env.journal.pending(), ())
        self.assertEqual(env.delivery.pending(), [])
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "rejected")
        self.assertEqual(intent["op_id"], first["pointer"]["op_id"])
        # NEW content recovers once the hub accepts: fresh entry, applied.
        healed = _PointerHub(pointer=None)
        second = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="recovered body\n", mode="remote", config=env.config(),
            journal=env.journal, delivery=env.delivery, http=healed)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["journal_state"], "applied")
        states = [e.state for e in env.journal.entries()]
        self.assertEqual(states.count("quarantined"), 1)

    def test_flush_rejection_owned_intent_records_rejected(self):
        """A rejection of an attempt that OWNS the intent (queued by
        capture, verdict at flush) records state rejected with the stake
        kept verbatim — and new content recovers on the hub's live row."""
        env = _Env()
        hub = _EnginePointerHub(env.store_path, apply_ok=False)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(first["pointer"]["status"], "queued")
        # Another writer lands ITS v1 through the real engine while ours
        # sits queued — our no-stake op becomes an honest cas_stale.
        other = prepare_document("notes", "run-notes", "other host v1\n")
        conn = _open_rw(env.store_path)
        try:
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=1, full_sha256=other.digest,
                summary="o", locator=other.locator, expected_row_id=None)
            live_row = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
        finally:
            conn.close()
        hub.apply_ok = True
        flush = workflow.flush(journal=env.journal, delivery=env.delivery,
                               http=hub)
        self.assertFalse(flush["ok"])
        self.assertEqual(len(flush["rejected"]), 1)
        self.assertEqual(flush["rejected"][0]["error"], "pointer_cas_stale")
        self.assertEqual(flush.get("journal_quarantined"),
                         [flush["rejected"][0]["op_id"]])
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "rejected")
        self.assertEqual(intent["op_id"], first["pointer"]["op_id"])
        self.assertIsNone(intent["expected_row_id"])  # stake kept verbatim
        # New content stakes the hub's LIVE row and advances.
        third = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="recovered v2 body\n", mode="remote", config=env.config(),
            journal=env.journal, delivery=env.delivery, http=hub)
        self.assertTrue(third["ok"], third)
        self.assertEqual(third["version"], 2)
        self.assertEqual(third["pointer"]["status"], "applied")
        sent = hub  # engine hub: verify the stake reached the engine
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.version, 2)
        finally:
            conn.close()
        self.assertTrue(live_row)


class SummaryScreenTests(unittest.TestCase):
    """A user-supplied summary is screened BEFORE the journal intent, the
    create and the queue (it rides the pointer row and the sidecar
    verbatim); a benign path snippet in prose is legal everywhere."""

    def test_user_summary_secret_rejected_before_any_write(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            # Fake scanner-shaped value, built from parts so the source
            # (and any log line) never carries the literal shape.
            bad = "deploy %s=%s" % ("pass" + "word", "0f1e" * 7)
            with self.assertRaises(workflow.WorkflowError) as ctx:
                workflow.capture(
                    target_id="notes", logical_key="run-notes",
                    text=NOTE_V1, summary=bad, mode="local", conn=conn,
                    config=env.config(), journal=env.journal,
                    delivery=env.delivery)
            self.assertIn("summary rejected", str(ctx.exception))
            self.assertIn("secret-like", str(ctx.exception))
            self.assertNotIn("0f1e", str(ctx.exception))  # count only
            self.assertIsNone(env.journal.entry_for(
                "notes", "run-notes", namespace="user:global"))
            self.assertEqual(list(env.vault.rglob("*.md")), [])
            n = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%'").fetchone()
            self.assertEqual(n["c"], 0)
            self.assertEqual(env.delivery.pending(), [])
        finally:
            conn.close()

    def test_summary_over_cap_rejected_before_any_write(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            from storelib import knowledge as _sk
            cap = _sk.MAX_SUMMARY_CHARS
            # Scanner-safe padding (no long alnum run): only the LENGTH
            # trips, so the refusal under test is the cap, not the secret
            # screen.
            long_summary = ("word " * ((cap // 5) + 2))[:cap + 1]
            self.assertEqual(len(long_summary), cap + 1)
            with self.assertRaises(workflow.WorkflowError) as ctx:
                workflow.capture(
                    target_id="notes", logical_key="run-notes",
                    text=NOTE_V1, summary=long_summary, mode="local",
                    conn=conn, config=env.config(), journal=env.journal,
                    delivery=env.delivery)
            self.assertIn("at most %d characters" % cap,
                          str(ctx.exception))
            self.assertIsNone(env.journal.entry_for(
                "notes", "run-notes", namespace="user:global"))
            self.assertEqual(list(env.vault.rglob("*.md")), [])
        finally:
            conn.close()

    def test_summary_path_snippet_parity_local_and_remote(self):
        benign = "see C:\\runbooks\\deploy.md for the rollback steps"
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            local = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                summary=benign, mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(local["ok"], local)
        finally:
            conn.close()
        # The same authored summary survives the REAL engine admission
        # remotely (summary is prose, not a routing field).
        hub = _EnginePointerHub(env.store_path)
        remote = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V2,
            summary=benign + " (v2)", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(remote["ok"], remote)
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.summary, benign + " (v2)")
        finally:
            conn.close()


class RemoteReadTests(unittest.TestCase):
    """A remote reader resolves the CURRENT version through the hub's
    AUTHORITATIVE knowledge_pointer read — a second host with empty local
    sidecars reads what host A published; a denied read never falls back,
    an unavailable one falls back to local state labeled stale."""

    @staticmethod
    def _vault_with(env, document):
        rel = document.locator.split("/")
        note_dir = env.vault.joinpath(*rel[:-1])
        note_dir.mkdir(parents=True, exist_ok=True)
        note_dir.joinpath(rel[-1]).write_bytes(document.content)

    def test_second_host_reads_and_checks_hub_pointer(self):
        host_b = _Env()  # EMPTY journal/delivery: host A published
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        self._vault_with(host_b, doc)
        hub = _PointerHub(pointer={
            "row_id": "row-9", "version": 1, "locator": doc.locator,
            "full_sha256": doc.digest, "summary": "s"})
        read = workflow.read(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=host_b.config(), journal=host_b.journal,
            delivery=host_b.delivery, http=hub)
        self.assertEqual(read["content"],
                         NOTE_V1.rstrip("\n") + "\n")
        self.assertEqual(read["provenance"], "hub-pointer")
        self.assertEqual(read["locator"], doc.locator)
        check = workflow.check(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=host_b.config(), journal=host_b.journal,
            delivery=host_b.delivery, http=hub)
        self.assertTrue(check["matches"], check)
        self.assertEqual(check["provenance"], "hub-pointer")
        self.assertEqual(hub.pointer_reads, 2)

    def test_hub_unavailable_with_empty_locals_is_honest_error(self):
        host_b = _Env()
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.read(
                target_id="notes", logical_key="run-notes",
                mode="remote", config=host_b.config(),
                journal=host_b.journal, delivery=host_b.delivery,
                http=_UnreachableHub())
        self.assertIn("no version recorded", str(ctx.exception))

    def test_hub_unavailable_falls_back_to_local_labeled_stale(self):
        env = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        self._vault_with(env, doc)
        env.delivery.set_intent(
            ref="knowledge:notes:run-notes", namespace="user:global",
            fields={"target": "notes", "key": "run-notes",
                    "namespace": "user:global", "digest": doc.digest,
                    "locator": doc.locator, "version": 1,
                    "expected_row_id": None, "row_id": None,
                    "state": "queued", "op_id": "kl-q", "staked": True})
        read = workflow.read(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=_UnreachableHub())
        self.assertEqual(read["provenance"], "intent-stale")
        self.assertEqual(read["content"], NOTE_V1.rstrip("\n") + "\n")

    def test_generic_pool_failure_is_not_a_refusal(self):
        """The exact-reason rule: a generic transport failure reason (the
        pre-fix empty-pool auth-bypass shape) stays an unavailable read —
        fallback labeled stale, never misclassified as a denial."""
        class _PoolFailHub(_PointerHub):
            def knowledge_pointer(self, target, key, *, namespace=None,
                                  **_kw):
                self.pointer_reads += 1
                return {"ok": False, "reason": "empty-pool"}

        env = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        self._vault_with(env, doc)
        env.delivery.set_intent(
            ref="knowledge:notes:run-notes", namespace="user:global",
            fields={"target": "notes", "key": "run-notes",
                    "namespace": "user:global", "digest": doc.digest,
                    "locator": doc.locator, "version": 1,
                    "expected_row_id": None, "row_id": None,
                    "state": "queued", "op_id": "kl-q", "staked": True})
        read = workflow.read(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=_PoolFailHub(pointer=None))
        self.assertEqual(read["provenance"], "intent-stale")

    def test_hub_denied_read_never_falls_back(self):
        env = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        self._vault_with(env, doc)
        env.delivery.set_intent(
            ref="knowledge:notes:run-notes", namespace="user:global",
            fields={"target": "notes", "key": "run-notes",
                    "namespace": "user:global", "digest": doc.digest,
                    "locator": doc.locator, "version": 1,
                    "expected_row_id": None, "row_id": None,
                    "state": "queued", "op_id": "kl-q", "staked": True})
        hub = _DeniedHub(pointer=None)
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.read(
                target_id="notes", logical_key="run-notes",
                mode="remote", config=env.config(), journal=env.journal,
                delivery=env.delivery, http=hub)
        self.assertIn("refused", str(ctx.exception))
        self.assertEqual(hub.pointer_reads, 1)


class WireShapeTests(unittest.TestCase):
    """Malformed wire shapes return None, never a traceback (the sidecar
    and hub answers are untrusted input)."""

    def test_digest_chunks_malformed_shapes_return_none(self):
        for malformed in (
                [12345678] * 8,            # non-str scalars (no TypeError)
                [None] * 8,
                ["aaaaaaaa"] * 7,          # wrong chunk count
                ["aaaaaaaa" * 2] * 8,      # wrong chunk length
                ["gggggggg"] * 8,          # non-hex
                {"not": "a list"},
                "aaaaaaaaaaaaaaaa",        # not a list at all
        ):
            self.assertIsNone(workflow._digest_from_chunks(malformed),
                              "shape %r must validate to None" %
                              type(malformed).__name__)
        good = ["0f1e0f1e"] * 8
        self.assertEqual(workflow._digest_from_chunks(good),
                         "0f1e0f1e" * 8)


class CaptureDrainTests(unittest.TestCase):
    """The capture-time best-effort flush uses the SHARED reconciliation:
    entries queued by EARLIER captures drain and reconcile too, and a
    whole-call hub denial is terminal (receipt + quarantine) everywhere
    it is observed."""

    def test_capture_drain_reconciles_earlier_queued_entries(self):
        env = _Env()
        hub = _EnginePointerHub(env.store_path, apply_ok=False)
        early = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(early["pointer"]["status"], "queued")
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "pointer_queued")
        # The hub heals; a DIFFERENT identity's capture drains BOTH.
        hub.apply_ok = True
        later = workflow.capture(
            target_id="notes", logical_key="other-notes",
            text="other identity body\n", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(later["ok"], later)
        self.assertEqual(env.delivery.pending(), [])
        # The EARLIER entry was reconciled by the drain — not left ACTIVE
        # pointer_queued forever.
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "applied")
        early_intent = env.delivery.get_intent(
            "knowledge:notes:run-notes", namespace="user:global")
        self.assertEqual(early_intent["state"], "applied")
        self.assertIsNotNone(early_intent["row_id"])
        # ... and the earlier content is now readable as the current
        # version through the authoritative hub pointer.
        read = workflow.read(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(read["content"], NOTE_V1.rstrip("\n") + "\n")

    def test_capture_whole_call_denial_is_terminal(self):
        from knowledge.delivery import (
            NONRETRYABLE_DENIAL_REASONS as _DENIALS)

        self.assertIn("namespace_not_allowed", _DENIALS)

        class _DenyApplyHub(_PointerHub):
            def apply_knowledge_ops(self, ops, principal="", **_kw):
                self.ops_seen.append([dict(op) for op in ops])
                return {"ok": False, "reason": "namespace_not_allowed"}

        env = _Env()
        hub = _DenyApplyHub(pointer=None)
        result = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertFalse(result["ok"])
        self.assertEqual(result["pointer"]["status"], "rejected")
        self.assertEqual(result["pointer"]["receipt"]["error"],
                         "namespace_not_allowed")
        # Terminal everywhere: queue drained, journal quarantined.
        self.assertEqual(result["journal_state"], "quarantined")
        self.assertEqual(env.delivery.pending(), [])
        self.assertEqual(env.journal.pending(), ())


class CrashWindowReconcileTests(unittest.TestCase):
    """The probe's crash window in every variant: the pointer commit
    LANDED but the verdict never reached the delivery intent (and in one
    variant not even the journal).  A retry must reconcile against the
    AUTHORITATIVE pointer — exact version + FULL digest + locator — and
    succeed (noop); a pointer another writer moved (different or newer)
    stays an honest cas_stale with nothing written."""

    def _seed_advance_window(self, env, conn, *, journal_applied):
        """v1 landed cleanly; the v2 attempt committed its advance, then
        crashed before recording the verdict (intent still staked)."""
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="local", conn=conn, config=env.config(),
            journal=env.journal, delivery=env.delivery)
        self.assertTrue(first["ok"], first)
        row_one = first["pointer"]["row_id"]
        doc2 = prepare_document("notes", "run-notes", NOTE_V2)
        env.journal.record_intent(
            target_id="notes", logical_key="run-notes",
            locator=doc2.locator, digest=doc2.digest,
            namespace="user:global")
        for state in ("doc_verified", "pointer_queued"):
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state=state, namespace="user:global")
        if journal_applied:
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state="applied", namespace="user:global")
        env.delivery.set_intent(
            ref="knowledge:notes:run-notes", namespace="user:global",
            fields={"target": "notes", "key": "run-notes",
                    "namespace": "user:global", "digest": doc2.digest,
                    "locator": doc2.locator, "version": 2,
                    "expected_row_id": row_one, "row_id": None,
                    "state": "staked", "op_id": None, "staked": True})
        # The crash-window commit itself: the v2 advance landed.
        store_knowledge.link_pointer(
            conn, namespace="user:global", target="notes",
            key="run-notes", version=2, full_sha256=doc2.digest,
            summary="v2", locator=doc2.locator, expected_row_id=row_one)
        return doc2

    def test_advance_crash_window_retries_reconcile(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            doc2 = self._seed_advance_window(env, conn,
                                             journal_applied=False)
            for attempt in (1, 2):
                retry = workflow.capture(
                    target_id="notes", logical_key="run-notes",
                    text=NOTE_V2, mode="local", conn=conn,
                    config=env.config(), journal=env.journal,
                    delivery=env.delivery)
                self.assertTrue(retry["ok"], "retry %d: %r"
                                % (attempt, retry))
                self.assertEqual(retry["pointer"]["status"], "noop")
                self.assertEqual(retry["journal_state"], "applied")
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, doc2.digest)
            self.assertEqual(pointer.version, 2)
        finally:
            conn.close()

    def test_advance_crash_window_journal_applied_still_reconciles(self):
        """Even when the journal DID reach applied before the crash, the
        staked sidecar is preserved (never silently re-observed) and the
        retry reconciles through the exact-match cas_stale path."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            self._seed_advance_window(env, conn, journal_applied=True)
            retry = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=NOTE_V2, mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(retry["ok"], retry)
            self.assertEqual(retry["pointer"]["status"], "noop")
            self.assertEqual(retry["journal_state"], "applied")
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)
        finally:
            conn.close()

    def test_advance_crash_window_other_writer_stays_stale(self):
        """The critic's negative: another writer's different/newer pointer
        is NOT ours — the retry stays cas_stale, nothing is written."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            self._seed_advance_window(env, conn, journal_applied=False)
            row_two = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
            other = prepare_document("notes", "run-notes",
                                     "writer B v3 wins\n")
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=3, full_sha256=other.digest,
                summary="v3", locator=other.locator,
                expected_row_id=row_two)
            retry = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=NOTE_V2, mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertFalse(retry["ok"])
            self.assertEqual(retry["pointer"]["status"], "cas_stale")
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, other.digest)
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)  # exactly one live row: B's
        finally:
            conn.close()

    def _seed_remote_window(self, env, hub, *, landed):
        """Remote: v1 applied through the engine; the v2 op committed (or,
        for landed=False, never did), the flush crashed before the verdict
        reached journal/intent — queue empty, intent still queued."""
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(first["ok"], first)
        conn = _open_rw(env.store_path)
        try:
            row_one = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
            doc2 = prepare_document("notes", "run-notes", NOTE_V2)
            env.journal.record_intent(
                target_id="notes", logical_key="run-notes",
                locator=doc2.locator, digest=doc2.digest,
                namespace="user:global")
            for state in ("doc_verified", "pointer_queued"):
                env.journal.mark(target_id="notes", logical_key="run-notes",
                                 state=state, namespace="user:global")
            env.delivery.set_intent(
                ref="knowledge:notes:run-notes", namespace="user:global",
                fields={"target": "notes", "key": "run-notes",
                        "namespace": "user:global", "digest": doc2.digest,
                        "locator": doc2.locator, "version": 2,
                        "expected_row_id": row_one, "row_id": None,
                        "state": "queued", "op_id": "kl-crash",
                        "staked": True})
            if landed:
                store_knowledge.link_pointer(
                    conn, namespace="user:global", target="notes",
                    key="run-notes", version=2, full_sha256=doc2.digest,
                    summary="v2", locator=doc2.locator,
                    expected_row_id=row_one)
            return doc2
        finally:
            conn.close()

    def test_remote_crash_window_reconciles_from_hub_read(self):
        """Remote equivalent: the cas-stale retry proves the landed
        pointer through the AUTHORITATIVE hub read (never the sidecar)
        and reconciles to applied."""
        env = _Env()
        hub = _EnginePointerHub(env.store_path)
        doc2 = self._seed_remote_window(env, hub, landed=True)
        retry = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V2,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["pointer"]["status"], "applied")
        self.assertEqual(retry["journal_state"], "applied")
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, doc2.digest)
            rows = conn.execute(
                "SELECT count(*) c FROM memory WHERE source_ref LIKE "
                "'knowledge:%' AND superseded_at IS NULL").fetchone()
            self.assertEqual(rows["c"], 1)
        finally:
            conn.close()
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "applied")
        self.assertIsNotNone(intent["row_id"])

    def test_remote_crash_window_other_writer_stays_rejected(self):
        env = _Env()
        hub = _EnginePointerHub(env.store_path)
        self._seed_remote_window(env, hub, landed=False)
        # Another writer lands ITS v2 while our queued op is in flight.
        conn = _open_rw(env.store_path)
        try:
            row_one = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
            other = prepare_document("notes", "run-notes",
                                     "writer B v2 wins\n")
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=2, full_sha256=other.digest,
                summary="b", locator=other.locator,
                expected_row_id=row_one)
        finally:
            conn.close()
        retry = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V2,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["pointer"]["status"], "rejected")
        self.assertEqual(retry["journal_state"], "quarantined")
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, other.digest)
        finally:
            conn.close()


class FlushRestartRecoveryTests(unittest.TestCase):
    """A flush that crashed AFTER its receipts were persisted and its
    entries removed, but BEFORE the journal/intent reconciliation: the
    next flush must recover from the persisted receipt + queued-state
    intent — not report a clean no-op while the journal stays ACTIVE."""

    def test_applied_receipt_orphan_recovers(self):
        from knowledge.delivery import flush_to_hub

        env = _Env()
        hub = _EnginePointerHub(env.store_path, apply_ok=False)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(first["pointer"]["status"], "queued")
        # The crash: the drain persists receipts + empties the queue, but
        # the reconciliation never runs.
        hub.apply_ok = True
        raw = flush_to_hub(env.delivery, hub)
        self.assertEqual(raw["applied"], [first["pointer"]["op_id"]])
        self.assertEqual(env.delivery.pending(), [])
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "pointer_queued")
        # The restarted flush recovers from the persisted receipt.
        result = workflow.flush(journal=env.journal, delivery=env.delivery,
                                http=hub)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovered_from_receipts"], 1)
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "applied")
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "applied")
        self.assertIsNotNone(intent["row_id"])

    def test_rejected_receipt_orphan_recovers_terminal(self):
        from knowledge.delivery import flush_to_hub

        env = _Env()
        hub = _EnginePointerHub(env.store_path)
        first = workflow.capture(
            target_id="notes", logical_key="run-notes", text=NOTE_V1,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(first["ok"], first)
        conn = _open_rw(env.store_path)
        try:
            row_one = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
        finally:
            conn.close()
        # A stale v2 attempt whose verdict (rejection) persisted while the
        # reconciliation crashed.
        doc2 = prepare_document("notes", "run-notes", NOTE_V2)
        env.journal.record_intent(
            target_id="notes", logical_key="run-notes",
            locator=doc2.locator, digest=doc2.digest,
            namespace="user:global")
        for state in ("doc_verified", "pointer_queued"):
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state=state, namespace="user:global")
        env.delivery.set_intent(
            ref="knowledge:notes:run-notes", namespace="user:global",
            fields={"target": "notes", "key": "run-notes",
                    "namespace": "user:global", "digest": doc2.digest,
                    "locator": doc2.locator, "version": 2,
                    "expected_row_id": "bogus-stale-row", "row_id": None,
                    "state": "queued", "op_id": "kl-orphan",
                    "staked": True})
        env.delivery.enqueue({
            "namespace": "user:global", "target": "notes",
            "key": "run-notes", "version": 2, "locator": doc2.locator,
            "fullhash": [doc2.digest[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "stale", "date": "2026-11-13T00:00:00Z",
            "expected_row_id": "bogus-stale-row", "op_id": "kl-orphan"})
        raw = flush_to_hub(env.delivery, hub)
        self.assertEqual(len(raw["rejected"]), 1)
        self.assertEqual(raw["rejected"][0]["op_id"], "kl-orphan")
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "pointer_queued")
        result = workflow.flush(journal=env.journal, delivery=env.delivery,
                                http=hub)
        self.assertFalse(result["ok"])
        self.assertEqual(result["recovered_from_receipts"], 1)
        self.assertEqual(env.journal.entry_for(
            "notes", "run-notes", namespace="user:global").state,
            "quarantined")
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["state"], "rejected")


class CreateFailureTests(unittest.TestCase):
    """FB-007 (workflow side): a typed create failure leaves the journal
    intent at doc_pending — no advance to doc_verified, no pointer queue
    transition — and surfaces a clean, content-free WorkflowError.
    FB-021: an unreadable note at the READ surface is likewise typed —
    the workflow never lets a raw OSError escape as a traceback."""

    def test_unreadable_note_read_surfaces_typed_workflow_error(self):
        """The CLI's read path catches only WorkflowError (cli.py) and
        _read_current catches only (TargetError, ValueError): the
        MarkdownStore read must convert an OSError itself (FB-021), so
        the workflow surface stays a clean, content-free error."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            with mock.patch("knowledge.documents._read_bounded",
                            side_effect=PermissionError("denied")):
                with self.assertRaises(workflow.WorkflowError) as ctx:
                    workflow.read(
                        target_id="notes", logical_key="run-notes",
                        mode="local", conn=conn, config=env.config(),
                        journal=env.journal, delivery=env.delivery)
            self.assertIn("PermissionError", str(ctx.exception))
            self.assertNotIsInstance(ctx.exception, OSError)
        finally:
            conn.close()

    def test_create_failure_keeps_doc_pending_and_recovers(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            def broken_link(src, dst, **_kw):
                raise OSError("simulated publish failure")

            with mock.patch("os.link", broken_link):
                with self.assertRaises(workflow.WorkflowError) as ctx:
                    workflow.capture(
                        target_id="notes", logical_key="run-notes",
                        text=NOTE_V1, mode="local", conn=conn,
                        config=env.config(), journal=env.journal,
                        delivery=env.delivery)
            self.assertNotIn("Pipefail", str(ctx.exception),
                             "the error must stay content-free")
            # The intent stays doc_pending: no doc_verified advance, no
            # pointer_queued transition, nothing enqueued.
            entry = env.journal.entry_for(
                "notes", "run-notes", namespace="user:global")
            self.assertEqual(entry.state, "doc_pending")
            self.assertEqual(env.delivery.pending(), [])
            intent = env.delivery.get_intent(
                "knowledge:notes:run-notes", namespace="user:global")
            self.assertEqual(intent["state"], "staked")
            self.assertIsNone(store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes"))
            self.assertEqual(list(env.vault.rglob("*.md")), [],
                             "no partial final was published")
            # The retry with a healthy link completes end to end.
            result = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["journal_state"], "applied")
        finally:
            conn.close()


class FlushSnapshotCostTests(unittest.TestCase):
    """FB-013: one workflow flush takes exactly ONE delivery sidecar
    snapshot for the whole restart-recovery pass (the per-receipt re-read
    made flush cost quadratic in persisted receipts)."""

    def test_exactly_one_snapshot_per_flush(self):
        env = _Env()
        delivery = env.delivery
        # Two orphan receipts (entries long gone from the queue, intents
        # still naming the ops) drive the recovery pass.
        for index in (1, 2):
            key = "run/orphan-%d" % index
            doc = prepare_document("notes", key, "orphan body %d\n" % index)
            delivery.set_intent(
                ref="knowledge:notes:%s" % key, namespace="user:global",
                fields={"target": "notes", "key": key,
                        "namespace": "user:global", "digest": doc.digest,
                        "locator": doc.locator, "version": 1,
                        "expected_row_id": None, "row_id": None,
                        "state": "queued", "op_id": "kl-orphan-%d" % index,
                        "staked": True})
            delivery.record_receipt({
                "op_id": "kl-orphan-%d" % index, "target": "notes",
                "key": key, "status": "committed",
                "link_status": "created", "version": 1,
                "row_id": "row-%d" % index, "error": None})
        calls = {"snapshot": 0}
        real_snapshot = delivery.snapshot

        def counting_snapshot():
            calls["snapshot"] += 1
            return real_snapshot()

        with mock.patch.object(delivery, "snapshot", counting_snapshot):
            result = workflow.flush(journal=env.journal,
                                    delivery=delivery, http=None)
        self.assertEqual(result["recovered_from_receipts"], 2, result)
        self.assertEqual(calls["snapshot"], 1,
                         "exactly one sidecar snapshot per flush")
        for index in (1, 2):
            intent = delivery.get_intent(
                "knowledge:notes:run/orphan-%d" % index,
                namespace="user:global")
            self.assertEqual(intent["state"], "applied")


class JournalFreshParentTests(unittest.TestCase):
    """A DIRECT constructor against a fresh nested path works: the lock
    file's parent is created before the lock is opened (no caller-side
    mkdir workaround required)."""

    def test_direct_constructor_nested_tmp(self):
        nested = (Path(tempfile.mkdtemp(prefix="zmem-knwf-jrn-"))
                  / "deep" / "nested" / "journal.json")
        self.assertFalse(nested.parent.exists())
        journal = KnowledgeJournal(nested)
        doc = prepare_document("notes", "fresh", "fresh parent body\n")
        entry = journal.record_intent(
            target_id="notes", logical_key="fresh", locator=doc.locator,
            digest=doc.digest, namespace="user:global")
        self.assertEqual(entry.state, "doc_pending")
        self.assertTrue(nested.exists())
        marked = journal.mark(target_id="notes", logical_key="fresh",
                              state="doc_verified", namespace="user:global")
        self.assertEqual(marked.state, "doc_verified")


class RemoteStakeTests(unittest.TestCase):
    """The remote predecessor is the AUTHORITATIVE hub pointer (the
    capability-gated knowledge_pointer read), so a SECOND host can stake
    and advance a pointer it never captured locally."""

    def test_second_host_stakes_hub_pointer(self):
        env = _Env()
        existing = prepare_document("notes", "run-notes",
                                    "first host body\n")
        hub = _PointerHub(pointer={
            "row_id": "row-1", "version": 1, "locator": existing.locator,
            "full_sha256": existing.digest, "summary": "s"})
        result = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="second host newer body\n", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["completed"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(hub.pointer_reads, 1)
        sent = hub.ops_seen[-1][0]["args"]
        self.assertEqual(sent["expected_row_id"], "row-1")
        self.assertEqual(sent["version"], 2)
        self.assertEqual(sent["namespace"], "user:global")

    def test_crash_retry_reuses_fixed_stake_not_newer_pointer(self):
        env = _Env()
        first = prepare_document("notes", "run-notes", "first body\n")
        hub = _PointerHub(pointer={
            "row_id": "row-1", "version": 1, "locator": first.locator,
            "full_sha256": first.digest, "summary": "s"}, apply_ok=False)
        staked = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="staked body\n", mode="remote", config=env.config(),
            journal=env.journal, delivery=env.delivery, http=hub)
        self.assertEqual(staked["pointer"]["status"], "queued")
        self.assertFalse(staked["ok"])
        self.assertFalse(staked["completed"])
        self.assertTrue(staked["doc_published"])
        # Another host advances the hub pointer while ours sits queued.
        other = prepare_document("notes", "run-notes", "other host v2\n")
        hub.pointer = {"row_id": "row-2", "version": 2,
                       "locator": other.locator,
                       "full_sha256": other.digest, "summary": "o"}
        retry = workflow.capture(
            target_id="notes", logical_key="run-notes",
            text="staked body\n", mode="remote", config=env.config(),
            journal=env.journal, delivery=env.delivery, http=hub)
        # The FIXED stake is reused: the queued op still carries row-1/v2
        # — never silently refreshed to the newer pointer row-2.
        self.assertEqual(retry["pointer"]["status"], "queued")
        pending = env.delivery.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["expected_row_id"], "row-1")
        self.assertEqual(pending[0]["version"], 2)
        sent = hub.ops_seen[-1][0]["args"]
        self.assertEqual(sent["expected_row_id"], "row-1")
        intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
        self.assertEqual(intent["expected_row_id"], "row-1")


class StakeLawTests(unittest.TestCase):
    """The stake is durable BEFORE the create and FIXED across retries: a
    crash-retry against a pointer another writer advanced is an honest
    cas_stale, never a silent supersede."""

    def test_local_retry_fixed_stale_stake_is_cas_stale(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            first = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(first["ok"])
            row_one = first["pointer"]["row_id"]
            # Host A staked row_one for its v2 durably, then crashed.
            doc2 = prepare_document("notes", "run-notes", NOTE_V2)
            env.journal.record_intent(
                target_id="notes", logical_key="run-notes",
                locator=doc2.locator, digest=doc2.digest,
                namespace="user:global")
            env.delivery.set_intent(
                ref="knowledge:notes:run-notes", namespace="user:global",
                fields={"target": "notes", "key": "run-notes",
                        "namespace": "user:global", "digest": doc2.digest,
                        "locator": doc2.locator, "version": 2,
                        "expected_row_id": row_one, "row_id": None,
                        "state": "staked", "op_id": None, "staked": True})
            # Host B advances the pointer to ITS v2 while A is down.
            other = prepare_document("notes", "run-notes", "host B wins\n")
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=2, full_sha256=other.digest,
                summary="b", locator=other.locator,
                expected_row_id=row_one)
            # A retries the SAME screened content: the fixed stake is NOT
            # refreshed to B's row — honest cas_stale; B's pointer lives.
            retry = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V2,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertFalse(retry["ok"])
            self.assertFalse(retry["completed"])
            self.assertTrue(retry["doc_published"])
            self.assertEqual(retry["pointer"]["status"], "cas_stale")
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.full_sha256, other.digest)
            # The stale attempt is TERMINAL (quarantined), not ACTIVE
            # forever, and its fixed stake is kept on the record.
            self.assertEqual(retry["journal_state"], "quarantined")
            self.assertEqual(env.journal.entry_for(
                "notes", "run-notes", namespace="user:global").state,
                "quarantined")
            self.assertEqual(env.journal.pending(), ())
            intent = env.delivery.get_intent("knowledge:notes:run-notes",
                                             namespace="user:global")
            self.assertEqual(intent["state"], "cas_stale")
            self.assertEqual(intent["expected_row_id"], row_one)
            # NEW content recovers on top of B's pointer: fresh journal
            # entry, fresh observation, advanced.
            recovered = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text="host A recovers\n", mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["pointer"]["status"], "advanced")
            self.assertEqual(recovered["version"], 3)
            self.assertEqual(recovered["journal_state"], "applied")
            states = [e.state for e in env.journal.entries()]
            self.assertEqual(states.count("quarantined"), 1)
        finally:
            conn.close()

    def test_cas_loser_never_clobbers_entry_another_writer_applied(self):
        """The quarantine guard: when the SAME screened content already
        landed through another writer (shared journal entry driven to
        applied), a stale-stake retry reports cas_stale but leaves the
        applied journal entry untouched."""
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            first = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            row_one = first["pointer"]["row_id"]
            doc2 = prepare_document("notes", "run-notes", NOTE_V2)
            # This host's v2 attempt reached pointer_queued with a stake on
            # row_one ...
            env.journal.record_intent(
                target_id="notes", logical_key="run-notes",
                locator=doc2.locator, digest=doc2.digest,
                namespace="user:global")
            env.delivery.set_intent(
                ref="knowledge:notes:run-notes", namespace="user:global",
                fields={"target": "notes", "key": "run-notes",
                        "namespace": "user:global", "digest": doc2.digest,
                        "locator": doc2.locator, "version": 2,
                        "expected_row_id": row_one, "row_id": None,
                        "state": "staked", "op_id": None, "staked": True})
            # ... while another writer landed v2 and then a v3 on top, and
            # drove the SHARED v2 journal entry to applied.
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=2, full_sha256=doc2.digest,
                summary="v2", locator=doc2.locator,
                expected_row_id=row_one)
            for state in ("doc_verified", "pointer_queued", "applied"):
                env.journal.mark(target_id="notes", logical_key="run-notes",
                                 state=state, namespace="user:global")
            row_two = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes").row_id
            v3 = prepare_document("notes", "run-notes", "writer B v3\n")
            store_knowledge.link_pointer(
                conn, namespace="user:global", target="notes",
                key="run-notes", version=3, full_sha256=v3.digest,
                summary="v3", locator=v3.locator, expected_row_id=row_two)
            retry = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V2,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertFalse(retry["ok"])
            self.assertEqual(retry["pointer"]["status"], "cas_stale")
            # The applied record survives — never rewritten to quarantined.
            self.assertEqual(retry["journal_state"], "applied")
            self.assertEqual([e.state for e in env.journal.entries()],
                             ["applied", "applied"])
        finally:
            conn.close()


class NamespaceIdentityTests(unittest.TestCase):
    """Full identity = (namespace, target, key): no cross-namespace
    crosstalk in stakes, intents, journal entries, pointers or the queue."""

    def test_two_namespaces_same_target_key_stay_separate(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            a = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                namespace="user:global", mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            b = workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                namespace="project:alpha", mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(a["ok"])
            self.assertTrue(b["ok"])
            # Two distinct pointer rows, both v1, same digest.
            pa = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            pb = store_knowledge.get_pointer(
                conn, "project:alpha", "notes", "run-notes")
            self.assertIsNotNone(pa)
            self.assertIsNotNone(pb)
            self.assertEqual(pa.version, 1)
            self.assertEqual(pb.version, 1)
            self.assertNotEqual(pa.row_id, pb.row_id)
            # Two distinct journal entries, both applied.
            self.assertEqual(env.journal.entry_for(
                "notes", "run-notes", namespace="user:global").state,
                "applied")
            self.assertEqual(env.journal.entry_for(
                "notes", "run-notes", namespace="project:alpha").state,
                "applied")
            # Two distinct intent records keyed by the FULL identity.
            ia = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="user:global")
            ib = env.delivery.get_intent("knowledge:notes:run-notes",
                                         namespace="project:alpha")
            self.assertIsNotNone(ia)
            self.assertIsNotNone(ib)
            self.assertEqual(ia["namespace"], "user:global")
            self.assertEqual(ib["namespace"], "project:alpha")
        finally:
            conn.close()

    def test_queue_dedup_includes_namespace(self):
        env = _Env()
        base = {"target": "notes", "key": "run-notes", "version": 1,
                "locator": "run-notes/x.md", "fullhash": ["aaaaaaaa"] * 8,
                "summary": "s", "date": "2026-11-13T00:00:00Z",
                "expected_row_id": None}
        env.delivery.enqueue(dict(base, namespace="user:global"))
        env.delivery.enqueue(dict(base, namespace="project:alpha"))
        env.delivery.enqueue(dict(base, namespace="user:global"))
        pending = env.delivery.pending()
        self.assertEqual(
            len(pending), 2,
            "same target/key/locator in two namespaces are two entries; a "
            "same-namespace retry dedupes to one")


class JournalVisibilityTests(unittest.TestCase):
    """A journal failure is VISIBLE: only a checked identical state may
    reconcile a mark refusal away; nothing claims completion against a
    journal that refused to record it."""

    def test_quarantined_intent_refused_before_create(self):
        env = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        env.journal.record_intent(
            target_id="notes", logical_key="run-notes",
            locator=doc.locator, digest=doc.digest,
            namespace="user:global")
        env.journal.mark(target_id="notes", logical_key="run-notes",
                         state="quarantined", namespace="user:global")
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="remote", config=env.config(), journal=env.journal,
                delivery=env.delivery, http=_UnsupportedHub())
        self.assertIn("quarantined", str(ctx.exception))
        self.assertEqual(list(env.vault.rglob("*.md")), [],
                         "the refusal precedes the create")

    def test_mark_failure_surfaces_never_swallowed(self):
        env = _Env()

        class _BrokenMark(KnowledgeJournal):
            def mark(self, **_kw):
                raise JournalError("journal disk full")

        doc = prepare_document("notes", "run-notes", NOTE_V1)
        broken = _BrokenMark(Path(env.tmp) / "knowledge" / "journal.json")
        broken.record_intent(target_id="notes", logical_key="run-notes",
                             locator=doc.locator, digest=doc.digest,
                             namespace="user:global")
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow._journal_mark(
                broken, target_id="notes", logical_key="run-notes",
                state="doc_verified", namespace="user:global")
        self.assertIn("journal refused", str(ctx.exception))

    def test_identical_terminal_state_reconciles(self):
        env = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        env.journal.record_intent(
            target_id="notes", logical_key="run-notes",
            locator=doc.locator, digest=doc.digest,
            namespace="user:global")
        for state in ("doc_verified", "pointer_queued", "applied"):
            env.journal.mark(target_id="notes", logical_key="run-notes",
                             state=state, namespace="user:global")
        # Reconciling an already-applied entry must NOT raise.
        workflow._journal_mark(
            env.journal, target_id="notes", logical_key="run-notes",
            state="applied", namespace="user:global")


class DeliveryLockTests(unittest.TestCase):
    """The sidecar lock is the journal's OS byte lock: a held lock is
    busy (bounded), never deleted, never taken over by age."""

    def test_held_lock_is_busy_and_never_unlinked(self):
        from knowledge.delivery import (  # noqa: PLC0415
            DeliveryError as _DeliveryError, _try_lock_nb, _unlock)

        env = _Env()
        sidecar = Path(env.tmp) / "knowledge" / "other-delivery.json"
        lock = sidecar.with_name(sidecar.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        entry = {"namespace": "user:global", "target": "notes",
                 "key": "run-notes", "version": 1,
                 "locator": "run-notes/x.md",
                 "fullhash": ["aaaaaaaa"] * 8, "summary": "s",
                 "date": "2026-11-13T00:00:00Z", "expected_row_id": None}
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.assertTrue(_try_lock_nb(fd))
            tight = KnowledgeDelivery(sidecar, lock_timeout_s=0.3)
            with self.assertRaises(_DeliveryError) as ctx:
                tight.enqueue(dict(entry))
            self.assertIn("lock busy", str(ctx.exception))
            self.assertTrue(lock.exists(),
                            "a live lock is never deleted (no unlink race)")
        finally:
            _unlock(fd)
            os.close(fd)
        # Once the holder releases, the next writer proceeds.
        stored = KnowledgeDelivery(sidecar, lock_timeout_s=2.0).enqueue(
            dict(entry))
        self.assertEqual(stored["status"], "queued")


class ReadIntegrityTests(unittest.TestCase):
    """FB-011 (2026-11-14): `read` VERIFIES — the actual digest of the
    bytes just read is compared with the expected digest BEFORE any
    decode or output.  A mismatch is a refusal with NO content bytes in
    the result envelope or any CLI stream; `check`'s contract (ok:false /
    matches:false / exit 1 / no content) is unchanged."""

    DISTINCT = "ZIRCONIUM-HAMMOCK-CRUCIBLE distinctive first line\n"

    def _captured_env(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=NOTE_V1, mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(result["ok"], result)
        finally:
            conn.close()
        return env

    @staticmethod
    def _tamper(env):
        note_files = list(env.vault.rglob("*.md"))
        assert len(note_files) == 1
        note_files[0].write_bytes(b"tampered bytes replacing the note\n")

    def test_local_mismatch_refusal_zero_content_anywhere(self):
        env = self._captured_env()
        self._tamper(env)
        conn = _open_rw(env.store_path)
        try:
            result = workflow.read(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["reason"], "digest-mismatch")
            self.assertNotIn("content", result)
            self.assertEqual(len(result["expected_digest"]), 64)
            self.assertEqual(len(result["actual_digest"]), 64)
            self.assertNotEqual(result["expected_digest"],
                                result["actual_digest"])
            # check contract unchanged: REPORTS the drift, no content.
            check = workflow.check(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertFalse(check["ok"])
            self.assertFalse(check["matches"])
            self.assertNotIn("content", check)
        finally:
            conn.close()

    def test_matching_digest_still_returns_content(self):
        env = self._captured_env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.read(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["content"],
                             NOTE_V1.rstrip("\n") + "\n")
            self.assertEqual(result["expected_digest"],
                             result["actual_digest"])
        finally:
            conn.close()

    def test_remote_mismatch_refusal_no_content(self):
        host_b = _Env()
        doc = prepare_document("notes", "run-notes", NOTE_V1)
        # The hub authoritatively points at doc's locator, but this host's
        # vault holds DIFFERENT bytes at that path.
        rel = doc.locator.split("/")
        note_dir = host_b.vault.joinpath(*rel[:-1])
        note_dir.mkdir(parents=True, exist_ok=True)
        note_dir.joinpath(rel[-1]).write_bytes(b"not the published bytes\n")
        hub = _PointerHub(pointer={
            "row_id": "row-11", "version": 1, "locator": doc.locator,
            "full_sha256": doc.digest, "summary": "s"})
        result = workflow.read(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=host_b.config(), journal=host_b.journal,
            delivery=host_b.delivery, http=hub)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "digest-mismatch")
        self.assertNotIn("content", result)
        self.assertEqual(result["provenance"], "hub-pointer")
        check = workflow.check(
            target_id="notes", logical_key="run-notes", mode="remote",
            config=host_b.config(), journal=host_b.journal,
            delivery=host_b.delivery, http=hub)
        self.assertFalse(check["ok"])
        self.assertFalse(check["matches"])
        self.assertNotIn("content", check)

    def test_malformed_locator_is_typed_content_free_error(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            for bad in ("not-a-locator", "run-notes/nothex.md",
                        "run-notes/" + "a" * 64 + ".md", ""):
                with self.subTest(locator=bad):
                    with self.assertRaises(workflow.WorkflowError) as ctx:
                        workflow.read(
                            target_id="notes", logical_key="run-notes",
                            locator=bad, mode="local", conn=conn,
                            config=env.config(), journal=env.journal,
                            delivery=env.delivery)
                    self.assertIn("malformed", str(ctx.exception))
        finally:
            conn.close()

    def test_non_utf8_digest_matching_read_is_typed_error(self):
        """A hand-crafted locator whose digest MATCHES non-UTF-8 bytes: the
        digest comparison passes and the decode failure must surface as a
        typed, content-free WorkflowError (COR-013), never a raw
        UnicodeDecodeError traceback."""
        import hashlib

        from knowledge.documents import version_locator

        env = _Env()
        raw = b"\xff\xfe broken utf8 payload \x81\n"
        digest = hashlib.sha256(raw).hexdigest()
        locator = version_locator("run-notes", digest)
        rel = locator.split("/")
        note_dir = env.vault.joinpath(*rel[:-1])
        note_dir.mkdir(parents=True, exist_ok=True)
        note_dir.joinpath(rel[-1]).write_bytes(raw)
        conn = _open_rw(env.store_path)
        try:
            with self.assertRaises(workflow.WorkflowError) as ctx:
                workflow.read(
                    target_id="notes", logical_key="run-notes",
                    locator=locator, mode="local", conn=conn,
                    config=env.config(), journal=env.journal,
                    delivery=env.delivery)
            self.assertIn("not valid UTF-8", str(ctx.exception))
        finally:
            conn.close()

    def test_cli_mismatch_refusal_no_content_any_stream(self):
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-rdmis-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")
        init = subprocess.run(
            [sys.executable, str(STORE_PY), "init"], capture_output=True,
            text=True,
            env={**{k: v for k, v in os.environ.items()
                    if not k.startswith("ZMEM_")},
                 "ZMEM_HOME": str(REPO_ROOT),
                 "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                 "ZMEM_DATA": tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                 "PYTHONIOENCODING": "utf-8"},
            cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(init.returncode, 0, init.stderr[-400:])

        def run(*argv):
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("ZMEM_")}
            env.update({
                "ZMEM_HOME": str(REPO_ROOT),
                "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                "ZMEM_DATA": tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "PYTHONIOENCODING": "utf-8",
            })
            return subprocess.run(
                [sys.executable, str(STORE_PY), "knowledge", *argv],
                capture_output=True, text=True, env=env,
                cwd=str(REPO_ROOT), timeout=180)

        secret_body = "TAMPERMARKER-UNIQUE-BODY-7f3a"
        cap = run("capture", "--target", "notes", "--key", "run-notes",
                  "--content", secret_body, "--json")
        self.assertEqual(cap.returncode, 0, cap.stderr[-300:])
        tampered = list(vault.rglob("*.md"))
        self.assertEqual(len(tampered), 1)
        tampered[0].write_text("replaced content\n", encoding="utf-8")

        mis = run("read", "--target", "notes", "--key", "run-notes",
                  "--json")
        self.assertEqual(mis.returncode, 1,
                         "%r %r" % (mis.stdout[-200:], mis.stderr[-200:]))
        payload = json.loads(mis.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "digest-mismatch")
        self.assertNotIn("content", payload)
        self.assertNotIn("replaced content", mis.stdout)
        self.assertNotIn(secret_body, mis.stdout + mis.stderr)

        plain = run("read", "--target", "notes", "--key", "run-notes")
        self.assertEqual(plain.returncode, 1)
        self.assertNotIn("replaced content", plain.stdout)
        self.assertEqual(plain.stdout, "")
        self.assertIn("digest-mismatch", plain.stderr)

        check = run("check", "--target", "notes", "--key", "run-notes",
                    "--json")
        self.assertEqual(check.returncode, 1)
        check_payload = json.loads(check.stdout.splitlines()[0])
        self.assertFalse(check_payload["ok"])
        self.assertFalse(check_payload["matches"])
        self.assertNotIn("content", check_payload)

        bad_locator = run("read", "--target", "notes", "--key",
                          "run-notes", "--locator", "nope", "--json")
        self.assertEqual(bad_locator.returncode, 2)
        err = json.loads(bad_locator.stdout.splitlines()[0])
        self.assertFalse(err["ok"])
        self.assertIn("malformed", err["error"])
        self.assertNotIn("Traceback", bad_locator.stderr)


class DefaultSummaryPlaceholderTests(unittest.TestCase):
    """FB-012 (2026-11-14): the default pointer summary is the FIXED
    placeholder `knowledge note <key>` — never derived from the note body.
    An explicit --summary still rides verbatim (and is still screened —
    SummaryScreenTests above)."""

    DISTINCT = "ZIRCONIUM-HAMMOCK-CRUCIBLE distinctive first line"

    def test_default_summary_is_placeholder_never_body(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=self.DISTINCT + "\nsecond line\n", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.summary, "knowledge note run-notes")
            self.assertNotIn("ZIRCONIUM", pointer.summary)
            self.assertNotIn("first line", pointer.summary)
        finally:
            conn.close()
        # Remote lane: the enqueued op's summary is the same placeholder
        # (the queue entry carries pointer metadata only). _PointerHub
        # records the ops the apply lane received.
        hub = _PointerHub(pointer=None)
        remote = workflow.capture(
            target_id="notes", logical_key="run-notes2",
            text=self.DISTINCT + " two\n", mode="remote",
            config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertTrue(remote["ok"], remote)
        seen = [op for batch in hub.ops_seen for op in batch]
        self.assertTrue(seen, "the capable hub saw the enqueued op")
        summaries = {str(op["args"].get("summary")) for op in seen}
        self.assertIn("knowledge note run-notes2", summaries)
        for summary in summaries:
            self.assertNotIn("ZIRCONIUM", summary)

    def test_explicit_summary_still_flows_verbatim(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=NOTE_V1, summary="operator authored deploy note",
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run-notes")
            self.assertEqual(pointer.summary, "operator authored deploy note")
        finally:
            conn.close()


class StatusCorruptionTests(unittest.TestCase):
    """FB-016 (2026-11-14): duplicate live verified pointer rows are
    REPORTED, not swallowed — status answers ok:false with a structured,
    content-free corruption entry and a nonzero exit; capture against the
    corruption is one clean typed JSON error (never a KnowledgeError
    traceback), leaving the journal at doc_pending."""

    def _corrupted_env(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.capture(
                target_id="notes", logical_key="run-notes",
                text=NOTE_V1, mode="local", conn=conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            # Store surgery: a SECOND live verified row for the same ref —
            # exactly the corruption get_pointer treats as unrecoverable.
            other = prepare_document("notes", "run-notes",
                                     "other body entirely\n")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "superseded_at, ingestion_ts, retrieval_count, "
                "last_retrieved, taint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "user:global", "fact",
                 store_knowledge.build_payload(
                     target="notes", key="run-notes", version=5,
                     full_sha256=other.digest,
                     summary="corrupt duplicate"),
                 "knowledge", "knowledge:notes:run-notes", "", 0.5, "none",
                 "2026-11-14T00:00:00Z", None, "2026-11-14T00:00:00Z",
                 0, None, "untrusted_tool"))
            conn.commit()
        finally:
            conn.close()
        return env

    def test_status_reports_corruption_structured(self):
        env = self._corrupted_env()
        conn = _open_rw(env.store_path)
        try:
            result = workflow.status(
                target_id="notes", logical_key="run-notes", mode="local",
                conn=conn, config=env.config(), journal=env.journal,
                delivery=env.delivery)
            self.assertFalse(result["ok"], result)
            self.assertEqual(len(result["corruption"]), 1)
            entry = result["corruption"][0]
            self.assertEqual((entry["target"], entry["key"]),
                             ("notes", "run-notes"))
            self.assertEqual(entry["code"], "duplicate-verified-rows")
            self.assertIn("verified", entry["error"])
            # the legitimate journal/queue rows still render
            self.assertTrue(result["journal"])
        finally:
            conn.close()

    def test_status_clean_store_stays_ok(self):
        env = _Env()
        conn = _open_rw(env.store_path)
        try:
            workflow.capture(
                target_id="notes", logical_key="run-notes", text=NOTE_V1,
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            result = workflow.status(
                mode="local", conn=conn, config=env.config(),
                journal=env.journal, delivery=env.delivery)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("corruption", result)
        finally:
            conn.close()

    def test_cli_status_and_capture_under_corruption(self):
        env = self._corrupted_env()

        def run(*argv):
            run_env = {k: v for k, v in os.environ.items()
                       if not k.startswith("ZMEM_")}
            run_env.update({
                "ZMEM_HOME": str(REPO_ROOT),
                "ZMEM_STORE": env.store_path,
                "ZMEM_DATA": env.tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "PYTHONIOENCODING": "utf-8",
            })
            return subprocess.run(
                [sys.executable, str(STORE_PY), "knowledge", *argv],
                capture_output=True, text=True, env=run_env,
                cwd=str(REPO_ROOT), timeout=180)

        status = run("status", "--target", "notes", "--key", "run-notes",
                     "--json")
        self.assertEqual(status.returncode, 1,
                         "%r %r" % (status.stdout[-300:],
                                    status.stderr[-300:]))
        payload = json.loads(status.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["corruption"][0]["code"],
                         "duplicate-verified-rows")
        self.assertNotIn("Traceback", status.stderr)

        plain = run("status", "--target", "notes", "--key", "run-notes")
        self.assertEqual(plain.returncode, 1)
        self.assertIn("corruption", plain.stderr)

        cap = run("capture", "--target", "notes", "--key", "run-notes",
                  "--content", "changed body under corruption",
                  "--json")
        self.assertEqual(cap.returncode, 2,
                         "%r %r" % (cap.stdout[-300:], cap.stderr[-300:]))
        lines = [ln for ln in cap.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1,
                         "the JSON error contract is one stdout line: %r"
                         % cap.stdout)
        err = json.loads(lines[0])
        self.assertFalse(err["ok"])
        self.assertIn("verified pointer rows", err["error"])
        self.assertNotIn("Traceback", cap.stderr)
        # the attempt's journal entry stays honestly doc_pending
        entry = env.journal.entry_for("notes", "run-notes",
                                      namespace="user:global")
        self.assertEqual(entry.state, "doc_pending")


class CaptureKillSwitchTests(unittest.TestCase):
    """FB-022 code half (2026-11-14): ZMEM_CAPTURE=0 refuses knowledge
    capture AND flush immediately — before sidecar creation, store
    opening, target writes, or network. read/check/status stay available;
    pending queue state is never deleted. Unset or 1 behaves normally."""

    def _run(self, tmp: str, *argv: str, capture_env: str | None = None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
            "ZMEM_DATA": tmp,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
        })
        if capture_env is not None:
            env["ZMEM_CAPTURE"] = capture_env
        return subprocess.run(
            [sys.executable, str(STORE_PY), "knowledge", *argv],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=180)

    @staticmethod
    def _enabled_config(tmp: str, vault: Path):
        (Path(tmp) / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)},
            }},
        }), encoding="utf-8")

    def test_capture_refused_before_any_side_effect(self):
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-kill-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        self._enabled_config(tmp, vault)
        proc = self._run(tmp, "capture", "--target", "notes",
                         "--key", "run-notes", "--content", NOTE_V1.strip(),
                         "--json", capture_env="0")
        self.assertEqual(proc.returncode, 2,
                         "%r %r" % (proc.stdout[-300:], proc.stderr[-300:]))
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, proc.stdout)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertIn("ZMEM_CAPTURE=0", payload["error"])
        # NOTHING was created or opened: no sidecar dir/journal, no store
        # file, no target write.
        self.assertFalse((Path(tmp) / "knowledge").exists(),
                         "the kill switch fires before default_journal() "
                         "pre-creates the sidecar dir")
        self.assertFalse((Path(tmp) / "store.sqlite").exists(),
                         "the kill switch fires before any store open")
        self.assertEqual(list(vault.rglob("*.md")), [],
                         "no target write happened")

    def test_flush_refused_and_pending_state_preserved(self):
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-killf-")
        (Path(tmp) / "knowledge").mkdir()
        queued_entry = {
            "op_id": "kl-keepme", "namespace": "user:global",
            "target": "notes", "key": "run-notes", "version": 1,
            "locator": "run-notes/x.md",
            "fullhash": ["aaaaaaaa"] * 8, "summary": "s",
            "date": "2026-11-14T00:00:00Z", "expected_row_id": None,
            "enqueued_at": "2026-11-14T00:00:00Z", "attempts": 0,
            "last_error": None, "status": "queued"}
        sidecar = Path(tmp) / "knowledge" / "delivery.json"
        sidecar.write_text(json.dumps({
            "version": 1, "queue": [queued_entry],
            "receipts": {}, "intents": {}}), encoding="utf-8")
        proc = self._run(tmp, "flush", "--json", capture_env="0")
        self.assertEqual(proc.returncode, 2,
                         "%r %r" % (proc.stdout[-300:], proc.stderr[-300:]))
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertIn("ZMEM_CAPTURE=0", payload["error"])
        # pending state preserved verbatim
        after = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(after["queue"], [queued_entry])

    def test_unset_and_one_capture_normally(self):
        for value in (None, "1"):
            with self.subTest(zmem_capture=value):
                tmp = tempfile.mkdtemp(
                    prefix="zmem-knwf-killn%s" % (value or "unset"))
                vault = Path(tmp) / "vault"
                vault.mkdir()
                self._enabled_config(tmp, vault)
                init = subprocess.run(
                    [sys.executable, str(STORE_PY), "init"],
                    capture_output=True, text=True,
                    env={**{k: v for k, v in os.environ.items()
                            if not k.startswith("ZMEM_")},
                         "ZMEM_HOME": str(REPO_ROOT),
                         "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                         "ZMEM_DATA": tmp,
                         "ZMEM_MODEL_AUTODOWNLOAD": "0",
                         "PYTHONIOENCODING": "utf-8"},
                    cwd=str(REPO_ROOT), timeout=180)
                self.assertEqual(init.returncode, 0, init.stderr[-400:])
                proc = self._run(tmp, "capture", "--target", "notes",
                                 "--key", "run-notes",
                                 "--content", NOTE_V1.strip(), "--json",
                                 capture_env=value)
                self.assertEqual(proc.returncode, 0,
                                 "%r %r" % (proc.stdout[-300:],
                                            proc.stderr[-300:]))
                self.assertTrue(json.loads(
                    proc.stdout.splitlines()[0])["ok"])

    def test_reads_and_status_available_under_kill_switch(self):
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-killr-")
        vault = Path(tmp) / "vault"
        vault.mkdir()
        self._enabled_config(tmp, vault)
        init = subprocess.run(
            [sys.executable, str(STORE_PY), "init"], capture_output=True,
            text=True,
            env={**{k: v for k, v in os.environ.items()
                    if not k.startswith("ZMEM_")},
                 "ZMEM_HOME": str(REPO_ROOT),
                 "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                 "ZMEM_DATA": tmp, "ZMEM_MODEL_AUTODOWNLOAD": "0",
                 "PYTHONIOENCODING": "utf-8"},
            cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(init.returncode, 0, init.stderr[-400:])
        cap = self._run(tmp, "capture", "--target", "notes",
                        "--key", "run-notes", "--content", NOTE_V1.strip(),
                        "--json")
        self.assertEqual(cap.returncode, 0, cap.stderr[-300:])
        for argv, want_rc in (
                (("read", "--target", "notes", "--key", "run-notes"), 0),
                (("check", "--target", "notes", "--key", "run-notes",
                  "--json"), 0),
                (("status", "--json"), 0)):
            with self.subTest(cmd=argv[0]):
                proc = self._run(tmp, *argv, capture_env="0")
                self.assertEqual(proc.returncode, want_rc,
                                 "%r %r" % (proc.stdout[-300:],
                                            proc.stderr[-300:]))
        read = self._run(tmp, "read", "--target", "notes",
                         "--key", "run-notes", capture_env="0")
        self.assertEqual(read.stdout, NOTE_V1.rstrip("\n") + "\n")

    def test_disabled_config_flush_refuses_and_preserves(self):
        """A disabled knowledge config refuses flush BEFORE any store or
        hub access (INT-004/FB-022 family) and deletes nothing."""
        tmp = tempfile.mkdtemp(prefix="zmem-knwf-killd-")
        (Path(tmp) / "knowledge").mkdir()
        queued_entry = {
            "op_id": "kl-keepme2", "namespace": "user:global",
            "target": "notes", "key": "run-notes", "version": 1,
            "locator": "run-notes/x.md",
            "fullhash": ["bbbbbbbb"] * 8, "summary": "s",
            "date": "2026-11-14T00:00:00Z", "expected_row_id": None,
            "enqueued_at": "2026-11-14T00:00:00Z", "attempts": 0,
            "last_error": None, "status": "queued"}
        sidecar = Path(tmp) / "knowledge" / "delivery.json"
        sidecar.write_text(json.dumps({
            "version": 1, "queue": [queued_entry],
            "receipts": {}, "intents": {}}), encoding="utf-8")
        # config.json WITHOUT a knowledge object (default off)
        (Path(tmp) / "config.json").write_text("{}", encoding="utf-8")
        proc = self._run(tmp, "flush", "--json")
        self.assertEqual(proc.returncode, 2,
                         "%r %r" % (proc.stdout[-300:], proc.stderr[-300:]))
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertIn("disabled", payload["error"])
        after = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(after["queue"], [queued_entry])


def _all_tests():
    return unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__])


if __name__ == "__main__":
    unittest.main(verbosity=2)
