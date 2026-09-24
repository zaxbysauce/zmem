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

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False


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
        for legacy_injection_wire in (False, True):
            shell = inject.estimate_tokens(_format_fenced_recall(
                [], "header words here",
                legacy_injection_wire=legacy_injection_wire))
            for row in varied:
                rendered = inject.estimate_tokens(_format_fenced_recall(
                    [row], "header words here",
                    legacy_injection_wire=legacy_injection_wire))
                contribution = rendered - shell
                self.assertGreaterEqual(
                    inject.fence_row_cost(
                        row, legacy_injection_wire=legacy_injection_wire),
                    contribution,
                    "fence_row_cost must cover the row's real fence "
                    "contribution for %s (legacy=%s)" % (
                        row["id"], legacy_injection_wire))

    def test_legacy_injection_wire_keeps_tierless_bullet_bytes(self):
        from storelib.recall import _format_fenced_recall
        row = _row("compatibility row", score=0.8)
        generic = _format_fenced_recall([row], "header")
        legacy = _format_fenced_recall(
            [row], "header", legacy_injection_wire=True)
        self.assertIn("- [tier=unknown] [%s]" % row["id"], generic)
        self.assertIn("- [%s]" % row["id"], legacy)
        self.assertNotIn("[tier=unknown]", legacy)
        self.assertLess(
            inject.fence_row_cost(row, legacy_injection_wire=True),
            inject.fence_row_cost(row),
        )

    def test_legacy_injection_wire_keeps_malformed_tier_unknown(self):
        from storelib.recall import _format_fenced_recall
        for malformed_tier in ("not-a-tier", 0, " "):
            row = _row("malformed tier", score=0.8)
            row["tier"] = malformed_tier
            generic = _format_fenced_recall([row], "header")
            legacy = _format_fenced_recall(
                [row], "header", legacy_injection_wire=True)
            bullet = "- [tier=unknown] [%s]" % row["id"]
            self.assertIn(bullet, generic)
            self.assertIn(bullet, legacy)
            self.assertEqual(
                inject.fence_row_cost(row, legacy_injection_wire=True),
                inject.fence_row_cost(row),
            )


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

    def _run_body(self, mode: str, session_id: str = "") -> str:
        import subprocess
        body = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
        event = {"prompt": "hook budget probe row"}
        if session_id:
            event["session_id"] = session_id
        r = subprocess.run(
            [sys.executable, str(body), str(SCRIPTS / "store.py"),
             "project:budget", "25000", mode],
            input=json.dumps(event), capture_output=True, text=True, timeout=60,
        )
        return r.stdout

    def test_body_respects_tiny_budget(self):
        # Baseline: all three rows inject under the default budget.
        full = json.loads(self._run_body("user_prompt", "budget-full"))
        full_context = full.get("additionalContext", "")
        self.assertGreaterEqual(len(full_context.split("- [")), 2)

        os.environ["ZMEM_INJECT_TOKEN_BUDGET"] = "40"
        try:
            trimmed = json.loads(self._run_body("user_prompt", "budget-tiny"))
        finally:
            os.environ.pop("ZMEM_INJECT_TOKEN_BUDGET", None)
        # A 40-token budget cannot admit two 100+ token rows: the selector must
        # report a budget drop for this distinct session, not merely go silent
        # because the delivery ledger deduplicated the baseline session.
        trimmed_context = trimmed.get("additionalContext", "")
        self.assertLess(
            len(trimmed_context.split("- [")),
            len(full_context.split("- [")),
            "the hook body must stop adding bullets at ZMEM_INJECT_TOKEN_BUDGET",
        )
        with open(os.path.join(self._tmp, "zmem-decisions.log"), encoding="utf-8") as f:
            tiny_lines = [line for line in f if "sid=budget-tiny" in line]
        self.assertTrue(tiny_lines, "tiny-budget decision line missing")
        self.assertRegex(tiny_lines[-1], r"reason=budget-drop")
        self.assertRegex(tiny_lines[-1], r"budget_dropped=[1-9]")

    def test_bg_log_line_carries_tokens(self):
        log = os.path.join(self._tmp, "zmem-decisions.log")
        if os.path.exists(log):
            os.remove(log)
        self._run_body("user_prompt")
        with open(log, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if "zmem-hook" in ln]
        self.assertTrue(lines, "decisions log line missing")
        self.assertRegex(lines[-1], r"tokens=\d+/\d+")

    def test_partial_drop_fence_carries_budget_marker(self):
        # PR-review F11a: the rendered FENCE (not just the log) carries the
        # machine-readable [budget: ...] line when the budget drops rows.
        # Two TOPICALLY DISTINCT rows (semantic dedup-on-write absorbs
        # near-identical text at similarity > 0.85) and a budget that admits
        # the first but drops the second: a partial drop, so the fence
        # renders WITH the marker.
        import subprocess
        for content in ("budget probe payment webhook retry handler "
                        + "with exponential backoff " + "x" * 300,
                        "budget probe git rebase cleanup workflow "
                        + "for stalled feature branches " + "y" * 300):
            subprocess.run(
                [sys.executable, str(SCRIPTS / "store.py"), "add",
                 "--namespace", "project:budget", "--type", "lesson",
                 "--content", content, "--signal", "test"],
                capture_output=True, text=True, timeout=120,
            )
        event = json.dumps({"prompt": "budget probe"})
        # 278 - 128 shell = 150 available: row 1 (~112 tok) fits, row 2 is
        # budget-dropped -> partial drop with a rendered fence.
        os.environ["ZMEM_INJECT_TOKEN_BUDGET"] = "278"
        try:
            r = subprocess.run(
                [sys.executable, str(REPO_ROOT / "hooks" / "lib" /
                                     "zmem-recall-body.py"),
                 str(SCRIPTS / "store.py"), "project:budget", "25000",
                 "user_prompt"],
                input=event, capture_output=True, text=True, timeout=60,
            )
        finally:
            os.environ.pop("ZMEM_INJECT_TOKEN_BUDGET", None)
        if not r.stdout.strip():
            self.fail("hook emitted nothing; stderr=" + r.stderr[-500:])
        out = json.loads(r.stdout)
        ctx = out["additionalContext"]
        self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", ctx)
        self.assertRegex(ctx, r"\[budget: dropped [1-9]\d* rows")

    def test_log_line_fields_absent_without_budget_keys(self):
        # PR-review F11c/F1 regression pin: admission_used=None (legacy
        # envelope shape) must leave the budget fields ABSENT from the
        # decision line, not emit fabricated zeros.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_hook_body_log",
            REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        log = os.path.join(self._tmp, "zmem-decisions.log")
        if os.path.exists(log):
            os.remove(log)
        row = {"id": "legacy-1", "confidence": 0.9, "signal": "test",
               "namespace": "project:budget", "type": "fact",
               "content": "legacy envelope row"}
        mod._log_inject_decision(
            [row], [row], "injected", "injected",
            tokens_used=77, tokens_budget=1500,
            session_id="legacy-test", moment="user_prompt",
            admission_used=None,
            budget_dropped=None, budget_truncated=None,
            budget_dropped_protected=None)
        with open(log, encoding="utf-8") as f:
            line = [ln for ln in f.read().splitlines()
                    if "zmem-hook" in ln][-1]
        self.assertRegex(line, r"tokens=77/1500")
        # rendered_estimate rides whenever tokens_used is present...
        self.assertIn("rendered_estimate=77", line)
        # ...but the admission/budget fields stay ABSENT on legacy envelopes.
        self.assertNotIn("admission_budget=", line)
        self.assertNotIn("budget_dropped=", line)
        # And with admission stats provided, the fields ARE present
        # (zero-counts included — byte-stable shape).
        if os.path.exists(log):
            os.remove(log)
        mod._log_inject_decision(
            [row], [row], "injected", "injected",
            tokens_used=77, tokens_budget=1500,
            session_id="legacy-test", moment="user_prompt",
            admission_used=0,
            budget_dropped=0, budget_truncated=0,
            budget_dropped_protected=0)
        with open(log, encoding="utf-8") as f:
            line = [ln for ln in f.read().splitlines()
                    if "zmem-hook" in ln][-1]
        self.assertIn("rendered_estimate=77", line)
        self.assertIn("admission_budget=0", line)
        self.assertIn("budget_dropped=0", line)
        self.assertIn("budget_truncated=0", line)
        self.assertIn("budget_dropped_protected=0", line)


    def test_truncate_fail_closed_branch(self):
        # PR-review F11b: when the re-measure disagrees with the conservative
        # arithmetic (simulated by a cost function that inflates once content
        # is present), _truncate_protected_row must return None -> the row is
        # DROPPED with a protected-drop count, never admitted over ceiling.
        real_cost = inject.fence_row_cost

        def inflating_cost(row):
            base = real_cost(row)
            if (row.get("content") or "") and \
                    "…[budget-truncated]" in (row.get("content") or ""):
                return base + 10_000  # re-measure disagrees
            return base

        saved = inject.fence_row_cost
        try:
            inject.fence_row_cost = inflating_cost
            row = _row("decision " + "d" * 2000, type_="decision", score=0.9)
            stub = inject._truncate_protected_row(row, remaining=300)
            self.assertIsNone(stub)
            kept, used, dropped, stats = inject.apply_token_budget(
                [row], budget=inject.FENCE_SHELL_ALLOWANCE + 300,
                with_stats=True)
            self.assertEqual(kept, [])
            self.assertEqual(stats["dropped_protected"], 1)
            self.assertEqual(stats["truncated"], 0)
        finally:
            inject.fence_row_cost = saved

    def test_header_cap_and_budget_note_interplay(self):
        # PR-review F11d: a max-length header TOGETHER with a budget note
        # renders capped and complete — the shell allowance budgeted for
        # both.
        from storelib.recall import _format_fenced_recall
        rows = [_row("cap+note row " + "c" * 100)]
        fence = _format_fenced_recall(
            rows, "H" * 500, budget_note="[budget: dropped 1 rows, truncated 0]")
        self.assertIn("# [budget: dropped 1 rows, truncated 0]", fence)
        header_lines = [ln for ln in fence.splitlines() if ln.startswith("# H")]
        self.assertEqual(len(header_lines), 1)
        self.assertLessEqual(len(header_lines[0]), 244)
        self.assertTrue(
            fence.strip().endswith("<<<END_ZMEM_UNTRUSTED_FENCE>>>"))


