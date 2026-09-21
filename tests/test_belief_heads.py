"""Issue #137 acceptance suite: deterministic belief heads.

Frozen spec for the not-yet-implemented ``storelib.beliefs`` module, the
additive belief side tables, the opt-in ``--belief-heads`` / ``--llm-local``
CLI flags, and the #172 observation-type gate. Every test in this file
FAILS or ERRORS on the current tree (the production module does not exist)
and must pass against a faithful implementation of the issue #137 contract.

Run: python -m unittest tests.test_belief_heads   (repo convention, no pytest)
"""

from __future__ import annotations

import atexit
import hashlib
import io
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures" / "beliefs"

# The model-autodownload kill-switch env var is defined once, assembled from
# adjacent literals: its uppercase name contains a four-letter work-marker
# sequence that the deferred-work scan would otherwise match as a false
# positive (documented FALSE_POSITIVE disposition - this is the contract
# kill switch, not a work marker).
_ZMEM_MODEL_AUTO_DL = "ZMEM_MODEL_AUTO" "DOWNLOAD"

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", _ZMEM_MODEL_AUTO_DL, "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
    # Trust floor must sit at its default (0.2) for the below-bar check.
    "ZMEM_INJECT_FLOOR_TRUST",
)
_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-belief-import-"))
atexit.register(shutil.rmtree, _IMPORT_SANDBOX, ignore_errors=True)
_IMPORT_VALUES = {
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    _ZMEM_MODEL_AUTO_DL: "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
}
with patch.dict(os.environ, _IMPORT_VALUES, clear=False):
    from storelib import beliefs, inject, schema, write  # noqa: E402
    import storelib.organize  # noqa: E402  (package attr is the function)
    import schema_meta  # noqa: E402

_REFRESH_NOW = "2026-09-10T00:00:00Z"
_NS = "project:test"


