"""Issue #493 follow-up checks (N11): store-integrity acceptance legs.

Six verified defects, one unittest class each. Every leg must FAIL or ERROR
at the branch base for the defect's own reason and pass once a correct fix
lands. No production code is modified here.

  C1 BackupRestoreMigratedShapeTest — `restore` deliberately bypasses
     connect()/init_db()/migrate() (cli.py dispatches it early, for Windows
     file-handle safety); the defect is that NOTHING proves the restored
     store reaches migrated shape, so restoring a snapshot whose schema
     predates the current client leaves the destination at the old
     schema_version with the migrated-era objects absent.
  C2 DryRunLaneParityTest — promote-store --dry-run models only
     destination-vs-source interactions; two source rows sharing a
     normalized content fold into each other in the REAL promote but the
     dry-run counts both as would_promote and projects the folded row's
     expiry record as memory_expiry, not the real orphan skip.
  C3 FamilyStreamingTest — dataset._read_family's jsonl lane materializes
     the whole family file via an unbounded fh.read() instead of streaming.
  C4 PrefixCaseAgreementTest — knowledge.is_pointer_ref is a case-sensitive
     startswith("knowledge:") while the SQL exclusion windows
     (NOT LIKE 'knowledge:%') are case-insensitive; the write-path
     admission guard therefore admits a KNOWLEDGE:-prefixed source_ref.
  C5 NonDictRowRefusalTest — the dataset import required-key loop runs
     `key not in row` on a JSONL record that parsed to a scalar, raising a
     raw TypeError instead of the DatasetError refusal.
  C6 NormVerifyRepairTest — _repair_null_content_norm_162 selects
     WHERE content_norm IS NULL only and short-circuits on its meta stamp,
     so a store already stamped under #491 keeps a planted STALE non-NULL
     content_norm forever.

Run per class (the frozen argv): python -X utf8 tests/test_followup_n11.py
<TestClass> for each of the six classes above.

House style: tests/test_feedback_promote.py (subprocess-driven real CLI)
and tests/test_content_norm_repair_on_open.py (seed-then-reopen). No pytest
constructs. Every store is a scratch file under a per-test temp dir; the
operator's real store is never resolved.
"""

from __future__ import annotations

import atexit
import builtins
import io as _io
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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
PYTHON = sys.executable

# --- Module-top scratch pins BEFORE any storelib import (storelib freezes
# STORE_PATH at first import). The module-level pin only keeps in-process
# storelib imports off the operator's home; every REAL store below is a
# per-test file driven through subprocesses with its own env.
_MODULE_TMP = tempfile.mkdtemp(prefix="zmem-n11-mod-")
atexit.register(shutil.rmtree, _MODULE_TMP, True)
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_TMP, "modstore", "store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_MODULE_TMP, "moddata")
os.makedirs(os.path.join(_MODULE_TMP, "modstore"), exist_ok=True)
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

sys.path.insert(0, str(SCRIPTS))

# schema_meta standalone (constants-only) so version assertions track the
# CURRENT supported version across bumps (the test_feedback_promote.py rule).
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location(
    "zmem_n11_schema_meta", SCRIPTS / "schema_meta.py")
_schema_meta = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_schema_meta)
SUPPORTED_VERSION = _schema_meta.SUPPORTED_SCHEMA_VERSION

# In-process storelib imports happen AFTER the pins above (STORE_PATH freeze).
from storelib import dataset as dataset_mod  # noqa: E402
from storelib import knowledge as knowledge_mod  # noqa: E402
from storelib.backup import create_snapshot  # noqa: E402
from storelib.schema import _normalize_content  # noqa: E402

_AMBIENT_PINS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_INJECT", "ZMEM_MODELS_DIR",
    "ZMEM_REMOTE_URL",
)


