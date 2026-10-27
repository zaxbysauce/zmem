"""Conditional-lesson contradiction link at write time — acceptance spec.

Defect under test: ``store.py add`` accepts a CONDITIONAL counter-lesson
("Never use git checkout -- when the fix is uncommitted") into a namespace
that already holds the UNCONDITIONAL counterpart ("Always restore probe
files with git checkout -- after every probe") without linking or even
noticing the pair — the operator gets no signal that a conditional override
now shadows an existing rule.

POST-FIX CONTRACT pinned here (all through the public ``store.py add``
subprocess with ``--json``):

- The counter-row is still STORED (both rows live; exit 0, result
  "stored") — the check is advisory, never a refusal.
- The structured ``warnings`` list carries an object identifying the
  EXISTING seeded row (some warning dict whose serialized form contains the
  seeded row's id string).
- stderr carries a human notice naming the existing row id.
- Negative control: adding an UNRELATED row produces no contradiction
  warning naming the seeded id.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (storelib freezes the store path at first import — an
unpinned child could auto-migrate the shared operator store). Runs
standalone: python -X utf8 tests/test_add_conditional_contradiction_link.py
"""

from __future__ import annotations

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

NS = "project:contra-probe"
ROW_UNCONDITIONAL = ("Always restore probe files with git checkout -- "
                     "after every probe")
ROW_CONDITIONAL = ("Never use git checkout -- when the fix is uncommitted; "
                   "use cp backup and restore instead")
ROW_UNRELATED = "Prefer py -3 over python3 on this box"

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
        "ZMEM_SESSION": "contra-probe-test-session",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _add(env: dict, content: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", NS, "--type", "lesson", "--content", content,
         "--signal", "reviewer", "--confidence", "0.9", "--json"],
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


def _warnings_mentioning(parsed: dict, needle: str) -> list:
    """Warning entries whose serialized form carries ``needle``."""
    hits = []
    for w in parsed.get("warnings") or []:
        blob = json.dumps(w) if isinstance(w, (dict, list)) else str(w)
        if needle in blob:
            hits.append(w)
    return hits


class AddConditionalContradictionLinkTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-contra-probe-")
        self._env = _clean_env(self._tmp)
        seeded = _add(self._env, ROW_UNCONDITIONAL)
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        payload = _parse_json_line(seeded.stdout)
        self.assertEqual(payload.get("result"), "stored", payload)
        self._seeded_id = payload.get("id")
        self.assertIsInstance(self._seeded_id, str)
        self.assertTrue(self._seeded_id)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_conditional_counter_row_links_existing(self):
        r = _add(self._env, ROW_CONDITIONAL)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_json_line(r.stdout)
        # (a) both rows live — the advisory never blocks the write.
        self.assertEqual(parsed.get("result"), "stored", parsed)
        # (b) a structured warning identifies the EXISTING row.
        self.assertTrue(
            _warnings_mentioning(parsed, self._seeded_id),
            "warnings must contain an object naming the existing row id "
            f"{self._seeded_id!r}; got {parsed.get('warnings')!r}")
        # (c) a human stderr notice names the existing row id.
        self.assertIn(self._seeded_id, r.stderr,
                      "stderr must carry a human notice naming the "
                      f"existing row id; stderr={r.stderr!r}")

    def test_unrelated_row_no_contradiction_warning(self):
        r = _add(self._env, ROW_UNRELATED)
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse_json_line(r.stdout)
        self.assertEqual(parsed.get("result"), "stored", parsed)
        self.assertFalse(
            _warnings_mentioning(parsed, self._seeded_id),
            "an unrelated row must not raise a contradiction warning "
            f"naming {self._seeded_id!r}; got {parsed.get('warnings')!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
