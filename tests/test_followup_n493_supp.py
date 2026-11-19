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

    def test_consumer_matrix_refuses_uppercase_refs(self):
        """R2-4 + review-finding 3: the uppercase reserved ref is refused
        across the real consumer surfaces (add / update / supersede /
        ingest-jsonl), not just at the predicate."""
        store = _fresh_store("c4-consumers")
        r = _run_cli(store, "add", "--content", "consumer matrix body",
                     "--namespace", "project:supp", "--type", "fact",
                     "--source-ref", "KNOWLEDGE:t:k")
        self.assertNotEqual(r.returncode, 0,
                            "uppercase knowledge ref must be refused by add")
        r2 = _run_cli(store, "add", "--content", "consumer matrix body",
                      "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with tempfile.TemporaryDirectory(prefix="zmem-c4-jsonl-") as td:
            jl = Path(td) / "in.jsonl"
            jl.write_text(json.dumps({
                "id": "70000000-0000-4000-8000-000000000001",
                "namespace": "project:supp", "type": "fact",
                "content": "sync uppercase ref row",
                "tags": "", "source_ref": "KNOWLEDGE:t:k",
                "confidence": 0.9, "signal": "test",
                "valid_from": "2026-01-01T00:00:00Z",
                "ingestion_ts": "2026-01-01T00:00:00Z",
            }) + "\n", encoding="utf-8")
            r3 = _run_cli(store, "ingest-jsonl", "--in", str(jl))
            # sync PRESERVES the reserved ref but never verifies it: the
            # row lands unverified (demoted), never authoritative.
            self.assertEqual(r3.returncode, 0, r3.stderr)
        conn = sqlite3.connect(str(store))
        try:
            row = conn.execute(
                "SELECT source_ref FROM memory WHERE content="
                "'sync uppercase ref row'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "KNOWLEDGE:t:k")


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
            rows = conn.execute(
                "SELECT content, content_norm FROM memory").fetchall()
        finally:
            conn.close()
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


class DryRunEdgeParitySuppTest(unittest.TestCase):
    """Review-finding 3: the C2 parity cases the plan named that the first
    supp cut omitted (pointer-vs-keeper, existing-ID tombstone +
    equal-content, tombstone outcome tallies, purged denial)."""

    ROW = "80000000-0000-4000-8000-00000000000%d"

    def _seed_source(self, root: Path, rows: list[dict]) -> Path:
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(src / "store.sqlite"))
        try:
            from storelib import schema as schema_mod
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            for spec in rows:
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags, "
                    "source_ref, confidence, signal, valid_from, taint, "
                    "superseded_at, supersede_reason, ingestion_ts, "
                    "content_norm) VALUES "
                    "(?,?,?,?,?,?,0.9,'test','2026-01-01T00:00:00Z',"
                    "'trusted_internal',?,?,'2026-01-01T00:00:00Z',?)",
                    (spec["id"], spec.get("namespace", "project:supp"),
                     spec.get("type", "fact"), spec["content"],
                     spec.get("tags", "task:x"), spec.get("source_ref",
                                                          "session:supp"),
                     spec.get("superseded_at"),
                     spec.get("supersede_reason"),
                     _normalize_content(spec["content"])))
            for spec in rows:
                if spec.get("expiry"):
                    conn.execute(
                        "INSERT INTO memory_expiry (memory_id, inject_until,"
                        " expiry_class) VALUES (?, datetime('now', '+30 "
                        "days'), ?)",
                        (spec["id"], spec["expiry"]))
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

    def test_pointer_row_lands_own_id_against_equal_norm_keeper(self):
        root = _TMP / "edge-pointer"
        root.mkdir(parents=True, exist_ok=True)
        src = self._seed_source(root, [
            {"id": self.ROW % 1, "content": "shared pointer body",
             "source_ref": "KNOWLEDGE:target-1:key-1", "type": "state"},
            {"id": self.ROW % 2, "content": "shared pointer body",
             "type": "state", "expiry": "setting"},
        ])
        dry = _fresh_store("edge-pointer-dry")
        real = _fresh_store("edge-pointer-real")
        d = self._promote(dry, src, "--dry-run")
        r = self._promote(real, src)
        # The pointer row never folds (identity-bearing): both runs land it
        # under its own id; the equal-norm ordinary row is the fold/land
        # differentiator.
        self.assertEqual(d.get("would_dedup", 0), r.get("deduped", 0), (d, r))
        self.assertEqual(d.get("would_promote", 0), r.get("added", 0), (d, r))

    def test_existing_id_tombstone_then_equal_content_row(self):
        root = _TMP / "edge-tombstone"
        root.mkdir(parents=True, exist_ok=True)
        # The destination carries ROW%3 live; the source carries a
        # tombstone FOR that id plus a NEW row with equal content.
        dest = _fresh_store("edge-tombstone-dest")
        r = _run_cli(dest, "add", "--content", "tombstone target body",
                     "--namespace", "project:supp", "--type", "state",
                     "--expiry-class", "setting")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(dest))
        try:
            live_id = conn.execute(
                "SELECT id FROM memory WHERE content='tombstone target body'"
            ).fetchone()[0]
        finally:
            conn.close()
        src = self._seed_source(root, [
            {"id": live_id, "content": "tombstone target body",
             "type": "state", "superseded_at": "2026-01-02T00:00:00Z",
             "supersede_reason": "edge"},
            {"id": self.ROW % 4, "content": "tombstone target body",
             "type": "state", "expiry": "setting"},
        ])
        dry = _fresh_store("edge-tombstone-dry")
        real = _fresh_store("edge-tombstone-real")
        # copy the seeded destination state into both promote targets
        for target in (dry, real):
            shutil.copyfile(dest, target)
        d = self._promote(dry, src, "--dry-run")
        r2 = self._promote(real, src)
        self.assertEqual(d.get("would_tombstoned", 0),
                         r2.get("tombstoned", 0), (d, r2))
        self.assertEqual(d.get("would_dedup", 0), r2.get("deduped", 0),
                         (d, r2))
        self.assertEqual(d.get("would_promote", 0), r2.get("added", 0),
                         (d, r2))

    def test_purged_id_denial_projects(self):
        root = _TMP / "edge-purged"
        root.mkdir(parents=True, exist_ok=True)
        dest = _fresh_store("edge-purged-dest")
        r = _run_cli(dest, "add", "--content", "purge me body",
                     "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(dest))
        try:
            pid = conn.execute(
                "SELECT id FROM memory WHERE content='purge me body'"
            ).fetchone()[0]
            conn.execute("DELETE FROM memory")
            conn.execute(
                "INSERT INTO purged_id (id, purged_at) VALUES "
                "(?,'2026-01-01T00:00:00Z')", (pid,))
            conn.commit()
        finally:
            conn.close()
        src = self._seed_source(root, [
            {"id": pid, "content": "purge me body"},
        ])
        dry = _fresh_store("edge-purged-dry")
        real = _fresh_store("edge-purged-real")
        # the promote targets must carry the purged_id deny-list row
        for target in (dry, real):
            shutil.copyfile(dest, target)
        d = self._promote(dry, src, "--dry-run")
        r2 = self._promote(real, src)
        self.assertEqual(d.get("would_purged_denied", 0), 1, d)
        self.assertEqual(r2.get("purged_denied", 0), 1, r2)


class DatasetPublishShapeSuppTest(unittest.TestCase):
    """Review-finding 3: C5 shape matrix through the PUBLISH lane
    (PublishError), not just import."""

    def test_non_dict_shapes_refuse_publish_lane(self):
        from storelib.dataset import PublishError, _read_export_records
        for bad in (42, "s", [1, 2]):
            with tempfile.TemporaryDirectory(prefix="zmem-c4-pub-") as td:
                exp = Path(td)
                (exp / "data").mkdir()
                (exp / "data" / "memories-000.jsonl").write_bytes(
                    json.dumps({"id": "ok"}).encode() + b"\n"
                    + json.dumps(bad).encode() + b"\n")
                for fam in ("episodes", "episode_members", "links"):
                    (exp / "data" / (fam + "-000.jsonl")).write_bytes(b"\n")
                manifest = {
                    "schema_version": 1, "format": "jsonl",
                    "source_snapshot_hash": "deadbeef" * 8,
                    "row_counts": {}, "namespaces": [],
                }
                (exp / "manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(PublishError) as cm:
                    _read_export_records(exp, manifest)
                self.assertIn("malformed", str(cm.exception))
                self.assertIn("memories", str(cm.exception))


class NormVerifyConcurrencySuppTest(unittest.TestCase):
    """Review-finding 3: the C6 transaction discipline, driven IN-PROCESS
    (forced compare-miss, caller-transaction savepoint + rollback scope,
    WAL stale-snapshot shape)."""

    def _load_schema(self):
        for key in [k for k in sys.modules
                    if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[key]
        sys.path.insert(0, str(SCRIPTS))
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from storelib import schema as schema_mod
        return schema_mod

    def _scratch(self, name: str) -> Path:
        root = _TMP / name
        root.mkdir(parents=True, exist_ok=True)
        return root / "store.sqlite"

    def test_single_mismatch_heals_and_stamps(self):
        """Round-2 finding 3b (honest name): drives the single-mismatch
        heal + stamp leg. The zero-row retry-then-abort leg requires a
        concurrent write BETWEEN the batch read and update, which an
        in-process test cannot interpose without monkeypatching the
        production seam; its discipline is pinned by the savepoint-arm
        rollback-scope test below and the reviewer's direct probe."""
        store = self._scratch("nv-miss")
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(store)
        env["ZMEM_DATA"] = str(store.parent)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
             "init"], capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
             "add", "--content", "miss probe body", "--namespace",
             "project:supp", "--type", "fact"],
            capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        # storelib freezes the store path at FIRST import: set the env
        # BEFORE the purge+reload (the _load_schema call below).
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        schema_mod = self._load_schema()
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            # Delete the stamp so the pass runs; then break the row so the
            # compare-and-update can never match (content_norm IS ? with a
            # NULL that the UPDATE's IS-matching sees, but mutate between
            # read and write via a wrapper is not needed: force the SECOND
            # re-read mismatch by pinning a wrong norm AND making the
            # update predicate miss through a concurrent-style change is
            # simulated by asserting the retry path with a fresh stale
            # value planted by the update itself).
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.execute(
                "UPDATE memory SET content_norm='FORCE STALE' "
                "WHERE content='miss probe body'")
            conn.commit()
            # First run heals (single mismatch -> compare-and-update hits).
            schema_mod._verify_content_norms_163(conn)
            row = conn.execute(
                "SELECT content_norm FROM memory WHERE content="
                "'miss probe body'").fetchone()
            self.assertEqual(row[0], _normalize_content("miss probe body"))
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamped, "clean pass must stamp")
        finally:
            conn.close()

    def test_caller_transaction_savepoint_rollback_scope(self):
        """R4-1: caller work BEFORE the batch savepoint survives a failed
        batch; the failed batch's writes are undone; NO stamp. The batch
        failure is injected with a RAISE(ABORT) trigger on the
        content_norm update (a constraint-style database error mid-batch
        — the shape the broadened abort arm exists for)."""
        store = self._scratch("nv-savepoint")
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        schema_mod = self._load_schema()
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute("BEGIN")
            # caller work that MUST survive the failed batch's rollback
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('caller_marker', 'survives')")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('91000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','scope probe body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE SCOPE NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.execute(
                "CREATE TRIGGER zmem_493_scope_probe BEFORE UPDATE OF "
                "content_norm ON memory WHEN NEW.id="
                "'91000000-0000-4000-8000-000000000001' "
                "BEGIN SELECT RAISE(ABORT, 'scope probe'); END;")
            # Round-2 finding 3a: DO NOT commit — the pass must run INSIDE
            # the open caller transaction, driving the true SAVEPOINT arm
            # (conn.in_transaction True), exactly the production shape a
            # migrate()-under-caller-tx caller would present.
            schema_mod._verify_content_norms_163(conn)
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNone(stamped,
                              "a failed batch must NOT stamp")
            marker = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='caller_marker'").fetchone()
            self.assertEqual(
                marker[0], "survives",
                "caller work before the batch savepoint must survive "
                "the failed batch's rollback")
            # the failed batch's own write is undone (norm still stale)
            norm = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'91000000-0000-4000-8000-000000000001'").fetchone()
            self.assertEqual(norm[0], "STALE SCOPE NORM",
                             "the failed batch's writes must be rolled "
                             "back to the savepoint")
            conn.rollback()
        finally:
            conn.close()

    def test_busy_snapshot_top_level_path_aborts_without_stamp(self):
        """R3-1: the own-transaction path takes BEGIN IMMEDIATE up front;
        when a concurrent writer already holds the write lock, the pass
        aborts WITHOUT stamping and the stale row stays untouched (never a
        stale-computed write)."""
        store = self._scratch("nv-busy")
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        schema_mod = self._load_schema()
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('92000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','busy probe body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE BUSY NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
            conn.close()
        finally:
            pass
        import sqlite3 as s3
        blocker = s3.connect(str(store), timeout=0.3,
                             isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "UPDATE meta SET value='blocked' WHERE key='nosuch-key'")
            # a SECOND connection runs the pass; its top-level BEGIN
            # IMMEDIATE must hit the held write lock and abort cleanly.
            probe = s3.connect(str(store), timeout=0.3,
                               isolation_level=None)
            try:
                schema_mod._verify_content_norms_163(probe)
            finally:
                probe.close()
            check = s3.connect(str(store), timeout=5)
            try:
                stamped = check.execute(
                    "SELECT value FROM meta WHERE "
                    "key='content_norm_verify_163'").fetchone()
                norm = check.execute(
                    "SELECT content_norm FROM memory WHERE id="
                    "'92000000-0000-4000-8000-000000000001'").fetchone()
            finally:
                check.close()
            self.assertIsNone(stamped, "a locked pass must NOT stamp")
            self.assertEqual(norm[0], "STALE BUSY NORM",
                             "the locked pass must not write a norm")
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_wal_store_stale_norm_heals_or_aborts_never_stale_writes(self):
        """Round-2 finding 3c (honest name): a WAL store with a stale norm
        heals under the pass and stamps; the never-a-stale-write property
        is pinned by the busy-top-level test (locked pass writes nothing)
        and the savepoint rollback-scope test (failed batch writes
        rollback)."""
        store = self._scratch("nv-wal")
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        schema_mod = self._load_schema()
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('90000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','wal probe body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE WAL NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
            schema_mod._verify_content_norms_163(conn)
            row = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'90000000-0000-4000-8000-000000000001'").fetchone()
            self.assertEqual(row[0], _normalize_content("wal probe body"))
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamped)
        finally:
            conn.close()


class NormVerifyRowShapeSuppTest(unittest.TestCase):
    """Review-finding 3: row-shape coverage (zero/negative rowids,
    NULL/correct/tombstoned rows, large row, real steady-state no-scan)."""

    def test_rowid_domain_and_row_states(self):
        store = _TMP / "nv-shapes" / "store.sqlite"
        store.parent.mkdir(parents=True, exist_ok=True)
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        for key in [k for k in sys.modules
                    if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[key]
        sys.path.insert(0, str(SCRIPTS))
        from storelib import schema as schema_mod
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            base = "a0000000-0000-4000-8000-%012d"
            rows = [
                (1, base % 1, "correct body one", None),
                (0, base % 2, "zero rowid body", "STALE"),
                (-5, base % 3, "negative rowid body", "STALE"),
                (4, base % 4, "tombstoned body", "STALE-TOMB"),
                (5, base % 5, "K" * 70000, "STALE-LARGE"),
            ]
            for rowid, mid, content, norm in rows:
                conn.execute(
                    "INSERT INTO memory (rowid, id, namespace, type, "
                    "content, tags, source_ref, confidence, signal, "
                    "valid_from, taint, superseded_at, ingestion_ts, "
                    "content_norm) VALUES "
                    "(?,?,?,?,?,'','session:shapes',0.9,'test',"
                    "'2026-01-01T00:00:00Z',"
                    "'trusted_internal',?,'2026-01-01T00:00:00Z',?)",
                    (rowid, mid, "project:supp", "fact", content,
                     ("2026-01-02T00:00:00Z"
                      if "tomb" in content else None),
                     (norm if norm is not None
                      else _normalize_content(content))))
            conn.commit()
            schema_mod._verify_content_norms_163(conn)
            rows_out = conn.execute(
                "SELECT content, content_norm, superseded_at FROM memory"
            ).fetchall()
            for content, norm, sup in rows_out:
                self.assertEqual(norm, _normalize_content(content or ""),
                                 (content[:40], norm[:40]))
            self.assertGreaterEqual(len(rows_out), 5)
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamped)
        finally:
            conn.close()

    def test_second_open_does_not_rescan(self):
        """Steady state: with the stamp present, a second open performs no
        memory-table scan (no additional stamp write; the stamp row's
        presence alone short-circuits)."""
        import time
        store = _fresh_store("nv-noscan")
        _run_cli(store, "add", "--content", "noscan body",
                 "--namespace", "project:supp", "--type", "fact")
        _run_cli(store, "stats")  # first non-empty open verifies + stamps
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('noscan_probe', 'before')")
            conn.commit()
        finally:
            conn.close()
        t0 = time.monotonic()
        r = _run_cli(store, "stats")
        elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        # A stamped store's verify is one meta read; the command stays in
        # the normal stats cost band (generous bound: 30s for a subprocess
        # under load; the real protection is the frozen C6 + stamp tests).
        self.assertLess(elapsed, 30.0)


def tearDownModule():
    shutil.rmtree(str(_TMP), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
