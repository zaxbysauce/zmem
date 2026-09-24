"""Issue #117 — session delivery ledger: unit + behavioral coverage.

Surfaces pinned here:
- storelib.delivery_ledger: hashed collision-free naming, record/prune/cap,
  atomic write (no tmp residue), strong_token_match escalation rule,
  fallback pending sidecar append-with-dedup, clear_delivery_state.
- store CLI: --exclude on recall/recent/search filters pre-gate and reports
  the envelope ``excluded`` count (candidate_ids stays PRE-exclude).
- hook body: pending sidecars are retired, session_end clears delivery state
  (even under the kill switch), and the decision line gains the additive
  exc= field.
- escalation input contract: derive_ops_tokens("git stash pop") yields the
  git/stash/pop token shape the PreToolUse escalation depends on.

All stores are throwaway temp dirs; ambient zmem env (including the ledger
knobs ZMEM_DELIVER_WINDOW_S / ZMEM_LEDGER_CAP / ZMEM_PENDING_SIDECAR) is
stripped from every child and cranked hermetically in-process.
Runs standalone: python tests/test_delivery_ledger.py
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

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
sys.path.insert(0, str(SCRIPTS))

STASH_TEXT = ("Never git stash pop without checking the reflog first; the "
              "drop is unrecoverable and the stash entry vanishes.")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_SESSION",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
    # issue #117 knobs — a leaked ambient value would flip behavior here.
    "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP", "ZMEM_PENDING_SIDECAR",
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


def _seed(tmp: str, ns: str, content: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "store.py"), "add",
         "--namespace", ns, "--type", "lesson", "--content", content,
         "--signal", "test"],
        capture_output=True, text=True, env=_clean_env(tmp), timeout=120)
    assert r.returncode == 0, r.stderr


def _run_body(tmp: str, event: dict, ns: str, mode: str,
              **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
         ns, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True,
        env=_clean_env(tmp, **extra_env), timeout=120)


def _ops_files(tmp: str) -> list:
    d = Path(tmp, "ops")
    return sorted(d.iterdir()) if d.is_dir() else []


class LedgerModuleTest(unittest.TestCase):
    """storelib.delivery_ledger unit contract."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-ledger-")
        import storelib.delivery_ledger as dl
        self.dl = dl

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hashed_names_collision_free_for_long_ids(self):
        a = "a" * 128 + "X1"
        b = "a" * 128 + "X2"
        self.assertNotEqual(a, b)
        pa = self.dl.ledger_path(self.tmp, a)
        pb = self.dl.ledger_path(self.tmp, b)
        self.assertIsNotNone(pa)
        self.assertNotEqual(pa, pb)
        # the old sanitize-and-truncate key collapsed these two
        import re
        self.assertEqual(
            re.sub(r"[^A-Za-z0-9._-]", "_", a)[:128],
            re.sub(r"[^A-Za-z0-9._-]", "_", b)[:128])
        # same id resolves stably; no session id -> None
        self.assertEqual(self.dl.ledger_path(self.tmp, a), pa)
        self.assertIsNone(self.dl.ledger_path(self.tmp, ""))

    # --- issue #122: hashed ops sidecars + atomic ring rotation ---

    def test_ops_full_session_hash_separates_sanitized_prefixes(self):
        # Two 130-char ids whose SANITIZED forms share their first 128
        # characters must land on DISTINCT ring and delivered-cursor paths
        # now that ops_tokens hashes the complete session id.
        import hashlib
        import storelib.ops_tokens as ops
        base = "s" + "x" * 127  # 128 shared sanitized chars
        sid_a = base + "a1"
        sid_b = base + "b2"
        self.assertEqual(len(sid_a), 130, len(sid_a))
        import re
        self.assertEqual(
            re.sub(r"[^A-Za-z0-9._-]", "_", sid_a)[:128],
            re.sub(r"[^A-Za-z0-9._-]", "_", sid_b)[:128])
        log_a = ops._ring_path(self.tmp, sid_a)
        log_b = ops._ring_path(self.tmp, sid_b)
        delivered_a = ops._marker_path(self.tmp, sid_a, ".delivered")
        delivered_b = ops._marker_path(self.tmp, sid_b, ".delivered")
        self.assertNotEqual(log_a, log_b)
        self.assertNotEqual(delivered_a, delivered_b)
        # the names are the sha256 stem of the COMPLETE id, truncated to 32
        expected_a = hashlib.sha256(sid_a.encode("utf-8")).hexdigest()[:32]
        self.assertTrue(log_a.replace("\\", "/").endswith(
            "/ops/" + expected_a + ".log"), log_a)
        self.assertTrue(delivered_a.replace("\\", "/").endswith(
            "/ops/" + expected_a + ".delivered"), delivered_a)

    def test_ring_trim_atomic_has_no_tmp(self):
        # Fill the ring past _RING_MAX_BYTES, append once more (rotation
        # fires), and assert the ops dir holds ONLY the expected hashed
        # .log/.delivered files — no *.tmp residue, still valid JSONL.
        import hashlib
        import json as _json
        import storelib.ops_tokens as ops
        ops_dir = Path(self.tmp) / "ops"
        ops_dir.mkdir(parents=True, exist_ok=True)
        sid = "trim-session-0001"
        ring = Path(ops._ring_path(self.tmp, sid))
        line = _json.dumps({"ts": 1700000000, "tool": "bash",
                            "ops": "git status"}) + "\n"
        with open(ring, "w", encoding="utf-8") as f:
            f.write(line * 1400)  # ~90KB, above _RING_MAX_BYTES (65536)
        # a delivered marker so the expected dir inventory is the pair
        ops.write_delivered_cursor(self.tmp, sid, (1700000001.0, 1401))
        self.assertTrue(ops.append_ops_ring(self.tmp, sid, "bash",
                                            "git stash pop"))
        names = sorted(p.name for p in ops_dir.iterdir())
        stem = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
        self.assertEqual(names, [stem + ".delivered", stem + ".log"])
        # the retained ring is still fully parseable JSONL
        with open(ring, "r", encoding="utf-8") as f:
            parsed = [_json.loads(l) for l in f if l.strip()]
        self.assertGreaterEqual(len(parsed), 1)
        self.assertEqual(parsed[-1]["ops"], "git stash pop")

    def test_record_delivered_roundtrip_and_upsert(self):
        row = {"id": "row-1", "content": STASH_TEXT}
        self.dl.record(self.tmp, "sess-a", [row], "user_prompt")
        ids = self.dl.delivered_ids(self.tmp, "sess-a")
        self.assertEqual(ids, ["row-1"])
        # upsert: a re-delivery refreshes rather than duplicates
        self.dl.record(self.tmp, "sess-a", [row], "pretool")
        self.assertEqual(self.dl.delivered_ids(self.tmp, "sess-a"), ["row-1"])
        # entry text captures the matcher fuel, lowercased
        entry = self.dl.delivered(self.tmp, "sess-a")[0]
        self.assertIn("git stash pop", entry["text"])

    def test_window_prune_and_cap(self):
        row = {"id": "row-1", "content": "alpha"}
        old = time.time() - 9999
        self.dl.record(self.tmp, "sess-a", [row], "user_prompt", now=old)
        self.assertEqual(self.dl.delivered_ids(self.tmp, "sess-a"),
                         ["row-1"])
        os.environ["ZMEM_DELIVER_WINDOW_S"] = "60"
        try:
            self.assertEqual(self.dl.delivered_ids(self.tmp, "sess-a"), [])
        finally:
            os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        # cap: oldest entries dropped beyond the cap
        os.environ["ZMEM_LEDGER_CAP"] = "3"
        try:
            for i in range(5):
                self.dl.record(self.tmp, "sess-cap",
                               [{"id": f"r{i}", "content": f"c{i}"}],
                               "user_prompt", now=time.time() + i)
            self.assertEqual(self.dl.delivered_ids(self.tmp, "sess-cap"),
                             ["r2", "r3", "r4"])
        finally:
            os.environ.pop("ZMEM_LEDGER_CAP", None)

    def test_atomic_write_leaves_no_tmp_residue(self):
        self.dl.record(self.tmp, "sess-a",
                       [{"id": "r", "content": "x"}], "user_prompt")
        residue = [f.name for f in _ops_files(self.tmp)
                   if ".tmp." in f.name]
        self.assertEqual(residue, [])

    def test_strong_token_match(self):
        f = self.dl.strong_token_match
        self.assertTrue(f("never git stash pop without reflog",
                          ["git", "stash", "pop"]))
        self.assertFalse(f("never git stash pop without reflog",
                           ["git", "stash", "pop", "rebase"]))
        self.assertFalse(f("anything", []))
        self.assertFalse(f("", ["git"]))
        # case-insensitive on both sides
        self.assertTrue(f("NEVER GIT STASH POP", ["git", "stash"]))

    def test_fallback_sidecar_append_with_dedup(self):
        fence1 = "<<<ZMEM_UNTRUSTED_FENCE>>> F1 <<<END_ZMEM_UNTRUSTED_FENCE>>>"
        fence2 = "<<<ZMEM_UNTRUSTED_FENCE>>> F2 <<<END_ZMEM_UNTRUSTED_FENCE>>>"
        self.dl.park_pending(self.tmp, "sess-a",
                             [{"id": "r1"}], fence1, "pretool")
        # a different id appends (N events all survive — the pre-#117 bug)
        self.dl.park_pending(self.tmp, "sess-a",
                             [{"id": "r2"}], fence2, "pretool")
        # the same id does not duplicate
        self.dl.park_pending(self.tmp, "sess-a",
                             [{"id": "r2"}], fence2, "pretool")
        ctx = self.dl.consume_pending(self.tmp, "sess-a")
        self.assertIn("F1", ctx)
        self.assertIn("F2", ctx)
        self.assertEqual(ctx.count("F2"), 1)
        # consumed clears
        self.assertEqual(self.dl.consume_pending(self.tmp, "sess-a"), "")

    def test_clear_delivery_state_clears_both(self):
        self.dl.record(self.tmp, "sess-a",
                       [{"id": "r", "content": "x"}], "user_prompt")
        self.dl.park_pending(self.tmp, "sess-a", [{"id": "r"}], "F", "p")
        self.dl.clear_delivery_state(self.tmp, "sess-a")
        self.assertEqual(self.dl.delivered_ids(self.tmp, "sess-a"), [])
        self.assertEqual(self.dl.consume_pending(self.tmp, "sess-a"), "")

    def test_park_multi_row_single_fence_stored_once(self):
        # Issue #151 review (COPILOT-2): a single pretool event selecting
        # N rows parks ONE fence — never N copies of the same fence.
        fence = "<<<ZMEM_UNTRUSTED_FENCE>>> F <<<END_ZMEM_UNTRUSTED_FENCE>>>"
        rows = [{"id": f"r{i}", "content": f"row {i}"} for i in range(3)]
        self.dl.park_pending(self.tmp, "sess-m", rows, fence, "pretool")
        ctx = self.dl.consume_pending(self.tmp, "sess-m")
        self.assertEqual(ctx.count(fence), 1,
                         "multi-row park must store the fence once")
        # the parked ids are all recorded (dedup preserved)
        self.dl.park_pending(self.tmp, "sess-m2", rows, fence, "pretool")
        self.dl.park_pending(self.tmp, "sess-m2", rows, fence, "pretool")
        self.assertEqual(self.dl.consume_pending(self.tmp, "sess-m2").count(fence), 1)

    def test_strong_token_match_boundaries(self):
        # Issue #151 review (CUBIC-ledger-287): tokens must match as whole
        # words, not substrings ("popular" does not satisfy "pop").
        f = self.dl.strong_token_match
        self.assertFalse(f("this popular command is fine", ["pop"]))
        self.assertTrue(f("git stash pop here", ["pop"]))
        # punctuation-adjacent tokens still match (path/flag shapes)
        self.assertTrue(f("never run rm -rf /tmp", ["-rf"]))
        self.assertTrue(f("about to git stash pop", ["git", "stash", "pop"]))

    def test_rows_present_in_filters_by_bullet(self):
        # Issue #151 review (CUBIC-body-1200): record-only-what-rendered.
        rows = [{"id": "r1", "content": "a"}, {"id": "r10", "content": "b"}]
        text = "- [r1] [conf=0.9] test\n    a"
        present = self.dl.rows_present_in(rows, text)
        self.assertEqual([r["id"] for r in present], ["r1"],
                         "bullet-form match must not confuse r1 with r10")

    def test_rows_present_in_accepts_renderer_marker_prefix(self):
        # Scoped and explicitly marked rows put only the renderer's known
        # provenance markers before the dash; arbitrary prose must not count.
        rows = [{"id": "r1", "content": "a"}, {"id": "r2", "content": "b"}]
        text = (
            " [PREVIOUSLY] [INJECTION RISK]- [tier=unknown] [r1] [conf=0.9]\n"
            "    a\n"
            " [arbitrary prose]- [tier=unknown] [r2] [conf=0.9]\n"
            "    b\n"
        )
        present = self.dl.rows_present_in(rows, text)
        self.assertEqual([r["id"] for r in present], ["r1"])

    def test_ops_tokens_escalation_input_contract(self):
        import storelib.ops_tokens as ot
        toks = ot.derive_ops_tokens("git stash pop")
        self.assertIn("git", toks)
        self.assertIn("stash", toks)
        self.assertIn("pop", toks)
        # and the escalation rule fires on the pinned row text
        self.assertTrue(self.dl.strong_token_match(STASH_TEXT.lower(), toks))


