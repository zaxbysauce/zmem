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
                    "ZMEM_TRANSCRIPT", "ZMEM_FAILURES_DB_TIMEOUT_S"):
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
        # AC2: an oversized detail entry renders through the same
        # whitespace-collapse + [:200] _clean_field cap as the other fields —
        # the longest Q-run in the rendered message must be in (0, 200].
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
