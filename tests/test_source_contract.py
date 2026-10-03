"""Focused trust-boundary regression tests for issue #139 source resolution."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from storelib.source import (  # noqa: E402
    SourceRefusal, _evidence, _file_window, _records, _safe_ref, _zcode,
    source_scan, source_show,
)
from storelib.write import redact_text  # noqa: E402


class SourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="zmem-source-contract-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_only_recognized_refs_cross_the_public_boundary(self) -> None:
        for value in ("C:relative.jsonl", "unknown:session/id", "https://host/a", "../escape.jsonl"):
            with self.assertRaises(SourceRefusal, msg=value):
                _safe_ref(value)
        self.assertEqual(_safe_ref("zcode:session/s-1"), "zcode:session/s-1")

    def test_unrecognized_jsonl_and_other_sessions_never_render(self) -> None:
        generic = self.root / "generic.jsonl"
        generic.write_text('{"session_id":"wanted","timestamp":"t0"}\n', encoding="utf-8")
        with self.assertRaises(SourceRefusal):
            _records(generic, "claude_transcript")
        transcript = self.root / "claude.jsonl"
        # A later wanted record must not make the resolver bridge across the
        # intervening physical session range.
        transcript.write_text(
            '{"type":"user","sessionId":"wanted","timestamp":"t0","message":{"content":"wanted"}}\n'
            '{"type":"user","sessionId":"other","timestamp":"t1","message":{"content":"other-secret"}}\n'
            '{"type":"assistant","sessionId":"wanted","timestamp":"t2","message":{"content":"wanted-later"}}\n',
            encoding="utf-8",
        )
        anchor = ("ev", "ev", "wanted", "claude", "t0", "wanted", "claude.jsonl", None)
        excerpt, detail, _ = _file_window(transcript, "claude_transcript", anchor=anchor, context=1)
        self.assertIn("wanted", excerpt)
        self.assertNotIn("other-secret", excerpt)
        self.assertNotIn("wanted-later", excerpt)
        self.assertEqual(detail["context_returned"] if "context_returned" in detail else detail["returned"], 1)

    def test_orphan_or_conflicting_associations_refuse(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE memory_evidence(memory_id TEXT,evidence_id TEXT)")
        conn.execute("CREATE TABLE evidence(id TEXT,session_id TEXT,lane TEXT,ts TEXT,excerpt TEXT,ref_path TEXT,ref_offset INTEGER)")
        conn.execute("INSERT INTO memory_evidence VALUES ('m','missing')")
        with self.assertRaises(SourceRefusal):
            _evidence(conn, "m")
        conn.execute("DELETE FROM memory_evidence")
        for ident, stamp in (("one", "t1"), ("two", "t2")):
            conn.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?)", (ident, "s", "claude", stamp, "e", "f.jsonl", 0))
            conn.execute("INSERT INTO memory_evidence VALUES ('m',?)", (ident,))
        with self.assertRaises(SourceRefusal):
            _evidence(conn, "m")

    def test_public_show_api_is_keyword_only_with_default_context(self) -> None:
        # Signature use is part of the API contract; resolution is intentionally
        # not attempted against an untrusted empty store.
        with self.assertRaises(TypeError):
            source_show(sqlite3.connect(":memory:"), "memory")

    def test_public_scan_covers_whole_run_caps_and_redacts_before_clipping(self) -> None:
        transcript = self.root / "run.jsonl"
        secret = "sk-" + ("Aa_-12" * 35)
        transcript.write_text("".join(
            '{"type":"user","sessionId":"s","timestamp":"t%d","message":{"content":"%s needle %d"}}\n'
            % (index, secret if index == 0 else "safe", index)
            for index in range(55)), encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE meta(key TEXT,value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version','14')")
        conn.execute("CREATE TABLE memory(id TEXT,namespace TEXT,source_ref TEXT)")
        conn.execute("CREATE TABLE memory_evidence(memory_id TEXT,evidence_id TEXT)")
        conn.execute("CREATE TABLE evidence(id TEXT,session_id TEXT,lane TEXT,ts TEXT,excerpt TEXT,ref_path TEXT,ref_offset INTEGER)")
        conn.execute("INSERT INTO memory VALUES ('m','project:source-contract','run.jsonl')")
        with patch.dict(os.environ, {"ZMEM_NAMESPACE": "project:source-contract", "ZMEM_TRANSCRIPT": str(transcript)}, clear=False):
            result = source_scan(conn, memory_id="m", needle="needle")
        self.assertEqual(result["match_count"], 55)
        self.assertEqual(len(result["matches"]), 50)
        self.assertTrue(result["truncated"])
        self.assertNotIn(secret[-80:], "\n".join(row["excerpt"] for row in result["matches"]))

    def test_native_anchor_matches_redacted_text_and_scan_offsets_are_null(self) -> None:
        db_path = self.root / "zcode.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE part(message_id TEXT,sequence INTEGER,data TEXT,session_id TEXT)")
        raw = "native " + "sk-" + ("Aa_-12" * 35) + " needle"
        import json
        conn.execute("INSERT INTO part VALUES ('turn-1',1,?, 's')", (json.dumps({"text": raw}),))
        conn.commit(); conn.close()
        anchor = ("e", "e", "s", "zcode", "t", redact_text(raw)[0], "zcode:session/s", None)
        with patch.dict(os.environ, {"ZMEM_ZCODE_DB": str(db_path)}, clear=False):
            _, detail, rows = _zcode("zcode:session/s", anchor, 0)
        self.assertEqual(detail["turn_start"], "turn-1")
        self.assertIsNone(rows[0]["start"])

    def test_large_file_refuses_before_parsing(self) -> None:
        oversized = self.root / "oversized.jsonl"
        oversized.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        with self.assertRaises(SourceRefusal):
            _records(oversized, "claude_transcript")

    def test_cli_missing_source_values_use_one_line_errors(self) -> None:
        store = SCRIPTS / "store.py"
        env = dict(os.environ, ZMEM_STORE=str(self.root / "absent.sqlite"))
        cases = ((["source", "--id", "m", "--context"], "store.py: error: --context must be between 0 and 20\n"),
                 (["source", "scan", "--needle", "x"], "store.py: error: --id is required\n"))
        for args, expected in cases:
            result = subprocess.run([sys.executable, str(store), *args], cwd=SCRIPTS.parents[2], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, expected)

    def test_cli_source_help_and_typed_errors_are_public_contract(self) -> None:
        store = SCRIPTS / "store.py"
        env = dict(os.environ, ZMEM_STORE=str(self.root / "absent.sqlite"))
        show_help = subprocess.run(
            [sys.executable, str(store), "source", "--help"],
            cwd=SCRIPTS.parents[2], env=env, capture_output=True, text=True, timeout=30,
        )
        scan_help = subprocess.run(
            [sys.executable, str(store), "source", "scan", "--help"],
            cwd=SCRIPTS.parents[2], env=env, capture_output=True, text=True, timeout=30,
        )
        malformed = subprocess.run(
            [sys.executable, str(store), "source", "--id", "m", "--context", "nope"],
            cwd=SCRIPTS.parents[2], env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(show_help.returncode, 0)
        self.assertIn("--id MEMORY_ID", show_help.stdout)
        self.assertIn("memory UUID", show_help.stdout)
        self.assertIn("--context CONTEXT", show_help.stdout)
        self.assertIn("turns (0..20)", show_help.stdout)
        self.assertEqual(scan_help.returncode, 0)
        self.assertIn("--id MEMORY_ID", scan_help.stdout)
        self.assertIn("--needle NEEDLE", scan_help.stdout)
        self.assertIn("literal substring", scan_help.stdout)
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stdout, "")
        self.assertEqual(
            malformed.stderr,
            "store.py: error: --context must be between 0 and 20\n",
        )


if __name__ == "__main__":
    unittest.main()
