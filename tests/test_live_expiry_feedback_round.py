"""Live-expiry feedback-round regression tests (PR #399 review round).

Covers the review findings the frozen acceptance checks cannot reach,
without touching the frozen files: the belief-head PARTIAL-read fail-open
regression (a later chunk failing must withhold the head, never stamp from
the readable subset — review R02/PRR-001/mutant M04), the annotate
read-receipt law (side-row-less rows from SUCCESSFUL chunks stay live —
R01/PRR-002), the _expiry_filter marker branch (mutant M03), the #344
candidate-population counting law (R10: expired counts only ids inside the
candidate population), the #349 fail-closed law itself (a garbage
inject_until is withheld — G34), the recent lane under a failed read
(R32), the expiry_read_failed emission seam (R29/PRR-008), the Hermes
fallback mirror pin (R20), the doctor per-leg guard (R35: a second-scan
failure preserves the first scan's rows), and the empty-deadline
doctor/gate alignment (R06/PRR-004).

Isolation contract (the tests/test_live_expiry.py discipline): every test
pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR at a fresh scratch and
ZMEM_MODEL_AUTODOWNLOAD=0 BEFORE importing storelib, purges
store/storelib from sys.modules in setUp AND tearDown, restores the
ambient env, and pins ZMEM_TEST_NOW only on clock-sensitive read
subprocesses. Runs standalone or via
`python -m unittest tests.test_live_expiry_feedback_round`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

NOW = "2026-10-23T12:00:00Z"
LONG_DEAD = "2020-01-01T00:00:00Z"
FAR_LIVE = "2027-06-01T00:00:00Z"
INGEST_BASE = "2026-01-01T00:00:00Z"

MEM_INSERT = (
    "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
    "source_hash, confidence, signal, valid_from, superseded_at, "
    "ingestion_ts, retrieval_count, last_retrieved, surfaced_count, "
    "last_surfaced, merged_from, content_norm, valid_until, update_of, "
    "taint, trust_score, applied_count, violated_count, embedding, "
    "embedding_model, embedded_at, consolidated_at, supersede_reason) "
    "VALUES (?, ?, 'state', ?, '', '', '', 0.9, 'test', ?, NULL, ?, "
    "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', 1.0, 0, 0, "
    "NULL, '', NULL, NULL, '')")


class FeedbackRoundBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-expiry-feedback-"))
        self._saved_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("ZMEM_"):
                os.environ.pop(key)
        os.environ.update(self._isolation_env(self.tmp))
        self._purge()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._purge()

    @staticmethod
    def _purge():
        for mod in list(sys.modules):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]

    @staticmethod
    def _isolation_env(store_dir: Path, now: str | None = None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
        env.update({
            "ZMEM_STORE": str(store_dir / "store.sqlite"),
            "ZMEM_DATA": str(store_dir),
            "ZMEM_MODELS_DIR": str(store_dir / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        if now is not None:
            env["ZMEM_TEST_NOW"] = now
        return env

    def fresh_conn(self, store_dir: Path) -> sqlite3.Connection:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.schema as schema
        conn = sqlite3.connect(str(store_dir / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        return conn

    def seed_state(self, conn, mid, ns, content, inject_until=None,
                   cls="endpoint"):
        conn.execute(MEM_INSERT, (mid, ns, content, INGEST_BASE, INGEST_BASE))
        if inject_until is not None:
            conn.execute(
                "INSERT INTO memory_expiry (memory_id, inject_until, "
                "expiry_class) VALUES (?, ?, ?)", (mid, inject_until, cls))

    def seed_belief_head(self, conn, head_id, ns, content, source_ids):
        conn.execute(
            "INSERT INTO belief_head (id, namespace, topic_identity, "
            "content, head_state, head_source_id, support_count, "
            "refresh_watermark, generator_revision, confidence, signal, "
            "taint, trust_score) VALUES (?, ?, ?, ?, 'active', ?, ?, "
            "'2026-01-01T00:00:00Z', 'belief-heads-v1', 0.9, 'test', "
            "'trusted_internal', 1.0)",
            (head_id, ns, "topic-" + head_id[:8], content, source_ids[0],
             len(source_ids)))
        for src in source_ids:
            conn.execute(
                "INSERT INTO belief_head_source (head_id, source_id, role, "
                "source_ingestion_ts, source_checksum) "
                "VALUES (?, ?, 'source', '2026-01-01T00:00:00Z', '')",
                (head_id, src))

    def run_cli(self, store_dir: Path, *args, now=None):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, timeout=180,
            env=self._isolation_env(store_dir, now=now))


class FlakyExpiryConn:
    """Proxy connection: every query succeeds EXCEPT the memory_expiry
    SELECTs from the Nth call on — simulates a lock acquired mid-annotate
    without failing the tier fetches (whose SQL never mentions the side
    table)."""

    def __init__(self, conn, fail_from_call: int):
        self._conn = conn
        self._calls = 0
        self._fail_from = fail_from_call

    def execute(self, sql, params=None):
        self._calls += 1
        if (self._calls >= self._fail_from
                and "memory_expiry" in sql.lower()):
            raise sqlite3.OperationalError("database is locked")
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class NeverExpiryConn:
    """Proxy connection: EVERY memory_expiry query raises (a lane whose
    side-table reads never succeed), while all other queries pass through
    to the real connection."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        if "memory_expiry" in sql.lower():
            raise sqlite3.OperationalError("database is locked")
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _NoSideTableConn:
    """Stub whose side-table query raises the pre-v15 absence error, so
    the annotate call inside the unit-level gate legs is a no-op (the rows
    carry their markers/stamps from the test)."""

    def execute(self, sql, params=None):
        raise sqlite3.OperationalError("no such table: memory_expiry")


