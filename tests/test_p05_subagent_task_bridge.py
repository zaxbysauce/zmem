"""Issue #253 (Workstream P): the delegated-task bridge for prompt-less
SubagentStart events. RED at base (1f44c4b), GREEN after the store-side
delegating-prompt bridge lands.

#253 reports that on Claude Code the delegated child's SubagentStart
payload carries ONLY {session_id, transcript_path, cwd, prompt_id,
agent_id, agent_type, hook_event_name} — no task text — while the hook
adapter's subagent-mode query ladder consults only event fields
(prompt/task/task_text/description). A child whose only key to its
dispatched task is that ladder therefore starts query-less. The issue's
frozen acceptance checks, pinned here end to end through the REAL hook
body:

- AC1: a prompt-less SubagentStart recalls the memory row matching the
  prompt the SAME session parked moments earlier at PreToolUse(Agent).
- AC2: park_task_text / consume_task_text in storelib are wired to at
  least one non-test call site under skills/ or hooks/ (or removed) —
  defined-and-never-called is a stranded API.
- AC6: two delegations in one session route to their two children in
  dispatch (FIFO) order — child 1 gets delegation A's row and not B's,
  child 2 gets B's and not A's.

All stores are throwaway temp dirs; every store-touching subprocess gets a
per-test clean env (the tests/test_subagent_tasktext.py strip-list
convention) with ZMEM_MODELS_DIR pinned to a non-existent path so recall
runs FTS-only, model-absent. Runs standalone:
python tests/test_p05_subagent_task_bridge.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Store-isolation env MUST be pinned before any storelib import anywhere in
# this process (hard assignment, not setdefault — an ambient ZMEM_STORE must
# never survive into this module's fixtures; the tests/test_p03_ops_token_
# flag_tail.py convention).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-p05-module-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

NS = "project:p05bridge"
SID = "p05-bridge"
MARKER_A = "zmemp05taskA"
MARKER_A1 = "zmemp05taskA1"
MARKER_B1 = "zmemp05taskB1"

DELEGATION_A = "fix the failing merge-queue ratchet flake quarantine lane"
DELEGATION_B = "review the docs snippet generation pipeline coverage checklist"

TASK_ROW = "merge queue ratchet flake quarantine re-run guidance " + MARKER_A
ROW_A1 = ("merge queue ratchet flake quarantine re-run guidance "
          + MARKER_A1)
ROW_B1 = ("docs snippet generation pipeline coverage review checklist "
          + MARKER_B1)

# Five lexically unrelated rows seeded AFTER a 1.5s gap so the task row
# falls outside the limit-5 recent window (the seed fixture convention of
# tests/test_subagent_tasktext.py: ingestion_ts has second granularity).
FILLERS = (
    "build cache eviction policy scans the ubuntu runner images nightly filler1",
    "editorial review prefers tables over bullet spam for short facts filler2",
    "winter tires for the fleet vans get swapped in november before mountain routes freeze filler3",
    "the greenhouse tomatoes need staking before the august storms arrive filler4",
    "archival paper stock prefers alkaline buffering for long-term storage filler5",
)

# Copied from tests/test_subagent_tasktext.py (_STRIP_ENV): the ambient
# keys every store-touching child env must shed.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_SESSION_SOURCE", "ZMEM_TRANSCRIPT",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
        # The #113 lexical floor is off: the subject here is the
        # task-bridge MECHANICS, not relevance-gate calibration.
        "ZMEM_INJECT_FLOOR_LEX": "0",
        # Fixed NON-EXISTENT model dir: recall must run FTS-only.
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
    })
    env.update(extra)
    return env


def _seed(env: dict, ns: str, content: str) -> None:
    r = subprocess.run(
        [sys.executable, str(STORE_PY), "add",
         "--namespace", ns, "--type", "fact", "--content", content,
         "--signal", "test", "--confidence", "0.9"],
        capture_output=True, text=True, env=env, timeout=180,
        stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


def _drive_body(env: dict, mode: str, event: dict) -> subprocess.CompletedProcess:
    """Drive the REAL hook body by subprocess with the event on stdin."""
    return subprocess.run(
        [sys.executable, str(BODY), str(STORE_PY), NS, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=180)


def _ctx(stdout: str) -> str:
    """The additionalContext of the LAST JSON line on stdout (modeled on
    tests/test_pretool_inject.py's _ctx; scans reversed lines so any log
    noise ahead of the envelope cannot shadow it)."""
    text = (stdout or "").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if not isinstance(envelope, dict):
            continue
        inner = envelope.get("hookSpecificOutput")
        if not isinstance(inner, dict):
            inner = {}
        ctx = inner.get("additionalContext")
        if not isinstance(ctx, str):
            ctx = envelope.get("additionalContext")
        return ctx if isinstance(ctx, str) else ""
    return ""


def _agent_pretool_event(prompt: str) -> dict:
    """The delegating parent's PreToolUse(Agent) event (Claude shape)."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": prompt},
        "session_id": SID,
        "cwd": str(REPO_ROOT),
    }


