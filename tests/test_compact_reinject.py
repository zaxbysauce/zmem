"""Session restart behavior after compaction (issue #158).

Proves through the REAL launcher chain that the retired #118 compact sidecar
is no longer part of the lifecycle:
- PreCompact clears the delivery ledger through the store CLI;
- PostCompact remains a registered, fail-open compatibility hook but emits no
  context and does not write a compact sidecar;
- SessionStart with source == "compact" uses the ordinary ``session_start``
  selector and recent-memory lane, retaining only the local
  ``session_start_compact`` decision-log label.

All stores are throwaway temp stores. Runs standalone:
python tests/test_compact_reinject.py
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

SUMMARY_MARKER = "zmem118summarymarker"
LEDGER_MARKER = "zmem118ledgemarker"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_SESSION",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_SESSION_SOURCE",
)


def _clean_env(tmp: str, host: str = "claude", **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
        # Launcher host detection: the claude lane (full compaction surface).
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    })
    if host == "codex":
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env["PLUGIN_ROOT"] = str(REPO_ROOT)
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


def _ctx_from_payload(stdout: str) -> str:
    """Issue #121: the payload module now emits its own sentinel-wrapped
    envelopes (two on the normal path). Extract the additionalContext of
    the LAST complete <<<ZMEM_JSON>>>...<<<END>>> pair — exactly what the
    launcher's extractPayload consumes — so a direct payload drive parses
    the same envelope the host receives."""
    text = (stdout or "")
    end = text.rfind("<<<END>>>")
    start = text.rfind("<<<ZMEM_JSON>>>", 0, end) if end >= 0 else -1
    if start < 0 or end <= start:
        return ""
    try:
        envelope = json.loads(
            text[start + len("<<<ZMEM_JSON>>>"):end].strip())
    except ValueError:
        return ""
    inner = envelope.get("hookSpecificOutput") or {}
    return inner.get("additionalContext") or envelope.get("additionalContext") or ""


def _decisions(tmp: str) -> list:
    log = Path(tmp) / "zmem-decisions.log"
    if not log.exists():
        return []
    return [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if "zmem-hook" in l]


def _seed_divergent_store(env: dict, ns: str) -> None:
    """Byte-identical to the frozen C1 fixture (repro/C1.sh): two marker
    rows FIRST, three lexically-diverse newer rows after a 1.5s gap (recent
    orders by ingestion_ts DESC with second granularity and NO tiebreaker;
    add dedups semantically at 0.85 cosine — the newer rows must be both
    newer AND distinct or the markers leak into the recency pull and the
    cold-start control goes vacuous). Do NOT reword these rows: the store's
    selective gate sits near the relevance boundary for the composed
    compact query, and cosmetic wording changes flip it."""
    _seed(env, ns,
          "postgresql migration lock retry backoff zmem118summarymarker")
    _seed(env, ns,
          "docker network prune leaves orphan bridges zmem118ledgemarker")
    time.sleep(1.5)
    _seed(env, ns,
          "build cache eviction policy scans the ubuntu runner images "
          "nightly for stale layers zmem118newerb")
    _seed(env, ns,
          "editorial review prefers tables over bullet spam for short "
          "enumerable facts zmem118newerc")
    _seed(env, ns,
          "winter tires for the fleet vans get swapped in november before "
          "the mountain routes freeze zmem118newerd")


class CompactSidecarUnitTest(unittest.TestCase):
    """Backward-compatible delivery_ledger compact-sidecar API tests."""

    def setUp(self):
        import storelib.delivery_ledger as dl
        self.dl = dl
        self._tmp = tempfile.mkdtemp(prefix="zmem118-unit-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_snapshot_then_merge_then_consume(self):
        rows = [{"id": "r1", "content": "alpha " + LEDGER_MARKER}]
        self.dl.record(self._tmp, "s1", rows, "user_prompt")
        self.dl.snapshot_for_compact(self._tmp, "s1")
        stash = json.loads(_ops_path(self._tmp, "s1", ".compact")
                           .read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in stash["entries"]], ["r1"])
        self.assertIsNone(stash["summary"])
        self.dl.park_compact_summary(self._tmp, "s1", "S" * 3000)
        summary, entries = self.dl.consume_compact_context(self._tmp, "s1")
        self.assertEqual(len(summary), self.dl.COMPACT_SUMMARY_MAX)
        self.assertEqual([e["id"] for e in entries], ["r1"])
        summary2, entries2 = self.dl.consume_compact_context(self._tmp, "s1")
        self.assertIsNone(summary2)
        self.assertEqual(entries2, [])

    def test_park_creates_stash_when_precompact_never_ran(self):
        self.dl.park_compact_summary(self._tmp, "s2", "summary only")
        summary, entries = self.dl.consume_compact_context(self._tmp, "s2")
        self.assertEqual(summary, "summary only")
        self.assertEqual(entries, [])

    def test_second_compaction_snapshot_supersedes(self):
        self.dl.record(self._tmp, "s3", [{"id": "old", "content": "x"}],
                       "user_prompt")
        self.dl.snapshot_for_compact(self._tmp, "s3")
        self.dl.park_compact_summary(self._tmp, "s3", "stale summary")
        self.dl.record(self._tmp, "s3", [{"id": "new", "content": "y"}],
                       "user_prompt")
        self.dl.snapshot_for_compact(self._tmp, "s3")
        summary, entries = self.dl.consume_compact_context(self._tmp, "s3")
        self.assertIsNone(summary)
        self.assertEqual([e["id"] for e in entries], ["old", "new"])

    def test_clear_delivery_state_keeps_compact_stash(self):
        self.dl.record(self._tmp, "s4", [{"id": "k", "content": "z"}],
                       "user_prompt")
        self.dl.snapshot_for_compact(self._tmp, "s4")
        self.dl.clear_delivery_state(self._tmp, "s4")
        self.assertFalse(_ops_path(self._tmp, "s4", ".ledger").exists())
        self.assertTrue(_ops_path(self._tmp, "s4", ".compact").exists())

    def test_sweep_reaps_stale_compact_stash(self):
        stash = _ops_path(self._tmp, "s5", ".compact")
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


class CompactSequenceTest(unittest.TestCase):
    """The full hook sequence through the launcher (behavioral)."""

    SID = "compact118-seq"

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem118-seq-")
        self._workdir = os.path.join(self._tmp, "workdir")
        os.makedirs(self._workdir, exist_ok=True)
        self._env = _clean_env(self._tmp)
        # Seed into the namespace the LAUNCHER resolves for the workdir cwd
        # (buildCanonicalEnv overwrites any ambient ZMEM_NAMESPACE for
        # session-start/recall — pinning a different key would seed a tier
        # the hook never queries and the cold-start control goes vacuous).
        import host as host_mod
        self.NS = host_mod.resolve_namespace(self._workdir)
        _seed_divergent_store(self._env, self.NS)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _drive(self, sub, payload, env=None):
        return _drive(env or self._env, self._workdir, sub, payload)

    def _recall_ledger_marker(self, env=None):
        p = self._drive("recall", {
            "hook_event_name": "UserPromptSubmit",
            "prompt": LEDGER_MARKER + " docker network prune",
            "session_id": self.SID, "cwd": self._workdir}, env=env)
        self.assertEqual(p.returncode, 0)

    def test_full_sequence_uses_normal_selector_after_compaction(self):
        # Realistic sequence (mirrors the frozen C1 check): a session ALWAYS
        # has a session-start before its first compaction. PreCompact clears
        # only the delivery ledger; the following SessionStart is the normal
        # recent-memory lane and has no compact summary/query composition.
        # Keep the lexical floor disabled so this test remains about the
        # lifecycle boundary rather than model availability.
        env = dict(self._env, ZMEM_INJECT_FLOOR_LEX="0")
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "startup",
            "session_id": self.SID, "cwd": self._workdir}, env=env)
        self.assertEqual(p.returncode, 0)
        p = self._drive("recall", {
            "hook_event_name": "UserPromptSubmit",
            "prompt": LEDGER_MARKER + " docker network prune",
            "session_id": self.SID, "cwd": self._workdir}, env=env)
        self.assertEqual(p.returncode, 0)
        p = self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"}, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".ledger").exists())
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        p = self._drive("postcompact", {
            "hook_event_name": "PostCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual",
            "compact_summary": "Compaction summary: the session was working "
                               "on " + SUMMARY_MARKER +
                               " postgresql migration lock handling."},
            env=env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "{}")
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir}, env=env)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        self.assertNotIn("Post-compaction memories", ctx)
        self.assertNotIn(SUMMARY_MARKER, ctx)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        tail = _decisions(self._tmp)[-1]
        self.assertRegex(
            tail,
            r"moment=session_start_compact lane=claude ver=\d+\.\d+\.\d+ "
            r"t_ms=\d+(?: |$)")

    def test_cold_start_path_unchanged(self):
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "startup",
            "session_id": "cold118", "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertNotIn(SUMMARY_MARKER, ctx)
        self.assertNotIn(LEDGER_MARKER, ctx)
        self.assertIn("Recent memories", ctx)
        tail = _decisions(self._tmp)[-1]
        self.assertRegex(
            tail,
            r"moment=session_start lane=claude ver=\d+\.\d+\.\d+ "
            r"t_ms=\d+(?: |$)")
        self.assertNotIn("session_start_compact", tail)
        # No-source payload (ZCode-style / manual invocation) also cold.
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart",
            "session_id": "cold118b", "cwd": self._workdir})
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        tail = _decisions(self._tmp)[-1]
        self.assertRegex(
            tail,
            r"moment=session_start lane=claude ver=\d+\.\d+\.\d+ "
            r"t_ms=\d+(?: |$)")

    def test_codex_compact_source_uses_normal_selector(self):
        """Codex has no PostCompact; its compact restart also uses recent."""
        env = dict(_clean_env(self._tmp, host="codex"),
                   ZMEM_INJECT_FLOOR_LEX="0")
        self._recall_ledger_marker(env=env)
        p = _drive(env, self._workdir, "precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        self.assertEqual(p.returncode, 0)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        p = _drive(env, self._workdir, "session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        self.assertNotIn("Post-compaction memories", ctx)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        # Issue #153 (ported onto the #158 selector moment): the compact
        # restart decision line is attributed to the codex host lane.
        tail = _decisions(self._tmp)[-1]
        self.assertRegex(
            tail,
            r"moment=session_start_compact lane=codex ver=\d+\.\d+\.\d+ "
            r"t_ms=\d+(?: |$)")

    def test_compact_source_without_prior_events_uses_cold_lane(self):
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": "nostash118", "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        tail = _decisions(self._tmp)[-1]
        self.assertRegex(
            tail,
            r"moment=session_start_compact lane=claude ver=\d+\.\d+\.\d+ "
            r"t_ms=\d+(?: |$)")

    def test_default_floor_realistic_query_injects(self):
        # The compact source follows the ordinary recent-memory lane. Keep a
        # default-floor check here so the compact restart can never become a
        # silent special case after the sidecar retirement.
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "startup",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        self._recall_ledger_marker()
        p = self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        self.assertEqual(p.returncode, 0)
        p = self._drive("postcompact", {
            "hook_event_name": "PostCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual",
            "compact_summary": (
                "Compaction summary of the session so far: we were deep in "
                "the merge queue work and the ratchet flake kept re-appearing "
                "after every rebase; the quarantine guidance was consulted, "
                "the docker network prune issue on the CI runner came up in "
                "passing, and we agreed to revisit the postgresql migration "
                "lock retry backoff tomorrow. Several other threads (build "
                "cache eviction policy, editorial review formatting, winter "
                "tire swaps for the fleet vans, greenhouse tomato staking "
                "before the august storms, archival paper stock buffering) "
                "were touched briefly but not resolved.")})
        self.assertEqual(p.returncode, 0)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        self.assertNotIn("Post-compaction memories", ctx)

    def test_recall_failure_has_no_compact_stash_to_preserve(self):
        # The retired sidecar means a broken post-compaction store cannot
        # strand a summary or snapshot for a later SessionStart.
        self._recall_ledger_marker()
        self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        stash = _ops_path(self._tmp, self.SID, ".compact")
        self.assertFalse(stash.exists())
        # Corrupt the store so every recall subprocess fails fast.
        (Path(self._tmp) / "store.sqlite").write_bytes(b"not a database")
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        self.assertFalse(stash.exists())

    def test_query_context_kill_switch_takes_cold_lane(self):
        # The compact lane is now the ordinary recency lane; the retired
        # sidecar is never created even when query context is disabled.
        self._recall_ledger_marker()
        self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        p = self._drive("postcompact", {
            "hook_event_name": "PostCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual",
            "compact_summary": "post-kill-switch summary text for the lane"})
        self.assertEqual(p.returncode, 0)
        env_qc = dict(self._env, ZMEM_QUERY_CONTEXT="0")
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir}, env=env_qc)
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        self.assertNotIn("Post-compaction memories", ctx)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())

    def test_source_resume_takes_cold_lane_without_stash(self):
        # source=resume remains a normal session-start lane and does not
        # touch the retired compact sidecar.
        self._recall_ledger_marker()
        self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        self._drive("postcompact", {
            "hook_event_name": "PostCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual",
            "compact_summary": "resume-lane summary text"})
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "resume",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        self.assertNotIn("Post-compaction memories", ctx)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())

    def test_stale_compact_json_is_ignored(self):
        # A stale file from a pre-#158 install is not consumed by the normal
        # SessionStart selector.
        stash = _ops_path(self._tmp, "malformed118", ".compact")
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_text("{not json at all", encoding="utf-8")
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": "malformed118", "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        self.assertIn("Recent memories", _ctx(p.stdout))
        self.assertTrue(stash.exists())

    def test_compact_source_uses_normal_selector_moment(self):
        # The payload still receives source=compact for host diagnostics, but
        # the store subprocess must receive the canonical session_start
        # moment. This direct drive keeps the assertion at the adapter seam.
        argv = [sys.executable,
                str(REPO_ROOT / "hooks" / "lib"
                    / "zmem-session-start-payload.py"),
                "", "", str(SCRIPTS / "store.py"),
                self._tmp, str(REPO_ROOT), self._tmp, str(self.NS),
                "25000", "claude", "", "", self.SID, "", "compact"]
        p = subprocess.run(argv, input="{}", capture_output=True, text=True,
                           env=self._env, timeout=180)
        self.assertEqual(p.returncode, 0)
        tail = _decisions(self._tmp)[-1]
        self.assertIn("moment=session_start_compact", tail)
        self.assertIn("Recent memories", _ctx_from_payload(p.stdout))
        self.assertNotIn("Post-compaction memories", _ctx_from_payload(p.stdout))

    def test_kill_switch_silent_and_no_stash_is_created(self):
        self._recall_ledger_marker()
        self._drive("precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        env0 = dict(self._env)
        env0["ZMEM_INJECT"] = "0"
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir}, env=env0)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(_ctx(p.stdout), "")
        tail = _decisions(self._tmp)[-1]
        self.assertIn("reason=disabled", tail)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())


class RegistrationNeedleTest(unittest.TestCase):
    """Source-text pins for the registration + sweep + doc surfaces."""

    def test_claude_registers_postcompact(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.claude.json").read_text("utf-8"))
        self.assertIn("PostCompact", hooks["hooks"])
        cmd = hooks["hooks"]["PostCompact"][0]["hooks"][0]["command"]
        self.assertIn("postcompact", cmd)

    def test_codex_does_not_register_postcompact(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_text("utf-8"))
        self.assertNotIn("PostCompact", hooks["hooks"])

    def test_zcode_does_not_register_postcompact(self):
        hooks = json.loads(
            (REPO_ROOT / "hooks" / "hooks.zcode.json").read_text("utf-8"))
        self.assertNotIn("PostCompact", hooks["hooks"])

    def test_postcompact_script_exists(self):
        self.assertTrue((REPO_ROOT / "hooks" / "zmem-postcompact.sh").is_file())

    def test_launcher_exports_session_source(self):
        text = (REPO_ROOT / "hooks" / "zmem-launch.js").read_text("utf-8")
        self.assertIn("ZMEM_SESSION_SOURCE", text)

    def test_skill_carries_dated_compact_entry(self):
        import re
        text = (REPO_ROOT / "skills" / "memory" / "SKILL.md").read_text("utf-8")
        # Dated (>= 2026-09-10) #118 entry naming PostCompact + compact,
        # including the open PreCompact-survival question owned by #96.
        pat = r"2026-09-1[0-9].*?#118.*?PostCompact.*?compact.*?#96"
        self.assertIsNotNone(
            re.search(pat, text, re.DOTALL),
            "SKILL.md must carry the dated #118 compact entry with the "
            "open #96 survival question")

    def test_session_start_branches_on_source(self):
        text = (REPO_ROOT / "hooks" / "zmem-session-start.sh").read_text("utf-8")
        self.assertIn("ZMEM_SESSION_SOURCE", text)
        self.assertIn("canonical `session_start` selector", text)
        self.assertIn("no compact snapshot/summary sidecar", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
