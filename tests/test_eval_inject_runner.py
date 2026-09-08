"""Issue #111: tests for the injection-direction precision gold.

Covers scripts/eval_inject_runner.py + storelib.eval_gold's
evaluate_injection_items: the real-lane report schema, the negative-control
known-failure reproduction, gold validation, the NO-SILENT-BYPASS contract
(stubbing the gate or the token budget must make the harness refuse), the
baseline-compare zero/drift/invalid paths, the ratchet flags, run
determinism, the precompact (recent) arm, and the empty-render fence.

Run: python tests/test_eval_inject_runner.py   (plain unittest, CI convention)
"""
import contextlib
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Isolation FIRST (storelib freezes STORE_PATH at first import — the
# repo-test hazard): a scratch store under the repo's gitignored .eval-tmp.
os.environ["ZMEM_STORE"] = str(REPO / ".eval-tmp" / "test-inject-store.sqlite")
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
from eval_store import BASE_ENV, EVAL_PIN_TS  # noqa: E402

for _k, _v in BASE_ENV.items():
    os.environ[_k] = _v
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS
for _k in ("ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_INJECT_FLOOR_PROMPT",
           "ZMEM_INJECT_FLOOR_GATE_NONE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(REPO / "skills" / "memory" / "scripts"))

RUNNER = REPO / "scripts" / "eval_inject_runner.py"
GOLD = REPO / "eval" / "injection_gold.jsonl"
BASELINE = REPO / "eval" / "baseline-injection.json"
SCRATCH = REPO / ".eval-tmp"


def setUpModule() -> None:
    from eval_store import build_eval_store
    store = Path(os.environ["ZMEM_STORE"])
    if not store.exists():
        build_eval_store(str(store))


def run_runner(*extra: str):
    """Run the harness as a subprocess (the way CI does) and return the
    CompletedProcess with the report parsed when present."""
    import subprocess
    out = SCRATCH / "test-inject-report.json"
    if out.exists():
        out.unlink()
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--store", os.environ["ZMEM_STORE"],
         "--gold", str(GOLD), "--json-out", str(out), *extra],
        capture_output=True, text=True, timeout=1200)
    report = None
    if out.exists():
        report = json.loads(out.read_text(encoding="utf-8"))
    return proc, report