class AnnotateReadReceiptTest(FeedbackRoundBase):
    """R01/PRR-002: `found` is a side-row map, not a read receipt — rows
    from SUCCESSFULLY read chunks that merely have no side row stay LIVE
    when a later chunk fails; only never-read rows are marked."""

    def test_annotate_read_receipt_keeps_read_rows_live(self):
        d = self.tmp / "receipt"
        d.mkdir()
        conn = self.fresh_conn(d)
        ns = "project:expiry-fb-receipt"
        # 600 rows: row 1 carries a far-future side row; rows 2..600 have
        # NO side row (live by the missing-side-row law). Chunk 2's read
        # (rows 501..600) fails.
        ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 601)]
        for i, mid in enumerate(ids, start=1):
            self.seed_state(
                conn, mid, ns, f"fb-receipt row {i}",
                inject_until=FAR_LIVE if i == 1 else None)
        conn.commit()
        conn.close()

        from storelib.recall import _annotate_expiry
        rows = [{"id": mid} for mid in ids]
        proxy_conn = sqlite3.connect(str(d / "store.sqlite"))
        proxy_conn.row_factory = sqlite3.Row
        _annotate_expiry(FlakyExpiryConn(proxy_conn, fail_from_call=2),
                         rows)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[ids[0]].get("inject_until"), FAR_LIVE,
                         "a side row read by a successful chunk keeps its "
                         "stamp (issue #342)")
        for mid in ids[1:500]:
            r = by_id[mid]
            self.assertIsNone(r.get("inject_until"),
                              f"{mid}: read-but-side-row-less stays LIVE")
            self.assertNotIn("_expiry_unread", r,
                             f"{mid}: a successfully read row must NOT be "
                             "marked unread (the read-receipt law)")
        for mid in ids[500:]:
            r = by_id[mid]
            self.assertTrue(r.get("_expiry_unread"),
                            f"{mid}: a never-read row must be marked")
            self.assertNotIn("inject_until", r)


