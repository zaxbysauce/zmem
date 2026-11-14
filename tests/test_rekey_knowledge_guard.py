"""FB-031 local coordinated rekey guard tests (Wave2 Cluster 6).

A namespace move (write.rekey_namespace, rekey.apply_namespace_map, and
schema's v5-migration autoload lane) strands every knowledge publication
durably in flight under the old namespace — the verified probe scenarios
(round3-rekey-probe.md, probe-264):

  A. a queued remote ADVANCE across the rekey loses recovery (the resent
     op CAS-misses, the journal is driven TERMINAL quarantined, the
     old-namespace identity is permanently un-republishable);
  B. a queued remote FIRST-CREATE lands in the ABANDONED namespace and a
     post-rekey client mints a second live verified row (orphan);
  C. a local crash between journal mark(pointer_queued) and link_pointer
     leaves an ACTIVE journal entry FOREVER (eternal pending).

This file proves the guard refuses all three BEFORE any SQLite or JSON
mutation, that the original namespace stays recoverable with the
original fixed stake, that corrupt sidecars fail closed, that
terminal-only history and unrelated namespaces do not block, that the
default-off lane creates NOTHING, and — with REAL cross-process
synchronization (multiprocessing events, never sleep-only asserts) —
that the journal-then-delivery lock protocol orders a concurrent capture
against a rekey on both OS legs (msvcrt/fcntl byte locks).

The crash windows are built ONLY through the product's own ordered
writes (workflow.capture against an engine-backed hub fake, the
tests/test_knowledge_delivery.py harness shape); no hand-written
sidecar state, no live config/vault/network.

Run: python tests/test_rekey_knowledge_guard.py
"""

from __future__ import annotations

import copy
import io
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

_TMP = tempfile.mkdtemp(prefix="zmem-rkg-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ.setdefault("ZMEM_MODELS_DIR", os.path.join(_TMP, "no-models"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
sys.path.insert(0, str(SCRIPTS_DIR))

import storelib  # noqa: E402
from storelib import schema as _schema  # noqa: E402
from storelib import write as _write  # noqa: E402
from storelib import knowledge as storelib_knowledge  # noqa: E402
from storelib.rekey import (  # noqa: E402
    RekeyKnowledgeBusy, apply_namespace_map, knowledge_rekey_fence)

from knowledge import workflow  # noqa: E402
from knowledge.config import load_knowledge_config  # noqa: E402
from knowledge.delivery import CAPABILITY, KnowledgeDelivery  # noqa: E402
from knowledge.journal import KnowledgeJournal  # noqa: E402

NS_OLD = "project:alpha"
NS_NEW = "project:beta"
NS_OTHER = "project:other"
V1 = "probe note body v1 - fleet lesson alpha\n"
V2 = "probe note body v2 - fleet lesson bravo\n"


# ---------------------------------------------------------------------------
# harness (test_knowledge_delivery.py / round3_rekey_probe.py shapes)


def _scratch_store_conn(path: Path) -> sqlite3.Connection:
    """Create + open a SECOND store file in-process (the env-swap dance
    the store.py shim performs; same shape as test_knowledge_guards)."""
    old = os.environ.get("ZMEM_STORE")
    os.environ["ZMEM_STORE"] = str(path)
    try:
        storelib._refresh_env_state()
        conn = _schema.connect()
        _schema.init_db(conn)
        _schema.migrate(conn)
    finally:
        if old is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = old
        storelib._refresh_env_state()
    return conn


@contextmanager
def _quiet():
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        yield


class _EngineHub:
    """Hub fake: hello/read routes real, apply either the REAL ops engine
    on the scratch store or 'down' (the durable crash window the delivery
    tests model with apply_mode='fail')."""

    def __init__(self, conn: sqlite3.Connection, *, apply_down=False):
        self.conn = conn
        self.apply_down = apply_down
        self.ops_seen: list[list[dict]] = []

    def hello(self):
        return {"protocol": 1, "capabilities": [CAPABILITY],
                "zmem_version": "guard-test-hub"}

    def knowledge_pointer(self, target, key, namespace=""):
        pointer = storelib_knowledge.get_pointer(
            self.conn, namespace, target, key)
        if pointer is None:
            return {"ok": True, "pointer": None}
        return {"ok": True, "pointer": {
            "row_id": pointer.row_id, "version": pointer.version,
            "full_sha256": pointer.full_sha256, "locator": pointer.locator,
            "summary": pointer.summary}}

    def apply_knowledge_ops(self, ops, principal: str = "", **_kw):
        self.ops_seen.append(copy.deepcopy(list(ops)))
        if self.apply_down:
            return {"ok": False, "reason": "hub-unreachable"}
        from storelib import ops as opslib
        from storelib.ops import LegacyPrincipal
        return opslib.apply_ops(
            self.conn, copy.deepcopy(list(ops)), principal=principal,
            default_principal=LegacyPrincipal())


class _Env:
    """A deterministic scratch store + knowledge sidecars, sidecar paths
    constructed directly under <store_dir>/knowledge (the guard's own
    construction rule)."""

    def __init__(self, *, knowledge_enabled=True, tag=""):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-rkg-%s-" % tag))
        self.store_path = self.tmp / "store.sqlite"
        self.conn = _scratch_store_conn(self.store_path)
        self.knowledge_enabled = knowledge_enabled
        if knowledge_enabled:
            self.vault = self.tmp / "vault"
            self.vault.mkdir()
            (self.tmp / "config.json").write_text(json.dumps({
                "knowledge": {"enabled": True, "targets": {
                    "notes": {"kind": "markdown", "root": str(self.vault)},
                }},
            }), encoding="utf-8")
        self.journal = KnowledgeJournal(
            self.tmp / "knowledge" / "journal.json")
        self.delivery = KnowledgeDelivery(
            self.tmp / "knowledge" / "delivery.json")

    def config(self):
        return load_knowledge_config(self.tmp / "config.json")

    def knowledge_dir(self) -> Path:
        return self.tmp / "knowledge"

    def journal_bytes(self) -> bytes:
        path = self.knowledge_dir() / "journal.json"
        return path.read_bytes() if path.exists() else b""

    def delivery_bytes(self) -> bytes:
        path = self.knowledge_dir() / "delivery.json"
        return path.read_bytes() if path.exists() else b""

    def live_rows(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, namespace, source_ref, superseded_at "
            "FROM memory ORDER BY id").fetchall()
            if r["superseded_at"] is None]

    def dead_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT id FROM memory WHERE superseded_at IS NOT NULL")}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _add_ordinary_row(conn, namespace, body):
    with _quiet():
        return str(_write.add_memory(
            conn, namespace=namespace, type_="fact", content=body,
            signal="test"))


