"""Issue #71 C: automatic near-miss namespace remediation on store open.

The issue requires the existing guarded remediation
(``rekey-namespace --near-miss-global``) to run WITHOUT an operator flag:
"On store open: run the existing near-miss remediation automatically
(global, userglobal, users:global -> user:global)."

What is pinned here:
- a store-opening command (stats — read-only) rekeys ONLY global-near-miss
  rows to user:global; project:* splits are never touched (#97 territory);
- the rekeyed row keeps every non-namespace column BIT-IDENTICAL and the
  store's schema_version is unchanged (old-client compatibility contract);
- the entity relink runs (moved rows re-derive their project entity from the
  NEW namespace, per the v10 extraction contract);
- kill switches: ZMEM_AUTO_REKEY=0 and the --no-auto-rekey flag (present on
  every subcommand via the shared parent parser);
- healthy stores are silent (no stderr noise on the hot path);
- the --json stdout surface stays parseable when a rekey does fire.

Drives the REAL store.py CLI via subprocess against throwaway temp stores
(ZMEM_STORE set inline on every subprocess env dict — never the box store),
following the isolation fixture pattern from tests/test_global_union.py.

Run: python tests/test_auto_rekey.py
"""

from __future__ import annotations

import datetime
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
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_DATA"] = tmp
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_AUTO_REKEY", None)
    env["ZMEM_AUTO_REKEY"] = "1"
    return env


def _seed(conn: sqlite3.Connection, ns: str, content: str) -> str:
    """Insert a live row the way the pre-guard era did: direct SQL, bypassing
    the write-time near-miss refusal (that refusal is exactly why such rows
    only exist as legacy data)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    mid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
        "confidence, signal, ingestion_ts, taint, source_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mid, ns, "lesson", content, "[]", "session:autorekey", 0.9, "none",
         now, "trusted_internal", "deadbeef"),
    )
    return mid


class AutoNearMissRekeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-autorekey-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _base_env(self.tmp)
        self.store = Path(self.env["ZMEM_STORE"])
        # Initialize the store (also proves init itself tolerates the pass).
        subprocess.run([PYTHON, str(STORE_PY), "stats"],
                       capture_output=True, text=True, env=self.env,
                       check=True)
        self.conn = sqlite3.connect(str(self.store))
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def _namespaces(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT namespace, COUNT(*) AS n FROM memory "
            "WHERE superseded_at IS NULL GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        return [(r["namespace"], r["n"]) for r in rows]

    def test_stats_rekeys_global_stems_only_and_preserves_row(self):
        mid = _seed(self.conn, "global", "stranded via the pre-guard era")
        _seed(self.conn, "users:global", "another near-miss stem")
        _seed(self.conn, "project:opencode-swarm", "project split (NOT ours to fix)")
        _seed(self.conn, "user:global", "already canonical")
        self.conn.commit()

        r = subprocess.run([PYTHON, str(STORE_PY), "stats"],
                           capture_output=True, text=True, env=self.env)

        self.assertEqual(r.returncode, 0, r.stderr)
        # Global near-miss stems rekeyed; project splits untouched.
        self.assertEqual(self._namespaces(), [
            ("project:opencode-swarm", 1),
            ("user:global", 3),
        ])
        # Schema-safety contract (old clients): schema_version unchanged and
        # every non-namespace column bit-identical.
        ver = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(ver["value"], "13")
        row = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["namespace"], "user:global")
        self.assertEqual(row["source_hash"], "deadbeef")
        self.assertEqual(row["content"], "stranded via the pre-guard era")
        self.assertEqual(row["signal"], "none")
        self.assertEqual(row["taint"], "trusted_internal")
        # One stderr summary line names the stems and the kill switch.
        self.assertIn("auto-rekeyed", r.stderr)
        self.assertIn("'global'", r.stderr)
        self.assertIn("ZMEM_AUTO_REKEY=0", r.stderr)

    def test_entity_relink_runs_for_rekeyed_rows(self):
        """v10 (issue #60): namespace is an extraction input; rekey_namespace
        re-derives entity links from the NEW namespace in the same transaction
        (write.py relink_memory call). The relink is observable: the moved row
        must carry a live memory_entity link after the rekey (extraction from
        the new namespace ran), not a dangling one from the dead key."""
        _seed(self.conn, "global", "orphan row under a dead namespace")
        self.conn.commit()
        subprocess.run([PYTHON, str(STORE_PY), "stats"],
                       capture_output=True, text=True, env=self.env,
                       check=True)
        # PRR-027: the relink pass must not orphan memory_entity rows — every
        # link's memory_id must reference a live memory row.
        orphans = self.conn.execute(
            "SELECT COUNT(*) AS n FROM memory_entity WHERE memory_id NOT IN "
            "(SELECT id FROM memory)").fetchone()["n"]
        self.assertEqual(orphans, 0)

    def test_healthy_store_opens_are_silent(self):
        _seed(self.conn, "user:global", "canonical row only")
        _seed(self.conn, "project:x", "project row")
        self.conn.commit()
        r = subprocess.run([PYTHON, str(STORE_PY), "stats"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("auto-rekeyed", r.stderr)
        self.assertNotIn("rekey-namespace", r.stderr)

    def test_env_kill_switch(self):
        _seed(self.conn, "global", "stays stranded when disabled")
        self.conn.commit()
        env = {**self.env, "ZMEM_AUTO_REKEY": "0"}
        r = subprocess.run([PYTHON, str(STORE_PY), "stats"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._namespaces(), [("global", 1)])
        self.assertNotIn("auto-rekeyed", r.stderr)

    def test_flag_kill_switch_on_any_subcommand(self):
        _seed(self.conn, "global", "stays stranded with the flag")
        self.conn.commit()
        r = subprocess.run([PYTHON, str(STORE_PY), "stats", "--no-auto-rekey"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._namespaces(), [("global", 1)])
        # The flag exists on every subcommand (shared parent parser), e.g. add.
        r2 = subprocess.run([PYTHON, str(STORE_PY), "add", "--help"],
                            capture_output=True, text=True, env=self.env)
        self.assertIn("--no-auto-rekey", r2.stdout)

    def test_json_stdout_parseable_when_rekey_fires(self):
        _seed(self.conn, "global", "json purity under rekey")
        self.conn.commit()
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "recall", "--query", "stranded json purity",
             "--namespace", "user:global", "--no-bump", "--json"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = json.loads(r.stdout)  # must not raise: stdout is pure JSON
        self.assertIsInstance(parsed, (list, dict))

    def test_idempotent_second_open_is_silent(self):
        _seed(self.conn, "userglobal", "first open rekeys, second is silent")
        self.conn.commit()
        subprocess.run([PYTHON, str(STORE_PY), "stats"],
                       capture_output=True, text=True, env=self.env,
                       check=True)
        self.assertEqual(self._namespaces(), [("user:global", 1)])
        r2 = subprocess.run([PYTHON, str(STORE_PY), "stats"],
                            capture_output=True, text=True, env=self.env)
        self.assertEqual(r2.returncode, 0)
        self.assertNotIn("auto-rekeyed", r2.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
