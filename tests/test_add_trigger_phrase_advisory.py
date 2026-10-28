"""Trigger-phrase phrasing advisory at write time — acceptance spec.

Defect under test: ``store.py add --type lesson`` silently accepts lessons
that carry NO trigger condition — free-floating retrospective prose ("the
rebase took a long time and the toolchain was confusing") that can never
fire at a decision point because it names no situation. The write path
gives the author no nudge to phrase the lesson in trigger→action form.

POST-FIX CONTRACT pinned here (public ``store.py add`` subprocess with
``--json``):

- A ``--type lesson`` row with NO trigger-condition marker still stores
  (exit 0, result "stored") but its ``warnings`` list gains a
  PHRASING-ADVISORY entry (a warning whose serialized form mentions the
  phrasing/trigger-condition problem).
- A ``--type lesson`` row that DOES carry a trigger-condition marker
  ("When a pull fails on this box, run ... because ...") raises NO
  phrasing advisory.
- A ``--type fact`` row without markers raises NO phrasing advisory —
  facts are exempt (facts state the world; only lessons need triggers).

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (storelib freezes the store path at first import — an
unpinned child could auto-migrate the shared operator store). Runs
standalone: python -X utf8 tests/test_add_trigger_phrase_advisory.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"

NS = "project:phrasing-probe"

LESSON_NO_MARKER = ("The rebase took a long time and the toolchain was "
                    "confusing overall")
LESSON_WITH_MARKER = ("When a pull fails on this box, run git pull "
                      "--rebase, because the branch tracks upstream badly")
FACT_NO_MARKER = "The staging cluster reboots every Tuesday night"

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL", "ZMEM_AUTO_REKEY",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_SESSION": "phrasing-probe-test-session",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _add(env: dict, type_: str, content: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", NS, "--type", type_, "--content", content,
         "--signal", "test", "--confidence", "0.9", "--json"],
        capture_output=True, text=True, env=env, timeout=180,
    )


def _parse_json_line(stdout: str) -> dict:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    return {}


def _phrasing_advisories(parsed: dict) -> list:
    """Warning entries that are the phrasing advisory (mention phrasing)."""
    hits = []
    for w in parsed.get("warnings") or []:
        blob = json.dumps(w) if isinstance(w, (dict, list)) else str(w)
        if "phras" in blob.lower():
            hits.append(w)
    return hits


class AddTriggerPhraseAdvisoryTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-phrasing-probe-")
        self._env = _clean_env(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_lesson_without_trigger_marker_gets_phrasing_advisory(self):
        r = _add(self._env, "lesson", LESSON_NO_MARKER)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_json_line(r.stdout)
        self.assertEqual(parsed.get("result"), "stored", parsed)
        self.assertTrue(
            _phrasing_advisories(parsed),
            "a trigger-less lesson must store WITH a phrasing-advisory "
            f"warning; got {parsed.get('warnings')!r}")

    def test_lesson_with_trigger_marker_no_advisory(self):
        r = _add(self._env, "lesson", LESSON_WITH_MARKER)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_json_line(r.stdout)
        self.assertEqual(parsed.get("result"), "stored", parsed)
        self.assertFalse(
            _phrasing_advisories(parsed),
            "a lesson that carries a trigger condition must NOT raise the "
            f"phrasing advisory; got {parsed.get('warnings')!r}")

    def test_fact_without_markers_exempt(self):
        r = _add(self._env, "fact", FACT_NO_MARKER)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_json_line(r.stdout)
        self.assertEqual(parsed.get("result"), "stored", parsed)
        self.assertFalse(
            _phrasing_advisories(parsed),
            "facts are exempt from the phrasing advisory; got "
            f"{parsed.get('warnings')!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
