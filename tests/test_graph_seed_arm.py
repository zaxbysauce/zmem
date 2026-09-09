"""Issue #136 acceptance suite: the graph-seed candidate arm + per-arm caps.

Pins, at the same levels as the issue's acceptance criteria:

  A. GOLD RESCUE (AC1) — a prompt that shares an ENTITY with a hit but ZERO
     content tokens with the hit's linked neighbor: the neighbor enters the
     candidate pool via the graph arm carrying a MEASURED graph lane (the
     best entry-edge score), and the C-2 relevance floor admits it only when
     that score clears the graph floor (default 0.75 = LINK_THRESHOLD).
  B. NEGATIVE CONTROL (AC2) — the same arm surfaces an unrelated neighbor
     through a curated sub-threshold edge (0.40): the row is judged (not
     exempt) and DROPPED, which also cures the pre-#136 behavior where such
     a neighbor rode post-result link expansion as an all-absent
     (floor-exempt) row and was injected.
  C. SEED POLICY (AC6) — `contradicts` never seeds the graph arm; the
     post-result [CONTESTED LINK] expansion (AC7) still surfaces it.
  D. PER-ARM CAPS (AC3) — named defaults equal the pre-#136 windows
     (byte-identical), ZMEM_ARM_CAP_* truncate BEFORE fusion, and the
     explain + for-injection envelopes report per-arm pre/post/cap counts.
  E. BYTE-IDENTICAL NO-LINKS (AC5) — a store with entities but zero
     memory_link rows produces byte-identical for-injection output with the
     arm on vs off (ZMEM_GRAPH_SEED=0), including the always-present arms
     dict (graph zero-filled).
  F. UNITS — _rrf_fuse 4th list additive / legacy callers unchanged;
     _arm_cap bounds; _lane_floors 4-tuple with env override (hermetic);
     parse_bg_log tolerates the arms= decision-line field both ways;
     the hook renders arms= from the envelope.

Run: python tests/test_graph_seed_arm.py   (no pytest — repo convention)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Env pin — BEFORE any storelib import (house contract: storelib freezes
# STORE_PATH on first import).
# ---------------------------------------------------------------------------
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
HOOK_BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

_TMP = tempfile.mkdtemp(prefix="zmem-graph-seed-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP, "nonexistent-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_EMBED_PROFILE"] = "minilm"   # model-absent shape
os.environ["ZMEM_LINK_THRESHOLD"] = "1.01"    # no auto links in fixtures
os.environ.pop("ZMEM_DATA", None)
for _k in ("ZMEM_GRAPH_SEED", "ZMEM_ARM_CAP_FTS", "ZMEM_ARM_CAP_VEC",
           "ZMEM_ARM_CAP_ENTITY", "ZMEM_ARM_CAP_GRAPH",
           "ZMEM_INJECT_FLOOR_GRAPH"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))

import calendar  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import unittest  # noqa: E402
import uuid  # noqa: E402

import storelib.recall as recall_mod  # noqa: E402
from storelib.inject import _lane_floors, selective_inject_filter  # noqa: E402
from storelib.links import add_link_pair  # noqa: E402
from storelib.schema import (  # noqa: E402
    _normalize_content, _prepare_store, connect, now_iso)
from storelib.write import add_memory  # noqa: E402

PIN_TS = "2026-06-01T00:00:00Z"
FIXED_NOW = float(calendar.timegm((2026, 6, 1, 0, 0, 0)))
os.environ["ZMEM_TEST_NOW"] = PIN_TS

NS = "project:grapharm"
QUERY = "search code with rg"   # normalized terms: search, code, rg

# H matches the query lexically AND carries the `rg` alias; T shares NO
# content token and no entity — only the H—T edge can reach it; N is the
# sub-threshold neighbor; D* are lexical decoys.
ROWS = {
    "H": "ripgrep is a fast code search tool for large repositories",
    "T": "quarterly rotate the deployment credentials in the vault",
    "N": "unrelated legacy note about printer drivers",
    "D1": "search strategies for large monorepos",
    "D2": "code review checklist for pull requests",
}
C_CONTENT = "the opposing view says this workflow is obsolete now"


def _build_store(edges=(), with_entity_on="H", extra_rows=None):
    """Real-write-path fixture; `edges` = (a_key, b_key, relation, score).

    Each build DELETES the (frozen-path) store file first: storelib freezes
    STORE_PATH at import, so per-class isolation comes from resetting the
    file while no connection is open (classes close their conn in
    tearDownClass)."""
    base = os.environ["ZMEM_STORE"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(base + suffix)
        except OSError:
            pass
    conn = connect()
    _prepare_store(conn)
    rows = dict(ROWS)
    if extra_rows:
        rows.update(extra_rows)
    ids = {}
    with contextlib.redirect_stdout(io.StringIO()):
        for key, content in rows.items():
            ids[key] = add_memory(
                conn, namespace=NS, type_="fact", content=content,
                tags="grapharm", signal="test", confidence=0.9,
                source_ref=f"session:grapharm-{key}")
    conn.execute("UPDATE memory SET ingestion_ts=?, valid_from=?",
                 (PIN_TS, PIN_TS))
    conn.commit()
    # Entity `ripgrep` (aliases ripgrep/rg) attached to the hit row.
    if with_entity_on:
        eid = str(uuid.uuid4())
        now = now_iso()
        conn.execute(
            "INSERT INTO entity (id, kind, canonical_name, created_at,"
            " updated_at) VALUES (?,?,?,?,?)",
            (eid, "tool", "ripgrep", now, now))
        for alias in ("ripgrep", "rg"):
            conn.execute(
                "INSERT OR IGNORE INTO entity_alias (entity_id, alias_norm)"
                " VALUES (?,?)", (eid, _normalize_content(alias)))
        conn.execute(
            "INSERT OR IGNORE INTO memory_entity (memory_id, entity_id,"
            " role) VALUES (?,?,'mentions')", (ids[with_entity_on], eid))
    conn.commit()
    for a, b, rel, score in edges:
        add_link_pair(conn, ids[a], ids[b], rel, score=score)
    return conn, ids


def _recall_envelope(conn, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        recall_mod.recall_memory(
            conn, query=QUERY, namespace=NS, as_json=True,
            no_bump=True, no_telemetry=True, for_injection=True, **kw)
    return json.loads(buf.getvalue())


def _explain_envelope(conn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        recall_mod.explain_recall(conn, query=QUERY, namespace=NS,
                                  as_json=True, no_bump=True)
    return json.loads(buf.getvalue())["explain"]


class _FloorEnvHermetic:
    """Save/pop/restore floor + arm env vars around each test (the PR #148
    hermetic mixin pattern — an ambient operator override must not flip
    these assertions)."""

    VARS = ("ZMEM_INJECT_FLOOR_GRAPH", "ZMEM_GRAPH_SEED",
            "ZMEM_ARM_CAP_FTS", "ZMEM_ARM_CAP_VEC",
            "ZMEM_ARM_CAP_ENTITY", "ZMEM_ARM_CAP_GRAPH")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.VARS}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class GoldRescueTest(_FloorEnvHermetic, unittest.TestCase):
    """AC1: entity-linked, content-disjoint target enters via the graph arm
    and passes the C-2 floor through its measured graph lane."""

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.ids = _build_store(
            edges=(("H", "T", "related", 0.85),))
        cls.addClassCleanup(cls.conn.close)

    def test_target_enters_pool_with_measured_graph_lane(self):
        env = _recall_envelope(self.conn)
        lanes = env["candidate_lanes"][self.ids["T"]]
        self.assertIsNotNone(lanes["graph"],
                             "the graph arm must stamp a measured lane on T")
        self.assertAlmostEqual(lanes["graph"], 0.85, places=9)
        self.assertIn(self.ids["T"], env["candidate_ids"])

    def test_target_passes_floor_and_injects(self):
        env = _recall_envelope(self.conn)
        result_ids = [r["id"] for r in env["results"]]
        self.assertIn(self.ids["T"], result_ids,
                      "T (edge 0.85 >= floor 0.75) must inject via graph")
        self.assertIn(self.ids["H"], result_ids)
        self.assertEqual(env["reason"], "injected")

    def test_arms_attribution_carries_the_hit(self):
        env = _recall_envelope(self.conn)
        arms = env["arms"]
        self.assertEqual(set(arms), {"fts", "vec", "entity", "graph"})
        for arm, entry in arms.items():
            self.assertEqual(set(entry), {"pre", "post", "cap"},
                             f"arms[{arm}] shape drifted: {entry}")
            self.assertGreaterEqual(entry["pre"], entry["post"])
        self.assertGreaterEqual(arms["graph"]["post"], 1,
                                "the graph arm carried T; counts must show it")

    def test_gate_rejects_when_graph_floor_raised(self):
        # Re-run the gate over freshly scored rows (graph lane stamped) with
        # the floor cranked past T's 0.85 edge: T must drop.
        os.environ["ZMEM_INJECT_FLOOR_GRAPH"] = "0.9"
        try:
            scored = recall_mod._recall_one_tier(
                self.conn, query=QUERY, ns_list=[NS], limit=10,
                min_confidence=None, hybrid=True, now_epoch=FIXED_NOW,
            )
            rows = [item for _s, item in scored]
            selected, _status = selective_inject_filter(rows)
            self.assertIn(self.ids["T"], [r["id"] for r in rows],
                          "T must be in the scored pool via the graph arm")
            self.assertNotIn(self.ids["T"], [r["id"] for r in selected],
                             "T (0.85 < raised floor 0.9) must be dropped")
        finally:
            os.environ.pop("ZMEM_INJECT_FLOOR_GRAPH", None)


class NegativeNeighborTest(_FloorEnvHermetic, unittest.TestCase):
    """AC2: the sub-threshold neighbor is judged and dropped (this also
    cures the pre-#136 false injection where N rode expansion as an
    all-absent floor-exempt row)."""

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.ids = _build_store(edges=(
            ("H", "T", "related", 0.85),
            ("H", "N", "related", 0.40),
        ))
        cls.addClassCleanup(cls.conn.close)

    def test_low_edge_neighbor_is_judged_and_dropped(self):
        env = _recall_envelope(self.conn)
        lanes = env["candidate_lanes"][self.ids["N"]]
        self.assertAlmostEqual(lanes["graph"], 0.40, places=9)
        result_ids = [r["id"] for r in env["results"]]
        self.assertNotIn(self.ids["N"], result_ids,
                         "N (edge 0.40 < floor 0.75) must NOT inject")
        self.assertIn(self.ids["T"], result_ids)
        self.assertIn(self.ids["H"], result_ids)


class SeedPolicyTest(_FloorEnvHermetic, unittest.TestCase):
    """AC6 + AC7: contradicts never seeds the arm; the post-result
    [CONTESTED LINK] expansion still surfaces the neighbor."""

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.ids = _build_store(
            edges=(("H", "C", "contradicts", 1.0),),
            extra_rows={"C": C_CONTENT})
        cls.addClassCleanup(cls.conn.close)

    def test_contradicts_edge_contributes_no_graph_candidates(self):
        env = _recall_envelope(self.conn)
        arms = env["arms"]
        self.assertEqual(arms["graph"]["pre"], 0,
                         "contradicts must never seed the graph arm")
        self.assertEqual(arms["graph"]["post"], 0)
        lanes = env["candidate_lanes"].get(self.ids["C"], {})
        self.assertIsNone(lanes.get("graph"))

    def test_contested_link_expansion_still_runs(self):
        # The plain (non-for-injection) path keeps the AC7 behavior: C rides
        # post-result expansion with the [CONTESTED LINK] marker key.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall_mod.recall_memory(
                self.conn, query=QUERY, namespace=NS, as_json=True,
                no_bump=True, no_telemetry=True)
        plain = json.loads(buf.getvalue())
        c_rows = [r for r in plain["results"] if r["id"] == self.ids["C"]]
        self.assertTrue(c_rows, "expansion must still surface C")
        self.assertIs(c_rows[0].get("contested_link"), True)


class PerArmCapsTest(_FloorEnvHermetic, unittest.TestCase):
    """AC3: caps truncate BEFORE fusion; envelopes report pre/post/cap."""

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.ids = _build_store(edges=(
            ("H", "T", "related", 0.85),
            ("H", "N", "related", 0.40),
        ))
        cls.addClassCleanup(cls.conn.close)

    def test_explain_envelope_reports_arms(self):
        exp = _explain_envelope(self.conn)
        self.assertEqual(set(exp["arms"]),
                         {"fts", "vec", "entity", "graph"})
        self.assertGreaterEqual(exp["arms"]["graph"]["post"], 2)

    def test_env_cap_truncates_fts_before_fusion(self):
        os.environ["ZMEM_ARM_CAP_FTS"] = "1"
        try:
            exp = _explain_envelope(self.conn)
            self.assertEqual(exp["arms"]["fts"]["cap"], 1)
            self.assertEqual(exp["arms"]["fts"]["post"], 1)
            self.assertGreater(exp["arms"]["fts"]["pre"], 1)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_FTS", None)

    def test_env_cap_truncates_graph_arm(self):
        os.environ["ZMEM_ARM_CAP_GRAPH"] = "1"
        try:
            exp = _explain_envelope(self.conn)
            self.assertEqual(exp["arms"]["graph"]["cap"], 1)
            self.assertEqual(exp["arms"]["graph"]["post"], 1)
            self.assertGreater(exp["arms"]["graph"]["pre"], 1)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_GRAPH", None)

    def test_for_injection_envelope_carries_arms(self):
        env = _recall_envelope(self.conn)
        self.assertIn("arms", env)
        for entry in env["arms"].values():
            self.assertEqual(set(entry), {"pre", "post", "cap"})

    def test_kill_switch_zero_fills_graph_only(self):
        os.environ["ZMEM_GRAPH_SEED"] = "0"
        try:
            env = _recall_envelope(self.conn)
            self.assertEqual(env["arms"]["graph"],
                             {"pre": 0, "post": 0,
                              "cap": env["arms"]["graph"]["cap"]})
            self.assertGreater(env["arms"]["fts"]["pre"], 0)
            # With the arm off, T loses its measured lane in candidate_lanes.
            self.assertIsNone(
                env["candidate_lanes"][self.ids["T"]]["graph"])
            # T may STILL inject — via the pre-existing post-result link
            # expansion (all-absent lane shape, exempt by design). The
            # kill-switch contract is the arm's contribution, not expansion.
            lanes_t = env["candidate_lanes"][self.ids["T"]]
            self.assertIsNone(lanes_t["lex"])
            self.assertIsNone(lanes_t["ent"])
        finally:
            os.environ.pop("ZMEM_GRAPH_SEED", None)


class ByteIdenticalNoLinksTest(_FloorEnvHermetic, unittest.TestCase):
    """AC5: entities without edges — arm on vs off is byte-identical."""

    def test_no_links_store_byte_identical(self):
        conn, _ids = _build_store()  # no edges
        self.addCleanup(conn.close)
        on_raw = None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall_mod.recall_memory(
                conn, query=QUERY, namespace=NS, as_json=True,
                no_bump=True, no_telemetry=True, for_injection=True)
        on_raw = buf.getvalue()
        os.environ["ZMEM_GRAPH_SEED"] = "0"
        try:
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                recall_mod.recall_memory(
                    conn, query=QUERY, namespace=NS, as_json=True,
                    no_bump=True, no_telemetry=True, for_injection=True)
            off_raw = buf2.getvalue()
        finally:
            os.environ.pop("ZMEM_GRAPH_SEED", None)
        self.assertEqual(on_raw, off_raw)


class TwoTierArmsMergeTest(unittest.TestCase):
    """F12 (review round): the ONE shared arms dict accumulates across the
    project + user:global tiers — post is the merged count, never the last
    tier's overwrite."""

    def test_arms_merge_across_tiers(self):
        conn, ids = _build_store()
        self.addCleanup(conn.close)
        with contextlib.redirect_stdout(io.StringIO()):
            gid = add_memory(
                conn, namespace="user:global", type_="fact",
                content="search code with rg ripgrep guide", tags="grapharm",
                signal="test", confidence=0.9, source_ref="session:grapharm-G")
        conn.execute("UPDATE memory SET ingestion_ts=?, valid_from=? "
                     "WHERE id=?", (PIN_TS, PIN_TS, gid))
        conn.commit()
        env = _recall_envelope(conn, include_global=True)
        self.assertGreaterEqual(
            env["arms"]["fts"]["post"], 2,
            "fts post must sum BOTH tiers (project row + global row)")
        self.assertIn("user:global",
                      {r["namespace"] for r in env["results"]})