def _rekey(env, *, frm=NS_OLD, to=NS_NEW):
    with _quiet():
        return _write.rekey_namespace(
            env.conn, from_namespace=frm, to_namespace=to)


def _apply_map(env, entries):
    with mock.patch.dict(os.environ, {
        "ZMEM_DATA": str(env.tmp / "data"),
        "ZMEM_BACKUP_DIR": str(env.tmp / "backups"),
    }, clear=False), _quiet():
        return apply_namespace_map(
            conn=env.conn, store_path=env.store_path, entries=entries)


# ---------------------------------------------------------------------------
# probe-264 scenarios (A / B / C) through the product's own writes


class ScenarioAGuardTest(unittest.TestCase):
    """Remote ADVANCE durably in flight (queue entry ns=old
    expected_row_id=R1 v2; journal pointer_queued; intent queued) — the
    rekey must refuse BEFORE any SQLite or JSON mutation, keep the
    original namespace recoverable with the ORIGINAL fixed stake, and
    create no quarantine or orphan."""

    def test_queued_advance_across_rekey_refused_and_recovery_intact(self):
        env = _Env(tag="a")
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)
        hub = _EngineHub(env.conn)

        with _quiet():
            r1 = workflow.capture(
                target_id="notes", logical_key="run/a", text=V1,
                mode="remote", namespace=NS_OLD, config=env.config(),
                journal=env.journal, delivery=env.delivery, http=hub)
        self.assertEqual(r1["pointer"]["status"], "applied", r1)
        row1 = storelib_knowledge.get_pointer(
            env.conn, NS_OLD, "notes", "run/a").row_id
        env.conn.commit()

        # the durable crash window: apply route down, v2 capture queued
        hub.apply_down = True
        with _quiet():
            r2 = workflow.capture(
                target_id="notes", logical_key="run/a", text=V2,
                mode="remote", namespace=NS_OLD, config=env.config(),
                journal=env.journal, delivery=env.delivery, http=hub)
        self.assertEqual(r2["pointer"]["status"], "queued", r2["pointer"])
        queue = env.delivery.pending()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["namespace"], NS_OLD)
        self.assertEqual(queue[0]["expected_row_id"], row1)
        self.assertEqual(
            env.journal.entry_for("notes", "run/a", namespace=NS_OLD).state,
            "pointer_queued")
        self.assertEqual(env.delivery.get_intent(
            "knowledge:notes:run/a", namespace=NS_OLD)["state"], "queued")

        rows_before = env.live_rows()
        dead_before = env.dead_ids()
        journal_before = env.journal_bytes()
        delivery_before = env.delivery_bytes()

        with self.assertRaises(RekeyKnowledgeBusy) as ctx:
            _rekey(env)
        self.assertIn(NS_OLD, str(ctx.exception))
        self.assertIn("knowledge", str(ctx.exception))

        # refused BEFORE any mutation: no SQLite write, no JSON change,
        # no quarantine, no orphan, no open transaction.
        self.assertFalse(env.conn.in_transaction)
        self.assertEqual(env.live_rows(), rows_before)
        self.assertEqual(env.dead_ids(), dead_before)
        self.assertEqual(env.journal_bytes(), journal_before)
        self.assertEqual(env.delivery_bytes(), delivery_before)
        self.assertEqual(
            env.journal.entry_for("notes", "run/a", namespace=NS_OLD).state,
            "pointer_queued",
            "the refusal must not quarantine the in-flight entry")

        # recovery intact in the ORIGINAL namespace with the ORIGINAL
        # fixed stake: heal the hub, flush, the advance CAS-matches row1.
        hub.apply_down = False
        with _quiet():
            flush = workflow.flush(journal=env.journal,
                                   delivery=env.delivery, http=hub)
        self.assertTrue(flush["ok"], flush)
        self.assertEqual(flush["pending"], 0)
        env.conn.commit()
        pointer = storelib_knowledge.get_pointer(
            env.conn, NS_OLD, "notes", "run/a")
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer.version, 2)
        self.assertEqual(env.dead_ids(), {row1},
                         "the honest CAS advance tombstoned exactly the "
                         "originally staked row")
        self.assertEqual(
            env.journal.entry_for("notes", "run/a", namespace=NS_OLD).state,
            "applied")

        # state drained -> the SAME rekey now proceeds
        moved = _rekey(env)
        self.assertGreaterEqual(moved, 1)
        env.conn.commit()
        self.assertEqual(
            storelib_knowledge.get_pointer(
                env.conn, NS_NEW, "notes", "run/a").version, 2)


