"""Behavioral regressions for issue #139 explicit-file source authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib.source import (  # noqa: E402
    SourceRefusal,
    _configured_file,
    _records,
    _safe_ref,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class SourceFileAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-source-authority-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _source_env(self, **overrides: str) -> dict[str, str]:
        values = {
            "ZMEM_TRANSCRIPT": "",
            "ZMEM_AGENT_TRANSCRIPT": "",
            "ZMEM_CODEX_MEMORY": "",
            "ZMEM_HERMES_SESSIONS": str(self.root / "no-hermes-sessions"),
            "HERMES_HOME": str(self.root / "no-hermes-home"),
        }
        values.update(overrides)
        return values

    @staticmethod
    def _write_claude_transcript(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "authority-session",
                    "timestamp": "2026-10-03T00:00:00Z",
                    "message": {"content": "configured transcript"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_codex_memory(path: Path) -> None:
        path.write_text(
            "# Configured memory\n\nconfigured Codex memory\n", encoding="utf-8"
        )

    @staticmethod
    def _resolve(ref: str) -> tuple[Path, str]:
        return _configured_file(_safe_ref(ref))

    def test_each_explicit_file_knob_accepts_its_exact_regular_file(self) -> None:
        self.assertFalse(self.root.resolve().is_relative_to(REPO_ROOT))
        cases = (
            (
                "ZMEM_TRANSCRIPT",
                "transcript.jsonl",
                "claude_transcript",
                self._write_claude_transcript,
            ),
            (
                "ZMEM_AGENT_TRANSCRIPT",
                "agent.jsonl",
                "claude_transcript",
                self._write_claude_transcript,
            ),
            (
                "ZMEM_CODEX_MEMORY",
                "MEMORY.md",
                "codex_session",
                self._write_codex_memory,
            ),
        )

        for key, name, kind, writer in cases:
            with self.subTest(key=key):
                path = self.root / name
                writer(path)
                with patch.dict(
                    os.environ,
                    self._source_env(**{key: str(path)}),
                    clear=False,
                ):
                    selected, selected_kind = self._resolve(name)
                    raw, records = _records(selected, selected_kind)

                self.assertEqual(selected, path)
                self.assertEqual(selected_kind, kind)
                self.assertTrue(raw)
                self.assertEqual(len(records), 1)

    def test_configured_file_does_not_authorize_a_sibling(self) -> None:
        selected = self.root / "selected.jsonl"
        sibling = self.root / "sibling.jsonl"
        self._write_claude_transcript(selected)
        self._write_claude_transcript(sibling)

        with patch.dict(
            os.environ,
            self._source_env(ZMEM_TRANSCRIPT=str(selected)),
            clear=False,
        ):
            with self.assertRaises(SourceRefusal):
                self._resolve(sibling.name)

    def test_unsafe_refs_refuse_even_when_their_basename_is_configured(
        self,
    ) -> None:
        selected = self.root / "selected.jsonl"
        self._write_claude_transcript(selected)
        unsafe_refs = (
            str(selected.resolve()),
            "https://example.test/selected.jsonl",
            r"\\server\share\selected.jsonl",
            "../selected.jsonl",
        )

        with patch.dict(
            os.environ,
            self._source_env(ZMEM_TRANSCRIPT=str(selected)),
            clear=False,
        ):
            for ref in unsafe_refs:
                with self.subTest(ref=ref), self.assertRaises(SourceRefusal):
                    self._resolve(ref)


if __name__ == "__main__":
    unittest.main()
