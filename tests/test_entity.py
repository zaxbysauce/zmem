"""Entity identity tests (schema v10, issue #60).

Covers: the three entity tables + their UNIQUE/CHECK constraints, the
deterministic extractor (acceptance AND negative cases from the issue), the
third RRF list at recall (model-absent by construction — ZMEM_MODELS_DIR
points at a nonexistent dir), entity cards on recall JSON / get / the fenced
hook render, the entity-list and entity-merge CLI surfaces, re-derivation at
the non-insert mutation sites (dedup merge, update, rekey), the JSONL
rebuild-on-ingest decision (two distinct stores derive the same entities),
the v9→v10 migration backfill, and the no-auto-merge rule for people.

Run: python tests/test_entity.py  (no pytest; house convention)
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

# Schema-version ratchets are DYNAMIC (v11, issue #61): the suite asserts the
# store lands on schema_meta's current version instead of pinning a number
# that goes stale on every bump (the pattern the v10 PR left behind for its
# own two ratchets).
sys.path.insert(0, str(SCRIPTS_DIR))
from schema_meta import ENTITY_KINDS, SUPPORTED_SCHEMA_VERSION  # noqa: E402


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    # Issue #179 (Files-to-change note): the isolated-store helper sets
    # ZMEM_DATA (alongside ZMEM_STORE / ZMEM_MODELS_DIR /
    # ZMEM_MODEL_AUTODOWNLOAD) so the store's data-directory side state can
    # never leak to or from the operator's ~/.zmem.
    env["ZMEM_DATA"] = str(Path(store_path).parent)
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_MMR_LAMBDA", None)
    # Issue #176 review: the byte-oracle assertions below assume DEFAULT
    # entity-kind weights; an ambient override would silently rewrite them.
    env.pop("ZMEM_ENTITY_KIND_WEIGHTS", None)
    return env


class _Store(unittest.TestCase):
    """Subprocess-driven house pattern: every command is the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-entity-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_store_bytes(self, *args, env=None, check=True):
        """Raw-bytes capture for byte-oracle assertions. `text=True` applies
        universal-newline decoding, which rewrites a CRLF-terminated payload
        to LF BEFORE any comparison, so the LF-only byte contract could not
        fail its own test (issue #176 review)."""
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, env=env or self.env, timeout=120,
        )
        if check and r.returncode != 0:
            self.fail(
                f"store.py {' '.join(args)} exited {r.returncode}\n"
                f"stdout: {r.stdout[:500]!r}\nstderr: {r.stderr[:500]!r}"
            )
        return r

    def run_store(self, *args, env=None, check=True):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=env or self.env, timeout=120,
        )
        if check and r.returncode != 0:
            self.fail(
                f"store.py {' '.join(args)} exited {r.returncode}\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )
        return r

    def db(self):
        conn = sqlite3.connect(self.store)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def add(self, content, *extra, ns="project:ent", check=True):
        # --signal user (conf 0.6) keeps every fixture above the 0.25 recall
        # floor; signal=none defaults to 0.2 and the lane would drop the row.
        return self.run_store(
            "add", "--namespace", ns, "--type", "lesson",
            "--content", content, "--signal", "user", *extra, check=check,
        )

    def entity_json(self):
        return json.loads(self.run_store("entity-list", "--json").stdout)

    def recall(self, query, *extra, ns="project:ent"):
        out = self.run_store(
            "recall", "--query", query, "--namespace", ns,
            "--json", "--limit", "10", "--no-hybrid", *extra,
        ).stdout
        parsed = json.loads(out)
        # v13 (issue #65, 10.8): recall --json emits the read envelope.
        return parsed["results"] if isinstance(parsed, dict) else parsed


class SchemaTest(_Store):
    def test_v16_entity_kind_check_accepts_all_twelve(self):
        self.add("row to build the store")
        conn = self.db()
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity'"
        ).fetchone()[0]
        self.assertEqual(set(V2_KINDS), set(V2_KIND_PAIRS),
                         "schema_meta.ENTITY_KINDS drifted from the v2 "
                         "twelve-kind contract (issue #175)")
        for kind in V2_KIND_PAIRS:
            self.assertIn(f"'{kind}'", ddl,
                          f"fresh entity CHECK must name the v2 kind {kind!r}")
        for kind in V2_KINDS:
            conn.execute(
                "INSERT INTO entity (id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)",
                (f"probe-{kind}", kind, kind, FIXED_TS, FIXED_TS),
            )
        conn.rollback()


    def test_v10_tables_and_constraints(self):
        self.run_store("init")
        conn = self.db()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(ver, str(SUPPORTED_SCHEMA_VERSION))
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for t in ("entity", "entity_alias", "memory_entity"):
            self.assertIn(t, tables)
        # GLOBAL alias uniqueness — the paraphrase-same-ids contract.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO entity(id, kind, canonical_name, created_at, updated_at) "
                "VALUES ('e1','tool','a','t','t')"
            )
            conn.execute(
                "INSERT INTO entity(id, kind, canonical_name, created_at, updated_at) "
                "VALUES ('e2','tool','b','t','t')"
            )
            conn.execute("INSERT INTO entity_alias VALUES ('e1','dup')")
            conn.execute("INSERT INTO entity_alias VALUES ('e2','dup')")
        conn.rollback()
        # Per-(memory, entity) link uniqueness.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO memory_entity VALUES ('m1','e1','mentions')")
            conn.execute("INSERT INTO memory_entity VALUES ('m1','e1','mentions')")
        conn.rollback()
        # kind CHECK is a closed five-value enum.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO entity(id, kind, canonical_name, created_at, updated_at) "
                "VALUES ('e9','robot','x','t','t')"
            )


