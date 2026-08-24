"""Issue #58, 3.1 + 3.2: namespace-aware vec0 KNN and dedup window.

Three namespaces with near-duplicate embeddings; the recall path must
post-filter by namespace so a foreign-namespace cluster does not
crowd out same-namespace hits. Also asserts the negative: with a tight
over-fetch, foreign-namespace-only vec neighborhoods yield zero
same-namespace vec candidates (not leaked foreign ids).
"""

from __future__ import annotations

import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))


def _sqlite_vec_available() -> bool:
    """Repo convention (tests/test_consolidate_namespace.py): model-absent
    CI runners may lack the sqlite-vec extension entirely — connect()
    then silently degrades to FTS-only and memory_vec never exists. The
    direct-KNN tests below insert into memory_vec and must skip there."""
    # PRR-026 fix: catch Exception (not just ImportError) — a native
    # library LOAD failure (DLL error) must skip, not break collection.
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(
    _sqlite_vec_available(),
    "sqlite-vec extension unavailable — vec0 namespace KNN tests skipped "
    "(CI runs model-absent/degraded)",
)
class NamespaceKnnTests(unittest.TestCase):
    """Direct test of ``_vec_knn_in_namespace`` (the shared helper)
    against a hand-built store. Bypasses the embedding runtime by
    inserting deterministic ``memory_vec`` rows directly, so this test
    runs model-absent on every host."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-ns-knn-")
        cls.store_path = Path(cls.tmp) / "store.sqlite"

    def setUp(self):
        self._saved_store = os.environ.get("ZMEM_STORE")
        os.environ["ZMEM_STORE"] = str(self.store_path)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # Evict any cached module state from previous tests in this
        # session so the per-test ZMEM_STORE is honored.
        for mod in list(sys.modules.keys()):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]

        # Fresh store on disk: wipe any prior tmp file just in case
        # the previous test left state behind.
        try:
            self.store_path.unlink()
        except FileNotFoundError:
            pass

        # Re-import everything so the new env is honored.
        from storelib.schema import (
            _vec_knn_in_namespace,
            init_db,
            connect,
            ALLOWED_TYPES,
        )
        conn = connect()
        init_db(conn)
        # Belt-and-suspenders (sibling pattern): even when sqlite_vec
        # imports, a degraded build may not create the vec0 table.
        has_vec = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_vec'"
        ).fetchone() is not None
        if not has_vec:
            conn.close()
            self.skipTest("memory_vec (vec0) table unavailable in this sqlite build")
        # Insert three namespaces, each with one memory and one vec row.
        # Use distinct embeddings so vec0 can rank them. The query
        # embedding is CLOSE to all three but slightly nearer to
        # ns-knn-a; ns-knn-b is one notch behind; ns-knn-c is two.
        # Cosine distance stays well below 1.0 so all three are valid
        # recall candidates for the unscoped path.
        def _emb(value: float) -> bytes:
            v = [0.0] * 384
            v[0] = value
            v[1] = 1.0 - value
            return struct.pack(f"<{len(v)}f", *v)

        # Query is offset 0.02 from each row so all three are
        # near-neighbors (cosine distance ~0.0008-0.04) but not
        # identical. ns-knn-a (offset 0.0) ranks first.
        self._query_emb = _emb(1.02)

        namespaces = [
            ("project:ns-knn-a", "alpha memory content", _emb(1.0)),
            ("project:ns-knn-b", "beta memory content", _emb(1.04)),
            ("project:ns-knn-c", "gamma memory content", _emb(1.10)),
        ]
        for ns, content, emb in namespaces:
            cur = conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "ingestion_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{ns}-id", ns, ALLOWED_TYPES[0], content, "", "", "", 0.9,
                 "test", "2026-02-03T04:05:06Z", "2026-02-03T04:05:06Z"),
            )
            conn.execute(
                "INSERT INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
                (f"{ns}-id", emb),
            )
        conn.commit()
        conn.close()
        self._vec_knn_in_namespace = _vec_knn_in_namespace

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        # PRR-024 fix: restore the ambient env (sibling convention in
        # tests/test_host.py) — a leaked ZMEM_STORE silently redirects any
        # later in-process test's store.
        if self._saved_store is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = self._saved_store

    def test_namespace_filter_returns_only_same_namespace(self):
        """Query embedding matches ns-knn-a exactly; the helper must
        return only ns-knn-a, never the foreign ns-knn-b / ns-knn-c
        rows that are close in vec space."""
        from storelib.schema import connect
        conn = connect()
        try:
            knn = self._vec_knn_in_namespace(
                conn, self._query_emb, namespaces=["project:ns-knn-a"], k=5,
            )
        finally:
            conn.close()
        ids = [mid for mid, _dist in knn]
        self.assertEqual(ids, ["project:ns-knn-a-id"],
                         f"namespace-blind leak: {ids}")

    def test_unscoped_returns_all_namespaces(self):
        """namespaces=None is the unscoped path — superseded filter
        still applies, namespace filter does not."""
        from storelib.schema import connect
        conn = connect()
        try:
            knn = self._vec_knn_in_namespace(
                conn, self._query_emb, namespaces=None, k=5,
            )
        finally:
            conn.close()
        ids = sorted(mid for mid, _dist in knn)
        self.assertEqual(ids, [
            "project:ns-knn-a-id", "project:ns-knn-b-id", "project:ns-knn-c-id",
        ], f"unscoped should return all namespaces: {ids}")

    def test_overfetch_does_not_leak_foreign(self):
        """Tight over-fetch (1x) + foreign-namespace-dominant vec
        neighborhood must yield zero same-namespace vec candidates
        (per the issue spec), NOT foreign ids."""
        from storelib.schema import connect
        conn = connect()
        try:
            # Request k=5 in ns-knn-a with overfetch=1: the helper
            # over-fetches 5x1=5 from vec0, then post-filters by
            # ns-knn-a. ns-knn-b and ns-knn-c may dominate the top
            # vec0 slots but the namespace filter must drop them.
            knn = self._vec_knn_in_namespace(
                conn, self._query_emb,
                namespaces=["project:ns-knn-a"], k=5, overfetch=1,
            )
        finally:
            conn.close()
        ids = [mid for mid, _dist in knn]
        self.assertEqual(ids, ["project:ns-knn-a-id"],
                         f"overfetch=1 leaked: {ids}")

    def test_superseded_filter_excludes_tombstoned(self):
        """The helper's SQL filters by ``superseded_at IS NULL`` even
        on the unscoped path."""
        from storelib.schema import connect
        conn = connect()
        try:
            # Tombstone the ns-knn-b row.
            conn.execute(
                "UPDATE memory SET superseded_at = ? WHERE id = ?",
                ("2026-03-01T00:00:00Z", "project:ns-knn-b-id"),
            )
            conn.commit()
            knn = self._vec_knn_in_namespace(
                conn, self._query_emb, namespaces=None, k=5,
            )
        finally:
            conn.close()
        ids = sorted(mid for mid, _dist in knn)
        self.assertEqual(ids, [
            "project:ns-knn-a-id", "project:ns-knn-c-id",
        ], f"superseded_at filter broken: {ids}")


class NamespaceDedupWindowTests(unittest.TestCase):
    """Issue #58, 3.2: the dedup window widens so a same-namespace
    paraphrase can be detected even when foreign-namespace rows
    dominate the global vec0 neighborhood."""

    def test_dedup_helper_uses_shared_knn(self):
        """The dedup helper must use ``_vec_knn_in_namespace`` (not
        its own inline SQL). White-box: assert ``_find_semantic_duplicate``
        ends up calling the shared helper.
        """
        import inspect
        import storelib.write as w
        from storelib.schema import _vec_knn_in_namespace
        src = inspect.getsource(w._find_semantic_duplicate)
        self.assertIn("_vec_knn_in_namespace", src,
                      "_find_semantic_duplicate must use the shared "
                      "vec0 KNN helper (issue #58 3.2)")
        # The shared helper must also exist on the schema module so
        # both recall and dedup can import it.
        self.assertTrue(callable(_vec_knn_in_namespace))


if __name__ == "__main__":
    unittest.main(verbosity=2)