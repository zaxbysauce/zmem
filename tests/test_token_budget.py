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
        # Budget fits exactly one row (admission estimator + reserved
        # shell): the grounded one wins.
        one_cost = inject.fence_row_cost(grounded) \
            + inject.FENCE_SHELL_ALLOWANCE
        kept, _u, dropped = inject.apply_token_budget(
            [none_row, grounded], budget=one_cost)
        self.assertEqual([r["id"] for r in kept], [grounded["id"]])
        self.assertEqual(dropped, 1)

    def test_lowest_score_drops_first(self):
        rows = [_row("low " + "x" * 400, score=0.2),
                _row("high " + "y" * 400, score=0.9)]
        # Issue #116: budgets are computed from the admission estimator
        # (fence_row_cost) plus the reserved fence shell, so exactly ONE
        # row fits and the higher-scored one wins.
        one_cost = inject.fence_row_cost(rows[1]) \
            + inject.FENCE_SHELL_ALLOWANCE
        kept, _u, _d = inject.apply_token_budget(rows, budget=one_cost)
        self.assertEqual([r["id"] for r in kept], [rows[1]["id"]])

    def test_decision_and_constraint_truncated_not_dropped(self):
        # Issue #116 spec change (was: protected rows kept whole over
        # budget). A decision row that does not fit the remaining ceiling
        # is TRUNCATED with an explicit marker — still present, never
        # silently dropped; the higher-scored normal row is skipped because
        # it does not fit either.
        rows = [
            _row("trivia one " + "x" * 400, score=0.9),
            _row("decision one " + "d" * 400, type_="decision", score=0.1),
        ]
        budget = inject.FENCE_SHELL_ALLOWANCE + 60
        kept, used, dropped, stats = inject.apply_token_budget(
            rows, budget=budget, with_stats=True)
        kept_ids = [r["id"] for r in kept]
        self.assertNotIn(rows[0]["id"], kept_ids)
        self.assertIn(rows[1]["id"], kept_ids)
        kept_decision = kept[kept_ids.index(rows[1]["id"])]
        self.assertTrue(
            kept_decision["content"].endswith(inject.TRUNCATION_MARKER),
            "truncated protected row must carry the explicit marker")
        self.assertEqual(stats["truncated"], 1)
        self.assertEqual(stats["dropped_protected"], 0)
        self.assertLessEqual(used + inject.FENCE_SHELL_ALLOWANCE, budget)

    def test_protected_only_truncated_to_fit(self):
        # Issue #116 spec change (was: kept even when they alone exceed the
        # budget). Protected rows alone over budget are truncated to fit —
        # the ceiling holds and the row survives with the marker.
        rows = [_row("big decision " + "d" * 4000, type_="decision")]
        budget = inject.FENCE_SHELL_ALLOWANCE + 100
        kept, used, dropped, stats = inject.apply_token_budget(
            rows, budget=budget, with_stats=True)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["content"].endswith(inject.TRUNCATION_MARKER))
        self.assertEqual(stats["truncated"], 1)
        self.assertLessEqual(used + inject.FENCE_SHELL_ALLOWANCE, budget)

    def test_absurd_budget_drops_protected_with_count(self):
        # Below the fence-shell allowance nothing fits — protected rows are
        # dropped WITH a count, never silently.
        rows = [_row("big decision " + "d" * 4000, type_="decision")]
        kept, _u, _d, stats = inject.apply_token_budget(
            rows, budget=10, with_stats=True)
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped_protected"], 1)
        self.assertEqual(stats["truncated"], 0)

    def test_scan_continues_past_oversized_row(self):
        # Issue #116: a later small row must not be lost to an earlier
        # oversized one.
        big = _row("big first " + "b" * 4000, score=0.9)
        small = _row("small late lesson", score=0.4)
        filler = _row("filler " + "f" * 4000, score=0.8)
        kept, _u, _d = inject.apply_token_budget(
            [big, filler, small], budget=1500)
        self.assertIn(small["id"], [r["id"] for r in kept])

    def test_budget_note_formatting(self):
        self.assertEqual(inject.budget_note({"dropped": 0, "truncated": 0}),
                         "")
        self.assertEqual(
            inject.budget_note({"dropped": 2, "truncated": 1}),
            "[budget: dropped 2 rows, truncated 1]")
        self.assertEqual(
            inject.budget_note({"dropped": 1, "truncated": 0,
                                "dropped_protected": 1}),
            "[budget: dropped 1 rows, truncated 0]")

    def test_renderer_header_capped(self):
        from storelib.recall import _format_fenced_recall
        rows = [_row("cap probe row " + "c" * 200)]
        long_header = "H" * 500
        fence = _format_fenced_recall(rows, long_header)
        header_lines = [ln for ln in fence.splitlines()
                        if ln.startswith("# H")]
        self.assertEqual(len(header_lines), 1)
        self.assertLessEqual(len(header_lines[0]), 244)  # "# " + 240 + ...

    def test_fence_row_cost_covers_real_render(self):
        # The admission charge must cover what the renderer actually emits
        # for the row (renderer-mirror property), over varied shapes. A
        # row's true contribution is render([row]) - render([]) — the
        # fence shell is FENCE_SHELL_ALLOWANCE's job, not the row cost's.
        from storelib.recall import _format_fenced_recall
        varied = [
            _row("plain content row " + "p" * 100),
            _row("rich " + "r" * 300, signal="test"),
        ]
        varied[1]["source_ref"] = "session:abc123"
        varied[1]["tags"] = "one,two"
        varied[1]["entities"] = [{"name": "Entity"}, {"name": "Beta"},
                                 {"name": "Gamma"}, {"name": "Delta"}]
        varied[1]["_stale_note"] = " [stale]"
        shell = inject.estimate_tokens(
            _format_fenced_recall([], "header words here"))
        for row in varied:
            rendered = inject.estimate_tokens(
                _format_fenced_recall([row], "header words here"))
            contribution = rendered - shell
            self.assertGreaterEqual(
                inject.fence_row_cost(row), contribution,
                "fence_row_cost must cover the row's real fence "
                "contribution for %s" % row["id"])


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
        # C26: save AND clear the budget knob — the subprocess inherits
        # this env and the test asserts the documented default 1500.
        cls._saved = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_INJECT_TOKEN_BUDGET",
            "ZMEM_MODEL_AUTODOWNLOAD")}
        cls.store = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_STORE"] = cls.store
        os.environ["ZMEM_DATA"] = cls._tmp
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_INJECT_TOKEN_BUDGET", None)
        cls._run(["init"])
        cls._run(["add", "--namespace", "project:budget", "--type", "fact",
                  "--content", "token budget envelope check row",
                  "--signal", "test"])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)
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
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)
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
        log = os.path.join(self._tmp, "zmem-decisions.log")
        if os.path.exists(log):
            os.remove(log)
        self._run_body("user_prompt")
        with open(log, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "zmem-hook" in ln]
        self.assertTrue(lines, "decisions log line missing")
        self.assertRegex(lines[-1], r"tokens=\d+/\d+")


