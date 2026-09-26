"""Wave A evidence schema, writer, retention, and strict transport tests."""

from __future__ import annotations

import contextlib
import atexit
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
)
_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-evidence-import-"))
atexit.register(shutil.rmtree, _IMPORT_SANDBOX, ignore_errors=True)
_IMPORT_VALUES = {
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
}
with patch.dict(os.environ, _IMPORT_VALUES, clear=False):
    from storelib import evidence, recall, schema, sync, write  # noqa: E402


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-evidence-test-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()


class EvidenceAssociationWriteTest(_StoreCase):
    def _evidence(self, evidence_id: str) -> None:
        evidence.write_evidence(
            self.conn, id=evidence_id, session_id="association-test",
            lane="codex", moment="pretool", kind="tool_call",
            ts="2026-09-10T00:00:00Z", excerpt="association evidence",
            ref_path="tests/test_evidence.py", ref_offset=1,
        )

    def test_add_attaches_sorted_ids_and_missing_id_rolls_back(self):
        first = "00000000-0000-4000-8000-000000000701"
        second = "00000000-0000-4000-8000-000000000702"
        missing = "00000000-0000-4000-8000-000000000799"
        self._evidence(first)
        self._evidence(second)
        memory_id = str(write.add_memory(
            self.conn, namespace="project:evidence-association", type_="fact",
            content="association write", signal="test", evidence_ids=[second, first],
        ))
        self.assertEqual(evidence.evidence_ids_for_memory(self.conn, memory_id), [first, second])
        before = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        with self.assertRaisesRegex(ValueError, f"evidence id not found: {missing}"):
            write.add_memory(
                self.conn, namespace="project:evidence-association", type_="fact",
                content="must not persist", signal="test", evidence_ids=[missing],
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0], before)

    def test_update_keeps_historical_links_and_attaches_to_replacement(self):
        old_evidence = "00000000-0000-4000-8000-000000000711"
        new_evidence = "00000000-0000-4000-8000-000000000712"
        self._evidence(old_evidence)
        self._evidence(new_evidence)
        old_id = str(write.add_memory(
            self.conn, namespace="project:evidence-association", type_="fact",
            content="original association", signal="test", evidence_ids=[old_evidence],
        ))
        replacement_id, created_new = write.update_memory(
            self.conn, mid=old_id, content="replacement association",
            evidence_ids=[new_evidence],
        )
        self.assertTrue(created_new)
        self.assertEqual(evidence.evidence_ids_for_memory(self.conn, old_id), [old_evidence])
        self.assertEqual(
            evidence.evidence_ids_for_memory(self.conn, str(replacement_id)), [new_evidence]
        )

    def test_passive_injection_capture_does_not_gain_evidence_ids(self):
        evidence_id = "00000000-0000-4000-8000-000000000713"
        self._evidence(evidence_id)
        memory_id = str(write.add_memory(
            self.conn, namespace="project:evidence-passive", type_="fact",
            content="passive injection evidence preservation", signal="test",
            evidence_ids=[evidence_id],
        ))
        captured: dict = {}
        with contextlib.redirect_stdout(io.StringIO()):
            recall.recent_memory(
                self.conn, namespace="project:evidence-passive", limit=5,
                as_json=True, for_injection=True, no_telemetry=True,
                _capture=captured,
            )
        rows = captured.get("results", [])
        self.assertEqual([row["id"] for row in rows], [memory_id], captured)
        self.assertNotIn("evidence_ids", rows[0], captured)


class EvidenceSchemaTest(_StoreCase):
    def test_fresh_init_and_v13_upgrade(self):
        version = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", ("schema_version",)
        ).fetchone()[0]
        self.assertEqual(version, "14")
        self.assertEqual(
            {r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) if r[0] in {"evidence", "episode_evidence", "memory_evidence"}},
            {"evidence", "episode_evidence", "memory_evidence"},
        )
        legacy = sqlite3.connect(self.root / "legacy.sqlite")
        schema.init_db(legacy)
        legacy.execute(
            "UPDATE meta SET value='13' WHERE key='schema_version'"
        )
        legacy.commit()
        self.assertNotIn(
            "evidence",
            {r[0] for r in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )},
        )
        schema.migrate(legacy)
        self.assertEqual(
            legacy.execute(
                "SELECT value FROM meta WHERE key=?", ("schema_version",)
            ).fetchone()[0],
            "14",
        )
        legacy.close()


