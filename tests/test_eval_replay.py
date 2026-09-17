"""Fixed-byte and safety tests for the #155 replay evaluator."""

from __future__ import annotations

import copy
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from tests.test_issue183_acceptance_observations import (
    BASE_TS,
    _build_fixture,
    _decision,
    _failure,
)


ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = ROOT / "scripts" / "eval_replay.py"
FIXTURES = ROOT / "tests" / "fixtures" / "replay"
BASELINE = ROOT / "eval" / "baseline-replay.json"
PYTHON = sys.executable


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
        "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_QUERY_CONTEXT",
        "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_MODELS_DIR",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(scratch / "ambient.sqlite"),
        "ZMEM_DATA": str(scratch),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "PYTHONUTF8": "1",
    })
    return env


class ReplayFixtureTest(unittest.TestCase):
    def _run(self, scratch: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(EVALUATOR), "--store", str(FIXTURES / "store.sqlite"),
             "--log", str(FIXTURES / "decisions.log"), "--days", "30", *extra],
            cwd=ROOT, env=_env(scratch), text=True, capture_output=True, timeout=60,
        )

    def test_fixture_output_is_byte_identical(self):
        expected = FIXTURES / "expected.json"
        for path in (FIXTURES / "store.sqlite", FIXTURES / "decisions.log", expected, BASELINE):
            self.assertTrue(path.is_file(), path)
        with tempfile.TemporaryDirectory(prefix="zmem-replay-test-") as raw:
            scratch = Path(raw)
            out = scratch / "report.json"
            run = self._run(scratch, "--json-out", str(out))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(out.read_bytes(), expected.read_bytes())

    def test_input_digest_and_store_sha_are_stable(self):
        store = FIXTURES / "store.sqlite"
        log = FIXTURES / "decisions.log"
        before = store.read_bytes()
        with tempfile.TemporaryDirectory(prefix="zmem-replay-test-") as raw:
            out = Path(raw) / "report.json"
            run = self._run(Path(raw), "--json-out", str(out))
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["store_sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(
                report["input_digest"],
                hashlib.sha256(before + log.read_bytes()).hexdigest(),
            )
            self.assertEqual(before, store.read_bytes())
            self.assertEqual(len(report["rows"]), 8)
            self.assertEqual(
                [(row["lane"], row["moment"]) for row in report["rows"]],
                sorted((row["lane"], row["moment"]) for row in report["rows"]),
            )


