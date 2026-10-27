"""Episode summary-pointer resolvability supplements (issue #395 item 2).

Non-frozen companions to the frozen decoy check
(tests/test_t13_peer_episode_decoy.py): the fix must not over-clear.
Authored-fresh pointers and allowed-namespace store-row pointers stay;
purged staged ids are cleared (the authored union subtracts purged ids,
mirroring the memory-pointer rule); empty summaries are untouched.
Subprocess-driven against a pinned scratch store (test_jsonl_sync pattern).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
NS = "project:allowed"
NS_B = "project:foreign"


def _row(rid, namespace, content="row"):
    return {
        "id": rid, "namespace": namespace, "type": "fact",
        "content": content, "tags": "", "source_ref": "",
        "confidence": 0.8, "signal": "test",
        "valid_from": "2026-10-27T00:00:00Z",
        "ingestion_ts": "2026-10-27T00:00:00Z",
        "superseded_at": None, "supersede_reason": "",
    }


def _episode(eid, sid, namespace=NS):
    return {
        "kind": "episode", "id": eid, "namespace": namespace,
        "started_at": "2026-10-27T00:00:00Z", "ended_at": "",
        "summary_memory_id": sid, "token_count": 3,
    }


class EpisodeResolvabilityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-t13-epsupp-")
        self._store = os.path.join(self._tmp, "store.sqlite")
        self._env = dict(os.environ)
        for k in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_MCP_TOKEN",
                  "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
                  "ZMEM_REMOTE_URL"):
            self._env.pop(k, None)
        self._env["ZMEM_STORE"] = self._store
        self._env["ZMEM_DATA"] = self._tmp
        self._env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self._env["ZMEM_MODELS_DIR"] = os.path.join(self._tmp, "no-models")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=self._env, cwd=str(REPO_ROOT),
            timeout=180)

    def _ingest_peer(self, rows, allow=NS):
        path = os.path.join(self._tmp, "peer.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return self._run("ingest-jsonl", "--in", path, "--strict",
                         "--trust", "untrusted-peer",
                         "--allow-namespace", allow)

    def _episode_sid(self, eid):
        import sqlite3
        conn = sqlite3.connect(self._store)
        row = conn.execute(
            "SELECT summary_memory_id FROM episode WHERE id=?",
            (eid,)).fetchone()
        conn.close()
        return row

    def test_authored_fresh_summary_pointer_kept(self):
        rid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707001"
        eid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707002"
        r = self._ingest_peer([_row(rid, NS, "authored summary source"),
                               _episode(eid, rid)])
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self._episode_sid(eid)
        self.assertIsNotNone(row, "the episode must land")
        self.assertEqual(row[0], rid,
                         "an AUTHORED staged summary row keeps the pointer")

    def test_allowed_store_row_pointer_kept(self):
        rid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707011"
        eid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707012"
        seed = os.path.join(self._tmp, "seed.jsonl")
        with open(seed, "w", encoding="utf-8") as f:
            f.write(json.dumps(_row(rid, NS, "pre-existing allowed row")) + "\n")
        r0 = self._run("ingest-jsonl", "--in", seed)
        self.assertEqual(r0.returncode, 0, r0.stderr)
        r = self._ingest_peer([_episode(eid, rid)])
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self._episode_sid(eid)
        self.assertEqual(row[0], rid,
                         "a store row inside the allow-list keeps the pointer")

    def test_pre_purged_id_pointer_cleared(self):
        rid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707021"
        eid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707022"
        # Land a live row, then PURGE it (the purged_id registry is
        # written by the purge command — issue #255 — not by expiry
        # records): after purge the id is on the deny-list forever.
        state_row = _row(rid, NS, "purged later")
        state_row["type"] = "state"
        r0 = self._ingest_peer([state_row])
        self.assertEqual(r0.returncode, 0, r0.stderr)
        rp = self._run("purge", "--id", rid)
        self.assertEqual(rp.returncode, 0, rp.stderr)
        # A later file re-stages a decoy with the same id (claiming the
        # allowed ns) plus the episode pointer: the authored union
        # subtracts the purged id, so the pointer must clear — mirroring
        # the memory-pointer rule exactly.
        decoy = _row(rid, NS, "decoy restage")
        decoy["type"] = "state"
        r = self._ingest_peer([decoy, _episode(eid, rid)])
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self._episode_sid(eid)
        self.assertEqual(row[0] or "", "",
                         "a pre-purged id must not stay resolvable")

    def test_empty_summary_untouched(self):
        eid = "3c3c3c3c-4d4d-5e5e-6f6f-707070707031"
        r = self._ingest_peer([_episode(eid, "")])
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self._episode_sid(eid)
        self.assertEqual(row[0] or "", "")


if __name__ == "__main__":
    unittest.main()
