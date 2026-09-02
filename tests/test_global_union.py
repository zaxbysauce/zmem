"""Tests for the `--include-global` recall/recent/search union + the write-time
namespace validation that closes issue #18.

Issue #18: `recall_memory`/`recent_memory` never unioned `user:global` when
called with a `project:<…>` namespace, so global memories could not surface in
a project-scoped session from any automatic hook. The fix adds an opt-in
`--include-global`/`--global-limit` pair (project-first merge, so a global row
never crowds out a project row), gives `recent` the v5 alias expansion `recall`
already had, collapses the SubagentStart shell bridge into the shared path, and
rejects obvious global-namespace misspellings at write time.

Drives the REAL store.py CLI via subprocess against a throwaway temp store —
never the box store — following the isolation fixture pattern from
tests/test_export_pack.py / tests/test_no_bump.py (ZMEM_STORE set inline on
every subprocess env dict; ZMEM_DATA and friends popped so no ambient env var
can redirect a run at the real ~/.zmem store).

Run: python tests/test_global_union.py
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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
PROJECT_NS = "project:globaluniontest"


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
    return env


class _StoreCase(unittest.TestCase):
    """Common temp-store fixture: a fresh store dir per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-globalunion-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _base_env(self.tmp)

    def run_store(self, *args, env: dict | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env or self.env, capture_output=True, text=True, timeout=60,
        )

    def add(self, namespace: str, content: str, *, confidence: float = 0.8,
            signal: str = "test", type_: str = "lesson") -> str:
        r = self.run_store("add", "--namespace", namespace, "--type", type_,
                           "--content", content, "--signal", signal,
                           "--confidence", str(confidence))
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content=? AND namespace=? "
                "ORDER BY ingestion_ts DESC LIMIT 1",
                (content, namespace),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, f"could not find just-added row: {content!r}")
        return row[0]

    def recall_json(self, query: str, *extra: str) -> list[dict]:
        r = self.run_store("recall", "--query", query, "--no-bump", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = json.loads(r.stdout) if r.stdout.strip() else []
        # v13 (issue #65, 10.8): read --json emits the envelope.
        return parsed["results"] if isinstance(parsed, dict) else parsed

    def recent_json(self, *extra: str) -> list[dict]:
        r = self.run_store("recent", "--no-bump", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = json.loads(r.stdout) if r.stdout.strip() else []
        # v13 (issue #65, 10.8): read --json emits the envelope.
        return parsed["results"] if isinstance(parsed, dict) else parsed


# --------------------------------------------------------------------------
# recall --include-global
# --------------------------------------------------------------------------

class TestRecallIncludeGlobal(_StoreCase):
    """The headline fix: a project-scoped session can finally reach user:global."""

    def test_recall_include_global_surfaces_user_global_row(self):
        """Gold-standard repro of issue #18: scoped recall WITH --include-global
        returns the relevant user:global row (without it, it does not)."""
        self.add("user:global",
                 "Always prefer per-tier budgets when unioning namespaces")
        self.add(PROJECT_NS, "unrelated project-scoped note about flaky widgets")
        # WITHOUT --include-global: the user:global row is invisible (defect 1).
        scoped = self.recall_json(
            "per-tier budgets unioning namespaces", "--namespace", PROJECT_NS)
        self.assertNotIn(
            "user:global", [r["namespace"] for r in scoped],
            "without --include-global the user:global row must NOT surface")
        # WITH --include-global: the user:global row surfaces.
        union = self.recall_json(
            "per-tier budgets unioning namespaces", "--namespace", PROJECT_NS,
            "--include-global")
        nss = [r["namespace"] for r in union]
        self.assertIn("user:global", nss,
                      "--include-global must surface the user:global row")

    def test_recall_without_include_global_returns_only_project_rows(self):
        """Strict-by-default: no flag ⇒ byte-identical to before (C-3 for recall)."""
        for i in range(5):
            self.add(PROJECT_NS, f"project widget note number {i}")
        for i in range(3):
            self.add("user:global", f"global cross-project lesson number {i}")
        results = self.recall_json("widget project note", "--namespace", PROJECT_NS,
                                   "--limit", "5")
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(all(r["namespace"] == PROJECT_NS for r in results),
                        "no user:global row may appear without --include-global")
        self.assertLessEqual(len(results), 5)


# --------------------------------------------------------------------------
# recent --include-global + recent alias expansion (defect 2)
# --------------------------------------------------------------------------

class TestRecentIncludeGlobal(_StoreCase):

    def test_recent_include_global_surfaces_user_global_row(self):
        """SessionStart Tier 2 path: scoped recent WITH --include-global returns
        global rows (defect 2 / SessionStart)."""
        self.add("user:global", "cross-project lesson about caching")
        self.add(PROJECT_NS, "project-scoped note about widgets")
        scoped = self.recent_json("--namespace", PROJECT_NS, "--limit", "3",
                                  "--min-confidence", "0.5")
        self.assertTrue(all(r["namespace"] == PROJECT_NS for r in scoped))
        union = self.recent_json("--namespace", PROJECT_NS, "--limit", "3",
                                 "--min-confidence", "0.5", "--include-global")
        nss = [r["namespace"] for r in union]
        self.assertIn("user:global", nss)
        self.assertIn(PROJECT_NS, nss)

    def test_recent_without_include_global_returns_only_project_rows(self):
        """Strict-by-default for recent (C-3 for recent)."""
        self.add(PROJECT_NS, "project note alpha")
        self.add("user:global", "global lesson beta")
        results = self.recent_json("--namespace", PROJECT_NS)
        self.assertTrue(all(r["namespace"] == PROJECT_NS for r in results))

    def test_recent_include_global_with_no_namespace_unscoped(self):
        """Reviewer blind spot: when no namespace is given, --include-global is
        a no-op (the unscoped recent already returns everything)."""
        self.add("user:global", "global lesson gamma")
        self.add(PROJECT_NS, "project note delta")
        results = self.recent_json("--include-global", "--limit", "5")
        nss = {r["namespace"] for r in results}
        self.assertIn("user:global", nss, "unscoped recent must still return the global row")
        self.assertIn(PROJECT_NS, nss)

    def test_recent_expands_v5_aliases(self):
        """Defect 2 fix: recent now honours v5 migration aliases the way recall
        already did. A row stored under the NEW (post-migration) key must be
        reachable by recent via the OLD (pre-migration) key."""
        self.run_store("init")  # ensure schema + meta table
        # Inject a synthetic migration alias map (project:oldkey -> project:newkey)
        # the same way tests/test_namespace.py does.
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('ns_migration_v5', ?)",
                (json.dumps({"project:oldkey": "project:newkey"}),),
            )
            conn.commit()
        finally:
            conn.close()
        self.add("project:newkey", "synthetic aliased lesson about gizmo assembly")
        # recent by the OLD (pre-migration) namespace must find the row via alias.
        results = self.recent_json("--namespace", "project:oldkey", "--limit", "5")
        self.assertTrue(
            any(r["namespace"] == "project:newkey" for r in results),
            "recent must expand v5 migration aliases (defect 2 fix)")


# --------------------------------------------------------------------------
# Merge contract: project-first, no crowding, no double-count (C-1, C-4)
# --------------------------------------------------------------------------

class TestMergeContract(_StoreCase):

    def test_merge_project_first_then_global_order(self):
        """The SubagentStart collapse must produce project-first then global,
        all distinct ids (the old two-pull bridge order). Seed project rows
        first, then globals (so globals are NEWER by ingestion_ts); the merge
        must still put the project tier first regardless of intra-tier order."""
        pids = [self.add(PROJECT_NS, f"project row {i}") for i in range(5)]
        gids = [self.add("user:global", f"global row {i}") for i in range(3)]
        results = self.recent_json("--namespace", PROJECT_NS, "--limit", "5",
                                   "--include-global", "--global-limit", "3",
                                   "--min-confidence", "0.5")
        ids = [r["id"] for r in results]
        # All 8 distinct ids present, no duplicates.
        self.assertEqual(len(ids), len(set(ids)), "no duplicate ids (dedup)")
        self.assertEqual(len(ids), 8, "5 project + 3 global")
        # Project tier occupies the FIRST 5 slots (as a set), global tier the
        # LAST 3 — regardless of intra-tier ingestion_ts order. This is the
        # project-first hard-floor contract (a global row never precedes a
        # project row), matching the old two-pull bridge.
        self.assertEqual(set(ids[:5]), set(pids),
                         "project tier is a hard floor, occupies first 5 slots")
        self.assertEqual(set(ids[5:]), set(gids),
                         "global tier fills the remaining slots")

    def test_global_does_not_crowd_out_project_when_full(self):
        """C-1 anti-crowding: fill the project tier to its limit with relevant
        rows; add MORE global rows than global_limit; assert all project rows
        present AND global is truncated to exactly global_limit (PRR-004: the
        old single-row/seeds<=limit form was vacuous — it never proved the
        global tier is truncated)."""
        for i in range(5):
            self.add(PROJECT_NS, f"project widget {i}", confidence=0.7)
        # Seed MORE global rows (4) than global_limit (2) so the test would
        # FAIL if _merge_tiers didn't slice global_scored[:global_limit].
        for i in range(4):
            self.add("user:global", f"global widget lesson {i}", confidence=0.95)
        results = self.recall_json("widget", "--namespace", PROJECT_NS,
                                   "--limit", "5", "--include-global",
                                   "--global-limit", "2")
        project_results = [r for r in results if r["namespace"] == PROJECT_NS]
        global_results = [r for r in results if r["namespace"] == "user:global"]
        # All 5 project rows present (global did not crowd them out).
        self.assertEqual(len(project_results), 5,
                         "project tier is a hard floor — global cannot crowd it out")
        # Global is truncated to EXACTLY global_limit (proves the per-tier cap).
        self.assertEqual(len(global_results), 2,
                         "global tier must be truncated to global_limit when more rows exist")

    def test_include_global_with_namespace_user_global_is_noop(self):
        """C-4: when namespace IS user:global, --include-global is a no-op (no
        double-count). The project tier already covers global."""
        for i in range(4):
            self.add("user:global", f"global lesson {i}")
        results = self.recall_json("global lesson", "--namespace", "user:global",
                                   "--limit", "5", "--include-global")
        ids = [r["id"] for r in results]
        self.assertEqual(len(ids), len(set(ids)), "no double-count when ns is global")
        self.assertTrue(all(r["namespace"] == "user:global" for r in results))

    def test_include_global_with_empty_project_tier_returns_global(self):
        """Reviewer blind spot: when the project tier has no matches but global
        does, --include-global still surfaces the global rows."""
        self.add("user:global", "global lesson about retry budgets")
        # No project rows at all that match the query.
        results = self.recall_json("retry budgets", "--namespace", PROJECT_NS,
                                   "--limit", "5", "--include-global")
        self.assertTrue(any(r["namespace"] == "user:global" for r in results),
                        "empty project tier must not hide matching global rows")

    def test_include_global_with_global_limit_zero_excludes_global(self):
        """Reviewer blind spot: --global-limit 0 means no global rows even with
        --include-global set (nonnegative_int allows 0)."""
        self.add("user:global", "global lesson about caching")
        self.add(PROJECT_NS, "project note about widgets")
        results = self.recall_json("caching widgets", "--namespace", PROJECT_NS,
                                   "--limit", "5", "--include-global",
                                   "--global-limit", "0")
        self.assertFalse(any(r["namespace"] == "user:global" for r in results),
                         "--global-limit 0 must exclude the global tier")

    def test_include_global_with_no_namespace_unscoped(self):
        """When no namespace is given, --include-global is a no-op: the unscoped
        query already searches everything. The contract must hold (critic test)."""
        self.add("user:global", "global cross-project fact about caching")
        self.add(PROJECT_NS, "project note")
        results = self.recall_json("caching", "--include-global")
        self.assertTrue(any(r["namespace"] == "user:global" for r in results),
                        "unscoped recall must still return the global row")


# --------------------------------------------------------------------------
# search --include-global (parity)
# --------------------------------------------------------------------------

class TestSearchIncludeGlobal(_StoreCase):

    def test_search_include_global_surfaces_user_global_row(self):
        self.add("user:global", "cross-project fact about retry budgets")
        self.add(PROJECT_NS, "project note about widgets")
        # search has no confidence floor (min_confidence=0.0); --no-bump avoids
        # advancing retrieval_count in this test (a surface is recorded instead — issue #21).
        r = self.run_store("search", "--text", "retry budgets",
                           "--namespace", PROJECT_NS, "--include-global",
                           "--no-bump")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("user:global", r.stdout,
                      "search --include-global must surface the global row")


# --------------------------------------------------------------------------
# write-time namespace validation (Edit 5)
# --------------------------------------------------------------------------

class TestNamespaceValidation(_StoreCase):

    def test_validate_namespace_rejects_near_miss_global_variants(self):
        """Obvious global-namespace misspellings are rejected at write time."""
        for bad in ["global", "Global", "globals", "userglobal", "users:global",
                    "user-global", "user_global", "global:user", "globals:user",
                    "user:globals"]:
            r = self.run_store("add", "--namespace", bad, "--type", "lesson",
                               "--content", f"x {bad}", "--signal", "test")
            self.assertNotEqual(r.returncode, 0,
                                f"{bad!r} must be rejected (got rc=0)")
            self.assertIn("user:global", r.stderr + r.stdout,
                          f"{bad!r}: refusal must name the canonical form")
        # And NONE of these got written.
        conn = sqlite3.connect(self.store)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE namespace IN "
                "('global','Global','globals','userglobal','users:global',"
                "'user-global','user_global','global:user','globals:user',"
                "'user:globals')"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "no near-miss row may be persisted")

    def test_validate_namespace_accepts_canonical_and_project(self):
        """The canonical form and project namespaces pass through untouched."""
        self.add("user:global", "canonical global row")
        self.add(PROJECT_NS, "project row")
        self.add("project:github.com/org/repo", "real remote-namespaced row")
        self.add("project:global-thing", "legit namespace containing the word global")
        conn = sqlite3.connect(self.store)
        try:
            nss = {row[0] for row in conn.execute(
                "SELECT DISTINCT namespace FROM memory")}
        finally:
            conn.close()
        self.assertEqual(nss, {
            "user:global", PROJECT_NS,
            "project:github.com/org/repo", "project:global-thing",
        })

    def test_validate_namespace_strips_whitespace(self):
        """Whitespace is trimmed (intentional canonicalization)."""
        r = self.run_store("add", "--namespace", "  user:global  ",
                           "--type", "lesson", "--content", "trimmed ns",
                           "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT namespace FROM memory WHERE content='trimmed ns'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "user:global",
                         "namespace must be stored trimmed, no surrounding whitespace")

    def test_validate_namespace_rejects_empty(self):
        r = self.run_store("add", "--namespace", "   ", "--type", "lesson",
                           "--content", "empty ns", "--signal", "test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("namespace is empty", r.stderr + r.stdout)

    def test_validate_namespace_reports_stranded_near_miss_count(self):
        """Reviewer M-1: the refusal message reports ALL stranded near-miss rows
        (any variant sharing a stem), not just the exact spelling typed. Seed a
        stranded row under one variant, then try to write a DIFFERENT variant —
        the count must be non-zero."""
        # Runs under ZMEM_AUTO_REKEY=0: with issue #71 C's auto-remediation
        # (default on), the store open for the refused add heals the stranded
        # rows BEFORE the write guard counts them, so the count clause is only
        # reachable in the kill-switch world this test pins.
        self.env["ZMEM_AUTO_REKEY"] = "0"
        # Seed a stranded row directly under a near-miss namespace (bypassing
        # the now-active write-time check by inserting straight into the DB).
        conn = sqlite3.connect(self.store)
        try:
            self.run_store("init")  # ensure schema
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, confidence, "
                "signal, valid_from, ingestion_ts, retrieval_count) VALUES "
                "('deadbeef', 'userglobal', 'lesson', 'stranded', 0.8, 'test', "
                "'2026-01-01', '2026-01-01', 0)"
            )
            conn.commit()
        finally:
            conn.close()
        # Now try to add under a DIFFERENT near-miss spelling.
        r = self.run_store("add", "--namespace", "global", "--type", "lesson",
                           "--content", "new near miss", "--signal", "test")
        self.assertNotEqual(r.returncode, 0)
        # The stranded count must reflect the stranded 'userglobal' row even
        # though we typed 'global' (M-1: widened from exact-match).
        self.assertIn("1 existing live row", r.stderr + r.stdout,
                      "stranded count must cover all near-miss variants, not just the typed one")


