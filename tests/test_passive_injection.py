"""Issue #158 acceptance checks for the single passive-injection seam.

These tests deliberately exercise the adapters at their process boundaries.
They use only GUID-named scratch stores and the committed two-row fixture;
the operator's ``~/.zmem`` is never a test target.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "injection-parity"
NAMESPACE = "project:parity"
ROW_IDS = [
    "e0000000-0000-4000-8000-000000000001",
    "e0000000-0000-4000-8000-000000000002",
]


def _fixture_module():
    spec = importlib.util.spec_from_file_location(
        f"zmem_phase25_fixture_{uuid.uuid4().hex}",
        FIXTURE_DIR / "generate.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_env(tmp: Path, *, data_dir: Path | None = None) -> dict[str, str]:
    strip = {
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_ROOT",
        "ZMEM_NAMESPACE", "ZMEM_HOST", "ZMEM_INJECT",
        "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MODEL_AUTODOWNLOAD",
        "ZMEM_MODELS_DIR", "ZMEM_TEST_NOW", "ZMEM_EMBED_PROFILE",
        "ZMEM_CROSS_ENCODER", "ZMEM_QUERY_CONTEXT", "ZMEM_SESSION",
        "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
        "ZCODE_PLUGIN_DATA", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    }
    env = {key: value for key, value in os.environ.items() if key not in strip}
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(data_dir or (tmp / "data")),
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_NAMESPACE": NAMESPACE,
        "ZMEM_HOST": "claude",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
        "ZMEM_TEST_NOW": "2026-06-01T00:00:00Z",
        "ZMEM_INJECT_TOKEN_BUDGET": "1500",
        "ZMEM_QUERY_CONTEXT": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _copy_empty_ledger(data_dir: Path, session_id: str) -> Path:
    path = data_dir / "ops" / (
        hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".ledger"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURE_DIR / "ledger.json").read_bytes())
    return path


def _build_fixture(tmp: Path) -> Path:
    store = tmp / "store.sqlite"
    _fixture_module().build_fixture_store(str(store))
    return store


def _run_body(
    env: dict[str, str], session_id: str, *, store_py: Path = STORE_PY,
) -> str:
    event = {"prompt": "stash pop", "session_id": session_id, "cwd": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(BODY), str(store_py), NAMESPACE, "25000", "user_prompt"],
        input=json.dumps(event), capture_output=True, text=True, env=env, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"hook failed: {result.returncode}\n{result.stderr}")
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(f"hook returned non-JSON stdout: {result.stdout!r}") from exc
    return payload.get("additionalContext", "")


def _load_storelib():
    """Load the public storelib surface without coupling tests to its layout."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import storelib
    return storelib


def _assert_silent_envelope(testcase: unittest.TestCase, payload: dict, budget: int):
    """Pin the complete fail-open envelope, including zero-valued fields."""
    testcase.assertEqual(
        payload,
        {
            "results": [],
            "count": 0,
            "omitted": 0,
            "reason": "empty-pool",
            "excluded": [],
            "candidate_ids": [],
            "tokens_used": 0,
            "tokens_budget": budget,
            "budget_dropped": 0,
            "budget_admission": 0,
            "budget_truncated": 0,
            "budget_dropped_protected": 0,
            "arms": {},
            "rendered": "",
        },
    )


