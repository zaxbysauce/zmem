"""D2-H hook query-rewrite and decision-parser contracts."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BODY_PATH = ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SCRIPTS = ROOT / "skills" / "memory" / "scripts"


def _load_body():
    spec = importlib.util.spec_from_file_location(
        "issue183_d2_body_" + uuid.uuid4().hex, BODY_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _stdin(value: str):
    old = __import__("sys").stdin
    __import__("sys").stdin = io.StringIO(value)
    try:
        yield
    finally:
        __import__("sys").stdin = old


def _run_body(body, fake_store, *, query="continue from yesterday",
              env_extra=None):
    env = {
        # Keep the hook's sidecar/log resolver under the OS scratch area.  A
        # repository-relative missing store would make _data_dir prefer ROOT
        # and pollute the worktree with zmem-decisions.log.
        "ZMEM_STORE": str(Path(tempfile.gettempdir()) / "zmem-query-rewrite-hook-store.sqlite"),
        "ZMEM_DATA": tempfile.gettempdir(),
        "ZMEM_INJECT": "1",
        "ZMEM_QUERY_CONTEXT": "1",
        "ZMEM_SESSION": "s",
        "ZMEM_HOST": "claude",
    }
    if env_extra:
        env.update(env_extra)
    old_argv = __import__("sys").argv[:]
    try:
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(body, "_run_store", side_effect=fake_store), \
                mock.patch.object(body.os.path, "isfile", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), \
                _stdin(json.dumps({"prompt": query, "session_id": "s"})):
            __import__("sys").argv = [
                str(BODY_PATH), "store.py", "project:test", "1500", "user_prompt"
            ]
            return body.main()
    finally:
        __import__("sys").argv = old_argv


class HookRewriteTest(unittest.TestCase):
    def test_real_rewrite_timeout_kills_store_and_fails_open(self):
        body = _load_body()
        with tempfile.TemporaryDirectory(prefix="zmem-183-timeout-") as raw:
            scratch = Path(raw)
            ready_pid = scratch / "ready.pid"
            store = scratch / "blocking_store.py"
            store.write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                "Path(os.environ['ZMEM_TEST_TIMEOUT_MARKER']).write_text(str(os.getpid()), encoding='ascii')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            recorded = {}
            original_popen = body.subprocess.Popen

            def recording_popen(*args, **kwargs):
                proc = original_popen(*args, **kwargs)
                recorded["proc"] = proc
                recorded["command"] = args[0]
                return proc

            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(scratch / "home"),
                        "USERPROFILE": str(scratch / "home"),
                        "ZMEM_STORE": str(scratch / "store.sqlite"),
                        "ZMEM_DATA": str(scratch / "data"),
                        "ZMEM_MODEL": str(scratch / "model"),
                        "ZMEM_QUERY_CONTEXT": "1",
                        "ZMEM_STORE_RECALL_TIMEOUT_S": "1",
                        "ZMEM_TEST_TIMEOUT_MARKER": str(ready_pid),
                    },
                    clear=True,
                ), mock.patch.object(
                    body.subprocess, "Popen", side_effect=recording_popen,
                ):
                    started = time.monotonic()
                    query, rewritten = body._rewrite_query(
                        str(store), "project:test", "s", "continue from yesterday"
                    )
                    elapsed = time.monotonic() - started
                self.assertEqual((query, rewritten), ("continue from yesterday", False))
                self.assertLess(elapsed, 4.0)
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and not ready_pid.exists():
                    time.sleep(0.05)
                self.assertTrue(ready_pid.exists())
                proc = recorded["proc"]
                self.assertEqual(int(ready_pid.read_text(encoding="ascii")), proc.pid)
                self.assertEqual(recorded["command"][0], body.sys.executable)
                self.assertEqual(recorded["command"][1], str(store))
                self.assertEqual(recorded["command"][2], "query-rewrite")
                self.assertIsNotNone(proc.poll())
                self.assertNotEqual(proc.returncode, 0)
                self.assertTrue(proc.stdout is None or proc.stdout.closed)
                self.assertTrue(proc.stderr is None or proc.stderr.closed)
            finally:
                proc = recorded.get("proc")
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_rewrite_precedes_recall_and_uses_bounded_timeout(self):
        body = _load_body()
        calls = []

        def fake_store(_store, args, timeout=None):
            calls.append((list(args), timeout))
            if args[0] == "query-rewrite":
                return body.subprocess.CompletedProcess(
                    args, 0, '{"query":"continue from yesterday git status","rewrite":1}', ""
                )
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":"ok","reason":"injected"}', ""
            )

        self.assertEqual(_run_body(body, fake_store), 0)
        self.assertEqual([args[0] for args, _ in calls], ["query-rewrite", "recall"])
        self.assertLessEqual(calls[0][1], 1.0)
        self.assertEqual(
            calls[1][0][calls[1][0].index("--query") + 1],
            "continue from yesterday git status",
        )

    def test_leading_dash_uses_equals_and_exact_switch_bypasses(self):
        body = _load_body()
        calls = []

        def fake_store(_store, args, timeout=None):
            calls.append(list(args))
            if args[0] == "query-rewrite":
                return body.subprocess.CompletedProcess(
                    args, 0, '{"query":"-dry-run alpha beta gamma","rewrite":1}', ""
                )
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":"ok"}', ""
            )

        _run_body(body, fake_store, query="-dry-run alpha beta gamma")
        rewrite = calls[0]
        self.assertIn("--prompt=-dry-run alpha beta gamma", rewrite)
        recall = calls[1]
        self.assertIn("--query=-dry-run alpha beta gamma", recall)

        calls.clear()
        _run_body(body, fake_store, env_extra={"ZMEM_QUERY_CONTEXT": "0"})
        self.assertEqual([args[0] for args in calls], ["recall"])

    def test_user_prompt_keeps_late_anchor_for_rewrite_and_overlong_fails_open(self):
        body = _load_body()
        late = ("context " * 500) + "src/main.py"
        self.assertLessEqual(len(late), 4096)
        self.assertEqual(body._query_for("user_prompt", {"prompt": late}), late)
        captured = {}

        def fake_store(_store, args, timeout=None):
            if args[0] == "query-rewrite":
                captured["prompt"] = next(
                    args[index + 1]
                    for index, value in enumerate(args)
                    if value == "--prompt"
                )
                return body.subprocess.CompletedProcess(
                    args, 0, '{"query":"fallback","rewrite":0}', ""
                )
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":"ok"}', ""
            )

        with mock.patch.object(body, "_run_store", side_effect=fake_store):
            rewritten, flag = body._rewrite_query("store.py", "project:test", "s", late)
        self.assertEqual((rewritten, flag), ("fallback", False))
        self.assertEqual(captured["prompt"], late)

        overlong = "x" * 4096 + "src/main.py"
        calls = []

        def capture_store(_store, args, timeout=None):
            calls.append(list(args))
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":"ok"}', ""
            )

        self.assertEqual(
            _run_body(body, capture_store, query=overlong),
            0,
        )
        self.assertEqual([args[0] for args in calls], ["recall"])
        recall = calls[0]
        self.assertEqual(recall[recall.index("--query") + 1], overlong[:500])

    def test_bad_rewrite_response_fails_open_without_rewrite_log(self):
        body = _load_body()
        calls = []

        def fake_store(_store, args, timeout=None):
            calls.append(list(args))
            output = '{"query":"rewritten","rewrite":2,"extra":true}'
            if args[0] == "query-rewrite":
                return body.subprocess.CompletedProcess(args, 0, output, "")
            return body.subprocess.CompletedProcess(
                args, 0, '{"rendered":"ok"}', ""
            )

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"ZMEM_DATA": raw}, clear=False
        ):
            _run_body(
                body, fake_store,
                env_extra={"ZMEM_DATA": raw, "ZMEM_STORE": str(Path(raw) / "store.sqlite")},
            )
            line = (Path(raw) / "zmem-decisions.log").read_text(encoding="utf-8")
        self.assertEqual(calls[1][calls[1].index("--query") + 1], "continue from yesterday")
        self.assertNotIn("rewrite=1", line)

    def test_rewrite_flag_and_duplicate_keys_are_strict(self):
        body = _load_body()
        for malformed in (
            '{"query":"rewritten","rewrite":1.0}',
            '{"query":"rewritten","rewrite":0.0}',
            '{"query":"rewritten","rewrite":[]}',
            '{"query":"rewritten","rewrite":1,"rewrite":0}',
        ):
            calls = []

            def fake_store(_store, args, timeout=None):
                calls.append(list(args))
                if args[0] == "query-rewrite":
                    return body.subprocess.CompletedProcess(args, 0, malformed, "")
                return body.subprocess.CompletedProcess(args, 0, '{"rendered":"ok"}', "")

            _run_body(body, fake_store)
            recall = next(args for args in calls if args[0] == "recall")
            self.assertEqual(
                recall[recall.index("--query") + 1], "continue from yesterday"
            )


class MissRateRewriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        sys.path.insert(0, str(SCRIPTS / "storelib"))
        from storelib import miss_rate
        cls.miss_rate = miss_rate

    def test_writer_order_and_parser_roundtrip(self):
        body = _load_body()
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
            os.environ, {"ZMEM_DATA": raw}, clear=False
        ):
            body._maybe_log_drift = lambda *_: None
            body._rotate_telemetry_logs = lambda *_: None
            body._log_inject_decision(
                [], [], "silent", "omitted", session_id="s", moment="user_prompt",
                margin=0.125, margin_pruned_ids=["id"], store_timeout=True,
                rewrite=True,
            )
            path = Path(raw) / "zmem-decisions.log"
            line = path.read_text(encoding="utf-8").strip()
            self.assertLess(line.index("store_timeout=1"), line.index("rewrite=1"))
            parsed = self.miss_rate.parse_bg_log(path)
        self.assertEqual(parsed[0]["rewrite"], True)
        self.assertEqual(parsed[0]["margin"], "0.125000")

    def test_invalid_rewrite_flag_is_rejected_and_old_shape_survives(self):
        line = (
            "[1] zmem-hook status=injected reason=injected ids=[] all=[] "
            "sid=s moment=user_prompt rewrite=0\n"
            "[2] zmem-hook status=injected reason=injected ids=[] all=[] "
            "sid=s moment=user_prompt\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "zmem-decisions.log"
            path.write_text(line, encoding="utf-8")
            parsed = self.miss_rate.parse_bg_log(path)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["ts"], 2)
        self.assertFalse(parsed[0].get("rewrite", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
