"""Subagent task-text recall via the Agent tool's PreToolUse payload
(issue #119, Workstream D PR 4).

Proves the two-moment handoff through the REAL launcher chain:
- PreToolUse(tool_name=Agent) parks the delegating tool_input.prompt in the
  hashed task-text sidecar and stays SILENT for the parent;
- SubagentStart builds its recall query from the stashed text (FIFO; exact
  agent_id match is future-host wiring — no probed host supplies agent_id
  at park time), with the parent transcript tail as the fallback rung and
  the queryless recency pull last.

All stores are throwaway temp stores. Runs standalone:
python tests/test_subagent_tasktext.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
LAUNCHER = REPO_ROOT / "hooks" / "zmem-launch.js"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

MARKER_A = "zmem119taskA"
MARKER_B = "zmem119taskB"

# The gate-sensitive sequence tests isolate the #113 lexical floor: their
# subject is the task-text MECHANICS (stash → consume → query), not the
# relevance-gate calibration the frozen checks exercise at default floors.
_FLOOR = {"ZMEM_INJECT_FLOOR_LEX": "0"}

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
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    })
    env.update(extra)
    return env


def _seed(env: dict, ns: str, content: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "store.py"), "add",
         "--namespace", ns, "--type", "fact", "--content", content,
         "--signal", "test", "--confidence", "0.9"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


def _drive(env: dict, cwd: str, subcommand: str, payload: dict,
           timeout: int = 120):
    return subprocess.run(
        ["node", str(LAUNCHER), subcommand],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=cwd, timeout=timeout)


def _ctx(stdout: str) -> str:
    text = (stdout or "").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        inner = envelope.get("hookSpecificOutput") or {}
        return inner.get("additionalContext") or envelope.get("additionalContext") or ""
    return ""


def _ops_path(tmp: str, sid: str, suffix: str) -> Path:
    h = hashlib.sha256(sid.encode("utf8")).hexdigest()[:32]
    return Path(tmp) / "ops" / (h + suffix)


def _seed_fixture(env: dict, ns: str) -> None:
    """Byte-identical to the frozen C1/C3 fixtures: the task rows FIRST,
    then FIVE lexically-diverse newer rows after a 1.5s gap (the
    subagent-recall recent pull is limit 5 / global 3, `add` dedups at
    0.85 cosine, and ingestion_ts has second granularity with no
    tiebreaker — see the frozen checks' adaptation notes)."""
    _seed(env, ns,
          "merge queue ratchet flake quarantine re-run guidance " + MARKER_A)
    _seed(env, ns,
          "code review swarms require pairwise sign-off before merge " + MARKER_B)
    time.sleep(1.5)
    for content, ref in (
        ("build cache eviction policy scans the ubuntu runner images nightly filler1", "f1"),
        ("editorial review prefers tables over bullet spam for short facts filler2", "f2"),
        ("winter tires for the fleet vans get swapped in november before mountain routes freeze filler3", "f3"),
        ("the greenhouse tomatoes need staking before the august storms arrive filler4", "f4"),
        ("archival paper stock prefers alkaline buffering for long-term storage filler5", "f5"),
    ):
        _seed(env, ns, content)


class TaskTextSidecarUnitTest(unittest.TestCase):
    """delivery_ledger task-text sidecar API (pure unit)."""

    def setUp(self):
        import storelib.delivery_ledger as dl
        self.dl = dl
        self._tmp = tempfile.mkdtemp(prefix="zmem119-unit-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_park_consume_fifo(self):
        self.dl.park_task_text(self._tmp, "s1", "first task text")
        self.dl.park_task_text(self._tmp, "s1", "second task text")
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s1"),
                         "first task text")
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s1"),
                         "second task text")
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s1"), "")

    def test_agent_id_exact_match_wins_over_fifo(self):
        # Future-host wiring: no probed host supplies agent_id at park
        # time today — this pins the API contract for the host that does.
        self.dl.park_task_text(self._tmp, "s2", "plain task", agent_id="")
        self.dl.park_task_text(self._tmp, "s2", "tagged task", agent_id="ag-x")
        self.assertEqual(
            self.dl.consume_task_text(self._tmp, "s2", agent_id="ag-x"),
            "tagged task")
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s2"),
                         "plain task")

    def test_cap_and_window(self):
        old = time.time() - 7 * 3600  # past the 6h window
        self.dl.park_task_text(self._tmp, "s3", "stale task", now=old)
        self.dl.park_task_text(self._tmp, "s3", "fresh task")
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s3"),
                         "fresh task")  # the stale entry window-pruned
        for i in range(self.dl.TASK_TEXT_CAP + 3):
            self.dl.park_task_text(self._tmp, "s4", f"task {i}")
        stash = json.loads(_ops_path(self._tmp, "s4", ".tasktext")
                           .read_text(encoding="utf-8"))
        self.assertEqual(len(stash["entries"]), self.dl.TASK_TEXT_CAP)
        self.assertEqual(stash["entries"][0]["text"],
                         f"task {3}")  # oldest evicted first

    def test_clear_delivery_state_keeps_tasktext_stash(self):
        # The load-bearing lifecycle claim: a subagent's task text must
        # survive another moment's delivery-state clear.
        self.dl.record(self._tmp, "s5", [{"id": "k", "content": "z"}],
                       "user_prompt")
        self.dl.park_task_text(self._tmp, "s5", "survivor task")
        self.dl.clear_delivery_state(self._tmp, "s5")
        self.assertFalse(_ops_path(self._tmp, "s5", ".ledger").exists())
        self.assertTrue(_ops_path(self._tmp, "s5", ".tasktext").exists())
        self.assertEqual(self.dl.consume_task_text(self._tmp, "s5"),
                         "survivor task")

    def test_sweep_reaps_stale_tasktext(self):
        stash = _ops_path(self._tmp, "s6", ".tasktext")
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_text("{}", encoding="utf-8")
        old = time.time() - 90 * 86400
        os.utime(stash, (old, old))
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "sweep",
             "--marker-dir", self._tmp, "--max-age-days", "30"],
            capture_output=True, text=True, timeout=120,
            env=_clean_env(self._tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(stash.exists())


class TaskTextSequenceTest(unittest.TestCase):
    """The two-moment handoff through the launcher (behavioral)."""

    SID = "tasktext119-seq"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem119-seq-")
        self._workdir = os.path.join(self._tmp, "workdir")
        os.makedirs(self._workdir, exist_ok=True)
        import host as host_mod
        self.NS = host_mod.resolve_namespace(self._workdir)
        self._env = _clean_env(self._tmp)
        _seed_fixture(self._env, self.NS)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _drive(self, sub, payload, env=None):
        return _drive(env or self._env, self._workdir, sub, payload)

    def _agent_pretool(self, prompt, env=None):
        return self._drive("pretool-recall", {
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"description": "delegated lane",
                           "prompt": prompt},
            "session_id": self.SID, "cwd": self._workdir}, env=env)

    def _subagent_start(self, agent_id, transcript_path="", env=None):
        return self._drive("subagent-recall", {
            "hook_event_name": "SubagentStart", "session_id": self.SID,
            "agent_id": agent_id, "agent_type": "general-purpose",
            "cwd": self._workdir, "transcript_path": transcript_path},
            env=env)

    def test_stash_then_subagent_query(self):
        env = dict(self._env, **_FLOOR)
        p = self._agent_pretool(
            "fix the failing merge-queue ratchet flake quarantine lane "
            "about " + MARKER_A, env=env)
        self.assertEqual(p.returncode, 0)
        p = self._subagent_start("agent-a", env=env)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn(MARKER_A, ctx)
        # The stash is consumed (entry-removed).
        stash = _ops_path(self._tmp, self.SID, ".tasktext")
        self.assertFalse(stash.exists())

    def test_agent_pretool_stays_silent_for_parent(self):
        # R3: the delegating PARENT must not get the child's recall —
        # silence is the contract.
        p = self._agent_pretool(
            "fix the failing merge-queue ratchet flake quarantine lane "
            "about " + MARKER_A)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(_ctx(p.stdout), "")

    def test_two_children_get_their_own_task_text(self):
        # SEQUENTIAL dispatch (host dispatch order = FIFO consume order);
        # out-of-order arrival of truly concurrent children is the
        # documented FIFO limitation (plan R2/R6).
        env = dict(self._env, **_FLOOR)
        self._agent_pretool(
            "fix the failing merge-queue ratchet flake quarantine lane "
            "about " + MARKER_A, env=env)
        p = self._subagent_start("agent-a", env=env)
        ctx_a = _ctx(p.stdout)
        self._agent_pretool(
            "run the code-review swarm pairwise sign-off lane about " + MARKER_B,
            env=env)
        p = self._subagent_start("agent-b", env=env)
        ctx_b = _ctx(p.stdout)
        self.assertIn(MARKER_A, ctx_a)
        self.assertIn(MARKER_B, ctx_b)

    def test_redaction_runs_and_agent_park_silent(self):
        # PR #192 review: the F-001 redaction must actually RUN in the
        # production shape (the bare import was a silent no-op — cubic
        # P2/Copilot) and the parent stays silent.
        env = dict(self._env, **_FLOOR)
        p = self._drive("pretool-recall", {
            "hook_event_name": "PreToolUse", "tool_name": "Agent",
            "tool_input": {"description": "delegated lane",
                           "prompt": "fix the AKIAIOSFODNN7EXAMPLE leak"},
            "session_id": self.SID, "cwd": self._workdir}, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(_ctx(p.stdout), "")
        stash = _ops_path(self._tmp, self.SID, ".tasktext")
        self.assertTrue(stash.exists())
        content = stash.read_text(encoding="utf-8")
        self.assertIn("[REDACTED_SECRET]", content)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", content)

    def test_transcript_tail_reads_tool_use_input(self):
        # PR #192 review (cubic P2): tool_use content items carry the
        # delegation in input.prompt — the tail rung must extract them.
        transcript = Path(self._tmp) / "parent-tu.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Agent",
                 "input": {"prompt": "fix the failing merge-queue ratchet "
                                     "flake quarantine lane about "
                                     + MARKER_A}}]}}) + chr(10),
            encoding="utf-8")
        env = dict(self._env, **_FLOOR)
        p = self._drive("subagent-recall", {
            "hook_event_name": "SubagentStart", "session_id": self.SID,
            "agent_id": "agent-tu", "agent_type": "general-purpose",
            "cwd": self._workdir, "transcript_path": str(transcript)},
            env=env)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn(MARKER_A, ctx)

    def test_transcript_tail_fallback_rung(self):
        # No stash; the parent transcript tail (exported by the launcher
        # from transcript_path) drives the query.
        transcript = Path(self._tmp) / "parent.jsonl"
        transcript.write_text(
            json.dumps({"type": "user",
                        "message": {"content": [
                            {"type": "text",
                             "text": "fix the failing merge-queue ratchet "
                                     "flake quarantine lane about " + MARKER_A}]}}) + "\n",
            encoding="utf-8")
        env = dict(self._env, **_FLOOR)
        p = self._subagent_start("agent-t",
                                 transcript_path=str(transcript), env=env)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn(MARKER_A, ctx)

    def test_recent_fallback_when_no_stash_no_transcript(self):
        # AC2: the queryless recency pull still works when both query
        # rungs are empty — the newest row surfaces, the old task rows
        # do not (they sit outside the limit-5 window).
        env = dict(self._env, **_FLOOR)
        _seed(env, self.NS,
              "recent lane row for the stashless subagent newerZ")
        p = self._subagent_start("agent-r", env=env)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("newerZ", ctx)
        self.assertNotIn(MARKER_A, ctx)

    def test_kill_switch_leaves_no_stash(self):
        env0 = dict(self._env, ZMEM_INJECT="0")
        p = self._agent_pretool(
            "fix the failing merge-queue ratchet flake quarantine lane "
            "about " + MARKER_A, env=env0)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(
            _ops_path(self._tmp, self.SID, ".tasktext").exists())