def _load_provider(module_name: str):
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal Hermes host ABC stand-in
        pass

    memory_provider.MemoryProvider = MemoryProvider
    agent.memory_provider = memory_provider
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", memory_provider)

    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "hermes-plugin" / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class PassiveParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-{uuid.uuid4().hex}-"
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hook_and_provider_rendered_sha_match(self):
        """The two adapters must pass through the same store-rendered bytes."""
        _build_fixture(self.tmp)
        # Both adapters resolve passive sidecars from ZMEM_STORE's parent
        # when no explicit selector data_dir is supplied.  Use independent
        # sessions so the hook's delivery ledger cannot suppress the
        # provider's parity turn through that shared resolved directory.
        hook_session_id = "phase25-parity-hook-session"
        provider_session_id = "phase25-parity-provider-session"
        hook_data = self.tmp / "hook-data"
        provider_data = self.tmp / "provider-data"
        hook_data.mkdir()
        provider_data.mkdir()

        hook_rendered = _run_body(
            _clean_env(self.tmp, data_dir=hook_data), hook_session_id
        )
        self.assertTrue(hook_rendered)
        self.assertNotIn("<memory-context>", hook_rendered)

        provider_env = _clean_env(self.tmp, data_dir=provider_data)
        old_env = os.environ.copy()
        module_name = f"zmem_phase25_parity_provider_{uuid.uuid4().hex}"
        try:
            os.environ.clear()
            os.environ.update(provider_env)
            module = _load_provider(module_name)
            provider = module.ZmemMemoryProvider()
            provider.initialize(provider_session_id)
            provider_rendered = provider.prefetch(
                "stash pop", session_id=provider_session_id
            )
        finally:
            sys.modules.pop(module_name, None)
            os.environ.clear()
            os.environ.update(old_env)

        self.assertTrue(provider_rendered)
        self.assertNotIn("<memory-context>", provider_rendered)
        hook_digest = hashlib.sha256(hook_rendered.encode("utf-8")).hexdigest()
        provider_digest = hashlib.sha256(provider_rendered.encode("utf-8")).hexdigest()
        self.assertEqual(hook_digest.lower(), provider_digest.lower())

    def test_provider_second_turn_is_already_delivered(self):
        _build_fixture(self.tmp)
        session_id = "phase25-second-turn-session"
        data_dir = self.tmp / "provider-data"
        data_dir.mkdir()
        provider_env = _clean_env(self.tmp, data_dir=data_dir)
        # With no explicit selector data_dir, the canonical resolver uses
        # ZMEM_STORE's parent rather than ZMEM_DATA.  Seed and inspect that
        # resolved ledger so the assertion exercises the actual sidecar.
        ledger_path = _copy_empty_ledger(
            Path(provider_env["ZMEM_STORE"]).expanduser().resolve().parent,
            session_id,
        )
        old_env = os.environ.copy()
        module_name = f"zmem_phase25_second_provider_{uuid.uuid4().hex}"
        try:
            os.environ.clear()
            os.environ.update(provider_env)
            module = _load_provider(module_name)
            provider = module.ZmemMemoryProvider()
            provider.initialize(session_id)
            first = provider.prefetch("stash pop", session_id=session_id)
            self.assertTrue(first)
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))["entries"]
            self.assertEqual([entry["id"] for entry in entries], ROW_IDS)

            second = provider.prefetch("stash pop", session_id=session_id)
            self.assertEqual(second, "")

            envelope = subprocess.run(
                [
                    sys.executable, str(STORE_PY), "recall", "--query", "stash pop",
                    "--limit", "5", "--no-bump", "--for-injection", "--json",
                    "--session-id", session_id, "--moment", "user_prompt",
                    "--lane", "hermes-provider", "--namespace", NAMESPACE,
                ],
                capture_output=True, text=True, env=provider_env, timeout=120,
            )
            self.assertEqual(envelope.returncode, 0, envelope.stderr)
            parsed = json.loads(envelope.stdout)
        finally:
            sys.modules.pop(module_name, None)
            os.environ.clear()
            os.environ.update(old_env)

        self.assertEqual(parsed["reason"], "already-delivered")
        self.assertEqual(parsed["excluded"], ROW_IDS)
        self.assertEqual(parsed["results"], [])
        self.assertEqual(parsed["rendered"], "")