class EndToEndReportTest(unittest.TestCase):
    """Tests (1)+(2): the real-lane report schema and the negative-control
    known-failure reproduction."""

    @classmethod
    def setUpClass(cls):
        from storelib.eval_gold import load_gold
        cls.labels = {g.id: g.must_include_ids
                      for g in load_gold(str(GOLD))}
        cls.proc, cls.report = run_runner()

    def test_run_succeeds_with_lane_and_provenance(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertEqual(self.report["lane"], "for-injection")
        self.assertEqual(self.report["profile"], "fake (model-absent)")
        self.assertEqual(self.report["clock"], EVAL_PIN_TS)
        self.assertEqual(self.report["metrics"]["items"], 110)
        self.assertEqual(self.report["metrics"]["positive_items"], 100)
        self.assertEqual(self.report["metrics"]["negative_items"], 10)

    def test_per_item_rendered_facts(self):
        for it in self.report["per_item"]:
            self.assertIn(it["moment"],
                          ("user-prompt", "pretool", "subagent", "precompact"))
            self.assertTrue(it["reason"], it["id"])
            self.assertIsInstance(it["candidate_ids"], list, it["id"])
            self.assertEqual(it["tokens_budget"], 1500, it["id"])
            used = it["tokens_used"]
            self.assertIsInstance(used, int, it["id"])
            self.assertGreaterEqual(used, 0, it["id"])
            # The budget/protected-overflow invariant is enforced by the
            # harness's own _verify_real_lane (which sees full candidate
            # rows); here we pin the auditable rank field instead.
            rank = it["first_hit_rank"]
            self.assertIsInstance(rank, int, it["id"])
            # Exact cross-check: the rank is the 1-based position of the
            # FIRST labeled id in rendered order, 0 when none rendered.
            present = [it["rendered_ids"].index(x) + 1
                       for x in self.labels.get(it["id"], ())
                       if x in it["rendered_ids"]]
            self.assertEqual(rank, min(present) if present else 0, it["id"])
            self.assertTrue(it["fence_ok"], it["id"])

    def test_per_moment_partition_is_exhaustive(self):
        per_moment = self.report["per_moment"]
        total = 0
        for moment in ("user-prompt", "pretool", "subagent", "precompact"):
            block = per_moment[moment]
            self.assertGreaterEqual(block["items"], 1, moment)
            total += block["items"]
        self.assertEqual(total, self.report["metrics"]["items"])

    def test_negative_controls_now_silent_below_relevance(self):
        # Issue #113: the negative controls used to reproduce the KNOWN
        # FAILURE the fix targets — neg-pt-status ("git status") injected a
        # single-generic-token match. The post-fix pinned state: every
        # negative control is silent (false_injection_rate 0.0) and the
        # non-empty-pool one is silent BECAUSE OF the relevance floor, with
        # reason exactly "below-relevance" (candidates existed and passed
        # the trust gate; every query-matched candidate failed a lane
        # floor) — not below-bar and not empty-pool.
        metrics = self.report["metrics"]
        self.assertEqual(metrics["false_injection_rate"], 0.0)
        injecting = [it for it in self.report["per_item"]
                     if it["expect"] == "silent" and it["rendered_ids"]]
        self.assertEqual(
            injecting, [],
            "no negative control may render rows after the #113 relevance "
            "floor; the known failure is FIXED, not reproduced")
        negatives = {it["id"]: it for it in self.report["per_item"]
                     if it["expect"] == "silent"}
        self.assertIn("neg-pt-status", negatives)
        status = negatives["neg-pt-status"]
        self.assertEqual(status["reason"], "below-relevance")
        self.assertTrue(status["candidate_ids"],
                        "neg-pt-status must have a non-empty candidate pool "
                        "(the below-relevance reason is only reachable when "
                        "candidates existed)")
        # The gold's negative queries are pinned so this scenario cannot
        # silently drift into testing different prompts.
        queries = {it["query"] for it in negatives.values()}
        self.assertIn("write a haiku about autumn leaves", queries)
        self.assertIn("update the README wording", queries)
        self.assertIn("git status", queries)


class GoldValidationTest(unittest.TestCase):
    """Test (3): the loader fail-closes on malformed injection items."""

    def _write(self, obj) -> str:
        p = SCRATCH / "test-bad-gold.jsonl"
        p.write_text(json.dumps(obj) + "\n", encoding="utf-8", newline="\n")
        return str(p)

    def test_bad_moment_raises(self):
        from storelib.eval_gold import GoldError, load_gold
        bad = {"id": "x1", "bucket": "fts", "query": "q", "moment": "nope",
               "must_include_ids": ["e0000000-0000-4000-8000-000000000046"]}
        with self.assertRaises(GoldError):
            load_gold(self._write(bad))

    def test_silent_with_labels_raises(self):
        from storelib.eval_gold import GoldError, load_gold
        bad = {"id": "x2", "bucket": "negative-control", "query": "q",
               "moment": "user-prompt", "expect": "silent",
               "must_include_ids": ["e0000000-0000-4000-8000-000000000046"]}
        with self.assertRaises(GoldError):
            load_gold(self._write(bad))

    def test_positive_without_labels_raises(self):
        from storelib.eval_gold import GoldError, load_gold
        bad = {"id": "x3", "bucket": "fts", "query": "kubernetes",
               "moment": "user-prompt"}
        with self.assertRaises(GoldError):
            load_gold(self._write(bad))

    def test_precompact_with_query_raises(self):
        from storelib.eval_gold import GoldError, load_gold
        bad = {"id": "x4", "bucket": "fts", "query": "kubernetes",
               "moment": "precompact",
               "must_include_ids": ["e0000000-0000-4000-8000-000000000046"]}
        with self.assertRaises(GoldError):
            load_gold(self._write(bad))


class NoSilentBypassTest(unittest.TestCase):
    """Test (4): stubbing the gate or the token budget (the exact module
    attributes the lane calls at recall.py's injection branch, bound at
    recall.py:25 and resolved at call time) must make the harness REFUSE,
    never silently score. The invariants are re-derived from pure
    primitives, so the stub cannot silence them."""

    def _items(self):
        from storelib.eval_gold import load_gold
        return load_gold(str(GOLD))[:2]  # one as-of + one injection item

    def test_stubbed_gate_refuses(self):
        import storelib.recall as recall_mod
        from storelib.eval_gold import BypassError, evaluate_injection_items
        from storelib.schema import connect
        real = recall_mod.selective_inject_filter
        caught = None
        try:
            # Issue #113: the injection lane calls the gate with
            # with_stats=True and unpacks (selected, status, stats) — the
            # stub must return that 3-tuple shape or it crashes as an
            # unpack error instead of exercising the bypass detection. The
            # stubbed gate passes EVERY row through (fabricated stats claim
            # all rows were trust-passing and none relevance-failed).
            recall_mod.selective_inject_filter = (
                lambda rows, *a, **k: (
                    rows, "injected",
                    {"trust_passed": len(rows), "relevance_failed": 0,
                     "trust_failed": 0}))
            with unittest.mock.patch.dict(os.environ,
                                          {"ZMEM_INJECT_FLOOR_PROMPT": "0.95"}):
                with self.assertRaises(BypassError) as ctx:
                    evaluate_injection_items(connect(), self._items())
            caught = ctx.exception
        finally:
            recall_mod.selective_inject_filter = real
        self.assertIn("gate", str(caught).lower())

    def test_stubbed_budget_refuses(self):
        import storelib.recall as recall_mod
        from storelib.eval_gold import BypassError, evaluate_injection_items
        from storelib.schema import connect
        real = recall_mod.apply_token_budget

        # Issue #116: the lane now calls the budget with with_stats=True,
        # so the identity stub returns the 4-tuple shape (all-zero
        # omissions — an admitted-everything stub, the exact bypass this
        # test refuses). Costs use fence_row_cost, the estimator the real
        # admission charges.
        def _identity(rows, budget=None, *, with_stats=False):
            from storelib.inject import fence_row_cost
            used = sum(fence_row_cost(r) for r in rows)
            if with_stats:
                return rows, used, 0, {"admission_used": used,
                                       "dropped": 0, "truncated": 0,
                                       "dropped_protected": 0,
                                       "budget": budget}
            return rows, used, 0

        caught = None
        try:
            recall_mod.apply_token_budget = _identity
            with unittest.mock.patch.dict(os.environ,
                                          {"ZMEM_INJECT_TOKEN_BUDGET": "1"}):
                with self.assertRaises(BypassError) as ctx:
                    evaluate_injection_items(connect(), self._items())
            caught = ctx.exception
        finally:
            recall_mod.apply_token_budget = real
        self.assertIn("budget", str(caught).lower())

    def test_stubbed_gate_dropping_rows_refuses_via_reconstruction(self):
        # A gate stub that DROPS rows (not pass-through, not inflate) is
        # invisible to the output-only invariants; the independent
        # reconstruction must catch it at DEFAULT thresholds — the
        # mismatch is structural (empty rendered set vs non-empty expected
        # selection), so no boundary-cranking is needed. Regression pin:
        # this path previously crashed with a NameError instead of
        # raising BypassError. Issue #113: the lane now calls the gate
        # with with_stats=True, so the stub returns the 3-tuple shape
        # (fabricating an all-relevance-failed stats dict so the stubbed
        # scenario stays internally coherent); the reconstruction — which
        # models the per-lane floors from the envelope's candidate_lanes —
        # still produces a non-empty expected selection and refuses.
        import storelib.recall as recall_mod
        from storelib.eval_gold import BypassError, evaluate_injection_items
        from storelib.schema import connect
        real = recall_mod.selective_inject_filter
        caught = None
        try:
            recall_mod.selective_inject_filter = (
                lambda rows, *a, **k: (
                    [], "silent",
                    {"trust_passed": len(rows), "relevance_failed": len(rows),
                     "trust_failed": 0}))
            with self.assertRaises(BypassError) as ctx:
                evaluate_injection_items(connect(), self._items())
            caught = ctx.exception
        finally:
            recall_mod.selective_inject_filter = real
        self.assertIn("reconstructed", str(caught))


class BaselineAndRatchetTest(unittest.TestCase):
    """Tests (5)+(6): --compare-baseline zero/drift/invalid exits and the
    ratchet flags."""

    def test_baseline_equal_exits_zero(self):
        proc, _ = run_runner("--compare-baseline", str(BASELINE))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_baseline_drift_exits_one(self):
        bad = SCRATCH / "test-drift-baseline.json"
        doc = json.loads(BASELINE.read_text(encoding="utf-8"))
        doc["metrics"]["hit_at_k"] = 9999.0
        bad.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
        proc, _ = run_runner("--compare-baseline", str(bad))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("DELTA", proc.stderr)

    def test_baseline_unreadable_exits_two(self):
        bad = SCRATCH / "test-invalid-baseline.json"
        bad.write_text("{not json", encoding="utf-8", newline="\n")
        proc, _ = run_runner("--compare-baseline", str(bad))
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_precision_ratchet_flips_exit(self):
        proc, _ = run_runner("--fail-under-precision", "2.0")
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_false_injection_ratchet_flips_exit(self):
        proc, _ = run_runner("--fail-under-false-injection", "-1.0")
        self.assertEqual(proc.returncode, 1, proc.stderr)


class DeterminismTest(unittest.TestCase):
    """Test (7): two runs on the same store are byte-equal (clock pinned)."""

    def test_two_runs_byte_equal(self):
        out_a = SCRATCH / "test-det-a.json"
        out_b = SCRATCH / "test-det-b.json"
        for out in (out_a, out_b):
            proc, _ = run_runner("--json-out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out_a.read_text(encoding="utf-8"),
                         out_b.read_text(encoding="utf-8"))


class PrecompactArmTest(unittest.TestCase):
    """Test (8): the precompact arm routes through the recent lane with the
    hook's flags (a recall_memory swap would not satisfy the labels)."""

    def test_precompact_only_gold(self):
        from storelib.eval_gold import load_gold
        items = [it for it in load_gold(str(GOLD))
                 if it.moment == "precompact"]
        self.assertGreaterEqual(len(items), 1)
        positives = [it for it in items if it.expect == "inject"]
        self.assertTrue(positives, "precompact suite keeps a positive item")
        from storelib.eval_gold import evaluate_injection_items
        from storelib.schema import connect
        per_item, _ = evaluate_injection_items(connect(), items)
        by_expect = {it["id"]: it for it in per_item}
        for it in per_item:
            self.assertEqual(it["tokens_budget"], 1500, it["id"])
        # The positive rides the recent lane with the hook's flags (a
        # recall_memory swap would not satisfy the labels); the negative
        # (a namespace whose rows are all invalidated) must stay silent.
        for item in positives:
            row = by_expect[item.id]
            self.assertTrue(row["hit"],
                            "precompact labels must match the recent lane")
            self.assertEqual(row["reason"], "injected")
        silent = [it for it in items if it.expect == "silent"]
        for item in silent:
            row = by_expect[item.id]
            self.assertEqual(row["reason"], "empty-pool")
            self.assertEqual(row["rendered_ids"], [])


class EmptyRenderFenceTest(unittest.TestCase):
    """Test (9): an empty render still emits both fence markers so the
    host's <<<END>>> extraction stays valid."""

    def test_empty_render_keeps_markers(self):
        from storelib.recall import (ZMEM_FENCE_CLOSE, ZMEM_FENCE_OPEN,
                                     _format_fenced_recall)
        fence = _format_fenced_recall([], header="Relevant memories.")
        self.assertTrue(fence.startswith(ZMEM_FENCE_OPEN))
        self.assertIn(ZMEM_FENCE_CLOSE, fence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