class RegistrationNeedleTest(unittest.TestCase):
    """Source-text pins for the registration + sweep + doc surfaces."""

    def test_claude_matcher_includes_agent(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.claude.json").read_text("utf-8"))
        # 0.31.0: "Task" accepted as the pre-rename delegation tool name
        # (community issue 29677, closed stale — not vendor-confirmed).
        self.assertEqual(hooks["hooks"]["PreToolUse"][0]["matcher"],
                         "Edit|Write|MultiEdit|NotebookEdit|Bash|Agent|Task")

    def test_zcode_matcher_excludes_agent(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.zcode.json").read_text("utf-8"))
        self.assertEqual(hooks["hooks"]["PreToolUse"][0]["matcher"],
                         "Edit|Write|MultiEdit|NotebookEdit|Bash")

    def test_codex_matcher_unchanged(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_text("utf-8"))
        self.assertEqual(hooks["hooks"]["PreToolUse"][0]["matcher"],
                         "Bash|apply_patch")

    def test_skill_carries_dated_tasktext_entry(self):
        import re
        text = (REPO_ROOT / "skills" / "memory" / "SKILL.md").read_text("utf-8")
        # F-005 fix: pin the citation PARAGRAPH-scoped (split on blank
        # lines), not file-wide DOTALL — a file-wide lazy regex was proven
        # vacuous by mutation (an unrelated Agent token in a different
        # paragraph satisfied it).
        para = next((blk for blk in text.split(chr(10) + chr(10))
                     if "#119" in blk and "Agent" in blk
                     and "2026-09-10" in blk), "")
        self.assertTrue(para, "dated #119 task-text/Agent paragraph missing")
        self.assertIn("Edit|Write|MultiEdit|NotebookEdit|Bash|Agent|Task", para,
                      "the #119 paragraph must carry the full matcher")
        self.assertIn("UNVERIFIED by #119", text,
                      "SKILL.md must mark the Codex delegation surface "
                      "unverified by #119")
        self.assertIn("#96", text,
                      "SKILL.md must name #96 as the live-probe owner")


    def test_body_carries_ladder_and_stash(self):
        text = (REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py") \
            .read_text("utf-8")
        self.assertIn('in (', text)
        self.assertIn('"Agent", "Task")', text)
        self.assertIn("redact_secret_like_text", text)
        self.assertIn("consume_task_text", text)
        self.assertIn("_transcript_tail", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
