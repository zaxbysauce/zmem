"""Plain-unittest tests for zmem transcript correction mining (issue #46).

Covers: user-rejection extraction (both CC transcript formats), the
rejection/failure split + tool_use_id dedup, the correction pattern library
(pure-text functions), the `corrections` subcommand (JSON shape, read-only,
secret redaction), the failures-output shape-stability contract, and fail-open
behavior on non-CC schemas.

Run: python tests/test_corrections.py
No pytest / third-party harness required — matches the repo convention."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys_path_inserted = False
if str(SCRIPTS_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SCRIPTS_DIR))


def _load_store():
    """Load store.py with STORE_PATH pointed at a throwaway temp path (the
    failures/corrections paths never open it, but import resolves it eagerly)."""
    tmp = Path(tempfile.mkdtemp()) / "store.sqlite"
    spec = importlib.util.spec_from_file_location("zmem_store_corrtest", SCRIPTS_DIR / "store.py")
    with mock.patch.dict(os.environ, {"ZMEM_STORE": str(tmp)}, clear=False):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


store = _load_store()

try:
    import corrections as cmod
except Exception:  # pragma: no cover - import already ensured via sys.path
    cmod = None


def _write_jsonl(records, path=None) -> str:
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def _assistant_tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _rejected_block(tid, content, tur=None):
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": content, "is_error": True, "tool_use_id": tid}]}}
    if tur is not None:
        rec["toolUseResult"] = tur
    return rec


class TestRejectionExtraction(unittest.TestCase):
    def test_content_block_string_form(self):
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1", "The user doesn't want to proceed.\nthe user said:\ndon't touch the CI config"),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(details, [])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["tool"], "Bash")
        self.assertEqual(rejections[0]["reason"], "don't touch the CI config")
        os.remove(path)

    def test_content_block_list_of_text_blocks(self):
        path = _write_jsonl([
            _assistant_tool_use("t1", "Edit"),
            _rejected_block("t1", [{"type": "text", "text": "The user doesn't want to proceed.\nthe user said:\nstop refactoring"}]),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(rejections[0]["tool"], "Edit")
        self.assertEqual(rejections[0]["reason"], "stop refactoring")
        os.remove(path)

    def test_rejection_without_reason(self):
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1", "The user doesn't want to proceed."),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "")
        os.remove(path)

    def test_sibling_toolUseResult_rejection_when_block_content_empty(self):
        # tool_result block has empty content but the sibling toolUseResult holds
        # the rejection string (CC may put it there).
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1", "", tur="The user doesn't want to proceed\nuser said:\nuse a different approach"),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "use a different approach")
        os.remove(path)

    def test_pure_sibling_string_form_no_block(self):
        # A record with NO tool_result block but a top-level toolUseResult that is
        # itself a rejection ("extends, doesn't regress" the toolUseResult path).
        path = _write_jsonl([
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t9"}]},
             "toolUseResult": "The user doesn't want to proceed\nuser said:\nstop that"},
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "stop that")
        os.remove(path)

    def test_mixed_record_splits_rejection_from_failure(self):
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _assistant_tool_use("t2", "Edit"),
            _rejected_block("t1", "Exit code 1", tur="Error: Exit code 1"),
            _rejected_block("t2", "The user doesn't want to proceed.\nthe user said:\ndon't touch the CI config"),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(details), 1)          # only the genuine failure
        self.assertEqual(details[0]["tool"], "Bash")
        self.assertEqual(len(rejections), 1)       # only the rejection
        self.assertEqual(rejections[0]["tool"], "Edit")
        os.remove(path)

    def test_dedup_by_tool_use_id_rejection(self):
        # A record carrying both a content-block rejection AND a sibling
        # toolUseResult rejection must count once.
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1",
                            "The user doesn't want to proceed.\nthe user said:\nstop",
                            tur="The user doesn't want to proceed\nuser said:\nstop"),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        os.remove(path)

    def test_multi_line_reason_marker_stripped_and_newline_free(self):
        reason = ("stop touching the auth module\nand also leave the config alone\n"
                  "then rerun the tests")
        path = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1", "The user doesn't want to proceed.\nthe user said:\n" + reason),
        ])
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        r = rejections[0]["reason"]
        self.assertNotIn("the user said:", r)      # marker stripped
        self.assertNotIn("\n", r)                  # fence-integrity
        self.assertNotIn("\r", r)
        # All three lines preserved (joined), not just the first.
        self.assertIn("auth module", r)
        self.assertIn("config alone", r)
        self.assertIn("rerun the tests", r)
        os.remove(path)

    def test_malformed_lines_skipped(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(_assistant_tool_use("t1", "Bash")) + "\n")
            f.write("this is not json{{{\n")
            f.write(json.dumps(_rejected_block("t1", "The user doesn't want to proceed.\nthe user said:\nno")) + "\n")
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(len(rejections), 1)
        os.remove(path)

    def test_non_hashable_tool_use_id_failopen(self):
        # PRR-002: a list/dict tool_use_id (malformed/foreign record) must not
        # raise TypeError in either pass (name-map write or dedup) — fail open.
        path = _write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": ["x", "y"], "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "boom", "is_error": True,
                 "tool_use_id": ["x", "y"]}]}},
            _assistant_tool_use("t1", "Edit"),
            _rejected_block("t1", "The user doesn't want to proceed.\nthe user said:\nno need"),
        ])
        try:
            details, rejections = store._failures_from_transcript(path)
            # The malformed id's failure is still detected (tool name unknown),
            # and the normal rejection is extracted; no crash.
            self.assertEqual(details, [{"tool": "?", "error": "boom"}])
            self.assertEqual(len(rejections), 1)
            self.assertEqual(rejections[0]["tool"], "Edit")
            self.assertEqual(rejections[0]["reason"], "no need")
        finally:
            os.remove(path)

    def test_rejections_empty_on_db_substrate(self):
        # ZCode db.sqlite substrate has no rejection records — must be empty on
        # the PUBLIC cmd_failures surface (not just the internal _failures_from_db
        # return), so this stays a genuine assertion and cannot regress silently.
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE tool_usage(
            session_id TEXT, tool_name TEXT, read_only INT, status TEXT,
            exit_code INT, error_message TEXT, error_type TEXT,
            retry_count INT, destructive INT, completed_at TEXT)""")
        conn.executemany("INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("s1", "Bash", 0, "error", 1, "compile failed", "BuildError", 0, 0, "2026-01-01"),
        ])
        conn.commit()
        conn.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = store.cmd_failures(session="s1", transcript="", db=db)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["rejections"], [])
        os.remove(db)

    def test_nonexistent_transcript_failopen(self):
        self.assertEqual(store._failures_from_transcript(r"C:\definitely\nope.jsonl"), ([], []))


