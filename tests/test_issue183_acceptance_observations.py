"""Pre-production acceptance checks for the #183 replay observation surface.

The fixture is deliberately made from the real store CLI and the real decision
log/transcript shapes consumed by ``storelib.miss_rate`` and
``storelib.false_inject``.  It is independent of any future evaluator helper:
on the pre-implementation checkout the missing evaluator is reported as a
specific ``NEW-SURFACE`` failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
STORE_PY = ROOT / "skills" / "memory" / "scripts" / "store.py"
EVALUATOR = ROOT / "scripts" / "eval_replay.py"
PYTHON = sys.executable
BASE = datetime(2026, 9, 10, tzinfo=timezone.utc)
BASE_TS = int(BASE.timestamp())

_AMBIENT = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_SESSION", "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_SESSION_ID",
    "ZCODE_SESSION_ID",
)


def _env(scratch: Path, store: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in _AMBIENT:
        env.pop(key, None)
    env.update(
        {
            # Every ambient path is throwaway, even though the evaluator also
            # receives explicit --store/--log/--transcript paths.
            "ZMEM_STORE": str(store or scratch / "ambient.sqlite"),
            "ZMEM_DATA": str(scratch),
            "ZMEM_HOME": str(scratch / "home"),
            "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
            "ZMEM_NAMESPACE": "project:issue183-observations",
            "ZMEM_HOST": "claude",
            "ZMEM_SESSION": "ambient-session",
            "ZMEM_QUERY_CONTEXT": "1",
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_TEST_NOW": "2026-09-11T00:00:00Z",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _run_store(scratch: Path, store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        cwd=ROOT,
        env=_env(scratch, store),
        text=True,
        capture_output=True,
        timeout=30,
    )


def _add_memory(scratch: Path, store: Path, content: str) -> str:
    result = _run_store(
        scratch,
        store,
        "add",
        "--namespace",
        "project:issue183-observations",
        "--type",
        "lesson",
        "--content",
        content,
        "--signal",
        "test",
        "--json",
    )
    if result.returncode != 0:
        raise AssertionError(f"store add failed: {result.stderr}")
    for line in reversed(result.stdout.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("id"):
            return str(obj["id"])
    raise AssertionError(f"store add emitted no id: {result.stdout!r}")


def _iso(offset: int) -> str:
    return (BASE + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def _decision(
    ts: int,
    sid: str,
    ids: list[str],
    all_ids: list[str],
    *,
    reason: str = "injected",
    status: str = "injected",
) -> str:
    return (
        f"[{ts}] zmem-hook status={status} reason={reason} ids={ids!r} "
        f"all={all_ids!r} tokens=1/1500 sid={sid} moment=user_prompt "
        "lane=claude ver=0.42.0 t_ms=10\n"
    )


def _failure(session: str, offset: int, call_id: str, operation: str) -> dict:
    return {
        "sessionId": session,
        "timestamp": _iso(offset),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Bash",
                    "input": {"command": operation},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": True,
                    "content": "Error: exit code 1",
                },
            ]
        },
    }


def _prompt(session: str, offset: int, text: str, *, camel: bool = False) -> dict:
    return {
        "sessionId" if camel else "session_id": session,
        "timestamp": _iso(offset),
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _build_fixture(scratch: Path) -> dict[str, Path | str]:
    store = scratch / "store.sqlite"
    initialized = _run_store(scratch, store, "init")
    if initialized.returncode != 0:
        raise AssertionError(initialized.stderr)

    surfaced = _add_memory(scratch, store, "git stash pop surfaced failure memory")
    missed = _add_memory(scratch, store, "git reset missed failure memory")
    referenced = _add_memory(scratch, store, "pytest referenced delivery memory")
    unreferenced = _add_memory(scratch, store, "cargo build unreferenced delivery memory")

    sessions = {
        "surfaced": "failure-surfaced-183",
        "missed": "failure-missed-183",
        "referenced": "delivery-referenced-183",
        "unreferenced": "delivery-unreferenced-183",
        "other": "other-session-183",
        "none": "no-observation-183",
    }
    log = scratch / "decisions.log"
    log.write_text(
        "".join(
            [
                _decision(BASE_TS + 100, sessions["surfaced"], [surfaced], [surfaced]),
                # A real candidate exists but was not delivered: this is the
                # known missed-failure arm, not an id-existence shortcut.
                _decision(
                    BASE_TS + 200,
                    sessions["missed"],
                    [],
                    [missed],
                    reason="omitted",
                    status="silent",
                ),
                _decision(BASE_TS + 300, sessions["referenced"], [referenced], [referenced]),
                _decision(BASE_TS + 400, sessions["unreferenced"], [unreferenced], [unreferenced]),
                # The later reference belongs to delivery-referenced-183, so
                # this same id in another session must remain unchecked.
                _decision(BASE_TS + 500, sessions["other"], [referenced], [referenced]),
                _decision(BASE_TS + 600, sessions["none"], [unreferenced], [unreferenced]),
                _decision(
                    BASE_TS + 700,
                    "empty-pool-183",
                    [],
                    [],
                    reason="empty-pool",
                    status="silent",
                ),
                _decision(
                    BASE_TS + 800,
                    "already-delivered-183",
                    [],
                    [surfaced],
                    reason="already-delivered",
                    status="silent",
                ),
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    transcript = scratch / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(obj, ensure_ascii=False)
            for obj in [
                _failure(sessions["surfaced"], 100, "failure-call-1", "git stash pop"),
                _failure(sessions["missed"], 200, "failure-call-2", "git reset --soft"),
                _prompt(
                    sessions["referenced"],
                    350,
                    f"I used the delivered memory {referenced} to continue.",
                    camel=True,
                ),
                _prompt(
                    sessions["unreferenced"],
                    450,
                    "This is an unrelated observation with no recalled memory.",
                ),
                _prompt(
                    sessions["referenced"],
                    550,
                    f"A later prompt still names {referenced}.",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    # A second, repeatable transcript input is valid JSONL but does not belong
    # to any delivery session. It makes the digest contract cover every input.
    extra = scratch / "transcript-extra.jsonl"
    extra.write_text(
        json.dumps(_prompt("extra-session-183", 900, "digest-only observation")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "store": store,
        "log": log,
        "transcript": transcript,
        "extra": extra,
        "surfaced": surfaced,
        "missed": missed,
        "referenced": referenced,
        "unreferenced": unreferenced,
    }


class Issue183AcceptanceObservationsTest(unittest.TestCase):
    def _require_surface(self) -> None:
        self.assertTrue(
            EVALUATOR.is_file(),
            f"NEW-SURFACE: missing evaluator {EVALUATOR}",
        )

    def _invoke(
        self,
        scratch: Path,
        store: Path,
        log: Path,
        transcripts: list[Path] = (),
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        self._require_surface()
        out = scratch / ("report-" + str(len(list(scratch.glob("report-*.json")))) + ".json")
        command = [
            PYTHON,
            str(EVALUATOR),
            "--store",
            str(store),
            "--log",
            str(log),
            "--days",
            "30",
            "--json-out",
            str(out),
        ]
        for transcript in transcripts:
            command.extend(("--transcript", str(transcript)))
        run = subprocess.run(
            command,
            cwd=ROOT,
            env=_env(scratch),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(out.is_file(), "evaluator did not write --json-out")
        try:
            report = json.loads(out.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.fail(f"evaluator output is not JSON: {exc}; stderr={run.stderr!r}")
        return report, run

    @staticmethod
    def _stable(report: dict) -> dict:
        return dict(report)

    def test_evaluator_is_an_explicit_new_surface(self):
        self._require_surface()

    def test_hand_derived_observation_oracle_and_schema(self):
        self._require_surface()
        with tempfile.TemporaryDirectory(prefix="zmem-183-observations-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            store, log = Path(fixture["store"]), Path(fixture["log"])
            report, run = self._invoke(
                scratch,
                store,
                log,
                [Path(fixture["transcript"]), Path(fixture["extra"])],
            )

            self.assertEqual(
                list(report),
                [
                    "schema_version",
                    "input_digest",
                    "store_sha256",
                    "days",
                    "rows",
                    "aggregate",
                    "input_metadata",
                    "usable_observation",
                    "generated_at",
                ],
            )
            self.assertEqual(report["days"], 30)
            self.assertEqual(report["store_sha256"], hashlib.sha256(store.read_bytes()).hexdigest())
            self.assertEqual(len(report["rows"]), 8)
            self.assertEqual(
                [(row["lane"], row["moment"]) for row in report["rows"]],
                sorted(
                    (lane, moment)
                    for lane in ("claude", "hermes-provider")
                    for moment in ("session_start", "user_prompt", "pretool", "precompact")
                ),
            )

            row = next(
                row
                for row in report["rows"]
                if row["lane"] == "claude" and row["moment"] == "user_prompt"
            )
            self.assertEqual(row["counts"], {
                "decisions": 8,
                "candidates": 7,
                "delivered": 5,
                "reference_checked": 2,
                "miss": 1,
                "empty_pool": 1,
                "already_delivered": 1,
            })
            self.assertEqual(
                {key: row[key] for key in (
                    "reference_precision", "false_injection_rate", "miss_rate",
                    "empty_pool_rate", "already_delivered_rate",
                )},
                {
                    "reference_precision": 0.5,
                    "false_injection_rate": 0.5,
                    "miss_rate": 0.5,
                    "empty_pool_rate": 0.125,
                    "already_delivered_rate": 1 / 7,
                },
            )
            self.assertEqual(row["t_ms"]["p50"], 10)
            self.assertEqual(row["t_ms"]["p95"], 10)
            for zero in report["rows"]:
                if zero is row:
                    continue
                self.assertTrue(all(value == 0 for value in zero["counts"].values()))

            self.assertEqual(report["aggregate"]["reference_precision"], 0.5)
            self.assertEqual(report["aggregate"]["miss_rate"], 0.5)

    def test_no_observation_is_zero_denominator_with_diagnostic(self):
        self._require_surface()
        with tempfile.TemporaryDirectory(prefix="zmem-183-no-observation-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            report, run = self._invoke(
                scratch,
                Path(fixture["store"]),
                Path(fixture["log"]),
                [],
            )
            row = next(
                row
                for row in report["rows"]
                if row["lane"] == "claude" and row["moment"] == "user_prompt"
            )
            self.assertEqual(row["counts"]["reference_checked"], 0)
            self.assertEqual(row["reference_precision"], 0)
            self.assertEqual(row["false_injection_rate"], 0)
            self.assertEqual(row["miss_rate"], 0)
            self.assertEqual(row["empty_pool_rate"], 1 / 8)
            self.assertEqual(row["already_delivered_rate"], 1 / 7)
            diagnostic = run.stderr.lower()
            self.assertIn("reference", diagnostic)
            self.assertIn("unavailable", diagnostic)

    def test_digest_covers_each_input_and_unlisted_rotation_or_ops_are_ignored(self):
        self._require_surface()
        with tempfile.TemporaryDirectory(prefix="zmem-183-integrity-") as raw:
            scratch = Path(raw)
            fixture = _build_fixture(scratch)
            store, log = Path(fixture["store"]), Path(fixture["log"])
            transcript, extra = Path(fixture["transcript"]), Path(fixture["extra"])
            before = {
                path: path.read_bytes()
                for path in (store, log, transcript, extra)
            }
            base, _ = self._invoke(scratch, store, log, [transcript, extra])

            # These are deliberately not supplied to the evaluator. A valid
            # rotated decision would change counts if log globbing leaked in;
            # an ops-ring event would turn the unreferenced delivery into a
            # referenced one if ambient ring reads leaked in.
            (Path(str(log) + ".1")).write_text(
                _decision(BASE_TS + 50, "ambient-sibling", [str(fixture["surfaced"])], [str(fixture["surfaced"])]),
                encoding="utf-8",
                newline="\n",
            )
            ops = scratch / "ops"
            ops.mkdir()
            sid = "delivery-unreferenced-183"
            scripts = str(ROOT / "skills" / "memory" / "scripts")
            saved_path = sys.path[:]
            try:
                with patch.dict(os.environ, _env(scratch), clear=True):
                    sys.path.insert(0, scripts)
                    from storelib.ops_tokens import _ring_path  # noqa: PLC0415

                    ring = Path(_ring_path(str(scratch), sid))
            finally:
                sys.path[:] = saved_path
            ring.parent.mkdir(parents=True, exist_ok=True)
            ring.write_text(json.dumps({"ts": BASE_TS + 450, "ops": "cargo build"}) + "\n", encoding="utf-8")
            ambient, _ = self._invoke(scratch, store, log, [transcript, extra])
            self.assertEqual(self._stable(base), self._stable(ambient))

            mutated_log = scratch / "mutated.log"
            mutated_log.write_bytes(log.read_bytes().replace(b"t_ms=10", b"t_ms=11", 1))
            changed_log, _ = self._invoke(scratch, store, mutated_log, [transcript, extra])
            self.assertNotEqual(base["input_digest"], changed_log["input_digest"])

            mutated_transcript = scratch / "mutated-transcript.jsonl"
            mutated_transcript.write_bytes(
                transcript.read_bytes().replace(b"used the delivered memory", b"changed the delivered memory", 1)
            )
            changed_transcript, _ = self._invoke(scratch, store, log, [mutated_transcript, extra])
            self.assertNotEqual(base["input_digest"], changed_transcript["input_digest"])

            mutated_extra = scratch / "mutated-extra.jsonl"
            mutated_extra.write_bytes(extra.read_bytes() + b"\n")
            changed_extra, _ = self._invoke(scratch, store, log, [transcript, mutated_extra])
            self.assertNotEqual(base["input_digest"], changed_extra["input_digest"])

            mutated_store = scratch / "mutated-store.sqlite"
            shutil.copyfile(store, mutated_store)
            _add_memory(scratch, mutated_store, "digest-only store sentinel")
            changed_store, _ = self._invoke(scratch, mutated_store, log, [transcript, extra])
            self.assertNotEqual(base["input_digest"], changed_store["input_digest"])

            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original, f"evaluator mutated {path.name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
