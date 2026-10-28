"""Strict-lane episode decoy: staged CLAIM must not keep a foreign
summary_memory_id pointer (issue #395 item 2 / trace AC4).

storelib/sync.py's strict episode pass clears an episode's
``summary_memory_id`` only when the id is neither store-resolvable in an
allowed namespace NOR claimed by a STAGED row in an allowed namespace
(``staged_mem_ns`` fallback, sync.py:2102-2109 at base). A staged claim is
attacker-controlled: a peer file can stage a memory row with an id that
collides with a FOREIGN-namespace store row; the staged row lands as
``skipped`` (never authored — the id already exists), yet its claimed
namespace keeps the episode's pointer alive, aimed at the foreign row.

Trace probe P3 proved the pointer is KEPT at base end-to-end (subprocess
ingest, then a direct sqlite read: summary_memory_id == the foreign row's
id). This check freezes the same scenario: RED at base (pointer kept),
GREEN after the fix (pointer CLEARED — the episode row lands with an empty
summary_memory_id).

Scenario (row shapes copied verbatim from probe P3, which validated
cleanly against the base parser):
  1. seed a project:foreign STORE row with id X via LOCAL ingest-jsonl
     (local ingest keeps ids);
  2. strict-peer-ingest a peer file staging a memory row with the SAME id
     X claiming project:allowed (the decoy) plus an episode row whose
     summary_memory_id = X;
  3. SELECT summary_memory_id FROM episode WHERE id=<episode id> and
     assert it was CLEARED (empty string). At base it equals X.

Every subprocess runs with a fully pinned env (ZMEM_STORE / ZMEM_DATA /
ZMEM_MODELS_DIR / ZMEM_MODEL_AUTODOWNLOAD set inline, every other ZMEM_*
variable popped) so the scratch store is the only store that can be
touched.

Runs standalone: python tests/test_t13_peer_episode_decoy.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

# Row shapes verbatim from trace probe P3.
_X_ID = "1a1a1a1a-2b2b-3c3c-4d4d-5e5e5e5e5e5e"
_E_ID = "2b2b2b2b-3c3c-4d4d-5e5e-6f6f6f6f6f6f"

_FOREIGN_ROW = {
    "id": _X_ID, "namespace": "project:foreign", "type": "fact",
    "content": "foreign ns store row X", "tags": "", "source_ref": "",
    "confidence": 0.8, "signal": "test",
    "valid_from": "2026-10-27T00:00:00Z",
    "ingestion_ts": "2026-10-27T00:00:00Z",
    "superseded_at": None, "supersede_reason": "",
}

_DECOY_STAGED_ROW = dict(
    _FOREIGN_ROW, namespace="project:allowed", content="decoy staged claim")

_EPISODE_ROW = {
    "kind": "episode", "id": _E_ID, "namespace": "project:allowed",
    "started_at": "2026-10-27T00:00:00Z", "ended_at": "",
    "summary_memory_id": _X_ID, "token_count": 3,
}


def _clean_env(store_path):
    """Subprocess env pinned to the scratch store only (probe pattern)."""
    env = dict(os.environ)
    for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_MCP_TOKEN",
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR"):
        env.pop(key, None)
    env["ZMEM_STORE"] = str(store_path)
    env["ZMEM_DATA"] = str(Path(store_path).parent)
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_MODELS_DIR"] = str(Path(store_path).parent / "no-models")
    return env


def _run(cmd, env, timeout=180):
    return subprocess.run(
        [sys.executable, "-X", "utf8"] + [str(c) for c in cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO_ROOT), timeout=timeout)


class StrictEpisodeDecoyTest(unittest.TestCase):
    """AC4: the strict lane must CLEAR an episode summary_memory_id that
    only a staged (never-authored) claim would keep alive."""

    def test_strict_episode_decoy_cleared(self):
        tmp = tempfile.mkdtemp(prefix="t13-episode-decoy-")
        try:
            store = Path(tmp) / "store.sqlite"
            env = _clean_env(store)

            # 1. Seed the foreign-namespace store row with id X (local
            #    ingest keeps explicit ids).
            seed_path = Path(tmp) / "local.jsonl"
            seed_path.write_text(
                json.dumps(_FOREIGN_ROW) + "\n", encoding="utf-8")
            seed = _run([STORE_PY, "ingest-jsonl", "--in", seed_path], env)
            self.assertEqual(
                seed.returncode, 0,
                f"local seed ingest failed: {seed.stderr[-400:]}")

            # 2. Strict peer file: the decoy staged memory row (same id X,
            #    claiming project:allowed) + the episode pointing at X.
            peer_path = Path(tmp) / "peer.jsonl"
            peer_path.write_text(
                json.dumps(_DECOY_STAGED_ROW) + "\n"
                + json.dumps(_EPISODE_ROW) + "\n", encoding="utf-8")
            strict = _run(
                [STORE_PY, "ingest-jsonl", "--in", peer_path, "--strict",
                 "--trust", "untrusted-peer",
                 "--allow-namespace", "project:allowed"], env)
            self.assertEqual(
                strict.returncode, 0,
                f"strict peer ingest failed: {strict.stderr[-400:]}")
            self.assertNotIn(
                "Traceback", strict.stderr,
                f"strict peer ingest crashed: {strict.stderr[-400:]}")

            # 3. The pointer verdict: the episode row must exist and its
            #    summary_memory_id must have been CLEARED. At base the
            #    staged-claim fallback keeps it equal to X (probe P3).
            conn = sqlite3.connect(str(store))
            try:
                row = conn.execute(
                    "SELECT summary_memory_id FROM episode WHERE id=?",
                    (_E_ID,)).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(
                row, "the episode row must land in the store")
            self.assertEqual(
                row[0], "",
                "a staged (never-authored) claim must not keep the episode "
                "summary_memory_id pointing at a foreign-namespace store "
                f"row; pointer kept as {row[0]!r}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