class CliExcludeTest(unittest.TestCase):
    """--exclude on recall/recent/search: pre-gate filter + envelope count."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-excl-")
        self.ns = "project:excl-test"
        _seed(self.tmp, self.ns, STASH_TEXT)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "recent",
             "--namespace", self.ns, "--limit", "5", "--json"],
            capture_output=True, text=True, env=_clean_env(self.tmp),
            timeout=120)
        self.mid = json.loads(r.stdout)["results"][0]["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recall(self, *extra: str, sub: str = "recall") -> dict:
        argv = [sys.executable, str(SCRIPTS / "store.py"), sub]
        if sub == "recall":
            argv += ["--query", "git stash pop", "--namespace", self.ns,
                     "--limit", "5", "--json"]
        elif sub == "recent":
            argv += ["--namespace", self.ns, "--limit", "5", "--json"]
        else:
            argv += ["--text", "git stash pop", "--namespace", self.ns,
                     "--json"]
        argv += list(extra)
        r = subprocess.run(argv, capture_output=True, text=True,
                           env=_clean_env(self.tmp), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_recall_exclude_filters_and_counts(self):
        env = self._recall("--exclude", self.mid)
        self.assertEqual([r["id"] for r in env["results"]], [])
        self.assertGreaterEqual(env["excluded"], 1)
        # candidate_ids keeps its PRE-exclude miss-rate-join meaning (the
        # for-injection lane is where candidate_ids rides the envelope)
        env_fi = self._recall("--exclude", self.mid, "--for-injection")
        self.assertEqual([r["id"] for r in env_fi["results"]], [])
        self.assertIn(self.mid, env_fi["candidate_ids"])
        self.assertEqual(env_fi["reason"], "already-delivered")
        # without the flag: delivered, and NO excluded key (byte-identical
        # unused path)
        env2 = self._recall()
        self.assertIn(self.mid, [r["id"] for r in env2["results"]])
        self.assertNotIn("excluded", env2)

    def test_explain_rejects_exclude(self):
        # Issue #151 review (CUBIC-cli-278): --explain --exclude is refused,
        # never silently ignored.
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "recall",
             "--query", "git stash pop", "--namespace", self.ns,
             "--explain", "--exclude", "some-id"],
            capture_output=True, text=True, env=_clean_env(self.tmp),
            timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--exclude", r.stderr)

    def test_recent_and_search_exclude(self):
        env = self._recall("--exclude", self.mid, sub="recent")
        self.assertEqual([r["id"] for r in env["results"]], [])
        self.assertGreaterEqual(env["excluded"], 1)
        env_fi = self._recall("--exclude", self.mid, "--for-injection",
                              sub="recent")
        self.assertEqual([r["id"] for r in env_fi["results"]], [])
        self.assertIn(self.mid, env_fi["candidate_ids"])
        self.assertEqual(env_fi["reason"], "already-delivered")
        env = self._recall("--exclude", self.mid, sub="search")
        self.assertEqual([r["id"] for r in env["results"]], [])
        self.assertGreaterEqual(env["excluded"], 1)


class ForInjectionGlobalScopeTest(unittest.TestCase):
    """Sessionless --for-injection must honor the global-tier opt-in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-fi-global-")
        self.ns = "project:fi-global"
        _seed(self.tmp, self.ns, "project-only passive injection lesson")
        _seed(self.tmp, "user:global", "global-only passive injection lesson")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, sub: str, *, include_global: bool) -> dict:
        argv = [sys.executable, str(SCRIPTS / "store.py"), sub]
        if sub == "recall":
            argv += ["--query", "global-only passive injection lesson"]
        else:
            argv += ["--limit", "5"]
        argv += ["--namespace", self.ns, "--for-injection", "--json"]
        if include_global:
            argv.append("--include-global")
        result = subprocess.run(
            argv, capture_output=True, text=True,
            env=_clean_env(self.tmp), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_sessionless_for_injection_requires_global_opt_in(self):
        for sub in ("recall", "recent"):
            without = self._run(sub, include_global=False)
            self.assertTrue(without["results"],
                            f"sessionless {sub} vacuity guard: the project "
                            f"lane must still return rows without --include-global")
            self.assertNotIn(
                "user:global", {r["namespace"] for r in without["results"]},
                f"sessionless {sub} --for-injection must not include global rows")
            with_global = self._run(sub, include_global=True)
            self.assertIn(
                "user:global", {r["namespace"] for r in with_global["results"]},
                f"sessionless {sub} --for-injection must honor --include-global")


class HookBodyDeliveryTest(unittest.TestCase):
    """Retirement, fallback env gate, session_end clear, exc= field."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-body117-")
        self.ns = "project:body117"
        _seed(self.tmp, self.ns, STASH_TEXT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pretool_retired_by_default_and_still_delivers(self):
        r = _run_body(self.tmp,
                      {"tool_input": {"command": "git stash pop"},
                       "session_id": "sess-r"},
                      self.ns, "pretool", ZMEM_HOST="claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        pend = [f.name for f in _ops_files(self.tmp)
                if f.name.endswith(".pending")]
        self.assertEqual(pend, [],
                         "sidecar must be retired by default (no "
                         "ZMEM_PENDING_SIDECAR)")
        led = [f.name for f in _ops_files(self.tmp)
               if f.name.endswith(".ledger")]
        self.assertEqual(len(led), 1, "delivery recorded in the ledger")

    def test_pending_sidecar_stays_retired_when_legacy_env_is_set(self):
        # #158 removes the hook-owned raw-row fallback entirely. A leaked
        # legacy opt-in must not revive a .pending sidecar or bypass the
        # store-owned rendered envelope/ledger path.
        r = _run_body(self.tmp,
                      {"tool_input": {"command": "git stash pop"},
                       "session_id": "sess-f"},
                      self.ns, "pretool", ZMEM_HOST="claude",
                      ZMEM_PENDING_SIDECAR="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        pend = [f.name for f in _ops_files(self.tmp)
                if f.name.endswith(".pending")]
        self.assertEqual(pend, [])
        led = [f.name for f in _ops_files(self.tmp)
               if f.name.endswith(".ledger")]
        self.assertEqual(len(led), 1,
                         "delivery remains store-owned and ledger-backed")

    def test_session_end_clears_even_under_kill_switch(self):
        # deliver first (records the ledger)
        _run_body(self.tmp,
                  {"prompt": "git stash pop hazard reminder",
                   "session_id": "sess-e"},
                  self.ns, "user_prompt")
        self.assertEqual(len([f for f in _ops_files(self.tmp)
                              if f.name.endswith(".ledger")]), 1)
        r = _run_body(self.tmp, {"session_id": "sess-e"},
                      self.ns, "session_end", ZMEM_INJECT="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {})
        self.assertEqual([f for f in _ops_files(self.tmp)
                          if f.name.endswith(".ledger")], [])

    def test_decision_line_carries_exc_field(self):
        _run_body(self.tmp,
                  {"prompt": "git stash pop hazard reminder",
                   "session_id": "sess-x"},
                  self.ns, "user_prompt")
        # second identical prompt: the row is in the ledger now -> excluded
        _run_body(self.tmp,
                  {"prompt": "git stash pop hazard reminder",
                   "session_id": "sess-x"},
                  self.ns, "user_prompt")
        log = Path(self.tmp, "zmem-decisions.log")
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines()
                 if "zmem-hook" in ln and "moment=user_prompt" in ln]
        self.assertTrue(lines)
        self.assertIn(" exc=", lines[-1],
                      f"decision line must carry the exc= field: {lines[-1]}")

    def test_empty_pool_keeps_legacy_decision_token_shape(self):
        _run_body(self.tmp,
                  {"prompt": "no matching passive injection lesson",
                   "session_id": "sess-empty"},
                  self.ns, "user_prompt")
        log = Path(self.tmp, "zmem-decisions.log")
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines()
                 if "zmem-hook" in ln and "moment=user_prompt" in ln]
        self.assertTrue(lines)
        self.assertIn(" reason=empty-pool", lines[-1])
        self.assertNotIn(" tokens=", lines[-1])
        self.assertNotIn(" rendered_estimate=", lines[-1])


class FeedbackLedgerTest(unittest.TestCase):
    """Issue #124: the per-session operation-feedback sidecar primitives
    (atomic write, loader-guarded idempotence, session attribution)."""

    def setUp(self):
        os.environ.pop("ZMEM_DELIVER_WINDOW_S", None)
        os.environ.pop("ZMEM_LEDGER_CAP", None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-fbledger-")
        import storelib.delivery_ledger as dl
        self.dl = dl

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_feedback_event_is_atomic_and_idempotent(self):
        now = "2026-09-10T10:01:00Z"
        # Empty session ids are refused loudly (never a silent fallback
        # path that would collide every session on one file).
        with self.assertRaises(ValueError):
            self.dl.feedback_event_path(self.tmp, "")

        self.dl.record_feedback_event(
            self.tmp, "sess-1", "ev-1", "mem-1", "violated", 2, "ev-1",
            now=now)
        path = self.dl.feedback_event_path(self.tmp, "sess-1")
        record = {"event_id": "ev-1", "evidence_id": "ev-1",
                  "memory_id": "mem-1", "overlap": 2,
                  "session_id": "sess-1", "timestamp": now,
                  "verdict": "violated"}
        expected = (json.dumps(record, sort_keys=True,
                               separators=(",", ":"),
                               ensure_ascii=False) + "\n").encode("utf-8")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), expected,
                             "the sidecar line must be exact sorted compact "
                             "JSON + LF (the #124 fixture byte contract)")
        # Atomicity: only the final file exists in ops — no tmp residue.
        ops = Path(self.tmp, "ops")
        self.assertEqual(sorted(p.name for p in ops.iterdir()),
                         [Path(path).name])

        # Idempotence at the event level: record_feedback_event is the raw
        # append primitive — tuple dedup is the LOADER's job (the exact
        # feedback_seen guard apply_operation_feedback runs before every
        # record), per the frozen #124 design. The guarded re-record of the
        # same tuple therefore appends nothing: still exactly one line.
        if not self.dl.feedback_seen(self.tmp, "sess-1", "ev-1", "mem-1",
                                     "violated"):
            self.dl.record_feedback_event(
                self.tmp, "sess-1", "ev-1", "mem-1", "violated", 2, "ev-1",
                now=now)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), expected)
        self.assertTrue(self.dl.feedback_seen(self.tmp, "sess-1", "ev-1",
                                              "mem-1", "violated"))
        # the verdict is part of the 4-tuple: a different verdict is unseen
        self.assertFalse(self.dl.feedback_seen(self.tmp, "sess-1", "ev-1",
                                               "mem-1", "applied"))

        # Forced append failure: FeedbackSidecarError, prior bytes unchanged,
        # still no tmp residue.
        before = Path(path).read_bytes()
        from unittest import mock
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(self.dl.FeedbackSidecarError):
                self.dl.record_feedback_event(
                    self.tmp, "sess-1", "ev-2", "mem-1", "applied", 1, None,
                    now=now)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(sorted(p.name for p in ops.iterdir()),
                         [Path(path).name])

    def test_feedback_is_session_attributed(self):
        sess_a = "00000000-0000-4000-8000-000000000124"
        sess_b = "00000000-0000-4000-8000-000000000134"
        self.dl.record_feedback_event(
            self.tmp, sess_a, "ev-1", "mem-1", "violated", 2, None,
            now="2026-09-10T10:01:00Z")
        # session A's hashed sidecar exists and carries the tuple...
        self.assertTrue(self.dl.feedback_seen(self.tmp, sess_a, "ev-1",
                                              "mem-1", "violated"))
        # ...while session B's sidecar was never created and reports nothing
        # (a per-session file may never answer for another session).
        path_b = self.dl.feedback_event_path(self.tmp, sess_b)
        self.assertFalse(os.path.exists(path_b))
        self.assertFalse(self.dl.feedback_seen(self.tmp, sess_b, "ev-1",
                                               "mem-1", "violated"))
        # and the two sessions never share one file (full-id hash key).
        self.assertNotEqual(self.dl.feedback_event_path(self.tmp, sess_a),
                            path_b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
