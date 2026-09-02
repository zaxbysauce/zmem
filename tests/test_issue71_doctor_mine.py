"""Issue #71 E + I: promote-store, doctor second-stores, mine-history adapters.

E — `promote-store --from <path>`: one-shot idempotent merge of a leftover
second store into the canonical one (source ids PRESERVED, validated through
the same _validate_sync_row contract ingest-jsonl uses, capture-mode manual,
newer source schemas refused). doctor's `second-stores` check FAILS when a
candidate store holds live rows missing from canonical and WARNs when the
second store is fully contained (stale copy).

I — `mine-history --source codex|hermes`: review-queue candidates ONLY from a
curated Codex MEMORY.md (three known sections; raw_memories.md refused) and
Hermes session JSONL (bounded; correction rules shared with the live hook).
Dedup-key idempotent on re-runs.

All stores/queues live under throwaway temp dirs (never the box store).
Runs standalone: python tests/test_issue71_doctor_mine.py
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
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

STRIP = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_CODEX_MEMORY",
         "ZMEM_HERMES_SESSIONS", "ZMEM_MODEL_AUTODOWNLOAD",
         "ZMEM_MODELS_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _run(env: dict, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(STORE_PY), *args],
                          capture_output=True, text=True, env=env,
                          timeout=120, **kw)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-i71-em-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _clean_env(self.tmp)
        self.assertEqual(_run(self.env, "stats").returncode, 0)


class PromoteStoreTest(_StoreCase):
    def _make_source_store(self, dir_name: str) -> tuple[str, str, str]:
        """A second store with one UNIQUE row and one row whose id already
        exists canonically (via ingest-jsonl). Returns (path, unique_id,
        shared_id)."""
        src_dir = os.path.join(self.tmp, dir_name)
        os.makedirs(src_dir, exist_ok=True)
        src_env = _clean_env(src_dir)
        self.assertEqual(_run(src_env, "stats").returncode, 0)
        shared_id = str(uuid.uuid4())
        unique_id = str(uuid.uuid4())
        rows = [
            {"id": shared_id, "namespace": "user:global", "type": "fact",
             "content": "promoteshared row already canonical",
             "signal": "test", "confidence": 0.8, "source_ref": "db:x:1"},
            {"id": unique_id, "namespace": "user:global", "type": "lesson",
             "content": "promoteunique row stranded in the second store",
             "signal": "test", "confidence": 0.8, "source_ref": "db:x:2"},
        ]
        jl = os.path.join(src_dir, "seed.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        r = _run(src_env, "ingest-jsonl", "--in", jl, "--capture-mode",
                 "auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        return os.path.join(src_dir, "store.sqlite"), unique_id, shared_id

    def test_promote_adds_unique_row_and_is_idempotent(self):
        src, unique_id, shared_id = self._make_source_store("second")
        # Canonical: ingest the shared row only.
        jl = os.path.join(self.tmp, "shared.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": shared_id, "namespace": "user:global", "type": "fact",
                "content": "promoteshared row already canonical",
                "signal": "test", "confidence": 0.8,
                "source_ref": "db:x:1"}) + "\n")
        self.assertEqual(_run(self.env, "ingest-jsonl", "--in", jl,
                              "--capture-mode", "auto").returncode, 0)

        r = _run(self.env, "promote-store", "--from", src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added=1", r.stdout)
        self.assertIn("skipped=1", r.stdout)

        # Re-run: fully idempotent (source ids preserved) — every row takes
        # the existing-local-row short-circuit, nothing re-added.
        r2 = _run(self.env, "promote-store", "--from", src)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("skipped=2", r2.stdout)
        self.assertNotIn("added=1", r2.stdout)

        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        try:
            row = conn.execute("SELECT id, content FROM memory WHERE id=?",
                               (unique_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "unique row must be promoted with its "
                                   "SOURCE id (idempotency contract)")
        self.assertEqual(row[1],
                         "promoteunique row stranded in the second store")

    def test_promote_dry_run_writes_nothing(self):
        # PRR-015 reviewer gate: the source carries a memory_link row too, so
        # the associated-table promotion path is exercised — a dry-run must
        # not write memory rows OR assoc rows.
        src_dir = os.path.join(self.tmp, "second-dry")
        os.makedirs(src_dir, exist_ok=True)
        src = os.path.join(src_dir, "store.sqlite")
        src_env = _clean_env(src_dir)
        self.assertEqual(_run(src_env, "stats").returncode, 0)
        row_id = str(uuid.uuid4())
        rows = [
            {"id": row_id, "namespace": "user:global", "type": "lesson",
             "content": "dryrun assoc unique row", "signal": "test",
             "confidence": 0.8, "source_ref": "db:d:1"},
        ]
        jl = os.path.join(src_dir, "seed.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.assertEqual(_run(src_env, "ingest-jsonl", "--in", jl,
                              "--capture-mode", "auto").returncode, 0)
        # Seed one memory_link row in the source (schema: src_id/dst_id/
        # relation/score/created_at, UNIQUE triple, src != dst).
        sconn = sqlite3.connect(src)
        try:
            sconn.execute(
                "INSERT OR IGNORE INTO memory_link (src_id, dst_id, "
                "relation, score, created_at) VALUES (?, ?, ?, ?, ?)",
                (row_id, "ffff0000-0000-4000-8000-000000000002",
                 "related", 0.5, "2026-09-01"))
            sconn.commit()
        finally:
            sconn.close()

        r = _run(self.env, "promote-store", "--from", src, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would_promote=1", r.stdout)
        self.assertIn("memory_link=1", r.stdout)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        try:
            n_mem = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            n_link = conn.execute(
                "SELECT COUNT(*) FROM memory_link").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_mem, 0, "dry-run must not write memory rows")
        self.assertEqual(n_link, 0, "dry-run must not write assoc rows")

    def test_promote_carries_associated_tables(self):
        # PRR-015: links + episode containers/memberships are promoted with
        # INSERT OR IGNORE idempotency when both stores carry the tables.
        src_dir = os.path.join(self.tmp, "assoc-store")
        os.makedirs(src_dir, exist_ok=True)
        src = os.path.join(src_dir, "store.sqlite")
        src_env = _clean_env(src_dir)
        self.assertEqual(_run(src_env, "stats").returncode, 0)
        row_id = str(uuid.uuid4())
        jl = os.path.join(src_dir, "seed.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": row_id, "namespace": "user:global", "type": "fact",
                "content": "assoc unique row", "signal": "test",
                "confidence": 0.8, "source_ref": "db:a:1"}) + "\n")
        self.assertEqual(_run(src_env, "ingest-jsonl", "--in", jl,
                              "--capture-mode", "auto").returncode, 0)
        sconn = sqlite3.connect(src)
        try:
            # Second row so the link's CHECK (src_id != dst_id) passes.
            sconn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, ingestion_ts) VALUES "
                "(?, 'user:global', 'fact', 'assoc second row', '', 'db:a:2', "
                "0.8, 'test', '2026-09-01')",
                ("ffff0000-0000-4000-8000-000000000002",))
            sconn.execute(
                "INSERT OR IGNORE INTO memory_link (src_id, dst_id, "
                "relation, score, created_at) VALUES (?, ?, ?, ?, ?)",
                (row_id, "ffff0000-0000-4000-8000-000000000002",
                 "related", 0.5, "2026-09-01"))
            sconn.commit()
        finally:
            sconn.close()

        r = _run(self.env, "promote-store", "--from", src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("memory_link=1", r.stdout)
        # Re-run: assoc rows are INSERT OR IGNORE-idempotent too.
        r2 = _run(self.env, "promote-store", "--from", src)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        try:
            n_link = conn.execute(
                "SELECT COUNT(*) FROM memory_link").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_link, 1, "re-run must not duplicate assoc rows")

    def test_promote_missing_source_fails_clean(self):
        r = _run(self.env, "promote-store", "--from",
                 os.path.join(self.tmp, "nope.sqlite"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stdout + r.stderr)

    def test_promote_refuses_newer_source_schema(self):
        # COV-008: a source whose meta.schema_version is NEWER than the
        # destination's must be refused (no trust demotion).
        src_dir = os.path.join(self.tmp, "future-store")
        os.makedirs(src_dir, exist_ok=True)
        src = os.path.join(src_dir, "store.sqlite")
        src_env = _clean_env(src_dir)
        self.assertEqual(_run(src_env, "stats").returncode, 0)
        conn = sqlite3.connect(src)
        try:
            conn.execute("UPDATE meta SET value='99' "
                         "WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        r = _run(self.env, "promote-store", "--from", src)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("refusing", r.stdout + r.stderr)
        self.assertIn("newer", r.stdout + r.stderr)

    def test_promote_malformed_row_exits_nonzero(self):
        # PRR-012: a row the validator rejects must make the command exit
        # non-zero — a partial merge must not look complete.
        src_dir = os.path.join(self.tmp, "bad-store")
        os.makedirs(src_dir, exist_ok=True)
        src_env = _clean_env(src_dir)
        self.assertEqual(_run(src_env, "stats").returncode, 0)
        jl = os.path.join(src_dir, "bad.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            # Non-UUID id -> rejected as malformed by _validate_sync_row.
            f.write(json.dumps({
                "id": "not-a-uuid", "namespace": "user:global",
                "type": "fact", "content": "badid row",
                "signal": "none", "confidence": 0.5}) + "\n")
        r = _run(src_env, "ingest-jsonl", "--in", jl, "--capture-mode",
                 "auto")
        # The bad row never enters the SOURCE via the normal path either;
        # seed it directly so promote-store sees it.
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(os.path.join(src_dir, "store.sqlite"))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, ingestion_ts) VALUES "
                "('bad-id', 'user:global', 'fact', 'badid row', '', '', "
                "0.5, 'none', '2026-09-01')")
            conn.commit()
        finally:
            conn.close()
        r2 = _run(self.env, "promote-store", "--from",
                  os.path.join(src_dir, "store.sqlite"))
        self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
        self.assertIn("malformed=1", r2.stdout)


class DoctorSecondStoresTest(_StoreCase):
    def _check(self, candidates):
        """Run the check with Path.home redirected at the temp dir so the
        box's REAL legacy stores (~/.zcode/memory — which legitimately holds
        349 live rows on this operator's machine, the exact field report this
        check exists for) cannot leak into the fixture assertions."""
        import doctor
        from unittest import mock
        with mock.patch.object(doctor.Path, "home", return_value=Path(self.tmp)):
            return doctor._check_second_stores(
                Path(self.env["ZMEM_STORE"]), extra_candidates=candidates)

    def test_unique_live_row_fails(self):
        second = os.path.join(self.tmp, "second.sqlite")
        env2 = _clean_env(os.path.join(self.tmp, "second-dir"))
        os.makedirs(env2["ZMEM_DATA"], exist_ok=True)
        env2["ZMEM_STORE"] = second
        self.assertEqual(_run(env2, "stats").returncode, 0)
        subprocess.run(
            [sys.executable, str(STORE_PY), "add", "--namespace",
             "user:global", "--type", "fact", "--content",
             "secondstore unique live row", "--signal", "test"],
            capture_output=True, text=True, env=env2, check=True)
        check = self._check([Path(second)])
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["details"]["second_stores"][0]
                         ["missing_in_canonical"], 1)

    def test_absent_candidate_skips(self):
        check = self._check([Path(os.path.join(self.tmp, "ghost.sqlite"))])
        self.assertEqual(check["status"], "skip")

    def test_contained_second_store_warns(self):
        # COV-009: a leftover store whose rows ALL exist canonically is a
        # stale copy — warn (promotable/retirable), never fail.
        second = os.path.join(self.tmp, "contained.sqlite")
        env2 = _clean_env(os.path.join(self.tmp, "contained-dir"))
        os.makedirs(env2["ZMEM_DATA"], exist_ok=True)
        env2["ZMEM_STORE"] = second
        self.assertEqual(_run(env2, "stats").returncode, 0)
        # Seed the SAME content canonically first, then in the second store
        # via ingest with a fixed id so both sides hold the identical row.
        fixed_id = "abababab-1111-4111-8111-111111111111"
        row = {"id": fixed_id, "namespace": "user:global", "type": "fact",
               "content": "contained second-store shared row",
               "signal": "test", "confidence": 0.8, "source_ref": "db:c:1"}
        jl = os.path.join(self.tmp, "contained.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        self.assertEqual(_run(self.env, "ingest-jsonl", "--in", jl,
                              "--capture-mode", "auto").returncode, 0)
        self.assertEqual(_run(env2, "ingest-jsonl", "--in", jl,
                              "--capture-mode", "auto").returncode, 0)
        check = self._check([Path(second)])
        self.assertEqual(check["status"], "warn", check["summary"])
        self.assertEqual(check["details"]["second_stores"][0]
                         ["missing_in_canonical"], 0)


CODEX_MEMORY = """# Codex memory

