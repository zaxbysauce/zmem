"""Issue #114 (P2-3): the passive injection lane counts only what it renders.

Three acceptance criteria from the issue, as executable pins:
  1. A passive `recall --no-bump --for-injection` and `recent --for-injection`
     leave surfaced_count, retrieval_count and last_surfaced UNCHANGED for
     rows that were not rendered (gate-dropped or budget-dropped).
  2. Rendered rows are counted exactly once per decision.
  3. The monotonic-inflation reproduction (five identical recalls with the
     clock pinned via ZMEM_TEST_NOW) shows stable scores — popularity no
     longer reads surfaced_count.

Truth table (recall/recent):
  default                -> retrieval_count+1 on matched rows (unchanged, #21)
  --no-bump              -> surfaced_count+1 on returned rows  (unchanged, #21)
  --for-injection        -> surfaced_count+1 on (gate+budget survivors) ∩
                            (pre-expansion matched set); never retrieval_count
  ... + no_telemetry     -> zero writes (filters still run)

Drives the REAL store.py CLI via subprocess against throwaway temp stores —
the same path the hooks exercise — never the box store. The one in-process
test (compute_score popularity twin) pins env BEFORE importing storelib, per
the storelib STORE_PATH-freeze hazard.

Run: python tests/test_zero_write_passive.py   (no pytest required)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable
NS = "project:zerowrite114"

# Module-top env pin BEFORE any storelib import (STORE_PATH freezes at first
# import from ambient env — the near-mutated-real-store hazard). The in-process
# test below re-points the env and calls _refresh_env_state().
_BOOT_TMP = tempfile.mkdtemp(prefix="zmem-zerowrite-boot-")
os.environ["ZMEM_STORE"] = os.path.join(_BOOT_TMP, "store.sqlite")
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
import storelib  # noqa: E402  (env pinned above, per module docstring)


def _close_silently() -> None:
    try:
        conn = sqlite3.connect(
            "file:" + os.environ["ZMEM_STORE"].replace(os.sep, "/") + "?mode=ro",
            uri=True)
        conn.close()
    except Exception:
        pass


class ForInjectionBase(unittest.TestCase):
    """Fixture: one gate-passing row (test/0.9), one gate-dropped row
    (none/0.3, under the 0.4 signal=none floor)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-zerowrite-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_AUTO_REKEY": "0", "ZMEM_INJECT": "1"}
        self.env.pop("ZMEM_TEST_NOW", None)
        self.env.pop("ZMEM_INJECT_TOKEN_BUDGET", None)
        self._run("add", "--namespace", NS, "--type", "lesson",
                  "--content", "keep the flange calibrated before every launch",
                  "--tags", "flange", "--signal", "test",
                  "--confidence", "0.9", "--json")
        # The dropped row deliberately shares the token "flange" with the
        # rendered row: pool membership must not depend on embeddings
        # (model-absent CI legs), only the gate decides its fate.
        self._run("add", "--namespace", NS, "--type", "lesson",
                  "--content", "flange vibes only opinion note about nothing much",
                  "--tags", "vibes", "--signal", "none",
                  "--confidence", "0.3", "--json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, extra_env=None):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def _counts(self):
        """{id-prefix: (retrieval_count, surfaced_count, last_surfaced)}."""
        conn = sqlite3.connect(self.store)
        try:
            return {
                row[0][:8]: (row[1], row[2], row[3])
                for row in conn.execute(
                    "SELECT id, retrieval_count, surfaced_count, last_surfaced "
                    "FROM memory WHERE superseded_at IS NULL "
                    "ORDER BY ingestion_ts")
        }
        finally:
            conn.close()

    def _signal_of(self, prefix):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(
                "SELECT signal FROM memory WHERE id LIKE ?", (prefix + "%",)
            ).fetchone()[0]
        finally:
            conn.close()


class AcceptanceRecallTest(ForInjectionBase):
    """AC1+AC2 on the recall lane, via the real CLI (the hook argv)."""

    def test_gate_dropped_rows_unchanged_rendered_counted_once(self):
        counts = self._counts()
        dropped = next(p for p, _ in
                       ((p, self._signal_of(p)) for p in counts)
                       if _ == "none")
        rendered = next(p for p in counts if p != dropped)
        self.assertEqual(counts[dropped][1], 0)
        self.assertIsNone(counts[dropped][2])

        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--limit", "5",
                      "--no-bump", "--for-injection", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)

        # The envelope reports the decision honestly.
        self.assertEqual(doc["reason"], "injected")
        self.assertEqual(set(i[:8] for i in doc["candidate_ids"]),
                         set(self._counts()))
        self.assertEqual([x["id"][:8] for x in doc["results"]], [rendered])

        after = self._counts()
        # AC1: rows that were NOT rendered are untouched.
        self.assertEqual(after[dropped], counts[dropped],
                         "gate-dropped row must keep surfaced/retrieval/"
                         "last_surfaced unchanged")
        # AC2: the rendered row is counted exactly once for this decision.
        self.assertEqual(after[rendered][1], 1)
        self.assertIsNotNone(after[rendered][2])
        # The injection lane never writes retrieval telemetry.
        self.assertEqual(after[rendered][0], 0)
        self.assertEqual(after[dropped][0], 0)

    def test_second_decision_counts_exactly_one_more(self):
        for _ in range(2):
            r = self._run("recall", "--query", "flange calibrated launch",
                          "--namespace", NS, "--limit", "5",
                          "--no-bump", "--for-injection", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
        counts = self._counts()
        rendered = [p for p, c in counts.items() if c[1] > 0]
        dropped = [p for p, c in counts.items() if c[1] == 0]
        self.assertEqual(len(rendered), 1)
        self.assertEqual(counts[rendered[0]][1], 2,
                         "exactly one surface event per decision, two decisions")
        self.assertTrue(all(counts[p][1] == 0 for p in dropped))

    def test_budget_dropped_rows_unchanged_and_reason_budget_drop(self):
        # A budget no normal row can fit under (fence overhead alone is 12) —
        # gate passes, admission wipes the set: the #87 budget-drop shape.
        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--limit", "5",
                      "--no-bump", "--for-injection", "--json",
                      extra_env={"ZMEM_INJECT_TOKEN_BUDGET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["results"], [])
        self.assertEqual(doc["reason"], "budget-drop")
        self.assertTrue(doc["candidate_ids"], "pre-gate ids still reported")
        for prefix, before in self._counts().items():
            self.assertEqual(self._counts()[prefix], before,
                             f"budget-dropped row {prefix} must stay untouched")

    def test_for_injection_implies_passive_semantics_without_no_bump(self):
        # --for-injection WITHOUT --no-bump: the lane is passive by
        # construction — surfaced-style telemetry, never retrieval_count.
        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--limit", "5",
                      "--for-injection", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(json.loads(r.stdout)["count"], 1)
        self.assertTrue(all(c[0] == 0 for c in self._counts().values()),
                        "retrieval_count never written on the injection lane")


class AcceptanceRecentTest(ForInjectionBase):
    """AC1+AC2 on the recent lane (SessionStart/PreCompact/subagent)."""

    def test_rendered_counted_once_and_gate_runs_for_shape_parity(self):
        # The recent SQL floor (0.5) already dominates the default 0.4
        # none-gate, so raise the gate via its env override to prove the
        # in-store gate actually runs on recent too: a none/0.55 row passes
        # SQL, then drops at the gate, untouched.
        self._run("add", "--namespace", NS, "--type", "lesson",
                  "--content", "borderline none-signal opinion middling note",
                  "--tags", "border", "--signal", "none",
                  "--confidence", "0.55", "--json")
        before = self._counts()
        none_rows = [p for p in before if self._signal_of(p) == "none"]
        self.assertEqual(len(none_rows), 2)
        # Identify by confidence: 0.3 (below SQL floor, never a candidate)
        # vs 0.55 (passes SQL 0.5, gate-dropped at the raised 0.9 floor).
        conn = sqlite3.connect(self.store)
        try:
            conf = {row[0][:8]: row[1] for row in conn.execute(
                "SELECT id, confidence FROM memory "
                "WHERE superseded_at IS NULL")}
        finally:
            conn.close()
        borderline = next(p for p in none_rows
                          if abs(conf[p] - 0.55) < 1e-9)

        r = self._run("recent", "--namespace", NS, "--limit", "5",
                      "--min-confidence", "0.5",
                      "--no-bump", "--for-injection", "--json",
                      extra_env={"ZMEM_INJECT_FLOOR_GATE_NONE": "0.9"})
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["reason"], "injected")
        # Only the test/0.9 row renders; both signal=none rows are absent.
        self.assertEqual([x["signal"] for x in doc["results"]], ["test"])
        self.assertNotIn(borderline, [x["id"][:8] for x in doc["results"]])
        after = self._counts()
        for p in none_rows:
            self.assertEqual(after[p], before[p],
                             "gate-dropped (or SQL-floored) recent row must "
                             "stay untouched")
        rendered = [p for p in after if after[p][1] == 1]
        self.assertEqual(len(rendered), 1, "one rendered row, one surface event")
        self.assertEqual(after[rendered[0]][0], 0, "never retrieval_count")

    def test_bare_recent_no_bump_still_records_surfaces(self):
        # Issue #21 regression pin: bare --no-bump behavior is UNCHANGED by
        # this work — the loop is broken by the scoring change, not by
        # deleting the passive-surface record. (The none/0.3 row sits under
        # the 0.5 SQL floor, so one row returns and records one surface.)
        r = self._run("recent", "--namespace", NS, "--limit", "5",
                      "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["count"], 1)
        counts = self._counts()
        surfaced = [p for p, c in counts.items() if c[1] == 1]
        self.assertEqual(surfaced, [doc["results"][0]["id"][:8]],
                         "every RETURNED row records exactly one surface")
        self.assertEqual(sum(c[1] for c in counts.values()), 1)


class EnvelopeContractTest(ForInjectionBase):
    """Plain-path output must stay byte-compatible (characterization freeze)."""

    def test_plain_no_bump_envelope_has_no_flag_only_keys(self):
        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--limit", "5",
                      "--no-bump", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertNotIn("reason", doc)
        self.assertNotIn("candidate_ids", doc)
        self.assertEqual(sorted(doc), ["count", "injection_risk", "omitted",
                                       "results", "tokens_budget",
                                       "tokens_used"])

    def test_for_injection_envelope_carries_reason_and_candidates(self):
        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--limit", "5",
                      "--no-bump", "--for-injection", "--json")
        doc = json.loads(r.stdout)
        self.assertEqual(doc["reason"], "injected")
        self.assertEqual(len(doc["candidate_ids"]), 2)
        self.assertIn("tokens_budget", doc)


class InflationStabilityTest(ForInjectionBase):
    """AC3: five identical recalls with the clock pinned -> stable scores."""

    def test_five_identical_for_injection_recalls_score_stable(self):
        scores = []
        for _ in range(5):
            r = self._run("recall", "--query", "flange calibrated launch",
                          "--namespace", NS, "--limit", "5",
                          "--no-bump", "--for-injection", "--json",
                          extra_env={"ZMEM_TEST_NOW": "2026-09-03T12:00:00Z"})
            self.assertEqual(r.returncode, 0, r.stderr)
            doc = json.loads(r.stdout)
            scores.append({x["id"][:8]: x["_score"] for x in doc["results"]})
        for round_n in range(1, 5):
            self.assertEqual(scores[round_n], scores[0],
                             f"round {round_n + 1} scores drifted — the "
                             "popularity loop is back")
        # The rendered row still recorded its five surface events (counted),
        # they just no longer feed the score.
        counts = self._counts()
        rendered = [c[1] for c in counts.values() if c[1] > 0]
        self.assertEqual(rendered, [5])


class ZeroWriteTest(ForInjectionBase):
    """--explain stays zero-write; the eval seam (no_telemetry) is zero-write
    even on the injection lane."""

    def test_explain_leaves_store_bytes_identical(self):
        before = Path(self.store).read_bytes()
        r = self._run("recall", "--query", "flange calibrated launch",
                      "--namespace", NS, "--explain", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(self.store).read_bytes(), before)

    def test_for_injection_with_no_telemetry_is_zero_write(self):
        before = Path(self.store).read_bytes()
        os.environ["ZMEM_STORE"] = self.store
        storelib._refresh_env_state()
        import io
        import contextlib
        conn = sqlite3.connect(
            "file:" + self.store.replace(os.sep, "/") + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row  # storelib readers index by column name
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rows = storelib.recall_memory(
                    conn, query="flange calibrated launch", namespace=NS,
                    limit=5, as_json=False, no_bump=True, for_injection=True,
                    no_telemetry=True)
            self.assertEqual(len(rows), 1, "filters ran; the gate still applies")
        finally:
            conn.close()
        self.assertEqual(Path(self.store).read_bytes(), before,
                         "no_telemetry must suppress the surfaced write while "
                         "the gate/budget filters still run")


class PopularityInputTest(unittest.TestCase):
    """compute_score popularity reads retrieval_count only (issue #114)."""

    ROW_BASE = {"confidence": 0.9, "ingestion_ts": "2026-09-01T00:00:00Z"}

    def _score(self, retrieval, surfaced):
        row = {**self.ROW_BASE, "retrieval_count": retrieval,
               "surfaced_count": surfaced}
        return storelib.compute_score(row, None, 1780000000.0, vec_sim=0.5)

    def test_surfaced_count_no_longer_feeds_the_score(self):
        # Pre-#114 this pair differed; the loop lived here.
        self.assertEqual(self._score(0, 0), self._score(0, 999))

    def test_retrieval_count_still_feeds_the_score(self):
        self.assertLess(self._score(0, 999), self._score(5, 0))

    def test_weights_unchanged_and_normalized(self):
        self.assertAlmostEqual(
            storelib.W_BM25 + storelib.W_CONFIDENCE
            + storelib.W_RECENCY + storelib.W_POPULARITY, 1.0)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_BOOT_TMP, ignore_errors=True)
