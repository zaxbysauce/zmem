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
        self._saved_store = os.environ.get("ZMEM_STORE")
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
        # PRR-024 fix: restore the ambient env (sibling convention in
        # tests/test_host.py) — a leaked ZMEM_STORE silently redirects any
        # later in-process test's store.
        if self._saved_store is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = self._saved_store

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
        ``valid_from`` — PRR-010 fix: use a BOUNDARY timestamp exactly equal
        to row T2's valid_from ('2026-02-01T00:00:00Z'). A Z input includes
        T2 (equal); a RAW un-normalized '+00:00' input would exclude it
        (ASCII '+' < 'Z'), so identical results prove the entry-point
        normalization (recall.py _normalize_as_of) fires for programmatic
        callers, not just the CLI argparse type.
        """
        from storelib import connect, recent_memory
        results_z = recent_memory(
            connect(),
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            as_of="2026-02-01T00:00:00Z",
        )
        results_p = recent_memory(
            connect(),
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            as_of="2026-02-01T00:00:00+00:00",
        )
        ids_z = sorted(r["id"] for r in results_z)
        ids_p = sorted(r["id"] for r in results_p)
        self.assertEqual(
            ids_z, ids_p,
            "Z-suffix and +00:00 inputs at the SAME instant must produce "
            "identical results (entry-point normalization). Z=%s, +00:00=%s"
            % (ids_z, ids_p),
        )
        self.assertEqual(
            ids_z, ["asof-row-0", "asof-row-1"],
            f"boundary as_of == T2.valid_from must include T1 and T2; got {ids_z}",
        )

    def test_as_of_non_utc_offset_normalized(self):
        """PRR-022 fix: a non-UTC offset (+05:30) must be converted to the
        UTC instant, not compared as wall-clock text. 2026-02-01T05:30+05:30
        == 2026-02-01T00:00Z == T2's valid_from, so T1+T2 return."""
        from storelib import connect, recent_memory
        results = recent_memory(
            connect(),
            namespace="project:asof-test",
            limit=10,
            as_json=True,
            no_bump=True,
            as_of="2026-02-01T05:30:00+05:30",
        )
        ids = sorted(r["id"] for r in results)
        self.assertEqual(
            ids, ["asof-row-0", "asof-row-1"],
            f"non-UTC offset must resolve to the UTC instant; got {ids}",
        )

    def test_search_cli_as_of(self):
        """PRR-010 fix: --as-of must work through the real `search`
        subcommand (previously only recall/recent were tested)."""
        import subprocess
        env = {**os.environ, "ZMEM_STORE": str(self.store_path),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        r = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "store.py"), "search",
             "--text", "row", "--namespace", "project:asof-test",
             "--no-bump", "--as-of", "2026-02-15T00:00:00Z"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("row T1", r.stdout)
        self.assertIn("row T2", r.stdout)
        self.assertNotIn("row T3", r.stdout)

    def test_search_cli_as_of_exclusive_boundary(self):
        """Issue #59, 4.4: `search --as-of` is a SEPARATE dispatcher from
        recall/recent (cli.py search pins ``hybrid=False``) and must apply the
        exact same temporal contract: the valid_until END is EXCLUSIVE and the
        ``superseded_at IS NULL`` filter is dropped under as-of. Pins it through
        the real `search` subprocess using the FTS/keyword lane."""
        import sqlite3
        import subprocess
        conn = sqlite3.connect(self.store_path)
        try:
            conn.execute(
                "UPDATE memory SET superseded_at=?, valid_until=? WHERE id=?",
                ("2026-02-10T00:00:00Z", "2026-02-10T00:00:00Z", "asof-row-1"))
            conn.commit()
        finally:
            conn.close()

        env = {**os.environ, "ZMEM_STORE": str(self.store_path),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}

        def search_at(as_of):
            r = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "store.py"), "search",
                 "--text", "row", "--namespace", "project:asof-test",
                 "--no-bump", "--as-of", as_of],
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        # Inside the validity window: row T2 returns DESPITE superseded_at being
        # set (live filter dropped under as-of); T3 is not yet born.
        out_inside = search_at("2026-02-05T00:00:00Z")
        self.assertIn("row T2", out_inside,
                      "search --as-of inside the window must surface the "
                      "superseded row")
        self.assertNotIn("row T3", out_inside)
        # AT the exclusive end: row T2 no longer valid.
        out_at = search_at("2026-02-10T00:00:00Z")
        self.assertNotIn("row T2", out_at,
                         "search --as-of == valid_until must EXCLUDE the row "
                         "(exclusive end)")
        # AFTER the end.
        out_after = search_at("2026-02-11T00:00:00Z")
        self.assertNotIn("row T2", out_after,
                         "search --as-of after valid_until must exclude the row")

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

    def test_superseded_row_valid_inside_window_gone_at_or_after_end(self):
        """Issue #59, 4.4: complete --as-of against valid_until. as_of must
        drop the hard ``superseded_at IS NULL`` live filter (a historically
        superseded row CAN be valid at an instant inside its window), and
        must apply valid_until as the EXCLUSIVE end (a row is valid while
        ``valid_until > as_of``, never at equality).

        Fixture: row T1 (valid_from 2026-02-01) is tagged as superseded with
        valid_until == superseded_at == 2026-02-10. As a function of as_of:
          - inside the window (02-05): T1 IS returned — proves the live
            filter is dropped, not just ignored in a footnote;
          - AT valid_until (02-10): T1 is NOT returned — the bound is
            exclusive;
          - after (02-11): T1 gone.
        """
        from storelib import connect, recent_memory
        conn = connect()
        try:
            conn.execute(
                "UPDATE memory SET superseded_at=?, valid_until=? WHERE id=?",
                ("2026-02-10T00:00:00Z", "2026-02-10T00:00:00Z", "asof-row-1"))
            conn.commit()

            def ids_at(as_of):
                rows = recent_memory(
                    conn, namespace="project:asof-test", limit=10,
                    as_json=True, no_bump=True, as_of=as_of,
                )
                return sorted(r["id"] for r in rows)

            # Inside the validity window: T1 returns DESPITE superseded_at
            # being set (the filter is dropped under as_of).
            self.assertEqual(
                ids_at("2026-02-05T00:00:00Z"),
                ["asof-row-0", "asof-row-1"],
                "as_of inside T1's window must return T1 (superseded filter "
                "dropped under as_of); T2 is not yet born",
            )
            # At the EXCLUSIVE end: T1 is no longer valid.
            self.assertEqual(
                ids_at("2026-02-10T00:00:00Z"),
                ["asof-row-0"],
                "as_of == valid_until must NOT return the row (exclusive end)",
            )
            # After the end.
            self.assertEqual(
                ids_at("2026-02-11T00:00:00Z"),
                ["asof-row-0"],
                "as_of after valid_until must NOT return the row",
            )
        finally:
            conn.close()


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