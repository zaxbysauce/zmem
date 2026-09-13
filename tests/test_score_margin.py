"""Independent acceptance tests for the issue #182 injection margin gate.

The pre-fix tree intentionally cannot import the two new helpers. This module
is therefore a red acceptance gate until the feature is implemented.
"""

from __future__ import annotations

import copy
import json
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
MARGIN_ENV = "ZMEM_INJECT_MARGIN"

# Do this before importing storelib: tests must not inherit an operator's
# rollout setting and accidentally make expected behaviour non-local.
os.environ.pop(MARGIN_ENV, None)
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
sys.path.insert(0, str(SCRIPTS))

# Direct imports are deliberate. They make this an honest RED gate on the
# pre-#182 tree instead of silently skipping the new public contract.
from storelib.inject import apply_score_margin, inject_score_margin  # noqa: E402


def _rows(*, first_type="fact", second_type="fact"):
    return [
        {"id": "m-top", "type": first_type, "_score": 0.80},
        {"id": "m-second", "type": second_type, "_score": 0.79},
        {"id": "m-third", "type": "lesson", "_score": 0.60},
    ]


def _ids(rows):
    return [row["id"] for row in rows]


class ScoreMarginFixtureTests(unittest.TestCase):
    """The recurrence fixture freezes the four issue-mandated outcomes."""

    def test_exact_fixture_cases(self):
        fixture_path = ROOT / "tests" / "fixtures" / "score_margin" / "expected.json"
        expected = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(len(expected), 4)

        cases = (
            _rows(),
            _rows(),
            _rows(first_type="decision"),
            _rows(second_type="constraint"),
        )
        for want, rows in zip(expected, cases):
            with self.subTest(want=want):
                retained, observed_margin, pruned = apply_score_margin(
                    rows, margin=want["threshold"]
                )
                self.assertEqual(_ids(retained), want["retained"])
                self.assertEqual(_ids(pruned), want["pruned"])
                if want["margin"] is None:
                    self.assertIsNone(observed_margin)
                else:
                    self.assertTrue(math.isfinite(observed_margin))
                    self.assertEqual(format(observed_margin, ".6f"), want["margin"])


class ScoreMarginConfigurationTests(unittest.TestCase):
    def test_absent_zero_invalid_nonfinite_and_negative_fail_open(self):
        for raw in (None, "0", "", "invalid", "NaN", "Inf", "-Inf", "-0.01"):
            env = {} if raw is None else {MARGIN_ENV: raw}
            with self.subTest(raw=raw), patch.dict(os.environ, env, clear=True):
                os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
                self.assertEqual(inject_score_margin(), 0.0)
                retained, observed_margin, pruned = apply_score_margin(_rows())
                self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
                self.assertIsNone(observed_margin)
                self.assertEqual(pruned, [])

    def test_value_above_one_clamps_to_one(self):
        with patch.dict(os.environ, {MARGIN_ENV: "1.7"}, clear=True):
            os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
            self.assertEqual(inject_score_margin(), 1.0)

    def test_configuration_is_read_per_call(self):
        with patch.dict(os.environ, {MARGIN_ENV: "0.05"}, clear=True):
            self.assertEqual(inject_score_margin(), 0.05)
        with patch.dict(os.environ, {MARGIN_ENV: "0.61"}, clear=True):
            self.assertEqual(inject_score_margin(), 0.61)