class ScenarioBGuardTest(unittest.TestCase):
    """Remote FIRST-CREATE durably in flight (expected_row_id=None)
    across the rekey — the refusal prevents the abandoned-namespace
    create; after the flush exactly ONE live verified row exists, in the
    (still current) original namespace."""

    def test_queued_first_create_across_rekey_refused_no_orphan(self):
        env = _Env(tag="b")
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)
        hub = _EngineHub(env.conn)

        with _quiet():
            r1 = workflow.capture(
                target_id="notes", logical_key="run/one", text=V1,
                mode="remote", namespace=NS_OLD, config=env.config(),
                journal=env.journal, delivery=env.delivery, http=hub)
        self.assertEqual(r1["pointer"]["status"], "applied", r1)
        env.conn.commit()

        hub.apply_down = True
        with _quiet():
            r2 = workflow.capture(
                target_id="notes", logical_key="run/two", text=V1,
                mode="remote", namespace=NS_OLD, config=env.config(),
                journal=env.journal, delivery=env.delivery, http=hub)
        self.assertEqual(r2["pointer"]["status"], "queued", r2["pointer"])
        queue = env.delivery.pending()
        self.assertEqual(queue[0]["expected_row_id"], None)

        rows_before = env.live_rows()
        journal_before = env.journal_bytes()
        delivery_before = env.delivery_bytes()
        with self.assertRaises(RekeyKnowledgeBusy):
            _rekey(env)
        self.assertFalse(env.conn.in_transaction)
        self.assertEqual(env.live_rows(), rows_before)
        self.assertEqual(env.journal_bytes(), journal_before)
        self.assertEqual(env.delivery_bytes(), delivery_before)

        # no old-namespace orphan: heal + flush lands the pointer in the
        # still-current namespace, exactly once.
        hub.apply_down = False
        with _quiet():
            flush = workflow.flush(journal=env.journal,
                                   delivery=env.delivery, http=hub)
        self.assertTrue(flush["ok"], flush)
        env.conn.commit()
        live_two = env.conn.execute(
            "SELECT namespace FROM memory WHERE superseded_at IS NULL "
            "AND source_ref='knowledge:notes:run/two'").fetchall()
        self.assertEqual(len(live_two), 1,
                         "exactly one live row for the first-create ref")
        self.assertEqual(live_two[0]["namespace"], NS_OLD)
        self.assertEqual(
            storelib_knowledge.get_pointer(
                env.conn, NS_NEW, "notes", "run/two"), None)


