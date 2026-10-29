"""Doctor gate-observability acceptance tests (zmem issues #342/#349).

Arm's-length acceptance checks for the doctor half of the live-expiry
gate hardening fix: #342 (a failed --expiring read on a store whose
memory_expiry table is PRESENT must surface a diagnostic — the report
keys expiry_read_error — instead of an empty list indistinguishable
from "nothing expires") and #349 (a side-table row whose inject_until
does not parse is named to the operator via the report's
unreadable_expiry_states section). The discriminating tests are RED at
base and GREEN after the fix; the preserving legs (readable store
still lists rows; absent side table still returns []) are green
throughout and pin the fail direction that must not move. The gate's
fail-closed WITHHOLDING of unreadable rows is deliberate (#174) and is
NOT asserted here.

Isolation contract (the tests/test_doctor.py V15DoctorChecksTest
discipline): every test pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR at a
fresh scratch and ZMEM_MODEL_AUTODOWNLOAD=0 BEFORE importing storelib
or doctor, purges store/storelib from sys.modules on setUp, and
restores the ambient env in teardown. ZMEM_TEST_NOW is never ambient;
the clock reaches doctor only through build_report's explicit now=
argument. Doctor is driven in-process (doctor.build_report with
store_override), exactly as the established V15 convention does — the
assertions hold on the REPORT CONTENT, never the process exit code.

The EXCLUSIVE-holder leg reproduces the failed read with a second
sqlite connection holding PRAGMA locking_mode=EXCLUSIVE + BEGIN
EXCLUSIVE over a store whose memory_expiry table EXISTS: doctor's own
mode=ro connection (timeout=5.0) then raises OperationalError instead
of reading. This leg stays on the _expiring_states unit seam; the
report-level failure pin uses a monkeypatched _expiring_states raising
the same error (a full build_report under the holder is not needed to
pin the caller contract — noted in the test body).

Runs standalone: python tests/test_doctor_gate_observability.py
Also: python -m unittest tests.test_doctor_gate_observability
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"

NOW = "2026-10-23T12:00:00Z"
IN_HORIZON = "2026-10-25T00:00:00Z"   # inside (NOW, NOW+7d]
LONG_DEAD = "2026-01-02T00:00:00Z"
INGEST_BASE = "2026-01-01T00:00:00Z"


class DoctorObservabilityBase(unittest.TestCase):
    """Shared isolation scaffold (env pin + storelib purge + store seed)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-doctor-gate-obs-"))
        self.store_path = self.tmp / "store.sqlite"
        self._saved_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("ZMEM_"):
                os.environ.pop(key)
        os.environ.update({
            "ZMEM_STORE": str(self.store_path),
            "ZMEM_DATA": str(self.tmp),
            "ZMEM_MODELS_DIR": str(self.tmp / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        for mod in list(sys.modules.keys()):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for mod in list(sys.modules.keys()):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]

    # -- helpers ----------------------------------------------------------

    def fresh_v15_conn(self) -> sqlite3.Connection:
        """A fresh init_db+migrate store at self.store_path."""
        import storelib.schema as schema  # imported AFTER the env pin
        conn = sqlite3.connect(str(self.store_path))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        return conn

    def insert_state_row(self, conn: sqlite3.Connection, mid: str, *,
                         namespace: str, content: str,
                         inject_until: str | None,
                         expiry_class: str = "endpoint",
                         ingestion_ts: str = INGEST_BASE) -> None:
        """Direct-INSERT one 29-column state memory row (the
        tests/test_live_expiry.py insert pattern) plus its memory_expiry
        side row when inject_until is given."""
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "superseded_at, ingestion_ts, retrieval_count, last_retrieved, "
            "surfaced_count, last_surfaced, merged_from, content_norm, "
            "valid_until, update_of, taint, trust_score, applied_count, "
            "violated_count, embedding, embedding_model, embedded_at, "
            "consolidated_at, supersede_reason) "
            "VALUES (?, ?, 'state', ?, '', '', '', 0.9, 'test', ?, NULL, ?, "
            "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', 1.0, 0, "
            "0, NULL, '', NULL, NULL, '')",
            (mid, namespace, content, ingestion_ts, ingestion_ts),
        )
        if inject_until is not None:
            conn.execute(
                "INSERT INTO memory_expiry (memory_id, inject_until, "
                "expiry_class) VALUES (?, ?, ?)",
                (mid, inject_until, expiry_class))

    def build_report(self, expiring: str = "7d") -> dict:
        """In-process doctor report against this test's store (the
        V15DoctorChecksTest._report convention: store_override + explicit
        clock; assertions hold on report content, never exit codes)."""
        import doctor  # noqa: E402  (scripts dir is on sys.path)
        return doctor.build_report(
            REPO_ROOT, REPO_ROOT, store_override=self.store_path,
            now=NOW, expiring=expiring)

    def hold_exclusive(self) -> sqlite3.Connection:
        """A second connection holding PRAGMA locking_mode=EXCLUSIVE +
        BEGIN EXCLUSIVE over the store — doctor's mode=ro read of
        memory_expiry now raises OperationalError('database is locked')
        while the table EXISTS (the #342 non-absent-table failure)."""
        holder = sqlite3.connect(str(self.store_path))
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("SELECT COUNT(*) FROM memory").fetchone()
        return holder


class DoctorSideTableFailureTest(DoctorObservabilityBase):
    """C3/AC3 (#342 doctor): a failed --expiring read on a store whose
    side table is PRESENT must fail closed at the _expiring_states seam
    and key expiry_read_error into the report — never a silent [].
    RED at base, GREEN after the fix. The readable-store and
    absent-table legs are preserving pins (green throughout)."""

    LIVE_ID = "00000000-0000-4000-8000-00000000e001"

    def test_locked_side_table_read_raises_not_silent_empty(self):
        """The unit seam: with memory_expiry PRESENT and its read failing
        (EXCLUSIVE holder), _expiring_states must RAISE
        sqlite3.OperationalError (fail-closed) rather than swallow it
        into [] — the decided contract; the doctor caller catches and
        keys expiry_read_error. RED at base (base returns [] after its
        5s timeout, indistinguishable from "nothing expires")."""
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.LIVE_ID, namespace="project:expiry-342-doctor",
            content="e342 expiring state", inject_until=IN_HORIZON)
        conn.commit()
        conn.close()

        holder = self.hold_exclusive()
        try:
            import doctor  # noqa: E402
            with self.assertRaises(
                    sqlite3.OperationalError,
                    msg="_expiring_states swallowed a FAILED side-table "
                        "read (memory_expiry present, EXCLUSIVE holder) "
                        "into a silent [] — it must fail closed so the "
                        "doctor report can key expiry_read_error "
                        "(issue #342)"):
                doctor._expiring_states(self.store_path, "7d", NOW)
        finally:
            holder.rollback()
            holder.close()

    def test_readable_store_still_lists_expiring_rows(self):
        """Preserving pin (green at base and after the fix): an ordinary
        readable store still returns the expiring list — one row inside
        (NOW, NOW+7d]."""
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.LIVE_ID, namespace="project:expiry-342-doctor",
            content="e342 expiring state", inject_until=IN_HORIZON)
        conn.commit()
        conn.close()

        import doctor  # noqa: E402
        states = doctor._expiring_states(self.store_path, "7d", NOW)
        self.assertIsInstance(states, list,
                              "a readable store must keep yielding a list")
        self.assertEqual(
            [e.get("memory_id") for e in states], [self.LIVE_ID],
            f"expected the in-horizon row to be listed, got {states}")

    def test_absent_side_table_keeps_empty_list(self):
        """Preserving pin (green at base and after the fix, the doctor-side
        AC2): a store WITHOUT the memory_expiry table (pre-v15 shape)
        still yields [] — absence is not a failure and must never raise
        or key a diagnostic."""
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.LIVE_ID, namespace="project:expiry-342-doctor",
            content="e342 legacy state", inject_until=None)
        conn.execute("DROP TABLE memory_expiry")
        conn.commit()
        conn.close()

        import doctor  # noqa: E402
        self.assertEqual(
            doctor._expiring_states(self.store_path, "7d", NOW), [],
            "an absent side table must keep yielding [] (pre-v15 stores "
            "keep working); absence is not a read failure")

    def test_report_keys_expiry_read_error_when_read_fails(self):
        """The caller contract: when _expiring_states fails, the report
        carries expiry_read_error (a short message) instead of a silent
        empty expiring_states. Pinned with a monkeypatched _expiring_states
        raising the same OperationalError a locked store produces — a full
        build_report under the EXCLUSIVE holder adds nothing to this
        contract (the seam raise is pinned above) and would pay doctor's
        5s timeout a second time. RED at base (base build_report lets the
        error propagate)."""
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.LIVE_ID, namespace="project:expiry-342-doctor",
            content="e342 expiring state", inject_until=IN_HORIZON)
        conn.commit()
        conn.close()

        import doctor  # noqa: E402
        from unittest import mock  # noqa: E402
        with mock.patch.object(
                doctor, "_expiring_states",
                side_effect=sqlite3.OperationalError("database is locked")):
            try:
                report = self.build_report()
            except sqlite3.OperationalError:
                self.fail(
                    "build_report propagated the side-table read failure — "
                    "the doctor REPORT must catch it and key "
                    "expiry_read_error instead (issue #342)")
        self.assertIn(
            "expiry_read_error", report,
            "a FAILED --expiring read must surface as the report key "
            "expiry_read_error (keyed only on failure), never as an empty "
            f"expiring_states indistinguishable from 'nothing expires'; "
            f"report keys: {sorted(k for k in report)[:24]}")
        self.assertTrue(
            str(report.get("expiry_read_error", "") or "").strip(),
            "expiry_read_error must carry a short non-empty message")