class RecentSurfaceGraphKeyTest(unittest.TestCase):
    """F12 (review round): the recent surface's candidate_lanes keeps the
    graph key (None everywhere — the recent lane runs no recall tiers), so
    the lane schema stays uniform across surfaces."""

    def test_recent_candidate_lanes_carry_graph_key(self):
        conn, ids = _build_store(edges=(("H", "T", "related", 0.85),))
        self.addCleanup(conn.close)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall_mod.recent_memory(
                conn, namespace=NS, as_json=True, no_bump=True,
                no_telemetry=True, for_injection=True, limit=5)
        env = json.loads(buf.getvalue())
        self.assertTrue(env["candidate_lanes"])
        for lanes in env["candidate_lanes"].values():
            self.assertIn("graph", lanes)
        self.assertIsNone(env["candidate_lanes"][ids["T"]]["graph"],
                          "recent runs no tiers — graph lane stays unmeasured")


class AsOfGraphArmTest(_FloorEnvHermetic, unittest.TestCase):
    """F12 (review round): the graph arm honors the as_of time-travel
    predicate — a row invisible at the as_of instant cannot enter via the
    arm either."""

    def test_graph_arm_respects_as_of(self):
        conn, ids = _build_store(edges=(("H", "T", "related", 0.85),))
        self.addCleanup(conn.close)
        # Rows are backdated to PIN_TS (2026-06-01) by _build_store.
        after = _recall_envelope(conn, as_of="2026-06-02T00:00:00Z")
        self.assertGreaterEqual(after["arms"]["graph"]["post"], 1)
        self.assertIn(ids["T"], after["candidate_ids"])
        before = _recall_envelope(conn, as_of="2026-05-01T00:00:00Z")
        self.assertEqual(before["arms"]["graph"]["post"], 0,
                         "nothing is visible before the rows exist — the "
                         "graph arm must not conjure them")
        self.assertNotIn(ids["T"], before["candidate_ids"])


