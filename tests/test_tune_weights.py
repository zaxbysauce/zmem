"""Tests for `store.py tune-weights --dry-run` (issue #64, 9.6).

Covers:
  - Dry-run works end-to-end against the fixture eval store: exit 0, JSON on
    stdout with `current` and `suggested` weight vectors that each sum to 1.0
    and carry the four W_* keys.
  - The store is untouched: the store FILE bytes are identical before/after a
    dry run (asserted per-test and again at class teardown).
  - No apply path ships: `--apply` is an argparse error (exit 2), and a
    source scan proves tune.py registers no --apply argument, assigns no W_*
    module globals, and performs no store writes.
  - In-process run leaves the recall W_* constants byte-identical (candidates
    travel through compute_score's `weights` parameter, never globals).
  - Refusals: `tune-weights` without --dry-run -> exit 2 with the manual-edit
    guidance; a missing / invalid gold file -> exit 2.

Run: python tests/test_tune_weights.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
RUNNER = REPO_ROOT / "scripts" / "eval_runner.py"
GOLD = REPO_ROOT / "eval" / "gold.jsonl"
TUNE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "tune.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
PYTHON = sys.executable

WEIGHT_KEYS = {"bm25", "confidence", "recency", "popularity"}
EVAL_PIN_TS = "2026-06-01T00:00:00Z"


class TuneWeightsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-tune-")
        cls.addClassCleanup(_rmtree, cls.tmp)
        # Build the deterministic eval corpus ONCE via the runner (the same
        # builder CI uses). The store lives under the test's tmp dir — never
        # the operator home store.
        cls.store = os.path.join(cls.tmp, "store.sqlite")
        build_env = {**os.environ,
                     "ZMEM_STORE": os.path.join(cls.tmp, "unused.sqlite")}
        r = subprocess.run([PYTHON, str(RUNNER), "--store", cls.store],
                           env=build_env, capture_output=True, text=True,
                           timeout=600)
        assert r.returncode == 0, r.stderr[-2000:]
        cls.env = {**os.environ,
                   "ZMEM_STORE": cls.store,
                   "ZMEM_EMBED_PROFILE": "fake",
                   "ZMEM_TEST_NOW": EVAL_PIN_TS,
                   "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        for k in ("ZMEM_DATA", "ZMEM_BACKUP_DIR"):
            cls.env.pop(k, None)
        cls.store_bytes = Path(cls.store).read_bytes()

    @classmethod
    def tearDownClass(cls):
        assert Path(cls.store).read_bytes() == cls.store_bytes, \
            "the tune test suite must leave the fixture store byte-identical"

    def setUp(self):
        self._bytes_before = Path(self.store).read_bytes()
        self.addCleanup(self._assert_store_untouched)

    def _assert_store_untouched(self):
        self.assertEqual(Path(self.store).read_bytes(), self._bytes_before,
                         "tune-weights --dry-run must write NOTHING to the store")

    def _tune(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), "tune-weights", *args],
                              env=self.env, capture_output=True, text=True,
                              timeout=600)

    def test_dry_run_reports_summing_weight_vectors(self):
        r = self._tune("--dry-run", "--gold", str(GOLD))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        report = json.loads(r.stdout)
        self.assertTrue(report["dry_run"])
        self.assertIn("nothing written", report["note"])
        for side in ("current", "suggested"):
            weights = report[side]["weights"]
            self.assertEqual(set(weights), WEIGHT_KEYS)
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=9,
                                   msg=f"{side} weights must sum to 1.0")
            self.assertIn("hit_at_k", report[side]["metrics"])
            self.assertIn("mrr", report[side]["metrics"])
        # The fixture corpus is built so the shipped weights already achieve
        # the maximum objective: the suggestion must not regress.
        self.assertGreaterEqual(report["suggested"]["objective"],
                                report["current"]["objective"] - 1e-9)

    def test_apply_flag_does_not_exist(self):
        r = self._tune("--dry-run", "--gold", str(GOLD), "--apply")
        self.assertEqual(r.returncode, 2, "an untested apply path must not ship")

    def test_without_dry_run_refused_exit_2(self):
        r = self._tune("--gold", str(GOLD))
        self.assertEqual(r.returncode, 2)
        self.assertIn("manual edit", r.stderr)

    def test_missing_gold_refused_exit_2(self):
        r = self._tune("--dry-run", "--gold",
                       os.path.join(self.tmp, "nope.jsonl"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read", r.stderr)

    def test_invalid_gold_refused_exit_2(self):
        bad = Path(self.tmp) / "bad.jsonl"
        bad.write_text(json.dumps({"id": "x", "bucket": "nope",
                                   "query": "q",
                                   "must_include_ids": ["y"]}) + "\n",
                       encoding="utf-8")
        r = self._tune("--dry-run", "--gold", str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn("x", r.stderr)


class TuneSourceAndGlobalsTest(unittest.TestCase):
    """Structural + in-process guarantees that a dry-run tune can never write
    the store or mutate the shipped ranking weights."""

    def test_tune_surface_has_no_apply_and_no_weight_assignment(self):
        # The REGISTERED argparse surface must not expose --apply (the
        # docstring may mention its absence; the parser must not offer it).
        tmp = tempfile.mkdtemp(prefix="zmem-tune-help-")
        self.addClassCleanup(_rmtree, tmp)
        env = {**os.environ,
               "ZMEM_STORE": os.path.join(tmp, "unused.sqlite"),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        r = subprocess.run([PYTHON, str(STORE_PY), "tune-weights", "--help"],
                           env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--apply", r.stdout)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("--gold", r.stdout)
        # tune.py itself assigns no W_* module globals and issues no memory
        # writes (evaluate_items/recall run with no_telemetry=True).
        source = TUNE_PY.read_text(encoding="utf-8")
        for forbidden in ("W_BM25 =", "W_CONFIDENCE =", "W_RECENCY =",
                          "W_POPULARITY ="):
            self.assertNotIn(forbidden, source)
        for sql_write in ("UPDATE memory", "INSERT INTO memory",
                          "DELETE FROM memory", "executescript"):
            self.assertNotIn(sql_write, source)

    def test_current_weights_baseline_matches_recall_constants(self):
        """tune.py duplicates the shipped W_* as its eval baseline (documented
        as intentional, for reproducible reports). This test closes the drift:
        if recall.py's constants ever change without tune.py following, the
        'current' baseline would silently misreport — pin them equal."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        self.addClassCleanup(sys.path.remove, str(SCRIPTS_DIR))
        os.environ["ZMEM_STORE"] = os.path.join(
            tempfile.mkdtemp(prefix="zmem-tune-pin-"), "import-only.sqlite")
        self.addCleanup(os.environ.pop, "ZMEM_STORE", None)
        for mod in ("storelib", "storelib.recall", "storelib.tune",
                    "storelib.eval_gold", "storelib.schema"):
            sys.modules.pop(mod, None)
        try:
            import storelib.recall as recall_mod
            import storelib.tune as tune_mod
            self.assertEqual(
                tune_mod._CURRENT_WEIGHTS,
                (recall_mod.W_BM25, recall_mod.W_CONFIDENCE,
                 recall_mod.W_RECENCY, recall_mod.W_POPULARITY),
                "tune.py's _CURRENT_WEIGHTS baseline drifted from the shipped "
                "W_* constants in storelib/recall.py — update both together")
        finally:
            for mod in ("storelib", "storelib.recall", "storelib.tune",
                        "storelib.eval_gold", "storelib.schema"):
                sys.modules.pop(mod, None)

    def test_in_process_tune_leaves_w_globals_identical(self):
        tmp = tempfile.mkdtemp(prefix="zmem-tune-proc-")
        self.addClassCleanup(_rmtree, tmp)
        # Build a small corpus store via the runner (same builder as CI).
        store = os.path.join(tmp, "store.sqlite")
        build_env = {**os.environ,
                     "ZMEM_STORE": os.path.join(tmp, "unused.sqlite")}
        r = subprocess.run([PYTHON, str(RUNNER), "--store", store],
                           env=build_env, capture_output=True, text=True,
                           timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

        sys.path.insert(0, str(SCRIPTS_DIR))
        self.addClassCleanup(sys.path.remove, str(SCRIPTS_DIR))
        os.environ["ZMEM_STORE"] = os.path.join(tmp, "import-only.sqlite")
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS
        self.addClassCleanup(os.environ.pop, "ZMEM_STORE", None)
        for mod in ("storelib", "storelib.schema", "storelib.recall",
                    "storelib.tune", "storelib.eval_gold"):
            sys.modules.pop(mod, None)
        try:
            import storelib.recall as recall_mod
            import storelib.tune as tune_mod
            keys = ("W_BM25", "W_CONFIDENCE", "W_RECENCY", "W_POPULARITY")
            before = tuple(getattr(recall_mod, k) for k in keys)
            # Strictly read-only connection: even a stray write would raise.
            conn = sqlite3.connect(
                "file:" + store.replace("\\", "/") + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rc = tune_mod.tune_weights(conn, gold_path=str(GOLD))
            self.assertEqual(rc, 0)
            after = tuple(getattr(recall_mod, k) for k in keys)
            self.assertEqual(before, after,
                             "tune must never mutate the W_* module globals")
        finally:
            for mod in ("storelib", "storelib.schema", "storelib.recall",
                        "storelib.tune", "storelib.eval_gold"):
                sys.modules.pop(mod, None)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
