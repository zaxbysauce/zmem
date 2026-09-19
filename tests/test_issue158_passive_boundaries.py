"""Issue #158 malformed-envelope adapter boundary checks."""

from __future__ import annotations

import json
import ast
import importlib
import importlib.util
import os
import sqlite3
import shutil
import sys
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

# CI executes every Python test file directly (``python tests/<file>.py``),
# which puts ``tests/`` rather than the repository root on sys.path. Keep the
# shared acceptance helpers importable in that prescribed mode as well as
# under ``python -m unittest``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.test_passive_injection import (
    NAMESPACE,
    PassiveSelectorContractTest,
    REPO_ROOT,
    ROW_IDS,
    _build_fixture,
    _clean_env,
    _copy_empty_ledger,
    _load_storelib,
    _load_provider,
    _run_body,
)


class PassiveEnvelopeBoundaryTest(unittest.TestCase):
    """Adapters reject malformed/missing shared envelopes without local text."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-envelope-{uuid.uuid4().hex}-"
        ))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _row():
        return {
            "id": ROW_IDS[0], "content": "unfenced candidate text",
            "type": "fact", "confidence": 0.9, "signal": "test",
            "namespace": NAMESPACE,
        }

    def _fake_store(self, name: str, stdout: str) -> Path:
        path = self.tmp / f"{name}.py"
        path.write_text("print(" + repr(stdout) + ")\n", encoding="utf-8")
        return path

    def test_hook_malformed_or_missing_rendered_envelope_is_empty(self):
        row = self._row()
        outputs = {
            "malformed": "not-json",
            "missing-rendered": json.dumps({"results": [row]}),
        }
        for name, stdout in outputs.items():
            rendered = _run_body(
                _clean_env(self.tmp, data_dir=self.tmp / name),
                f"phase25-hook-{name}",
                store_py=self._fake_store(name, stdout),
            )
            self.assertEqual(rendered, "", name)
            self.assertNotIn("unfenced candidate text", rendered)
            self.assertNotIn("<memory-context>", rendered)

    def test_provider_malformed_or_missing_rendered_envelope_is_empty(self):
        row = self._row()
        outputs = {
            "malformed": "not-json",
            "missing-rendered": json.dumps({"results": [row]}),
        }
        for name, stdout in outputs.items():
            env = _clean_env(self.tmp, data_dir=self.tmp / name)
            old_env = os.environ.copy()
            module_name = f"zmem_phase25_boundary_{name}_{uuid.uuid4().hex}"
            try:
                os.environ.clear()
                os.environ.update(env)
                module = _load_provider(module_name)
                module._run_store = lambda *args, **kwargs: {
                    "ok": True, "stdout": stdout, "stderr": "", "returncode": 0,
                }
                provider = module.ZmemMemoryProvider()
                provider.initialize(f"phase25-provider-{name}")
                rendered = provider.prefetch("stash pop")
            finally:
                sys.modules.pop(module_name, None)
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(rendered, "", name)
            self.assertNotIn("unfenced candidate text", rendered)
            self.assertNotIn("<memory-context>", rendered)

    def test_ledger_write_failure_preserves_rendered_envelope(self):
        """A post-render delivery-write failure must not discard safe text."""
        store = _build_fixture(self.tmp)
        data_dir = self.tmp / "data"
        data_dir.mkdir()
        session_id = "phase25-ledger-write-error"
        _copy_empty_ledger(data_dir, session_id)
        old_env = os.environ.copy()
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        try:
            os.environ.clear()
            os.environ.update(_clean_env(self.tmp, data_dir=data_dir))
            storelib = _load_storelib()
            selector = getattr(storelib, "select_and_budget_for_injection", None)
            self.assertTrue(callable(selector), "public selector seam missing")
            ledger = importlib.import_module("storelib.delivery_ledger")
            with mock.patch.object(ledger, "record", side_effect=OSError("write")):
                payload = selector(
                    conn,
                    query="stash pop", namespace=NAMESPACE,
                    moment="user_prompt", session_id=session_id,
                    lane="claude", limit=5, budget_tokens=1500,
                    data_dir=str(data_dir),
                )
            self.assertTrue(payload["rendered"])
            self.assertTrue(payload["rendered"].startswith("<<<ZMEM_UNTRUSTED_FENCE>>>"))
            self.assertTrue(payload["rendered"].endswith("<<<END_ZMEM_UNTRUSTED_FENCE>>>\n"))
            self.assertNotIn("<memory-context>", payload["rendered"])
            self.assertIn("stash pop recovery note", payload["rendered"])
            self.assertEqual(
                json.loads(next(data_dir.joinpath("ops").glob("*.ledger")).read_text())[
                    "entries"
                ],
                [],
            )
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)


class ProcessBoundaryTest(unittest.TestCase):
    """Passive callers must not own store state or a second render path."""

    def test_hook_has_no_ledger_or_storelib_access(self):
        sources = [
            REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py",
            REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py",
            REPO_ROOT / "hooks" / "zmem-session-start.sh",
            REPO_ROOT / "hermes-plugin" / "__init__.py",
        ]
        forbidden = (
            "_LEDGER_MOD", "_OPS_TOKENS", "import inject", "import ops_tokens",
            "import delivery_ledger", "from storelib", "import storelib",
            "import schema_meta", "sqlite3", "delivery_ledger", "correction_queue",
        )
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{needle!r} remains in {path}")

        hermes = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(
            hermes,
            r"(?is)(zmem.{0,120}pre_tool_call|pre_tool_call.{0,120}zmem)",
        )
        tree = ast.parse(hermes)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in {"pre_tool_call", "zmem_pre_tool_call"}:
                continue
            callback_source = ast.get_source_segment(hermes, node) or ""
            self.assertNotRegex(
                callback_source,
                r"(?m)(open\(|sqlite3|subprocess|read_text\(|write_text\(|unlink\()",
            )

    def test_checkpoint_policy_stays_out_of_hook_adapter(self):
        """#99 enriches only at the centralized store selector boundary."""
        hook = (REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py").read_text(
            encoding="utf-8"
        )
        for needle in (
            "CHECKPOINT_PHRASES", "checkpoint_query_expansion",
            "compose_pretool_query",
            "foreign-stash conflict verify stash list",
            "stale tree fetch main rebase verify diff",
            "stale tree fetched base force-with-lease",
            "base drift citation re-pin",
            "basename ratchet citation re-pin local battery",
        ):
            self.assertNotIn(needle, hook, needle)
        policy = (REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" /
                  "ops_tokens.py").read_text(encoding="utf-8")
        for needle in ("CHECKPOINT_PHRASES", "checkpoint_query_expansion",
                       "compose_pretool_query"):
            self.assertIn("def " + needle if needle != "CHECKPOINT_PHRASES"
                          else needle, policy)