class TestFailuresShape(unittest.TestCase):
    def _run_failures(self, session="", transcript="", db=r"C:\nope.sqlite"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = store.cmd_failures(session=session, transcript=transcript, db=db)
        return rc, json.loads(buf.getvalue())

    def test_success_shape_has_exact_three_keys(self):
        rc, out = self._run_failures()
        self.assertEqual(rc, 0)
        self.assertEqual(set(out.keys()), {"count", "details", "rejections"})

    def test_broken_substrate_shape_has_error_key(self):
        fd, junk = tempfile.mkstemp(suffix=".sqlite")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("not a sqlite db")
        try:
            rc, out = self._run_failures(session="s1", transcript="", db=junk)
            self.assertEqual(rc, 2)
            self.assertEqual(set(out.keys()), {"count", "details", "rejections", "error"})
        finally:
            os.remove(junk)

    def test_rejections_present_in_transcript_output(self):
        tpath = _write_jsonl([
            _assistant_tool_use("t1", "Bash"),
            _rejected_block("t1", "The user doesn't want to proceed.\nthe user said:\nstop"),
        ])
        rc, out = self._run_failures(session="s", transcript=tpath)
        self.assertEqual(rc, 0)
        self.assertEqual(out["count"], 0)          # not a failure
        self.assertEqual(len(out["rejections"]), 1)
        os.remove(tpath)


class TestNonCCSchemaFailOpen(unittest.TestCase):
    """Codex-style rollout JSONL with a completely different record shape must
    never crash and never fabricate rejections/corrections."""

    def test_failures_noncc_empty(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"tool": "Shell", "result": {"errors": ["x"]}}) + "\n")
            f.write(json.dumps({"type": "user", "text": "no, use uv not pip", "isMeta": 1}) + "\n")
        details, rejections = store._failures_from_transcript(path)
        self.assertEqual(details, [])
        self.assertEqual(rejections, [])
        os.remove(path)

    def test_corrections_noncc_empty(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"tool": "Shell", "result": {"errors": ["x"]}}) + "\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = store.cmd_corrections(transcript=path)
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out, {"count": 0, "items": []})
        os.remove(path)


