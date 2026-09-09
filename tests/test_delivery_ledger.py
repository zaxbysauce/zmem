"""Issue #117 — session delivery ledger: unit + behavioral coverage.

Surfaces pinned here:
- storelib.delivery_ledger: hashed collision-free naming, record/prune/cap,
  atomic write (no tmp residue), strong_token_match escalation rule,
  fallback pending sidecar append-with-dedup, clear_delivery_state.
- store CLI: --exclude on recall/recent/search filters pre-gate and reports
  the envelope ``excluded`` count (candidate_ids stays PRE-exclude).
- hook body: sidecar retired by default, fallback env-gated, session_end
  clears delivery state (even under the kill switch), decision line gains
  the additive exc= field.
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
        # without the flag: delivered, and NO excluded key (byte-identical
        # unused path)
        env2 = self._recall()
        self.assertIn(self.mid, [r["id"] for r in env2["results"]])
        self.assertNotIn("excluded", env2)

    def test_recent_and_search_exclude(self):
        env = self._recall("--exclude", self.mid, sub="recent")
        self.assertEqual([r["id"] for r in env["results"]], [])
        self.assertGreaterEqual(env["excluded"], 1)
        env = self._recall("--exclude", self.mid, sub="search")
        self.assertEqual([r["id"] for r in env["results"]], [])
        self.assertGreaterEqual(env["excluded"], 1)


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

    def test_fallback_env_parks_hashed_pending(self):
        _run_body(self.tmp,
                  {"tool_input": {"command": "git stash pop"},
                   "session_id": "sess-f"},
                  self.ns, "pretool", ZMEM_HOST="claude",
                  ZMEM_PENDING_SIDECAR="1")
        pend = [f.name for f in _ops_files(self.tmp)
                if f.name.endswith(".pending")]
        self.assertEqual(len(pend), 1)
        self.assertNotIn("sess-f", pend[0],
                         "fallback file must be hash-keyed, not sanitize+truncate")

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


def main() -> int:
    unittest.main(module=sys.modules[__name__], exit=False, verbosity=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
