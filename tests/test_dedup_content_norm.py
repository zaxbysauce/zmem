"""Tests for store.py's content_norm column + indexed exact-match dedup
(#39 E4).

Covers:
  - Fresh store: two adds with whitespace/case differences dedup (only 1 live row)
  - Legacy-store migration: a pre-v8 store (schema_version=7, no content_norm)
    is migrated to v8 with content_norm backfilled for all rows + the index
    created + schema_version bumped to 8
  - Backfill batch-safety: 600 rows at v7 all get content_norm after migrate
  - _absorb_into_keeper path: after consolidate merges content into a keeper,
    the keeper's content_norm matches its NEW content (not stale)
  - Superseded rows are excluded from dedup (a tombstoned memory is not matched)
  - _normalize_content equivalence: matches the former inline expression across
    whitespace, unicode, case, and embedded newlines

Drives the REAL store.py CLI via subprocess against a throwaway temp store.

Run: python tests/test_dedup_content_norm.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


def _norm(s: str) -> str:
    """The canonical normalization expression (mirrors _normalize_content)."""
    import re
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class ContentNormDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-norm-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def _connect(self):
        c = sqlite3.connect(self.store)
        c.row_factory = sqlite3.Row
        return c

    def test_fresh_store_dedups_whitespace_and_case_differences(self):
        """Two adds that differ only in whitespace + case dedup to one row."""
        self._run("init")
        r1 = self._run("add", "--namespace", "project:t", "--type", "fact",
                       "--content", "Always   check   the types", "--signal", "test")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self._run("add", "--namespace", "project:t", "--type", "fact",
                       "--content", "always check the TYPES", "--signal", "test")
        # Second add should report a dedup refresh, not a new add.
        self.assertIn("dedup", r2.stdout + r2.stderr)
        c = self._connect()
        try:
            n = c.execute("SELECT count(*) FROM memory WHERE superseded_at IS NULL").fetchone()[0]
            self.assertEqual(n, 1, "near-identical content should dedup to 1 live row")
            row = c.execute("SELECT content_norm FROM memory").fetchone()
            self.assertEqual(row["content_norm"], "always check the types")
        finally:
            c.close()

    def test_distinct_content_does_not_dedup(self):
        """Genuinely different content must NOT dedup."""
        self._run("init")
        self._run("add", "--namespace", "project:t", "--type", "fact",
                  "--content", "first lesson here", "--signal", "test")
        self._run("add", "--namespace", "project:t", "--type", "fact",
                  "--content", "second different lesson", "--signal", "test")
        c = self._connect()
        try:
            n = c.execute("SELECT count(*) FROM memory WHERE superseded_at IS NULL").fetchone()[0]
            self.assertEqual(n, 2, "distinct content must not dedup")
        finally:
            c.close()

    def _regress_to_v7(self, n_rows: int):
        """Create a REAL store at the current version (full schema via init),
        seed rows, then regress it to v7: NULL out content_norm and set
        schema_version=7. This simulates a pre-v8 store the migration must
        upgrade, WITHOUT a fragile hand-crafted schema (the real schema has
        23+ columns + FTS external-content triggers that a toy fixture
        breaks)."""
        self._run("init")
        for i in range(n_rows):
            self._run("add", "--namespace", "project:legacy", "--type", "fact",
                      "--content", f"Some content row {i}  with  extra  spaces",
                      "--signal", "test")
        c = self._connect()
        try:
            c.execute("UPDATE memory SET content_norm=NULL")
            c.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
            c.commit()
        finally:
            c.close()

    def test_legacy_v7_store_is_migrated_with_backfill(self):
        """A real store regressed to v7 (content_norm NULLed, schema_version=7)
        is migrated back to the current version: all rows backfilled with correct
        content_norm and schema_version bumped to the supported version (now 9)."""
        self._regress_to_v7(3)
        # Re-run any store.py command — migrate() runs as part of connect().
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)

        c = self._connect()
        try:
            ver = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(ver, "9", "schema_version must bump to 9 after migrate")
            # Every row backfilled with the correct normalized form.
            for row in c.execute("SELECT content, content_norm FROM memory"):
                self.assertIsNotNone(row["content_norm"],
                                     "content_norm must not be NULL after backfill")
                self.assertEqual(row["content_norm"], _norm(row["content"]),
                                 f"backfill mismatch for {row['content']!r}")
        finally:
            c.close()

    def test_backfill_completes_for_600_rows(self):
        """Backfill in batches of 500: 600 rows must ALL get content_norm
        (the batch loop must not stop early at the first batch boundary)."""
        self._regress_to_v7(600)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)

        c = self._connect()
        try:
            null_count = c.execute(
                "SELECT count(*) FROM memory WHERE content_norm IS NULL"
            ).fetchone()[0]
            self.assertEqual(null_count, 0,
                             "all 600 rows must be backfilled (batch loop completes)")
            total = c.execute("SELECT count(*) FROM memory").fetchone()[0]
            self.assertGreaterEqual(total, 600)
        finally:
            c.close()

    def test_superseded_rows_excluded_from_dedup(self):
        """A tombstoned (superseded) memory must NOT dedup a new add of the
        same content — the partial index and the dedup query both filter
        WHERE superseded_at IS NULL."""
        self._run("init")
        self._run("add", "--namespace", "project:t", "--type", "fact",
                  "--content", "a memory to supersede", "--signal", "test")
        c = self._connect()
        try:
            row = c.execute(
                "SELECT id FROM memory WHERE content='a memory to supersede'"
            ).fetchone()
            mem_id = row["id"]
        finally:
            c.close()
        # Supersede (tombstone) it.
        r = self._run("supersede", "--id", mem_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Re-add the same content — must NOT dedup against the tombstoned row.
        self._run("add", "--namespace", "project:t", "--type", "fact",
                  "--content", "a memory to supersede", "--signal", "test")
        c = self._connect()
        try:
            live = c.execute(
                "SELECT count(*) FROM memory WHERE superseded_at IS NULL "
                "AND content='a memory to supersede'"
            ).fetchone()[0]
            self.assertEqual(live, 1,
                             "re-added content after supersede must create a new live row, "
                             "not dedup against the tombstone")
        finally:
            c.close()

    def test_absorb_into_keeper_updates_content_norm(self):
        """After consolidate merges an absorbed row's content into the keeper
        (_absorb_into_keeper), the keeper's content_norm must match its NEW
        content — not be stale. This is the only content-mutating UPDATE path
        (store.py:2870); if it forgot content_norm, the indexed dedup would
        later miss a re-add of the grown content."""
        self._run("init")
        # Two near-duplicate rows in the same namespace (different enough that
        # they're separate adds, similar enough that consolidate merges them
        # via lexical overlap in degraded mode).
        self._run("add", "--namespace", "project:absorb", "--type", "fact",
                  "--content", "pytest xdist workers share a tmpdir race condition",
                  "--signal", "test", "--confidence", "0.9")
        self._run("add", "--namespace", "project:absorb", "--type", "fact",
                  "--content", "pytest xdist workers share a tmpdir race condition and lane ordering",
                  "--signal", "test", "--confidence", "0.85")
        # Force consolidate (bypass cadence gate) to merge them.
        r = self._run("consolidate", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self._connect()
        try:
            # Exactly one live row remains (the keeper); the other is absorbed.
            live = c.execute(
                "SELECT id, content, content_norm FROM memory "
                "WHERE superseded_at IS NULL AND namespace='project:absorb'"
            ).fetchall()
            self.assertEqual(len(live), 1,
                             f"expected 1 live keeper after consolidate, got {len(live)}")
            keeper = live[0]
            # The invariant: content_norm tracks the keeper's CURRENT content
            # (which now includes the absorbed append), not the original.
            self.assertEqual(
                keeper["content_norm"], _norm(keeper["content"]),
                f"keeper content_norm is stale — must match _normalize_content of "
                f"the grown content. content={keeper['content']!r}, "
                f"content_norm={keeper['content_norm']!r}"
            )
        finally:
            c.close()

    def test_normalize_content_equivalence(self):
        """_normalize_content matches the former inline expression across
        whitespace, unicode, case, and embedded newlines."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("zmem_store_norm", STORE_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cases = [
            "Hello World",
            "  Hello   World  ",
            "HELLO WORLD",
            "Hello\n\n  World\t",
            "café résumé naïve",
            "CAFÉ RÉSUMÉ NAÏVE",
            "  café   résumé  ",
            "",
            "single",
        ]
        for s in cases:
            self.assertEqual(mod._normalize_content(s), _norm(s),
                             f"normalize mismatch for {s!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
