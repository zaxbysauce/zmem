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


def _base_env(store_path: str) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = store_path
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_MMR_LAMBDA", None)
    return env


class _Store(unittest.TestCase):
    """Subprocess-driven house pattern: every command is the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-entity-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = _base_env(self.store)
        self.addCleanup(shutil.rmtree, self.tmp, True)

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
        return json.loads(out)


class SchemaTest(_Store):
    def test_v10_tables_and_constraints(self):
        self.run_store("init")
        conn = self.db()
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(ver, "10")
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
        self.add("use `ripgrep`", "--tags", "python")
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


class MigrationTest(_Store):
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
        self.assertEqual(ver, "10")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
