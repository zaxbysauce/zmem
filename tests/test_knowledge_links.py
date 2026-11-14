"""Knowledge pointer handler tests (PR1): CAS link operation, grammar
delegation, payload codec, and identity protection.

Covers, per the approved plan ("Required critic corrections" +
"Explicit preimplementation refinements"):
  - initial create + idempotent noop (same version, same digest -> no writes)
  - immutable-version conflict (same version, DIFFERENT digest refused)
  - CAS advance via expected_row_id: stale id / regression / vanished row
    all refused with NOTHING written; a genuine advance tombstones the old
    row and mints a fresh one in one transaction
  - transaction discipline: the handler owns BEGIN IMMEDIATE on an idle
    connection and nests in a SAVEPOINT under a caller-held transaction; a
    mid-operation failure rolls back only its own work
  - the ref grammar is DELEGATED to knowledge.documents /
    knowledge.config (the handler never restates the regexes)
  - the payload is closed-set and secret-pattern-safe (64-hex stored as
    eight 8-hex chunks: no >=32-hex run can trip the generic redactor)
  - distinct refs with IDENTICAL summaries never collapse, and an ordinary
    fact whose content equals a pointer payload never folds into the
    pointer nor links to it (reserved-ref isolation on the write path)

Run: python tests/test_knowledge_links.py   (no pytest -- repo convention)
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-knlinks-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
# Force embeddings deterministically OUT of scope (same pattern as
# test_consolidate_lossy): every dedup/link decision below exercises the
# lexical lanes, which are deterministic.
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "no-such-models")
sys.path.insert(0, str(REPO_ROOT / "tests"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)
sys.path.insert(0, str(SCRIPTS_DIR))

import storelib  # noqa: E402
from storelib import schema as _schema  # noqa: E402
from storelib import write as _write  # noqa: E402
from storelib.knowledge import (  # noqa: E402
    KnowledgePointerCasError,
    KnowledgePointerConflict,
    KnowledgePayloadError,
    KnowledgeRefError,
    build_payload,
    demote_imported_payload,
    get_pointer,
    get_pointer_rows,
    is_wellformed_pointer_ref,
    link_pointer,
    make_source_ref,
    parse_payload,
    parse_ref,
)
from knowledge import documents as _documents  # noqa: E402

SECRET_RUN_RE = re.compile(r"[0-9a-fA-F]{32,}")

TARGET = "fleet-kb"
KEY = "runbooks/deploy-window"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _ns() -> str:
    return "project:kn-links-" + uuid.uuid4().hex[:10]


def _live_rows(conn, namespace: str, source_ref: str):
    return conn.execute(
        "SELECT id, content, superseded_at, supersede_reason, tags, taint, type "
        "FROM memory WHERE namespace=? AND source_ref=? AND superseded_at IS NULL "
        "ORDER BY ingestion_ts, id",
        (namespace, source_ref),
    ).fetchall()


def _insert_raw_row(conn, *, namespace: str, content: str, source_ref: str = "",
                    tags: str = "") -> str:
    """Direct INSERT bypassing every write-path guard (store-surgery shape
    used to stage imported/corrupt copies for the handler to face)."""
    mid = str(uuid.uuid4())
    ts = _schema.now_iso()
    conn.execute(
        """INSERT INTO memory
           (id, namespace, type, content, tags, source_ref, source_hash,
            confidence, signal, valid_from, superseded_at, ingestion_ts,
            retrieval_count, last_retrieved, taint)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, namespace, "fact", content, tags, source_ref, "", 0.5, "none",
         ts, None, ts, 0, None, "untrusted_tool"),
    )
    conn.commit()
    return mid


