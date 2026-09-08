"""AC3 acceptance test for issue #113: three-signal relevance composition.

The composite score's relevance term is now the MAX of the measured per-lane
values (lexical coverage x rank-ratio / vec cosine / entity proxy) instead of
the saturated ``abs(fts_rank)/(1+abs(fts_rank))`` back-solve — so a candidate
with BOTH a lexical hit and a strong cosine is scored on BOTH (#113's exact
acceptance line). Pins, at three levels:

  A. compute_score unit level — the ``relevance=`` kwarg drives exactly the
     W_BM25 (0.55) share of the score, and ``relevance=None`` keeps the legacy
     ar/(1+ar) arithmetic byte-for-byte (one exact float, 12 decimals).
  B. _recall_one_tier integration — a real 3-row store (real write path, so
     FTS rows + memory_vec blobs exist) under a stubbed embedding provider
     whose geometry is hand-placed: the both-terms row shows lex > 0 AND
     cos > 0 with rel == max(lex, cos); a one-term row has lex == 0.0
     (measured-but-ineligible under the >= 2-term rule) while its cosine
     still participates — the strong-cosine one-term row OUTSCORES the
     otherwise-identical weak-cosine row.
  C. inject gate precedence — trust first, relevance floors second: trusted
     rows whose only measured lane fails name ``below-relevance``; untrusted
     rows still name ``below-bar``; the default filter call stays a 2-tuple;
     a row with NO measured lanes (link-expansion shape) is exempt.

Run: python tests/test_score_composition.py   (no pytest — repo convention)
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

_TMP = tempfile.mkdtemp(prefix="zmem-score-comp-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "nonexistent-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
# No auto link generation in the fixture (edges would only add noise); the
# write path itself stays real. Same knob as tests/fixtures/eval_store.py.
os.environ["ZMEM_LINK_THRESHOLD"] = "1.01"
for _k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR",
           "ZMEM_INJECT_FLOOR_LEX", "ZMEM_INJECT_FLOOR_COS",
           "ZMEM_INJECT_FLOOR_ENT"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))

import calendar  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import math  # noqa: E402
import struct  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import storelib.recall as recall_mod  # noqa: E402  (env pinned above)
import storelib.write as write_mod  # noqa: E402
from storelib.inject import classify_silent_reason, selective_inject_filter  # noqa: E402
from storelib.schema import _prepare_store, connect  # noqa: E402
from storelib.write import add_memory  # noqa: E402

# Deterministic scoring clock: ingestion instant == now => recency == 1.0 for
# every row, so score deltas below come from relevance lanes ONLY.
PIN_TS = "2026-06-01T00:00:00Z"
FIXED_NOW = float(calendar.timegm((2026, 6, 1, 0, 0, 0)))


# ---------------------------------------------------------------------------
# A. compute_score unit level
# ---------------------------------------------------------------------------

class ComputeScoreRelevanceLaneTest(unittest.TestCase):
    """``relevance`` kwarg: exactly the 0.55-weighted share; None = legacy."""

    ROW = {"confidence": 0.9, "retrieval_count": 0, "surfaced_count": 0,
           "ingestion_ts": PIN_TS}

    def test_relevance_kwarg_drives_exactly_the_bm25_share(self):
        # Same synthetic row, same clock: the ONLY difference is the lane
        # value 0.9 vs 0.1, so the score difference must be exactly
        # W_BM25 * (0.9 - 0.1) — the lane value drives the relevance term.
        hi = recall_mod.compute_score(self.ROW, None, FIXED_NOW, relevance=0.9)
        lo = recall_mod.compute_score(self.ROW, None, FIXED_NOW, relevance=0.1)
        self.assertAlmostEqual(hi - lo, 0.55 * 0.8, places=12,
                               msg="relevance lane must drive W_BM25 exactly")

    def test_relevance_none_reproduces_legacy_arithmetic(self):
        # fts_rank -1.5 => ar = 1.5, ar/(1+ar) = 0.6;
        # 0.55*0.6 + 0.20*0.9 + 0.15*1.0 + 0.10*0 = 0.66 exactly (12 dp).
        score = recall_mod.compute_score(self.ROW, -1.5, FIXED_NOW)
        self.assertAlmostEqual(score, 0.66, places=12,
                               msg="relevance=None must keep the legacy "
                                   "ar/(1+ar) formula byte-identically")


# ---------------------------------------------------------------------------
# B. _recall_one_tier integration — the AC3 core
# ---------------------------------------------------------------------------

NS = "project:lanecompose"
QUERY = "kubernetes tolerations"                       # 2 normalized terms
ROW_BOTH = "kubernetes tolerations schedule tainted nodes"        # both terms
ROW_ONE_STRONG = "kubernetes quota defaults for the staging cluster"  # 1 term
ROW_ONE_WEAK = "kubernetes upgrade channel pinning cadence"       # 1 term

# Hand-placed 16-dim geometry (embed_profiles.FAKE_DIM == 16; the stub packs
# exactly like the fake profile: struct.pack("<16f", ...)). Q = unit(0.6*e1 +
# 1.0*e2 + 0.05*e4); rows sit on e1 / e2 / unit(0.05*e2 + 1.0*e4):
#   cos(Q, both)    ~ 0.514   (lexically matched AND cosine-matched)
#   cos(Q, strong)  ~ 0.857   (one-term row, STRONG cosine)
#   cos(Q, weak)    ~ 0.086   (one-term row, weak-but-positive cosine)
DIM = 16


def _axis(i: int, scale: float = 1.0) -> list[float]:
    vals = [0.0] * DIM
    vals[i] = scale
    return vals


def _unit_blob(vals: list[float]) -> bytes:
    norm = math.sqrt(sum(v * v for v in vals))
    return struct.pack(f"<{DIM}f", *(v / norm for v in vals))


_BLOB_BOTH = _unit_blob(_axis(0))
_BLOB_STRONG = _unit_blob(_axis(1))
_BLOB_WEAK = _unit_blob([0.0, 0.05, 0.0, 1.0] + [0.0] * (DIM - 4))
_BLOB_QUERY = _unit_blob([0.6, 1.0, 0.0, 0.05] + [0.0] * (DIM - 4))


class _StubEmbeddings:
    """Deterministic embedding provider (no model files): exact-text lookup.

    is_available() -> True so the hybrid vec lane runs; embed_text returns
    the hand-placed blob for the query and each seeded content. Anything
    else gets the weak blob — the stub must never return None (a None would
    silently degrade the lane this test exists to measure)."""

    def is_available(self) -> bool:
        return True

    def embed_text(self, text: str):
        return {
            QUERY: _BLOB_QUERY,
            ROW_BOTH: _BLOB_BOTH,
            ROW_ONE_STRONG: _BLOB_STRONG,
            ROW_ONE_WEAK: _BLOB_WEAK,
        }.get((text or "").strip(), _BLOB_WEAK)


STUB = _StubEmbeddings()


class RecallLaneCompositionTest(unittest.TestCase):
    """One real 3-row store, one namespace, scored via _recall_one_tier."""

    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        _prepare_store(cls.conn)
        cls.ids: dict[str, str] = {}
        # Seed through the REAL write path with the stub as the embedding
        # provider (storelib.write reads its own by-value import), so FTS
        # rows, content_norm, and memory_vec blobs all exist exactly as a
        # production add would leave them.
        original = write_mod._embeddings
        write_mod._embeddings = STUB
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for key, content in (("both", ROW_BOTH),
                                     ("one_strong", ROW_ONE_STRONG),
                                     ("one_weak", ROW_ONE_WEAK)):
                    cls.ids[key] = add_memory(
                        cls.conn, namespace=NS, type_="fact", content=content,
                        tags="lanetest", signal="test", confidence=0.9,
                        source_ref="session:lane-seed",
                    )
        finally:
            write_mod._embeddings = original
        # Equalize the clock-dependent inputs (the real add stamps wall-clock
        # now; pin all three rows to the fixed scoring instant so recency is
        # identical and score deltas isolate the relevance lanes).
        cls.conn.execute(
            "UPDATE memory SET ingestion_ts=?, valid_from=? WHERE namespace=?",
            (PIN_TS, PIN_TS, NS),
        )
        cls.conn.commit()
        cls.addClassCleanup(cls.conn.close)

    def _recall(self):
        recall_mod._embeddings = STUB
        self.addCleanup(setattr, recall_mod, "_embeddings",
                        recall_mod._embeddings)
        return recall_mod._recall_one_tier(
            self.conn, query=QUERY, ns_list=[NS], limit=5,
            min_confidence=None, hybrid=True, now_epoch=FIXED_NOW,
            collect_lanes=True,
        )

    def test_both_terms_row_carries_lex_and_cos_lanes(self):
        scored = self._recall()
        rows = {item["id"]: item for _score, item in scored}
        both = rows[self.ids["both"]]
        lanes = both["_lanes"]
        self.assertGreater(lanes["lex"], 0.0, "both-terms row: lexical lane "
                           "must be measured positive")
        self.assertGreater(lanes["cos"], 0.0, "both-terms row: cosine lane "
                           "must be measured positive (AC3: both lanes live)")
        expected_rel = max(v for v in (lanes["lex"], lanes["cos"],
                                       lanes["entity"]) if v is not None)
        self.assertAlmostEqual(lanes["rel"], expected_rel, places=12,
                               msg="rel must be the max of measured lanes")

    def test_per_pk_fallback_measures_cos_when_knn_misses(self):
        # Issue #113 review round (tc-3): a candidate the KNN generator did
        # NOT surface must still get a measured cosine lane via the per-PK
        # embedding lookup (recall.py:1011-1024). Simulate a KNN total miss
        # (vec lane returns nothing) — every row then rides the fallback,
        # reading the stored memory.embedding blob.
        both_id = self.ids["both"]
        self.assertGreater(
            float(self.conn.execute(
                "SELECT length(embedding) FROM memory WHERE id = ?",
                (both_id,)).fetchone()[0]), 0,
            "fixture precondition: memory.embedding must be populated")
        with mock.patch.object(recall_mod, "_vec_knn_in_namespace",
                               lambda *a, **k: []):
            scored = self._recall()
        rows = {item["id"]: item for _score, item in scored}
        both = rows[both_id]
        self.assertGreater(both["_lanes"]["cos"], 0.0,
                           "per-PK fallback must measure the cosine lane "
                           "when the KNN lane surfaces nothing")
        self.assertGreater(both["_rel_cos"], 0.0)
        # The fallback value is the same primitive the KNN path produces:
        # cos(Q, both) ~ 0.514 for the hand-placed geometry.
        self.assertAlmostEqual(both["_rel_cos"], 0.514, delta=0.05)

    def test_one_term_row_lex_is_measured_zero(self):
        scored = self._recall()
        rows = {item["id"]: item for _score, item in scored}
        strong = rows[self.ids["one_strong"]]
        self.assertEqual(strong["_lanes"]["matched"], 1)
        self.assertLess(strong["_lanes"]["cov"], 1.0)
        # Measured-but-ineligible under the >= 2 distinct terms rule: exactly
        # 0.0 (not None) so the inject gate's lex floor judges it.
        self.assertEqual(strong["_lanes"]["lex"], 0.0)
        self.assertEqual(strong["_rel_lex"], 0.0)

    def test_strong_cosine_outscores_identical_weak_cosine_row(self):
        # AC3's exact line: both one-term rows share confidence/signal/
        # recency/popularity and the same (zero) lexical lane; the ONLY
        # difference is the cosine lane value — and it moves the score.
        scored = self._recall()
        rows = {item["id"]: item for _score, item in scored}
        strong, weak = rows[self.ids["one_strong"]], rows[self.ids["one_weak"]]
        self.assertGreater(strong["_lanes"]["cos"], 0.7)
        self.assertLess(weak["_lanes"]["cos"], strong["_lanes"]["cos"])
        self.assertGreater(strong["_score"], weak["_score"],
                           "a lexically-matched row must also be scored on "
                           "its cosine (composition = max of lanes)")


# ---------------------------------------------------------------------------
# C. inject gate precedence
# ---------------------------------------------------------------------------

class _RrBindsStub:
    """Per-row hand-placed blobs for the rr-binds fixture (issue #113 tc-2).

    The write path dedups on embedding cosine >= 0.85, so every fixture row
    needs its OWN blob: the shared STUB above returns one weak blob for any
    unknown text, which silently collapses this fixture into pre-existing
    rows (probed 2026-09-07: add_memory returned another row's id). Geometry:
    query = _BLOB_QUERY (0.6 e0 + 1.0 e1 + 0.05 e3); target = e15 so its
    cosine lane is 0.0 and ONLY the lexical lane can admit it; decoy_i =
    unit(e_(2+i) + 0.9 e0) — pairwise cos ~0.45, cos to query ~0.34, cos to
    the target 0.0, all far under the 0.85 dedup threshold."""

    def __init__(self, blobs: dict):
        self._blobs = blobs

    def is_available(self) -> bool:
        return True

    def embed_text(self, text: str):
        return self._blobs.get((text or "").strip(), _BLOB_WEAK)


# Target: both query terms at tf=1 spread across a 150-token unique-filler
# body — long enough that its bm25 sinks well under the short tf=4 decoys'
# (rr needs real headroom under 0.30, and bm25's tf saturation caps how far
# tf alone can go).
_RR_TARGET_CONTENT = (QUERY + " "
                      + " ".join(f"filler{i}" for i in range(250)))
_RR_DECOY_CONTENTS = [
    f"kubernetes kubernetes kubernetes kubernetes tolerations "
    f"decoy{i} zzz{i} qqq{i}" for i in range(8)
]
_RR_BLOBS = {
    QUERY: _BLOB_QUERY,
    _RR_TARGET_CONTENT: _unit_blob(_axis(15)),
}
for _i, _c in enumerate(_RR_DECOY_CONTENTS):
    _RR_BLOBS[_c] = _unit_blob(
        [0.9 if _j == 0 else (1.0 if _j == 2 + _i else 0.0)
         for _j in range(DIM)])
RR_STUB = _RrBindsStub(_RR_BLOBS)


class RrBindsIntegrationTest(unittest.TestCase):
    """Integration pin (issue #113 review round, tc-2): a cov=1.0 row that is
    NOT pool-best gets lex = 1.0 x rr with rr << 1, so the 0.30 lexical floor
    actually TRIPS through the real recall pipeline — short tf-heavy decoys
    dominate |bm25| and dilute the long target's rank ratio."""

    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        _prepare_store(cls.conn)
        original = write_mod._embeddings
        write_mod._embeddings = RR_STUB
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                # Target: matches BOTH query terms (cov = 1.0, eligible) but
                # long content dilutes its term frequency -> deep pool rank.
                cls.target_id = add_memory(
                    cls.conn, namespace=NS, type_="fact",
                    content=_RR_TARGET_CONTENT,
                    tags="rrbind", signal="test", confidence=0.9,
                    source_ref="session:rr-seed",
                )
                # Decoys: short docs with tf=4 on term 1 AND term 2 present
                # -> high |bm25| on both lanes; they dominate the pool-best
                # rank the rr ratio uses (a decoy WITHOUT "tolerations"
                # cannot bind: the rare term's idf hands the long target
                # the pool-best bm25 regardless of dilution).
                for content in _RR_DECOY_CONTENTS:
                    add_memory(
                        cls.conn, namespace=NS, type_="fact",
                        content=content,
                        tags="rrbind", signal="test", confidence=0.9,
                        source_ref="session:rr-seed",
                    )
        finally:
            write_mod._embeddings = original
        cls.conn.execute(
            "UPDATE memory SET ingestion_ts=?, valid_from=? WHERE namespace=?",
            (PIN_TS, PIN_TS, NS),
        )
        cls.conn.commit()
        cls.addClassCleanup(cls.conn.close)

    def test_deep_pool_cov_full_row_trips_the_lexical_floor(self):
        orig_emb = recall_mod._embeddings
        recall_mod._embeddings = RR_STUB
        self.addCleanup(setattr, recall_mod, "_embeddings", orig_emb)
        scored = recall_mod._recall_one_tier(
            self.conn, query=QUERY, ns_list=[NS], limit=25,
            min_confidence=None, hybrid=True, now_epoch=FIXED_NOW,
            collect_lanes=True,
        )
        # The fixture must survive write-path dedup: 9 rows written, so the
        # pool must still hold most of them (a dedup collapse silently
        # empties the decoy pool and pins rr at 1.0).
        self.assertGreaterEqual(
            len(scored), 7,
            "fixture collapsed: write-path dedup ate the decoy pool")
        rows = {item["id"]: item for _score, item in scored}
        target = rows[self.target_id]
        lanes = target["_lanes"]
        self.assertEqual(lanes["cov"], 1.0, "target matches both terms")
        self.assertLess(lanes["rr"], 1.0, "target is NOT pool-best")
        self.assertLess(
            lanes["lex"], 0.30,
            f"cov*rr must trip the 0.30 lexical floor; got {lanes}")
        selected, status, stats = selective_inject_filter(
            [target], with_stats=True)
        self.assertEqual(selected, [], "lex below floor -> relevance-dropped")
        self.assertEqual(stats["relevance_failed"], 1)


class GatePrecedenceTest(unittest.TestCase):
    """Trust gate first, per-lane relevance floors second (issue #113)."""

    @staticmethod
    def _row(**over) -> dict:
        row = {"id": "row", "content": "synthetic", "type": "fact",
               "signal": "test", "confidence": 0.9,
               "_rel_lex": 0.0, "_rel_cos": None, "_rel_ent": None}
        row.update(over)
        return row

    def test_trusted_but_irrelevant_rows_go_below_relevance(self):
        rows = [self._row(id=f"row-{i}") for i in range(3)]
        selected, status, stats = selective_inject_filter(rows, with_stats=True)
        self.assertEqual(selected, [])
        self.assertEqual(status, "silent")
        self.assertEqual(stats, {"trust_passed": 3, "relevance_failed": 3,
                                 "trust_failed": 0})
        self.assertEqual(classify_silent_reason(rows, lane_stats=stats),
                         "below-relevance")

    def test_untrusted_rows_still_name_below_bar(self):
        # Trust failure wins even though the relevance lanes would also fail:
        # "nothing trusted" is a different failure than "nothing relevant".
        rows = [self._row(id=f"row-{i}", confidence=0.1) for i in range(3)]
        selected, status, stats = selective_inject_filter(rows, with_stats=True)
        self.assertEqual((selected, status), ([], "silent"))
        self.assertEqual(stats, {"trust_passed": 0, "relevance_failed": 0,
                                 "trust_failed": 3})
        self.assertEqual(classify_silent_reason(rows, lane_stats=stats),
                         "below-bar")

    def test_default_call_contract_is_a_two_tuple(self):
        result = selective_inject_filter([self._row()])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_disjunctive_lanes_one_clearing_lane_admits(self):
        # Issue #113 (final-critic round): the per-lane floors are
        # DISJUNCTIVE — a trusted row is admitted when ANY measured lane
        # clears its own floor, even if another measured lane fails. This
        # is the calibrated semantics ("no lane nulls another"); a
        # conjunctive reading would drop entity/cosine-only positives that
        # carry a measured-zero lex lane. Pinned both directions.
        lex_fail_cos_pass = self._row(
            id="lfcp", _rel_lex=0.0, _rel_cos=0.9, _rel_ent=None)
        lex_pass_cos_fail = self._row(
            id="lpcf", _rel_lex=0.9, _rel_cos=0.0, _rel_ent=None)
        both_fail = self._row(
            id="both-fail", _rel_lex=0.0, _rel_cos=0.0, _rel_ent=0.0)
        selected, status, stats = selective_inject_filter(
            [lex_fail_cos_pass, lex_pass_cos_fail, both_fail],
            with_stats=True)
        self.assertEqual(status, "injected")
        self.assertEqual([r["id"] for r in selected], ["lfcp", "lpcf"])
        self.assertEqual(stats, {"trust_passed": 3, "relevance_failed": 1,
                                 "trust_failed": 0})

    def test_all_lanes_absent_row_is_exempt_and_admitted(self):
        # Link-expansion row shape: NO _rel_* keys at all. Absent lanes are
        # exempt from the relevance gate, so a trusted row passes.
        row = {"id": "expansion-row", "content": "linked neighbor",
               "type": "fact", "signal": "test", "confidence": 0.9}
        selected, status, stats = selective_inject_filter([row],
                                                          with_stats=True)
        self.assertEqual(selected, [row])
        self.assertEqual(status, "injected")
        self.assertEqual(stats["relevance_failed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
