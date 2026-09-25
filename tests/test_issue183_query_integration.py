"""Independent issue #183 query-boundary integration checks.

These probes use the real store subprocess for filesystem/WAL/CLI contracts and
small adapter seams only where a host callback owns the subprocess invocation.
They never use the operator store or model cache.
"""

from __future__ import annotations

import contextlib
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
BODY = ROOT / "hooks" / "lib" / "zmem-recall-body.py"
PROVIDER = ROOT / "hermes-plugin" / "__init__.py"


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("ZMEM_") or key.startswith("CLAUDE_PLUGIN_") \
                or key.startswith("ZCODE_PLUGIN_"):
            env.pop(key, None)
    for key in (
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
        "ZMEM_HOME": str(ROOT),
        "ZMEM_NAMESPACE": "project:integration",
        "ZMEM_HOST": "claude",
        "ZMEM_QUERY_CONTEXT": "1",
        "ZMEM_INJECT": "1",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "PYTHONUTF8": "1",
        "HOME": str(tmp / "home"),
        "USERPROFILE": str(tmp / "home"),
        "APPDATA": str(tmp / "appdata"),
        "LOCALAPPDATA": str(tmp / "localappdata"),
        "XDG_DATA_HOME": str(tmp / "xdg-data"),
        "XDG_CONFIG_HOME": str(tmp / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp / "xdg-cache"),
    })
    env.update(extra)
    return env


def _run_store(tmp: Path, *args: str, input_text: str | None = None,
               **env_extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STORE), *args],
        env=_env(tmp, **env_extra),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _load(path: Path, prefix: str):
    module_name = prefix + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _load_provider():
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = memory_provider
    with mock.patch.dict(
        sys.modules,
        {"agent": agent, "agent.memory_provider": memory_provider},
    ):
        return _load(PROVIDER, "issue183_integration_provider_")


def _load_mcp_server():
    module_name = "issue183_integration_mcp_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "hermes-plugin" / "server" / "mcp_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, module_name


def _load_reflect_hook():
    return _load(
        ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py",
        "issue183_integration_reflect_",
    )


def _init_store(tmp: Path) -> Path:
    result = _run_store(tmp, "init")
    if result.returncode:
        raise AssertionError(result.stderr)
    return tmp / "store.sqlite"


def _schema_snapshot(conn: sqlite3.Connection) -> tuple[tuple, ...]:
    return tuple(conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "ORDER BY type, name"
    ).fetchall())


def _insert_edit(conn: sqlite3.Connection, evidence_id: str, ts: str,
                 ref_path: str, session_id: str = "integration-session") -> None:
    digest = hashlib.sha256(f"edit|{ts}|edit".encode()).hexdigest()
    conn.execute(
        "INSERT INTO evidence "
        "(id, session_id, lane, moment, kind, ts, hash, excerpt, ref_path, ref_offset) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence_id, session_id, "claude", "user_prompt", "edit", ts,
         digest, "edit", ref_path, None),
    )