class RrfFuseUnitTest(unittest.TestCase):
    """The 4th list is additive; legacy 2/3-arg callers are unchanged."""

    def test_four_lists_accumulate_additively(self):
        fused = recall_mod._rrf_fuse(["a", "b"], ["b"], ["c"],
                                     graph_ids=["d"], k=60)
        self.assertEqual(fused, ["b", "a", "c", "d"])

    def test_legacy_call_shapes_unchanged(self):
        self.assertEqual(recall_mod._rrf_fuse(["a"], []), ["a"])
        self.assertEqual(recall_mod._rrf_fuse(["a"], [], ["b"]), ["a", "b"])
        self.assertEqual(recall_mod._rrf_fuse(["a"], [], None), ["a"])


class ArmCapUnitTest(unittest.TestCase):
    def test_bounds(self):
        os.environ["ZMEM_ARM_CAP_FTS"] = "7"
        try:
            self.assertEqual(recall_mod._arm_cap("ZMEM_ARM_CAP_FTS", 20), 7)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_FTS", None)
        os.environ["ZMEM_ARM_CAP_FTS"] = "junk"
        try:
            self.assertEqual(recall_mod._arm_cap("ZMEM_ARM_CAP_FTS", 20), 20)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_FTS", None)
        os.environ["ZMEM_ARM_CAP_FTS"] = "-5"
        try:
            self.assertEqual(recall_mod._arm_cap("ZMEM_ARM_CAP_FTS", 20), 0)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_FTS", None)
        os.environ["ZMEM_ARM_CAP_FTS"] = "100000"
        try:
            self.assertEqual(recall_mod._arm_cap("ZMEM_ARM_CAP_FTS", 20),
                             1000)
        finally:
            os.environ.pop("ZMEM_ARM_CAP_FTS", None)


