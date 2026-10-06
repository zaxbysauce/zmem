"""Issue #259 feedback round (PRR-007) — behavioral interpreter check for the
rejections-only Stop nudge.

hooks/zmem-reflect.sh renders a FIFTH add-command template in the
rejections-only branch (``count == 0`` with user rejections: "this session
had tool rejections but no tool failures"). The frozen checks (byte-locked)
drive only three of the four reflect templates; this unfrozen companion
drives the fourth the same way and applies the same token rule.

Run: python tests/test_r03_rejections_interpreter.py
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_GIT_BASHES = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
)
_BASH = next((str(path) for path in _GIT_BASHES if path.is_file()),
             shutil.which("bash"))


def interpreter_before_store_py(cmd):
    """Token immediately before the first ``store.py`` token, or ``''`` when
    the store.py token is first, or ``None`` when there is none (same rule
    as the frozen checks)."""
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
    if "<<<ZMEM_JSON>>>" not in raw:
        return {}
    inner = raw.split("<<<ZMEM_JSON>>>", 1)[1].split("<<<END>>>", 1)[0]
    try:
        return json.loads(inner)
    except Exception:
        return {}


class RejectionsOnlyNudgeInterpreterTest(unittest.TestCase):
    def test_rejections_only_nudge_names_an_interpreter(self):
        tmp = Path(__file__).parent / (
            "zmem-r03-rej-" + __import__("uuid").uuid4().hex[:8])
        tmp.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # A zero-failure transcript carrying one user rejection drives the
        # count==0 / rej_msg branch (the rejections-only nudge).
        records = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Edit",
                 "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "The user doesn't want "
                 "to proceed.\nthe user said:\ndon't touch the CI config",
                 "is_error": True, "tool_use_id": "t1"}]}},
        ]
        trans = tmp / "transcript.jsonl"
        trans.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8")
        env = dict(os.environ)
        for key in ("ZMEM_REFLECT", "ZMEM_CAPTURE", "ZMEM_ZCODE_DB",
                    "ZMEM_TRANSCRIPT", "ZMEM_FAILURES_DB_TIMEOUT_S"):
            env.pop(key, None)
        env.update({
            "ZMEM_DATA": str(tmp),
            "ZMEM_STORE": str(tmp / "store.sqlite"),
            "ZMEM_SESSION": "rtest-rej",
            "ZMEM_NAMESPACE": "project:rtest",
            "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_TRANSCRIPT": str(trans),
        })
        proc = subprocess.run(
            [_BASH, str(REPO_ROOT / "hooks" / "zmem-reflect.sh")],
            input="{}", text=True, capture_output=True,
            encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        msg = extract_ctx(proc.stdout).get("additionalContext", "")
        self.assertIn("had tool rejections but no tool failures", msg, msg)
        cmds = re.findall(r"`([^`]*add --namespace[^`]*)`", msg)
        self.assertTrue(cmds, msg)
        prefix = interpreter_before_store_py(cmds[0])
        self.assertIsNotNone(prefix, msg)
        self.assertTrue(os.path.isfile(prefix), repr(prefix))


if __name__ == "__main__":
    unittest.main()
