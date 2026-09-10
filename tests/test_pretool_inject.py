"""Pre-tool inject + subagent task-text + Hermes pre_llm_call delivery tests
(issue #90 / #85 directions C+D+E).

Proves:
- the shared body's "pretool" mode derives the query from the TOOL INPUT
  itself (the #85 failure shape: the only event that sees `git stash pop`
  before it runs) and injects matching hazard lessons — fully silent when
  nothing qualified (no per-tool-call one-liner noise), fail-open, NEVER a
  permissionDecision;
- the pending-inject sidecar for hosts that may ignore pre-tool
  additionalContext (Claude: documented since 2.1.9, older builds ignore
  it): parked pre-tool, delivered by the NEXT user_prompt run even when
  that prompt's own recall is silent, then cleared;
- "subagent" mode prefers the delegated task text over the recent pull when
  the host event carries it, and falls back otherwise;
- the Hermes reflect hook delivers operation-context recall on pre_llm_call
  at most once per ring timestamp, with the ZMEM_QUERY_CONTEXT kill switch;
- E: the shipped skill files carry the decision-point checkpoint contract
  (named hazardous verbs), and the host maps register PreToolUse where the
  contract was probed (ZCode + Claude) and NOT on Codex, whose PreToolUse
  wiring is deliberately deferred to issue #95 (upstream shipped a full
  hooks system after our 2026-08-30 probe — openai/codex#19385 was
  resolved; flip the Codex assertions inside #95's PR, never before).

All stores are throwaway temp stores. Runs standalone:
python tests/test_pretool_inject.py
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
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
REFLECT = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

LESSON = ("pretoolcanary hazard: a later blind git stash pop can apply a "
          "foreign pre-existing stash; verify git stash list before any "
          "consuming command")
TASK_LESSON = ("taskcanary: merge-queue citation shifts renumber registry "
               "rows; re-pin citations before pushing")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    # Hook dir-resolution chains consult the plugin-data vars (host.py:42-66,
    # strip them like test_sweep's DATA_DIR_ENV_VARS so ambient dev-box values
    # can never receive subprocess writes.
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    # #93 A1 residue: eval-runner pollution vars - a single-process
    # multi-file runner must not leak the fake embedder or pinned clock in.
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    # Issue #151 review (CUBIC-killsw-251): the #117 knobs - an ambient
    # ZMEM_PENDING_SIDECAR=1 flips the retired-by-default premise.
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
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


def _seed(env: dict, ns: str, content: str, confidence: str = "0.9") -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "store.py"), "add",
         "--namespace", ns, "--type", "lesson", "--content", content,
         "--signal", "test", "--confidence", confidence],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


def _run_body(tmp: str, mode: str, event: dict, ns: str = "user:global",
              **extra: str) -> tuple[str, int]:
    env = _clean_env(tmp, **extra)
    r = subprocess.run(
        [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
         ns, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=120)
    return r.stdout, r.returncode


def _ctx(stdout: str) -> str:
    text = stdout.strip()
    return json.loads(text)["additionalContext"] if text else ""


class PreToolModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-pretool-")
        _seed(_clean_env(self._tmp), "project:pretool", LESSON)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_bash_command_query_injects_hazard_lesson(self):
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"},
             "session_id": "s-pretool"},
            ns="project:pretool")
        self.assertEqual(rc, 0)
        ctx = _ctx(out)
        self.assertIn("pretoolcanary", ctx)
        self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", ctx)
        line = [l for l in (Path(self._tmp) / "zmem-decisions.log")
                .read_text(encoding="utf-8").splitlines()
                if "zmem-hook" in l][-1]
        self.assertIn("reason=injected", line)
        self.assertRegex(line, r"ops=\d+")

    def test_edit_file_path_derives_basename_query(self):
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Edit",
             "tool_input": {"file_path": "src/lib/pr-workflow-gate.ts"}},
            ns="project:pretool")
        self.assertEqual(rc, 0)
        # No lesson matches the path in this namespace — and a non-operation
        # derivation stays fully silent: no output payload at all (NOT the
        # #87 one-liner; per-tool-call one-liners would be noise).
        self.assertEqual(_ctx(out), "")

    def test_non_operation_event_is_fully_silent(self):
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "plainword"},
             "session_id": "s2"},
            ns="project:pretool")
        self.assertEqual(rc, 0)
        self.assertEqual(_ctx(out), "")

    def test_unset_host_does_not_park(self):
        # Review PRR-92-T1/T10: the park gate is ZMEM_HOST=="claude" exactly;
        # an unset host must not write a sidecar (asserted so a regression to
        # park-on-any-host fails loudly here).
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"},
             "session_id": "s-unset"},
            ns="project:pretool")
        self.assertEqual(rc, 0)
        self.assertIn("pretoolcanary", _ctx(out))  # direct emit still happens
        self.assertFalse(
            Path(self._tmp, "ops", "s-unset.pending").exists(),
            "unset ZMEM_HOST must not park a sidecar")

    def test_kill_switch_silences_the_pretool_lane_too(self):
        # Review round 1: ZMEM_QUERY_CONTEXT=0 is a GLOBAL kill switch — an
        # operator flipping it expects silence everywhere, and this lane
        # costs a subprocess per matched tool call.
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"},
             "session_id": "s3"},
            ns="project:pretool", ZMEM_QUERY_CONTEXT="0")
        self.assertEqual(rc, 0)
        self.assertEqual(_ctx(out), "")

    def test_missing_store_fails_open(self):
        tmp2 = tempfile.mkdtemp(prefix="zmem-pretool-nostore-")
        try:
            out, rc = _run_body(
                tmp2, "pretool",
                {"tool_name": "Bash", "tool_input": {"command": "git stash pop"}})
            self.assertEqual(rc, 0)
            self.assertEqual(_ctx(out), "")
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_never_emits_permission_decision(self):
        # C contract: surfacing only — no permissionDecision anywhere.
        src = BODY.read_text(encoding="utf-8")
        self.assertNotIn("permissionDecision", src)
        wrapper = (REPO_ROOT / "hooks" / "zmem-pretool-recall.sh") \
            .read_text(encoding="utf-8")
        self.assertNotIn("permissionDecision", wrapper)
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"}})
        self.assertEqual(rc, 0)
        self.assertNotIn("permissionDecision", out)


class PendingSidecarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-pending-")
        _seed(_clean_env(self._tmp), "project:pending", LESSON)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_claude_parks_and_next_prompt_delivers(self):
        # Issue #117 AMENDMENT: the sidecar is retired by default, so the
        # park/deliver contract now pins the NARROW FALLBACK
        # (ZMEM_PENDING_SIDECAR=1) on the parking AND delivery legs (the
        # writer gates the park, the consumer gates the delivery). The
        # parked file is hash-keyed now (sha256 of the full session id),
        # hence the glob instead of the literal sanitize+truncate name.
        # Leg 3 runs WITHOUT the env: after the fence is consumed, the
        # delivery ledger alone must keep the row suppressed.
        def _pending_file():
            ops = Path(self._tmp, "ops")
            files = sorted(ops.glob("*.pending")) if ops.is_dir() else []
            return files[0] if files else None

        # 1) Pre-tool run on the claude host parks the fence (fallback env).
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"},
             "session_id": "s-pend"},
            ns="project:pending", ZMEM_HOST="claude",
            ZMEM_PENDING_SIDECAR="1")
        self.assertEqual(rc, 0)
        self.assertIn("pretoolcanary", _ctx(out))  # direct emit still happens
        pending = _pending_file()
        self.assertTrue(pending is not None and pending.is_file(),
                        "claude must park the fence (under the fallback env)")

        # 2) Next user_prompt run (prose that recalls NOTHING) still
        #    delivers the parked fence and clears the sidecar.
        out, rc = _run_body(
            self._tmp, "user_prompt",
            {"prompt": "keep going with unrelated zebra work",
             "session_id": "s-pend"},
            ns="project:pending", ZMEM_HOST="claude",
            ZMEM_PENDING_SIDECAR="1")
        self.assertEqual(rc, 0)
        ctx = _ctx(out)
        self.assertIn("pretoolcanary", ctx)
        self.assertTrue(_pending_file() is None, "sidecar must be consumed")

        # 3) A third run (default mode) does not re-deliver: the ledger
        #    recorded the pretool delivery, so the row stays suppressed
        #    with no sidecar involved at all.
        out, rc = _run_body(
            self._tmp, "user_prompt",
            {"prompt": "keep going with unrelated zebra work",
             "session_id": "s-pend"},
            ns="project:pending", ZMEM_HOST="claude")
        self.assertEqual(rc, 0)
        self.assertNotIn("pretoolcanary", _ctx(out))

    def test_zcode_does_not_park(self):
        out, rc = _run_body(
            self._tmp, "pretool",
            {"tool_name": "Bash", "tool_input": {"command": "git stash pop"},
             "session_id": "s-z"},
            ns="project:pending", ZMEM_HOST="zcode")
        self.assertEqual(rc, 0)
        self.assertIn("pretoolcanary", _ctx(out))
        self.assertFalse(
            Path(self._tmp, "ops", "s-z.pending").exists(),
            "zcode additionalContext is documented honored — no sidecar")


class SubagentModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-subagent-")
        _seed(_clean_env(self._tmp), "project:subagent", TASK_LESSON)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_task_text_becomes_the_query(self):
        out, rc = _run_body(
            self._tmp, "subagent",
            {"prompt": "fix the merge queue citation failures", "session_id": "s"},
            ns="project:subagent")
        self.assertEqual(rc, 0)
        self.assertIn("taskcanary", _ctx(out))

    def test_no_task_text_falls_back_to_recent(self):
        out, rc = _run_body(
            self._tmp, "subagent",
            {"session_id": "s"}, ns="project:subagent")
        self.assertEqual(rc, 0)
        # The recent pull surfaces the seeded lesson by recency.
        self.assertIn("taskcanary", _ctx(out))


class HermesReflectDeliveryTest(unittest.TestCase):
    def _run_reflect(self, tmp: str) -> str:
        env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT))
        r = subprocess.run(
            [sys.executable, str(REFLECT)],
            input=json.dumps({"session_id": "s-reflect"}),
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_fresh_ring_delivers_once_then_silent(self):
        tmp = tempfile.mkdtemp(prefix="zmem-reflect-")
        try:
            _seed(_clean_env(tmp), "user:global", LESSON)
            ring = Path(tmp, "ops", "s-reflect.log")
            ring.parent.mkdir(parents=True)
            ring.write_text(
                json.dumps({"ts": 200, "tool": "Bash",
                            "ops": "git stash pop"}) + "\n",
                encoding="utf-8")
            first = self._run_reflect(tmp)
            self.assertIn("pretoolcanary", first)
            self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", first)
            # Same ring (no new verbs) → silent.
            second = self._run_reflect(tmp)
            self.assertEqual(second, "{}")
            # New verb timestamp → delivered again.
            with open(ring, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": 300, "tool": "Bash",
                                    "ops": "git push origin"}) + "\n")
            third = self._run_reflect(tmp)
            self.assertIn("pretoolcanary", third)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kill_switch_disables_delivery(self):
        tmp = tempfile.mkdtemp(prefix="zmem-reflect-ks-")
        try:
            _seed(_clean_env(tmp), "user:global", LESSON)
            ring = Path(tmp, "ops", "s-reflect.log")
            ring.parent.mkdir(parents=True)
            ring.write_text(
                json.dumps({"ts": 200, "tool": "Bash",
                            "ops": "git stash pop"}) + "\n",
                encoding="utf-8")
            env = _clean_env(tmp, ZMEM_HOME=str(REPO_ROOT),
                             ZMEM_QUERY_CONTEXT="0")
            r = subprocess.run(
                [sys.executable, str(REFLECT)],
                input=json.dumps({"session_id": "s-reflect"}),
                capture_output=True, text=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "{}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_same_second_event_still_delivers_and_no_meta_write(self):
        """Final-critic findings: (1) a second event appended in the SAME
        second (ring ts are int(time.time())) must still deliver — a
        ts-only comparison suppressed it forever; (2) the delivery path
        persists ONLY under the ops/ sidecar namespace — the store's meta
        table must not grow."""
        import sqlite3

        tmp = tempfile.mkdtemp(prefix="zmem-reflect-ss-")
        try:
            _seed(_clean_env(tmp), "user:global", LESSON)
            ring = Path(tmp, "ops", "s-reflect.log")
            ring.parent.mkdir(parents=True)
            same_second = json.dumps({"ts": 200, "tool": "Bash",
                                      "ops": "git stash pop"}) + "\n"
            ring.write_text(same_second, encoding="utf-8")

            db = os.path.join(tmp, "store.sqlite")
            conn = sqlite3.connect(db)
            meta_before = conn.execute(
                "SELECT key, value FROM meta ORDER BY key").fetchall()
            conn.close()

            first = self._run_reflect(tmp)
            self.assertIn("pretoolcanary", first)
            # Same ring → silent (cursor (200,1) already delivered).
            self.assertEqual(self._run_reflect(tmp), "{}")
            # SECOND event in the SAME second: cursor (200,2) > (200,1)
            # must deliver — the count half of the cursor exists for this.
            with open(ring, "a", encoding="utf-8") as f:
                f.write(same_second)
            third = self._run_reflect(tmp)
            self.assertIn("pretoolcanary", third,
                          "same-second event must not be suppressed")

            # Sidecar marker exists; the store's meta table did not grow
            # from the delivery path (the nudge-flag keys predate this PR
            # and no nudge fired in this fixture).
            marker = Path(tmp, "ops", "s-reflect.delivered")
            self.assertTrue(marker.is_file())
            conn = sqlite3.connect(db)
            meta_after = conn.execute(
                "SELECT key, value FROM meta ORDER BY key").fetchall()
            conn.close()
            self.assertEqual(meta_before, meta_after,
                             "query-context delivery must not write the "
                             "store's meta table (sidecar-only persistence)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RegistrationAndContractTest(unittest.TestCase):
    """Where PreToolUse is registered (probed hosts); the wired Codex state
    (issue #95 flipped the former deferral pin in this PR); the E
    skill-contract text; launcher verbs."""

    def test_pretool_registered_on_zcode_claude_and_codex(self):
        for name in ("hooks.zcode.json", "hooks.claude.json"):
            cfg = json.loads(
                (REPO_ROOT / "hooks" / name).read_text(encoding="utf-8"))
            self.assertIn("PreToolUse", cfg["hooks"], name)
            entries = cfg["hooks"]["PreToolUse"]
            self.assertEqual(entries[0]["matcher"],
                             "Edit|Write|MultiEdit|NotebookEdit|Bash", name)
            self.assertIn("pretool-recall",
                          entries[0]["hooks"][0]["command"], name)
        codex = json.loads(
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_text(encoding="utf-8"))
        # Issue #95 flip (this PR): the former assertNotIn deferral pin is
        # inverted. Codex PreToolUse is REGISTERED with the matcher dumped
        # live from codex-cli 0.153.0 (2026-09-09): shell ops emit `Bash`,
        # file patches emit `apply_patch`, and Codex treats an all-alnum/pipe
        # matcher as EXACT alternation. Do not change the matcher without a
        # fresh live tool_name re-probe, and never revert this assertion to
        # an absence pin except alongside a deliberate un-registration.
        self.assertIn("PreToolUse", codex["hooks"],
                      "Codex PreToolUse registration (issue #95) is missing — "
                      "this assertion is the #95 flip; re-probe live tool "
                      "names before touching the matcher, never revert to the "
                      "absence pin without un-registering deliberately")
        entries = codex["hooks"]["PreToolUse"]
        self.assertEqual(entries[0]["matcher"], "Bash|apply_patch",
                         "matcher must stay the dump-verified exact "
                         "alternation (live codex-cli 0.153.0 dump, "
                         "2026-09-09, issue #95)")
        self.assertIn("pretool-recall",
                      entries[0]["hooks"][0]["command"])
        # Codex PreCompact rides the same shared precompact handler Claude
        # uses; upstream drops additionalContext on that event, so its
        # payload on Codex is the delivery-ledger clear.
        self.assertIn("PreCompact", codex["hooks"],
                      "Codex PreCompact registration (issue #95) is missing")
        self.assertIn("precompact",
                      codex["hooks"]["PreCompact"][0]["hooks"][0]["command"])

    def test_memory_skill_pins_wired_codex_parity_state(self):
        # Issue #95 flip: the former pin required SKILL.md to say the Codex
        # wiring was deferred to #95; the wiring has landed, so the ceiling
        # pin now ratchets the WIRED-state claim set (presence needles per
        # the host-capability-rot convention — dated probe, matcher, MCP-out
        # decision, PostCompact deferral owner).
        memory_skill = (REPO_ROOT / "skills" / "memory" / "SKILL.md") \
            .read_text(encoding="utf-8")
        self.assertIn("`Bash|apply_patch`", memory_skill,
                      "SKILL.md must state the dump-verified Codex matcher")
        self.assertIn("2026-09-09", memory_skill,
                      "SKILL.md must carry the live-dump probe date")
        self.assertIn("codex-cli 0.153.0", memory_skill,
                      "SKILL.md must carry the probe's codex-cli version")
        self.assertIn("#118", memory_skill,
                      "SKILL.md must name #118 as the PostCompact deferral "
                      "owner")
        self.assertIn(
            "with a live tool_name dump before changing the matcher",
            memory_skill,
            "SKILL.md must keep the live-dump-first re-probe convention")
        self.assertIn(
            "and `write_stdin` are deliberately OUT",
            memory_skill,
            "SKILL.md must record the MCP-and-write_stdin-out decision")

    def test_launcher_knows_the_verb(self):
        src = (REPO_ROOT / "hooks" / "zmem-launch.js").read_text(encoding="utf-8")
        self.assertIn('"pretool-recall": "PreToolUse"', src)
        translated = src.split("const TRANSLATED_HOOKS")[1].split("]);")[0]
        self.assertIn('"pretool-recall"', translated)
        needs_ns = src.split("const NEEDS_NAMESPACE")[1].split("]);")[0]
        self.assertIn('"pretool-recall"', needs_ns)

    def test_subagent_wrapper_pipes_stdin_to_body(self):
        # Review PRR-92-A regression ratchet: the subagent body reads the
        # event from STDIN (task-text lane) — the wrapper MUST pipe $INPUT
        # to it. This exact gap shipped once: the wrapper drained stdin and
        # invoked the body with argv only, silently disabling task-text
        # recall while every body-level test stayed green.
        wrapper = (REPO_ROOT / "hooks" / "zmem-subagent-recall.sh")             .read_text(encoding="utf-8")
        self.assertIn(
            "printf '%s' \"$INPUT\" | \"$PYTHON_BIN\" \"$RECALL_BODY\"",
            wrapper,
            "zmem-subagent-recall.sh must pipe the event to the body")

    def test_wrapper_wires_mode_pretool(self):
        wrapper = (REPO_ROOT / "hooks" / "zmem-pretool-recall.sh") \
            .read_text(encoding="utf-8")
        self.assertIn('"pretool"', wrapper)
        self.assertIn("zmem-recall-body.py", wrapper)
        self.assertIn("<<<ZMEM_JSON>>>", wrapper)

    def test_skill_contract_names_the_checkpoint_verbs(self):
        # E: the shipped skill text MUST name the hazardous operations.
        memory_skill = (REPO_ROOT / "skills" / "memory" / "SKILL.md") \
            .read_text(encoding="utf-8")
        closeout_skill = (REPO_ROOT / "skills" / "closeout" / "SKILL.md") \
            .read_text(encoding="utf-8")
        for text, label in ((memory_skill, "memory"), (closeout_skill, "closeout")):
            for needle in ("stash pop", "reset", "push", "ratchet"):
                self.assertIn(needle, text,
                              f"{label} SKILL.md missing checkpoint verb {needle!r}")
            self.assertIn("blocking review", text, label)

    def test_zcode_subagent_compact_gap_documented(self):
        # D: ZCode has no SubagentStart/PreCompact events (officially
        # unsupported) — the gap must be DOCUMENTED in SKILL.md, not papered
        # over with inert registrations.
        zcode = json.loads(
            (REPO_ROOT / "hooks" / "hooks.zcode.json").read_text(encoding="utf-8"))
        self.assertNotIn("SubagentStart", zcode["hooks"])
        self.assertNotIn("PreCompact", zcode["hooks"])
        issue_note = (REPO_ROOT / "skills" / "memory" / "SKILL.md") \
            .read_text(encoding="utf-8")
        self.assertIn("SubagentStart", issue_note,
                      "the ZCode SubagentStart gap must be stated where "
                      "operators read the surface map")


if __name__ == "__main__":
    unittest.main(verbosity=2)
