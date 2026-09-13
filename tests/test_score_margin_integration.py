"""Issue #182 integration coverage for the hook's margin telemetry seam.

The store owns score-margin selection. This file only verifies that the hook
preserves the CLI's existing ``--explain --exclude`` refusal and carries the
optional envelope fields into both decision-log consumers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SESSION_START_PAYLOAD = (
    REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"
)
NS = "project:score-margin-integration"


class ScoreMarginIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-margin-hook-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
            "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_MARGIN",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_SESSION",
            "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
        ):
            env.pop(key, None)
        env.update({
            "ZMEM_STORE": str(Path(self.tmp, "store.sqlite")),
            "ZMEM_DATA": self.tmp,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_TEST_NOW": "2026-06-01T00:00:00Z",
            "PYTHONUTF8": "1",
        })
        return env

    def _load_body(self):
        spec = importlib.util.spec_from_file_location(
            "zmem_recall_body_score_margin_integration", str(BODY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run_body(self, envelope: dict, session_id: str):
        mod = self._load_body()
        old_argv = sys.argv
        old_stdin = sys.stdin
        sys.argv = [str(BODY), str(STORE_PY), NS, "25000", "user_prompt"]
        sys.stdin = io.StringIO(json.dumps({
            "prompt": "score margin integration probe",
            "session_id": session_id,
        }))
        stdout = io.StringIO()
        try:
            with patch.dict(os.environ, self._env(), clear=True):
                with patch.object(
                    mod.subprocess,
                    "check_output",
                    return_value=json.dumps(envelope).encode("utf-8"),
                ):
                    with contextlib.redirect_stdout(stdout):
                        return_code = mod.main()
        finally:
            sys.argv = old_argv
            sys.stdin = old_stdin
        return return_code, stdout.getvalue()

    def _decision_lines(self) -> list[str]:
        log = Path(self.tmp, "zmem-decisions.log")
        return [line for line in log.read_text(encoding="utf-8").splitlines()
                if "zmem-hook" in line]

    def _run_session_start(self, envelope: dict, session_id: str):
        old_argv = sys.argv
        sys.argv = [
            str(SESSION_START_PAYLOAD), "", "", str(STORE_PY),
            self.tmp, self.tmp, self.tmp, NS, "25000", "", "", "",
            session_id, "", "",
        ]
        try:
            with patch.dict(os.environ, self._env(), clear=True):
                with patch.object(
                    subprocess,
                    "check_output",
                    return_value=json.dumps(envelope).encode("utf-8"),
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        return runpy.run_path(
                            str(SESSION_START_PAYLOAD),
                            run_name="zmem_session_start_score_margin_test",
                        )
        finally:
            sys.argv = old_argv

    @staticmethod
    def _without_timestamp(line: str) -> str:
        return re.sub(r"^\[\d+\]", "[TIMESTAMP]", line)

    @staticmethod
    def _memory_conn():
        """Return a real, current-schema scratch store for library-path tests."""
        sys.path.insert(0, str(SCRIPTS))
        from storelib.schema import init_db, migrate

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        migrate(conn)
        return conn

    @staticmethod
    def _seed_memory_rows(conn, ids: list[str], *, content_prefix: str = ""):
        for index, mid in enumerate(ids):
            conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    source_hash, confidence, signal, valid_from, ingestion_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, NS, "fact", f"{content_prefix}{mid}", "",
                 "session:score-margin-integration", "", 0.9, "test",
                 f"2026-01-01T00:00:0{index}Z",
                 f"2026-01-01T00:00:0{index}Z"),
            )
        conn.commit()

    @staticmethod
    def _scored_row(mid: str, score: float) -> dict:
        return {
            "id": mid,
            "namespace": NS,
            "type": "fact",
            "content": f"score-margin row {mid}",
            "tags": "",
            "source_ref": "session:score-margin-integration",
            "source_hash": "",
            "confidence": 0.9,
            "signal": "test",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "",
            "update_of": "",
            "taint": "trusted_internal",
            "trust_score": 1.0,
            "_score": score,
        }

    def test_recall_injection_margin_runs_in_production_path(self):
        """The actual recall entry point gates, orders, and bumps survivors."""
        sys.path.insert(0, str(SCRIPTS))
        from storelib import recall as recall_mod

        conn = self._memory_conn()
        ids = ["recall-low", "recall-top", "recall-second"]
        self._seed_memory_rows(conn, ids)
        # The scorer's presentation order is deliberately not score order.
        presented = [
            self._scored_row("recall-low", 0.5),
            self._scored_row("recall-top", 0.9),
            self._scored_row("recall-second", 0.88),
        ]
        scored = [(row["_score"], row) for row in presented]

        with patch.object(recall_mod, "_recall_one_tier", return_value=scored), \
                patch.dict(os.environ, {"ZMEM_INJECT_MARGIN": "0.05"},
                           clear=False), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            returned = recall_mod.recall_memory(
                conn, query="score margin", namespace=NS, limit=10,
                hybrid=False, no_mmr=True, link_hops=0, no_unfold=True,
                as_json=True, no_bump=True, for_injection=True,
            )
        envelope = json.loads(stdout.getvalue())
        self.assertEqual([r["id"] for r in returned], ["recall-top"])
        self.assertEqual([r["id"] for r in envelope["results"]],
                         ["recall-top"])
        self.assertEqual(envelope["candidate_ids"], ids)
        self.assertEqual(envelope["margin"], "0.022222")
        self.assertEqual(envelope["margin_pruned_ids"],
                         ["recall-second", "recall-low"])
        self.assertEqual(envelope["reason"], "injected")

        telemetry = {
            row["id"]: (row["surfaced_count"], row["last_surfaced"])
            for row in conn.execute(
                "SELECT id, surfaced_count, last_surfaced FROM memory "
                "WHERE id IN (?, ?, ?)", ids)
        }
        self.assertEqual(telemetry["recall-top"][0], 1)
        self.assertIsNotNone(telemetry["recall-top"][1])
        self.assertEqual(telemetry["recall-second"], (0, None))
        self.assertEqual(telemetry["recall-low"], (0, None))

        # With a threshold below the observed gap, all rows survive in their
        # original presentation order.  no_telemetry proves that this same
        # production path can be replayed without changing the final-row
        # telemetry established above.
        presented_again = [
            self._scored_row("recall-low", 0.5),
            self._scored_row("recall-top", 0.9),
            self._scored_row("recall-second", 0.88),
        ]
        with patch.object(recall_mod, "_recall_one_tier", return_value=[
                (row["_score"], row) for row in presented_again]), \
                patch.dict(os.environ, {"ZMEM_INJECT_MARGIN": "0.01"},
                           clear=False), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            replay = recall_mod.recall_memory(
                conn, query="score margin", namespace=NS, limit=10,
                hybrid=False, no_mmr=True, link_hops=0, no_unfold=True,
                as_json=True, no_bump=True, no_telemetry=True,
                for_injection=True,
            )
        replay_envelope = json.loads(stdout.getvalue())
        self.assertEqual([r["id"] for r in replay], ids)
        self.assertEqual(replay_envelope["candidate_ids"], ids)
        self.assertEqual(replay_envelope["margin"], "0.022222")
        self.assertEqual(replay_envelope["margin_pruned_ids"], [])
        self.assertEqual(
            telemetry,
            {
                row["id"]: (row["surfaced_count"], row["last_surfaced"])
                for row in conn.execute(
                    "SELECT id, surfaced_count, last_surfaced FROM memory "
                    "WHERE id IN (?, ?, ?)", ids)
            },
        )
        conn.close()

    def test_recent_injection_queryless_rows_fail_open_without_diagnostics(self):
        """Real recent rows have no production score and therefore fail open."""
        sys.path.insert(0, str(SCRIPTS))
        from storelib import recall as recall_mod

        conn = self._memory_conn()
        ids = ["recent-new", "recent-middle", "recent-old"]
        self._seed_memory_rows(conn, list(reversed(ids)))
        before = conn.execute(
            "SELECT id, content, surfaced_count, last_surfaced FROM memory "
            "WHERE namespace=? ORDER BY ingestion_ts DESC", (NS,)
        ).fetchall()
        with patch.dict(os.environ, {"ZMEM_INJECT_MARGIN": "0.05"},
                        clear=False), contextlib.redirect_stdout(
                            io.StringIO()) as stdout:
            returned = recall_mod.recent_memory(
                conn, namespace=NS, limit=10, as_json=True, no_bump=True,
                no_telemetry=True, for_injection=True,
            )
        envelope = json.loads(stdout.getvalue())
        self.assertEqual([r["id"] for r in returned], ids)
        self.assertEqual([r["id"] for r in envelope["results"]], ids)
        self.assertEqual(envelope["candidate_ids"], ids)
        self.assertNotIn("margin", envelope)
        self.assertNotIn("margin_pruned_ids", envelope)
        self.assertTrue(all("_score" not in row for row in returned))
        after = conn.execute(
            "SELECT id, content, surfaced_count, last_surfaced FROM memory "
            "WHERE namespace=? ORDER BY ingestion_ts DESC", (NS,)
        ).fetchall()
        self.assertEqual(before, after)
        conn.close()

    def test_recent_injection_scored_stage_applies_gate_before_budget(self):
        """An internal scored stage exercises recent's real gate placement."""
        sys.path.insert(0, str(SCRIPTS))
        from storelib import recall as recall_mod

        conn = self._memory_conn()
        ids = ["recent-low", "recent-top", "recent-second"]
        self._seed_memory_rows(conn, ids)
        presented = [
            self._scored_row("recent-low", 0.5),
            self._scored_row("recent-top", 0.9),
            self._scored_row("recent-second", 0.88),
        ]
        budget_rows = []
        real_budget = recall_mod.apply_token_budget

        def capture_budget(rows, **kwargs):
            budget_rows.append([row["id"] for row in rows])
            return real_budget(rows, **kwargs)

        with patch.object(recall_mod, "_recent_one_tier",
                          return_value=presented), \
                patch.object(recall_mod, "apply_token_budget",
                             side_effect=capture_budget), \
                patch.dict(os.environ, {"ZMEM_INJECT_MARGIN": "0.05"},
                           clear=False), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            returned = recall_mod.recent_memory(
                conn, namespace=NS, limit=10, as_json=True,
                no_bump=True, for_injection=True,
            )
        envelope = json.loads(stdout.getvalue())
        self.assertEqual([r["id"] for r in returned], ["recent-top"])
        self.assertEqual(envelope["candidate_ids"], ids)
        self.assertEqual(envelope["margin"], "0.022222")
        self.assertEqual(envelope["margin_pruned_ids"],
                         ["recent-second", "recent-low"])
        self.assertEqual(budget_rows, [["recent-top"]])
        self.assertEqual(envelope["budget_dropped"], 0)
        telemetry = {
            row["id"]: row["surfaced_count"]
            for row in conn.execute(
                "SELECT id, surfaced_count FROM memory "
                "WHERE id IN (?, ?, ?)", ids)
        }
        self.assertEqual(telemetry,
                         {"recent-low": 0, "recent-top": 1,
                          "recent-second": 0})
        conn.close()

    def test_explain_exact_id_precedes_fragment_collision(self):
        """Exact IDs are additive and win over a colliding content fragment."""
        sys.path.insert(0, str(SCRIPTS))
        from storelib import recall as recall_mod

        conn = self._memory_conn()
        exact_id = "collision-id"
        self._seed_memory_rows(conn, [exact_id, "fragment-row"],
                               content_prefix="content mentions collision-id ")
        rows, is_fragment = recall_mod._resolve_explain_targets(
            conn, exact_id, [NS])
        self.assertFalse(is_fragment)
        self.assertEqual([row["id"] for row in rows], [exact_id])
        conn.close()

    def test_explain_rejects_exclude_with_exit_two(self):
        env = self._env()
        init = subprocess.run(
            [sys.executable, str(STORE_PY), "init"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(init.returncode, 0, init.stderr)
        result = subprocess.run(
            [sys.executable, str(STORE_PY), "recall",
             "--query", "score margin", "--namespace", NS,
             "--explain", "--exclude", "not-present"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--exclude", result.stderr)

    def test_injection_explain_reports_effective_passive_mode(self):
        env = self._env()
        init = subprocess.run(
            [sys.executable, str(STORE_PY), "init"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(init.returncode, 0, init.stderr)
        result = subprocess.run(
            [sys.executable, str(STORE_PY), "recall",
             "--query", "score margin", "--namespace", NS,
             "--for-injection", "--explain", "--json"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["explain"]["no_bump"])

    def test_finite_zero_and_negative_runner_up_scores_are_usable(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib.inject import apply_score_margin

        cases = (
            ([{"id": "top", "_score": 0.8},
              {"id": "zero", "_score": 0.0}], 1.0),
            ([{"id": "top", "_score": 0.8},
              {"id": "negative", "_score": -0.1}], 1.125),
        )
        for rows, expected_margin in cases:
            with self.subTest(rows=rows):
                retained, observed, pruned = apply_score_margin(
                    rows, margin=0.05)
                self.assertEqual(retained, rows)
                self.assertEqual(observed, expected_margin)
                self.assertEqual(pruned, [])

        retained, observed, pruned = apply_score_margin(
            [{"id": "only", "_score": 0.8}], margin=0.05)
        self.assertEqual(retained, [{"id": "only", "_score": 0.8}])
        self.assertIsNone(observed)
        self.assertEqual(pruned, [])

    def test_explain_margin_pruned_scores_are_target_specific(self):
        sys.path.insert(0, str(SCRIPTS))
        from storelib import recall as recall_mod

        rows = [
            {"id": "m-top", "_score": 0.8, "type": "fact"},
            {"id": "m-second", "_score": 0.79, "type": "fact"},
            {"id": "m-third", "_score": 0.7, "type": "fact"},
        ]
        _retained, observed, pruned = recall_mod.apply_score_margin(
            rows, margin=0.05)
        self.assertEqual([row["id"] for row in pruned],
                         ["m-second", "m-third"])
        detail = recall_mod._explain_margin_detail(rows, observed, 0.05)
        details = {row["id"]: detail for row in pruned}
        scores = {row["id"]: row.get("_score") for row in pruned}

        for row in rows[1:]:
            target = {
                "id": row["id"], "namespace": NS,
                "superseded_at": None, "confidence": 0.9,
            }
            verdict = recall_mod._explain_verdict_for_target(
                None, target, as_of=None, hybrid=False,
                ns_list=[NS], global_ns_list=[], include_global=False,
                min_confidence=None, presented=[], omitted=[],
                project_deep=[], global_deep=[], margin_pruned=details,
                margin_pruned_scores=scores,
            )
            self.assertEqual(verdict["score"], row["_score"])
            self.assertEqual(verdict["detail"], detail)
            self.assertEqual(len(verdict["detail"]), 10)

    def test_envelope_margin_fields_reach_injected_and_silent_loggers(self):
        row = {
            "id": "m-top", "type": "fact", "signal": "test",
            "confidence": 0.9, "_score": 0.9,
            "content": "score margin integration row",
            "namespace": NS,
        }
        injected_code, _ = self._run_body(
            {
                "results": [row],
                "reason": "injected",
                "candidate_ids": ["m-top", "m-second"],
                "margin": "0.012500",
                "margin_pruned_ids": ["m-second"],
            },
            "margin-injected",
        )
        self.assertEqual(injected_code, 0)

        silent_code, _ = self._run_body(
            {
                "results": [],
                "reason": "below-bar",
                "candidate_ids": ["m-top", "m-second"],
                "margin": "0.012500",
                "margin_pruned_ids": [],
            },
            "margin-silent",
        )
        self.assertEqual(silent_code, 0)

        lines = self._decision_lines()
        self.assertEqual(len(lines), 2)
        normalized = [self._without_timestamp(line) for line in lines]
        self.assertEqual(
            normalized[0],
            "[TIMESTAMP] zmem-hook status=injected reason=injected "
            "ids=['m-top'] all=['m-top', 'm-second'] "
            "tokens=96/1500 rendered_estimate=96 "
            "sid=margin-injected moment=user_prompt margin=0.012500 "
            "margin_pruned=['m-second']",
        )
        self.assertEqual(
            normalized[1],
            "[TIMESTAMP] zmem-hook status=silent reason=below-bar "
            "ids=[] all=['m-top', 'm-second'] sid=margin-silent "
            "moment=user_prompt margin=0.012500",
        )

    def test_session_start_consumer_preserves_margin_diagnostics(self):
        row = {
            "id": "m-top", "type": "fact", "signal": "test",
            "confidence": 0.9, "_score": 0.9,
            "content": "score margin session-start row",
            "namespace": NS,
        }
        self._run_session_start(
            {
                "results": [row],
                "reason": "injected",
                "candidate_ids": ["m-top", "m-second"],
                "tokens_used": 96,
                "tokens_budget": 1500,
                "margin": "0.012500",
                "margin_pruned_ids": ["m-second"],
            },
            "session-start-injected",
        )
        self._run_session_start(
            {
                "results": [],
                "reason": "below-bar",
                "candidate_ids": ["m-top", "m-second"],
                "margin": 0.0,
                "margin_pruned_ids": [],
            },
            "session-start-silent",
        )
        lines = self._decision_lines()
        self.assertEqual(len(lines), 2)
        normalized = [self._without_timestamp(line) for line in lines]
        self.assertEqual(
            normalized[0],
            "[TIMESTAMP] zmem-hook status=injected reason=injected "
            "ids=['m-top'] all=['m-top', 'm-second'] tokens=96/1500 "
            "sid=session-start-injected moment=session_start "
            "margin=0.012500 margin_pruned=['m-second']",
        )
        self.assertEqual(
            normalized[1],
            "[TIMESTAMP] zmem-hook status=silent reason=below-bar "
            "ids=[] all=['m-top', 'm-second'] sid=session-start-silent "
            "moment=session_start margin=0.000000",
        )

    def test_session_start_consumer_validates_margin_fields_independently(self):
        row = {
            "id": "m-top", "type": "fact", "signal": "test",
            "confidence": 0.9, "_score": 0.9,
            "content": "score margin malformed envelope row",
            "namespace": NS,
        }
        self._run_session_start(
            {
                "results": [row],
                "reason": "injected",
                "margin": True,
                "margin_pruned_ids": ["m-second"],
            },
            "session-start-invalid-margin",
        )
        self._run_session_start(
            {
                "results": [row],
                "reason": "injected",
                "margin": "0.012500",
                "margin_pruned_ids": ["m-second", 17],
            },
            "session-start-invalid-pruned",
        )
        lines = self._decision_lines()
        normalized = [self._without_timestamp(line) for line in lines]
        self.assertIn(
            "sid=session-start-invalid-margin moment=session_start "
            "margin_pruned=['m-second']",
            normalized[0],
        )
        self.assertNotIn("margin=", normalized[0])
        self.assertIn(
            "sid=session-start-invalid-pruned moment=session_start "
            "margin=0.012500",
            normalized[1],
        )
        self.assertNotIn("margin_pruned=", normalized[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
