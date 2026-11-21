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
_SYS_PATH_SAVED = list(sys.path)  # closeout critic: restore at teardown
sys.path.insert(0, str(SCRIPTS))

_TMP = Path(tempfile.mkdtemp(prefix="zmem-n493-supp-"))
# closeout critic (ASK adjudication): snapshot ONLY the keys this module
# pins — a full os.environ copy taken at COLLECTION time would later wipe
# import-time env set by modules collected after this one (executed:
# ZMEM_MCP_TOKEN_FILE from test_tasktext_hub_argv.py vanished -> auth
# SystemExit in the canary co-run)
_ENV_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
             "ZMEM_MODELS_DIR")
_ENV_ORIGINAL = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ["ZMEM_STORE"] = str(_TMP / "store" / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_TMP / "data")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ["ZMEM_MODELS_DIR"] = str(_TMP / "models")  # nonexistent -> degraded
_SY_MODULES_SAVED = {k: m for k, m in sys.modules.items()
                     if k == "storelib" or k.startswith("storelib.")
                     or k == "_store_guard"}

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard; the
# env pins above precede it per the house rule)

from storelib import knowledge, write  # noqa: E402
from storelib.schema import _normalize_content  # noqa: E402


def _fresh_store(name: str) -> Path:
    root = _TMP / name
    store = root / "store" / "store.sqlite"
    store.parent.mkdir(parents=True, exist_ok=True)
    env = _store_env(root / "data")
    env["ZMEM_STORE"] = str(store)
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"), "init"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300, cwd=str(REPO))
    assert r.returncode == 0, r.stderr
    return store


_AMBIENT_MARKERS = (
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_NAMESPACE", "ZMEM_PROJECT",
    "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR")


def _store_env(store: Path) -> dict:
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(store)
    env["ZMEM_DATA"] = str(store.parent)
    # round-1 F-8 (subprocess-platform-04): ambient session/namespace/
    # project-dir markers leak into child process envs and skew the
    # launcher's namespace resolution; strip them for every driver.
    for key in _AMBIENT_MARKERS:
        env.pop(key, None)
    return env


