"""Issue #259 [Workstream R] PR 3 — interpreter surfacing tests.

Every hook-suggested ``store.py`` command must name an existing interpreter
executable immediately before the script path. At MAIN the SessionStart note
and all four ``zmem-reflect.sh`` nudges render the bare script path, and the
two capture hooks (convention-capture, capture-failure) do the same, so an
agent that copies a suggested command on Windows can hit the Store
``python`` stub. ``store.py`` has no shebang, so a bare token depends
entirely on the caller's shell resolution.

The token rule is deliberately interpreter-agnostic (the issue's AC harness):
split the rendered command with ``shlex.split(cmd, posix=False)`` (non-POSIX
mode keeps Windows backslashes), strip surrounding quotes from each token,
find the first token ending in ``store.py`` after normalizing path
separators, and require the token before it to be an existing file.
Existence rather than equality to the test's ``sys.executable`` keeps the
check independent of which interpreter the hook resolved.

Run: python tests/test_r03_interpreter_in_commands.py
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"

_GIT_BASHES = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
)
_BASH = next((str(path) for path in _GIT_BASHES if path.is_file()),
             shutil.which("bash"))

# Command-rendering sites (the Phase 4.2 census): every file that renders a
# suggested ``store.py`` command. The census asserts each binds the
# interpreter from ``sys.executable`` and carries no bare-path binding left.
RENDER_SITES = (
    "hooks/lib/zmem-session-start-payload.py",
    "hooks/zmem-reflect.sh",
    "hooks/zmem-convention-capture.sh",
    "hooks/zmem-capture-failure.sh",
)


def interpreter_before_store_py(cmd):
    """Token immediately before the first ``store.py`` token, or ``''`` when
    the store.py token is first, or ``None`` when there is no store.py
    token at all."""
    tokens = shlex.split(cmd, posix=False)
    stripped = []
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        stripped.append(tok)
    for i, tok in enumerate(stripped):
        if tok.replace("\\", "/").endswith("store.py"):
            return stripped[i - 1] if i > 0 else ""
    return None


def extract_ctx(raw):
    """Pull the JSON object out of the sentinel envelope; {} on failure."""
    if "<<<ZMEM_JSON>>>" not in raw:
        return {}
    inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
    try:
        return json.loads(inner)
    except Exception:
        return {}


def recent_sidecar_created():
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def write_transcript(records):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def tool_result(tid, content, is_error=True):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": content, "is_error": is_error,
         "tool_use_id": tid}]}}


class InterpreterInSessionStartNoteTest(unittest.TestCase):
    """AC1: the SessionStart memory-skill note names a real interpreter."""

    def test_store_note_names_an_interpreter(self):
        base = Path(tempfile.mkdtemp(prefix="zmem-r03-ss-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        (base / "core.md").write_text("R03-CORE", encoding="utf-8")
        data = base / "data"
        data.mkdir()
        store_py = base / "store.py"
        stub = [
            "import os, sys, time, json",
            "if len(sys.argv) > 1 and sys.argv[1] == 'recent':",
            "    print(json.dumps({'results':[{'id':'r03-stub','text':'stub row',"
            "'confidence':0.9,'kind':'lesson'}],'count':1,'omitted':0,"
            "'reason':'injected','candidate_ids':['r03-stub']}))",
            "elif len(sys.argv) > 1 and sys.argv[1] == 'promote':",
            "    print('0 promotion candidates')",
            "else:",
            "    print('')",
        ]
        store_py.write_text("\n".join(stub) + "\n", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "ZMEM_STORE": str(data / "store.sqlite"),
            "ZMEM_DATA": str(data),
            "ZMEM_MODELS_DIR": str(base / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        argv = [sys.executable, str(PAYLOAD),
                str(base / "core.md"), "", str(store_py), str(data), str(base),
                str(base), "project:fixture/zmem", "25000", "zcode", "", "",
                "sid-r03", "", ""]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              cwd=str(REPO_ROOT), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("<<<ZMEM_JSON>>>", proc.stdout, proc.stdout[:400])
        first = proc.stdout.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
        ctx = json.loads(first)
        note = ctx.get("additionalContext", "")
        match = re.search(r"invoke `([^`]+)`", note)
        self.assertIsNotNone(match, note)
        prefix = interpreter_before_store_py(match.group(1))
        self.assertIsNotNone(prefix, note)
        self.assertTrue(os.path.isfile(prefix), repr(prefix))


class InterpreterInReflectCommandsTest(unittest.TestCase):
    """AC2/AC3/AC4: every Stop-hook nudge names a real interpreter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-r03-reflect-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, env_extra, stdin="{}"):
        env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_CAPTURE", "ZMEM_ZCODE_DB",
                    "ZMEM_TRANSCRIPT", "ZMEM_FAILURES_DB_TIMEOUT_S"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": self.tmp,
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_SESSION": "rtest",
            "ZMEM_NAMESPACE": "project:rtest",
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        env.update(env_extra)
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-reflect.sh")],
            input=stdin, text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=env, timeout=60,
        )
        return proc.stdout

    def _assert_command_names_interpreter(self, msg):
        """Extract the backticked add-command and apply the AC token rule."""
        cmds = re.findall(r"`([^`]*add --namespace[^`]*)`", msg)
        self.assertTrue(cmds, msg)
        prefix = interpreter_before_store_py(cmds[0])
        self.assertIsNotNone(prefix, msg)
        self.assertTrue(os.path.isfile(prefix), repr(prefix))

    def test_no_failure_nudge_names_an_interpreter(self):
        raw = self._run({})
        msg = extract_ctx(raw).get("additionalContext", "")
        self.assertIn("had no tool failures", msg, msg)
        self._assert_command_names_interpreter(msg)

    def test_subagent_handoff_nudge_names_an_interpreter(self):
        ring = Path(self.tmp) / "subagent-reflections"
        ring.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "session": "rtest",
            "agent_id": "agent-r03",
            "agent_type": "explorer",
            "source_ref": "session:rtest:agent:agent-r03",
            "count": 1,
            "tool_summary": "1=Bash",
            "details": ["  - Bash : boom"],
            "rejections": "",
            "created": recent_sidecar_created(),
        }
        (ring / "r03.json").write_text(json.dumps(sidecar) + "\n",
                                       encoding="utf-8")
        raw = self._run({})  # no transcript: hand-off alone is sufficient
        msg = extract_ctx(raw).get("additionalContext", "")
        self.assertIn("dispatched subagent", msg, msg)
        self._assert_command_names_interpreter(msg)

    def test_failure_prompt_names_an_interpreter(self):
        trans = write_transcript([
            tool_use("t1", "Bash"),
            tool_result("t1", "Exit code 1"),
        ])
        self.addCleanup(os.remove, trans)
        raw = self._run({"ZMEM_TRANSCRIPT": os.path.abspath(trans)})
        msg = extract_ctx(raw).get("additionalContext", "")
        self.assertIn("failed tool call(s)", msg, msg)
        self._assert_command_names_interpreter(msg)