class ExtractorAcceptanceTest(_Store):
    def test_issue_acceptance_ripgrep_and_project(self):
        self.add("use `ripgrep` not grep", "--tags", "search")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("ripgrep", ents)
        self.assertEqual(ents["ripgrep"]["kind"], "tool")
        # project:zmem namespace mints the project entity
        self.add("another row", ns="project:zmem")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("zmem", ents)
        self.assertEqual(ents["zmem"]["kind"], "project")

    def test_paraphrase_hits_same_entity_ids(self):
        self.add("use `ripgrep` not grep for search")
        first = [e for e in self.entity_json() if e["name"] == "ripgrep"]
        self.assertEqual(len(first), 1)
        # Different wording, same backticked identifier → same entity id.
        self.add("prefer `ripgrep` over plain grep when searching")
        again = [e for e in self.entity_json() if e["name"] == "ripgrep"]
        self.assertEqual(len(again), 1)
        self.assertEqual(first[0]["id"], again[0]["id"])
        self.assertEqual(again[0]["links"], 2)

    def test_stopwords_and_paths_are_not_entities(self):
        self.add("the and use it of", "--tags", "the,and,use")
        names = {e["name"] for e in self.entity_json()}
        self.assertEqual(
            names, {"ent"},
            "stopwords must never mint entities (only the namespace suffix)",
        )
        self.add(r"see C:\Temp\dir and /e/ZCode and `C:\Temp\dir` and https://x.io/y")
        names = {e["name"] for e in self.entity_json()}
        self.assertEqual(
            names, {"ent"},
            "path/URL-shaped tokens must never mint entities",
        )

    def test_explicit_entity_tags_and_person(self):
        self.add("entity:person:Kim reviewed ContentTooLarge here")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertEqual(ents["Kim"]["kind"], "person")
        self.assertEqual(ents["ContentTooLarge"]["kind"], "other")
        # entity:<kind>:Name in the tags field
        self.add("row two", "--tags", "entity:tool:fzf,entity:preference:dark-mode")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertEqual(ents["fzf"]["kind"], "tool")
        self.assertEqual(ents["dark-mode"]["kind"], "preference")

    def test_camelcase_rules(self):
        # Two-plus humps, uppercase-led: match. Lowercase-led (zmemStore) and
        # acronym-only (MySQL, OpenAI, FTS5): no match — documented rule.
        self.add("MemoryProvider and ZmemStore match; MySQL OpenAI FTS5 do not")
        names = {e["name"] for e in self.entity_json()}
        self.assertIn("MemoryProvider", names)
        self.assertIn("ZmemStore", names)
        self.assertNotIn("MySQL", names)
        self.assertNotIn("OpenAI", names)
        self.assertNotIn("FTS5", names)
        self.assertNotIn("zmemStore", names)

    def test_first_seen_kind_wins(self):
        # A plain tag mints other:rg; the later backticked tool mention must
        # NOT re-kind the existing entity (manual reconciliation is
        # entity-merge).
        self.add("row one", "--tags", "rg")
        self.add("row two mentions `rg` the tool")
        rg = [e for e in self.entity_json() if e["name"] == "rg"]
        self.assertEqual(len(rg), 1)
        self.assertEqual(rg[0]["kind"], "other")


class ThirdListTest(_Store):
    def test_alias_match_surfaces_bm25_missed_row(self):
        # Memory A mentions `ripgrep` (so BM25 matches "ripgrep" but NOT the
        # bare token "rg"); memory B (different namespace) mints the rg alias
        # via a kind-prefixed tag so the manual merge is kind-compatible.
        self.add("use `ripgrep` for fast code search", ns="project:lane")
        self.add("the launcher shorthand row", "--tags", "tool:rg", ns="project:other")
        ents = {e["name"]: e for e in self.entity_json()}
        rg_id = ents["rg"]["id"]
        ripgrep_id = ents["ripgrep"]["id"]
        # Manual merge: rg -> ripgrep moves the alias to the ripgrep entity.
        self.run_store("entity-merge", "--from", rg_id, "--to", ripgrep_id,
                       "--confirm")
        # Query "rg" cannot FTS-match A ("ripgrep" is one unicode61 token),
        # but the entity lane resolves alias rg -> ripgrep -> memory A.
        rows = self.recall("rg", ns="project:lane")
        ids_contents = [r["content"] for r in rows]
        self.assertTrue(
            any("ripgrep" in c for c in ids_contents),
            f"entity lane must surface the BM25-missed row; got {ids_contents}",
        )

    def test_unknown_alias_is_empty_no_crash(self):
        self.add("some anchored content")
        rows = self.recall("zzznope zzznothing")
        self.assertIsInstance(rows, list)  # no crash; other lanes fuse as-is

    def test_namespace_scoping(self):
        self.add("use `ripgrep` here", ns="project:lane")
        self.add("unrelated row", ns="project:elsewhere")
        # "ripgrep" matches nothing in project:elsewhere — not via FTS, and
        # the entity lane's tier namespace filter must not leak the
        # project:lane row in either.
        rows = self.recall("ripgrep", ns="project:elsewhere")
        self.assertEqual(rows, [])

    def test_as_of_reaches_tombstoned_linked_row(self):
        self.add("use `ripgrep` daily")
        line = self.run_store("list", "--namespace", "project:ent").stdout.strip()
        mid = line.split("]")[0].lstrip("[")
        # Pin a controlled past valid_from: now_iso() has SECOND granularity,
        # so an unpinned add+invalidate in the same second would make
        # as_of == valid_from == valid_until and the EXCLUSIVE bound would
        # (correctly) exclude the row — a flake, not a product guarantee.
        pinned = "2026-01-01T00:00:00Z"
        conn = self.db()
        conn.execute(
            "UPDATE memory SET valid_from=? WHERE id=?", (pinned, mid))
        conn.commit()
        conn.close()
        self.run_store("invalidate", "--id", mid, "--reason", "test tombstone")
        # At pinned valid_from (inclusive) the row was valid: the entity lane
        # + temporal predicate must surface it; a far-future as-of must not
        # (valid_until == the tombstone instant is EXCLUSIVE).
        seen = self.recall("ripgrep", "--as-of", pinned)
        self.assertTrue(any(r["id"] == mid for r in seen),
                        "as-of at valid_from must reach the linked row")
        later = self.recall("ripgrep", "--as-of", "2099-01-01T00:00:00Z")
        self.assertFalse(any(r["id"] == mid for r in later),
                         "as-of after valid_until must not resurrect the row")


class EntityCardsTest(_Store):
    def test_recall_json_cards_and_get(self):
        self.add("use `ripgrep` and `fzf` with entity:person:Kim", ns="project:cards")
        rows = self.recall("ripgrep", ns="project:cards")
        self.assertTrue(rows)
        ents = rows[0]["entities"]
        self.assertIsInstance(ents, list)
        kinds = {e["kind"] for e in ents}
        self.assertIn("tool", kinds)
        self.assertIn("person", kinds)
        for e in ents:
            self.assertEqual(set(e.keys()), {"id", "kind", "name"})

    def test_fence_shows_at_most_three_names(self):
        self.add("`a1` `b2` `c3` `d4` `e5` tools galore", ns="project:fence")
        text = self.run_store(
            "recall", "--query", "a1", "--namespace", "project:fence",
            "--limit", "3",
        ).stdout
        self.assertIn("entities: ", text)
        for line in text.splitlines():
            if line.strip().startswith("entities:"):
                shown = [p.strip() for p in line.split(":", 1)[1].split(",")]
                self.assertLessEqual(len(shown), 3)
                self.assertNotIn("[", line)  # names, never ids

    def test_get_json_carries_entities(self):
        self.add("mentions `ripgrep` only")
        line = self.run_store("list", "--namespace", "project:ent").stdout.strip()
        mid = line.split("]")[0].lstrip("[")
        d = json.loads(self.run_store("get", "--id", mid).stdout)
        names = {e["name"] for e in d["entities"]}
        self.assertIn("ripgrep", names)
        self.assertIn("ent", names)  # project namespace suffix


