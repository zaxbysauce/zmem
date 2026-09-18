"""Acceptance evidence for the read-only #155 replay gate consumed by #183."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = ROOT / "scripts" / "eval_replay.py"
FIXTURES = ROOT / "tests" / "fixtures" / "replay"
BASELINE = ROOT / "eval" / "baseline-replay.json"
PYTHON = sys.executable


class Issue183AcceptanceReplayTest(unittest.TestCase):
    def test_ac10_read_only_replay_fixture_hashes_home_refusal_ratchet_and_rows(self):
        """AC10: replay is reproducible, read-only, and ratchetable."""
        required = [FIXTURES / "store.sqlite", FIXTURES / "decisions.log", FIXTURES / "expected.json", BASELINE]
        for path in required:
            self.assertTrue(path.is_file(), f"missing replay artifact: {path}")

        with tempfile.TemporaryDirectory(prefix="zmem-183-ac10-") as td:
            scratch = Path(td)
            env = os.environ.copy()
            env.update(
                ZMEM_STORE=str(scratch / "isolated.sqlite"),
                ZMEM_DATA=str(scratch),
                ZMEM_MODELS_DIR=str(scratch / "missing-models"),
                ZMEM_MODEL_AUTODOWNLOAD="0",
                ZMEM_EMBED_PROFILE="fake",
            )
            out = scratch / "report.json"
            store_bytes = (FIXTURES / "store.sqlite").read_bytes()
            log_bytes = (FIXTURES / "decisions.log").read_bytes()
            before = hashlib.sha256(store_bytes).hexdigest()
            input_digest = hashlib.sha256(store_bytes + log_bytes).hexdigest()
            command = [
                PYTHON, str(EVALUATOR), "--store", str(FIXTURES / "store.sqlite"),
                "--log", str(FIXTURES / "decisions.log"), "--days", "30",
                "--json-out", str(out),
            ]
            run = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(before, hashlib.sha256((FIXTURES / "store.sqlite").read_bytes()).hexdigest())
            self.assertEqual(
                hashlib.sha256(log_bytes).hexdigest(),
                hashlib.sha256((FIXTURES / "decisions.log").read_bytes()).hexdigest(),
            )
            expected = (FIXTURES / "expected.json").read_bytes()
            self.assertEqual(out.read_bytes(), expected)
            report = json.loads(expected)
            self.assertEqual(report["store_sha256"], before)
            self.assertEqual(report["input_digest"], input_digest)
            self.assertEqual(
                list(report),
                ["schema_version", "input_digest", "store_sha256", "days", "rows", "aggregate", "input_metadata", "generated_at"],
            )
            self.assertEqual(len(report["rows"]), 8)
            self.assertEqual(
                [(row["lane"], row["moment"]) for row in report["rows"]],
                sorted((row["lane"], row["moment"]) for row in report["rows"]),
            )

            home_store = Path.home() / ".zmem" / "store.sqlite"
            refusal = subprocess.run(
                [PYTHON, str(EVALUATOR), "--store", str(home_store), "--log", str(FIXTURES / "decisions.log"), "--days", "30"],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(refusal.returncode, 2)
            self.assertEqual(refusal.stderr, "replay: --store must be a regular file outside the operator store\n")

            baseline = json.loads((BASELINE).read_text(encoding="utf-8"))
            candidate = copy.deepcopy(report)
            precision = float(candidate["aggregate"]["reference_precision"])
            baseline["aggregate"]["reference_precision"] = precision + 0.02
            self.assertAlmostEqual(
                precision - baseline["aggregate"]["reference_precision"], -0.02, places=12
            )
            bad_baseline = scratch / "bad-baseline.json"
            bad_baseline.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
            bad_out = scratch / "breached.json"
            breached = subprocess.run(
                command[:-2] + ["--compare-baseline", str(bad_baseline), "--fail-under", "precision_delta=-0.01", "--json-out", str(bad_out)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(breached.returncode, 1, breached.stderr)
            self.assertTrue(bad_out.is_file())
            json.loads(bad_out.read_text(encoding="utf-8"))
            passing_out = scratch / "passing.json"
            passing = subprocess.run(
                command[:-2] + ["--compare-baseline", str(BASELINE), "--fail-under", "precision_delta=-0.01", "--json-out", str(passing_out)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(passing.returncode, 0, passing.stderr)
            self.assertTrue(passing_out.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