# --------------------------------------------------------------------------
# Hybrid RRF honours the union (C-2) — guarded by embeddings availability.
# --------------------------------------------------------------------------

class TestHybridRrfGlobalUnion(_StoreCase):
    """C-2: a global-only vec neighbor (no FTS hit) must survive the project
    tier's _fetch_by_ids namespace filter when --include-global is on. This
    requires the embedding model, so it self-skips when embeddings are absent
    (repo convention from tests/test_model_fallback.py)."""

    def test_recall_include_global_hybrid_rrf_keeps_global_vec_neighbor(self):
        # Detect embedding availability the same way store.py does.
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            import embeddings  # noqa: E402
        except Exception:
            self.skipTest("embeddings module not importable")
        # Embeddings need the ONNX model present + onnxruntime installed.
        env = dict(self.env)
        # Allow the real model dir (not the forced-absent one) only if present.
        # Reuse the production models dir by clearing the override.
        env.pop("ZMEM_MODELS_DIR", None)
        env.pop("ZMEM_MODEL_AUTODOWNLOAD", None)
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            emb_mod = __import__("embeddings")
        except Exception:
            self.skipTest("embeddings module not importable")
        if not emb_mod.is_available():
            self.skipTest("ONNX embedding model / runtime not available — hybrid test skipped")

        # Seed a global row with content that has NO FTS token overlap with the
        # query, so it can only be found via vector similarity.
        self.add("user:global", "the quick brown fox jumps over the lazy dog",
                 confidence=0.9)
        self.add(PROJECT_NS, "completely unrelated project scaffolding note",
                 confidence=0.9)
        r = self.run_store("recall", "--query", "fast nimble canine vaults the sleepy hound",
                           "--namespace", PROJECT_NS, "--include-global",
                           "--hybrid", "--no-bump", "--json", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = json.loads(r.stdout) if r.stdout.strip() else []
        # v13 (issue #65, 10.8): read --json emits the envelope.
        results = parsed["results"] if isinstance(parsed, dict) else parsed
        # The global fox row (semantic match only) must surface via the union.
        self.assertTrue(
            any(r_["namespace"] == "user:global" for r_ in results),
            "hybrid RRF + --include-global must surface the global-only vec neighbor")


# --------------------------------------------------------------------------
# rekey-namespace admin (F3)
# --------------------------------------------------------------------------

class TestRekeyNamespace(_StoreCase):
    """Pins the MANUAL rekey-namespace machinery (F3, PRR-001/002/013). Runs
    under ZMEM_AUTO_REKEY=0: issue #71 C's auto-remediation (default on,
    covered by tests/test_auto_rekey.py) heals stranded rows on every store
    open, which would otherwise consume these fixtures before the operator
    command under test runs."""

    def setUp(self):
        super().setUp()
        self.env["ZMEM_AUTO_REKEY"] = "0"

    def _seed_stranded(self, ns: str, mid: str):
        conn = sqlite3.connect(self.store)
        try:
            self.run_store("init")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, confidence, "
                "signal, valid_from, ingestion_ts, retrieval_count) VALUES "
                "(?, ?, 'lesson', ?, 0.8, 'test', '2026-01-01', '2026-01-01', 0)",
                (mid, ns, f"stranded under {ns}")
            )
            conn.commit()
        finally:
            conn.close()

    def test_rekey_near_miss_global_dry_run_writes_nothing(self):
        self._seed_stranded("userglobal", "deadbeef-0000-0000-0000-000000000001")
        r = self.run_store("rekey-namespace", "--near-miss-global", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 live row", r.stdout)
        # Row still stranded.
        conn = sqlite3.connect(self.store)
        try:
            ns = conn.execute(
                "SELECT namespace FROM memory WHERE id=?",
                ("deadbeef-0000-0000-0000-000000000001",)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ns, "userglobal")

    def test_rekey_without_confirm_refuses(self):
        self._seed_stranded("userglobal", "deadbeef-0000-0000-0000-000000000002")
        r = self.run_store("rekey-namespace", "--near-miss-global")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--confirm", r.stderr + r.stdout)

    def test_rekey_near_miss_global_confirm_moves_rows(self):
        self._seed_stranded("userglobal", "deadbeef-0000-0000-0000-000000000003")
        self._seed_stranded("global", "deadbeef-0000-0000-0000-000000000004")
        r = self.run_store("rekey-namespace", "--near-miss-global", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rekeyed 2 row", r.stdout)
        conn = sqlite3.connect(self.store)
        try:
            nss = {row[0] for row in conn.execute(
                "SELECT namespace FROM memory WHERE superseded_at IS NULL")}
        finally:
            conn.close()
        self.assertEqual(nss, {"user:global"})

    def test_rekey_refuses_near_miss_destination(self):
        r = self.run_store("rekey-namespace", "--from", "project:x",
                           "--to", "global", "--confirm")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("near-miss", r.stderr + r.stdout)

    def test_rekey_from_to_exact_namespace(self):
        self.add("project:oldname", "row to rekey")
        r = self.run_store("rekey-namespace", "--from", "project:oldname",
                           "--to", "project:newname", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            ns = conn.execute(
                "SELECT namespace FROM memory WHERE content='row to rekey'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ns, "project:newname")

    def test_rekey_near_miss_global_does_not_touch_canonical_user_global(self):
        """PRR-001: --near-miss-global must EXCLUDE canonical user:global rows
        from the source set (they normalize to "userglobal" ∈ stems but are NOT
        stranded). `--to project:x --confirm` must NOT move legit global rows."""
        self._seed_stranded("userglobal", "deadbeef-aaaa-0000-0000-000000000001")
        # A legit canonical user:global row that must be left alone.
        self.add("user:global", "legitimate canonical global lesson")
        r = self.run_store("rekey-namespace", "--near-miss-global",
                           "--to", "project:dest", "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        # Only the userglobal row was rekeyed; the canonical row stays.
        conn = sqlite3.connect(self.store)
        try:
            nss = {row[0] for row in conn.execute(
                "SELECT namespace FROM memory WHERE superseded_at IS NULL")}
        finally:
            conn.close()
        self.assertIn("user:global", nss,
                      "canonical user:global must NOT be rekeyed")
        self.assertIn("project:dest", nss,
                      "the near-miss row was rekeyed to the destination")
        # The original near-miss namespace is gone.
        self.assertNotIn("userglobal", nss)

    def test_rekey_rejects_empty_destination(self):
        """PRR-002: --to "" and whitespace are rejected (empty namespace is a
        one-way door — rows stranded under "" can't be rekeyed back)."""
        self.add(PROJECT_NS, "row to potentially strand")
        for bad_to in ["", "   "]:
            r = self.run_store("rekey-namespace", "--from", PROJECT_NS,
                               "--to", bad_to, "--confirm")
            self.assertNotEqual(r.returncode, 0,
                                f"--to {bad_to!r} must be rejected")
            self.assertIn("empty", (r.stderr + r.stdout).lower())
        # The row was NOT moved (no empty-namespace rows created AND the
        # original row stays under PROJECT_NS).
        conn = sqlite3.connect(self.store)
        try:
            n_empty = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE namespace='' OR namespace='   '"
            ).fetchone()[0]
            orig = conn.execute(
                "SELECT namespace FROM memory WHERE content='row to potentially strand'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(n_empty, 0, "no empty-namespace row may be written")
        self.assertIsNotNone(orig, "the original row must still exist")
        self.assertEqual(orig[0], PROJECT_NS,
                         "the original row must remain under its original namespace")

    def test_rekey_near_miss_global_reports_stranded_excluding_canonical(self):
        """PRR-001 sibling: the add-time refusal stranded-count must NOT count
        canonical user:global rows as stranded."""
        self._seed_stranded("userglobal", "deadbeef-bbbb-0000-0000-000000000002")
        self.add("user:global", "legit canonical global row for count test")
        r = self.run_store("add", "--namespace", "global", "--type", "lesson",
                           "--content", "trigger near miss", "--signal", "test")
        self.assertNotEqual(r.returncode, 0)
        msg = r.stderr + r.stdout
        self.assertIn("1 existing live row", msg,
                      "stranded count must exclude canonical user:global (only the "
                      "userglobal row is stranded, not the legit global row)")

    def test_rekey_leaves_superseded_rows_untouched(self):
        # Insert a superseded near-miss row directly (history must be preserved).
        conn = sqlite3.connect(self.store)
        try:
            self.run_store("init")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, confidence, "
                "signal, valid_from, ingestion_ts, retrieval_count, superseded_at) "
                "VALUES ('sup-0000-0000-0000-000000000001', 'userglobal', "
                "'lesson', 'superseded stranded', 0.8, 'test', '2026-01-01', "
                "'2026-01-01', 0, '2026-02-01')"
            )
            conn.commit()
        finally:
            conn.close()
        self.run_store("rekey-namespace", "--near-miss-global", "--confirm")
        conn = sqlite3.connect(self.store)
        try:
            ns = conn.execute(
                "SELECT namespace FROM memory WHERE content='superseded stranded'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ns, "userglobal",
                         "superseded rows must NOT be rekeyed (history preserved)")


# --------------------------------------------------------------------------
# ingest-jsonl near-miss rejection (F2)
# --------------------------------------------------------------------------

class TestIngestJsonlNearMiss(_StoreCase):

    def test_ingest_jsonl_rejects_near_miss_namespace(self):
        """F2: ingest-jsonl applies the same near-miss rejection as add, so a
        remote peer cannot strand rows under `global`/`userglobal` on this store."""
        self.run_store("init")
        sync_line = json.dumps({
            "id": "12345678-1234-5678-1234-567812345678",
            "namespace": "global",
            "type": "lesson",
            "content": "sync-imported near-miss row",
            "signal": "test",
            "confidence": 0.8,
            "valid_from": "2026-01-01T00:00:00Z",
            "ingestion_ts": "2026-01-01T00:00:00Z",
        })
        sync_file = os.path.join(self.tmp, "sync.jsonl")
        with open(sync_file, "w", encoding="utf-8") as f:
            f.write(sync_line + "\n")
        r = self.run_store("ingest-jsonl", "--in", sync_file)
        self.assertEqual(r.returncode, 0, r.stderr)  # exit 0: counted malformed, continued
        self.assertIn("malformed=1", r.stdout + r.stderr)
        self.assertIn("misspelling of the global namespace", r.stdout + r.stderr)
        # And the row was NOT written.
        conn = sqlite3.connect(self.store)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE namespace='global'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "near-miss row must not be ingested")

    def test_ingest_jsonl_accepts_canonical_global(self):
        """A canonical user:global sync row ingests normally."""
        self.run_store("init")
        sync_line = json.dumps({
            "id": "87654321-4321-8765-4321-876543218765",
            "namespace": "user:global",
            "type": "lesson",
            "content": "legit global sync row",
            "signal": "test",
            "confidence": 0.8,
            "valid_from": "2026-01-01T00:00:00Z",
            "ingestion_ts": "2026-01-01T00:00:00Z",
        })
        sync_file = os.path.join(self.tmp, "sync_ok.jsonl")
        with open(sync_file, "w", encoding="utf-8") as f:
            f.write(sync_line + "\n")
        r = self.run_store("ingest-jsonl", "--in", sync_file)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added=1", r.stdout + r.stderr)


# --------------------------------------------------------------------------
# search bump / no-bump (critic minor: CLI lease coverage)
# --------------------------------------------------------------------------

class TestSearchBump(_StoreCase):

    def _count_retrieval(self, content: str) -> int:
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT retrieval_count FROM memory WHERE content=?", (content,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else 0

    def test_search_bumps_retrieval_count_by_default(self):
        self.add(PROJECT_NS, "widget alpha beta gamma")
        self.run_store("search", "--text", "widget alpha")
        self.assertEqual(self._count_retrieval("widget alpha beta gamma"), 1,
                         "search defaults to bumping like recall")

    def test_search_no_bump_leaves_retrieval_count_unchanged(self):
        self.add(PROJECT_NS, "gizmo delta epsilon zeta")
        self.run_store("search", "--text", "gizmo delta", "--no-bump")
        self.assertEqual(self._count_retrieval("gizmo delta epsilon zeta"), 0,
                         "search --no-bump must not bump retrieval_count "
                         "(surface recorded instead — issue #21)")


# --------------------------------------------------------------------------
# _merge_tiers unit test — proves the per-tier-budget slice DIRECTLY
# (the end-to-end crowding test cannot, because _recall_one_tier already
# truncates to limit=global_limit before _merge_tiers sees the list).
# --------------------------------------------------------------------------

class TestMergeTiersUnit(unittest.TestCase):
    """Drive _merge_tiers directly with oversized inputs to prove it — not the
    upstream tier fetch — enforces the per-tier budget (PRR-004 follow-up)."""

    def _load_store(self):
        import importlib.util
        tmp = tempfile.mkdtemp(prefix="zmem-mergetiers-")
        self.addCleanup(shutil.rmtree, tmp, True)
        spec = importlib.util.spec_from_file_location(
            f"zmem_store_mt_{id(tmp)}", SCRIPTS_DIR / "store.py")
        env = {**os.environ, "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
               "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        with mock.patch.dict(os.environ, env, clear=False):
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def _item(self, mid, ns):
        return {"id": mid, "namespace": ns, "type": "lesson", "content": "",
                "tags": "", "confidence": 0.9, "signal": "test",
                "source_ref": "", "valid_from": "", "stale": False,
                "_stale_note": "", "_score": 0.5}

    def test_merge_tiers_truncates_global_at_global_limit(self):
        """Feed _merge_tiers 4 project + 5 global scored tuples with
        project_limit=4, global_limit=2. It must return 4 project + exactly 2
        global — proving the slice `global_scored[:global_limit]` works even
        when the upstream tier fetch did NOT pre-truncate."""
        mod = self._load_store()
        project = [(0.5 - i * 0.01, self._item(f"p{i}", PROJECT_NS))
                   for i in range(4)]
        glob = [(0.9 - i * 0.01, self._item(f"g{i}", "user:global"))
                for i in range(5)]
        results = mod._merge_tiers(project, glob, project_limit=4, global_limit=2)
        self.assertEqual(len(results), 6, "4 project + 2 global (truncated)")
        self.assertEqual([r["id"] for r in results[:4]],
                         ["p0", "p1", "p2", "p3"], "project tier first, hard floor")
        self.assertEqual([r["id"] for r in results[4:]],
                         ["g0", "g1"], "global truncated to global_limit, top-scored first")

    def test_merge_tiers_global_cannot_displace_project(self):
        """Even if every global row outscores every project row, _merge_tiers
        must return ALL project rows first (hard floor) and global only fills
        the remaining global_limit slots."""
        mod = self._load_store()
        project = [(0.1, self._item(f"p{i}", PROJECT_NS)) for i in range(3)]
        # Globals all outscore projects (0.9 > 0.1).
        glob = [(0.9, self._item(f"g{i}", "user:global")) for i in range(5)]
        results = mod._merge_tiers(project, glob, project_limit=3, global_limit=2)
        self.assertEqual([r["namespace"] for r in results[:3]],
                         [PROJECT_NS] * 3, "project hard floor despite lower scores")
        self.assertEqual([r["namespace"] for r in results[3:]],
                         ["user:global"] * 2, "global fills remaining slots")

    def test_merge_tiers_dedups_by_id_project_wins(self):
        """An id present in both tiers is kept once; the project occurrence
        wins (project tier is iterated first)."""
        mod = self._load_store()
        shared = self._item("shared", PROJECT_NS)
        project = [(0.9, shared)]
        glob = [(0.5, {"id": "shared", "namespace": "user:global"})]
        results = mod._merge_tiers(project, glob, project_limit=5, global_limit=5)
        ids = [r["id"] for r in results]
        self.assertEqual(ids, ["shared"], "deduped to one")
        self.assertEqual(results[0]["namespace"], PROJECT_NS,
                         "project wins on id collision")


if __name__ == "__main__":
    unittest.main(verbosity=2)
