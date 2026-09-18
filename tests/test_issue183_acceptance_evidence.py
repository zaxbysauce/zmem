"""Acceptance evidence for issue #183's expanded dependency scope.

These tests intentionally exercise the #169/#170 public seams through the
store command (and the launcher seam), rather than checking implementation
source.  They are isolated from the operator store and are expected to fail
on the pre-#169/#170 base because the commands and tables do not exist yet.
"""

from __future__ import annotations

import hashlib
import importlib.util
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        ZMEM_STORE=str(scratch / "store.sqlite"),
        ZMEM_DATA=str(scratch),
        ZMEM_MODELS_DIR=str(scratch / "missing-models"),
        ZMEM_MODEL_AUTODOWNLOAD="0",
        ZMEM_EMBED_PROFILE="fake",
        ZMEM_NAMESPACE="project:acceptance",
    )
    return env


def _run(scratch: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(STORE), *args],
        cwd=ROOT,
        env=_env(scratch),
        input=input_text,
        text=True,
        capture_output=True,
    )


def _record(evidence_id: str, *, ts: str, excerpt: str, kind: str = "turn") -> dict[str, object]:
    return {
        "session_id": "acceptance-183",
        "lane": "codex",
        "moment": "user_prompt",
        "kind": kind,
        "ts": ts,
        "excerpt": excerpt,
        "ref_path": "tests/fixtures/183/session.txt",
        "ref_offset": 0,
        "id": evidence_id,
    }


