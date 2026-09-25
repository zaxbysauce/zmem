"""Combined-path pins for the #126/#167 recall integration seams."""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import recall as recall_mod  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402


SCOPES = {
    "project": "project:demo",
    "domain": "domain:demo",
}


class CombinedScopedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-recall-main-integration-")
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "store.sqlite"))
        self.conn.row_factory = sqlite3.Row
        schema_mod.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _insert(
        self,
        memory_id: str,
        *,
        namespace: str,
        type_name: str,
        content: str,
        ingestion_ts: str,
    ) -> int:
        self.conn.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref,
                confidence, signal, valid_from, ingestion_ts)
               VALUES (?, ?, ?, ?, 'test', ?, 0.9, 'test', ?, ?)""",
            (
                memory_id,
                namespace,
                type_name,
                content,
                f"test:{memory_id}",
                ingestion_ts,
                ingestion_ts,
            ),
        )
        row = self.conn.execute(
            "SELECT rowid FROM memory WHERE id = ?", (memory_id,)
        ).fetchone()
        self.conn.commit()
        assert row is not None
        return int(row[0])

    def test_scoped_recent_applies_profile_then_rowid_tie_inside_reservation(self):
        """#126 ordering survives #167's scoped recent slot reservation."""
        self._insert(
            "constraint-old",
            namespace="project:demo",
            type_name="constraint",
            content="combined recent context",
            ingestion_ts="2026-09-24T00:00:00Z",
        )
        first_rowid = self._insert(
            "fact-a",
            namespace="project:demo",
            type_name="fact",
            content="combined recent context",
            ingestion_ts="2026-09-24T00:01:00Z",
        )
        later_rowid = self._insert(
            "fact-z",
            namespace="project:demo",
            type_name="fact",
            content="combined recent context",
            ingestion_ts="2026-09-24T00:01:00Z",
        )
        self.assertLess(first_rowid, later_rowid)

        with mock.patch.dict(os.environ, {"ZMEM_TIER_SLOTS": "2,0,0,0,0"}), \
                mock.patch.object(
                    recall_mod,
                    "type_preference",
                    wraps=recall_mod.type_preference,
                ) as type_preference, \
                contextlib.redirect_stdout(io.StringIO()):
            rows = recall_mod.recent_memory(
                self.conn,
                scopes=SCOPES,
                min_confidence=0.0,
                no_bump=True,
                no_telemetry=True,
                moment="pretool",
                lane="codex",
            )

        # The profile beats recency, while the two equal-profile facts use the
        # later SQLite arrival rowid as their deterministic tie-break. The
        # project reservation then admits exactly two rows.
        self.assertEqual([row["id"] for row in rows], ["constraint-old", "fact-z"])
        self.assertEqual([row["tier"] for row in rows], ["project", "project"])
        self.assertGreaterEqual(type_preference.call_count, 3)
        for call in type_preference.call_args_list:
            self.assertEqual(call.kwargs["moment"], "pretool")
            self.assertEqual(call.kwargs["lane"], "codex")

    def test_cross_rerank_receives_one_reserved_labeled_set_and_preserves_wire(self):
        """Rerank follows tier reservation and only changes presentation order."""
        self._insert(
            "project-tier",
            namespace="project:demo",
            type_name="lesson",
            content="combined reservation project",
            ingestion_ts="2026-09-24T00:01:00Z",
        )
        self._insert(
            "domain-tier",
            namespace="domain:demo",
            type_name="lesson",
            content="combined reservation domain",
            ingestion_ts="2026-09-24T00:02:00Z",
        )

        calls: list[tuple[str, list[tuple[str, str | None]]]] = []

        def rerank(query: str, rows: list[dict]) -> list[dict]:
            calls.append((query, [(row["id"], row.get("tier")) for row in rows]))
            return list(reversed(rows))

        rendered = io.StringIO()
        with mock.patch.dict(os.environ, {"ZMEM_TIER_SLOTS": "1,1,0,0,0"}), \
                mock.patch.object(
                    recall_mod, "_cross_maybe_rerank", side_effect=rerank
                ), \
                contextlib.redirect_stdout(rendered):
            rows = recall_mod.recall_memory(
                self.conn,
                query="combined reservation",
                scopes=SCOPES,
                min_confidence=0.0,
                hybrid=False,
                no_bump=True,
                no_telemetry=True,
                no_mmr=True,
                link_hops=0,
                link_budget=0,
                cross_rerank=True,
            )

        # A single call with both reserved tiers proves reranking is applied to
        # the merged presentation set, rather than independently per pool or
        # before reservation. The fake reverses order without changing rows.
        self.assertEqual(
            calls,
            [("combined reservation", [("project-tier", "project"),
                                       ("domain-tier", "domain")])],
        )
        self.assertEqual([row["id"] for row in rows], ["domain-tier", "project-tier"])
        self.assertEqual(
            {(row["id"], row["tier"]) for row in rows},
            {("project-tier", "project"), ("domain-tier", "domain")},
        )

        wire = rendered.getvalue()
        self.assertLess(wire.index("[tier=domain]"), wire.index("[tier=project]"))
        self.assertIn("[tier=domain] [domain-tier]", wire)
        self.assertIn("[tier=project] [project-tier]", wire)


if __name__ == "__main__":
    unittest.main()
