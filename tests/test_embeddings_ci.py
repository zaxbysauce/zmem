"""Issue #77: the dependency-enforcing sqlite-vec / fake-profile smoke test.

This file has NO skip guard BY CONTRACT — when `sqlite-vec` (or the fake
embedder's deps) is missing, importing/running it must FAIL LOUDLY. The
model-absent CI job excludes this file from its `tests/test_*.py` loop; the
`test-embeddings` CI job installs `hermes-plugin/server/requirements-embeddings.txt`
and runs it on both matrix legs, so a broken or undeclared vector dependency
can never leave CI green again.

Run: python tests/test_embeddings_ci.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import sqlite_vec  # noqa: E402  (contract: import failure = loud failure)

from embed_profiles import fake_embed  # noqa: E402


class EmbeddingCiTest(unittest.TestCase):
    """Loads the sqlite-vec extension into an in-memory connection and proves
    a vec0 nearest-neighbor query works against the deterministic fake
    embedder — the exact surface the declared embedding requirements enable."""

    def test_sqlite_vec_extension_and_fake_profile(self):
        fixture = json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "embeddings-ci" / "fake-vector.json")
            .read_text(encoding="utf-8"))
        expected = json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "embeddings-ci" / "fake-vector.expected.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(fixture["text"], "ci vector")
        self.assertEqual(expected["nearest_id"],
                         fixture["expected_nearest_id"])

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        dim = int(expected["dimension"])
        self.assertEqual(dim, 16)
        blob = fake_embed(fixture["text"])
        self.assertIsInstance(blob, bytes)
        # float32 little-endian blob: FAKE_DIM floats = 4 * dim bytes.
        self.assertEqual(len(blob), dim * 4,
                         "fake_embed must produce the fixture dimension")
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_items USING vec0("
            f"embedding float[{dim}])")
        conn.execute(
            "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
            (771, blob))
        conn.commit()
        row = conn.execute(
            "SELECT rowid, distance FROM vec_items WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT 1",
            (blob,)).fetchone()
        self.assertIsNotNone(row, "nearest-neighbor query must return a row")
        self.assertEqual(row[0], 771)
        self.assertIsInstance(row[1], float)


if __name__ == "__main__":
    unittest.main(verbosity=2)
