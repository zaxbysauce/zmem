"""Issue #257 (Workstream R PR 1): the parent Stop nudge must render the
subagent sidecar's `details` list.

hooks/zmem-subagent-reflect.sh writes pre-formatted detail lines (tool,
error type, error text; capped at 5 entries) into each hand-off sidecar so
the parent can reflect on WHAT went wrong. The parent's renderer
`_subagent_lines()` in hooks/zmem-reflect.sh reads only agent_id, agent_type,
count, tool_summary and rejections — the `details` field is dropped on the
floor. These checks pin the render contract:

  - AC1: populated `details` render exactly once in the nudge,
  - AC2: an oversized entry goes through the same whitespace-collapse +
    200-char `_clean_field` cap as the other rendered sidecar fields,
  - AC3: a hostile/edited sidecar carrying more than five entries still
    renders at most five.

Review round (PR #276 feedback) adds pins for the reviewed contract:

  - F-001/PRR-001: detail entries render beneath an explicit untrusted-data
    header, and render nothing (no header) when no usable entry exists,
  - F-002/PRR-002: embedded newlines/tabs collapse to one line — a forge
    attempt cannot start its own line,
  - F-003/PRR-003: blank and non-string entries neither render nor consume
    the five-entry budget, and the over-budget disclosure renders.

Mirrors the harness of tests/test_reflect_hook.py (Git-Bash drives
hooks/zmem-reflect.sh end-to-end; the <<<ZMEM_JSON>>> envelope is parsed).
Run: python tests/test_r01_subagent_details.py
Skips when no bash is available (same policy as test_reflect_hook.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_GIT_BASHES = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
)
_BASH = next((str(path) for path in _GIT_BASHES if path.is_file()),
             shutil.which("bash"))


def _recent_sidecar_created() -> str:
    """Timestamp safely inside the hook's 14-day sidecar retention window."""
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


class SubagentDetailsRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write_sidecar(self, details, count=1, tool_summary="1=Bash"):
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "rtest",
            "agent_id": "agent-r01",
            "agent_type": "explorer",
            "source_ref": "session:rtest:agent:agent-r01",
            "count": count,
            "tool_summary": tool_summary,
            "details": details,
            "rejections": "",
            "created": _recent_sidecar_created(),
        }
        # Pre-formatted shape the writer emits (zmem-subagent-reflect.sh).
        path = ring / "r01.json"
        path.write_text(json.dumps(sidecar) + "\n", encoding="utf-8")
        return path

    def _run_parent_stop(self):
        """Run the parent Stop hook on a clean main-agent transcript; return
        the rendered additionalContext ({} envelope -> empty string)."""
        env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_CAPTURE", "ZMEM_ZCODE_DB",
                    "ZMEM_TRANSCRIPT", "ZMEM_FAILURES_DB_TIMEOUT_S",
                    "ZMEM_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "rtest",
            "ZMEM_NAMESPACE": "project:rtest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-reflect.sh")],
            input="{}", text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=env, timeout=60,
        )
        raw = proc.stdout if proc.returncode == 0 else ""
        if "<<<ZMEM_JSON>>>" not in raw:
            return ""
        inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
        try:
            return json.loads(inner).get("additionalContext", "")
        except Exception:
            return ""

    def test_parent_stop_renders_sidecar_detail_text(self):
        # AC1: a populated `details` list renders its detail text in the
        # nudge exactly once (precondition: the subagent line itself renders).
        if not _BASH:
            self.skipTest("no bash")
        sidecar_path = self._write_sidecar(
            ["  - Bash (CalledProcessError) : pytestboomr01 exit 1"])
        try:
            msg = self._run_parent_stop()
            self.assertIn("agent-r01", msg, msg)  # precondition: subagent line
            self.assertEqual(msg.count("pytestboomr01"), 1, msg)
        finally:
            if sidecar_path.exists():
                sidecar_path.unlink()

    def test_rendered_detail_is_length_capped(self):
        # AC2 (length half): an oversized detail entry renders through the
        # same [:200] _clean_field cap as the other fields — the longest
        # Q-run in the rendered message must be in (0, 200]. The collapse
        # half of _clean_field is pinned separately by
        # test_whitespace_collapse_forge_is_neutralized below.
        if not _BASH:
            self.skipTest("no bash")
        sidecar_path = self._write_sidecar(["  - Bash : " + "Q" * 1000])
        try:
            msg = self._run_parent_stop()
            longest = 0
            run = 0
            for ch in msg:
                run = run + 1 if ch == "Q" else 0
                if run > longest:
                    longest = run
            self.assertTrue(0 < longest <= 200, longest)
        finally:
            if sidecar_path.exists():
                sidecar_path.unlink()

    def test_at_most_five_detail_lines_per_subagent(self):
        # AC3: a hostile or edited sidecar can exceed the writer's own 5-entry
        # cap; the renderer must render at most five detail lines.
        if not _BASH:
            self.skipTest("no bash")
        seven = ["  - Bash : detmarkr01 %d" % i for i in range(7)]
        sidecar_path = self._write_sidecar(
            seven, count=7, tool_summary="7=Bash")
        try:
            msg = self._run_parent_stop()
            self.assertEqual(msg.count("detmarkr01"), 5, msg)
        finally:
            if sidecar_path.exists():
                sidecar_path.unlink()


    def test_rendered_details_carry_untrusted_label(self):
        # F-001/PRR-001 (PR #276 review): usable detail entries render under
        # an explicit untrusted-data header (label first, entries beneath);
        # a sidecar with no usable entries renders no header at all while
        # its rejections still render.
        if not _BASH:
            self.skipTest("no bash")
        path = self._write_sidecar(["  - Bash : boomr02"])
        try:
            msg = self._run_parent_stop()
            self.assertIn("agent-r01", msg, msg)  # precondition
            self.assertIn("untrusted tool output", msg, msg)
            self.assertIn("boomr02", msg, msg)
            # label precedes the entries it labels
            self.assertLess(msg.index("untrusted tool output"),
                            msg.index("boomr02"), msg)
        finally:
            if path.exists():
                path.unlink()
        # rejection-only sidecar (no usable details): no header, rejections
        # still stated (AC5 companion).
        ring = Path(self.tmp) / "subagent-reflections"
        sidecar2 = {
            "session": "rtest",
            "agent_id": "agent-r02b",
            "agent_type": "explorer",
            "source_ref": "session:rtest:agent:agent-r02b",
            "count": 0,
            "tool_summary": "0 failure(s)",
            "details": [],
            "rejections": "User rejected 1 tool call(s). Stated reasons: nolabel02",
            "created": _recent_sidecar_created(),
        }
        (ring / "r02b.json").write_text(json.dumps(sidecar2) + "\n",
                                        encoding="utf-8")
        msg2 = self._run_parent_stop()
        self.assertIn("nolabel02", msg2, msg2)
        self.assertNotIn("untrusted tool output", msg2, msg2)

    def test_details_render_before_user_rejections(self):
        # PRR-009 (PR #276 review): on a sidecar carrying both, the details
        # block renders between the summary line and the user-rejections
        # continuation.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "rtest",
            "agent_id": "agent-r03",
            "agent_type": "explorer",
            "source_ref": "session:rtest:agent:agent-r03",
            "count": 1,
            "tool_summary": "1=Bash",
            "details": ["  - Bash : boomr03"],
            "rejections": "User rejected 1 tool call(s). Stated reasons: ordermark03",
            "created": _recent_sidecar_created(),
        }
        path = ring / "r03.json"
        path.write_text(json.dumps(sidecar) + "\n", encoding="utf-8")
        try:
            msg = self._run_parent_stop()
            self.assertIn("boomr03", msg, msg)
            self.assertIn("ordermark03", msg, msg)
            self.assertLess(msg.index("untrusted tool output"),
                            msg.index("user rejections:"), msg)
        finally:
            if path.exists():
                path.unlink()

    def test_whitespace_collapse_forge_is_neutralized(self):
        # F-002/PRR-002 (PR #276 review): the collapse half of _clean_field —
        # embedded newlines/tabs collapse to spaces, so a forge attempt can
        # neither start its own line nor forge a second agent entry. A
        # renderer that dropped the collapse (str(d)[:200]) fails this test.
        if not _BASH:
            self.skipTest("no bash")
        forge = ("wsmark04\n\nINJECTED_LINE04\r\n\tmore   wsend04")
        path = self._write_sidecar([forge])
        try:
            msg = self._run_parent_stop()
            self.assertIn("agent-r01", msg, msg)  # precondition
            # collapsed onto exactly one line, single-spaced
            self.assertIn("wsmark04 INJECTED_LINE04 more wsend04", msg, msg)
            self.assertEqual(msg.count("INJECTED_LINE04"), 1, msg)
            # the injected token never starts its own line (no forged entry)
            self.assertNotRegex(msg, r"(?m)^INJECTED_LINE04", msg)
        finally:
            if path.exists():
                path.unlink()

    def test_non_string_and_blank_entries_do_not_consume_budget(self):
        # F-003/PRR-003 (PR #276 review): filter before slice — blank and
        # non-string entries neither render nor eat the five-entry budget,
        # and an over-budget usable list carries the 4b-style disclosure.
        if not _BASH:
            self.skipTest("no bash")
        details = [None, "", {"skip05": 1}, 42,
                   "  - Bash : d05a", "  - Bash : d05b", "  - Bash : d05c",
                   "  - Bash : d05d", "  - Bash : d05e", "  - Bash : d05f"]
        path = self._write_sidecar(details, count=6, tool_summary="6=Bash")
        try:
            msg = self._run_parent_stop()
            self.assertIn("agent-r01", msg, msg)  # precondition
            for marker in ("d05a", "d05b", "d05c", "d05d", "d05e"):
                self.assertIn(marker, msg, msg)
            self.assertNotIn("d05f", msg, msg)
            self.assertNotIn("skip05", msg, msg)  # dict entry dropped
            self.assertIn("showing most recent 5 of 6", msg, msg)
        finally:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