def _store_env(store: Path, data: Path | None = None) -> dict:
    """Subprocess env pinned at a scratch store (house pattern from
    tests/test_content_norm_repair_on_open.py)."""
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_PINS}
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(data if data is not None else Path(store).parent),
        "ZMEM_MODELS_DIR": str(Path(store).parent / "models-absent"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_INJECT": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_store(store: Path, *args: str, timeout: int = 600):
    """Run the real store.py CLI against a scratch store."""
    return subprocess.run(
        [PYTHON, "-X", "utf8", str(STORE_PY), *args],
        capture_output=True, text=True, env=_store_env(store),
        cwd=str(REPO_ROOT), timeout=timeout,
    )


def _clean_close(conn: sqlite3.Connection) -> None:
    """Leave a hand-seeded scratch store sidecar-free (WAL checkpointed, back
    on the rollback journal) so snapshots/clones are single files."""
    try:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


def _insert_memory_row(conn: sqlite3.Connection, mid: str, ns: str,
                       mtype: str, content: str, *,
                       source_ref: str = "session:n11-seed") -> None:
    """Hand-seed one fully-populated live memory row (the column set the
    sync validator demands; content_norm canonical)."""
    conn.execute(
        "INSERT INTO memory (id, namespace, type, content, tags,"
        " source_ref, confidence, signal, valid_from, valid_until,"
        " update_of, taint, superseded_at, supersede_reason,"
        " ingestion_ts, content_norm, trust_score, applied_count,"
        " violated_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, ns, mtype, content, "", source_ref, 0.9, "test", "", "", "",
         "trusted_internal", None, None, "2026-11-19T00:00:00Z",
         _normalize_content(content), 1.0, 0, 0))


def _read_meta(path: Path, key: str) -> str | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _table_names(path: Path) -> set:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C1 — restore must leave the destination at the client's migrated shape
# ---------------------------------------------------------------------------

