"""Regression tests for issue #244: the write-time fixture refusal.

store.py add must refuse fixture-shaped writes (the leaked-seed shapes: a
-seed-suffixed --source-ref, the user:long-row probe ref, the known fixture
namespaces) whenever the RESOLVED store is the operator's real home store
area — regardless of existence (first polluting write covered). Ordinary
default-store writes and isolated scratch writes stay allowed; the opt-out
is ZMEM_ALLOW_FIXTURE_WRITES=1. Subprocess-driven under a FAKE home, exactly
like the frozen C8 check, so nothing here can touch a real store.

Run: python tests/test_fixture_write_guard.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"


def _run_add(fakehome, namespace, source_ref, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = fakehome
    env["USERPROFILE"] = fakehome
    for k in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKENS_FILE",
              "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
              "ZCODE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT",
              "ZMEM_ALLOW_FIXTURE_WRITES"):
        env.pop(k, None)
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_MODELS_DIR"] = os.path.join(fakehome, "no-models")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(STORE_PY), "add",
         "--namespace", namespace, "--type", "fact",
         "--content", "fixture guard probe " + source_ref,
         "--source-ref", source_ref],
        capture_output=True, text=True, env=env, timeout=180,
        cwd=str(REPO_ROOT))


class FixtureWriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.fakehome = tempfile.mkdtemp(prefix="zmem-fwg-")

    def test_fixture_shaped_default_store_write_refused(self):
        r = _run_add(self.fakehome, "project:lanecompose",
                     "session:lane-seed")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("fixture", (r.stdout + r.stderr).lower())

    def test_fixture_shaped_explicit_real_path_refused(self):
        default = os.path.join(self.fakehome, ".zmem", "store.sqlite")
        r = _run_add(self.fakehome, "project:lanecompose",
                     "session:lane-seed", {"ZMEM_STORE": default})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_ordinary_default_store_write_allowed(self):
        r = _run_add(self.fakehome, "project:fwg-real", "session:real-op")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_scratch_store_fixture_write_allowed(self):
        r = _run_add(self.fakehome, "project:lanecompose",
                     "session:lane-seed",
                     {"ZMEM_STORE": os.path.join(self.fakehome,
                                                 "scratch.sqlite")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_opt_out_allows_fixture_write(self):
        r = _run_add(self.fakehome, "project:lanecompose",
                     "session:lane-seed", {"ZMEM_ALLOW_FIXTURE_WRITES": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_long_row_probe_ref_refused(self):
        r = _run_add(self.fakehome, "project:organize-77", "user:long-row")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
