"""Issue #170 evidence-writer and fail-open boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
LAUNCHER = ROOT / "hooks" / "zmem-launch.js"
PYTHON = sys.executable


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        ZMEM_STORE=str(scratch / "store.sqlite"),
        ZMEM_DATA=str(scratch),
        ZMEM_MODELS_DIR=str(scratch / "missing-models"),
        ZMEM_MODEL_AUTODOWNLOAD="0",
        ZMEM_NAMESPACE="project:issue170",
    )
    return env


class EvidenceWritersIssue170Test(unittest.TestCase):
    def test_cli_edit_writer_round_trip_and_invalid_payload_is_fail_open(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-writers-") as td:
            scratch = Path(td)
            env = _env(scratch)
            initialized = subprocess.run(
                [PYTHON, str(STORE), "init"], cwd=ROOT, env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            payload = {
                "session_id": "issue170-writer",
                "lane": "codex",
                "moment": "user_prompt",
                "kind": "edit",
                "ts": "2026-09-10T00:00:00Z",
                "excerpt": "README.md",
                "ref_path": "README.md",
                "ref_offset": 12,
                "id": "00000000-0000-4000-8000-000000001710",
            }
            written = subprocess.run(
                [PYTHON, str(STORE), "evidence", "write"], cwd=ROOT, env=env,
                input=json.dumps(payload), text=True, capture_output=True,
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            listed = subprocess.run(
                [PYTHON, str(STORE), "evidence", "list", "--namespace",
                 "project:issue170", "--session-id", "issue170-writer", "--json"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["kind"], "edit")
            self.assertEqual(json.loads(listed.stdout)[0]["moment"], "user_prompt")
            shown = subprocess.run(
                [PYTHON, str(STORE), "evidence", "show", "--namespace",
                 "project:issue170", "--id", payload["id"], "--json"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["ref_path"], "README.md")

            bad = dict(payload, id="00000000-0000-4000-8000-000000001711", kind="not-a-kind")
            rejected = subprocess.run(
                [PYTHON, str(STORE), "evidence", "write"], cwd=ROOT, env=env,
                input=json.dumps(bad), text=True, capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            conn = sqlite3.connect(env["ZMEM_STORE"])
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1
                )
            finally:
                conn.close()

    def test_launcher_success_and_spawn_failure_remain_fail_open(self):
        script = r'''
const launcher = require(process.argv[1]);
const payload = {session_id: "issue170-writer", tool_name: "Bash",
  tool_input: {command: "git status"}};
const meta = {session_id: "issue170-writer",
  evidence_id: "00000000-0000-4000-8000-000000001712"};
const env = {ZMEM_HOST: "codex", ZMEM_NAMESPACE: "project:issue170"};
const ok = launcher.recordEvidence("codex", "convention-capture", payload, meta,
  env, () => "2026-09-10T00:00:00Z", () => ({stdin: {write() {}, end() {}}, on() {}, unref() {}}));
let threw = false;
try {
  launcher.recordEvidence("codex", "convention-capture", payload, meta, env,
    () => "2026-09-10T00:00:00Z", () => { throw new Error("writer unavailable"); });
} catch { threw = true; }
process.stdout.write(JSON.stringify({ok, threw}));
'''
        result = subprocess.run(
            ["node", "-e", script, str(LAUNCHER)], cwd=ROOT,
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "threw": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
