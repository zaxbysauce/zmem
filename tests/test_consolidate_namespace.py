"""Namespace containment in `consolidate`, plus import-time robustness of the
store's float env knobs.

Two data-integrity guarantees are covered here:

  1. NAMESPACE CONTAINMENT (both clustering paths). `consolidate` must never
     merge/supersede a memory from one namespace into another. The dangerous
     caller is the automatic one: `zmem-session-start.sh` fires
     `store.py consolidate` with NO `--namespace`, and before this fix the
     namespace filter in BOTH the lexical (Jaccard) loop and the cosine (vec0)
     neighbour-verification query was applied only when the caller had passed
     one — so the background run could supersede one project's memory into an
     unrelated project's. Both tests below therefore invoke consolidate with no
     namespace argument at all, which is the exact production shape.

  2. A MALFORMED FLOAT ENV VAR MUST NOT BREAK UNRELATED COMMANDS. The
     consolidate/dedup thresholds are parsed at module scope, so a bare
     `float(os.environ[...])` there turns one typo'd env var into an
     import-time crash of every store.py subcommand.

Run: python tests/test_consolidate_namespace.py   (no pytest — repo convention)
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS_A = "project:consolidate-ns-a"
NS_B = "project:consolidate-ns-b"

# Deliberately IDENTICAL text in the two namespaces: Jaccard similarity 1.0 and
# cosine 1.0, far above either threshold, so the only thing that can keep them
# apart is the namespace check itself. A test built on merely "similar-sounding"
# content could pass with the bug present.
SHARED_TEXT = ("The build pipeline uses pytest with the --tb=short flag "
               "for concise tracebacks on failure")
NEAR_DUP_TEXT = ("The build pipeline uses pytest with the --tb=short flag "
                 "for concise output on failure")
UNRELATED_TEXT = "Completely unrelated memory about database indexing strategies"


def _load_store_module(store_path: Path, models_dir: Path):
    """A fresh store.py module instance pinned to a throwaway store, with the
    embedding model forced absent (store.py resolves both at import time)."""
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_consolidate_{id(store_path)}", STORE_PY
    )
    env = {
        "ZMEM_STORE": str(store_path),
        "ZMEM_MODELS_DIR": str(models_dir),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _vec(*values: float) -> bytes:
    """A 384-dim float32 blob in store.py's on-disk embedding format
    (struct.pack("384f", ...)), padded with zeros after `values`."""
    dim = 384
    vals = list(values) + [0.0] * (dim - len(values))
    return struct.pack(f"{dim}f", *vals)


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1a. Lexical (Jaccard) fallback path — end-to-end through the real CLI
# ---------------------------------------------------------------------------
class LexicalNamespaceContainmentTest(unittest.TestCase):
    """Drives the real `store.py consolidate` CLI with the model absent, so the
    lexical fallback is the clustering path, and with NO --namespace, so the
    only namespace scoping in play is the seed-namespace containment check."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-consolidate-ns-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.models = os.path.join(self.tmp, "no-such-models")
        os.makedirs(self.models, exist_ok=True)
        self.env = {
            **os.environ,
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_MODELS_DIR": self.models,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        self.env.pop("ZMEM_DATA", None)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def _add(self, namespace: str, content: str, confidence: str):
        r = self._run("add", "--namespace", namespace, "--type", "fact",
                      "--content", content, "--signal", "test",
                      "--confidence", confidence)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _live(self, namespace: str) -> list[str]:
        r = self._run("list", "--namespace", namespace, "--limit", "50")
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l for l in r.stdout.splitlines() if l.startswith("[") and " live " in l]

    def test_background_consolidate_never_merges_across_namespaces(self):
        # NS_A holds a true near-duplicate pair (must merge). NS_B holds a row
        # whose content is IDENTICAL to NS_A's keeper (must NOT merge).
        self._add(NS_A, SHARED_TEXT, "0.9")
        self._add(NS_A, NEAR_DUP_TEXT, "0.85")
        self._add(NS_B, SHARED_TEXT, "0.8")
        self._add(NS_B, UNRELATED_TEXT, "0.7")

        # No --namespace: exactly how zmem-session-start.sh invokes it.
        r = self._run("consolidate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lexical", r.stderr.lower(), "expected the lexical fallback path")
        # Exactly ONE merge: the within-NS_A near-duplicate. If the namespace
        # containment check regresses, the NS_B twin is absorbed too -> 2.
        self.assertIn("merged 1 memories", r.stdout, r.stdout)

        self.assertEqual(len(self._live(NS_A)), 1, "NS_A near-dup pair should collapse to 1")
        self.assertEqual(len(self._live(NS_B)), 2,
                         "NS_B rows must be untouched by a consolidation seeded in NS_A")

    def test_within_namespace_duplicates_still_merge_with_no_namespace_arg(self):
        """The containment fix must not turn consolidate into a no-op: with a
        single namespace and no --namespace argument, real near-duplicates
        still merge."""
        self._add(NS_A, SHARED_TEXT, "0.9")
        self._add(NS_A, NEAR_DUP_TEXT, "0.85")
        self._add(NS_A, UNRELATED_TEXT, "0.7")

        r = self._run("consolidate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("merged 1 memories", r.stdout, r.stdout)
        self.assertEqual(len(self._live(NS_A)), 2)


# ---------------------------------------------------------------------------
# 1b. Cosine (vec0 KNN) path — in-process, with hand-written embeddings
# ---------------------------------------------------------------------------
@unittest.skipUnless(_sqlite_vec_available(),
                     "sqlite-vec not installed — the vec0 KNN path cannot be exercised")
class CosineNamespaceContainmentTest(unittest.TestCase):
    """The cosine path needs embeddings, and CI deliberately runs without the
    ~90MB model, so the vectors here are written by hand in store.py's own
    on-disk format and `_embeddings` is stubbed available. That exercises the
    real vec0 KNN query and the real neighbour-verification SELECT — the two
    places the namespace filter has to live — without needing the model.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-consolidate-cos-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        models = Path(self.tmp) / "no-such-models"
        models.mkdir()
        self.store_mod = _load_store_module(Path(self.tmp) / "store.sqlite", models)
        self.conn = self.store_mod.connect()
        self.addCleanup(self.conn.close)
        self.store_mod.init_db(self.conn)
        self.store_mod.migrate(self.conn)
        have_vec = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_vec'"
        ).fetchone()
        if not have_vec:
            self.skipTest("memory_vec (vec0) table unavailable in this sqlite build")

    def _add_embedded(self, namespace: str, content: str, confidence: float,
                      embedding: bytes) -> str:
        """Add a row, then REPLACE its embedding with the supplied vector.

        Contents are deliberately unrelated to each other so write-time dedup
        never collapses two of them before consolidate ever runs — in this test
        the hand-written vectors, not the text, decide what is a near-duplicate.
        Any vec0 row add_memory may have written is dropped first, so each id
        contributes exactly one (known) vector to the KNN index.
        """
        mid = self.store_mod.add_memory(
            self.conn, namespace=namespace, type_="fact", content=content,
            signal="test", confidence=confidence,
        )
        self.conn.execute(
            "UPDATE memory SET embedding=?, embedding_model='test-stub' WHERE id=?",
            (embedding, mid),
        )
        self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
        self.conn.execute(
            "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
            (embedding, mid),
        )
        self.conn.commit()
        return mid

    def _is_live(self, mid: str) -> bool:
        row = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (mid,)
        ).fetchone()
        return row is not None and row["superseded_at"] is None

    def test_vec_knn_neighbours_are_scoped_to_the_seed_namespace(self):
        seed_vec = _vec(1.0)
        # cos(near, seed) = 1/sqrt(1.16) ~= 0.928 -> above the 0.80 default.
        near_vec = _vec(1.0, 0.4)
        far_vec = _vec(0.0, 0.0, 1.0)  # orthogonal to the seed

        a_seed = self._add_embedded(NS_A, "alpha keeper row about deployment", 0.9, seed_vec)
        a_near = self._add_embedded(NS_A, "bravo row about scheduling windows", 0.85, near_vec)
        # Byte-identical vector to the seed, in a DIFFERENT namespace: cosine
        # distance 0, the strongest possible KNN hit. Only the namespace check
        # can keep it out of the cluster.
        b_twin = self._add_embedded(NS_B, "charlie row about index rebuilds", 0.8, seed_vec)
        b_other = self._add_embedded(NS_B, "delta row about log rotation", 0.7, far_vec)

        # Force the cosine path (the model file is genuinely absent here).
        stub = mock.Mock()
        stub.is_available.return_value = True
        with mock.patch.object(self.store_mod, "_embeddings", stub):
            # No `namespace=` argument — the background-run shape.
            self.store_mod.consolidate(self.conn)

        self.assertTrue(self._is_live(a_seed), "keeper must stay live")
        self.assertFalse(self._is_live(a_near),
                         "same-namespace near-duplicate should have been absorbed")
        self.assertTrue(self._is_live(b_twin),
                        "an identical-embedding row in ANOTHER namespace must never "
                        "be superseded by a consolidation seeded in NS_A")
        self.assertTrue(self._is_live(b_other))

        reason = self.conn.execute(
            "SELECT supersede_reason FROM memory WHERE id=?", (a_near,)
        ).fetchone()["supersede_reason"]
        self.assertIn(a_seed, reason)


# ---------------------------------------------------------------------------
# 2. Malformed float env vars must not crash unrelated commands
# ---------------------------------------------------------------------------
class MalformedFloatEnvTest(unittest.TestCase):
    """Every module-scope float knob is parsed through store._env_float, which
    falls back to the default instead of raising. A garbage value must leave
    completely unrelated subcommands working."""

    GARBAGE = {
        "ZMEM_CONSOLIDATE_LEXICAL_THRESHOLD": "not-a-number",
        "ZMEM_CONSOLIDATE_THRESHOLD": "",
        "ZMEM_DEDUP_THRESHOLD": "0.8x",
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-envfloat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {
            **os.environ,
            **self.GARBAGE,
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        self.env.pop("ZMEM_DATA", None)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_stats_still_runs_with_garbage_threshold_env(self):
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("total=", r.stdout)

    def test_add_and_consolidate_still_run_with_garbage_threshold_env(self):
        r = self._run("add", "--namespace", NS_A, "--type", "fact",
                      "--content", SHARED_TEXT, "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("consolidate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_defaults_are_used_when_the_value_is_garbage(self):
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r});"
            "import store;"
            "print(store.CONSOLIDATE_LEXICAL_THRESHOLD,"
            " store.CONSOLIDATE_DEFAULT_THRESHOLD,"
            " store.DEDUP_SIMILARITY_THRESHOLD)"
        )
        r = subprocess.run([PYTHON, "-c", code], env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["0.6", "0.8", "0.85"])

    def test_a_valid_value_is_still_honoured(self):
        env = {**self.env, "ZMEM_CONSOLIDATE_LEXICAL_THRESHOLD": "0.42"}
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r});"
            "import store; print(store.CONSOLIDATE_LEXICAL_THRESHOLD)"
        )
        r = subprocess.run([PYTHON, "-c", code], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0.42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