class Issue183AcceptanceEvidenceTest(unittest.TestCase):
    def test_ac8_schema14_redaction_hash_round_trip_and_rollback(self):
        """AC8: schema14 evidence survives deterministic JSONL transport."""
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac8-") as td:
            scratch = Path(td)
            env = _env(scratch)
            self.assertEqual(_run(scratch, "init").returncode, 0)

            conn = sqlite3.connect(env["ZMEM_STORE"])
            with conn:
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            conn.close()
            self.assertEqual(version, "14")
            self.assertTrue(
                {"evidence", "episode_evidence", "memory_evidence"}.issubset(tables)
            )

            secret = _record(
                "00000000-0000-4000-8000-000000000801",
                ts="2026-09-10T00:00:00Z",
                excerpt="token=sk-test-1234567890",
            )
            result = _run(scratch, "evidence", "write", input_text=json.dumps(secret))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, secret["id"] + "\n")
            shown = _run(
                scratch,
                "evidence",
                "show",
                "--namespace",
                "project:acceptance",
                "--id",
                str(secret["id"]),
                "--json",
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            row = json.loads(shown.stdout)
            self.assertEqual(row["excerpt"], "[REDACTED_SECRET]")
            self.assertNotIn("hash", row)
            secret_conn = sqlite3.connect(env["ZMEM_STORE"])
            with secret_conn:
                stored = secret_conn.execute(
                    "SELECT kind, ts, excerpt, hash FROM evidence WHERE id=?",
                    (secret["id"],),
                ).fetchone()
            secret_conn.close()
            self.assertEqual(
                stored[3], hashlib.sha256(f"{stored[0]}|{stored[1]}|{stored[2]}".encode()).hexdigest()
            )

            capped = _record(
                "00000000-0000-4000-8000-000000000802",
                ts="2026-09-10T00:00:01Z",
                excerpt="ordinary evidence " * 30,
            )
            self.assertEqual(_run(scratch, "evidence", "write", input_text=json.dumps(capped)).returncode, 0)
            capped_row = json.loads(
                _run(
                    scratch,
                    "evidence",
                    "show",
                    "--namespace",
                    "project:acceptance",
                    "--id",
                    str(capped["id"]),
                    "--json",
                ).stdout
            )
            self.assertNotIn("hash", capped_row)
            capped_conn = sqlite3.connect(env["ZMEM_STORE"])
            with capped_conn:
                capped_stored = capped_conn.execute(
                    "SELECT kind, ts, excerpt, hash FROM evidence WHERE id=?",
                    (capped["id"],),
                ).fetchone()
            capped_conn.close()
            self.assertEqual(capped_stored[2], capped["excerpt"][:400])
            self.assertEqual(len(capped_stored[2]), 400)
            self.assertEqual(
                capped_stored[3],
                hashlib.sha256(
                    f"{capped_stored[0]}|{capped_stored[1]}|{capped_stored[2]}".encode()
                ).hexdigest(),
            )

            exported = scratch / "export.jsonl"
            self.assertEqual(_run(scratch, "export-jsonl", "--out", str(exported)).returncode, 0)
            exported_bytes = exported.read_bytes()
            self.assertTrue(exported_bytes.endswith(b"\n"))
            self.assertEqual(exported_bytes, exported_bytes.replace(b"\r\n", b"\n"))

            destination = scratch / "destination"
            destination.mkdir()
            dest_env = _env(destination)
            init_dest = subprocess.run(
                [PYTHON, str(STORE), "init"], cwd=ROOT, env=dest_env,
                text=True, capture_output=True,
            )
            self.assertEqual(init_dest.returncode, 0, init_dest.stderr)
            imported = subprocess.run(
                [PYTHON, str(STORE), "ingest-jsonl", "--in", str(exported)],
                cwd=ROOT, env=dest_env, text=True, capture_output=True,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            round_trip = destination / "round-trip.jsonl"
            exported_again = subprocess.run(
                [PYTHON, str(STORE), "export-jsonl", "--out", str(round_trip)],
                cwd=ROOT, env=dest_env, text=True, capture_output=True,
            )
            self.assertEqual(exported_again.returncode, 0, exported_again.stderr)
            self.assertEqual(round_trip.read_bytes(), exported_bytes)
            self.assertNotEqual(env["ZMEM_STORE"], dest_env["ZMEM_STORE"])

            bad = scratch / "bad.jsonl"
            bad.write_bytes(exported_bytes + b'{"table":"evidence","id":"bad"}\n')
            dest_conn = sqlite3.connect(dest_env["ZMEM_STORE"])
            with dest_conn:
                before = dest_conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            dest_conn.close()
            rejected = subprocess.run(
                [PYTHON, str(STORE), "ingest-jsonl", "--in", str(bad)],
                cwd=ROOT, env=dest_env, text=True, capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            dest_conn = sqlite3.connect(dest_env["ZMEM_STORE"])
            with dest_conn:
                after = dest_conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            dest_conn.close()
            self.assertEqual(after, before)


class Issue183AcceptanceHostEvidenceTest(unittest.TestCase):
    def test_ac9_detached_fail_open_retention_cli_cadence_and_edit_data(self):
        """AC9: host writers and read/retention surfaces remain fail-open."""
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac9-") as td:
            scratch = Path(td)
            self.assertEqual(_run(scratch, "init").returncode, 0)
            rows = [
                _record("00000000-0000-4000-8000-000000000901", ts="2026-08-10T23:59:59Z", excerpt="old", kind="edit"),
                _record("00000000-0000-4000-8000-000000000902", ts="2026-08-11T00:00:00Z", excerpt="cutoff", kind="edit"),
                _record("00000000-0000-4000-8000-000000000903", ts="2026-09-10T00:00:00Z", excerpt="new", kind="edit"),
            ]
            for payload in rows:
                result = _run(scratch, "evidence", "write", input_text=json.dumps(payload))
                self.assertEqual(result.returncode, 0, result.stderr)

            # Exercise the real detached-process boundary: persistence must be
            # visible after the child exits, not only through an in-process
            # connection that happened to share transaction state.
            detached = _record(
                "00000000-0000-4000-8000-000000000908",
                ts="2026-09-10T00:00:02Z",
                excerpt="detached writer",
                kind="edit",
            )
            child = subprocess.Popen(
                [PYTHON, str(STORE), "evidence", "write"],
                cwd=ROOT,
                env=_env(scratch),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_stdout, child_stderr = child.communicate(
                json.dumps(detached), timeout=60
            )
            self.assertEqual(child.returncode, 0, child_stderr)
            self.assertTrue(child_stdout.strip())
            persisted = sqlite3.connect(_env(scratch)["ZMEM_STORE"])
            try:
                self.assertEqual(
                    persisted.execute(
                        "SELECT excerpt FROM evidence WHERE id=?",
                        (detached["id"],),
                    ).fetchone()[0],
                    "detached writer",
                )
            finally:
                persisted.close()

            listed = _run(
                scratch,
                "evidence",
                "list",
                "--namespace",
                "project:acceptance",
                "--session-id",
                "acceptance-183",
                "--json",
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listing = json.loads(listed.stdout)
            self.assertEqual(
                [item["id"] for item in listing],
                [row["id"] for row in rows] + [detached["id"]],
            )
            shown = _run(
                scratch,
                "evidence",
                "show",
                "--namespace",
                "project:acceptance",
                "--id",
                str(rows[-1]["id"]),
                "--json",
            )
            self.assertEqual(json.loads(shown.stdout)["excerpt"], "new")

            env = _env(scratch)
            env.update(ZMEM_EVIDENCE_DAYS="30", ZMEM_EVIDENCE_CAP="50000")
            with patch.dict(os.environ, env, clear=False), patch.object(
                sys,
                "path",
                [str(ROOT / "skills" / "memory" / "scripts"), *sys.path],
            ):
                import storelib.cli as cli

                class FrozenDateTime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        value = cls(2026, 9, 10, tzinfo=timezone.utc)
                        return value if tz is not None else value.replace(tzinfo=None)

                output = io.StringIO()
                with patch.object(cli, "datetime", FrozenDateTime), patch.object(
                    sys, "argv", [str(STORE), "session-cadence", "--json"]
                ), contextlib.redirect_stdout(output):
                    try:
                        cli.main()
                    except SystemExit as exc:
                        self.assertEqual(exc.code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(
                list(report),
                ["organized", "backed_up", "evidence_expired", "evidence_capped", "episode_links", "memory_links"],
            )
            self.assertEqual(report["evidence_expired"], 1)
            remaining = sqlite3.connect(env["ZMEM_STORE"])
            try:
                self.assertIsNone(
                    remaining.execute(
                        "SELECT 1 FROM evidence WHERE id=?", (rows[0]["id"],)
                    ).fetchone()
                )
            finally:
                remaining.close()

            launcher = ROOT / "hooks" / "zmem-launch.js"
            # Exercise the actual launcher detached-process boundary as well as
            # the direct store subprocess above.  The child must persist a row
            # after the short-lived Node parent exits.
            real_id = "00000000-0000-4000-8000-000000000909"
            real_env = _env(scratch)
            real_script = r'''
const launcher = require(process.argv[1]);
const env = { ...process.env, ZMEM_STORE: process.env.ZMEM_STORE,
  ZMEM_DATA: process.env.ZMEM_DATA, ZMEM_ROOT: process.cwd(),
  ZMEM_HOST: "claude", ZMEM_MODEL_AUTODOWNLOAD: "0" };
const accepted = launcher.recordEvidence(
  "claude", "convention-capture",
  {session_id:"acceptance-183", tool_name:"Bash",
   tool_input:{command:"git status"}},
  {session_id:"acceptance-183", evidence_id:"00000000-0000-4000-8000-000000000909"},
  env, () => "2026-09-10T00:00:03Z");
process.stdout.write(JSON.stringify({accepted}));
'''
            real_launch = subprocess.run(
                ["node", "-e", real_script, str(launcher)], cwd=ROOT,
                env=real_env, text=True, capture_output=True, timeout=60,
            )
            self.assertEqual(real_launch.returncode, 0, real_launch.stderr)
            self.assertEqual(json.loads(real_launch.stdout), {"accepted": True})
            deadline = time.monotonic() + 20
            real_row = None
            while time.monotonic() < deadline:
                probe = sqlite3.connect(real_env["ZMEM_STORE"])
                try:
                    real_row = probe.execute(
                        "SELECT excerpt FROM evidence WHERE id=?", (real_id,)
                    ).fetchone()
                finally:
                    probe.close()
                if real_row:
                    break
                time.sleep(0.1)
            self.assertEqual(real_row, ("git status",))

            script = r'''
const launcher = require(process.argv[1]);
const calls = [];
const children = [];
let spawnAttempted = false;
const fakeSpawn = (...args) => {
  const stdin = { data: "", on() { return this; }, write(value) { this.data += String(value); },
    end(value) { if (value !== undefined) this.data += String(value); } };
  const child = { stdin, on() { return this; }, unref() {} };
  calls.push(args);
  children.push(child);
  return child;
};
const env = { ZMEM_STORE: process.env.ZMEM_STORE, ZMEM_DATA: process.env.ZMEM_DATA,
  ZMEM_MODELS_DIR: process.env.ZMEM_MODELS_DIR, ZMEM_MODEL_AUTODOWNLOAD: "0",
  ZMEM_NAMESPACE: "project:acceptance", ZMEM_HOST: "claude", ZMEM_ROOT: process.cwd() };
launcher.recordEvidence("claude", "convention-capture",
  {session_id:"acceptance-183", tool_name:"Bash", tool_input:{command:"git status"}},
  {session_id:"acceptance-183", evidence_id:"00000000-0000-4000-8000-000000000904"},
  env, () => "2026-09-10T00:00:00Z", fakeSpawn);
process.stdout.write(JSON.stringify({calls, stdin: children[0].stdin.data}));
'''
            launched = subprocess.run(
                ["node", "-e", script, str(launcher)], cwd=ROOT,
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            launch_result = json.loads(launched.stdout)
            calls = launch_result["calls"]
            self.assertEqual(len(calls), 1)
            executable_name = Path(str(calls[0][0])).name.lower()
            expected_executables = {"python", "python.exe"} if os.name == "nt" else {"python3"}
            self.assertIn(executable_name, expected_executables)
            argv = calls[0][1]
            self.assertIn("evidence", argv)
            self.assertIn("write", argv)
            self.assertTrue(any(str(arg).endswith("store.py") for arg in argv))
            self.assertTrue(calls[0][2]["detached"])
            self.assertEqual(calls[0][2]["stdio"], ["pipe", "ignore", "ignore"])
            self.assertEqual(json.loads(launch_result["stdin"])["id"], "00000000-0000-4000-8000-000000000904")

            failing_script = script.replace(
                '''const children = [];
let spawnAttempted = false;
const fakeSpawn = (...args) => {
  const stdin = { data: "", on() { return this; }, write(value) { this.data += String(value); },
    end(value) { if (value !== undefined) this.data += String(value); } };
  const child = { stdin, on() { return this; }, unref() {} };
  calls.push(args);
  children.push(child);
  return child;
};''',
                '''const children = [];
let spawnAttempted = false;
const fakeSpawn = (...args) => { spawnAttempted = true; throw new Error("writer unavailable"); };''',
            ).replace(
                'process.stdout.write(JSON.stringify({calls, stdin: children[0].stdin.data}));',
                'process.stdout.write(JSON.stringify({spawnAttempted}));',
            )
            self.assertNotEqual(failing_script, script)
            self.assertIn("let spawnAttempted = false;", failing_script)
            self.assertIn("spawnAttempted = true", failing_script)
            self.assertIn('throw new Error("writer unavailable")', failing_script)
            self.assertNotIn("calls.push(args);", failing_script)
            failed_writer = subprocess.run(
                ["node", "-e", failing_script, str(launcher)], cwd=ROOT,
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(failed_writer.returncode, 0, failed_writer.stderr)
            self.assertEqual(json.loads(failed_writer.stdout), {"spawnAttempted": True})

            hermes_path = ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py"
            hermes_env = _env(scratch)
            with patch.dict(os.environ, hermes_env, clear=True):
                spec = importlib.util.spec_from_file_location("zmem_hermes_convention_183", hermes_path)
                self.assertIsNotNone(spec and spec.loader)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                popen_calls = []

                class FakeChild:
                    def __init__(self):
                        self.stdin = None
                        self.captured_stdin = b""
                        self.communicated_input = None
                        self.returncode = None

                    def communicate(self, input=None, timeout=None):
                        if input is not None:
                            # This fallback records communicate(input) only;
                            # it does not pretend a real PIPE exists. The
                            # production path supplies a file-backed stdin.
                            self.communicated_input = input
                        self.returncode = 0
                        return b"", b""

                    def wait(self, timeout=None):
                        self.returncode = 0
                        return self.returncode

                    def poll(self):
                        return self.returncode

                child = FakeChild()

                def fake_popen(args, **kwargs):
                    popen_calls.append((list(args), dict(kwargs)))
                    stdin = kwargs.get("stdin")
                    if stdin is not None and stdin is not module.subprocess.PIPE:
                        child.captured_stdin = stdin.read()
                    return child

                with patch.object(module.subprocess, "Popen", side_effect=fake_popen):
                    module._write_post_tool_evidence(
                        {"tool_name": "Bash", "args": {"command": "git status"},
                         "session_id": "acceptance-183", "task_id": "task-183",
                         "tool_call_id": "call-183", "result": "ok", "duration_ms": 1},
                        {"evidence_id": "00000000-0000-4000-8000-000000000905"},
                        clock=lambda: "2026-09-10T00:00:00Z",
                    )

                self.assertEqual(len(popen_calls), 1)
                command_args, command_kwargs = popen_calls[0]
                self.assertTrue(any(str(arg).endswith("store.py") for arg in command_args))
                self.assertIn("evidence", command_args)
                self.assertIn("write", command_args)
                self.assertTrue(
                    command_kwargs.get("start_new_session")
                    or command_kwargs.get("creationflags")
                )
                payload = json.loads(child.captured_stdin)
                self.assertEqual(payload["id"], "00000000-0000-4000-8000-000000000905")
                self.assertEqual(payload["session_id"], "acceptance-183")
                self.assertEqual(payload["lane"], "hermes-compat")

                init = subprocess.run(
                    [PYTHON, str(STORE), "init"], cwd=ROOT, env=hermes_env,
                    text=True, capture_output=True,
                )
                self.assertEqual(init.returncode, 0, init.stderr)
                written = subprocess.run(
                    [PYTHON, str(STORE), "evidence", "write"], cwd=ROOT,
                    env=hermes_env, input=json.dumps(payload), text=True,
                    capture_output=True,
                )
                self.assertEqual(written.returncode, 0, written.stderr)
                stored_conn = sqlite3.connect(hermes_env["ZMEM_STORE"])
                with stored_conn:
                    stored_hermes = stored_conn.execute(
                        "SELECT session_id, lane FROM evidence WHERE id=?",
                        (payload["id"],),
                    ).fetchone()
                stored_conn.close()
                self.assertEqual(stored_hermes, ("acceptance-183", "hermes-compat"))

                module._resolve_store_py = lambda: scratch / "missing-store.py"
                with patch.object(module.subprocess, "Popen", return_value=FakeChild()):
                    module._write_post_tool_evidence(
                        {"tool_name": "Bash", "args": {}, "session_id": "acceptance-183",
                         "task_id": "task-183", "tool_call_id": "failed", "result": "ok",
                         "duration_ms": 1},
                        {"evidence_id": "00000000-0000-4000-8000-000000000906"},
                        clock=lambda: "2026-09-10T00:00:00Z",
                    )
                missing_conn = sqlite3.connect(hermes_env["ZMEM_STORE"])
                with missing_conn:
                    missing_row = missing_conn.execute(
                        "SELECT 1 FROM evidence WHERE id=?",
                        ("00000000-0000-4000-8000-000000000906",),
                    ).fetchone()
                missing_conn.close()
                self.assertIsNone(missing_row)

                module._resolve_store_py = lambda: STORE
                launch_failure_calls = []

                def launch_failure_popen(*args, **kwargs):
                    launch_failure_calls.append((args, kwargs))
                    raise OSError("writer unavailable")

                with patch.object(module.subprocess, "Popen", side_effect=launch_failure_popen):
                    module._write_post_tool_evidence(
                        {"tool_name": "Bash", "args": {}, "session_id": "acceptance-183",
                         "task_id": "task-183", "tool_call_id": "launch-failed", "result": "ok",
                         "duration_ms": 1},
                        {"evidence_id": "00000000-0000-4000-8000-000000000907"},
                        clock=lambda: "2026-09-10T00:00:00Z",
                    )
                self.assertEqual(len(launch_failure_calls), 1)
                launch_failure_conn = sqlite3.connect(hermes_env["ZMEM_STORE"])
                with launch_failure_conn:
                    launch_failure_row = launch_failure_conn.execute(
                        "SELECT 1 FROM evidence WHERE id=?",
                        ("00000000-0000-4000-8000-000000000907",),
                    ).fetchone()
                launch_failure_conn.close()
                self.assertIsNone(launch_failure_row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
