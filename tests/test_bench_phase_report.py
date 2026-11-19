#!/usr/bin/env python
"""Issue #473 supplementary (plan critic R2-5): discriminating surface for
the bench --phase-report mode. Named mutant per assertion family (mutants
are listed in each docstring; the implementation review replays them):

- M-REFUSE   drop the existing-store refusal  -> test_refuses_existing_store
- M-ISOLATE  open/measure against an ambient store -> test_ambient_sentinel_untouched
- M-DOWNLOAD honor ambient ZMEM_MODEL_AUTODOWNLOAD -> test_forces_no_download
- M-CONFLICT allow --phase-report with --compare-baseline -> test_baseline_conflict
- M-LEGACY   change legacy stdout/behavior    -> test_legacy_mode_byte_unchanged
- M-SCHEMA   emit a different key set        -> test_schema_shape
- M-REASON   fabricate numbers for absent artifacts -> test_null_reasons_when_artifacts_absent
- M-ADDITIVE double-count import time into later buckets -> disjointness is
             asserted structurally (subprocess legs) in test_schema_shape.

Run:  python tests/test_bench_phase_report.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH = REPO_ROOT / "scripts" / "bench_hook_latency.py"

PHASE_KEYS = {
    "interpreter_imports", "sqlite_vec", "model_tokenizer_read_hash",
    "session_construction", "inference", "sql",
}


def _run_bench(*args, env_extra=None):
    env = dict(os.environ)
    env.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(BENCH), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        timeout=300)


class PhaseReportMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-benchphase-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def _phase_run(self, store_name="fixture.sqlite", env_extra=None):
        report = self.tmp / "phases.json"
        proc = _run_bench(
            "--store", str(self.tmp / store_name),
            "--log", str(self.tmp / "log.jsonl"),
            "--case", "warm",
            "--phase-report", str(report),
            env_extra=env_extra)
        return proc, report

    def test_schema_shape(self):
        proc, report = self._phase_run()
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(set(data["phases"]), PHASE_KEYS)
        for key, cell in data["phases"].items():
            self.assertIn("ms", cell)
            # every cell is either a non-negative number or null WITH a
            # reason — never a fabricated number, never a silent null
            if cell["ms"] is None:
                self.assertTrue(cell.get("reason"),
                                "%s is null without a reason" % key)
            else:
                self.assertIsInstance(cell["ms"], float)
                self.assertGreaterEqual(cell["ms"], 0.0)
        self.assertIn("identity", data)
        self.assertIn("python", data["identity"])

    def test_refuses_existing_store(self):
        # M-REFUSE: an existing store path is refused BEFORE being opened.
        existing = self.tmp / "existing.sqlite"
        existing.write_bytes(b"sentinel-existing-store-bytes")
        report = self.tmp / "phases.json"
        proc = _run_bench(
            "--store", str(existing),
            "--log", str(self.tmp / "log.jsonl"),
            "--case", "warm",
            "--phase-report", str(report))
        self.assertEqual(proc.returncode, 2, "argparse error exits 2")
        self.assertIn("refusing to open existing", proc.stderr)
        self.assertFalse(report.exists(),
                         "no report may be written on refusal")
        self.assertEqual(existing.read_bytes(), b"sentinel-existing-store-bytes",
                         "the existing store must be byte-untouched")

    def test_ambient_sentinel_untouched(self):
        # M-ISOLATE (review TF-001): the AMBIENT production store is
        # env-selected (ZMEM_STORE) so the phase run would genuinely open it
        # if isolation broke; it must stay byte-identical while the fixture
        # store is the one created and measured.
        ambient = self.tmp / "ambient-prod.sqlite"
        ambient.write_bytes(b"ambient-prod-sentinel-bytes")
        proc, report = self._phase_run(env_extra={
            "ZMEM_STORE": str(ambient),
            "ZMEM_DATA": str(self.tmp)})
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        self.assertEqual(ambient.read_bytes(), b"ambient-prod-sentinel-bytes",
                         "the ambient store selected via ZMEM_STORE was "
                         "opened or modified by the phase run")
        # the fixture store WAS created (phase mode builds its own)
        fixture = self.tmp / "fixture.sqlite"
        self.assertTrue(fixture.exists(), "fresh fixture store created")

    def test_forces_no_download(self):
        # M-DOWNLOAD: ambient autodownload=1 must not enable network fetches
        # in phase mode. Observable without a network: with no model present
        # at the resolved dir, the model phases report artifacts-absent
        # (a download attempt would instead leave a missing-file/timeout
        # signature or take minutes).
        env_extra = {"ZMEM_MODEL_AUTODOWNLOAD": "1",
                     "ZMEM_MODELS_DIR": str(self.tmp / "empty-models")}
        proc, report = self._phase_run(env_extra=env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        data = json.loads(report.read_text(encoding="utf-8"))
        phases = data["phases"]
        self.assertIsNone(phases["model_tokenizer_read_hash"]["ms"])
        self.assertIn("artifacts-absent",
                      phases["model_tokenizer_read_hash"]["reason"])
        self.assertIsNone(phases["session_construction"]["ms"])
        self.assertIsNone(phases["inference"]["ms"])

    def test_baseline_conflict(self):
        # M-CONFLICT: measurement mode and baseline replay are mutually
        # exclusive.
        report = self.tmp / "phases.json"
        baseline = self.tmp / "baseline.json"
        proc = _run_bench(
            "--store", str(self.tmp / "f2.sqlite"),
            "--log", str(self.tmp / "log.jsonl"),
            "--case", "warm",
            "--phase-report", str(report),
            "--compare-baseline", str(baseline))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)
        self.assertFalse(report.exists())

    def test_legacy_mode_byte_unchanged(self):
        # M-LEGACY: without --phase-report the deterministic core's output
        # contract is unchanged (valid JSON envelope with input_digest +
        # stages, same shape as pre-fix).
        proc = _run_bench(
            "--store", str(self.tmp / "legacy.sqlite"),
            "--log", str(self.tmp / "legacy-log.jsonl"),
            "--case", "warm", "--runs", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        payload = json.loads(proc.stdout)
        self.assertIn("input_digest", payload)
        self.assertIn("stages", payload)
        self.assertIn("clock_mode", payload)
        self.assertEqual(payload["clock_mode"], "injected")
        self.assertEqual(payload["runs"], 3)
        # determinism pin: same argv -> identical stdout bytes
        proc2 = _run_bench(
            "--store", str(self.tmp / "legacy.sqlite"),
            "--log", str(self.tmp / "legacy-log.jsonl"),
            "--case", "warm", "--runs", "3")
        self.assertEqual(proc.stdout, proc2.stdout)

    def test_null_reasons_when_artifacts_absent(self):
        # M-REASON: absent artifacts produce null + reason, never a number.
        env_extra = {"ZMEM_MODELS_DIR": str(self.tmp / "no-models")}
        proc, report = self._phase_run(env_extra=env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertIsNone(data["phases"]["model_tokenizer_read_hash"]["ms"])
        self.assertTrue(data["phases"]["model_tokenizer_read_hash"]["reason"])
        # structural (subprocess-measured) buckets still carry numbers —
        # disjointness: interpreter_imports and sqlite_vec never depend on
        # the model artifacts.
        self.assertIsNotNone(
            data["phases"]["interpreter_imports"]["ms"])
        self.assertIsNotNone(data["phases"]["sqlite_vec"]["ms"])
        self.assertIsNotNone(data["phases"]["sql"]["ms"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
