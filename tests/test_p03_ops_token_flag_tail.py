"""Issue #251 (Workstream P PR 3): ops-token flag-tail regression pins.

Freezes the three acceptance checks of zaxbysauce/zmem#251 against the
ops-token non-runner fallback:

- C1 (AC1): ``derive_ops_tokens`` never emits a leading-dash token from the
  non-runner fallback branch — ``cd x && grep foo bar | head -5`` derives no
  ``-5``.
- C2 (AC2): tokens derived from that command never strong-match the unrelated
  delivered row that merely contains the same flag.
- C3 (AC3): a row delivered at ``--moment user_prompt`` stays suppressed when
  the same session recalls at ``--moment pretool`` (the PostToolBatch
  decision moment, zmem-recall-body ``_decision_moment``) with the flag-tailed
  command as the query.

All three are PRESERVING at the trace base (8f2aeb1): the defect behind
issue #251 was fixed by 7f8600f (PR #300 / issue #163, PRR-022) but shipped
with no pin — these tests ARE the pin. Deleting the guard at
``ops_tokens.py`` non-runner branch turns C1/C2/C3 red with exactly the
issue's anchor signatures (tokens ``['-5']``, strong match True, delivered
row re-admitted at pretool).

``CMD`` is a POSIX-shaped STRING parsed as text; nothing runs in a shell, so
the tests are host-independent (Windows CI included).

Env isolation: ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR /
ZMEM_MODEL_AUTODOWNLOAD are pinned to a scratch dir at MODULE level before
any storelib import (the storelib STORE_PATH freeze rule), and every
store-touching subprocess gets a per-test clean env built by ``_clean_env``
(the test_posttoolbatch strip-list convention). Runs standalone:
``python tests/test_p03_ops_token_flag_tail.py``
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Store-isolation env MUST be pinned before any storelib import anywhere in
# this process (hard assignment, not setdefault — an ambient ZMEM_STORE must
# never survive into this module's fixtures).
_MODULE_SCRATCH = tempfile.mkdtemp(prefix="zmem-p03-module-")
os.environ["ZMEM_STORE"] = os.path.join(_MODULE_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _MODULE_SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_MODULE_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORELIB = SCRIPTS / "storelib"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(STORELIB))

import ops_tokens  # noqa: E402  (path + env pins above are load-bearing)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


delivery_ledger = _load_module(STORELIB / "delivery_ledger.py",
                               "delivery_ledger_p03")

# The issue's POSIX-shaped command string (text, never executed).
CMD = "cd x && grep foo bar | head -5"
# The delivered row from AC3 (project:p03flag, signal test, confidence 0.9).
ROW = "grep foo output: pipe it through head -5 before pasting"
# The unrelated row AC2 matches the CMD tokens against.
UNRELATED_ROW = "pipe build output through head -5 before pasting it"
NS = "project:p03flag"
SESSION = "sess-p03"

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


class NonRunnerFlagTailTest(unittest.TestCase):
    """C1/C2: flag tails never become ops tokens or strong matches."""

    def test_no_leading_dash_token_from_non_runner_tail(self):
        # AC1 — the non-runner fallback takes the head-"cd" path and must
        # emit no leading-dash token (the -5 tail is a flag, not a basename).
        toks = ops_tokens.derive_ops_tokens(CMD)
        self.assertEqual(sum(1 for t in toks if t.startswith("-")), 0)

    def test_flag_tail_does_not_strong_match_unrelated_row(self):
        # AC2 — with no flag token derived, the matcher must not strong-match
        # a row that merely mentions "head -5". The matcher itself keeps its
        # punctuation-tolerant whole-token semantics (AC9 / -rf).
        toks = ops_tokens.derive_ops_tokens(CMD)
        self.assertFalse(
            delivery_ledger.strong_token_match(UNRELATED_ROW, toks))

    def test_python_script_py_keeps_script_basename(self):
        # AC4's recorded decision (ops_tokens.py comment + CHANGELOG): plain
        # interpreter invocations stay UN-collapsed — the script basename is
        # the operation-adjacent recall signal, while -c/-m collapse to the
        # bare runner because their payloads are flags/values. This pin makes
        # the documented decision drift-proof; changing the behaviour forces
        # a conscious, documented change instead of silent re-collapse.
        self.assertEqual(ops_tokens.derive_ops_tokens("python script.py"),
                         ["python", "script.py"])
        self.assertEqual(
            ops_tokens.derive_ops_tokens('python -c "print(1)"'), ["python"])


class PretoolFlagReadmissionTest(unittest.TestCase):
    """C3: the flag-tailed command does not re-admit a delivered row."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-p03-ac3-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._env = _clean_env(self._tmp)

    def _recall(self, moment: str) -> dict:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "recall",
             "--query", CMD, "--namespace", NS, "--no-hybrid", "--no-bump",
             "--for-injection", "--json", "--session-id", SESSION,
             "--moment", moment],
            capture_output=True, text=True, env=self._env, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_flag_tail_does_not_readmit_delivered_row_at_pretool(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), "add",
             "--namespace", NS, "--type", "lesson", "--content", ROW,
             "--signal", "test", "--confidence", "0.9"],
            capture_output=True, text=True, env=self._env, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)

        def row_present(results) -> bool:
            return any(ROW in (row.get("content") or "")
                       for row in results or [])

        # Precondition (vacuity guard): the row must render on the first,
        # delivering call — otherwise "suppressed at pretool" proves nothing.
        first = self._recall("user_prompt")
        self.assertTrue(row_present(first.get("results")),
                        "precondition failed: row never rendered at "
                        "user_prompt; the suppression check would be vacuous")

        # The same session, same query, pretool moment (also the
        # PostToolBatch moment): the delivered row stays suppressed because
        # the flag tail derives no token, so no strong-match carve-out runs.
        second = self._recall("pretool")
        self.assertFalse(row_present(second.get("results")),
                         "delivered row was re-admitted at pretool")


if __name__ == "__main__":
    unittest.main(verbosity=2)