class LaneFloorsUnitTest(_FloorEnvHermetic, unittest.TestCase):
    def test_default_four_tuple_includes_graph(self):
        floors = _lane_floors()
        self.assertEqual(len(floors), 4)
        self.assertAlmostEqual(floors[3], 0.75, places=9)

    def test_graph_floor_env_override(self):
        os.environ["ZMEM_INJECT_FLOOR_GRAPH"] = "0.5"
        try:
            self.assertAlmostEqual(_lane_floors()[3], 0.5, places=9)
        finally:
            os.environ.pop("ZMEM_INJECT_FLOOR_GRAPH", None)
        os.environ["ZMEM_INJECT_FLOOR_GRAPH"] = "-1"
        try:
            self.assertEqual(_lane_floors()[3], 0.0)
        finally:
            os.environ.pop("ZMEM_INJECT_FLOOR_GRAPH", None)


class LegacyLaneFloorsCompatTest(_FloorEnvHermetic, unittest.TestCase):
    """F8 (review round): a legacy 3-value lane_floors override must not
    IndexError the gate — the graph floor simply does not apply, exactly
    as before the graph lane existed."""

    ROW = {"id": "g", "signal": "test", "confidence": 0.9,
           "_rel_lex": None, "_rel_cos": None, "_rel_ent": None,
           "_rel_graph": 0.99}

    def test_three_value_override_tolerated(self):
        kept, _status = selective_inject_filter(
            [dict(self.ROW)], lane_floors=(0.30, 0.50, 0.5))
        self.assertEqual([r["id"] for r in kept], ["g"],
                         "with a 3-tuple the graph lane carries no floor, "
                         "so the row keeps its absent-lane exemption")

    def test_four_value_override_applies_graph_floor(self):
        kept, _status = selective_inject_filter(
            [dict(self.ROW)], lane_floors=(0.30, 0.50, 0.5, 0.75))
        self.assertEqual([r["id"] for r in kept], ["g"])
        strict, _status = selective_inject_filter(
            [dict(self.ROW, _rel_graph=0.5)],
            lane_floors=(0.30, 0.50, 0.5, 0.75))
        self.assertEqual(strict, [],
                         "0.5 < the supplied 0.75 graph floor: "
                         "measured-but-failing must drop")