class TestPatternLibrary(unittest.TestCase):
    def test_strong_correction(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("no, use uv not pip")
        self.assertEqual(t, "auto")
        self.assertEqual(sent, "correction")
        self.assertGreaterEqual(conf, 0.70)

    def test_question_vetoed(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("can you help me fix this?")
        self.assertIsNone(t)

    def test_no_problem_vetoed(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("no problem, that's fine")
        self.assertIsNone(t)

    def test_guardrail(self):
        t, pat, conf, sent, decay = cmod.detect_patterns(
            "don't add any extra files unless I ask")
        self.assertEqual(t, "guardrail")
        self.assertGreaterEqual(conf, 0.90)

    def test_remember_explicit(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("remember: always run make lint")
        self.assertEqual(t, "explicit")
        self.assertEqual(conf, 0.90)

    def test_cjk_correction(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("違う、それは間違い")
        self.assertEqual(t, "auto")
        self.assertEqual(sent, "correction")

    def test_short_message_boost(self):
        t, pat, conf, sent, decay = cmod.detect_patterns("no, wrong")
        self.assertEqual(t, "auto")
        self.assertGreaterEqual(conf, 0.70)

    def test_zmem_marker_skipped_by_should_include(self):
        for text in [
            "<<<ZMEM_JSON>>>{\"count\": 1}<<<END>>>",
            "# Relevant memories (zmem recall, namespace project:foo). Consider if they apply; ignore if not.",
            "# Loaded from memory (Tier 0 — core.md, user-level):",
            "<system-reminder>ignore this</system-reminder>",
        ]:
            self.assertFalse(cmod.should_include_message(text), repr(text))

    def test_plain_user_message_kept(self):
        self.assertTrue(cmod.should_include_message("no, use uv not pip"))

    def test_strong_correction_overrides_bug_report_false_positive(self):
        # PRR-003: a structural bug-report FP ("is not broken") must not veto a
        # genuine strong correction ("use X not Y").
        t, pat, conf, sent, decay = cmod.detect_patterns(
            "no, the build is not broken; use uv not pip")
        self.assertEqual(t, "auto")
        self.assertEqual(sent, "correction")

    def test_plain_bug_report_still_vetoed(self):
        # Without a strong correction, a bug-report message remains vetoed.
        t, _, _, _, _ = cmod.detect_patterns("the build is not broken")
        self.assertIsNone(t)

    def test_cjk_never_matches_ma_chigae_te_accidentally(self):
        # PRR-004: 間違えて ("accidentally") must not be classed as a correction.
        t, _, _, _, _ = cmod.detect_patterns("間違えて")
        self.assertIsNone(t)

    def test_cjk_machigatte_correction_matches(self):
        # 間違って / 間違ってる ("it's wrong") still matches.
        for text in ("間違ってる", "間違って"):
            t, _, conf, sent, _ = cmod.detect_patterns(text)
            self.assertEqual(t, "auto", repr(text))
            self.assertEqual(sent, "correction", repr(text))

    def test_cjk_strong_overrides_question_false_positive(self):
        # Symmetric with English "that's wrong?": a strong CJK correction ending
        # in a question mark must not be vetoed as a false positive.
        t, _, _, sent, _ = cmod.detect_patterns("間違ってる？")
        self.assertEqual(t, "auto")
        self.assertEqual(sent, "correction")


class TestRenderRejectionSection(unittest.TestCase):
    """PRR-005/PRR-009: the shared helper both reflect hooks call must render the
    same fenced, newline-free, context-budget-capped block."""

    def test_empty(self):
        self.assertEqual(cmod.render_rejection_section([]), "")

    def test_single_with_reason(self):
        msg = cmod.render_rejection_section([{"tool": "Edit", "reason": "don't touch CI"}])
        self.assertIn("User rejected 1 tool call(s)", msg)
        self.assertIn("don't touch CI", msg)
        self.assertIn("--signal user", msg)
        self.assertNotIn("the user said:", msg)
        self.assertEqual(msg.count("don't touch CI"), 1)  # newline-free → 1 line

    def test_no_reason_wording(self):
        msg = cmod.render_rejection_section([{"tool": "Bash", "reason": ""}])
        self.assertIn("(no reason given)", msg)
        self.assertNotIn("--signal user", msg)

    def test_capped_at_detail_limit(self):
        # Most-recent rejections are kept (chronological tail), oldest dropped.
        # Indexes t0..t11; cap 5 → t7..t11 shown, t0..t6 hidden.
        rejs = [{"tool": "t%d" % i, "reason": "r%d" % i} for i in range(12)]
        msg = cmod.render_rejection_section(rejs, detail_limit=5)
        self.assertIn("User rejected 12 tool call(s) (showing most recent 5 of 12)", msg)
        self.assertIn("r7", msg)
        self.assertIn("r11", msg)
        self.assertNotIn("r0", msg)
        self.assertNotIn("r6", msg)
        self.assertIn("--signal user", msg)

    def test_no_cap_shows_all_in_order(self):
        rejs = [{"tool": "t%d" % i, "reason": "r%d" % i} for i in range(3)]
        msg = cmod.render_rejection_section(rejs, detail_limit=5)
        self.assertIn("User rejected 3 tool call(s).", msg)
        for i in range(3):
            self.assertIn("r%d" % i, msg)


class TestExtractFailOpen(unittest.TestCase):
    """PRR-001: extract_user_messages must never raise on a malformed/foreign
    record (non-dict message, non-str text) — fail open, skip the record."""

    def _extract(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        try:
            return cmod.extract_user_messages(path)
        finally:
            os.remove(path)

    def test_non_dict_message_skipped(self):
        msgs = self._extract([
            {"type": "user", "message": "just a bare string"},
            {"type": "user", "message": None},
            {"type": "user", "message": {"content": "no, use uv not pip"}},
        ])
        self.assertEqual(msgs, ["no, use uv not pip"])

    def test_non_string_text_skipped(self):
        msgs = self._extract([
            {"type": "user", "message": {"content": [
                {"type": "text", "text": 123},
                {"type": "text", "text": "remember: run make lint"},
            ]}},
        ])
        self.assertEqual(msgs, ["remember: run make lint"])


class TestCorrectionsSubcommand(unittest.TestCase):
    def _run(self, transcript, env=None):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False):
            with redirect_stdout(buf):
                rc = store.cmd_corrections(transcript=transcript)
        return rc, json.loads(buf.getvalue())

    def test_json_shape_and_veto(self):
        path = _write_jsonl([
            {"type": "user", "message": {"content": [{"type": "text", "text": "no, use uv not pip"}]}},
            {"type": "user", "message": {"content": [{"type": "text", "text": "can you help me fix this?"}]}},
            {"type": "user", "message": {"content": [{"type": "text", "text": "remember: always run make lint"}]}},
        ])
        rc, out = self._run(path)
        self.assertEqual(rc, 0)
        self.assertEqual(out["count"], 2)          # question vetoed
        types = {i["type"] for i in out["items"]}
        self.assertEqual(types, {"explicit", "auto"})
        self.assertTrue(any(i["patterns"] == "remember:" for i in out["items"]))
        os.remove(path)

    def test_read_only_never_touches_store(self):
        # cmd_corrections must never modify the store file. Point ZMEM_STORE at a
        # dummy file and assert its bytes are unchanged after the run.
        store_path = Path(tempfile.mkdtemp()) / "store.sqlite"
        original = b"SENTINEL-STORE-BYTES"
        store_path.write_bytes(original)
        tpath = _write_jsonl([
            {"type": "user", "message": {"content": [{"type": "text", "text": "no, wrong"}]}},
        ])
        rc, out = self._run(tpath, env={"ZMEM_STORE": str(store_path)})
        self.assertEqual(rc, 0)
        self.assertEqual(store_path.read_bytes(), original)
        os.remove(tpath)

    def test_secret_redaction_auto_mode(self):
        secret_content = "remember: set api_key=abcdefghijklmnop1234567890"
        path = _write_jsonl([
            {"type": "user", "message": {"content": [{"type": "text", "text": secret_content}]}},
        ])
        rc, out = self._run(path, env={"ZMEM_CAPTURE_MODE": "auto"})
        self.assertEqual(rc, 0)
        item = out["items"][0]
        self.assertTrue(item.get("secret_warning"))
        self.assertNotIn("abcdefghijklmnop", item["message"])
        os.remove(path)

    def test_secret_manual_mode_annotates_not_redacts(self):
        secret_content = "remember: set api_key=abcdefghijklmnop1234567890"
        path = _write_jsonl([
            {"type": "user", "message": {"content": [{"type": "text", "text": secret_content}]}},
        ])
        rc, out = self._run(path, env={"ZMEM_CAPTURE_MODE": "manual"})
        self.assertEqual(rc, 0)
        item = out["items"][0]
        self.assertTrue(item.get("secret_warning"))
        self.assertIn("abcdefghijklmnop", item["message"])  # verbatim for review
        os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
