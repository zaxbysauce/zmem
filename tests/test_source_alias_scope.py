"""Namespace-alias scope regressions for issue #139 source resolution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib.source import SourceRefusal, _memory  # noqa: E402


class SourceAliasScopeTests(unittest.TestCase):
    """Source lookup may bridge only the v5 project namespace pair."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        self.conn.execute(
            "CREATE TABLE memory(id TEXT, namespace TEXT, source_ref TEXT)"
        )

    def tearDown(self) -> None:
        self.conn.close()

    def _set_map(self, value: object) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES ('ns_migration_v5', ?)", (json.dumps(value),)
        )

    def _set_raw_map(self, value: object) -> None:
        self.conn.execute("INSERT INTO meta VALUES ('ns_migration_v5', ?)", (value,))

    def _insert_memory(self, namespace: str, memory_id: str = "memory") -> None:
        self.conn.execute(
            "INSERT INTO memory VALUES (?, ?, 'fixture.jsonl')", (memory_id, namespace)
        )

    def _lookup(
        self, namespace: str, memory_id: str = "memory"
    ) -> tuple[sqlite3.Row, str]:
        with patch.dict(os.environ, {"ZMEM_NAMESPACE": namespace}, clear=False):
            return _memory(self.conn, memory_id)

    def test_source_accepts_forward_project_alias(self) -> None:
        old_namespace = "project:legacy-name"
        new_namespace = "project:github.com/example/repository"
        self._set_map({old_namespace: new_namespace})
        self._insert_memory(new_namespace)

        row, requested = self._lookup(old_namespace)

        self.assertEqual(row["namespace"], new_namespace)
        self.assertEqual(requested, old_namespace)

    def test_source_accepts_reverse_project_alias(self) -> None:
        old_namespace = "project:legacy-name"
        new_namespace = "project:github.com/example/repository"
        self._set_map({old_namespace: new_namespace})
        self._insert_memory(old_namespace)

        row, requested = self._lookup(new_namespace)

        self.assertEqual(row["namespace"], old_namespace)
        self.assertEqual(requested, new_namespace)

    def test_source_keeps_exact_nonproject_namespace_without_aliasing(self) -> None:
        requested = "host:legacy"
        self._set_map({requested: "host:replacement"})
        self._insert_memory(requested)

        row, returned_namespace = self._lookup(requested)

        self.assertEqual(row["namespace"], requested)
        self.assertEqual(returned_namespace, requested)

    def test_source_refuses_project_to_global_alias(self) -> None:
        requested = "project:allowed"
        self._set_map({requested: "user:global"})
        self._insert_memory(requested, "exact")
        self._insert_memory("user:global", "foreign")

        row, _ = self._lookup(requested, "exact")
        self.assertEqual(row["namespace"], requested)

        with self.assertRaises(SourceRefusal):
            self._lookup(requested, "foreign")

    def test_source_refuses_global_to_project_alias(self) -> None:
        requested = "user:global"
        self._set_map({requested: "project:allowed"})
        self._insert_memory(requested, "exact")
        self._insert_memory("project:allowed", "foreign")

        row, _ = self._lookup(requested, "exact")
        self.assertEqual(row["namespace"], requested)

        with self.assertRaises(SourceRefusal):
            self._lookup(requested, "foreign")

    def test_source_refuses_non_object_alias_metadata(self) -> None:
        self._set_map(["project:legacy-name"])
        self._insert_memory("project:allowed", "secret-row")

        with self.assertRaisesRegex(SourceRefusal, r"^source unavailable$") as raised:
            self._lookup("project:allowed")
        self.assertNotIn("secret-row", str(raised.exception))

    def test_source_refuses_invalid_json_alias_metadata(self) -> None:
        self._set_raw_map("{not-json")
        self._insert_memory("project:allowed", "secret-row")

        with self.assertRaisesRegex(SourceRefusal, r"^source unavailable$") as raised:
            self._lookup("project:allowed")
        self.assertNotIn("secret-row", str(raised.exception))

    def test_source_refuses_recursively_nested_alias_metadata(self) -> None:
        self._set_raw_map("[" * 1_100 + "]" * 1_100)
        self._insert_memory("project:allowed", "secret-row")

        with self.assertRaisesRegex(SourceRefusal, r"^source unavailable$") as raised:
            self._lookup("project:allowed")
        self.assertNotIn("secret-row", str(raised.exception))

    def test_source_refuses_non_string_alias_value(self) -> None:
        self._set_map({"project:legacy-name": 7})
        self._insert_memory("project:allowed", "secret-row")

        with self.assertRaisesRegex(SourceRefusal, r"^source unavailable$") as raised:
            self._lookup("project:allowed")
        self.assertNotIn("secret-row", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