class EvidenceWriterTest(_StoreCase):
    def test_redacts_and_hashes(self):
        evidence.write_evidence(
            self.conn, session_id="s", lane="codex", moment="user_prompt",
            kind="turn", ts="2026-09-10T00:00:00Z",
            excerpt="token=sk-test-1234567890", ref_path="session.txt", ref_offset=0,
            id="00000000-0000-4000-8000-000000000001",
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM evidence").fetchone()
        self.assertEqual(row["excerpt"], "[REDACTED_SECRET]")
        self.assertEqual(
            row["hash"],
            hashlib.sha256(
                f"{row['kind']}|{row['ts']}|{row['excerpt']}".encode()
            ).hexdigest(),
        )

    def test_caps_excerpt(self):
        evidence.write_evidence(
            self.conn, session_id="s", lane=None, moment="pretool",
            kind="tool_call", ts="2026-09-10T00:00:00Z",
            excerpt="a " * 201,
            ref_path="tool.jsonl", ref_offset=None,
            id="00000000-0000-4000-8000-000000000002",
        )
        row = self.conn.execute("SELECT * FROM evidence").fetchone()
        self.assertEqual(len(row["excerpt"]), 400)

    def test_rejects_closed_values(self):
        base = dict(
            session_id="s", lane="codex", moment="user_prompt", kind="turn",
            ts="2026-09-10T00:00:00Z", excerpt="safe", ref_path="x",
            ref_offset=0,
        )
        for field, value in (
            ("lane", "unknown"), ("moment", "unknown"), ("kind", "unknown"),
            ("ts", "2026-09-10T00:00:00+00:00"), ("ref_offset", -1),
            ("ref_offset", 2**63),
            ("session_id", ""), ("excerpt", ""), ("ref_path", ""),
        ):
            payload = dict(base)
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence.write_evidence(self.conn, **payload)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0
        )


class EvidenceMigrationAtomicityTest(_StoreCase):
    def _legacy_v13(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "migration.sqlite")
        schema.init_db(conn)
        conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
        conn.commit()
        return conn

    def test_each_v14_ddl_boundary_rolls_back(self):
        original = schema._EVIDENCE_SCHEMA_DDL
        for failed_at in range(len(original)):
            with self.subTest(failed_at=failed_at):
                conn = self._legacy_v13()
                ddl = list(original)
                ddl[failed_at] = "CREATE TABLE definitely_invalid_v14("
                schema._EVIDENCE_SCHEMA_DDL = tuple(ddl)
                try:
                    with self.assertRaises(sqlite3.Error):
                        schema._migrate_v14(conn)
                    self.assertEqual(
                        conn.execute(
                            "SELECT value FROM meta WHERE key='schema_version'"
                        ).fetchone()[0],
                        "13",
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                            "AND name IN ('evidence','episode_evidence','memory_evidence')"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    schema._EVIDENCE_SCHEMA_DDL = original
                    conn.close()

    def test_v14_version_update_failure_rolls_back(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE meta SET value='14'"):
                    raise sqlite3.OperationalError("injected v14 version failure")
                return super().execute(sql, parameters)

        conn = sqlite3.connect(
            self.root / "migration-version.sqlite", factory=FailingConnection
        )
        schema.init_db(conn)
        conn.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
        conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            schema._migrate_v14(conn)
        self.assertEqual(
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            "13",
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='evidence'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_caller_transaction_owns_v14_commit(self):
        conn = self._legacy_v13()
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE caller_sentinel(value TEXT)")
        schema._migrate_v14(conn)
        self.assertTrue(conn.in_transaction)
        self.assertEqual(
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            "14",
        )
        conn.rollback()
        self.assertEqual(
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            "13",
        )
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'"
        ).fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='caller_sentinel'"
        ).fetchone())
        conn.close()

    def test_failed_v14_migration_preserves_open_caller_transaction(self):
        conn = self._legacy_v13()
        conn.execute("CREATE TABLE caller_sentinel(value TEXT)")
        conn.commit()
        before_tables = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        before_version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_sentinel(value) VALUES ('keep')")
        original = schema._EVIDENCE_SCHEMA_DDL
        ddl = list(original)
        ddl[1] = "CREATE TABLE definitely_invalid_v14("
        schema._EVIDENCE_SCHEMA_DDL = tuple(ddl)
        try:
            with self.assertRaises(sqlite3.Error):
                schema._migrate_v14(conn)
        finally:
            schema._EVIDENCE_SCHEMA_DDL = original
        self.assertEqual(
            conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall(),
            before_tables,
        )
        self.assertEqual(
            conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            before_version,
        )
        self.assertEqual(
            conn.execute("SELECT value FROM caller_sentinel").fetchone()[0],
            "keep",
        )
        conn.rollback()
        conn.close()


