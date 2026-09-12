"""Miss-rate join tests (issue #94, tasks 2-4).

Proves, on synthetic fixtures (model-absent):
- parse_bg_log handles both writer shapes, legacy sid-less lines, torn and
  maintenance lines;
- failures_from_db_rich recovers the failed operation (command / file_path)
  from the ZCode db's part table, converts completed_at ms→s, and honors
  the >= since / < until bounds;
- the join classifies surfaced_sid / surfaced_legacy / missed (+all_only) /
  capture_gap / no_query exactly per the pinned definitions, including
  window edges and partial bg-log coverage;
- ring events strictly BEFORE the failure feed the query (subagent-named
  rings included), with the operation chain taking precedence;
- doctor --miss-rate REFUSES the host-default store without running the
  join and without creating any store file;
- an end-to-end report run leaves the store byte-identical (mode=ro +
  no_telemetry + link_hops=0 — the zero-write guarantee rests on all
  three) and creates no -wal/-shm artifacts;
- the join pins link_hops=0 (source pin: the matched set must stay exactly
  the query-ranked rows).

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process. The operator's real store is never touched.

Runs standalone: python tests/test_miss_rate.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
DOCTOR_PY = SCRIPTS / "doctor.py"
sys.path.insert(0, str(SCRIPTS))
from storelib import miss_rate  # noqa: E402

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _seed_and_get_id(tmp: str, ns: str, content: str) -> str:
    r = subprocess.run(
        [sys.executable, str(STORE_PY), "add",
         "--namespace", ns, "--type", "lesson", "--content", content,
         "--signal", "test", "--json"],
        capture_output=True, text=True, env=_clean_env(tmp), timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"
    # add --json prints the added row; fall back to a direct ro read.
    try:
        obj = json.loads(r.stdout.strip().splitlines()[-1])
        rid = obj.get("id")
        if rid:
            return rid
    except (ValueError, IndexError):
        pass
    conn = sqlite3.connect("file:" + os.path.join(tmp, "store.sqlite")
                           .replace(chr(92), "/") + "?mode=ro", uri=True)
    row = conn.execute(
        "SELECT id FROM memory WHERE content LIKE ? ORDER BY rowid DESC",
        (content[:40] + "%",)).fetchone()
    conn.close()
    assert row, "seeded row not found"
    return row[0]


def _make_fixture_db(path: str, failures: list) -> None:
    """Synthetic ZCode episodic db: tool_usage + part tables with the real
    column shapes the miners query."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE tool_usage (
        id TEXT, session_id TEXT NOT NULL, turn_id TEXT, trace_id TEXT,
        tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
        side_effect_scope TEXT, read_only INTEGER, destructive INTEGER,
        approval_status TEXT, status TEXT NOT NULL, started_at INTEGER,
        first_output_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
        time_to_first_output_ms INTEGER, exit_code INTEGER,
        output_bytes INTEGER NOT NULL DEFAULT 0,
        stdout_bytes INTEGER NOT NULL DEFAULT 0,
        stderr_bytes INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0,
        retry_count INTEGER NOT NULL DEFAULT 0,
        retryable INTEGER NOT NULL DEFAULT 0,
        cancelled_by_user INTEGER, error_type TEXT, error_code TEXT,
        error_message TEXT)""")
    conn.execute("""CREATE TABLE part (
        id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER,
        time_updated INTEGER, data TEXT, sequence INTEGER)""")
    for f in failures:
        conn.execute(
            "INSERT INTO tool_usage (session_id, completed_at, tool_name,"
            " tool_call_id, read_only, status, exit_code, error_message,"
            " error_type) VALUES (?, ?, ?, ?, 0, ?, ?, ?, '')",
            (f["session_id"], int(f["ts_s"]) * 1000, f.get("tool", "Bash"),
             f["call_id"], f.get("status", "completed"),
             f.get("exit_code", 1), f.get("error_message", "")))
        if f.get("operation"):
            conn.execute(
                "INSERT INTO part (session_id, data) VALUES (?, ?)",
                (f["session_id"], json.dumps({
                    "type": "tool", "callID": f["call_id"],
                    "tool": f.get("tool", "Bash"),
                    "state": {"status": "error",
                              "input": {"command": f["operation"]},
                              "error": f.get("error_message", "")},
                })))
    conn.commit()
    conn.close()


def _bg_line(ts: int, ids, all_ids=None, sid="sess-a",
             reason="injected", status="injected",
             moment=None) -> str:
    ids = list(ids)
    all_ids = list(all_ids if all_ids is not None else ids)
    line = f"[{ts}] zmem-hook status={status}"
    if reason is not None:
        line += f" reason={reason}"
    line += f" ids={ids} all={all_ids}"
    if sid is not None:
        line += f" sid={sid}"
    if moment is not None:
        line += f" moment={moment}"
    return line


class ParseBgLogTest(unittest.TestCase):
    def test_both_writer_shapes_and_legacy_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zmem-bg.log")
            Path(path).write_text(
                "[5] zmem-hook status=injected reason=injected"
                " ids=['a'] all=['a', 'b'] tokens=10/1500 ops=3 sid=sess-1\n"
                "[6] zmem-hook status=silent ids=[] all=[]"
                " tokens=0/1500 sid=sess-2\n"
                "[7] zmem-hook status=injected reason=injected"
                " ids=['c'] all=['c'] tokens=1/1500\n"
                "[zmem] backup: snapshot ok\n"
                "[8] zmem-hook status=injected reason=injected ids=['to\n",
                encoding="utf-8")
            lines = miss_rate.parse_bg_log(path)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["sid"], "sess-1")
        self.assertEqual(lines[0]["ops"], 3)
        self.assertEqual(lines[0]["ids"], ["a"])
        self.assertEqual(lines[0]["all"], ["a", "b"])
        self.assertEqual(lines[1]["sid"], "sess-2")
        self.assertIsNone(lines[1]["reason"])  # writer B shape
        self.assertIsNone(lines[2]["sid"])     # legacy line
        self.assertEqual(lines[2]["ids"], ["c"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(miss_rate.parse_bg_log("Z:/no/such/log"), [])

    def test_posttoolbatch_batch_tail_lines_parse(self):
        # Issue #120 / PR #193 review C-F3-001: the posttoolbatch writer
        # appends batch=1 tools=... paths=... AFTER moment=/arms=. The
        # reader's optional-group chain must keep matching (a silent skip
        # here reclassifies delivered rows as misses in the #100-gated
        # report) while pre-#120 lines keep parsing byte-compatibly.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zmem-bg.log")
            Path(path).write_text(
                "[9] zmem-hook status=injected reason=injected"
                " ids=['a'] all=['a'] tokens=10/1500 ops=2 sid=sess-b"
                " moment=pretool arms=fts:1/15,vec:0/25"
                " batch=1 tools=Edit,Write,Bash paths=a.py,guide.md\n"
                "[10] zmem-hook status=silent reason=empty-pool"
                " ids=[] all=[] ops=0 sid=sess-b moment=pretool batch=1\n"
                "[11] zmem-hook status=injected reason=injected"
                " ids=['x'] all=['x'] tokens=1/1500 sid=sess-c"
                " moment=pretool arms=fts:1/15\n",
                encoding="utf-8")
            lines = miss_rate.parse_bg_log(path)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["sid"], "sess-b")
        self.assertEqual(lines[0]["reason"], "injected")
        self.assertEqual(lines[1]["reason"], "empty-pool")
        self.assertEqual(lines[2]["arms"], "fts:1/15")  # pre-#120 shape intact


class FailuresFromDbRichTest(unittest.TestCase):
    def test_operation_recovery_and_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            now = int(time.time())
            _make_fixture_db(db, [
                {"session_id": "sess-a", "ts_s": now - 100,
                 "call_id": "call-1", "tool": "Bash",
                 "operation": "git stash pop",
                 "error_message": "conflict in foo.py"},
                {"session_id": "sess-a", "ts_s": now - 50,
                 "call_id": "call-2", "tool": "Edit",
                 "operation": "src/gates/config.ts",
                 "error_message": ""},
                # read-only failure row that must be EXCLUDED
                {"session_id": "sess-a", "ts_s": now - 10,
                 "call_id": "call-3", "tool": "Read",
                 "operation": "read-only-op"},
            ])
            # mark call-3 read_only via a direct update (fixture helper
            # writes read_only=0 for all rows)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE tool_usage SET read_only=1"
                         " WHERE tool_call_id='call-3'")
            conn.commit()
            conn.close()
            rows = miss_rate.failures_from_db_rich(db)
            self.assertEqual(len(rows), 2)
            by_call = {r["call_id"]: r for r in rows}
            self.assertEqual(by_call["call-1"]["operation"], "git stash pop")
            self.assertEqual(by_call["call-2"]["operation"],
                             "src/gates/config.ts")
            self.assertEqual(by_call["call-1"]["ts_s"], now - 100)
            self.assertEqual(by_call["call-1"]["error"],
                             "conflict in foo.py")
            # since/until bounds: >= since, < until (on completed_at/1000)
            only_first = miss_rate.failures_from_db_rich(
                db, since_s=now - 101, until_s=now - 50)
            self.assertEqual([r["call_id"] for r in only_first], ["call-1"])
            self.assertEqual(
                miss_rate.failures_from_db_rich("Z:/no/db.sqlite"), [])

    def test_no_query_bucket_when_operation_derives_nothing(self):
        # A failure whose operation and tool derive zero tokens (no ring,
        # bare name) lands in no_query — NOT capture_gap.
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            now = int(time.time())
            _make_fixture_db(db, [
                {"session_id": "sess-a", "ts_s": now,
                 "call_id": "call-x", "tool": "WebSearch",
                 "operation": ""},
            ])
            rows = miss_rate.failures_from_db_rich(db)
            self.assertEqual(rows[0]["operation"], "")


class _JoinFixture:
    """Temp store seeded with one 'git stash pop' lesson + fixture db with
    one matching Bash failure; bg log written per scenario."""

    NS = "project:mr94"
    LESSON = ("mrcanary: git stash pop conflicts need stash drop after "
              "resolve, verified by test")

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-mr94-")
        self.now = int(time.time())
        self.row_id = _seed_and_get_id(self._tmp, self.NS, self.LESSON)
        self.db = os.path.join(self._tmp, "db.sqlite")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fixture(self, *, session="sess-a", ts=None, operation=None,
                 tool="Bash"):
        ts = self.now if ts is None else ts
        op = "git stash pop" if operation is None else operation
        _make_fixture_db(self.db, [
            {"session_id": session, "ts_s": ts, "call_id": "call-1",
             "tool": tool, "operation": op,
             "error_message": "conflict"},
        ])

    def _bg_log(self, lines):
        Path(os.path.join(self._tmp, "zmem-bg.log")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def _run(self, **kwargs):
        return miss_rate.run_miss_report(
            store_path=os.path.join(self._tmp, "store.sqlite"),
            db_path=self.db, **kwargs)


class DisabledWindowClassificationTest(_JoinFixture, unittest.TestCase):
    """Issue #133 (PR #132 follow-up): reason=disabled bg-log lines are the
    ZMEM_INJECT=0 kill-switch marker — a failure in such a window is "switch
    off", never a retrieval miss. Pre-#133 every disabled line collapsed
    into missed and a disabled box reported a false miss_rate=1.0."""

    def test_disabled_only_window_is_switch_off_not_miss(self):
        self._fixture()
        self._bg_log([
            _bg_line(self.now, [], reason="disabled", status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 1)
        self.assertEqual(report["counts"]["missed"], 0)
        self.assertIsNone(report["miss_rate"],
                          "a disabled-only report reads as switch-off, "
                          "never 100% miss")
        self.assertTrue(any("switch off" in c for c in report["caveats"]),
                        report["caveats"])

    def test_mixed_disabled_window_and_genuine_miss(self):
        _make_fixture_db(self.db, [
            {"session_id": "sess-a", "ts_s": self.now, "call_id": "call-1",
             "tool": "Bash", "operation": "git stash pop",
             "error_message": "conflict"},
            {"session_id": "sess-b", "ts_s": self.now - 7200,
             "call_id": "call-2", "tool": "Bash",
             "operation": "git stash pop", "error_message": "conflict"},
        ])
        self._bg_log([
            # switch off around the FIRST failure only
            _bg_line(self.now, [], reason="disabled", status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 1)
        self.assertEqual(report["counts"]["missed"], 1,
                         "the out-of-window failure stays a real miss")
        self.assertEqual(report["miss_rate"], 1.0,
                         "1 real miss over 1 measured failure")

    def test_cross_session_disabled_line_does_not_absorb_a_miss(self):
        # PRR-004 (PR #140 review round): disabled-window attribution must be
        # sid-correlated like injected attribution — session B's failure
        # inside session A's kill-switch window is a genuine miss, not
        # switch-off (ZMEM_INJECT is per-process env).
        _make_fixture_db(self.db, [
            {"session_id": "sess-b", "ts_s": self.now, "call_id": "call-1",
             "tool": "Bash", "operation": "git stash pop",
             "error_message": "conflict"},
        ])
        self._bg_log([
            _bg_line(self.now, [], sid="sess-a",
                     reason="disabled", status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["missed"], 1,
                         "another session's kill-switch window must not "
                         "absorb this failure")
        self.assertEqual(report["counts"]["disabled"], 0)
        self.assertEqual(report["miss_rate"], 1.0)

    def test_sidless_disabled_line_weakly_matches_any_session(self):
        # Review round: a sid-less disabled line (pre-#94 log, host supplied
        # no session id) still proves the switch was off for whatever ran —
        # weak legacy match, mirroring surfaced-sid/surfaced-legacy.
        self._fixture(session="sess-b")
        self._bg_log([
            _bg_line(self.now, [], sid=None,
                     reason="disabled", status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 1)
        self.assertEqual(report["counts"]["missed"], 0)
        self.assertIsNone(report["miss_rate"])

    def test_same_session_disabled_line_absorbs_the_miss(self):
        # The positive twin: sid-matched kill-switch window -> switch off.
        self._fixture(session="sess-a")
        self._bg_log([
            _bg_line(self.now, [], sid="sess-a",
                     reason="disabled", status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 1)
        self.assertEqual(report["counts"]["missed"], 0)

    def test_disabled_lines_present_but_no_window_overlap(self):
        self._fixture()
        self._bg_log([
            _bg_line(self.now - 86400, [], reason="disabled",
                     status="silent"),
        ])
        report = self._run()
        self.assertEqual(report["counts"]["disabled"], 0)
        self.assertEqual(report["counts"]["missed"], 1)
        self.assertTrue(any("kill-switch decision line" in c
                            for c in report["caveats"]),
                        report["caveats"])


class JoinClassificationTest(_JoinFixture, unittest.TestCase):
    def test_a_matched_and_injected_sid_line_is_surfaced_sid(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run()
        self.assertNotIn("error", rep)
        self.assertEqual(rep["counts"]["surfaced_sid"], 1)
        self.assertEqual(rep["counts"]["missed"], 0)
        self.assertEqual(rep["query_source"]["operation"], 1)
        self.assertEqual(rep["miss_rate"], 0.0)

    def test_b_injected_line_without_the_matched_row_is_missed(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, ["other-uuid"])])
        rep = self._run()
        self.assertEqual(rep["counts"]["missed"], 1)
        self.assertEqual(rep["counts"]["surfaced_sid"], 0)
        self.assertEqual(rep["miss_rate"], 1.0)
        self.assertEqual(rep["top_missed_ids"][0]["id"], self.row_id)
        self.assertNotIn("content_preview", rep["top_missed_ids"][0])

    def test_b2_matched_row_in_all_only_is_missed_with_all_only(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, ["other-uuid"],
                               all_ids=["other-uuid", self.row_id])])
        rep = self._run()
        self.assertEqual(rep["counts"]["missed"], 1)
        self.assertEqual(rep["missed_all_only"], 1)

    def test_c_no_store_match_is_capture_gap(self):
        # cargo build tokens derive fine but match nothing in the store.
        self._fixture(operation="cargo build weirdcrate")
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run()
        self.assertEqual(rep["counts"]["capture_gap"], 1)
        self.assertEqual(rep["counts"]["missed"], 0)

    def test_c2_underivable_operation_is_no_query_not_capture_gap(self):
        self._fixture(operation="")
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run()
        self.assertEqual(rep["counts"]["no_query"], 1)
        self.assertEqual(rep["counts"]["capture_gap"], 0)
        self.assertIsNotNone(rep["no_query_pct"])
        self.assertTrue(any("no query" in c or "derived no query" in c
                            for c in rep["caveats"]))

    def test_d_no_bg_log_all_matched_failures_missed(self):
        self._fixture()
        rep = self._run()  # no zmem-bg.log written
        self.assertEqual(rep["counts"]["missed"], 1)
        self.assertTrue(any("no bg-log" in c for c in rep["caveats"]))

    def test_e_line_just_outside_window_is_missed(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 3600, [self.row_id])])
        rep = self._run(window_before_s=1800, window_after_s=300)
        self.assertEqual(rep["counts"]["missed"], 1)
        # …and one just INSIDE the before-window still surfaces
        self._bg_log([_bg_line(self.now - 1799, [self.row_id])])
        rep = self._run(window_before_s=1800, window_after_s=300)
        self.assertEqual(rep["counts"]["surfaced_sid"], 1)

    def test_f_partial_bg_log_coverage(self):
        self._fixture(ts=self.now - 86400)  # failure a day before the log
        self._bg_log([_bg_line(self.now, [self.row_id])])
        rep = self._run()
        self.assertEqual(rep["counts"]["missed"], 1)
        self.assertEqual(rep["failures_in_bg_log_period"], 0)

    def test_g_legacy_sid_less_line_is_surfaced_legacy_not_sid(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id], sid=None)])
        rep = self._run()
        self.assertEqual(rep["counts"]["surfaced_legacy"], 1)
        self.assertEqual(rep["counts"]["surfaced_sid"], 0)
        self.assertEqual(rep["legacy_attributions"], 1)
        # strict rate is null while no sid-carrying lines exist at all
        self.assertIsNone(rep["miss_rate_strict_sid"])
        self.assertEqual(rep["miss_rate"], 0.0)

    def test_g2_writer_b_injection_line_surfaces_despite_no_reason(self):
        # Baseline-run finding: the session-start writer NEVER carries
        # reason= — filtering injections on reason=injected alone dropped
        # every session-start injection and inflated the miss rate to 1.0.
        # Writer-B shape (status=injected, no reason=) must count.
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id], sid=None,
                              reason=None)])
        rep = self._run()
        self.assertEqual(rep["counts"]["surfaced_legacy"], 1)
        self.assertEqual(rep["counts"]["missed"], 0)
        # …and a writer-B SILENT line (status=silent, no reason) must not.
        self._bg_log([_bg_line(self.now - 60, [self.row_id], sid=None,
                              reason=None, status="silent")])
        rep = self._run()
        self.assertEqual(rep["counts"]["missed"], 1)

    def test_m6_sid_unknown_line_gets_legacy_not_sid_attribution(self):
        # Broad-review M6: a sid=unknown line (host supplied no session id,
        # e.g. non-launcher session-start invocation) must never sid-PROVE
        # attribution and must never be attribution-dead — it gets the
        # weaker legacy treatment.
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id], sid="unknown")])
        rep = self._run()
        self.assertEqual(rep["counts"]["surfaced_legacy"], 1)
        self.assertEqual(rep["counts"]["surfaced_sid"], 0)

    def test_m5_missing_timestamp_excluded_not_missed(self):
        # Broad-review M5: no timestamp ⇒ no window ⇒ excluded with a
        # caveat, never silently classified missed. (A transcript record
        # without a timestamp is the natural no-ts failure shape.)
        with tempfile.TemporaryDirectory() as tdir:
            trans = os.path.join(tdir, "session.jsonl")
            Path(trans).write_text(json.dumps({
                "session_id": "sess-nts",
                "message": {"content": [
                    {"type": "tool_use", "id": "tu-2", "name": "Bash",
                     "input": {"command": "git stash pop"}},
                    {"type": "tool_result", "tool_use_id": "tu-2",
                     "is_error": True, "content": "boom"},
                ]},
            }) + "\n", encoding="utf-8")
            self._fixture()
            self._bg_log([_bg_line(self.now - 60, [self.row_id])])
            rep = self._run(transcripts=[trans])
            self.assertEqual(rep["no_timestamp"], 1)
            self.assertEqual(rep["counts"]["missed"], 0)
            self.assertTrue(any("no usable timestamp" in c
                                for c in rep["caveats"]))

    def test_m5_seconds_unit_completed_at_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            _make_fixture_db(db, [
                {"session_id": "sess-a", "ts_s": self.now,
                 "call_id": "call-s", "tool": "Bash",
                 "operation": "git stash pop", "error_message": "x"},
            ])
            # rewrite completed_at to epoch SECONDS (mixed-unit row)
            conn = sqlite3.connect(db)
            conn.execute("UPDATE tool_usage SET completed_at = ?",
                         (self.now,))
            conn.commit()
            conn.close()
            rows = miss_rate.failures_from_db_rich(db)
            self.assertEqual(rows[0]["ts_s"], self.now)

    def test_m4_transcript_failures_not_starved_by_db_limit(self):
        # Broad-review M4: db failures alone exceed the limit; the newer
        # transcript failure must still be examined (fair merge + sort
        # before truncation), and the truncation must be caveated.
        import glob as _globmod
        with tempfile.TemporaryDirectory() as tdir:
            trans = os.path.join(tdir, "session.jsonl")
            Path(trans).write_text(json.dumps({
                "session_id": "sess-t", "timestamp":
                    datetime.fromtimestamp(self.now + 5, tz=timezone.utc)
                    .isoformat(),
                "message": {"content": [
                    {"type": "tool_use", "id": "tu-1", "name": "Bash",
                     "input": {"command": "git stash pop"}},
                    {"type": "tool_result", "tool_use_id": "tu-1",
                     "is_error": True, "content": "boom"},
                ]},
            }) + "\n", encoding="utf-8")
            self._fixture()
            self._bg_log([_bg_line(self.now, [self.row_id], sid="sess-t")])
            rep = self._run(transcripts=[trans], limit=1)
            self.assertEqual(rep["failures_examined"], 1)
            # the NEWER transcript failure won the limit slot
            self.assertEqual(rep["counts"]["surfaced_sid"], 1)
            self.assertTrue(rep["failures_truncated"])
            self.assertTrue(any("failure limit" in c for c in rep["caveats"]))

    def test_l7_distinct_same_second_failures_not_deduped(self):
        # Broad-review L7: two distinct failures (different call ids) in
        # the same session+second+tool with empty error text must BOTH be
        # examined.
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            _make_fixture_db(db, [
                {"session_id": "sess-a", "ts_s": self.now,
                 "call_id": "call-1", "tool": "Bash",
                 "operation": "git stash pop", "error_message": ""},
                {"session_id": "sess-a", "ts_s": self.now,
                 "call_id": "call-2", "tool": "Bash",
                 "operation": "cargo build weirdcrate", "error_message": ""},
            ])
            rows = miss_rate.failures_from_db_rich(db)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["call_id"] for r in rows},
                             {"call-1", "call-2"})

    def test_zero_write_and_no_wal_artifacts(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        store = os.path.join(self._tmp, "store.sqlite")
        # The zero-DATA-write guarantee rests on mode=ro AND no_telemetry —
        # CLI --no-bump alone still bumps surfaced_count. (A mode=ro open
        # of a WAL-mode store may create EMPTY -wal/-shm bookkeeping files
        # next to it — the same artifact doctor's existing mode=ro checks
        # produce; assert they stay empty, i.e. zero data written.)
        with open(store, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        rep = self._run()
        self.assertNotIn("error", rep)
        with open(store, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(before, after, "the join must not modify the store")
        # (The -shm is a fixed-size wal-index mapping — only the -wal
        # carries data frames; assert the -wal stays EMPTY.)
        wal = store + "-wal"
        if os.path.exists(wal):
            self.assertEqual(os.path.getsize(wal), 0,
                             f"-wal must stay empty (zero data frames "
                             f"written) — got {os.path.getsize(wal)} bytes")

    def test_missing_store_is_error_never_created(self):
        self._fixture()
        store = os.path.join(self._tmp, "absent.sqlite")
        rep = miss_rate.run_miss_report(store_path=store, db_path=self.db)
        self.assertIn("error", rep)
        self.assertFalse(os.path.exists(store),
                         "the join must never create a store")

    def test_empty_sqlite_file_is_error_never_migrated(self):
        empty = os.path.join(self._tmp, "empty.sqlite")
        open(empty, "wb").close()
        rep = miss_rate.run_miss_report(store_path=empty, db_path=self.db)
        self.assertIn("error", rep)
        self.assertEqual(os.path.getsize(empty), 0,
                         "the join must never migrate/create schema")

    def test_stale_schema_memory_without_fts_is_loud_error(self):
        # Review round 8b: a stale-schema store whose `memory` table exists
        # but whose `memory_fts` does not must be a LOUD error — without
        # the fts probe it would slip through and classify every failure as
        # capture-gap (recall's FTS OperationalError degrades to rows=[]
        # inside storelib), misreading a schema problem as a store gap.
        stale = os.path.join(self._tmp, "stale.sqlite")
        conn = sqlite3.connect(stale)
        conn.execute("CREATE TABLE memory (id TEXT PRIMARY KEY,"
                     " namespace TEXT, content TEXT, confidence REAL)")
        conn.execute("INSERT INTO memory VALUES ('x', 'user:global',"
                     " 'stale row', 0.9)")
        conn.commit()
        conn.close()
        size_before = os.path.getsize(stale)
        rep = miss_rate.run_miss_report(store_path=stale, db_path=self.db)
        self.assertIn("error", rep)
        self.assertIn("unreadable", rep["error"])
        self.assertEqual(os.path.getsize(stale), size_before,
                         "the join must never migrate a stale-schema store")

    def test_transcript_glob_miss_produces_caveat(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run(transcripts=[os.path.join(self._tmp, "nope",
                                                  "*.jsonl")])
        self.assertNotIn("error", rep)
        self.assertTrue(any("matched no files" in c for c in rep["caveats"]),
                        f"caveats: {rep['caveats']}")

    def test_link_hops_pinned_to_zero(self):
        src = Path(miss_rate.__file__).read_text(encoding="utf-8")
        self.assertIn("link_hops=0", src,
                      "the join's recall must pin link_hops=0 — link "
                      "expansion runs independently of no_bump and would "
                      "inflate the matched-id set")

    def test_prr005_camelcase_sessionid_is_read(self):
        # Swarm-review PRR-005 (HIGH): real CC transcript records are
        # predominantly camelCase `sessionId`-only — the record key must be
        # read in BOTH spellings or sid attribution dies on the lane.
        with tempfile.TemporaryDirectory() as tdir:
            trans = os.path.join(tdir, "cc.jsonl")
            Path(trans).write_text(json.dumps({
                "sessionId": "sess-cc-1",
                "timestamp": datetime.fromtimestamp(
                    self.now - 30, tz=timezone.utc).isoformat(),
                "message": {"content": [
                    {"type": "tool_use", "id": "tu-c", "name": "Bash",
                     "input": {"command": "git stash pop"}},
                    {"type": "tool_result", "tool_use_id": "tu-c",
                     "is_error": True, "content": "conflict"},
                ]},
            }) + "\n", encoding="utf-8")
            rows = miss_rate.failures_from_transcript_rich(trans)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["session_id"], "sess-cc-1")
            # end-to-end: the camelCase session gets SID-proven attribution
            self._fixture()
            self._bg_log([_bg_line(self.now - 20, [self.row_id],
                                   sid="sess-cc-1")])
            rep = self._run(transcripts=[trans])
            self.assertEqual(rep["counts"]["surfaced_sid"], 1)

    def test_prr004_tur_only_error_is_a_failure(self):
        # Swarm-review PRR-004: the sibling toolUseResult "Error…" shape
        # (mine.py's tur_is_err path) must classify as a failure.
        with tempfile.TemporaryDirectory() as tdir:
            trans = os.path.join(tdir, "cc.jsonl")
            Path(trans).write_text(json.dumps({
                "session_id": "sess-tur",
                "timestamp": datetime.fromtimestamp(
                    self.now - 30, tz=timezone.utc).isoformat(),
                "message": {"content": [
                    {"type": "tool_use", "id": "tu-t", "name": "Bash",
                     "input": {"command": "git stash pop"}},
                    {"type": "tool_result", "tool_use_id": "tu-t",
                     "content": ""},
                ]},
                "toolUseResult": "Error: command failed with exit code 1",
            }) + "\n", encoding="utf-8")
            rows = miss_rate.failures_from_transcript_rich(trans)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["operation"], "git stash pop")
            self.assertIn("Error", rows[0]["error"])

    def test_prr004_user_rejection_is_not_a_failure(self):
        # Swarm-review PRR-004 (the substantive half the pass-1 critic
        # flagged): rejection-shaped blocks are split OUT by mine.py and
        # must stay excluded here — counting them inflates `missed`.
        with tempfile.TemporaryDirectory() as tdir:
            trans = os.path.join(tdir, "cc.jsonl")
            Path(trans).write_text(json.dumps({
                "session_id": "sess-rej",
                "timestamp": datetime.fromtimestamp(
                    self.now - 30, tz=timezone.utc).isoformat(),
                "message": {"content": [
                    {"type": "tool_use", "id": "tu-r", "name": "Bash",
                     "input": {"command": "git push --force"}},
                    {"type": "tool_result", "tool_use_id": "tu-r",
                     "is_error": True,
                     "content": "The user doesn't want to proceed to make "
                                "changes"},
                ]},
            }) + "\n", encoding="utf-8")
            rows = miss_rate.failures_from_transcript_rich(trans)
            self.assertEqual(rows, [])

    def test_prr001_negative_window_is_rejected(self):
        # Swarm-review PRR-001: a negative window inverts the interval and
        # silently classified everything missed — it must be a loud error.
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run(window_before_s=-500)
        self.assertIn("error", rep)
        self.assertIn(">= 0", rep["error"])
        rep = self._run(window_after_s=-5)
        self.assertIn("error", rep)

    def test_prr002_corrupt_db_is_loud_not_silent(self):
        # Swarm-review PRR-002: a PRESENT-but-unreadable db must surface as
        # a report caveat + db_error field (doctor flips the check to warn),
        # never as a silent zero-failure run.
        bad = os.path.join(self._tmp, "bad.sqlite")
        Path(bad).write_bytes(b"not a sqlite file at all" * 100)
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        healthy = self._run()
        self.assertIsNone(healthy.get("db_error"))
        rep = miss_rate.run_miss_report(
            store_path=os.path.join(self._tmp, "store.sqlite"),
            db_path=bad, bg_log_path=os.path.join(self._tmp, "zmem-bg.log"))
        self.assertIsNotNone(rep.get("db_error"))
        self.assertEqual(rep["failures_examined"], 0)
        self.assertTrue(any("could not be read" in c for c in rep["caveats"]))

    def test_prr003_recall_error_is_not_capture_gap(self):
        # Swarm-review PRR-003: a recall exception must be excluded with a
        # caveat (recall_errors), never classified as a capture gap.
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        import storelib.recall as _sr
        original = _sr.recall_memory

        def _boom(*a, **k):
            raise RuntimeError("injected recall failure")

        _sr.recall_memory = _boom
        try:
            rep = self._run()
        finally:
            _sr.recall_memory = original
        self.assertEqual(rep["recall_errors"], 1)
        self.assertEqual(rep["counts"]["capture_gap"], 0)
        self.assertEqual(rep["counts"]["missed"], 0)
        self.assertTrue(any("could not be recalled" in c
                            for c in rep["caveats"]))

    def test_prr013_verbose_positive(self):
        # Swarm-review PRR-013: the verbose content_preview path had no
        # positive test.
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, ["other-uuid"])])
        rep = self._run(verbose=True)
        entry = rep["top_missed_ids"][0]
        self.assertIn("content_preview", entry)
        self.assertTrue(entry["content_preview"].startswith("mrcanary"))

    def test_prr007_zero_denominator_caveat(self):
        # Swarm-review PRR-007 / cubic #6: an all-capture-gap run has no
        # rate denominator — the report must say so instead of a bare null.
        self._fixture(operation="cargo build weirdcrate")
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        rep = self._run()
        self.assertIsNone(rep["miss_rate"])
        self.assertEqual(rep["counts"]["capture_gap"], 1)
        self.assertTrue(any("no miss-rate denominator" in c
                            for c in rep["caveats"]))


class RingQueryTest(_JoinFixture, unittest.TestCase):
    def _ring(self, session, events):
        ops_dir = os.path.join(self._tmp, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        safe = miss_rate.sanitize_sid(session)
        Path(os.path.join(ops_dir, safe + ".log")).write_text(
            "".join(json.dumps(ev) + "\n" for ev in events), encoding="utf-8")

    def test_ring_events_strictly_before_failure(self):
        ev_before = {"ts": self.now - 300, "tool": "Bash", "ops": "git stash"}
        ev_after = {"ts": self.now + 300, "tool": "Bash", "ops": "cargo build"}
        events = miss_rate._ring_events_before(
            self._tmp, "sess-a", self.now)
        self.assertEqual(events, [])  # no ring yet
        self._ring("sess-a", [ev_before, ev_after])
        events = miss_rate._ring_events_before(
            self._tmp, "sess-a", self.now)
        self.assertEqual(events, ["git stash"])
        events = miss_rate._ring_events_before(
            self._tmp, "sess-a", self.now + 600)
        self.assertEqual(events, ["git stash", "cargo build"])

    def test_subagent_named_ring_is_read(self):
        self._ring("sess_subagent_agent_abc-123",
                   [{"ts": self.now - 60, "tool": "Bash",
                     "ops": "git stash pop"}])
        events = miss_rate._ring_events_before(
            self._tmp, "sess_subagent_agent_abc-123", self.now + 10)
        self.assertEqual(events, ["git stash pop"])

    def test_operation_chain_wins_over_ring(self):
        # No operation in the fixture → ring drives the query; with an
        # operation present, the operation wins.
        self._ring("sess-a", [{"ts": self.now - 60, "tool": "Bash",
                               "ops": "cargo build"}])
        self._fixture(operation="")  # nothing recoverable from the db
        self._bg_log([])
        rep = self._run()
        self.assertEqual(rep["query_source"]["ring"], 1)
        self.assertEqual(rep["counts"]["capture_gap"], 1)


class FalseInjectionReviewHardeningTest(unittest.TestCase):
    """PR #144 review-hardening regressions on the counter itself
    (PRR-007/010/014/015): direct build_false_injection_report calls — no
    store needed (id-literal arm only)."""

    def _ln(self, ts=100, ids=("r1",), sid="sess-a", status="injected",
            reason="injected", moment="user_prompt"):
        return {"ts": ts, "status": status, "reason": reason,
                "omitted": None, "ids": list(ids), "all": list(ids),
                "ops": None, "sid": sid, "moment": moment}

    def test_contradictory_silent_reason_injected_not_counted(self):
        # PRR-010: status=silent + reason=injected is a writer bug; the
        # explicit-reason branch must not count it as injected.
        from storelib import false_inject
        report = false_inject.build_false_injection_report(
            [self._ln(status="silent", reason="injected")], failure_rows=[])
        self.assertEqual(report["overall"]["injected"], 0)

    def test_prompt_reference_from_transcript_marks_used(self):
        # PRR-007: user prompts mined from the same transcripts the join
        # consumes are reference events; before this the transcripts
        # parameter was accepted but inert, so a line whose only
        # same-session evidence was the operator prompt read as false.
        from storelib import false_inject
        tmp = tempfile.mkdtemp(prefix="zmem-fi129prompt-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tr = Path(tmp, "t.jsonl")
        tr.write_text(json.dumps({
            "type": "user", "sessionId": "sess-a",
            "timestamp": "2026-01-01T00:10:00Z",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "what about row-1 again?"}]}},
        ) + "\n", encoding="utf-8")
        lines = [self._ln(ts=100, ids=("row-1",))]
        r0 = false_inject.build_false_injection_report(
            lines, failure_rows=[])
        self.assertEqual(r0["overall"]["false"], 1)
        r1 = false_inject.build_false_injection_report(
            lines, failure_rows=[], transcripts=[tr])
        self.assertEqual(r1["overall"]["used"], 1)

    def test_tool_result_blocks_are_not_prompt_references(self):
        # PRR-007 scoping: tool_result blocks belong to the captured-
        # failure lane; only text blocks (and bare strings) are prompts.
        from storelib import false_inject
        tmp = tempfile.mkdtemp(prefix="zmem-fi129tool-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tr = Path(tmp, "t.jsonl")
        tr.write_text(json.dumps({
            "type": "user", "sessionId": "sess-a",
            "timestamp": "2026-01-01T00:10:00Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "row-1 was here"}]}},
        ) + "\n", encoding="utf-8")
        report = false_inject.build_false_injection_report(
            [self._ln(ts=100, ids=("row-1",))], failure_rows=[],
            transcripts=[tr])
        self.assertEqual(report["overall"]["false"], 1)

    def test_moment_metacharacters_sanitized_in_report_keys(self):
        # PRR-014: the reader applies the writers' charset rule so a
        # forged moment cannot smuggle metacharacters into report keys.
        from storelib import false_inject
        report = false_inject.build_false_injection_report(
            [self._ln(moment="evil\nstatus=x")], failure_rows=[])
        self.assertNotIn("evil\nstatus=x", report["per_moment"])
        self.assertEqual(
            report["per_moment"]["evil_status_x"]["injected"], 1)

    def test_hostile_ts_values_never_raise(self):
        # PRR-015: the "Never raises" contract held for None/ints but
        # ValueError-escaped on non-numeric strings.
        from storelib import false_inject
        report = false_inject.build_false_injection_report(
            [self._ln(ts="notanint")],
            failure_rows=[{"session_id": "sess-a", "ts_s": "bogus",
                           "tool": "Bash", "operation": "op",
                           "error": "err"}])
        self.assertEqual(report["overall"]["injected"], 1)


class FalseInjectionScopeAndDegradedTest(_JoinFixture, unittest.TestCase):
    """PRR-006/008: the counter rides the join with an UNBOUNDED reference
    scope and reports its own degradation distinctly."""

    def _transcript_with_failures(self, n):
        path = Path(self._tmp, "tr.jsonl")
        recs = []
        for i in range(n):
            recs.append({
                "sessionId": "sess-a",
                "timestamp": "2026-01-01T00:1%d:00Z" % i,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call-%d" % i,
                     "name": "Bash",
                     "input": {"command": "git stash pop"}}]},
                "toolUseResult": "Error: conflict %d" % i,
            })
        path.write_text("".join(json.dumps(r) + "\n" for r in recs),
                        encoding="utf-8")
        return str(path)

    def _run(self, **kw):
        return miss_rate.run_miss_report(
            store_path=os.path.join(self._tmp, "store.sqlite"),
            db_path=None,
            bg_log_path=os.path.join(self._tmp, "zmem-decisions.log"),
            **kw)

    def test_counter_reference_scope_ignores_miss_limit(self):
        # PRR-008: with --miss-limit 1 and two mined failures the join
        # examines 1 while the counter still sees both — the counter's
        # rate must not inflate as the operator shrinks the join's limit.
        tr = self._transcript_with_failures(2)
        Path(self._tmp, "zmem-decisions.log").write_text(
            _bg_line(self.now - 60, [self.row_id], moment="user_prompt")
            + "\n", encoding="utf-8")
        report = self._run(transcripts=[tr], limit=1)
        self.assertEqual(report["failures_examined"], 1)
        self.assertEqual(report["false_injection_failure_rows"], 2)

    def test_counter_failure_degrades_loudly(self):
        # PRR-006 (join layer): a counter crash yields a degraded stub the
        # doctor can distinguish from a genuine zero.
        from unittest import mock
        from storelib import false_inject
        tr = self._transcript_with_failures(1)
        Path(self._tmp, "zmem-decisions.log").write_text(
            _bg_line(self.now - 60, [self.row_id], moment="user_prompt")
            + "\n", encoding="utf-8")
        with mock.patch.object(false_inject, "build_false_injection_report",
                               side_effect=RuntimeError("boom")):
            report = self._run(transcripts=[tr])
        self.assertTrue(report["false_injection"]["degraded"])
        self.assertIn("RuntimeError: boom",
                      "".join(report["false_injection"]["caveats"]))

    def test_doctor_renders_degraded_counter_distinctly(self):
        # PRR-006 (doctor layer): the human summary must not print "no
        # injected decision lines" for a crashed counter — it prints
        # DEGRADED with the cause and downgrades the check to warn.
        from unittest import mock
        import doctor as doctor_mod
        canned = {
            "error": None,
            "counts": {"surfaced_sid": 0, "surfaced_legacy": 0,
                       "missed": 0, "capture_gap": 1, "no_query": 0,
                       "disabled": 0},
            "failures_examined": 1, "bg_log_decision_lines": 1,
            "db_error": None, "failures_truncated": False,
            "false_injection": {
                "degraded": True,
                "overall": {"injected": 0, "used": 0, "false": 0,
                            "false_rate": None},
                "per_moment": {}, "min_token_overlap": 2,
                "caveats": ["false-injection counter failed: "
                            "RuntimeError: boom"]},
        }
        store = os.path.join(self._tmp, "store.sqlite")
        with mock.patch.object(miss_rate, "run_miss_report",
                               return_value=canned):
            chk = doctor_mod._check_miss_rate(store, {}, True)
        self.assertEqual(chk.get("status"), "warn", chk)
        self.assertIn("DEGRADED", chk.get("summary") or "", chk)


class FalseInjectionTest(_JoinFixture, unittest.TestCase):
    """Issue #129: the false-injection counter subtree.

    Counter semantics under test: the denominator is injected decision
    LINES (status=injected + non-empty ids) counted once each; a line is
    USED when a same-sid reference event strictly after its ts contains one
    of its ids literally OR shares >= min_token_overlap distinct ops tokens
    with the injected row's content; sid-less/unknown lines weak-match any
    session; legacy (moment-less) lines report under per_moment["legacy"].

    The token fixtures are exact: TOKEN_ROW content derives exactly
    {git, stash, pop}; "git stash pop" shares 3 of them, "git stash list"
    shares exactly 2 — the boundary the min_token_overlap threshold rides.
    """

    # Command-shaped content: derive_ops_tokens yields {git, stash, pop}.
    TOKEN_ROW = "git stash pop conflicts need stash drop after resolve"

    def setUp(self):
        super().setUp()
        self.tok_row = _seed_and_get_id(self._tmp, "project:fi129",
                                        self.TOKEN_ROW)

    def _ring(self, session, events):
        ops_dir = os.path.join(self._tmp, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        safe = miss_rate.sanitize_sid(session)
        Path(os.path.join(ops_dir, safe + ".log")).write_text(
            "".join(json.dumps(ev) + "\n" for ev in events), encoding="utf-8")

    def _decisions_log(self, lines):
        """Write the canonical #129 decisions log (preferred over any
        legacy zmem-bg.log when both exist)."""
        Path(os.path.join(self._tmp, "zmem-decisions.log")).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def test_used_via_ops_ring_overlap_three_tokens(self):
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row],
                     moment="user_prompt"),
        ])
        self._ring("sess-a", [{"ts": self.now, "tool": "Bash",
                               "ops": "git stash pop"}])  # 3 shared >= 2
        rep = self._run()  # no db file: failures side is legitimately []
        self.assertNotIn("error", rep)
        fi = rep["false_injection"]
        self.assertEqual(fi["min_token_overlap"], 2)
        self.assertEqual(fi["overall"],
                         {"injected": 1, "used": 1, "false": 0,
                          "false_rate": 0.0})
        self.assertEqual(fi["per_moment"]["user_prompt"]["used"], 1)

    def test_used_via_db_failure_operation_overlap_exactly_two(self):
        # The mined failure's recovered operation "git stash list" shares
        # EXACTLY 2 tokens with the row content — the == threshold edge.
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row], moment="pretool"),
        ])
        self._fixture(operation="git stash list")
        rep = self._run()
        fi = rep["false_injection"]
        self.assertEqual(fi["overall"]["injected"], 1)
        self.assertEqual(fi["overall"]["used"], 1,
                         "an exactly-2-token overlap credits at the default "
                         "min_token_overlap=2")

    def test_false_when_never_referenced(self):
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row],
                     moment="user_prompt"),
        ])
        rep = self._run()  # no ring, no db: nothing references the line
        fi = rep["false_injection"]
        self.assertEqual(fi["overall"],
                         {"injected": 1, "used": 0, "false": 1,
                          "false_rate": 1.0})
        self.assertIsInstance(fi["overall"]["false_rate"], float)
        self.assertTrue(any("no reference events" in c
                            for c in fi["caveats"]), fi["caveats"])

    def test_sid_discipline_and_sidless_weak_match(self):
        # PRR-004 rule: session B's reference never credits session A's
        # line; a sid-less legacy line weak-matches ANY session. The sess-b
        # ring only enters the reference pool because a sess-b decision
        # line names it (the counter mines rings per decision-line sid).
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row], sid="sess-a",
                     moment="user_prompt"),
            # denominator-neutral carrier of the sess-b sid
            _bg_line(self.now - 60, [], sid="sess-b",
                     reason="empty-pool", status="silent"),
            # sid-less legacy line (no moment= -> legacy bucket)
            _bg_line(self.now - 60, [self.tok_row], sid=None),
        ])
        self._ring("sess-b", [{"ts": self.now, "tool": "Bash",
                               "ops": "git stash pop"}])
        rep = self._run()
        fi = rep["false_injection"]
        self.assertEqual(fi["overall"]["injected"], 2,
                         "the empty-pool carrier line is not a denominator "
                         "line")
        self.assertEqual(fi["overall"]["used"], 1,
                         "only the sid-less line may credit from sess-b's "
                         "ring")
        self.assertEqual(fi["overall"]["false"], 1,
                         "sess-b's ring event must NOT credit sess-a's line")
        self.assertEqual(fi["per_moment"]["user_prompt"]["false"], 1)
        self.assertEqual(fi["per_moment"]["legacy"]["used"], 1)

    def test_min_token_overlap_three_flips_overlap_two_to_false(self):
        # The overlap-2 reference ("git stash list") credits at threshold 2
        # and fails at threshold 3 — the override provably reaches the arm.
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row],
                     moment="user_prompt"),
        ])
        self._ring("sess-a", [{"ts": self.now, "tool": "Bash",
                               "ops": "git stash list"}])  # exactly 2 shared
        rep2 = self._run(min_token_overlap=2)
        self.assertEqual(rep2["false_injection"]["min_token_overlap"], 2)
        self.assertEqual(rep2["false_injection"]["overall"]["used"], 1)
        rep3 = self._run(min_token_overlap=3)
        fi3 = rep3["false_injection"]
        self.assertEqual(fi3["min_token_overlap"], 3)
        self.assertEqual(fi3["overall"]["used"], 0,
                         "a 2-token overlap must NOT credit at threshold 3")
        self.assertEqual(fi3["overall"]["false"], 1)

    def test_legacy_momentless_lines_bucket_and_caveat(self):
        self._decisions_log([
            _bg_line(self.now - 60, [self.tok_row]),  # no moment= field
        ])
        rep = self._run()
        fi = rep["false_injection"]
        self.assertEqual(list(fi["per_moment"]), ["legacy"])
        self.assertEqual(fi["per_moment"]["legacy"]["injected"], 1)
        self.assertEqual(fi["overall"]["injected"], 1,
                         "legacy lines still count in the overall rate")
        self.assertTrue(any("no moment= field" in c for c in fi["caveats"]),
                        fi["caveats"])
        self.assertTrue(any("legacy bucket" in c for c in fi["caveats"]),
                        fi["caveats"])

    def test_per_line_double_count_guard(self):
        # The same memory id re-injected at two moments is TWO denominator
        # LINES — counted once per line, never once per id.
        self._decisions_log([
            _bg_line(self.now - 120, [self.tok_row],
                     moment="user_prompt"),
            _bg_line(self.now - 60, [self.tok_row],
                     moment="pretool"),
        ])
        rep = self._run()
        fi = rep["false_injection"]
        self.assertEqual(fi["overall"]["injected"], 2)
        self.assertEqual(fi["overall"]["false"], 2)
        self.assertEqual(fi["per_moment"]["user_prompt"]["injected"], 1)
        self.assertEqual(fi["per_moment"]["pretool"]["injected"], 1)

    def test_parse_bg_log_rotation_aware_thirteen_lines(self):
        # 13 parseable decision lines across .2 (4) + .1 (5) + active (4);
        # the marker lines must not count. The join sees the full history
        # across rotation boundaries instead of the active cap-worth.
        base = os.path.join(self._tmp, "zmem-decisions.log")
        def _seg_lines(prefix, n):
            return "".join(
                _bg_line(self.now - 1000 + i, [f"{prefix}-{i}"],
                         sid=f"sess-{prefix}-{i}", moment="user_prompt")
                + "\n" for i in range(n))
        Path(base + ".2").write_text(
            "# zmem-seq=1 rotated_at=1740000000\n" + _seg_lines("old", 4),
            encoding="utf-8")
        Path(base + ".1").write_text(
            "# zmem-seq=1 rotated_at=1740000100\n" + _seg_lines("mid", 5),
            encoding="utf-8")
        Path(base).write_text(_seg_lines("new", 4), encoding="utf-8")
        parsed = miss_rate.parse_bg_log(base)
        self.assertEqual(len(parsed), 13)
        rep = self._run()  # decisions file preferred over legacy bg log
        self.assertEqual(rep["bg_log_decision_lines"], 13)


class DoctorSurfaceTest(_JoinFixture, unittest.TestCase):
    def _doctor(self, *args, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["PYTHONUTF8"] = "1"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(DOCTOR_PY), "--format", "json", *args],
            capture_output=True, text=True, env=env, timeout=180)

    def _report(self, r) -> dict:
        """Parse doctor's JSON; do NOT assert the process exit code —
        unrelated checks can legitimately fail on the host running the
        suite (e.g. second-stores on a box with leftover stores). A crash
        (non-JSON stdout) is still a failure."""
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.fail(f"doctor crashed (rc={r.returncode}): "
                      f"{r.stderr[-800:]}")

    def _miss_check(self, r) -> dict:
        rep = self._report(r)
        found = [c for c in rep["checks"] if c["id"] == "miss-rate"]
        self.assertTrue(found, "miss-rate check missing from report")
        return found[0]

    def test_happy_path_check_runs_with_counts(self):
        self._fixture()
        self._bg_log([_bg_line(self.now - 60, [self.row_id])])
        r = self._doctor(
            "--store", os.path.join(self._tmp, "store.sqlite"),
            "--miss-rate", "--miss-db", self.db)
        check = self._miss_check(r)
        self.assertEqual(check["status"], "pass")
        self.assertIn("missed 0", check["summary"])
        self.assertEqual(check["details"]["report"]["counts"]
                         ["surfaced_sid"], 1)

    def test_refuses_host_default_store_and_runs_nothing(self):
        # No --store, env stripped: doctor resolves the host default
        # (~/.zmem/store.sqlite via clean-env host resolution) → REFUSED,
        # join not executed (no report key), and no store file created.
        with tempfile.TemporaryDirectory() as fake_home:
            absent = os.path.join(fake_home, ".zmem", "store.sqlite")
            r = self._doctor("--miss-rate",
                             env_extra={"HOME": fake_home,
                                        "USERPROFILE": fake_home})
            check = self._miss_check(r)
            self.assertEqual(check["status"], "fail")
            self.assertIn("REFUSED", check["summary"])
            self.assertNotIn("report", check["details"])
            self.assertFalse(os.path.exists(absent),
                             "the refused join must not create a store")

    def test_m1_env_resolved_store_is_never_sufficient(self):
        # Broad-review M1: an ambient ZMEM_STORE (documented deployment
        # mode) used to bypass the host-default comparison and silently
        # point the join at the live store. --miss-rate now REQUIRES an
        # explicit --store regardless of env.
        r = self._doctor("--miss-rate",
                         env_extra={
                             "ZMEM_STORE": os.path.join(
                                 self._tmp, "store.sqlite"),
                             "ZMEM_DATA": self._tmp})
        check = self._miss_check(r)
        self.assertEqual(check["status"], "fail")
        self.assertIn("requires an explicit --store", check["summary"])
        self.assertNotIn("report", check["details"])

    def test_store_flag_repoints_resolved_store(self):
        r = self._doctor("--store", os.path.join(self._tmp, "store.sqlite"))
        rep = self._report(r)
        self.assertIn("store.sqlite", str(rep["resolved_store"]))

    def test_no_miss_rate_flag_no_check_entry(self):
        r = self._doctor()
        rep = self._report(r)
        self.assertFalse([c for c in rep["checks"]
                          if c["id"] == "miss-rate"])




class SilentLinesWithIdsExcludedTest(_JoinFixture, unittest.TestCase):
    """PR-review hardening (implementation-review probe C, issue #129): a
    line with status=silent must NEVER enter the false-injection
    denominator even when it carries a non-empty ids list — the
    denominator is injected decision LINES only. Writers never emit
    silent+ids today, but the counter must not depend on that."""

    def test_silent_line_with_ids_is_excluded(self):
        now = int(time.time())
        self._bg_log([
            _bg_line(now - 3600, [], sid="sess-fi",
                     status="silent", reason="empty-pool"),
        ])
        # hand-patch the ids list non-empty on the silent line: the
        # writers never produce this shape, the counter must reject it
        log = Path(self._tmp) / "zmem-bg.log"
        rid = self.row_id
        text = log.read_text(encoding="utf-8").replace(
            "ids=[]", "ids=['{}']".format(rid))
        log.write_text(text, encoding="utf-8")
        report = self._run()
        fi = report["false_injection"]
        self.assertEqual(fi["overall"]["injected"], 0,
                         "a silent line with ids must not enter the "
                         "false-injection denominator (injected lines "
                         "only); fix the counter, never weaken this pin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
