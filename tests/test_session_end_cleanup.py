"""Issue #189 SessionEnd cleanup and Codex host contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
BODY_PY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "session-end"
SESSION_ID = json.loads(
    (FIXTURE_DIR / "payload.json").read_text(encoding="utf-8")
)["session_id"]


def _sidecar_paths(data_dir: Path) -> dict[str, Path]:
    stem = hashlib.sha256(SESSION_ID.encode("utf-8")).hexdigest()[:32]
    ops = data_dir / "ops"
    return {
        suffix: ops / f"{stem}.{suffix}"
        for suffix in ("ledger", "pending", "compact", "tasktext")
    }


def _isolated_env(data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ZMEM_DATA": str(data_dir),
        "ZMEM_STORE": str(data_dir / "store.sqlite"),
        "ZMEM_MODELS_DIR": str(data_dir / "models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_NAMESPACE": "project:issue-189",
    })
    return env


class SessionEndCleanupTest(unittest.TestCase):
    def _seed(self, data_dir: Path) -> dict[str, Path]:
        paths = _sidecar_paths(data_dir)
        paths["ledger"].parent.mkdir(parents=True)
        sentinels = json.loads(
            (FIXTURE_DIR / "sentinels.json").read_text(encoding="utf-8")
        )
        for suffix, path in paths.items():
            path.write_text(sentinels[suffix], encoding="utf-8")
        return paths

    def test_ledger_clear_removes_both_delivery_sidecars_without_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="zmem-189-cli-") as raw:
            data_dir = Path(raw) / "data"
            paths = self._seed(data_dir)
            result = subprocess.run(
                [sys.executable, str(STORE_PY), "ledger-clear",
                 "--session-id", SESSION_ID],
                input="",
                capture_output=True,
                env=_isolated_env(data_dir),
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                json.dumps({"ok": True, "session_id": SESSION_ID,
                            "cleared": True}, separators=(",", ":")) + "\n",
            )
            self.assertFalse(paths["ledger"].exists())
            self.assertFalse(paths["pending"].exists())
            self.assertTrue(paths["compact"].exists())
            self.assertTrue(paths["tasktext"].exists())
            self.assertFalse((data_dir / "store.sqlite").exists())

            again = subprocess.run(
                [sys.executable, str(STORE_PY), "ledger-clear",
                 "--session-id", SESSION_ID],
                capture_output=True,
                env=_isolated_env(data_dir),
                text=True,
                timeout=30,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(again.stdout, result.stdout)

    def test_session_end_body_is_exact_empty_json_and_cleans_both_sidecars(self):
        with tempfile.TemporaryDirectory(prefix="zmem-189-body-") as raw:
            data_dir = Path(raw) / "data"
            paths = self._seed(data_dir)
            payload = (FIXTURE_DIR / "payload.json").read_text(encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(BODY_PY), str(STORE_PY),
                 "project:issue-189", "25000", "session_end"],
                input=payload,
                capture_output=True,
                env=_isolated_env(data_dir),
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "{}\n")
            self.assertEqual(result.stderr, "")
            self.assertFalse(paths["ledger"].exists())
            self.assertFalse(paths["pending"].exists())
            self.assertTrue(paths["compact"].exists())
            self.assertTrue(paths["tasktext"].exists())
            self.assertFalse((data_dir / "store.sqlite").exists())

    def test_codex_session_end_entry_is_main_thread_command_with_two_second_timeout(self):
        spec = json.loads(
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_text(encoding="utf-8")
        )
        groups = spec["hooks"].get("SessionEnd", [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].get("thread"), "main")
        hooks = groups[0].get("hooks", [])
        self.assertEqual(len(hooks), 1)
        hook = hooks[0]
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], 2)
        self.assertEqual(hook["commandWindows"],
                         "node ${PLUGIN_ROOT}/hooks/zmem-launch.js session-end")
        self.assertNotIn("additionalContextLimit", hook)
        self.assertNotIn("Interrupt", spec["hooks"])


if __name__ == "__main__":
    unittest.main()
