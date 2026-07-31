"""Tests for store.py's `export-jsonl` / `ingest-jsonl` subcommands (Tier 3
box-to-box sync).

Covers:
  - round-trip: export from store A, ingest into fresh store B, re-export from
    B -> identical live set (same rows, same field values, same JSONL order)
  - id-idempotency: ingesting the same file twice adds nothing the second time
  - tombstone propagation WITH --allow-tombstones: supersede in A, export
    --include-superseded, ingest into B kills the live row in B (INCLUDING
    the exact superseded_at timestamp -- ingest must not re-stamp it with
    local ingest time)
  - tombstone AUTHORITY without the flag: an incoming superseded row may NOT
    kill a live local row; it is counted as tombstones_refused and the local
    row is untouched
  - malformed-line resilience: bad lines are counted and skipped, good lines
    around them still ingest, file-level exit code stays 0
  - per-row validation of remote-authored rows: types, enums, id shape,
    content size cap, confidence coercion/clamp, future-timestamp clamp
  - partial-file resilience: a row that RAISES mid-apply is counted and the
    file keeps going; the summary line always prints
  - unreadable / empty file -> exit 2
  - dedup-on-write applies to synced rows exactly like a local `add`
  - a brand-new id that arrives already-tombstoned is inserted as history
    (never live, never resurfaces) -- with or without --allow-tombstones
  - a synced row leaves embedding NULL *when no embedding runtime is
    available* (which is how these tests run: ZMEM_MODELS_DIR points at
    nothing), and recall in the receiving store still finds it via FTS
  - UTF-8 (ensure_ascii=False) content survives an export/ingest/export round
    trip byte-for-byte
  - scripts/ingest_harvest.py drives its store.py child with a UTF-8 stdio
    encoding, so a non-cp1252 namespace cannot make the child die AFTER its
    commit and get the landed row reported as FAILED

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
            r"added=(\d+) tombstoned=(\d+) tombstones_refused=(\d+) "
            r"deduped=(\d+) skipped=(\d+) malformed=(\d+)",
            stdout,
        )
        assert m, f"summary line not found in: {stdout!r}"
        keys = ("added", "tombstoned", "tombstones_refused",
                "deduped", "skipped", "malformed")
        return dict(zip(keys, (int(g) for g in m.groups())))

    def _write_jsonl(self, store: "Store", name: str, rows) -> str:
        """Write `rows` as a JSONL file inside `store`'s temp dir; return the path."""
        path = os.path.join(store.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path


def _sync_row(**overrides) -> dict:
    """A well-formed export-jsonl row, overridable field by field. Every
    validation test is 'this one field is hostile' against a known-good base."""
    row = {
        "id": "00000000-0000-0000-0000-000000000000",
        "namespace": NS,
        "type": "fact",
        "content": "a perfectly ordinary synced memory",
        "tags": "",
        "source_ref": "",
        "confidence": 0.8,
        "signal": "test",
        "valid_from": "2026-01-01T00:00:00Z",
        "ingestion_ts": "2026-01-01T00:00:00Z",
        "superseded_at": None,
        "supersede_reason": "",
    }
    row.update(overrides)
    return row


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
    """A ships its own export downstream to B, so A is authoritative for these
    ids and B opts in with --allow-tombstones. The refusal path (no flag, the
    untrusted direction) is TombstoneAuthorityTest below."""

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

        r = self.b.run("ingest-jsonl", "--in", export_a2, "--allow-tombstones")
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstoned"], 1)
        self.assertEqual(counts["added"], 0)
        self.assertEqual(counts["tombstones_refused"], 0)

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
    """No --allow-tombstones anywhere in this class: a NEW id that arrives
    already tombstoned destroys nothing local (it was never live here), so it
    is inserted as history in both modes. Only a tombstone aimed at a LIVE
    local row needs the flag."""

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
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(counts["tombstones_refused"], 0,
                         "a NEW pre-tombstoned id destroys nothing and must not be refused")

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


# ---------------------------------------------------------------------------
# Unicode line-separator fidelity: writer escapes U+2028/U+2029/U+0085 so one
# JSON object == one physical line, and the reader splits on a bare "\n" (not
# str.splitlines()) so it tolerates a third-party writer that does not escape
# them. Written as \uXXXX escapes throughout (never a literal glyph) to keep
# this source ASCII-only and unambiguous under any console codepage.
# ---------------------------------------------------------------------------
class LineTerminatorFidelityTest(_TwoStoreCase):
    def test_export_escapes_separators_and_round_trip_is_byte_identical(self):
        """The verifier's exact repro shape: before the fix, export-jsonl left
        U+2028/U+2029/U+0085 raw, and ingest-jsonl's str.splitlines() then
        split one such row into multiple malformed fragments."""
        tricky = "alpha\u2028beta\u2029gamma\u0085delta"
        contents = ["plain row one", tricky, "plain row two", "plain row three"]
        for c in contents:
            self.a.add(NS, c, confidence=0.9)

        export_a = os.path.join(self.a.tmp, "separators.jsonl")
        r = self.a.run("export-jsonl", "--out", export_a)
        self.assertEqual(r.returncode, 0, r.stderr)

        raw = Path(export_a).read_bytes().decode("utf-8")
        # One JSON object per line when split on a bare "\n" -- the writer
        # must not have left a raw separator that would fragment a row.
        data_lines = [ln for ln in raw.split("\n") if ln.strip()]
        self.assertEqual(len(data_lines), len(contents))
        for ln in data_lines:
            json.loads(ln)  # every line parses standalone as one JSON object
        # The three separators must not appear as raw characters in the file
        # at all -- only as their escaped \uXXXX text.
        self.assertNotIn("\u2028", raw)
        self.assertNotIn("\u2029", raw)
        self.assertNotIn("\u0085", raw)
        self.assertIn("\\u2028", raw)
        self.assertIn("\\u2029", raw)
        self.assertIn("\\u0085", raw)

        r = self.b.run("ingest-jsonl", "--in", export_a)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], len(contents))
        self.assertEqual(counts["malformed"], 0)

        stored = self.b.query_one(
            "SELECT content FROM memory WHERE namespace=? AND content LIKE ?",
            (NS, "alpha%delta"))
        self.assertIsNotNone(stored, "the tricky row must have landed in B")
        # Byte-identical: the separators must be REAL characters in the DB,
        # not escaped text -- the escaping is a wire-format concern only.
        self.assertEqual(stored[0], tricky)

    def test_reader_tolerates_a_raw_unicode_separator_from_a_naive_writer(self):
        """Reader robustness: a third-party JSONL writer might not escape
        U+2028/U+2029/U+0085 the way this file's own export-jsonl does. With
        the reader split on a bare "\n" (not str.splitlines()), a raw U+2028
        inside a JSON string value keeps the line intact and JSON-valid --
        json.loads permits an unescaped U+2028/U+2029/U+0085 inside a string
        (JSON only forbids raw codepoints < 0x20)."""
        content = "naive writer row with a raw U+2028 separator: alpha\u2028beta"
        row = _sync_row(id="66666666-7777-8888-9999-aaaaaaaaaaaa", content=content)
        path = os.path.join(self.b.tmp, "naive.jsonl")
        # Hand-built, NOT via export-jsonl, and NOT escaping the separator --
        # this is the "naive third-party writer" this test simulates.
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        raw = Path(path).read_bytes().decode("utf-8")
        self.assertIn("\u2028", raw, "fixture sanity: the raw separator must actually be in the file")

        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(counts["malformed"], 0)

        stored = self.b.query_one(
            "SELECT content FROM memory WHERE id=?", (row["id"],))[0]
        self.assertEqual(stored, content)
        self.assertIn("\u2028", stored, "the real separator character must survive")


