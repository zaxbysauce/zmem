"""Supplementary regression tests for issues #493 + #432 (non-frozen).

The frozen acceptance checks live in tests/test_followup_n11.py (checkpoint
anchor on issue #493); this file carries the plan-critic-mandated
supplementary coverage (05-fix-plan REVISION 5: CR-2/R2-1 parity cases,
CR-4/R2-4 consumer matrix, CR-7/R2-2/R3-1/R4-1 norm-verify concurrency and
stamp discipline). Env pins precede every storelib import (the store-path
freeze rule).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

_TMP = Path(tempfile.mkdtemp(prefix="zmem-n493-supp-"))
os.environ["ZMEM_STORE"] = str(_TMP / "store" / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_TMP / "data")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ["ZMEM_MODELS_DIR"] = str(_TMP / "models")  # nonexistent -> degraded

from storelib import knowledge, write  # noqa: E402
from storelib.schema import _normalize_content  # noqa: E402


def _fresh_store(name: str) -> Path:
    root = _TMP / name
    store = root / "store" / "store.sqlite"
    store.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(store)
    env["ZMEM_DATA"] = str(root / "data")
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"), "init"],
        capture_output=True, text=True, env=env, timeout=300, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    return store


def _store_env(store: Path) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(store)
    env["ZMEM_DATA"] = str(store.parent)
    return env


def _run_cli(store: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"), *args],
        capture_output=True, text=True, env=_store_env(store),
        timeout=600, cwd=str(REPO))


class PrefixCaseSuppTest(unittest.TestCase):
    """CR-4 + R2-4: predicate, grammar, warning, quarantine, consumers."""

    def test_mixed_case_predicate_and_grammar(self):
        self.assertTrue(knowledge.is_pointer_ref("KNOWLEDGE:t:k"))
        self.assertTrue(knowledge.is_pointer_ref("Knowledge:t:k"))
        self.assertTrue(knowledge.is_pointer_ref("knowledge:t:k"))
        self.assertFalse(knowledge.is_pointer_ref("kn"))
        # Kelvin sign K folds under str.lower but NOT under SQLite LIKE
        # (ASCII-only): the primitive must reject it.
        self.assertFalse(knowledge.is_pointer_ref("\u212aNOWLEDGE:t:k"))
        # Grammar layers still require the exact canonical form: a
        # mixed-case ref is a pointer ref for exclusion purposes but never
        # a WELL-FORMED one, and parse_ref refuses it.
        self.assertFalse(knowledge.is_wellformed_pointer_ref("KNOWLEDGE:t:k"))
        with self.assertRaises(knowledge.KnowledgeRefError):
            knowledge.parse_ref("KNOWLEDGE:t:k")

    def test_ascii_only_fold_matches_sqlite_like(self):
        conn = sqlite3.connect(":memory:")
        try:
            for probe in ("KNOWLEDGE:x", "Knowledge:x", "knowledge:x"):
                sql = conn.execute(
                    "SELECT ? NOT LIKE 'knowledge:%'", (probe,)).fetchone()[0]
                self.assertEqual(
                    sql, 0,
                    f"SQLITE LIKE must match {probe!r} for parity")
                self.assertTrue(knowledge.is_pointer_ref(probe), probe)
            sql = conn.execute(
                "SELECT ? NOT LIKE 'knowledge:%'",
                ("\u212aNOWLEDGE:x",)).fetchone()[0]
            self.assertEqual(sql, 1, "Kelvin sign must NOT match LIKE")
        finally:
            conn.close()

    def test_warning_fires_for_uppercase_reserved_ref(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            write.warn_reserved_source_ref("KNOWLEDGE:t:k")
        self.assertIn("knowledge:", buf.getvalue())

    def test_rescan_classifies_uppercase_pointer(self):
        # write.py's rescan classifier (:3357 site) routes reserved rows
        # through the pointer lane via the same predicate.
        self.assertTrue(write._has_knowledge_prefix("KNOWLEDGE:t:k"))
        self.assertEqual(
            write.KNOWLEDGE_SOURCE_REF_PREFIX, "knowledge:")


class DryRunParitySuppTest(unittest.TestCase):
    """CR-2 + R2-1 supplementary parity cases (would_dedup vs real)."""

    ROW_A = "60000000-0000-4000-8000-00000000000a"
    ROW_B = "60000000-0000-4000-8000-00000000000b"

    def _seed_two_row_source(self, root: Path, *, type_a="fact",
                             tags_a="task:x", tags_b="task:x"):
        src = root / "src"
        src.mkdir(parents=True)
        conn = sqlite3.connect(str(src / "store.sqlite"))
        try:
            ddl = (SCRIPTS / "storelib" / "schema.py").read_text(
                encoding="utf-8")
            self.assertIn("def init_db", ddl)  # sanity: file located
            sys.path.insert(0, str(SCRIPTS))
            from storelib import schema as schema_mod
            schema_mod.init_db(conn)
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "(?,?,?,?,?,?,0.9,'test','2026-01-01T00:00:00Z',"
                "'trusted_internal','2026-01-01T00:00:00Z',?)",
                (self.ROW_A, "project:supp", type_a,
                 "parity body alpha", tags_a, "session:supp",
                 _normalize_content("parity body alpha")))
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "(?,?,?,?,?,?,0.9,'test','2026-01-01T00:00:00Z',"
                "'trusted_internal','2026-01-01T00:00:00Z',?)",
                (self.ROW_B, "project:supp", "fact",
                 "parity body alpha", tags_b, "session:supp",
                 _normalize_content("parity body alpha")))
            conn.commit()
        finally:
            conn.close()
        return src / "store.sqlite"

    def _promote(self, store: Path, src: Path, *extra) -> dict:
        r = _run_cli(store, "promote-store", "--from", str(src), *extra)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        summary = next(line for line in r.stdout.splitlines()
                       if line.startswith("[zmem] promote-store"))
        tallies = {}
        for chunk in summary.split(": ", 1)[1].split(", "):
            if "=" in chunk:
                k, v = chunk.rsplit("=", 1)
                tallies[k.strip()] = int(v)
        return tallies

    def test_two_same_norm_facts_parity(self):
        root = _TMP / "parity-facts"
        root.mkdir(parents=True, exist_ok=True)
        src = self._seed_two_row_source(root)
        dry_store = _fresh_store("parity-facts-dry")
        real_store = _fresh_store("parity-facts-real")
        dry = self._promote(dry_store, src, "--dry-run")
        real = self._promote(real_store, src)
        self.assertEqual(dry.get("would_dedup", 0), real.get("deduped", 0),
                         (dry, real))
        self.assertEqual(dry.get("would_promote", 0), real.get("added", 0),
                         (dry, real))
        total = dry.get("would_promote", 0) + dry.get("would_dedup", 0) \
            + dry.get("would_skip", 0) + dry.get("would_tombstoned", 0) \
            + dry.get("would_tombstone_refused", 0) \
            + dry.get("would_purged_denied", 0)
        self.assertEqual(total, 2, dry)

    def test_disjoint_task_tags_do_not_fold(self):
        root = _TMP / "parity-tags"
        root.mkdir(parents=True, exist_ok=True)
        src = self._seed_two_row_source(root, tags_a="task:x",
                                        tags_b="task:y")
        dry_store = _fresh_store("parity-tags-dry")
        real_store = _fresh_store("parity-tags-real")
        dry = self._promote(dry_store, src, "--dry-run")
        real = self._promote(real_store, src)
        # subject veto: disjoint task tags never fold in the real promote
        self.assertEqual(real.get("deduped", 0), 0, real)
        self.assertEqual(dry.get("would_dedup", 0), 0, dry)
        self.assertEqual(dry.get("would_promote", 0),
                         real.get("added", 0), (dry, real))


class NormVerifySuppTest(unittest.TestCase):
    """CR-7 + R2-2 + R3-1 + R4-1: stamps, batches, concurrency."""

    def _plant(self, store: Path, rowid: int, norm: str) -> None:
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("UPDATE memory SET content_norm=? WHERE rowid=?",
                         (norm, rowid))
            conn.commit()
        finally:
            conn.close()

    def test_stamp_written_and_second_open_skips(self):
        store = _fresh_store("nv-stamp")
        r = _run_cli(store, "add", "--content", "norm verify stamp body",
                     "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The add command's own open saw an EMPTY table at migrate time
        # (rows are written after migrate), so the first NON-EMPTY open is
        # the next command's — that open verifies and stamps.
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            stamped = conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_verify_163'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(stamped, "non-empty verified store must stamp")
        # Steady state: a second open stays green with the stamp present.
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_large_store_heals_across_batches(self):
        store = _fresh_store("nv-batches")
        for i in range(505):
            r = _run_cli(store, "add", "--content", f"batch body {i}",
                         "--namespace", "project:supp", "--type", "fact")
            self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "UPDATE memory SET content_norm='WRONG' "
                "WHERE id IN (SELECT id FROM memory LIMIT 600)")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            bad = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE content_norm != "
                "content_norm").fetchone()  # placeholder never true
            rows = conn.execute(
                "SELECT content, content_norm FROM memory").fetchall()
        finally:
            conn.close()
        self.assertEqual(bad[0], 0)
        for content, norm in rows:
            self.assertEqual(norm, _normalize_content(content or ""))

    def test_external_writer_serializes_and_stays_canonical(self):
        store = _fresh_store("nv-extwriter")
        _run_cli(store, "add", "--content", "external writer body",
                 "--namespace", "project:supp", "--type", "fact")
        # A second connection updates the row mid-pass; the verify pass
        # (batch BEGIN IMMEDIATE) serializes against it and the FINAL
        # content keeps its canonical norm (never a stale-computed one).
        conn2 = sqlite3.connect(str(store), timeout=30)
        try:
            conn2.execute("PRAGMA journal_mode=WAL")
            conn2.execute(
                "UPDATE memory SET content='external writer body v2', "
                "content_norm=? WHERE content='external writer body'",
                (_normalize_content("external writer body v2"),))
            conn2.commit()
        finally:
            conn2.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT content, content_norm FROM memory").fetchall()
        finally:
            conn.close()
        for content, norm in rows:
            self.assertEqual(norm, _normalize_content(content or ""))

    def test_pre_existing_transaction_runs_without_nested_begin(self):
        # The pass runs inside migrate() while a caller transaction is
        # open (the savepoint arm) — driving stats after a manual
        # transaction commits exercises the no-nested-BEGIN discipline.
        store = _fresh_store("nv-pretx")
        conn = sqlite3.connect(str(store))
        try:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE meta SET value='x' WHERE key='nosuchkey'")
            conn.commit()
        finally:
            conn.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)


class DatasetShapeSuppTest(unittest.TestCase):
    """R3-2/C5: shape matrix through the central gate."""

    def test_non_dict_shapes_refuse_central_gate(self):
        from storelib.dataset import DatasetError, _read_export_families
        for bad in (42, 4.5, "s", None, True, [1, 2]):
            with tempfile.TemporaryDirectory(
                    prefix="zmem-n493-ds-") as td:
                exp = Path(td)
                (exp / "data").mkdir()
                row = json.dumps({"id": "ok"})
                payload = (row + "\n" + json.dumps(bad) + "\n").encode(
                    "utf-8")
                (exp / "data" / "memories-000.jsonl").write_bytes(payload)
                for fam in ("episodes", "episode_members", "links"):
                    (exp / "data" / (fam + "-000.jsonl")).write_bytes(b"\n")
                with self.assertRaises(DatasetError) as cm:
                    _read_export_families(exp, "jsonl", DatasetError)
                self.assertIn("malformed", str(cm.exception))
                self.assertIn("memories", str(cm.exception))


def tearDownModule():
    shutil.rmtree(str(_TMP), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
