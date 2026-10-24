"""Live-expiry gate hardening acceptance tests (zmem issues #342/#343/#344).

These are the arm's-length acceptance checks for the collated fix of
#342 (annotate fail-open under a failed side-table read), #343
(belief-head expiry resolved through provenance), and #344
(candidate_ids / candidate_lanes population reconciliation on the
engaged envelope). They encode the contract, not the implementation:
the discriminating tests are RED at base and GREEN after the fix; the
preserving legs (absent side table stays every-row-live) are green
throughout and pin the fail direction that must not move.

Isolation contract (per the repo's store-test discipline, copied from
tests/test_live_expiry.py): every test pins ZMEM_STORE/ZMEM_DATA/
ZMEM_MODELS_DIR at a fresh nonexistent scratch and
ZMEM_MODEL_AUTODOWNLOAD=0 BEFORE importing storelib, purges
store/storelib from sys.modules on setUp, and restores the ambient env
in teardown. ZMEM_TEST_NOW is NEVER ambient in this process and NEVER
set on a writer: it exists only inside the env of a clock-sensitive
READ subprocess, immediately around the assertion it pins. State rows
are seeded by direct INSERT with explicit fixed uuids (the full
29-column memory insert) so ingestion order is part of the contract;
belief heads are direct-INSERTed the same way.

Envelope-content discipline (zmem lesson acaee4a4): the passive
selector wraps recall in a blanket except, so an exit-0 parseable
envelope can still be a silent failure. Every passive-lane assertion
goes through the envelope's results[*]["content"], the rendered fence,
the expired count, and the envelope population fields — never exit-0
alone.

The EXCLUSIVE-holder legs (#342) reproduce the read failure with a
second sqlite connection holding PRAGMA locking_mode=EXCLUSIVE +
BEGIN EXCLUSIVE over a store whose memory_expiry table EXISTS — the
reader connection is built with a short timeout because
_annotate_expiry uses the conn it is given. These legs stay unit
level on purpose: a full CLI recall under the holder fails its FTS
reads first (known), so the annotate/gate seams are the honest
surface.

Runs standalone: python tests/test_live_expiry_gate_hardening.py
Also: python -m unittest tests.test_live_expiry_gate_hardening
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

# One fixed clock for every read leg: all under-test inject_until
# values are far from it on either side, so the assertions are
# insensitive to second-level jitter in the pinned instant.
NOW = "2026-10-23T12:00:00Z"
LONG_DEAD = "2026-01-02T00:00:00Z"    # long elapsed at NOW
FAR_LIVE = "2027-01-01T00:00:00Z"     # far future at NOW
TWIN_LIVE = "2027-06-01T00:00:00Z"    # far future at NOW

INGEST_BASE = "2026-01-01T00:00:00Z"


class GateHardeningBase(unittest.TestCase):
    """Shared isolation scaffold (env pin + storelib purge + CLI helper)."""

    def setUp(self):
        # Fresh nonexistent scratch per test; the four isolation vars are
        # pinned BEFORE any storelib import (sys.modules purge below, the
        # tests/test_as_of_recall.py:26-81 pattern) so the operator store is
        # never opened and STORE_PATH never freezes onto it.
        self.tmp = tempfile.mkdtemp(prefix="zmem-expiry-gate-hard-")
        self.scratch = Path(self.tmp)
        self._saved_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("ZMEM_"):
                os.environ.pop(key)
        os.environ.update(self._isolation_env(self.scratch))
        self._purge_storelib()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._purge_storelib()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _purge_storelib():
        for mod in list(sys.modules):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]

    @staticmethod
    def _isolation_env(store_dir: Path, now: str | None = None) -> dict:
        """Subprocess env pinned at `store_dir`; ZMEM_TEST_NOW ONLY when the
        leg is a clock-sensitive read (never a writer)."""
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

    def run_cli(self, store_dir: Path, *args, now: str | None = None
                ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            capture_output=True, text=True, timeout=180,
            env=self._isolation_env(store_dir, now=now),
        )

    def fresh_v15_conn(self, store_dir: Path) -> sqlite3.Connection:
        """A fresh init_db+migrate store at store_dir/store.sqlite."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import storelib.schema as schema  # imported AFTER the env pin
        conn = sqlite3.connect(str(store_dir / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        return conn

    def insert_memory_row(self, conn: sqlite3.Connection, *, mid: str,
                          namespace: str, content: str,
                          ingestion_ts: str = INGEST_BASE) -> None:
        """Direct-INSERT one 29-column state memory row (the
        tests/test_live_expiry.py insert_memory_row pattern)."""
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

    def insert_expiry_row(self, conn: sqlite3.Connection, mid: str,
                          inject_until: str,
                          expiry_class: str = "endpoint") -> None:
        conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, expiry_class) "
            "VALUES (?, ?, ?)", (mid, inject_until, expiry_class))

    def envelope(self, r: subprocess.CompletedProcess) -> dict:
        """Parse a --json envelope, refusing the exit-0-garbage trap."""
        self.assertEqual(
            r.returncode, 0,
            f"expected exit 0, got {r.returncode}; stderr={r.stderr.strip()[:300]}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout is not JSON: {r.stdout[:300]}")

    @staticmethod
    def contents(env: dict) -> list:
        return [row.get("content", "") for row in env.get("results", [])]

    def hold_exclusive(self, store_dir: Path) -> sqlite3.Connection:
        """A second connection holding PRAGMA locking_mode=EXCLUSIVE +
        BEGIN EXCLUSIVE over the store — every other connection's read of
        memory_expiry now raises OperationalError('database is locked')
        while the table EXISTS (the #342 non-absent-table failure)."""
        holder = sqlite3.connect(str(store_dir / "store.sqlite"))
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("SELECT COUNT(*) FROM memory").fetchone()
        return holder