class SelectorCliTest(unittest.TestCase):
    def test_session_aware_recent_honors_min_confidence(self):
        """The selector must preserve an explicit recent SQL floor."""
        tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-floor-{uuid.uuid4().hex}-"
        ))
        try:
            store = _build_fixture(tmp)
            conn = sqlite3.connect(str(store))
            try:
                conn.execute(
                    "UPDATE memory SET confidence = 0.30 WHERE id = ?",
                    (ROW_IDS[0],),
                )
                conn.commit()
            finally:
                conn.close()
            env = _clean_env(tmp, data_dir=tmp / "data")
            result = subprocess.run(
                [
                    sys.executable, str(REPO_ROOT / "skills" / "memory" /
                                       "scripts" / "store.py"),
                    "recent", "--namespace", NAMESPACE, "--limit", "5",
                    "--min-confidence", "0.25", "--no-bump",
                    "--for-injection", "--json", "--session-id",
                    "phase25-explicit-floor", "--moment", "session_start",
                    "--lane", "claude",
                ],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn(ROW_IDS[0], payload["candidate_ids"])
            self.assertIn("stash pop recovery note one", payload["rendered"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ledger_clear_is_store_independent(self):
        tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-clear-{uuid.uuid4().hex}-"
        ))
        try:
            store = tmp / "store.sqlite"
            data_dir = tmp / "missing-data"
            env = _clean_env(tmp, data_dir=data_dir)
            session_id = "sess-clear"
            command = [
                sys.executable, str(REPO_ROOT / "skills" / "memory" / "scripts" /
                                   "store.py"), "ledger-clear",
                "--session-id", session_id,
            ]
            expected = '{"ok":true,"session_id":"sess-clear","cleared":true}\n'
            first = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, expected)
            self.assertFalse(store.exists(), "ledger-clear must not open SQLite")

            ledger = _copy_empty_ledger(data_dir, session_id)
            self.assertTrue(ledger.exists())
            second = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, expected)
            self.assertFalse(store.exists(), "ledger-clear must remain SQLite-free")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SelectorValidationTest(unittest.TestCase):
    """The selector validates attribution before consulting delivery state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(
            prefix=f"zmem-phase25-validation-{uuid.uuid4().hex}-"
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _selector_context(self):
        store = _build_fixture(self.tmp)
        conn = sqlite3.connect(str(store))
        conn.row_factory = sqlite3.Row
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(_clean_env(self.tmp, data_dir=self.tmp / "data"))
        storelib = _load_storelib()
        selector = getattr(storelib, "select_and_budget_for_injection", None)
        self.assertTrue(callable(selector), "public selector seam missing")
        return conn, old_env, selector

    def _kwargs(self, *, moment="user_prompt", lane="claude", query="stash pop"):
        return {
            "query": query,
            "namespace": NAMESPACE,
            "moment": moment,
            "session_id": "phase25-validation-session",
            "lane": lane,
            "limit": 5,
            "budget_tokens": 1500,
            "data_dir": str(self.tmp / "data"),
        }

    def test_invalid_moment_is_rejected_before_ledger_access(self):
        conn, old_env, selector = self._selector_context()
        try:
            ledger = importlib.import_module("storelib.delivery_ledger")
            with mock.patch.object(ledger, "delivered_ids") as delivered_ids:
                with self.assertRaises(ValueError):
                    selector(conn, **self._kwargs(moment="not-a-moment"))
                delivered_ids.assert_not_called()
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)

    def test_invalid_non_null_lane_is_rejected_before_ledger_access(self):
        conn, old_env, selector = self._selector_context()
        try:
            ledger = importlib.import_module("storelib.delivery_ledger")
            with mock.patch.object(ledger, "delivered_ids") as delivered_ids:
                with self.assertRaises(ValueError):
                    selector(conn, **self._kwargs(lane="not-a-lane"))
                delivered_ids.assert_not_called()
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)

    def test_empty_query_dispatches_to_recent(self):
        conn, old_env, selector = self._selector_context()
        try:
            recall_module = importlib.import_module("storelib.recall")
            ledger = importlib.import_module("storelib.delivery_ledger")
            with mock.patch.object(
                recall_module, "recall_memory",
                side_effect=AssertionError("empty query must not use recall"),
            ) as recall_memory, mock.patch.object(
                recall_module, "recent_memory", return_value=[]
            ) as recent_memory, mock.patch.object(
                ledger, "delivered_ids", return_value=[]
            ):
                selector(conn, **self._kwargs(query=""))
            recall_memory.assert_not_called()
            recent_memory.assert_called_once()
        finally:
            conn.close()
            os.environ.clear()
            os.environ.update(old_env)

    def test_cli_session_id_without_moment_exits_two(self):
        env = _clean_env(self.tmp, data_dir=self.tmp / "data")
        result = subprocess.run(
            [
                sys.executable, str(REPO_ROOT / "skills" / "memory" / "scripts" /
                                   "store.py"),
                "recall", "--query", "stash pop", "--json",
                "--session-id", "phase25-missing-moment",
            ],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(result.returncode, 2, result.stderr)


class FixtureOracleBindingTest(unittest.TestCase):
    """The committed oracle must equal the generator's independent literal."""

    def test_expected_envelope_matches_literal_generator_bytes(self):
        saved_path = list(sys.path)
        try:
            fixture = importlib.util.spec_from_file_location(
                f"zmem_phase25_oracle_{uuid.uuid4().hex}",
                REPO_ROOT / "tests" / "fixtures" / "injection-parity" / "generate.py",
            )
            module = importlib.util.module_from_spec(fixture)
            fixture.loader.exec_module(module)
            literal = json.dumps(
                module.EXPECTED_ENVELOPE,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        finally:
            sys.path[:] = saved_path
        committed = (
            REPO_ROOT / "tests" / "fixtures" / "injection-parity" /
            "expected-envelope.json"
        )
        self.assertEqual(committed.read_bytes(), literal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
