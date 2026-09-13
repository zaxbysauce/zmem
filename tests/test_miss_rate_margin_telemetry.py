"""Regression tests for score-margin decision-log telemetry."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SESSION_START_PAYLOAD = (
    REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"
)
sys.path.insert(0, str(SCRIPTS))
from storelib import miss_rate  # noqa: E402


def _load_body():
    spec = importlib.util.spec_from_file_location(
        "zmem_recall_body_margin_telemetry_test", str(BODY))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarginTelemetryParserTest(unittest.TestCase):
    def test_legacy_and_margin_tails_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "zmem-decisions.log")
            path.write_text(
                "[1] zmem-hook status=injected reason=injected "
                "ids=['legacy'] all=['legacy'] sid=legacy\n"
                "[2] zmem-hook status=injected reason=injected "
                "ids=['top'] all=['top', 'second'] sid=margin "
                "moment=pretool batch=1 tools=Edit paths=notes.md "
                "margin=0.012500 margin_pruned=['second', 'third']\n",
                encoding="utf-8")

            parsed = miss_rate.parse_bg_log(path)

        self.assertEqual(len(parsed), 2)
        self.assertIsNone(parsed[0]["margin"])
        self.assertEqual(parsed[1]["margin"], "0.012500")
        self.assertEqual(parsed[1]["margin_pruned"], ["second", "third"])

    def test_recall_writer_round_trips_sanitized_pruned_ids(self):
        unsafe = ["second id", "third]id\nforged=field"]
        expected = ["second_id", "third_id_forged_field"]
        with tempfile.TemporaryDirectory() as tmp:
            module = _load_body()
            env = {
                "ZMEM_DATA": tmp,
                "ZMEM_STORE": "",
                "CLAUDE_PLUGIN_DATA": "",
                "ZCODE_PLUGIN_DATA": "",
            }
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(module, "_maybe_log_drift"), \
                    patch.object(module, "_rotate_telemetry_logs"):
                module._log_inject_decision(
                    [{"id": "top"}, {"id": "second"}],
                    [{"id": "top"}],
                    "injected", "injected", all_ids=["top", "second"],
                    session_id="margin-body", moment="user_prompt",
                    path_basenames=["notes.md"], margin=0.0125,
                    margin_pruned_ids=unsafe)

            parsed = miss_rate.parse_bg_log(
                Path(tmp, "zmem-decisions.log"))

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["margin"], "0.012500")
        self.assertEqual(parsed[0]["margin_pruned"], expected)

    def test_session_start_writer_sanitizes_pruned_ids(self):
        unsafe = ["second id", "third]id\nforged=field"]
        expected = ["second_id", "third_id_forged_field"]
        with tempfile.TemporaryDirectory() as tmp:
            old_argv = sys.argv
            sys.argv = [
                str(SESSION_START_PAYLOAD), "", "", str(SCRIPTS / "store.py"),
                tmp, tmp, tmp, "project:test", "25000", "", "", "",
                "margin-session", "", "",
            ]
            envelope = {
                "results": [{"id": "top", "content": "row"}],
                "reason": "injected",
                "candidate_ids": ["top", "second"],
                "margin": "0.012500",
                "margin_pruned_ids": unsafe,
            }
            try:
                with patch.dict(os.environ, {
                    "ZMEM_DATA": tmp,
                    "ZMEM_STORE": str(Path(tmp, "store.sqlite")),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0",
                    "PYTHONUTF8": "1",
                }, clear=False), \
                        patch.object(
                            __import__("subprocess"), "check_output",
                            return_value=json.dumps(envelope).encode("utf-8")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(
                        str(SESSION_START_PAYLOAD),
                        run_name="zmem_session_start_margin_telemetry_test")
            finally:
                sys.argv = old_argv

            parsed = miss_rate.parse_bg_log(
                Path(tmp, "zmem-decisions.log"))

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["margin"], "0.012500")
        self.assertEqual(parsed[0]["margin_pruned"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
