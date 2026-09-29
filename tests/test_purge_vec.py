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


class PurgeKeeperResidueContractTest(unittest.TestCase):
    """Final-critic follow-ups: the keeper refusal (verbatim purged text
    retained) and the shared-fragment rc-0 contract get permanent coverage
    here (the frozen q01 C6 constants are lexically disjoint, so neither
    shape is reachable from that suite)."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="zmem-purge-keep-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**os.environ}
        env.update({"ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
                    "ZMEM_DATA": tmp,
                    "ZMEM_MODELS_DIR": os.path.join(tmp, "nm"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0",
                    "ZMEM_CAPTURE_MODE": "manual"})
        self.env = env
        self.tmp = tmp

    def _add(self, content):
        r = subprocess.run(
            [sys.executable, str(STORE_PY), "add", "--namespace", NS,
             "--type", "lesson", "--content", content, "--signal",
             "test", "--confidence", "0.9", "--json"],
            capture_output=True, text=True, env=self.env, timeout=120,
            cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _purge(self, mid):
        return subprocess.run(
            [sys.executable, str(STORE_PY), "purge", "--id", mid],
            capture_output=True, text=True, env=self.env, timeout=180,
            cwd=str(REPO_ROOT))

    def _content(self, mid):
        conn = sqlite3.connect(
            "file:" + self.env["ZMEM_STORE"].replace(os.sep, "/")
            + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT content FROM memory WHERE id=?",
                               (mid,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_verbatim_quoting_keeper_is_refused_by_name(self):
        # keeper FIRST: adding the keeper after target would dedup (its
        # content will contain the full target text). Refusal shape: the
        # stored block HEADER names some other id, so the header-keyed strip
        # cannot remove the purged text and it survives verbatim — the purge
        # must refuse (exit 4) naming the keeper, not silently keep the text
        # (a block whose header names the target is removed cleanly and is
        # the rc-0 contract exercised by the next test).
        keeper = self._add("keeper quotes zebraquux marker again outside")
        target = self._add("deploy checklist zebraquux marker duplication case")
        decoy = "00000000-0000-4000-8000-00000000decoy"
        sep = chr(10) * 2 + "--- merged from %s ---" % decoy + chr(10)
        dup = "keeper quotes zebraquux marker again outside" + sep + (
            "deploy checklist zebraquux marker duplication case")
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        conn.execute("UPDATE memory SET content=?, content_norm=?,"
                     " merged_from=? WHERE id=?",
                     (dup, dup.lower(), target, keeper))
        conn.commit()
        conn.close()
        r = self._purge(target)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn(keeper, r.stderr)
        self.assertIsNotNone(self._content(keeper))
        self.assertIn("zebraquux", (self._content(keeper) or "").lower())

    def test_shared_fragment_keeper_rewrites_cleanly(self):
        target = self._add("deployment checklist zebraquux marker r two probe")
        keeper = self._add("deployment runbook kept for the r two probe team")
        sep = chr(10) * 2 + "--- merged from %s ---" % target + chr(10)
        merged = ("deployment runbook kept for the r two probe team" + sep +
                  "deployment checklist zebraquux marker r two probe")
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        conn.execute("UPDATE memory SET content=?, content_norm=?,"
                     " merged_from=? WHERE id=?",
                     (merged, merged.lower(), target, keeper))
        conn.commit()
        conn.close()
        r = self._purge(target)
        self.assertEqual(r.returncode, 0, r.stderr)
        kept = self._content(keeper)
        self.assertIsNotNone(kept)
        self.assertNotIn("zebraquux", kept.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