def _run_cli(store: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"), *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=_store_env(store),
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

    def _seed_uppercase_row(self, store: Path) -> str:
        """Seed a live KNOWLEDGE:-prefixed row via raw SQL (the write path
        refuses it — that is the fix), returning its id."""
        r = _run_cli(store, "add", "--content", "uppercase carrier body",
                     "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content="
                "'uppercase carrier body'").fetchone()
            self.assertIsNotNone(row)
            conn.execute(
                "UPDATE memory SET source_ref='KNOWLEDGE:target-1:key-1' "
                "WHERE id=?", (row[0],))
            conn.commit()
        finally:
            conn.close()
        return row[0]

    def test_consumer_matrix_refuses_uppercase_refs(self):
        """R2-4 + final-critic finding 2: the uppercase reserved ref is
        refused by add / update / supersede (the real write consumers), not
        just at the predicate. A 3-site case-sensitive revert of
        update/supersede/rescan made every earlier test pass — this leg is
        what kills it."""
        store = _fresh_store("c4-consumers")
        r = _run_cli(store, "add", "--content", "consumer matrix body",
                     "--namespace", "project:supp", "--type", "fact",
                     "--source-ref", "KNOWLEDGE:t:k")
        self.assertNotEqual(r.returncode, 0,
                            "uppercase knowledge ref must be refused by add")
        mid = self._seed_uppercase_row(store)
        r = _run_cli(store, "update", "--id", mid, "--content",
                     "uppercase carrier body v2")
        self.assertNotEqual(r.returncode, 0,
                            "update must refuse a KNOWLEDGE:-prefixed row")
        r = _run_cli(store, "supersede", "--id", mid, "--reason",
                     "explicit correction")
        self.assertNotEqual(r.returncode, 0,
                            "supersede must refuse a KNOWLEDGE:-prefixed "
                            "row")

    def test_rescan_quarantines_uppercase_row(self):
        """The write.py:3374 rescan classifier routes reserved rows
        through the POINTER quarantine lane: a secret-suspect
        KNOWLEDGE:-ref row is tombstoned with the quarantine
        supersede_reason and NO live successor carries the reserved ref.
        Final-critic round-2 finding 2: a case-sensitive revert routes the
        row to the ordinary update_memory remediation lane, which
        supersedes with 'updated' and leaves a LIVE redacted successor
        still carrying the uppercase ref — the discriminators asserted
        here are the quarantine reason and the absence of a live
        reserved-ref row."""
        store = _fresh_store("c4-rescan")
        # Plant the secret URedacted via raw SQL — the write path redacts
        # at admission (probed), leaving rescan nothing to change; the
        # quarantine lane needs a policy-CHANGED payload under the
        # reserved (uppercase) ref.
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('95000000-0000-4000-8000-000000000001','project:supp',"
                "'fact',?,'','KNOWLEDGE:target-1:key-1',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',?)",
                ("token ghp_" + "a" * 36,
                 _normalize_content("token ghp_" + "a" * 36)))
            conn.commit()
        finally:
            conn.close()
        mid = "95000000-0000-4000-8000-000000000001"
        r = _run_cli(store, "rescan-secrets", "--apply")
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        conn = sqlite3.connect(str(store))
        try:
            state = conn.execute(
                "SELECT superseded_at, supersede_reason FROM memory "
                "WHERE id=?", (mid,)).fetchone()
            live_reserved = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
                "AND (source_ref LIKE 'knowledge:%' ESCAPE '\\' "
                "OR source_ref LIKE 'KNOWLEDGE:%')").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(state)
        self.assertIsNotNone(
            state[0],
            "a secret-suspect reserved-ref row must be tombstoned by the "
            "pointer quarantine lane, not left live")
        self.assertTrue(
            (state[1] or "").startswith(
                "knowledge pointer quarantined (secret rescan)"),
            f"the tombstone must carry the quarantine reason, got "
            f"{state[1]!r}")
        self.assertEqual(
            live_reserved[0], 0,
            "no live successor may carry the reserved ref (the ordinary "
            "remediation lane would leave one)")

    def test_ingest_uppercase_ref_lands_demoted_not_authoritative(self):
        store = _fresh_store("c4-ingest")
        with tempfile.TemporaryDirectory(prefix="zmem-c4-jsonl-") as td:
            jl = Path(td) / "in.jsonl"
            # a PARSEABLE pointer payload with verified=true — the shape
            # demote_imported_payload actually re-emits (verified=false)
            payload = knowledge.build_payload(
                target="target-1", key="key-1", version=1,
                full_sha256="0" * 64, summary="supp probe")
            body = {"id": "70000000-0000-4000-8000-000000000001",
                    "namespace": "project:supp", "type": "fact",
                    "content": payload,
                    "tags": "", "source_ref": "KNOWLEDGE:target-1:key-1",
                    "confidence": 0.9, "signal": "test",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "ingestion_ts": "2026-01-01T00:00:00Z"}
            jl.write_text(json.dumps(body) + "\n", encoding="utf-8")
            r3 = _run_cli(store, "ingest-jsonl", "--in", str(jl))
            self.assertEqual(r3.returncode, 0, r3.stderr)
        conn = sqlite3.connect(str(store))
        try:
            row = conn.execute(
                "SELECT source_ref, content FROM memory WHERE id="
                "'70000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "KNOWLEDGE:target-1:key-1",
                         "sync preserves the reserved ref verbatim")
        self.assertIn(
            '"verified":false', row[1].replace(" ", ""),
            "the payload must land DEMOTED (verified false), never "
            "authoritative")

    def test_import_store_uppercase_ref_preserved_unverified(self):
        """Import-store lane (R2-4 + reviewer round-4 finding + final-critic
        round-2 finding 1): the CLI is --source/--dest-dir; the row MUST
        land (no absence tolerance), the uppercase ref MUST be preserved
        verbatim, and the payload MUST land demoted (verified false). The
        source store is converted to DELETE journal mode FIRST because
        import-store carries a PRE-EXISTING WAL bug (filed as its own
        follow-up: `INSERT INTO memory_fts('optimize')` leaves an implicit
        transaction open, so `PRAGMA journal_mode=DELETE` fails with
        'cannot change out of wal mode from within a transaction' on any
        WAL source whose rows do not trigger a demotion rewrite —
        identical at base). With a DELETE-mode source the DEMOTION
        ASSERTION is what catches a case-sensitive revert of the prefix
        site, not that unrelated crash."""
        src_dir = _TMP / "c4-import"
        src_dir.mkdir(parents=True, exist_ok=True)
        src = src_dir / "src.sqlite"
        if src.exists():
            src.unlink()
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(src)
        env["ZMEM_DATA"] = str(src_dir / "data")
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
             "init"], capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = knowledge.build_payload(
            target="target-1", key="key-1", version=1,
            full_sha256="0" * 64, summary="import probe")
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('71000000-0000-4000-8000-000000000001','project:supp',"
                "'fact',?,'','KNOWLEDGE:target-1:key-1',0.9,"
                "'test','2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',?)",
                (payload, _normalize_content(payload)))
            conn.commit()
        finally:
            conn.close()
        dest_dir = _TMP / "c4-import-dest-dir"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "store.sqlite"
        if dest.exists():
            dest.unlink()
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             str(SCRIPTS / "import-store.py"),
             "--source", str(src), "--dest-dir", str(dest_dir)],
            capture_output=True, text=True, timeout=300, cwd=str(REPO))
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertTrue(dest.is_file(), "the destination store must exist")
        conn = sqlite3.connect(str(dest))
        try:
            row = conn.execute(
                "SELECT source_ref, content FROM memory WHERE id="
                "'71000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(
            row, "the uppercase-ref row MUST be imported (no absence "
                 "tolerance)")
        self.assertEqual(row[0], "KNOWLEDGE:target-1:key-1",
                         "import-store preserves the reserved ref verbatim")
        self.assertIn(
            '"verified":false', row[1].replace(" ", ""),
            "the imported payload must land DEMOTED (verified false), "
            "never authoritative")


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

    def test_clean_store_is_zero_write_and_unstamped(self):
        """CI round (PR #525): the pass is a ZERO-WRITE surface on clean
        stores — byte-identical contracts (explain zero-write, op-queue
        replay idempotence) snapshot stores whose first production open is
        the assertion's own read. A clean scan writes NOTHING (no stamp);
        the stamp rides an actual heal (frozen C6 + the heal tests)."""
        store = _fresh_store("nv-stamp")
        r = _run_cli(store, "add", "--content", "norm verify stamp body",
                     "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r.returncode, 0, r.stderr)
        before = store.read_bytes()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            stamped = conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_verify_163'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(stamped,
                          "a clean (all-canonical) store must NOT be "
                          "stamped by a read-only open")
        self.assertEqual(store.read_bytes(), before,
                         "a clean open must not change store bytes")
        # Steady state: repeated opens stay green and byte-identical.
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(store.read_bytes(), before)

    def test_heal_writes_stamp_and_certifies(self):
        store = _fresh_store("nv-heal-stamp")
        r = _run_cli(store, "add", "--content", "norm heal stamp body",
                     "--namespace", "project:supp", "--type", "fact")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "UPDATE memory SET content_norm='STALE HEAL NORM'")
            conn.commit()
        finally:
            conn.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            stamped = conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_verify_163'"
            ).fetchone()
            norms = [row[0] for row in conn.execute(
                "SELECT content_norm FROM memory")]
        finally:
            conn.close()
        self.assertIsNotNone(stamped,
                             "a completed heal pass must carry the stamp")
        for n in norms:
            self.assertEqual(n, _normalize_content("norm heal stamp body"))

    def test_partial_pass_leaves_no_stamp_and_retries(self):
        """Round-6 reviewer finding: a heal batch that COMMITS must NOT
        certify the pass. 205 rows so the scan spans TWO batches
        (batch_size=200): batch 1 heals rows 1-200 and COMMITS; batch 2
        aborts on a RAISE(ABORT) trigger on row 205. Expect: no stamp
        (an aborted pass is never certified), batch 1's heals durable and
        canonical, row 205 still stale; drop the trigger, re-run — the
        stale row heals and the completed pass stamps."""
        store = _TMP / "nv-partial" / "store.sqlite"
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
            rows = [
                ("96000000-0000-4000-8000-" + f"{i:012d}",
                 f"partial pass body {i}")
                for i in range(1, 206)]
            # STALE NON-NULL norms (this pass's lane; NULLs are 162's)
            conn.executemany(
                "INSERT INTO memory (id, namespace, type, content, "
                "tags, source_ref, confidence, signal, valid_from, "
                "taint, ingestion_ts, content_norm) VALUES "
                "(?,?,?,?,?,'session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',?)",
                [(mid, "project:supp", "fact", content, "",
                  "STALE " + content)
                 for mid, content in rows])
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            # row 205 sits in batch 2; its UPDATE aborts via the trigger
            conn.execute(
                "CREATE TRIGGER zmem_493_partial_probe BEFORE UPDATE OF "
                "content_norm ON memory WHEN NEW.id="
                "'96000000-0000-4000-8000-000000000205' "
                "BEGIN SELECT RAISE(ABORT, 'partial probe'); END;")
            conn.commit()
            schema_mod._verify_content_norms_163(conn)
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNone(stamped,
                              "an aborted pass must NOT be certified")
            n1 = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'96000000-0000-4000-8000-000000000001'").fetchone()[0]
            n205 = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'96000000-0000-4000-8000-000000000205'").fetchone()[0]
            stale_cnt = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE content_norm LIKE "
                "'STALE %'").fetchone()[0]
            self.assertFalse(
                conn.in_transaction,
                "the aborted pass leaves no verifier-owned transaction "
                "open on the connection (round-2 F-2)")
            # Durability must be observed from a SECOND connection: the
            # later retry commits this connection's pending work, so
            # same-connection reads cannot see a missing per-batch
            # commit (round-2 F-2: the mutant survived the old shape).
            check = sqlite3.connect(str(store), timeout=5)
            try:
                n1 = check.execute(
                    "SELECT content_norm FROM memory WHERE id="
                    "'96000000-0000-4000-8000-000000000001'").fetchone()[0]
                stale_cnt = check.execute(
                    "SELECT COUNT(*) FROM memory WHERE content_norm "
                    "LIKE 'STALE %'").fetchone()[0]
            finally:
                check.close()
            self.assertEqual(
                n1, _normalize_content("partial pass body 1"),
                "batch 1's committed heal stays durable")
            self.assertEqual(n205, "STALE partial pass body 205",
                             "the unverified batch-2 row stays stale "
                             "for retry")
            self.assertEqual(stale_cnt, 5,
                             "exactly batch 2's rows (201-205) remain "
                             "unverified; batch 1's 200 healed")
            # retry: drop the trigger, re-run — everything heals + stamps
            conn.execute("DROP TRIGGER zmem_493_partial_probe")
            conn.commit()
            schema_mod._verify_content_norms_163(conn)
            stamped2 = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamped2,
                                 "the completed retry carries the stamp")
            bad = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE content_norm LIKE "
                "'STALE %'").fetchone()[0]
            self.assertEqual(bad, 0, "the retry healed every row")
        finally:
            conn.close()

    def test_large_store_heals_across_batches(self):
        store = _fresh_store("nv-batches")
        # round-1 F-8 (tf-04/05): 505 serial CLI adds cost ~11 minutes;
        # seed the rows directly — the coverage target is the pass's
        # batch-boundary healing, not the CLI add path (exercised
        # elsewhere in this file), so one CLI init + SQL seeds keep the
        # leg inside the CI loop's runtime band.
        conn = sqlite3.connect(str(store))
        try:
            conn.executemany(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "(?,?,?,?,?,'session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',?)",
                [("97000000-0000-4000-8000-" + f"{i:012d}",
                  "project:supp", "fact", f"batch body {i}", "",
                  _normalize_content(f"batch body {i}"))
                 for i in range(505)])
            conn.commit()
        finally:
            conn.close()
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

    def test_non_text_content_rows_skip_and_open_succeeds(self):
        """round-1 F-2: rows whose content is not verifiable text — an
        invalid-UTF-8 BLOB, a valid-UTF-8 BLOB, an invalid-UTF-8 TEXT
        cell — must not crash the open. The 162 repair and the 163 pass
        both SKIP the unverifiable shapes (norm stays NULL / stale stays)
        and heal/verify the decodable ones; the open succeeds and the
        stamps follow their normal cadence."""
        store = _fresh_store("nv-nontext")
        _run_cli(store, "add", "--content", "anchor body",
                 "--namespace", "project:supp", "--type", "fact")
        from storelib import schema as schema_mod  # env pinned at import
        conn = sqlite3.connect(str(store))
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_repair_162'")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        raw = sqlite3.connect(str(store))
        try:
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('96000000-0000-4000-8000-000000000901','project:supp',"
                "'fact',CAST(X'41424300FF44' AS BLOB),'','session:x',"
                "0.9,'test','2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',NULL)")  # invalid-UTF-8 BLOB
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('96000000-0000-4000-8000-000000000902','project:supp',"
                "'fact',CAST(X'7370616365' AS BLOB),'','session:x',"
                "0.9,'test','2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',NULL)")  # valid-UTF-8 BLOB
            # an invalid-UTF-8 TEXT cell: CAST keeps the TEXT storage
            # class carrying non-UTF-8 bytes (a plain bytes bind would be
            # stored as a BLOB — stage-B reviewer correction)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "tags, source_ref, confidence, signal, valid_from, "
                "taint, ingestion_ts, content_norm) VALUES "
                "('96000000-0000-4000-8000-000000000903','project:supp',"
                "'fact',CAST(X'FFFE' AS TEXT),'','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',NULL)")
            # an undecodable-content row WITH a stale non-NULL norm: the
            # 163 pass must SKIP it (norm unchanged) — this row is the
            # discriminator for the skip arm and for the reviewer's
            # double-conversion bug (which healed it to an empty norm)
            raw.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "tags, source_ref, confidence, signal, valid_from, "
                "taint, ingestion_ts, content_norm) VALUES "
                "('96000000-0000-4000-8000-000000000904','project:supp',"
                "'fact',CAST(X'FFFE' AS TEXT),'','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','WRONG UNDECODABLE NORM')")
            raw.commit()
        finally:
            raw.close()
        # reopen through the full migrate(): was the round-1 F-2 crash
        conn2 = sqlite3.connect(str(store))
        try:
            schema_mod.migrate(conn2)  # must not raise
            # typeof() instead of content: the undecodable TEXT cell
            # would crash a plain SELECT's decode
            rows = conn2.execute(
                "SELECT id, typeof(content), content_norm FROM memory "
                "WHERE id LIKE '96000000-0000-4000-8000-0000000009%' "
                "ORDER BY id").fetchall()
            by_id = {r[0][-3:]: (r[1], r[2]) for r in rows}
            self.assertEqual(by_id["901"][0], "blob", "fixture shape")
            self.assertEqual(by_id["903"][0], "text", "fixture shape")
            self.assertIsNone(
                by_id["901"][1],
                "fixture: invalid-UTF-8 BLOB row keeps NULL norm")
            self.assertIsNone(
                by_id["903"][1],
                "fixture: invalid-UTF-8 TEXT row keeps NULL norm")
            self.assertEqual(
                by_id["904"][1], "WRONG UNDECODABLE NORM",
                "fixture setup: the undecodable row carries a stale "
                "non-NULL norm")
            self.assertEqual(
                by_id["902"][1],
                _normalize_content("space"),
                "fixture: the valid-UTF-8 BLOB row was repaired")
            stamped = conn2.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            # the 162 repair healed 902 (and the anchor row healed at the
            # first migrate) so 163's scan finds nothing stale and stays
            # unstamped (zero-write)
            self.assertIsNone(stamped,
                              "zero-write: nothing stale for 163 to heal")
        finally:
            conn2.close()
        # mixed-batch discriminator (stage-B reviewer round 2): after the
        # 162 repair has stamped, a NEW NULL-norm row that shares a
        # decode-fallback batch with the undecodable rows must STAY NULL
        # (163's lane split) — the pre-fix fallback converted the NULL to
        # "" and healed it
        raw2 = sqlite3.connect(str(store))
        try:
            raw2.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('96000000-0000-4000-8000-000000000905','project:supp',"
                "'fact','null lane mixed body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z',NULL)")
            raw2.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            raw2.commit()
        finally:
            raw2.close()
        conn5 = sqlite3.connect(str(store))
        try:
            schema_mod.migrate(conn5)  # fallback batch: undecodable + NULL
            n905 = conn5.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'96000000-0000-4000-8000-000000000905'").fetchone()[0]
        finally:
            conn5.close()
        self.assertIsNone(
            n905,
            "a NULL norm in a decode-fallback batch stays NULL (162's "
            "lane; not healed by the 163 pass)")
        # and a stale NORM on a decodable row still heals next open even
        # with the unverifiable rows present
        conn3 = sqlite3.connect(str(store))
        try:
            conn3.execute(
                "UPDATE memory SET content_norm='WRONG' "
                "WHERE content='anchor body'")
            conn3.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn3.commit()
        finally:
            conn3.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn4 = sqlite3.connect(str(store))
        try:
            norm = conn4.execute(
                "SELECT content_norm FROM memory WHERE "
                "content='anchor body'").fetchone()
            bad = conn4.execute(
                "SELECT content_norm FROM memory WHERE id IN "
                "('96000000-0000-4000-8000-000000000901',"
                "'96000000-0000-4000-8000-000000000903')").fetchall()
            n904 = conn4.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'96000000-0000-4000-8000-000000000904'").fetchone()[0]
        finally:
            conn4.close()
        self.assertEqual(norm[0], _normalize_content("anchor body"),
                         "verifiable rows still heal alongside skips")
        for (bnorm,) in bad:
            self.assertIsNone(bnorm, "unverifiable rows stay untouched")
        self.assertEqual(
            n904, "WRONG UNDECODABLE NORM",
            "the undecodable stale row is SKIPPED (not healed, not "
            "emptied) by the verification pass")

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
        concurrent write BETWEEN the batch read and update — driven
        directly by test_raise_ignore_trigger_aborts_without_stamp below
        (final-critic finding 1: a RAISE(IGNORE) trigger makes every
        compare-and-update a zero-row miss)."""
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

    def test_raise_ignore_trigger_aborts_without_stamp(self):
        """Final-critic finding 1, leg (b): a RAISE(IGNORE) trigger makes
        every compare-and-update a zero-row miss — the retry re-read still
        mismatches, the pass aborts WITHOUT stamping, and on the
        OWN-transaction arm the BEGIN IMMEDIATE write lock is RELEASED
        (the connection must not stay in a transaction afterwards)."""
        store = self._scratch("nv-raiseignore")
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
                "('93000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','raise ignore body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE IGNORE NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.execute(
                "CREATE TRIGGER zmem_493_ignore_probe BEFORE UPDATE OF "
                "content_norm ON memory BEGIN SELECT RAISE(IGNORE); "
                "END;")
            conn.commit()
            schema_mod._verify_content_norms_163(conn)
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNone(stamped,
                              "an all-zero-row pass must NOT stamp")
            self.assertFalse(
                conn.in_transaction,
                "the aborted own-transaction pass must release its BEGIN "
                "IMMEDIATE write lock")
        finally:
            conn.close()

    def test_caller_tx_success_keeps_caller_transaction_open(self):
        """Final-critic finding 1, leg (c): when the pass runs inside a
        caller transaction and SUCCEEDS, the stamp must ride the caller's
        commit — the caller's transaction stays open afterwards and a
        caller ROLLBACK undoes heals + stamp together (the pass never
        closes a transaction it did not open)."""
        store = self._scratch("nv-callertx-ok")
        os.environ.update({"ZMEM_STORE": str(store),
                           "ZMEM_DATA": str(store.parent)})
        schema_mod = self._load_schema()
        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute("BEGIN")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('caller_marker', 'rides')")
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, confidence, signal, valid_from, taint, "
                "ingestion_ts, content_norm) VALUES "
                "('94000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','caller tx ok body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE CALLERTX NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            schema_mod._verify_content_norms_163(conn)
            self.assertTrue(
                conn.in_transaction,
                "a successful pass inside a caller transaction must leave "
                "the caller's transaction OPEN")
            stamp = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamp, "the stamp is written under the "
                                        "caller's transaction")
            norm = conn.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'94000000-0000-4000-8000-000000000001'").fetchone()
            self.assertEqual(norm[0],
                             _normalize_content("caller tx ok body"))
            # the caller's ROLLBACK still owns everything
            conn.rollback()
            gone = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNone(gone,
                               "caller rollback must undo the stamp too")
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
        finally:
            conn.close()
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

    def test_stamp_time_failure_never_aborts_the_open(self):
        """Round-7: the post-scan stamp write sits AFTER the last heal
        batch released the write lock, so a concurrent writer (or any
        sqlite failure) at THAT seam must not abort the caller's open —
        the pass returns unstamped, heals stay durable, and the
        connection is released clean. A meta-table trigger raises ABORT
        at exactly the stamp INSERT (the memory-table heals are
        unaffected), deterministically driving the seam."""
        store = self._scratch("nv-stampfail")
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
                "('93000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','stamp probe body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE STAMP NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        import sqlite3 as s3
        setup = s3.connect(str(store), timeout=5)
        try:
            # SQLite triggers take ONE event each; the stamp write is
            # INSERT OR REPLACE, so both shapes are probed.
            for ev in ("INSERT", "UPDATE"):
                setup.execute(
                    f"CREATE TRIGGER zmem_493_stamp_probe_{ev} BEFORE "
                    f"{ev} ON meta WHEN NEW.key='content_norm_verify_163' "
                    "BEGIN SELECT RAISE(ABORT, 'stamp lock probe'); END")
            setup.commit()
        finally:
            setup.close()
        probe = s3.connect(str(store), timeout=5)
        try:
            # own-transaction path: the heal batch commits, the post-scan
            # stamp hits the trigger — the call must RETURN, never raise.
            schema_mod._verify_content_norms_163(probe)
            self.assertFalse(
                probe.in_transaction,
                "a failed stamp must not strand a verifier-owned "
                "transaction on the connection")
        finally:
            probe.close()
        check = s3.connect(str(store), timeout=5)
        try:
            stamped = check.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            norm = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'93000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            check.close()
        self.assertIsNone(stamped,
                          "a stamp-time failure must leave the pass "
                          "unstamped for a later retry")
        self.assertEqual(norm[0], _normalize_content("stamp probe body"),
                         "the committed heal stays durable across the "
                         "stamp-time failure")
        # Retry after the simulated contention clears. DISCLOSED RESIDUAL
        # (round-7 fix): the healed row leaves nothing stale, so the retry
        # scan heals nothing (healed == 0) and the store stays unstamped —
        # it re-scans read-only on every open (the disclosed clean-store
        # steady state) until any future drift re-opens the stamp path.
        retry_setup = s3.connect(str(store), timeout=5)
        try:
            for ev in ("INSERT", "UPDATE"):
                retry_setup.execute(
                    f"DROP TRIGGER zmem_493_stamp_probe_{ev}")
            retry_setup.commit()
        finally:
            retry_setup.close()
        retry = s3.connect(str(store), timeout=5)
        try:
            schema_mod._verify_content_norms_163(retry)  # must not raise
        finally:
            retry.close()
        final = s3.connect(str(store), timeout=5)
        try:
            stamped2 = final.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            norm2 = final.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'93000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            final.close()
        self.assertIsNone(stamped2,
                          "the residual: a fully-healed store re-scans "
                          "clean and does not re-stamp")
        self.assertEqual(norm2[0], _normalize_content("stamp probe body"),
                         "the healed row stays canonical across retries")
        # Self-healing property: any FUTURE drift re-opens the stamp path.
        drift = s3.connect(str(store), timeout=5)
        try:
            drift.execute(
                "UPDATE memory SET content_norm='STALE AGAIN' WHERE id="
                "'93000000-0000-4000-8000-000000000001'")
            drift.commit()
        finally:
            drift.close()
        recovery = s3.connect(str(store), timeout=5)
        try:
            schema_mod._verify_content_norms_163(recovery)
        finally:
            recovery.close()
        post = s3.connect(str(store), timeout=5)
        try:
            stamped3 = post.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            norm3 = post.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'93000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            post.close()
        self.assertIsNotNone(stamped3,
                             "future drift re-heals AND re-opens the "
                             "stamp path")
        self.assertEqual(norm3[0], _normalize_content("stamp probe body"),
                         "the drifted row heals canonically")

    def test_stamp_fail_trigger_undoes_partial_insert_on_caller_tx(self):
        """Round-8: SQLite RAISE(FAIL) does NOT undo the failing
        statement's own changes, so an AFTER INSERT trigger firing FAIL
        after the stamp row landed would leave the partial stamp pending
        inside the CALLER-owned transaction. The stamp-local savepoint
        must undo exactly that insert: no stamp in the caller's
        transaction (before OR after its commit), caller tx stays open
        with the heals intact, and the caller's commit persists the heals
        but never the failed stamp."""
        store = self._scratch("nv-stampfail-caller")
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
                "('94000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','caller fail body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE CALLER NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        import sqlite3 as s3
        setup = s3.connect(str(store), timeout=5)
        try:
            # FAIL (not ABORT): preserves the statement's own changes —
            # the worst-case partial-insert shape. AFTER (not BEFORE):
            # the row has already landed when the raise fires.
            for ev in ("INSERT", "UPDATE"):
                setup.execute(
                    f"CREATE TRIGGER zmem_493_stamp_fail_{ev} AFTER "
                    f"{ev} ON meta WHEN NEW.key='content_norm_verify_163' "
                    "BEGIN SELECT RAISE(FAIL, 'stamp fail probe'); END")
            setup.commit()
        finally:
            setup.close()
        probe = s3.connect(str(store), timeout=5)
        try:
            probe.execute("BEGIN")  # caller-owned transaction
            schema_mod._verify_content_norms_163(probe)  # must not raise
            self.assertTrue(
                probe.in_transaction,
                "the caller's transaction stays open — the helper never "
                "commits or rolls back caller-owned work")
            in_tx_stamp = probe.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            in_tx_norm = probe.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'94000000-0000-4000-8000-000000000001'").fetchone()
            self.assertIsNone(
                in_tx_stamp,
                "the savepoint undoes the FAIL-preserved partial stamp "
                "inside the caller's transaction")
            self.assertEqual(
                in_tx_norm[0], _normalize_content("caller fail body"),
                "the heals ride the caller transaction intact")
            probe.commit()  # the caller commits its own work
        finally:
            probe.close()
        check = s3.connect(str(store), timeout=5)
        try:
            stamped = check.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            norm = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'94000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            check.close()
        self.assertIsNone(stamped,
                          "the caller's commit persists the heals but "
                          "NEVER the failed stamp")
        self.assertEqual(norm[0], _normalize_content("caller fail body"),
                         "the caller's commit persists the heals")

    def test_stamp_commit_failure_rolls_back_stamp_keeps_heals(self):
        """Round-8 reviewer question: a commit() failure on the
        verifier-owned stamp transaction must leave no stamp while the
        already-committed heal batches stay durable. Driven with a
        connection subclass whose commit() raises on the stamp commit
        (the last commit of the pass: one heal batch + the stamp)."""
        store = self._scratch("nv-stampcommitfail")
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
                "('95000000-0000-4000-8000-000000000001','project:supp',"
                "'fact','commit fail body','','session:x',0.9,'test',"
                "'2026-01-01T00:00:00Z','trusted_internal',"
                "'2026-01-01T00:00:00Z','STALE COMMIT NORM')")
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        import sqlite3 as s3

        class _BoomCommit(s3.Connection):
            commits = 0

            def commit(self):
                _BoomCommit.commits += 1
                if _BoomCommit.commits == 2:
                    raise s3.OperationalError(
                        "simulated stamp commit failure")
                super().commit()

        probe = s3.connect(str(store), timeout=5, factory=_BoomCommit)
        try:
            # commit #1 = the heal batch; commit #2 = the stamp -> raises
            schema_mod._verify_content_norms_163(probe)  # must not raise
            # In-connection assertions BEFORE any close (round-9: close()
            # itself rolls back an open transaction, so post-close checks
            # cannot establish the verifier's own rollback ran).
            self.assertFalse(
                probe.in_transaction,
                "the failed stamp commit leaves no verifier-owned "
                "transaction stranded on the connection")
            in_conn_stamp = probe.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNone(
                in_conn_stamp,
                "the verifier's rollback discarded the stamp the "
                "failed commit left in the open transaction")
        finally:
            probe.close()
        check = s3.connect(str(store), timeout=5)
        try:
            stamped = check.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            norm = check.execute(
                "SELECT content_norm FROM memory WHERE id="
                "'95000000-0000-4000-8000-000000000001'").fetchone()
        finally:
            check.close()
        self.assertIsNone(stamped,
                          "a failed stamp commit leaves no stamp")
        self.assertEqual(norm[0], _normalize_content("commit fail body"),
                         "the heal batch committed before the stamp "
                         "stays durable")

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
        """Steady state (round-2 F-3 behavioral rewrite): the stamped
        early return SKIPS the scan — a stale norm planted AFTER the
        stamp survives a later open untouched, while the same stale row
        heals on an unstamped store. (The old shape timed an unstamped
        re-scan: a clean store never stamps, so the wall-clock bound
        passed regardless and the early return's deletion survived.)"""
        store = _fresh_store("nv-noscan")
        _run_cli(store, "add", "--content", "noscan body",
                 "--namespace", "project:supp", "--type", "fact")
        _run_cli(store, "stats")  # clean scan: heals nothing, no stamp
        # Give the store a heal so the post-scan stamp lands.
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "UPDATE memory SET content_norm='WRONG NOSCAN' "
                "WHERE content='noscan body'")
            conn.commit()
        finally:
            conn.close()
        _run_cli(store, "stats")  # heals + stamps
        conn = sqlite3.connect(str(store))
        try:
            stamped = conn.execute(
                "SELECT value FROM meta WHERE "
                "key='content_norm_verify_163'").fetchone()
            self.assertIsNotNone(stamped, "fixture: stamp must be present")
            # Plant a stale norm AFTER the stamp; the stamped early return
            # must leave it untouched on the next open.
            conn.execute(
                "UPDATE memory SET content_norm='STALE AFTER STAMP' "
                "WHERE content='noscan body'")
            conn.commit()
        finally:
            conn.close()
        r = _run_cli(store, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(store))
        try:
            norm = conn.execute(
                "SELECT content_norm FROM memory WHERE "
                "content='noscan body'").fetchone()
        finally:
            conn.close()
        self.assertEqual(
            norm[0], "STALE AFTER STAMP",
            "the stamped early return skipped the scan (no re-heal)")
        # Discriminator: the SAME stale row heals on an unstamped store.
        conn = sqlite3.connect(str(store))
        try:
            conn.execute(
                "DELETE FROM meta WHERE key='content_norm_verify_163'")
            conn.commit()
        finally:
            conn.close()
        _run_cli(store, "stats")
        conn = sqlite3.connect(str(store))
        try:
            norm2 = conn.execute(
                "SELECT content_norm FROM memory WHERE "
                "content='noscan body'").fetchone()
        finally:
            conn.close()
        self.assertEqual(norm2[0], _normalize_content("noscan body"),
                         "unstamped, the same stale row heals")