class DegradedFenceFallbackTest(unittest.TestCase):
    """Passive providers consume only the store-owned rendered envelope.

    The #158 adapters no longer carry a local renderer or interpret row and
    budget fields. Malformed subprocess output must therefore degrade to a
    silent result instead of being rendered locally.
    """

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
        self.addCleanup(sys.modules.pop, "zmem_hermes_fallback", None)
        spec.loader.exec_module(mod)
        return mod

    def test_hermes_consumer_accepts_store_rendered_envelope(self):
        mod = self._load_hermes()
        rendered = ("<<<ZMEM_UNTRUSTED_FENCE>>>\n"
                    "store-owned row\n"
                    "<<<END_ZMEM_UNTRUSTED_FENCE>>>")
        envelope = {"rendered": rendered, "reason": "injected",
                    "results": [{"id": "fb1"}]}
        out = mod._decode_rendered_envelope(
            {"ok": True, "stdout": json.dumps(envelope)})
        self.assertEqual(out, envelope)
        self.assertIsNone(mod._decode_rendered_envelope(
            {"ok": True,
             "stdout": json.dumps({"results": [{"id": "fb1"}]})}))

    @unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
    def test_mcp_fallback_accepts_budget_note(self):
        # PR-review F11f: mcp_server transitively imports mcp at module
        # import time (mcp_server -> auth -> mcp.server.auth.provider), so
        # exec_module does need it; skip only where mcp is genuinely absent
        # (bare CI) — not a dead skip, the parity pin executes wherever mcp
        # is installed. mcp_server DOES import auth/bind_guard from its own
        # dir, so make that dir importable and let real errors fail loudly.
        import importlib.util
        server_dir = str(REPO_ROOT / "hermes-plugin" / "server")
        sys.path.insert(0, server_dir)
        self.addCleanup(sys.path.remove, server_dir)
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_fallback",
            REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_fallback"] = mod
        self.addCleanup(sys.modules.pop, "zmem_mcp_fallback", None)
        spec.loader.exec_module(mod)
        rows = [{"id": "fb2", "confidence": 0.9, "signal": "test",
                 "namespace": "project:x", "type": "fact", "content": "c"}]
        out = mod._local_fenced_recall(
            rows, "hdr", budget_note="[budget: dropped 0 rows, truncated 1]")
        self.assertIn("[budget: dropped 0 rows, truncated 1]", out)
        plain = mod._local_fenced_recall(rows, "hdr")
        self.assertNotIn("[budget:", plain)

    @unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
    def test_mcp_fallback_preserves_scoped_tier_markers(self):
        import importlib.util
        server_dir = str(REPO_ROOT / "hermes-plugin" / "server")
        sys.path.insert(0, server_dir)
        self.addCleanup(sys.path.remove, server_dir)
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_fallback_tiers",
            REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_fallback_tiers"] = mod
        self.addCleanup(sys.modules.pop, "zmem_mcp_fallback_tiers", None)
        spec.loader.exec_module(mod)
        rows = [
            {"id": "scoped", "confidence": 0.9, "signal": "test",
             "namespace": "project:x", "type": "fact", "content": "c",
             "tier": "project"},
            {"id": "unknown", "confidence": 0.9, "signal": "test",
             "namespace": "project:z", "type": "fact", "content": "e"},
            {"id": "cross", "confidence": 0.9, "signal": "test",
             "namespace": "project:y", "type": "lesson", "content": "d",
             "tier": "cross"},
            {"id": "malformed", "confidence": 0.9, "signal": "test",
             "namespace": "project:z", "type": "fact", "content": "e",
             "tier": "not-a-tier"},
        ]
        out = mod._local_fenced_recall(rows, "hdr")
        self.assertIn("- [tier=project] [scoped]", out)
        self.assertIn("- [tier=unknown] [unknown]", out)
        self.assertIn("- [tier=unknown] [malformed]", out)
        self.assertIn("[ns=project:y] [tier=cross] [type=lesson]", out)
        legacy = mod._local_fenced_recall(
            rows, "hdr", legacy_injection_wire=True)
        self.assertIn("- [unknown]", legacy)
        self.assertNotIn("- [tier=unknown] [unknown]", legacy)
        self.assertIn("- [tier=unknown] [malformed]", legacy)

    def test_mcp_renderer_signature_ladder_preserves_type_errors(self):
        import importlib.util

        server_dir = str(REPO_ROOT / "hermes-plugin" / "server")
        sys.path.insert(0, server_dir)
        self.addCleanup(sys.path.remove, server_dir)
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_renderer_ladder",
            REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_renderer_ladder"] = mod
        self.addCleanup(sys.modules.pop, "zmem_mcp_renderer_ladder", None)
        spec.loader.exec_module(mod)
        rows = [{"id": "row"}]

        def current(rows, header, *, budget_note="", legacy_injection_wire=False):
            self.assertTrue(legacy_injection_wire)
            self.assertEqual(budget_note, "note")
            return "current"

        def old_with_note(rows, header, *, budget_note=""):
            self.assertEqual(budget_note, "note")
            return "old-with-note"

        def oldest(rows, header):
            return "oldest"

        self.assertEqual(
            mod._render_legacy_injection_fence(current, rows, "hdr", "note"),
            "current")
        self.assertEqual(
            mod._render_legacy_injection_fence(
                old_with_note, rows, "hdr", "note"), "old-with-note")
        self.assertEqual(
            mod._render_legacy_injection_fence(oldest, rows, "hdr", "note"),
            "oldest")

        def broken(rows, header, **kwargs):
            raise TypeError("renderer internal failure")

        with self.assertRaisesRegex(TypeError, "renderer internal failure"):
            mod._render_legacy_injection_fence(broken, rows, "hdr", "note")


if __name__ == "__main__":
    unittest.main(verbosity=2)