class BeliefPartialReadTest(FeedbackRoundBase):
    """R02/PRR-001/mutant M04: a head whose expired source sat in a FAILED
    chunk must be withheld (marked unread), never stamped from the readable
    subset and delivered."""

    NS = "project:expiry-fb-belief"

    def _seed(self, store_dir: Path):
        conn = self.fresh_conn(store_dir)
        srcs = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 601)]
        for i, sid in enumerate(srcs, start=1):
            # sources 1..500 LIVE (chunk 1), source 600 EXPIRED (chunk 2)
            self.seed_state(conn, sid, self.NS, f"fb-belief src {i}",
                            inject_until=FAR_LIVE if i <= 500 else LONG_DEAD)
        head = "33333333-3333-4333-8333-333333333333"
        self.seed_belief_head(conn, head, self.NS,
                              "fb-belief head gamma", srcs)
        conn.commit()
        conn.close()
        return head, srcs

    def test_partial_source_read_marks_head_unread(self):
        d = self.tmp / "belief-partial"
        d.mkdir()
        head, srcs = self._seed(d)
        from storelib.recall import _annotate_virtual_expiry, _expiry_filter
        row = {"id": "belief:" + head, "source_ids": srcs}
        proxy_conn = sqlite3.connect(str(d / "store.sqlite"))
        proxy_conn.row_factory = sqlite3.Row
        _annotate_virtual_expiry(FlakyExpiryConn(proxy_conn,
            fail_from_call=2), [row])
        self.assertTrue(
            row.get("_expiry_unread"),
            "a head with a never-read source has UNKNOWN state and must be "
            "marked, never stamped from the readable subset (the stamp from "
            "chunk 1's live sources delivered this head at the reviewed "
            "head — executed repro)")
        self.assertNotIn("inject_until", row)
        live, removed = _expiry_filter([row], now_iso=NOW)
        self.assertEqual(removed, 1, "the gate must withhold the head")
        self.assertEqual(live, [])

    def test_full_read_still_stamps_earliest(self):
        d = self.tmp / "belief-full"
        d.mkdir()
        head, srcs = self._seed(d)
        from storelib.recall import _annotate_virtual_expiry, _expiry_filter
        row = {"id": "belief:" + head, "source_ids": srcs}
        full_conn = sqlite3.connect(str(d / "store.sqlite"))
        full_conn.row_factory = sqlite3.Row
        _annotate_virtual_expiry(full_conn, [row])
        self.assertEqual(row.get("inject_until"), LONG_DEAD,
                         "a fully-read head stamps the earliest source "
                         "deadline (the expired source in chunk 2 wins)")
        live, removed = _expiry_filter([row], now_iso=NOW)
        self.assertEqual(removed, 1)

    def test_empty_sibling_stamp_does_not_defeat_expired_source(self):
        """Final-critic repro: a hand-planted EMPTY source stamp must not
        win min() and read the head as live — the gate reads '' as live,
        so a head stamped '' with an expired sibling source would be
        DELIVERED (AC5 mixed-source violation). The empty stamp is
        excluded from the competition; the expired sibling wins."""
        d = self.tmp / "belief-empty"
        d.mkdir()
        conn = self.fresh_conn(d)
        ns = self.NS
        s1 = "00000000-0000-4000-8000-00000000fc01"
        s2 = "00000000-0000-4000-8000-00000000fc02"
        self.seed_state(conn, s1, ns, "fb empty-stamp source")
        conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, "
            "expiry_class) VALUES (?, '', 'endpoint')", (s1,))
        self.seed_state(conn, s2, ns, "fb expired sibling source",
                        inject_until=LONG_DEAD)
        head = "44444444-4444-4444-8444-444444444444"
        self.seed_belief_head(conn, head, ns, "fb empty-sibling head",
                              [s1, s2])
        conn.commit()
        conn.close()

        from storelib.recall import _annotate_virtual_expiry, _expiry_filter
        row = {"id": "belief:" + head, "source_ids": [s1, s2]}
        full_conn = sqlite3.connect(str(d / "store.sqlite"))
        full_conn.row_factory = sqlite3.Row
        _annotate_virtual_expiry(full_conn, [row])
        self.assertEqual(row.get("inject_until"), LONG_DEAD,
                         "the expired sibling must win the min()")
        live, removed = _expiry_filter([row], now_iso=NOW)
        self.assertEqual(removed, 1,
                         "the head must be withheld (AC5 mixed-source law)")


class ExpiryFilterMarkerTest(FeedbackRoundBase):
    """Mutant M03: removing the _expiry_unread branch from _expiry_filter
    must fail this leg."""

    def test_filter_removes_marked_row(self):
        from storelib.recall import _expiry_filter
        rows = [{"id": "a", "_expiry_unread": True},
                {"id": "b", "inject_until": FAR_LIVE}]
        live, removed = _expiry_filter(rows, now_iso=NOW)
        self.assertEqual([r["id"] for r in live], ["b"])
        self.assertEqual(removed, 1)


class CandidatePopulationLawTest(FeedbackRoundBase):
    """R10/#344 AC1: expired/unread count only ids inside the candidate
    population (the pre-gate window + arrivals) — an engaged envelope's
    numbers reconcile against its own candidate_ids."""

    def test_gate_counts_only_window(self):
        from storelib.recall import _gate_passive_expiry
        dead_in = {"id": "d-in", "inject_until": LONG_DEAD}
        dead_beyond = {"id": "d-beyond", "inject_until": LONG_DEAD}
        acc = {"expired_ids": set(), "pre_ids": [], "pre_rows": [],
               "engaged": False}
        live = _gate_passive_expiry(
            _NoSideTableConn(), [(2.0, dead_in), (1.0, dead_beyond)],
            limit=1, now_iso=NOW, acc=acc)
        self.assertEqual(live, [], "both expired rows are withheld")
        self.assertEqual(acc["expired_ids"], {"d-in"},
                         "only the CANDIDATE (in-window) removal counts")
        self.assertEqual(list(dict.fromkeys(acc["pre_ids"])), ["d-in"],
                         "candidate_ids is the window population")
        self.assertNotIn("unread_ids", acc)

    def test_gate_counts_unread_only_in_window(self):
        from storelib.recall import _gate_passive_expiry
        unread_in = {"id": "u-in", "_expiry_unread": True}
        unread_beyond = {"id": "u-beyond", "_expiry_unread": True}
        acc = {"expired_ids": set(), "pre_ids": [], "pre_rows": [],
               "engaged": False}
        live = _gate_passive_expiry(
            _NoSideTableConn(), [(2.0, unread_in), (1.0, unread_beyond)],
            limit=1, now_iso=NOW, acc=acc)
        self.assertEqual(live, [])
        self.assertEqual(acc["unread_ids"], {"u-in"})
        self.assertEqual(acc["expired_ids"], {"u-in"},
                         "the uniform removal law holds inside the window")


