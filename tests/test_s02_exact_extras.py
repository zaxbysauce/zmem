"""Coverage pins for documented `search --exact` behaviors (issue #263
feedback round, PRR-004a-g from the PR #274 review).

Each test pins one behavior the docs/SKILL.md/cli help promise that the frozen
acceptance file and the first companion file do not exercise:

  (a) empty --text trivially matches every row in scope (SKILL.md;
      Python `"" in content` semantics, SQLite instr(content, '') == 1).
  (b) matching is case-SENSITIVE (cli help; recall.py docstring).
  (c) non-ASCII literals are codepoint-faithful (recall.py docstring).
  (d) unscoped mode (no --namespace) searches the whole store.
  (e) --limit truncates exact results.
  (f) the --json envelope carries the full shared key set on the exact path.
  (g) the as-of valid_until boundary is EXCLUSIVE (valid_until == as_of
      excludes; 1 second before includes) on the exact path.

tests/test_s02_search_exact.py is the frozen issue-tracer acceptance file
(hash-pinned at the red checkpoint) and must stay byte-identical; everything
here lives in this NEW unfrozen file.

Drives the REAL store.py CLI via subprocess against throwaway temp stores —
the same isolation fixture as tests/test_global_union.py / the s02 companion
files (ZMEM_STORE pinned per subprocess; ZMEM_DATA popped; model absent).

Run: python tests/test_s02_exact_extras.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:s02extras"
OTHER_NS = "project:s02extras-other"


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_AUTO_REKEY"] = "0"
    return env


class _ExtrasBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-s02extras-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def run_store(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def add_row(self, namespace: str, content: str) -> str:
        r = self.run_store("add", "--namespace", namespace, "--type", "lesson",
                           "--content", content, "--signal", "test",
                           "--confidence", "0.8")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content=? AND namespace=? "
                "ORDER BY ingestion_ts DESC, id LIMIT 1",
                (content, namespace),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        return row[0]

    def exact(self, *args) -> dict:
        """Run `search --exact --json ...`; return the parsed envelope."""
        r = self.run_store("search", "--exact", "--json", "--no-bump", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def exact_ids(self, *args) -> list[str]:
        payload = self.exact(*args)
        return [row["id"] for row in payload["results"]]


class TestEmptyLiteral(_ExtrasBase):
    """(a) empty --text matches every row in scope, and ONLY that scope."""

    def test_empty_text_matches_every_row_in_scope(self):
        one = self.add_row(NS, "alpha bravo charlie")
        two = self.add_row(NS, "delta echo foxtrot")
        foreign = self.add_row(OTHER_NS, "golf hotel india")
        ids = self.exact_ids("--text", "", "--namespace", NS)
        self.assertEqual(sorted(ids), sorted([one, two]))
        self.assertNotIn(foreign, ids)
        # Unscoped empty --text: every row in the store.
        ids_all = self.exact_ids("--text", "")
        self.assertEqual(len(ids_all), 3)


class TestCaseSensitivity(_ExtrasBase):
    """(b) --exact is case-sensitive (documented binary substring)."""

    def test_case_mismatch_does_not_match(self):
        row = self.add_row(NS, "hello world marker")
        hit = self.exact_ids("--text", "hello world", "--namespace", NS)
        self.assertEqual(hit, [row])
        for variant in ("HELLO world", "Hello World", "hello WORLD"):
            self.assertEqual(
                self.exact_ids("--text", variant, "--namespace", NS),
                [], variant)


class TestNonAscii(_ExtrasBase):
    """(c) non-ASCII literals match codepoint-faithfully."""

    def test_unicode_literal_matches(self):
        row = self.add_row(NS, "café résumé — naïve ünïcode")
        self.assertEqual(
            self.exact_ids("--text", "résumé", "--namespace", NS), [row])
        self.assertEqual(
            self.exact_ids("--text", "café résumé", "--namespace", NS), [row])
        # Codepoint-faithful: a decomposed lookalike must NOT match the
        # precomposed stored form (e + combining acute != é).
        decomposed = "café résumé"
        self.assertEqual(
            self.exact_ids("--text", decomposed, "--namespace", NS), [])


class TestUnscoped(_ExtrasBase):
    """(d) exact without --namespace searches the whole store."""

    def test_unscoped_exact_spans_namespaces(self):
        one = self.add_row(NS, "shared token alfa")
        two = self.add_row(OTHER_NS, "shared token bravo")
        ids = self.exact_ids("--text", "shared token")
        self.assertEqual(sorted(ids), sorted([one, two]))
        self.assertEqual(
            self.exact_ids("--text", "token bravo"), [two])


class TestLimit(_ExtrasBase):
    """(e) --limit truncates exact results deterministically."""

    def test_limit_truncates(self):
        for n in range(1, 6):
            self.add_row(NS, f"limit probe row number {n}")
        payload = self.exact("--text", "limit probe row", "--namespace", NS,
                             "--limit", "3")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["results"]), 3)
        payload_default = self.exact("--text", "limit probe row",
                                     "--namespace", NS)
        self.assertEqual(payload_default["count"], 5)


class TestEnvelopeShape(_ExtrasBase):
    """(f) the exact path emits the full shared read-envelope key set."""

    def test_envelope_keys_exact_path(self):
        self.add_row(NS, "envelope probe kilo")
        payload = self.exact("--text", "envelope probe kilo",
                             "--namespace", NS)
        for key in ("results", "count", "omitted", "injection_risk",
                    "tokens_used", "tokens_budget"):
            self.assertIn(key, payload)
        self.assertEqual(payload["count"], 1)
        # Row shape: the shared per-row contract (evidence ids attached on
        # the plain lane; score constant; lanes absent).
        row = payload["results"][0]
        self.assertEqual(row["_score"], 1.0)
        self.assertIsNone(row["_rel_lex"])
        self.assertFalse(row["_graph_arrival_only"])
        # Conditional key only when --exclude was supplied (issue #117 D-1).
        self.assertNotIn("excluded", payload)
        excluded_payload = self.exact("--text", "envelope probe kilo",
                                      "--namespace", NS,
                                      "--exclude", row["id"])
        self.assertIn("excluded", excluded_payload)
        self.assertEqual(excluded_payload["excluded"], 1)
        self.assertEqual(excluded_payload["results"], [])


class TestAsOfExclusiveBoundary(_ExtrasBase):
    """(g) valid_until is EXCLUSIVE under --as-of on the exact path."""

    def test_valid_until_boundary_is_exclusive(self):
        row = self.add_row(NS, "boundary probe lima")
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE memory SET valid_from='2026-01-01T00:00:00Z', "
                "valid_until='2026-01-15T00:00:00Z' WHERE id=?", (row,))
            conn.commit()
        finally:
            conn.close()
        # At the boundary instant: excluded (valid_until EXCLUSIVE).
        self.assertEqual(
            self.exact_ids("--text", "boundary probe lima",
                           "--namespace", NS, "--as-of",
                           "2026-01-15T00:00:00Z"),
            [])
        # One second before: included.
        self.assertEqual(
            self.exact_ids("--text", "boundary probe lima",
                           "--namespace", NS, "--as-of",
                           "2026-01-14T23:59:59Z"),
            [row])
        # Before valid_from: excluded (valid_from INCLUSIVE lower bound).
        self.assertEqual(
            self.exact_ids("--text", "boundary probe lima",
                           "--namespace", NS, "--as-of",
                           "2025-12-31T23:59:59Z"),
            [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
