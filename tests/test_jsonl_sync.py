"""Tests for store.py's `export-jsonl` / `ingest-jsonl` subcommands (Tier 3
box-to-box sync).

Covers:
  - round-trip: export from store A, ingest into fresh store B, re-export from
    B -> identical live set (same rows, same field values, same JSONL order)
  - id-idempotency: ingesting the same file twice adds nothing the second time
  - tombstone propagation: supersede in A, export --include-superseded,
    ingest into B kills the live row in B (INCLUDING the exact superseded_at
    timestamp -- ingest must not re-stamp it with local ingest time)
  - malformed-line resilience: bad lines are counted and skipped, good lines
    around them still ingest, file-level exit code stays 0
  - unreadable / empty file -> exit 2
  - dedup-on-write applies to synced rows exactly like a local `add`
  - a brand-new id that arrives already-tombstoned is inserted as history
    (never live, never resurfaces)
  - a synced row leaves embedding NULL, and recall in the receiving store
    still finds it via FTS
  - UTF-8 (ensure_ascii=False) content survives an export/ingest/export round
    trip byte-for-byte

Drives the REAL store.py CLI via subprocess against two throwaway temp
stores ("A" and "B") -- never the box store -- following the isolation
fixture pattern established in tests/test_backup.py / tests/test_no_bump.py:
ZMEM_STORE (and friends) set inline on every subprocess env dict, per store,
in the same call that runs the subprocess. This file does not `import store`
at all (subprocess-only), specifically to avoid the module-level STORE_PATH
freeze-at-import trap that makes a *single* shared store safe to import in
test_backup.py but not two independent stores in the same process.

Run: python -m pytest tests/test_jsonl_sync.py -q
"""

from __future__ import annotations

import json
import os
import re
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
NS = "project:jsonlsync"


