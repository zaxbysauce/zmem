"""Frozen acceptance tests for issue #167's five-tier scoped recall contract.

Run from the repository root:
    python tests/fixtures/recall_tiers/build_fixture.py
    python -m unittest tests/test_recall_tiers.py

The tests use a throwaway SQLite store and do not touch ~/.zmem.  They are
intentionally red on the two-tier base commit until issue #167 is implemented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "recall_tiers"
QUERY = "reserved tier recall acceptance candidate"
SCOPES = {
    "project": "project:demo",
    "domain": "domain:demo",
    "fleet": "fleet:dgx",
    "host": "host:spark1",
}
TS = "2026-01-01T00:00:00Z"

# Pin store resolution before importing storelib.  This mirrors the hermetic
# fixture convention used by test_cross_project_lane.py.
_BOOT_TMP = tempfile.mkdtemp(prefix="zmem-recall-tiers-boot-")
os.environ["ZMEM_STORE"] = os.path.join(_BOOT_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _BOOT_TMP
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_BOOT_TMP, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ.pop("ZMEM_TIER_SLOTS", None)
sys.path.insert(0, str(SCRIPTS_DIR))

from storelib import cli as cli_mod  # noqa: E402
from storelib import recall as recall_mod  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def _open_store(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    schema_mod.init_db(conn)
    return conn


def _seed_fixture(conn: sqlite3.Connection) -> None:
    rows = json.loads((FIXTURE_DIR / "reserved_slots.json").read_text(
        encoding="utf-8"))
    for row in rows:
        conn.execute(
            "INSERT INTO memory "
            "(id, namespace, type, content, tags, source_ref, confidence, "
            "signal, valid_from, ingestion_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["namespace"], row["type"], row["content"],
                row["tags"], row["source_ref"], row["confidence"],
                row["signal"], row["valid_from"], row["ingestion_ts"],
            ),
        )
    conn.commit()


def _fixture_key(row: dict) -> str:
    return str(row["source_ref"]).removeprefix("fixture:")


def _projection(rows: list[dict]) -> dict:
    counts = {
        "project": 0,
        "domain": 0,
        "fleet_host": 0,
        "cross_project": 0,
        "user_global": 0,
    }
    projected = []
    for row in rows:
        tier = row.get("tier")
        if tier in counts:
            counts[tier] += 1
        projected.append({"key": _fixture_key(row), "tier": tier})
    return {"counts": counts, "rows": projected}


class RecallTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zmem-recall-tiers-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.conn = _open_store(self.store)
        _seed_fixture(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scoped_recall(self) -> list[dict]:
        return recall_mod.recall_memory(
            self.conn,
            query=QUERY,
            include_global=True,
            min_confidence=0.0,
            hybrid=False,
            no_mmr=True,
            no_bump=True,
            no_telemetry=True,
            no_unfold=True,
            link_hops=0,
            link_budget=0,
            scopes=SCOPES,
        )

    def _assert_projection(self, rows: list[dict], filename: str) -> None:
        actual = _compact(_projection(rows))
        expected = (FIXTURE_DIR / filename).read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_reserved_slots_without_cross_project(self):
        rows = self._scoped_recall()
        self._assert_projection(rows, "expected_without_cross.json")

    def test_cross_project_predicate_fills_reserved_slots(self):
        def eligible(row: dict, scopes: dict[str, str]) -> bool:
            del scopes
            return _fixture_key(row) in {"cross-01", "cross-02"}

        with mock.patch.object(
            recall_mod, "_cross_project_eligible", side_effect=eligible
        ):
            rows = self._scoped_recall()
        self._assert_projection(rows, "expected_with_cross.json")

    def test_fleet_score_cannot_displace_project(self):
        rows = self._scoped_recall()
        project_keys = [
            _fixture_key(row) for row in rows if row.get("tier") == "project"
        ]
        self.assertEqual(
            project_keys,
            ["project-01", "project-02", "project-03", "project-04",
             "project-05"],
        )
        self.assertNotIn("project-06", project_keys)

    def test_fenced_rows_have_tier_labels(self):
        rows = []
        for tier in ("project", "domain", "fleet_host",
                     "cross_project", "user_global"):
            rows.append({
                "id": f"{tier}-id",
                "confidence": 0.9,
                "signal": "test",
                "namespace": f"{tier}:fixture",
                "type": "lesson",
                "content": "scoped row",
                "tier": tier,
            })
        rows.append({
            "id": "unknown-id",
            "confidence": 0.9,
            "signal": "test",
            "namespace": "project:fixture",
            "type": "lesson",
            "content": "legacy row",
        })
        rendered = recall_mod._format_fenced_recall(rows, "fixture")
        for tier in ("project", "domain", "fleet_host",
                     "cross_project", "user_global"):
            self.assertIn(
                f"- [tier={tier}] [{tier}-id]",
                rendered,
            )
        self.assertIn("- [tier=unknown] [unknown-id]", rendered)

    def test_invalid_tier_slots_fail_closed(self):
        expected = {
            "project": 5,
            "domain": 2,
            "fleet_host": 2,
            "cross_project": 2,
            "user_global": 3,
        }
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_TIER_SLOTS", None)
            self.assertEqual(recall_mod._tier_slots(), expected)
        with mock.patch.dict(os.environ, {"ZMEM_TIER_SLOTS": "0,1,0,2,3"}):
            self.assertEqual(
                recall_mod._tier_slots(),
                {
                    "project": 0,
                    "domain": 1,
                    "fleet_host": 0,
                    "cross_project": 2,
                    "user_global": 3,
                },
            )
        for raw in ("", "-1,2,2,2,3", "1,2", "1,2,3,4,5,6",
                    "1,a,2,3,4", "１,2,3,4,5"):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"ZMEM_TIER_SLOTS": raw}
            ):
                with self.assertRaises(ValueError):
                    recall_mod._tier_slots()

    def test_legacy_two_tier_call_is_unchanged(self):
        rows = recall_mod.recall_memory(
            self.conn,
            query=QUERY,
            namespace="project:demo",
            limit=5,
            include_global=True,
            global_limit=3,
            min_confidence=0.0,
            hybrid=False,
            no_mmr=True,
            no_bump=True,
            no_telemetry=True,
            no_unfold=True,
            link_hops=0,
            link_budget=0,
        )
        self.assertEqual(
            [row["namespace"] for row in rows[:5]],
            ["project:demo"] * 5,
        )
        self.assertEqual(
            [row["namespace"] for row in rows[5:]],
            ["user:global"] * 3,
        )
        self.assertTrue(all("tier" not in row for row in rows))


    def test_unscoped_call_remains_flat(self):
        rows = recall_mod.recall_memory(
            self.conn,
            query=QUERY,
            limit=5,
            include_global=True,
            global_limit=3,
            min_confidence=0.0,
            hybrid=False,
            no_mmr=True,
            no_bump=True,
            no_telemetry=True,
            no_unfold=True,
            link_hops=0,
            link_budget=0,
        )
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(all("tier" not in row for row in rows))

class ExplainTierOverflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zmem-recall-explain-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.conn = _open_store(self.store)
        _seed_fixture(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tier_slot_exhausted(self):
        before = hashlib.sha256(Path(self.store).read_bytes()).digest()
        verdicts = recall_mod.explain_recall(
            self.conn,
            query=QUERY,
            target="project-06",
            include_global=True,
            min_confidence=0.0,
            hybrid=False,
            no_mmr=True,
            no_bump=True,
            link_hops=0,
            link_budget=0,
            scopes=SCOPES,
        )
        after = hashlib.sha256(Path(self.store).read_bytes()).digest()
        self.assertEqual(before, after)
        verdict = next(item for item in verdicts if item["id"].endswith("0006"))
        self.assertEqual(verdict["reason"], "tier_slot_exhausted")
        self.assertEqual(
            json.dumps(verdict["detail"], sort_keys=True, separators=(",", ":")),
            '{"score":0.1,"slot":5,"tier":"project"}',
        )
        self.assertIn("tier_slot_exhausted", recall_mod.EXPLAIN_REASONS)


class CliScopePropagationTests(unittest.TestCase):
    def test_explicit_namespace_classification(self):
        self.assertEqual(
            cli_mod._recall_scopes(argparse.Namespace(namespace="project:demo")),
            {"project": "project:demo"},
        )
        self.assertEqual(
            cli_mod._recall_scopes(argparse.Namespace(namespace="user:global")),
            {"user_global": "user:global"},
        )
        self.assertEqual(
            cli_mod._recall_scopes(argparse.Namespace(namespace="domain:demo")),
            {"project": "domain:demo"},
        )

    def test_implicit_scopes_delegate_to_issue_166_resolver(self):
        resolved = {
            "project": "project:demo",
            "domain": "domain:demo",
            "fleet": "fleet:dgx",
            "host": "host:spark1",
        }
        with mock.patch.object(
            cli_mod._schema_host, "resolve_scopes", return_value=resolved
        ) as resolver:
            actual = cli_mod._recall_scopes(
                argparse.Namespace(namespace=None)
            )
        self.assertEqual(actual, resolved)
        resolver.assert_called_once_with(
            project_dir=Path.cwd(),
            hostname=socket.gethostname(),
            env=os.environ,
            hermes_kwargs={},
        )


class CrossProjectCompatibilityTests(unittest.TestCase):
    def test_legacy_cross_project_marker_remains_legacy(self):
        row = {
            "id": "cross-id",
            "confidence": 0.9,
            "signal": "test",
            "namespace": "project:foreign",
            "type": "lesson",
            "content": "legacy hazard lane row",
            "tier": "cross",
        }
        rendered = recall_mod._format_fenced_recall([row], "fixture")
        self.assertIn(
            "- [cross-id] [conf=0.9] [signal=test] "
            "[ns=project:foreign] [tier=cross] [type=lesson]",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
