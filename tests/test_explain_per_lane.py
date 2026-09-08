"""AC4 acceptance test for issue #113: per-lane numbers in `recall --explain`.

Pins the explain surface additions:
- ``found`` verdicts carry ``detail["lanes"]`` (per-lane relevance numbers:
  numeric ``lex``; numeric ``cos`` when the embedding lane is live) and the
  envelope reports the resolved inject-gate thresholds as ``lane_floors``
  (lex/cos/ent — the calibrated defaults 0.30/0.50/0.50);
- the new ``link_expansion`` verdict: with a query matching ONLY P (limit 1),
  the linked neighbor Q — in no pool because it shares no query token and has
  no vector in the KNN window — enters context only via the 1-hop link walk,
  and explain names that path (parent row, relation, link score) instead of
  ``not_in_pool``; the same verdict fires for an explicit ``--target`` Q;
- the link-aware explain path is ZERO-WRITE: the store file's bytes are
  identical before and after the call.

Fixture: two rows in one namespace, P lexically + vector matched by the
query, Q linked FROM P (``related``) via the real storelib.links add_link
helper, with NO memory_vec row so the vec lane can never pull Q into a pool
(an orthogonal zero-cosine vector still rides the KNN window — the only way Q
stays pool-less, which is the shape the link_expansion verdict exists for).

Run: python tests/test_explain_per_lane.py   (no pytest — repo convention)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Env pin — BEFORE any storelib import. storelib freezes STORE_PATH on first
# import, so an unpinned in-process connect() would touch the operator's real
# home store. Same contract as tests/test_explain_recall.py's module pin.
# ---------------------------------------------------------------------------
import os
import sys
import tempfile  # noqa: F401  (used by the pin above)
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP_KEEPALIVE = tempfile.TemporaryDirectory(prefix="zmem-explain-lanes-")
_TMP = _TMP_KEEPALIVE.name  # TemporaryDirectory cleans up at exit
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "nonexistent-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
os.environ["ZMEM_TEST_NOW"] = "2026-06-01T00:00:00Z"
for _k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR",
           "ZMEM_INJECT_FLOOR_LEX", "ZMEM_INJECT_FLOOR_COS",
           "ZMEM_INJECT_FLOOR_ENT"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))

import io  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import unittest  # noqa: E402
import contextlib  # noqa: E402

import embed_profiles  # noqa: E402  (env pinned above)
import storelib.recall as recall_mod  # noqa: E402
from storelib.links import add_link  # noqa: E402
from storelib.schema import _prepare_store, connect  # noqa: E402

NS = "project:explainlane"
# Hex/uuid-shaped fixture ids: _resolve_explain_targets resolves UUID-shaped
# --target values by id prefix (anything else is treated as a CONTENT
# fragment), so the target-mode test needs id-shaped ids.
P_ID = "a0000000-0000-4000-8000-000000000001"
Q_ID = "a0000000-0000-4000-8000-000000000002"
P_CONTENT = "alpha deployment pipeline runbook steps"
Q_CONTENT = "beta rollback procedure for the cluster"  # zero query-token overlap
QUERY = "alpha deployment"      # matches ONLY P
LINK_SCORE = 0.9

# The explain JSON envelope prints one object: {"results": ..., "explain":
# {query, ..., verdicts, lane_floors}} — parsed with json.loads below.


class ExplainPerLaneTest(unittest.TestCase):
    """One seeded store shared by the per-lane/link assertions."""

    @classmethod
    def setUpClass(cls):
        cls.store = os.environ["ZMEM_STORE"]
        conn = connect()
        _prepare_store(conn)
        rows = [
            (P_ID, P_CONTENT),
            (Q_ID, Q_CONTENT),
        ]
        for mid, content in rows:
            conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    source_hash, confidence, signal, valid_from,
                    valid_until, update_of, taint, superseded_at,
                    ingestion_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, NS, "fact", content, "explaintest", "session:seed", "",
                 0.9, "test", "2026-01-01T00:00:00Z", "", "",
                 "trusted_internal", None, "2026-01-01T00:00:00Z"),
            )
        # P carries the real fake-profile vector (content + memory_vec) so the
        # cosine lane is live for the found verdict. Q gets NO vector: with a
        # vec row it would ride the KNN window at cosine 0.0 and score into
        # the deep pool (below_limit) instead of staying pool-less for the
        # link walk to surface. If sqlite-vec is unavailable the insert fails
        # and the cos assertions below degrade to the lane-absent contract.
        cls.vec_lane_live = True
        emb = embed_profiles.fake_embed(P_CONTENT)
        conn.execute(
            "UPDATE memory SET embedding=?, embedding_model=?, embedded_at=? "
            "WHERE id=?",
            (emb, embed_profiles.embedding_model_name("fake"),
             "2026-01-01T00:00:00Z", P_ID),
        )
        try:
            conn.execute(
                "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                (emb, P_ID),
            )
        except sqlite3.OperationalError:
            cls.vec_lane_live = False
        # Link P -> Q (related) through the real links helper (validates ids,
        # same-namespace, relation enum; INSERT OR IGNORE + explicit commit).
        inserted = add_link(conn, P_ID, Q_ID, "related", score=LINK_SCORE)
        assert inserted, "fixture link insert unexpectedly deduped"
        conn.commit()
        conn.close()

    def _explain(self, **over) -> dict:
        """Run explain (json mode) capturing the printed envelope."""
        conn = connect()
        self.addCleanup(conn.close)
        kwargs = dict(query=QUERY, namespace=NS, limit=1, link_hops=1,
                      link_budget=2, as_json=True)
        kwargs.update(over)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall_mod.explain_recall(conn, **kwargs)
        doc = json.loads(buf.getvalue())
        self.assertIn("explain", doc)
        return doc

    @staticmethod
    def _verdicts_by_id(doc: dict) -> dict:
        return {v["id"]: v for v in doc["explain"]["verdicts"]}

    def test_found_verdict_carries_lane_numbers_and_floors(self):
        doc = self._explain()
        exp = doc["explain"]
        self.assertTrue(exp["hybrid"])
        by_id = self._verdicts_by_id(doc)
        self.assertIn(P_ID, by_id)
        v = by_id[P_ID]
        self.assertEqual(v["reason"], "found")
        self.assertEqual(v["rank"], 1)
        lanes = v["detail"]["lanes"]
        self.assertGreater(float(lanes["lex"]), 0.0,
                           "the lexically-matched row must report a positive "
                           "lexical lane")
        if self.vec_lane_live and embed_profiles.PROFILES and _embeddings_ok():
            self.assertIsNotNone(lanes["cos"])
            self.assertGreater(float(lanes["cos"]), 0.0,
                               "the vector-matched row must report a numeric "
                               "cosine lane")
        # The resolved gate thresholds ride the envelope next to the numbers.
        # Issue #115 added the trust floor as a fourth entry.
        self.assertEqual(exp["lane_floors"],
                         {"lex": 0.30, "cos": 0.50, "ent": 0.50, "trust": 0.2})
        for floor in exp["lane_floors"].values():
            self.assertIsInstance(floor, float)

    def test_linked_neighbor_reports_link_expansion(self):
        doc = self._explain()
        by_id = self._verdicts_by_id(doc)
        self.assertIn(Q_ID, by_id,
                      "the linked neighbor must be explained, not dropped")
        v = by_id[Q_ID]
        self.assertEqual(v["reason"], "link_expansion")
        self.assertIsNone(v["rank"])
        self.assertIsNone(v["score"])
        self.assertEqual(v["detail"]["link_of"], P_ID)
        self.assertEqual(v["detail"]["link_relation"], "related")
        self.assertEqual(v["detail"]["link_score"], LINK_SCORE)
        # Q must NOT have scored into any pool — the link walk is its only way
        # in (a below_limit/not_in_pool Q here would mean the fixture drifted).
        self.assertNotIn("not_in_pool", [x["reason"]
                                         for x in doc["explain"]["verdicts"]])

    def test_target_mode_miss_becomes_link_expansion(self):
        doc = self._explain(target=Q_ID)
        verdicts = doc["explain"]["verdicts"]
        self.assertEqual(len(verdicts), 1)
        v = verdicts[0]
        self.assertEqual(v["id"], Q_ID)
        self.assertEqual(v["reason"], "link_expansion")
        self.assertEqual(v["detail"]["link_of"], P_ID)

    def test_link_aware_explain_is_zero_write(self):
        before = Path(self.store).read_bytes()
        conn = connect()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                recall_mod.explain_recall(
                    conn, query=QUERY, namespace=NS, limit=1, link_hops=1,
                    link_budget=2, as_json=True)
        finally:
            conn.close()
        after = Path(self.store).read_bytes()
        self.assertEqual(before, after,
                         "the link-aware explain path must write NOTHING")


def _embeddings_ok() -> bool:
    """True when the embeddings runtime reports available (fake profile)."""
    import embeddings
    return bool(embeddings and embeddings.is_available())


if __name__ == "__main__":
    unittest.main(verbosity=2)