class UnreadableDeadlineTest(DoctorObservabilityBase):
    """C9/AC9 (#349): a memory_expiry row whose inject_until does not
    parse (e.g. 'not-a-timestamp') is named to the operator through the
    report's unreadable_expiry_states section. RED at base (base skips
    the row silently). The parseable-store leg is the AC10 preserving
    pin (key absent when everything parses). The gate keeps WITHHOLDING
    such rows (fail-closed is deliberate) — delivery is NOT asserted."""

    BAD_ID = "00000000-0000-4000-8000-00000000d001"
    BAD_NS = "project:expiry-349"
    OK_ID = "00000000-0000-4000-8000-00000000d002"

    def test_unreadable_inject_until_is_named_to_operator(self):
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.BAD_ID, namespace=self.BAD_NS,
            content="d349 unreadable deadline row",
            inject_until="not-a-timestamp")
        conn.commit()
        conn.close()

        report = self.build_report()
        self.assertIn(
            "unreadable_expiry_states", report,
            "issue #349: a side-table row whose inject_until does not parse "
            "is invisible in the doctor report (silently skipped by "
            "--expiring) — the report must gain unreadable_expiry_states "
            f"(keyed only when nonempty); report keys: "
            f"{sorted(k for k in report)[:24]}")
        entries = report["unreadable_expiry_states"]
        self.assertIsInstance(entries, list)
        named = [e for e in entries if e.get("memory_id") == self.BAD_ID]
        self.assertTrue(
            named,
            f"unreadable_expiry_states must name {self.BAD_ID} "
            f"(inject_until='not-a-timestamp'), got {entries}")
        entry = named[0]
        self.assertEqual(
            entry.get("inject_until"), "not-a-timestamp",
            "the unreadable deadline value itself must be reported")
        self.assertEqual(
            entry.get("namespace"), self.BAD_NS,
            "the row's namespace must be reported")
        self.assertIn(
            "content_preview", entry,
            "each entry must carry a content_preview")

    def test_parseable_store_keys_no_unreadable_section(self):
        """Preserving pin (green at base and after the fix, AC10): while
        every inject_until parses, the report carries NO
        unreadable_expiry_states key (keyed only when nonempty) and the
        in-horizon row still lists normally."""
        conn = self.fresh_v15_conn()
        self.insert_state_row(
            conn, self.OK_ID, namespace=self.BAD_NS,
            content="d349 parseable deadline row",
            inject_until=IN_HORIZON)
        conn.commit()
        conn.close()

        report = self.build_report()
        self.assertNotIn(
            "unreadable_expiry_states", report,
            "the key is keyed ONLY when an unreadable deadline exists — a "
            "fully parseable store must keep the report shape unchanged")
        states = report.get("expiring_states") or []
        self.assertEqual(
            [e.get("memory_id") for e in states], [self.OK_ID],
            f"the parseable in-horizon row must still list, got {states}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
