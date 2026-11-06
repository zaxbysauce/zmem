"""Issue #439 AC17: one shared failure classifier, parity across substrates.

The frozen post-fix contract: ``classify_failure(detail)`` is importable
from ``skills/memory/scripts/capture_quality.py`` and returns exactly one
of ``"trivial"`` / ``"signal"`` / ``"other"`` for a failure detail dict.

- trivial: a read-only search miss -- a Grep/Glob error, a Read whose
  error names a nonexistent path, or a Bash non-zero where every
  ``&&``/``;``/``|`` segment's first token is one of
  grep/rg/find/ls/head/tail/wc/cat, no redirection/substitution
  characters appear, no write-capable option token appears, and no
  segment is a runner;
- signal: a recognized runner failure (infer_signal test/compile/lint) or
  a user rejection;
- other: everything else (today's behavior -- the sidecar still fires).

Both failure paths must agree: the transcript path
(zmem-subagent-reflect.sh) and the ZCode database path (the mine.py details
feed) classify EQUIVALENT fixtures identically. This test feeds the same
failure in both substrate shapes (the transcript detail shape and the
enriched db detail shape) and pins the verdict per pair.

At base ``classify_failure`` does not exist, so the import fails -- the
NEW-SURFACE base signature.

Run: python tests/test_classifier_parity.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from capture_quality import classify_failure  # noqa: E402

VERDICTS = ("trivial", "signal", "other")

# Equivalent fixture pairs: the same failure rendered in the transcript
# detail shape and in the enriched ZCode-db detail shape.
_TRANSCRIPT_BASE = {"error": "Exit code 1"}
_DB_BASE = {"error": "Exit code 1", "error_type": "", "retry_count": 0,
            "destructive": False}

FIXTURE_PAIRS = [
    ("bash-grep-miss",
     dict(_TRANSCRIPT_BASE, tool="Bash", command="grep -r needle /nope",
          path=""),
     dict(_DB_BASE, tool="Bash", command="grep -r needle /nope", path=""),
     "trivial"),
    ("bash-pipeline-of-readers",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="ls -1 src && grep -c thing notes.txt; wc -l notes.txt",
          path=""),
     dict(_DB_BASE, tool="Bash",
          command="ls -1 src && grep -c thing notes.txt; wc -l notes.txt",
          path=""),
     "trivial"),
    ("grep-tool-miss",
     dict(_TRANSCRIPT_BASE, tool="Grep", command="", path="/nonexistent"),
     dict(_DB_BASE, tool="Grep", command="", path="/nonexistent"),
     "trivial"),
    ("read-nonexistent-path",
     dict(_TRANSCRIPT_BASE, tool="Read", command="", path="/nonexistent.md",
          error="File does not exist: /nonexistent.md"),
     dict(_DB_BASE, tool="Read", command="", path="/nonexistent.md",
          error="File does not exist: /nonexistent.md"),
     "trivial"),
    ("read-permission-denied",
     dict(_TRANSCRIPT_BASE, tool="Read", command="", path="/etc/shadow",
          error="Permission denied"),
     dict(_DB_BASE, tool="Read", command="", path="/etc/shadow",
          error="Permission denied"),
     "other"),
    ("bash-find-fprint-write",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="find /nonexistent -fprint out.txt", path=""),
     dict(_DB_BASE, tool="Bash",
          command="find /nonexistent -fprint out.txt", path=""),
     "other"),
    ("bash-rg-pre-exec",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="rg --pre helper.sh needle /nope", path=""),
     dict(_DB_BASE, tool="Bash",
          command="rg --pre helper.sh needle /nope", path=""),
     "other"),
    ("bash-rg-pre-attached",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="rg --pre=helper.sh needle /nope", path=""),
     dict(_DB_BASE, tool="Bash",
          command="rg --pre=helper.sh needle /nope", path=""),
     "other"),
    ("bash-reader-then-runner-multiline",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="cat missing.txt" + chr(10) + "pytest tests/test_example.py",
          path=""),
     dict(_DB_BASE, tool="Bash",
          command="cat missing.txt" + chr(10) + "pytest tests/test_example.py",
          path=""),
     "other"),
    ("bash-background-write",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="cat x & rm -rf build", path=""),
     dict(_DB_BASE, tool="Bash",
          command="cat x & rm -rf build", path=""),
     "other"),
    ("bash-redirect-write",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="grep foo needle > out.txt", path=""),
     dict(_DB_BASE, tool="Bash",
          command="grep foo needle > out.txt", path=""),
     "other"),
    ("bash-stdin-redirect",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="cat < /etc/infile", path=""),
     dict(_DB_BASE, tool="Bash",
          command="cat < /etc/infile", path=""),
     "other"),
    ("bash-command-substitution",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="cat " + chr(36) + "(rm -rf x)", path=""),
     dict(_DB_BASE, tool="Bash",
          command="cat " + chr(36) + "(rm -rf x)", path=""),
     "other"),
    ("bash-backtick-exec",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="ls " + chr(96) + "touch y" + chr(96), path=""),
     dict(_DB_BASE, tool="Bash",
          command="ls " + chr(96) + "touch y" + chr(96), path=""),
     "other"),
    ("runner-test-failure",
     dict(_TRANSCRIPT_BASE, tool="Bash",
          command="pytest tests/test_example.py", path=""),
     dict(_DB_BASE, tool="Bash", command="pytest tests/test_example.py",
          path=""),
     "signal"),
    ("unrecognized-runner",
     dict(_TRANSCRIPT_BASE, tool="Bash", command="npx vitest run", path=""),
     dict(_DB_BASE, tool="Bash", command="npx vitest run", path=""),
     "other"),
    ("write-tool-failure",
     dict(_TRANSCRIPT_BASE, tool="Edit", command="",
          path="src/locked.py"),
     dict(_DB_BASE, tool="Edit", command="", path="src/locked.py"),
     "other"),
]


class ClassifierParityTest(unittest.TestCase):
    def test_verdicts_and_parity_across_substrate_shapes(self):
        for name, transcript_detail, db_detail, expected in FIXTURE_PAIRS:
            with self.subTest(failure=name):
                verdict_t = classify_failure(dict(transcript_detail))
                verdict_d = classify_failure(dict(db_detail))
                self.assertIn(verdict_t, VERDICTS)
                self.assertIn(verdict_d, VERDICTS)
                self.assertEqual(
                    verdict_t, expected,
                    "transcript-shape fixture %s must classify %s" %
                    (name, expected))
                self.assertEqual(
                    verdict_d, expected,
                    "the equivalent ZCode-db-shape fixture %s must classify "
                    "%s (one classifier, both paths)" % (name, expected))
                self.assertEqual(
                    verdict_t, verdict_d,
                    "the two substrate shapes of %s must agree" % name)


class DbWiringIntegrationTest(unittest.TestCase):
    """The classifier is genuinely consumed on the ZCode database path:
    mine.py's db branch recovers command/path by joining tool_call_id to
    the SEPARATE ``part`` table (records keyed by callID carrying
    state.input -- the miss_rate._failures_from_part_inputs precedent;
    tool_usage itself has no command column) and annotates every detail
    with classify_failure's verdict. Killing mutations for the bundled
    C17 argv: removing classify_failure fails the module import; removing
    the DB call site in mine.py fails the verdict assertions below while
    the pure-parity class stays green."""

    def _make_db(self, with_part_table: bool, with_enrichment: bool = True):
        import json as _json
        import os as _os
        import sqlite3 as _sqlite3
        import tempfile as _tempfile

        def _cleanup_db(p):
            # main db + sqlite3 -wal/-shm siblings (NEW-3a leak pin)
            for s in ("", "-wal", "-shm"):
                try:
                    _os.remove(p + s)
                except OSError:
                    pass

        fd, path = _tempfile.mkstemp(suffix=".sqlite")
        _os.close(fd)
        self.addCleanup(_cleanup_db, path)
        conn = _sqlite3.connect(path)
        cols = ("session_id TEXT, tool_name TEXT, read_only INT, "
                "status TEXT, exit_code INT, error_message TEXT, "
                "error_type TEXT, retry_count INT, destructive INT, "
                "completed_at TEXT, tool_call_id TEXT")
        if not with_enrichment:
            cols = ("session_id TEXT, tool_name TEXT, read_only INT, "
                    "status TEXT, exit_code INT, tool_call_id TEXT")
        conn.execute("CREATE TABLE tool_usage(%s)" % cols)

        def row(sid, tool, read_only, status, exit_code, msg, etype,
                retry, destructive, completed, tcid):
            if with_enrichment:
                return (sid, tool, read_only, status, exit_code, msg,
                        etype, retry, destructive, completed, tcid)
            return (sid, tool, read_only, status, exit_code, tcid)

        rows = [
            row("s1", "Bash", 0, "error", 1, "Exit code 1", "", 0, 0,
                "2026-01-01", "tc1"),
            row("s1", "Bash", 0, "error", 1, "Exit code 1", "", 0, 0,
                "2026-01-02", "tc2"),
            row("s1", "Edit", 0, None, 3, "edit refused", "", 0, 0,
                "2026-01-03", "tc3"),
        ]
        conn.executemany("INSERT INTO tool_usage VALUES (%s)"
                         % ",".join("?" * len(rows[0])), rows)
        if with_part_table:
            conn.execute(
                "CREATE TABLE part (id TEXT, message_id TEXT, "
                "session_id TEXT, time_created INTEGER, "
                "time_updated INTEGER, data TEXT, sequence INTEGER)")
            parts = [
                ("tc1", "Bash", {"command": "grep -r needle /nope"}),
                ("tc2", "Bash", {"command": "pytest tests/test_example.py"}),
                ("tc3", "Edit", {"file_path": "src/locked.py"}),
            ]
            for seq, (tcid, tool, inp) in enumerate(parts):
                conn.execute(
                    "INSERT INTO part (session_id, data) VALUES (?, ?)",
                    ("s1", _json.dumps({
                        "type": "tool", "callID": tcid, "tool": tool,
                        "state": {"status": "error", "input": inp},
                    })))
        conn.commit()
        conn.close()
        return path

    def _failures(self, db):
        import io
        import contextlib
        from storelib import mine
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mine.cmd_failures("s1", "", db)
        self.assertEqual(rc, 0)
        return _json_loads(buf.getvalue())

    def test_db_details_recover_inputs_and_verdicts(self):
        obj = self._failures(self._make_db(with_part_table=True))
        self.assertEqual(obj.get("count"), 3)
        by_tool = {}
        for d in obj["details"]:
            by_tool.setdefault(d["tool"], []).append(d)
        greps = [d for d in by_tool["Bash"]
                 if d.get("command") == "grep -r needle /nope"]
        self.assertEqual(len(greps), 1)
        self.assertEqual(greps[0].get("path"), "")
        self.assertEqual(greps[0].get("verdict"), "trivial")
        pytest = [d for d in by_tool["Bash"]
                  if d.get("command") == "pytest tests/test_example.py"]
        self.assertEqual(len(pytest), 1)
        self.assertEqual(pytest[0].get("verdict"), "signal")
        self.assertEqual(len(by_tool["Edit"]), 1)
        self.assertEqual(by_tool["Edit"][0].get("path"), "src/locked.py")
        self.assertEqual(by_tool["Edit"][0].get("verdict"), "other")

    def test_db_part_table_absent_falls_back_to_empty_inputs(self):
        # tool_call_id exists but the part table is absent: the join is
        # empty and command/path degrade to "" without discarding the
        # other detail fields or the verdict.
        obj = self._failures(self._make_db(with_part_table=False))
        self.assertEqual(obj.get("count"), 3)
        for d in obj["details"]:
            self.assertEqual(d.get("command"), "")
            self.assertEqual(d.get("path"), "")
            self.assertIn("verdict", d)

    def test_db_schema_drift_falls_back_to_empty_inputs(self):
        obj = self._failures(self._make_db(with_part_table=False,
                                           with_enrichment=False))
        self.assertEqual(obj.get("count"), 3)
        for d in obj["details"]:
            self.assertIn("command", d)
            self.assertIn("path", d)
            self.assertEqual(d["command"], "")
            self.assertEqual(d["path"], "")
            self.assertIn("verdict", d)


def _json_loads(text):
    import json as _json_mod
    return _json_mod.loads(text)


if __name__ == "__main__":
    unittest.main()
