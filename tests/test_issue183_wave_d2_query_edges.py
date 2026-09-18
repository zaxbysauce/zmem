"""Non-frozen Wave D2 boundary probes.

These tests exercise the real store subprocess where the contract is about
read-only/schema behavior, and use a narrow provider seam only for argv and
timeout/fail-open behavior.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
import io
import contextlib
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
PROVIDER = ROOT / "hermes-plugin" / "__init__.py"


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_HOME",
        "ZMEM_NAMESPACE", "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT",
        "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_HOST", "ZMEM_SESSION",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
        "ZMEM_HOME": str(ROOT),
        "ZMEM_NAMESPACE": "project:d2",
        "ZMEM_QUERY_CONTEXT": "1",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _run_store(tmp: Path, *args: str, **kwargs):
    return subprocess.run(
        [sys.executable, str(STORE), *args],
        env=_env(tmp, **kwargs.pop("env_extra", {})),
        capture_output=True,
        text=True,
        timeout=10,
        **kwargs,
    )


def _load_provider():
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = memory_provider
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", memory_provider)
    spec = importlib.util.spec_from_file_location("d2_provider_" + uuid.uuid4().hex, PROVIDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_compat_hook():
    path = ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-convention.py"
    spec = importlib.util.spec_from_file_location("d2_compat_" + uuid.uuid4().hex, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WaveD2QueryEdges(unittest.TestCase):
    def test_query_rewrite_real_writer_edit_and_exact_wire(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-d2-query-") as raw:
            tmp = Path(raw)
            self.assertEqual(_run_store(tmp, "init").returncode, 0)
            evidence = {
                "session_id": "d2-session", "lane": "claude",
                "moment": "user_prompt", "kind": "edit",
                "ts": "2026-09-17T12:00:00Z", "excerpt": "write",
                "ref_path": "C:/work/actual.py", "ref_offset": None,
            }
            write = _run_store(
                tmp, "evidence", "write",
                env_extra={"ZMEM_STORE": str(tmp / "store.sqlite")},
                input=json.dumps(evidence),
            )
            self.assertEqual(write.returncode, 0, write.stderr)
            result = _run_store(
                tmp, "query-rewrite", "--prompt", "continue",
                "--session-id", "d2-session", "--namespace", "project:d2",
                "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(list(payload), ["query", "rewrite"])
            self.assertIs(type(payload["query"]), str)
            self.assertIs(type(payload["rewrite"]), int)
            self.assertEqual(payload["rewrite"], 1)
            self.assertIn("actual.py", payload["query"])

    def test_query_rewrite_legacy_missing_evidence_is_read_only(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-d2-legacy-") as raw:
            tmp = Path(raw)
            self.assertEqual(_run_store(tmp, "init").returncode, 0)
            store = tmp / "store.sqlite"
            conn = sqlite3.connect(store)
            try:
                conn.execute("DROP TABLE evidence")
                conn.commit()
            finally:
                conn.close()
            before = hashlib.sha256(store.read_bytes()).hexdigest()
            result = _run_store(
                tmp, "query-rewrite", "--prompt=continue", "--session-id", "s",
                "--namespace", "project:d2", "--json",
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"query": "continue", "rewrite": 0})
            self.assertIn("unavailable", result.stderr)
            self.assertEqual(hashlib.sha256(store.read_bytes()).hexdigest(), before)
            conn = sqlite3.connect(store)
            try:
                names = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            self.assertNotIn("evidence", names)

    def test_provider_rewrite_uses_leading_dash_safe_prompt_and_one_recall(self):
        provider_mod = _load_provider()
        calls: list[list[str]] = []

        def fake_store(args, timing=None, input_text=None):
            calls.append(list(args))
            if args[0] == "query-rewrite":
                return {"ok": True, "stdout": '{"query":"--dry-run git","rewrite":1}\n'}
            return {"ok": True, "stdout": '{"rendered":"ok"}\n'}

        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "1", "ZMEM_INJECT": "1"}, clear=False):
            with mock.patch.object(provider_mod, "_run_store", fake_store):
                provider = provider_mod.ZmemMemoryProvider()
                provider._namespace = "project:d2"
                self.assertEqual(provider.prefetch("--dry-run", session_id="s"), "ok")
        self.assertEqual([args[0] for args in calls], ["query-rewrite", "recall"])
        self.assertEqual(calls[0][calls[0].index("--prompt=--dry-run")], "--prompt=--dry-run")
        recall = calls[1]
        self.assertIn("--query=--dry-run git", recall)

    def test_provider_rewrite_bridge_failure_is_fail_open(self):
        provider_mod = _load_provider()
        provider = provider_mod.ZmemMemoryProvider()
        provider._namespace = "project:d2"
        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "1", "ZMEM_INJECT": "1"}, clear=False):
            with mock.patch.object(provider_mod, "_run_store", side_effect=OSError("offline")):
                self.assertEqual(provider.prefetch("continue", session_id="s"), "")

    def test_provider_empty_user_prompt_uses_shared_rewrite_before_recent(self):
        provider_mod = _load_provider()
        calls: list[list[str]] = []

        def fake_store(args, timing=None, input_text=None):
            calls.append(list(args))
            if args[0] == "query-rewrite":
                return {"ok": True, "stdout": '{"query":"actual.py","rewrite":1}\n'}
            return {"ok": True, "stdout": '{"rendered":"ok"}\n'}

        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "1", "ZMEM_INJECT": "1"}, clear=False):
            with mock.patch.object(provider_mod, "_run_store", fake_store):
                provider = provider_mod.ZmemMemoryProvider()
                provider._namespace = "project:d2"
                self.assertEqual(provider.prefetch("", session_id="s"), "ok")
        self.assertEqual([args[0] for args in calls], ["query-rewrite", "recall"])

    def test_provider_classifies_full_prompt_before_500_char_output_cap(self):
        provider_mod = _load_provider()
        calls: list[list[str]] = []
        prefix = "please continue finalizing work " * 20
        prompt = prefix + " actual.py"
        self.assertGreater(len(prompt), 500)
        self.assertLessEqual(len(prompt), 4096)

        def prompt_arg(args):
            if "--prompt" in args:
                return args[args.index("--prompt") + 1]
            return next(item.split("=", 1)[1] for item in args
                        if item.startswith("--prompt="))

        def fake_store(args, timing=None, input_text=None):
            del timing, input_text
            calls.append(list(args))
            if args[0] == "query-rewrite":
                full_prompt = prompt_arg(args)
                # A real store rewrite would preserve this exact anchor and
                # therefore decline rewriting.  A pre-capped provider would
                # not see it and would take the rewrite branch instead.
                if "actual.py" in full_prompt:
                    return {
                        "ok": True,
                        "stdout": json.dumps({
                            "query": full_prompt[:500], "rewrite": 0,
                        }),
                    }
                return {
                    "ok": True,
                    "stdout": '{"query":"rewritten","rewrite":1}',
                }
            return {"ok": True, "stdout": '{"rendered":"ok"}'}

        with mock.patch.dict(
            os.environ, {"ZMEM_QUERY_CONTEXT": "1", "ZMEM_INJECT": "1"},
            clear=False,
        ), mock.patch.object(provider_mod, "_run_store", fake_store):
            provider = provider_mod.ZmemMemoryProvider()
            provider._namespace = "project:d2"
            self.assertEqual(provider.prefetch(prompt, session_id="s"), "ok")

        rewrite_args, recall_args = calls
        self.assertEqual(rewrite_args[0], "query-rewrite")
        self.assertEqual(prompt_arg(rewrite_args), prompt)
        self.assertEqual(
            recall_args[recall_args.index("--query") + 1], prompt.strip()[:500]
        )

    def test_provider_oversized_prompt_skips_rewrite_and_falls_back_bounded(self):
        provider_mod = _load_provider()
        calls: list[list[str]] = []

        def unexpected_store(args, timing=None, input_text=None):
            del timing, input_text
            calls.append(list(args))
            raise AssertionError("oversized prompt must not launch rewrite")

        prompt = "continue " + ("ambiguous work " * 300)
        self.assertGreater(len(prompt), 4096)
        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "1"}, clear=False), \
                mock.patch.object(provider_mod, "_run_store", unexpected_store):
            result = provider_mod._rewrite_provider_query(
                prompt, namespace="project:d2", session_id="s"
            )
        self.assertEqual(result, (prompt.strip()[:500], False))
        self.assertEqual(calls, [])

    def test_provider_valid_long_ambiguous_rewrite_stays_bounded(self):
        provider_mod = _load_provider()
        calls: list[list[str]] = []
        prompt = "please continue finalizing work " * 60
        self.assertLess(len(prompt), 4096)

        def fake_store(args, timing=None, input_text=None):
            del timing, input_text
            calls.append(list(args))
            return {
                "ok": True,
                "stdout": json.dumps({"query": "x" * 500, "rewrite": 1}),
            }

        with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "1"}, clear=False), \
                mock.patch.object(provider_mod, "_run_store", fake_store):
            result = provider_mod._rewrite_provider_query(
                prompt, namespace="project:d2", session_id="s"
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, ("x" * 500, True))

    def test_query_rewrite_command_timeout_is_one_second(self):
        provider_mod = _load_provider()
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        with mock.patch.object(provider_mod, "_resolve_store_py", return_value=STORE):
            with mock.patch.object(provider_mod.subprocess, "run", side_effect=fake_run):
                result = provider_mod._run_store(["query-rewrite", "--json"])
        self.assertEqual(result["returncode"], 124)
        self.assertEqual(calls, [1.0])

    def test_native_scalar_and_conflicting_statuses_fail_closed(self):
        provider_mod = _load_provider()
        base = {
            "tool_name": "write_file", "session_id": "s", "task_id": "t",
            "tool_call_id": "c", "args": {"path": "actual.py"},
            "result": {"status": "ok"}, "duration_ms": 1,
        }
        self.assertIsNotNone(provider_mod._native_evidence_row(base))
        for field in ("tool_name", "session_id", "task_id", "tool_call_id", "status"):
            huge = dict(base, **{field: "x" * 100_000})
            self.assertIsNone(
                provider_mod._native_evidence_row(huge), field
            )
        conflict = dict(base, status="error")
        row = provider_mod._native_evidence_row(conflict)
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row)["kind"], "tool_failure")

    def test_compat_top_level_failure_status_wins_nested_success(self):
        compat = _load_compat_hook()
        captured = {}

        class _Child:
            def wait(self, timeout=None):
                return 0

        def fake_popen(command, **kwargs):
            captured["row"] = json.loads(kwargs["stdin"].read().decode("utf-8"))
            return _Child()

        payload = {
            "tool_name": "write_file", "args": {"path": "actual.py"},
            "session_id": "s", "task_id": "t", "tool_call_id": "c",
            "result": {"status": "ok"}, "duration_ms": 1,
        }
        with mock.patch.object(compat, "_resolve_store_py", return_value=STORE):
            with mock.patch.object(compat.subprocess, "Popen", fake_popen):
                self.assertTrue(compat._write_post_tool_evidence(
                    payload, {"status": "error"}, clock=lambda: "2026-09-17T00:00:00Z"
                ))
        self.assertEqual(captured["row"]["kind"], "tool_failure")
        self.assertEqual(captured["row"]["ref_path"], "hermes://t/c")

    def test_internal_compat_status_conflict_records_failure_not_convention(self):
        if str(ROOT / "skills" / "memory" / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
        from storelib import cli

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            payload = {
                "session_id": "s", "status": "error",
                "result": {"status": "ok"},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_hermes_convention(conn, payload=payload), 0)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM meta WHERE key='hermes_pending_failure_s'"
            ).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM meta WHERE key='hermes_convention_count_s'"
            ).fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
