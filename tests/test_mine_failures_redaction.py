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
        """A secret straddling the 200-char window is still redacted."""
        filler = "x" * 190 + " "
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "B"}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": True,
                 "content": filler + "Bearer " + SECRET}]}},
        ]
        details, _ = mine._failures_from_transcript(
            None, "\n".join(json.dumps(r) for r in records))
        self.assertNotIn(SECRET, details[0]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
