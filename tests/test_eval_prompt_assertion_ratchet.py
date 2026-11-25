#!/usr/bin/env python3
"""Issue #530 AC5 ratchet (plan rev 12 item 6): the committed prompt-
assertion gold set plus the refreshed baseline make a CONSISTENT nominate-
all rescue mutant drift the committed baseline, so the eval ratchet bites
on every PR that mutates the rescue/partition seam.

Runs the REAL scripts/eval_inject_runner.py three times against the
deterministic eval corpus (built by the fixture builder at a scratch
store):

  1. STOCK   - the published command: exit 0 AND a baseline match (a
               stale baseline is not this criterion).
  2. MUTANT  - the same runner main() with a run-time nominate-all rescue
               monkeypatch applied consistently to BOTH selection
               derivations (production storelib.inject.rescue_nominees and
               the eval verifier's rescue leg, replaced by a delegation to
               the same mutated seam) — no source file is edited.
  3. VERDICT - the mutant run must exit 1 with a baseline drift verdict
               naming a moved metric. The drift comes from the
               prompt-assertion bucket's rescue-window rows: stock admits
               exactly ONE best nominee per unanswered focus; nominate-all
               also renders the second compatible approver row, which moves
               precision_at_k (and trips the must_exclude list).

CI-form: CI runs each tests/test_*.py directly with zero args, so the file
carries a __main__ block that executes the test methods and exits nonzero
on failure (repo rule: silent exit 0 is test theater under this repo's CI).
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-pa-ratchet-"))
atexit.register(shutil.rmtree, SCRATCH, True, None)

os.environ["ZMEM_STORE"] = str(SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(SCRATCH)
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_MODELS_DIR"] = str(SCRATCH / "models-empty")
os.makedirs(os.environ["ZMEM_MODELS_DIR"], exist_ok=True)
_HOME = SCRATCH / "home"
_HOME.mkdir(exist_ok=True)
os.environ["HOME"] = str(_HOME)
os.environ["USERPROFILE"] = str(_HOME)
for _k in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID",
           "ZCODE_SESSION_ID", "ZMEM_SESSION", "ZMEM_NAMESPACE", "ZMEM_HOST",
           "ZMEM_REMOTE_URL", "ZMEM_EMBED_PROFILE", "ZMEM_QUERY_CONTEXT",
           "ZMEM_CROSS_ENCODER", "ZMEM_CROSS_PROJECT"):
    os.environ.pop(_k, None)
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
os.environ["ZMEM_QUERY_CONTEXT"] = "0"
os.environ["ZMEM_CROSS_ENCODER"] = "0"
os.environ["ZMEM_CROSS_PROJECT"] = "0"

sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import eval_store  # noqa: E402

STORE = SCRATCH / "store.sqlite"
MUTANT_DRIVER = r'''"""Nominate-all rescue mutant driver (generated into scratch).