class InterpreterInCaptureHooksTest(unittest.TestCase):
    """AC9/AC10 (Phase 4.2 siblings): the two capture hooks render the same
    defect class — their suggested add commands must name an interpreter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-r03-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _hook_env(self):
        env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_CAPTURE", "ZMEM_TRANSCRIPT"):
            env.pop(key, None)
        env.update({
            "ZMEM_ROOT": str(REPO_ROOT),
            "ZMEM_DATA": self.tmp,
            "ZMEM_STORE": os.path.join(self.tmp, "store.sqlite"),
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        return env

    def _assert_command_names_interpreter(self, msg, anchors):
        """The capture hooks render the command in prose after 'run: ' /
        'running: ' (no backticks, unlike the reflect hooks)."""
        cmds = []
        for anchor, trailer in anchors:
            m = re.search(re.escape(anchor) + r"(.*?)" + re.escape(trailer),
                          msg)
            if m:
                cmds = [m.group(1)]
                break
        self.assertTrue(cmds, msg)
        prefix = interpreter_before_store_py(cmds[0])
        self.assertIsNotNone(prefix, msg)
        self.assertTrue(os.path.isfile(prefix), repr(prefix))

    def test_convention_capture_names_an_interpreter(self):
        env = self._hook_env()
        env.update({"ZMEM_SESSION": "sess-r03", "ZMEM_NAMESPACE":
                    "project:rtest", "ZMEM_CONVENTION_INTERVAL": "10"})
        event = json.dumps({"tool_name": "Bash",
                            "tool_input": {"command": "git commit -m r03"},
                            "session_id": "sess-r03"})
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-convention-capture.sh")],
            input=event, text=True, capture_output=True, encoding="utf-8",
            errors="replace", env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        msg = extract_ctx(proc.stdout).get("additionalContext", "")
        self.assertIn("ZMem convention capture", msg, msg)
        self._assert_command_names_interpreter(
            msg, [("run: ", ". If not, do nothing")])

    def test_capture_failure_names_an_interpreter(self):
        env = self._hook_env()
        env.update({"ZMEM_SESSION": "r03cap", "ZMEM_NAMESPACE":
                    "project:rtest", "ZMEM_CAPTURE": "1"})
        payload = json.dumps({"session_id": "r03cap", "tool_name": "Bash",
                              "tool_input": {"command":
                                             "pytest tests/test_example.py"},
                              "error": "Exit code 1"})
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-capture-failure.sh")],
            input=payload, text=True, capture_output=True, encoding="utf-8",
            errors="replace", env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        msg = extract_ctx(proc.stdout).get("additionalContext", "")
        self.assertIn("additionalContext", json.dumps(extract_ctx(proc.stdout)))
        self._assert_command_names_interpreter(
            msg, [("running: ", ". If it is a one-off")])


class InterpreterBindingCensusTest(unittest.TestCase):
    """AC11 (defect-class guardrail): every command-rendering site must bind
    the interpreter from sys.executable, and no site may keep a bare-path
    binding. Static census tripwire — the behavioral checks above remain the
    per-site proof."""

    def test_every_command_rendering_site_binds_sys_executable(self):
        for rel in RENDER_SITES:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(site=rel):
                if rel.endswith("zmem-session-start-payload.py"):
                    self.assertIn("invoke `%s %s <subcommand>`", text,
                                  "%s must render the interpreter ahead of "
                                  "the script path" % rel)
                else:
                    self.assertRegex(
                        text, r"shlex\.quote\(sys\.executable\)",
                        "%s must bind the interpreter from sys.executable"
                        % rel)
                # No bare-path binding may survive anywhere in the site.
                # (?m) line-anchors the pattern (the review round found the
                # bare `$` form could never fire mid-file), and the variable
                # alternation names only the store-path binding variables so
                # legitimate namespace bindings (shlex.quote(sys.argv[3]))
                # never match. The concatenation continuation after the first
                # shlex.quote call also keeps prefixed bindings from matching.
                self.assertNotRegex(
                    text,
                    r"(?m)^\s*(?:store|store_py|store_py_arg)\s*=\s*"
                    r"shlex\.quote\([^)]*\)\s*$",
                    "%s still binds a bare store path" % rel)


if __name__ == "__main__":
    unittest.main()
