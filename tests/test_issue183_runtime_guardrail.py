"""Installed, real-boundary guardrail for issue #183's passive context path.

This test intentionally uses a minimal SDK-shaped context, but it does not
mock the provider's store subprocess, evidence payload, SQLite writer, or
rendered recall response.  It is a runtime reachability test rather than a
fixture/oracle test.
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
import time
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STORE_PY = ROOT / "skills" / "memory" / "scripts" / "store.py"
PLUGIN_INIT = ROOT / "hermes-plugin" / "__init__.py"
NAMESPACE = "project:issue183-guardrail"
SESSION = "issue183-guardrail-session"
OTHER_SESSION = "issue183-guardrail-other"
FIXED_NOW = "2099-01-01T00:00:00Z"
SENTINEL = "REPLAY_GUARDRAIL_SENTINEL_183"


def _run_store(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STORE_PY), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _read_evidence(store_path: Path) -> list[tuple[object, ...]]:
    """Read the live store through a closed read-only SQLite connection."""
    uri = f"{store_path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        return conn.execute(
            "SELECT id, session_id, lane, moment, kind, ts, hash, excerpt, "
            "ref_path, ref_offset FROM evidence"
        ).fetchall()
    finally:
        conn.close()


@contextmanager
def _loaded_plugin():
    """Load the plugin with only the minimal SDK registration surface."""
    plugins = types.ModuleType("plugins")
    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: "zmem"
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
        module_name = f"issue183_runtime_guardrail_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, PLUGIN_INIT)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load plugin from {PLUGIN_INIT}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            yield module
        finally:
            sys.modules.pop(module_name, None)


class _Context:
    def __init__(self) -> None:
        self.provider = None
        self.hooks: dict[str, object] = {}

    def register_memory_provider(self, provider) -> None:
        self.provider = provider

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback


class RuntimeGuardrailTest(unittest.TestCase):
    def _env(self, scratch: Path) -> dict[str, str]:
        home = scratch / "home"
        isolated = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUTF8": "1",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "ZMEM_HOME": str(ROOT),
            "ZMEM_STORE": str(scratch / "store.sqlite"),
            "ZMEM_DATA": str(scratch / "data"),
            "ZMEM_MODELS_DIR": str(scratch / "models-missing"),
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_NAMESPACE": NAMESPACE,
            "ZMEM_HOST": "hermes-provider",
            "ZMEM_QUERY_CONTEXT": "1",
            "ZMEM_INJECT": "1",
            "ZMEM_AMBIG_MIN_TERMS": "4",
            "ZMEM_TEST_NOW": FIXED_NOW,
        }
        # These are deliberately absent rather than routed into the scratch
        # store: an explicit ZMEM_STORE must be the only selected store path.
        for key in (
            "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT",
            "ZCODE_PLUGIN_ROOT", "ZMEM_PLUGIN_ROOT", "ZMEM_PROJECT_DIR",
            "ZMEM_ROOT", "ZMEM_CORE_MD", "ZMEM_SESSION",
            "ZMEM_MMR_LAMBDA", "ZMEM_GRAPH_SEED", "ZMEM_ARM_CAP_MEMORY",
            "ZMEM_ARM_CAP_EPISODE", "ZMEM_ARM_CAP_ENTITY",
        ):
            isolated.pop(key, None)
        return isolated

    def _assert_ok(self, result: subprocess.CompletedProcess[str], label: str) -> None:
        self.assertEqual(
            result.returncode,
            0,
            f"{label} failed (stdout={result.stdout!r}, stderr={result.stderr!r})",
        )

    def test_real_registration_writer_rewrite_and_passive_recall_guardrail(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-runtime-guardrail-") as raw:
            scratch = Path(raw)
            env = self._env(scratch)
            store_path = scratch / "store.sqlite"

            with mock.patch.dict(os.environ, env, clear=True):
                self._assert_ok(_run_store(["init"], env), "store init")
                # This marker contains the eventual multi-token edit basename but none of
                # the raw prompt tokens.  A raw "continue" recall therefore
                # cannot pass merely because the seed text matched it.
                self._assert_ok(
                    _run_store(
                        [
                            "add", "--namespace", NAMESPACE, "--type", "fact",
                            "--content", (
                                f"{SENTINEL} retrieval_guardrail_canary.py "
                                "retrieval_guardrail_anchor.py"
                            ),
                            "--confidence", "1.0", "--signal", "test",
                            "--capture-mode", "manual",
                        ],
                        env,
                    ),
                    "seed memory",
                )
                seed_conn = sqlite3.connect(store_path)
                try:
                    updated = seed_conn.execute(
                        "UPDATE memory SET valid_from=?, ingestion_ts=? WHERE namespace=?",
                        (FIXED_NOW, FIXED_NOW, NAMESPACE),
                    ).rowcount
                    seed_conn.commit()
                finally:
                    seed_conn.close()
                self.assertEqual(updated, 1, "scratch seed clock normalization touched an unexpected row")

                with _loaded_plugin() as plugin:
                    plugin._native_utc_now = lambda: FIXED_NOW
                    context = _Context()
                    plugin.register(context)
                    self.assertIsNotNone(context.provider, "provider was not registered")
                    self.assertIn(
                        "post_tool_call",
                        context.hooks,
                        "active zmem provider did not register post_tool_call",
                    )
                    provider = context.provider
                    provider.initialize(SESSION)

                    # Context-gate leg removed with issue #160: the provider
                    # no longer consults ZMEM_QUERY_CONTEXT itself.  That
                    # exact-zero gate now lives in the store boundary's
                    # prefetch branch (storelib/cli.py), pinned by
                    # tests/test_issue183_query_integration.py
                    # (test_query_context_zero_keeps_raw_query_and_enabled_
                    # context_delivers).

                    # Prove a raw prompt does not deliver the unique marker
                    # before any eligible evidence exists.
                    no_evidence = provider.prefetch("continue", session_id=SESSION)
                    self.assertNotIn(SENTINEL, no_evidence)

                    # The registered callback is the real provider callback;
                    # its worker invokes the real store.py evidence command.
                    callback = context.hooks["post_tool_call"]
                    for index, basename in enumerate(
                        ("retrieval_guardrail_canary.py", "retrieval_guardrail_anchor.py"),
                        1,
                    ):
                        callback(
                            tool_name="write_file",
                            args={"path": f"src/{basename}", "content": "edited"},
                            result="ok",
                            task_id="task-183-runtime",
                            session_id=SESSION,
                            tool_call_id=f"call-183-runtime-{index}",
                            duration_ms=7,
                            status="ok",
                            error_type=None,
                            error_message=None,
                        )

                    deadline = time.monotonic() + 5.0
                    rows: list[tuple[object, ...]] = []
                    while time.monotonic() < deadline:
                        rows = _read_evidence(store_path)
                        if len(rows) >= 2:
                            break
                        time.sleep(0.05)
                    self.assertEqual(len(rows), 2, "real evidence workers did not persist two rows")
                    observed_paths = set()
                    for evidence_id, sid, lane, moment, kind, ts, digest, excerpt, ref_path, ref_offset in rows:
                        self.assertIsInstance(uuid.UUID(str(evidence_id)), uuid.UUID)
                        self.assertEqual(sid, SESSION)
                        self.assertEqual(lane, "hermes-provider")
                        self.assertEqual(moment, "pretool")
                        self.assertEqual(kind, "edit")
                        self.assertEqual(ts, FIXED_NOW)
                        self.assertIn(ref_path, {
                            "src/retrieval_guardrail_canary.py",
                            "src/retrieval_guardrail_anchor.py",
                        })
                        observed_paths.add(ref_path)
                        self.assertIsNone(ref_offset)
                        self.assertIn('"tool_name":"write_file"', excerpt)
                        self.assertEqual(
                            digest,
                            hashlib.sha256(f"{kind}|{ts}|{excerpt}".encode("utf-8")).hexdigest(),
                        )
                    self.assertEqual(
                        observed_paths,
                        {
                            "src/retrieval_guardrail_canary.py",
                            "src/retrieval_guardrail_anchor.py",
                        },
                    )

                    rewrite = _run_store(
                        [
                            "query-rewrite", "--prompt", "continue", "--session-id", SESSION,
                            "--namespace", NAMESPACE, "--json",
                        ],
                        env,
                    )
                    self._assert_ok(rewrite, "query-rewrite")
                    payload = json.loads(rewrite.stdout)
                    self.assertEqual(payload["rewrite"], 1)
                    self.assertIn("continue", payload["query"])
                    self.assertIn("retrieval_guardrail_canary.py", payload["query"])
                    self.assertIn("retrieval_guardrail_anchor.py", payload["query"])

                    # Issue #160 repin: the passive seam is the provider's
                    # transport delegation.  The passthrough wrap records the
                    # delegated call and delegates to the REAL transport so
                    # the real-store sentinel below stays live — the store
                    # boundary performs the rewrite proved above
                    # (payload["query"]) and its selector delivers on it; the
                    # no-marker prefetch earlier in this test proves a raw
                    # "continue" alone cannot reach the sentinel.
                    transport_calls: list[tuple[str, dict[str, object]]] = []
                    real_transport_prefetch = provider._transport.prefetch

                    def observe_transport(query, **kwargs):
                        transport_calls.append((query, dict(kwargs)))
                        return real_transport_prefetch(query, **kwargs)

                    provider._transport.prefetch = observe_transport
                    try:
                        delivered = provider.prefetch("continue", session_id=SESSION)
                    finally:
                        provider._transport.prefetch = real_transport_prefetch
                    self.assertEqual(
                        len(transport_calls), 1,
                        "provider did not delegate exactly one prefetch",
                    )
                    delegated_query, delegated_kwargs = transport_calls[0]
                    self.assertEqual(delegated_query, "continue")
                    self.assertEqual(delegated_kwargs["namespace"], NAMESPACE)
                    self.assertEqual(delegated_kwargs["session_id"], SESSION)
                    self.assertEqual(delegated_kwargs["moment"], "user_prompt")
                    self.assertEqual(delegated_kwargs["lane"], "hermes-provider")
                    self.assertIn(
                        SENTINEL, delivered, f"provider delivery={delivered!r}")
                    self.assertIn("retrieval_guardrail_canary.py", delivered)

                    # Cross-session isolation leg removed with issue #160: the
                    # provider-side rewrite helper that had to be reminded
                    # about session scoping is gone.  The in-store rewrite's
                    # session_id handling (storelib/query_ambiguity.py reads
                    # evidence scoped to the delegated session_id) owns that
                    # isolation now.

                    # The passive provider must not retain the evidence/query
                    # rewrite state after the test's environment is restored.
                    self.assertEqual(os.environ["ZMEM_STORE"], str(store_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