# ---------------------------------------------------------------------------
# RecursionError from a nesting bomb must not escape the per-line parse guard
# ---------------------------------------------------------------------------
class NestingBombResilienceTest(_TwoStoreCase):
    def test_deeply_nested_json_value_is_malformed_not_a_run_abort(self):
        """The verifier's exact repro shape: a 5000-deep nested JSON array
        blows Python's recursion limit inside json.loads() with RecursionError
        -- not json.JSONDecodeError/ValueError -- which used to escape the
        per-line guard entirely and abort the whole run mid-file with no
        summary line printed."""
        good = [
            _sync_row(id="dddddddd-1111-1111-1111-111111111111", content="good row one"),
            _sync_row(id="dddddddd-2222-2222-2222-222222222222", content="good row two"),
            _sync_row(id="dddddddd-3333-3333-3333-333333333333", content="good row three"),
        ]
        more_good = [
            _sync_row(id="dddddddd-4444-4444-4444-444444444444", content="good row four"),
            _sync_row(id="dddddddd-5555-5555-5555-555555555555", content="good row five"),
            _sync_row(id="dddddddd-6666-6666-6666-666666666666", content="good row six"),
        ]
        nested = "[" * 5000 + "]" * 5000
        bomb_line = (
            '{"id": "dddddddd-9999-9999-9999-999999999999", "namespace": "' + NS + '", '
            '"type": "fact", "content": ' + nested + ', "tags": "", "source_ref": "", '
            '"confidence": 0.8, "signal": "test", "valid_from": "2026-01-01T00:00:00Z", '
            '"ingestion_ts": "2026-01-01T00:00:00Z", "superseded_at": null, '
            '"supersede_reason": ""}'
        )

        path = os.path.join(self.b.tmp, "bomb.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for row in good:
                f.write(json.dumps(row) + "\n")
            f.write(bomb_line + "\n")
            for row in more_good:
                f.write(json.dumps(row) + "\n")

        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[zmem] ingest-jsonl: added=", r.stdout,
                      "the summary line must print even after a RecursionError mid-file")
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 6)
        self.assertEqual(counts["malformed"], 1)
        # The bomb is the 4th line (after 3 good rows); reporting its own
        # line number, same as every other malformed-line case, also proves
        # the RecursionError was actually handled in place -- a re-raise or a
        # second stack blow inside the except block would never reach this
        # print at all.
        self.assertIn("malformed line 4", r.stderr)

        ids = {row[0] for row in self.b.query_all("SELECT id FROM memory")}
        self.assertEqual(ids, {row["id"] for row in good + more_good},
                         "all six well-formed rows must land; the bomb must not abort the run")