class EntityListTest(_Store):
    def test_human_and_json_and_kind_filter(self):
        self.run_store("init")
        # "zebra" classifies to `other`; since issue #176 the plain tag
        # "python" would classify to `command` (runner head), so this test
        # uses a neutral token to keep asserting the kind=other row shape.
        self.add("use `ripgrep`", "--tags", "zebra")
        out = self.run_store("entity-list").stdout
        self.assertIn("kind=tool", out)
        self.assertIn("kind=project", out)
        self.assertIn("kind=other", out)
        items = self.entity_json()
        for it in items:
            self.assertEqual(
                set(it.keys()), {"id", "kind", "name", "aliases", "links"}
            )
        tools = json.loads(
            self.run_store("entity-list", "--kind", "tool", "--json").stdout
        )
        self.assertTrue(tools and all(t["kind"] == "tool" for t in tools))

    def test_empty_store_lists_nothing(self):
        self.run_store("init")
        self.assertIn("(no entities)", self.run_store("entity-list").stdout)
        self.assertEqual(self.entity_json(), [])

    def test_help_documents_the_contract(self):
        for sub in ("entity-list", "entity-merge"):
            r = self.run_store(sub, "--help")
            self.assertEqual(r.returncode, 0)
        self.assertIn("--confirm", self.run_store("entity-merge", "--help").stdout)
        self.assertIn("--kind", self.run_store("entity-list", "--help").stdout)


class EntityMergeTest(_Store):
    def _ids_by_name(self):
        return {e["name"]: e["id"] for e in self.entity_json()}

    def test_dry_run_default_writes_nothing(self):
        self.add("use `ripgrep` here")
        self.add("shorthand `rg` row")
        before = self._ids_by_name()
        r = self.run_store("entity-merge", "--from", before["rg"],
                           "--to", before["ripgrep"])
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("no writes", r.stdout)
        after = self._ids_by_name()
        self.assertEqual(before, after, "dry run must not write")
        conn = self.db()
        n = conn.execute("SELECT count(*) FROM entity").fetchone()[0]
        self.assertEqual(n, len(before))

    def test_confirm_moves_and_deletes(self):
        self.add("use `ripgrep` here", ns="project:m1")
        self.add("alias `rg` row", ns="project:m1")
        ids = self._ids_by_name()
        r = self.run_store("entity-merge", "--from", ids["rg"],
                           "--to", ids["ripgrep"], "--confirm")
        self.assertEqual(r.returncode, 0)
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertNotIn("rg", ents, "from-entity must be deleted")
        merged = ents["ripgrep"]
        self.assertIn("rg", merged["aliases"], "alias must move to the target")
        self.assertEqual(merged["links"], 2, "links must move to the target")
        # recall via the merged alias resolves both memories
        rows = self.recall("rg", ns="project:m1")
        self.assertEqual(len(rows), 2)

    def test_refusals(self):
        self.add("use `ripgrep` here")
        self.add("ns row", ns="project:zmem")
        ids = self._ids_by_name()
        # kind mismatch (tool -> project)
        r = self.run_store("entity-merge", "--from", ids["ripgrep"],
                           "--to", ids["zmem"], "--confirm", check=False)
        self.assertEqual(r.returncode, 2)
        # unknown ids
        r = self.run_store("entity-merge", "--from", "nope", "--to", ids["ripgrep"],
                           "--confirm", check=False)
        self.assertEqual(r.returncode, 2)
        r = self.run_store("entity-merge", "--from", ids["ripgrep"], "--to", "nope",
                           "--confirm", check=False)
        self.assertEqual(r.returncode, 2)
        # same id
        r = self.run_store("entity-merge", "--from", ids["ripgrep"],
                           "--to", ids["ripgrep"], "--confirm", check=False)
        self.assertEqual(r.returncode, 2)
        # nothing was written by any refusal
        self.assertEqual(len(self.entity_json()), len(ids))

    def test_manual_person_merge_allowed_with_confirm(self):
        self.add("entity:person:Kim wrote it", ns="project:pp")
        self.add("entity:person:Kimberly signed off", ns="project:pp")
        ids = self._ids_by_name()
        r = self.run_store("entity-merge", "--from", ids["Kimberly"],
                           "--to", ids["Kim"], "--confirm")
        self.assertEqual(r.returncode, 0)
        names = set(self._ids_by_name())
        self.assertNotIn("Kimberly", names)

    def test_no_auto_merge_of_people(self):
        # Two person entities that share an alias_norm-normalized STEM but
        # differ as authored must stay separate — nothing ever auto-merges.
        self.add("entity:person:Alice prime", ns="project:pp2")
        self.add("entity:person:Bob second", ns="project:pp2")
        people = [e for e in self.entity_json() if e["kind"] == "person"]
        self.assertEqual(len(people), 2)


