"""Focused regressions for PR #275 source and redaction feedback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from redaction import (  # noqa: E402
    _redact_secret_like_text_with_positions,
)
from storelib import source  # noqa: E402
from storelib.recall import _format_fenced_recall, _source_hint_namespaces  # noqa: E402


class RedactionPositionTests(unittest.TestCase):
    def test_position_engine_keeps_canonical_output_count_and_marker_noop(self):
        token = "sk-" + ("A" * 20) + "-"
        for text, expected, expected_count in (
            ("ordinary text", "ordinary text", 0),
            (token, "[REDACTED_SECRET]", 1),
            ("api_key=abcdefgh1234", "api_key=[REDACTED_SECRET]", 1),
            ("api_key=[REDACTED_SECRET]", "api_key=[REDACTED_SECRET]", 1),
        ):
            with self.subTest(text=text):
                actual, count, _positions = (
                    _redact_secret_like_text_with_positions(text, []))
                self.assertEqual(actual, expected)
                self.assertEqual(count, expected_count)

    def test_position_engine_maps_prefix_value_and_suffix(self):
        text = "pre api_key=abcdefgh1234 suffix"
        value_start = text.index("abcdefgh1234")
        suffix_start = text.index(" suffix")
        redacted, count, positions = _redact_secret_like_text_with_positions(
            text, [2, text.index("api_key"), value_start, suffix_start]
        )
        self.assertEqual(count, 1)
        self.assertEqual(redacted, "pre api_key=[REDACTED_SECRET] suffix")
        self.assertEqual(positions[0], 2)
        self.assertEqual(positions[1], text.index("api_key"))
        self.assertEqual(positions[2], len("pre api_key="))
        self.assertEqual(positions[3], len("pre api_key=[REDACTED_SECRET]"))


class SourceScanFeedbackTests(unittest.TestCase):
    @staticmethod
    def _scan(record: str, needle: str, start: int | None = 0) -> dict:
        shown = {"session_id": "session", "source_path": "fixture"}
        rows = [{"raw": record, "turn": "0", "start": start}]
        with patch.object(source, "_resolved", return_value=(shown, rows)):
            return source.source_scan(None, memory_id="memory", needle=needle)

    def test_each_far_apart_match_uses_its_own_center_and_original_bytes(self):
        prefix = "α\r\nfirst "
        record = prefix + "token " + ("." * 240) + " second token tail"
        result = self._scan(record, "token", start=17)
        self.assertEqual(result["match_count"], 2)
        first, second = result["matches"]
        self.assertIn("first token", first["excerpt"])
        self.assertNotIn("second token", first["excerpt"])
        self.assertIn("second token tail", second["excerpt"])
        self.assertNotIn("first token", second["excerpt"])
        self.assertEqual(first["byte_start"], 17 + len(prefix.encode("utf-8")))
        expected_second = 17 + len(
            (prefix + "token " + ("." * 240) + " second ").encode("utf-8")
        )
        self.assertEqual(second["byte_start"], expected_second)

    def test_secret_matches_and_marker_collisions_never_choose_by_display_find(self):
        secret = "sk-" + ("A" * 25) + "SECRET" + ("B" * 100) + "needle" + ("C" * 100)
        record = "before " + secret + (" z" * 150) + " plain SECRET tail"

        secret_result = self._scan(record, "needle")
        self.assertEqual(secret_result["match_count"], 1)
        self.assertIn("[REDACTED_SECRET]", secret_result["matches"][0]["excerpt"])
        self.assertNotIn(secret[-40:], secret_result["matches"][0]["excerpt"])

        collision_result = self._scan(record, "SECRET")
        self.assertEqual(collision_result["match_count"], 2)
        first, second = collision_result["matches"]
        self.assertIn("[REDACTED_SECRET]", first["excerpt"])
        self.assertNotIn(secret, first["excerpt"])
        self.assertIn("plain SECRET tail", second["excerpt"])
        self.assertNotIn("[REDACTED_SECRET]", second["excerpt"])


class SourceHintAndCliFeedbackTests(unittest.TestCase):
    def test_text_recall_hints_match_live_source_authorization(self):
        """The production renderer wire must not advertise refused source IDs.

        This is deliberately a CLI test rather than a formatter test: removing
        the ``source_hint_namespaces`` argument from ``_recall_memory_impl``
        makes the two authorized rows lose their hints and fails this test.
        """
        with tempfile.TemporaryDirectory(prefix="zmem-source-hint-") as temp:
            root = Path(temp)
            store_path = root / "store.sqlite"
            memory_file = root / "MEMORY.md"
            memory_file.write_text("# Source fixture\n", encoding="utf-8")
            env = dict(
                os.environ,
                ZMEM_STORE=str(store_path),
                ZMEM_DATA=str(root / "data"),
                ZMEM_MODELS_DIR=str(root / "missing-models"),
                ZMEM_MODEL_AUTODOWNLOAD="0",
                ZMEM_EMBED_PROFILE="fake",
                ZMEM_NAMESPACE="project:current",
                ZMEM_CODEX_MEMORY=str(memory_file),
            )

            def run(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "store.py"), *args],
                    cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                    timeout=30,
                )

            self.assertEqual(run("init").returncode, 0)

            def add(namespace: str, content: str) -> str:
                added = run(
                    "add", "--namespace", namespace, "--type", "fact",
                    "--content", content, "--source-ref", "MEMORY.md",
                    "--signal", "test", "--confidence", "0.9",
                )
                self.assertEqual(added.returncode, 0, added.stderr)
                conn = sqlite3.connect(store_path)
                try:
                    row = conn.execute(
                        "SELECT id FROM memory WHERE namespace=? AND content=?",
                        (namespace, content),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertIsNotNone(row)
                return str(row[0])

            current = add("project:current", "currenthintuniqueword")
            legacy = add("project:legacy", "legacyhintuniqueword")
            global_id = add("user:global", "globalhintuniqueword")
            foreign = add("project:foreign", "foreignhintuniqueword")
            conn = sqlite3.connect(store_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('ns_migration_v5', ?)",
                    (json.dumps({"project:legacy": "project:current"}),),
                )
                conn.commit()
            finally:
                conn.close()

            current_text = run(
                "recall", "--query", "currenthintuniqueword",
                "--namespace", "project:current",
            )
            legacy_text = run(
                "recall", "--query", "legacyhintuniqueword",
                "--namespace", "project:current",
            )
            global_text = run(
                "recall", "--query", "globalhintuniqueword",
                "--namespace", "project:current", "--include-global",
                "--global-limit", "5",
            )
            foreign_text = run(
                "recall", "--query", "foreignhintuniqueword",
                "--namespace", "project:foreign",
            )
            for result in (current_text, legacy_text, global_text, foreign_text):
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertIn(f"-> source {current}", current_text.stdout)
            self.assertIn(f"-> source {legacy}", legacy_text.stdout)
            self.assertNotIn(f"-> source {global_id}", global_text.stdout)
            self.assertNotIn(f"-> source {foreign}", foreign_text.stdout)

            self.assertEqual(run("source", "--id", current).returncode, 0)
            self.assertEqual(run("source", "--id", legacy).returncode, 0)
            self.assertEqual(run("source", "--id", global_id).returncode, 1)
            self.assertEqual(run("source", "--id", foreign).returncode, 1)

    def test_hints_use_source_current_namespace_and_project_aliases_only(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE meta(key TEXT, value TEXT)")
            conn.execute(
                "INSERT INTO meta VALUES ('ns_migration_v5', ?)",
                (json.dumps({"project:legacy": "project:current"}),),
            )
            rows = [
                {"id": "current", "confidence": 1, "signal": "test", "namespace": "project:current", "type": "fact", "content": "current"},
                {"id": "legacy", "confidence": 1, "signal": "test", "namespace": "project:legacy", "type": "fact", "content": "legacy"},
                {"id": "global", "confidence": 1, "signal": "test", "namespace": "user:global", "type": "fact", "content": "include-global row"},
                {"id": "foreign", "confidence": 1, "signal": "test", "namespace": "project:foreign", "type": "fact", "content": "explicit foreign row"},
            ]
            with patch.dict(os.environ, {"ZMEM_NAMESPACE": "project:current"}, clear=False):
                allowed = _source_hint_namespaces(conn)
            rendered = _format_fenced_recall(
                rows, "feedback", source_hint=True,
                source_hint_namespaces=allowed,
            )
        finally:
            conn.close()
        self.assertIn("-> source current", rendered)
        self.assertIn("-> source legacy", rendered)
        self.assertNotIn("-> source global", rendered)
        self.assertNotIn("-> source foreign", rendered)

    def test_source_argparse_errors_are_one_line_and_do_not_echo_unknown_values(self):
        store = SCRIPTS / "store.py"
        secret = "sk-" + ("A" * 20) + "-"
        cases = (
            ("source", "--id", "m", "--bogus"),
            ("source", "scan", "--id", "m", "--needle", "x", "--bogus"),
            ("source", "scan", "scan", "--id", "m", "--needle", "x"),
            ("source", "--id", "m", "--bogus=" + secret),
        )
        for args in cases:
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, str(store), *args], cwd=REPO_ROOT,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "store.py: error: unrecognized source arguments\n")
                self.assertNotIn(secret, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