# ---------------------------------------------------------------------------
# Validation of remote-authored rows (HIGH-1 / HIGH-2 / MED-5 + recency forgery)
# ---------------------------------------------------------------------------
class IngestValidationTest(_TwoStoreCase):
    """ingest-jsonl writes straight into the table, bypassing add_memory() and
    argparse. Every one of these is a field a sync file could set to something
    the local writers can never produce."""

    def test_string_and_nan_confidence_fall_back_and_recall_and_pack_survive(self):
        """THE regression for HIGH-1/HIGH-2.

        A string confidence is stored by SQLite as TEXT, and TEXT sorts above
        every numeric -- so the poisoned row passes recall's `confidence >= ?`
        floor, takes the top of export-pack's `ORDER BY confidence DESC`, and
        then crashes compute_score with ValueError on float(). "nan" is the
        same attack through float()'s own permissiveness (min/max propagate
        NaN, so a naive clamp does not catch it).
        """
        rows = [
            _sync_row(id="aaaaaaaa-0000-0000-0000-00000000000a",
                      content="poisoned confidence row about widgets",
                      confidence="zzz"),
            _sync_row(id="aaaaaaaa-0000-0000-0000-00000000000b",
                      content="nan confidence row about widgets",
                      confidence="nan"),
            _sync_row(id="aaaaaaaa-0000-0000-0000-00000000000c",
                      content="infinite confidence row about widgets",
                      confidence="inf"),
        ]
        path = self._write_jsonl(self.b, "poison.jsonl", rows)

        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 3)

        # Stored as a real number, at the signal-derived default (signal=test).
        for row in rows:
            value, kind = self.b.query_one(
                "SELECT confidence, typeof(confidence) FROM memory WHERE id=?",
                (row["id"],))
            self.assertEqual(kind, "real",
                             f"{row['confidence']!r} must not land as TEXT")
            self.assertEqual(value, 0.9)

        # Both downstream consumers must survive the poisoned input.
        r = self.b.run("recall", "--query", "widgets", "--namespace", NS, "--json")
        self.assertEqual(r.returncode, 0,
                         f"recall must not crash on an ingested row: {r.stderr}")
        self.assertEqual(len(json.loads(r.stdout)), 3)

        r = self.b.run("export-pack", "--namespace", NS, "--min-confidence", "0.0")
        self.assertEqual(r.returncode, 0,
                         f"export-pack must not crash on an ingested row: {r.stderr}")
        self.assertIn("poisoned confidence row about widgets", r.stdout)

    def test_out_of_range_confidence_is_clamped_to_unit_interval(self):
        path = self._write_jsonl(self.b, "range.jsonl", [
            _sync_row(id="bbbbbbbb-0000-0000-0000-00000000000a",
                      content="confidence far above one", confidence=99.0),
            _sync_row(id="bbbbbbbb-0000-0000-0000-00000000000b",
                      content="confidence far below zero", confidence=-5.0),
        ])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 2)
        self.assertEqual(self.b.query_one(
            "SELECT confidence FROM memory WHERE content=?",
            ("confidence far above one",))[0], 1.0)
        self.assertEqual(self.b.query_one(
            "SELECT confidence FROM memory WHERE content=?",
            ("confidence far below zero",))[0], 0.0)

    def test_numeric_string_confidence_still_coerces(self):
        """Coercion, not rejection: "0.75" is a real value badly typed."""
        path = self._write_jsonl(self.b, "numstr.jsonl", [
            _sync_row(id="bbbbbbbb-0000-0000-0000-00000000000c",
                      content="numeric string confidence", confidence="0.75"),
        ])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertEqual(self.b.query_one(
            "SELECT confidence FROM memory WHERE content=?",
            ("numeric string confidence",))[0], 0.75)

    def test_non_uuid_shaped_id_is_rejected(self):
        for bad_id in ("../../etc/passwd", "short", 12345, None,
                       "x" * 37, "aaaaaaaa-0000-0000-0000-00000000000z"):
            with self.subTest(id=bad_id):
                path = self._write_jsonl(self.b, "badid.jsonl",
                                         [_sync_row(id=bad_id, content="row with a bad id")])
                r = self.b.run("ingest-jsonl", "--in", path)
                self.assertEqual(r.returncode, 0, r.stderr)
                counts = self._summary_counts(r.stdout)
                self.assertEqual(counts["malformed"], 1)
                self.assertEqual(counts["added"], 0)
                self.assertIn("'id'", r.stderr)

    def test_non_string_or_empty_required_fields_are_rejected(self):
        cases = [
            ("content", {"content": {"not": "a string"}}),
            ("content", {"content": ""}),
            ("content", {"content": "   "}),
            ("content", {"content": 42}),
            ("namespace", {"namespace": ["list"]}),
            ("namespace", {"namespace": ""}),
        ]
        for field, override in cases:
            with self.subTest(field=field, override=override):
                path = self._write_jsonl(self.b, "badfield.jsonl", [
                    _sync_row(id="cccccccc-0000-0000-0000-00000000000a", **override)])
                r = self.b.run("ingest-jsonl", "--in", path)
                counts = self._summary_counts(r.stdout)
                self.assertEqual(counts["malformed"], 1)
                self.assertEqual(counts["added"], 0)
                self.assertIn(f"'{field}'", r.stderr)
        self.assertEqual(self.b.query_one("SELECT count(*) FROM memory")[0], 0)

    def test_type_outside_the_enum_is_rejected(self):
        for bad_type in ("arbitrary", "", None, 7, "FACT"):
            with self.subTest(type=bad_type):
                path = self._write_jsonl(self.b, "badtype.jsonl", [
                    _sync_row(id="dddddddd-0000-0000-0000-00000000000a", type=bad_type)])
                r = self.b.run("ingest-jsonl", "--in", path)
                counts = self._summary_counts(r.stdout)
                self.assertEqual(counts["malformed"], 1)
                self.assertEqual(counts["added"], 0)
                self.assertIn("'type'", r.stderr)

    def test_unknown_signal_is_normalized_to_none_not_rejected(self):
        """Recoverable, unlike type: the row keeps its content, it just loses
        its signal (and therefore gets the 'none' confidence default)."""
        path = self._write_jsonl(self.b, "badsignal.jsonl", [
            _sync_row(id="eeeeeeee-0000-0000-0000-00000000000a",
                      content="row with a made-up signal", signal="totally-made-up",
                      confidence=None),
            _sync_row(id="eeeeeeee-0000-0000-0000-00000000000b",
                      content="row with a non-string signal", signal=17,
                      confidence=None),
        ])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 2)
        for content in ("row with a made-up signal", "row with a non-string signal"):
            signal, confidence = self.b.query_one(
                "SELECT signal, confidence FROM memory WHERE content=?", (content,))
            self.assertEqual(signal, "none")
            self.assertEqual(confidence, 0.3)

    def test_content_over_the_size_cap_is_rejected(self):
        over = self._write_jsonl(self.b, "huge.jsonl", [
            _sync_row(id="ffffffff-0000-0000-0000-00000000000a",
                      content="X" * 65537)])
        r = self.b.run("ingest-jsonl", "--in", over)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["malformed"], 1)
        self.assertEqual(counts["added"], 0)
        self.assertIn("65536", r.stderr)

        # Exactly at the cap is fine -- the boundary is inclusive.
        at = self._write_jsonl(self.b, "atcap.jsonl", [
            _sync_row(id="ffffffff-0000-0000-0000-00000000000b",
                      content="Y" * 65536)])
        r = self.b.run("ingest-jsonl", "--in", at)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)

    def test_optional_field_with_a_wrong_type_is_rejected(self):
        for field in ("tags", "source_ref", "supersede_reason",
                      "valid_from", "ingestion_ts", "superseded_at"):
            with self.subTest(field=field):
                path = self._write_jsonl(self.b, "badopt.jsonl", [
                    _sync_row(id="99999999-0000-0000-0000-00000000000a",
                              **{field: {"nested": "object"}})])
                r = self.b.run("ingest-jsonl", "--in", path)
                counts = self._summary_counts(r.stdout)
                self.assertEqual(counts["malformed"], 1)
                self.assertEqual(counts["added"], 0)
                self.assertIn(f"'{field}'", r.stderr)

    def test_optional_fields_may_be_null_or_absent(self):
        row = _sync_row(id="99999999-0000-0000-0000-00000000000b",
                        content="row with nulls and absences everywhere",
                        tags=None, source_ref=None, supersede_reason=None,
                        valid_from=None, confidence=None)
        del row["ingestion_ts"]
        path = self._write_jsonl(self.b, "nulls.jsonl", [row])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        tags, source_ref, valid_from, ingestion_ts = self.b.query_one(
            "SELECT tags, source_ref, valid_from, ingestion_ts FROM memory WHERE id=?",
            (row["id"],))
        self.assertEqual((tags, source_ref), ("", ""))
        # An absent ingestion_ts becomes now, and valid_from defaults to it.
        self.assertTrue(ingestion_ts)
        self.assertEqual(valid_from, ingestion_ts)

    def test_far_future_ingestion_ts_is_clamped_to_now(self):
        """Recency is a ranking input (compute_score's exponential decay), so
        an unbounded future ingestion_ts is a permanent top-of-recall boost
        for whoever authored the sync file."""
        path = self._write_jsonl(self.b, "future.jsonl", [
            _sync_row(id="77777777-0000-0000-0000-00000000000a",
                      content="row claiming to be from the year 2999",
                      ingestion_ts="2999-01-01T00:00:00Z")])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        stored = self.b.query_one(
            "SELECT ingestion_ts FROM memory WHERE id=?",
            ("77777777-0000-0000-0000-00000000000a",))[0]
        self.assertNotEqual(stored, "2999-01-01T00:00:00Z")
        self.assertLess(stored, "2100-01-01T00:00:00Z")

    def test_unparsable_ingestion_ts_is_kept_verbatim(self):
        """Deliberately NOT an error: only a parsed, implausibly-future value
        is forgeable. compute_score already treats an unparsable timestamp as
        unknown-age/neutral, and older stores wrote other shapes."""
        path = self._write_jsonl(self.b, "weirdts.jsonl", [
            _sync_row(id="77777777-0000-0000-0000-00000000000b",
                      content="row with a timestamp shape we do not parse",
                      ingestion_ts="last tuesday")])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertEqual(self.b.query_one(
            "SELECT ingestion_ts FROM memory WHERE id=?",
            ("77777777-0000-0000-0000-00000000000b",))[0], "last tuesday")

    def test_secret_scan_runs_on_the_tombstoned_history_insert_path_too(self):
        """A tombstoned row's content is still written to disk and still
        readable via `get` / `list --include-superseded`, so 'it arrived dead'
        is not a reason to skip the advisory warning."""
        path = self._write_jsonl(self.b, "deadsecret.jsonl", [
            _sync_row(id="88888888-0000-0000-0000-00000000000a",
                      content="api_key = AKIAIOSFODNN7EXAMPLE1234567890",
                      superseded_at="2026-02-02T00:00:00Z",
                      supersede_reason="dead upstream")])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        self.assertIn("WARNING (advisory, write proceeded)", r.stderr)