class AnnotateFailOpenTest(GateHardeningBase):
    """C1/AC1 (#342): when memory_expiry EXISTS but its read fails for a
    reason other than the table being absent, the annotate/gate seam must
    NOT stamp the un-read pool as live. RED at base, GREEN after the fix.
    Plus the AC2 preserving pin: an absent side table stays every-row-live
    (green at base and after the fix)."""

    DEAD_ID = "00000000-0000-4000-8000-00000000a001"
    LIVE_ID = "00000000-0000-4000-8000-00000000a002"

    def _seed_two_sided_rows(self, store_dir: Path) -> sqlite3.Connection:
        """A v15 store whose memory_expiry table EXISTS with one
        long-elapsed row (DEAD_ID) and one far-future row (LIVE_ID)."""
        conn = self.fresh_v15_conn(store_dir)
        self.insert_memory_row(conn, mid=self.DEAD_ID,
                               namespace="project:expiry-342",
                               content="a-failopen dead state")
        self.insert_expiry_row(conn, self.DEAD_ID, LONG_DEAD)
        self.insert_memory_row(conn, mid=self.LIVE_ID,
                               namespace="project:expiry-342",
                               content="a-failopen live state")
        self.insert_expiry_row(conn, self.LIVE_ID, FAR_LIVE)
        conn.commit()
        return conn

    def test_failed_read_marks_rows_unread_not_live(self):
        """The unit seam: _annotate_expiry on a reader whose side-table read
        fails (EXCLUSIVE holder, table PRESENT) must not stamp the un-read
        rows inject_until=None-as-live — they carry _expiry_unread=True
        instead (or no stamp at all). RED at base (base stamps None)."""
        d = self.scratch / "failopen"
        d.mkdir()
        conn = self._seed_two_sided_rows(d)
        holder = self.hold_exclusive(d)
        reader = sqlite3.connect(str(d / "store.sqlite"), timeout=0.05)
        reader.row_factory = sqlite3.Row
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from storelib.recall import _annotate_expiry
            rows = [{"id": self.DEAD_ID}, {"id": self.LIVE_ID}]
            _annotate_expiry(reader, rows)
            for row in rows:
                self.assertTrue(
                    "inject_until" not in row
                    or row.get("_expiry_unread") is True,
                    f"row {row['id']}: a FAILED side-table read (memory_expiry "
                    f"present, EXCLUSIVE holder) stamped inject_until="
                    f"{row.get('inject_until')!r} — the un-read pool must "
                    f"carry _expiry_unread=True and no live stamp (issue #342)")
        finally:
            holder.rollback()
            holder.close()
            reader.close()
            conn.close()

    def test_gate_withholds_rows_whose_expiry_read_failed(self):
        """The gate seam: under the same failed read, a row whose side row
        says long-elapsed must NOT survive _gate_passive_expiry — the gate
        fails closed on unread expiry state (same direction as unreadable
        deadlines). RED at base (the fail-open stamp disables the gate and
        the long-elapsed row is returned as live)."""
        d = self.scratch / "gateseam"
        d.mkdir()
        conn = self._seed_two_sided_rows(d)
        holder = self.hold_exclusive(d)
        reader = sqlite3.connect(str(d / "store.sqlite"), timeout=0.05)
        reader.row_factory = sqlite3.Row
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from storelib.recall import _gate_passive_expiry
            scored = [
                (2.0, {"id": self.DEAD_ID, "type": "state",
                       "content": "a-failopen dead state"}),
                (1.0, {"id": self.LIVE_ID, "type": "state",
                       "content": "a-failopen live state"}),
            ]
            acc = {"expired_ids": set(), "pre_ids": [], "pre_rows": [],
                   "engaged": False}
            live = _gate_passive_expiry(
                reader, scored, limit=5, now_iso=NOW, acc=acc)
            survivor_ids = [row["id"] for _, row in live]
            self.assertNotIn(
                self.DEAD_ID, survivor_ids,
                f"the gate returned {self.DEAD_ID} (whose memory_expiry row "
                f"says long-elapsed at {NOW}) as LIVE because the side-table "
                f"read failed under an EXCLUSIVE holder — the gate must fail "
                f"closed on unread expiry state, not fail open (issue #342); "
                f"survivors={survivor_ids}")
        finally:
            holder.rollback()
            holder.close()
            reader.close()
            conn.close()

    def test_absent_side_table_stamps_every_row_live(self):
        """AC2/C2 preserving pin (green at base and after the fix): a store
        WITHOUT the memory_expiry table (pre-v15 shape) keeps today's
        every-row-live annotate — no _expiry_unread marker, no gate."""
        d = self.scratch / "absent"
        d.mkdir()
        conn = self.fresh_v15_conn(d)
        self.insert_memory_row(conn, mid=self.DEAD_ID,
                               namespace="project:expiry-342-absent",
                               content="a-absent legacy state")
        conn.execute("DROP TABLE memory_expiry")
        conn.commit()
        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            from storelib.recall import _annotate_expiry
            rows = [{"id": self.DEAD_ID}]
            _annotate_expiry(conn, rows)
            self.assertIsNone(
                rows[0].get("inject_until"),
                "an absent side table must keep stamping every row "
                "inject_until=None (live)")
            self.assertNotIn(
                "_expiry_unread", rows[0],
                "an absent side table is NOT a read failure — no unread "
                "marker may appear (pre-v15 stores keep working)")
        finally:
            conn.close()