def tearDownModule():
    shutil.rmtree(str(_TMP), ignore_errors=True)
    # closeout critic: restore ONLY the keys this module pinned (never
    # os.environ.clear() — it wiped later-collected modules' import-time
    # env and deleted pytest's PYTEST_CURRENT_TEST). CAVEAT: pytest imports
    # every module at collection BEFORE any test runs, so a later-collected
    # module's import-time pin DOES sit in os.environ while our tests run
    # — the compare-and-restore below leaves those values alone (they are
    # not ours), which is the whole reason for the guard. sys.path and the
    # purged storelib modules are restored to their import-time state too.
    for key, value in _ENV_ORIGINAL.items():
        current = os.environ.get(key)
        ours = current is not None and (
            current == value
            or (current.startswith(str(_TMP))))
        if ours:
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for key in list(sys.modules):
        if (key == "storelib" or key.startswith("storelib.")
                or key == "_store_guard") and key not in _SY_MODULES_SAVED:
            del sys.modules[key]
    for key, mod in _SY_MODULES_SAVED.items():
        sys.modules[key] = mod
    # sys.path: pre-import entries in their original order, then any
    # entries later modules added (order preserved) — never reorders or
    # drops a sibling's insertions
    sys.path[:] = _SYS_PATH_SAVED + [sp for sp in sys.path
                                     if sp not in _SYS_PATH_SAVED]


if __name__ == "__main__":
    unittest.main(verbosity=1)
