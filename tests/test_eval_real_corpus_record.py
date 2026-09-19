"""Guardrail test for the committed real-corpus replay record (issue #155).

`eval/real-corpus-2026-09-19.json` is the predeclared private real-corpus
measurement of record (issue #155, comments 5744282680 and 5744445706): the
evaluator replayed a FROZEN cohort — a standalone store snapshot, the
ver=0.49.0 release-availability projection of the frozen decision log, and the
one cohort transcript with a Claude-shaped prompt-event surface — after the
declaration was published. The committed report carries aggregates and digests
only; the private cohort itself is never committed.

This test pins the record's shape, its digest binding (the store snapshot
SHA-256 declared in the predeclaration, and the input digest measured at the
run), and its privacy boundary (no memory-content keys, no absolute paths), so
the record cannot be silently replaced, emptied, or extended with private
data. It is a pure file/JSON test: no storelib import, no store access, no
env dependence.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "eval" / "real-corpus-2026-09-19.json"

# Digests declared in the predeclaration (issue #155 comment 5744282680) and
# the input digest measured by the run of record, disclosed in the addendum
# (comment 5744445706) and recorded in the PR description.
DECLARED_STORE_SHA256 = (
    "071f3bc68f35f88c5fbd1cc7bcc58c3072d4c5c826f4d0dad97812021451f74a"
)
RECORDED_INPUT_DIGEST = (
    "e4da4216601bf882b318c3496293e054b4c06cf6ffa0b3fb04dc6fe3c3e01381"
)
KEY_ORDER = [
    "schema_version", "input_digest", "store_sha256", "days", "rows",
    "aggregate", "input_metadata", "usable_observation", "generated_at",
]
EXPECTED_ROWS = [
    (lane, moment)
    for lane in ("claude", "hermes-provider")
    for moment in ("precompact", "pretool", "session_start", "user_prompt")
]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_PATH = re.compile(r"^([A-Za-z]:[\\/]|/|~[\\/])")


class RealCorpusRecordTests(unittest.TestCase):
    def _load(self) -> dict:
        self.assertTrue(RECORD.is_file(),
                        "committed real-corpus record is missing")
        return json.loads(RECORD.read_text(encoding="utf-8"))

    def test_record_digests_are_pinned_to_the_declaration(self) -> None:
        report = self._load()
        self.assertEqual(report["store_sha256"], DECLARED_STORE_SHA256)
        self.assertEqual(report["input_digest"], RECORDED_INPUT_DIGEST)
        for field in ("store_sha256", "input_digest"):
            self.assertRegex(report[field], _HEX64)

    def test_record_schema_shape(self) -> None:
        report = self._load()
        self.assertEqual(list(report.keys()), KEY_ORDER)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["days"], 1)
        self.assertEqual(report["input_metadata"],
                         {"version": "0.49.0", "parsed_rows": 162})
        self.assertIsInstance(report["usable_observation"], bool)
        self.assertTrue(report["usable_observation"],
                        "run of record must not be structurally vacuous")
        self.assertEqual(report["generated_at"], "2026-09-19T18:17:05Z")
        self.assertRegex(report["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_rows_cover_eight_sorted_lane_moment_buckets(self) -> None:
        report = self._load()
        rows = report["rows"]
        self.assertEqual([(r["lane"], r["moment"]) for r in rows],
                         EXPECTED_ROWS)
        count_keys = {"decisions", "candidates", "delivered",
                      "reference_checked", "miss", "empty_pool",
                      "already_delivered"}
        for row in rows:
            self.assertEqual(set(row["counts"].keys()), count_keys)
            for value in row["counts"].values():
                self.assertIsInstance(value, int)
            for field in ("reference_precision", "false_injection_rate",
                          "miss_rate", "empty_pool_rate",
                          "already_delivered_rate"):
                self.assertIsInstance(row[field], (int, float))
            timing = row["t_ms"]
            self.assertIn("p50", timing)
            self.assertIn("p95", timing)
            for value in timing.values():
                self.assertTrue(value is None or isinstance(value, (int, float)))

    def test_record_carries_no_private_content_or_paths(self) -> None:
        report = self._load()
        violations: list[str] = []

        def walk(node, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("content", "text"):
                        violations.append(f"content key at {path}/{key}")
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str) and _ABSOLUTE_PATH.match(node):
                violations.append(f"absolute path at {path}")

        walk(report, "")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