Runs the real scripts/eval_inject_runner.py main() with a run-time
nominate-all rescue mutant - no repository source file is edited. Applied
consistently to BOTH selection derivations (production rescue seam and the
verifier's rescue leg) so a consistent regression reaches the metric
comparison instead of tripping the bypass error (exit 2).
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True

REPO, STORE, GOLD, BASELINE = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

runner_path = os.path.join(REPO, "scripts", "eval_inject_runner.py")
spec = importlib.util.spec_from_file_location("_pa_ratchet_runner", runner_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

mod._bootstrap_env(STORE)

sys.argv = ["eval_inject_runner.py", "--store", STORE, "--gold", GOLD,
            "--compare-baseline", BASELINE]

sys.path.insert(0, str(mod.SCRIPTS_DIR))
import storelib.schema as _schema

_scratch = os.path.dirname(os.path.abspath(STORE))
_frozen = os.path.abspath(_schema.STORE_PATH)
try:
    _inside = os.path.commonpath([_frozen, _scratch]) == _scratch
except ValueError:
    _inside = False
if not _inside:
    print("frozen STORE_PATH %s is outside scratch %s" % (_frozen, _scratch),
          file=sys.stderr)
    raise SystemExit(97)

import storelib.inject as _inject
import storelib.eval_gold as _eval_gold

_orig_rescue = _inject.rescue_nominees


def _nominate_all_rescue(frame, survivors, ordinary_selected):
    nominees = dict(_orig_rescue(frame, survivors, ordinary_selected))
    if not frame["foci"]:
        return nominees
    for _ in range(10000):
        shield = [{"id": rid, "content": ""} for rid in nominees]
        nxt = _orig_rescue(frame, survivors, list(ordinary_selected) + shield)
        new = {k: v for k, v in nxt.items() if k not in nominees}
        if not new:
            return nominees
        nominees.update(new)
    raise RuntimeError("nominate-all fixpoint guard exceeded")


_inject.rescue_nominees = _nominate_all_rescue

import storelib.recall as _recall
if hasattr(_recall, "rescue_nominees"):
    _recall.rescue_nominees = _nominate_all_rescue


def _delegating_expected_selection(executed_query, cand_rows, *, floor,
                                   gate_none_floor, grounded):
    frame = _inject.analyze_prompt(executed_query or "")
    lane_floors = _inject._lane_floors()
    trust_floor = _inject._trust_floor()

    def _passes(r):
        return _eval_gold._gate_passes(
            r, floor, gate_none_floor, grounded,
            lane_floors=lane_floors, trust_floor=trust_floor)

    _drops, survivors = _inject.partition_prompt_assertions(frame, cand_rows)
    ordinary = [r for r in survivors if _passes(r)]
    if not frame["foci"]:
        return ordinary
    nominees = _inject.rescue_nominees(frame, survivors, ordinary)
    if not nominees:
        return ordinary
    return [r for r in survivors if _passes(r) or r["id"] in nominees]


_eval_gold._expected_selection = _delegating_expected_selection

sys.exit(mod.main())
'''


def _child_env(extra=None):
    env = {k: v for k, v in os.environ.items()}
    env.update(extra or {})
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class PromptAssertionRatchetTests(unittest.TestCase):
    """The AC5 ratchet: stock run matches the committed baseline; the
    consistent nominate-all rescue mutant drifts it (exit 1 + metrics
    differ)."""

    @classmethod
    def setUpClass(cls):
        if STORE.exists():
            STORE.unlink()
        eval_store.build_eval_store(str(STORE))
        cls.env = _child_env()

    def _run_runner(self, argv_extra=(), driver=None):
        if driver is None:
            cmd = [sys.executable, "-X", "utf8", "-B",
                   "scripts/eval_inject_runner.py",
                   "--store", str(STORE),
                   "--gold", str(REPO / "eval" / "injection_gold.jsonl"),
                   "--compare-baseline",
                   str(REPO / "eval" / "baseline-injection.json"),
                   *argv_extra]
        else:
            driver_path = SCRATCH / "mutant_driver.py"
            driver_path.write_text(driver, encoding="utf-8", newline="\n")
            cmd = [sys.executable, "-X", "utf8", "-B", str(driver_path),
                   str(REPO), str(STORE),
                   str(REPO / "eval" / "injection_gold.jsonl"),
                   str(REPO / "eval" / "baseline-injection.json")]
        proc = subprocess.run(cmd, cwd=str(REPO), env=self.env,
                              capture_output=True, text=True, timeout=600)
        return proc

    def test_stock_matches_committed_baseline(self):
        proc = self._run_runner()
        self.assertEqual(proc.returncode, 0,
                         "stock runner failed: %s" % proc.stderr[-400:])
        self.assertIn("baseline match", proc.stderr,
                      "stock run must match the committed baseline")

    def test_nominate_all_rescue_mutant_drifts_baseline(self):
        proc = self._run_runner(driver=MUTANT_DRIVER)
        self.assertEqual(
            proc.returncode, 1,
            "the nominate-all mutant did NOT drift the baseline (rc=%s); "
            "stderr tail: %s" % (proc.returncode, proc.stderr[-400:]))
        self.assertIn("metrics differ", proc.stderr,
                      "the runner must report the drift as a metric delta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
