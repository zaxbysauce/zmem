"""Issue #58, 3.6: ``--as-of ISO-8601`` temporal predicate.

Three rows with staggered ``valid_from``; recall at T2 returns only the
first two. Absent flag → all live rows (current behavior).

Also tests the Z-suffix normalization: ``+00:00`` input must compare
correctly against ``valid_from`` stored with a Z-suffix (I6 critic-fix).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))


class AsOfBehaviorTests(unittest.TestCase):

    def setUp(self):
        # Per-test tmp dir so state cannot leak across cases (UNIQUE
        # constraint violations on the deterministic ids).
        self.tmp = tempfile.mkdtemp(prefix="zmem-asof-")
        self.store_path = Path(self.tmp) / "store.sqlite"
        os.environ["ZMEM_STORE"] = str(self.store_path)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        for mod in list(sys.modules.keys()):
            if mod == "store" or mod.startswith("storelib"):
                del sys.modules[mod]
        from storelib.schema import init_db, connect, ALLOWED_TYPES
        conn = connect()
        init_db(conn)
        # Three rows with staggered valid_from. T1 < T2 < T3.
        for idx, (ts, content) in enumerate([
            ("2026-01-01T00:00:00Z", "row T1"),
            ("2026-02-01T00:00:00Z", "row T2"),
            ("2026-03-01T00:00:00Z", "row T3"),
        ]):
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "ingestion_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"asof-row-{idx}", "project:asof-test", ALLOWED_TYPES[0],
                 content, "", "", "", 0.9, "test", ts, ts),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_as_of_returns_rows_at_or_before(self):
        """Query at T2 returns the first two rows only."""
        from storelib import recall_memory, connect
        results = recall_memory(
            connect(),
            query="row",
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            hybrid=False,
            as_of="2026-02-15T00:00:00Z",
        )
        contents = sorted(r["content"] for r in results)
        self.assertEqual(
            contents, ["row T1", "row T2"],
            f"as_of=T2 must return T1 and T2 only, got {contents}",
        )

    def test_as_of_z_suffix_normalization(self):
        """``+00:00`` input must compare correctly against Z-suffixed
        ``valid_from`` (I6 critic-fix). The _iso8601 helper normalizes
        ``+00:00`` to ``Z`` so the lex-comparison works.
        """
        from storelib import _expand_namespace_aliases, connect, recent_memory
        # If this test ran with T2 as_of, recent with +00:00 must match
        # exactly what Z-suffix does.
        results_z = recent_memory(
            connect(),
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            as_of="2026-02-15T00:00:00Z",
        )
        results_p = recent_memory(
            connect(),
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            as_of="2026-02-15T00:00:00+00:00",
        )
        ids_z = sorted(r["id"] for r in results_z)
        ids_p = sorted(r["id"] for r in results_p)
        self.assertEqual(
            ids_z, ids_p,
            "Z-suffix and +00:00 inputs must produce identical results "
            "(I6 critic-fix). Otherwise the lex-comparison silently "
            "fails for ISO-8601 inputs without the Z-suffix.",
        )

    def test_absent_as_of_returns_all_live(self):
        """Absent flag → no temporal predicate → all live rows."""
        from storelib import recall_memory, connect
        results = recall_memory(
            connect(),
            query="row",
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            hybrid=False,
        )
        self.assertEqual(
            len(results), 3,
            f"absent --as-of must return all 3 live rows, got {len(results)}",
        )

    def test_as_of_before_any_valid_from_returns_nothing(self):
        """as_of before every valid_from must return zero rows (the
        rows are 'not yet born' at that instant).
        Note: ``as_of=2099`` is the OPPOSITE — it returns all rows
        because every row's valid_from is <= 2099.
        """
        from storelib import recall_memory, connect
        results = recall_memory(
            connect(),
            query="row",
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            hybrid=False,
            as_of="2025-12-31T00:00:00Z",
        )
        self.assertEqual(
            len(results), 0,
            f"as_of=2025-12-31 (before all valid_from) must return 0 "
            f"rows, got {len(results)}",
        )


class Iso8601ArgparseTests(unittest.TestCase):

    def test_iso8601_normalizes_plus_to_z(self):
        """``_iso8601`` (the argparse type=) must convert ``+00:00`` to
        ``Z`` so lex-comparison against ``now_iso()`` output works."""
        from storelib.cli import _iso8601
        self.assertEqual(_iso8601("2026-02-15T00:00:00+00:00"),
                         "2026-02-15T00:00:00Z")
        self.assertEqual(_iso8601("2026-02-15T00:00:00Z"),
                         "2026-02-15T00:00:00Z")

    def test_iso8601_rejects_garbage(self):
        from storelib.cli import _iso8601
        import argparse
        for bad in ["", "not-a-date", "2026-13-01T00:00:00Z"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                _iso8601(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)