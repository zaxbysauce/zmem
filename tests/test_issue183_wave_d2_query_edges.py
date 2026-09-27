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


# A complete #158/#159 selector envelope (the transport coerces to exactly
# this key set), used by the recording transport stubs below.
_OK_ENVELOPE = {
    "results": [], "count": 0, "omitted": 0, "reason": "ok",
    "excluded": [], "candidate_ids": [], "tokens_used": 0,
    "tokens_budget": 0, "budget_dropped": 0, "budget_admission": 0,
    "budget_truncated": 0, "budget_dropped_protected": 0, "arms": {},
    "rendered": "",
}

# Issue #160: deterministic local-mode construction for the provider under
# test (auto-local mode against this checkout, never a stray ZMEM_MCP_URL).
_PROVIDER_ENV = {
    "ZMEM_QUERY_CONTEXT": "1", "ZMEM_INJECT": "1",
    "ZMEM_HOME": str(ROOT), "ZMEM_MCP_URL": "",
}


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
        # Issue #160 repin: the provider delegates ONE prefetch through the
        # real transport; dash-safety now lives in the transport's argv
        # builder (a leading-dash query rides the --query=<value> form — also
        # pinned by tests/test_hermes_transport.py RecordedCallTest).
        provider_mod = _load_provider()
        argv_seen: list[list[str]] = []

        class RecordingExecutor:
            def run(self, fn, deadline_s):
                del deadline_s
                argv_seen.append(list(fn._cmd))
                return json.dumps(dict(_OK_ENVELOPE, rendered="ok"))

        with mock.patch.dict(os.environ, dict(_PROVIDER_ENV), clear=False):
            provider = provider_mod.ZmemMemoryProvider()
            provider._namespace = "project:d2"
            self.assertIsNotNone(provider._transport)
            provider._transport = provider_mod._transport.LocalSubprocess(
                store_py=str(STORE), executor=RecordingExecutor(),
                deadline_s=6.0)
            self.assertEqual(provider.prefetch("--dry-run", session_id="s"), "ok")
        self.assertEqual(len(argv_seen), 1, argv_seen)
        argv = argv_seen[0]
        self.assertIn("prefetch", argv)
        self.assertIn("--query=--dry-run", argv)
        self.assertFalse(
            any("query-rewrite" in part for part in argv),
            "no provider-side query-rewrite subprocess may run",
        )

    def test_provider_rewrite_bridge_failure_is_fail_open(self):
        # Issue #160 repin: fail-open at the transport seam — a real
        # LocalSubprocess + DeadlineExecutor pointed at a nonexistent store.py
        # returns the empty envelope and the provider surfaces "".
        provider_mod = _load_provider()
        with mock.patch.dict(os.environ, dict(_PROVIDER_ENV), clear=False):
            provider = provider_mod.ZmemMemoryProvider()
            provider._namespace = "project:d2"
            self.assertIsNotNone(provider._transport)
            with tempfile.TemporaryDirectory(prefix="zmem-183-d2-failopen-") as raw:
                provider._transport = provider_mod._transport.LocalSubprocess(
                    store_py=str(Path(raw) / "no-such-store.py"),
                    executor=provider_mod._transport.DeadlineExecutor(),
                    deadline_s=6.0)
                self.assertEqual(provider.prefetch("continue", session_id="s"), "")

    def test_provider_empty_user_prompt_uses_shared_rewrite_before_recent(self):
        # Issue #160 repin: an empty prompt is delegated ONCE with query "";
        # the queryless selector path inside store.py's prefetch replaces the
        # old provider-side rewrite-before-recent sequence.
        provider_mod = _load_provider()
        delegations: list[tuple[str, dict]] = []

        def recording_prefetch(query, **kwargs):
            delegations.append((query, dict(kwargs)))
            return dict(_OK_ENVELOPE, rendered="ok")

        with mock.patch.dict(os.environ, dict(_PROVIDER_ENV), clear=False):
            provider = provider_mod.ZmemMemoryProvider()
            provider._namespace = "project:d2"
            self.assertIsNotNone(provider._transport)
            provider._transport.prefetch = recording_prefetch
            self.assertEqual(provider.prefetch("", session_id="s"), "ok")
        self.assertEqual(len(delegations), 1, delegations)
        self.assertEqual(delegations[0][0], "")
        self.assertEqual(delegations[0][1]["moment"], "user_prompt")

    def test_provider_classifies_full_prompt_before_500_char_output_cap(self):
        # Issue #160 repin: the provider delegates the RAW prompt — no
        # provider-side pre-truncation.  Classification-on-complete-prompt
        # and the 500-char rewrite output cap are store-owned
        # (storelib/query_ambiguity.py), reached through the delegated
        # prefetch subprocess.
        provider_mod = _load_provider()
        prefix = "please continue finalizing work " * 20
        prompt = prefix + " actual.py"
        self.assertGreater(len(prompt), 500)
        self.assertLessEqual(len(prompt), 4096)
        delegations: list[tuple[str, dict]] = []

        def recording_prefetch(query, **kwargs):
            delegations.append((query, dict(kwargs)))
            return dict(_OK_ENVELOPE, rendered="ok")

        with mock.patch.dict(os.environ, dict(_PROVIDER_ENV), clear=False):
            provider = provider_mod.ZmemMemoryProvider()
            provider._namespace = "project:d2"
            self.assertIsNotNone(provider._transport)
            provider._transport.prefetch = recording_prefetch
            self.assertEqual(provider.prefetch(prompt, session_id="s"), "ok")

        self.assertEqual(len(delegations), 1, delegations)
        delegated_query, delegated_kwargs = delegations[0]
        self.assertEqual(delegated_query, prompt)
        self.assertEqual(len(delegated_query), len(prompt))
        self.assertEqual(delegated_kwargs["moment"], "user_prompt")
        self.assertEqual(delegated_kwargs["lane"], "hermes-provider")

    # test_provider_oversized_prompt_skips_rewrite_and_falls_back_bounded was
    # removed with issue #160 — the provider no longer gates at 4096 (it
    # 4096-truncates the prompt and delegates; the store's rewrite output cap
    # owns the bound).

    def test_provider_valid_long_ambiguous_rewrite_stays_bounded(self):
        # Issue #160 repin: the cap's home moved in-store with #160 — the
        # store's rewrite output cap (storelib/query_ambiguity.py) owns the
        # 500-char bound.  Observed at the store boundary the provider's
        # transport now delegates to: one query-rewrite subprocess proves the
        # rewritten query stays bounded, and the prefetch subprocess (the
        # transport's exact argv shape) completes against the seeded scratch
        # store.
        # AC2 (tests/test_issue183_acceptance_query.py) proves this long
        # prompt is classified ambiguous; here it must rewrite but stay
        # bounded by the store's cap.
        prompt = "x" * 900
        self.assertLess(len(prompt), 4096)
        with tempfile.TemporaryDirectory(prefix="zmem-183-d2-bound-") as raw:
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
            rewrite = _run_store(
                tmp, "query-rewrite", "--prompt", prompt,
                "--session-id", "d2-session", "--namespace", "project:d2",
                "--json",
            )
            self.assertEqual(rewrite.returncode, 0, rewrite.stderr)
            payload = json.loads(rewrite.stdout)
            self.assertEqual(payload["rewrite"], 1, payload)
            self.assertLessEqual(len(payload["query"]), 500, payload)
            prefetch = _run_store(
                tmp, "prefetch", "--query", prompt,
                "--namespace", "project:d2", "--session-id", "d2-session",
                "--moment", "user_prompt", "--lane", "hermes-provider",
                "--json",
            )
            self.assertEqual(prefetch.returncode, 0, prefetch.stderr)
            envelope = json.loads(prefetch.stdout)
            self.assertIn("rendered", envelope)

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