class ScenarioCGuardTest(unittest.TestCase):
    """LOCAL crash between capture's durable mark(pointer_queued) and
    link_pointer (states reachable only via capture's own ordered
    writes) — the rekey refuses and the retry under the ORIGINAL
    namespace completes: no eternal pending."""

    def test_local_crash_pointer_queued_across_rekey_refused_no_eternal(self):
        env = _Env(tag="c")
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)

        with _quiet():
            r1 = workflow.capture(
                target_id="notes", logical_key="run/c", text=V1,
                mode="local", namespace=NS_OLD, conn=env.conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
        self.assertEqual(r1["pointer"]["status"], "created", r1["pointer"])
        env.conn.commit()

        def _die_link(*_a, **_k):
            raise RuntimeError(
                "simulated process death after mark(pointer_queued)")

        with mock.patch.object(storelib_knowledge, "link_pointer",
                               _die_link):
            with self.assertRaises(RuntimeError):
                with _quiet():
                    workflow.capture(
                        target_id="notes", logical_key="run/c", text=V2,
                        mode="local", namespace=NS_OLD, conn=env.conn,
                        config=env.config(), journal=env.journal,
                        delivery=env.delivery)
        entry = env.journal.entry_for("notes", "run/c", namespace=NS_OLD)
        self.assertEqual(entry.state, "pointer_queued")
        self.assertEqual(env.delivery.get_intent(
            "knowledge:notes:run/c", namespace=NS_OLD)["state"], "staked")

        rows_before = env.live_rows()
        journal_before = env.journal_bytes()
        with self.assertRaises(RekeyKnowledgeBusy):
            _rekey(env)
        self.assertFalse(env.conn.in_transaction)
        self.assertEqual(env.live_rows(), rows_before)
        self.assertEqual(env.journal_bytes(), journal_before)

        # the identical retry under the ORIGINAL namespace self-heals —
        # the ACTIVE entry is driven terminal, pending() empties.
        with _quiet():
            retry = workflow.capture(
                target_id="notes", logical_key="run/c", text=V2,
                mode="local", namespace=NS_OLD, conn=env.conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
        self.assertTrue(retry["ok"], retry)
        env.conn.commit()
        self.assertEqual(
            env.journal.entry_for("notes", "run/c", namespace=NS_OLD).state,
            "applied")
        self.assertEqual(env.journal.pending(), (),
                         "no eternal pending entry may survive")


# ---------------------------------------------------------------------------
# sidecar state matrix: corrupt / terminal-only / unrelated / disabled


class SidecarStateMatrixTest(unittest.TestCase):
    def _env_with_row(self, **kw):
        env = _Env(**kw)
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)
        _add_ordinary_row(env.conn, NS_OLD, "ordinary alpha row")
        env.conn.commit()
        return env

    def _active_intent(self, env, *, key="run/x", namespace=NS_OLD):
        from knowledge.documents import prepare_document
        doc = prepare_document("notes", key, "active intent body\n")
        env.journal.record_intent(
            target_id="notes", logical_key=key, locator=doc.locator,
            digest=doc.digest, namespace=namespace)

    def _terminal_intent(self, env, *, key="run/x", namespace=NS_OLD):
        from knowledge.documents import prepare_document
        doc = prepare_document("notes", key, "terminal intent body\n")
        env.journal.record_intent(
            target_id="notes", logical_key=key, locator=doc.locator,
            digest=doc.digest, namespace=namespace)
        for state in ("doc_verified", "pointer_queued", "applied"):
            env.journal.mark(target_id="notes", logical_key=key,
                             state=state, namespace=namespace)

    def test_corrupt_journal_fails_closed(self):
        env = self._env_with_row(tag="corrupt-j")
        self._active_intent(env)
        journal_path = env.knowledge_dir() / "journal.json"
        good = journal_path.read_bytes()
        journal_path.write_bytes(b"{not json")
        rows_before = env.live_rows()
        with self.assertRaises(RekeyKnowledgeBusy) as ctx:
            _rekey(env)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertEqual(env.live_rows(), rows_before)
        self.assertFalse(env.conn.in_transaction)

    def test_corrupt_delivery_sidecar_fails_closed(self):
        env = self._env_with_row(tag="corrupt-d")
        delivery_path = env.knowledge_dir() / "delivery.json"
        delivery_path.parent.mkdir(parents=True, exist_ok=True)
        delivery_path.write_bytes(b"[]")
        with self.assertRaises(RekeyKnowledgeBusy) as ctx:
            _rekey(env)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertFalse(env.conn.in_transaction)

    def test_terminal_history_only_permits_and_creates_normal_locks(self):
        env = self._env_with_row(tag="terminal")
        self._terminal_intent(env)
        env.delivery.set_intent(
            ref="knowledge:notes:run/x", namespace=NS_OLD, fields={
                "target": "notes", "key": "run/x", "digest": "0" * 64,
                "state": "applied", "staked": True})
        moved = _rekey(env)
        env.conn.commit()
        self.assertGreaterEqual(moved, 1)
        rows = env.conn.execute(
            "SELECT namespace FROM memory WHERE superseded_at IS NULL "
            "AND namespace=?", (NS_NEW,)).fetchall()
        self.assertTrue(rows, "terminal-only history must not block")
        # protocol (3): the enabled lane created/used the NORMAL
        # persistent journal+delivery lock files.
        self.assertTrue(
            (env.knowledge_dir() / "journal.json.lock").exists())
        self.assertTrue(
            (env.knowledge_dir() / "delivery.json.lock").exists())

    def test_unrelated_namespace_active_entry_does_not_block(self):
        env = self._env_with_row(tag="unrelated")
        self._active_intent(env, key="run/other", namespace=NS_OTHER)
        moved = _rekey(env)
        env.conn.commit()
        self.assertGreaterEqual(moved, 1,
                                "an active entry under an unrelated "
                                "namespace must not block a scoped move")

    def test_disabled_no_state_proceeds_and_creates_nothing(self):
        env = self._env_with_row(knowledge_enabled=False, tag="off")
        moved = _rekey(env)
        env.conn.commit()
        self.assertGreaterEqual(moved, 1)
        # protocol (1): NO knowledge directory, NO lock files, anywhere.
        self.assertFalse(env.knowledge_dir().exists(),
                         "default-off rekey must not create the knowledge "
                         "directory")
        self.assertEqual(list(env.tmp.glob("*.lock")), [])

    def test_disabled_existing_sidecars_still_coordinated(self):
        env = self._env_with_row(knowledge_enabled=False, tag="off-sc")
        # recovery records left behind by an earlier enabled era: the
        # guard must still inspect and coordinate them.
        self._active_intent(env)
        with self.assertRaises(RekeyKnowledgeBusy) as ctx:
            _rekey(env)
        self.assertIn(NS_OLD, str(ctx.exception))
        self.assertFalse(env.conn.in_transaction)
        # terminal-only history in the disabled lane permits again
        entry = env.journal.entry_for("notes", "run/x", namespace=NS_OLD)
        for state in ("doc_verified", "pointer_queued", "applied"):
            env.journal.mark(target_id="notes", logical_key="run/x",
                             state=state, namespace=NS_OLD)
        moved = _rekey(env)
        env.conn.commit()
        self.assertGreaterEqual(moved, 1)


# ---------------------------------------------------------------------------
# every entry point enforces; the autoload lane cannot recurse or
# partially migrate


class MapLaneGuardTest(unittest.TestCase):
    def test_apply_namespace_map_refuses_in_flight_publication(self):
        env = _Env(tag="map")
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)
        key = "run/map"
        with _quiet():
            workflow.capture(
                target_id="notes", logical_key=key, text=V1,
                mode="local", namespace=NS_OLD, conn=env.conn,
                config=env.config(), journal=env.journal,
                delivery=env.delivery)
        env.conn.commit()
        # a second version durably queued (remote crash window)
        hub = _EngineHub(env.conn, apply_down=True)
        with _quiet():
            workflow.capture(
                target_id="notes", logical_key=key, text=V2,
                mode="remote", namespace=NS_OLD, config=env.config(),
                journal=env.journal, delivery=env.delivery, http=hub)

        entries = [(f"knowledge:notes:{key}", NS_NEW)]
        rows_before = env.live_rows()
        journal_before = env.journal_bytes()
        delivery_before = env.delivery_bytes()
        rc = _apply_map(env, entries)
        self.assertEqual(rc, 1, "the map apply must refuse (exit 1)")
        self.assertFalse(env.conn.in_transaction)
        self.assertEqual(env.live_rows(), rows_before)
        self.assertEqual(env.journal_bytes(), journal_before)
        self.assertEqual(env.delivery_bytes(), delivery_before)

        # drained (heal the hub, flush reconciles journal + queue) ->
        # the same map applies
        hub.apply_down = False
        with _quiet():
            flush = workflow.flush(journal=env.journal,
                                   delivery=env.delivery, http=hub)
        self.assertTrue(flush["ok"], flush)
        rc = _apply_map(env, entries)
        self.assertEqual(rc, 0)
        env.conn.commit()
        self.assertEqual(
            storelib_knowledge.get_pointer(
                env.conn, NS_NEW, "notes", key).version, 2)


