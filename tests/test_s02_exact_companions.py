"""Companion tests for `search --exact` (issue #263) — the plan-critic round's
coverage gaps, OUTSIDE the frozen acceptance file.

tests/test_s02_search_exact.py is the frozen issue-tracer check file
(hash-pinned at the red checkpoint) and must stay byte-identical; everything
the plan critic required beyond the issue's own ACs lives here:

  (a) --exact + --include-global: the user:global tier is literal-fetched and
      merged with the project hard floor intact (project row first).
  (b) --exact + --as-of: the [valid_from, valid_until) temporal predicate AND
      the live-filter drop — a superseded row valid at the instant surfaces
      under --as-of only, and a live row whose valid_until predates the
      instant is excluded by the temporal clause under --as-of only.
  (c) --exact telemetry (issue #21 law): default bumps retrieval_count;
      --no-bump leaves retrieval_count unchanged and bumps surfaced_count.
  (d) library-seam guards: recall_memory(exact=True) refuses the combinations
      that would splice non-literal rows (for_injection, scopes,
      include_cross_project, link_hops>=1, cross_rerank); with link_hops=0 on
      a link-seeded store it returns EXACTLY the literal rows (plain recall
      on the same store shows the neighbor, proving the splice would fire).
  (e) belief-head isolation: with a belief head whose content shares the
      query's tokens (so the plain path admits the virtual row), --exact
      returns only the literal-matching memory rows.

Drives the REAL store.py CLI via subprocess against throwaway temp stores
(same isolation fixture as tests/test_global_union.py). Case (d) additionally
imports storelib in-process with the env pinned and sys.modules purged first
(the repo convention — storelib freezes the store path at import).

Run: python tests/test_s02_exact_companions.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:s02companions"


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


class _CompanionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-s02comp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def run_store(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env or self.env, capture_output=True, text=True, timeout=60,
        )

    def add_row(self, namespace: str, content: str) -> str:
        r = self.run_store("add", "--namespace", namespace, "--type", "lesson",
                           "--content", content, "--signal", "test",
                           "--confidence", "0.8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return self._row_id(namespace, content)

    def _row_id(self, namespace: str, content: str) -> str:
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content=? AND namespace=? "
                "ORDER BY ingestion_ts DESC, id LIMIT 1",
                (content, namespace),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, f"row not found for {namespace!r}")
        return row[0]

    def search_ids(self, *args) -> tuple[list[str], list[dict]]:
        r = self.run_store("search", "--json", "--no-bump", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        rows = payload["results"] if isinstance(payload, dict) else payload
        return [row["id"] for row in rows], rows


class TestExactIncludeGlobal(_CompanionBase):
    """(a) --exact honors --include-global with the project hard floor."""

    def test_exact_include_global_merges_global_row_after_project(self):
        proj = self.add_row(NS, "alpha marker lives in the project lane")
        glob = self.add_row("user:global", "alpha marker lives in the global lane")
        ids, _ = self.search_ids(
            "--exact", "--text", "alpha marker lives", "--namespace", NS,
            "--include-global")
        self.assertEqual(set(ids), {proj, glob})
        # Hard floor: the project row is never crowded out — it comes first.
        self.assertEqual(ids[0], proj)


class TestExactAsOf(_CompanionBase):
    """(b) --exact honors --as-of: temporal predicate + live-filter drop."""

    def test_exact_as_of_predicate_and_live_filter_drop(self):
        live = self.add_row(NS, "temporal probe zulu in flight")
        expired = self.add_row(NS, "temporal probe zulu expired")
        time.sleep(1.2)  # now_iso() is second-granularity; keep the window open
        r = self.run_store("supersede", "--id", live, "--reason", "companion test")
        self.assertEqual(r.returncode, 0, r.stderr)
        # A LIVE row whose valid_until predates any sane --as-of instant:
        # excluded by the temporal clause under --as-of, but present without
        # it (temporal bounds are only enforced under --as-of). All SQL edits
        # happen in ONE committed-and-closed block — an open write
        # transaction locks out the subprocess searches below.
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE memory SET valid_until='2020-06-01T00:00:00Z' "
                "WHERE id=?", (expired,))
            conn.commit()
            valid_from = conn.execute(
                "SELECT valid_from FROM memory WHERE id=?", (live,)).fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(valid_from, "superseded row must carry valid_from")

        # Without --as-of: the live filter keeps the superseded row out and
        # the (still-live) expired row in — identical to the plain-path law.
        ids, _ = self.search_ids(
            "--exact", "--text", "temporal probe zulu", "--namespace", NS)
        self.assertEqual(ids, [expired])

        # With --as-of inside [valid_from, valid_until): the superseded row
        # surfaces (live filter dropped) and the expired row is excluded by
        # the temporal clause (valid_until <= instant).
        ids, _ = self.search_ids(
            "--exact", "--text", "temporal probe zulu", "--namespace", NS,
            "--as-of", valid_from)
        self.assertEqual(ids, [live])


class TestExactTelemetry(_CompanionBase):
    """(c) --exact interacts with --no-bump exactly like plain search (#21)."""

    def test_exact_bumps_retrieval_count_and_no_bump_does_not(self):
        row = self.add_row(NS, "telemetry probe whiskey contains the literal")

        def counts() -> tuple[int, int]:
            conn = sqlite3.connect(self.store)
            try:
                cur = conn.execute(
                    "SELECT retrieval_count, surfaced_count FROM memory "
                    "WHERE id=?", (row,)).fetchone()
            finally:
                conn.close()
            return cur[0], cur[1]

        self.assertEqual(counts(), (0, 0))
        r = self.run_store("search", "--exact", "--text",
                           "telemetry probe whiskey", "--namespace", NS,
                           "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(counts(), (1, 0))

        r = self.run_store("search", "--exact", "--text",
                           "telemetry probe whiskey", "--namespace", NS,
                           "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(counts(), (1, 1))


class TestLibrarySeamGuards(_CompanionBase):
    """(d) recall_memory(exact=True) refuses non-literal splices."""

    def _import_storelib_recall(self):
        # Repo convention (memory: storelib freezes the store path at import):
        # pin env, purge any cached storelib modules, THEN import.
        self._saved_env = {k: os.environ.get(k) for k in
                           ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                            "ZMEM_MODEL_AUTODOWNLOAD")}
        os.environ["ZMEM_STORE"] = self.store
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-such-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        for key in [k for k in list(sys.modules)
                    if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[key]
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.recall import recall_memory  # noqa: E402 (post-pin import)
        return recall_memory

    def setUp(self):
        super().setUp()
        recall_memory = self._import_storelib_recall()

        def _restore():
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for key in [k for k in list(sys.modules)
                        if k == "storelib" or k.startswith("storelib.")]:
                del sys.modules[key]
        self.addCleanup(_restore)
        self.recall_memory = recall_memory

    def test_guards_refuse_non_literal_combinations(self):
        # Default kwargs (link_hops defaults to 1) must refuse, not splice.
        with self.assertRaises(ValueError):
            self.recall_memory(None, query="anything", exact=True)
        with self.assertRaises(ValueError):
            self.recall_memory(None, query="x", exact=True, for_injection=True)
        with self.assertRaises(ValueError):
            self.recall_memory(None, query="x", exact=True, link_hops=0,
                               scopes={"lane": "user"})
        with self.assertRaises(ValueError):
            self.recall_memory(None, query="x", exact=True, link_hops=0,
                               include_cross_project=True)
        with self.assertRaises(ValueError):
            self.recall_memory(None, query="x", exact=True, link_hops=0,
                               cross_rerank=True)

    def test_exact_link_hops_zero_returns_only_literal_rows(self):
        hit = self.add_row(NS, "neighbor probe victor holds the literal")
        neighbor = self.add_row(NS, "neighbor probe victor sibling lacks it")
        r = self.run_store("links", "--add", "--id", hit, "--id", neighbor,
                           "--relation", "related")
        self.assertEqual(r.returncode, 0, r.stderr)

        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rows = self.recall_memory(
                conn, query="holds the literal", namespace=NS, exact=True,
                link_hops=0, no_bump=True, as_json=True)
        self.assertEqual([row["id"] for row in rows], [hit])

        # Contrast: the SAME store through plain recall (link_hops=1) shows
        # the neighbor — proving the link exists and expansion would splice.
        buf = io.StringIO()
        with redirect_stdout(buf):
            plain = self.recall_memory(
                conn, query="holds the literal", namespace=NS,
                link_hops=1, no_bump=True, as_json=True)
        self.assertEqual({row["id"] for row in plain}, {hit, neighbor})


class TestBeliefIsolation(_CompanionBase):
    """(e) --exact never admits virtual belief rows nor suppresses members."""

    def test_exact_skips_belief_head_merge(self):
        member = self.add_row(
            NS, "belief probe uniform flange calibrates nightly before standby")
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT INTO belief_head (id, namespace, topic_identity, "
                "content, head_state, head_source_id, support_count, "
                "refresh_watermark, generator_revision, confidence, signal, "
                "taint, trust_score) VALUES "
                "('bh-s02c-1', ?, 's02c-topic', "
                "'nightly before standby the calibrates flange uniform probe', "
                "'active', ?, 5, '', 'companion', 0.8, 'test', "
                "'trusted_internal', 1.0)",
                (NS, member))
            ingestion = conn.execute(
                "SELECT ingestion_ts FROM memory WHERE id=?",
                (member,)).fetchone()[0]
            conn.execute(
                "INSERT INTO belief_head_source (head_id, source_id, role, "
                "source_ingestion_ts, source_checksum) VALUES "
                "('bh-s02c-1', ?, 'member', ?, 'x')",
                (member, ingestion))
            conn.commit()
        finally:
            conn.close()

        # Fixture is live: the plain path admits the virtual head (the head
        # content shares the query's tokens, so the term match fires).
        _, plain_rows = self.search_ids(
            "--text", "flange calibrates nightly", "--namespace", NS)
        self.assertTrue(
            any(row.get("type") == "belief_head" for row in plain_rows),
            "fixture self-check failed: plain search did not admit the head")

        # The exact path returns ONLY the literal-matching memory row: no
        # virtual belief row, and the member is not suppressed by one.
        ids, rows = self.search_ids(
            "--exact", "--text", "flange calibrates nightly", "--namespace", NS)
        self.assertEqual(ids, [member])
        self.assertTrue(all(row.get("type") != "belief_head" for row in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