class MutationSitesTest(_Store):
    def test_dedup_merge_rederives_keeper_links(self):
        # Same content (exact content_norm dedup hit), new tag on the re-add:
        # the keeper unions the tags and must gain the new entity link.
        self.add("identical content line", "--tags", "python")
        self.add("identical content line", "--tags", "rust")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("python", ents)
        self.assertIn("rust", ents)
        self.assertEqual(ents["python"]["links"], 1)
        self.assertEqual(ents["rust"]["links"], 1)

    def test_update_links_new_row_and_keeps_history(self):
        self.add("original mentions `toolfoo`")
        line = self.run_store("list", "--namespace", "project:ent").stdout.strip()
        mid = line.split("]")[0].lstrip("[")
        self.run_store("update", "--id", mid,
                       "--content", "replacement mentions `toolbar` instead")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("toolfoo", ents)
        self.assertIn("toolbar", ents)
        conn = self.db()
        old_links = conn.execute(
            "SELECT count(*) FROM memory_entity me JOIN memory m ON m.id=me.memory_id "
            "WHERE m.id=? AND m.superseded_at IS NOT NULL", (mid,)
        ).fetchone()[0]
        self.assertGreater(old_links, 0, "tombstoned row keeps links for --as-of")

    def test_rekey_rederives_namespace_entity(self):
        self.add("row in old namespace", ns="project:oldkey")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("oldkey", ents)
        self.run_store("rekey-namespace", "--from", "project:oldkey",
                       "--to", "project:newkey", "--confirm")
        ents = {e["name"]: e for e in self.entity_json()}
        self.assertIn("newkey", ents)
        conn = self.db()
        stale = conn.execute(
            "SELECT count(*) FROM memory_entity me JOIN entity e ON e.id=me.entity_id "
            "JOIN memory m ON m.id=me.memory_id "
            "WHERE e.canonical_name='oldkey' AND m.superseded_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(stale, 0, "live rows must not keep the dead project link")

    def test_ingest_rebuilds_entities_two_store_parity(self):
        self.add("use `ripgrep` and entity:person:Kim here", "--tags", "python")
        export = str(Path(self.tmp) / "export.jsonl")
        self.run_store("export-jsonl", "--out", export)
        # Second, distinct store ingests the file: same entities derived,
        # different store-local ids, identical alias_norm set.
        store2 = str(Path(self.tmp) / "store2.sqlite")
        env2 = _base_env(store2)
        self.run_store("init", env=env2)
        self.run_store("ingest-jsonl", "--in", export, env=env2)
        e1 = json.loads(self.run_store("entity-list", "--json").stdout)
        r2 = subprocess.run(
            [PYTHON, str(STORE_PY), "entity-list", "--json"],
            capture_output=True, text=True, env=env2, timeout=120,
        )
        e2 = json.loads(r2.stdout)
        a1 = sorted(a for e in e1 for a in e["aliases"])
        a2 = sorted(a for e in e2 for a in e["aliases"])
        self.assertEqual(a1, a2, "two stores derive the same alias set")
        k1 = sorted((a, e["kind"]) for e in e1 for a in e["aliases"])
        k2 = sorted((a, e["kind"]) for e in e2 for a in e["aliases"])
        self.assertEqual(k1, k2, "kinds agree across stores")
        # The JSONL itself carries no entity fields (the documented decision).
        line = json.loads(Path(export).read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("entities", line)
        entity_keys = {k for k in line if "entit" in k.lower()}
        self.assertEqual(entity_keys, set(),
                         f"export rows must not carry entity fields: {entity_keys}")


GENERATOR = REPO_ROOT / "tests" / "fixtures" / "entity" / "generate_migration_expected.py"
EXPECTED = REPO_ROOT / "tests" / "fixtures" / "entity" / "migration-expected.json"
FIXED_TS = "2026-09-10T00:00:00Z"

# The v2 vocabulary is pinned against schema_meta's shared tuple (not a
# local literal) so a regression of ENTITY_KINDS itself flips this suite RED.
V2_KINDS = ENTITY_KINDS
V2_KIND_PAIRS = ("person", "project", "tool", "preference", "host", "service",
                 "model", "repo", "file", "command", "error_class", "other")


class V16SchemaTestMixin:
    """Shared helpers for the issue #175 v16 tests (seed/dump via the
    committed generator subprocess; store-building never runs in-process, so
    STORE_PATH freezes can never cross-contaminate)."""

    def seed_v15(self):
        """Seed the canonical v15 fixture store; returns (store, pre-state)."""
        scratch = Path(self.tmp)
        inp = scratch / "migration-input.jsonl"
        state = scratch / "pre-state.json"
        store = scratch / "v15-store.sqlite"
        r = subprocess.run(
            [PYTHON, str(GENERATOR), "--input", str(inp), "--out", str(state),
             "--store", str(store), "--phase", "seed"],
            capture_output=True, text=True, env=self.env, timeout=300,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return store, json.loads(
            state.read_text(encoding="utf-8"))["pre"]

    def dump_state(self, store):
        out = Path(self.tmp) / "post-state.json"
        r = subprocess.run(
            [PYTHON, str(GENERATOR), "--out", str(out), "--store", str(store),
             "--phase", "dump"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(out.read_text(encoding="utf-8"))["post"]

    def migrate_via_cli(self, store):
        """Any writable command migrates the store (the house trigger)."""
        return self.run_store("stats", env=_base_env(str(store)))


class MigrationTest(V16SchemaTestMixin, _Store):
    def test_v9_store_migrates_and_backfills(self):
        self.add("use `ripgrep` for search")
        self.add("plain tagged row", "--tags", "python")
        conn = self.db()
        n_mem = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        # Simulate a v9 store: drop the entity tables, roll the version back.
        conn.execute("DROP TABLE memory_entity")
        conn.execute("DROP TABLE entity_alias")
        conn.execute("DROP TABLE entity")
        conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        # Any writable command re-opens → migrate() v10 block runs.
        self.run_store("stats")
        conn = self.db()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(ver, str(SUPPORTED_SCHEMA_VERSION))
        self.assertEqual(
            conn.execute("SELECT count(*) FROM memory").fetchone()[0], n_mem,
            "migration must preserve every memory row",
        )
        names = {
            r[0] for r in conn.execute("SELECT canonical_name FROM entity")
        }
        self.assertIn("ripgrep", names)
        self.assertIn("python", names)
        self.assertIn("ent", names)
        # Idempotent re-run: nothing doubles.
        n_ent = conn.execute("SELECT count(*) FROM entity").fetchone()[0]
        n_link = conn.execute("SELECT count(*) FROM memory_entity").fetchone()[0]
        conn.close()
        self.run_store("stats")
        conn = self.db()
        self.assertEqual(conn.execute("SELECT count(*) FROM entity").fetchone()[0], n_ent)
        self.assertEqual(conn.execute("SELECT count(*) FROM memory_entity").fetchone()[0], n_link)


    def test_v15_to_v16_preserves_entity_alias_and_memory_entity_counts_and_sha(self):
        store, pre = self.seed_v15()
        self.migrate_via_cli(store)
        post = self.dump_state(store)
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        # Issue contract: the characterization runs PRAGMA foreign_key_check
        # after the committed migration AND (below) on the second no-op one.
        conn = sqlite3.connect(str(store))
        try:
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), [],
                "foreign_key_check must be empty after the committed migration")
        finally:
            conn.close()
        # Preservation: every count and canonical SHA survives the rebuild.
        for table in ("entity", "entity_alias", "memory_entity"):
            self.assertEqual(
                post["tables"][table]["count"], pre["tables"][table]["count"],
                f"{table} row count changed across the v16 rebuild")
            self.assertEqual(
                post["tables"][table]["sha256"], pre["tables"][table]["sha256"],
                f"{table} canonical SHA changed across the v16 rebuild")
            self.assertEqual(post["tables"][table]["rows"],
                             pre["tables"][table]["rows"])
        # Fixture identity: the regeneration matches the committed canonical
        # expected file (pre AND post).
        # PRR-028: the artifact records its input digest; pin it too, so a
        # hand-edited or CRLF-materialised input file cannot pass unnoticed.
        input_file = REPO_ROOT / "tests" / "fixtures" / "entity" / "migration-input.jsonl"
        import hashlib as _hashlib
        self.assertEqual(
            expected["input_sha256"],
            _hashlib.sha256(input_file.read_bytes()).hexdigest(),
            "migration-input.jsonl drifted from the digest recorded in "
            "migration-expected.json")
        self.assertEqual(pre, expected["pre"])
        self.assertEqual(post, expected["post"])
        # v17 (PR #385 review F-1/PRR-006, 2026-10-18): a v15 store the CLI
        # migrates walks THROUGH the v16 rebuild to the v17 entity_card
        # stamp (stamp-only, no DDL) — the fixture's post.schema_version and
        # this pin advanced 16 -> 17 together; the entity-table preservation
        # invariants above are the point of the test and are unchanged.
        self.assertEqual(post["schema_version"], 17)

        # PR #355 review PRR-007: the fixture's five legacy rows cannot prove
        # the MIGRATED table carries the new vocabulary — on a real v15 store
        # the legacy five-kind CHECK makes a v2-kind row unrepresentable (a
        # v15 client stored `host:acme-prod` as ONE `other` token), so the
        # widening is only observable AFTER the rebuild. Assert it here: a
        # migrated table still carrying the five-kind CHECK fails this.
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            for kind in V2_KIND_PAIRS:
                conn.execute(
                    "INSERT INTO entity (id, kind, canonical_name, created_at,"
                    " updated_at) VALUES (?,?,?,?,?)",
                    (f"post-migrate-{kind}", kind, kind, FIXED_TS, FIXED_TS),
                )
            conn.rollback()
        finally:
            conn.close()

        # PR #355 critic FINDING-7: a migrated store must REJECT a bogus kind
        # too, or a CHECK-removed mutant of the recreate DDL survives.
        conn = sqlite3.connect(str(store))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO entity (id, kind, canonical_name, created_at,"
                    " updated_at) VALUES (?,?,?,?,?)",
                    ("bogus-post-migrate", "bogus_v16", "bogus",
                     FIXED_TS, FIXED_TS),
                )
        finally:
            conn.close()

    def test_v16_migration_is_idempotent(self):
        store, _pre = self.seed_v15()
        self.migrate_via_cli(store)
        first = self.dump_state(store)
        self.migrate_via_cli(store)
        second = self.dump_state(store)
        self.assertEqual(first, second, "second migration changed state")
        # v17 (PR #385 review F-1, 2026-10-18): the terminal stamp is 17
        # (v16 entity rebuild + the stamp-only entity_card bump); a re-run
        # is a byte-identical no-op at 17.
        self.assertEqual(second["schema_version"], 17)
        conn = sqlite3.connect(str(store))
        try:
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_failed_v16_rebuild_rolls_back(self):
        store, pre = self.seed_v15()
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, "15")
            schema_bytes = sorted(
                (r["type"], r["name"], r["sql"]) for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'index')"
                )
            )
            n_entity = conn.execute(
                "SELECT count(*) FROM entity").fetchone()[0]
        finally:
            conn.close()

        # Deny DROP TABLE inside the rebuild: the migration must roll the
        # whole transaction back and leave v15 untouched.
        os.environ["ZMEM_STORE"] = str(store)
        os.environ["ZMEM_DATA"] = str(Path(self.tmp))
        os.environ["ZMEM_MODELS_DIR"] = str(Path(self.tmp) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        # PR #355 review PRR-015: these are process-global; restore them
        # so a later in-process test in this process is not redirected.
        _saved_env = {
            k: os.environ.get(k) for k in (
                "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")
        }

        def _restore_env():
            for _k, _v in _saved_env.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v

        self.addCleanup(_restore_env)
        from storelib import schema as schema_mod
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            def _deny_drop(action, arg1, arg2, db, trigger):
                if action == sqlite3.SQLITE_DROP_TABLE:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(_deny_drop)
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                schema_mod.migrate(conn)
            # TST-1: prove the authorizer fired (not some incidental error)
            # and that the transaction actually closed.
            self.assertIn("not authorized", str(caught.exception).lower())
            self.assertFalse(conn.in_transaction,
                             "the failed rebuild must leave no open transaction")
        finally:
            conn.set_authorizer(None)
            conn.close()

        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0], "15", "failed rebuild must leave version 15")
            after_bytes = sorted(
                (r["type"], r["name"], r["sql"]) for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'index')"
                )
            )
            self.assertEqual(after_bytes, schema_bytes,
                             "failed rebuild must leave table/index DDL "
                             "byte-identical")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM entity").fetchone()[0],
                n_entity, "failed rebuild must leave rows unchanged")
        finally:
            conn.close()

    def test_pre_v10_store_with_v2_kind_tag_backfills(self):
        """Finding-3 guardrail (plan-critic round 1): the v10 backfill runs
        with the SHARED 12-kind vocabulary, so a v2-kind tag on a legacy
        store must backfill — not brick the migration on the old CHECK.

        The migration is driven by DIRECT migrate() on a raw connection:
        every CLI path re-creates the entity table via init_db() before the
        versioned blocks run, which would mask the v10 DDL entirely (its
        twelve-kind shape is the backstop for exactly this direct caller)."""
        self.add("legacy row with v2 tag", "--tags", "host:acme-prod")
        conn = self.db()
        conn.execute("DROP TABLE memory_entity")
        conn.execute("DROP TABLE entity_alias")
        conn.execute("DROP TABLE entity")
        conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        os.environ["ZMEM_STORE"] = str(self.store)
        os.environ["ZMEM_DATA"] = str(Path(self.tmp))
        os.environ["ZMEM_MODELS_DIR"] = str(Path(self.tmp) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        _saved_env2 = {
            k: os.environ.get(k) for k in (
                "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")
        }

        def _restore_env2():
            for _k, _v in _saved_env2.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v

        self.addCleanup(_restore_env2)
        from storelib import schema as schema_mod
        conn = sqlite3.connect(str(self.store))
        conn.row_factory = sqlite3.Row
        try:
            schema_mod.migrate(conn)
        finally:
            conn.close()

        conn = self.db()
        try:
            ver = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(ver, str(SUPPORTED_SCHEMA_VERSION))
            kinds = {r[0] for r in conn.execute("SELECT kind FROM entity")}
            self.assertIn("host", kinds,
                          "v2-kind tag must backfill on a legacy store")
        finally:
            conn.close()


class ConstraintTest(_Store):
    """v16 SQL CHECK enforcement (issue #175 AC5)."""

    def test_bogus_v16_entity_kind_is_rejected_by_sql(self):
        self.add("row to build the store")
        conn = self.db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO entity (id, kind, canonical_name, created_at, "
                "updated_at) VALUES (?,?,?,?,?)",
                ("bogus-kind-probe", "bogus_v16", "bogus", FIXED_TS, FIXED_TS),
            )


class ClassifierScopeTest(_Store):
    """Issue #175 AC7: the classifier source is #176's, not this PR's."""

    ENTITY_PY = "skills/memory/scripts/storelib/entity.py"

    def test_v16_change_does_not_modify_entity_classifier_source(self):
        """PR #355 feedback PRR-002: the scope ratchet is narrowed from
        "entity.py is byte-unchanged" to "the CLASSIFIER is unchanged".

        The v2 vocabulary shipped with `_ENTITY_TAG_RE` still hardcoding the
        five-kind alternation, so `entity:host:web01` misparsed to
        ('other', 'host') and — because entity_alias.alias_norm is globally
        UNIQUE — every `entity:host:*` tag collapsed into ONE wrong-kind
        singleton with the real names discarded. Deriving that alternation from
        schema_meta.ENTITY_KINDS is a vocabulary-admission fix forced by THIS
        PR, not a classification-policy change; #176 still owns the policy.

        So this ratchet pins the classifier's decision order and kind-conflict
        policy (which #176 will change) instead of the file's bytes.
        """
        import storelib.entity as entity_mod

        # The extraction ORDER and the first-seen-kind-wins conflict policy
        # are #176's to change; they must be identical here.
        src = Path(entity_mod.__file__).read_text(encoding="utf-8")
        for anchor in (
            "def extract_entities(",
            "# 1) Explicit entity: tags",
            "# 2) project:<suffix> namespace.",
            "# 3) Remaining tag tokens",
            "# 4) Backtick-quoted spans in content",
            "# 5) CamelCase identifiers",
            "# Dedup by alias_norm, first-seen order",
            "FIRST-SEEN KIND WINS",
        ):
            self.assertIn(anchor, src,
                          f"classifier structure changed in entity.py: {anchor!r}")

        # The tag grammar must be DERIVED from the shared vocabulary, never a
        # hand-written copy (this is the defect the feedback round fixed).
        self.assertIn('"|".join(ENTITY_KINDS)', src,
                      "the entity: tag regex must derive its kind alternation "
                      "from schema_meta.ENTITY_KINDS")
        pairs = entity_mod.extract_entities("", "entity:host:web01", "")
        self.assertEqual(pairs, [("host", "web01")],
                         "every v2 kind must round-trip through the "
                         "entity:<kind>:<name> grammar")
        for kind in V2_KIND_PAIRS:
            self.assertEqual(
                entity_mod.extract_entities("", f"entity:{kind}:name1", ""),
                [(kind, "name1")],
                f"{kind!r} must survive the entity:<kind>:<name> grammar")

class ClassifierAcceptanceTest(_Store):
    """Issue #176: the deterministic classifier and its acceptance fixture."""

    FIXTURE = REPO_ROOT / "tests" / "fixtures" / "entity" / "classify.json"

    def test_thirty_fixture_strings_map_to_expected_kinds(self):
        rows = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 30, "classify.json must hold exactly 30 rows")
        from storelib.entity import classify_entity_kind
        for row in rows:
            got = classify_entity_kind(row["name"])
            self.assertEqual(got, row["expected"],
                             f"{row['name']!r} classified {got!r}, "
                             f"expected {row['expected']!r}")
            self.assertIn(got, ENTITY_KINDS)

    def test_precedence_is_deterministic(self):
        from storelib.entity import classify_entity_kind
        # Earlier rules win over later ones: a port shape beats the host
        # reading, the *Exception suffix beats the service noun INSIDE the
        # word, a source-suffixed path beats the generic owner/repo shape,
        # and repeated calls are stable.
        precedence = [
            ("api.example.com:8080", "service"),   # colon blocks host, port wins
            ("DatabaseException", "error_class"),  # word-bound: not a service
            ("src/main.py", "file"),               # dot-free repo labels only
            ("gpt-5.3", "model"),                  # host digit-rule reroutes
            ("localhost", "host"),
            ("git status", "command"),
            ("Brett", "other"),
        ]
        for name, expected in precedence:
            for _ in range(3):  # determinism: same input, same output
                self.assertEqual(classify_entity_kind(name), expected, name)