class SchemaAutoloadGuardTest(unittest.TestCase):
    """schema._rekey_namespaces (v5 migration + the every-migrate retry
    pass) is the third enforcement point: refuse-and-retry per
    namespace, never a partial migration, never a recursive schema init
    or a second store."""

    LEGACY = "legacy-guard-" + uuid.uuid4().hex[:8]

    def setUp(self):
        self.env = _Env(tag="v5")
        self.addCleanup(shutil.rmtree, self.env.tmp, True)
        self.addCleanup(self.env.close)
        _add_ordinary_row(self.env.conn, NS_OLD, "row riding the move")
        with _quiet():
            storelib_knowledge.link_pointer(
                self.env.conn, namespace=NS_OLD, target="notes",
                key="run/v5", version=1, full_sha256="a" * 64,
                summary="v5 lane", expected_row_id=None)
        self.env.conn.commit()
        self.env.conn.execute(
            "UPDATE memory SET namespace=? WHERE namespace=?",
            (self.LEGACY, NS_OLD))
        self.env.conn.commit()
        self.checkout = self.env.tmp / "checkout"
        self.checkout.mkdir()
        self.fake_host = types.SimpleNamespace(
            _get_git_remote_status=lambda p: (
                "https://example.invalid/org/repo.git", "remote"),
            _normalize_remote=lambda url: "alpha")

    def _with_migration_map(self):
        return mock.patch.dict(os.environ, {
            "ZMEM_NS_MIGRATION_MAP": json.dumps(
                {self.LEGACY: str(self.checkout)})}, clear=False)

    def test_autoload_lane_refuses_active_namespace(self):
        from knowledge.documents import prepare_document
        doc = prepare_document("notes", "run/v5", "active v5 body\n")
        self.env.journal.record_intent(
            target_id="notes", logical_key="run/v5", locator=doc.locator,
            digest=doc.digest, namespace=NS_OLD)  # the DESTINATION ns
        with self._with_migration_map(), \
                mock.patch.object(_schema, "_host", self.fake_host), _quiet():
            mapping = _schema._rekey_namespaces(self.env.conn, [self.LEGACY])
        self.assertEqual(mapping, {},
                         "the active destination namespace must be skipped")
        kept = self.env.conn.execute(
            "SELECT count(*) FROM memory WHERE namespace=?",
            (self.LEGACY,)).fetchone()[0]
        self.assertGreaterEqual(kept, 2, "nothing moved, no partial migration")

    def test_autoload_via_migrate_refuse_and_retry_without_recursion(self):
        from knowledge.documents import prepare_document
        doc = prepare_document("notes", "run/v5", "active v5 body two\n")
        self.env.journal.record_intent(
            target_id="notes", logical_key="run/v5", locator=doc.locator,
            digest=doc.digest, namespace=NS_OLD)
        fence_entries = []
        real_fence = knowledge_rekey_fence

        def _spy_fence(store_dir, **kw):
            fence_entries.append(store_dir)
            return real_fence(store_dir, **kw)

        with self._with_migration_map(), \
                mock.patch.object(_schema, "_host", self.fake_host), \
                mock.patch("storelib.rekey.knowledge_rekey_fence",
                           side_effect=_spy_fence), \
                mock.patch("storelib.schema.connect",
                           side_effect=AssertionError(
                               "autoload opened a second store")), \
                mock.patch("storelib.schema.init_db",
                           side_effect=AssertionError(
                               "autoload recursively initialized schema")), _quiet():
            _schema.migrate(self.env.conn)
        self.env.conn.commit()
        # refuse-and-retry: migrate() COMPLETED, the namespace is intact
        # and unrecorded, and the fence was used (journal-then-delivery)
        # without recursion.
        kept = self.env.conn.execute(
            "SELECT count(*) FROM memory WHERE namespace=?",
            (self.LEGACY,)).fetchone()[0]
        self.assertGreaterEqual(kept, 2)
        record = self.env.conn.execute(
            "SELECT value FROM meta WHERE key='ns_migration_v5'"
        ).fetchone()
        recorded = json.loads(record[0]) if record and record[0] else {}
        self.assertNotIn(self.LEGACY, recorded,
                         "a refused namespace must not be recorded as moved")
        self.assertTrue(fence_entries,
                        "the autoload lane must route through the fence")
        self.assertEqual(len(fence_entries), 1)
        self.assertEqual(Path(fence_entries[0]).resolve(),
                         self.env.tmp.resolve())

        # drained -> the retry pass (inside a plain migrate) moves it
        entry = self.env.journal.entry_for("notes", "run/v5", namespace=NS_OLD)
        for state in ("doc_verified", "pointer_queued", "applied"):
            if entry.state != state:
                self.env.journal.mark(
                    target_id="notes", logical_key="run/v5",
                    state=state, namespace=NS_OLD)
        with self._with_migration_map(), \
                mock.patch.object(_schema, "_host", self.fake_host), _quiet():
            _schema.migrate(self.env.conn)
        self.env.conn.commit()
        moved = self.env.conn.execute(
            "SELECT count(*) FROM memory WHERE namespace=?",
            (NS_OLD,)).fetchone()[0]
        self.assertGreaterEqual(moved, 2)


