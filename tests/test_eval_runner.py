"""Tests for the offline eval runner and public-corpus adapters
(issue #64, 9.1/9.2/9.3; issue #82 eval honesty).

Covers:
  - End-to-end: the runner auto-builds its deterministic fixture corpus at the
    given --store path, runs the committed eval/gold.jsonl, and exits 0 with a
    full JSON report on stdout. The original six issue-#64 buckets (30 items,
    5 each) stay deterministic 1.0; the issue-#82 honesty buckets (retraction,
    polarity, change-intent, >=3 items each) are pinned with their actual
    deterministic hit rates.
  - Byte-stability: two consecutive runs produce byte-identical JSON.
  - Passivity: a run leaves the store byte-identical — including the
    issue-#82 EXPLICIT items (no_bump=False + no_telemetry=True is a
    zero-write combination; pinned at unit level below).
  - Operational refusals: missing --store -> exit 2; invalid gold -> exit 2
    naming the item; --fail-under breach -> exit 1.
  - Gold-set shape: every committed item passes load_gold validation; the
    original six buckets keep >=5 items, the #82 buckets >=3; ids 1-50 are
    the frozen contract and no original item references a 51+ id.
  - Adapters: toy fixtures convert and validate through load_gold.

Run: python tests/test_eval_runner.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "scripts" / "eval_runner.py"
ADAPTERS = REPO_ROOT / "scripts" / "eval_adapters.py"
GOLD = REPO_ROOT / "eval" / "gold.jsonl"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Module-level ZMEM_STORE pin: this file's in-process tests import storelib
# mid-run, and storelib freezes STORE_PATH at FIRST import. Without this pin
# that first import resolves (and could, on schema skew, auto-migrate) the
# operator's real home store. Subprocess tests are env-pinned per run and the
# runner overrides ZMEM_STORE itself; the frozen path here only serves the
# in-process TestExplicitPassivity class, which builds its fixture AT it.
# ---------------------------------------------------------------------------
INPROC_STORE = os.path.join(
    tempfile.mkdtemp(prefix="zmem-eval-inproc-"), "inproc.sqlite")
os.environ["ZMEM_STORE"] = INPROC_STORE
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ["ZMEM_MODELS_DIR"] = "/nonexistent-zmem-models-dir"
os.environ["ZMEM_EMBED_PROFILE"] = "fake"


class EvalRunnerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-eval-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.store = os.path.join(cls.tmp, "store.sqlite")
        # base_env deliberately points ZMEM_STORE at an "unused" path: the
        # runner itself overrides os.environ with the explicit --store value,
        # so the ambient value must never be the store under test.
        cls.base_env = {**os.environ,
                        "ZMEM_STORE": os.path.join(cls.tmp, "unused.sqlite")}

    def _run(self, *args):
        return subprocess.run([PYTHON, str(RUNNER), *args],
                              env=self.base_env, capture_output=True,
                              text=True, timeout=600)


class TestRunnerEndToEnd(EvalRunnerTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # One build+run shared by the whole class (the corpus build is the
        # slow part); the byte-stability test runs the runner a second time.
        r = subprocess.run(
            [PYTHON, str(RUNNER), "--store", cls.store,
             "--json-out", os.path.join(cls.tmp, "results.json")],
            env=cls.base_env, capture_output=True, text=True, timeout=600)
        cls.run_result = r
        cls.report = json.loads(r.stdout)

    def test_exit_0_with_full_report(self):
        self.assertEqual(self.run_result.returncode, 0,
                         self.run_result.stderr[-2000:])
        self.assertEqual(self.report["runner"], "scripts/eval_runner.py")
        self.assertIn("metrics", self.report)
        for key in ("hit_at_k", "mrr", "as_of_accuracy", "injection_omit_rate"):
            self.assertIn(key, self.report["metrics"])

    def test_gold_set_covers_original_buckets_with_metrics_1(self):
        # The original issue-#64 gold (ids 1-50, five items per bucket) is a
        # frozen contract: every bucket's assertion must keep holding at 1.0.
        items = [it for it in self.report["per_item"]
                 if it["bucket"] in {"as-of", "injection", "namespace",
                                     "contested", "entity-alias", "fts"}]
        self.assertEqual(len(items), 30)
        for bucket in {"as-of", "injection", "namespace", "contested",
                       "entity-alias", "fts"}:
            agg = self.report["per_bucket"][bucket]
            self.assertEqual(agg["items"], 5, bucket)
            self.assertEqual(agg["hits"], 5, bucket)
            self.assertEqual(agg["excluded_surfaced"], 0, bucket)

    def test_gold_set_has_issue_82_buckets(self):
        # Issue #82 honesty buckets: >=3 items each, all deterministic.
        # Issue #88 adds the decision-point bucket (6 items, ops-composed).
        buckets = set(self.report["per_bucket"].keys())
        self.assertEqual(
            buckets,
            {"as-of", "injection", "namespace", "contested", "entity-alias",
             "fts", "retraction", "polarity", "change-intent",
             "decision-point"})
        self.assertEqual(len(self.report["per_item"]), 48)
        for bucket in ("retraction", "polarity", "change-intent"):
            agg = self.report["per_bucket"][bucket]
            self.assertGreaterEqual(agg["items"], 3, bucket)
            self.assertEqual(agg["hits"], agg["items"], bucket)
            self.assertEqual(agg["excluded_surfaced"], 0, bucket)
        # Decision-point items: all hit WITH ops composed (ranks pinned by
        # the deterministic fixture; the prose-only MISS direction is pinned
        # in tests/test_ops_tokens.py against the same fixture).
        dec = self.report["per_bucket"]["decision-point"]
        self.assertEqual(dec["items"], 6)
        self.assertEqual(dec["hits"], 6)
        # Review round 1: pin each item's first_hit_rank explicitly so a
        # fixture-content drift cannot silently move a rank while hit@k
        # still passes (the MRR pin below would then fail opaquely).
        expected_ranks = {
            "decision-stash-65": 1,
            "decision-reset-66": 2,
            "decision-ratchet-67": 1,
            "decision-queue-68": 1,
            "decision-push-69": 1,
            "decision-worktree-70": 1,
        }
        for it in self.report["per_item"]:
            if it["id"] in expected_ranks:
                self.assertEqual(it["first_hit_rank"],
                                 expected_ranks[it["id"]], it["id"])

    def test_explicit_items_flow_through_and_stay_flagged(self):
        explicit = {it["id"]: it for it in self.report["per_item"]
                    if it["explicit"]}
        self.assertEqual(
            set(explicit.keys()),
            {"ci-explicit-1", "ci-explicit-2", "ci-explicit-3"})
        self.assertTrue(all(it["hit"] for it in explicit.values()),
                        "explicit change-intent items must surface the live "
                        "head AND the [PREVIOUSLY] predecessor (unfold ran)")
        # The passive twins must have kept the predecessor OUT (hooks never
        # unfold) while still surfacing the live head.
        passive = {it["id"]: it for it in self.report["per_item"]
                   if it["id"].startswith("ci-passive")}
        self.assertEqual(len(passive), 3)
        for it in passive.values():
            self.assertTrue(it["hit"])
            self.assertEqual(it["excluded_ids_surfaced"], [],
                             "a passive twin must never see the predecessor")

    def test_metrics_1_with_pinned_mrr(self):
        self.assertAlmostEqual(self.report["metrics"]["hit_at_k"], 1.0)
        # MRR counts only items with an include-assertion; the two
        # exclude-only retraction items legitimately contribute 0 (40 of 42
        # pre-#88). Issue #88 adds six decision-point items with deterministic
        # ranks 1,2,1,1,1,1 → rr sum 5.5 → (40 + 5.5) / 48.
        self.assertAlmostEqual(self.report["metrics"]["mrr"], 45.5 / 48)
        self.assertAlmostEqual(self.report["metrics"]["as_of_accuracy"], 1.0)
        self.assertAlmostEqual(self.report["metrics"]["injection_omit_rate"], 1.0)
        self.assertEqual(self.report["metrics"]["as_of_items"], 6)
        self.assertEqual(self.report["metrics"]["injection_items"], 5)

    def test_runs_are_byte_stable(self):
        r = subprocess.run(
            [PYTHON, str(RUNNER), "--store", self.store],
            env=self.base_env, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The report embeds the store path (stable here) and no wall-clock
        # data: two runs of the same gold vs the same store must be identical.
        self.assertEqual(r.stdout, self.run_result.stdout)

    def test_store_is_untouched_by_evaluation(self):
        before = Path(self.store).read_bytes()
        r = subprocess.run(
            [PYTHON, str(RUNNER), "--store", self.store],
            env=self.base_env, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(self.store).read_bytes(), before,
                         "evaluation is passive (no_bump) — the store must "
                         "stay byte-identical")

    def test_json_out_artifact_written(self):
        artifact = Path(self.tmp) / "results.json"
        self.assertTrue(artifact.is_file())
        json.loads(artifact.read_text(encoding="utf-8"))  # parseable


class TestRunnerRefusals(EvalRunnerTestBase):
    def test_missing_store_flag_is_a_usage_error(self):
        r = subprocess.run([PYTHON, str(RUNNER)], env=self.base_env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--store", r.stderr)

    def test_invalid_gold_names_the_item(self):
        gold = Path(self.tmp) / "bad_gold.jsonl"
        gold.write_text(json.dumps({
            "id": "bogus-1", "bucket": "not-a-bucket", "query": "q",
            "must_include_ids": ["x"]}) + "\n", encoding="utf-8")
        r = self._run("--store", self.store, "--gold", str(gold))
        self.assertEqual(r.returncode, 2)
        self.assertIn("bogus-1", r.stderr)

    def test_assertion_less_gold_item_refused(self):
        gold = Path(self.tmp) / "assertionless.jsonl"
        gold.write_text(json.dumps({
            "id": "empty-1", "bucket": "fts", "query": "q"}) + "\n",
            encoding="utf-8")
        r = self._run("--store", self.store, "--gold", str(gold))
        self.assertEqual(r.returncode, 2)
        self.assertIn("empty-1", r.stderr)

    def test_overlapping_gold_assertions_refused(self):
        gold = Path(self.tmp) / "overlap.jsonl"
        gold.write_text(json.dumps({
            "id": "overlap-1", "bucket": "fts", "query": "q",
            "must_include_ids": ["a"], "must_exclude_ids": ["a"]}) + "\n",
            encoding="utf-8")
        r = self._run("--store", self.store, "--gold", str(gold))
        self.assertEqual(r.returncode, 2)
        self.assertIn("overlap-1", r.stderr)

    def test_unbuildable_store_path_exit_2(self):
        blocked = os.path.join(self.tmp, "file-blocks-dir")
        Path(blocked).write_text("not a dir", encoding="utf-8")
        r = self._run("--store", os.path.join(blocked, "nested", "s.sqlite"))
        self.assertEqual(r.returncode, 2)

    def test_fail_under_breach_exits_1(self):
        gold = Path(self.tmp) / "tiny.jsonl"
        gold.write_text(json.dumps({
            "id": "tiny-1", "bucket": "fts",
            "query": "totally unrelated nonmatching tokens",
            "namespace": "project:eval-fts",
            "must_include_ids": ["e0000000-0000-4000-8000-000000000049"]}) + "\n",
            encoding="utf-8")
        r = self._run("--store", self.store, "--gold", str(gold),
                      "--fail-under", "1.01")
        self.assertEqual(r.returncode, 1)
        self.assertIn("fail-under", r.stderr)

    def test_fail_under_pass_exits_0(self):
        # Symmetric case: a reachable threshold that the corpus MEETS must
        # stay exit 0 (guards against the breach branch firing on every run).
        r = self._run("--store", self.store, "--gold", str(GOLD),
                      "--fail-under", "0.99")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])


class TestGoldSetShape(unittest.TestCase):
    def test_committed_gold_is_valid_and_covers_buckets(self):
        scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
        fixtures = str(FIXTURES)
        sys.path.insert(0, scripts)
        sys.path.insert(0, fixtures)
        self.addClassCleanup(sys.path.remove, fixtures)
        self.addClassCleanup(sys.path.remove, scripts)
        from storelib.eval_gold import load_gold  # local import (path above)
        import eval_store
        items = load_gold(str(GOLD))
        self.assertGreaterEqual(len(items), 42)
        counts: dict[str, int] = {}
        for it in items:
            counts[it.bucket] = counts.get(it.bucket, 0) + 1
        for bucket in ("as-of", "injection", "entity-alias", "namespace",
                       "contested", "fts"):
            self.assertGreaterEqual(counts.get(bucket, 0), 5, bucket)
        for bucket in ("retraction", "polarity", "change-intent"):
            self.assertGreaterEqual(counts.get(bucket, 0), 3, bucket)
        # Every fixture id the gold names must be one the builder mints.
        named: set[str] = set()
        for it in items:
            named.update(it.must_include_ids)
            named.update(it.must_exclude_ids)
        self.assertLessEqual(named, set(eval_store.EVAL_IDS["all"]),
                             "gold names an id the fixture builder never mints")
        # The original 30-item contract is frozen: no pre-#82 item may have
        # been renumbered onto a 51+ id.
        original = [it for it in items if it.bucket in
                    {"as-of", "injection", "entity-alias", "namespace",
                     "contested", "fts"}]
        self.assertEqual(len(original), 30)
        for it in original:
            for mid in it.must_include_ids + it.must_exclude_ids:
                self.assertLessEqual(int(mid.rsplit("-", 1)[1]), 50,
                                     f"{it.id} references a post-#82 id: {mid}")
        # Explicit flags live only on the change-intent bucket's explicit items.
        for it in items:
            if it.explicit:
                self.assertEqual(it.bucket, "change-intent")


class TestExplicitPassivity(unittest.TestCase):
    """The issue-#82 explicit eval seam (no_bump=False + no_telemetry=True +
    link_hops=1/link_budget=0) is a combination no other caller uses: prove it
    is a zero-write read at the evaluate_items level, not just byte-stable
    output at the runner level."""

    @classmethod
    def setUpClass(cls):
        # Build the fixture AT the module-frozen INPROC store so the
        # in-process connect() (frozen STORE_PATH) reads the corpus the
        # subprocess seed wrote.
        r = subprocess.run(
            [PYTHON, str(FIXTURES / "eval_store.py"), INPROC_STORE],
            env={**os.environ, "PYTHONUTF8": "1"},
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise AssertionError(r.stderr[-2000:])
        sys.path.insert(0, str(FIXTURES))
        cls.addClassCleanup(sys.path.remove, str(FIXTURES))
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        cls.addClassCleanup(sys.path.remove,
                            str(REPO_ROOT / "skills" / "memory" / "scripts"))
        from eval_store import EVAL_PIN_TS
        os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS

    def test_explicit_items_are_zero_write(self):
        import sqlite3
        from eval_store import EVAL_IDS
        from storelib.eval_gold import GoldItem, evaluate_items
        from storelib.schema import connect as storelib_connect

        def counters():
            conn = sqlite3.connect(INPROC_STORE)
            try:
                return conn.execute(
                    "SELECT retrieval_count, surfaced_count FROM memory "
                    "WHERE id IN (?, ?)", (EVAL_IDS["change_head"][62],
                                           EVAL_IDS["change_pred"][59]),
                ).fetchall()
            finally:
                conn.close()

        before_bytes = Path(INPROC_STORE).read_bytes()
        before_counts = counters()
        items = [
            GoldItem(id="x-explicit-1", bucket="change-intent",
                     query="what changed about the lint gate",
                     namespace="project:eval-change", explicit=True,
                     must_include_ids=[EVAL_IDS["change_head"][62],
                                       EVAL_IDS["change_pred"][59]]),
            GoldItem(id="x-passive-1", bucket="change-intent",
                     query="what changed about the lint gate",
                     namespace="project:eval-change",
                     must_include_ids=[EVAL_IDS["change_head"][62]],
                     must_exclude_ids=[EVAL_IDS["change_pred"][59]]),
        ]
        conn = storelib_connect()
        try:
            per_item, metrics = evaluate_items(conn, items)
        finally:
            conn.close()
        self.assertEqual([it["hit"] for it in per_item], [True, True])
        self.assertAlmostEqual(metrics["hit_at_k"], 1.0)
        self.assertEqual(counters(), before_counts,
                         "explicit eval items must not advance either counter")
        self.assertEqual(Path(INPROC_STORE).read_bytes(), before_bytes,
                         "explicit eval items must be byte-passive")


class TestAdapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-adapter-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)

    def _convert(self, adapter, src, name, extra=()):
        out = os.path.join(self.tmp, name)
        return subprocess.run(
            [PYTHON, str(ADAPTERS), "--adapter", adapter,
             "--input", src, "--out", out, *extra],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT)), out

    def test_longmemeval_toy_converts_and_validates(self):
        r, out = self._convert(
            "longmemeval",
            str(FIXTURES / "adapters" / "longmemeval_toy.jsonl"),
            "gold_lme.jsonl")
        self.assertEqual(r.returncode, 0, r.stderr)
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        self.addClassCleanup(sys.path.remove,
                             str(REPO_ROOT / "skills" / "memory" / "scripts"))
        from storelib.eval_gold import load_gold
        items = load_gold(out)
        self.assertEqual(len(items), 3)
        self.assertEqual({i.bucket for i in items}, {"adapter"})
        self.assertTrue(all(i.must_include_text for i in items))

    def test_locomo_toy_converts_and_validates(self):
        r, out = self._convert(
            "locomo",
            str(FIXTURES / "adapters" / "locomo_toy.json"),
            "gold_loco.jsonl")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(l) for l in
                 Path(out).read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(l["must_include_text"] for l in lines))

    def test_missing_corpus_skips_cleanly_exit_0(self):
        missing = os.path.join(self.tmp, "not-there.jsonl")
        r, _out = self._convert("longmemeval", missing, "never.jsonl")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("skipped", r.stdout)

    def test_unknown_adapter_refused(self):
        r = subprocess.run(
            [PYTHON, str(ADAPTERS), "--adapter", "notreal",
             "--input", "whatever", "--out", os.path.join(self.tmp, "x.jsonl")],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 2)

    def test_garbage_corpus_is_an_operational_error(self):
        garbage = Path(self.tmp) / "garbage.jsonl"
        garbage.write_text("{not json at all", encoding="utf-8")
        r, _out = self._convert("longmemeval", str(garbage), "nope.jsonl")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