class BeliefHeadExpiryTest(GateHardeningBase):
    """C4+C5/AC4+AC5 (#343): a belief head's expiry resolves through its
    SOURCE rows — the earliest source inject_until wins; an all-expired (or
    earliest-elapsed) head is withheld by the gate and counted in the
    expired pool, while an all-live twin head still delivers (positive
    control). RED at base (base delivers the head: the synthetic
    belief:<head_id> never matches memory_expiry.memory_id)."""

    NS_SOLE = "project:expiry-343-sole"
    NS_MIXED = "project:expiry-343-mixed"

    SRC_SOLE = "00000000-0000-4000-8000-00000000b001"
    HEAD_SOLE_ID = "11111111-1111-4111-8111-111111111111"
    HEAD_SOLE_CONTENT = "b343 head content alpha"

    MIX_DEAD = "00000000-0000-4000-8000-00000000b101"
    MIX_LIVE = "00000000-0000-4000-8000-00000000b102"
    TWIN_SRC = "00000000-0000-4000-8000-00000000b103"
    HEAD_MIXED_ID = "22222222-2222-4222-8222-222222222221"
    HEAD_TWIN_ID = "22222222-2222-4222-8222-222222222222"
    HEAD_MIXED_CONTENT = "b343 mixed head beta"
    HEAD_TWIN_CONTENT = "b343 twin head beta"

    def insert_belief_head(self, conn: sqlite3.Connection, *, head_id: str,
                           namespace: str, topic: str, content: str,
                           source_ids: list[str]) -> None:
        """Direct-INSERT one active belief_head + its belief_head_source
        rows (the schema's exact columns; role='source')."""
        conn.execute(
            "INSERT INTO belief_head (id, namespace, topic_identity, "
            "content, head_state, head_source_id, support_count, "
            "refresh_watermark, generator_revision, confidence, signal, "
            "taint, trust_score) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, '2026-01-01T00:00:00Z', "
            "'belief-heads-v1', 0.9, 'test', 'trusted_internal', 1.0)",
            (head_id, namespace, topic, content, source_ids[0],
             len(source_ids)))
        for src in source_ids:
            conn.execute(
                "INSERT INTO belief_head_source (head_id, source_id, role, "
                "source_ingestion_ts, source_checksum) "
                "VALUES (?, ?, 'source', '2026-01-01T00:00:00Z', '')",
                (head_id, src))

    def test_sole_expired_source_withholds_head_and_counts_it(self):
        """AC4 (leg 1): a head whose sole source row expired long ago is NOT
        delivered on the passive lane and the envelope's expired count
        includes it. RED at base (base delivers the head; expired counts
        only the gated source row)."""
        d = self.scratch / "belief-sole"
        d.mkdir()
        conn = self.fresh_v15_conn(d)
        self.insert_memory_row(conn, mid=self.SRC_SOLE,
                               namespace=self.NS_SOLE,
                               content="b343 source state alpha")
        self.insert_expiry_row(conn, self.SRC_SOLE, LONG_DEAD)
        self.insert_belief_head(
            conn, head_id=self.HEAD_SOLE_ID, namespace=self.NS_SOLE,
            topic="topic-b343-sole", content=self.HEAD_SOLE_CONTENT,
            source_ids=[self.SRC_SOLE])
        conn.commit()
        conn.close()

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "alpha", "--namespace", self.NS_SOLE,
            "--for-injection", "--json",
            "--session-id", f"b343-a-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=NOW))
        self.assertNotIn(
            self.HEAD_SOLE_CONTENT, self.contents(env),
            f"a belief head whose sole source expired at {LONG_DEAD} was "
            f"DELIVERED on the passive lane at ZMEM_TEST_NOW={NOW} — the "
            f"head's expiry must resolve through its SOURCE row's "
            f"memory_expiry entry (issue #343); "
            f"results={self.contents(env)}")
        self.assertNotIn(
            self.HEAD_SOLE_CONTENT, env.get("rendered") or "",
            "the expired-source head leaked into the rendered fence")
        self.assertGreaterEqual(
            env.get("expired", 0), 2,
            f"the envelope's expired count must include the withheld head "
            f"(its expired source row counts 1, the head itself 1 more); "
            f"got expired={env.get('expired', None)!r}")

    def test_earliest_source_expiry_gates_head_and_live_twin_delivers(self):
        """AC5 (leg 2): a head over a mix of expired + live sources gates on
        the EARLIEST source expiry (withheld), while a twin head whose
        sources are all live still delivers (the positive control proves
        the seeding and the merge path — it is green at base and after the
        fix). RED at base (base delivers the mixed head too)."""
        d = self.scratch / "belief-mixed"
        d.mkdir()
        conn = self.fresh_v15_conn(d)
        self.insert_memory_row(conn, mid=self.MIX_DEAD,
                               namespace=self.NS_MIXED,
                               content="b343 source dead beta",
                               ingestion_ts="2026-01-02T00:00:00Z")
        self.insert_expiry_row(conn, self.MIX_DEAD, LONG_DEAD)
        self.insert_memory_row(conn, mid=self.MIX_LIVE,
                               namespace=self.NS_MIXED,
                               content="b343 source live beta")
        self.insert_expiry_row(conn, self.MIX_LIVE, FAR_LIVE)
        self.insert_memory_row(conn, mid=self.TWIN_SRC,
                               namespace=self.NS_MIXED,
                               content="b343 twin source beta")
        self.insert_expiry_row(conn, self.TWIN_SRC, TWIN_LIVE)
        self.insert_belief_head(
            conn, head_id=self.HEAD_MIXED_ID, namespace=self.NS_MIXED,
            topic="topic-b343-mixed", content=self.HEAD_MIXED_CONTENT,
            source_ids=[self.MIX_DEAD, self.MIX_LIVE])
        self.insert_belief_head(
            conn, head_id=self.HEAD_TWIN_ID, namespace=self.NS_MIXED,
            topic="topic-b343-twin", content=self.HEAD_TWIN_CONTENT,
            source_ids=[self.TWIN_SRC])
        conn.commit()
        conn.close()

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "beta", "--namespace", self.NS_MIXED,
            "--for-injection", "--json",
            "--session-id", f"b343-b-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=NOW))
        self.assertNotIn(
            self.HEAD_MIXED_CONTENT, self.contents(env),
            f"a belief head whose EARLIEST source ({self.MIX_DEAD} until "
            f"{LONG_DEAD}) elapsed was DELIVERED at ZMEM_TEST_NOW={NOW} — a "
            f"mixed live+expired source set must gate on the earliest "
            f"source expiry (issue #343); results={self.contents(env)}")
        self.assertNotIn(
            self.HEAD_MIXED_CONTENT, env.get("rendered") or "",
            "the earliest-source-expired head leaked into the rendered fence")
        self.assertIn(
            self.HEAD_TWIN_CONTENT, self.contents(env),
            f"positive control: the all-live twin head must still DELIVER "
            f"(issue #343 fixes the expired path, not the live one); "
            f"results={self.contents(env)}")


