"""Tests for `search --exact` — literal substring existence check (issue #263).

`store.py search` is FTS5 MATCH-backed: the query is normalized into a
stop-word-filtered, OR-composed term list (`_normalize_query_terms` /
`_fts_expression`), so (a) a literal made only of stop-words returns zero rows
even when the string is present verbatim, (b) FTS5 query-syntax characters in
the literal change the query's meaning, and (c) a token bag whose tokens all
occur — but not contiguously — matches. None of that is wrong for ranked
relevance search; it is the wrong tool for an existence probe ("is this exact
id/path/error-string/redaction-marker present verbatim?"). Issue #263 adds a
`--exact` flag that checks `--text` as a literal substring of `memory.content`
instead, honoring the same `--namespace` / `--as-of` / `--exclude` filters and
the same `--no-bump` (issue #21) semantics.

Plain `search` without `--exact` must stay byte-identical — the constraint is
pinned here by test_plain_search_stopword_literal_stays_empty (the stop-word
gap itself is the frozen pre-fix behavior) and by the sibling checks in
tests/test_global_union.py + tests/test_store_characterization.py.

Drives the REAL store.py CLI via subprocess against a throwaway temp store —
never the box store — following the isolation fixture pattern from
tests/test_global_union.py / tests/test_no_bump.py (ZMEM_STORE set inline on
every subprocess env dict; ZMEM_DATA and friends popped so no ambient env var
can redirect a run at the real ~/.zmem store; embedding model forced absent so
the suite runs in model-absent degraded mode, which is the only mode a literal
substring check ever needs).

Run: python tests/test_s02_search_exact.py
"""

from __future__ import annotations

import json
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
NS = "project:s02exact"
OTHER_NS = "project:s02other"

STOP_CONTENT = "the flange is to be or not to be calibrated"
SYNTAX_CONTENT = 'run pytest -k "not slow" OR skip before merging'
OTHER_CONTENT = "the flange is to be or not to be calibrated elsewhere"


def _base_env(tmp: str) -> dict:
    """Env for a store.py subprocess pinned to a throwaway store, with the
    embedding model forced absent (fast + deterministic; repo convention from
    tests/test_model_fallback.py, tests/test_export_pack.py)."""
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_AUTO_REKEY"] = "0"
    return env


class _ExactBase(unittest.TestCase):
    """Common temp-store fixture: a fresh store dir per test, seeded with the
    issue #263 AC fixture rows (`stop` + `syntax` in project:s02exact, `other`
    holding the same literal in project:s02other)."""

    def setUp(self):
        # Windows note (issue #263 Verification): mkdtemp + explicit cleanup,
        # NOT a context-managed TemporaryDirectory, so a still-open sqlite
        # handle during teardown cannot raise PermissionError.
        self.tmp = tempfile.mkdtemp(prefix="zmem-s02exact-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)
        self.stop_id = self._add(NS, STOP_CONTENT)
        self.syntax_id = self._add(NS, SYNTAX_CONTENT)
        self.other_id = self._add(OTHER_NS, OTHER_CONTENT)

    def run_store(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env or self.env, capture_output=True, text=True, timeout=60,
        )

    def _add(self, namespace: str, content: str) -> str:
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
        self.assertIsNotNone(row, f"seed row not found for {namespace!r}")
        return row[0]

    def _search(self, *args) -> list[str]:
        """Run `search --json ...`, assert success (stderr as the message),
        and return the result ids in returned order."""
        r = self.run_store("search", "--json", "--no-bump", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        rows = payload["results"] if isinstance(payload, dict) else payload
        return [row["id"] for row in rows]


class SearchExactTest(_ExactBase):
    """AC1-AC5 (issue #263): the `--exact` literal-substring checks."""

    def test_stopword_literal_found_by_exact(self):
        # AC1: a literal made only of FTS stop-words is found by --exact.
        ids = self._search("--exact", "--text", "to be or not",
                           "--namespace", NS)
        self.assertEqual(ids, [self.stop_id])

    def test_fts_syntax_literal_found_by_exact(self):
        # AC2: FTS5 query-syntax characters (leading dash, quotes, OR) are
        # matched as plain text. The whole literal — spaces and all — is one
        # argv element, so no shell is involved.
        ids = self._search("--exact", "--text", '-k "not slow" OR',
                           "--namespace", NS)
        self.assertEqual(ids, [self.syntax_id])

    def test_exact_is_not_token_match(self):
        # AC3: tokens all present but NOT as that contiguous substring must
        # not match. Plain FTS DOES return the row for the same text (token
        # bag, any order) — that contrast is the discriminator, so both sides
        # are pinned: plain non-empty, exact empty.
        plain_ids = self._search("--text", "calibrated flange",
                                 "--namespace", NS)
        self.assertIn(self.stop_id, plain_ids)
        exact_ids = self._search("--exact", "--text", "calibrated flange",
                                 "--namespace", NS)
        self.assertEqual(exact_ids, [])

    def test_exact_honors_namespace(self):
        # AC4: --exact with --namespace returns only rows from that
        # namespace; the identical literal in project:s02other must not leak.
        ids = self._search("--exact", "--text", "to be or not",
                           "--namespace", NS)
        self.assertEqual(ids.count(self.stop_id), 1)
        self.assertNotIn(self.other_id, ids)

    def test_exact_honors_exclude(self):
        # AC5: --exact with --exclude drops that id from the results.
        ids = self._search("--exact", "--text", "to be or not",
                           "--namespace", NS, "--exclude", self.stop_id)
        self.assertEqual(ids, [])


class PlainSearchPinnedTest(_ExactBase):
    """Companion pins (not frozen AC rows): the plain FTS path's observable
    behavior on the same fixtures, guarding the byte-identical constraint of
    issue #263 from the other direction."""

    def test_plain_search_stopword_literal_stays_empty(self):
        # The motivating gap, pinned as the PLAIN path's contract: a
        # stop-word-only literal returns zero rows from plain search even
        # though the string is present verbatim (probed GREEN at MAIN in the
        # issue's extraction). --exact exists to answer this probe; plain
        # search must keep refusing to.
        ids = self._search("--text", "to be or not", "--namespace", NS)
        self.assertEqual(ids, [])

    def test_plain_search_syntax_literal_needs_luck(self):
        # Plain search on the FTS-syntax literal happens to return the right
        # row here only through boolean-OR luck (the issue's P2 probe): the
        # normalized terms (not/slow/or/skip...) match via OR composition,
        # not literal presence. Pinning it documents why AC2 is NEW-SURFACE
        # on the flag alone: plain search neither errors nor does literal
        # matching.
        ids = self._search("--text", '-k "not slow" OR', "--namespace", NS)
        self.assertIn(self.syntax_id, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