# ---------------------------------------------------------------------------
# partial-file resilience: one bad row never eats the rest of the file
# ---------------------------------------------------------------------------
class PartialFileResilienceTest(_TwoStoreCase):
    def test_good_bad_good_ingests_both_good_rows_and_prints_the_summary(self):
        """The reviewer's exact repro shape: before the fix this aborted at row
        2 with NO summary line, and row 3 was silently lost -- the run looked
        like a crash, not like a partial import."""
        path = self._write_jsonl(self.b, "gbg.jsonl", [
            _sync_row(id="11111111-2222-3333-4444-555555555551",
                      content="good row one alpha"),
            _sync_row(id="11111111-2222-3333-4444-555555555552",
                      content={"not": "a string"}),
            _sync_row(id="11111111-2222-3333-4444-555555555553",
                      content="good row three gamma"),
        ])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertIn("[zmem] ingest-jsonl: added=", r.stdout,
                      "the summary line must print even when a row fails")
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 2)
        self.assertEqual(counts["malformed"], 1)
        self.assertIn("malformed line 2", r.stderr)

        ids = {row[0] for row in self.b.query_all("SELECT id FROM memory")}
        self.assertEqual(ids, {"11111111-2222-3333-4444-555555555551",
                               "11111111-2222-3333-4444-555555555553"})

    def test_row_that_raises_mid_apply_is_counted_and_the_file_continues(self):
        """Validation cannot catch everything: a lone UTF-16 surrogate is a
        legitimate str that json.loads accepts and _validate_sync_row passes,
        but sqlite3 refuses to bind it -- an exception raised INSIDE
        _ingest_row, after validation. It must be counted, reported with its
        line number, rolled back, and stepped over."""
        good1 = _sync_row(id="22222222-3333-4444-5555-666666666661",
                          content="good row before the exploding one")
        boom = _sync_row(id="22222222-3333-4444-5555-666666666662",
                         content="lone surrogate \ud800 row")
        good2 = _sync_row(id="22222222-3333-4444-5555-666666666663",
                          content="good row after the exploding one")

        path = os.path.join(self.b.tmp, "boom.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n",
                  errors="surrogatepass") as f:
            for row in (good1, boom, good2):
                f.write(json.dumps(row) + "\n")

        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["added"], 2)
        self.assertEqual(counts["malformed"], 1)
        self.assertIn("malformed line 2", r.stderr)

        ids = {row[0] for row in self.b.query_all("SELECT id FROM memory")}
        self.assertEqual(ids, {good1["id"], good2["id"]},
                         "the exploding row must be rolled back, not half-written")

    def test_every_row_malformed_still_prints_the_summary_and_exits_0(self):
        path = self._write_jsonl(self.b, "allbad.jsonl", [
            _sync_row(id="nope"), _sync_row(type="nonsense")])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["malformed"], 2)
        self.assertEqual(counts["added"], 0)