class EnvelopePopulationTest(GateHardeningBase):
    """C7/AC7 (#344): on an ENGAGED passive envelope, candidate_ids and
    candidate_lanes describe the SAME population — an id the gate removed
    for expiry appears in BOTH. RED at base (base keys candidate_ids over
    the pre-gate pool but candidate_lanes over the post-gate pool, so the
    expired id is only in candidate_ids). The expired==1 and live-only
    composition assertions are preserving legs (green at base)."""

    LIVE_ID = "00000000-0000-4000-8000-00000000c001"
    DEAD_ID = "00000000-0000-4000-8000-00000000c002"
    LIVE_CONTENT = "c344 live one alpha"
    DEAD_CONTENT = "c344 dead two alpha"
    NS = "project:expiry-344"

    def test_expired_id_in_both_candidate_ids_and_lanes(self):
        d = self.scratch / "envelope"
        d.mkdir()
        conn = self.fresh_v15_conn(d)
        self.insert_memory_row(conn, mid=self.LIVE_ID, namespace=self.NS,
                               content=self.LIVE_CONTENT,
                               ingestion_ts="2026-01-01T00:00:00Z")
        self.insert_expiry_row(conn, self.LIVE_ID, FAR_LIVE, "setting")
        self.insert_memory_row(conn, mid=self.DEAD_ID, namespace=self.NS,
                               content=self.DEAD_CONTENT,
                               ingestion_ts="2026-01-02T00:00:00Z")
        self.insert_expiry_row(conn, self.DEAD_ID, LONG_DEAD, "metric")
        conn.commit()
        conn.close()

        env = self.envelope(self.run_cli(
            d, "recall", "--query", "alpha", "--namespace", self.NS,
            "--for-injection", "--json",
            "--session-id", f"c344-{uuid.uuid4().hex}",
            "--moment", "user_prompt", "--lane", "zcode", now=NOW))

        # Preserving legs first (green at base): the gate still withholds
        # the expired row and still counts it.
        self.assertEqual(
            env.get("expired", None), 1,
            f"expected expired==1, got {env.get('expired', None)!r}")
        self.assertEqual(
            self.contents(env), [self.LIVE_CONTENT],
            f"results must contain only the live row, got "
            f"{self.contents(env)}")

        # The discriminating population law (#344): both fields describe
        # the SAME pre-gate arrival pool.
        self.assertIn(
            self.DEAD_ID, env.get("candidate_ids") or [],
            "the expired id must appear in candidate_ids (the pre-gate "
            "arrival pool) on an engaged envelope")
        lanes_keys = sorted((env.get("candidate_lanes") or {}).keys())
        self.assertIn(
            self.DEAD_ID, env.get("candidate_lanes") or {},
            f"issue #344: candidate_lanes keys={lanes_keys} omit the "
            f"expired id {self.DEAD_ID} that candidate_ids carries — on an "
            f"engaged envelope both fields must describe the SAME "
            f"population (the pre-gate arrival pool), never two pools in "
            f"one envelope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
