"""Additional integration checks for the scoped recall tier boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
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
    "fleet": "fleet:dgx",
    "host": "host:spark1",
}


class ScopedTierIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-recall-tier-additional-")
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "store.sqlite"))
        self.conn.row_factory = sqlite3.Row
        schema_mod.init_db(self.conn)
        rows = (
            ("project", "project:demo", "2026-09-24T00:01:00Z"),
            ("domain", "domain:demo", "2026-09-24T00:02:00Z"),
            ("fleet", "fleet:dgx", "2026-09-24T00:03:00Z"),
            ("host", "host:spark1", "2026-09-24T00:04:00Z"),
            ("global", "user:global", "2026-09-24T00:05:00Z"),
        )
        for key, namespace, ingestion_ts in rows:
            self.conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    confidence, signal, valid_from, ingestion_ts)
                   VALUES (?, ?, 'lesson', ?, 'test', ?, 0.9, 'test', ?, ?)""",
                (f"tier-{key}", namespace, f"tier {key} recall", f"test:{key}",
                 ingestion_ts, ingestion_ts),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_recent_uses_the_same_reservation_order_and_global_gate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rows = recall_mod.recent_memory(
                self.conn,
                scopes=SCOPES,
                include_global=True,
                min_confidence=0.0,
                no_bump=True,
                no_telemetry=True,
            )
        self.assertEqual(
            [row["id"] for row in rows],
            ["tier-project", "tier-domain", "tier-host", "tier-fleet",
             "tier-global"],
        )
        self.assertEqual(
            [row["tier"] for row in rows],
            ["project", "domain", "fleet_host", "fleet_host", "user_global"],
        )

        with contextlib.redirect_stdout(io.StringIO()):
            without_global = recall_mod.recent_memory(
                self.conn,
                scopes=SCOPES,
                min_confidence=0.0,
                no_bump=True,
                no_telemetry=True,
            )
        self.assertNotIn("tier-global", [row["id"] for row in without_global])

    def test_scoped_tiers_preserve_moment_profile_ranking(self):
        """The #126 profile must reach #167's reserved project pool."""
        self.conn.executemany(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref,
                confidence, signal, valid_from, ingestion_ts)
               VALUES (?, 'project:demo', ?, 'profile context query',
                       'test', ?, 0.9, 'test', ?, ?)""",
            (
                ("tier-profile-fact", "fact", "test:profile-fact",
                 "2026-09-24T00:06:00Z", "2026-09-24T00:06:00Z"),
                ("tier-profile-constraint", "constraint", "test:profile-constraint",
                 "2026-09-24T00:06:00Z", "2026-09-24T00:06:00Z"),
            ),
        )
        self.conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            rows = recall_mod.recall_memory(
                self.conn,
                query="profile context query",
                scopes=SCOPES,
                min_confidence=0.0,
                no_bump=True,
                no_telemetry=True,
                no_mmr=True,
                moment="pretool",
                lane="codex",
            )
        profile_ids = [
            row["id"] for row in rows
            if row["id"] in {"tier-profile-fact", "tier-profile-constraint"}
        ]
        self.assertEqual(profile_ids, ["tier-profile-constraint", "tier-profile-fact"])
        self.assertTrue(all(row["tier"] == "project" for row in rows if row["id"] in profile_ids))

    def test_invalid_slots_fail_before_sql_or_telemetry(self):
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        with mock.patch.dict(os.environ, {"ZMEM_TIER_SLOTS": "1,2,bad,4,5"}), \
                mock.patch.object(recall_mod, "_bump_telemetry") as bump:
            with self.assertRaises(ValueError):
                recall_mod.recall_memory(
                    self.conn,
                    query="tier recall",
                    scopes=SCOPES,
                    min_confidence=0.0,
                    no_bump=True,
                    no_telemetry=True,
                )
        self.assertEqual(statements, [])
        bump.assert_not_called()

    def test_scoped_injection_refusal_does_not_enter_collector(self):
        with mock.patch.object(
                recall_mod, "_collect_injection_candidates") as collector:
            with self.assertRaisesRegex(ValueError, "scoped recall"):
                recall_mod.recall_memory(
                    self.conn,
                    query="tier recall",
                    scopes=SCOPES,
                    for_injection=True,
                )
            collector.assert_not_called()
        with mock.patch.object(
                recall_mod, "_collect_injection_candidates") as collector:
            with self.assertRaisesRegex(ValueError, "scoped recent"):
                recall_mod.recent_memory(
                    self.conn,
                    scopes=SCOPES,
                    for_injection=True,
                )
            collector.assert_not_called()

    def test_scoped_legacy_cross_flag_refuses_before_sql(self):
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        with self.assertRaisesRegex(ValueError, "scoped recall.*include_cross_project"):
            recall_mod.recall_memory(
                self.conn, query="tier recall", scopes=SCOPES,
                include_cross_project=True, no_bump=True,
            )
        with self.assertRaisesRegex(ValueError, "scoped recent.*include_cross_project"):
            recall_mod.recent_memory(
                self.conn, scopes=SCOPES, include_cross_project=True,
                no_bump=True,
            )
        self.assertEqual(statements, [])

    def test_duplicate_ids_do_not_consume_a_later_tier_slot(self):
        tiers = {
            "project": [(1.0, {"id": "shared"})],
            "domain": [
                (0.9, {"id": "shared"}),
                (0.8, {"id": "domain-one"}),
                (0.7, {"id": "domain-two"}),
            ],
        }
        slots = {
            "project": 1,
            "domain": 2,
            "fleet_host": 0,
            "cross_project": 0,
            "user_global": 0,
        }
        rows = recall_mod._merge_reserved_tiers(tiers, slots)
        self.assertEqual([row["id"] for row in rows],
                         ["shared", "domain-one", "domain-two"])

    def test_implicit_cli_recall_uses_resolved_project_tier(self):
        import host as host_mod

        project_env = dict(os.environ)
        project_env.pop("ZMEM_FLEET", None)
        resolved = host_mod.resolve_scopes(
            project_dir=REPO_ROOT,
            hostname="tier-test-host",
            env=project_env,
            hermes_kwargs={},
        )
        project_namespace = resolved["project"]
        self.conn.execute(
            "UPDATE memory SET id='implicit-project', namespace=?, "
            "content='implicit scoped routing note' WHERE id='tier-project'",
            (project_namespace,),
        )
        self.conn.commit()

        env = dict(project_env)
        env.update({
            "ZMEM_STORE": str(Path(self.tmp.name) / "store.sqlite"),
            "ZMEM_DATA": self.tmp.name,
            "ZMEM_MODELS_DIR": str(Path(self.tmp.name) / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"),
             "recall", "--query", "implicit scoped routing", "--json",
             "--no-bump", "--no-hybrid", "--no-mmr"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        rows = envelope["results"]
        self.assertEqual([row["id"] for row in rows], ["implicit-project"])
        self.assertEqual(rows[0]["tier"], "project")

    def test_cli_cross_policy_keeps_scope_and_dispatch_consistent(self):
        """Env-enabled implicit scopes must not reach the legacy cross guard."""
        store = Path(self.tmp.name) / "store.sqlite"
        env = dict(os.environ)
        env.update({
            "ZMEM_STORE": str(store),
            "ZMEM_DATA": self.tmp.name,
            "ZMEM_MODELS_DIR": str(Path(self.tmp.name) / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_CROSS_PROJECT": "1",
        })
        store_py = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
        commands = (
            ("recall", "--query", "cross policy", "--json", "--no-bump",
             "--no-hybrid", "--no-mmr"),
            ("recent", "--json", "--no-bump"),
        )
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(store_py), *command], cwd=str(REPO_ROOT),
                env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

        # The default policy enables cross-project only on pretool. This had
        # the same raw/effective mismatch as ZMEM_CROSS_PROJECT=1.
        env.pop("ZMEM_CROSS_PROJECT")
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(store_py), *command, "--moment",
                 "pretool", "--session-id", "cross-policy-pretool"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

        # The explicit flag continues to select the legacy unscoped lane even
        # when the policy kill switch makes its effective dispatch value false.
        env["ZMEM_CROSS_PROJECT"] = "0"
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(store_py), *command,
                 "--include-cross-project"], cwd=str(REPO_ROOT), env=env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()
