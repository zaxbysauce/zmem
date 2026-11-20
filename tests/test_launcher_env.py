"""Launcher capture-policy propagation tests.

Run: python -m unittest tests/test_launcher_env.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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


    def test_python_pipe_encoding_is_overridden_only_on_windows(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252"
        script = (
            "Object.defineProperty(process,'platform',{value:process.argv[2]});"
            "const launch=require(process.argv[1]);"
            "const out=launch.buildCanonicalEnv('codex',{},'session-start');"
            "process.stdout.write(JSON.stringify(out.PYTHONIOENCODING));"
        )
        for platform, expected in (("win32", "utf-8:surrogateescape"), ("linux", "cp1252")):
            with self.subTest(platform=platform):
                result = subprocess.run(
                    ["node", "-e", script, str(LAUNCHER), platform],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                self.assertEqual(json.loads(result.stdout), expected)


@unittest.skipUnless(os.name == "nt", "Windows launcher encoding contract")
class WindowsLauncherUtf8ContractTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(
            prefix="zmem-launcher-utf8-", ignore_cleanup_errors=True
        )
        self.addCleanup(scratch.cleanup)
        self.project = Path(scratch.name)
        self.data = self.project / "data"
        self.data.mkdir()
        self.env = os.environ.copy()
        for key in (
            "PYTHONUTF8", "PYTHONIOENCODING", "ZMEM_REMOTE_URL", "ZMEM_MCP_URL",
            "ZMEM_CAPTURE_MODE", "OPENCODE_PLUGIN_ROOT",
        ):
            self.env.pop(key, None)
        self.env.update({
            "PLUGIN_ROOT": str(REPO_ROOT),
            "ZMEM_HOST": "codex",
            "CODEX_PROJECT_DIR": str(self.project),
            "ZMEM_DATA": str(self.data),
            "ZMEM_STORE": str(self.data / "store.sqlite"),
            "ZMEM_INJECT": "1",
            "ZMEM_CAPTURE": "1",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_SESSION_CADENCE": "0",
            "ZMEM_BG_LOG": "0",
        })

    def _launch(self, hook, **payload):
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(
                ["node", str(LAUNCHER), hook],
                cwd=REPO_ROOT,
                env=self.env,
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                stdout=stdout,
                stderr=stderr,
                timeout=45,
            )
            stderr.seek(0)
            diagnostic = stderr.read(4096).decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertLessEqual(stdout.tell(), 256 * 1024, diagnostic)
            stdout.seek(0)
            return stdout.read(256 * 1024).decode("utf-8", "strict")

    def test_session_start_delivers_utf8_core_without_parent_override(self):
        marker = "UTF8-OUT → café 日本語 😀"
        core = self.project / "core.md"
        core.write_text(marker, encoding="utf-8")
        self.env["ZMEM_CORE_MD"] = str(core)

        output = self._launch("session-start", session_id="utf8-out", cwd=str(self.project))

        context = json.loads(output).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn(marker.encode("utf-8"), context.encode("utf-8"))
        self.assertNotIn("\ufffd", context)

    def test_capture_correction_preserves_raw_utf8_without_parent_override(self):
        prompt = "remember: use the → arrow, café and 日本語 😀 exactly"
        self._launch(
            "capture-correction", session_id="utf8-in", prompt=prompt, cwd=str(self.project)
        )

        messages = []
        for path in (self.data / "queue").glob("*.json"):
            for item in json.loads(path.read_text(encoding="utf-8")):
                if isinstance(item, dict) and isinstance(item.get("message"), str):
                    messages.append(item["message"].encode("utf-8"))
        self.assertEqual(messages, [prompt.encode("utf-8")])


if __name__ == "__main__":
    unittest.main()
