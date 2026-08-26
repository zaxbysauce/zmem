"""Tests for the v11 associative-link surface (issue #61).

Covers: schema (memory_link + trust_score, CHECKs, UNIQUE), the v10->v11
migration (trust defaults, merged_from normalization), automatic link
generation on add/update (lexical path — the suite runs model-absent, matching
CI; the vec path is exercised via a stubbed embeddings runtime when
sqlite-vec is importable), the write-time polarity guard, trust deltas +
clamping, attribute evolution (tags only), budgeted 1-hop recall expansion
(flags, floor gating, [CONTESTED LINK], as-of, namespace containment), and
the links/contradict CLI contracts.

House pattern (test_entity.py): every command is the real CLI subprocess
against a throwaway ZMEM_STORE; in-process module loads for internals that
are not CLI-reachable.

Run: python tests/test_memory_links.py
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
import unittest.mock  # noqa: F401  (patch.dict used by TrustClampTest)
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# Schema-version ratchets are DYNAMIC: assert schema_meta's current version
# instead of pinning a number that goes stale on every bump.
sys.path.insert(0, str(SCRIPTS_DIR))
from schema_meta import SUPPORTED_SCHEMA_VERSION  # noqa: E402


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_MMR_LAMBDA", None)
    env.pop("ZMEM_LINK_THRESHOLD", None)
    return env


class _Store(unittest.TestCase):
    """Subprocess-driven house pattern: every command is the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-links-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store(self, *args, env=None):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=120,
        )
        return r

    def add(self, content, *extra, ns="project:links", type_="fact",
            signal="test", tags=None):
        args = ["add", "--namespace", ns, "--type", type_,
                "--content", content, "--signal", signal]
        if tags is not None:
            args += ["--tags", tags]
        r = self.run_store(*args, *extra)
        self.assertEqual(r.returncode, 0, f"add failed:\n{r.stdout}\n{r.stderr}")
        # "[zmem] added memory <uuid> (...)" on insert;
        # "[zmem] dedup: existing memory <uuid> refreshed (...)" on merge.
        for line in r.stdout.splitlines():
            if line.startswith("[zmem] added memory "):
                return line.split()[3]
            if line.startswith("[zmem] dedup: existing memory "):
                return line.split()[4]
        self.fail(f"no added-id line in stdout:\n{r.stdout}")

    def db(self):
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def edges(self):
        return self.db().execute(
            "SELECT src_id, dst_id, relation, score FROM memory_link "
            "ORDER BY src_id, dst_id, relation"
        ).fetchall()

    def trust(self, mid):
        return self.db().execute(
            "SELECT trust_score FROM memory WHERE id=?", (mid,)
        ).fetchone()[0]

    def recall(self, *extra, query="matching query terms", ns="project:links",
               limit=None):
        args = ["recall", "--query", query, "--namespace", ns, "--json"]
        if limit is not None:
            args += ["--limit", str(limit)]
        r = self.run_store(*args, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


# Two contents with token overlap far above 0.75 Jaccard (link threshold) but
# NOT exact matches (so write-time dedup never merges them).
_POS_A = ("the release pipeline always gates deploys on the green full "
          "integration suite run")
_POS_B = ("the release pipeline always gates every deploy on the green full "
          "integration suite run")
# Same high-overlap shape, opposite negation polarity (issue #49's contested
# pair pattern) — must link as contradicts, never merge.
_NEG_B = ("the release pipeline never gates deploys on the green full "
          "integration suite run")
_UNRELATED = "a completely unrelated note about pasta shapes and sauces"


class SchemaTest(_Store):
    def test_v11_tables_columns_and_checks(self):
        self.run_store("init")
        conn = self.db()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, str(SUPPORTED_SCHEMA_VERSION))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("memory_link", tables)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        self.assertIn("trust_score", cols)

    def test_relation_check_rejects_unknown_values(self):
        """The CHECK is schema-level: a direct SQL insert (bypassing every
        Python guard) must be rejected — unknown enum values are unstorable,
        per the issue's 'do not add enum values you cannot store' rule."""
        self.run_store("init")
        conn = self.db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_link(src_id,dst_id,relation,score,created_at)"
                " VALUES ('a','b','bogus',0.5,'x')")

    def test_self_link_check_rejects_at_sql_level(self):
        self.run_store("init")
        conn = self.db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_link(src_id,dst_id,relation,score,created_at)"
                " VALUES ('a','a','related',0.5,'x')")

    def test_trust_defaults_to_one_on_fresh_rows(self):
        self.add(_POS_A)
        rows = self.db().execute(
            "SELECT trust_score FROM memory").fetchall()
        self.assertTrue(all(r[0] == 1.0 for r in rows), rows)

    def test_v10_store_migrates_losslessly_to_v11(self):
        """A faithful v10 store (memory_link absent, trust_score absent,
        merged_from carrying duplicates) migrates to v11 on the next writable
        command: version stamped, trust defaults 1.0, every memory row kept,
        merged_from normalized losslessly."""
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        conn = self.db()
        n_mem = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        conn.execute("DROP TABLE memory_link")
        conn.execute("ALTER TABLE memory DROP COLUMN trust_score")
        # Hand-plant a duplicated merged_from (the pre-v11 writer's possible
        # output) to prove the migration's normalization pass.
        conn.execute(
            "UPDATE memory SET merged_from=? WHERE id=?",
            (f"{b},{a},{b}", a),
        )
        conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        r = self.run_store("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = self.db()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver, str(SUPPORTED_SCHEMA_VERSION))
        self.assertEqual(
            conn.execute("SELECT count(*) FROM memory").fetchone()[0], n_mem,
            "migration must preserve every memory row")
        self.assertEqual(
            [tuple(r) for r in conn.execute(
                "SELECT trust_score FROM memory").fetchall()],
            [(1.0,)] * n_mem,
            "migrated rows must read trust_score 1.0 (column default)")
        self.assertEqual(
            conn.execute("SELECT merged_from FROM memory WHERE id=?",
                         (a,)).fetchone()[0],
            f"{b},{a}",
            "duplicate merged_from ids collapse, first-seen order, none lost")

    def test_reopen_idempotent_after_migration(self):
        """A second open of a migrated store is an exact no-op (the version
        guard makes every v11 block step a no-op)."""
        self.add(_POS_A)
        r = self.run_store("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_store("stats")
        self.assertEqual(r.returncode, 0, r.stderr)


class GenerationTest(_Store):
    def test_paraphrases_link_related_both_directions(self):
        a = self.add(_POS_A)
        b = self.add(_POS_B)
        edges = [(e["src_id"], e["dst_id"], e["relation"]) for e in self.edges()]
        self.assertIn((a, b, "related"), edges)
        self.assertIn((b, a, "related"), edges)
        # Link generation never touches trust (only contradicts/supports do).
        self.assertEqual(self.trust(a), 1.0)
        self.assertEqual(self.trust(b), 1.0)

    def test_below_threshold_generates_no_link(self):
        self.add(_POS_A)
        self.add(_UNRELATED)
        self.assertEqual(len(self.edges()), 0)

    def test_threshold_env_override_is_honored(self):
        """Raising ZMEM_LINK_THRESHOLD above every possible similarity
        disables generation (the characterization builder relies on this)."""
        env = dict(self.env)
        env["ZMEM_LINK_THRESHOLD"] = "1.01"
        self.run_store("add", "--namespace", "project:links", "--type", "fact",
                       "--content", _POS_A, "--signal", "test", env=env)
        self.run_store("add", "--namespace", "project:links", "--type", "fact",
                       "--content", _POS_B, "--signal", "test", env=env)
        self.assertEqual(len(self.edges()), 0)

    def test_no_cross_namespace_links(self):
        a = self.add(_POS_A, ns="project:one")
        b = self.add(_POS_A, ns="project:two")   # identical content, other ns
        edges = self.edges()
        self.assertEqual(len(edges), 0, edges)
        self.assertNotEqual(a, b)

    def test_identical_id_never_links_to_itself(self):
        self.add(_POS_A)
        self.add(_POS_B)
        for e in self.edges():
            self.assertNotEqual(e["src_id"], e["dst_id"])

    def test_polarity_disagreement_links_contradicts_and_never_merges(self):
        a = self.add(_POS_A)
        b = self.add(_NEG_B)
        edges = [(e["src_id"], e["dst_id"], e["relation"]) for e in self.edges()]
        self.assertIn((a, b, "contradicts"), edges)
        self.assertIn((b, a, "contradicts"), edges)
        # Both rows stay live — a contradiction is not a duplicate.
        conn = self.db()
        live = conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(live, 2)
        # One contradicts event = -0.10 per endpoint, from 1.0.
        self.assertAlmostEqual(self.trust(a), 0.9)
        self.assertAlmostEqual(self.trust(b), 0.9)

    def test_exact_duplicate_readd_merges_and_corroborates(self):
        self.add(_POS_A)
        keeper = self.add(_POS_A)   # exact content -> dedup merge path
        conn = self.db()
        live = conn.execute(
            "SELECT id FROM memory WHERE superseded_at IS NULL").fetchall()
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["id"], keeper)
        # Corroborating add: +0.05 clamped at 1.0 (already max).
        self.assertEqual(self.trust(keeper), 1.0)

    def test_corroboration_restores_trust_after_contradiction(self):
        a = self.add(_POS_A)
        self.add(_NEG_B)            # contradicts -> both 0.9
        self.assertAlmostEqual(self.trust(a), 0.9)
        # Re-adding the SAME content as `a` dedup-merges into a (polarity
        # agrees with itself) -> corroborating +0.05.
        self.add(_POS_A)
        self.assertAlmostEqual(self.trust(a), 0.95)

    def test_ten_distinct_contradicts_clamp_at_zero(self):
        a = self.add(_POS_A)
        conn = self.db()
        # Ten DISTINCT contradiction events against `a` (different partners).
        for i in range(10):
            b = self.add(
                f"the release pipeline never gates deploys on the green full "
                f"integration suite run variant {i}")
            del b
        self.assertEqual(self.trust(a), 0.0,
                         "ten contradicts must land at exactly 0.0, "
                         "clamped, never negative")

    def test_repeat_contradiction_is_idempotent_no_double_delta(self):
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        r1 = self.run_store("contradict", "--id", a, "--id", b,
                            "--reason", "first pass")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertAlmostEqual(self.trust(a), 0.9)
        r2 = self.run_store("contradict", "--id", a, "--id", b,
                            "--reason", "re-run")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertAlmostEqual(self.trust(a), 0.9,
                               "re-contradicting the same pair is an exact "
                               "no-op (no second trust delta)")

    def test_attribute_evolution_tags_only(self):
        a = self.add(_POS_A, tags="ci,deploy")
        before = self.db().execute(
            "SELECT content, confidence, signal, retrieval_count, tags "
            "FROM memory WHERE id=?", (a,)).fetchone()
        self.add(_POS_B, tags="deploy,green")   # links to a; a gains 'green'
        after = self.db().execute(
            "SELECT content, confidence, signal, retrieval_count, tags "
            "FROM memory WHERE id=?", (a,)).fetchone()
        self.assertEqual(after["content"], before["content"],
                         "content is never rewritten by linking")
        self.assertEqual(after["confidence"], before["confidence"])
        self.assertEqual(after["signal"], before["signal"])
        self.assertEqual(after["retrieval_count"], before["retrieval_count"])
        # Tag union: sorted, deduplicated, nothing lost.
        self.assertEqual(
            sorted(t.strip() for t in after["tags"].split(",")),
            ["ci", "deploy", "green"])

    def test_update_generates_links_on_replacement_row(self):
        a = self.add(_POS_A)
        r = self.run_store("update", "--id", a, "--content", _POS_B)
        self.assertEqual(r.returncode, 0, r.stderr)
        new_id = None
        for line in r.stdout.splitlines():
            if line.startswith("[zmem] updated memory "):
                new_id = line.split("->")[1].split()[0]
        self.assertIsNotNone(new_id, r.stdout)
        edges = [(e["src_id"], e["dst_id"], e["relation"]) for e in self.edges()]
        # The only other live row is gone (tombstoned), so the replacement's
        # neighbor set is empty of live rows — but the walk itself must not
        # crash and must not resurrect tombstoned neighbors.
        self.assertEqual(edges, [])

    def test_update_dedup_polarity_guard_prevents_contradiction_merge(self):
        """update() content that contradicts ANOTHER live row must not dedup-
        merge into it: the replacement row stays its own row."""
        self.add(_POS_A)
        c = self.add("a separate live row about the pasta note of unrelated things")
        r = self.run_store("update", "--id", c, "--content", _NEG_B)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = self.db()
        live = conn.execute(
            "SELECT id FROM memory WHERE superseded_at IS NULL").fetchall()
        self.assertEqual(len(live), 2,
                         "contradicting update must not merge into the "
                         "contradicted row")


class RecallExpansionTest(_Store):
    def _seed_linked_pair(self):
        """One query-matching row linked to one neighbor that does NOT match
        the query's distinctive terms (so it can only surface via expansion)."""
        a = self.add("kubernetes sidecar containers restart when their liveness "
                     "probe fails repeatedly during rollout windows",
                     tags="k8s,ops")
        b = self.add("kubernetes sidecar containers restart when their readiness "
                     "probe fails repeatedly during rollout windows",
                     tags="k8s")
        return a, b

    def test_default_expansion_appends_linked_neighbor(self):
        a, b = self._seed_linked_pair()
        # "liveness" matches ONLY a (b says readiness) — so b can surface
        # ONLY through the 1-hop walk, never as a query match.
        rows = self.recall(query="liveness")
        mains = [r for r in rows if "link_relation" not in r]
        extras = [r for r in rows if "link_relation" in r]
        self.assertTrue(any(r["id"] == a for r in mains), rows)
        self.assertEqual([r["id"] for r in mains], [a],
                         "the readiness row must not match the query itself")
        exp = [r for r in extras if r["id"] == b]
        self.assertEqual(len(exp), 1, rows)
        self.assertEqual(exp[0]["link_relation"], "related")
        self.assertEqual(exp[0]["link_of"], a)
        self.assertFalse(exp[0]["contested_link"])
        self.assertGreaterEqual(exp[0]["link_score"], 0.75)

    def test_expansion_never_duplicates_main_results(self):
        self._seed_linked_pair()
        rows = self.recall(query="probe rollout windows", limit=5)
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), rows)

    def test_link_hops_zero_disables_expansion(self):
        self._seed_linked_pair()
        rows = self.recall("--link-hops", "0",
                           query="liveness probe rollout windows")
        self.assertTrue(rows)
        self.assertTrue(all("link_relation" not in r for r in rows), rows)

    def test_link_budget_zero_disables_expansion(self):
        self._seed_linked_pair()
        rows = self.recall("--link-budget", "0",
                           query="liveness probe rollout windows")
        self.assertTrue(rows)
        self.assertTrue(all("link_relation" not in r for r in rows), rows)

    def test_budget_caps_extra_rows(self):
        # Three mutually-linked neighbors of one main row: budget 2 -> at
        # most 2 extra rows total (not 2 per result row).
        self.add("curated deployment notes about canary percentage ramp "
                 "settings for service mesh rollouts", tags="deploy")
        self.add("curated deployment notes about canary percentage ramp "
                 "tuning for service mesh rollouts", tags="deploy")
        self.add("curated deployment notes about canary percentage ramp "
                 "limits for service mesh rollouts", tags="deploy")
        self.add("curated deployment notes about canary percentage ramp "
                 "stages for service mesh rollouts", tags="deploy")
        rows = self.recall(query="canary percentage ramp settings rollouts",
                           limit=1)
        extras = [r for r in rows if "link_relation" in r]
        self.assertLessEqual(len(extras), 2, rows)

    def test_contested_neighbor_floor_and_tag(self):
        """A contradicts neighbor surfaces ONLY above the confidence floor,
        tagged contested_link (JSON) / [CONTESTED LINK] (fenced text).

        PRR-002 (swarm PR review): the assertions are RANK-AGNOSTIC and
        UNCONDITIONAL — execution showed the `never` row reliably wins the
        main slot, so the original `if contested:` guards (keyed on the
        other row) skipped on every run and pinned nothing."""
        a = self.add("the cache ttl setting should always be ten minutes "
                     "for the rate limiter service", tags="cache")
        b = self.add("the cache ttl setting should never be ten minutes "
                     "for the rate limiter service", tags="cache")
        # Both a and b match the query; limiting to 1 leaves exactly one
        # main and (if expansion works) its contradicted partner as the
        # single extra — whichever way the ranking breaks.
        rows = self.recall(query="cache ttl rate limiter service", limit=1)
        mains = [r for r in rows if "link_relation" not in r]
        extras = [r for r in rows if "link_relation" in r]
        self.assertEqual(len(mains), 1, rows)
        self.assertEqual(len(extras), 1, rows)
        main, extra = mains[0], extras[0]
        contested_id = b if main["id"] == a else a
        self.assertEqual(extra["id"], contested_id,
                         "the contradicted partner must be the expansion row")
        self.assertTrue(extra["contested_link"],
                        "contradicts expansion rows must carry "
                        "contested_link=true")
        self.assertEqual(extra["link_relation"], "contradicts")
        self.assertEqual(extra["link_of"], main["id"])
        # Fenced render tags the contested neighbor — unconditionally.
        r = self.run_store("recall", "--query", "cache ttl rate limiter",
                           "--namespace", "project:links", "--limit", "1")
        self.assertIn("[CONTESTED LINK]", r.stdout, r.stdout)
        # Below the floor, the contradicts neighbor is dropped from expansion
        # — whichever row was the contested extra. If the floor gate broke,
        # it would resurface as the (now-main) partner's contested extra.
        conn = self.db()
        conn.execute("UPDATE memory SET confidence=0.1 WHERE id=?",
                     (contested_id,))
        conn.commit()
        rows = self.recall(query="cache ttl rate limiter service", limit=1)
        extras = [r for r in rows if "link_relation" in r]
        self.assertNotIn(contested_id, [r["id"] for r in extras],
                         "contradicts neighbor below the confidence floor "
                         "must not expand")

    def test_as_of_excludes_neighbors_invalid_at_instant(self):
        a = self.add("the feature flag table schema landed with a nullable "
                     "default column for rollout defaults", tags="flags")
        b = self.add("the feature flag table schema landed with a nullable "
                     "default column for rollout experiments", tags="flags")
        conn = self.db()
        b_valid_from = conn.execute(
            "SELECT valid_from FROM memory WHERE id=?", (b,)).fetchone()[0]
        # Tombstone b NOW (valid_until = now): at an as-of AFTER that instant
        # b is dead and must not expand; before it, b was alive.
        self.run_store("supersede", "--id", b, "--reason", "no longer true")
        b_until = self.db().execute(
            "SELECT valid_until FROM memory WHERE id=?", (b,)).fetchone()[0]
        later = self._bump_iso(b_until, +3600)
        rows = self.recall("--as-of", later,
                           query="feature flag table schema rollout defaults", limit=1)
        extras = [r for r in rows if "link_relation" in r]
        self.assertNotIn(b, [r["id"] for r in extras],
                         "a neighbor invalid at the as-of instant must not "
                         "expand")
        earlier = self._bump_iso(b_valid_from, +1)
        if earlier < b_until:
            rows = self.recall("--as-of", earlier,
                           query="feature flag table schema rollout defaults",
                           limit=1)
            extras = [r for r in rows if "link_relation" in r]
            self.assertIn(b, [r["id"] for r in extras],
                          "a neighbor valid at the as-of instant must expand")

    def test_expansion_stays_inside_the_tier_namespace(self):
        """A linked neighbor in ANOTHER namespace must never surface in a
        scoped recall (links never cross namespaces at write time; the
        expansion fetch re-enforces it)."""
        self.add("the migration guide says run backups before schema swaps "
                 "on the primary database", ns="project:links")
        self.add("the migration guide says run backups before schema swaps "
                 "on the replica database", ns="project:links")
        self.add("the migration guide says run backups before schema swaps "
                 "on the primary database", ns="project:other")
        rows = self.recall(query="backups before schema swaps primary",
                           limit=1)
        for r in rows:
            self.assertEqual(r["namespace"], "project:links")

    @staticmethod
    def _bump_iso(ts: str, seconds: int) -> str:
        from datetime import datetime, timedelta, timezone
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        dt += timedelta(seconds=seconds)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class LinksCliTest(_Store):
    def test_links_list_missing_id_exits_one_like_get(self):
        self.run_store("init")
        r = self.run_store("links", "--id", "10000000-0000-4000-8000-000000000001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[zmem] no memory with id", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_links_list_json_shape(self):
        a = self.add(_POS_A)
        self.add(_POS_B)
        r = self.run_store("links", "--id", a, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        edges = json.loads(r.stdout)
        self.assertTrue(edges)
        self.assertEqual(len(edges), 2, edges)
        for e in edges:
            self.assertEqual(e["relation"], "related")
            self.assertIn(e["direction"], ("out", "in"))
            # `other` is always the FAR endpoint; direction follows src.
            self.assertNotEqual(e["other"], a)
            self.assertEqual(e["direction"] == "out", e["src"] == a)
            self.assertIsInstance(e["score"], float)
            self.assertTrue(e["created_at"])
        # exactly one out (a->b) and one in (b->a)
        self.assertEqual(
            sorted(e["direction"] for e in edges), ["in", "out"])

    def test_links_add_supports_applies_trust_delta(self):
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        # PRR-001: supports adjusts trust, so --reason is REQUIRED.
        r = self.run_store("links", "--add", "--id", a, "--id", b,
                           "--relation", "supports")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--reason is required", r.stderr)
        r = self.run_store("links", "--add", "--id", a, "--id", b,
                           "--relation", "supports",
                           "--reason", "both verified in CI logs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertAlmostEqual(self.trust(a), 1.0)  # clamped from 1.05
        self.assertAlmostEqual(self.trust(b), 1.0)
        # A SECOND supports event between the same pair is idempotent.
        r = self.run_store("links", "--add", "--id", a, "--id", b,
                           "--relation", "supports",
                           "--reason", "re-confirmed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertAlmostEqual(self.trust(a), 1.0)

    def test_links_add_supports_delta_lowers_from_reduced_trust(self):
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        self.run_store("contradict", "--id", a, "--id", b, "--reason", "x")
        self.assertAlmostEqual(self.trust(a), 0.9)
        self.run_store("links", "--add", "--id", a, "--id", b,
                       "--relation", "supports", "--reason", "corroborated")
        self.assertAlmostEqual(self.trust(a), 0.95)

    def test_links_add_typed_relations_are_directed_and_visible(self):
        """Every relation enum value must be storable AND readable back via
        links --json (the issue's no-dead-enum rule)."""
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        for rel in ("updates", "extends", "derives"):
            r = self.run_store("links", "--add", "--id", a, "--id", b,
                               "--relation", rel)
            self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr}")
        r = self.run_store("links", "--id", a, "--json")
        edges = json.loads(r.stdout)
        rels = sorted(e["relation"] for e in edges)
        self.assertEqual(rels, ["derives", "extends", "updates"])
        # Typed relations keep their ONE authored direction.
        self.assertTrue(all(e["direction"] == "out" for e in edges))
        # No trust event for typed relations.
        self.assertEqual(self.trust(a), 1.0)
        self.assertEqual(self.trust(b), 1.0)

    def test_links_add_refusals(self):
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        c = self.add(_POS_A, ns="project:other")
        # Self-link refused (fail-closed write) — exit 1, stable stderr, no
        # traceback (the `get` contract, PR-review R2).
        r = self.run_store("links", "--add", "--id", a, "--id", a,
                           "--relation", "related")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        # Cross-namespace refused, same contract.
        r = self.run_store("links", "--add", "--id", a, "--id", c,
                           "--relation", "related")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cross-namespace", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        # Missing id refused with the stable get-style line, exit 1.
        r = self.run_store("links", "--add", "--id", a,
                           "--id", "10000000-0000-4000-8000-000000000003",
                           "--relation", "related")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[zmem] no memory with id", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(self.edges()), 0)
        # Bad arity refused with exit 2 (argparse convention).
        r = self.run_store("links", "--add", "--id", a, "--relation", "related")
        self.assertEqual(r.returncode, 2)
        # PRR-001: contradicts/supports via --add without --reason refused.
        r = self.run_store("links", "--add", "--id", a, "--id", b,
                           "--relation", "contradicts")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--reason is required", r.stderr)
        self.assertEqual(len(self.edges()), 0, "nothing written on refusal")
        # With a reason it works (and carries the trust event).
        r = self.run_store("links", "--add", "--id", a, "--id", b,
                           "--relation", "contradicts",
                           "--reason", "conflicting guidance observed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertAlmostEqual(self.trust(a), 0.9)
        del b

    def test_links_single_id_more_than_one_refused(self):
        self.run_store("init")
        r = self.run_store("links", "--id", "10000000-0000-4000-8000-000000000001",
                           "--id", "10000000-0000-4000-8000-000000000002")
        self.assertEqual(r.returncode, 2)

    def test_contradict_missing_reason_refused_by_argparse(self):
        a = self.add(_POS_A)
        b = self.add(_UNRELATED)
        r = self.run_store("contradict", "--id", a, "--id", b)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.edges()), 0, "nothing written on refusal")

    def test_contradict_missing_id_exits_one(self):
        self.add(_POS_A)
        r = self.run_store("contradict", "--id", "10000000-0000-4000-8000-000000000009",
                           "--id", "10000000-0000-4000-8000-000000000008",
                           "--reason", "whatever")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[zmem] no memory with id", r.stderr)

    def test_contradict_happy_path_never_merges_or_rewrites(self):
        a = self.add(_POS_A, tags="keepme")
        b = self.add(_UNRELATED)
        before = self.db().execute(
            "SELECT id, content, confidence, signal, tags FROM memory "
            "ORDER BY id").fetchall()
        r = self.run_store("contradict", "--id", a, "--id", b,
                           "--reason", "opposite guidance observed")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.db().execute(
            "SELECT id, content, confidence, signal, tags FROM memory "
            "ORDER BY id").fetchall()
        self.assertEqual(before, after,
                         "contradict must not merge, delete, or rewrite "
                         "either row")
        self.assertAlmostEqual(self.trust(a), 0.9)
        self.assertAlmostEqual(self.trust(b), 0.9)
        # The reason is echoed (validated) but the schema has no column.
        self.assertIn("opposite guidance observed", r.stdout)
        conn = self.db()
        self.assertFalse(any(
            "opposite guidance" in (col[1] or "") for col in conn.execute(
                "PRAGMA table_info(memory_link)").fetchall()))


class TrustClampTest(unittest.TestCase):
    """Direct unit coverage of the clamp (in-process, on a THROWAWAY store —
    never the operator default: opening the default store with newer code
    auto-migrates it, which is exactly the schema-skew failure mode the
    SCHEMA-SKEW runbook exists for)."""

    def test_adjust_trust_clamps_both_directions(self):
        tmp = tempfile.mkdtemp(prefix="zmem-trust-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # Fresh module load pinned to the scratch store via env (the
        # storelib singleton resolves STORE_PATH at import; a bare
        # `import storelib` here would inherit whatever env the process
        # started with).
        import importlib.util
        env = {
            "ZMEM_STORE": str(Path(tmp) / "store.sqlite"),
            "ZMEM_MODELS_DIR": str(Path(tmp) / "no-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            spec = importlib.util.spec_from_file_location(
                "zmem_links_trust_unit", str(STORE_PY))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        conn = mod.connect()
        mod.init_db(conn)
        mod.migrate(conn)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES ('m','ns','fact','x','2026-01-01T00:00:00Z')")
        adjust = mod.adjust_trust
        adjust(conn, "m", -5.0)
        self.assertEqual(conn.execute(
            "SELECT trust_score FROM memory WHERE id='m'").fetchone()[0], 0.0)
        adjust(conn, "m", +0.05)
        self.assertAlmostEqual(conn.execute(
            "SELECT trust_score FROM memory WHERE id='m'").fetchone()[0], 0.05)
        adjust(conn, "m", +5.0)
        self.assertEqual(conn.execute(
            "SELECT trust_score FROM memory WHERE id='m'").fetchone()[0], 1.0)


class VecPathTest(_Store):
    """The embedding-cosine link path (model-absent suite: skipped unless
    sqlite-vec is importable — the same guard as test_namespace_knn)."""

    def test_vec_neighbors_link(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self.skipTest("sqlite-vec not importable in this environment")

        import struct
        sys.path.insert(0, str(SCRIPTS_DIR))
        os.environ.update(
            ZMEM_STORE=self.store,
            ZMEM_MODELS_DIR=str(Path(self.tmp) / "no-models"),
            ZMEM_MODEL_AUTODOWNLOAD="0",
        )
        import storelib
        storelib._refresh_env_state()
        from storelib.schema import connect, init_db, migrate
        conn = connect()
        init_db(conn)
        migrate(conn)

        def _vec(seed: float) -> bytes:
            # 384-dim unit-ish vectors: (1,0,...) vs (0.8,0.6,0,...) have
            # cosine ~0.80 — above the 0.75 link threshold, BELOW the 0.85
            # dedup threshold, so the second add links instead of merging.
            dims = [0.0] * 384
            dims[0] = 0.8
            dims[1] = 0.6
            return struct.pack("384f", *([1.0] + [0.0] * 383 if seed == 0 else dims))

        class _StubEmbeddings:
            available = True

            def is_available(self):
                return True

            def embed_text(self, text):
                return _vec(0 if "alpha" in text else 1)

        # CPython does not honor module __setattr__ across re-exports: the
        # stub must be installed on the OWNING submodule (write), which reads
        # `_embeddings` as a bare global (memory lesson from PR #68).
        from storelib.write import add_memory
        from storelib import write as write_mod
        old = write_mod._embeddings
        write_mod._embeddings = _StubEmbeddings()
        try:
            a = add_memory(conn, namespace="project:vec", type_="fact",
                           content="identical semantic payload alpha",
                           signal="test")
            b = add_memory(conn, namespace="project:vec", type_="fact",
                           content="identical semantic payload beta",
                           signal="test")
        finally:
            write_mod._embeddings = old
        edges = conn.execute(
            "SELECT src_id, dst_id, relation FROM memory_link").fetchall()
        rels = {(e[0], e[1], e[2]) for e in edges}
        self.assertIn((a, b, "related"), rels)
        self.assertIn((b, a, "related"), rels)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