class ReclassifyTest(_Store):
    """Issue #176: dry-run/apply against the fixed reclassify.json seed."""

    NS = "fixture:entity-176"
    FIXTURE = REPO_ROOT / "tests" / "fixtures" / "entity" / "reclassify.json"
    EXPECTED = (REPO_ROOT / "tests" / "fixtures" / "entity"
                / "reclassify-expected.json")

    def _seed(self, extra_tool=False):
        self.run_store("init")
        data = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        conn = self.db()
        mem_ids = sorted({m["memory_id"] for m in data["memory_entity"]})
        conn.executemany(
            "INSERT INTO memory(id, namespace, type, content, tags, "
            "ingestion_ts) VALUES (?,?,?,?,?,?)",
            [(mid, self.NS, "fact", f"fixture row {i + 1}", "", FIXED_TS)
             for i, mid in enumerate(mem_ids)])
        conn.executemany(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            [(e["id"], e["kind"], e["canonical_name"], e["created_at"],
              e["updated_at"]) for e in data["entities"]]
            + ([("00000000-0000-4000-8000-000000000006", "tool", "ripgrep",
                 FIXED_TS, FIXED_TS)] if extra_tool else []))
        conn.executemany(
            "INSERT INTO entity_alias(entity_id, alias_norm) VALUES (?,?)",
            [(a["entity_id"], a["alias_norm"]) for a in data["aliases"]]
            + ([("00000000-0000-4000-8000-000000000006", "ripgrep")]
               if extra_tool else []))
        conn.executemany(
            "INSERT INTO memory_entity(memory_id, entity_id, role) "
            "VALUES (?,?,?)",
            [(m["memory_id"], m["entity_id"], m["role"])
             for m in data["memory_entity"]]
            + ([("40000000-0000-4000-8000-000000000004",
                 "00000000-0000-4000-8000-000000000006", "mentions")]
               if extra_tool else []))
        conn.commit()

    def _snapshot(self, conn):
        return {
            "entity": [tuple(r) for r in conn.execute(
                "SELECT id, kind, canonical_name, created_at, updated_at "
                "FROM entity ORDER BY id")],
            "alias": [tuple(r) for r in conn.execute(
                "SELECT entity_id, alias_norm FROM entity_alias "
                "ORDER BY entity_id, alias_norm")],
            "links": [tuple(r) for r in conn.execute(
                "SELECT memory_id, entity_id, role FROM memory_entity "
                "ORDER BY memory_id, entity_id")],
        }

    def test_dry_run_matches_expected_without_sqlite_change(self):
        self._seed()
        before = Path(self.store).read_bytes()
        # Raw bytes: a text=True capture would universal-newline-decode CRLF
        # to LF BEFORE the comparison, so the LF-only byte contract could
        # never fail this test (issue #176 review).
        r = self.run_store_bytes("reclassify-entities", "--dry-run", "--json")
        self.assertEqual(Path(self.store).read_bytes(), before,
                         "dry-run must change zero database bytes")
        expected = self.EXPECTED.read_bytes()
        self.assertNotIn(b"\r", r.stdout,
                         "the envelope must be LF-only bytes")
        self.assertEqual(r.stdout, expected,
                         "dry-run JSON must equal reclassify-expected.json")
        leases = list(Path(self.tmp).rglob("*.lease"))
        self.assertEqual(leases, [], "dry-run must not leave a writer lease")

    def test_apply_changes_only_other_and_preserves_ids_aliases_and_links(self):
        self._seed()
        conn = self.db()
        before = self._snapshot(conn)
        r = self.run_store_bytes("reclassify-entities", "--apply", "--json")
        expected = self.EXPECTED.read_bytes()
        self.assertNotIn(b"\r", r.stdout,
                         "the envelope must be LF-only bytes")
        self.assertEqual(r.stdout, expected,
                         "apply JSON must byte-equal the dry-run oracle")
        after = self._snapshot(conn)
        kinds = {row[0]: row[1] for row in after["entity"]}
        self.assertEqual(
            kinds,
            {"00000000-0000-4000-8000-000000000001": "host",
             "00000000-0000-4000-8000-000000000002": "service",
             "00000000-0000-4000-8000-000000000003": "model",
             "00000000-0000-4000-8000-000000000004": "repo",
             "00000000-0000-4000-8000-000000000005": "other"})
        # ids, canonical names, timestamps: everything EXCEPT kind unchanged.
        self.assertEqual(
            [(row[0], row[2], row[3], row[4]) for row in after["entity"]],
            [(row[0], row[2], row[3], row[4]) for row in before["entity"]])
        self.assertEqual(after["alias"], before["alias"])
        self.assertEqual(after["links"], before["links"])
        self.assertEqual(len(after["links"]), 3,
                         "the shares denominator is exactly three "
                         "memory_entity rows")

    def test_non_other_rows_are_untouched(self):
        self._seed(extra_tool=True)
        tool_id = "00000000-0000-4000-8000-000000000006"
        r = self.run_store("reclassify-entities", "--apply", "--json")
        proposal = json.loads(r.stdout)
        self.assertNotIn(tool_id, {c["id"] for c in proposal["changes"]},
                         "a non-other entity must never be proposed")
        conn = self.db()
        row = conn.execute(
            "SELECT kind, canonical_name FROM entity WHERE id=?", (tool_id,)
        ).fetchone()
        self.assertEqual(tuple(row), ("tool", "ripgrep"))
        link = conn.execute(
            "SELECT COUNT(*) FROM memory_entity WHERE entity_id=?", (tool_id,)
        ).fetchone()[0]
        self.assertEqual(link, 1, "the tool entity's link must survive")


