"""Issue #121 — SessionStart timeout-budget tests (payload side).

SessionStartTimeoutTest: Tier 0 precedes the first store call, exactly ONE
store attempt, and the slow fixture drops Tier 2 with a reason=omitted
store_timeout=1 decision line. Drives the REAL payload builder
(hooks/lib/zmem-session-start-payload.py) with a stub store.py that records
its invocations. The fast-timeout sub-run pins ZMEM_STORE_RECALL_TIMEOUT_S
below 1.0 (the contract honors values below 8.0) so the suite never waits
on a wall-clock deadline — no test asserts elapsed time.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
from eval_store import BASE_ENV  # noqa: E402

NL = chr(10)
STUB_LINES = [
    "import os, sys, time, json",
    "mode = os.environ.get('SS_STUB_MODE', 'ok')",
    "if len(sys.argv) > 1 and sys.argv[1] == 'recent':",
    "    with open(os.environ['SS_STUB_LOG'], 'a') as f:",
    "        f.write(str(time.time()) + ' invoked' + chr(10))",
    "    if mode == 'fail':",
    "        sys.exit(1)",
    "    if mode == 'slow':",
    "        time.sleep(float(os.environ.get('SS_STUB_SLOW_S', '9')))",
    "    print(json.dumps({'results':[{'id':'stub-row-1','text':'tier two stub row','confidence':0.9,'kind':'lesson'}],'count':1,'omitted':0,'reason':'injected','candidate_ids':['stub-row-1']}))",
    "elif len(sys.argv) > 1 and sys.argv[1] == 'promote':",
    "    print('0 promotion candidates')",
    "else:",
    "    print('')",
]


class SessionStartTimeoutTest(unittest.TestCase):
    maxDiff = None

    def _scratch(self):
        base = Path(tempfile.mkdtemp(prefix="zmem-121-ss-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        (base / "core.md").write_text("SS-TIER0-CORE", encoding="utf-8")
        data = base / "data"
        data.mkdir()
        (base / "store.py").write_text(NL.join(STUB_LINES) + NL,
                                       encoding="utf-8", newline=NL)
        return base, data

    def _env(self, base, data, mode, slow_s="9", timeout_s=None):
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update({
            "ZMEM_STORE": str(data / "store.sqlite"),
            "ZMEM_DATA": str(data),
            "ZMEM_INJECT": "1",
            "SS_STUB_MODE": mode,
            "SS_STUB_LOG": str(base / "stub.log"),
            "SS_STUB_SLOW_S": slow_s,
        })
        if timeout_s is not None:
            env["ZMEM_STORE_RECALL_TIMEOUT_S"] = str(timeout_s)
        return env

    def _run_payload_streamed(self, env):
        """Run the payload reading stdout incrementally; record the arrival
        time of the first complete sentinel on the payload's own stdout."""
        argv = [sys.executable, str(PAYLOAD),
                str(env["_CORE"]), "", str(env["_STORE_PY"]), str(env["_DATA"]),
                str(env["_BASE"]), str(env["_BASE"]),
                "project:fixture/zmem", "25000", "zcode", "", "", "sid-ss",
                "", ""]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env,
                                cwd=str(REPO_ROOT))
        first_sentinel_t = None
        buf = b""

        def reader():
            nonlocal first_sentinel_t, buf
            try:
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        break
                    buf += ch
                    if first_sentinel_t is None and b"<<<END>>>" in buf:
                        first_sentinel_t = time.time()
            finally:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

        thread = threading.Thread(target=reader)
        thread.start()
        rc = proc.wait(timeout=120)
        thread.join(timeout=10)
        return rc, buf.decode("utf-8", "replace"), first_sentinel_t

    def test_tier0_before_store(self):
        base, data = self._scratch()
        env = self._env(base, data, "ok")
        env["_CORE"] = str(base / "core.md")
        env["_STORE_PY"] = str(base / "store.py")
        env["_DATA"] = str(data)
        env["_BASE"] = str(base)
        rc, out, first_t = self._run_payload_streamed(env)
        self.assertEqual(rc, 0)
        stub_log = base / "stub.log"
        self.assertTrue(stub_log.exists(), "the store stub must be invoked")
        stub_t = float(stub_log.read_text().split()[0])
        self.assertIsNotNone(first_t, "a complete sentinel must reach stdout")
        self.assertLess(first_t, stub_t,
                        "Tier 0 must be emitted BEFORE the first store subprocess")
        first_payload = json.loads(
            out.split("<<<ZMEM_JSON>>>")[1].split("<<<END>>>")[0])
        self.assertIn("SS-TIER0-CORE",
                      first_payload.get("additionalContext", ""))
        self.assertNotIn("tier two stub row",
                         first_payload.get("additionalContext", ""),
                         "the early envelope carries Tier 0 only")

    def test_one_store_attempt(self):
        base, data = self._scratch()
        env = self._env(base, data, "fail")
        env["_CORE"] = str(base / "core.md")
        env["_STORE_PY"] = str(base / "store.py")
        env["_DATA"] = str(data)
        env["_BASE"] = str(base)
        proc = subprocess.run(
            [sys.executable, str(PAYLOAD),
             str(base / "core.md"), "", str(base / "store.py"), str(data),
             str(base), str(base), "project:fixture/zmem", "25000", "zcode",
             "", "", "sid-ss", "", ""],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=120)
        self.assertEqual(proc.returncode, 0)
        count = len((base / "stub.log").read_text().splitlines())
        self.assertEqual(count, 1,
                         "exactly ONE store attempt (the pre-fix retry loop "
                         "made 3)")

    def test_slow_fixture_output(self):
        # 0.5 s cap (below-8.0 values are honored) vs a 3 s stub: the timeout
        # fires, Tier 2 is dropped, and the decision line carries the
        # reason=omitted store_timeout=1 pair. No wall-clock assertion.
        base, data = self._scratch()
        env = self._env(base, data, "slow", slow_s="3", timeout_s="0.5")
        proc = subprocess.run(
            [sys.executable, str(PAYLOAD),
             str(base / "core.md"), "", str(base / "store.py"), str(data),
             str(base), str(base), "project:fixture/zmem", "25000", "zcode",
             "", "", "sid-ss", "", ""],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("<<<ZMEM_JSON>>>", proc.stdout)
        final = proc.stdout.split("<<<ZMEM_JSON>>>")[-1].split("<<<END>>>")[0]
        payload = json.loads(final)
        self.assertNotIn("tier two stub row",
                         payload.get("additionalContext", ""),
                         "a store timeout must drop Tier 2")
        self.assertIn("SS-TIER0-CORE",
                      payload.get("additionalContext", ""),
                      "Tier 0 is never delayed by the store timeout")
        decisions = data / "zmem-decisions.log"
        self.assertTrue(decisions.exists(),
                        "the timeout decision line must land")
        line = decisions.read_text(encoding="utf-8")
        self.assertIn("reason=omitted", line)
        self.assertIn("store_timeout=1", line)


if __name__ == "__main__":
    unittest.main()