class ReplayRefusalTest(unittest.TestCase):
    def test_home_store_returns_exit_two(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-test-") as raw:
            scratch = Path(raw)
            home_store = Path.home() / ".zmem" / "store.sqlite"
            run = subprocess.run(
                [PYTHON, str(EVALUATOR), "--store", str(home_store),
                 "--log", str(FIXTURES / "decisions.log"), "--days", "30"],
                cwd=ROOT, env=_env(scratch), text=True, capture_output=True,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(
                run.stderr,
                "replay: --store must be a regular file outside the operator store\n",
            )

    def test_legacy_plugin_store_is_protected_before_isolation(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-home-") as raw:
            home = Path(raw)
            legacy = home / ".zcode" / "cli" / "plugins" / "data" / "old-zmem-plugin"
            legacy.mkdir(parents=True)
            store = legacy / "store.sqlite"
            store.write_bytes(b"operator")
            with patch.dict(
                os.environ,
                {"HOME": str(home), "USERPROFILE": str(home)},
                clear=True,
            ):
                from scripts.eval_replay import _operator_store_candidates

                candidates = _operator_store_candidates()
            self.assertIn(store.resolve(), candidates)

    def test_json_output_operator_alias_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-output-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            operator_output = scratch / "ambient.sqlite"
            run = subprocess.run(
                [
                    PYTHON,
                    str(EVALUATOR),
                    "--store",
                    str(fixture["store"]),
                    "--log",
                    str(fixture["log"]),
                    "--days",
                    "30",
                    "--json-out",
                    str(operator_output),
                ],
                cwd=ROOT,
                env=_env(scratch),
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("--json-out", run.stderr)
            self.assertFalse(operator_output.exists())

    def test_log_transcript_and_baseline_operator_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-path-alias-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            operator_path = scratch / "ambient.sqlite"
            cases = (
                ("log", fixture["log"].read_bytes(), "input file is inside the operator store"),
                ("transcript", b"{}\n", "input file is inside the operator store"),
                ("baseline", b"{}\n", "baseline must be distinct from inputs/output"),
            )
            for kind, data, message in cases:
                operator_path.write_bytes(data)
                arguments = ["--store", str(fixture["store"])]
                if kind == "log":
                    arguments += ["--log", str(operator_path)]
                else:
                    arguments += ["--log", str(fixture["log"])]
                    arguments += (["--transcript", str(operator_path)] if kind == "transcript"
                                  else ["--compare-baseline", str(operator_path)])
                run = subprocess.run(
                    [PYTHON, str(EVALUATOR), *arguments, "--days", "30"],
                    cwd=ROOT,
                    env=_env(scratch),
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(run.returncode, 2, kind)
                self.assertIn(message, run.stderr, kind)


class ReplayInputContractTest(unittest.TestCase):
    def _run(self, scratch: Path, store: Path, log: Path, *extra: str):
        return subprocess.run(
            [PYTHON, str(EVALUATOR), "--store", str(store), "--log", str(log),
             "--days", "30", *extra],
            cwd=ROOT,
            env=_env(scratch),
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_missing_store_or_log_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-missing-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            missing_store = scratch / "missing-store.sqlite"
            missing_log = scratch / "missing-decisions.log"
            for store, log in ((missing_store, fixture["log"]), (fixture["store"], missing_log)):
                out = scratch / f"{store.stem}-{log.stem}.json"
                sentinel = b"preserve on missing input\n"
                out.write_bytes(sentinel)
                run = self._run(scratch, store, log, "--json-out", str(out))
                self.assertEqual(run.returncode, 2)
                self.assertEqual(out.read_bytes(), sentinel)

    def test_hardlink_alias_is_rejected_before_report_write(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-alias-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            alias = scratch / "store-hardlink.sqlite"
            try:
                os.link(fixture["store"], alias)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")
            run = self._run(
                scratch,
                fixture["store"],
                fixture["log"],
                "--json-out",
                str(alias),
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("--json-out must not overwrite an input", run.stderr)
            self.assertEqual(alias.read_bytes(), fixture["store"].read_bytes())

    def test_mixed_nonempty_versions_are_rejected_before_projection(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-versions-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            log = scratch / "mixed.log"
            lines = fixture["log"].read_text(encoding="utf-8").splitlines()
            lines[0] = lines[0].replace("ver=0.42.0", "ver=0.43.0")
            log.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            out = scratch / "mixed-output.json"
            sentinel = b"preserve on version refusal\n"
            out.write_bytes(sentinel)
            run = self._run(scratch, fixture["store"], log, "--json-out", str(out))
            self.assertEqual(run.returncode, 2)
            self.assertIn("mixed nonempty decision-log versions", run.stderr)
            self.assertEqual(out.read_bytes(), sentinel)

    def test_nan_baseline_metric_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-nan-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            baseline = scratch / "nan-baseline.json"
            baseline.write_text(json.dumps({
                "schema_version": 1,
                "input_metadata": {"version": "0.42.0"},
                "aggregate": {"reference_precision": "NaN", "miss_rate": 0},
            }), encoding="utf-8")
            out = scratch / "nan-output.json"
            sentinel = b"preserve on NaN refusal\n"
            out.write_bytes(sentinel)
            run = self._run(
                scratch,
                fixture["store"],
                fixture["log"],
                "--compare-baseline",
                str(baseline),
                "--fail-under",
                "precision_delta=-0.01",
                "--json-out",
                str(out),
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("invalid baseline metrics", run.stderr)
            self.assertEqual(out.read_bytes(), sentinel)

    def test_noncovered_lane_is_diagnosed_without_contaminating_eight_rows(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-lane-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            log = scratch / "excluded-lane.log"
            lines = fixture["log"].read_text(encoding="utf-8").splitlines()
            lines[0] = lines[0].replace("lane=claude", "lane=codex")
            log.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            out = scratch / "lane-output.json"
            run = self._run(scratch, fixture["store"], log, "--json-out", str(out))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("excluded lanes (valid in-window rows): codex=1", run.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(report["rows"]), 8)
            self.assertEqual(report["input_metadata"]["parsed_rows"], 8)

    def test_nonempty_wal_is_refused_before_evaluation(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-wal-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            snapshot = scratch / "snapshot.sqlite"
            snapshot.write_bytes(fixture["store"].read_bytes())
            Path(str(snapshot) + "-wal").write_bytes(b"live")
            run = self._run(scratch, snapshot, fixture["log"])
            self.assertEqual(run.returncode, 2)
            self.assertIn("live WAL-backed database", run.stderr)


class ReplayRatchetTest(unittest.TestCase):
    def test_precision_delta_breach_and_pass(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-test-") as raw:
            scratch = Path(raw)
            baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
            changed = copy.deepcopy(baseline)
            changed["aggregate"]["reference_precision"] += 0.02
            bad = scratch / "bad-baseline.json"
            bad.write_text(json.dumps(changed), encoding="utf-8")
            out = scratch / "breached.json"
            command = [PYTHON, str(EVALUATOR), "--store", str(FIXTURES / "store.sqlite"),
                       "--log", str(FIXTURES / "decisions.log"), "--days", "30",
                       "--compare-baseline", str(bad), "--fail-under", "precision_delta=-0.01",
                       "--json-out", str(out)]
            run = subprocess.run(command, cwd=ROOT, env=_env(scratch), text=True, capture_output=True)
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertTrue(out.is_file())
            json.loads(out.read_text(encoding="utf-8"))
            passing = subprocess.run(
                command[:command.index("--compare-baseline")] + ["--compare-baseline", str(BASELINE),
                "--fail-under", "precision_delta=-0.01", "--json-out", str(scratch / "pass.json")],
                cwd=ROOT, env=_env(scratch), text=True, capture_output=True,
            )
            self.assertEqual(passing.returncode, 0, passing.stderr)


class ReplayBucketJoinRegressionTest(unittest.TestCase):
    def test_same_bucket_later_delivery_surfaces_one_failure_not_one_miss(self):
        """The bucket audit must not invoke miss attribution per decision."""
        with tempfile.TemporaryDirectory(prefix="zmem-replay-join-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            memory_id = str(fixture["surfaced"])
            sid = "same/bucket/session-183"
            log = scratch / "same-bucket.log"
            log.write_text(
                "".join(
                    (
                        _decision(
                            BASE_TS + 100,
                            sid,
                            [],
                            [memory_id],
                            reason="omitted",
                            status="silent",
                        ),
                        _decision(BASE_TS + 150, sid, [memory_id], [memory_id]),
                    )
                ),
                encoding="utf-8",
                newline="\n",
            )
            transcript = scratch / "same-bucket.jsonl"
            transcript.write_text(
                json.dumps(_failure(sid, 100, "same-bucket-call", "git stash pop")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            out = scratch / "report.json"
            run = subprocess.run(
                [
                    PYTHON,
                    str(EVALUATOR),
                    "--store",
                    str(fixture["store"]),
                    "--log",
                    str(log),
                    "--transcript",
                    str(transcript),
                    "--days",
                    "30",
                    "--json-out",
                    str(out),
                ],
                cwd=ROOT,
                env=_env(scratch),
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            row = next(
                row
                for row in report["rows"]
                if row["lane"] == "claude" and row["moment"] == "user_prompt"
            )
            self.assertEqual(row["counts"]["decisions"], 2)
            self.assertEqual(row["counts"]["candidates"], 2)
            self.assertEqual(row["counts"]["delivered"], 1)
            self.assertEqual(row["counts"]["miss"], 0)
            self.assertEqual(row["miss_rate"], 0)


class ReplayObservationInputTest(unittest.TestCase):
    def test_replay_clock_and_recall_knobs_are_ambient_invariant(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-clock-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)

            def invoke(name: str, overrides: dict[str, str]) -> bytes:
                out = scratch / name
                env = _env(scratch)
                env.update(overrides)
                run = subprocess.run(
                    [
                        PYTHON,
                        str(EVALUATOR),
                        "--store",
                        str(fixture["store"]),
                        "--log",
                        str(fixture["log"]),
                        "--transcript",
                        str(fixture["transcript"]),
                        "--transcript",
                        str(fixture["extra"]),
                        "--days",
                        "30",
                        "--json-out",
                        str(out),
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                return out.read_bytes()

            baseline = invoke(
                "baseline.json",
                {
                    "ZMEM_TEST_NOW": "1999-01-01T00:00:00Z",
                    "ZMEM_MMR_LAMBDA": "0.01",
                    "ZMEM_ARM_CAP_FTS": "0",
                    "ZMEM_ARM_CAP_VEC": "0",
                    "ZMEM_GRAPH_SEED": "0",
                },
            )
            changed = invoke(
                "changed.json",
                {
                    "ZMEM_TEST_NOW": "2099-01-01T00:00:00Z",
                    "ZMEM_MMR_LAMBDA": "0.99",
                    "ZMEM_ARM_CAP_FTS": "1000",
                    "ZMEM_ARM_CAP_VEC": "1000",
                    "ZMEM_GRAPH_SEED": "1",
                },
            )
            self.assertEqual(baseline, changed)

    def test_timestamp_only_record_does_not_suppress_unavailable_warning(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-empty-observation-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            transcript = scratch / "timestamp-only.jsonl"
            transcript.write_text(
                json.dumps({
                    "session_id": "known-session",
                    "timestamp": "2026-09-10T00:00:01Z",
                }) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            out = scratch / "report.json"
            run = subprocess.run(
                [
                    PYTHON,
                    str(EVALUATOR),
                    "--store",
                    str(fixture["store"]),
                    "--log",
                    str(fixture["log"]),
                    "--transcript",
                    str(transcript),
                    "--days",
                    "30",
                    "--json-out",
                    str(out),
                ],
                cwd=ROOT,
                env=_env(scratch),
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("no usable miss/reference observations", run.stderr)

    def test_oversized_transcript_rejects_without_overwriting_output(self):
        with tempfile.TemporaryDirectory(prefix="zmem-replay-transcript-limit-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            transcript = scratch / "too-large.jsonl"
            from scripts.eval_replay import MAX_TRANSCRIPT_BYTES

            transcript.write_bytes(b"x" * (MAX_TRANSCRIPT_BYTES + 1))
            out = scratch / "report.json"
            sentinel = b"preserve me\n"
            out.write_bytes(sentinel)
            run = subprocess.run(
                [
                    PYTHON,
                    str(EVALUATOR),
                    "--store",
                    str(fixture["store"]),
                    "--log",
                    str(fixture["log"]),
                    "--transcript",
                    str(transcript),
                    "--days",
                    "30",
                    "--json-out",
                    str(out),
                ],
                cwd=ROOT,
                env=_env(scratch),
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(out.read_bytes(), sentinel)


class ReplayMeasurementErrorTest(unittest.TestCase):
    def test_shared_join_error_is_not_zero_filled(self):
        from scripts.eval_replay import ReplayError, _bucket_observations

        scripts = str(ROOT / "skills" / "memory" / "scripts")
        saved = sys.path[:]
        try:
            sys.path.insert(0, scripts)
            import storelib.miss_rate as miss_rate
        finally:
            sys.path[:] = saved
        conn = sqlite3.connect(":memory:")
        try:
            with patch.object(miss_rate, "run_miss_report", return_value={"error": "read failed"}):
                with self.assertRaisesRegex(ReplayError, "observation measurement unavailable"):
                    _bucket_observations([], [], conn, [], Path(tempfile.gettempdir()))
        finally:
            conn.close()

    def test_false_reference_helper_degraded_is_not_zero_filled(self):
        from scripts.eval_replay import ReplayError, _bucket_observations

        scripts = str(ROOT / "skills" / "memory" / "scripts")
        saved = sys.path[:]
        try:
            sys.path.insert(0, scripts)
            import storelib.false_inject as false_inject
        finally:
            sys.path[:] = saved
        conn = sqlite3.connect(":memory:")
        bucket = [{
            "sid": "known-session",
            "ts": 100,
            "ids": ["memory-id"],
            "all": ["memory-id"],
        }]
        try:
            with patch.object(false_inject, "_read_prompt_events", return_value=[
                (200, "git stash pop", "known-session"),
            ]), patch.object(
                false_inject,
                "build_false_injection_report",
                return_value={"degraded": True},
            ):
                with self.assertRaisesRegex(ReplayError, "reference measurement unavailable"):
                    _bucket_observations(bucket, [], conn, [], Path(tempfile.gettempdir()))
        finally:
            conn.close()


class ReplaySchemaTest(unittest.TestCase):
    def test_baseline_has_required_keys(self):
        report = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(
            list(report),
            ["schema_version", "input_digest", "store_sha256", "days", "rows",
             "aggregate", "input_metadata", "generated_at"],
        )
        self.assertEqual(len(report["rows"]), 8)
        self.assertEqual(report["input_metadata"]["parsed_rows"], 8)
        self.assertNotIn(str(ROOT), json.dumps(report))

    def test_committed_decision_fixture_and_metrics_have_independent_guards(self):
        """Pin the small canonical corpus independently of the maintainer generator."""
        lines = (FIXTURES / "decisions.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 8)
        self.assertTrue(all("namespace=" not in line for line in lines))
        expected_keys = {
            (lane, moment)
            for lane in ("claude", "hermes-provider")
            for moment in ("session_start", "user_prompt", "pretool", "precompact")
        }
        parsed_log = []
        log_counts = {}
        log_timings = {}
        for line in lines:
            fields = {
                token.split("=", 1)[0]: token.split("=", 1)[1]
                for token in line.split()
                if "=" in token
            }
            key = (fields.get("lane"), fields.get("moment"))
            parsed_log.append(key)
            ids = ast.literal_eval(fields["ids"])
            all_ids = ast.literal_eval(fields["all"])
            self.assertIsInstance(ids, list)
            self.assertIsInstance(all_ids, list)
            counts = log_counts.setdefault(key, [0, 0, 0, 0, 0])
            counts[0] += 1
            counts[1] += len(all_ids)
            counts[2] += len(ids)
            counts[3] += fields.get("reason") == "empty-pool"
            counts[4] += fields.get("reason") == "already-delivered" and bool(all_ids)
            log_timings.setdefault(key, []).append(int(fields["t_ms"]))
        self.assertEqual(set(parsed_log), expected_keys)
        self.assertEqual(len(parsed_log), len(set(parsed_log)))
        report = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        rows = {(row["lane"], row["moment"]): row for row in report["rows"]}
        self.assertEqual(set(rows), expected_keys)
        expected_counts = {
            "session_start": (1, 1, 1, 0, 0),
            "user_prompt": (1, 1, 0, 0, 1),
            "pretool": (1, 0, 0, 1, 0),
            "precompact": (1, 1, 1, 0, 0),
        }
        expected_latency = {
            "session_start": 10,
            "user_prompt": 20,
            "pretool": 30,
            "precompact": 40,
        }
        for (lane, moment), row in rows.items():
            decisions, candidates, delivered, empty_pool, already_delivered = expected_counts[moment]
            self.assertEqual(
                tuple(log_counts[(lane, moment)]),
                (decisions, candidates, delivered, empty_pool, already_delivered),
                f"log {lane}/{moment}",
            )
            self.assertEqual(
                log_timings[(lane, moment)], [expected_latency[moment]],
                f"timing log {lane}/{moment}",
            )
            counts = row["counts"]
            self.assertEqual(
                (counts["decisions"], counts["candidates"], counts["delivered"],
                 counts["empty_pool"], counts["already_delivered"]),
                (decisions, candidates, delivered, empty_pool, already_delivered),
                f"{lane}/{moment}",
            )
            self.assertEqual(row["t_ms"], {"p50": expected_latency[moment], "p95": expected_latency[moment]})
            self.assertEqual(counts["reference_checked"], 0)
            self.assertEqual(counts["miss"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