class ReclassifyCliTest(_Store):
    """Issue #176: the CLI's mode-flag, read-only, and lease contracts."""

    def _seed_one(self):
        self.run_store("init")
        conn = self.db()
        conn.execute(
            "INSERT INTO entity(id, kind, canonical_name, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            ("00000000-0000-4000-8000-000000000001", "other",
             "api.example.com", FIXED_TS, FIXED_TS))
        conn.commit()

    def test_exactly_one_mode_is_required(self):
        self.run_store("init")
        for argv in (["--dry-run", "--apply"], []):
            r = self.run_store("reclassify-entities", *argv, check=False)
            self.assertEqual(r.returncode, 2, argv)
            self.assertIn("exactly one of --dry-run or --apply is required",
                          r.stderr)

    def test_dry_run_is_read_only(self):
        self._seed_one()
        before = Path(self.store).read_bytes()
        r = self.run_store("reclassify-entities", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scanned=1 changed=1", r.stdout)
        self.assertEqual(Path(self.store).read_bytes(), before)
        self.assertEqual(list(Path(self.tmp).rglob("*.lease")), [])

    def test_apply_uses_writer_lease(self):
        self._seed_one()
        # Block the writer-lease DIRECTORY with a regular file: an apply that
        # (correctly) acquires the lease cannot create it and must fail
        # loudly with zero changes; dry-run on the same store still succeeds,
        # proving the two arms differ on lease acquisition.
        writers = Path(self.tmp) / ".zmem-writers"
        writers.write_text("lease-dir blocker", encoding="utf-8")
        r = self.run_store("reclassify-entities", "--apply", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[zmem] reclassify-entities:", r.stderr)
        conn = self.db()
        kind = conn.execute(
            "SELECT kind FROM entity WHERE id="
            "'00000000-0000-4000-8000-000000000001'").fetchone()[0]
        self.assertEqual(kind, "other", "failed apply must change nothing")
        writers.unlink()
        r2 = self.run_store("reclassify-entities", "--dry-run")
        self.assertEqual(r2.returncode, 0, r2.stderr)


    def test_dry_run_never_acquires_the_writer_lease(self):
        """Issue #176 review: AC4 says "dry-run acquires no writer lease".

        The post-hoc `rglob("*.lease") == []` assertions cannot prove it —
        acquire-then-release in the CLI's `finally` leaves the same empty
        directory, and a mutation hoisting `_acquire_writer_lease` above the
        `if args.apply:` branch passed the whole suite. Discriminating
        probe: block the lease DIRECTORY with a regular file so an arm that
        tries to acquire MUST fail. Dry-run must still succeed (it never
        touches the directory) while apply on the same store fails loudly.
        """
        self._seed_one()
        writers = Path(self.tmp) / ".zmem-writers"
        writers.write_text("lease-dir blocker", encoding="utf-8")
        dry = self.run_store("reclassify-entities", "--dry-run")
        self.assertEqual(dry.returncode, 0,
                         "dry-run must not need the lease directory: "
                         f"{dry.stderr}")
        self.assertIn("scanned=1 changed=1", dry.stdout)
        applied = self.run_store("reclassify-entities", "--apply", check=False)
        self.assertNotEqual(applied.returncode, 0,
                            "apply MUST try to acquire the lease")
        writers.unlink()


class WriteTimeClassificationTest(_Store):
    """Issue #176 review: the WRITE-TIME classification path is behavior and
    needs an end-to-end pin.

    The only classifier test called `classify_entity_kind` directly, so a
    mutation reverting all three `extract_entities` classify sites to
    `"other"` kept the suite green (42/42 here plus 4 sibling modules).
    These drive the real `store.py add` and assert the STORED kinds.
    """

    def _kinds(self):
        return {e["name"]: e["kind"] for e in
                json.loads(self.run_store("entity-list", "--json").stdout)}

    def test_plain_tags_are_classified_at_write_time(self):
        self.run_store("init")
        # A slash-bearing tag never becomes an entity at all (`_eligible_name`
        # refuses path shapes), so this asserts only slash-free ones.
        self.add("seed", "--tags", "python,postgres:5432,api.example.com")
        kinds = self._kinds()
        self.assertEqual(kinds.get("python"), "command")
        self.assertEqual(kinds.get("postgres:5432"), "service")
        self.assertEqual(kinds.get("api.example.com"), "host")

    def test_untyped_entity_tag_is_classified_and_typed_tag_wins(self):
        self.run_store("init")
        # Untyped `entity:Name` carries no kind -> classified (host).
        self.add("seed a", "--tags", "entity:api.example.com")
        # A TYPED tag is authoritative: `Brett` classifies to `other`, but
        # `entity:person:Brett` must stay `person`.
        self.add("seed b", "--tags", "entity:person:Brett")
        kinds = self._kinds()
        self.assertEqual(kinds.get("api.example.com"), "host")
        self.assertEqual(kinds.get("Brett"), "person")

    def test_camelcase_identifier_in_content_is_classified(self):
        self.run_store("init")
        self.add("boom ValueError while handling TimeoutError here")
        kinds = self._kinds()
        self.assertEqual(kinds.get("ValueError"), "error_class")
        self.assertEqual(kinds.get("TimeoutError"), "error_class")


class ReDosRegressionTest(unittest.TestCase):
    """Issue #176 review: the classifier must not backtrack quadratically.

    A hostile hyphen-dense name (reachable through the sync/ingest `tags`
    field, which is NOT length-capped) spent 7.6 s inside
    `_CLASSIFIER_HOST_ONEDOT_RE` at 64 KB. The pattern is now written
    `[a-z0-9]*(?:-[a-z0-9]*)+` so every repetition must start with a literal
    hyphen and the split point is unambiguous. The scaling RATIO (doubling
    the input must not more than quadruple the time) is the primary
    assertion and is machine-independent; the absolute ceiling is a very
    generous backstop with ~1000x headroom over the linear cost, so a
    quadratic engine cannot pass it on any runner.
    """

    def test_ipv4_octets_are_ascii_strict(self):
        """Issue #176 review: the octet class is `[0-9]`, not `\\d`.

        Python's `\\d` matches Unicode decimal digits, so an Arabic-Indic
        dotted quad used to classify as `host` even though the docstring
        promises STRICT octets. No ASCII input changes; this pins the
        tightening so a revert to `\\d` fails.
        """
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.entity import classify_entity_kind
        self.assertEqual(classify_entity_kind("127.0.0.1"), "host")
        self.assertEqual(classify_entity_kind("255.255.255.255"), "host")
        # Arabic-Indic (U+0660-0669) and fullwidth (U+FF10-FF19) digits.
        self.assertEqual(classify_entity_kind("١٢٧.٠.٠.١"), "other")
        self.assertEqual(classify_entity_kind("１２７.０.０.１"), "other")

    def test_host_one_dot_pattern_is_linear_on_hostile_input(self):
        import time
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.entity import _CLASSIFIER_HOST_ONEDOT_RE
        small = time.perf_counter()
        _CLASSIFIER_HOST_ONEDOT_RE.fullmatch("a-" * 64_000 + ".")
        mid = time.perf_counter()
        _CLASSIFIER_HOST_ONEDOT_RE.fullmatch("a-" * 128_000 + ".")
        large = time.perf_counter()
        t_small, t_large = mid - small, large - mid
        # A quadratic engine more than quadruples per doubling AND cannot
        # finish 256 KB in seconds (it measured ~7.6 s at only 64 KB).
        self.assertLess(t_large, 2.0,
                        f"host pattern looks super-linear: "
                        f"{t_small:.4f}s@128KB -> {t_large:.4f}s@256KB")
        self.assertLess(t_large / max(t_small, 1e-6), 4.0,
                        f"doubling input more than quadrupled the time "
                        f"({t_small:.4f}s -> {t_large:.4f}s)")

    def test_rewrite_is_semantically_identical_on_edge_cases(self):
        """The rewrite changes SPEED only, never the accepted language.

        The oracle is the pre-rewrite pattern reproduced verbatim below.
        """
        import re
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.entity import _CLASSIFIER_HOST_ONEDOT_RE
        legacy = re.compile(r"^[a-z0-9-]*-[a-z0-9-]*\.[a-z][a-z0-9-]*$")
        for value in ("build-host-1.example", "db-host.internal",
                      "my-script.py", "app-config.json", "a--b.com",
                      "a-.com", "-a.com", "a.b-c.com", "a.1b", "a.9",
                      "ab.cd", "a-b", "", ".com", "a.", "a-b-c-d.e", "x-y.z-",
                      "9-9.9a", "A-B.c", "a-b.c.d", "---.a"):
            self.assertEqual(
                bool(_CLASSIFIER_HOST_ONEDOT_RE.fullmatch(value)),
                bool(legacy.fullmatch(value)),
                f"rewrite changed the accepted language for {value!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