class ScoreMarginTest(unittest.TestCase):
    def test_margin_005_keeps_only_top(self):
        rows = _rows()
        before = copy.deepcopy(rows)
        retained, observed_margin, pruned = apply_score_margin(rows, margin=0.05)
        self.assertEqual(_ids(retained), ["m-top"])
        self.assertEqual(_ids(pruned), ["m-second", "m-third"])
        self.assertEqual(observed_margin, 0.0125)
        self.assertEqual(rows, before)

    def test_margin_zero_keeps_all_three(self):
        rows = _rows()
        before = copy.deepcopy(rows)
        retained, observed_margin, pruned = apply_score_margin(rows, margin=0.0)
        self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
        self.assertIsNone(observed_margin)
        self.assertEqual(pruned, [])
        self.assertEqual(rows, before)

    def test_constraint_as_second_row_is_kept(self):
        rows = _rows(second_type="constraint")
        before = copy.deepcopy(rows)
        retained, observed_margin, pruned = apply_score_margin(rows, margin=0.05)
        self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
        self.assertEqual(observed_margin, 0.0125)
        self.assertEqual(pruned, [])
        self.assertEqual(rows, before)

    def test_decision_as_top_row_is_kept(self):
        rows = _rows(first_type="decision")
        before = copy.deepcopy(rows)
        retained, observed_margin, pruned = apply_score_margin(rows, margin=0.05)
        self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
        self.assertEqual(observed_margin, 0.0125)
        self.assertEqual(pruned, [])
        self.assertEqual(rows, before)

    def test_invalid_margin_fails_open(self):
        for raw in ("invalid", "NaN", "Inf", "-0.01"):
            with self.subTest(raw=raw), patch.dict(
                os.environ, {MARGIN_ENV: raw}, clear=True
            ):
                retained, observed_margin, pruned = apply_score_margin(_rows())
                self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
                self.assertIsNone(observed_margin)
                self.assertEqual(pruned, [])

    def test_margin_is_strictly_below_threshold(self):
        rows = [
            {"id": "top", "type": "fact", "_score": 0.80},
            {"id": "second", "type": "fact", "_score": 0.76},
            {"id": "third", "type": "lesson", "_score": 0.40},
        ]
        retained, observed_margin, pruned = apply_score_margin(rows, margin=0.05)
        self.assertEqual(_ids(retained), _ids(rows))
        self.assertEqual(pruned, [])
        self.assertEqual(format(observed_margin, ".6f"), "0.050000")

    def test_missing_malformed_nonfinite_and_nonpositive_scores_fail_open(self):
        unusable_scores = (
            ("missing", None), ("malformed", "not-a-number"),
            ("nan", float("nan")), ("positive-inf", float("inf")),
            ("negative-inf", float("-inf")),
        )
        for label, score in unusable_scores:
            with self.subTest(score=label):
                bad = {"id": label, "type": "fact"}
                if score is not None:
                    bad["_score"] = score
                for position in ("top", "runner-up"):
                    with self.subTest(position=position):
                        usable = {"id": "usable", "type": "fact", "_score": 0.80}
                        rows = [bad, usable] if position == "top" else [usable, bad]
                        retained, observed_margin, pruned = apply_score_margin(
                            rows, margin=0.05
                        )
                        self.assertEqual(_ids(retained), _ids(rows))
                        self.assertIsNone(observed_margin)
                        self.assertEqual(pruned, [])

        nonpositive_top_scores = (("zero", 0.0), ("negative", -0.01))
        for label, score in nonpositive_top_scores:
            with self.subTest(score=label):
                rows = [
                    {"id": label, "type": "fact", "_score": score},
                    {"id": "usable", "type": "fact", "_score": 0.80},
                ]
                retained, observed_margin, pruned = apply_score_margin(
                    rows, margin=0.05
                )
                self.assertEqual(_ids(retained), _ids(rows))
                self.assertIsNone(observed_margin)
                self.assertEqual(pruned, [])

    def test_protected_top_or_runner_up_prevents_pruning_but_reports_margin(self):
        for position, first_type, second_type in (
            ("top", "decision", "fact"),
            ("runner-up", "fact", "constraint"),
        ):
            with self.subTest(position=position):
                retained, observed_margin, pruned = apply_score_margin(
                    _rows(first_type=first_type, second_type=second_type), margin=0.05
                )
                self.assertEqual(_ids(retained), ["m-top", "m-second", "m-third"])
                self.assertEqual(pruned, [])
                self.assertEqual(format(observed_margin, ".6f"), "0.012500")

    def test_helper_never_mutates_input_rows(self):
        rows = _rows()
        before = copy.deepcopy(rows)
        apply_score_margin(rows, margin=0.05)
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
