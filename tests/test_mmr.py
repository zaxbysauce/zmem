"""MMR diversity tests (v10, issue #60, 5.5).

All behavioral tests run model-absent (ZMEM_MODELS_DIR at a nonexistent
path) — the Jaccard-on-content_norm similarity path is the CI-verified
default, exactly as the issue requires. Cosine behavior is unit-tested with
known packed bytes that mirror embeddings.py's writer format.

Run: python tests/test_mmr.py  (no pytest; house convention)
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_MMR_LAMBDA", None)
    return env


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-mmr-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args, env=None):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=120,
        )
        if r.returncode != 0:
            self.fail(
                f"store.py {' '.join(args)} exited {r.returncode}\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )
        return r

    def recall_ids(self, *extra, env_extra=None):
        env = dict(self.env)
        env.update(env_extra or {})
        out = subprocess.run(
            [PYTHON, str(STORE_PY), "recall", "--query",
             "docker networking bridge setup", "--namespace", "project:mmr",
             "--json", "--limit", "4", *extra],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = json.loads(out.stdout)
        return [r["content"] for r in rows]

    def seed_crowded(self):
        """8 near-paraphrases + 1 distinct fact sharing exactly one query
        term ('docker') — the distinct row enters the candidate pool but
        ranks below the paraphrase cluster on pure composite score."""
        for i, w in enumerate(WORDS):
            self.run_store(
                "add", "--namespace", "project:mmr", "--type", "fact",
                "--content",
                f"docker networking bridge setup variant {w} number {i}",
                "--signal", "test",
            )
        self.run_store(
            "add", "--namespace", "project:mmr", "--type", "fact",
            "--content", "docker compose override file syntax quick note",
            "--signal", "test",
        )

    @staticmethod
    def _is_distinct(content: str) -> bool:
        return "compose override" in content


class MmrAcceptanceTest(_Store):
    def test_default_top4_includes_and_promotes_distinct_fact(self):
        self.seed_crowded()
        default = self.recall_ids()
        no_mmr = self.recall_ids("--no-mmr")
        # The issue's acceptance: the distinct fact is IN the default top-4.
        self.assertTrue(
            any(self._is_distinct(c) for c in default),
            f"default (MMR) top-4 must include the distinct fact; got {default}",
        )
        self.assertTrue(
            any(self._is_distinct(c) for c in no_mmr),
            "the distinct fact shares a query term, so it is expected in the "
            "no-mmr pool too (the issue's 'may return four paraphrases' is a "
            "MAY, not a must)",
        )
        # And MMR promotes it strictly earlier than pure score order does.
        self.assertLess(
            [self._is_distinct(c) for c in default].index(True),
            [self._is_distinct(c) for c in no_mmr].index(True),
            "MMR must rank the distinct fact earlier than --no-mmr does",
        )
        # Diversity is real: the two orderings differ.
        self.assertNotEqual(default, no_mmr)

    def test_lambda_one_equals_no_mmr(self):
        self.seed_crowded()
        no_mmr = self.recall_ids("--no-mmr")
        lam_one = self.recall_ids(env_extra={"ZMEM_MMR_LAMBDA": "1.0"})
        self.assertEqual(
            no_mmr, lam_one,
            "lambda=1.0 must degenerate to pure composite order — identical "
            "sequence to --no-mmr",
        )

    def test_lambda_clamped_and_garbage_env_safe(self):
        self.seed_crowded()
        default = self.recall_ids()
        garbage = self.recall_ids(env_extra={"ZMEM_MMR_LAMBDA": "not-a-float"})
        self.assertEqual(
            default, garbage,
            "a malformed ZMEM_MMR_LAMBDA must fall back to the 0.7 default "
            "(never crash at import or recall time)",
        )
        over = self.recall_ids(env_extra={"ZMEM_MMR_LAMBDA": "1.5"})
        no_mmr = self.recall_ids("--no-mmr")
        self.assertEqual(
            no_mmr, over,
            "an out-of-range lambda clamps to 1.0 (== no diversity)",
        )

    def test_limit_one_returns_top_row(self):
        self.run_store(
            "add", "--namespace", "project:mmr", "--type", "fact",
            "--content", "solo anchor content", "--signal", "test",
        )
        rows = json.loads(self.run_store(
            "recall", "--query", "solo anchor", "--namespace", "project:mmr",
            "--json", "--limit", "1",
        ).stdout)
        self.assertEqual(len(rows), 1)

    def test_no_mmr_flag_documented(self):
        out = self.run_store("recall", "--help").stdout
        self.assertIn("--no-mmr", out)
        self.assertIn("ZMEM_MMR_LAMBDA", out)


class MmrUnitTests(unittest.TestCase):
    """In-process checks of the similarity + ordering primitives."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-mmr-unit-")
        os.environ["ZMEM_STORE"] = str(Path(cls.tmp) / "s.sqlite")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        import importlib.util
        spec = importlib.util.spec_from_file_location("zmem_store_mmr", STORE_PY)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("ZMEM_STORE", None)

    def _pack(self, vals):
        # Mirror the writer exactly: embeddings.py uses
        # struct.pack(f"{_MODEL_DIM}f", *pooled[0]) — native float32.
        return struct.pack(f"{len(vals)}f", *vals)

    def test_cosine_known_bytes(self):
        cos = self.mod._cosine_blob
        a = self._pack([1.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(cos(a, a), 1.0, places=6)
        self.assertAlmostEqual(cos(a, self._pack([0.0, 1.0, 0.0, 0.0])), 0.0, places=6)
        self.assertAlmostEqual(cos(a, self._pack([-1.0, 0.0, 0.0, 0.0])), -1.0, places=6)
        self.assertAlmostEqual(
            cos(self._pack([1.0, 1.0]), self._pack([1.0, 0.0])), 0.70710678, places=5
        )
        self.assertIsNone(cos(a, self._pack([1.0, 0.0])), "length mismatch → None")
        self.assertIsNone(cos(None, a))
        self.assertIsNone(cos(a, b""))
        self.assertEqual(cos(a, self._pack([0.0, 0.0, 0.0, 0.0])), 0.0,
                         "zero vector degrades to 0, never a ZeroDivisionError")

    def test_jaccard_norm(self):
        jac = self.mod._jaccard_norm
        self.assertEqual(jac("a b c", "a b c"), 1.0)
        self.assertEqual(jac("a b", "c d"), 0.0)
        self.assertAlmostEqual(jac("a b c", "a b d"), 0.5)
        self.assertEqual(jac("", "a b"), 0.0)
        self.assertEqual(jac(None, None), 0.0)

    def _scored(self):
        # Three near-identical rows (high Jaccard) + one distinct row that
        # scores lowest on pure composite.
        return [
            (0.9, {"id": "p1", "content": "docker network bridge setup alpha"}),
            (0.85, {"id": "p2", "content": "docker network bridge setup beta"}),
            (0.8, {"id": "p3", "content": "docker network bridge setup gamma"}),
            (0.7, {"id": "d1", "content": "banana bread baking timer notes"}),
        ], {
            "p1": "docker network bridge setup alpha",
            "p2": "docker network bridge setup beta",
            "p3": "docker network bridge setup gamma",
            "d1": "banana bread baking timer notes",
        }

    def test_mmr_picks_distinct_early(self):
        scored, norms = self._scored()
        out = self.mod._mmr_order(scored, 4, 0.7, norms, {})
        ids = [item["id"] for _s, item in out]
        self.assertEqual(ids[0], "p1", "first pick is the top-scored row")
        self.assertEqual(ids[1], "d1",
                         "the distinct fact must be picked before the "
                         "redundant paraphrases")

    def test_mmr_lambda_one_is_pure_order(self):
        scored, norms = self._scored()
        out = self.mod._mmr_order(scored, 4, 1.0, norms, {})
        ids = [item["id"] for _s, item in out]
        self.assertEqual(ids, ["p1", "p2", "p3", "d1"],
                         "lambda=1.0 must equal the pure score order")

    def test_mmr_respects_limit_and_single_candidate(self):
        scored, norms = self._scored()
        out = self.mod._mmr_order(scored, 2, 0.7, norms, {})
        self.assertEqual(len(out), 2)
        single = self.mod._mmr_order(scored[:1], 4, 0.7, norms, {})
        self.assertEqual(len(single), 1)
        self.assertEqual(self.mod._mmr_order([], 4, 0.7, {}, {}), [])
        self.assertEqual(self.mod._mmr_order(scored, 0, 0.7, norms, {}), [])

    def test_mmr_all_zero_scores_still_seeks_diversity(self):
        """Reviewer-round pin: when every composite score is 0 (denominator
        guard falls back to 1.0, all rel values tie at 0.0), the greedy pick
        reduces to maximizing -diversity — which picks the MOST diverse row
        next, not the least. Pinned so the degenerate boundary cannot invert
        silently."""
        scored = [
            (0.0, {"id": "a", "content": "alpha beta gamma"}),
            (0.0, {"id": "b", "content": "alpha beta gamma"}),
            (0.0, {"id": "c", "content": "alpha beta gamma"}),
            (0.0, {"id": "d", "content": "totally different tokens"}),
        ]
        norms = {i["id"]: i["content"] for _s, i in scored}
        out = self.mod._mmr_order(scored, 4, 0.7, norms, {})
        ids = [i["id"] for _s, i in out]
        self.assertEqual(ids[0], "a", "first pick stays the list head on ties")
        self.assertEqual(ids[1], "d",
                         "the distinct row must be picked before the "
                         "redundant cluster even when all scores are zero")

    def test_mmr_uses_embedding_cosine_when_available(self):
        scored, norms = self._scored()
        # p1 and p2 embeddings identical; d1 orthogonal to both. With lambda
        # 0.7 the cosine path must demote p2 exactly like the Jaccard path.
        embs = {
            "p1": self._pack([1.0, 0.0]),
            "p2": self._pack([1.0, 0.0]),
            "p3": self._pack([0.99, 0.1]),
            "d1": self._pack([0.0, 1.0]),
        }
        out = self.mod._mmr_order(scored, 4, 0.7, norms, embs)
        ids = [item["id"] for _s, item in out]
        self.assertEqual(ids[1], "d1")

    def test_module_lambda_default(self):
        self.assertEqual(self.mod.MMR_LAMBDA_DEFAULT, 0.7)
        self.assertEqual(self.mod.MMR_LAMBDA, 0.7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