class QueryRewriteStoreIntegrationTest(unittest.TestCase):
    def test_live_wal_reader_sees_only_committed_latest_edit_and_is_read_only(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-query-wal-") as raw:
            tmp = Path(raw)
            store = _init_store(tmp)
            writer = sqlite3.connect(store, timeout=1.0)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.commit()
                _insert_edit(writer, "00000000-0000-4000-8000-000000000001",
                             "2026-09-17T12:00:01Z", "C:/work/first.py")
                writer.commit()
                before = hashlib.sha256(store.read_bytes()).hexdigest()
                schema_before = _schema_snapshot(writer)
                version_before = writer.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]

                writer.execute("BEGIN IMMEDIATE")
                _insert_edit(writer, "00000000-0000-4000-8000-000000000002",
                             "2026-09-17T12:00:02Z", "C:/work/uncommitted.py")
                first = _run_store(
                    tmp, "query-rewrite", "--prompt", "continue", "--session-id",
                    "integration-session", "--namespace", "project:integration", "--json",
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                first_payload = json.loads(first.stdout)
                self.assertEqual(first_payload["rewrite"], 1)
                self.assertIn("first.py", first_payload["query"])
                self.assertNotIn("uncommitted.py", first_payload["query"])

                writer.commit()
                writer.execute("BEGIN IMMEDIATE")
                second = _run_store(
                    tmp, "query-rewrite", "--prompt", "continue", "--session-id",
                    "integration-session", "--namespace", "project:integration", "--json",
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                second_payload = json.loads(second.stdout)
                self.assertEqual(second_payload["rewrite"], 1)
                self.assertIn("uncommitted.py", second_payload["query"])
                self.assertEqual(
                    hashlib.sha256(store.read_bytes()).hexdigest(), before
                )
                self.assertEqual(_schema_snapshot(writer), schema_before)
                self.assertEqual(
                    writer.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    version_before,
                )
            finally:
                writer.rollback()
                writer.close()

    def test_missing_store_query_rewrite_does_not_create_parents(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-query-missing-") as raw:
            tmp = Path(raw)
            missing = tmp / "not-created" / "store.sqlite"
            result = _run_store(
                tmp, "query-rewrite", "--prompt=continue", "--session-id", "s",
                "--namespace", "project:integration", "--json",
                ZMEM_STORE=str(missing),
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"query": "continue", "rewrite": 0})
            self.assertIn("query-rewrite unavailable", result.stderr)
            self.assertFalse(missing.exists())
            self.assertFalse(missing.parent.exists())

    def test_malformed_legacy_evidence_fails_open_with_one_warning(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-query-legacy-") as raw:
            tmp = Path(raw)
            store = _init_store(tmp)
            conn = sqlite3.connect(store)
            try:
                conn.execute("DROP TABLE evidence")
                conn.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()
            before = hashlib.sha256(store.read_bytes()).hexdigest()
            result = _run_store(
                tmp, "query-rewrite", "--prompt=continue", "--session-id", "s",
                "--namespace", "project:integration", "--json",
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"query": "continue", "rewrite": 0})
            self.assertEqual(result.stderr.count("query-rewrite unavailable"), 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(hashlib.sha256(store.read_bytes()).hexdigest(), before)

    def test_query_context_switch_is_exact_zero_only(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-query-switch-") as raw:
            tmp = Path(raw)
            store = _init_store(tmp)
            conn = sqlite3.connect(store)
            try:
                _insert_edit(conn, "00000000-0000-4000-8000-000000000003",
                             "2026-09-17T12:00:03Z", "C:/work/switch.py")
                conn.commit()
            finally:
                conn.close()
            for value, expected in (("0", 0), (" 0 ", 0), ("false", 1), ("00", 1)):
                with self.subTest(value=value):
                    result = _run_store(
                        tmp, "query-rewrite", "--prompt=continue", "--session-id",
                        "integration-session", "--namespace", "project:integration", "--json",
                        ZMEM_QUERY_CONTEXT=value,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["rewrite"], expected)
                    if expected:
                        self.assertIn("switch.py", payload["query"])


class QueryRewriteSurfaceIntegrationTest(unittest.TestCase):
    def test_real_provider_and_mcp_prefetch_share_one_rewrite_boundary(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-query-surfaces-") as raw:
            tmp = Path(raw)
            env = _env(tmp, ZMEM_MCP_TOKEN="integration-test-token")
            with mock.patch.dict(os.environ, env, clear=True):
                _init_store(tmp)
                added = _run_store(
                    tmp, "add", "--namespace", "project:integration",
                    "--type", "fact", "--content", "provider_py sentinel-provider",
                    "--signal", "test", "--confidence", "0.9", "--json",
                )
                self.assertEqual(added.returncode, 0, added.stderr)
                added_mcp = _run_store(
                    tmp, "add", "--namespace", "project:integration",
                    "--type", "fact", "--content", "mcp_py sentinel-mcp",
                    "--signal", "test", "--confidence", "0.9", "--json",
                )
                self.assertEqual(added_mcp.returncode, 0, added_mcp.stderr)
                conn = sqlite3.connect(tmp / "store.sqlite")
                try:
                    _insert_edit(
                        conn, "00000000-0000-4000-8000-000000000010",
                        "2026-09-17T12:01:00Z", "C:/work/provider_py",
                        "provider-s",
                    )
                    _insert_edit(
                        conn, "00000000-0000-4000-8000-000000000011",
                        "2026-09-17T12:01:01Z", "C:/work/mcp_py",
                        "mcp-s",
                    )
                    conn.commit()
                finally:
                    conn.close()

                provider = _load_provider()
                instance = provider.ZmemMemoryProvider()
                instance._namespace = "project:integration"
                provider_calls: list[list[str]] = []
                provider_results: list[dict] = []
                provider_run = provider._run_store

                def tracked_provider(args, timing=None, input_text=None):
                    provider_calls.append(list(args))
                    result = provider_run(args, timing=timing, input_text=input_text)
                    provider_results.append(result)
                    return result

                with mock.patch.object(provider, "_run_store", tracked_provider):
                    rendered = instance.prefetch("the", session_id="provider-s")
                self.assertIn("sentinel-provider", rendered, (provider_calls, provider_results))
                self.assertEqual(
                    [args[0] for args in provider_calls],
                    ["query-rewrite", "recall"],
                )
                self.assertIn("the provider_py", provider_calls[1])

                mcp, module_name = _load_mcp_server()
                try:
                    server = mcp.build_server(
                        host="127.0.0.1", port=0, use_tls=False
                    )
                    mcp_calls: list[list[str]] = []
                    mcp_results: list[dict] = []
                    mcp_run = mcp._run_store_async

                    async def tracked_mcp(args, *positional, **keyword):
                        mcp_calls.append(list(args))
                        result = await mcp_run(args, *positional, **keyword)
                        mcp_results.append(result)
                        return result

                    with mock.patch.object(mcp, "_run_store_async", tracked_mcp):
                        result = asyncio.run(server._tool_manager.call_tool(
                            "prefetch",
                            {
                                "query": "the",
                                "namespace": "project:integration",
                                "session_id": "mcp-s",
                                "moment": "user_prompt",
                                "lane": "hermes-compat",
                            },
                            context=None,
                        ))
                    self.assertNotIn("error", result, result)
                    self.assertIn("sentinel-mcp", result["rendered"], (result, mcp_results))
                    self.assertEqual(len(mcp_calls), 1)
                    self.assertEqual(mcp_calls[0][0], "prefetch")
                    self.assertIn("the", mcp_calls[0])
                finally:
                    sys.modules.pop(module_name, None)

    def test_mcp_query_context_zero_keeps_raw_query_and_hides_discriminating_memory(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-mcp-context-off-") as raw:
            tmp = Path(raw)
            env = _env(
                tmp,
                ZMEM_QUERY_CONTEXT="0",
                ZMEM_MCP_TOKEN="integration-test-token",
            )
            with mock.patch.dict(os.environ, env, clear=True):
                _init_store(tmp)
                added = _run_store(
                    tmp, "add", "--namespace", "project:integration",
                    "--type", "fact", "--content", "mcp_off_py sentinel-mcp-off",
                    "--signal", "test", "--confidence", "0.9", "--json",
                )
                self.assertEqual(added.returncode, 0, added.stderr)
                conn = sqlite3.connect(tmp / "store.sqlite")
                try:
                    _insert_edit(
                        conn, "00000000-0000-4000-8000-000000000023",
                        "2026-09-17T12:04:00Z", "C:/work/mcp_off_py",
                        "mcp-off-s",
                    )
                    conn.commit()
                finally:
                    conn.close()

                mcp, module_name = _load_mcp_server()
                try:
                    server = mcp.build_server(
                        host="127.0.0.1", port=0, use_tls=False
                    )
                    calls: list[list[str]] = []
                    results: list[dict] = []
                    mcp_run = mcp._run_store_async

                    async def tracked_mcp(args, *positional, **keyword):
                        calls.append(list(args))
                        result = await mcp_run(args, *positional, **keyword)
                        results.append(result)
                        return result

                    with mock.patch.object(mcp, "_run_store_async", tracked_mcp):
                        result = asyncio.run(server._tool_manager.call_tool(
                            "prefetch",
                            {
                                "query": "the",
                                "namespace": "project:integration",
                                "session_id": "mcp-off-s",
                                "moment": "user_prompt",
                                "lane": "hermes-compat",
                            },
                            context=None,
                        ))
                    self.assertNotIn("error", result, result)
                    self.assertNotIn("sentinel-mcp-off", result["rendered"],
                                     (result, results))
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][0], "prefetch")
                    self.assertIn("the", calls[0])
                finally:
                    sys.modules.pop(module_name, None)

    def test_query_context_zero_keeps_raw_query_and_enabled_context_delivers(self):
        def run_provider_case(context_value: str, evidence_id: str,
                              session_id: str, basename: str,
                              sentinel: str):
            with tempfile.TemporaryDirectory(prefix="zmem-183-query-switch-") as raw:
                tmp = Path(raw)
                env = _env(tmp, ZMEM_QUERY_CONTEXT=context_value)
                with mock.patch.dict(os.environ, env, clear=True):
                    _init_store(tmp)
                    added = _run_store(
                        tmp, "add", "--namespace", "project:integration",
                        "--type", "fact", "--content", f"{basename} {sentinel}",
                        "--signal", "test", "--confidence", "0.9", "--json",
                    )
                    self.assertEqual(added.returncode, 0, added.stderr)
                    conn = sqlite3.connect(tmp / "store.sqlite")
                    try:
                        _insert_edit(
                            conn, evidence_id, "2026-09-17T12:02:00Z",
                            f"C:/work/{basename}", session_id,
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    provider = _load_provider()
                    instance = provider.ZmemMemoryProvider()
                    instance._namespace = "project:integration"
                    calls: list[list[str]] = []
                    provider_run = provider._run_store

                    def tracked(args, timing=None, input_text=None):
                        calls.append(list(args))
                        return provider_run(args, timing=timing, input_text=input_text)

                    with mock.patch.object(provider, "_run_store", tracked):
                        rendered = instance.prefetch("the", session_id=session_id)
                    return rendered, calls

        disabled_rendered, disabled_calls = run_provider_case(
            "0", "00000000-0000-4000-8000-000000000020", "disabled-s",
            "disabled_py", "sentinel-disabled",
        )
        self.assertNotIn("sentinel-disabled", disabled_rendered)
        self.assertEqual([args[0] for args in disabled_calls], ["recall"])
        self.assertIn("the", disabled_calls[0])

        enabled_rendered, enabled_calls = run_provider_case(
            "1", "00000000-0000-4000-8000-000000000021", "enabled-s",
            "enabled_py", "sentinel-enabled",
        )
        self.assertIn("sentinel-enabled", enabled_rendered)
        self.assertEqual(
            [args[0] for args in enabled_calls],
            ["query-rewrite", "recall"],
        )
        self.assertIn("the enabled_py", enabled_calls[1])

    def test_hermes_reflect_entrypoint_prefetches_once_and_kill_switches(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-reflect-") as raw:
            tmp = Path(raw)
            env = _env(tmp)
            with mock.patch.dict(os.environ, env, clear=True):
                _init_store(tmp)
                added = _run_store(
                    tmp, "add", "--namespace", "project:integration",
                    "--type", "fact", "--content", "reflect_py sentinel-reflect",
                    "--signal", "test", "--confidence", "0.9", "--json",
                )
                self.assertEqual(added.returncode, 0, added.stderr)
                conn = sqlite3.connect(tmp / "store.sqlite")
                try:
                    _insert_edit(
                        conn, "00000000-0000-4000-8000-000000000022",
                        "2026-09-17T12:03:00Z", "C:/work/reflect_py",
                        "reflect-s",
                    )
                    conn.commit()
                finally:
                    conn.close()

                reflect = _load_reflect_hook()
                real_run = subprocess.run
                calls: list[list[str]] = []

                def forwarding_run(command, *args, **kwargs):
                    calls.append(list(command))
                    return real_run(command, *args, **kwargs)

                output = io.StringIO()
                with mock.patch.object(
                    reflect.subprocess, "run", side_effect=forwarding_run
                ), mock.patch.object(
                    reflect.sys, "stdin",
                    io.StringIO(json.dumps({
                        "user_message": "the", "session_id": "reflect-s",
                    })),
                ), contextlib.redirect_stdout(output):
                    self.assertEqual(reflect.main(), 0)
                payload = json.loads(output.getvalue())
                self.assertIn("sentinel-reflect", payload.get("context", ""))
                prefetch_calls = [
                    command for command in calls if "prefetch" in command
                ]
                self.assertEqual(len(prefetch_calls), 1, calls)
                self.assertIn("--query", prefetch_calls[0])
                self.assertEqual(
                    prefetch_calls[0][prefetch_calls[0].index("--query") + 1],
                    "the",
                )
                self.assertIn("--lane", prefetch_calls[0])
                self.assertEqual(
                    prefetch_calls[0][prefetch_calls[0].index("--lane") + 1],
                    "hermes-compat",
                )

            with tempfile.TemporaryDirectory(prefix="zmem-183-reflect-context-off-") as off_raw:
                off = Path(off_raw)
                context_off_env = _env(off, ZMEM_QUERY_CONTEXT="0")
                with mock.patch.dict(os.environ, context_off_env, clear=True):
                    _init_store(off)
                    added_off = _run_store(
                        off, "add", "--namespace", "project:integration",
                        "--type", "fact",
                        "--content", "reflect_off_py sentinel-reflect-off",
                        "--signal", "test", "--confidence", "0.9", "--json",
                    )
                    self.assertEqual(added_off.returncode, 0, added_off.stderr)
                    conn = sqlite3.connect(off / "store.sqlite")
                    try:
                        _insert_edit(
                            conn, "00000000-0000-4000-8000-000000000024",
                            "2026-09-17T12:05:00Z", "C:/work/reflect_off_py",
                            "reflect-context-off-s",
                        )
                        conn.commit()
                    finally:
                        conn.close()

                    reflect_context_off = _load_reflect_hook()
                    context_off_calls: list[list[str]] = []

                    def forwarding_context_off(command, *args, **kwargs):
                        context_off_calls.append(list(command))
                        return real_run(command, *args, **kwargs)

                    context_off_output = io.StringIO()
                    with mock.patch.object(
                        reflect_context_off.subprocess,
                        "run",
                        side_effect=forwarding_context_off,
                    ), mock.patch.object(
                        reflect_context_off.sys,
                        "stdin",
                        io.StringIO(json.dumps({
                            "user_message": "the",
                            "session_id": "reflect-context-off-s",
                        })),
                    ), contextlib.redirect_stdout(context_off_output):
                        self.assertEqual(reflect_context_off.main(), 0)
                    context_off_payload = json.loads(context_off_output.getvalue())
                    self.assertNotIn(
                        "sentinel-reflect-off",
                        context_off_payload.get("context", ""),
                    )
                    context_off_prefetch = [
                        command for command in context_off_calls if "prefetch" in command
                    ]
                    self.assertEqual(len(context_off_prefetch), 1, context_off_calls)
                    self.assertIn("--query", context_off_prefetch[0])
                    self.assertEqual(
                        context_off_prefetch[0][
                            context_off_prefetch[0].index("--query") + 1
                        ],
                        "the",
                    )

            with tempfile.TemporaryDirectory(prefix="zmem-183-reflect-off-") as off_raw:
                off = Path(off_raw)
                disabled_env = _env(off, ZMEM_INJECT="0")
                with mock.patch.dict(os.environ, disabled_env, clear=True):
                    _init_store(off)
                    reflect_off = _load_reflect_hook()
                    off_calls: list[list[str]] = []

                    def forwarding_off(command, *args, **kwargs):
                        off_calls.append(list(command))
                        return real_run(command, *args, **kwargs)

                    off_output = io.StringIO()
                    with mock.patch.object(
                        reflect_off.subprocess, "run", side_effect=forwarding_off
                    ), mock.patch.object(
                        reflect_off.sys, "stdin",
                        io.StringIO(json.dumps({
                            "user_message": "the", "session_id": "reflect-off",
                        })),
                    ), contextlib.redirect_stdout(off_output):
                        self.assertEqual(reflect_off.main(), 0)
                    self.assertEqual(json.loads(off_output.getvalue()), {})
                    self.assertFalse(
                        any("prefetch" in command for command in off_calls),
                        off_calls,
                    )

    def test_user_prompt_provider_rewrites_once_and_cli_negative_surfaces_stay_closed(self):
        provider = _load_provider()
        calls: list[list[str]] = []

        def fake_provider_store(args, timing=None, input_text=None):
            del timing, input_text
            calls.append(list(args))
            if args[0] == "query-rewrite":
                return {
                    "ok": True,
                    "stdout": '{"query":"continue edit.py","rewrite":1}',
                    "stderr": "",
                    "returncode": 0,
                }
            return {
                "ok": True,
                "stdout": '{"rendered":"selected"}',
                "stderr": "",
                "returncode": 0,
            }

        with mock.patch.dict(os.environ, {
            "ZMEM_INJECT": "1", "ZMEM_QUERY_CONTEXT": "1",
        }, clear=False), mock.patch.object(
            provider, "_run_store", fake_provider_store
        ):
            instance = provider.ZmemMemoryProvider()
            instance._namespace = "project:integration"
            self.assertEqual(instance.prefetch("continue", session_id="s"), "selected")
        self.assertEqual([args[0] for args in calls], ["query-rewrite", "recall"])
        self.assertEqual(
            calls[1][calls[1].index("--query") + 1], "continue edit.py"
        )

        with tempfile.TemporaryDirectory(prefix="zmem-183-query-closed-") as raw:
            tmp = Path(raw)
            _init_store(tmp)
            pretool = _run_store(
                tmp, "prefetch", "--query", "continue", "--namespace",
                "project:integration", "--session-id", "s", "--moment", "pretool",
                "--lane", "claude", "--for-injection", "--no-bump", "--json",
            )
            search = _run_store(
                tmp, "search", "--text", "continue", "--namespace",
                "project:integration", "--json", ZMEM_QUERY_CONTEXT="1",
            )
            self.assertEqual(pretool.returncode, 0, pretool.stderr)
            self.assertEqual(search.returncode, 0, search.stderr)
            pretool_payload = json.loads(pretool.stdout)
            search_payload = json.loads(search.stdout)
            self.assertIn("rendered", pretool_payload)
            self.assertNotIn("query", pretool_payload)
            self.assertNotIn("rewrite", pretool_payload)
            self.assertEqual(
                set(search_payload),
                {"results", "count", "omitted", "injection_risk",
                 "tokens_used", "tokens_budget"},
            )

    def test_empty_query_rewrite_context_is_shared_across_surfaces(self):
        body = _load(BODY, "issue183_integration_body_")
        calls: list[list[str]] = []

        def fake_body_store(_store, args, timeout=None):
            del timeout
            calls.append(list(args))
            if args[0] == "query-rewrite":
                return body.subprocess.CompletedProcess(
                    args, 0, '{"query":"empty.py","rewrite":1}', ""
                )
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":""}', ""
            )

        old_argv = sys.argv[:]
        try:
            with tempfile.TemporaryDirectory(prefix="zmem-183-hook-log-") as hook_raw:
                with mock.patch.dict(os.environ, {
                    "ZMEM_STORE": str(Path(hook_raw) / "store.sqlite"),
                    "ZMEM_DATA": str(Path(hook_raw) / "data"), "ZMEM_INJECT": "1",
                    "ZMEM_QUERY_CONTEXT": "1", "ZMEM_SESSION": "s", "ZMEM_HOST": "claude",
                }, clear=False), mock.patch.object(body, "_run_store", fake_body_store), \
                        mock.patch.object(body.os.path, "isfile", return_value=True), \
                        mock.patch.object(sys, "stdin", io.StringIO('{"prompt":"","session_id":"s"}')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    sys.argv = [str(BODY), "store.py", "project:integration", "1500", "user_prompt"]
                    self.assertEqual(body.main(), 0)
        finally:
            sys.argv = old_argv
        self.assertEqual([args[0] for args in calls], ["query-rewrite", "recall"])
        self.assertEqual(calls[1][calls[1].index("--query") + 1], "empty.py")

        provider = _load_provider()
        provider_calls: list[list[str]] = []

        def fake_provider_store(args, timing=None, input_text=None):
            del timing, input_text
            provider_calls.append(list(args))
            if args[0] == "query-rewrite":
                return {"ok": True, "stdout": '{"query":"empty.py","rewrite":1}',
                        "stderr": "", "returncode": 0}
            return {"ok": True, "stdout": '{"rendered":""}', "stderr":"", "returncode":0}

        with mock.patch.dict(os.environ, {"ZMEM_INJECT": "1", "ZMEM_QUERY_CONTEXT": "1"}, clear=False), \
                mock.patch.object(provider, "_run_store", fake_provider_store):
            instance = provider.ZmemMemoryProvider()
            instance._namespace = "project:integration"
            self.assertEqual(instance.prefetch("", session_id="s"), "")
        self.assertEqual(
            [args[0] for args in provider_calls], ["query-rewrite", "recall"]
        )
        self.assertEqual(
            provider_calls[1][provider_calls[1].index("--query") + 1], "empty.py"
        )

        with tempfile.TemporaryDirectory(prefix="zmem-183-query-prefetch-") as raw:
            tmp = Path(raw)
            store = _init_store(tmp)
            conn = sqlite3.connect(store)
            try:
                _insert_edit(
                    conn, "00000000-0000-4000-8000-000000000004",
                    "2026-09-17T12:00:04Z", "C:/work/empty.py", "s",
                )
                conn.commit()
            finally:
                conn.close()
            rewrite = _run_store(
                tmp, "query-rewrite", "--prompt", "", "--session-id", "s",
                "--namespace", "project:integration", "--json",
            )
            self.assertEqual(rewrite.returncode, 0, rewrite.stderr)
            self.assertEqual(
                json.loads(rewrite.stdout), {"query": "empty.py", "rewrite": 1}
            )
            result = _run_store(
                tmp, "prefetch", "--query", "", "--namespace", "project:integration",
                "--session-id", "s", "--moment", "user_prompt", "--lane", "claude",
                "--for-injection", "--no-bump", "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsInstance(payload, dict)
            self.assertIn("results", payload)

    def test_native_worker_admission_is_one_shot_and_malformed_rows_start_none(self):
        provider = _load_provider()
        starts: list[object] = []

        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                starts.append(self)
                if len(starts) == 2:
                    raise RuntimeError("simulated start refusal")

        original_started = provider._NATIVE_EVIDENCE_STARTED
        original_thread = provider.threading.Thread
        try:
            provider._NATIVE_EVIDENCE_STARTED = False
            provider.threading.Thread = FakeThread
            provider._ensure_native_evidence_workers()
            provider._ensure_native_evidence_workers()
            self.assertEqual(len(starts), 2)
            self.assertTrue(provider._NATIVE_EVIDENCE_STARTED)
        finally:
            provider._NATIVE_EVIDENCE_STARTED = original_started
            provider.threading.Thread = original_thread

        values = {
            "tool_name": "write_file", "session_id": "s", "task_id": "t",
            "tool_call_id": "c", "args": {"path": "x.py"},
            "result": {"status": "ok"}, "duration_ms": 1,
        }
        with mock.patch.object(provider, "_ensure_native_evidence_workers") as ensure:
            provider._enqueue_native_evidence({}, clock=lambda: "2026-09-17T00:00:00Z")
            provider._enqueue_native_evidence(
                dict(values, args={"path": "x" * 100_000}),
                clock=lambda: "2026-09-17T00:00:00Z",
            )
        ensure.assert_not_called()

        original_queue = provider._NATIVE_EVIDENCE_QUEUE
        try:
            provider._NATIVE_EVIDENCE_QUEUE = queue.Queue(maxsize=1)
            provider._NATIVE_EVIDENCE_QUEUE.put_nowait("already-full")
            with mock.patch.object(provider, "_ensure_native_evidence_workers"):
                provider._enqueue_native_evidence(
                    values, clock=lambda: "2026-09-17T00:00:00Z"
                )
            self.assertEqual(provider._NATIVE_EVIDENCE_QUEUE.qsize(), 1)
        finally:
            provider._NATIVE_EVIDENCE_QUEUE = original_queue


class HermesExplicitSearchIntegrationTest(unittest.TestCase):
    def test_star_search_returns_foreign_project_and_user_global_rows(self):
        """The Hermes '*' search must retain the store-wide search contract.

        This crosses the real provider -> ``store.py`` subprocess boundary.  A
        source or argv-only assertion would miss the #167 regression, where
        the namespace-less CLI call entered implicit scoped recall and silently
        dropped both a foreign project row and the ``user:global`` row.
        """
        with tempfile.TemporaryDirectory(prefix="zmem-hermes-star-search-") as raw:
            tmp = Path(raw)
            env = _env(tmp, ZMEM_NAMESPACE="project:current")
            with mock.patch.dict(os.environ, env, clear=True):
                _init_store(tmp)
                for namespace, content in (
                    (
                        "project:foreign",
                        "hermes star regression foreign project sentinel",
                    ),
                    (
                        "user:global",
                        "hermes star regression user global sentinel",
                    ),
                ):
                    added = _run_store(
                        tmp,
                        "add", "--namespace", namespace,
                        "--type", "fact", "--content", content,
                        "--signal", "test", "--confidence", "1.0", "--json",
                    )
                    self.assertEqual(added.returncode, 0, added.stderr)

                provider = _load_provider()
                instance = provider.ZmemMemoryProvider()
                instance._namespace = "project:current"
                raw_result = instance.handle_tool_call(
                    "zmem_search",
                    {
                        "query": "hermes star regression sentinel",
                        "namespace": "*",
                        "limit": 5,
                    },
                )
                payload = json.loads(raw_result)
                self.assertEqual(payload.get("count"), 2, payload)
                contents = {item.get("content") for item in payload["results"]}
                self.assertIn(
                    "hermes star regression foreign project sentinel", contents
                )
                self.assertIn(
                    "hermes star regression user global sentinel", contents
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
