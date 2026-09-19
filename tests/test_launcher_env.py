"""Launcher capture-policy propagation tests.

Run: python -m unittest tests/test_launcher_env.py
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "hooks" / "zmem-launch.js"


class LauncherEnvironmentTest(unittest.TestCase):
    def _child_value(self, parent_value: str | None) -> str:
        env = os.environ.copy()
        env.pop("ZMEM_CAPTURE", None)
        env["ZMEM_HOST"] = "zcode"
        if parent_value is not None:
            env["ZMEM_CAPTURE"] = parent_value
        script = (
            "const launch=require(process.argv[1]);"
            "const out=launch.buildCanonicalEnv('zcode',"
            "{cwd:process.cwd(),session_id:'launcher-env-test'},'reflect');"
            "process.stdout.write(JSON.stringify(out.ZMEM_CAPTURE));"
        )
        result = subprocess.run(
            ["node", "-e", script, str(LAUNCHER)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_parent_capture_value_reaches_child(self):
        for value in ("0", "1", ""):
            with self.subTest(value=value):
                self.assertEqual(self._child_value(value), value)

    def test_undefined_capture_defaults_only_to_one(self):
        self.assertEqual(self._child_value(None), "1")


if __name__ == "__main__":
    unittest.main()