class DegradedFenceFallbackTest(unittest.TestCase):
    """Issue #116 final-critic catch: the degraded-mode fallback renderers in
    both Hermes twins must accept the ``budget_note`` kwarg the session_start
    paths now pass — the fallback exists for exactly the import-failure
    scenario where a TypeError would defeat the fail-open contract."""

    def _load_hermes(self):
        import importlib.util
        import types
        agent = types.ModuleType("agent")

        class MemoryProvider:  # minimal stand-in (provided by the gateway)
            pass

        mp = types.ModuleType("agent.memory_provider")
        mp.MemoryProvider = MemoryProvider
        agent.memory_provider = mp
        sys.modules.setdefault("agent", agent)
        sys.modules.setdefault("agent.memory_provider", mp)
        spec = importlib.util.spec_from_file_location(
            "zmem_hermes_fallback",
            REPO_ROOT / "hermes-plugin" / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zmem_hermes_fallback"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_hermes_fallback_accepts_budget_note(self):
        mod = self._load_hermes()
        rows = [{"id": "fb1", "confidence": 0.9, "signal": "test",
                 "namespace": "project:x", "type": "fact", "content": "c"}]
        out = mod._local_fenced_recall(
            rows, "hdr", budget_note="[budget: dropped 1 rows, truncated 0]")
        self.assertIn("[budget: dropped 1 rows, truncated 0]", out)
        self.assertTrue(
            out.strip().endswith("<<<END_ZMEM_UNTRUSTED_FENCE>>>"))
        plain = mod._local_fenced_recall(rows, "hdr")
        self.assertNotIn("[budget:", plain)

    def test_mcp_fallback_accepts_budget_note(self):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "zmem_mcp_fallback",
                REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except ImportError:
            self.skipTest("mcp package not installed")
            return
        rows = [{"id": "fb2", "confidence": 0.9, "signal": "test",
                 "namespace": "project:x", "type": "fact", "content": "c"}]
        out = mod._local_fenced_recall(
            rows, "hdr", budget_note="[budget: dropped 0 rows, truncated 1]")
        self.assertIn("[budget: dropped 0 rows, truncated 1]", out)
        plain = mod._local_fenced_recall(rows, "hdr")
        self.assertNotIn("[budget:", plain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
