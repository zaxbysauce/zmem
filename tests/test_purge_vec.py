"""AC10 guardrail for issue #255: purge deletes the memory_vec entry by its
memory_id column (the real table needs the sqlite-vec extension, which CI and
model-absent hosts do not load — this stand-in exercises the exact DELETE
statement shape on a plain table named memory_vec).

Run: python tests/test_purge_vec.py   (no pytest)
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
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable
NS = "project:q01vec"


class PurgeVecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-purge-vec-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ}
        self.env["ZMEM_STORE"] = self.store
        self.env["ZMEM_DATA"] = self.tmp
        self.env["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-such-models")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env["ZMEM_CAPTURE_MODE"] = "manual"
        for var in ("ZMEM_TEST_NOW", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
                    "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS",
                    "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
                    "ZMEM_AUTO_REKEY", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_QUERY_CONTEXT", "ZMEM_CROSS_ENCODER"):
            self.env.pop(var, None)

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args], env=self.env,
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))

    def test_purge_deletes_memory_vec_row_by_memory_id(self):
        r = self._run("add", "--namespace", NS, "--type", "lesson",
                      "--content", "vector holder row for purge vec check",
                      "--signal", "test", "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = json.loads(r.stdout)["id"]

        # Seed the REAL memory_vec (vec0) row the way every write path does:
        # load sqlite-vec on this handle (schema._load_vec shape), pack a
        # float32 vector matching the table dimension. On hosts without the
        # extension the add above degrades vec-less and this test skips
        # itself rather than reporting a false result.
        import struct
        conn = sqlite3.connect(self.store)
        try:
            try:
                import sqlite_vec
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception:
                self.skipTest("sqlite-vec unavailable on this host")
            dim = None
            for probe in (384, 16, 64):
                try:
                    conn.execute(
                        "INSERT INTO memory_vec(embedding, memory_id) "
                        "VALUES (?, ?)",
                        (struct.pack("%df" % probe, *([0.01] * probe)), mid))
                    dim = probe
                    break
                except sqlite3.OperationalError as e:
                    if "Dimension mismatch" not in str(e):
                        raise
            if dim is None:
                self.fail("could not determine memory_vec dimension")
            conn.commit()
        finally:
            conn.close()

        # The vec delete is wrapped in try/except OperationalError in purge,
        # so a sqlite-vec-shaped failure must not mask the delete either.
        r = self._run("purge", "--id", mid)
        self.assertEqual(r.returncode, 0, r.stderr)

        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        try:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception:
                pass  # read of the vec0 table needs the module; insert proved it
            count = conn.execute(
                "SELECT COUNT(*) FROM memory_vec WHERE memory_id=?",
                (mid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0,
                         "purge must delete the memory_vec entry by memory_id")


class PurgeJsonSnapshotsContractTest(unittest.TestCase):
    """PRR-018: the --json snapshots payload is structured (dicts with
    snapshot/status/detail), not prose strings."""

    def test_snapshots_payload_is_structured(self):
        tmp = tempfile.mkdtemp(prefix="zmem-purge-json-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**os.environ}
        env.update({"ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                    "ZMEM_DATA": tmp,
                    "ZMEM_MODELS_DIR": os.path.join(tmp, "nm"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0",
                    "ZMEM_CAPTURE_MODE": "manual"})
        r = subprocess.run(
            [sys.executable, str(STORE_PY), "add", "--namespace", NS,
             "--type", "lesson", "--content",
             "json snapshots contract probe row", "--signal", "test",
             "--confidence", "0.9", "--json"],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = json.loads(r.stdout)["id"]
        bdir = os.path.join(tmp, "backups")
        rb = subprocess.run(
            [sys.executable, str(STORE_PY), "backup", "--out-dir", bdir],
            capture_output=True, text=True, env=env, timeout=120,
            cwd=str(REPO_ROOT))
        self.assertEqual(rb.returncode, 0, rb.stderr)
        r = subprocess.run(
            [sys.executable, str(STORE_PY), "purge", "--id", mid,
             "--scrub-backups", "--out-dir", bdir, "--json"],
            capture_output=True, text=True, env=env, timeout=180,
            cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertIsInstance(payload["snapshots"], list)
        self.assertGreaterEqual(len(payload["snapshots"]), 1)
        for entry in payload["snapshots"]:
            self.assertIsInstance(entry, dict, entry)
            self.assertIn(entry["status"], ("scrubbed", "skipped"))
            self.assertTrue(entry["snapshot"].endswith(".sqlite"))
            self.assertIsInstance(entry["detail"], str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