# ---------------------------------------------------------------------------
# REAL cross-process ordering: multiprocessing events, no sleep-only
# asserts. The byte locks are knowledge.locking's own cross-platform
# discipline (msvcrt on Windows, fcntl on POSIX), so these lanes RUN on
# both OS legs (plan gate 3). Workers are module-level (spawn pickles by
# name); every wait has a deadline.

_MP = multiprocessing


def _worker_path_bootstrap():
    sys.path.insert(0, str(SCRIPTS_DIR))


def _worker_capture_hold_journal(store_dir: str, held, release) -> None:
    """A capture process mid-flight: its FIRST durable write (the journal
    intent) is on disk, and it holds the journal lock (exactly the state
    between record_intent's write and its unlock while the next capture
    step runs)."""
    _worker_path_bootstrap()
    from knowledge.documents import prepare_document
    from knowledge.journal import KnowledgeJournal, journal_path_for_store
    from knowledge.locking import try_lock_nb, unlock

    journal = KnowledgeJournal(journal_path_for_store(store_dir))
    doc = prepare_document("notes", "run/xp", "cross-process body\n")
    journal.record_intent(
        target_id="notes", logical_key="run/xp", locator=doc.locator,
        digest=doc.digest, namespace=NS_OLD)
    lock_path = Path(store_dir) / "knowledge" / "journal.json.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not try_lock_nb(fd):
            raise SystemExit("XP-HOLD: journal lock unexpectedly busy")
        held.set()
        if not release.wait(30):
            raise SystemExit("XP-HOLD: release event timed out")
    finally:
        unlock(fd)
        os.close(fd)


