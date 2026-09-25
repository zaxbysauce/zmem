"""Frozen acceptance tests for issue #168 namespace-map rekeying.

Run from the repository root with::

    python tests/test_rekey_namespace_map.py

The base commit is intentionally RED because ``--map`` does not exist yet.
The fixture is built in a throwaway directory for every test and contains no
operator or repository data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
FIXTURE_BUILDER = REPO_ROOT / "tests" / "fixtures" / "rekey" / "build_fixture.py"
MAP_FILE = REPO_ROOT / "tests" / "fixtures" / "rekey" / "map.yaml"
PYTHON = sys.executable


def _run_builder(store: Path) -> None:
    result = subprocess.run(
        [PYTHON, str(FIXTURE_BUILDER), str(store)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise AssertionError(
            f"fixture builder failed: rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


def _surface_digest(*roots: Path) -> str:
    """Digest bytes and mtimes of the database, data, and backup surfaces."""
    h = hashlib.sha256()
    for root in roots:
        root = Path(root)
        h.update(f"ROOT:{root}\n".encode())
        if not root.exists():
            h.update(b"MISSING\n")
            continue
        paths = [root, *sorted(root.rglob("*"))]
        for path in paths:
            rel = path.relative_to(root)
            stat = path.stat()
            h.update(
                f"{rel}|{path.is_dir()}|{stat.st_mtime_ns}|{stat.st_size}\n".encode()
            )
            if path.is_file():
                h.update(path.read_bytes())
    return h.hexdigest()


def _store_digest(store: Path) -> str:
    """Include SQLite sidecars so WAL writes cannot hide from rollback checks."""
    h = hashlib.sha256()
    for path in (store, Path(f"{store}-wal"), Path(f"{store}-shm")):
        h.update(str(path.name).encode())
        if path.exists():
            h.update(path.read_bytes())
        else:
            h.update(b"<missing>")
    return h.hexdigest()


def _sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    if _sqlite_vec_available():
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    return conn


class RekeyNamespaceMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-rekey-map-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store.sqlite"
        self.data = self.tmp / "data"
        self.backups = self.tmp / "backups"
        self.data.mkdir()
        _run_builder(self.store)
        self.env = dict(os.environ)
        self.env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.data),
            "ZMEM_BACKUP_DIR": str(self.backups),
            "ZMEM_MODELS_DIR": str(self.tmp / "no-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        self.env.pop("ZMEM_EMBED_PROFILE", None)

    def run_store(self, *args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True,
            text=True,
            env=env or self.env,
            cwd=str(SCRIPTS),
            timeout=60,
        )

    def query(self, sql: str, params=()):
        conn = _connect(self.store)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _map_command(self, *args: str, map_path: Path = MAP_FILE,
                     env: dict[str, str] | None = None):
        command = ("rekey-namespace", "--map", str(map_path), *args)
        if env is None:
            return self.run_store(*command)
        return self.run_store(*command, env=env)

    def test_map_command_argv_places_flag_after_map_path(self):
        with mock.patch.object(self, "run_store", return_value="sentinel") as run:
            self.assertEqual(self._map_command("--dry-run"), "sentinel")
        run.assert_called_once_with(
            "rekey-namespace", "--map", str(MAP_FILE), "--dry-run"
        )

    def _all_memory_rows(self):
        rows = self.query("SELECT * FROM memory ORDER BY id")
        return {row["id"]: dict(row) for row in rows}

    def test_dry_run_output_and_sha_unchanged(self):
        before = _surface_digest(self.store, self.data, self.backups)
        result = self._map_command("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "db:spark-kb -> fleet:dgx-spark: 12\n"
            "hermes-spark1 -> host:spark1: 5\n"
            "unmapped: 3\n",
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            _surface_digest(self.store, self.data, self.backups), before,
            "dry-run must not change database, WAL, log, or backup bytes/mtimes",
        )

    def test_map_refuses_onedrive_path_with_cli_error_contract(self):
        env = {**self.env, "OneDrive": str(self.tmp)}
        result = self._map_command("--dry-run", env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("OneDrive root", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_apply_moves_only_source_prefix_matches(self):
        before = self._all_memory_rows()
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self._all_memory_rows()
        self.assertEqual(len(after), 20)

        counts = self.query(
            "SELECT namespace, COUNT(*) AS n FROM memory "
            "WHERE superseded_at IS NULL GROUP BY namespace ORDER BY namespace"
        )
        self.assertEqual(
            [(row["namespace"], row["n"]) for row in counts],
            [("fleet:dgx-spark", 12), ("host:spark1", 5), ("user:global", 3)],
        )
        for memory_id, old in before.items():
            new = after[memory_id]
            for key, value in old.items():
                if key != "namespace":
                    self.assertEqual(
                        new[key], value,
                        f"rekey changed {key} for {memory_id}",
                    )
        self.assertTrue(
            all(after[memory_id]["namespace"] == "user:global"
                for memory_id in ("fixture-18", "fixture-19", "fixture-20")),
            "unmapped source_ref rows must remain untouched",
        )

    def test_yaml_order_controls_overlapping_prefixes(self):
        ordered_map = self.tmp / "ordered-map.yaml"
        ordered_map.write_text(
            '"db:spark-kb:lesson-01": "host:precedence"\n'
            '"db:spark-kb": "fleet:dgx-spark"\n'
            '"hermes-spark1": "host:spark1"\n',
            encoding="utf-8",
        )
        result = self._map_command("--confirm", map_path=ordered_map)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.query(
            "SELECT namespace, COUNT(*) AS n FROM memory "
            "WHERE superseded_at IS NULL GROUP BY namespace ORDER BY namespace"
        )
        self.assertEqual(
            [(row["namespace"], row["n"]) for row in rows],
            [
                ("fleet:dgx-spark", 11),
                ("host:precedence", 1),
                ("host:spark1", 5),
                ("user:global", 3),
            ],
        )

    def test_unknown_target_scope_refused_before_mutation(self):
        bad_map = self.tmp / "bad-map.yaml"
        bad_map.write_text('"db:spark-kb": "global"\n', encoding="utf-8")
        before = _surface_digest(self.store, self.data, self.backups)
        result = self._map_command("--dry-run", map_path=bad_map)
        self.assertEqual(result.returncode, 2)
        self.assertIn("has invalid target scope 'global'", result.stderr)
        self.assertEqual(_surface_digest(self.store, self.data, self.backups), before)

    def test_strict_map_syntax_refused_without_mutation(self):
        invalid_maps = (
            "  \"db:spark-kb\": \"fleet:dgx-spark\"\n",
            "\"db:spark-kb\": [\"fleet:dgx-spark\"]\n",
            "\"db:spark-kb\\\"x\": \"fleet:dgx-spark\"\n",
            "\"db:spark-kb\": \"fleet:dgx-spark\"\n"
            "\"db:spark-kb\": \"host:spark1\"\n",
            "\"db:\\tspark-kb\": \"fleet:dgx-spark\"\n",
            "\"db:spark-kb target=x mapped=999\": \"fleet:dgx-spark\"\n",
        )
        for number, contents in enumerate(invalid_maps):
            with self.subTest(number=number):
                path = self.tmp / f"invalid-{number}.yaml"
                path.write_text(contents, encoding="utf-8")
                before = _surface_digest(self.store, self.data, self.backups)
                result = self._map_command("--dry-run", map_path=path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    _surface_digest(self.store, self.data, self.backups), before
                )

    def test_map_rejects_legacy_source_flags(self):
        before = _surface_digest(self.store, self.data, self.backups)
        result = self._map_command("--dry-run", "--from", "user:global")
        self.assertEqual(result.returncode, 2)
        self.assertRegex(result.stderr.lower(), r"map|from|exclusive|combine")
        self.assertEqual(_surface_digest(self.store, self.data, self.backups), before)

    def test_map_requires_exactly_one_mode_and_rejects_every_legacy_selector(self):
        cases = (
            (),
            ("--dry-run", "--confirm"),
            ("--dry-run", "--to", "user:global"),
            ("--dry-run", "--near-miss-global"),
        )
        for args in cases:
            with self.subTest(args=args):
                before = _surface_digest(self.store, self.data, self.backups)
                result = self._map_command(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertRegex(result.stderr.lower(), r"map|confirm|dry.run|combine")
                self.assertEqual(_surface_digest(self.store, self.data, self.backups), before)

    def test_bank_name_is_not_inferred(self):
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.query(
            "SELECT namespace, source_ref, content FROM memory "
            "WHERE source_ref LIKE 'other:%' ORDER BY source_ref"
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["namespace"] == "user:global" for row in rows))
        self.assertTrue(all("spark-kb" in row["content"] for row in rows))

    @unittest.skipUnless(_sqlite_vec_available(), "sqlite-vec required")
    def test_derived_tables_follow_atomically(self):
        vectors_before = {
            row[0]: row[1]
            for row in self.query("SELECT memory_id, embedding FROM memory_vec")
        }
        links_before = {
            tuple(row)
            for row in self.query(
                "SELECT src_id, dst_id, relation, score, created_at "
                "FROM memory_link ORDER BY src_id, dst_id, relation"
            )
        }
        entities_before = {
            tuple(row)
            for row in self.query(
                "SELECT memory_id, entity_id, role FROM memory_entity "
                "ORDER BY memory_id, entity_id"
            )
        }
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        vectors_after = {
            row[0]: row[1]
            for row in self.query("SELECT memory_id, embedding FROM memory_vec")
        }
        self.assertEqual(vectors_after, vectors_before)
        self.assertEqual(
            {
                tuple(row)
                for row in self.query(
                    "SELECT src_id, dst_id, relation, score, created_at "
                    "FROM memory_link ORDER BY src_id, dst_id, relation"
                )
            },
            links_before,
        )
        self.assertEqual(
            {
                tuple(row)
                for row in self.query(
                    "SELECT memory_id, entity_id, role FROM memory_entity "
                    "ORDER BY memory_id, entity_id"
                )
            },
            entities_before,
        )
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory_fts "
                "WHERE namespace='fleet:dgx-spark'"
            )[0][0],
            12,
        )
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory_fts "
                "WHERE namespace='host:spark1'"
            )[0][0],
            5,
        )
        self.assertEqual(
            [tuple(row) for row in self.query(
                "SELECT f.rowid, f.content, f.tags, f.namespace "
                "FROM memory_fts f JOIN memory m ON m.rowid=f.rowid "
                "WHERE m.superseded_at IS NULL ORDER BY f.rowid"
            )],
            [tuple(row) for row in self.query(
                "SELECT rowid, content, tags, namespace FROM memory "
                "WHERE superseded_at IS NULL ORDER BY rowid"
            )],
            "FTS rows must preserve each memory row identity and exact content",
        )

    def test_verified_single_backup_and_decision_log(self):
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshots = sorted(self.backups.glob("store-*.sqlite"))
        self.assertEqual(len(snapshots), 1)
        snapshot = _connect(snapshots[0])
        try:
            self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                snapshot.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
                ).fetchone()[0],
                20,
            )
            self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            snapshot.close()
        self.assertFalse(Path(f"{snapshots[0]}-wal").exists())
        self.assertFalse(Path(f"{snapshots[0]}-shm").exists())

        lines = (self.data / "zmem-decisions.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        snapshot_sha = hashlib.sha256(snapshots[0].read_bytes()).hexdigest()
        before_line = next(
            line for line in result.stdout.splitlines()
            if line.startswith("rekey-namespace map before: ")
        )
        after_line = next(
            line for line in result.stdout.splitlines()
            if line.startswith("rekey-namespace map after: ")
        )
        expected_prefixes = [
            {"source_ref_prefix": "db:spark-kb", "target": "fleet:dgx-spark",
             "total": 12, "live": 12, "moved": 0},
            {"source_ref_prefix": "hermes-spark1", "target": "host:spark1",
             "total": 5, "live": 5, "moved": 0},
        ]
        self.assertEqual(json.loads(before_line.split(": ", 1)[1]), {
            "total": 20, "live": 20,
            "by_namespace": [
                {"namespace": "user:global", "total": 20, "live": 20},
            ],
            "by_prefix": expected_prefixes,
            "unmapped": {"total": 3, "live": 3},
        })
        expected_after_prefixes = [
            {**expected_prefixes[0], "moved": 12},
            {**expected_prefixes[1], "moved": 5},
        ]
        self.assertEqual(json.loads(after_line.split(": ", 1)[1]), {
            "total": 20, "live": 20,
            "by_namespace": [
                {"namespace": "fleet:dgx-spark", "total": 12, "live": 12},
                {"namespace": "host:spark1", "total": 5, "live": 5},
                {"namespace": "user:global", "total": 3, "live": 3},
            ],
            "by_prefix": expected_after_prefixes,
            "unmapped": {"total": 3, "live": 3},
        })
        for line, source, target, mapped in (
            (lines[0], "db:spark-kb", "fleet:dgx-spark", 12),
            (lines[1], "hermes-spark1", "host:spark1", 5),
        ):
            self.assertRegex(
                line,
                rf"^\[\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z\] "
                rf"zmem-rekey kind=namespace source_ref_prefix={re.escape(source)} "
                rf"target={re.escape(target)} matched={mapped} moved={mapped} unmapped=3 "
                r"snapshot_sha256=[0-9a-f]{64}$",
            )
            self.assertEqual(line.rsplit("snapshot_sha256=", 1)[1], snapshot_sha)

    def test_decision_log_percent_encodes_target_field_delimiters(self):
        mapping = self.tmp / "target-with-delimiters.yaml"
        mapping.write_text(
            '"db:spark-kb": "project:tenant injected=999"\n',
            encoding="utf-8",
        )
        result = self._map_command("--confirm", map_path=mapping)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (self.data / "zmem-decisions.log").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("target=project:tenant%20injected%3D999 ", lines[0])
        self.assertNotIn("target=project:tenant injected=999", lines[0])
        self.assertIn(" matched=12 moved=12 unmapped=8 ", lines[0])

    def test_reapply_records_matches_but_zero_rows_moved(self):
        first = self._map_command("--confirm")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._map_command("--confirm")
        self.assertEqual(second.returncode, 0, second.stderr)
        lines = (self.data / "zmem-decisions.log").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("matched=12 moved=0 unmapped=3", lines[2])
        self.assertIn("matched=5 moved=0 unmapped=3", lines[3])

    def test_backup_failure_blocks_apply(self):
        self.backups.write_text("not a directory", encoding="utf-8")
        before = _store_digest(self.store)
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(_store_digest(self.store), before)
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory WHERE namespace='user:global'"
            )[0][0],
            20,
        )
        self.assertFalse((self.data / "zmem-decisions.log").exists())

    def test_trigger_failure_rolls_back(self):
        conn = _connect(self.store)
        try:
            conn.execute(
                "CREATE TRIGGER fail_namespace_rekey BEFORE UPDATE OF namespace "
                "ON memory BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END"
            )
            conn.commit()
        finally:
            conn.close()
        before_rows = self._all_memory_rows()
        before_store = _store_digest(self.store)
        before_data = _surface_digest(self.data)
        result = self._map_command("--confirm")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._all_memory_rows(), before_rows)
        self.assertEqual(_store_digest(self.store), before_store)
        self.assertEqual(_surface_digest(self.data), before_data)
        # The snapshot is deliberately before BEGIN IMMEDIATE; a failed
        # transaction may leave that verified recovery artifact, but no log.
        self.assertEqual(len(list(self.backups.glob("store-*.sqlite"))), 1)

    def test_derived_identity_invariant_failure_rolls_back(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import apply_namespace_map

        before = self._all_memory_rows()
        conn = _connect(self.store)
        conn.row_factory = sqlite3.Row
        try:
            with mock.patch.dict(os.environ, {
                "ZMEM_DATA": str(self.data),
                "ZMEM_BACKUP_DIR": str(self.backups),
            }, clear=False), mock.patch(
                "storelib.rekey._derived_identity_digest",
                side_effect=("before", "after"),
            ):
                result = apply_namespace_map(
                    conn, store_path=self.store,
                    entries=[("db:spark-kb", "fleet:dgx-spark")],
                )
        finally:
            conn.close()
        self.assertEqual(result, 1)
        self.assertEqual(self._all_memory_rows(), before)
        self.assertFalse((self.data / "zmem-decisions.log").exists())
        self.assertEqual(self.query("PRAGMA integrity_check")[0][0], "ok")
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory WHERE namespace='user:global'"
            )[0][0],
            20,
        )

    def test_decision_log_failure_reports_committed_result(self):
        (self.data / "zmem-decisions.log").mkdir()
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 3)
        self.assertIn("committed, but decision log append failed", result.stderr)
        self.assertIn("map changes are committed", result.stderr)
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory WHERE namespace='fleet:dgx-spark'"
            )[0][0],
            12,
        )
        self.assertEqual(
            self.query(
                "SELECT COUNT(*) FROM memory WHERE namespace='host:spark1'"
            )[0][0],
            5,
        )

    def test_mapped_tombstone_is_excluded_from_apply_and_census(self):
        conn = _connect(self.store)
        try:
            conn.execute("UPDATE memory SET superseded_at='2026-01-01T00:00:00Z' "
                         "WHERE id='fixture-01'")
            conn.commit()
        finally:
            conn.close()
        result = self._map_command("--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.query(
            "SELECT namespace FROM memory WHERE id='fixture-01'"
        )[0][0], "user:global")
        after = result.stdout.split("rekey-namespace map after: ", 1)[1].splitlines()[0]
        census = json.loads(after)
        self.assertEqual(census["total"], 20)
        self.assertEqual(census["live"], 19)
        self.assertEqual(census["by_prefix"][0]["total"], 12)
        self.assertEqual(census["by_prefix"][0]["live"], 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
