"""Issue #524 regression: import-store must complete on WAL-journal source
stores (the zmem default journal mode).

Root cause (fixed in import-store.py): the FTS optimize INSERT opened an
implicit transaction (Python sqlite3 legacy isolation) that was never
committed before ``PRAGMA journal_mode=DELETE``; SQLite refuses the mode
change inside a transaction ("cannot change out of wal mode from within a
transaction") and the staged copy — which INHERITS the source's WAL mode
from the online backup's page-1 copy — made every WAL source fail wholesale
after successful staging, leaving an empty destination.

The knowledge-prefix demotion does NOT mask the bug (issue claim 2,
disproved by probe during the trace): the failure is journal-mode-only, so
this file pins both row shapes.

Legs:
- plain-row WAL source: exit 0, destination carries the row with its
  ORIGINAL id and content, destination exists.
- knowledge-pointer WAL source: same outcome (row-shape independence).
- DELETE-mode source: still imports (the PRAGMA result check never
  false-fails on an already-DELETE database).

Hermetic discipline: ZMEM_STORE/ZMEM_DATA pinned to a scratch dir in
setUpModule BEFORE any storelib import (storelib freezes STORE_PATH at
import), restored in tearDownModule; source stores built through the real
schema; import-store driven as a subprocess exactly like production.

Run (the file is its own runner):
    python -X utf8 tests/test_import_store_wal.py
Prints a final CHECK-PASS / CHECK-FAIL line.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "skills", "memory", "scripts")
IMPORT_STORE = os.path.join(SCRIPTS, "import-store.py")

_SAVED_ENV = {}


def setUpModule():
    scratch = tempfile.mkdtemp(prefix="zw-524-wal-")
    _SAVED_ENV["scratch"] = scratch
    for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_NAMESPACE",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        _SAVED_ENV[key] = os.environ.pop(key, None)
    os.environ["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
    os.environ["ZMEM_DATA"] = scratch


def tearDownModule():
    for key, value in _SAVED_ENV.items():
        if key == "scratch":
            shutil.rmtree(value, ignore_errors=True)
        elif value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _build_source(path: str, row_sql: str, journal_mode: str) -> None:
    sys.path.insert(0, SCRIPTS)
    from storelib.schema import init_db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(row_sql)
    conn.commit()
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == journal_mode.lower(), (
        f"source journal mode is {mode}, expected {journal_mode}")


def _run_import(source: str, dest_dir: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.pop("ZMEM_STORE", None)
    env.pop("ZMEM_DATA", None)
    return subprocess.run(
        [sys.executable, "-X", "utf8", IMPORT_STORE,
         "--source", source, "--dest-dir", dest_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=REPO, timeout=280)


PLAIN_ROW = (
    "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
    " confidence, signal, ingestion_ts, content_norm) VALUES"
    " ('0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f','ns','lesson',"
    " 'plain row body','[]','plain-ref',0.5,'test',"
    " '2026-01-01T00:00:00Z','plain row body')")

KNOWLEDGE_ROW = (
    "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
    " confidence, signal, ingestion_ts, content_norm) VALUES"
    " ('1f1f1f1f-1f1f-4f1f-8f1f-1f1f1f1f1f1f','ns','lesson',"
    " 'knowledge:payload body','[]','knowledge:abc123',0.5,'test',"
    " '2026-01-01T00:00:00Z','knowledge:payload body')")


class ImportStoreWalSourceTest(unittest.TestCase):
    """AC4 (#524): a WAL source with an ordinary (non-demoted) row imports
    cleanly, end to end, with the row landing intact."""

    def test_plain_row_wal_source_imports(self):
        scratch = tempfile.mkdtemp(prefix="zw-524-plain-")
        try:
            src = os.path.join(scratch, "src.sqlite")
            dest_dir = os.path.join(scratch, "dest")
            _build_source(src, PLAIN_ROW, "WAL")
            proc = _run_import(src, dest_dir)
            self.assertEqual(
                proc.returncode, 0,
                "WAL source import failed (the #524 defect): "
                f"stderr={proc.stderr[-400:]!r}")
            dest = os.path.join(dest_dir, "store.sqlite")
            self.assertTrue(os.path.exists(dest), "destination store absent")
            conn = sqlite3.connect(dest)
            try:
                rows = conn.execute(
                    "SELECT id, content FROM memory").fetchall()
            finally:
                conn.close()
            self.assertEqual(
                rows, [("0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f",
                        "plain row body")],
                "destination must carry the imported row with its ORIGINAL "
                "id and content")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_knowledge_row_wal_source_imports(self):
        """The demotion path never masked the WAL failure (issue claim 2
        disproved during the trace) — pin the row-shape independence."""
        scratch = tempfile.mkdtemp(prefix="zw-524-know-")
        try:
            src = os.path.join(scratch, "src.sqlite")
            dest_dir = os.path.join(scratch, "dest")
            _build_source(src, KNOWLEDGE_ROW, "WAL")
            proc = _run_import(src, dest_dir)
            self.assertEqual(
                proc.returncode, 0,
                "WAL source import failed on a knowledge row (the #524 "
                f"defect is journal-mode-only): stderr={proc.stderr[-400:]!r}")
            dest = os.path.join(dest_dir, "store.sqlite")
            conn = sqlite3.connect(dest)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM memory").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_delete_mode_source_still_imports(self):
        """The PRAGMA-result check must never false-fail on an already-DELETE
        source (the switch is a no-op there and still returns 'delete')."""
        scratch = tempfile.mkdtemp(prefix="zw-524-del-")
        try:
            src = os.path.join(scratch, "src.sqlite")
            dest_dir = os.path.join(scratch, "dest")
            _build_source(src, PLAIN_ROW, "DELETE")
            proc = _run_import(src, dest_dir)
            self.assertEqual(
                proc.returncode, 0,
                f"DELETE-mode source import failed: {proc.stderr[-400:]!r}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ImportStoreWalSourceTest,):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("CHECK-PASS import-store WAL regression")
        return 0
    print("CHECK-FAIL import-store WAL regression")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
