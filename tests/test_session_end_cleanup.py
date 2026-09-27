"""Issue #189 SessionEnd cleanup and Codex host contract tests."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
BODY_PY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "session-end"
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
SESSION_ID = json.loads(
    (FIXTURE_DIR / "payload.json").read_text(encoding="utf-8")
)["session_id"]


def _sidecar_paths_for(data_dir: Path, session_id: str) -> dict[str, Path]:
    stem = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    ops = data_dir / "ops"
    return {
        suffix: ops / f"{stem}.{suffix}"
        for suffix in ("ledger", "pending", "compact", "tasktext")
    }


def _sidecar_paths(data_dir: Path) -> dict[str, Path]:
    return _sidecar_paths_for(data_dir, SESSION_ID)


def _ended_marker(data_dir: Path, session_id: str) -> Path:
    stem = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return data_dir / "ops" / f"{stem}.delivery-ended"


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


class FakeScheduler:
    """Test-only clock used by the issue #189 body boundary helper."""

    def __init__(self) -> None:
        self._elapsed_ms = 0.0

    def advance(self, ms: float) -> None:
        self._elapsed_ms += ms

    def elapsed_ms(self) -> float:
        return self._elapsed_ms


def invoke_session_end(data_dir: Path, session_id: str,
                       scheduler: FakeScheduler) -> tuple[int, str, list[list[str]]]:
    """Run the real body with one fake #158 delivery-clear subprocess."""
    calls: list[list[str]] = []
    payload = json.dumps({"session_id": session_id})

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        from storelib import delivery_ledger
        delivery_ledger.clear_delivery_state(str(data_dir), session_id)
        scheduler.advance(25.0)
        return subprocess.CompletedProcess(argv, 0, "", "")

    env = _isolated_env(data_dir)
    original_env = os.environ.copy()
    original_argv = sys.argv[:]
    original_stdin = sys.stdin
    output = io.StringIO()
    error = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(env)
        sys.argv = [str(BODY_PY), str(STORE_PY), "user:global", "25000", "session_end"]
        sys.stdin = io.StringIO(payload)
        with patch("subprocess.run", side_effect=fake_run):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                try:
                    runpy.run_path(str(BODY_PY), run_name="__main__")
                except SystemExit as exc:
                    rc = int(exc.code or 0)
        # The body writes directly to stdout; the named helper's contract
        # records the invocation and elapsed control while the caller captures
        # the exact output in the integration test below.
        return rc, output.getvalue(), calls
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        sys.argv = original_argv
        sys.stdin = original_stdin


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

    def test_ledger_clear_removes_only_ledger_without_sqlite(self):
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
            self.assertTrue(paths["pending"].exists())
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

    def test_delivery_clear_cli_boundary_removes_both_sidecars_without_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="zmem-189-delivery-cli-") as raw:
            data_dir = Path(raw) / "data"
            paths = self._seed(data_dir)
            result = subprocess.run(
                [sys.executable, str(STORE_PY), "delivery-clear",
                 f"--session-id={SESSION_ID}"],
                capture_output=True, text=True, env=_isolated_env(data_dir),
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
            self.assertTrue(_ended_marker(data_dir, SESSION_ID).exists())
            self.assertFalse((data_dir / "store.sqlite").exists())
            again = subprocess.run(
                [sys.executable, str(STORE_PY), "delivery-clear",
                 f"--session-id={SESSION_ID}"],
                capture_output=True, text=True, env=_isolated_env(data_dir),
                timeout=30,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(again.stdout, result.stdout)
            self.assertTrue(_ended_marker(data_dir, SESSION_ID).exists())

    def test_delivery_clear_accepts_leading_dash_session_id(self):
        session_id = "-leading"
        with tempfile.TemporaryDirectory(prefix="zmem-189-leading-dash-") as raw:
            data_dir = Path(raw) / "data"
            paths = _sidecar_paths_for(data_dir, session_id)
            paths["ledger"].parent.mkdir(parents=True)
            paths["ledger"].write_text('{"entries":[]}', encoding="utf-8")
            paths["pending"].write_text('{"entries":[]}', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(STORE_PY), "delivery-clear",
                 "--session-id=-leading"],
                capture_output=True, text=True, env=_isolated_env(data_dir),
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                json.dumps({"ok": True, "session_id": session_id,
                            "cleared": True}, separators=(",", ":")) + "\n",
            )
            self.assertFalse(paths["ledger"].exists())
            self.assertFalse(paths["pending"].exists())
            self.assertTrue(_ended_marker(data_dir, session_id).exists())
            self.assertFalse((data_dir / "store.sqlite").exists())

    def test_session_end_clears_ledger_and_pending_via_store_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="zmem-189-fake-body-") as raw:
            data_dir = Path(raw) / "data"
            paths = _sidecar_paths(data_dir)
            paths["ledger"].parent.mkdir(parents=True)
            paths["ledger"].write_bytes((FIXTURE_DIR / "ledger.bin").read_bytes())
            paths["pending"].write_bytes((FIXTURE_DIR / "pending.bin").read_bytes())
            self.assertEqual((FIXTURE_DIR / "expected.json").read_bytes(), b"{}\n")
            self.assertEqual(
                (FIXTURE_DIR / "manifest.json").read_bytes(),
                b'{"schema":1,"session_id":"00000000-0000-4000-8000-000000000189",'
                b'"namespace":"user:global","timestamp":"2026-09-10T00:00:00Z",'
                b'"files":["ledger.bin","pending.bin","expected.json"]}\n',
            )
            scheduler = FakeScheduler()
            rc, stdout, calls = invoke_session_end(data_dir, SESSION_ID, scheduler)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "{}\n")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2:], ["delivery-clear", f"--session-id={SESSION_ID}"])
            self.assertEqual(scheduler.elapsed_ms(), 25.0)
            self.assertFalse(paths["ledger"].exists())
            self.assertFalse(paths["pending"].exists())

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
                         'node "${PLUGIN_ROOT}/hooks/zmem-launch.js" session-end')
        self.assertNotIn("additionalContextLimit", hook)
        self.assertNotIn("Interrupt", spec["hooks"])


if __name__ == "__main__":
    unittest.main()