## User preferences
- prefer the shared fleet store over per-tool memory files

## Reusable knowledge
- the ingest lane is idempotent on source ids

## Failures and how to do differently
- never bulk-import raw model memory; precision beats coverage

## Ignored section
- this section is not in the allowlist and must be skipped
"""


class MineAdaptersTest(_StoreCase):
    def test_codex_sections_to_candidates_and_queue(self):
        mem = os.path.join(self.tmp, "MEMORY.md")
        with open(mem, "w", encoding="utf-8") as f:
            f.write(CODEX_MEMORY)
        r = _run(self.env, "mine-history", "--source", "codex",
                 "--transcript-dir", mem, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        by_content = {c["message"]: c["type"] for c in report["candidates"]}
        self.assertEqual(by_content.get(
            "prefer the shared fleet store over per-tool memory files"),
            "preference")
        self.assertEqual(by_content.get(
            "never bulk-import raw model memory; precision beats coverage"),
            "lesson")
        # The ignored section never becomes a candidate.
        self.assertNotIn(
            "this section is not in the allowlist and must be skipped",
            by_content)
        # Queue it: host=codex, dedup-key idempotent.
        r2 = _run(self.env, "mine-history", "--source", "codex",
                  "--transcript-dir", mem, "--queue")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        q = os.path.join(self.tmp, "queue")
        files = [f for f in os.listdir(q) if f.endswith(".json")]
        items = json.loads(Path(q, files[0]).read_text(encoding="utf-8"))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["host"], "codex")
        # Re-run: nothing new appended.
        r3 = _run(self.env, "mine-history", "--source", "codex",
                  "--transcript-dir", mem, "--queue")
        self.assertEqual(r3.returncode, 0, r3.stderr)
        items2 = json.loads(Path(q, files[0]).read_text(encoding="utf-8"))
        self.assertEqual(len(items2), 3, "dedup_key idempotency")

    def test_raw_memories_refused(self):
        mem = os.path.join(self.tmp, "raw_memories.md")
        with open(mem, "w", encoding="utf-8") as f:
            f.write("## User preferences\n- x\n")
        r = _run(self.env, "mine-history", "--source", "codex",
                 "--transcript-dir", mem)
        self.assertEqual(r.returncode, 1)
        self.assertIn("raw_memories.md", r.stderr)

    def test_hermes_sessions_jsonl_candidates(self):
        sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(sessions, exist_ok=True)
        turns = [
            {"role": "user", "content":
             "remember: the deploy window opens after the audit"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "ok"},
            "not-even-a-dict",
            "{broken json",
        ]
        jl = os.path.join(sessions, "session-1.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            for t in turns:
                f.write(t if isinstance(t, str) else json.dumps(t))
                f.write("\n")
        r = _run(self.env, "mine-history", "--source", "hermes",
                 "--transcript-dir", sessions, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        # Critic #10: at least ONE candidate emerges from a known shape.
        self.assertGreaterEqual(report["count"], 1)
        self.assertIn("deploy window", report["candidates"][0]["message"])

    def test_hermes_missing_dir_fails_clean(self):
        r = _run(self.env, "mine-history", "--source", "hermes",
                 "--transcript-dir", os.path.join(self.tmp, "ghost"))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
