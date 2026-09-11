"""Query-aware re-injection after compaction (issue #118, Workstream D-2).

Proves the three scope items end to end through the REAL launcher chain:
- PreCompact snapshots the delivery ledger into the compact sidecar
  (<data>/ops/<sha256(sid)[:32]>.compact) BEFORE clearing it;
- PostCompact stashes compact_summary into the same sidecar (Claude-only
  registration; the handler emits no context);
- SessionStart with source == "compact" composes a query from the stashed
  summary PLUS the pre-compaction ledger snapshot and runs the query-aware
  recall lane (moment=session_start_compact), while every other source —
  and an empty stash — keeps the byte-identical cold-start recency lane.

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
    """delivery_ledger compact-sidecar API (pure unit)."""

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
        # PostCompact merge keeps the entries and sets the bounded summary.
        self.dl.park_compact_summary(self._tmp, "s1", "S" * 3000)
        summary, entries = self.dl.consume_compact_context(self._tmp, "s1")
        self.assertEqual(len(summary), self.dl.COMPACT_SUMMARY_MAX)
        self.assertEqual([e["id"] for e in entries], ["r1"])
        # Consumed exactly once: a second read is the empty shape.
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
        # Second compaction: a fresh PreCompact wipes the stale summary.
        self.dl.record(self._tmp, "s3", [{"id": "new", "content": "y"}],
                       "user_prompt")
        self.dl.snapshot_for_compact(self._tmp, "s3")
        summary, entries = self.dl.consume_compact_context(self._tmp, "s3")
        self.assertIsNone(summary)
        self.assertEqual([e["id"] for e in entries], ["old", "new"])

    def test_clear_delivery_state_keeps_compact_stash(self):
        # The PreCompact clear must NOT remove the snapshot it just wrote.
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

    def test_full_sequence_injects_from_summary_and_ledger(self):
        # Realistic sequence (mirrors the frozen C1 check): a session ALWAYS
        # has a session-start before its first compaction — the cold start
        # records its recency rows into the ledger, so the PreCompact
        # snapshot carries the session working set, not a single row. (With
        # a one-entry snapshot the query is dominated by that row verbatim
        # and the store-side selective gate legitimately trims the weaker
        # summary match — same behavior a user prompt with that text gets.)
        # ZMEM_INJECT_FLOOR_LEX=0 isolates this test from the #113 lexical
        # floor calibration: the subject here is the compact-branch
        # mechanics (composition, stash consumption, moment/header), not
        # the gate threshold — which the frozen C1 check exercises at the
        # DEFAULT floors. Without it, a bare-interpreter CI leg (no
        # embedding model → no _rel_cos lane) sits the composed query at
        # the lexical boundary and flakes by ±wiring.
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
        self.assertTrue(_ops_path(self._tmp, self.SID, ".compact").exists())
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
        self.assertIn(SUMMARY_MARKER, ctx)
        self.assertIn(LEDGER_MARKER, ctx)
        self.assertIn("Post-compaction memories", ctx)
        self.assertNotIn("Recent memories", ctx)
        self.assertFalse(_ops_path(self._tmp, self.SID, ".compact").exists())
        tail = _decisions(self._tmp)[-1]
        self.assertIn("moment=session_start_compact", tail)

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
        self.assertIn("moment=session_start\n", tail + "\n")
        self.assertNotIn("session_start_compact", tail)
        # No-source payload (ZCode-style / manual invocation) also cold.
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart",
            "session_id": "cold118b", "cwd": self._workdir})
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)

    def test_codex_compact_branch_composes_from_snapshot_only(self):
        """Codex has no PostCompact (no compact_summary upstream) — the
        snapshot alone must still compose the query (the amendment's
        Codex shape). Floor calibration isolated as in the full-sequence
        test above."""
        env = dict(_clean_env(self._tmp, host="codex"),
                   ZMEM_INJECT_FLOOR_LEX="0")
        self._recall_ledger_marker(env=env)
        p = _drive(env, self._workdir, "precompact", {
            "hook_event_name": "PreCompact", "session_id": self.SID,
            "cwd": self._workdir, "trigger": "manual"})
        self.assertEqual(p.returncode, 0)
        p = _drive(env, self._workdir, "session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": self.SID, "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn(LEDGER_MARKER, ctx)
        self.assertIn("Post-compaction memories", ctx)

    def test_empty_stash_degrades_to_cold_lane(self):
        p = self._drive("session-start", {
            "hook_event_name": "SessionStart", "source": "compact",
            "session_id": "nostash118", "cwd": self._workdir})
        self.assertEqual(p.returncode, 0)
        ctx = _ctx(p.stdout)
        self.assertIn("Recent memories", ctx)
        tail = _decisions(self._tmp)[-1]
        self.assertIn("moment=session_start\n", tail + "\n")

    def test_kill_switch_silent_and_stash_survives(self):
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
        # The stash was not consumed by the disabled run — it survives for
        # the next enabled session-start.
        self.assertTrue(_ops_path(self._tmp, self.SID, ".compact").exists())


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
        self.assertIn('source == "compact"', text)
        self.assertIn("ZMEM_SESSION_SOURCE", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
