"""Additional integration checks for the scoped recall tier boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
_SAVED_SYS_PATH = list(sys.path)
sys.path.insert(0, str(SCRIPTS))
try:
    # Load the actual script-level module first; tests below call the resolver
    # directly after the temporary scripts path has been restored.
    import host as host_mod  # noqa: E402
    from storelib import recall as recall_mod  # noqa: E402
    from storelib import schema as schema_mod  # noqa: E402
finally:
    sys.path[:] = _SAVED_SYS_PATH


SCOPES = {
    "project": "project:demo",
    "domain": "domain:demo",
    "fleet": "fleet:dgx",
    "host": "host:spark1",
}


class ScopedTierIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tier_env = mock.patch.dict(os.environ)
        self._tier_env.start()
        os.environ.pop("ZMEM_TIER_SLOTS", None)
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
        self._tier_env.stop()

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

    def test_implicit_include_global_preserves_scoped_ids_and_tiers(self):
        project_env = dict(os.environ)
        project_env.pop("ZMEM_FLEET", None)
        project_namespace = host_mod.resolve_scopes(
            project_dir=REPO_ROOT, hostname="tier-test-host",
            env=project_env, hermes_kwargs={},
        )["project"]
        self.conn.execute(
            "UPDATE memory SET id='implicit-project', namespace=?, "
            "content='implicit scoped routing project note' WHERE id='tier-project'",
            (project_namespace,),
        )
        self.conn.execute(
            "UPDATE memory SET id='implicit-global', "
            "content='implicit scoped routing global note' WHERE id='tier-global'"
        )
        self.conn.execute(
            "DELETE FROM memory WHERE id NOT IN "
            "('implicit-project', 'implicit-global')"
        )
        self.conn.commit()

        env = dict(project_env)
        env.update({
            "ZMEM_STORE": str(Path(self.tmp.name) / "store.sqlite"),
            "ZMEM_DATA": self.tmp.name,
            "ZMEM_MODELS_DIR": str(Path(self.tmp.name) / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        store_py = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
        recall_result = subprocess.run(
            [sys.executable, str(store_py), "recall", "--query",
             "implicit scoped routing", "--include-global", "--json",
             "--no-bump", "--no-hybrid", "--no-mmr"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=60,
        )
        self.assertEqual(recall_result.returncode, 0, recall_result.stderr)
        recall_rows = json.loads(recall_result.stdout)["results"]
        self.assertEqual(
            [(row["id"], row["tier"]) for row in recall_rows],
            [("implicit-project", "project"),
             ("implicit-global", "user_global")],
        )

        recent_result = subprocess.run(
            [sys.executable, str(store_py), "recent", "--include-global",
             "--json", "--no-bump"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=60,
        )
        self.assertEqual(recent_result.returncode, 0, recent_result.stderr)
        recent_rows = json.loads(recent_result.stdout)["results"]
        self.assertEqual(
            [(row["id"], row["tier"]) for row in recent_rows],
            [("implicit-project", "project"),
             ("implicit-global", "user_global")],
        )

    def test_invalid_scoped_config_uses_cli_error_contract(self):
        store_py = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
        base_env = dict(os.environ)
        base_env.update({
            "ZMEM_STORE": str(Path(self.tmp.name) / "store.sqlite"),
            "ZMEM_DATA": self.tmp.name,
            "ZMEM_MODELS_DIR": str(Path(self.tmp.name) / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        base_env.pop("ZMEM_FLEET", None)
        for key, value, message in (
            ("ZMEM_TIER_SLOTS", "1,2,bad,4,5", "ZMEM_TIER_SLOTS"),
            ("ZMEM_FLEET", "bad value", "invalid fleet scope value"),
        ):
            env = dict(base_env)
            env[key] = value
            result = subprocess.run(
                [sys.executable, str(store_py), "recall", "--query", "x",
                 "--json", "--no-bump", "--no-hybrid", "--no-mmr"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(message, result.stderr)
            self.assertTrue(result.stderr.startswith("[zmem]"), result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_cli_cross_policy_keeps_scope_and_dispatch_consistent(self):
        """CLI policy lanes return their actual rows with intended provenance."""
        store = Path(self.tmp.name) / "store.sqlite"
        scope_env = dict(os.environ)
        scope_env.pop("ZMEM_FLEET", None)
        scopes = host_mod.resolve_scopes(
            project_dir=REPO_ROOT, hostname=socket.gethostname().lower(),
            env=scope_env, hermes_kwargs={},
        )
        self.conn.execute("DELETE FROM memory")
        timestamp = "2026-09-24T00:06:00Z"
        rows_to_seed = (
            ("policy-project", scopes["project"],
             "cross policy project sentinel"),
            ("policy-host", scopes["host"], "host-only recent sentinel"),
            ("policy-global", "user:global", "global-only recent sentinel"),
        )
        self.conn.executemany(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref,
                confidence, signal, valid_from, ingestion_ts)
               VALUES (?, ?, 'lesson', ?, 'test', ?, 0.9, 'test', ?, ?)""",
            [(memory_id, namespace, content, f"test:{memory_id}",
              timestamp, timestamp)
             for memory_id, namespace, content in rows_to_seed],
        )
        self.conn.commit()

        base_env = dict(scope_env)
        base_env.update({
            "ZMEM_STORE": str(store),
            "ZMEM_DATA": self.tmp.name,
            "ZMEM_MODELS_DIR": str(Path(self.tmp.name) / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        store_py = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

        def run(command, env, extra=()):
            result = subprocess.run(
                [sys.executable, str(store_py), *command, *extra],
                cwd=str(REPO_ROOT), env=env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["results"]

        recall = ("recall", "--query", "cross policy project sentinel",
                  "--json", "--no-bump", "--no-hybrid", "--no-mmr")
        recent = ("recent", "--json", "--no-bump")

        # An explicit disabled policy uses the scoped allocator: project rows
        # carry tier provenance; host rows reserve the fleet_host tier; global
        # rows remain excluded unless --include-global is passed.
        disabled_env = dict(base_env, ZMEM_CROSS_PROJECT="0")
        self.assertEqual(
            sorted((row["id"], row["tier"])
                   for row in run(recall, disabled_env)),
            [("policy-host", "fleet_host"), ("policy-project", "project")],
        )
        self.assertEqual(
            sorted((row["id"], row["tier"]) for row in run(recent, disabled_env)),
            [("policy-host", "fleet_host"), ("policy-project", "project")],
        )

        # The unset default and explicit env opt-in route to the legacy hazard
        # lane when enabled. The legacy lane returns its store-wide result set
        # without scoped tier labels. Recall's FTS query uses OR matching, so
        # the shared "sentinel" token intentionally admits all three rows.
        for env, extra in (
            (base_env, ("--moment", "pretool", "--session-id",
                        "cross-policy-pretool")),
            (dict(base_env, ZMEM_CROSS_PROJECT="1"), ()),
        ):
            recall_rows = run(recall, env, extra)
            self.assertEqual(
                sorted(row["id"] for row in recall_rows),
                ["policy-global", "policy-host", "policy-project"],
            )
            self.assertTrue(all("tier" not in row for row in recall_rows))
            recent_rows = run(recent, env, extra)
            self.assertEqual(
                sorted(row["id"] for row in recent_rows),
                ["policy-global", "policy-host", "policy-project"],
            )
            self.assertTrue(all("tier" not in row for row in recent_rows))

        # An explicit flag still routes through the legacy lane, while the
        # operator kill switch prevents cross-project admissions.
        explicit_rows = run(
            recall, disabled_env, ("--include-cross-project",)
        )
        self.assertEqual(
            sorted(row["id"] for row in explicit_rows),
            ["policy-global", "policy-host", "policy-project"],
        )
        self.assertTrue(all("tier" not in row for row in explicit_rows))


if __name__ == "__main__":
    unittest.main()