class BgLogArmsFieldTest(unittest.TestCase):
    """[B1] parse_bg_log must parse the arms= decision-line field both ways."""

    BASE = ("[1700000000] zmem-hook status=injected reason=injected "
            "ids=['a'] all=['a'] tokens=10/1500 sid=sess-a "
            "moment=user_prompt")

    def _parse(self, text, tmp):
        log = Path(tmp) / "zmem-decisions.log"
        log.write_text(text, encoding="utf-8")
        from storelib.miss_rate import parse_bg_log
        return parse_bg_log(str(log))

    def test_line_with_arms_parses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            line = self.BASE + " arms=fts:3/15,vec:0/25,ent:2/50,graph:1/5\n"
            out = self._parse(line, tmp)
            self.assertEqual(len(out), 1,
                             "arms= line must still match _BG_LINE_RE")
            self.assertEqual(out[0]["arms"],
                             "fts:3/15,vec:0/25,ent:2/50,graph:1/5")
            self.assertEqual(out[0]["reason"], "injected")

    def test_line_without_arms_unchanged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._parse(self.BASE + "\n", tmp)
            self.assertEqual(len(out), 1)
            self.assertIsNone(out[0]["arms"])

    def test_hook_renders_arms_from_envelope(self):
        # F11 (review round): functional, not source-grep — load the real
        # hook body module and prove _log_inject_decision renders the
        # arms= field from the envelope's arms dict, INCLUDING the ent
        # wire label mapped from the envelope's "entity" key (the #136
        # round's F1 fix: the serializer used to look up arms["ent"],
        # which no caller supplies, silently dropping entity attribution).
        import importlib.util
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ZMEM_DATA"] = tmp
            try:
                spec = importlib.util.spec_from_file_location(
                    "zmem_recall_body_under_test", str(HOOK_BODY))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Resolve the log home through the module's own resolver
                # (this checkout's chain prefers ZMEM_STORE's parent, so the
                # line may land in the module fixture dir, not tmp) — the
                # pin targets the arms RENDERING, not data-dir precedence
                # (that is PR #101's pinned contract).
                data_dir = mod._data_dir()
                mod._log_inject_decision(
                    [{"id": "a"}], [{"id": "a"}], "injected", "injected",
                    tokens_used=10, tokens_budget=1500,
                    session_id="sess-armtest", store_py="",
                    arms={"fts": {"pre": 5, "post": 3, "cap": 15},
                          "vec": {"pre": 2, "post": 0, "cap": 25},
                          "entity": {"pre": 4, "post": 2, "cap": 50},
                          "graph": {"pre": 3, "post": 1, "cap": 5}})
            finally:
                os.environ.pop("ZMEM_DATA", None)
            log = Path(data_dir) / "zmem-decisions.log"
            self.assertTrue(log.is_file(), "decision line must be written")
            lines = log.read_text(encoding="utf-8").splitlines()
        arms_lines = [ln for ln in lines if " arms=" in ln]
        self.assertEqual(len(arms_lines), 1, lines)
        self.assertIn("fts:3/15", arms_lines[0])
        self.assertIn("vec:0/25", arms_lines[0])
        self.assertIn("ent:2/50", arms_lines[0],
                      "envelope key 'entity' must render as wire label ent")
        self.assertIn("graph:1/5", arms_lines[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
