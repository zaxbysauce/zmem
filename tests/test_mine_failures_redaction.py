"""Regression tests for issue #277: failure-record text is secret-redacted.

A failed tool call whose output echoes a credential used to persist verbatim
into the failure `details`/`rejections` that reflect hooks render into
prompts (parent nudge + subagent sidecars). storelib.mine now applies the
standard redaction filter at EVERY failure-record build site (transcript
details, both rejection-reason sites, db details) before collapse/truncate,
degrading fail-open to the pre-#277 renderer when the primitive is absent.

Run: python tests/test_mine_failures_redaction.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))

from storelib import mine  # noqa: E402

SECRET = "ghp_0123456789abcdef0123456789abcdef01234567"
SECRET_2 = "sk-ant-api03-QUICKFOXjumpsoverthelazydog012345"
SECRET_3 = "ghp_fedcba9876543210fedcba9876543210fedcba98"


def _transcript_text():
    records = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash"},
            {"type": "tool_use", "id": "t2", "name": "WebFetch"},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": ("Error: curl failed: Authorization Bearer "
                         + SECRET + " and api_key=" + SECRET_2)},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2",
             "content": ("The user doesn't want to proceed. "
                         "user said: the fetch URL leaked token " + SECRET_3)},
        ]}},
        {"type": "user",
         "toolUseResult": ("The user doesn't want to proceed. "
                           "user said: push refused, remote echoed "
                           "https://user:" + SECRET_3 + "@github.com/x.git")},
    ]
    return "\n".join(json.dumps(r) for r in records)


class FailureRedactionTest(unittest.TestCase):
    def test_transcript_failure_details_are_redacted(self):
        details, rejections = mine._failures_from_transcript(
            None, _transcript_text())
        blob = json.dumps(details) + json.dumps(rejections)
        for secret in (SECRET, SECRET_2, SECRET_3):
            self.assertNotIn(secret, blob)
        self.assertTrue(details, "a genuine failure must still be captured")

    def test_both_rejection_forms_carry_reasons(self):
        _, rejections = mine._failures_from_transcript(
            None, _transcript_text())
        self.assertEqual(len(rejections), 2)
        for r in rejections:
            self.assertTrue(r.get("reason"), f"empty reason: {r!r}")

    def test_plain_failure_text_is_byte_identical(self):
        plain = "uvicorn startup failed with address already in use on 8123"
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "B"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": True, "content": plain}]}},
        ]
        details, _ = mine._failures_from_transcript(
            None, "\n".join(json.dumps(r) for r in records))
        self.assertEqual(details[0]["error"], plain)

    def test_degraded_redaction_seam_fails_open(self):
        """The redaction primitive raising must never kill failure capture."""
        def _boom(*a, **k):
            raise RuntimeError("simulated absent redaction")

        orig = mine.redact_text
        mine.redact_text = _boom
        try:
            details, rejections = mine._failures_from_transcript(
                None, _transcript_text())
            self.assertTrue(details)
        finally:
            mine.redact_text = orig

    def test_redaction_precedes_truncation(self):
        """PRR-004: redaction must receive the FULL pre-truncation text — a
        truncate-first pipeline hands it the ≤200-char cut, losing any tail
        the detector could have matched. Spy-pinned, because placeholder
        presence in the OUTPUT is order-blind (the detector matches partial
        secret fragments too — reviewer-verified mutation). The behavioral
        guarantee (no verbatim secret survives) is pinned alongside."""
        filler = "- " * 97  # 194 chars, then the secret starts past 200
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "B"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": True,
                 "content": filler + "Bearer " + SECRET}]}},
        ]
        text_in = "\n".join(json.dumps(r) for r in records)
        seen = {}
        orig = mine.redact_text

        def _spy(text):
            seen["len"] = len(text)
            seen["text"] = text
            return "[REDACTED_SECRET]", 1

        mine.redact_text = _spy
        try:
            details, _ = mine._failures_from_transcript(None, text_in)
        finally:
            mine.redact_text = orig
        self.assertGreater(
            seen["len"], 200,
            "redaction saw the post-truncation text — the cap ran first")
        self.assertIn(
            SECRET, seen["text"],
            "the secret was truncated away before redaction saw it")
        self.assertNotIn(SECRET, details[0]["error"])
        self.assertLessEqual(len(details[0]["error"]), 204)

    def test_db_leg_redacts_error_message_and_type(self):
        """PRR-003: the db substrate leg (``_failures_from_db``) must redact
        exactly like the transcript leg — reverting either call site is a
        regression this file must catch."""
        import sqlite3
        import tempfile

        tmp = tempfile.mkdtemp(prefix="zmem-redact-db-")
        db = os.path.join(tmp, "db.sqlite")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE tool_usage(
            session_id TEXT, tool_name TEXT, read_only INT, status TEXT,
            exit_code INT, error_message TEXT, error_type TEXT,
            retry_count INT, destructive INT, completed_at TEXT)""")
        conn.execute(
            "INSERT INTO tool_usage VALUES "
            "('s1', 'Bash', 0, 'error', 1, ?, ?, 2, 1, '2026-01-01')",
            ("push failed: Authorization Bearer " + SECRET,
             "AuthError api_key=" + SECRET_2))
        conn.commit()
        conn.close()
        count, details = mine._failures_from_db(db, "s1")
        self.assertEqual(count, 1)
        blob = json.dumps(details)
        for secret in (SECRET, SECRET_2):
            self.assertNotIn(secret, blob)
        self.assertIn("[REDACTED", details[0]["error_type"])

    def test_degraded_redaction_is_visible(self):
        """PRR-009: a failing redaction primitive degrades fail-open BUT is
        observable — one stderr notice and ``redaction_degraded`` on the
        cmd_failures result."""
        import contextlib
        import io
        import os
        import sqlite3
        import tempfile

        tmp = tempfile.mkdtemp(prefix="zmem-redact-degrade-")
        db = os.path.join(tmp, "db.sqlite")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE tool_usage(
            session_id TEXT, tool_name TEXT, read_only INT, status TEXT,
            exit_code INT, error_message TEXT, error_type TEXT,
            retry_count INT, destructive INT, completed_at TEXT)""")
        conn.execute(
            "INSERT INTO tool_usage VALUES "
            "('s1', 'Bash', 0, 'error', 1, 'Authorization Bearer " + SECRET
            + "', 'AuthError', 2, 1, '2026-01-01')")
        conn.commit()
        conn.close()

        def _boom(*a, **k):
            raise RuntimeError("simulated absent redaction")

        orig = mine.redact_text
        mine.redact_text = _boom
        # Reset the module-global one-shot notice + counter: a sibling test
        # (test_degraded_redaction_seam_fails_open) may already have fired
        # them — order-dependent without this (reviewer round, PRR-009).
        orig_count, orig_warned = mine._REDACT_DEGRADE_COUNT, mine._REDACT_DEGRADE_WARNED
        mine._REDACT_DEGRADE_COUNT = [0]
        mine._REDACT_DEGRADE_WARNED = False
        outcap, errcap = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(outcap), \
                    contextlib.redirect_stderr(errcap):
                rc = mine.cmd_failures("s1", None, db=db)
        finally:
            mine.redact_text = orig
            mine._REDACT_DEGRADE_COUNT = orig_count
            mine._REDACT_DEGRADE_WARNED = orig_warned
        # Degrade never kills the checked-result contract (exit 0, JSON out).
        self.assertEqual(rc, 0)
        result = json.loads(outcap.getvalue())
        self.assertEqual(result["count"], 1)
        self.assertTrue(result.get("redaction_degraded"), result)
        self.assertIn("UNREDACTED", errcap.getvalue())
        # The details survive (fail-open) even though unprotected.
        self.assertTrue(result["details"])

    def test_error_type_is_redacted_and_capped(self):
        """PRR-007: error_type rides the same failure record and previously
        passed through raw and uncapped; the db-leg test above pins the
        redaction, this pins the cap."""
        long_type = "y" * 500
        out = mine._redact_failure_text(long_type, 100)
        self.assertLessEqual(len(out), 104)  # cap + ellipsis tolerance


if __name__ == "__main__":
    unittest.main(verbosity=2)
