"""Fail-closed closeout gate — acceptance spec.

Defect under test (PR zaxbysauce/zmem#-closeout-gate; observed live on
zaxbygraph PR #81's feedback session, 2026-11-19): `store.py
closeout-ack` was a bare file touch — an agent could capture ad-hoc
rows, write "closeout DONE", and never run any closeout step, and every
marker/reminder path stayed satisfied. The operator flagged the class as
recurring.

POST-FIX CONTRACT (public behaviors, driven through the real store.py
and the real hook scripts):

- `closeout-step` appends receipts to
  `<sha256(sid)[:32]>.closeout-steps.jsonl`; an unknown step name is
  refused (rc 2).
- `closeout-skip` requires --reason (rc 2 without) and satisfies its
  step for the ack.
- `closeout-ack` FAILS CLOSED (rc 2, JSON `ok:false`, NO `.closeout`
  marker) when required-step receipts are missing or STALE (a store
  write happened after them), or when session-attributed rows lack
  `closeout-review` verdicts. It writes the marker only on a fully
  discharged gate.
- A REAL `store.py add --source-ref "session:<sid>"` stamps the
  `.wrote` marker with a parseable epoch-seconds last-write line and
  REMOVES the `.nudged` marker (the SessionStart reminder rearms after
  every new write batch).
- `closeout-status` reports missing_steps / stale_steps /
  unreviewed_rows / ack_valid as JSON.
- The reminder surfaces (zmem-recall-body session-end leg,
  zmem-session-start-payload) no longer offer `closeout-ack` as a
  no-work alternative — the ack is fail-closed, so pointing at it as a
  shortcut was the false-done trap.
- `zmem-closeout-gate.sh` (Stop): rc 2 + stderr reason while the
  request marker is armed and the gate is unmet; rc 0 and the request
  marker removed once ack_valid; rc 0 when no closeout was requested.
- `zmem-closeout-request.sh` (UserPromptSubmit): arms
  `.closeout-requested` on closeout phrasing ONLY when the session has
  a `.wrote` marker; a non-closeout prompt leaves it unarmed; a fresh
  prompt disarms a stale request.

FROZEN PUBLIC SEAM: the ops filename contract
(`<sha256(sid)[:32]>.wrote` / `.nudged` / `.closeout`) is unchanged;
the NEW sidecar is `<sha256(sid)[:32]>.closeout-steps.jsonl`.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _store_guard  # noqa: F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
GATE_SH = REPO_ROOT / "hooks" / "zmem-closeout-gate.sh"
REQUEST_SH = REPO_ROOT / "hooks" / "zmem-closeout-request.sh"
SESSION_START_SH = REPO_ROOT / "hooks" / "zmem-session-start.sh"
SESSION_END_SH = REPO_ROOT / "hooks" / "zmem-session-end.sh"

NS = "project:closeout-gate-probe"

REQUIRED = ("review-queue", "recall-before-write", "consolidate",
            "promote", "knowledge-status")

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_REMOTE_URL",
    "ZMEM_TRANSCRIPT", "ZMEM_CAPTURE", "ZMEM_AUTO_RETAIN",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
)


def _clean_env(tmp: str, sid: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_SESSION": sid,
        "ZMEM_NAMESPACE": NS,
        "ZMEM_ROOT": str(REPO_ROOT),
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _marker(tmp: str, sid: str, suffix: str) -> Path:
    stem = hashlib.sha256(sid.encode("utf8")).hexdigest()[:32]
    return Path(tmp) / "ops" / (stem + suffix)


def _store(args: list, tmp: str, sid: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), *args],
        capture_output=True, text=True, env=_clean_env(tmp, sid),
        timeout=180,
    )


def _store_add(tmp: str, sid: str, content: str = "closeout gate probe row",
               namespace: str = NS) -> subprocess.CompletedProcess:
    env = _clean_env(tmp, sid)
    env.pop("ZMEM_SESSION", None)  # marker keys on the parsed source-ref
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", namespace, "--type", "fact",
         "--content", content,
         "--signal", "test", "--confidence", "0.9",
         "--source-ref", f"session:{sid}:probe"],
        capture_output=True, text=True, env=env, timeout=180,
    )


def _all_required_receipts(tmp: str, sid: str) -> None:
    """Record every required-step receipt (fresh against now)."""
    for step in REQUIRED:
        p = _store(["closeout-step", "--session-id", sid, "--step", step],
                   tmp, sid)
        assert p.returncode == 0, p.stdout + p.stderr


def _ack(tmp: str, sid: str) -> subprocess.CompletedProcess:
    return _store(["closeout-ack", "--session-id", sid], tmp, sid)


def _status(tmp: str, sid: str) -> dict:
    p = _store(["closeout-status", "--session-id", sid], tmp, sid)
    return json.loads(p.stdout)


def _run_script(script: Path, env: dict) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        raise unittest.SkipTest("no bash on PATH")
    return subprocess.run(
        [bash, str(script)], input="{}",
        capture_output=True, text=True, env=env, timeout=180,
        cwd=env["ZMEM_DATA"],
    )


class CloseoutStepReceiptsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sid = "closeout-gate-sess"
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_step_receipt_appends_and_unknown_step_refused(self):
        p = _store(["closeout-step", "--session-id", self.sid,
                    "--step", "review-queue"], self.tmp, self.sid)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        receipts = _marker(self.tmp, self.sid, ".closeout-steps.jsonl")
        self.assertTrue(receipts.is_file())
        lines = [json.loads(ln) for ln in receipts.read_text(
            encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(lines[0].get("step"), "review-queue")
        self.assertIn("ts", lines[0])

        bad = _store(["closeout-step", "--session-id", self.sid,
                      "--step", "not-a-step"], self.tmp, self.sid)
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)

    def test_skip_requires_reason(self):
        p = _store(["closeout-skip", "--session-id", self.sid,
                    "--step", "consolidate"], self.tmp, self.sid)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        p2 = _store(["closeout-skip", "--session-id", self.sid,
                     "--step", "consolidate",
                     "--reason", "cadence gate declined"], self.tmp, self.sid)
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)


class CloseoutAckFailClosedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sid = "closeout-ack-sess"
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ack_without_receipts_fails_closed(self):
        p = _ack(self.tmp, self.sid)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        body = json.loads(p.stdout)
        self.assertFalse(body.get("ok"))
        self.assertEqual(sorted(body.get("missing_steps", [])),
                         sorted(REQUIRED))
        self.assertFalse(_marker(self.tmp, self.sid, ".closeout").is_file())

    def test_ack_with_fresh_receipts_passes(self):
        _all_required_receipts(self.tmp, self.sid)
        p = _ack(self.tmp, self.sid)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(_marker(self.tmp, self.sid, ".closeout").is_file())

    def test_ack_refuses_when_a_write_postdates_the_receipts(self):
        _all_required_receipts(self.tmp, self.sid)
        # A store write AFTER the receipts: the receipts are now stale.
        add = _store_add(self.tmp, self.sid)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        p = _ack(self.tmp, self.sid)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        body = json.loads(p.stdout)
        self.assertEqual(sorted(body.get("stale_steps", [])),
                         sorted(REQUIRED))

    def test_unreviewed_session_row_blocks_ack_and_review_discharges(self):
        add = _store_add(self.tmp, self.sid, content="unreviewed probe row")
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        _all_required_receipts(self.tmp, self.sid)
        status = _status(self.tmp, self.sid)
        self.assertFalse(status.get("ack_valid"))
        self.assertEqual(len(status.get("unreviewed_rows", [])), 1)
        row_id = status["unreviewed_rows"][0]["id"]

        p = _ack(self.tmp, self.sid)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

        rev = _store(["closeout-review", "--session-id", self.sid,
                      "--memory-id", row_id, "--verdict", "keep"],
                     self.tmp, self.sid)
        self.assertEqual(rev.returncode, 0, rev.stdout + rev.stderr)
        p2 = _ack(self.tmp, self.sid)
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertTrue(_marker(self.tmp, self.sid, ".closeout").is_file())

    def test_skip_satisfies_a_step_for_the_ack(self):
        for step in REQUIRED:
            if step == "consolidate":
                continue
            p = _store(["closeout-step", "--session-id", self.sid,
                        "--step", step], self.tmp, self.sid)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        sk = _store(["closeout-skip", "--session-id", self.sid,
                     "--step", "consolidate",
                     "--reason", "cadence gate declined; no new rows"],
                    self.tmp, self.sid)
        self.assertEqual(sk.returncode, 0, sk.stdout + sk.stderr)
        p = _ack(self.tmp, self.sid)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_status_reports_the_gate_shape(self):
        status = _status(self.tmp, self.sid)
        for key in ("ack_valid", "missing_steps", "stale_steps",
                    "unreviewed_rows", "last_write_ts", "last_ack_ts",
                    "receipt_count"):
            self.assertIn(key, status)


class WriteMarkerContractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sid = "write-marker-sess"
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_attributed_add_stamps_wrote_with_parseable_ts_and_clears_nudge(self):
        nudged = _marker(self.tmp, self.sid, ".nudged")
        nudged.parent.mkdir(parents=True, exist_ok=True)
        nudged.write_text("nudged\n", encoding="utf-8")

        add = _store_add(self.tmp, self.sid)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)

        wrote = _marker(self.tmp, self.sid, ".wrote")
        self.assertTrue(wrote.is_file())
        ts = float(wrote.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertGreater(ts, 0)
        self.assertFalse(nudged.is_file(),
                         "the write must rearm (remove) the .nudged marker")

    def test_second_write_appends_a_later_ts(self):
        _store_add(self.tmp, self.sid, content="row one")
        wrote = _marker(self.tmp, self.sid, ".wrote")
        first = float(wrote.read_text(encoding="utf-8").strip().splitlines()[-1])
        _store_add(self.tmp, self.sid, content="row two")
        second = float(wrote.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertGreaterEqual(second, first)


class ReminderTextContractTest(unittest.TestCase):
    """The reminder surfaces must NOT offer `closeout-ack` as a no-work
    alternative — that phrasing was the false-done trap (the ack is now
    fail-closed, so an ack without the steps refuses)."""

    def test_session_start_payload_no_longer_offers_the_ack_alternative(self):
        text = Path(REPO_ROOT / "hooks" / "lib" /
                    "zmem-session-start-payload.py").read_text(
                        encoding="utf-8")
        self.assertNotIn("if nothing needs capturing", text)

    def test_recall_body_no_longer_offers_the_ack_alternative(self):
        text = Path(REPO_ROOT / "hooks" / "lib" /
                    "zmem-recall-body.py").read_text(encoding="utf-8")
        # The removed phrase is the no-work alternative "(or `store.py
        # closeout-ack --session-id <id>`)" — target it exactly; the file
        # legitimately contains other "(or " parentheticals.
        self.assertNotIn("closeout-ack --session-id <id>",
                         text.replace("\r\n", "\n"))


class CloseoutRequestScriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sid = "request-script-sess"
        env = _clean_env(self.tmp, self.sid)
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)
        wrote = _marker(self.tmp, self.sid, ".wrote")
        wrote.write_text(f"{__import__('time').time():.6f}\n",
                         encoding="utf-8")
        self.env = env

    def tearDown(self):
        self._tmp.cleanup()

    def _run_request(self, prompt: str) -> subprocess.CompletedProcess:
        bash = shutil.which("bash")
        if not bash:
            raise unittest.SkipTest("no bash on PATH")
        payload = json.dumps({"session_id": self.sid, "prompt": prompt})
        return subprocess.run(
            [bash, str(REQUEST_SH)], input=payload,
            capture_output=True, text=True, env=self.env, timeout=180,
            cwd=self.env["ZMEM_DATA"],
        )

    def test_closeout_prompt_arms_the_marker_when_wrote_exists(self):
        p = self._run_request("please run the closeout for this session")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(_marker(self.tmp, self.sid,
                                ".closeout-requested").is_file())

    def test_non_closeout_prompt_leaves_it_unarmed(self):
        p = self._run_request("fix the flaky test in src/auth.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(_marker(self.tmp, self.sid,
                                 ".closeout-requested").is_file())

    def test_session_without_writes_is_not_armed(self):
        sid2 = "request-script-nowrite"
        env2 = _clean_env(self.tmp, sid2)
        payload = json.dumps({"session_id": sid2, "prompt": "run the closeout"})
        bash = shutil.which("bash")
        if not bash:
            raise unittest.SkipTest("no bash on PATH")
        p = subprocess.run([bash, str(REQUEST_SH)], input=payload,
                           capture_output=True, text=True, env=env2,
                           timeout=180, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(_marker(self.tmp, sid2,
                                 ".closeout-requested").is_file())


class CloseoutGateScriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.sid = "gate-script-sess"
        os.makedirs(os.path.join(self.tmp, "ops"), exist_ok=True)
        wrote = _marker(self.tmp, self.sid, ".wrote")
        wrote.write_text(f"{__import__('time').time():.6f}\n",
                         encoding="utf-8")
        # The gate resolves store.py through ZMEM_ROOT.
        self.env = _clean_env(self.tmp, self.sid)

    def tearDown(self):
        self._tmp.cleanup()

    def _arm(self):
        req = _marker(self.tmp, self.sid, ".closeout-requested")
        req.write_text("armed\n", encoding="utf-8")

    def test_armed_and_unmet_blocks_with_rc2(self):
        self._arm()
        p = _run_script(GATE_SH, self.env)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("closeout", (p.stderr or "").lower())

    def test_armed_and_valid_passes_and_disarms(self):
        self._arm()
        _all_required_receipts(self.tmp, self.sid)
        ack = _ack(self.tmp, self.sid)
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        p = _run_script(GATE_SH, self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(_marker(self.tmp, self.sid,
                                 ".closeout-requested").is_file())

    def test_no_request_is_inert(self):
        p = _run_script(GATE_SH, self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_loop_budget_degrades_after_five_blocks(self):
        self._arm()
        for _ in range(5):
            p = _run_script(GATE_SH, self.env)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        p = _run_script(GATE_SH, self.env)
        self.assertEqual(p.returncode, 0,
                         "the 6th consecutive block must degrade to "
                         "advisory (loop budget)")
        self.assertIn("degrading", (p.stderr or "").lower())


if __name__ == "__main__":
    unittest.main()