class PassiveSelectorContractTest(unittest.TestCase):
    """Acceptance checks for the store-owned passive selector boundary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-selector-{uuid.uuid4().hex}-"
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open_fixture(self):
        store = _build_fixture(self.tmp)
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        return conn

    def _selector(self, env):
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        storelib = _load_storelib()
        selector = getattr(storelib, "select_and_budget_for_injection", None)
        self.assertTrue(
            callable(selector),
            "the passive selector must be a public storelib seam",
        )
        return old_env, storelib, selector

    def _call_selector(self, selector, conn, *, session_id, budget, data_dir):
        return selector(
            conn,
            query="stash pop",
            namespace=NAMESPACE,
            moment="user_prompt",
            session_id=session_id,
            lane="claude",
            limit=5,
            budget_tokens=budget,
            data_dir=str(data_dir),
        )

    def test_passive_selector_no_bump_and_rendered_rows_only_are_recorded(self):
        """AC5: only rendered rows may gain surfaced telemetry or ledger entries."""
        session_id = "phase25-selector-contract"
        data_dir = self.tmp / "data"
        data_dir.mkdir()
        ledger_path = _copy_empty_ledger(data_dir, session_id)
        conn = self._open_fixture()
        old_env, _storelib, selector = self._selector(
            _clean_env(self.tmp, data_dir=data_dir)
        )
        try:
            before_rows = conn.execute(
                """
                SELECT id, quote(retrieval_count), quote(last_retrieved),
                       quote(surfaced_count), quote(last_surfaced)
                FROM memory ORDER BY id
                """
            ).fetchall()
            before = {row[0]: tuple(row[1:]) for row in before_rows}

            # 162 is exactly the fixed 128-token fence shell plus one
            # 34-token fixture row.  The second row would require 196, so
            # this makes the rendered/ledger set differ from the candidate
            # set without depending on ranking order.
            payload = self._call_selector(
                selector, conn, session_id=session_id, budget=162,
                data_dir=data_dir,
            )
            self.assertEqual(set(payload["candidate_ids"]), set(ROW_IDS))
            self.assertEqual(len(payload["results"]), 1)
            selected_ids = [row["id"] for row in payload["results"]]
            self.assertTrue(payload["rendered"])
            rendered_ids = [
                row_id for row_id in selected_ids if row_id in payload["rendered"]
            ]
            self.assertEqual(rendered_ids, selected_ids)
            non_rendered = set(ROW_IDS) - set(rendered_ids)
            self.assertTrue(non_rendered)
            self.assertTrue(all(row_id not in payload["rendered"] for row_id in non_rendered))

            after_rows = conn.execute(
                """
                SELECT id, quote(retrieval_count), quote(last_retrieved),
                       quote(surfaced_count), quote(last_surfaced)
                FROM memory ORDER BY id
                """
            ).fetchall()
            after = {row[0]: tuple(row[1:]) for row in after_rows}
            self.assertEqual(
                {row_id: after[row_id][:2] for row_id in ROW_IDS},
                {row_id: before[row_id][:2] for row_id in ROW_IDS},
            )
            for row_id in non_rendered:
                self.assertEqual(after[row_id][2:], before[row_id][2:])

            entries = json.loads(ledger_path.read_text(encoding="utf-8"))["entries"]
            self.assertEqual(
                [entry["id"] for entry in entries],
                rendered_ids,
                "delivery ledger must contain only rows present in rendered output",
            )
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)

    def test_selector_failure_returns_complete_silent_envelope(self):
        """AC6: selection errors fail open with every required zero field."""
        conn = self._open_fixture()
        old_env, _storelib, selector = self._selector(
            _clean_env(self.tmp, data_dir=self.tmp / "data")
        )
        try:
            conn.close()
            payload = self._call_selector(
                selector, conn, session_id="phase25-selector-error", budget=333,
                data_dir=self.tmp / "data",
            )
            _assert_silent_envelope(self, payload, 333)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_render_failure_returns_complete_silent_envelope(self):
        """AC6: a canonical renderer failure cannot leak partial text."""
        session_id = "phase25-render-error"
        data_dir = self.tmp / "data"
        data_dir.mkdir()
        _copy_empty_ledger(data_dir, session_id)
        conn = self._open_fixture()
        old_env, storelib, selector = self._selector(
            _clean_env(self.tmp, data_dir=data_dir)
        )
        try:
            recall_module = importlib.import_module("storelib.recall")
            with mock.patch.object(
                storelib, "_format_fenced_recall", side_effect=RuntimeError("render")
            ), mock.patch.object(
                recall_module, "_format_fenced_recall", side_effect=RuntimeError("render")
            ):
                payload = self._call_selector(
                    selector, conn, session_id=session_id, budget=333,
                    data_dir=data_dir,
                )
            _assert_silent_envelope(self, payload, 333)
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)

    def test_ledger_failure_returns_complete_silent_envelope(self):
        """AC6: a ledger write failure is fail-open and text-free."""
        session_id = "phase25-ledger-error"
        bad_data_dir = self.tmp / "data-file"
        bad_data_dir.write_text("not a directory", encoding="utf-8")
        conn = self._open_fixture()
        old_env, _storelib, selector = self._selector(
            _clean_env(self.tmp, data_dir=bad_data_dir)
        )
        try:
            payload = self._call_selector(
                selector, conn, session_id=session_id, budget=333,
                data_dir=bad_data_dir,
            )
            _assert_silent_envelope(self, payload, 333)
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)
if __name__ == "__main__":
    unittest.main(verbosity=2)
