"""Issue #170 retention contract tests.

These tests seed evidence and association rows before every destructive-path
probe so an invalid operator setting cannot pass by observing an empty store.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
from storelib import evidence, schema  # noqa: E402


ZERO = {"expired": 0, "capped": 0, "episode_links": 0, "memory_links": 0}


class EvidenceRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-170-retention-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_EVIDENCE_DAYS", "ZMEM_EVIDENCE_CAP"):
            os.environ.pop(key, None)
        os.environ.update(
            ZMEM_STORE=str(self.root / "store.sqlite"),
            ZMEM_DATA=str(self.root),
        )
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def _seed(self) -> tuple[str, str, str]:
        memory_id = "00000000-0000-4000-8000-000000001701"
        episode_id = "00000000-0000-4000-8000-000000001702"
        evidence_id = "00000000-0000-4000-8000-000000001703"
        self.conn.execute(
            "INSERT INTO memory(id, namespace, type, content, ingestion_ts) "
            "VALUES (?, 'project:issue170', 'fact', 'retention fixture', ?)",
            (memory_id, "2026-09-10T00:00:00Z"),
        )
        self.conn.execute(
            "INSERT INTO episode(id, namespace, started_at) VALUES (?, ?, ?)",
            (episode_id, "project:issue170", "2026-09-10T00:00:00Z"),
        )
        evidence.write_evidence(
            self.conn,
            session_id="issue170",
            lane="codex",
            moment="user_prompt",
            kind="turn",
            ts="2026-08-01T00:00:00Z",
            excerpt="old evidence",
            ref_path="issue170.txt",
            ref_offset=0,
            id=evidence_id,
        )
        self.conn.execute(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        self.conn.execute(
            "INSERT INTO episode_evidence(episode_id, evidence_id) VALUES (?, ?)",
            (episode_id, evidence_id),
        )
        self.conn.commit()
        return memory_id, episode_id, evidence_id

    def _counts(self) -> tuple[int, int, int, int]:
        return tuple(
            self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("evidence", "memory_evidence", "episode_evidence", "memory")
        )

    def test_strict_expiry_boundary(self):
        os.environ["ZMEM_EVIDENCE_DAYS"] = "1"
        for suffix, ts in (("001", "2026-09-08T23:59:59Z"),
                           ("002", "2026-09-09T00:00:00Z")):
            evidence.write_evidence(
                self.conn, session_id="issue170", lane="codex", moment="pretool",
                kind="tool_call", ts=ts, excerpt=suffix, ref_path="x",
                ref_offset=0, id=f"00000000-0000-4000-8000-000000001{suffix}",
            )
        self.conn.commit()
        result = evidence.sweep_evidence(self.conn, now_ts="2026-09-10T00:00:00Z")
        self.assertEqual(result["expired"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000001001",),
        ).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?",
            ("00000000-0000-4000-8000-000000001002",),
        ).fetchone())

    def test_cap_orders_newest_then_id(self):
        os.environ.update(ZMEM_EVIDENCE_DAYS="0", ZMEM_EVIDENCE_CAP="2")
        for suffix in ("001", "002", "003"):
            evidence.write_evidence(
                self.conn, session_id="issue170", lane="codex", moment="pretool",
                kind="tool_call", ts="2026-09-10T00:00:00Z", excerpt=suffix,
                ref_path="x", ref_offset=0,
                id=f"00000000-0000-4000-8000-000000001{suffix}",
            )
        self.conn.commit()
        result = evidence.sweep_evidence(self.conn, now_ts="2026-09-10T00:00:00Z")
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["capped"], 1)
        self.assertEqual(
            [row[0] for row in self.conn.execute("SELECT id FROM evidence ORDER BY id")],
            [
                "00000000-0000-4000-8000-000000001002",
                "00000000-0000-4000-8000-000000001003",
            ],
        )

    def test_join_rows_deleted_before_evidence(self):
        _, episode_id, evidence_id = self._seed()
        os.environ["ZMEM_EVIDENCE_DAYS"] = "0"
        result = evidence.sweep_evidence(self.conn, now_ts="2026-09-10T00:00:00Z")
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["memory_links"], 1)
        self.assertEqual(result["episode_links"], 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM episode_evidence WHERE episode_id=? AND evidence_id=?",
            (episode_id, evidence_id),
        ).fetchone())

    def test_invalid_env_disables_only_sweep(self):
        invalid_cases = (
            ("ZMEM_EVIDENCE_DAYS", "abc", "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n"),
            ("ZMEM_EVIDENCE_DAYS", "-1", "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n"),
            ("ZMEM_EVIDENCE_DAYS", str(2**63), "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n"),
            ("ZMEM_EVIDENCE_CAP", "abc", "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP\n"),
            ("ZMEM_EVIDENCE_CAP", "0", "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP\n"),
            ("ZMEM_EVIDENCE_CAP", str(2**63), "evidence retention disabled: invalid ZMEM_EVIDENCE_CAP\n"),
        )
        for env_name, raw, warning in invalid_cases:
            with self.subTest(env_name=env_name, raw=raw):
                self._seed()
                before = self._counts()
                os.environ[env_name] = raw
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = evidence.sweep_evidence(
                        self.conn, now_ts="2026-09-10T00:00:00Z"
                    )
                self.assertEqual(result, ZERO)
                self.assertEqual(stderr.getvalue(), warning)
                self.assertEqual(self._counts(), before)
                self.assertFalse(self.conn.in_transaction)
                self.conn.execute("DELETE FROM episode_evidence")
                self.conn.execute("DELETE FROM memory_evidence")
                self.conn.execute("DELETE FROM evidence")
                self.conn.execute("DELETE FROM episode")
                self.conn.execute("DELETE FROM memory")
                self.conn.commit()
                os.environ.pop(env_name, None)

        memory_id, episode_id, evidence_id = self._seed()
        os.environ.update(ZMEM_EVIDENCE_DAYS="bad", ZMEM_EVIDENCE_CAP="0")
        self.conn.execute("BEGIN")
        self.conn.execute(
            "UPDATE memory SET content='caller transaction' WHERE id=?", (memory_id,)
        )
        before = self._counts()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = evidence.sweep_evidence(
                self.conn, now_ts="2026-09-10T00:00:00Z"
            )
        self.assertEqual(result, ZERO)
        self.assertEqual(
            stderr.getvalue(),
            "evidence retention disabled: invalid ZMEM_EVIDENCE_DAYS\n",
        )
        self.assertEqual(self._counts(), before)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(
            self.conn.execute("SELECT content FROM memory WHERE id=?", (memory_id,)).fetchone()[0],
            "caller transaction",
        )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM episode_evidence WHERE episode_id=? AND evidence_id=?",
                              (episode_id, evidence_id)).fetchone()
        )
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