def _subagent_start_event(agent_n: str, transcript: str) -> dict:
    """A prompt-less SubagentStart with ONLY Claude Code's real keys —
    no prompt/task/task_text/description anywhere in the payload."""
    return {
        "session_id": SID,
        "transcript_path": transcript,
        "cwd": str(REPO_ROOT),
        "prompt_id": f"prompt_{agent_n}",
        "agent_id": f"agent_{agent_n}",
        "agent_type": "general-purpose",
        "hook_event_name": "SubagentStart",
    }


class BodyPromptlessSubagentTest(unittest.TestCase):
    """AC1: the prompt-less SubagentStart recalls the delegating prompt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p05-")
        self.env = _clean_env(self.tmp)
        _seed(self.env, NS, TASK_ROW)
        time.sleep(1.5)
        for row in FILLERS:
            _seed(self.env, NS, row)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_promptless_subagent_start_recalls_delegating_prompt(self):
        # The delegating parent dispatches its Agent task at PreToolUse...
        _drive_body(self.env, "pretool", _agent_pretool_event(DELEGATION_A))
        # ...then the child starts with ONLY Claude's real SubagentStart
        # keys — no prompt/task/task_text/description anywhere. The child's
        # recall is keyed to the task its session dispatched, so the task
        # row's marker surfaces in the child's additionalContext. The
        # parent envelope carries no frozen assertion: its render shape is
        # free to differ across the fix boundary, and the discriminator is
        # the child's context alone.
        child = _drive_body(
            self.env, "subagent",
            _subagent_start_event("01A", os.path.join(self.tmp, "t.jsonl")))
        child_ctx = _ctx(child.stdout)
        # assertEqual (not assertTrue) pins the frozen base signature
        # "AssertionError: False != True" from the issue's AC1 expect field.
        self.assertEqual(MARKER_A in child_ctx, True)


class TaskTextStrandedTest(unittest.TestCase):
    """AC2: park_task_text / consume_task_text are not stranded dead code."""

    def test_task_text_functions_are_not_stranded(self):
        # Deterministic scan rule (frozen): walk skills/ for *.py files and
        # hooks/ recursively for files of ANY extension; skip any path with
        # a "tests" or ".agents" path part, and skip __pycache__ entirely —
        # a compiled .pyc embeds the function name as a co_name and would
        # phantom-count as a caller. Within a kept file:
        #   defined = line contains "def <name>" (a definition line);
        #   caller  = line contains "<name>(" (invocation shape), is not
        #             the definition line, and is not a pure comment line
        #             (lstrip().startswith("#")). Docstring prose that
        #             merely mentions the name carries no "(" and never
        #             counts.
        defined = 0
        callers = 0
        for root in (REPO_ROOT / "skills", REPO_ROOT / "hooks"):
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                parts = path.parts
                if ("__pycache__" in parts or "tests" in parts
                        or ".agents" in parts):
                    continue
                if root.name == "skills" and path.suffix != ".py":
                    continue
                try:
                    text = path.read_text(encoding="utf-8",
                                          errors="replace")
                except OSError:
                    continue
                for line in text.splitlines():
                    for name in ("park_task_text", "consume_task_text"):
                        if name not in line:
                            continue
                        if f"def {name}" in line:
                            defined += 1
                        elif (line.lstrip().startswith("#")):
                            continue
                        elif f"{name}(" in line:
                            callers += 1
        stranded = (defined > 0) and (callers == 0)
        # assertEqual pins the frozen base signature "AssertionError:
        # True != False" from the issue's AC2 expect field.
        self.assertEqual(stranded, False)


class FifoCorrelationTest(unittest.TestCase):
    """AC6: two delegations route FIFO to their two children."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p05-fifo-")
        self.env = _clean_env(self.tmp)
        _seed(self.env, NS, ROW_A1)
        _seed(self.env, NS, ROW_B1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_delegations_route_fifo_to_two_children(self):
        # Both delegating parents dispatch BEFORE either child starts
        # (order A then B) — the FIFO shape AC6 pins: child 1's recall is
        # keyed to delegation A, child 2's to delegation B, and neither
        # child ever sees the sibling's task row.
        for prompt in (DELEGATION_A, DELEGATION_B):
            _drive_body(self.env, "pretool", _agent_pretool_event(prompt))
        child1 = _drive_body(
            self.env, "subagent",
            _subagent_start_event("01A1", os.path.join(self.tmp, "t1.jsonl")))
        child2 = _drive_body(
            self.env, "subagent",
            _subagent_start_event("02A2", os.path.join(self.tmp, "t2.jsonl")))
        ctx1 = _ctx(child1.stdout)
        ctx2 = _ctx(child2.stdout)
        self.assertTrue(MARKER_A1 in ctx1)
        self.assertFalse(MARKER_B1 in ctx1)
        self.assertTrue(MARKER_B1 in ctx2)
        self.assertFalse(MARKER_A1 in ctx2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
