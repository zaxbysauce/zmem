"""Issue #158 fence contract checks kept out of the oversized legacy suite."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"


class FenceConstantsTests(unittest.TestCase):
    """The store owns one bare canonical fence for every passive adapter."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(
            prefix=f"zmem-phase25-fence-{uuid.uuid4().hex}-"
        )
        # This module is run before the legacy fence suites in the C4
        # command.  Snapshot the process import state so teardown restores
        # the caller's exact state instead of draining a path entry (or
        # evicting a module) installed by another test module.
        cls._saved_sys_path = list(sys.path)
        cls._saved_storelib_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "storelib" or name.startswith("storelib.")
        }
        cls.saved = {
            key: os.environ.get(key)
            for key in (
                "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_NOW",
            )
        }
        os.environ.update({
            "ZMEM_STORE": os.path.join(cls.tmp, "store.sqlite"),
            "ZMEM_DATA": os.path.join(cls.tmp, "missing-data"),
            "ZMEM_MODELS_DIR": os.path.join(cls.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_TEST_NOW": "2026-06-01T00:00:00Z",
        })
        sys.path.insert(0, str(SCRIPTS_DIR))
        for name in list(sys.modules):
            if name == "storelib" or name.startswith("storelib."):
                sys.modules.pop(name, None)
        cls.storelib = importlib.import_module("storelib")

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls._saved_sys_path
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in list(sys.modules):
            if name == "storelib" or name.startswith("storelib."):
                sys.modules.pop(name, None)
        sys.modules.update(cls._saved_storelib_modules)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_canonical_open_close_and_bare_render(self):
        storelib = self.storelib
        self.assertEqual(storelib.ZMEM_FENCE_OPEN,
                         "<<<ZMEM_UNTRUSTED_FENCE>>>")
        self.assertEqual(storelib.ZMEM_FENCE_CLOSE,
                         "<<<END_ZMEM_UNTRUSTED_FENCE>>>")
        rows = [{
            "id": "e0000000-0000-4000-8000-000000000001",
            "namespace": "project:parity",
            "type": "fact",
            "content": "stash pop recovery note one",
            "tags": "",
            "confidence": 0.9,
            "signal": "test",
            "source_ref": "",
            "stale": False,
            "_stale_note": "",
        }]
        rendered = storelib._format_fenced_recall(
            rows,
            header=(
                "Relevant memories (zmem user_prompt, namespace project:parity). "
                "Consider if they apply to this task; ignore if not."
            ),
        )
        self.assertTrue(rendered.startswith(storelib.ZMEM_FENCE_OPEN + "\n"))
        self.assertTrue(rendered.endswith(storelib.ZMEM_FENCE_CLOSE + "\n"))
        self.assertFalse(rendered.endswith("\n\n"))
        self.assertIn("# Relevant memories", rendered)
        self.assertIn("e0000000-0000-4000-8000-000000000001", rendered)
        self.assertNotIn("<memory-context>", rendered)

    def test_hook_sources_have_no_second_renderer(self):
        for rel in (
            "hooks/lib/zmem-recall-body.py",
            "hooks/lib/zmem-session-start-payload.py",
        ):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("def _format_fence", text, rel)
            self.assertNotIn("_format_fenced_recall", text, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