class EvidenceExportCompatibilityTest(_StoreCase):
    def test_partial_v14_schema_fails_closed(self):
        self.conn.execute("DROP TABLE memory_evidence")
        self.conn.commit()
        out = self.root / "partial.jsonl"
        with self.assertRaises(sqlite3.DatabaseError):
            sync.cmd_export_jsonl(self.conn, out=str(out))
        self.assertFalse(out.exists())

    def test_v14_marker_with_all_evidence_tables_missing_fails_closed(self):
        self.conn.execute("DROP TABLE episode_evidence")
        self.conn.execute("DROP TABLE memory_evidence")
        self.conn.execute("DROP TABLE evidence")
        self.conn.commit()
        out = self.root / "missing-v14.jsonl"
        with self.assertRaises(sqlite3.DatabaseError):
            sync.cmd_export_jsonl(self.conn, out=str(out))
        self.assertFalse(out.exists())
        self.assertFalse(self.conn.in_transaction)

        legacy = sqlite3.connect(self.root / "legacy-export.sqlite")
        schema.init_db(legacy)
        schema.migrate(legacy)
        legacy.execute("DROP TABLE episode_evidence")
        legacy.execute("DROP TABLE memory_evidence")
        legacy.execute("DROP TABLE evidence")
        legacy.execute(
            "UPDATE meta SET value='13' WHERE key='schema_version'"
        )
        legacy.commit()
        legacy_out = self.root / "legacy-export.jsonl"
        try:
            self.assertEqual(
                sync.cmd_export_jsonl(legacy, out=str(legacy_out)),
                0,
            )
            self.assertTrue(legacy_out.exists())
        finally:
            legacy.close()

    def test_unexpected_evidence_query_error_fails_closed(self):
        self.conn.execute("DROP TABLE evidence")
        self.conn.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY)")
        self.conn.commit()
        out = self.root / "malformed-schema.jsonl"
        with self.assertRaises(sqlite3.OperationalError):
            sync.cmd_export_jsonl(self.conn, out=str(out))
        self.assertFalse(out.exists())
        self.assertFalse(self.conn.in_transaction)

    def test_output_error_rolls_back_owned_snapshot(self):
        bad_parent = self.root / "not-a-directory"
        bad_parent.write_text("sentinel", encoding="utf-8")
        with self.assertRaises(OSError):
            sync.cmd_export_jsonl(
                self.conn, out=str(bad_parent / "export.jsonl")
            )
        self.assertFalse(self.conn.in_transaction)

    def test_scoped_export_keeps_association_closure(self):
        selected_memory = "00000000-0000-4000-8000-000000000801"
        outside_memory = "00000000-0000-4000-8000-000000000802"
        selected_episode = "00000000-0000-4000-8000-000000000803"
        outside_episode = "00000000-0000-4000-8000-000000000804"
        self.conn.executemany(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES (?, ?, 'fact', ?, '2026-09-10T00:00:00Z')",
            [
                (selected_memory, "project:selected", "selected"),
                (outside_memory, "project:outside", "outside"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO episode(id, namespace, started_at) VALUES (?, ?, ?)",
            [
                (selected_episode, "project:selected", "2026-09-10T00:00:00Z"),
                (outside_episode, "project:outside", "2026-09-10T00:00:00Z"),
            ],
        )
        evidence.write_evidence(
            self.conn, session_id="s", lane="codex", moment="user_prompt",
            kind="turn", ts="2026-09-10T00:00:00Z", excerpt="shared",
            ref_path="x", ref_offset=0,
            id="00000000-0000-4000-8000-000000000805",
        )
        evidence.write_evidence(
            self.conn, session_id="s", lane="codex", moment="user_prompt",
            kind="turn", ts="2026-09-10T00:00:01Z", excerpt="outside",
            ref_path="x", ref_offset=0,
            id="00000000-0000-4000-8000-000000000806",
        )
        self.conn.executemany(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            [
                (selected_memory, "00000000-0000-4000-8000-000000000805"),
                (outside_memory, "00000000-0000-4000-8000-000000000805"),
                (outside_memory, "00000000-0000-4000-8000-000000000806"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO episode_evidence(episode_id, evidence_id) VALUES (?, ?)",
            [
                (selected_episode, "00000000-0000-4000-8000-000000000805"),
                (outside_episode, "00000000-0000-4000-8000-000000000805"),
            ],
        )
        self.conn.commit()
        out = self.root / "scoped.jsonl"
        self.assertEqual(
            sync.cmd_export_jsonl(
                self.conn, out=str(out), namespace="project:selected"
            ),
            0,
        )
        records = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertIn(selected_memory, {r.get("id") for r in records})
        self.assertNotIn(outside_memory, {r.get("id") for r in records})
        self.assertIn(selected_episode, {r.get("id") for r in records})
        self.assertNotIn(outside_episode, {r.get("id") for r in records})
        assoc = [r for r in records if r.get("table") in {
            "episode_evidence", "memory_evidence"
        }]
        self.assertTrue(assoc)
        self.assertTrue(all(
            r.get("memory_id", selected_memory) == selected_memory
            and r.get("episode_id", selected_episode) == selected_episode
            for r in assoc
        ))
        self.assertNotIn(
            "00000000-0000-4000-8000-000000000806",
            {r.get("id") for r in records},
        )


class EvidenceRetentionTest(_StoreCase):
    def _write(self, evidence_id: str, ts: str) -> None:
        evidence.write_evidence(
            self.conn, session_id="s", lane="codex", moment="user_prompt",
            kind="turn", ts=ts, excerpt=evidence_id, ref_path="x", ref_offset=0,
            id=evidence_id,
        )

    def test_strict_expiry_boundary(self):
        self._write("00000000-0000-4000-8000-000000000501", "2026-08-10T23:59:59Z")
        self._write("00000000-0000-4000-8000-000000000502", "2026-08-11T00:00:00Z")
        self.conn.commit()
        self.assertEqual(
            evidence.sweep_evidence(self.conn, now_ts="2026-09-10T00:00:00Z")["expired"],
            1,
        )
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000000502",),
        ).fetchone())

    def test_year_one_zero_day_cutoff_is_zero_padded_and_valid(self):
        os.environ["ZMEM_EVIDENCE_DAYS"] = "0"
        self._write("00000000-0000-4000-8000-000000000503",
                    "0001-01-01T00:00:00Z")
        self.conn.commit()
        result = evidence.sweep_evidence(
            self.conn, now_ts="0001-01-01T00:00:00Z"
        )
        self.assertEqual(result["expired"], 0)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000000503",),
        ).fetchone())

    def test_cutoff_crossing_year_0999_remains_lexically_padded(self):
        os.environ["ZMEM_EVIDENCE_DAYS"] = "1"
        self._write("00000000-0000-4000-8000-000000000504",
                    "0999-12-30T23:59:59Z")
        self._write("00000000-0000-4000-8000-000000000505",
                    "0999-12-31T00:00:00Z")
        self.conn.commit()
        result = evidence.sweep_evidence(
            self.conn, now_ts="1000-01-01T00:00:00Z"
        )
        self.assertEqual(result["expired"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000000504",),
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000000505",),
        ).fetchone())

    def test_cap_orders_newest_then_id(self):
        os.environ["ZMEM_EVIDENCE_DAYS"] = "0"
        os.environ["ZMEM_EVIDENCE_CAP"] = "2"
        for suffix in ("601", "602", "603"):
            self._write(
                "00000000-0000-4000-8000-000000000" + suffix,
                "2026-09-10T00:00:00Z",
            )
        self.conn.commit()
        result = evidence.sweep_evidence(self.conn, now_ts="2026-09-10T00:00:00Z")
        self.assertEqual(result["capped"], 1)
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT id FROM evidence ORDER BY id")],
            [
                "00000000-0000-4000-8000-000000000602",
                "00000000-0000-4000-8000-000000000603",
            ],
        )

    def test_invalid_env_disables_sweep_without_mutation(self):
        memory_id = "00000000-0000-4000-8000-000000000506"
        evidence_id = "00000000-0000-4000-8000-000000000507"
        self.conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES (?, 'project:p', 'fact', 'retention', ?)",
            (memory_id, "2026-09-10T00:00:00Z"),
        )
        self._write(evidence_id, "2026-08-01T00:00:00Z")
        self.conn.execute(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        self.conn.commit()
        os.environ["ZMEM_EVIDENCE_DAYS"] = "abc"
        with contextlib.redirect_stderr(__import__("io").StringIO()) as err:
            result = evidence.sweep_evidence(
                self.conn, now_ts="2026-09-10T00:00:00Z"
            )
        self.assertEqual(result, {
            "expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0,
        })
        self.assertEqual(
            err.getvalue(),
            "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n",
        )
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM memory_evidence WHERE memory_id=? AND evidence_id=?",
            (memory_id, evidence_id),
        ).fetchone())
        self.assertFalse(self.conn.in_transaction)

    def test_sql_failure_rolls_back_evidence_and_links(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "DELETE FROM evidence WHERE ts < ?":
                    raise sqlite3.OperationalError("injected retention failure")
                return super().execute(sql, parameters)

        conn = sqlite3.connect(
            self.root / "retention-failure.sqlite", factory=FailingConnection
        )
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        memory_id = "00000000-0000-4000-8000-000000000901"
        evidence_id = "00000000-0000-4000-8000-000000000902"
        conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES (?, 'project:p', 'fact', 'retention', ?)",
            (memory_id, "2026-09-10T00:00:00Z"),
        )
        evidence.write_evidence(
            conn, session_id="s", lane="codex", moment="user_prompt",
            kind="turn", ts="2026-09-09T00:00:00Z", excerpt="old",
            ref_path="x", ref_offset=0, id=evidence_id,
        )
        conn.execute(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        conn.commit()
        os.environ["ZMEM_EVIDENCE_DAYS"] = "0"
        result = evidence.sweep_evidence(
            conn, now_ts="2026-09-10T00:00:00Z"
        )
        self.assertEqual(result, {
            "expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0,
        })
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM memory_evidence WHERE memory_id=? AND evidence_id=?",
            (memory_id, evidence_id),
        ).fetchone())
        conn.close()

    def test_unrepresentable_limits_disable_without_crashing(self):
        zero = {
            "expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0,
        }
        for env_name, env_value, now_ts, warning in (
            (
                "ZMEM_EVIDENCE_DAYS", str(2**63),
                "2026-09-10T00:00:00Z",
                "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n",
            ),
            (
                "ZMEM_EVIDENCE_DAYS", None,
                "0001-01-01T00:00:00Z",
                "",
            ),
            (
                "ZMEM_EVIDENCE_CAP", str(2**63),
                "2026-09-10T00:00:00Z",
                "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP\n",
            ),
        ):
            with self.subTest(env_name=env_name, env_value=env_value):
                os.environ.pop("ZMEM_EVIDENCE_DAYS", None)
                os.environ.pop("ZMEM_EVIDENCE_CAP", None)
                if env_value is not None:
                    os.environ[env_name] = env_value
                with contextlib.redirect_stderr(__import__("io").StringIO()) as err:
                    result = evidence.sweep_evidence(self.conn, now_ts=now_ts)
                self.assertEqual(result, zero)
                self.assertEqual(err.getvalue(), warning)


