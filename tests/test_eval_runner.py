"""Tests for the offline eval runner and public-corpus adapters
(issue #64, 9.1/9.2/9.3).

Covers:
  - End-to-end: the runner auto-builds its deterministic fixture corpus at the
    given --store path, runs the committed eval/gold.jsonl (30 items, six
    buckets), and exits 0 with a full JSON report on stdout. On the fixture
    corpus the metrics are deterministic 1.0 (hit@k, MRR, as-of accuracy,
    injection-omit) — this is the empirical proof the gold set is sound.
  - Byte-stability: two consecutive runs produce byte-identical JSON.
  - Passivity: a run leaves the store byte-identical (no_bump eval must not
    even bump surfaced_count).
  - Operational refusals: missing --store -> exit 2; invalid gold (unknown
    bucket, empty query, assertion-less item, id overlap) -> exit 2 naming
    the item; --fail-under breach -> exit 1.
  - Gold-set shape: every committed item passes load_gold validation and
    covers all six issue-mandated buckets (>=5 items each).
  - Adapters: toy fixtures convert and validate through load_gold; a missing
    corpus prints "skipped" and exits 0; an unknown adapter exits 2.

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

    def test_gold_set_has_all_six_buckets_with_metrics_1(self):
        # The fixture corpus is designed so every bucket's assertion holds —
        # a miss here means either the corpus or the recall pipeline drifted.
        self.assertEqual(len(self.report["per_item"]), 30)
        buckets = {b["bucket"] for b in self.report["per_item"]}
        self.assertEqual(buckets, {"as-of", "injection", "namespace",
                                   "contested", "entity-alias", "fts"})
        for bucket, agg in self.report["per_bucket"].items():
            self.assertEqual(agg["items"], 5, bucket)
            self.assertEqual(agg["hits"], 5, bucket)
            self.assertEqual(agg["excluded_surfaced"], 0, bucket)
        self.assertAlmostEqual(self.report["metrics"]["hit_at_k"], 1.0)
        self.assertAlmostEqual(self.report["metrics"]["mrr"], 1.0)
        self.assertAlmostEqual(self.report["metrics"]["as_of_accuracy"], 1.0)
        self.assertAlmostEqual(self.report["metrics"]["injection_omit_rate"], 1.0)
        self.assertEqual(self.report["metrics"]["as_of_items"], 5)
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
    def test_committed_gold_is_valid_and_covers_six_buckets(self):
        scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
        fixtures = str(FIXTURES)
        sys.path.insert(0, scripts)
        sys.path.insert(0, fixtures)
        self.addClassCleanup(sys.path.remove, fixtures)
        self.addClassCleanup(sys.path.remove, scripts)
        from storelib.eval_gold import load_gold  # local import (path above)
        import eval_store
        items = load_gold(str(GOLD))
        self.assertGreaterEqual(len(items), 30)
        counts: dict[str, int] = {}
        for it in items:
            counts[it.bucket] = counts.get(it.bucket, 0) + 1
        for bucket in ("as-of", "injection", "entity-alias", "namespace",
                       "contested", "fts"):
            self.assertGreaterEqual(counts.get(bucket, 0), 5, bucket)
        # Every fixture id the gold names must be one the builder mints.
        named: set[str] = set()
        for it in items:
            named.update(it.must_include_ids)
            named.update(it.must_exclude_ids)
        self.assertLessEqual(named, set(eval_store.EVAL_IDS["all"]),
                             "gold names an id the fixture builder never mints")


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