# ---------------------------------------------------------------------------
# tombstone authority: --allow-tombstones gates killing a LIVE local row
# ---------------------------------------------------------------------------
class TombstoneAuthorityTest(_TwoStoreCase):
    LIVE_ID = "abcdabcd-0000-0000-0000-00000000000a"
    CONTENT = "a live local row a remote outbox wants dead"

    def _seed_live(self) -> str:
        path = self._write_jsonl(self.b, "live.jsonl", [
            _sync_row(id=self.LIVE_ID, content=self.CONTENT)])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        return self._write_jsonl(self.b, "kill.jsonl", [
            _sync_row(id=self.LIVE_ID, content=self.CONTENT,
                      superseded_at="2026-02-02T00:00:00Z",
                      supersede_reason="the remote says so")])

    def test_tombstone_against_a_live_local_row_is_refused_without_the_flag(self):
        kill_path = self._seed_live()

        r = self.b.run("ingest-jsonl", "--in", kill_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstones_refused"], 1)
        self.assertEqual(counts["tombstoned"], 0)

        self.assertIsNone(
            self.b.query_one("SELECT superseded_at FROM memory WHERE id=?",
                             (self.LIVE_ID,))[0],
            "the local row must still be LIVE")
        self.assertIn("--allow-tombstones", r.stderr)
        self.assertIn(self.LIVE_ID, r.stderr)

        # And still recallable -- the refusal is real, not cosmetic.
        r = self.b.run("recall", "--query", "outbox wants dead",
                       "--namespace", NS, "--json")
        self.assertIn(self.LIVE_ID, [x["id"] for x in json.loads(r.stdout)])

    def test_same_file_with_the_flag_applies_the_tombstone(self):
        kill_path = self._seed_live()

        r = self.b.run("ingest-jsonl", "--in", kill_path, "--allow-tombstones")
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstoned"], 1)
        self.assertEqual(counts["tombstones_refused"], 0)
        self.assertEqual(
            self.b.query_one("SELECT superseded_at FROM memory WHERE id=?",
                             (self.LIVE_ID,))[0], "2026-02-02T00:00:00Z")

    def test_refusal_does_not_block_the_other_rows_in_the_same_file(self):
        self._seed_live()
        mixed = self._write_jsonl(self.b, "mixed.jsonl", [
            _sync_row(id=self.LIVE_ID, content=self.CONTENT,
                      superseded_at="2026-02-02T00:00:00Z"),
            _sync_row(id="abcdabcd-0000-0000-0000-00000000000b",
                      content="an ordinary new row riding along"),
        ])
        r = self.b.run("ingest-jsonl", "--in", mixed)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstones_refused"], 1)
        self.assertEqual(counts["added"], 1)

    def test_applied_tombstone_is_not_misreported_when_stdout_cannot_encode_the_reason(self):
        """`supersede_memory`'s confirmation print interpolates the incoming
        row's supersede_reason, which is REMOTE-authored. stdout is strict
        under a legacy codepage, so an unencodable character used to raise
        AFTER the commit -- and cmd_ingest_jsonl's per-row guard then counted
        a supersession that had actually landed as `malformed`. Same
        landed-but-reported-FAILED class as the ingest_harvest.py encoding
        bug, one process further in."""
        self._seed_live()
        kill_path = self._write_jsonl(self.b, "kill_greek.jsonl", [
            _sync_row(id=self.LIVE_ID, content=self.CONTENT,
                      superseded_at="2026-02-02T00:00:00Z",
                      supersede_reason="superseded by Ω-analysis")])

        self.b.env["PYTHONIOENCODING"] = "cp1252"
        self.addCleanup(self.b.env.pop, "PYTHONIOENCODING", None)

        r = self.b.run("ingest-jsonl", "--in", kill_path, "--allow-tombstones")
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["tombstoned"], 1)
        self.assertEqual(counts["malformed"], 0,
                         "a landed supersession must never be tallied as malformed")

        superseded_at, reason = self.b.query_one(
            "SELECT superseded_at, supersede_reason FROM memory WHERE id=?", (self.LIVE_ID,))
        self.assertEqual(superseded_at, "2026-02-02T00:00:00Z")
        # The STORE keeps the real UTF-8 reason; only the console echo degrades.
        self.assertEqual(reason, "superseded by Ω-analysis")

    def test_already_tombstoned_local_row_is_a_plain_skip_not_a_refusal(self):
        """Nothing live is at risk, so this is not the gated case."""
        path = self._write_jsonl(self.b, "dead.jsonl", [
            _sync_row(id="abcdabcd-0000-0000-0000-00000000000c",
                      content="arrived dead, stays dead",
                      superseded_at="2026-02-02T00:00:00Z")])
        r = self.b.run("ingest-jsonl", "--in", path)
        self.assertEqual(self._summary_counts(r.stdout)["added"], 1)
        r = self.b.run("ingest-jsonl", "--in", path)
        counts = self._summary_counts(r.stdout)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["tombstones_refused"], 0)