def _base_env(tmp: str) -> dict:
    """Env for a store.py subprocess pinned to a throwaway store, with the
    embedding model forced absent (fast + deterministic; repo convention from
    tests/test_model_fallback.py, tests/test_backup.py)."""
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("ZMEM_BACKUP_DIR", None)
    env.pop("ZMEM_BACKUP_INTERVAL_DAYS", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


class Store:
    """One throwaway store + its pinned subprocess env. Two independent
    instances (A, B) stand in for two boxes syncing via JSONL files."""

    def __init__(self, tmp_dir: str):
        self.tmp = tmp_dir
        self.path = os.path.join(tmp_dir, "store.sqlite")
        self.env = _base_env(tmp_dir)

    def run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=60,
        )

    def add(self, namespace: str, content: str, *, type_: str = "lesson",
            signal: str = "test", confidence: float | None = None,
            tags: str = "", source_ref: str = "") -> str:
        args = ["add", "--namespace", namespace, "--type", type_,
                "--content", content, "--signal", signal]
        if confidence is not None:
            args += ["--confidence", str(confidence)]
        if tags:
            args += ["--tags", tags]
        if source_ref:
            args += ["--source-ref", source_ref]
        r = self.run(*args)
        assert r.returncode == 0, r.stderr
        mid = self.find_id(namespace, content)
        assert mid is not None, f"could not find just-added row: {content!r}"
        return mid

    def find_id(self, namespace: str, content: str) -> str | None:
        row = self.query_one(
            "SELECT id FROM memory WHERE namespace=? AND content=? "
            "ORDER BY ingestion_ts DESC LIMIT 1",
            (namespace, content),
        )
        return row[0] if row else None

    def query_one(self, sql: str, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def query_all(self, sql: str, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def vec_ids_for(self, mid: str) -> list[str]:
        """memory_vec rows for `mid`, tolerating a store with no vec0 table
        at all (sqlite-vec unavailable in this environment)."""
        try:
            return [r[0] for r in self.query_all(
                "SELECT memory_id FROM memory_vec WHERE memory_id=?", (mid,))]
        except sqlite3.OperationalError:
            return []


class _TwoStoreCase(unittest.TestCase):
    def setUp(self):
        self.a = Store(tempfile.mkdtemp(prefix="zmem-jsonl-a-"))
        self.b = Store(tempfile.mkdtemp(prefix="zmem-jsonl-b-"))
        self.addCleanup(shutil.rmtree, self.a.tmp, True)
        self.addCleanup(shutil.rmtree, self.b.tmp, True)
        r = self.a.run("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.b.run("init")
        self.assertEqual(r.returncode, 0, r.stderr)

    @staticmethod
    def _read_jsonl(path: str) -> list[dict]:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines if l.strip()]

    @staticmethod
    def _summary_counts(stdout: str) -> dict:
        m = re.search(
            r"added=(\d+) tombstoned=(\d+) deduped=(\d+) skipped=(\d+) malformed=(\d+)",
            stdout,
        )
        assert m, f"summary line not found in: {stdout!r}"
        keys = ("added", "tombstoned", "deduped", "skipped", "malformed")
        return dict(zip(keys, (int(g) for g in m.groups())))


# ---------------------------------------------------------------------------
# round trip + id-idempotency
# ---------------------------------------------------------------------------
class RoundTripTest(_TwoStoreCase):
    def test_export_ingest_reexport_identical_live_set(self):
        self.a.add(NS, "always run pytest --tb=short before committing", confidence=0.9)
        self.a.add(NS, "prefer small, reviewable commits", signal="user", confidence=0.6)
        self.a.add("user:global", "never hardcode a secret in a fixture", confidence=0.85,
                   tags="security,secrets")

        export_a = os.path.join(self.a.tmp, "export_a.jsonl")
        r = self.a.run("export-jsonl", "--out", export_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows_a = self._read_jsonl(export_a)
        self.assertEqual(len(rows_a), 3)

        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 3)
        self.assertEqual(counts["tombstoned"], 0)
        self.assertEqual(counts["deduped"], 0)
        self.assertEqual(counts["malformed"], 0)

        export_b = os.path.join(self.b.tmp, "export_b.jsonl")
        r = self.b.run("export-jsonl", "--out", export_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows_b = self._read_jsonl(export_b)

        self.assertEqual(rows_a, rows_b, "re-export from B must match A's export exactly")

    def test_id_idempotency_second_ingest_adds_nothing(self):
        self.a.add(NS, "idempotency check row", confidence=0.9)
        export_a = os.path.join(self.a.tmp, "export_a.jsonl")
        self.a.run("export-jsonl", "--out", export_a)

        r1 = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(self._summary_counts(r1.stdout)["added"], 1)

        r2 = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        counts2 = self._summary_counts(r2.stdout)
        self.assertEqual(counts2["added"], 0)
        self.assertEqual(counts2["skipped"], 1)

        self.assertEqual(
            self.b.query_one("SELECT count(*) FROM memory WHERE namespace=?", (NS,))[0], 1)

    def test_export_namespace_filter_excludes_other_namespaces(self):
        self.a.add(NS, "project scoped row", confidence=0.9)
        self.a.add("user:global", "global scoped row", confidence=0.9)
        out_path = os.path.join(self.a.tmp, "ns_filtered.jsonl")
        r = self.a.run("export-jsonl", "--out", out_path, "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self._read_jsonl(out_path)
        self.assertEqual({row["namespace"] for row in rows}, {NS})
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# --source-ref: attribute a whole import batch, vs. keep each row's own
# ---------------------------------------------------------------------------
class SourceRefOverrideTest(_TwoStoreCase):
    def test_source_ref_override_lands_on_inserted_rows(self):
        self.a.add(NS, "row whose provenance gets rewritten on import", confidence=0.9)
        export_a = os.path.join(self.a.tmp, "export.jsonl")
        self.a.run("export-jsonl", "--out", export_a)

        r = self.b.run("ingest-jsonl", "--in", export_a, "--source-ref", "sync:boxA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertEqual(self.b.query_one(
            "SELECT source_ref FROM memory WHERE content=?",
            ("row whose provenance gets rewritten on import",))[0], "sync:boxA")

    def test_without_override_keeps_original_source_ref(self):
        missing_path = os.path.join(self.a.tmp, "notes-that-do-not-exist-on-b.md")
        self.a.add(NS, "row referencing a file: source_ref", confidence=0.9,
                  source_ref=f"file:{missing_path}")
        export_a = os.path.join(self.a.tmp, "export.jsonl")
        self.a.run("export-jsonl", "--out", export_a)

        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertEqual(self.b.query_one(
            "SELECT source_ref FROM memory WHERE content=?",
            ("row referencing a file: source_ref",))[0], f"file:{missing_path}")
        # The referenced file does not exist under this path -- _source_hash's
        # existing fail-loud staleness warning fires for a synced row exactly
        # as it would for a local add referencing a moved/deleted file. This
        # is the concrete reason --source-ref exists: a real cross-box sync
        # would otherwise print this warning for every row whose source_ref
        # is a local path meaningless on the receiving box.
        self.assertIn("could not read source_ref", r.stderr)


# ---------------------------------------------------------------------------
# tombstone propagation
# ---------------------------------------------------------------------------
class TombstonePropagationTest(_TwoStoreCase):
    def test_supersede_in_a_propagates_and_kills_live_row_in_b(self):
        mid = self.a.add(NS, "row that will be superseded upstream", confidence=0.9)

        export_a = os.path.join(self.a.tmp, "export1.jsonl")
        self.a.run("export-jsonl", "--out", export_a)
        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertIsNone(self.b.query_one(
            "SELECT superseded_at FROM memory WHERE id=?", (mid,))[0])

        r = self.a.run("supersede", "--id", mid, "--reason", "no longer accurate")
        self.assertEqual(r.returncode, 0, r.stderr)
        a_superseded_at = self.a.query_one(
            "SELECT superseded_at FROM memory WHERE id=?", (mid,))[0]
        self.assertIsNotNone(a_superseded_at)

        export_a2 = os.path.join(self.a.tmp, "export2.jsonl")
        r = self.a.run("export-jsonl", "--out", export_a2, "--include-superseded")
        self.assertEqual(r.returncode, 0, r.stderr)

        r = self.b.run("ingest-jsonl", "--in", export_a2)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstoned"], 1)
        self.assertEqual(counts["added"], 0)

        b_row = self.b.query_one(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?", (mid,))
        self.assertIsNotNone(b_row[0], "row must be dead (tombstoned) in B")
        self.assertEqual(b_row[0], a_superseded_at,
                         "B must carry A's own superseded_at, not local ingest time")
        self.assertEqual(b_row[1], "no longer accurate")

        # Dead in recall too.
        r = self.b.run("recall", "--query", "superseded upstream", "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        results = json.loads(r.stdout)
        self.assertNotIn(mid, [x["id"] for x in results])


# ---------------------------------------------------------------------------
# malformed lines / unreadable / empty file
# ---------------------------------------------------------------------------
class MalformedAndFileErrorsTest(_TwoStoreCase):
    def test_malformed_lines_are_counted_and_skipped_good_lines_still_ingest(self):
        good1 = {
            "id": "11111111-1111-1111-1111-111111111111", "namespace": NS,
            "type": "fact", "content": "good row one", "tags": "", "source_ref": "",
            "confidence": 0.8, "signal": "test", "valid_from": "2026-01-01T00:00:00Z",
            "ingestion_ts": "2026-01-01T00:00:00Z", "superseded_at": None,
            "supersede_reason": "",
        }
        good2 = {**good1, "id": "22222222-2222-2222-2222-222222222222",
                 "content": "good row two"}
        missing_content = {**good1, "id": "33333333-3333-3333-3333-333333333333"}
        del missing_content["content"]

        mixed_path = os.path.join(self.b.tmp, "mixed.jsonl")
        with open(mixed_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(good1) + "\n")
            f.write("{this is not valid json\n")
            f.write("\n")  # blank line: not counted as malformed
            f.write(json.dumps(missing_content) + "\n")
            f.write(json.dumps(good2) + "\n")

        r = self.b.run("ingest-jsonl", "--in", mixed_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 2)
        self.assertEqual(counts["malformed"], 2)
        self.assertIn("malformed line 2", r.stderr)
        self.assertIn("malformed line 4", r.stderr)

    def test_unreadable_file_exits_2(self):
        r = self.b.run("ingest-jsonl", "--in", os.path.join(self.b.tmp, "does-not-exist.jsonl"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("[zmem]", r.stderr)

    def test_empty_file_exits_2(self):
        empty_path = os.path.join(self.b.tmp, "empty.jsonl")
        Path(empty_path).write_text("", encoding="utf-8")
        r = self.b.run("ingest-jsonl", "--in", empty_path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("[zmem]", r.stderr)

    def test_whitespace_only_file_exits_2(self):
        ws_path = os.path.join(self.b.tmp, "whitespace.jsonl")
        Path(ws_path).write_text("\n\n   \n", encoding="utf-8")
        r = self.b.run("ingest-jsonl", "--in", ws_path)
        self.assertEqual(r.returncode, 2)


# ---------------------------------------------------------------------------
# dedup-on-write applies to synced rows
# ---------------------------------------------------------------------------
class DedupOnIngestTest(_TwoStoreCase):
    def test_synced_row_dedups_against_existing_local_content(self):
        local_id = self.b.add(NS, "identical content for dedup test", confidence=0.6,
                              signal="user")
        before = self.b.query_one(
            "SELECT retrieval_count FROM memory WHERE id=?", (local_id,))[0]

        incoming = {
            "id": "44444444-4444-4444-4444-444444444444", "namespace": NS,
            "type": "lesson", "content": "identical content for dedup test",
            "tags": "", "source_ref": "", "confidence": 0.9, "signal": "test",
            "valid_from": "2026-01-01T00:00:00Z", "ingestion_ts": "2026-01-01T00:00:00Z",
            "superseded_at": None, "supersede_reason": "",
        }
        dedup_path = os.path.join(self.b.tmp, "dedup.jsonl")
        Path(dedup_path).write_text(json.dumps(incoming) + "\n", encoding="utf-8", newline="\n")

        r = self.b.run("ingest-jsonl", "--in", dedup_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["deduped"], 1)
        self.assertEqual(counts["added"], 0)

        # The incoming id must NOT have been inserted as its own row.
        self.assertIsNone(self.b.query_one(
            "SELECT id FROM memory WHERE id=?", (incoming["id"],)))
        # dedup-on-write merges into the existing row (mirrors `add`'s own
        # semantics exactly, including its retrieval_count bump on merge).
        after = self.b.query_one(
            "SELECT retrieval_count, confidence, signal FROM memory WHERE id=?", (local_id,))
        self.assertEqual(after[0], before + 1)
        self.assertEqual(after[1], 0.9)   # stronger incoming confidence wins
        self.assertEqual(after[2], "test")  # stronger incoming signal wins


# ---------------------------------------------------------------------------
# new id, already tombstoned upstream -> inserted as history, never live
# ---------------------------------------------------------------------------
class NewTombstonedIdTest(_TwoStoreCase):
    def test_new_already_tombstoned_row_inserted_dead_never_resurfaces(self):
        row = {
            "id": "55555555-5555-5555-5555-555555555555", "namespace": NS,
            "type": "fact", "content": "this arrived pre-tombstoned",
            "tags": "", "source_ref": "", "confidence": 0.7, "signal": "test",
            "valid_from": "2026-01-01T00:00:00Z", "ingestion_ts": "2026-01-01T00:00:00Z",
            "superseded_at": "2026-01-02T00:00:00Z", "supersede_reason": "stale on origin",
        }
        path = os.path.join(self.b.tmp, "pretombstoned.jsonl")
        Path(path).write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)

        db_row = self.b.query_one(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?", (row["id"],))
        self.assertEqual(db_row[0], "2026-01-02T00:00:00Z")
        self.assertEqual(db_row[1], "stale on origin")

        r = self.b.run("recall", "--query", "pre-tombstoned", "--namespace", NS, "--json")
        results = json.loads(r.stdout)
        self.assertNotIn(row["id"], [x["id"] for x in results])

        r = self.b.run("list", "--namespace", NS)
        self.assertNotIn(row["id"][:8], r.stdout)
        r = self.b.run("list", "--namespace", NS, "--include-superseded")
        self.assertIn(row["id"][:8], r.stdout)


# ---------------------------------------------------------------------------
# no embeddings on a synced row; FTS still finds it
# ---------------------------------------------------------------------------
class NoEmbeddingFtsRecallTest(_TwoStoreCase):
    def test_ingested_row_has_no_embedding_and_recall_finds_it_via_fts(self):
        mid = self.a.add(NS, "xylophone marmalade quokka unique keyword combo", confidence=0.9)
        export_a = os.path.join(self.a.tmp, "export.jsonl")
        self.a.run("export-jsonl", "--out", export_a)
        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)

        emb_row = self.b.query_one("SELECT embedding FROM memory WHERE id=?", (mid,))
        self.assertIsNone(emb_row[0], "ingest must leave embedding NULL for reembed to backfill")
        self.assertEqual(self.b.vec_ids_for(mid), [], "no memory_vec entry for a synced row")

        r = self.b.run("recall", "--query", "xylophone quokka", "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        results = json.loads(r.stdout)
        self.assertIn(mid, [x["id"] for x in results], "FTS recall must still find the synced row")


# ---------------------------------------------------------------------------
# UTF-8 fidelity (ensure_ascii=False) through a full export/ingest/export cycle
# ---------------------------------------------------------------------------
class Utf8FidelityTest(_TwoStoreCase):
    def test_non_ascii_content_survives_round_trip_byte_for_byte(self):
        content = "café — €100 résumé 日本語"  # café — €100 résumé 日本語
        self.a.add(NS, content, confidence=0.9)

        export_a = os.path.join(self.a.tmp, "utf8.jsonl")
        self.a.run("export-jsonl", "--out", export_a)
        raw = Path(export_a).read_bytes()
        self.assertIn(content.encode("utf-8"), raw, "ensure_ascii=False must keep UTF-8, not \\uXXXX escapes")
        self.assertNotIn(b"\\u00e9", raw)

        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)

        export_b = os.path.join(self.b.tmp, "utf8_b.jsonl")
        self.b.run("export-jsonl", "--out", export_b)
        raw_b = Path(export_b).read_bytes()
        self.assertIn(content.encode("utf-8"), raw_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
