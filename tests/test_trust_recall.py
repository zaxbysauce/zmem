"""Issue #115 (Workstream C-4): trust_score applied at recall.

The v11 contradiction ledger wrote memory.trust_score but no recall path
read it — a row contradicted ten times kept injecting on its original
confidence. This suite pins the three pieces of the fix:

1. the shared selective-inject gate hard-drops rows whose trust_score sits
   below INJECT_FLOOR_TRUST (default 0.2) while a missing trust key stays
   exempt (legacy callers keep byte-identical behavior);
2. compute_score multiplies the composite by trust_score — identity at the
   schema default 1.0, so uncontradicted rankings are unchanged;
3. the passive injection lane blocks a trust_score=0 confidence=0.9 row
   end-to-end while explicit `search` still retrieves it, and --explain
   reports the trust contribution (envelope trust_floor + lanes.trust).

The eval re-derivation contract is pinned too: eval_gold._gate_passes with
trust_floor models the real gate, and the 3-key gate-stats shape is
unchanged (floor-dropped rows count in the existing trust_failed bucket).
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib.inject import (_row_trust, _trust_floor,
                             selective_inject_filter)  # noqa: E402
from storelib.recall import compute_score  # noqa: E402

NS = "project:test-trust-115"
NOW_EPOCH = 1750000000.0
FIXED_TS = "2026-08-01T00:00:00Z"


def _row(**over):
    base = {"id": "row", "confidence": 0.9, "signal": "test",
            "type": "lesson", "content": "x", "ingestion_ts": FIXED_TS,
            "retrieval_count": 0}
    base.update(over)
    return base


class RowTrustNormalizationTest(unittest.TestCase):
    def test_missing_key_and_none_are_identity(self):
        self.assertEqual(_row_trust({}), 1.0)
        self.assertEqual(_row_trust({"trust_score": None}), 1.0)

    def test_unparseable_and_nonfinite_fail_closed(self):
        self.assertEqual(_row_trust({"trust_score": "abc"}), 0.0)
        self.assertEqual(_row_trust({"trust_score": float("nan")}), 0.0)
        self.assertEqual(_row_trust({"trust_score": float("inf")}), 0.0)

    def test_out_of_range_clamps(self):
        self.assertEqual(_row_trust({"trust_score": -0.5}), 0.0)
        self.assertEqual(_row_trust({"trust_score": 1.5}), 1.0)

    def test_value_passthrough(self):
        self.assertEqual(_row_trust({"trust_score": 0.2}), 0.2)
        self.assertEqual(_row_trust(_row()), 1.0)


class TrustFloorEnvTest(unittest.TestCase):
    def test_default_floor(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_INJECT_FLOOR_TRUST", None)
            self.assertAlmostEqual(_trust_floor(), 0.2)

    def test_env_override_and_negative_clamp(self):
        with mock.patch.dict(os.environ,
                             {"ZMEM_INJECT_FLOOR_TRUST": "0.95"}):
            self.assertAlmostEqual(_trust_floor(), 0.95)
        with mock.patch.dict(os.environ,
                             {"ZMEM_INJECT_FLOOR_TRUST": "-3"}):
            self.assertEqual(_trust_floor(), 0.0)
        with mock.patch.dict(os.environ,
                             {"ZMEM_INJECT_FLOOR_TRUST": "garbage"}):
            self.assertAlmostEqual(_trust_floor(), 0.2)


class GateTrustFloorTest(unittest.TestCase):
    def test_row_below_floor_dropped_at_boundary_semantics(self):
        at_floor = _row(id="at", trust_score=0.2)
        just_below = _row(id="below", trust_score=0.19)
        zero = _row(id="zero", trust_score=0.0)
        negative = _row(id="neg", trust_score=-0.5)
        nan = _row(id="nan", trust_score=float("nan"))
        selected, status = selective_inject_filter(
            [at_floor, just_below, zero, negative, nan])
        self.assertEqual([r["id"] for r in selected], ["at"])
        self.assertEqual(status, "injected")

    def test_missing_key_exempt(self):
        legacy = _row(id="legacy")  # no trust key at all
        selected, _ = selective_inject_filter([legacy])
        self.assertEqual([r["id"] for r in selected], ["legacy"])

    def test_stats_shape_is_still_exactly_three_keys(self):
        # Issue #115 decision: floor-dropped rows count in the EXISTING
        # trust_failed bucket — no fourth key, or the pinned stats-dict
        # assertions across the suite would change shape.
        rows = [_row(id=f"r{i}", trust_score=0.0) for i in range(3)]
        _sel, _status, stats = selective_inject_filter(rows, with_stats=True)
        self.assertEqual(stats, {"trust_passed": 0, "relevance_failed": 0,
                                 "trust_failed": 3})

    def test_floor_drop_classifies_below_bar(self):
        from storelib.inject import classify_silent_reason
        rows = [_row(id=f"r{i}", trust_score=0.0) for i in range(2)]
        _sel, _status, stats = selective_inject_filter(rows, with_stats=True)
        self.assertEqual(classify_silent_reason(rows, lane_stats=stats),
                         "below-bar")


class ComputeScoreTrustDiscountTest(unittest.TestCase):
    def test_discount_and_boundary(self):
        hi = compute_score(_row(trust_score=1.0), None, NOW_EPOCH,
                           relevance=0.8)
        mid = compute_score(_row(trust_score=0.5), None, NOW_EPOCH,
                            relevance=0.8)
        lo = compute_score(_row(trust_score=0.1), None, NOW_EPOCH,
                           relevance=0.8)
        self.assertTrue(mid < hi)
        self.assertTrue(lo < mid)
        self.assertAlmostEqual(
            compute_score(_row(trust_score=0.5), None, NOW_EPOCH,
                          relevance=0.8) * 2, hi, places=12)

    def test_missing_key_identity(self):
        self.assertEqual(
            compute_score(_row(), None, NOW_EPOCH, relevance=0.8),
            compute_score(_row(trust_score=1.0), None, NOW_EPOCH,
                          relevance=0.8))


class TrustRecallCliBase(unittest.TestCase):
    """Drives the real store.py CLI against a throwaway store."""

    def setUp(self):
        self.scratch = tempfile.mkdtemp(prefix="zmem-test-trust-115-")
        self.data = pathlib.Path(self.scratch, "data")
        self.data.mkdir()
        self.env = dict(os.environ)
        self.env["ZMEM_DATA"] = str(self.data).replace("\\", "/")
        self.env.pop("ZMEM_TEST_NOW", None)
        # Review round (PRR): floor env overrides must not leak from the
        # operator environment into these fixtures — the boundary tests pin
        # exact floor semantics at the 0.2 default.
        for _k in ("ZMEM_INJECT_FLOOR_TRUST", "ZMEM_INJECT_FLOOR_PROMPT",
                   "ZMEM_INJECT_FLOOR_RECENT", "ZMEM_INJECT_FLOOR_GATE_NONE",
                   "ZMEM_INJECT_FLOOR_LEX", "ZMEM_INJECT_FLOOR_COS",
                   "ZMEM_INJECT_FLOOR_ENT"):
            self.env.pop(_k, None)
        # Fixture rows must not semantic-dedup into each other (write-path
        # cosine merge returns the PRE-EXISTING id), so every content is
        # lexically distinct; dedup knob pinned anyway for model-present
        # boxes.
        self.env["ZMEM_DEDUP_THRESHOLD"] = "1.1"

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def cli(self, *args):
        store = str(SCRIPTS / "store.py")
        return subprocess.run([sys.executable, store, *args], env=self.env,
                              capture_output=True, text=True, cwd=str(REPO))

    def add(self, content, confidence=0.9, signal="test"):
        r = self.cli("add", "--namespace", NS, "--type", "lesson",
                     "--content", content, "--confidence", str(confidence),
                     "--signal", signal, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def set_trust(self, mid, value):
        store_path = next(self.data.rglob("*.sqlite"))
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute("UPDATE memory SET trust_score=? WHERE id=?",
                         (value, mid))
            conn.commit()
        finally:
            conn.close()

    def recall_json(self, query, *extra):
        r = self.cli("recall", "--query", query, "--namespace", NS,
                     "--no-bump", "--for-injection", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def search_json(self, text):
        r = self.cli("search", "--text", text, "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


QUERY = "frobnicator quuxlet deployment"
PROBE = ("Frobnicator quuxlet deployment must set FLUX_CAPACITOR=7 before "
         "the first rollout or the harness refuses to start.")
CONTROL = ("Frobnicator deployment uses the widget harness with the quuxlet "
           "adapter enabled by default in staging.")


class TrustBlocksInjectionButNotSearchTest(TrustRecallCliBase):
    def test_trust0_conf09_row_passive_blocked_searchable(self):
        probe = self.add(PROBE)
        control = self.add(CONTROL)
        self.set_trust(probe, 0.0)

        envelope = self.recall_json(QUERY)
        rendered = [r["id"] for r in envelope["results"]]
        self.assertNotIn(probe, rendered,
                         "trust-0 row must not inject on the passive lane")
        self.assertIn(control, rendered)

        found = [r["id"] for r in self.search_json(QUERY)["results"]]
        self.assertIn(probe, found,
                      "the same row must stay retrievable by explicit search")
        self.assertIn(control, found)

    def test_candidate_lanes_carry_trust(self):
        probe = self.add(PROBE)
        self.set_trust(probe, 0.6)
        envelope = self.recall_json(QUERY)
        lanes = envelope["candidate_lanes"][probe]
        self.assertIn("trust", lanes)
        # Review round (PRR): pin the EXACT row trust, not just the range —
        # a hardcoded constant must not satisfy this test.
        self.assertAlmostEqual(lanes["trust"], 0.6, places=6)

    def test_row_trust_overflow_fails_closed(self):
        # Review round (PRR): a huge-int trust_score raises OverflowError
        # in float(); _row_trust must fail closed to 0.0, not crash the gate.
        from storelib.inject import _row_trust
        self.assertEqual(_row_trust({"trust_score": 10 ** 400}), 0.0)

    def test_absence_form_every_no_bump_builder_has_gate_flag(self):
        # Review round (PRR): pin the absence-form invariant from the 4.2
        # sweep — every argv builder that passes --no-bump must also pass
        # --for-injection, or a contradicted row could ride an ungated lane.
        import re
        builders = [
            "hermes-plugin/__init__.py",
            "hermes-plugin/server/mcp_server.py",
            "hermes-plugin/hooks/zmem-hermes-reflect.py",
            "hooks/lib/zmem-recall-body.py",
            "hooks/zmem-session-start.sh",
        ]
        checked = 0
        for rel in builders:
            text = (REPO / rel).read_text(encoding="utf-8")
            for m in re.finditer(r'"--no-bump"', text):
                window = text[max(0, m.start() - 400):m.end() + 400]
                self.assertIn('"--for-injection"', window,
                              f"{rel}: a --no-bump builder lacks the "
                              f"--for-injection gate flag")
                checked += 1
        self.assertGreaterEqual(checked, 7, "expected >=7 builders")


class UncontradictedRankingStableTest(TrustRecallCliBase):
    def test_all_trust1_order_is_insertion_order(self):
        ids = []
        for i in range(4):
            ids.append(self.add(
                f"Frobnicator quuxlet deployment variant zebra{i} keeps its "
                f"own marker token zebra{i} for ordering"))
        results = [r["id"] for r in self.search_json(
            "frobnicator quuxlet deployment zebra", )["results"]][:4]
        # search is explicit, unexpanded, no gate. This is the SET-pin only:
        # recency 1s apart is not guaranteed at CLI speed, so the strict
        # ORDER pin lives in the frozen C2 acceptance check (which replays
        # base and head with identical fixture timing); we additionally pin
        # compute_score's trust identity separately below.
        self.assertEqual(sorted(results), sorted(ids))

    def test_compute_score_identity_keeps_order(self):
        a = _row(id="a", trust_score=1.0)
        b = _row(id="b")  # legacy row without the key
        sa = compute_score(a, None, NOW_EPOCH, relevance=0.8)
        sb = compute_score(b, None, NOW_EPOCH, relevance=0.8)
        self.assertEqual(sa, sb)


class ExplainReportsTrustTest(TrustRecallCliBase):
    def test_explain_envelope_reports_trust(self):
        self.add(PROBE)
        r = self.cli("recall", "--query", QUERY, "--namespace", NS,
                     "--explain", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        explain = json.loads(r.stdout)["explain"]
        self.assertIn("trust", explain["lane_floors"])
        self.assertGreaterEqual(explain["lane_floors"]["trust"], 0.0)
        self.assertGreaterEqual(explain["trust_floor"], 0.0)
        with_trust = [v for v in explain["verdicts"]
                      if "lanes" in (v.get("detail") or {})
                      and "trust" in v["detail"]["lanes"]]
        self.assertTrue(with_trust,
                        "found/below_limit verdicts must carry lanes.trust")
        for v in with_trust:
            t = v["detail"]["lanes"]["trust"]
            self.assertIsInstance(t, float)


class EvalRederivationTrustTest(unittest.TestCase):
    def test_gate_passes_models_trust_floor(self):
        import storelib.eval_gold as eg
        import storelib.inject as inj
        tf = inj._trust_floor()
        blocked = _row(trust_score=0.0)
        admitted = _row(trust_score=0.9)
        self.assertFalse(eg._gate_passes(
            blocked, 0.25, 0.4, frozenset({"test"}), trust_floor=tf))
        self.assertTrue(eg._gate_passes(
            admitted, 0.25, 0.4, frozenset({"test"}), trust_floor=tf))

    def test_lane_map_missing_trust_degrades_to_exempt(self):
        import storelib.eval_gold as eg
        # A legacy envelope without the trust entry: _row_trust treats the
        # candidate as trust=1.0 (exempt), so the re-derivation must NOT
        # drop it for trust reasons.
        r = _row()
        r["trust_score"] = None  # what lanes.get("trust") attaches
        self.assertTrue(eg._gate_passes(
            r, 0.25, 0.4, frozenset({"test"}),
            trust_floor=_trust_floor()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