def _worker_rekey(store_path: str, marker_path: str) -> None:
    """A rekey process: run write.rekey_namespace directly (the direct
    API + CLI lane) and record the outcome for the parent."""
    _worker_path_bootstrap()
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(store_path, timeout=10.0)
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    from storelib import write as worker_write
    from storelib.rekey import RekeyKnowledgeBusy
    try:
        with redirect_stdout(io.StringIO()):
            worker_write.rekey_namespace(
                conn, from_namespace=NS_OLD, to_namespace=NS_NEW)
    except RekeyKnowledgeBusy as exc:
        text = str(exc)
        reason = ("lock-busy" if "lock busy" in text
                  else "active" if "in flight" in text else "other")
        Path(marker_path).write_text(
            "REFUSED %s\n%s\n" % (reason, text), encoding="utf-8")
        return
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        Path(marker_path).write_text(
            "ERROR %s\n" % exc, encoding="utf-8")
        return
    Path(marker_path).write_text("MOVED\n", encoding="utf-8")


def _worker_hold_fence(store_dir: str, held, release) -> None:
    """A rekey process holding BOTH sidecar locks — exactly
    rekey_namespace's coordination state between BEGIN and COMMIT."""
    _worker_path_bootstrap()
    from storelib.rekey import knowledge_rekey_fence
    with knowledge_rekey_fence(Path(store_dir)):
        held.set()
        if not release.wait(30):
            raise SystemExit("XP-FENCE: release event timed out")


def _worker_capture_write(store_path: str, attempting, wrote, ts_path: str,
                          *, first_ever: bool = False) -> None:
    """A capture process performing its first durable sidecar write
    (journal record_intent — capture step 2, always before any target
    create), timed so the parent can prove the write is INVISIBLE until
    the rekey's locks release."""
    _worker_path_bootstrap()
    os.environ["ZMEM_STORE"] = store_path
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    # The spawn child imports this test module (and with it storelib,
    # whose STORE_PATH is resolved at import time from the PARENT's
    # global scratch env) while unpickling the target — refresh the env
    # state so default_journal()/config resolution land on THIS scratch
    # store before the first write.
    import storelib as _storelib_boot
    _storelib_boot._refresh_env_state()
    from knowledge.documents import prepare_document
    if first_ever:
        from knowledge.workflow import default_journal
        journal = default_journal()
    else:
        from knowledge.journal import KnowledgeJournal
        journal = KnowledgeJournal(
            Path(store_path).parent / "knowledge" / "journal.json")
    key = "run/xp-first" if first_ever else "run/xp-second"
    doc = prepare_document("notes", key, "cross-process %s\n" % key)
    attempting.set()
    journal.record_intent(
        target_id="notes", logical_key=key, locator=doc.locator,
        digest=doc.digest, namespace=NS_OLD)
    with open(ts_path, "a", encoding="utf-8") as fh:
        fh.write("wrote %.6f\n" % time.time())
    wrote.set()


