"""Non-frozen Wave C host/CLI adversarial checks.

These tests exercise the host boundaries without touching an operator store or
the frozen acceptance files.  Query rewriting is intentionally out of scope.
"""

from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
LAUNCHER = ROOT / "hooks" / "zmem-launch.js"
GENERATOR = ROOT / "tests" / "fixtures" / "evidence" / "make_fixture.py"


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_MODELS_DIR",
        "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_NAMESPACE", "ZMEM_HOST",
        "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_QUERY_CONTEXT",
        "ZMEM_TEST_NOW", "ZMEM_EMBED_PROFILE",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(scratch / "store.sqlite"),
        "ZMEM_DATA": str(scratch / "data"),
        "ZMEM_HOME": str(ROOT),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_NAMESPACE": "project:wave-c",
        "ZMEM_HOST": "codex",
        "HOME": str(scratch / "home"),
        "USERPROFILE": str(scratch / "home"),
        "APPDATA": str(scratch / "appdata"),
        "LOCALAPPDATA": str(scratch / "localappdata"),
    })
    return env


class WaveCHostEdges(unittest.TestCase):
    def test_fixture_generator_requires_scratch_output(self) -> None:
        missing = subprocess.run(
            [sys.executable, str(GENERATOR)], capture_output=True, text=True,
        )
        self.assertEqual(missing.returncode, 2)
        committed = subprocess.run(
            [sys.executable, str(GENERATOR), "--out", str(GENERATOR.parent)],
            capture_output=True, text=True,
        )
        self.assertEqual(committed.returncode, 2)
        self.assertIn("scratch", committed.stderr)
        with tempfile.TemporaryDirectory(prefix="zmem-183-fixture-candidates-") as raw:
            out = Path(raw) / "fixtures"
            generated = subprocess.run(
                [sys.executable, str(GENERATOR), "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            for host in ("claude", "codex", "zcode"):
                self.assertEqual(
                    (out / f"candidate-expected-{host}.json").read_bytes(),
                    (GENERATOR.parent / f"expected-{host}.json").read_bytes(),
                )
            for lane in ("hermes-provider", "hermes-compat"):
                self.assertEqual(
                    (out / f"candidate-expected-{lane}.json").read_bytes(),
                    (GENERATOR.parent / f"expected-{lane}.json").read_bytes(),
                )
            self.assertEqual(
                (out / "hermes-post-tool.json").read_bytes(),
                (GENERATOR.parent / "hermes-post-tool.json").read_bytes(),
            )

    def test_launcher_drops_oversize_and_failed_events_and_extracts_patch(self) -> None:
        node_code = r"""
const launcher = require(process.argv[1]);
let data = "";
const child = {
  stdin: { write(value) { data += value; }, end() {}, on() {} },
  on() {}, unref() {}
};
const spawn = () => child;
function capture(hook, payload) {
  data = "";
  const ok = launcher.recordEvidence("codex", hook, payload, {},
    { ZMEM_ROOT: process.cwd() }, () => "2026-09-17T00:00:00Z", spawn);
  return { ok, row: data ? JSON.parse(data) : null };
}
const patch = ["*** Update File: src/actual.py", "@@"].join(String.fromCharCode(10));
process.stdout.write(JSON.stringify({
  edit: capture("convention-capture", {
    tool_name: "apply_patch", tool_input: { patch }, session_id: "s-wave-c"
  }),
  failed: capture("convention-capture", {
    tool_name: "Edit", tool_input: { file_path: "src/failed.py" },
    status: "error", error: "writer failed", session_id: "s-wave-c"
  }),
  oversize: capture("convention-capture", {
    tool_name: "Write", tool_input: { file_path: "src/large.py", content: "x".repeat(70000) },
    session_id: "s-wave-c"
  })
}));
"""
        result = subprocess.run(
            ["node", "-e", node_code, str(LAUNCHER)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["edit"]["ok"])
        self.assertEqual(output["edit"]["row"]["kind"], "edit")
        self.assertEqual(output["edit"]["row"]["ref_path"], "src/actual.py")
        self.assertFalse(output["failed"]["ok"])
        self.assertFalse(output["oversize"]["ok"])

    def test_cli_sanitizes_deep_duplicate_and_overflow_inputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-wave-c-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            init = subprocess.run(
                [sys.executable, str(STORE), "init"], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            cases = [
                (b'{"session_id":"secret-token","session_id":"x"}', "DuplicateJSONKeyError"),
                ((b"[" * 1200) + b"0" + (b"]" * 1200), "JSONDecodeError"),
            ]
            base = {
                "session_id": "s-wave-c", "lane": "codex", "moment": "pretool",
                "kind": "tool_call", "ts": "2026-09-17T00:00:00Z",
                "excerpt": "safe", "ref_path": "src/a.py", "ref_offset": 2**100,
            }
            cases.append((json.dumps(base).encode(), "ValueError"))
            for raw_input, marker in cases:
                result = subprocess.run(
                    [sys.executable, str(STORE), "evidence", "write"], env=env,
                    input=raw_input, capture_output=True,
                )
                self.assertEqual(result.returncode, 2)
                stderr = result.stderr.decode("utf-8", "replace")
                self.assertIn(marker, stderr)
                self.assertNotIn("secret-token", stderr)
            conn = sqlite3.connect(env["ZMEM_STORE"])
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)
            finally:
                conn.close()

    def test_cli_text_evidence_results_are_one_tabbed_physical_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-evidence-display-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            init = subprocess.run(
                [sys.executable, str(STORE), "init"], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            evidence_id = "00000000-0000-4000-8000-00000000d15a"
            payload = {
                "id": evidence_id,
                "session_id": "s-display",
                "lane": "codex",
                "moment": "pretool",
                "kind": "tool_call",
                "ts": "2026-09-17T00:00:00Z",
                "excerpt": "line one\nline two\ttoken=sk-test-1234567890",
                "ref_path": "src/\r\nactual.py",
                "ref_offset": None,
            }
            write = subprocess.run(
                [sys.executable, str(STORE), "evidence", "write"], env=env,
                input=json.dumps(payload), capture_output=True, text=True,
            )
            self.assertEqual(write.returncode, 0, write.stderr)
            listed = subprocess.run(
                [sys.executable, str(STORE), "evidence", "list", "--namespace", "project:wrong", "--session-id", "s-display"],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(len(listed.stdout.splitlines()), 1)
            self.assertIn(r"line one\nline two\t[REDACTED_SECRET]", listed.stdout)
            self.assertIn(r"src/\r\nactual.py", listed.stdout)
            shown = subprocess.run(
                [sys.executable, str(STORE), "evidence", "show", "--namespace", "project:wrong", "--id", evidence_id],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(len(shown.stdout.splitlines()), 1)

    def test_hermes_compat_is_silent_and_does_not_create_missing_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-hermes-empty-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            payload = {
                "session_id": "s-wave-c",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
                "extra": {"status": "ok", "tool": "Bash"},
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py")],
                env=env, input=json.dumps(payload), capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "{}")
            self.assertFalse(Path(env["ZMEM_STORE"]).exists())

    def test_native_provider_row_requires_contract_and_uses_injected_clock(self) -> None:
        agent = types.ModuleType("agent")
        provider_api = types.ModuleType("agent.memory_provider")
        provider_api.MemoryProvider = type("MemoryProvider", (), {})
        agent.memory_provider = provider_api
        with mock.patch.dict(sys.modules, {
            "agent": agent,
            "agent.memory_provider": provider_api,
        }):
            spec = importlib.util.spec_from_file_location(
                f"issue183_wave_c_native_{uuid.uuid4().hex}",
                ROOT / "hermes-plugin" / "__init__.py",
            )
            assert spec is not None and spec.loader is not None
            plugin = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            values = {
                "tool_name": "Bash",
                "args": {"command": "git status"},
                "result": "ok",
                "duration_ms": 4,
                "task_id": "task-wave-c",
                "tool_call_id": "call-wave-c",
                "session_id": "session-wave-c",
            }
            row = json.loads(plugin._native_evidence_row(
                values, clock=lambda: "2026-09-17T00:00:00Z"
            ))
            self.assertEqual(row["ts"], "2026-09-17T00:00:00Z")
            self.assertEqual(row["ref_path"], "hermes://task-wave-c/call-wave-c")
            self.assertIsNone(plugin._native_evidence_row(
                {key: value for key, value in values.items() if key != "duration_ms"}
            ))
            huge = dict(values)
            huge["args"] = {"blob": "x" * (64 * 1024 * 4)}
            self.assertIsNone(plugin._native_evidence_row(huge))
            deep = dict(values)
            nested: dict[str, object] = {}
            cursor = nested
            for _ in range(40):
                cursor["next"] = {}
                cursor = cursor["next"]  # type: ignore[assignment]
            deep["args"] = nested
            self.assertIsNone(plugin._native_evidence_row(deep))
            wide = dict(values)
            wide["args"] = {"items": ["x"] * 1000}
            self.assertIsNone(plugin._native_evidence_row(wide))
            with mock.patch.object(plugin, "_ensure_native_evidence_workers") as start:
                plugin._enqueue_native_evidence(huge)
                start.assert_not_called()
            values["task_id"] = "task/unsafe"
            self.assertIsNone(plugin._native_evidence_row(values))

    def test_hermes_compat_real_writer_records_edit_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-hermes-real-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            init = subprocess.run(
                [sys.executable, str(STORE), "init"], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            payload = {
                "tool_name": "write_file",
                "args": {"path": "src/compat.py", "content": "edited"},
                "session_id": "s-wave-c",
                "task_id": "task-wave-c",
                "tool_call_id": "call-wave-c",
                "result": "ok",
                "duration_ms": 3,
                "extra": {
                    "status": "ok",
                    "evidence_id": "00000000-0000-4000-8000-0000000009c1",
                },
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py")],
                env=env, input=json.dumps(payload), capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "{}")
            row = None
            for _ in range(50):
                conn = sqlite3.connect(env["ZMEM_STORE"])
                try:
                    row = conn.execute(
                        "SELECT kind, lane, ref_path FROM evidence WHERE id=?",
                        (payload["extra"]["evidence_id"],),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    break
                time.sleep(0.1)
            self.assertEqual(row, ("edit", "hermes-compat", "src/compat.py"))

    def test_hermes_compat_writer_uses_inherited_file_for_large_slow_child(self) -> None:
        """A child that does not read stdin cannot backpressure the hook."""
        with tempfile.TemporaryDirectory(prefix="zmem-183-hermes-slow-") as raw:
            scratch = Path(raw)
            helper = scratch / "slow_writer.py"
            marker = scratch / "slow_writer.ready"
            helper.write_text(
                f"from pathlib import Path\nimport time\n"
                f"Path({str(marker)!r}).write_text('ready', encoding='utf-8')\n"
                "time.sleep(1.0)\n", encoding="utf-8"
            )
            env = _env(scratch)
            env["ZMEM_PYTHON"] = sys.executable
            hermes_path = ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py"
            spec = importlib.util.spec_from_file_location(
                f"issue183_wave_c_compat_{uuid.uuid4().hex}", hermes_path
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, env, clear=True):
                spec.loader.exec_module(module)
                module._resolve_store_py = lambda: helper
                payload = {
                    "tool_name": "Bash",
                    "args": {"command": "git status", "blob": "x" * 50000},
                    "session_id": "s-wave-c-slow",
                    "task_id": "task-wave-c",
                    "tool_call_id": "call-wave-c-slow",
                    "result": "ok",
                    "duration_ms": 2,
                }
                real_popen = module.subprocess.Popen
                children = []

                def capture_popen(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child

                started = time.perf_counter()
                with mock.patch.object(module.subprocess, "Popen", side_effect=capture_popen):
                    self.assertTrue(module._write_post_tool_evidence(
                        payload, {"evidence_id": "00000000-0000-4000-8000-00000000d15b"},
                        clock=lambda: "2026-09-17T00:00:00Z",
                    ))
                elapsed = time.perf_counter() - started
                deadline = time.time() + 2.0
                while not marker.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "slow child did not start")
                self.assertEqual(len(children), 1)
                children[0].wait(timeout=3.0)
                self.assertEqual(children[0].poll(), 0)
            self.assertLess(elapsed, 0.75, f"writer blocked on child stdin for {elapsed:.3f}s")

    def test_hermes_compat_top_level_failure_wins_over_edit_shape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-hermes-failure-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            self.assertEqual(
                subprocess.run([sys.executable, str(STORE), "init"], env=env,
                               capture_output=True, text=True).returncode,
                0,
            )
            payload = {
                "tool_name": "write_file",
                "args": {"path": "src/failed.py", "content": "not written"},
                "session_id": "s-wave-c-failure",
                "task_id": "task-wave-c",
                "tool_call_id": "call-wave-c-failure",
                "result": "ok",
                "duration_ms": 3,
                "status": "error",
                "extra": {"evidence_id": "00000000-0000-4000-8000-00000000d15c"},
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py")],
                env=env, input=json.dumps(payload), capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            row = None
            for _ in range(50):
                conn = sqlite3.connect(env["ZMEM_STORE"])
                try:
                    row = conn.execute(
                        "SELECT kind, ref_path FROM evidence WHERE id=?",
                        (payload["extra"]["evidence_id"],),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    break
                time.sleep(0.1)
            self.assertEqual(row, ("tool_failure", "hermes://task-wave-c/call-wave-c-failure"))

    def test_hermes_convention_metadata_does_not_migrate_legacy_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zmem-183-hermes-legacy-") as raw:
            scratch = Path(raw)
            env = _env(scratch)
            init = subprocess.run(
                [sys.executable, str(STORE), "init"], env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            conn = sqlite3.connect(env["ZMEM_STORE"])
            try:
                conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
                conn.execute("DROP TABLE episode_evidence")
                conn.execute("DROP TABLE memory_evidence")
                conn.execute("DROP TABLE evidence")
                conn.commit()
            finally:
                conn.close()
            payload = {
                "session_id": "s-legacy",
                "tool_name": "Bash",
                "args": {"command": "git status"},
                "task_id": "task-legacy",
                "tool_call_id": "call-legacy",
                "result": "ok",
                "duration_ms": 1,
                "extra": {"status": "ok", "tool": "Bash", "command": "git status"},
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py")],
                env=env, input=json.dumps(payload), capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            conn = sqlite3.connect(env["ZMEM_STORE"])
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                    "13",
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence'"
                    ).fetchone()
                )
                legacy = conn.execute(
                    "SELECT value FROM meta WHERE key='hermes_convention_count_s-legacy'"
                ).fetchone()
                self.assertEqual(legacy, ("1",), conn.execute("SELECT key,value FROM meta").fetchall())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
