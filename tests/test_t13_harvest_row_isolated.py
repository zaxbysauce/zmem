"""Default-mode harvest: a failing row is ISOLATED, the file continues
(issue #395 item 3 / trace AC5 — PRESERVING).

Issue #395 claimed default-mode harvest aborts the whole file on a row
delivery error. Trace probe P1 DISPROVED that at base: the per-row guard
around ingest_row (scripts/ingest_harvest.py common row loop) isolates the
bad row with a counted `FAILED row N` line on stderr, prints the summary,
and exits 1 — rows before and after are stored. This check freezes that
behavior as a PRESERVING pin (GREEN at base, must stay GREEN): a future
regression that lets one bad row abort the file (or crash the run) goes
RED.

Scenario copied from probe P1: a 3-row harvest file where row 2's content
carries a NUL byte (the store refuses it with ValueError); ingest runs in
DEFAULT mode (no --trust) against scripts/ingest_harvest.py's --store
skills/memory/scripts/store.py with a fully pinned scratch-store env.

Contract asserted:
  - exit code 1 (a failed row makes the run nonzero, but only that row);
  - `FAILED row 2` on stderr, no traceback anywhere;
  - the summary line is printed (`summary: ... 2 stored ... 1 failed`);
  - rows 1 and 3 ARE stored (read back from the scratch store's memory
    table by content — the harvest ingester mints new row ids), row 2 is
    not.

Runs standalone: python tests/test_t13_harvest_row_isolated.py
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
INGEST_HARVEST = REPO_ROOT / "scripts" / "ingest_harvest.py"


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


def _mkrow(i, content):
    # Row shape verbatim from trace probe P1.
    return {"id": f"0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f000{i}",
            "type": "fact", "namespace": "project:probe",
            "content": content, "tags": "probe",
            "signal": "test", "why": "t13 harvest isolation check"}


class DefaultHarvestRowIsolationTest(unittest.TestCase):
    """AC5 (PRESERVING): default-mode harvest isolates exactly the failing
    row, stores the rest, prints the summary, exits 1 — never aborts."""

    def test_default_harvest_row_error_isolated(self):
        tmp = tempfile.mkdtemp(prefix="t13-harvest-iso-")
        try:
            store = Path(tmp) / "store.sqlite"
            env = _clean_env(store)
            rows = [
                _mkrow(1, "row one good"),
                _mkrow(2, "row with nul \x00 byte"),
                _mkrow(3, "row three good"),
            ]
            harvest_path = Path(tmp) / "harvest.json"
            harvest_path.write_text(json.dumps(rows), encoding="utf-8")

            # DEFAULT mode: no --trust.
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(INGEST_HARVEST),
                 str(harvest_path), "--store", str(STORE_PY)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, cwd=str(REPO_ROOT), timeout=180)

            self.assertEqual(
                result.returncode, 1,
                f"default-mode harvest with one bad row must exit 1; got "
                f"{result.returncode}: {result.stdout[-300:]} "
                f"{result.stderr[-300:]}")
            self.assertIn(
                "FAILED row 2", result.stderr,
                f"the bad row must be isolated with a counted FAILED line "
                f"on stderr; stderr: {result.stderr[-400:]}")
            self.assertNotIn(
                "Traceback", result.stderr,
                f"row failure must not crash the run: "
                f"{result.stderr[-400:]}")
            self.assertIn(
                "summary:", result.stdout,
                f"the summary line must be printed; stdout tail: "
                f"{result.stdout[-400:]}")
            self.assertIn("2 stored", result.stdout, result.stdout[-400:])
            self.assertIn("1 failed", result.stdout, result.stdout[-400:])

            # Rows 1 and 3 stored; row 2 not. The harvest ingester MINTS
            # new row ids (it does not keep the file's id field), so the
            # readback is by content.
            conn = sqlite3.connect(str(store))
            try:
                contents = {
                    row[0] for row in conn.execute(
                        "SELECT content FROM memory").fetchall()}
            finally:
                conn.close()
            self.assertIn(
                rows[0]["content"], contents, "row 1 must be stored")
            self.assertIn(
                rows[2]["content"], contents, "row 3 must be stored")
            self.assertFalse(
                any("row with nul" in c for c in contents),
                "the NUL row 2 must NOT be stored")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