class BackupRestoreMigratedShapeTest(unittest.TestCase):
    """Restore a snapshot whose schema predates the current client (a v18
    store; SUPPORTED is 19) through the REAL restore dispatch and assert the
    destination reaches migrated shape — schema_version == SUPPORTED and the
    v19 migrated-era object (the applied_ops op-queue table) present. The
    snapshot is produced by the real backup path (create_snapshot); the
    restore runs via `store.py restore`, the exact early-dispatch locus of
    the defect (cli.py dispatches restore BEFORE connect/init_db/migrate).
    """

    def test_restored_store_reaches_migrated_shape(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c1-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        src = tmp / "src" / "store.sqlite"
        src.parent.mkdir(parents=True)

        # Create the store through the production open path (init runs
        # connect/init_db/migrate under the current client).
        r = _run_store(src, "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_read_meta(src, "schema_version"),
                         str(SUPPORTED_VERSION),
                         "precondition: fresh store at the current version")

        # Downgrade to the old shape exactly as an older client would have
        # left it: drop the v19 object, stamp schema_version=18.
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("DROP TABLE IF EXISTS applied_ops")
            conn.execute(
                "UPDATE meta SET value='18' WHERE key='schema_version'")
            conn.commit()
        finally:
            _clean_close(conn)
        self.assertEqual(_read_meta(src, "schema_version"), "18")
        self.assertNotIn("applied_ops", _table_names(src))

        # Snapshot the old-shape store via the REAL backup path.
        snap_dir = tmp / "snap"
        snap_dir.mkdir()
        snap = snap_dir / "old-shape.sqlite"
        info = create_snapshot(src, snap)
        self.assertEqual(info["integrity_check"], "ok")
        self.assertEqual(_read_meta(snap, "schema_version"), "18",
                         "precondition: the snapshot itself is old-shape")

        # Restore into a fresh scratch destination via the real CLI
        # dispatch (the restore-before-connect locus).
        dest = tmp / "dest" / "store.sqlite"
        dest.parent.mkdir(parents=True)
        self.assertFalse(dest.exists())
        r = _run_store(dest, "restore", "--from", str(snap))
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertIn("post-restore integrity_check=ok", r.stdout)
        self.assertTrue(dest.is_file(), "the restored destination exists")

        # 5. THE CHECK: the destination must be at migrated shape with no
        #    later open required — read it with a raw connection (going
        #    through connect()/migrate() here would mask the defect).
        ver_after = _read_meta(dest, "schema_version")
        tables_after = _table_names(dest)
        self.assertEqual(
            ver_after, str(SUPPORTED_VERSION),
            "restore left the destination at the SNAPSHOT's old "
            f"schema_version ({ver_after!r}): the restored store must reach "
            "the current client's migrated shape (schema_version == "
            f"{SUPPORTED_VERSION}) without waiting for a later open to "
            "migrate it")
        self.assertIn(
            "applied_ops", tables_after,
            "the v19 migrated-era object (applied_ops) is absent after "
            "restore: the destination was copied verbatim from the "
            "old-shape snapshot and nothing migrated it")


# ---------------------------------------------------------------------------
# C2 — promote-store --dry-run tallies must match the real landed outcomes
# ---------------------------------------------------------------------------

class DryRunLaneParityTest(unittest.TestCase):
    """Two source state rows share the same normalized content; the second
    carries one memory_expiry record. The destination is non-fresh (it
    already holds an unrelated live row). The dry-run projection and a REAL
    promote into an identical clone must agree: the row that really folds
    must not be projected as landing under its own id, and its expiry
    record must project as an orphan skip, exactly like the real run.
    """

    ID_A = "20000000-0000-4000-8000-00000000000a"
    ID_B = "20000000-0000-4000-8000-00000000000b"
    ID_P = "20000000-0000-4000-8000-00000000000p"
    NS = "project:n11-parity"
    SHARED = "parity lane shared state body"

    def _seed(self, tmp: Path) -> Path:
        src = tmp / "src" / "store.sqlite"
        src.parent.mkdir(parents=True)
        r = _run_store(src, "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(src))
        try:
            for mid in (self.ID_A, self.ID_B):
                _insert_memory_row(conn, mid, self.NS, "state", self.SHARED)
            conn.execute(
                "INSERT INTO memory_expiry (memory_id, inject_until,"
                " expiry_class) VALUES (?, '2027-01-01T00:00:00Z', 'metric')",
                (self.ID_B,))
            conn.commit()
        finally:
            _clean_close(conn)
        return src

    def _seed_dest(self, tmp: Path, name: str) -> Path:
        dest = tmp / name / "store.sqlite"
        dest.parent.mkdir(parents=True)
        r = _run_store(dest, "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(str(dest))
        try:
            _insert_memory_row(conn, self.ID_P, self.NS, "fact",
                               "destination preexisting unrelated row")
            conn.commit()
        finally:
            _clean_close(conn)
        return dest

    _SUMMARY_RE = re.compile(
        r"^\[zmem\] promote-store \((dry-run|promoted)\) from .*?: (.+)$")

    def _tallies(self, r) -> dict:
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        line = next(l for l in r.stdout.splitlines()
                    if l.startswith("[zmem] promote-store ("))
        m = self._SUMMARY_RE.match(line)
        self.assertIsNotNone(m, f"no summary line in {r.stdout!r}")
        raw = dict(pair.split("=", 1) for pair in m.group(2).split(", "))
        return {k: int(v) for k, v in raw.items()}

    def test_dry_run_matches_real_landed_outcomes(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c2-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        src = self._seed(tmp)
        base = self._seed_dest(tmp, "dst-seed")
        dest_dry = tmp / "dst-dry" / "store.sqlite"
        dest_real = tmp / "dst-real" / "store.sqlite"
        for d in (dest_dry, dest_real):
            d.parent.mkdir(parents=True)
            shutil.copy2(str(base), str(d))

        r_dry = _run_store(dest_dry, "promote-store", "--from", str(src),
                           "--dry-run")
        dry = self._tallies(r_dry)
        r_real = _run_store(dest_real, "promote-store", "--from", str(src))
        real = self._tallies(r_real)

        # Pin the REAL run's landed outcomes (the oracle; true at base and
        # under the fix): A lands under its own id, B folds into A, and B's
        # expiry record orphans-skips because B never landed.
        self.assertEqual(real.get("added"), 1,
                         f"real promote oracle drifted: {real}")
        self.assertEqual(real.get("deduped"), 1,
                         f"real promote oracle drifted: {real}")
        self.assertEqual(real.get("memory_expiry_orphan_skipped"), 1,
                         f"real promote oracle drifted: {real}")
        self.assertEqual(real.get("memory_expiry", 0), 0,
                         f"real promote oracle drifted: {real}")

        # THE CHECK (parity): the dry-run tallies must match the real
        # landed outcomes. At base would_promote counts the folded row too,
        # and the expiry side pass projects B's record as a would-copy.
        self.assertEqual(
            dry.get("would_promote"), real.get("added"),
            "dry-run would_promote overcounts: it counts the source row a "
            "REAL promote folds into an earlier source row (same "
            "content_norm) as landing under its own id "
            f"(dry={dry} real={real})")
        self.assertEqual(
            dry.get("memory_expiry_orphan_skipped", 0),
            real.get("memory_expiry_orphan_skipped", 0),
            "the folded row's expiry record must project as an orphan skip, "
            "matching the real run "
            f"(dry={dry} real={real})")
        self.assertEqual(
            dry.get("memory_expiry", 0), real.get("memory_expiry", 0),
            "dry-run projects the folded row's expiry record as a "
            "memory_expiry would-copy where the real run orphan-skips it "
            f"(dry={dry} real={real})")

        # Dry-run purity (true at base and under the fix).
        conn = sqlite3.connect(str(dest_dry))
        try:
            n = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1, "dry-run must not write rows")

        # Real landed shape (true at base and under the fix).
        conn = sqlite3.connect(str(dest_real))
        try:
            live_a = conn.execute(
                "SELECT count(*) FROM memory WHERE id=? AND superseded_at "
                "IS NULL", (self.ID_A,)).fetchone()[0]
            any_b = conn.execute(
                "SELECT count(*) FROM memory WHERE id=?", (self.ID_B,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual((live_a, any_b), (1, 0),
                         "real promote: A lands live, B folds into A")


# ---------------------------------------------------------------------------
# C3 — the jsonl family lane must stream, never read the whole file
# ---------------------------------------------------------------------------

class FamilyStreamingTest(unittest.TestCase):
    """Guard an unbounded `.read()` (no size argument) on the family file:
    the jsonl lane of dataset._read_family materializes the whole file
    today. io.BufferedReader is a C type (attributes immutable), so the
    guard wraps builtins.open for the family file's handle only — every
    other open passes through untouched — and lets bounded reads proceed,
    so a streaming fix stays green.
    """

    def test_read_family_streams_rows(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c3-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        exp = tmp / "exp"
        (exp / "data").mkdir(parents=True)
        rows = [{"id": f"30000000-0000-4000-8000-{i:012d}",
                 "namespace": "project:n11-stream", "seq": i}
                for i in range(5)]
        fam_name = (
            f"{dataset_mod._FAMILY_FILES['memories']}.jsonl")
        fam_path = exp / "data" / fam_name
        fam_path.write_bytes(
            b"".join(dataset_mod.canonical_row_bytes(r) for r in rows))

        fired = []

        class _GuardedFile:
            """Proxies the real binary handle; refuses only an UNBOUNDED
            read() on the guarded family file."""

            def __init__(self, fh):
                self._fh = fh

            def read(self, size=-1):
                if size is None or size < 0:
                    fired.append(True)
                    raise AssertionError(
                        "unbounded read(): dataset._read_family "
                        "materialized the whole family file instead of "
                        "streaming it")
                return self._fh.read(size)

            def __iter__(self):
                return self

            def __next__(self):
                line = self._fh.readline()
                if not line:
                    raise StopIteration
                return line

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()
                return False

            def close(self):
                self._fh.close()

            def __getattr__(self, name):
                return getattr(self._fh, name)

        orig_open = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            fh = orig_open(file, mode, *args, **kwargs)
            if ("b" in mode and isinstance(fh, _io.BufferedReader)
                    and Path(str(file)).name == fam_name):
                return _GuardedFile(fh)
            return fh

        with mock.patch.object(builtins, "open", guarded_open):
            got = dataset_mod._read_family(
                exp, "memories", dataset_mod.DATASET_FORMAT_JSONL)

        self.assertEqual(got, rows,
                         "all rows must parse identically under the "
                         "streaming reader")
        self.assertEqual(fired, [], "no unbounded read may occur")


# ---------------------------------------------------------------------------
# C4 — the knowledge-prefix predicate must agree with the SQL anchor
# ---------------------------------------------------------------------------

class PrefixCaseAgreementTest(unittest.TestCase):
    """SQLite LIKE is case-insensitive for ASCII, so the SQL exclusion
    windows (source_ref NOT LIKE 'knowledge:%') treat 'KNOWLEDGE:x' as a
    knowledge ref; is_pointer_ref's case-sensitive startswith disagrees.
    Post-fix the PYTHON side case-folds (SQLite LIKE stays the anchor) and
    the write-path admission guard refuses the uppercase form like the
    lowercase one.
    """

    def test_predicate_agrees_with_sql_anchor(self):
        probe = sqlite3.connect(":memory:")
        try:
            sql_not_like = probe.execute(
                "SELECT 'KNOWLEDGE:target-1:key-1' NOT LIKE 'knowledge:%'"
            ).fetchone()[0]
        finally:
            probe.close()
        self.assertEqual(
            sql_not_like, 0,
            "SQL anchor: NOT LIKE 'knowledge:%' is case-insensitive, so "
            "'KNOWLEDGE:target-1:key-1' DOES match the pattern "
            "(excluded=0). SQLite LIKE stays the anchor post-fix.")
        # RED at base: returns False today.
        self.assertTrue(
            knowledge_mod.is_pointer_ref("KNOWLEDGE:target-1:key-1"),
            "is_pointer_ref must case-fold to agree with the SQL "
            "exclusion: 'KNOWLEDGE:target-1:key-1' is a pointer ref")
        # Both directions hold post-fix (lowercase stays True).
        self.assertTrue(knowledge_mod.is_pointer_ref(
            "knowledge:target-1:key-1"))
        self.assertFalse(knowledge_mod.is_pointer_ref("session:n11"))
        self.assertFalse(knowledge_mod.is_pointer_ref(None))

    _DRIVER = r'''
import json
import sys

scripts_dir, result_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts_dir)

import storelib.knowledge  # arms the write capability, as the real CLI does
from storelib.schema import connect, init_db, migrate
from storelib.write import add_memory

conn = connect()
init_db(conn)
migrate(conn)
NS = "project:n11-prefix"


def attempt(ref):
    try:
        add_memory(conn, namespace=NS, type_="fact",
                   content="case-fold admission probe " + ref,
                   source_ref=ref, capture_mode="manual")
        return None
    except ValueError as exc:
        return str(exc)


lower_msg = attempt("knowledge:target-1:key-1")
upper_msg = attempt("KNOWLEDGE:target-1:key-1")
conn.close()
with open(result_path, "w", encoding="utf-8") as fh:
    json.dump({"lower_refused": lower_msg is not None,
               "upper_refused": upper_msg is not None,
               "lower_msg": lower_msg, "upper_msg": upper_msg}, fh)
'''

    def test_write_path_admission_refuses_uppercase_prefix(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c4-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        driver = tmp / "driver.py"
        driver.write_text(self._DRIVER, encoding="utf-8", newline="\n")
        result_path = tmp / "result.json"
        store = tmp / "store" / "store.sqlite"
        store.parent.mkdir(parents=True)
        r = _run_store(store, "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The driver opens the SAME per-test store through the production
        # open path (its env pins ZMEM_STORE before the storelib import).
        proc = subprocess.run(
            [PYTHON, "-X", "utf8", str(driver), str(SCRIPTS),
             str(result_path)],
            capture_output=True, text=True, env=_store_env(store),
            cwd=str(REPO_ROOT), timeout=600)
        self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertTrue(
            result["lower_refused"],
            "control leg: the lowercase knowledge: source_ref must be "
            f"refused by the write-path admission guard ({result})")
        # RED at base: the uppercase form is admitted today.
        self.assertTrue(
            result["upper_refused"],
            "write-path admission parity: a KNOWLEDGE:-prefixed source_ref "
            "must be refused exactly like the lowercase knowledge: form "
            f"({result})")


# ---------------------------------------------------------------------------
# C5 — a non-dict JSONL record must be refused as DatasetError
# ---------------------------------------------------------------------------

class NonDictRowRefusalTest(unittest.TestCase):
    """Craft a minimal JSONL dataset export whose memories.jsonl carries one
    scalar record FIRST (the required-key loop hits it before any checksum
    processing) plus one valid dict row. The validation surface must refuse
    it as a DatasetError naming the malformed record; today the membership
    test `key not in row` raises a raw TypeError instead.
    """

    def test_scalar_row_refused_as_dataseterror(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c5-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        exp = tmp / "exp"
        (exp / "data").mkdir(parents=True)

        good = {"id": "40000000-0000-4000-8000-000000000001",
                "namespace": "project:n11-nondict", "type": "fact",
                "content": "valid dict row body"}
        good["row_checksum"] = dataset_mod.row_checksum(good)
        # The scalar record FIRST: the required-key loop reaches it before
        # the checksum pass ever sees the valid row.
        (exp / "data" / "memories-000.jsonl").write_bytes(
            b"42\n" + dataset_mod.canonical_row_bytes(good))
        for family in ("episodes", "episode_members", "links"):
            stem = dataset_mod._FAMILY_FILES[family]
            (exp / "data" / f"{stem}.jsonl").write_bytes(b"")
        # memory_expiry is the additive family: absent file is tolerated.

        fake_hash = "f" * 64
        manifest = {
            "schema_version": dataset_mod.DATASET_SCHEMA_VERSION,
            "format": dataset_mod.DATASET_FORMAT_JSONL,
            "source_snapshot_hash": fake_hash,
            "row_counts": {"memories": 2, "episodes": 0,
                           "episode_members": 0, "links": 0},
            "namespaces": ["project:n11-nondict"],
        }
        (exp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")

        dest = tmp / "dest"
        with self.assertRaises(dataset_mod.DatasetError) as cm:
            dataset_mod.import_dataset(
                str(exp), revision=fake_hash, dest_dir=str(dest))
        msg = str(cm.exception)
        self.assertIn(
            "malformed", msg,
            "the refusal must name the malformed record "
            f"(got {type(cm.exception).__name__}: {msg!r})")
        self.assertIn(
            "memories", msg,
            "the refusal must name the family of the malformed record "
            f"(got {msg!r})")


# ---------------------------------------------------------------------------
# C6 — a stale non-NULL content_norm must be re-verified on the next open
# ---------------------------------------------------------------------------

class NormVerifyRepairTest(unittest.TestCase):
    """A store opened under the current client already carries the
    content_norm_repair_162 stamp (so the NULL-selecting repair
    short-circuits forever). Plant a STALE non-NULL content_norm on one
    row, close, re-open through the production open path: every row's
    content_norm must equal the canonical normalize of its own content. At
    base no verification pass fires and the stale value survives.
    """

    ROW_ID = "50000000-0000-4000-8000-000000000001"
    BODY = "norm verify repair body"

    def test_stale_nonnull_norm_heals_on_reopen(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-n11-c6-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        store = tmp / "store" / "store.sqlite"
        store.parent.mkdir(parents=True)

        r = _run_store(store, "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(
            _read_meta(store, "content_norm_repair_162"),
            "precondition: the store was opened under the current client "
            "and already carries the #491 repair stamp (the state the fix "
            "must still heal)")

        conn = sqlite3.connect(str(store))
        try:
            _insert_memory_row(conn, self.ROW_ID, "project:n11-norm",
                               "fact", self.BODY, source_ref="session:n11-norm")
            conn.execute(
                "UPDATE memory SET content_norm='STALE WRONG NORM' "
                "WHERE id=?", (self.ROW_ID,))
            conn.commit()
        finally:
            _clean_close(conn)

        # Production re-open (any store.py command runs the same
        # _prepare_store path: connect/init_db/migrate + repair passes).
        r = _run_store(store, "stats")
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))

        conn = sqlite3.connect(str(store))
        try:
            rows = conn.execute(
                "SELECT id, content, content_norm FROM memory").fetchall()
        finally:
            conn.close()
        self.assertTrue(rows, "the seeded row must survive the re-open")
        bad = [(mid, content, norm) for mid, content, norm in rows
               if norm != _normalize_content(content or "")]
        self.assertEqual(
            bad, [],
            "after a production re-open every row's content_norm must equal "
            "the canonical normalize of its own content: a store already "
            "stamped by the #491 NULL-repair keeps a planted STALE non-NULL "
            "norm forever because nothing re-verifies non-NULL values "
            f"(offenders: {bad})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
