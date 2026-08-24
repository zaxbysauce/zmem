"""Issue #58, 3.3: default hybrid on when embeddings exist — BEHAVIORAL.

PRR-009/029 fix: the first draft of this file asserted on
inspect.getsource() substrings (source-grep theater) and never invoked
recall_memory. This rewrite drives the real code paths:

  1. ``hybrid=None`` auto-picks lexical when embeddings are unavailable
     (results identical to ``hybrid=False``) — runs model-absent.
  2. ``hybrid=None`` resolves to hybrid=True when embeddings are
     available, and a failed embed (embed_text -> None) fails open to
     lexical results — runs model-absent.
  3. Model-present fusion (sqlite_vec-gated): a stub embedder + a real
     vec row surface a vec-only candidate (no FTS match) through the
     None sentinel — the auto-fuse gate the issue requires.
  4. CLI flag precedence --no-hybrid > --hybrid (sqlite_vec-gated,
     in-process main() with the stub embedder).
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


class _StubEmbeddings:
    """Minimal stand-in for the embeddings module on recall's module global."""

    def __init__(self, available: bool, vector: bytes | None = None):
        self._available = available
        self._vector = vector

    def is_available(self) -> bool:
        return self._available

    def embed_text(self, text: str):
        return self._vector


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-hybdef-")
        self.store_path = Path(self.tmp) / "store.sqlite"
        self._saved_store = os.environ.get("ZMEM_STORE")
        os.environ["ZMEM_STORE"] = str(self.store_path)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        for mod in list(sys.modules.keys()):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]
        import storelib.recall as recall_mod
        import storelib.schema as schema_mod
        from storelib.schema import ALLOWED_TYPES
        self.recall_mod = recall_mod
        self._connect = schema_mod.connect
        conn = schema_mod.connect()
        schema_mod.init_db(conn)
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "ingestion_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("lex-row", "project:hyb", ALLOWED_TYPES[0],
             "lexical anchor tokens only", "", "", "", 0.9, "test",
             "2026-02-03T04:05:06Z", "2026-02-03T04:05:06Z"),
        )
        conn.commit()
        self.conn = conn
        self._orig_embeddings = recall_mod._embeddings

    def tearDown(self):
        self.recall_mod._embeddings = self._orig_embeddings
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._saved_store is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = self._saved_store


class HybridAutoPickBehavioral(_Base):

    def test_sentinel_default_is_none(self):
        import inspect
        sig = inspect.signature(self.recall_mod.recall_memory)
        self.assertIsNone(
            sig.parameters["hybrid"].default,
            "recall_memory's hybrid default must be None (auto-pick sentinel)",
        )

    def test_auto_picks_lexical_when_embeddings_unavailable(self):
        """hybrid=None with embeddings unavailable → results identical to
        forcing hybrid=False (behavioral, model-absent)."""
        self.recall_mod._embeddings = _StubEmbeddings(available=False)
        auto = self.recall_mod.recall_memory(
            self._connect(), query="lexical anchor",
            namespace="project:hyb", limit=5, as_json=True,
            no_bump=True, hybrid=None,
        )
        forced = self.recall_mod.recall_memory(
            self._connect(), query="lexical anchor",
            namespace="project:hyb", limit=5, as_json=True,
            no_bump=True, hybrid=False,
        )
        self.assertEqual([r["id"] for r in auto], ["lex-row"])
        self.assertEqual(
            [r["id"] for r in auto], [r["id"] for r in forced],
            "auto-pick must degrade to exactly the lexical result set",
        )

    def test_auto_resolves_hybrid_and_fails_open_on_failed_embed(self):
        """hybrid=None with embeddings 'available' resolves the sentinel to
        True; when embed_text then returns None (model load failure), the
        vec lane is skipped and lexical results still come back."""
        self.recall_mod._embeddings = _StubEmbeddings(
            available=True, vector=None,
        )
        results = self.recall_mod.recall_memory(
            self._connect(), query="lexical anchor",
            namespace="project:hyb", limit=5, as_json=True,
            no_bump=True, hybrid=None,
        )
        self.assertEqual(
            [r["id"] for r in results], ["lex-row"],
            "available-but-failed embed must fail open to lexical results",
        )


@unittest.skipUnless(
    _sqlite_vec_available(),
    "sqlite-vec unavailable — model-present fusion tests skipped "
    "(CI runs model-absent/degraded)",
)
class ModelPresentAutoFuse(_Base):
    """PRR-029: the issue gate 'hybrid auto-fuse must fire when embeddings
    are present' — proven behaviorally with a stub embedder + a real vec0
    row that FTS cannot match."""

    VEC = struct.pack("<384f", *([0.25] * 384))

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "ingestion_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("vec-row", "project:hyb", "fact",
             "semantically proximate wumpus tiddlywinks zqxx",
             "", "", "", 0.9, "test",
             "2026-02-03T04:05:06Z", "2026-02-03T04:05:06Z"),
        )
        self.conn.execute(
            "INSERT INTO memory_vec (memory_id, embedding) VALUES (?, ?)",
            ("vec-row", self.VEC),
        )
        self.conn.commit()

    def test_auto_sentinel_fuses_vec_only_candidate(self):
        """Query has NO lexical overlap with vec-row; only the vector lane
        can surface it. hybrid=None (with the stub embedder returning the
        row's own vector) must return it — the auto-fuse contract."""
        self.recall_mod._embeddings = _StubEmbeddings(
            available=True, vector=self.VEC,
        )
        results = self.recall_mod.recall_memory(
            self._connect(), query="alpha beta gamma delta epsilon",
            namespace="project:hyb", limit=5, as_json=True,
            no_bump=True, hybrid=None,
        )
        ids = {r["id"] for r in results}
        self.assertIn(
            "vec-row", ids,
            f"hybrid auto-fuse must surface the vec-only candidate; got {ids}",
        )

    def test_cli_no_hybrid_overrides_hybrid_flag(self):
        """PRR-013 behavioral: passing BOTH --hybrid and --no-hybrid must
        force lexical (vec-only candidate absent) — driven through the real
        CLI dispatch (in-process main()) with the stub embedder."""
        from storelib.cli import main as cli_main
        self.recall_mod._embeddings = _StubEmbeddings(
            available=True, vector=self.VEC,
        )
        saved_argv = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = [
                "store.py", "recall",
                "--query", "alpha beta gamma delta epsilon",
                "--namespace", "project:hyb",
                "--no-bump", "--json",
                "--hybrid", "--no-hybrid",
            ]
            with redirect_stdout(buf):
                cli_main()
        finally:
            sys.argv = saved_argv
        results = json.loads(buf.getvalue())
        ids = {r["id"] for r in results}
        self.assertNotIn(
            "vec-row", ids,
            f"--no-hybrid must win over --hybrid (lexical-only); got {ids}",
        )
        # Control: without --no-hybrid the SAME invocation fuses the row.
        buf2 = io.StringIO()
        try:
            sys.argv = [
                "store.py", "recall",
                "--query", "alpha beta gamma delta epsilon",
                "--namespace", "project:hyb",
                "--no-bump", "--json",
                "--hybrid",
            ]
            with redirect_stdout(buf2):
                cli_main()
        finally:
            sys.argv = saved_argv
        results2 = json.loads(buf2.getvalue())
        self.assertIn(
            "vec-row", {r["id"] for r in results2},
            "control: --hybrid alone must fuse the vec-only candidate",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)