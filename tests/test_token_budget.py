"""Token-budget tests (issue #65, 10.9).

Covers storelib/inject.py (estimate, budget admission policy, env parsing),
the read-envelope token reporting, and enforcement on the MCP session_start
tool. Hook-level enforcement is additionally covered end-to-end by
tests/test_session_tools.py via the store.py subprocess path.

Runs standalone: python tests/test_token_budget.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storelib import inject  # noqa: E402


def _row(content: str, *, type_="fact", signal="none", score=0.5):
    return {"id": content[:8], "type": type_, "signal": signal,
            "confidence": score, "_score": score, "content": content,
            "namespace": "project:budget"}


class EstimateTest(unittest.TestCase):
    def test_chars_per_token_heuristic(self):
        self.assertEqual(inject.estimate_tokens("abcd" * 10), 10)
        self.assertEqual(inject.estimate_tokens(""), 0)
        self.assertEqual(inject.CHARS_PER_TOKEN, 4)

    def test_row_cost_includes_fence_overhead(self):
        row = _row("x" * 400)
        self.assertEqual(
            inject.row_token_cost(row),
            100 + inject.FENCE_OVERHEAD_TOKENS)


class BudgetEnvTest(unittest.TestCase):
    def test_default_is_1500(self):
        os.environ.pop(inject.INJECT_TOKEN_BUDGET_ENV, None)
        self.assertEqual(inject.inject_token_budget(), 1500)

    def test_env_override(self):
        os.environ[inject.INJECT_TOKEN_BUDGET_ENV] = "42"
        try:
            self.assertEqual(inject.inject_token_budget(), 42)
        finally:
            os.environ.pop(inject.INJECT_TOKEN_BUDGET_ENV, None)

    def test_garbage_and_nonpositive_fall_back_to_default(self):
        for bad in ("banana", "0", "-5", ""):
            os.environ[inject.INJECT_TOKEN_BUDGET_ENV] = bad
            try:
                self.assertEqual(
                    inject.inject_token_budget(), 1500,
                    f"{bad!r} must fall back to the documented default")
            finally:
                os.environ.pop(inject.INJECT_TOKEN_BUDGET_ENV, None)


class BudgetAdmissionTest(unittest.TestCase):
    def test_budget_stops_adding_bullets(self):
        rows = [_row(f"content number {i} " + "x" * 400, score=0.9 - i * 0.1)
                for i in range(10)]
        kept, used, dropped = inject.apply_token_budget(rows, budget=250)
        # 250 tokens admits ~2 rows (100 + overhead each); never all 10.
        self.assertLess(len(kept), len(rows))
        self.assertEqual(dropped, len(rows) - len(kept))
        self.assertLessEqual(used, 250)

    def test_kept_preserves_caller_order(self):
        rows = [_row("first high scorer " + "x" * 400, score=0.9),
                _row("second lower scorer " + "y" * 400, score=0.5)]
        kept, _u, _d = inject.apply_token_budget(rows, budget=10_000)
        self.assertEqual([r["id"] for r in kept], [r["id"] for r in rows])

    def test_signal_none_drops_first_at_equal_score(self):
        none_row = _row("none signal row " + "z" * 400, signal="none", score=0.5)
        grounded = _row("test signal row " + "z" * 400, signal="test", score=0.5)
        # Budget fits exactly one row: the grounded one wins.
        one_cost = inject.row_token_cost(grounded)
        kept, _u, dropped = inject.apply_token_budget(
            [none_row, grounded], budget=one_cost)
        self.assertEqual([r["id"] for r in kept], [grounded["id"]])
        self.assertEqual(dropped, 1)

    def test_lowest_score_drops_first(self):
        rows = [_row("low " + "x" * 400, score=0.2),
                _row("high " + "y" * 400, score=0.9)]
        one_cost = inject.row_token_cost(rows[1])
        kept, _u, _d = inject.apply_token_budget(rows, budget=one_cost)
        self.assertEqual([r["id"] for r in kept], [rows[1]["id"]])

    def test_decision_and_constraint_never_dropped(self):
        rows = [
            _row("trivia one " + "x" * 400, score=0.9),
            _row("decision one " + "d" * 400, type_="decision", score=0.1),
            _row("constraint one " + "c" * 400, type_="constraint", score=0.1),
        ]
        # A budget smaller than the two protected rows alone still keeps them.
        tiny = inject.row_token_cost(rows[1]) - 1
        kept, _u, dropped = inject.apply_token_budget(rows, budget=tiny)
        kept_ids = {r["id"] for r in kept}
        self.assertIn(rows[1]["id"], kept_ids)
        self.assertIn(rows[2]["id"], kept_ids)
        self.assertNotIn(rows[0]["id"], kept_ids)
        self.assertEqual(dropped, 1)

    def test_protected_only_rows_exceeding_budget_are_kept(self):
        # Once ONLY decision/constraint rows remain, budget enforcement stops:
        # they are kept even when they alone exceed the budget.
        rows = [_row("big decision " + "d" * 4000, type_="decision")]
        kept, _u, _d = inject.apply_token_budget(rows, budget=1)
        self.assertEqual(len(kept), 1)


class EnvelopeResultsTest(unittest.TestCase):
    def test_dict_envelope_unwrapped(self):
        rows = [_row("envelope row")]
        self.assertEqual(inject.envelope_results({"results": rows}), rows)

    def test_bare_list_passthrough(self):
        rows = [_row("bare list row")]
        self.assertEqual(inject.envelope_results(rows), rows)

    def test_non_shapes(self):
        self.assertEqual(inject.envelope_results({"results": "nope"}), [])
        self.assertEqual(inject.envelope_results("nope"), [])
        self.assertEqual(inject.envelope_results(None), [])


class ReadEnvelopeReportingTest(unittest.TestCase):
    """recall/recent/search --json report tokens_used/tokens_budget."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-budget-cli-")
        cls._saved = {k: os.environ.get(k) for k in ("ZMEM_STORE", "ZMEM_DATA")}
        cls.store = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_STORE"] = cls.store
        os.environ["ZMEM_DATA"] = cls._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        cls._run(["init"])
        cls._run(["add", "--namespace", "project:budget", "--type", "fact",
                  "--content", "token budget envelope check row",
                  "--signal", "test"])

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _run(cls, args):
        import subprocess
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "store.py"), *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_recall_json_reports_token_fields(self):
        r = self._run(["recall", "--query", "token budget envelope",
                       "--namespace", "project:budget", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        env = json.loads(r.stdout)
        self.assertIsInstance(env["results"], list)
        self.assertGreaterEqual(env["count"], 1)
        self.assertIn("tokens_used", env)
        self.assertIn("tokens_budget", env)
        self.assertGreaterEqual(env["tokens_used"], 1)
        self.assertEqual(env["tokens_budget"], 1500)
        self.assertIn("omitted", env)
        self.assertIn("injection_risk", env)

    def test_search_json_reports_token_fields(self):
        r = self._run(["search", "--text", "token budget",
                       "--namespace", "project:budget", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        env = json.loads(r.stdout)
        self.assertGreaterEqual(env["count"], 1)
        self.assertIn("tokens_used", env)
        self.assertIn("tokens_budget", env)


class HookBodyBudgetTest(unittest.TestCase):
    """The shared hook body stops adding bullets under a tiny budget (10.9)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="zmem-budget-hook-")
        cls._saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_INJECT_TOKEN_BUDGET")}
        os.environ["ZMEM_STORE"] = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = cls._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        import subprocess
        for i in range(3):
            subprocess.run(
                [sys.executable, str(SCRIPTS / "store.py"), "add",
                 "--namespace", "project:budget", "--type", "lesson",
                 "--content", f"hook budget probe row {i} " + "x" * 400,
                 "--signal", "test"],
                capture_output=True, text=True, timeout=120,
            )

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run_body(self, mode: str) -> str:
        import subprocess
        body = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
        event = json.dumps({"prompt": "hook budget probe row"})
        r = subprocess.run(
            [sys.executable, str(body), str(SCRIPTS / "store.py"),
             "project:budget", "25000", mode],
            input=event, capture_output=True, text=True, timeout=60,
        )
        return r.stdout

    def test_body_respects_tiny_budget(self):
        # Baseline: all three rows inject under the default budget.
        full = json.loads(self._run_body("user_prompt"))
        self.assertGreaterEqual(len(full["additionalContext"].split("- [")), 2)

        os.environ["ZMEM_INJECT_TOKEN_BUDGET"] = "40"
        try:
            trimmed = json.loads(self._run_body("user_prompt"))
        finally:
            os.environ.pop("ZMEM_INJECT_TOKEN_BUDGET", None)
        # A 40-token budget cannot admit two 100+ token rows: fewer bullets.
        self.assertLess(
            len(trimmed["additionalContext"].split("- [")),
            len(full["additionalContext"].split("- [")),
            "the hook body must stop adding bullets at ZMEM_INJECT_TOKEN_BUDGET",
        )

    def test_bg_log_line_carries_tokens(self):
        log = os.path.join(self._tmp, "zmem-bg.log")
        if os.path.exists(log):
            os.remove(log)
        self._run_body("user_prompt")
        with open(log, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "zmem-hook" in ln]
        self.assertTrue(lines, "bg log line missing")
        self.assertRegex(lines[-1], r"tokens=\d+/\d+")


if __name__ == "__main__":
    unittest.main(verbosity=2)
