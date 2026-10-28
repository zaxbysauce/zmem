"""Evidence-first association lookup index migration coverage."""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

from storelib import schema  # noqa: E402


class EvidenceReverseIndexTest(unittest.TestCase):
    def test_existing_v14_store_gets_index_without_version_bump(self):
        with tempfile.TemporaryDirectory(prefix="zmem-evidence-index-") as td:
            conn = sqlite3.connect(Path(td) / "store.sqlite")
            try:
                schema.init_db(conn)
                schema.migrate(conn)
                version_before = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                import schema_meta
                self.assertEqual(
                    version_before, str(schema_meta.SUPPORTED_SCHEMA_VERSION))

                conn.execute("DROP INDEX memory_evidence_evidence_memory_idx")
                conn.commit()
                schema.migrate(conn)

                version_after = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(version_after, version_before)
                indexes = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("memory_evidence_evidence_memory_idx", indexes)

                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT m.id, m.namespace "
                    "FROM memory m JOIN memory_evidence me ON me.memory_id=m.id "
                    "WHERE me.evidence_id=? ORDER BY m.namespace, m.id",
                    ("evidence-test",),
                ).fetchall()
                self.assertTrue(
                    any("memory_evidence_evidence_memory_idx" in str(row) for row in plan),
                    plan,
                )

                schema.migrate(conn)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                        "AND name='memory_evidence_evidence_memory_idx'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
