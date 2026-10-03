"""Round-two page/purge boundary tests.

These tests use the existing Q01 subprocess harness so purge runs through the
real CLI and its maintenance-lock ladder.  Every page path is disposable.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_q01():
    path = Path(__file__).with_name("test_q01_purge.py")
    spec = importlib.util.spec_from_file_location("round2_q01_helpers", path)
    if spec is None or spec.loader is None:
        raise ImportError("unable to load Q01 purge helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_Q01 = _load_q01()


class PagesPurgeFeedback(_Q01._PurgeBase):
    def _pages_root(self) -> Path:
        root = Path(self.data_dir) / "pages"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_derived_token_residue_refuses_before_live_purge_without_plaintext(self):
        target_text = "grounded longsecret zebraquux marker"
        target = self.add_row(target_text)
        page = self._pages_root() / "derived-only"
        page.mkdir()
        projection = page / "page.md"
        projection.write_text("derived-only zebraquux\n", encoding="utf-8")
        before_page = projection.read_bytes()
        before_store = Path(self.store).read_bytes()
        before_wal = Path(self.store + "-wal").read_bytes() if Path(self.store + "-wal").exists() else None

        result = self._purge(target)

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn(target_text, result.stderr)
        self.assertNotIn("zebraquux", result.stderr.lower())
        self.assertEqual(self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,)), 1)
        self.assertEqual(projection.read_bytes(), before_page)
        self.assertEqual(Path(self.store).read_bytes(), before_store)
        if before_wal is not None:
            self.assertEqual(Path(self.store + "-wal").read_bytes(), before_wal)

    def test_native_windows_junction_is_refused_and_not_followed(self):
        if os.name != "nt":
            self.skipTest("Windows junction contract")
        target = self.add_row("junction residue zebraquux source")
        pages = self._pages_root()
        outside = Path(self.tmp) / "outside-junction"
        outside.mkdir()
        outside_file = outside / "page.md"
        outside_file.write_text("unrelated outside bytes\n", encoding="utf-8")
        junction = pages / "junction"
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J",
             str(junction), str(outside)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not junction.exists():
            self.skipTest("mklink /J unavailable without elevation")
        before = outside_file.read_bytes()
        purge = self._purge(target)
        self.assertEqual(purge.returncode, 4, purge.stderr)
        self.assertEqual(outside_file.read_bytes(), before)
        self.assertEqual(self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,)), 1)

    def test_unreadable_nested_page_directory_refuses_scan(self):
        # Patch the directory enumeration itself, rather than file reads: the
        # old Path.rglob implementation swallowed this exact traversal error.
        scripts = str(_Q01.SCRIPTS_DIR)
        sys.path.insert(0, scripts)
        try:
            from storelib import purge  # type: ignore
        finally:
            sys.path.remove(scripts)
        pages = self._pages_root()
        blocked = pages / "unreadable-nested"
        blocked.mkdir()
        real_scandir = purge.os.scandir
        blocked_scans = []

        def deny_nested(path):
            if Path(path) == blocked:
                blocked_scans.append(Path(path))
                raise PermissionError("simulated nested directory denial")
            return real_scandir(path)

        with patch.object(purge.os, "scandir", side_effect=deny_nested):
            self.assertFalse(purge._scan_page_artifacts(pages, []))
        self.assertEqual(blocked_scans, [blocked])

    def test_escaped_history_json_residue_refuses_without_projection_or_plaintext(self):
        # The only derived token contains quotes and a backslash. JSON escapes
        # those bytes in the immutable history copy, so the old raw-byte-only
        # scanner cannot see the exact needle; decoded semantic scanning must.
        target_text = '"quoted\\longsecretmarker"'
        target = self.add_row(target_text)
        history = self._pages_root() / "history-only" / "versions"
        history.mkdir(parents=True)
        history_file = history / "v000001.json"
        history_file.write_text(
            json.dumps({"content": target_text, "note": "immutable history"}),
            encoding="utf-8",
        )
        before_history = history_file.read_bytes()
        # Full-content and token derivation collapse to this one whitespace
        # token. Assert every derived needle is absent from raw JSON bytes.
        derived_needles = {target_text, target_text.split()[0]}
        self.assertEqual(derived_needles, {target_text})
        for needle in derived_needles:
            self.assertNotIn(needle.encode("utf-8"), before_history)
        before_store = Path(self.store).read_bytes()
        result = self._purge(target)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn(target_text, result.stderr)
        self.assertEqual(history_file.read_bytes(), before_history)
        self.assertEqual(Path(self.store).read_bytes(), before_store)
        self.assertEqual(self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,)), 1)

    def test_posix_symlink_counterpart_is_refused_and_not_cleaned(self):
        if os.name == "nt":
            self.skipTest("POSIX symlink counterpart")
        target = self.add_row("symlink residue zebraquux source")
        pages = self._pages_root()
        outside = Path(self.tmp) / "outside-symlink"
        outside.mkdir()
        outside_file = outside / "page.md"
        outside_file.write_text("unrelated outside bytes\n", encoding="utf-8")
        link = pages / "symlink"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink unavailable")
        before = outside_file.read_bytes()
        purge = self._purge(target)
        self.assertEqual(purge.returncode, 4, purge.stderr)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside_file.read_bytes(), before)
        self.assertEqual(self.qone("SELECT COUNT(*) FROM memory WHERE id=?", (target,)), 1)


if __name__ == "__main__":
    unittest.main()
