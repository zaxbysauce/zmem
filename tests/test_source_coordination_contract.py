"""Focused contracts for C1's canonical-store coordination exception."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "tests" / "fixtures" / "source" / "repro" / "checks.py"


def _load_checks():
    spec = importlib.util.spec_from_file_location("source_coordination_checks", CHECKS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceCoordinationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checks = _load_checks()

    @staticmethod
    def _digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def test_owned_directory_alias_uses_resolved_fixture_root(self):
        with tempfile.TemporaryDirectory(prefix="zmem-c1-alias-") as raw:
            parent = Path(raw)
            actual = parent / "actual-root"
            actual.mkdir()
            alias = parent / "fixture-root"
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(actual)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
            else:
                alias.symlink_to(actual, target_is_directory=True)
            try:
                store = actual / "store.sqlite"
                store_bytes = b"canonical store bytes"
                store.write_bytes(store_bytes)
                wal = actual / "store.sqlite-wal"
                shm = actual / "store.sqlite-shm"
                wal.write_bytes(b"")
                shm.write_bytes(b"sqlite coordination")
                before = {"store.sqlite": self._digest(store_bytes)}
                after = {
                    **before,
                    "store.sqlite-wal": self._digest(b""),
                    "store.sqlite-shm": self._digest(b"sqlite coordination"),
                }
                self.checks._assert_canonical_store_coordination_only(
                    before, after, scratch=alias, store_path=store
                )
            finally:
                if alias.exists() or alias.is_symlink():
                    if os.name == "nt":
                        alias.rmdir()
                    else:
                        alias.unlink()

    def test_real_external_store_remains_refused(self):
        with tempfile.TemporaryDirectory(prefix="zmem-c1-outside-") as raw:
            parent = Path(raw)
            scratch = parent / "fixture-root"
            external = parent / "external-root"
            scratch.mkdir()
            external.mkdir()
            with self.assertRaisesRegex(self.checks.CheckFailure, "outside its fixture root"):
                self.checks._assert_canonical_store_coordination_only(
                    {}, {}, scratch=scratch, store_path=external / "store.sqlite"
                )

    def test_nonempty_new_canonical_wal_remains_refused(self):
        with tempfile.TemporaryDirectory(prefix="zmem-c1-sidecar-") as raw:
            scratch = Path(raw)
            store = scratch / "store.sqlite"
            store_bytes = b"canonical store bytes"
            store.write_bytes(store_bytes)
            wal = scratch / "store.sqlite-wal"
            shm = scratch / "store.sqlite-shm"
            wal_bytes = b"unexpected writer data"
            shm_bytes = b"sqlite coordination"
            wal.write_bytes(wal_bytes)
            shm.write_bytes(shm_bytes)
            before = {"store.sqlite": self._digest(store_bytes)}
            after = {
                **before,
                "store.sqlite-wal": self._digest(wal_bytes),
                "store.sqlite-shm": self._digest(shm_bytes),
            }
            with self.assertRaisesRegex(self.checks.CheckFailure, "created a non-empty canonical WAL"):
                self.checks._assert_canonical_store_coordination_only(
                    before, after, scratch=scratch, store_path=store
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
