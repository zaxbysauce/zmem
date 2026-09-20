"""Guardrail test for the committed real-corpus replay record (issue #155).

`eval/real-corpus-2026-09-19.json` is the predeclared private real-corpus
measurement of record (issue #155, comments 5744282680 and 5744445706): the
evaluator replayed a FROZEN cohort — a standalone store snapshot, the
ver=0.49.0 release-availability projection of the frozen decision log, and the
one cohort transcript with a Claude-shaped prompt-event surface — after the
declaration was published. The committed report carries aggregates and digests
only; the private cohort itself is never committed.

This test pins the record's bytes (the file is a frozen artifact: any byte
change is a replacement), its digest binding (the store snapshot SHA-256
declared in the predeclaration, and the input digest measured at the run),
its schema, its privacy boundary (no memory-content keys, no absolute
paths), and demonstrates the reject behavior on tampered copies. It is a
pure file/JSON test: no storelib import, no store access, no env dependence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "eval" / "real-corpus-2026-09-19.json"

# Digests declared in the predeclaration (issue #155 comment 5744282680) and
# the input digest measured by the run of record, disclosed in the addendum
# (comment 5744445706) and recorded in the PR description. FILE_SHA256 pins
# the committed artifact's exact bytes: the record is frozen, so any edit —
# including a value-only edit that keeps the schema — is a replacement.
DECLARED_STORE_SHA256 = (
    "071f3bc68f35f88c5fbd1cc7bcc58c3072d4c5c826f4d0dad97812021451f74a"
)
RECORDED_INPUT_DIGEST = (
    "e4da4216601bf882b318c3496293e054b4c06cf6ffa0b3fb04dc6fe3c3e01381"
)
FILE_SHA256 = "620385af99e860d415a2716051f7a3063b1d203a5254af4a1bcb275eb8ebbb0d"
KEY_ORDER = [
    "schema_version", "input_digest", "store_sha256", "days", "rows",
    "aggregate", "input_metadata", "usable_observation", "generated_at",
]
EXPECTED_AGGREGATE = {"reference_precision": 0.0, "miss_rate": 0}
_ZERO_COUNTS = {
    "decisions": 0, "candidates": 0, "delivered": 0, "reference_checked": 0,
    "miss": 0, "empty_pool": 0, "already_delivered": 0,
}
EXPECTED_COUNTS = {
    ("claude", "precompact"): dict(_ZERO_COUNTS),
    ("claude", "pretool"): {
        "decisions": 152, "candidates": 953, "delivered": 109,
        "reference_checked": 20, "miss": 0, "empty_pool": 0,
        "already_delivered": 49,
    },
    ("claude", "session_start"): dict(_ZERO_COUNTS),
    ("claude", "user_prompt"): {
        "decisions": 5, "candidates": 34, "delivered": 0,
        "reference_checked": 0, "miss": 0, "empty_pool": 0,
        "already_delivered": 0,
    },
    ("hermes-provider", "precompact"): dict(_ZERO_COUNTS),
    ("hermes-provider", "pretool"): dict(_ZERO_COUNTS),
    ("hermes-provider", "session_start"): dict(_ZERO_COUNTS),
    ("hermes-provider", "user_prompt"): dict(_ZERO_COUNTS),
}
EXPECTED_TIMING = {
    ("claude", "pretool"): {"p50": 825, "p95": 1076},
    ("claude", "user_prompt"): {"p50": 936, "p95": 977},
}
EXPECTED_ROWS = [
    (lane, moment)
    for lane in ("claude", "hermes-provider")
    for moment in ("precompact", "pretool", "session_start", "user_prompt")
]
# Keys that must never appear anywhere in a published report: they are the
# shapes private prompt/transcript content would ride in.
_FORBIDDEN_KEYS = ("content", "text", "prompt", "message", "transcript")
_HEX64 = frozenset("0123456789abcdef")


def _privacy_violations(node, path: str = "") -> list[str]:
    """Return every privacy violation in ``node``: forbidden content keys and
    absolute-path-shaped strings, at any depth."""
    violations: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_KEYS:
                violations.append(f"content key at {path}/{key}")
            violations.extend(_privacy_violations(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            violations.extend(_privacy_violations(value, f"{path}[{index}]"))
    elif isinstance(node, str):
        if _looks_like_absolute_path(node):
            violations.append(f"absolute path at {path}")
    return violations


def _looks_like_absolute_path(value: str) -> bool:
    if value[:1] in ("/", "~"):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\")


class RealCorpusRecordTests(unittest.TestCase):
    def _load(self) -> dict:
        self.assertTrue(RECORD.is_file(),
                        "committed real-corpus record is missing")
        return json.loads(RECORD.read_text(encoding="utf-8"))

    def test_record_bytes_are_frozen(self) -> None:
        self.assertTrue(RECORD.is_file(),
                        "committed real-corpus record is missing")
        digest = hashlib.sha256(RECORD.read_bytes()).hexdigest()
        self.assertEqual(digest, FILE_SHA256,
                         "the frozen measurement record was modified")

    def test_record_digests_are_pinned_to_the_declaration(self) -> None:
        report = self._load()
        self.assertEqual(report["store_sha256"], DECLARED_STORE_SHA256)
        self.assertEqual(report["input_digest"], RECORDED_INPUT_DIGEST)
        for field in ("store_sha256", "input_digest"):
            value = report[field]
            self.assertIsInstance(value, str)
            self.assertEqual(len(value), 64)
            self.assertTrue(set(value) <= _HEX64, f"{field} is not 64-hex")

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
        self.assertEqual(report["aggregate"], EXPECTED_AGGREGATE)

    def test_rows_cover_eight_sorted_lane_moment_buckets(self) -> None:
        report = self._load()
        rows = report["rows"]
        self.assertEqual([(r["lane"], r["moment"]) for r in rows],
                         EXPECTED_ROWS)
        count_keys = {"decisions", "candidates", "delivered",
                      "reference_checked", "miss", "empty_pool",
                      "already_delivered"}
        for row in rows:
            key = (row["lane"], row["moment"])
            self.assertEqual(set(row["counts"].keys()), count_keys)
            # Value pins: every measurement number in the frozen record is
            # exact, so a value-only edit cannot pass the suite.
            self.assertEqual(row["counts"], EXPECTED_COUNTS[key],
                             f"counts changed for {key}")
            timing = row["t_ms"]
            self.assertEqual(timing, EXPECTED_TIMING.get(
                key, {"p50": None, "p95": None}))
            for field in ("reference_precision", "false_injection_rate",
                          "miss_rate", "empty_pool_rate",
                          "already_delivered_rate"):
                self.assertIsInstance(row[field], (int, float))

    def test_record_carries_no_private_content_or_paths(self) -> None:
        violations = _privacy_violations(self._load())
        self.assertEqual(violations, [])


class GuardrailRejectBehaviorTests(unittest.TestCase):
    """Negative-path demonstrations: tampered variants of the record must be
    rejected by the same pins the happy-path tests apply."""

    def _load(self) -> dict:
        return json.loads(RECORD.read_text(encoding="utf-8"))

    def test_value_tampering_is_rejected_by_the_pins(self) -> None:
        report = self._load()
        tampered = copy.deepcopy(report)
        tampered["rows"][1]["counts"]["decisions"] = 7
        self.assertNotEqual(tampered["rows"][1]["counts"],
                            EXPECTED_COUNTS[("claude", "pretool")])
        tampered_aggregate = copy.deepcopy(report)
        tampered_aggregate["aggregate"]["miss_rate"] = 0.5
        self.assertNotEqual(tampered_aggregate["aggregate"],
                            EXPECTED_AGGREGATE)

    def test_privacy_walker_flags_injected_private_shapes(self) -> None:
        report = self._load()
        for mutate in (
            lambda r: r["rows"][0].update({"prompt": "private text"}),
            lambda r: r["rows"][0].update({"content": "private text"}),
            lambda r: r["input_metadata"].update({"message": "ses"}),
            lambda r: r.update({"notes": "C:/data/private/store.sqlite"}),
            lambda r: r.update({"notes": "/srv/private/store.sqlite"}),
        ):
            tampered = copy.deepcopy(report)
            mutate(tampered)
            self.assertTrue(_privacy_violations(tampered),
                            "walker missed an injected private shape")

    def test_clean_record_produces_no_walker_violations(self) -> None:
        self.assertEqual(_privacy_violations(self._load()), [])


if __name__ == "__main__":
    unittest.main()