class EvidenceJsonlTest(_StoreCase):
    def test_strict_timestamp_and_offset_match_writer_contract(self):
        for index, ts in enumerate((
            "2026-9-10T00:00:00Z", "2026-09-10T00:00:60Z",
        ), start=1):
            row = {
                "table": "evidence", "id": f"00000000-0000-4000-8000-0000000007{20 + index:02d}",
                "session_id": "s", "lane": "codex", "moment": "user_prompt",
                "kind": "turn", "ts": ts, "excerpt": "safe", "ref_path": "x",
                "ref_offset": 0,
            }
            row["hash"] = hashlib.sha256(
                f"turn|{ts}|safe".encode()
            ).hexdigest()
            path = self.root / f"bad-ts-{index}.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertNotEqual(
                sync.cmd_ingest_jsonl_strict(
                    self.conn, in_path=str(path), source_ref=None,
                ),
                0,
            )
        row = {
            "table": "evidence", "id": "00000000-0000-4000-8000-000000000724",
            "session_id": "s", "lane": "codex", "moment": "user_prompt",
            "kind": "turn", "ts": "2026-09-10T00:00:00Z", "excerpt": "safe",
            "ref_path": "x", "ref_offset": 2**63,
        }
        row["hash"] = hashlib.sha256(
            b"turn|2026-09-10T00:00:00Z|safe"
        ).hexdigest()
        path = self.root / "bad-offset.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertNotEqual(
            sync.cmd_ingest_jsonl_strict(
                self.conn, in_path=str(path), source_ref=None,
            ),
            0,
        )

    def test_strict_roundtrip_preserves_near_duplicate_parents_and_links(self):
        memory_one = "00000000-0000-4000-8000-000000000711"
        memory_two = "00000000-0000-4000-8000-000000000712"
        episode_id = "00000000-0000-4000-8000-000000000713"
        evidence_id = "00000000-0000-4000-8000-000000000714"
        ts = "2026-09-10T00:00:00Z"
        digest = hashlib.sha256(f"turn|{ts}|trace".encode()).hexdigest()
        rows = [
            {
                "kind": "memory", "id": memory_one, "namespace": "project:p",
                "type": "fact", "content": "near duplicate parent one",
                "tags": "", "source_ref": "", "signal": "test",
                "confidence": 0.9, "ingestion_ts": ts, "links": [],
            },
            {
                "kind": "memory", "id": memory_two, "namespace": "project:p",
                "type": "fact", "content": "near duplicate parent one!",
                "tags": "", "source_ref": "", "signal": "test",
                "confidence": 0.9, "ingestion_ts": ts, "links": [],
            },
            {
                "kind": "episode_memory", "episode_id": episode_id,
                "memory_id": memory_two, "added_at": ts,
            },
            {
                "kind": "episode", "id": episode_id, "namespace": "project:p",
                "started_at": ts, "ended_at": ts,
                "summary_memory_id": memory_one, "token_count": 2,
            },
            {
                "table": "evidence", "id": evidence_id, "session_id": "s",
                "lane": "codex", "moment": "user_prompt", "kind": "turn",
                "ts": ts, "hash": digest, "excerpt": "trace",
                "ref_path": "session.txt", "ref_offset": 0,
            },
            {"table": "memory_evidence", "memory_id": memory_one,
             "evidence_id": evidence_id},
            {"table": "memory_evidence", "memory_id": memory_two,
             "evidence_id": evidence_id},
            {"table": "episode_evidence", "episode_id": episode_id,
             "evidence_id": evidence_id},
        ]
        path = self.root / "mixed.jsonl"
        path.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        self.assertEqual(
            sync.cmd_ingest_jsonl(self.conn, in_path=str(path), source_ref=None),
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM memory WHERE id IN (?, ?)",
                (memory_one, memory_two),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM memory_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM episode_memory WHERE episode_id=?",
                (episode_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM episode_evidence WHERE episode_id=?",
                (episode_id,),
            ).fetchone()[0],
            1,
        )

    def test_strict_bad_later_row_rolls_back(self):
        good = {
            "table": "evidence", "id": "00000000-0000-4000-8000-000000000701",
            "session_id": "s", "lane": "codex", "moment": "user_prompt",
            "kind": "turn", "ts": "2026-09-10T00:00:00Z",
            "hash": hashlib.sha256(b"turn|2026-09-10T00:00:00Z|safe").hexdigest(),
            "excerpt": "safe", "ref_path": "x", "ref_offset": 0,
        }
        path = self.root / "bad.jsonl"
        path.write_text(
            json.dumps(good, separators=(",", ":")) + "\n"
            + '{"table":"evidence","id":"bad"}\n',
            encoding="utf-8",
        )
        self.assertNotEqual(
            sync.cmd_ingest_jsonl(
                self.conn, in_path=str(path), source_ref=None,
            ),
            0,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
