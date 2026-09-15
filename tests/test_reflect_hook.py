"""Hook-level tests for the reflection prompts (issue #46).

Drives hooks/zmem-reflect.sh and hooks/zmem-subagent-reflect.sh end-to-end
(via bash) against synthetic Claude Code transcripts containing user
rejections, asserting the rendered additionalContext prompt:

  - the failure count line reports GENUINE failures only (a rejection is not
    miscounted as a failure),
  - user-rejection reasons render in a distinct fenced section with the
    `--signal user` hint,
  - when there are no rejections the prompt renders as before (no rejection
    section), and
  - the subagent hook surfaces a rejection-only transcript instead of no-op'ing.

These are the acceptance behaviours the issue's Tests list calls for at the
prompt level, which the store.py-unit tests cannot reach.

Run: python tests/test_reflect_hook.py
Requires a POSIX-ish `bash` on PATH (as CI provides on both runners); skipped
if unavailable. If the hook under test stops emitting when it should not, the
test FAILS rather than silently passing."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The hooks are bash scripts (they use bash-only constructs). Require `bash`
# specifically; falling back to `sh` (usually dash on Debian/Ubuntu) would make
# the tests fail spuriously instead of skipping. Test bodies skip when unset.
_BASH = shutil.which("bash")


def _write_transcript(records) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _tool_result(tid, content, is_error=True):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": content, "is_error": is_error, "tool_use_id": tid}]}}


def _rejection(tid, name, reason):
    return [
        _tool_use(tid, name),
        _tool_result(tid, "The user doesn't want to proceed.\nthe user said:\n" + reason),
    ]


def _run_hook(hook, env_extra, stdin="{}"):
    """Run a hook script with `stdin` on stdin and env_extra merged, returning
    the raw stdout (the <<<ZMEM_JSON>>>…<<<END>>> envelope)."""
    rc, stdout = _run_hook_rc(hook, env_extra, stdin=stdin)
    if rc != 0:
        return ""
    return stdout


def _run_hook_rc(hook, env_extra, stdin="{}"):
    """Like _run_hook but returns (returncode, stdout) so tests can assert the
    hook's exit code itself (#194: the kill switch and lock fail-open paths
    must exit 0, not merely print an empty envelope). Strips the hook-sensitive
    ZMEM_* vars from the ambient environment first (PRR-005): an operator's
    exported ZMEM_REFLECT=0 or ZMEM_ZCODE_DB must not flip test outcomes."""
    env = dict(os.environ)
    for key in ("ZMEM_REFLECT", "ZMEM_ZCODE_DB", "ZMEM_TRANSCRIPT",
                "ZMEM_FAILURES_DB_TIMEOUT_S"):
        env.pop(key, None)
    env.update(env_extra)
    proc = subprocess.run(
        [_BASH, str(hook)], input=stdin, text=True,
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    return proc.returncode, proc.stdout


def _extract_ctx(raw):
    """Pull the JSON object out of the sentinel envelope; return {} on failure."""
    if "<<<ZMEM_JSON>>>" not in raw:
        return {}
    inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
    try:
        return json.loads(inner)
    except Exception:
        return {}


class TestReflectHookMessaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run(self, hook_name, env_extra, stdin="{}"):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_NAMESPACE": "project:hooktest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        env.update(env_extra)
        return _run_hook(REPO_ROOT / "hooks" / hook_name, env, stdin=stdin)

    def _run_rc(self, hook_name, env_extra, stdin="{}"):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_NAMESPACE": "project:hooktest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        env.update(env_extra)
        return _run_hook_rc(REPO_ROOT / "hooks" / hook_name, env, stdin=stdin)

    def test_failures_plus_rejection_not_miscounted_and_reason_shown(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),   # genuine failure (no marker)
            *_rejection("t2", "Edit", "don't touch the CI config"),  # rejection
        ])
        try:
            raw = self._run("zmem-reflect.sh", {"ZMEM_TRANSCRIPT": os.path.abspath(trans)})
            ctx = _extract_ctx(raw)
            msg = ctx.get("additionalContext", "")
            # Genuine failure count, not inflated to 2 by the rejection.
            self.assertIn("1 failed tool call(s)", msg, msg)
            self.assertNotIn("2 failed tool call(s)", msg, msg)
            # Rejection surfaced in a distinct, fenced section with its reason.
            self.assertIn("User rejected 1 tool call(s)", msg, msg)
            self.assertIn("don't touch the CI config", msg, msg)
            self.assertIn("--signal user", msg, msg)
            # Marker stripped; reason newline-free → appears on exactly one line
            # and cannot open its own fence line (fence-integrity composition).
            self.assertNotIn("the user said:", msg)
            self.assertEqual(msg.count("don't touch the CI config"), 1)
        finally:
            os.remove(trans)

    def test_renders_like_before_when_no_rejections(self):
        if not _BASH:
            self.skipTest("no bash")
        # db path, no transcript, no rejection substrate → must render as before
        # (no rejection section at all).
        raw = self._run("zmem-reflect.sh", {})
        ctx = _extract_ctx(raw)
        msg = ctx.get("additionalContext", "")
        self.assertIn("had no tool failures", msg, msg)   # success nudge kept
        self.assertNotIn("User rejected", msg, msg)       # no rejection section
        self.assertNotIn("Stated reasons", msg, msg)
        self.assertNotIn("--signal user", msg, msg)

    def test_stop_hook_active_loop_guard(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Bash", "stop"))
        try:
            raw = self._run("zmem-reflect.sh", {"ZMEM_TRANSCRIPT": os.path.abspath(trans)},
                            stdin='{"stop_hook_active": true}')
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    def test_reflect_kill_switch_emits_empty(self):
        # #194: ZMEM_REFLECT=0 disables the hook entirely — empty sentinel
        # envelope, exit 0 — even when the transcript contains a failure.
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            rc, raw = self._run_rc("zmem-reflect.sh", {
                "ZMEM_TRANSCRIPT": os.path.abspath(trans),
                "ZMEM_REFLECT": "0",
            })
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    @staticmethod
    def _make_zcode_db(path):
        conn = sqlite3.connect(path)
        # Pin rollback-journal mode: the BEGIN EXCLUSIVE lock below only blocks
        # readers on a delete-journal db (a WAL db would let the reader proceed
        # instantly and the timing assertion would be meaningless).
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("""CREATE TABLE tool_usage(
            session_id TEXT, tool_name TEXT, read_only INT, status TEXT,
            exit_code INT, error_message TEXT, error_type TEXT,
            retry_count INT, destructive INT, completed_at TEXT)""")
        conn.execute("INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("hooktest", "Bash", 0, "error", 1, "compile failed",
                      "BuildError", 0, 0, "2026-01-01"))
        conn.commit()
        conn.close()
        return path

    def test_locked_zcode_db_fails_open(self):
        # #194: a busy ZCode db (BEGIN EXCLUSIVE holder) must fail open inside
        # the bounded budget — hook exits 0 and never claims failed tool calls.
        # Substrate note: the production db is WAL-mode, where readers do not
        # block on a writer's BEGIN EXCLUSIVE at all; this fixture pins
        # rollback-journal mode as the deterministic substrate that engages
        # sqlite's busy handler, which is the mechanism the timeout kwarg
        # controls. The differential-timing assertion (PRR-002) proves the
        # env-driven bound is real end-to-end: with the lock held, the
        # 0.2s-budget run must finish well over 2s faster than the 3.0s-budget
        # run (sqlite's busy handler overshoots ~1.5x, so the slow run waits
        # ~4.5-5s — still inside the hook's 10s subprocess budget). If the env
        # var stopped reaching the reader, both runs would take the same
        # default wait and this assertion would fail.
        if not _BASH:
            self.skipTest("no bash")
        path = os.path.join(self.tmp, "zcode-db.sqlite")
        self._make_zcode_db(path)
        holder = sqlite3.connect(path)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            t0 = time.perf_counter()
            rc, raw = self._run_rc("zmem-reflect.sh", {
                "ZMEM_ZCODE_DB": path,
                "ZMEM_FAILURES_DB_TIMEOUT_S": "0.2",
            })
            fast = time.perf_counter() - t0
            self.assertEqual(rc, 0)
            self.assertNotIn("failed tool call(s)", raw, raw)
            t0 = time.perf_counter()
            rc2, raw2 = self._run_rc("zmem-reflect.sh", {
                "ZMEM_ZCODE_DB": path,
                "ZMEM_FAILURES_DB_TIMEOUT_S": "3.0",
            })
            slow = time.perf_counter() - t0
            self.assertEqual(rc2, 0)
            self.assertNotIn("failed tool call(s)", raw2, raw2)
            self.assertLess(
                fast + 2.0, slow,
                f"bounded wait not honored end-to-end: fast={fast:.2f}s slow={slow:.2f}s")
        finally:
            holder.rollback()
            holder.close()

    def test_zcode_db_override_is_used(self):
        # #194: ZMEM_ZCODE_DB points the db substrate at a scratch copy; a
        # qualifying row there must be detected (override honored end-to-end).
        if not _BASH:
            self.skipTest("no bash")
        path = os.path.join(self.tmp, "zcode-db.sqlite")
        self._make_zcode_db(path)
        rc, raw = self._run_rc("zmem-reflect.sh", {"ZMEM_ZCODE_DB": path})
        self.assertEqual(rc, 0)
        self.assertIn("1 failed tool call(s)", raw, raw)

    def test_stop_marker_guard_agent_id_only(self):
        # #204: EITHER Claude subagent marker alone makes a Stop payload a
        # subagent-context stop — no injection (the clobber vector). Pinned
        # with rc + sentinel so a crashing hook cannot masquerade as a clean
        # no-op (PR review PRR-004).
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            rc, raw = self._run_rc(
                "zmem-reflect.sh",
                {"ZMEM_TRANSCRIPT": os.path.abspath(trans)},
                stdin='{"session_id":"hooktest","agent_id":"agent-9"}')
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    def test_stop_marker_guard_agent_transcript_only(self):
        # #204: OR-semantics — agent_transcript_path alone also suppresses.
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            rc, raw = self._run_rc(
                "zmem-reflect.sh",
                {"ZMEM_TRANSCRIPT": os.path.abspath(trans)},
                stdin='{"session_id":"hooktest","agent_transcript_path":"/tmp/agent-9.jsonl"}')
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(_extract_ctx(raw), {}, raw)
        finally:
            os.remove(trans)

    def test_stop_marker_guard_non_json_payload(self):
        # PRR-005: an unparseable payload must fail open to the main-agent
        # path without crashing the hook (rc 0, sentinel emitted; no transcript
        # here so the success nudge renders — the assertion is the healthy
        # envelope, not silence).
        if not _BASH:
            self.skipTest("no bash")
        rc, raw = self._run_rc("zmem-reflect.sh", {}, stdin="not json at all")
        self.assertEqual(rc, 0)
        self.assertIn("<<<ZMEM_JSON>>>", raw, raw)
        self.assertIn("<<<END>>>", raw, raw)

    def test_stop_consumes_pending_subagent_sidecar(self):
        # #204: the parent-side hand-off — a sidecar written by the
        # SubagentStop hook for THIS session is surfaced in the parent's Stop
        # prompt and consumed (deleted) once rendered; a second Stop with no
        # sidecars carries no subagent section. PRR-001: the stored rejection
        # reason must render too, and a rejection-only sidecar (count=0) must
        # not read as a false "0 failure(s)"-only line.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "hooktest",
            "agent_id": "agent-777",
            "agent_type": "explorer",
            "source_ref": "session:hooktest:agent:agent-777",
            "count": 2,
            "tool_summary": "2=Bash",
            "details": ["  - Bash : boom"],
            "rejections": "User rejected 1 tool call(s). Stated reasons: leave the schema alone",
            "created": "2026-09-15T00:00:00+00:00",
        }
        sidecar_path = ring / "deadbeef.json"
        sidecar_path.write_text(json.dumps(sidecar) + "\n", encoding="utf-8")
        raw = self._run("zmem-reflect.sh", {})  # clean main-agent transcript
        ctx = _extract_ctx(raw)
        msg = ctx.get("additionalContext", "")
        self.assertIn("agent-777", msg, msg)
        self.assertIn("session:hooktest:agent:agent-777", msg, msg)
        self.assertIn("2 failure(s)", msg, msg)
        # PRR-001: the stored rejection reason surfaces in the parent prompt.
        self.assertIn("leave the schema alone", msg, msg)
        self.assertFalse(sidecar_path.exists(), "sidecar must be consumed")
        # Second stop: nothing pending, no subagent section.
        raw2 = self._run("zmem-reflect.sh", {})
        msg2 = _extract_ctx(raw2).get("additionalContext", "")
        self.assertNotIn("agent-777", msg2, msg2)

    def test_stop_rejection_only_sidecar_states_rejections(self):
        # PRR-001: a rejection-only sidecar (count=0) renders the reason and
        # does NOT present a bare misleading "0 failure(s)" line without it.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "hooktest",
            "agent_id": "agent-rej",
            "agent_type": "explorer",
            "source_ref": "session:hooktest:agent:agent-rej",
            "count": 0,
            "tool_summary": "0 failure(s)",
            "details": [],
            "rejections": "User rejected 1 tool call(s). Stated reasons: not that file",
            "created": "2026-09-15T00:00:00+00:00",
        }
        (ring / "rejonly.json").write_text(
            json.dumps(sidecar) + "\n", encoding="utf-8")
        msg = _extract_ctx(self._run("zmem-reflect.sh", {})).get("additionalContext", "")
        self.assertIn("agent-rej", msg, msg)
        self.assertIn("not that file", msg, msg)
        self.assertIn("user rejections:", msg, msg)

    def test_stop_prunes_stale_sidecar(self):
        # PRR-005: a sidecar older than the 14-day retention window is pruned
        # (never rendered, never left on disk).
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        stale = {
            "session": "hooktest",
            "agent_id": "agent-stale",
            "agent_type": "explorer",
            "source_ref": "session:hooktest:agent:agent-stale",
            "count": 1,
            "tool_summary": "1=Bash",
            "details": [],
            "rejections": "",
            "created": "2026-08-01T00:00:00+00:00",
        }
        stale_path = ring / "stale.json"
        stale_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        msg = _extract_ctx(self._run("zmem-reflect.sh", {})).get("additionalContext", "")
        self.assertNotIn("agent-stale", msg, msg)
        self.assertFalse(stale_path.exists(), "stale sidecar must be pruned")

    def test_stop_ignores_other_session_sidecar(self):
        # PRR-005: sidecars belonging to a different session are neither
        # rendered nor consumed.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        other = {
            "session": "some-other-session",
            "agent_id": "agent-other",
            "agent_type": "explorer",
            "source_ref": "session:some-other-session:agent:agent-other",
            "count": 1,
            "tool_summary": "1=Bash",
            "details": [],
            "rejections": "",
            "created": "2026-09-15T00:00:00+00:00",
        }
        other_path = ring / "other.json"
        other_path.write_text(json.dumps(other) + "\n", encoding="utf-8")
        msg = _extract_ctx(self._run("zmem-reflect.sh", {})).get("additionalContext", "")
        self.assertNotIn("agent-other", msg, msg)
        self.assertTrue(other_path.exists(), "other session sidecar must survive")

    def test_stop_treats_missing_created_as_pending(self):
        # PRR-005 + PRR-010: a sidecar with no `created` field is treated as
        # pending (age falls back to file mtime, which is fresh here), not
        # silently pruned.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        bare = {
            "session": "hooktest",
            "agent_id": "agent-bare",
            "agent_type": "explorer",
            "source_ref": "session:hooktest:agent:agent-bare",
            "count": 1,
            "tool_summary": "1=Bash",
            "details": [],
            "rejections": "",
        }
        bare_path = ring / "bare.json"
        bare_path.write_text(json.dumps(bare) + "\n", encoding="utf-8")
        msg = _extract_ctx(self._run("zmem-reflect.sh", {})).get("additionalContext", "")
        self.assertIn("agent-bare", msg, msg)
        self.assertFalse(bare_path.exists(), "pending sidecar must be consumed")

    def test_stop_append_branch_with_parent_failures(self):
        # PRR-005: pending sidecars AND genuine parent failures coexist — the
        # failure prompt renders AND the subagent section is appended AND the
        # sidecars are consumed.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "hooktest",
            "agent_id": "agent-both",
            "agent_type": "explorer",
            "source_ref": "session:hooktest:agent:agent-both",
            "count": 1,
            "tool_summary": "1=Read",
            "details": [],
            "rejections": "",
            "created": "2026-09-15T00:00:00+00:00",
        }
        both_path = ring / "both.json"
        both_path.write_text(json.dumps(sidecar) + "\n", encoding="utf-8")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            msg = _extract_ctx(self._run(
                "zmem-reflect.sh",
                {"ZMEM_TRANSCRIPT": os.path.abspath(trans)})).get("additionalContext", "")
            self.assertIn("1 failed tool call(s)", msg, msg)   # parent failures
            self.assertIn("agent-both", msg, msg)              # appended section
            self.assertFalse(both_path.exists(), "sidecar must be consumed")
        finally:
            os.remove(trans)


class TestSubagentReflectMessaging(unittest.TestCase):
    """Issue #204 contract: the SubagentStop hook NEVER emits additionalContext
    (a prompt into a finishing subagent replaces its <result> deliverable).
    Its failure/rejection signal is handed to the parent via a sidecar under
    <ZMEM_DATA>/subagent-reflections/, consumed by zmem-reflect.sh at the
    parent's own Stop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _env(self, env_extra):
        env = {
            "ZMEM_DATA": self.tmp,
            "ZMEM_SESSION": "hooktest",
            "ZMEM_AGENT_ID": "agent-1",
            "ZMEM_AGENT_TYPE": "explorer",
            "ZMEM_NAMESPACE": "project:hooktest",
        }
        env.update(env_extra)
        return env

    def _run(self, env_extra, stdin="{}"):
        return _extract_ctx(_run_hook(
            REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh",
            self._env(env_extra), stdin=stdin))

    def _run_checked(self, env_extra, stdin="{}"):
        """PRR-004: run the hook and return (rc, raw, ctx) so callers can pin
        rc==0 and the sentinel — a crashed hook must not masquerade as a
        clean no-op."""
        proc_env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_ZCODE_DB", "ZMEM_TRANSCRIPT",
                    "ZMEM_AGENT_TRANSCRIPT", "ZMEM_AGENT_ID",
                    "ZMEM_FAILURES_DB_TIMEOUT_S"):
            proc_env.pop(key, None)
        proc_env.update(self._env(env_extra))
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh")],
            input=stdin, text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=proc_env, timeout=60)
        return proc.returncode, proc.stdout, _extract_ctx(proc.stdout)

    def _sidecars(self):
        ring = Path(self.tmp) / "subagent-reflections"
        if not ring.is_dir():
            return []
        out = []
        for p in sorted(ring.glob("*.json")):
            try:
                out.append((p, json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                out.append((p, None))
        return out

    def test_rejection_only_writes_sidecar_and_no_prompt(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Edit", "leave the schema alone"))
        try:
            rc, raw, ctx = self._run_checked(
                {"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            # PRR-004: a crash must not read as a clean no-op.
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(ctx, {}, ctx)  # never prompt a finishing subagent
            cars = self._sidecars()
            self.assertEqual(len(cars), 1, cars)
            _, obj = cars[0]
            self.assertEqual(obj.get("session"), "hooktest")
            self.assertEqual(obj.get("agent_id"), "agent-1")
            self.assertEqual(obj.get("source_ref"), "session:hooktest:agent:agent-1")
            self.assertEqual(obj.get("count"), 0)
            self.assertIn("leave the schema alone", obj.get("rejections", ""))
        finally:
            os.remove(trans)

    def test_failures_plus_rejection_sidecar_fields(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "boom"),   # genuine failure
            *_rejection("t2", "Read", "not that file"),  # rejection
        ])
        try:
            rc, raw, ctx = self._run_checked(
                {"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(ctx, {}, ctx)
            cars = self._sidecars()
            self.assertEqual(len(cars), 1, cars)
            _, obj = cars[0]
            self.assertEqual(obj.get("count"), 1)
            self.assertEqual(obj.get("agent_type"), "explorer")
            self.assertIn("1=Bash", obj.get("tool_summary", ""))
            self.assertTrue(obj.get("details"), obj)
            self.assertIn("not that file", obj.get("rejections", ""))
            self.assertTrue(obj.get("created"), obj)
        finally:
            os.remove(trans)

    def test_no_failures_no_rejections_is_noop(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "all good", is_error=False),
        ])
        try:
            rc, raw, ctx = self._run_checked(
                {"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(ctx, {}, ctx)
            self.assertEqual(self._sidecars(), [])  # clean subagent: no hand-off
        finally:
            os.remove(trans)

    def test_stop_hook_active_loop_guard(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript(_rejection("t1", "Edit", "stop"))
        try:
            rc, raw, ctx = self._run_checked(
                {"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)},
                stdin='{"stop_hook_active": true}')
            self.assertEqual(rc, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", raw, raw)
            self.assertEqual(ctx, {}, raw)
            self.assertEqual(self._sidecars(), [])  # re-fire writes nothing
        finally:
            os.remove(trans)

    def test_kill_switch_emits_empty_and_no_sidecar(self):
        # #204 parity with zmem-reflect.sh (#194): ZMEM_REFLECT=0 disables the
        # hook entirely — empty envelope, exit 0, no sidecar.
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            env = self._env({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans),
                             "ZMEM_REFLECT": "0"})
            proc_env = dict(os.environ)
            for key in ("ZMEM_REFLECT", "ZMEM_ZCODE_DB", "ZMEM_TRANSCRIPT",
                        "ZMEM_AGENT_TRANSCRIPT", "ZMEM_AGENT_ID",
                        "ZMEM_FAILURES_DB_TIMEOUT_S"):
                proc_env.pop(key, None)
            proc_env.update(env)
            proc = subprocess.run(
                [_BASH, str(REPO_ROOT / "hooks" / "zmem-subagent-reflect.sh")],
                input="{}", text=True, capture_output=True,
                encoding="utf-8", errors="replace", env=proc_env, timeout=60)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("<<<ZMEM_JSON>>>{}<<<END>>>", proc.stdout, proc.stdout)
            self.assertEqual(self._sidecars(), [])
        finally:
            os.remove(trans)

    def test_sidecar_filename_deterministic_across_fires(self):
        if not _BASH:
            self.skipTest("no bash")
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            self._run({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            self._run({"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            cars = self._sidecars()
            self.assertEqual(len(cars), 1, cars)  # one file, overwritten
        finally:
            os.remove(trans)

    def test_no_agent_id_siblings_get_distinct_sidecars(self):
        # PRR-002: when the host sends no agent_id, the sidecar key falls back
        # to the unique transcript basename — siblings in one session must not
        # overwrite each other's hand-offs.
        if not _BASH:
            self.skipTest("no bash")
        tx_a = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1 from agent A"),
        ])
        tx_b = _write_transcript([
            _tool_use("t2", "Read"),
            _tool_result("t2", "different failure from agent B"),
        ])
        try:
            # ZMEM_AGENT_ID explicitly blanked: the host sent no agent id, so
            # the sidecar key must fall back to the transcript basename.
            self._run({"ZMEM_AGENT_ID": "",
                       "ZMEM_AGENT_TRANSCRIPT": os.path.abspath(tx_a)})
            self._run({"ZMEM_AGENT_ID": "",
                       "ZMEM_AGENT_TRANSCRIPT": os.path.abspath(tx_b)})
            cars = self._sidecars()
            self.assertEqual(len(cars), 2, cars)
            refs = sorted(obj.get("source_ref", "") for _, obj in cars)
            self.assertEqual(refs, ["session:hooktest", "session:hooktest"])
            blobs = " ".join(json.dumps(obj) for _, obj in cars)
            self.assertIn("agent A", blobs)
            self.assertIn("agent B", blobs)
        finally:
            os.remove(tx_a)
            os.remove(tx_b)

    def test_write_path_sweep_prunes_backdated_tmp_orphan(self):
        # PRR-011 (final-critic round 1): the write-path retention sweep must
        # prune dot-prefixed .sidecar-*.tmp orphans — glob "*" never matches
        # dotfiles, so the sweep enumerates via os.listdir. A backdated orphan
        # must be gone after the next sidecar write.
        if not _BASH:
            self.skipTest("no bash")
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        orphan = ring / ".sidecar-interrupted.tmp"
        orphan.write_text("{\"truncated\":", encoding="utf-8")
        stale = time.time() - 20 * 86400
        os.utime(orphan, (stale, stale))
        trans = _write_transcript([
            _tool_use("t1", "Bash"),
            _tool_result("t1", "Exit code 1"),
        ])
        try:
            rc, raw, ctx = self._run_checked(
                {"ZMEM_AGENT_TRANSCRIPT": os.path.abspath(trans)})
            self.assertEqual(rc, 0)
            self.assertEqual(ctx, {}, ctx)
            self.assertFalse(orphan.exists(),
                             "backdated .tmp orphan must be swept on write")
        finally:
            os.remove(trans)


class ReflectCompatibilityLaneTest(unittest.TestCase):
    def test_compatibility_request_has_explicit_lane(self):
        """Issue #122 rework: the reflect hook is a thin adapter over ONE
        query-aware selector call. The remote (compatibility) request is
        built by ``_prefetch`` — mcp_client.py ``call prefetch`` carrying
        the DERIVED namespace, ``user_prompt``/``hermes-compat``, the user
        message as the query, and one ``--ops-token`` per ring token."""
        import importlib.util

        path = REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py"
        spec = importlib.util.spec_from_file_location("zmem_hermes_reflect_test", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        saved = {key: os.environ.get(key) for key in
                 ("ZMEM_MCP_URL", "ZMEM_MCP_NAMESPACE")}
        try:
            os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:8765/mcp"
            os.environ["ZMEM_MCP_NAMESPACE"] = "project:compat"
            # A complete closed selector envelope (the #122 14-key set): a
            # response missing any key is a failed prefetch, so the mock must
            # carry all of them for the call to count as delivered.
            envelope = {
                "results": [], "count": 0, "omitted": 0, "reason": "ok",
                "excluded": [], "candidate_ids": [], "tokens_used": 0,
                "tokens_budget": 1500, "budget_dropped": 0,
                "budget_admission": 0, "budget_truncated": 0,
                "budget_dropped_protected": 0, "arms": {}, "rendered": "",
            }
            completed = mock.Mock(returncode=0,
                                  stdout=json.dumps(envelope) + "\n",
                                  stderr="")
            namespace = mod._resolve_hook_namespace()
            self.assertEqual(namespace, "project:compat")
            with mock.patch.object(mod.subprocess, "run", return_value=completed) as run:
                got = mod._prefetch("compat query", namespace, "sess42",
                                    ["ring-token-a"], (0.0, 0))
            self.assertEqual(got, envelope)
            argv = run.call_args.args[0]
            self.assertTrue(argv[1].endswith("mcp_client.py"), argv[:2])
            self.assertIn("prefetch", argv)
            self.assertIn("--lane", argv)
            self.assertEqual(argv[argv.index("--lane") + 1], "hermes-compat")
            self.assertIn("--namespace", argv)
            self.assertEqual(argv[argv.index("--namespace") + 1], "project:compat")
            self.assertIn("--moment", argv)
            self.assertEqual(argv[argv.index("--moment") + 1], "user_prompt")
            self.assertIn("--query", argv)
            self.assertEqual(argv[argv.index("--query") + 1], "compat query")
            self.assertIn("--session-id", argv)
            self.assertEqual(argv[argv.index("--session-id") + 1], "sess42")
            self.assertIn("--ops-token", argv)
            self.assertEqual(argv[argv.index("--ops-token") + 1], "ring-token-a")
            # Remote mode must NOT carry the local-mode markers.
            self.assertNotIn("--for-injection", argv)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
