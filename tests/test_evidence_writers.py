"""Issue #170 evidence-writer, CLI, and fail-open boundary tests."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
LAUNCHER = ROOT / "hooks" / "zmem-launch.js"
COMPAT = ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py"
PLUGIN = ROOT / "hermes-plugin" / "__init__.py"
PYTHON = sys.executable


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        ZMEM_STORE=str(scratch / "store.sqlite"),
        ZMEM_DATA=str(scratch),
        ZMEM_MODELS_DIR=str(scratch / "missing-models"),
        ZMEM_MODEL_AUTODOWNLOAD="0",
        ZMEM_NAMESPACE="project:issue170",
        ZMEM_HOME=str(ROOT),
    )
    return env


def _run_store(
    env: dict[str, str], args: list[str], payload: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(STORE), *args],
        cwd=ROOT,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _init_store(env: dict[str, str]) -> None:
    result = _run_store(env, ["init"])
    if result.returncode:
        raise AssertionError(result.stderr)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _plugin_context(active: str):
    plugins = types.ModuleType("plugins")
    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: active
    plugins.memory = memory
    agent = types.ModuleType("agent")
    provider_api = types.ModuleType("agent.memory_provider")
    provider_api.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = provider_api
    modules = {
        "plugins": plugins,
        "plugins.memory": memory,
        "agent": agent,
        "agent.memory_provider": provider_api,
    }
    with mock.patch.dict(sys.modules, modules):
        module = _load_module(PLUGIN, f"issue170_plugin_{uuid.uuid4().hex}")
        yield module


class EvidenceWriterTest(unittest.TestCase):
    def test_launcher_writer_is_detached_and_fail_open(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-launcher-") as td:
            scratch = Path(td)
            env = _env(scratch)
            script = r'''
const launcher = require(process.argv[1]);
const calls = [];
function child() {
  return {stdin: {write() {}, end() {}}, on() { return this; }, unref() {}};
}
const accepted = launcher.recordEvidence(
  "codex", "convention-capture",
  {session_id: "issue170-writer", tool_name: "Bash", tool_input: {command: "git status"}},
  {session_id: "issue170-writer", evidence_id: "00000000-0000-4000-8000-000000001801"},
  {ZMEM_ROOT: process.cwd(), ZMEM_STORE: "scratch.sqlite", ZMEM_DATA: "scratch", ZMEM_NAMESPACE: "project:issue170"},
  () => "2026-09-10T00:00:00Z",
  (...args) => { calls.push(args); return child(); },
);
let failed = false;
try {
  failed = launcher.recordEvidence(
    "codex", "convention-capture",
    {session_id: "issue170-writer", tool_name: "Bash", tool_input: {command: "git status"}},
    {session_id: "issue170-writer", evidence_id: "00000000-0000-4000-8000-000000001802"},
    {ZMEM_ROOT: process.cwd()},
    () => "2026-09-10T00:00:00Z",
    () => { throw new Error("writer unavailable"); },
  );
} catch { failed = true; }
process.stdout.write(JSON.stringify({accepted, failed, calls}));
'''
            result = subprocess.run(
                ["node", "-e", script, str(LAUNCHER)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["accepted"])
            self.assertFalse(report["failed"])
            self.assertEqual(len(report["calls"]), 1)
            argv, options = report["calls"][0][1:]
            self.assertIn("evidence", argv)
            self.assertIn("write", argv)
            self.assertTrue(any(str(value).endswith("store.py") for value in argv))
            self.assertTrue(options["detached"])
            self.assertEqual(options["stdio"], ["pipe", "ignore", "ignore"])

    def test_launcher_writer_preserves_delivery(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-delivery-") as td:
            env = _env(Path(td))
            script = r'''
const launcher = require(process.argv[1]);
const payload = {session_id: "issue170-delivery", tool_name: "Edit", tool_input: {file_path: "README.md"}};
const meta = {session_id: "issue170-delivery", evidence_id: "00000000-0000-4000-8000-000000001803"};
const before = JSON.stringify({payload, meta});
const accepted = launcher.recordEvidence(
  "claude", "convention-capture",
  payload,
  meta,
  {ZMEM_ROOT: process.cwd()},
  () => "2026-09-10T00:00:00Z",
  () => { throw new Error("writer unavailable"); },
);
process.stdout.write(JSON.stringify({
  accepted,
  unchanged: JSON.stringify({payload, meta}) === before,
  payload,
  meta,
}));
'''
            result = subprocess.run(
                ["node", "-e", script, str(LAUNCHER)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["accepted"])
            self.assertTrue(report["unchanged"])
            self.assertEqual(report["payload"]["tool_name"], "Edit")
            self.assertEqual(report["meta"]["evidence_id"], "00000000-0000-4000-8000-000000001803")


class HermesWriterTest(unittest.TestCase):
    def test_hermes_compatibility_writer_uses_store_cli(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-hermes-compat-") as td:
            scratch = Path(td)
            env = _env(scratch)
            captured: list[tuple[list[str], dict[str, object], bytes]] = []

            class FakeChild:
                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    return None

            def fake_popen(args, **kwargs):
                stream = kwargs["stdin"]
                captured.append((list(args), dict(kwargs), stream.read()))
                return FakeChild()

            payload = {
                "tool_name": "Bash",
                "args": {"command": "git status"},
                "session_id": "issue170-hermes",
                "task_id": "task-170",
                "tool_call_id": "call-170",
                "result": {"status": "success"},
                "duration_ms": 10,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                module = _load_module(COMPAT, "issue170_compat")
                with mock.patch.object(module.subprocess, "Popen", side_effect=fake_popen):
                    self.assertTrue(module._write_post_tool_evidence(
                        payload,
                        {"evidence_id": "00000000-0000-4000-8000-000000001811"},
                        clock=lambda: "2026-09-10T00:00:00Z",
                    ))
            self.assertEqual(len(captured), 1)
            args, options, raw = captured[0]
            self.assertIn("evidence", args)
            self.assertIn("write", args)
            self.assertTrue(any(str(value).endswith("store.py") for value in args))
            if os.name == "nt":
                self.assertEqual(
                    options.get("creationflags"),
                    0x00000008 | 0x00000200,
                )
                self.assertNotIn("start_new_session", options)
            else:
                self.assertIs(options.get("start_new_session"), True)
                self.assertNotIn("creationflags", options)
            row = json.loads(raw)
            self.assertEqual(row["lane"], "hermes-compat")
            self.assertEqual(row["kind"], "tool_call")
            self.assertEqual(row["moment"], "pretool")

    def test_hermes_provider_writer_uses_run_store(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-hermes-provider-") as td:
            scratch = Path(td)
            calls: list[tuple[list[str], str | None]] = []
            completed = threading.Event()

            def fake_store(args, *, input_text=None, **kwargs):
                calls.append((list(args), input_text))
                completed.set()
                return {"ok": True, "returncode": 0, "stdout": "{}\n", "stderr": ""}

            with mock.patch.dict(os.environ, _env(scratch), clear=True):
                with _plugin_context("zmem") as plugin:
                    provider = plugin.ZmemMemoryProvider()
                    with mock.patch.object(plugin, "_run_store", side_effect=fake_store):
                        self.assertEqual(provider.post_tool_call(
                            tool_name="write_file",
                            args={"path": "src/actual.py", "content": "edited"},
                            session_id="issue170-provider",
                            task_id="task-170",
                            tool_call_id="call-170",
                            result="ok",
                            duration_ms=7,
                            status="ok",
                        ), {})
                        self.assertTrue(completed.wait(5), "provider writer did not run")
            self.assertTrue(calls)
            args, raw = calls[0]
            self.assertEqual(args, ["evidence", "write"])
            row = json.loads(raw)
            self.assertEqual(row["lane"], "hermes-provider")
            self.assertEqual(row["kind"], "edit")
            self.assertEqual(row["ref_path"], "src/actual.py")


class CliEvidenceTest(unittest.TestCase):
    def test_write_list_show_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-cli-") as td:
            scratch = Path(td)
            env = _env(scratch)
            _init_store(env)
            first = {
                "session_id": "issue170-cli",
                "lane": "codex",
                "moment": "user_prompt",
                "kind": "edit",
                "ts": "2026-09-10T00:00:02Z",
                "excerpt": "README.md",
                "ref_path": "README.md",
                "ref_offset": 12,
                "id": "00000000-0000-4000-8000-000000001821",
            }
            second = dict(first, kind="tool_call", moment="pretool",
                          ts="2026-09-10T00:00:01Z",
                          id="00000000-0000-4000-8000-000000001822")
            for payload in (first, second):
                written = _run_store(env, ["evidence", "write"], json.dumps(payload))
                self.assertEqual(written.returncode, 0, written.stderr)
                self.assertEqual(written.stdout, payload["id"] + "\n")
            listed = _run_store(env, [
                "evidence", "list", "--namespace", "project:issue170",
                "--session-id", "issue170-cli", "--json",
            ])
            self.assertEqual(listed.returncode, 0, listed.stderr)
            rows = json.loads(listed.stdout)
            self.assertEqual([row["id"] for row in rows], [second["id"], first["id"]])
            self.assertEqual(rows[1]["kind"], "edit")
            self.assertEqual(rows[1]["moment"], "user_prompt")
            self.assertEqual(list(rows[0]), [
                "id", "session_id", "lane", "moment", "kind", "ts",
                "excerpt", "ref_path", "ref_offset", "untrusted", "content_type",
            ])
            self.assertTrue(rows[0]["untrusted"])
            self.assertEqual(rows[0]["content_type"], "untrusted_evidence")
            shown = _run_store(env, [
                "evidence", "show", "--namespace", "project:issue170",
                "--id", first["id"], "--json",
            ])
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["ref_path"], "README.md")
            self.assertEqual(json.loads(shown.stdout)["kind"], "edit")
            self.assertEqual(json.loads(shown.stdout)["moment"], "user_prompt")
            self.assertNotIn("hash", shown.stdout)

    def test_show_missing_id_is_stderr_only(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-cli-missing-") as td:
            env = _env(Path(td))
            _init_store(env)
            result = _run_store(env, [
                "evidence", "show", "--namespace", "project:issue170",
                "--id", "00000000-0000-4000-8000-000000001823",
            ])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "evidence id not found\n")

    def test_writer_rejects_unknown_keys_and_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory(prefix="zmem-170-cli-invalid-") as td:
            scratch = Path(td)
            env = _env(scratch)
            _init_store(env)
            payload = {
                "session_id": "issue170-cli",
                "lane": "codex",
                "moment": "pretool",
                "kind": "tool_call",
                "ts": "2026-09-10T00:00:00Z",
                "excerpt": "safe",
                "ref_path": "x",
                "ref_offset": None,
            }
            unknown = _run_store(env, ["evidence", "write"], json.dumps(dict(payload, extra="nope")))
            self.assertEqual(unknown.returncode, 2)
            self.assertEqual(unknown.stdout, "")
            self.assertEqual(unknown.stderr, "evidence write failed: ValueError\n")
            duplicate = _run_store(
                env,
                ["evidence", "write"],
                '{"session_id":"issue170-cli","session_id":"duplicate"}',
            )
            self.assertEqual(duplicate.returncode, 2)
            self.assertEqual(duplicate.stdout, "")
            self.assertEqual(duplicate.stderr, "evidence write failed: DuplicateJSONKeyError\n")
            conn = sqlite3.connect(scratch / "store.sqlite")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
