"""Pinned-provider regressions for source's implicit Hermes home contract."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
PROVIDER_ROOT_ENV = "ZMEM_TEST_HERMES_PROVIDER_ROOT"


@unittest.skipUnless(os.environ.get(PROVIDER_ROOT_ENV), "pinned Hermes provider is CI input")
class SourceHermesDefaultHomeTests(unittest.TestCase):
    """Run only in test-source-hermes, which supplies the pinned provider."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.provider_root = Path(os.environ[PROVIDER_ROOT_ENV]).resolve()
        if not (cls.provider_root / "hermes_constants.py").is_file():
            raise AssertionError("test-source-hermes requires the pinned Hermes provider")
        sys.path[:0] = [str(cls.provider_root), str(SCRIPTS)]
        cls.constants = importlib.import_module("hermes_constants")
        cls.hermes_state = importlib.import_module("hermes_state")
        cls.source = importlib.import_module("storelib.source")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-source-hermes-default-")
        self.root = Path(self.temp.name)
        self.before_env = dict(os.environ)
        self.writer: sqlite3.Connection | None = None
        for key in ("HERMES_HOME", "ZMEM_HERMES_SESSIONS", "HERMES_DATA_DIR_SUFFIX"):
            os.environ.pop(key, None)
        os.environ["HOME"] = str(self.root / "home")
        os.environ["LOCALAPPDATA"] = str(self.root / "localappdata")
        self.home = self.constants.get_hermes_home()

    def tearDown(self) -> None:
        if self.writer is not None:
            self.writer.close()
        os.environ.clear()
        os.environ.update(self.before_env)
        self.temp.cleanup()

    def _create_native_db(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        db = self.hermes_state.SessionDB(db_path=self.home / "state.db")
        try:
            db.create_session("default-home-session", "hermes")
            db.append_message("default-home-session", "user", "native prompt")
            db.append_message("default-home-session", "assistant", "native answer")
        finally:
            db.close()
        self.writer = sqlite3.connect(self.home / "state.db")
        mode = self.writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        self.writer.execute("PRAGMA user_version=139")
        self.writer.commit()
        self.assertEqual(str(mode).lower(), "wal")
        self.assertTrue((self.home / "state.db-wal").is_file())
        self.assertTrue((self.home / "state.db-shm").is_file())

    def _write_jsonl(self, directory: Path, name: str = "export.jsonl") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(
            '{"type":"user","sessionId":"fallback","timestamp":"t","message":{"content":"fallback"}}\n',
            encoding="utf-8",
        )
        return path

    def _snapshot_home(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }

    def test_default_provider_home_db_reads_with_real_sessiondb(self) -> None:
        self._create_native_db()
        ref = "hermes:state.db/session/default-home-session"
        anchor = ("e", "e", "default-home-session", "hermes", "t", "native answer", ref, None)
        before = self._snapshot_home()
        excerpt, detail, _rows = self.source._hermes(ref, anchor, 0)
        after = self._snapshot_home()
        self.assertIn("native answer", excerpt)
        self.assertEqual(detail["kind"], "hermes_session")
        self.assertEqual(set(before), set(after))
        self.assertEqual(before["state.db"], after["state.db"])
        self.assertEqual(before["state.db-wal"], after["state.db-wal"])
        self.assertEqual(
            [name for name in before if before[name] != after[name]],
            ["state.db-shm"] if before["state.db-shm"] != after["state.db-shm"] else [],
        )

    def test_present_default_db_suppresses_explicit_jsonl_fallback(self) -> None:
        self._create_native_db()
        bait = self._write_jsonl(self.root / "bait")
        os.environ["ZMEM_HERMES_SESSIONS"] = str(bait.parent)
        with self.assertRaises(self.source.SourceRefusal):
            self.source._configured_file(bait.name)

    def test_broken_present_default_db_still_suppresses_jsonl_fallback(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "state.db").write_bytes(b"not a sqlite database")
        bait = self._write_jsonl(self.root / "bait")
        os.environ["ZMEM_HERMES_SESSIONS"] = str(bait.parent)
        with self.assertRaises(self.source.SourceRefusal):
            self.source._configured_file(bait.name)

    def test_missing_default_db_uses_only_provider_home_sessions(self) -> None:
        fallback = self._write_jsonl(self.home / "sessions")
        selected, kind = self.source._configured_file(fallback.name)
        self.assertEqual(selected, fallback)
        self.assertEqual(kind, "hermes_session")

    def test_unavailable_provider_with_implicit_home_refuses(self) -> None:
        self._write_jsonl(self.home / "sessions")
        with patch.dict(sys.modules, {"hermes_constants": None}):
            with self.assertRaises(self.source.SourceRefusal):
                self.source._configured_file("export.jsonl")


if __name__ == "__main__":
    unittest.main()