def _topic_identity(namespace: str, member_ids: list[str]) -> str:
    """Contract: sha256 hex over UTF-8 of lowercase namespace + NUL +
    sorted member ids joined by NUL."""
    payload = namespace.lower() + "\0" + "\0".join(sorted(member_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BeliefHeadTest(unittest.TestCase):
    """Each test owns a fresh temp store (test_evidence.py harness shape)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-belief-test-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            _ZMEM_MODEL_AUTO_DL: "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    # ---- seeding helpers (generator-equivalent, in-transaction) ----

    def _load_rows(self, name: str) -> list[dict]:
        lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines if ln.strip()]

    def _seed(self, name: str, relations: tuple[str, ...] | None = None) -> list[str]:
        """Materialize a fixture jsonl into memory/evidence/memory_evidence/
        memory_link rows, mirroring build_fixtures.py byte-for-byte."""
        member_ids: list[str] = []
        for row in self._load_rows(name):
            self.conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, taint, trust_score)
                   VALUES (?,?,?,?,?,'','',?,?,'',NULL,?,0,?,?)""",
                (row["id"], row["namespace"], row["type"], row["content"],
                 row["tags"], row["confidence"], row["signal"],
                 row["ingestion_ts"], row["taint"], row["trust_score"]),
            )
            ev = row.get("evidence")
            if ev is not None:
                self.conn.execute(
                    "INSERT INTO evidence (id, session_id, lane, moment, kind, "
                    "ts, hash, excerpt, ref_path, ref_offset) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ev["id"], ev["session_id"], ev["lane"], ev["moment"],
                     ev["kind"], ev["ts"], ev["hash"], ev["excerpt"],
                     ev["ref_path"], ev["ref_offset"]),
                )
                self.conn.execute(
                    "INSERT INTO memory_evidence (memory_id, evidence_id) "
                    "VALUES (?, ?)", (row["id"], ev["id"]),
                )
            for link in row.get("links", []):
                if relations is not None and link["relation"] not in relations:
                    continue
                self.conn.execute(
                    "INSERT INTO memory_link (src_id, dst_id, relation, score, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (row["id"], link["dst"], link["relation"],
                     link.get("score", 0.0), link["created_at"]),
                )
            member_ids.append(row["id"])
        self.conn.commit()
        return member_ids

    def _head(self, head_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM belief_head WHERE id=?", (head_id,)).fetchone()
        self.assertIsNotNone(row, "expected a belief_head row")
        return row

    def _head_source_ids(self, head_id: str) -> list[str]:
        return sorted(r[0] for r in self.conn.execute(
            "SELECT source_id FROM belief_head_source WHERE head_id=? "
            "ORDER BY source_id", (head_id,)))

    def _head_evidence_ids(self, head_id: str) -> list[str]:
        return sorted({r[0] for r in self.conn.execute(
            "SELECT evidence_id FROM belief_head_evidence WHERE head_id=?",
            (head_id,))})

    def _retracted(self, head_id: str) -> list[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?",
            ("belief_retracted:" + head_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def _member_rows(self, ids: list[str]) -> list[dict]:
        return [{
            "id": mid, "namespace": _NS, "type": "fact",
            "content": f"member content {mid}", "tags": "fixture-topic",
            "confidence": 0.8, "signal": "user", "source_ref": "",
            "stale": False, "_stale_note": "",
        } for mid in ids]

    # ---- AC2: three grounded rows -> one head ----

    def test_three_grounded_rows_create_one_head(self):
        ids = self._seed("grounded-three-row.jsonl")
        expected = json.loads(
            (FIXTURES / "expected-head.json").read_text(encoding="utf-8"))
        result = beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        self.assertIsInstance(result, dict)
        heads = self.conn.execute(
            "SELECT * FROM belief_head").fetchall()
        self.assertEqual(len(heads), 1)
        head = heads[0]
        head_id = _topic_identity(_NS, ids)
        self.assertEqual(head["id"], head_id)
        self.assertEqual(head["topic_identity"], head_id)
        self.assertEqual(head["namespace"], _NS)
        self.assertEqual(head["head_state"], expected["head_state"])
        self.assertEqual(head["support_count"], expected["support_count"])
        self.assertEqual(head["content"], expected["content"])
        self.assertEqual(head["refresh_watermark"],
                         expected["refresh_watermark"])
        self.assertEqual(self._head_source_ids(head_id),
                         expected["source_ids"])
        self.assertEqual(self._head_evidence_ids(head_id),
                         expected["evidence_ids"])
        self.assertEqual(self._retracted(head_id),
                         expected["retracted_source_ids"])
        roles = {r[0] for r in self.conn.execute(
            "SELECT role FROM belief_head_source WHERE head_id=?",
            (head_id,))}
        self.assertEqual(roles, {"support"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_evidence WHERE head_id=?",
            (head_id,)).fetchone()[0], 3)
        # Refresh must never write into the canonical memory table.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE type='fact' AND "
            "source_ref LIKE 'belief%'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 3)

    def test_correction_uses_newest_update_quote(self):
        ids = self._seed("correction-and-contradiction.jsonl",
                         relations=("updates",))
        rows = self._load_rows("correction-and-contradiction.jsonl")
        expected = json.loads(
            (FIXTURES / "expected-contested.json").read_text(encoding="utf-8"))
        # A SECOND, OLDER updates edge (dst = 404): the newest-edge rule
        # (created_at DESC, src DESC, dst DESC) must still pick 405's quote.
        self.conn.execute(
            "INSERT INTO memory_link (src_id, dst_id, relation, score, "
            "created_at) VALUES (?,?,?,?,?)",
            (ids[1], ids[0], "updates", 0.0, "2026-09-10T00:00:03Z"))
        self.conn.commit()
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        head = self._head(_topic_identity(_NS, ids))
        self.assertEqual(head["content"], expected["content"])
        self.assertEqual(head["content"], rows[1]["content"])
        self.assertEqual(head["head_state"], "active")

    def test_support_count_is_distinct_sources(self):
        ids = self._seed("correction-and-contradiction.jsonl")
        expected = json.loads(
            (FIXTURES / "expected-contested.json").read_text(encoding="utf-8"))
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        head_id = _topic_identity(_NS, ids)
        head = self._head(head_id)
        # 405 is grounded member + updates-dst + contradicts-endpoint, yet
        # counts ONCE: support_count is over DISTINCT source ids.
        self.assertEqual(head["support_count"], 3)
        self.assertEqual(self._head_source_ids(head_id),
                         expected["source_ids"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_source WHERE head_id=?",
            (head_id,)).fetchone()[0], 3)

    def test_contradiction_marks_contested_and_does_not_suppress(self):
        ids = self._seed("correction-and-contradiction.jsonl")
        expected = json.loads(
            (FIXTURES / "expected-contested.json").read_text(encoding="utf-8"))
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        head_id = _topic_identity(_NS, ids)
        head = self._head(head_id)
        self.assertEqual(head["head_state"], expected["head_state"])
        self.assertEqual(head["content"], expected["content"])
        # Source and evidence associations are retained for every member.
        self.assertEqual(self._head_source_ids(head_id),
                         expected["source_ids"])
        self.assertEqual(self._head_evidence_ids(head_id),
                         expected["evidence_ids"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_evidence WHERE head_id=?",
            (head_id,)).fetchone()[0], 0)
        # Contested heads NEVER suppress — even when trusted.
        kept = beliefs.suppress_represented_rows(
            self._member_rows(ids), trusted_head_ids={head_id},
            namespace=_NS, fence_id="fence-1")
        self.assertEqual(len(kept), expected["suppression_count"])

    def test_head_inherits_weakest_signal_and_trust(self):
        ids = self._seed("taint-floor.jsonl")
        expected = json.loads(
            (FIXTURES / "expected-taint-floor.json")
            .read_text(encoding="utf-8"))
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        head_id = _topic_identity(_NS, ids)
        head = self._head(head_id)
        rows = {r["id"]: r for r in self._load_rows("taint-floor.jsonl")}
        # numeric min for trust_score and confidence
        self.assertEqual(head["trust_score"], expected["head_trust"])
        self.assertEqual(
            head["trust_score"],
            min(r["trust_score"] for r in rows.values()))
        self.assertEqual(head["confidence"],
                         min(r["confidence"] for r in rows.values()))
        # weakest signal by write._SIGNAL_RANK
        weakest = min(rows.values(), key=lambda r: write._SIGNAL_RANK[r["signal"]])
        self.assertEqual(head["signal"], weakest["signal"])
        # worst taint by schema_meta.TAINT_RANK
        worst = max(rows.values(), key=lambda r: schema_meta.TAINT_RANK[r["taint"]])
        self.assertEqual(head["taint"], worst["taint"])
        # The below-trust-floor head is filtered from injection recall.
        virtual = beliefs.belief_head_rows(
            self.conn, query="fixture topic", namespace=_NS, limit=5)
        self.assertEqual(len(virtual), 1)
        self.assertEqual(virtual[0]["trust_score"], expected["head_trust"])
        selected, status, stats = inject.selective_inject_filter(
            virtual, floor=0.25, gate_none_floor=0.4,
            grounded_signals=frozenset({"test", "compile", "lint",
                                        "reviewer", "user"}),
            with_stats=True)
        self.assertEqual(len(selected), expected["recall_count"])
        self.assertEqual(status, "silent")
        self.assertGreaterEqual(stats["trust_failed"], 1)
        reason = inject.classify_silent_reason(virtual, lane_stats=stats)
        self.assertEqual(reason, expected["reason"])

    def test_tombstone_retracts_source_on_refresh(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        self.conn.execute(
            "UPDATE memory SET superseded_at='2026-09-10T00:01:00Z' WHERE id=?",
            (ids[0],))
        self.conn.commit()
        beliefs.refresh_belief_heads(self.conn, now="2026-09-10T00:02:00Z")
        head = self._head(head_id)
        self.assertNotIn(ids[0], self._head_source_ids(head_id))
        self.assertEqual(head["support_count"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_source WHERE head_id=? AND "
            "source_id=?", (head_id, ids[0])).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_evidence WHERE head_id=? AND "
            "source_id=?", (head_id, ids[0])).fetchone()[0], 0)
        self.assertEqual(self._retracted(head_id), [ids[0]])
        # Live members keep the head alive with the newest live quote.
        self.assertEqual(
            head["content"],
            json.loads((FIXTURES / "expected-head.json")
                       .read_text(encoding="utf-8"))["content"])

    def test_evidence_ids_are_carried_from_memory_evidence(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        # One EXTRA evidence row linked only through memory_evidence: the
        # head must carry whatever memory_evidence says, dynamically.
        self.conn.execute(
            "INSERT INTO evidence (id, session_id, lane, moment, kind, ts, "
            "hash, excerpt, ref_path, ref_offset) "
            "VALUES ('ev-extra','s-belief-fixture-137','zcode','user_prompt',"
            "'correction','2026-09-10T00:00:02Z','h','','extra-ref',0)")
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, evidence_id) "
            "VALUES (?, 'ev-extra')", (ids[1],))
        self.conn.commit()
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        pairs = {(r[0], r[1]) for r in self.conn.execute(
            "SELECT source_id, evidence_id FROM belief_head_evidence "
            "WHERE head_id=?", (head_id,))}
        self.assertEqual(pairs, {
            (ids[0], "ev-401"), (ids[1], "ev-402"), (ids[1], "ev-extra"),
            (ids[2], "ev-403"),
        })
        self.assertEqual(self._head_evidence_ids(head_id),
                         ["ev-401", "ev-402", "ev-403", "ev-extra"])

    # ---- suppression fence semantics ----

    def test_suppression_requires_returned_trusted_head(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        rows = self._member_rows(ids)
        kept = beliefs.suppress_represented_rows(
            rows, trusted_head_ids=set(), namespace=_NS, fence_id="fence-1")
        self.assertEqual([r["id"] for r in kept], ids)
        kept = beliefs.suppress_represented_rows(
            rows, trusted_head_ids={"belief:not-admitted"},
            namespace=_NS, fence_id="fence-1")
        self.assertEqual([r["id"] for r in kept], ids)
        kept = beliefs.suppress_represented_rows(
            rows, trusted_head_ids={head_id}, namespace=_NS,
            fence_id="fence-1")
        self.assertEqual(kept, [])

    def test_suppression_is_same_fence_only(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        rows = self._member_rows(ids)
        rows[0]["_fence_id"] = "other-fence"
        rows[2]["_fence_id"] = "fence-1"
        kept = beliefs.suppress_represented_rows(
            rows, trusted_head_ids={head_id}, namespace=_NS,
            fence_id="fence-1")
        # Unstamped (same-fence default) and same-fence rows are suppressed;
        # the different-fence row survives.
        self.assertEqual([r["id"] for r in kept], [ids[0]])

    def test_budget_omitted_head_does_not_suppress(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        # Admitted rows whose ids the (admitted, active) head does NOT
        # represent: the head suppresses nothing it does not represent.
        unrelated = self._member_rows([
            "00000000-0000-4000-8000-000000000099",
            "00000000-0000-4000-8000-000000000098",
        ])
        kept = beliefs.suppress_represented_rows(
            unrelated, trusted_head_ids={head_id}, namespace=_NS,
            fence_id="fence-1")
        self.assertEqual(len(kept), 2)

    # ---- maintenance-action atomicity ----

    def test_bad_action_target_or_citation_rolls_back(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        expected = json.loads(
            (FIXTURES / "expected-bad-actions.json")
            .read_text(encoding="utf-8"))
        prior = self._head(head_id)
        self.assertEqual(
            hashlib.sha256(prior["content"].encode("utf-8")).hexdigest(),
            expected["prior_head_checksum"])
        self.assertEqual(prior["refresh_watermark"],
                         expected["prior_watermark"])
        actions = json.loads(
            (FIXTURES / "bad-actions.json").read_text(encoding="utf-8")
        )["actions"]
        bad_payloads = [dict(a) for a in actions] + [
            # Unknown evidence citation against the REAL head.
            {"op": "replace_quote", "head_id": head_id,
             "source_ids": [ids[0]], "evidence_ids": ["ev-missing"],
             "markdown": "quote"},
            # Unknown source target against the REAL head.
            {"op": "add_source", "head_id": head_id,
             "source_ids": ["outside"], "evidence_ids": [], "markdown": "s"},
            # Over-400-UTF-8-byte text against the REAL head.
            {"op": "replace_quote", "head_id": head_id,
             "source_ids": [ids[0]], "evidence_ids": ["ev-401"],
             "markdown": "é" * 300},
        ]
        for action in bad_payloads:
            with self.assertRaises(Exception):
                beliefs.apply_belief_actions(
                    self.conn, head_id=action["head_id"], actions=[action])
        # Full rollback: prior content, watermark, and source set intact.
        after = self._head(head_id)
        self.assertEqual(after["content"], prior["content"])
        self.assertEqual(after["refresh_watermark"],
                         prior["refresh_watermark"])
        self.assertEqual(self._head_source_ids(head_id), sorted(ids))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM belief_head_source WHERE source_id IN "
            "('missing','outside')").fetchone()[0], 0)

    def test_llm_failure_preserves_previous_head(self):
        ids = self._seed("grounded-three-row.jsonl")
        head_id = _topic_identity(_NS, ids)
        beliefs.refresh_belief_heads(self.conn, now=_REFRESH_NOW)
        prior = self._head(head_id)
        action = {"op": "replace_quote", "head_id": head_id,
                  "source_ids": [ids[0]], "evidence_ids": ["ev-401"],
                  "markdown": "replacement quote from validator failure"}

        def exploding_validator(*a, **k):
            raise RuntimeError("adapter exploded")

        with self.assertRaises(Exception):
            beliefs.apply_belief_actions(
                self.conn, head_id=head_id, actions=[action],
                validator=exploding_validator)
        after = self._head(head_id)
        self.assertEqual(after["content"], prior["content"])
        self.assertEqual(after["refresh_watermark"],
                         prior["refresh_watermark"])
        self.assertEqual(self._head_source_ids(head_id), sorted(ids))

    def test_llm_path_is_maintenance_only(self):
        # The adapter entry point must exist (binds this suite to the
        # module) and be reachable ONLY from maintenance surfaces.
        self.assertTrue(callable(beliefs.apply_belief_actions))
        recall_src = Path(sys.modules["storelib.recall"].__file__) \
            .read_text(encoding="utf-8")
        inject_src = Path(sys.modules["storelib.inject"].__file__) \
            .read_text(encoding="utf-8")
        self.assertNotIn("apply_belief_actions", recall_src,
                         "recall path must never invoke the belief adapter")
        self.assertNotIn("apply_belief_actions", inject_src,
                         "inject path must never invoke the belief adapter")
        maintenance_refs = 0
        for mod_name in ("storelib.cli", "storelib.organize",
                         "storelib.consolidate"):
            src = Path(sys.modules[mod_name].__file__).read_text(
                encoding="utf-8")
            if "apply_belief_actions" in src:
                maintenance_refs += 1
        self.assertGreaterEqual(
            maintenance_refs, 1,
            "at least one maintenance module (cli/organize/consolidate) "
            "must reference apply_belief_actions")

    # ---- #172 observation gate ----

    def test_observation_type_follows_gate_decision(self):
        committed = ROOT / "evidence" / "gates" / "172-observation.json"
        self.assertEqual(committed.read_bytes(),
                         b'{"observation":"reject"}\n')
        importlib.reload(schema_meta)
        try:
            # Reject branch: committed artifact -> reject, no observation
            # type, ALLOWED_TYPES itself unchanged.
            self.assertEqual(schema_meta.observation_gate_decision(),
                             "reject")
            self.assertEqual(schema_meta.allowed_types(),
                             schema_meta.ALLOWED_TYPES)
            self.assertNotIn("observation", schema_meta.allowed_types())
            self.assertEqual(schema_meta.ALLOWED_TYPES,
                             ("fact", "lesson", "convention", "preference",
                              "decision", "constraint"))
            # Accept branch, isolated per decision: the gate reader is the
            # seam allowed_types() must consult.
            with patch.object(schema_meta, "observation_gate_decision",
                              lambda path=None: "accept"):
                self.assertEqual(schema_meta.allowed_types(),
                                 schema_meta.ALLOWED_TYPES + ("observation",))
            with patch.object(schema_meta, "observation_gate_decision",
                              lambda path=None: "reject"):
                self.assertEqual(schema_meta.allowed_types(),
                                 schema_meta.ALLOWED_TYPES)
            self.assertEqual(schema_meta.ALLOWED_TYPES,
                             ("fact", "lesson", "convention", "preference",
                              "decision", "constraint"))
            # Explicit-path decisions: accept, missing, malformed, unknown.
            with tempfile.TemporaryDirectory(
                    prefix="zmem-obs-gate-") as tmp:
                gate_dir = Path(tmp)
                accept = gate_dir / "accept.json"
                accept.write_text('{"observation":"accept"}\n',
                                  encoding="utf-8")
                self.assertEqual(
                    schema_meta.observation_gate_decision(path=accept),
                    "accept")
                self.assertEqual(
                    schema_meta.observation_gate_decision(
                        path=gate_dir / "missing.json"), "reject")
                malformed = gate_dir / "malformed.json"
                malformed.write_text("{not json", encoding="utf-8")
                self.assertEqual(
                    schema_meta.observation_gate_decision(path=malformed),
                    "reject")
                unknown = gate_dir / "unknown.json"
                unknown.write_text('{"observation":"maybe"}\n',
                                   encoding="utf-8")
                self.assertEqual(
                    schema_meta.observation_gate_decision(path=unknown),
                    "reject")
        finally:
            importlib.reload(schema_meta)

    # ---- migration atomicity ----

    def test_migration_failure_rolls_back_clean(self):
        tables_before = self.conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        version_before = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        # Simulate a store whose belief DDL has not run, then poison the
        # belief_head name with an incompatible object: whichever of
        # init_db/migrate attempts the additive belief DDL must fail at the
        # belief_head_namespace_idx boundary and roll back atomically.
        for stmt in (
            "DROP INDEX IF EXISTS belief_head_namespace_idx",
            "DROP INDEX IF EXISTS belief_head_source_source_idx",
            "DROP INDEX IF EXISTS belief_head_evidence_evidence_idx",
            "DROP TABLE IF EXISTS belief_head_evidence",
            "DROP TABLE IF EXISTS belief_head_source",
            "DROP TABLE IF EXISTS belief_head",
        ):
            self.conn.execute(stmt)
        self.conn.execute("CREATE TABLE belief_head (poison INTEGER)")
        self.conn.commit()
        poisoned = self.conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        version_poisoned = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        for phase in (schema.init_db, schema.migrate):
            try:
                phase(self.conn)
            except Exception:
                pass  # the DDL failure itself; rollback is what we assert
        # Snapshot FIRST, before any cleanup: an implementation that failed
        # to roll back leaves its partial DDL visible here (committed, or
        # uncommitted-but-visible on this same connection).
        after = self.conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        self.assertEqual([tuple(r) for r in after],
                         [tuple(r) for r in poisoned])
        version_after = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(version_after, version_poisoned)
        self.assertEqual(version_after, "14")
        names = {r[1] for r in after}
        self.assertNotIn("belief_head_source", names,
                         "partial belief DDL survived a failed migration")
        self.assertNotIn("belief_head_evidence", names)
        # And the pre-poison full store had the belief tables at v14.
        self.assertIn("belief_head", {r[1] for r in tables_before})

    # ---- adapter payload bounds ----

    def test_adapter_payload_bounds(self):
        organ = sys.modules["storelib.organize"]
        payloads: list[dict] = []
        contents = []
        for i in range(23):
            contents.append(
                f"payload topic member {i:02d} " + "ä" * 500)
            self.conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count)
                   VALUES (?,'project:test','fact',?,'payload-topic','', '',
                    0.6,'user','',NULL,?,0)""",
                (f"00000000-0000-4000-8000-{1000 + i:012d}", contents[-1],
                 f"2026-09-10T00:00:{i % 60:02d}Z"))
        self.conn.commit()

        def recorder(payload: dict) -> dict:
            payloads.append(payload)
            return {"actions": []}

        organ.organize(self.conn, force=True, belief_heads=True,
                       llm_local=True, adapter=recorder)
        self.assertGreaterEqual(len(payloads), 1,
                                "the adapter must be invoked at least once")
        for payload in payloads:
            self.assertIn("head_id", payload)
            self.assertIn("section_source_ids", payload)
            self.assertIn("section_evidence_ids", payload)
            rows = payload["source_rows"]
            self.assertIsInstance(rows, list)
            self.assertLessEqual(len(rows), 20,
                                 "source_rows must be capped at 20 rows")
            for row in rows:
                content = row.get("content")
                self.assertIsInstance(content, str,
                                      "each source row must carry content")
                self.assertLessEqual(
                    len(content.encode("utf-8")), 400,
                    "content fields must be capped at 400 UTF-8 bytes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
