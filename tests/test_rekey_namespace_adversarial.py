"""Additive guardrails for issue #168's public migration surfaces."""

from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
STORE = SCRIPTS / "store.py"
BUILDER = ROOT / "tests" / "fixtures" / "rekey" / "build_fixture.py"


def _surface_digest(*roots: Path) -> bytes:
    """Stable byte digest for map refusals and contention checks."""
    import hashlib

    digest = hashlib.sha256()
    for root in roots:
        for path in [root, Path(f"{root}-wal"), Path(f"{root}-shm")]:
            digest.update(str(path).encode())
            if path.is_file():
                digest.update(path.read_bytes())
            elif path.is_dir():
                for child in sorted(path.rglob("*")):
                    digest.update(str(child.relative_to(path)).encode())
                    if child.is_file():
                        digest.update(child.read_bytes())
            else:
                digest.update(b"missing")
    return digest.digest()


def _sqlite_vec_available() -> bool:
    """Whether this process can open the fixture's vec0 virtual table.

    The regular CI job deliberately omits sqlite-vec to exercise degraded
    operation.  The two ``reembed --check`` cases below inspect a persisted
    vec0 table, so their non-skipping coverage belongs in test-embeddings.
    """
    try:
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


class NamespaceMapAdversarial(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-168-adv-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = root / "store.sqlite"
        self.data = root / "data"
        self.data.mkdir()
        built = subprocess.run([sys.executable, str(BUILDER), str(self.store)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.env = {**os.environ, "ZMEM_STORE": str(self.store),
                    "ZMEM_DATA": str(self.data),
                    "ZMEM_BACKUP_DIR": str(root / "backups"),
                    "ZMEM_MODELS_DIR": str(root / "no-models"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        self.env.pop("ZMEM_EMBED_PROFILE", None)

    def run_store(self, *args):
        return subprocess.run([sys.executable, str(STORE), *args],
                              capture_output=True, text=True, env=self.env,
                              cwd=str(SCRIPTS), timeout=60)

    def test_missing_apply_store_reports_cli_error(self):
        self.env["ZMEM_STORE"] = str(Path(self.temp.name) / "absent.sqlite")
        mapping = ROOT / "tests" / "fixtures" / "rekey" / "map.yaml"
        result = self.run_store("rekey-namespace", "--map", str(mapping),
                                "--confirm")
        self.assertEqual(result.returncode, 2)
        self.assertIn("[zmem] rekey-namespace:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_newer_schema_refuses_map_preview_and_apply_without_side_effects(self):
        conn = sqlite3.connect(self.store)
        try:
            conn.execute("UPDATE meta SET value='999999' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        mapping = ROOT / "tests" / "fixtures" / "rekey" / "map.yaml"
        for mode in ("--dry-run", "--confirm"):
            with self.subTest(mode=mode):
                before = _surface_digest(self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"]))
                result = self.run_store("rekey-namespace", "--map", str(mapping), mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("newer than", result.stderr)
                self.assertEqual(
                    _surface_digest(self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"])), before
                )

    def test_newer_schema_refuses_reembed_check_before_vector_runtime_load(self):
        conn = sqlite3.connect(self.store)
        try:
            conn.execute("UPDATE meta SET value='999999' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        before = _surface_digest(self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"]))
        result = self.run_store("reembed", "--check")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("newer than", result.stderr)
        self.assertNotIn("sqlite_vec", result.stderr)
        self.assertEqual(
            _surface_digest(self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"])), before
        )

    def test_apply_reports_writer_contention_without_partial_update(self):
        check = sqlite3.connect(self.store)
        try:
            before_rows = check.execute(
                "SELECT id, namespace FROM memory ORDER BY id"
            ).fetchall()
        finally:
            check.close()
        conn = sqlite3.connect(self.store, timeout=0.1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            mapping = ROOT / "tests" / "fixtures" / "rekey" / "map.yaml"
            result = self.run_store("rekey-namespace", "--map", str(mapping), "--confirm")
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr.lower(), r"locked|lease|writer|busy")
        finally:
            conn.rollback()
            conn.close()
        check = sqlite3.connect(self.store)
        try:
            self.assertEqual(check.execute(
                "SELECT id, namespace FROM memory ORDER BY id"
            ).fetchall(), before_rows)
        finally:
            check.close()
        self.assertFalse((self.data / "zmem-decisions.log").exists())

    def test_reembed_check_rejects_each_mutation_flag(self):
        cases = (
            ("--all",),
            ("--dry-run",),
            ("--confirm",),
            ("--profile", "fake"),
            ("--batch", "64"),
        )
        for flags in cases:
            with self.subTest(flags=flags):
                before = _surface_digest(
                    self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"])
                )
                result = self.run_store("reembed", "--check", *flags)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(
                    "reembed: --check is exclusive with", result.stderr
                )
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(
                    _surface_digest(
                        self.store, self.data, Path(self.env["ZMEM_BACKUP_DIR"])
                    ), before,
                )

    def test_crlf_map_reapply_is_noop_for_entity_links(self):
        mapping = Path(self.temp.name) / "map.yaml"
        mapping.write_bytes(
            b'"db:spark-kb": "fleet:dgx-spark"\r\n'
            b'"hermes-spark1": "host:spark1"\r\n')
        first = self.run_store("rekey-namespace", "--map", str(mapping), "--confirm")
        self.assertEqual(first.returncode, 0, first.stderr)
        conn = sqlite3.connect(self.store)
        links_before = conn.execute(
            "SELECT memory_id,entity_id,role FROM memory_entity ORDER BY 1,2,3"
        ).fetchall()
        conn.close()
        second = self.run_store("rekey-namespace", "--map", str(mapping), "--confirm")
        self.assertEqual(second.returncode, 0, second.stderr)
        conn = sqlite3.connect(self.store)
        self.assertEqual(conn.execute(
            "SELECT memory_id,entity_id,role FROM memory_entity ORDER BY 1,2,3"
        ).fetchall(), links_before)
        conn.close()

    @unittest.skipUnless(_sqlite_vec_available(), "sqlite-vec required")
    def test_check_bypasses_auto_rekey_and_rejects_explicit_batch(self):
        conn = sqlite3.connect(self.store)
        conn.execute("UPDATE memory SET namespace='global' WHERE id='fixture-01'")
        conn.commit(); conn.close()
        refused = self.run_store("reembed", "--check", "--batch", "64")
        self.assertEqual(refused.returncode, 2)
        checked = self.run_store("reembed", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout, "reembed check: 0 inconsistencies\n")
        conn = sqlite3.connect(self.store)
        self.assertEqual(conn.execute(
            "SELECT namespace FROM memory WHERE id='fixture-01'"
        ).fetchone()[0], "global")
        conn.close()

    @unittest.skipUnless(_sqlite_vec_available(), "sqlite-vec required")
    def test_tombstoned_retained_vector_is_not_orphan(self):
        conn = sqlite3.connect(self.store)
        conn.execute("UPDATE memory SET superseded_at='2026-03-01T00:00:00Z' "
                     "WHERE id='fixture-01'")
        conn.commit(); conn.close()
        checked = self.run_store("reembed", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout, "reembed check: 0 inconsistencies\n")


class ReadonlyAndCensusGuardrail(unittest.TestCase):
    def test_optional_vector_extension_only_suppresses_missing_dependency(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import load_vec_extension_if_available
        conn = sqlite3.connect(":memory:")

        def missing_package(_conn):
            try:
                raise ModuleNotFoundError("sqlite_vec is optional")
            except ImportError as cause:
                raise RuntimeError("cannot load sqlite-vec") from cause

        with mock.patch("storelib.rekey.load_vec_extension",
                        side_effect=missing_package):
            self.assertFalse(load_vec_extension_if_available(conn))
        with mock.patch("storelib.rekey.load_vec_extension", return_value=None):
            self.assertTrue(load_vec_extension_if_available(conn))
        with mock.patch("storelib.rekey.load_vec_extension",
                        side_effect=RuntimeError("extension load failed")):
            with self.assertRaisesRegex(RuntimeError, "extension load failed"):
                load_vec_extension_if_available(conn)
        conn.close()

    def test_readonly_handle_uses_immutable_ro_uri(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import open_readonly_store
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            path.write_bytes(b"SQLite format 3\x00")
            with mock.patch("storelib.rekey.sqlite3.connect") as connect:
                open_readonly_store(path)
            self.assertIn("mode=ro&immutable=1", connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs["uri"])

    def test_readonly_handle_refuses_hot_wal_without_recovery(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import open_readonly_store
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            sqlite3.connect(path).close()
            wal = Path(f"{path}-wal")
            wal.write_bytes(b"do not recover this")
            with self.assertRaisesRegex(RuntimeError, "WAL sidecar"):
                open_readonly_store(path)
            self.assertEqual(wal.read_bytes(), b"do not recover this")

    def test_readonly_handle_refuses_resolved_target_wal(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import open_readonly_store
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.sqlite"
            sqlite3.connect(target).close()
            alias = root / "alias.sqlite"
            try:
                alias.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable on this host")
            target_wal = Path(f"{target}-wal")
            target_wal.write_bytes(b"target WAL")
            with self.assertRaisesRegex(RuntimeError, "WAL sidecar"):
                open_readonly_store(alias)
            self.assertEqual(target_wal.read_bytes(), b"target WAL")

    def test_immutable_handle_rejects_actual_write(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import open_readonly_store
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE evidence(value TEXT)")
            conn.commit()
            conn.close()
            ro = open_readonly_store(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO evidence VALUES ('must fail')")
            finally:
                ro.close()
            self.assertFalse(Path(f"{path}-wal").exists())
            self.assertFalse(Path(f"{path}-shm").exists())

    def test_dimension_census_uses_memory_blob_not_vector_blob(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.rekey import embedding_census
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE memory(id TEXT, superseded_at TEXT, embedding BLOB)")
        conn.execute("CREATE TABLE memory_vec(memory_id TEXT, embedding BLOB)")
        conn.execute("INSERT INTO memory VALUES ('m1', NULL, ?)",
                     (struct.pack("<384f", *([0.25] * 384)),))
        conn.execute("INSERT INTO memory_vec VALUES ('m1', ?)",
                     (struct.pack("<2f", 0.25, 0.25),))
        self.assertEqual(embedding_census(conn, 384), (0, 0, 0))
        conn.execute("UPDATE memory SET embedding=? WHERE id='m1'",
                     (struct.pack("<384f", *([0.25] * 384)) + b"x",))
        self.assertEqual(embedding_census(conn, 384), (0, 0, 1))
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
