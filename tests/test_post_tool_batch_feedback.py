"""Issue #124 hook-lane tests: the PostToolBatch and PostToolUseFailure
adapters report operation outcomes to the store's feedback loop.

Proves end to end, through the REAL hook scripts:
- a successful PostToolBatch dispatches `operation-feedback --outcome success`
  carrying the payload's session id and evidence id, and increments
  applied_count on exactly the matching delivered memory;
- a PostToolUseFailure dispatches `--outcome failure` (session from
  ZMEM_SESSION, evidence id from the payload) and increments violated_count
  on exactly the matching delivered memory;
- an unrelated batch event writes exactly one unmatched sidecar row
  (memory_id "", overlap 0, verdict "unmatched") and moves no counter.

Pattern (tests/test_posttoolbatch.py conventions): a scratch plugin root
whose skills/memory/scripts/store.py is a WRAPPER that appends its argv to a
recording file and then execs the real skills/memory/scripts/store.py, so
every host-visible store invocation is observable while the real CLI does
the work. The store is a throwaway scratch store; the ledger is seeded with
the #124 fixture semantics (ts one minute before the event, which uses the
real clock because the hooks pass no --now).

Store-isolation env is pinned at MODULE level before any storelib import
(the storelib STORE_PATH freeze rule). Runs standalone:
python tests/test_post_tool_batch_feedback.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Store-isolation env MUST be pinned before any storelib import anywhere in
# this process (hard assignment, NOT setdefault — an ambient ZMEM_STORE must
# never survive into this module's fixtures).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-ptbf-module-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
REAL_STORE_PY = SCRIPTS / "store.py"
BATCH_HOOK = REPO_ROOT / "hooks" / "zmem-posttoolbatch-recall.sh"
FAILURE_HOOK = REPO_ROOT / "hooks" / "zmem-capture-failure.sh"

sys.path.insert(0, str(SCRIPTS))

FB_NS = "project:feedback-124"
FB_SESS = "00000000-0000-4000-8000-000000000124"
FB_M125 = "00000000-0000-4000-8000-000000000125"
FB_M126 = "00000000-0000-4000-8000-000000000126"
FB_EV130 = "00000000-0000-4000-8000-000000000130"
FB_EV131 = "00000000-0000-4000-8000-000000000131"
FB_EV132 = "00000000-0000-4000-8000-000000000132"

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _bash() -> str:
    bash = shutil.which("bash")
    if os.name == "nt":
        bash = next((str(path) for path in (
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
            Path(r"C:\Program Files\Git\bin\bash.exe"),
        ) if path.is_file()), bash)
    return bash


class PostToolBatchFeedbackTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        bash = _bash()
        if not bash:
            raise unittest.SkipTest("bash is required for the hook lane")
        # The scratch plugin root: a copy of the real scripts tree with
        # store.py replaced by the recording wrapper. The copy keeps
        # capture_quality.py / redaction.py importable next to the wrapper
        # (the capture-failure adapter imports them from the store's dir);
        # every actual store command is executed by the REAL store.py.
        cls.plugin_root = Path(
            tempfile.mkdtemp(prefix="zmem-ptbf-plugin-")) / "plugin"
        scripts_copy = cls.plugin_root / "skills" / "memory" / "scripts"
        shutil.copytree(
            SCRIPTS, scripts_copy,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        wrapper = scripts_copy / "store.py"
        wrapper.write_text(
            "import json, os, runpy, sys\n"
            "with open(os.environ['ZMEM_WRAPPER_RECORD'], 'a',"
            " encoding='utf-8') as _f:\n"
            "    _f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "runpy.run_path(r'%s', run_name='__main__')\n"
            % str(REAL_STORE_PY).replace("\\", "\\\\"),
            encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.plugin_root.parent, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-ptbf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.record = os.path.join(self.tmp, "argv.jsonl")
        # A real initialized store, then the #124 fixture rows/evidence.
        r = subprocess.run(
            [sys.executable, str(REAL_STORE_PY), "init"],
            capture_output=True, text=True,
            env=_clean_env(self.tmp), timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        store = os.path.join(self.tmp, "store.sqlite")
        conn = sqlite3.connect(store)
        try:
            conn.executemany(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " signal, confidence, ingestion_ts, source_ref, content_norm)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(FB_M125, FB_NS, "lesson",
                  "python tests/test_feedback_promote.py guard",
                  "tests,python", "test", 0.9, "2026-09-10T09:00:00Z", "", "a"),
                 (FB_M126, FB_NS, "lesson", "git status --short advice",
                  "git", "test", 0.9, "2026-09-10T09:00:00Z", "", "b")])
            conn.executemany(
                "INSERT INTO memory_evidence (memory_id, evidence_id)"
                " VALUES (?,?)",
                [(FB_M125, FB_EV130), (FB_M126, FB_EV131)])
            conn.commit()
        finally:
            conn.close()
        # The session delivery ledger, seeded one minute before the event
        # (the hooks pass no --now, so apply uses the real clock).
        ops = os.path.join(self.tmp, "ops")
        os.makedirs(ops, exist_ok=True)
        ts = time.time() - 60.0
        ledger = os.path.join(
            ops, hashlib.sha256(FB_SESS.encode("utf-8")).hexdigest()[:32]
            + ".ledger")
        with open(ledger, "w", encoding="utf-8") as f:
            json.dump({"entries": [
                {"id": FB_M125, "moment": "pretool", "ts": ts,
                 "text": "pytest tests/test_feedback_promote.py"},
                {"id": FB_M126, "moment": "pretool", "ts": ts,
                 "text": "git stash pop"}]}, f)

    # -- helpers -------------------------------------------------------------

    def _hook_env(self) -> dict:
        return _clean_env(
            self.tmp,
            ZMEM_ROOT=str(self.plugin_root),
            ZMEM_NAMESPACE=FB_NS,
            ZMEM_WRAPPER_RECORD=self.record,
        )

    def _run_hook(self, hook: Path, payload: dict, **extra: str):
        env = self._hook_env()
        env.update(extra)
        return subprocess.run(
            [_bash(), str(hook)],
            input=json.dumps(payload), text=True, capture_output=True,
            env=env, timeout=180)

    def _recorded(self) -> list:
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _operation_feedback_argv(self) -> list:
        calls = [a for a in self._recorded()
                 if "operation-feedback" in a]
        self.assertTrue(calls, f"no operation-feedback invocation recorded; "
                               f"argv log: {self._recorded()}")
        return calls[0]

    def _counters(self) -> dict:
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        try:
            return {mid: (applied, violated) for mid, applied, violated in
                    conn.execute("SELECT id, applied_count, violated_count "
                                 "FROM memory WHERE namespace=?", (FB_NS,))}
        finally:
            conn.close()

    def _sidecar_records(self) -> list:
        sidecar = os.path.join(
            self.tmp, "ops", hashlib.sha256(
                FB_SESS.encode("utf-8")).hexdigest()[:32]
            + ".feedback.jsonl")
        with open(sidecar, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # -- tests ----------------------------------------------------------------

    def test_successful_batch_applies_only_matching_memory(self):
        # The hook derives operation tokens from the restricted input-field
        # set (command/file_path/notebook_path/path — no tool-name prefix),
        # matching the delivery side, so a matching batch increments
        # applied_count on exactly the matching memory.
        r = self._run_hook(BATCH_HOOK, {
            "session_id": FB_SESS,
            "evidence_id": FB_EV131,
            "tool_uses": [{"name": "Bash",
                           "input": {"command": "git stash pop"}}],
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self._operation_feedback_argv()
        self.assertEqual(argv[0], "operation-feedback", argv)
        self.assertIn(FB_SESS, argv, argv)
        self.assertIn(FB_EV131, argv, argv)
        self.assertEqual(argv[argv.index("--outcome") + 1], "success")
        self.assertGreaterEqual(argv.count("--operation-token"), 3)
        self.assertEqual(self._counters(), {FB_M125: (0, 0), FB_M126: (1, 0)},
                         "a matching successful batch must increment "
                         "applied_count on exactly the matching memory — "
                         "see the comment above: the hook's tool-name-prefixed "
                         "raw tokens can never clear the matcher's "
                         "min_overlap=2")
        records = self._sidecar_records()
        self.assertEqual([rec["verdict"] for rec in records], ["applied"])
        self.assertEqual(records[0]["memory_id"], FB_M126)
        self.assertEqual(records[0]["evidence_id"], FB_EV131)

    def test_batch_without_payload_evidence_id_increments(self):
        # Final-critic round 1 (production-liveness proof): a real batch
        # payload carries no evidence_id (that association-write API is
        # issue #171, unshipped). The hook must OMIT --evidence-id so the
        # store association gate is vacuous, and the matched counter must
        # still move -- the pre-fix always-synthetic-id behavior dead-ended
        # every production event at applied_count=0.
        r = self._run_hook(BATCH_HOOK, {
            "session_id": FB_SESS,
            "tool_uses": [{"name": "Bash",
                           "input": {"command": "git stash pop"}}],
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self._operation_feedback_argv()
        self.assertEqual(argv[0], "operation-feedback", argv)
        self.assertIn(FB_SESS, argv, argv)
        self.assertNotIn("--evidence-id", argv, argv)
        self.assertEqual(argv[argv.index("--outcome") + 1], "success")
        self.assertEqual(self._counters(), {FB_M125: (0, 0), FB_M126: (1, 0)})
        records = self._sidecar_records()
        self.assertEqual([rec["verdict"] for rec in records], ["applied"])
        self.assertIsNone(records[0]["evidence_id"])

    def test_failed_event_violates_only_matching_memory(self):
        # The capture-failure lane works: its tokens come from the tool
        # INPUT only (no tool-name prefix), so a runner-head command derives
        # cleanly and the matcher reaches overlap 2.
        r = self._run_hook(FAILURE_HOOK, {
            "tool_name": "Bash",
            "tool_input": {"command":
                           "pytest tests/test_feedback_promote.py"},
            "error": {"message": "Exit code 1", "type": "error"},
            "evidence_id": FB_EV130,
        }, ZMEM_SESSION=FB_SESS, ZMEM_CAPTURE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self._operation_feedback_argv()
        self.assertEqual(argv[0], "operation-feedback", argv)
        self.assertIn(FB_SESS, argv, argv)
        self.assertIn(FB_EV130, argv, argv)
        self.assertEqual(argv[argv.index("--outcome") + 1], "failure")
        self.assertEqual(self._counters(), {FB_M125: (0, 1), FB_M126: (0, 0)})
        records = self._sidecar_records()
        self.assertEqual([rec["verdict"] for rec in records], ["violated"])
        self.assertEqual(records[0]["memory_id"], FB_M125)
        self.assertEqual(records[0]["evidence_id"], FB_EV130)

    def test_unrelated_batch_event_is_unmatched(self):
        r = self._run_hook(BATCH_HOOK, {
            "session_id": FB_SESS,
            "evidence_id": FB_EV132,
            "tool_uses": [{"name": "Bash",
                           "input": {"command": "bun test unrelated"}}],
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self._operation_feedback_argv()
        self.assertEqual(argv[argv.index("--outcome") + 1], "success")
        self.assertIn(FB_EV132, argv, argv)
        # No counter moved for the unrelated operation...
        self.assertEqual(self._counters(), {FB_M125: (0, 0), FB_M126: (0, 0)})
        # ...and the sidecar holds exactly the unmatched row.
        records = self._sidecar_records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["memory_id"], "")
        self.assertEqual(records[0]["overlap"], 0)
        self.assertEqual(records[0]["verdict"], "unmatched")
        self.assertEqual(records[0]["evidence_id"], FB_EV132)
        self.assertEqual(records[0]["session_id"], FB_SESS)
        self.assertEqual(sorted(records[0]),
                         ["event_id", "evidence_id", "memory_id", "overlap",
                          "session_id", "timestamp", "verdict"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