class GarbageDeadlineWithheldTest(FeedbackRoundBase):
    """G34/#349: the gate FAILS CLOSED on an unparseable inject_until —
    a garbage deadline is never delivered on the passive lane (the mutant
    flipping the gate comparison to read 0.0 as live survived every test at
    the reviewed head)."""

    def test_garbage_deadline_never_delivers(self):
        d = self.tmp / "garbage"
        d.mkdir()
        conn = self.fresh_conn(d)
        ns = "project:expiry-fb-garbage"
        self.seed_state(conn, "00000000-0000-4000-8000-00000000f001", ns,
                        "fb garbage deadline row",
                        inject_until="not-a-timestamp")
        self.seed_state(conn, "00000000-0000-4000-8000-00000000f002", ns,
                        "fb live control row", inject_until=FAR_LIVE)
        conn.commit()
        conn.close()

        r = self.run_cli(
            d, "recall", "--query", "fb", "--namespace", ns,
            "--for-injection", "--json",
            "--session-id", f"fb-g-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=NOW)
        self.assertEqual(r.returncode, 0, r.stderr[:300])
        env = json.loads(r.stdout)
        got = [row.get("content") for row in env.get("results", [])]
        self.assertEqual(
            got, ["fb live control row"],
            f"the garbage-deadline row must be WITHHELD (fail-closed), "
            f"got {got}")
        self.assertEqual(env.get("expired"), 1)
        self.assertEqual(env.get("reason"), "injected",
                         "the live control row delivers, so the reason is "
                         "not the silent-expired path")


class RecentLaneFailedReadTest(FeedbackRoundBase):
    """R32: the recent lane under a failed side-table read — never-read
    rows are withheld AND counted (in-window), never delivered live."""

    def test_recent_tier_marks_and_counts_unread(self):
        d = self.tmp / "recent-fb"
        d.mkdir()
        conn = self.fresh_conn(d)
        ns = "project:expiry-fb-recent"
        dead = "00000000-0000-4000-8000-00000000fb01"
        live = "00000000-0000-4000-8000-00000000fb02"
        self.seed_state(conn, dead, ns, "fb recent dead row",
                        inject_until=LONG_DEAD)
        self.seed_state(conn, live, ns, "fb recent live row")
        conn.commit()
        conn.close()

        from storelib.recall import _recent_one_tier
        acc = {"expired_ids": set(), "pre_ids": [], "pre_rows": [],
               "engaged": False}
        real = sqlite3.connect(str(d / "store.sqlite"))
        real.row_factory = sqlite3.Row
        proxy = NeverExpiryConn(real)
        results = _recent_one_tier(
            proxy, ns_list=[ns], limit=10, min_confidence=0.0,
            as_of=None, moment=None, lane="zcode", expiry_lane=True,
            expiry_acc=acc, expiry_now_iso=NOW)
        got = [r["id"] for r in results]
        self.assertNotIn(dead, got, "the expired row is withheld")
        self.assertNotIn(live, got,
                         "the NEVER-READ live row must be withheld too "
                         "(unknown state, fail-closed)")
        self.assertIn(dead, acc["expired_ids"])
        self.assertIn(live, acc.get("unread_ids", set()),
                      "the never-read row is counted in unread_ids")
        real.close()


class EmissionSeamTest(FeedbackRoundBase):
    """R29/PRR-008: the expiry_read_failed emission seam — details keying,
    envelope param keyed-only-when-present, and the accounting the frozen
    gate test does not assert."""

    def test_details_key_expiry_read_failed(self):
        from storelib.recall import _recall_injection_details
        acc = {"expired_ids": {"a", "b"}, "unread_ids": {"a"},
               "pre_ids": ["a", "b"], "pre_rows": [], "engaged": True}
        details, _ = _recall_injection_details(
            [], omitted=0, exclude_ids=None, arms={}, injection_risk=0,
            budget_tokens=1000, expiry_acc=acc)
        self.assertEqual(details.get("expired"), 2)
        self.assertEqual(details.get("expiry_read_failed"), 1)

    def test_details_silent_without_unread(self):
        from storelib.recall import _recall_injection_details
        acc = {"expired_ids": {"a"}, "pre_ids": ["a"], "pre_rows": [],
               "engaged": True}
        details, _ = _recall_injection_details(
            [], omitted=0, exclude_ids=None, arms={}, injection_risk=0,
            budget_tokens=1000, expiry_acc=acc)
        self.assertNotIn("expiry_read_failed", details,
                         "keyed only when a read failure withheld rows")

    def test_envelope_keyed_only_when_present(self):
        from storelib.recall import build_injection_envelope
        base = dict(
            omitted=0, reason="injected", excluded=[], candidate_ids=[],
            tokens_used=0, tokens_budget=100, budget_dropped=0,
            budget_admission=0, budget_truncated=0,
            budget_dropped_protected=0, arms={}, rendered="")
        env = build_injection_envelope([], **base, expiry_read_failed=2)
        self.assertEqual(env.get("expiry_read_failed"), 2)
        env_none = build_injection_envelope([], **base)
        self.assertNotIn("expiry_read_failed", env_none)

    def test_hermes_fallback_mirror_pin(self):
        """R20: the hermes degraded fallback set mirrors the store's
        OPTIONAL set — a key registered on one side but not the other
        blackouts recall on the degraded path. Source-level pin: the
        plugin module imports Hermes-only dependencies."""
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(
            encoding="utf-8")
        start = text.index('frozenset(("injection_risk", "candidate_lanes",')
        block = text[start:]
        end = block.index("expiry_read_failed")
        block = block[: end + 40]
        self.assertIn(
            "expiry_read_failed", block,
            "the hermes fallback optional set must mirror "
            "INJECTION_ENVELOPE_OPTIONAL (a key missing from BOTH sets "
            "blackouts recall)")


class DoctorPerLegGuardTest(FeedbackRoundBase):
    """R35/PRR-003: a failure of the SECOND scan preserves the first
    scan's computed rows; R06/PRR-004: an empty deadline is not named
    unreadable (the gate reads it as live)."""

    def setUp(self):
        super().setUp()
        self.store_path = self.tmp / "store.sqlite"
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    def _seed(self, mid, content, inject_until):
        import storelib.schema as schema
        conn = sqlite3.connect(str(self.store_path))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        conn.execute(MEM_INSERT, (mid, "project:expiry-fb-doc", content,
                                  INGEST_BASE, INGEST_BASE))
        if inject_until is not None:
            conn.execute(
                "INSERT INTO memory_expiry (memory_id, inject_until, "
                "expiry_class) VALUES (?, ?, 'endpoint')",
                (mid, inject_until))
        conn.commit()
        conn.close()

    def test_second_scan_failure_keeps_first_scan_rows(self):
        self._seed("00000000-0000-4000-8000-00000000fb11",
                   "fb doctor expiring row", "2026-10-25T00:00:00Z")
        import doctor
        from unittest import mock
        with mock.patch.object(
                doctor, "_unreadable_expiry_states",
                side_effect=sqlite3.OperationalError("database is locked")):
            report = doctor.build_report(
                REPO_ROOT, REPO_ROOT, store_override=self.store_path,
                now=NOW, expiring="7d")
        self.assertEqual(
            [e.get("memory_id") for e in report.get("expiring_states", [])],
            ["00000000-0000-4000-8000-00000000fb11"],
            "a second-scan failure must NOT discard the first scan's rows")
        self.assertIn("expiry_read_error", report)

    def test_empty_deadline_not_named_unreadable(self):
        self._seed("00000000-0000-4000-8000-00000000fb12",
                   "fb doctor empty deadline row", "")
        import doctor
        report = doctor.build_report(
            REPO_ROOT, REPO_ROOT, store_override=self.store_path,
            now=NOW, expiring="7d")
        named = [e.get("memory_id")
                 for e in report.get("unreadable_expiry_states", [])]
        self.assertNotIn(
            "00000000-0000-4000-8000-00000000fb12", named,
            "an empty deadline reads LIVE at the gate (no stamp) — naming "
            "it withheld would misreport a delivered row")

    def test_control_chars_neutralized_in_preview(self):
        import doctor
        preview = doctor._operator_preview("ok \x1b]0;evil\x07 tail")
        self.assertNotIn("\x1b", preview)
        self.assertNotIn("\x07", preview)
        self.assertTrue(preview.startswith("ok "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
