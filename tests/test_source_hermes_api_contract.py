"""Focused Hermes provider contract tests for issue #139.

The frozen C6 AST guard scopes the assigned read-only constructor to the
module-level ``_hermes`` function.  These non-frozen runtime tests provide the
behavioral half of the guarantee: canonical result use, resolved session and
message/context arguments, returned window and scan rows, deterministic close,
and fail-closed provider errors.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from storelib import source  # noqa: E402


class _SpySessionDB:
    instances: list["_SpySessionDB"] = []
    fail_window = False

    def __init__(self, *, db_path: Path, read_only: bool) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        type(self).instances.append(self)

    def resolve_resume_session_id(self, continuation_id: str) -> str:
        self.calls.append(("resolve", continuation_id))
        return "resolved-session"

    def get_messages(self, session_id: str) -> list[dict[str, object]]:
        self.calls.append(("messages", session_id))
        return [
            {"id": 401, "content": "available-anchor"},
            {"id": 402, "content": "available-neighbor"},
        ]

    def get_messages_around(
        self,
        session_id: str,
        *,
        around_message_id: int,
        window: int,
    ) -> dict[str, object]:
        self.calls.append(("around", session_id, around_message_id, window))
        if type(self).fail_window:
            raise RuntimeError("window provider failure")
        return {
            "window": [{"id": 901, "content": "window-only"}],
            "messages_before": 1,
            "messages_after": 2,
        }

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True


class SourceHermesApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-hermes-api-contract-")
        self.root = Path(self.temp.name)
        self.home = self.root / "hermes-home"
        self.home.mkdir()
        db_path = self.home / "state.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE owned_fixture (value TEXT)")
            conn.commit()
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            (self.home / f"state.db{suffix}").write_bytes(b"owned sidecar")
        _SpySessionDB.instances.clear()
        _SpySessionDB.fail_window = False
        self.hermes_state = types.ModuleType("hermes_state")
        self.hermes_state.SessionDB = _SpySessionDB
        self.anchor = (
            "evidence-id",
            "evidence-id",
            "continuation-id",
            "hermes-provider",
            "2026-10-03T00:00:00Z",
            "available-anchor",
            "hermes:state.db/session/continuation-id",
            None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_hermes(self, context: int = 2):
        return source._hermes(
            self.anchor[6],
            self.anchor,
            context,
        )

    def test_hermes_uses_resolved_session_selected_message_window_and_available_rows(self) -> None:
        with patch.dict(
            source.os.environ,
            {"HERMES_HOME": str(self.home)},
            clear=False,
        ), patch.dict(sys.modules, {"hermes_state": self.hermes_state}):
            excerpt, detail, scan_rows = self._run_hermes(context=2)

        self.assertEqual(excerpt, "window-only")
        self.assertIn("window-only", excerpt)
        self.assertNotIn("available-anchor", excerpt)
        self.assertEqual(detail["session_id"], "resolved-session")
        self.assertEqual(detail["turn_start"], 901)
        self.assertEqual(detail["turn_end"], 901)
        self.assertEqual(detail["returned"], 1)
        self.assertTrue(detail["truncated"])
        self.assertIsNone(detail["byte_start"])
        self.assertIsNone(detail["byte_end"])
        self.assertEqual(
            scan_rows,
            [
                {"turn": 401, "text": "available-anchor", "raw": "available-anchor", "start": None, "end": None},
                {"turn": 402, "text": "available-neighbor", "raw": "available-neighbor", "start": None, "end": None},
            ],
        )

        self.assertEqual(len(_SpySessionDB.instances), 1)
        db = _SpySessionDB.instances[0]
        self.assertEqual(db.db_path, self.home / "state.db")
        self.assertTrue(db.read_only)
        self.assertEqual(
            db.calls,
            [
                ("resolve", "continuation-id"),
                ("messages", "resolved-session"),
                ("around", "resolved-session", 401, 2),
                ("close",),
            ],
        )
        self.assertTrue(db.closed)

    def test_hermes_provider_failure_closes_and_refuses_without_partial_result(self) -> None:
        _SpySessionDB.fail_window = True
        with patch.dict(
            source.os.environ,
            {"HERMES_HOME": str(self.home)},
            clear=False,
        ), patch.dict(sys.modules, {"hermes_state": self.hermes_state}):
            with self.assertRaisesRegex(source.SourceRefusal, "hermes_db_unavailable"):
                self._run_hermes(context=3)

        db = _SpySessionDB.instances[0]
        self.assertEqual(
            db.calls,
            [
                ("resolve", "continuation-id"),
                ("messages", "resolved-session"),
                ("around", "resolved-session", 401, 3),
                ("close",),
            ],
        )
        self.assertTrue(db.closed)

    def test_c6_guard_rejects_unused_constructor_and_private_hermes_reader(self) -> None:
        checks_path = Path(__file__).parent / "fixtures" / "source" / "repro" / "checks.py"
        spec = importlib.util.spec_from_file_location("issue139_c6_checks", checks_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        checks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checks)

        source_path = Path(source.__file__).resolve()
        original = source_path.read_text(encoding="utf-8")
        mutated = original.replace(
            "\n\ndef _hermes(",
            "\n\ndef _unused_session_db_helper():\n"
            "    return SessionDB(db_path=\"unused\", read_only=True)\n\n\ndef _hermes(",
            1,
        )
        start = mutated.index("def _hermes(")
        end = mutated.index("\ndef _resolved(", start)
        private_reader = (
            "def _hermes(ref, anchor, context):\n"
            "    connection = sqlite3.connect(\"private.sqlite\")\n"
            "    try:\n"
            "        return \"private reader\", {}, []\n"
            "    finally:\n"
            "        connection.close()\n"
        )
        mutated = mutated[:start] + private_reader + mutated[end + 1 :]

        with tempfile.TemporaryDirectory(prefix="zmem-hermes-guard-mutation-") as temp:
            mutated_root = Path(temp)
            mutated_path = mutated_root / "skills" / "memory" / "scripts" / "storelib" / "source.py"
            mutated_path.parent.mkdir(parents=True)
            mutated_path.write_text(mutated, encoding="utf-8")
            with self.assertRaises(checks.CheckFailure):
                checks._assert_hermes_resolver_uses_canonical_api(mutated_root)


if __name__ == "__main__":
    unittest.main()
