"""Issue #77: the store CLI warns (stderr only) when a caller manually writes
the reserved `organize:` structural source_ref prefix, while JSON stdout stays
machine-parseable. The warning must never reject the write and never appear
on stdout (machine-output contract, PRR-004 lineage).

Run: python tests/test_store_cli.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

WARNING = ("[zmem] WARNING: source_ref prefix organize: is reserved "
           "for organize summaries")


class StoreCliReservedPrefixTest(unittest.TestCase):
    """Each test owns a fresh temp store driven through the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-storecli-77-")
        self.addCleanup(self._cleanup)
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = {**os.environ,
                    "ZMEM_STORE": self.store,
                    "ZMEM_DATA": self.tmp,
                    "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env_extra=None):
        env = {**self.env, **(env_extra or {})}
        return subprocess.run([PYTHON, str(STORE_PY), *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_add_reserved_source_ref_warns_on_stderr(self):
        """A reserved `organize:` source_ref warns on stderr, the write still
        succeeds, and a non-reserved ref stays silent."""
        r = self._run("add", "--namespace", "project:storecli-77",
                      "--type", "fact",
                      "--content", "row carrying a forged structural ref",
                      "--source-ref", "organize:forged-topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(WARNING, r.stderr)
        self.assertNotIn(WARNING, r.stdout)
        r2 = self._run("add", "--namespace", "project:storecli-77",
                       "--type", "fact",
                       "--content", "row with a plain provenance ref",
                       "--source-ref", "user:plain")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertNotIn("reserved", r2.stderr)
        # The row really landed under the forged ref (warn, never reject).
        import sqlite3
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT superseded_at FROM memory WHERE source_ref=?",
                ("organize:forged-topic",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])

    def test_json_reserved_source_ref_keeps_stdout_parseable(self):
        """`add --json` with a reserved source_ref: stdout stays strictly
        json.loads-parseable with the warning confined to stderr; ingest-jsonl
        row-carried refs warn the same way."""
        r = self._run("add", "--namespace", "project:storecli-77",
                      "--type", "fact",
                      "--content", "json row with forged ref",
                      "--source-ref", "organize:forged-json",
                      "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(WARNING, r.stderr)
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"--json stdout not parseable: {exc}: {r.stdout[:200]!r}")
        self.assertTrue(payload)
        # JSONL ingest with a row-carried reserved source_ref warns too.
        rowfile = Path(self.tmp) / "rows.jsonl"
        rowfile.write_text(
            '{"id": "00000000-0000-4000-8000-000000000779", '
            '"namespace": "project:storecli-77", "type": "fact", '
            '"content": "ingested row with reserved ref", '
            '"source_ref": "organize:forged-ingest", '
            '"timestamp": "2026-09-10T00:00:00Z"}\n', encoding="utf-8")
        r2 = self._run("ingest-jsonl", "--in", str(rowfile))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn(WARNING, r2.stderr)
        # The warning never leaks into stdout (ingest's human report lines
        # stay clean; the JSON-parseable surface `add --json` is covered
        # above).
        self.assertNotIn(WARNING, r2.stdout)
        self.assertIn("added=1", r2.stdout)
        import sqlite3
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT superseded_at FROM memory WHERE source_ref=?",
                ("organize:forged-ingest",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row,
                             "ingested row must land with its own ref")

    def test_override_does_not_silence_row_carried_reserved_ref(self):
        """PRR-004: a non-reserved --source-ref override must not silently
        swallow a row-carried reserved ref — the row's original ref warns
        before the override replaces it."""
        rowfile = Path(self.tmp) / "rows-override.jsonl"
        row = ("{\"id\": \"00000000-0000-4000-8000-000000000780\", "
               "\"namespace\": \"project:storecli-77\", \"type\": \"fact\", "
               "\"content\": \"ingested row with reserved ref plus override\", "
               "\"source_ref\": \"organize:forged-then-overridden\", "
               "\"timestamp\": \"2026-09-10T00:00:00Z\"}")
        rowfile.write_text(row + chr(10), encoding="utf-8")
        r = self._run("ingest-jsonl", "--in", str(rowfile),
                      "--source-ref", "user:batch-override")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(WARNING, r.stderr,
                      "row-carried reserved ref must warn even under an override")
        self.assertNotIn(WARNING, r.stdout)



if __name__ == "__main__":
    unittest.main(verbosity=2)