def _read_marker(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class CrossProcessOrderingTest(unittest.TestCase):
    """Byte-lock ordering across real processes, synchronized with
    multiprocessing events (never sleep-only asserts; every wait has a
    deadline). Portable by construction: the locks are knowledge.locking's
    msvcrt (Windows) / fcntl (POSIX) discipline, so the same three lanes
    run on both OS CI legs (plan gate 3)."""

    def _scratch(self, *, enabled=True, prepare_sidecars=False):
        env = _Env(knowledge_enabled=enabled, tag="xp")
        self.addCleanup(shutil.rmtree, env.tmp, True)
        self.addCleanup(env.close)
        _add_ordinary_row(env.conn, NS_OLD, "cross-process row")
        env.conn.commit()
        env.conn.close()  # workers own the file now
        return env

    def _spawn(self, target, args=(), kwargs=None):
        proc = _MP.get_context("spawn").Process(
            target=target, args=args, kwargs=kwargs or {})
        proc.daemon = True
        proc.start()
        self.addCleanup(proc.join, 30)
        self.addCleanup(proc.terminate)
        return proc

    def test_capture_holding_journal_lock_rekey_refuses_unmutated(self):
        env = self._scratch()
        ctx = _MP.get_context("spawn")
        held, release = ctx.Event(), ctx.Event()
        capture = self._spawn(
            _worker_capture_hold_journal, (str(env.tmp), held, release))
        self.assertTrue(held.wait(30), "capture worker never took the lock")

        # Phase 1 — the journal lock is HELD: the rekey must refuse
        # (fail closed on the busy coordination lock) without touching
        # the store, and it must have stopped at the JOURNAL lock (the
        # fixed order): no delivery lock file appears.
        marker = env.tmp / "rekey-1.txt"
        rekey = self._spawn(
            _worker_rekey, (str(env.store_path), str(marker)))
        rekey.join(30)
        self.assertFalse(rekey.is_alive())
        self.assertTrue(marker.exists(), "rekey worker produced no marker")
        self.assertTrue(
            _read_marker(marker).startswith("REFUSED"),
            _read_marker(marker))
        self.assertFalse(
            (env.knowledge_dir() / "delivery.json.lock").exists(),
            "the fence must acquire journal-then-delivery and never "
            "touch the delivery lock when the journal lock is held")
        rows = sqlite3.connect(str(env.store_path)).execute(
            "SELECT namespace FROM memory WHERE superseded_at IS NULL "
            "AND namespace=?", (NS_OLD,)).fetchall()
        self.assertTrue(rows, "store unmutated while refused")

        # Phase 2 — the lock is free but the ACTIVE journal entry (the
        # capture's durable intent) is on disk: the rekey observes it
        # and refuses again, still unmutated.
        release.set()
        capture.join(30)
        self.assertFalse(capture.is_alive())
        marker2 = env.tmp / "rekey-2.txt"
        rekey2 = self._spawn(
            _worker_rekey, (str(env.store_path), str(marker2)))
        rekey2.join(30)
        self.assertFalse(rekey2.is_alive())
        text = _read_marker(marker2)
        self.assertTrue(text.startswith("REFUSED active"), text)
        self.assertIn(NS_OLD, text)
        journal_now = env.journal_bytes()
        self.assertIn(b"doc_pending", journal_now)
        conn = sqlite3.connect(str(env.store_path))
        self.assertTrue(conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL "
            "AND namespace=?", (NS_OLD,)).fetchone()[0] > 0)
        conn.close()

    def test_rekey_holding_both_locks_capture_write_waits_for_release(self):
        from knowledge.documents import prepare_document
        env = self._scratch()
        # a pre-existing journal so the capture lane's write is a SECOND
        # identity (the guard still holds both locks over it)
        doc = prepare_document("notes", "run/xp-pre", "pre-existing\n")
        env.journal.record_intent(
            target_id="notes", logical_key="run/xp-pre",
            locator=doc.locator, digest=doc.digest, namespace=NS_OLD)
        ctx = _MP.get_context("spawn")
        held, release = ctx.Event(), ctx.Event()
        attempting, wrote = ctx.Event(), ctx.Event()
        ts_path = env.tmp / "times.txt"

        fence = self._spawn(
            _worker_hold_fence, (str(env.tmp), held, release))
        self.assertTrue(held.wait(30), "fence worker never held the locks")

        capture = self._spawn(
            _worker_capture_write,
            (str(env.store_path), attempting, wrote, str(ts_path)))
        self.assertTrue(attempting.wait(30),
                        "capture worker never attempted the write")

        # While both locks are held the capture's sidecar write CANNOT
        # be observable (bounded liveness probe — the ordering proof is
        # the timestamp assertion below, not this window).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.assertFalse(wrote.is_set(),
                             "capture write completed under a held fence")
            self.assertNotIn("wrote", ts_path.read_text(
                encoding="utf-8") if ts_path.exists() else "")
            time.sleep(0.05)

        release_ts = time.time()
        release.set()
        fence.join(30)
        self.assertFalse(fence.is_alive())
        self.assertTrue(wrote.wait(30),
                        "capture write never completed after release")
        capture.join(30)
        self.assertFalse(capture.is_alive())
        wrote_ts = float(ts_path.read_text(encoding="utf-8")
                         .splitlines()[-1].split()[1])
        self.assertGreaterEqual(
            wrote_ts, release_ts,
            "the capture's sidecar write became observable only after "
            "the rekey's locks released")

    def test_first_ever_capture_cannot_interleave_with_guard(self):
        env = self._scratch()  # knowledge enabled, NO knowledge/ dir yet
        self.assertFalse(env.knowledge_dir().exists())
        ctx = _MP.get_context("spawn")
        held, release = ctx.Event(), ctx.Event()
        attempting, wrote = ctx.Event(), ctx.Event()
        ts_path = env.tmp / "times.txt"

        fence = self._spawn(
            _worker_hold_fence, (str(env.tmp), held, release))
        self.assertTrue(held.wait(30), "fence worker never held the locks")

        # protocol (3): an ENABLED first-use guard creates the NORMAL
        # persistent journal+delivery lock files — that is what makes a
        # first-ever capture unable to interleave.
        self.assertTrue(
            (env.knowledge_dir() / "journal.json.lock").exists(),
            "the enabled first-use fence must create the journal lock")
        self.assertTrue(
            (env.knowledge_dir() / "delivery.json.lock").exists(),
            "the enabled first-use fence must create the delivery lock")
        self.assertFalse((env.knowledge_dir() / "journal.json").exists(),
                         "the fence must not fabricate sidecar state")

        capture = self._spawn(
            _worker_capture_write,
            (str(env.store_path), attempting, wrote, str(ts_path)),
            {"first_ever": True})
        self.assertTrue(attempting.wait(30))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.assertFalse(
                (env.knowledge_dir() / "journal.json").exists(),
                "the first-ever capture write landed under a held fence")
            time.sleep(0.05)

        release_ts = time.time()
        release.set()
        fence.join(30)
        self.assertTrue(wrote.wait(30))
        capture.join(30)
        wrote_ts = float(ts_path.read_text(encoding="utf-8")
                         .splitlines()[-1].split()[1])
        self.assertGreaterEqual(wrote_ts, release_ts)
        self.assertTrue((env.knowledge_dir() / "journal.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