class _StoreTestCase(unittest.TestCase):
    """One shared module store, one fresh NAMESPACE per test (everything
    under test is namespace-scoped, so this is full isolation)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _schema.connect()
        _schema.init_db(cls.conn)
        _schema.migrate(cls.conn)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:
            pass


class GrammarDelegationTest(_StoreTestCase):
    """The handler grammar IS the knowledge package grammar — no restated
    regexes that can drift (plan: shared contracts)."""

    def test_make_source_ref_matches_package_authority(self):
        self.assertEqual(
            make_source_ref(TARGET, KEY),
            _documents.logical_ref(TARGET, KEY),
        )
        self.assertEqual(make_source_ref(TARGET, KEY), f"knowledge:{TARGET}:{KEY}")

    def test_bad_target_and_key_refused(self):
        with self.assertRaises(KnowledgeRefError):
            make_source_ref("FleetKB", KEY)  # uppercase target id
        with self.assertRaises(KnowledgeRefError):
            make_source_ref(TARGET, "runbooks//deploy")  # empty segment
        with self.assertRaises(KnowledgeRefError):
            make_source_ref(TARGET, "runbooks/../etc")  # relative segment
        with self.assertRaises(KnowledgeRefError):
            parse_ref("knowledge:" + TARGET)  # no key half

    def test_credential_shaped_target_ref_refused_at_admission(self):
        """`knowledge:dev-token:runbooks/deploy` reads as ``token:<8+>`` to
        the capture credential scanner: local manual writes used to
        succeed while every auto-mode sync/import/rescan of the SAME row
        was refused (parity break). The ref grammar authority now rejects
        the assembled ref BEFORE any document create or pointer mint."""
        for bad_target in ("dev-token", "api-key", "service-secret",
                           "db-password", "passwd"):
            with self.assertRaises(KnowledgeRefError, msg=bad_target):
                make_source_ref(bad_target, KEY)
        # delta-4 short-key bypass: the bare ref scanned clean, but the
        # payload-embedded ref (","locator" glued on in compact JSON)
        # tripped the auto detector and ingest redacted the raw JSON
        for short_key in ("x", "a/b", "run"):
            with self.assertRaises(KnowledgeRefError, msg=short_key):
                make_source_ref("dev-token", short_key)
        # handler end-to-end: refused before any mutation
        ns = _ns()
        with self.assertRaises(KnowledgeRefError):
            link_pointer(
                self.conn, namespace=ns, target="dev-token",
                key="runbooks/deploy", version=1,
                full_sha256=_digest("devtoken"), summary="parity probe",
                expected_row_id=None)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) AS c FROM memory WHERE namespace=?",
                (ns,)).fetchone()["c"], 0)

    def test_wellformed_predicate(self):
        good = f"knowledge:{TARGET}:{KEY}"
        self.assertTrue(is_wellformed_pointer_ref(good))
        self.assertFalse(is_wellformed_pointer_ref(f"knowledge:{TARGET}:{KEY}:extra"))
        self.assertFalse(is_wellformed_pointer_ref(f"knowledge:{TARGET.upper()}:{KEY}"))
        self.assertFalse(is_wellformed_pointer_ref("knowledge::deploy"))
        self.assertFalse(is_wellformed_pointer_ref("card:" + TARGET + ":" + KEY))
        self.assertFalse(is_wellformed_pointer_ref(None))
        # parse_ref round-trips only the canonical form
        self.assertEqual(parse_ref(good), (TARGET, KEY))


class PayloadCodecTest(_StoreTestCase):
    """Closed-set payload, chunked digest, derived locator."""

    def test_roundtrip_and_field_set(self):
        d = _digest("payload-roundtrip")
        payload = build_payload(
            target=TARGET, key=KEY, version=3, full_sha256=d,
            summary="deploy window runbook v3",
        )
        parsed = parse_payload(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(
            set(parsed),
            {"logical_id", "version", "locator", "full_sha256", "summary",
             "date", "verified"},
        )
        self.assertEqual(parsed["version"], 3)
        self.assertEqual(parsed["full_sha256"], d)
        self.assertTrue(parsed["verified"])
        self.assertEqual(
            parsed["locator"], _documents.version_locator(KEY, d))
        self.assertEqual(parsed["logical_id"], f"knowledge:{TARGET}:{KEY}")
        # stored bytes carry the chunked digest, never a 64-hex run
        self.assertNotIn(d, payload)
        self.assertIsNone(SECRET_RUN_RE.search(payload))
        # and the generic secret redactor finds nothing to redact
        redacted, hits = _write.redact_text(payload)
        self.assertEqual(hits, 0)
        self.assertEqual(redacted, payload)

    def test_closed_field_set_enforced_on_parse(self):
        d = _digest("payload-closed")
        payload = build_payload(
            target=TARGET, key=KEY, version=1, full_sha256=d, summary="s")
        obj = json.loads(payload)
        extra = dict(obj, note="extra")
        self.assertIsNone(parse_payload(json.dumps(extra)))
        missing = dict(obj)
        missing.pop("summary")
        self.assertIsNone(parse_payload(json.dumps(missing)))
        # raw 64-hex fullhash (not chunks) is not the closed shape
        raw = dict(obj, fullhash=[d[:8]] * 8)  # 8 chunks but all identical
        self.assertIsNone(parse_payload(json.dumps(raw)))
        self.assertIsNone(parse_payload("not json"))
        self.assertIsNone(parse_payload(""))

    def test_locator_argument_must_match_derived(self):
        d = _digest("payload-locator")
        with self.assertRaises(KnowledgePayloadError):
            build_payload(
                target=TARGET, key=KEY, version=1, full_sha256=d,
                summary="s", locator=KEY + "/deadbeef.md",
            )

    def test_demote_imported_payload(self):
        d = _digest("payload-demote")
        verified = build_payload(
            target=TARGET, key=KEY, version=2, full_sha256=d, summary="s")
        demoted = demote_imported_payload(verified)
        self.assertIsNotNone(demoted)
        parsed = parse_payload(demoted)
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed["verified"])
        # everything else byte-stable
        self.assertEqual(parsed["version"], 2)
        self.assertEqual(parsed["full_sha256"], d)
        # already-unverified and garbage stay as-is (None)
        self.assertIsNone(demote_imported_payload(demoted))
        self.assertIsNone(demote_imported_payload("garbage"))

    def test_summary_secret_shapes_rejected_count_only(self):
        """The summary is authored text riding the payload into the store —
        and link_pointer writes with capture_mode=manual (advisory-only, no
        downstream redaction/refusal), so the FIELD VALIDATOR must screen it
        with the same real capture-pattern scanner the document body faces.
        A detection rejects (never silently redacts); the message carries
        the count only, never the value."""
        d = _digest("payload-summary-screen")
        samples = (
            ("api_key=abcdEFGH1234", "abcdEFGH1234"),
            ("TOKEN: supersecretvalue1", "supersecretvalue1"),
            ("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
             "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
        )
        for bad_summary, secret_value in samples:
            with self.assertRaises(KnowledgePayloadError, msg=bad_summary[:20]) as ctx:
                build_payload(target=TARGET, key=KEY, version=1,
                              full_sha256=d, summary=bad_summary)
            # count-only refusal: the pattern count is present, the value is not
            self.assertIn("secret-like pattern", str(ctx.exception))
            self.assertNotIn(secret_value, str(ctx.exception))
        # a hand-forged stored payload with a secret summary is not parseable
        # (parse_payload routes through the same validator), so it can never
        # surface as an authoritative pointer
        obj = json.loads(build_payload(
            target=TARGET, key=KEY, version=1, full_sha256=d,
            summary="clean summary text"))
        obj["summary"] = "api_key=abcdEFGH1234"
        self.assertIsNone(parse_payload(json.dumps(obj)))
        # a clean summary still builds and round-trips
        payload = build_payload(
            target=TARGET, key=KEY, version=1, full_sha256=d,
            summary="deploy window runbook")
        self.assertIsNotNone(parse_payload(payload))

    def test_link_pointer_refuses_secret_summary_nothing_written(self):
        """End-to-end guard: the handler refuses to mint (or advance to) a
        pointer whose summary carries a secret-shaped token — pre-fix the
        row landed raw (manual capture never redacts) and the value sat in
        the store forever."""
        ns = _ns()
        with self.assertRaises(KnowledgePayloadError):
            link_pointer(
                self.conn, namespace=ns, target=TARGET,
                key="runbooks/leaky", version=1,
                full_sha256=_digest("leaky"),
                summary="deploy token api_key=abcdEFGH1234 in summary",
                expected_row_id=None)
        rows = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=?",
            (ns,)).fetchone()["c"]
        self.assertEqual(rows, 0)  # refused BEFORE any mutation


class LinkPointerCasTest(_StoreTestCase):
    """The conditional pointer operation: initial create, noop, immutable
    conflict, CAS advance, stale/lost races, transaction discipline."""

    def setUp(self):
        self.ns = _ns()
        self.ref = make_source_ref(TARGET, KEY)
        self.d1 = _digest(self.ns + "-v1")
        self.d2 = _digest(self.ns + "-v2")

    def _link(self, **overrides):
        kwargs = dict(
            conn=self.conn, namespace=self.ns, target=TARGET, key=KEY,
            version=1, full_sha256=self.d1, summary="deploy runbook",
            expected_row_id=None,
        )
        kwargs.update(overrides)
        return link_pointer(**kwargs)

    def _current_row_id(self) -> str:
        rows = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(rows), 1)
        return rows[0]["id"]

    def test_initial_create_mints_verified_pointer_row(self):
        out = self._link()
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["replaced_unverified"], 0)
        # the handler owned the transaction and left the connection idle
        self.assertFalse(self.conn.in_transaction)
        row = _live_rows(self.conn, self.ns, self.ref)[0]
        self.assertEqual(row["type"], "fact")       # no new type
        self.assertEqual(row["tags"], "knowledge")  # structural tag
        self.assertEqual(row["taint"], "untrusted_tool")
        self.assertEqual(row["id"], out["row_id"])
        ptr = get_pointer(self.conn, self.ns, TARGET, KEY)
        self.assertIsNotNone(ptr)
        self.assertEqual(ptr.row_id, out["row_id"])
        self.assertEqual(ptr.version, 1)
        self.assertEqual(ptr.full_sha256, self.d1)
        self.assertEqual(ptr.locator, _documents.version_locator(KEY, self.d1))
        self.assertTrue(self.conn.in_transaction is False)

    def test_initial_create_refused_when_verified_pointer_exists(self):
        first = self._link()
        with self.assertRaises(KnowledgePointerCasError):
            self._link(full_sha256=_digest(self.ns + "-other"))
        # nothing written by the refusal
        rows = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first["row_id"])

    def test_noop_same_version_same_digest(self):
        first = self._link()
        out = self._link(expected_row_id=first["row_id"])
        self.assertEqual(out["status"], "noop")
        self.assertEqual(out["row_id"], first["row_id"])
        self.assertEqual(out["previous_row_id"], first["row_id"])
        self.assertEqual(len(_live_rows(self.conn, self.ns, self.ref)), 1)

    def test_immutable_conflict_same_version_different_digest(self):
        first = self._link()
        with self.assertRaises(KnowledgePointerConflict):
            self._link(expected_row_id=first["row_id"],
                       full_sha256=self.d2)
        rows = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(rows), 1)
        parsed = parse_payload(rows[0]["content"])
        self.assertEqual(parsed["full_sha256"], self.d1)  # unchanged

    def test_advance_tombstones_old_row_and_mints_new(self):
        first = self._link()
        out = self._link(version=2, full_sha256=self.d2,
                         expected_row_id=first["row_id"])
        self.assertEqual(out["status"], "advanced")
        self.assertEqual(out["previous_row_id"], first["row_id"])
        self.assertNotEqual(out["row_id"], first["row_id"])
        # exactly one live row; the old one tombstoned with the CAS reason
        live = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["id"], out["row_id"])
        old = self.conn.execute(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?",
            (first["row_id"],),
        ).fetchone()
        self.assertIsNotNone(old["superseded_at"])
        self.assertIn("CAS advance", old["supersede_reason"])
        ptr = get_pointer(self.conn, self.ns, TARGET, KEY)
        self.assertEqual(ptr.version, 2)
        self.assertEqual(ptr.full_sha256, self.d2)

    def test_stale_expected_row_id_rejected_nothing_written(self):
        first = self._link()
        with self.assertRaises(KnowledgePointerCasError):
            self._link(version=2, full_sha256=self.d2,
                       expected_row_id=str(uuid.uuid4()))
        rows = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(rows), 1)
        self.assertEqual(parse_payload(rows[0]["content"])["version"], 1)

    def test_stale_advance_when_pointer_vanished(self):
        first = self._link()
        # store surgery: the pointer row is gone (tombstoned elsewhere)
        self.conn.execute(
            "UPDATE memory SET superseded_at=? WHERE id=?",
            (_schema.now_iso(), first["row_id"]),
        )
        self.conn.commit()
        with self.assertRaises(KnowledgePointerCasError):
            self._link(version=2, full_sha256=self.d2,
                       expected_row_id=first["row_id"])

    def test_version_regression_rejected(self):
        first = self._link()
        self._link(version=2, full_sha256=self.d2,
                   expected_row_id=first["row_id"])
        second = self._current_row_id()
        with self.assertRaises(KnowledgePointerCasError):
            self._link(version=1, full_sha256=self.d1,
                       expected_row_id=second)
        self.assertEqual(
            parse_payload(_live_rows(self.conn, self.ns, self.ref)[0]["content"])["version"],
            2,
        )

    def test_initial_create_replaces_unverified_imported_copies(self):
        imported = _insert_raw_row(
            self.conn, namespace=self.ns,
            content=build_payload(target=TARGET, key=KEY, version=7,
                                  full_sha256=_digest(self.ns + "-forgery"),
                                  summary="imported copy", verified=False),
            source_ref=self.ref, tags="knowledge",
        )
        out = self._link()
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["replaced_unverified"], 1)
        self.assertEqual(len(_live_rows(self.conn, self.ns, self.ref)), 1)
        gone = self.conn.execute(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?",
            (imported,),
        ).fetchone()
        self.assertIsNotNone(gone["superseded_at"])
        self.assertIn("relink", gone["supersede_reason"])

    def test_advance_also_replaces_unverified_copies(self):
        first = self._link()
        _insert_raw_row(
            self.conn, namespace=self.ns,
            content=build_payload(target=TARGET, key=KEY, version=9,
                                  full_sha256=_digest(self.ns + "-forgery2"),
                                  summary="imported copy 2", verified=False),
            source_ref=self.ref, tags="knowledge",
        )
        out = self._link(version=2, full_sha256=self.d2,
                         expected_row_id=first["row_id"])
        self.assertEqual(out["status"], "advanced")
        self.assertEqual(out["replaced_unverified"], 1)
        self.assertEqual(len(_live_rows(self.conn, self.ns, self.ref)), 1)

    def test_two_verified_live_rows_is_refused_corruption(self):
        first = self._link()
        _insert_raw_row(
            self.conn, namespace=self.ns,
            content=build_payload(target=TARGET, key=KEY, version=5,
                                  full_sha256=_digest(self.ns + "-corrupt"),
                                  summary="corrupt duplicate"),
            source_ref=self.ref, tags="knowledge",
        )
        with self.assertRaises(KnowledgePointerConflict):
            get_pointer(self.conn, self.ns, TARGET, KEY)
        with self.assertRaises(KnowledgePointerConflict):
            self._link(version=2, full_sha256=self.d2,
                       expected_row_id=first["row_id"])

    def test_get_pointer_rows_reports_unverified_copies(self):
        first = self._link()
        _insert_raw_row(
            self.conn, namespace=self.ns,
            content=build_payload(target=TARGET, key=KEY, version=9,
                                  full_sha256=_digest(self.ns + "-copy"),
                                  summary="inert copy", verified=False),
            source_ref=self.ref, tags="knowledge",
        )
        rows = get_pointer_rows(self.conn, self.ns, TARGET, KEY)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(r["verified"] for r in rows), [False, True])
        self.assertEqual(
            [r["row_id"] for r in rows if r["verified"]][0], first["row_id"])

    # --- transaction discipline -------------------------------------------
    def test_savepoint_success_under_caller_transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            out = self._link()
            self.assertEqual(out["status"], "created")
            self.assertTrue(self.conn.in_transaction)  # caller tx untouched
        finally:
            self.conn.commit()
        self.assertEqual(len(_live_rows(self.conn, self.ns, self.ref)), 1)

    def test_savepoint_failure_isolates_caller_work(self):
        seed = _insert_raw_row(self.conn, namespace=self.ns,
                               content="caller-owned work")
        self.conn.execute("BEGIN IMMEDIATE")
        # a stale CAS inside the caller's transaction must roll back only
        # the handler's savepoint
        with self.assertRaises(KnowledgePointerCasError):
            self._link(version=2, full_sha256=self.d2,
                       expected_row_id=str(uuid.uuid4()))
        self.assertTrue(self.conn.in_transaction)  # caller tx still alive
        # caller keeps writing and commits its own work
        self.conn.execute(
            "UPDATE memory SET retrieval_count=1 WHERE id=?", (seed,))
        self.conn.commit()
        self.assertFalse(self.conn.in_transaction)
        kept = self.conn.execute(
            "SELECT retrieval_count FROM memory WHERE id=?", (seed,)).fetchone()
        self.assertEqual(kept["retrieval_count"], 1)
        # and no pointer row leaked from the refused operation
        self.assertEqual(len(_live_rows(self.conn, self.ns, self.ref)), 0)

    def test_owned_transaction_rolls_back_on_mid_operation_failure(self):
        first = self._link()
        with mock.patch.object(
                _write, "add_memory", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self._link(version=2, full_sha256=self.d2,
                           expected_row_id=first["row_id"])
        # the BEGIN IMMEDIATE rollback restored the tombstone the handler
        # had already written before the failure
        self.assertFalse(self.conn.in_transaction)
        rows = _live_rows(self.conn, self.ns, self.ref)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first["row_id"])
        self.assertIsNone(rows[0]["superseded_at"])
        # and no half-written successor row exists anywhere
        n = self.conn.execute(
            "SELECT count(*) AS c FROM memory WHERE namespace=? AND source_ref=?",
            (self.ns, self.ref),
        ).fetchone()["c"]
        self.assertEqual(n, 1)


class DistinctIdentityTest(_StoreTestCase):
    """Stable logical identity vs immutable versions: distinct refs with
    IDENTICAL summaries never collapse, and the reserved prefix keeps
    ordinary dedup/link lanes away from pointer rows."""

    def setUp(self):
        self.ns = _ns()

    def test_identical_summaries_distinct_refs_no_collapse(self):
        summary = "identical summary text for two distinct logical refs"
        a = link_pointer(
            self.conn, namespace=self.ns, target=TARGET, key="runbooks/a",
            version=1, full_sha256=_digest("a"), summary=summary,
            expected_row_id=None)
        b = link_pointer(
            self.conn, namespace=self.ns, target=TARGET, key="runbooks/b",
            version=1, full_sha256=_digest("b"), summary=summary,
            expected_row_id=None)
        self.assertEqual(a["status"], "created")
        self.assertEqual(b["status"], "created")  # no dedup fold, no refusal
        ptr_a = get_pointer(self.conn, self.ns, TARGET, "runbooks/a")
        ptr_b = get_pointer(self.conn, self.ns, TARGET, "runbooks/b")
        self.assertIsNotNone(ptr_a)
        self.assertIsNotNone(ptr_b)
        self.assertNotEqual(ptr_a.row_id, ptr_b.row_id)

    def test_ordinary_fact_with_pointer_payload_never_folds_or_links(self):
        ptr = link_pointer(
            self.conn, namespace=self.ns, target=TARGET, key=KEY,
            version=1, full_sha256=_digest("payload-clone"),
            summary="pointer payload clone probe",
            expected_row_id=None)
        payload = self.conn.execute(
            "SELECT content FROM memory WHERE id=?", (ptr["row_id"],)
        ).fetchone()["content"]

        # 1) dedup lane: an ordinary fact whose content is EXACTLY the
        #    payload lands as its own row; the pointer is not a keeper
        fact = _write.add_memory(
            self.conn, namespace=self.ns, type_="fact", content=payload,
            signal="test")
        self.assertNotEqual(str(fact), ptr["row_id"])
        # a second identical fact folds into the FIRST FACT (dedup active),
        # never into the pointer
        _write.add_memory(
            self.conn, namespace=self.ns, type_="fact", content=payload,
            signal="test")
        live_payload_rows = self.conn.execute(
            "SELECT id FROM memory WHERE namespace=? AND content=? "
            "AND superseded_at IS NULL",
            (self.ns, payload),
        ).fetchall()
        self.assertEqual(len(live_payload_rows), 2)  # pointer + one fact

        # 2) link lane (lexical, Jaccard 1.0): no edge touches the pointer
        edges = self.conn.execute(
            "SELECT count(*) AS c FROM memory_link "
            "WHERE src_id=? OR dst_id=?", (ptr["row_id"], ptr["row_id"]),
        ).fetchone()["c"]
        self.assertEqual(edges, 0)
        # and the pointer's structural tags were never unioned
        tags = self.conn.execute(
            "SELECT tags FROM memory WHERE id=?", (ptr["row_id"],)
        ).fetchone()["tags"]
        self.assertEqual(tags, "knowledge")

        # control: the lexical link lane IS active in this store — a
        # near-identical ordinary pair in a fresh namespace does link
        ctl_ns = _ns()
        base = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        _write.add_memory(self.conn, namespace=ctl_ns, type_="fact",
                          content=base, signal="test")
        _write.add_memory(self.conn, namespace=ctl_ns, type_="fact",
                          content=base + " lambda mu", signal="test")
        ctl_edges = self.conn.execute(
            "SELECT count(*) AS c FROM memory_link l JOIN memory m "
            "ON (l.src_id=m.id OR l.dst_id=m.id) WHERE m.namespace=?",
            (ctl_ns,),
        ).fetchone()["c"]
        self.assertGreater(ctl_edges, 0)


class OpsReplayPointerIsolationTest(_StoreTestCase):
    """FB-017/SEC-005: the ops-lane identical-content replay fold
    (_identical_content_row, issue #363) must never fold an ordinary
    add/capture onto a knowledge POINTER row — the fold receipt would name
    the pointer row as the add's canonical id. Ordinary-to-ordinary
    identical adds still fold (the control)."""

    def setUp(self):
        self.ns = _ns()
        self.ptr_digest = _digest(self.ns + "-replay-isolation")

    def _pointer_payload(self) -> str:
        return self.conn.execute(
            "SELECT content FROM memory WHERE id=?",
            (self.ptr_row_id,),
        ).fetchone()["content"]

    def test_ordinary_add_op_never_folds_onto_pointer_row(self):
        from storelib import ops as opslib
        from storelib.ops import LegacyPrincipal

        created = link_pointer(
            self.conn, namespace=self.ns, target=TARGET, key=KEY,
            version=1, full_sha256=self.ptr_digest,
            summary="replay isolation probe", expected_row_id=None)
        self.ptr_row_id = created["row_id"]
        payload = self._pointer_payload()

        # An ordinary add op whose content is byte-identical to the
        # pointer payload: with the pre-fix plain WHERE namespace/content
        # lookup this folded (committed, deduped, canonical_id = the
        # POINTER row). The exclusion makes it a normal fresh add.
        batch = opslib.apply_ops(self.conn, [
            {"op_id": "probe-add-1-" + uuid.uuid4().hex[:8], "op": "add",
             "args": {"namespace": self.ns, "type": "fact",
                      "content": payload}},
        ], principal="", default_principal=LegacyPrincipal())
        receipt = batch["receipts"][0]
        self.assertEqual(receipt["status"], "committed", receipt)
        self.assertNotEqual(receipt["canonical_id"], self.ptr_row_id,
                            "the replay fold must never name the pointer "
                            "row as an ordinary add's canonical id")
        self.assertFalse(receipt.get("deduped"), receipt)
        # A distinct live fact row exists alongside the untouched pointer.
        live = self.conn.execute(
            "SELECT id, source_ref FROM memory WHERE namespace=? AND "
            "content=? AND superseded_at IS NULL",
            (self.ns, payload)).fetchall()
        self.assertEqual(len(live), 2, live)  # pointer + one ordinary fact
        facts = [r for r in live if r["id"] != self.ptr_row_id]
        self.assertEqual(facts[0]["id"], receipt["canonical_id"])
        self.assertEqual(facts[0]["source_ref"], "")
        ptr = get_pointer(self.conn, self.ns, TARGET, KEY)
        self.assertEqual(ptr.row_id, self.ptr_row_id)
        self.assertEqual(ptr.version, 1)
        self.assertEqual(ptr.full_sha256, self.ptr_digest)

        # CONTROL: a second byte-identical ordinary add op still folds —
        # onto the FIRST FACT row, never onto the pointer.
        batch2 = opslib.apply_ops(self.conn, [
            {"op_id": "probe-add-2-" + uuid.uuid4().hex[:8], "op": "add",
             "args": {"namespace": self.ns, "type": "fact",
                      "content": payload}},
        ], principal="", default_principal=LegacyPrincipal())
        receipt2 = batch2["receipts"][0]
        self.assertEqual(receipt2["status"], "committed", receipt2)
        self.assertTrue(receipt2.get("deduped"), receipt2)
        self.assertEqual(receipt2["canonical_id"], receipt["canonical_id"],
                         "ordinary-to-ordinary identical adds still fold")
        self.assertNotEqual(receipt2["canonical_id"], self.ptr_row_id)
        # and the pointer is still exactly one authoritative row
        self.assertEqual(len(_live_rows(
            self.conn, self.ns, make_source_ref(TARGET, KEY))), 1)


class TypedOpJsonlFramingTest(unittest.TestCase):
    """The typed-op JSONL framing law: records are delimited by LITERAL LF
    only.  U+2028/U+2029/U+0085 are legal unescaped inside JSON strings,
    and ``str.splitlines()`` tears on them — a knowledge_link
    ``summary`` (or any payload string) carrying one must round-trip whole
    through the real ``apply-ops`` CLI reader (MIC-INPUT-003 pin,
    2026-11-14)."""

    SEP_LINE = "\u2028"  # LINE SEPARATOR
    SEP_PARA = "\u2029"  # PARAGRAPH SEPARATOR
    SEP_NEL = "\u0085"   # NEXT LINE

    def _apply_ops_env(self, tmp: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
            "ZMEM_DATA": tmp,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
            "PYTHONIOENCODING": "utf-8",
        })
        return env

    def test_line_separators_never_tear_typed_op_records(self):
        import hashlib as _hashlib

        tmp = tempfile.mkdtemp(prefix="zmem-knjsonl-")
        store = os.path.join(tmp, "store.sqlite")
        env = self._apply_ops_env(tmp)
        init = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "store.py"), "init"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=180)
        self.assertEqual(init.returncode, 0, init.stderr[-400:])

        separators = (self.SEP_LINE + self.SEP_PARA + self.SEP_NEL)
        summary_with_seps = ("unicode" + self.SEP_LINE + "separators"
                             + self.SEP_PARA + "stay" + self.SEP_NEL
                             + "intact")
        body_with_seps = ("body with" + self.SEP_LINE + "line"
                          + self.SEP_PARA + "para" + self.SEP_NEL + "nel")
        # One add op whose CONTENT and one knowledge_link op whose SUMMARY
        # carry the separators — written with ensure_ascii=False so
        # they are RAW in the JSONL line (the worst-case writer; the
        # in-repo writers use the default ensure_ascii=True, which escapes
        # them).
        digest = _hashlib.sha256(b"framing-pin").hexdigest()
        chunks = [digest[i * 8:(i + 1) * 8] for i in range(8)]
        locator = "%s/%s.md" % (KEY, "-".join(
            digest[i:i + 16] for i in range(0, 64, 16)))
        ops = [
            {"op_id": "kl-frame-add", "op": "add",
             "args": {"namespace": "user:global", "type": "fact",
                      "content": body_with_seps}},
            {"op_id": "kl-frame-link", "op": "knowledge_link",
             "args": {"namespace": "user:global", "target": TARGET,
                      "key": KEY, "version": 1, "locator": locator,
                      "fullhash": chunks,
                      "summary": summary_with_seps,
                      "date": "2026-11-14T00:00:00Z",
                      "expected_row_id": None}},
        ]
        ops_path = Path(tmp) / "ops.jsonl"
        ops_path.write_text(
            "".join(json.dumps(op, ensure_ascii=False) + "\n"
                    for op in ops),
            encoding="utf-8", newline="")
        # the file really carries the raw separators (the tear precondition)
        raw = ops_path.read_text(encoding="utf-8")
        for sep in (self.SEP_LINE, self.SEP_PARA, self.SEP_NEL):
            self.assertIn(sep, raw)
            self.assertNotIn(sep.encode("unicode_escape").decode(), raw)

        run = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "store.py"),
             "apply-ops", "--in", str(ops_path), "--json"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(run.returncode, 0,
                         "a U+2028/U+2029/U+0085 payload must not tear "
                         "the JSONL record: %r %r"
                         % (run.stdout[-300:], run.stderr[-300:]))
        result = json.loads(run.stdout.splitlines()[-1])
        statuses = {r["op_id"]: r["status"] for r in result["receipts"]}
        self.assertEqual(statuses["kl-frame-add"], "committed", result)
        self.assertEqual(statuses["kl-frame-link"], "committed", result)
        # Round-trip: the stored pointer summary kept the separators.
        conn = sqlite3.connect(store)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT content FROM memory WHERE source_ref=? AND "
                "superseded_at IS NULL", (make_source_ref(TARGET, KEY),)
            ).fetchone()
            self.assertIsNotNone(row)
            parsed = parse_payload(row["content"])
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["summary"], summary_with_seps)
            fact = conn.execute(
                "SELECT content FROM memory WHERE id=?",
                ([r for r in result["receipts"]
                  if r["op_id"] == "kl-frame-add"][0]["canonical_id"],)
            ).fetchone()
            self.assertEqual(fact["content"], body_with_seps)
        finally:
            conn.close()

    def test_writer_ascii_escaping_needs_no_raw_separators(self):
        r"""Pin the writer half of the law: the in-repo batch writer
        (ops-flush's producer) uses json.dumps' default ensure_ascii=True,
        which escapes U+2028/U+2029/U+0085 as \uXXXX — it can never
        emit a raw separator into a JSONL line, and the escaped form
        round-trips through the literal-LF reader unchanged."""
        body = ("kept" + self.SEP_LINE + "escaped" + self.SEP_PARA
                + "as" + self.SEP_NEL + "text")
        op = {"op_id": "x", "op": "add",
              "args": {"content": body}}
        line = json.dumps(op)  # the writer's exact call shape
        for sep in (self.SEP_LINE, self.SEP_PARA, self.SEP_NEL):
            self.assertNotIn(sep, line)
        # the six-character ESCAPED forms are what the line carries
        self.assertIn("\\u2028", line)
        self.assertIn("\\u2029", line)
        self.assertIn("\\u0085", line)
        self.assertEqual(json.loads(line)["args"]["content"], body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
