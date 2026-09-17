"""Regression tests for the launcher's legacy outer-timeout diagnostic."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_replay import _latest_log_timestamp, _parse_outer_timeout_diagnostic


EVALUATOR = ROOT / "scripts" / "eval_replay.py"
FIXTURES = ROOT / "tests" / "fixtures" / "replay"
PYTHON = sys.executable
NODE = shutil.which("node") or "node"


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
        "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_QUERY_CONTEXT",
        "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_MODELS_DIR",
        "ZMEM_PLUGIN_ROOT", "ZMEM_PROJECT_DIR", "ZMEM_ROOT", "ZMEM_TEST_NOW",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(scratch / "ambient.sqlite"),
        "ZMEM_DATA": str(scratch / "ambient-data"),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "PYTHONUTF8": "1",
    })
    return env


def _run(scratch: Path, log: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(EVALUATOR), "--store", str(FIXTURES / "store.sqlite"),
         "--log", str(log), "--days", "30", *extra],
        cwd=ROOT,
        env=_env(scratch),
        text=True,
        capture_output=True,
        timeout=60,
    )


def _writer_line(data_dir: Path) -> str:
    """Call the real launcher writer at a deterministic newer timestamp."""
    env = _env(data_dir.parent)
    for key in (
        "ZMEM_STORE", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        "ZMEM_PLUGIN_ROOT", "ZMEM_PROJECT_DIR", "ZMEM_ROOT",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_DATA": str(data_dir),
        "ZMEM_NAMESPACE": "project=timeout-proof",
        "ZMEM_SESSION": "timeout=session",
        "ZMEM_HOST": "claude",
    })
    script = (
        "Date.now = () => 1999999999 * 1000; "
        "const { appendOuterTimeoutDecision } = "
        "require('./hooks/zmem-launch.js'); "
        "appendOuterTimeoutDecision(process.env, 'pretool-recall', 'launcher', "
        "{timeout_ms: 3000, tier0_emitted: 1});"
    )
    run = subprocess.run(
        [NODE, "-e", script], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=30,
    )
    if run.returncode != 0:
        raise AssertionError(run.stderr or run.stdout)
    path = data_dir / "zmem-decisions.log"
    return path.read_text(encoding="utf-8")


class OuterTimeoutDiagnosticTest(unittest.TestCase):
    def test_exact_writer_line_is_excluded_but_digest_and_report_rows_remain(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-timeout-") as raw:
            scratch = Path(raw)
            writer_data = scratch / "writer-data"
            writer_data.mkdir()
            diagnostic = _writer_line(writer_data)
            canonical = (FIXTURES / "decisions.log").read_text(encoding="utf-8")
            log = scratch / "combined.log"
            log.write_text(canonical + diagnostic, encoding="utf-8", newline="\n")
            out = scratch / "report.json"

            run = _run(scratch, log, "--json-out", str(out))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("outer_timeout=1", diagnostic)
            self.assertTrue(
                run.stderr.startswith(
                    "replay: excluded diagnostics (not decisions): "
                    "outer_timeout=1 count=1\n"
                )
            )
            report = json.loads(out.read_text(encoding="utf-8"))
            expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
            for key, value in expected.items():
                if key != "input_digest":
                    self.assertEqual(report[key], value, key)
            self.assertEqual(report["rows"], expected["rows"])
            self.assertEqual(report["generated_at"], expected["generated_at"])
            self.assertEqual(report["input_metadata"]["parsed_rows"], 8)
            self.assertEqual(
                report["input_digest"],
                hashlib.sha256(
                    (FIXTURES / "store.sqlite").read_bytes() + log.read_bytes()
                ).hexdigest(),
            )

    def test_empty_and_equals_values_are_writer_compatible(self):
        line = (
            "[1999999999] zmem-hook status=silent reason=omitted "
            "outer_timeout=1 stage=launcher timeout_ms=3000 tier0_emitted=0 "
            "tier2_rows=0 hook= ns=project=foo sid= moment="
        )
        parsed = _parse_outer_timeout_diagnostic(line)
        self.assertTrue(parsed)
        self.assertFalse(
            _parse_outer_timeout_diagnostic(line.replace("stage=launcher", "stage=worker"))
        )

    def test_canonical_sid_containing_timeout_marker_is_not_excluded(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-timeout-sid-") as raw:
            scratch = Path(raw)
            canonical = (FIXTURES / "decisions.log").read_text(encoding="utf-8")
            log = scratch / "canonical.log"
            log.write_text(
                canonical.replace("sid=replay-claude", "sid=outer_timeout=token", 1),
                encoding="utf-8",
                newline="\n",
            )
            out = scratch / "report.json"
            run = _run(scratch, log, "--json-out", str(out))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("excluded diagnostics", run.stderr)
            self.assertIn("outer_timeout=", log.read_text(encoding="utf-8"))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["input_metadata"]["parsed_rows"], 8)

    def test_newer_diagnostic_does_not_change_replay_window(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-timeout-clock-") as raw:
            scratch = Path(raw)
            canonical = (FIXTURES / "decisions.log").read_text(encoding="utf-8")
            writer_data = scratch / "writer-data"
            writer_data.mkdir()
            log = scratch / "combined.log"
            log.write_text(canonical + _writer_line(writer_data), encoding="utf-8", newline="\n")
            baseline_log = scratch / "canonical.log"
            baseline_log.write_text(canonical, encoding="utf-8", newline="\n")
            combined_out = scratch / "combined.json"
            baseline_out = scratch / "baseline.json"

            combined = _run(scratch, log, "--json-out", str(combined_out))
            baseline = _run(scratch, baseline_log, "--json-out", str(baseline_out))
            self.assertEqual(combined.returncode, 0, combined.stderr)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertEqual(_latest_log_timestamp(log), 1780272000)
            combined_report = json.loads(combined_out.read_text(encoding="utf-8"))
            baseline_report = json.loads(baseline_out.read_text(encoding="utf-8"))
            self.assertEqual(combined_report["rows"], baseline_report["rows"])
            self.assertEqual(combined_report["aggregate"], baseline_report["aggregate"])
            self.assertEqual(combined_report["generated_at"], baseline_report["generated_at"])
            self.assertNotEqual(combined_report["input_digest"], baseline_report["input_digest"])

    def test_diagnostic_only_log_fails_without_clobbering_output(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-timeout-empty-") as raw:
            scratch = Path(raw)
            writer_data = scratch / "writer-data"
            writer_data.mkdir()
            log = scratch / "diagnostic-only.log"
            log.write_text(_writer_line(writer_data), encoding="utf-8", newline="\n")
            out = scratch / "report.json"
            sentinel = b"preserve me\n"
            out.write_bytes(sentinel)

            run = _run(scratch, log, "--json-out", str(out))
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stderr, "replay: decision log contains no valid rows\n")
            self.assertEqual(out.read_bytes(), sentinel)

    def test_malformed_timeout_lookalikes_fail_and_do_not_exclude(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-timeout-invalid-") as raw:
            scratch = Path(raw)
            canonical = (FIXTURES / "decisions.log").read_text(encoding="utf-8")
            valid = (
                "[1999999999] zmem-hook status=silent reason=omitted "
                "outer_timeout=1 stage=launcher timeout_ms=3000 tier0_emitted=1 "
                "tier2_rows=0 hook=pretool-recall ns=project=foo sid=sid moment=PreToolUse"
            )
            variants = {
                "reordered": valid.replace("timeout_ms=3000 tier0_emitted=1", "tier0_emitted=1 timeout_ms=3000"),
                "extra": valid + " extra=1",
                "bad-stage": valid.replace("stage=launcher", "stage=worker"),
                "bad-timeout": valid.replace("timeout_ms=3000", "timeout_ms=-1"),
                "bad-bool": valid.replace("tier0_emitted=1", "tier0_emitted=2"),
                "bad-tier2": valid.replace("tier2_rows=0", "tier2_rows=1"),
                "torn": valid.rsplit(" moment=", 1)[0],
            }
            for name, line in variants.items():
                with self.subTest(name=name):
                    log = scratch / f"{name}.log"
                    log.write_text(canonical + line + "\n", encoding="utf-8", newline="\n")
                    out = scratch / f"{name}.json"
                    sentinel = f"{name} sentinel\n".encode()
                    out.write_bytes(sentinel)
                    run = _run(scratch, log, "--json-out", str(out))
                    self.assertEqual(run.returncode, 2)
                    self.assertEqual(run.stderr, "replay: malformed decision row line 9\n")
                    self.assertEqual(out.read_bytes(), sentinel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