# ---------------------------------------------------------------------------
# scripts/ingest_harvest.py: child stdio encoding (MED-2)
# ---------------------------------------------------------------------------
# Lives in this file rather than a new tests/test_ingest_harvest.py to stay
# inside the permitted edit set for this change.
class IngestHarvestChildEncodingTest(unittest.TestCase):
    """store.py's `add` echoes the namespace on success. If the CHILD's stdio
    encoding cannot represent that namespace, the child dies with
    UnicodeEncodeError AFTER it has already committed -- so ingest_harvest.py
    sees a nonzero exit and reports a row that LANDED as FAILED. The operator
    then re-runs and the store gains a near-duplicate. Two fixes, both needed:
    PYTHONIOENCODING=utf-8 in the child's env, and an explicit utf-8/replace
    decode of the child's pipes in the parent."""

    # "project:<Japanese for 'Japanese'>" -- written as escapes to keep this
    # source ASCII-only. cp1252 cannot encode any of these codepoints.
    NS_NON_CP1252 = "project:日本語"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-harvest-enc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _base_env(self.tmp)
        # The legacy-console condition the reviewer reproduced, forced
        # deterministically (and portably) instead of depending on the host
        # codepage: the PARENT inherits a cp1252 stdio encoding.
        self.env["PYTHONIOENCODING"] = "cp1252"
        self.store_path = os.path.join(self.tmp, "store.sqlite")

    def test_precondition_a_cp1252_child_really_does_die_on_this_namespace(self):
        """INVERTED PRECONDITION TEST -- it asserts store.py *fails*.

        Its only job is to prove this fixture still reproduces the hazard, so
        that the test below is actually testing something. If store.py's own
        stdio is ever hardened (a good change), this becomes a SKIP with an
        explanation rather than a mystery red -- the fix under test here is
        ingest_harvest.py's, and it stays valid either way.
        """
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "add", "--namespace", self.NS_NON_CP1252,
             "--type", "fact", "--content", "control row", "--signal", "test"],
            env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if r.returncode == 0:
            self.skipTest(
                "store.py no longer dies on a non-cp1252 namespace under a "
                "strict-stdio child; this fixture can no longer demonstrate "
                "the hazard that ingest_harvest.py's env fix defends against")
        self.assertIn("UnicodeEncodeError", r.stderr)
        # ...and it died AFTER the write: the row is in the store anyway.
        # That is what makes the bug nasty -- the caller sees a failure for a
        # row that actually landed.
        conn = sqlite3.connect(self.store_path)
        try:
            count = conn.execute(
                "SELECT count(*) FROM memory WHERE content=?", ("control row",)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1, "the crash happened after the commit")

    def test_harvest_ingest_survives_a_non_cp1252_namespace(self):
        harvest = [{
            "namespace": self.NS_NON_CP1252,
            "type": "lesson",
            "content": "a harvested lesson with a non-cp1252 namespace",
            "tags": "encoding",
            "signal": "test",
            "why": "regression fixture",
        }]
        harvest_path = os.path.join(self.tmp, "harvest.json")
        Path(harvest_path).write_text(json.dumps(harvest), encoding="utf-8")

        r = subprocess.run(
            [PYTHON, str(REPO_ROOT / "scripts" / "ingest_harvest.py"),
             harvest_path, "--store", str(STORE_PY)],
            env=self.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertIn("added row 1", r.stdout)
        self.assertNotIn("FAILED", r.stdout + r.stderr)

        conn = sqlite3.connect(self.store_path)
        try:
            row = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("a harvested lesson with a non-cp1252 namespace",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "the row must have landed exactly once")
        self.assertEqual(row[0], self.NS_NON_CP1252)

    def test_oversized_harvest_content_is_rejected(self):
        harvest = [{
            "namespace": "project:harvestcap", "type": "lesson",
            "content": "Z" * 65537, "tags": "", "signal": "test", "why": "too big",
        }]
        harvest_path = os.path.join(self.tmp, "huge.json")
        Path(harvest_path).write_text(json.dumps(harvest), encoding="utf-8")

        env = _base_env(self.tmp)
        r = subprocess.run(
            [PYTHON, str(REPO_ROOT / "scripts" / "ingest_harvest.py"),
             harvest_path, "--store", str(STORE_PY)],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("REJECTED", r.stderr)
        self.assertIn("65536", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
