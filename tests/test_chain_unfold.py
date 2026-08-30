"""Tests for the change-intent lineage unfold — explicit recall only (issue #82).

Pins, per the issue's mandatory test table:
- `_CHANGE_INTENT_RES`: >= 8 positive queries fire, >= 8 negatives (ordinary
  hook-shaped coding prompts) do not — false positives are a merge blocker;
- explicit change-intent recall appends the tombstoned predecessor with
  `[PREVIOUSLY]` + `unfold_of`/`unfold_hop`;
- `--no-bump` (hooks), `--no-unfold`, and `search` do NOT unfold;
- budget/hop caps (incl. the ZMEM_UNFOLD_* env knobs) hold;
- unfold extras are NEVER in the telemetry bump set (retrieval_count stays 0);
- namespace isolation: a walk never crosses namespaces;
- injection-risk / untrusted predecessors are prefixed, not silently dropped;
- `--json` keys `unfold_of`/`unfold_hop` appear ONLY on extras.

Run: python tests/test_chain_unfold.py   (no pytest required — repo convention)
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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable
NS = "project:unfoldtest"

# ---------------------------------------------------------------------------
# In-process storelib loads in THIS file are pinned at MODULE IMPORT TIME
# (storelib freezes STORE_PATH on first import; an unpinned in-process
# connect() would resolve — and on schema skew, auto-migrate — the operator's
# real home store). The in-process tests below seed THIS store via the same
# subprocess env so the frozen path and the CLI path agree.
# ---------------------------------------------------------------------------
_INPROC_TMP = tempfile.mkdtemp(prefix="zmem-unfold-inproc")
INPROC_STORE = os.path.join(_INPROC_TMP, "inproc.sqlite")
os.environ["ZMEM_STORE"] = INPROC_STORE
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ["ZMEM_MODELS_DIR"] = "/nonexistent-zmem-models-dir"
os.environ["ZMEM_EMBED_PROFILE"] = "fake"
os.environ["ZMEM_TEST_NOW"] = "2026-06-01T00:00:00Z"


def _pin_env(store: str) -> dict:
    return {
        **os.environ,
        "ZMEM_STORE": store,
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_MODELS_DIR": "/nonexistent-zmem-models-dir",
        "ZMEM_TEST_NOW": "2026-06-01T00:00:00Z",
    }


class ChangeIntentRegexTests(unittest.TestCase):
    """The deterministic trigger. Pure unit tests — no store needed."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.recall import _is_change_intent_query
        cls.matches = staticmethod(_is_change_intent_query)

    POSITIVES = (
        "what changed about the lint gate",
        "what has changed in the deploy policy",
        "what did we change about the release notes",
        "what did we switch to for the CI cache",
        "why did we switch away from tarballs",
        "why did we replace the old runner",
        "why did we stop using the raw tarball route",
        "we used to sign builds with the release key",
        "the cache warmup used to be manual",
        "previously the pipeline ran without a gate",
        "before we merged this we ran the suite",
        "old vs new deploy policy",
        "is this superseded by the canary rollout",
        "was the tarball route replaced by zstd",
    )

    NEGATIVES = (
        "use pytest for the unit tests",
        "how do I configure the linter",
        "remember the printer IP address",
        "fix the failing test in the upload path",
        "add a retry to the webhook client",
        "what is the deploy policy",
        "python insertion sort implementation",
        "summarize the incident retrospective",
        "run the linter before committing",
        "the deploy is blocked, check the flag",
        "update the lockfile before the build",
        "how do I configure the staging cache",
    )

    def test_at_least_eight_positive_queries_fire(self):
        fired = [q for q in self.POSITIVES if self.matches(q)]
        self.assertGreaterEqual(len(fired), 8)
        self.assertEqual(fired, list(self.POSITIVES),
                         "every mandated positive phrasing must fire")

    def test_hook_shaped_negatives_never_fire(self):
        for q in self.NEGATIVES:
            self.assertFalse(self.matches(q),
                             f"false positive on hook-shaped prompt: {q!r}")


class UnfoldFixtureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = _pin_env(self.store)
        self._seed()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        r = subprocess.run([PYTHON, str(STORE_PY), "init"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for args in (
            ["--content", "lint gate runs biome with the default rule set",
             "--namespace", NS, "--type", "fact", "--signal", "test"],
            ["--content", "the scraper digest quotes the vendor changelog verbatim",
             "--namespace", NS, "--type", "fact", "--signal", "test"],
        ):
            r = subprocess.run([PYTHON, str(STORE_PY), "add", *args],
                               env=self.env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        # Build lineage through the REAL update path: predecessor tombstoned,
        # live successor written with update_of.
        conn = sqlite3.connect(self.store)
        try:
            self.pred_id = conn.execute(
                "SELECT id FROM memory WHERE content LIKE 'lint gate%'"
            ).fetchone()[0]
        finally:
            conn.close()
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "update", "--id", self.pred_id,
             "--content", "lint gate runs biome with the strict rule set"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE update_of=? AND superseded_at IS NULL",
                (self.pred_id,)).fetchone()
            self.head_id = row[0]
        finally:
            conn.close()
        # Tag the scraper row untrusted_web to exercise the prefix-not-drop
        # contract on a tainted predecessor later.
        conn = sqlite3.connect(self.store)
        try:
            self.scraper_id = conn.execute(
                "SELECT id FROM memory WHERE content LIKE '%scraper%'"
            ).fetchone()[0]
            conn.execute("UPDATE memory SET taint='untrusted_web' WHERE id=?",
                         (self.scraper_id,))
            conn.commit()
        finally:
            conn.close()

    def _recall(self, *args):
        return subprocess.run(
            [PYTHON, str(STORE_PY), "recall", *args],
            env=self.env, capture_output=True, text=True, timeout=120,
        )

    def _recall_json(self, *args) -> dict:
        r = self._recall("--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class UnfoldBehaviorTests(UnfoldFixtureBase):
    CI_QUERY = "what changed about the lint gate"

    def test_explicit_change_intent_appends_previously_predecessor(self):
        doc = self._recall_json("--query", self.CI_QUERY, "--namespace", NS)
        extras = [r for r in doc["results"] if r.get("unfold_hop")]
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0]["id"], self.pred_id)
        self.assertEqual(extras[0]["unfold_of"], self.head_id)
        self.assertEqual(extras[0]["unfold_hop"], 1)

    def test_text_render_marks_extras_previously(self):
        r = self._recall("--query", self.CI_QUERY, "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[PREVIOUSLY]", r.stdout)
        # The head row (a query match) must NOT carry the marker.
        head_line = next(line for line in r.stdout.splitlines()
                         if self.head_id in line)
        self.assertNotIn("[PREVIOUSLY]", head_line)

    def test_no_bump_never_unfolds(self):
        doc = self._recall_json("--query", self.CI_QUERY, "--namespace", NS,
                                "--no-bump")
        self.assertFalse(any(r.get("unfold_hop") for r in doc["results"]))

    def test_no_unfold_flag_disables(self):
        doc = self._recall_json("--query", self.CI_QUERY, "--namespace", NS,
                                "--no-unfold")
        self.assertFalse(any(r.get("unfold_hop") for r in doc["results"]))

    def test_search_subcommand_never_unfolds(self):
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "search", "--text", self.CI_QUERY,
             "--namespace", NS, "--json"],
            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertFalse(any(r.get("unfold_hop") for r in doc["results"]))

    def test_non_change_intent_query_does_not_unfold(self):
        doc = self._recall_json("--query", "biome rule set", "--namespace", NS)
        self.assertFalse(any(r.get("unfold_hop") for r in doc["results"]))

    def test_json_unfold_keys_only_on_extras(self):
        doc = self._recall_json("--query", self.CI_QUERY, "--namespace", NS)
        for r in doc["results"]:
            if r.get("unfold_hop"):
                self.assertIn("unfold_of", r)
            else:
                self.assertNotIn("unfold_of", r)
                self.assertNotIn("unfold_hop", r)


class UnfoldBudgetTests(unittest.TestCase):
    """Caps and env knobs, exercised through the library seam IN-PROCESS.

    These run against the module-pinned INPROC store (storelib freezes
    STORE_PATH at first import — the CLI seeds below write to the same path).
    Each test scopes its chains with a DISTINCT keyword pair so coexisting
    chains from sibling tests cannot crowd the TOP_K presented set."""

    def _make_chain(self, kw: str, topic: str) -> list[str]:
        """Build a depth-long update chain through the real `update` path;
        return [root, rev0, rev1, rev2(head)]. Fillers are lexically distinct
        per chain so the fake embedder's coarse 16-bucket hash can never
        cosine-dedup a chain onto another row."""
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "add", "--content",
             f"{kw} baseline {topic}",
             "--namespace", NS, "--type", "fact", "--signal", "test"],
            env=_pin_env(INPROC_STORE), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        conn = sqlite3.connect(INPROC_STORE)
        try:
            cur = conn.execute(
                "SELECT id FROM memory WHERE content LIKE ?",
                (f"{kw} baseline {topic}",),
            ).fetchone()[0]
        finally:
            conn.close()
        chain = [cur]
        for i in range(3):
            r = subprocess.run(
                [PYTHON, str(STORE_PY), "update", "--id", cur, "--content",
                 f"{kw} revision {i} {topic} adjusted"],
                env=_pin_env(INPROC_STORE), capture_output=True, text=True,
                timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            conn = sqlite3.connect(INPROC_STORE)
            try:
                cur = conn.execute(
                    "SELECT id FROM memory WHERE update_of=?",
                    (cur,)).fetchone()[0]
            finally:
                conn.close()
            chain.append(cur)
        return chain  # [root, rev0, rev1, rev2(head)]

    def test_max_hops_caps_the_walk(self):
        # chain = [root, rev0, rev1, rev2(head)]; the walk from the head goes
        # rev2 -> rev1 -> rev0, so with MAX_HOPS=1 only rev1 (p2) appears.
        _root, p1, p2, _head = self._make_chain(
            "escalation matrix", "alpha billing exports")
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.schema import connect
        from storelib.recall import recall_memory
        conn = connect()
        try:
            with mock.patch.dict(os.environ, {"ZMEM_UNFOLD_MAX_HOPS": "1"}):
                rows = recall_memory(
                    conn, query="what changed about the escalation matrix",
                    namespace=NS, no_bump=False, no_telemetry=True,
                    link_hops=1, link_budget=0, no_mmr=True)
            extras = {r["id"]: r["unfold_hop"] for r in rows
                      if r.get("unfold_hop")}
            self.assertIn(p2, extras,
                          "the immediate predecessor must be the first hop")
            self.assertNotIn(p1, extras,
                             "ZMEM_UNFOLD_MAX_HOPS=1 must stop after one hop")
        finally:
            conn.close()

    def test_budget_caps_total_extras(self):
        # Three independent chains (one revision each — the walk stops when
        # update_of runs out); one query matching all three heads: extras
        # must cap at ZMEM_UNFOLD_BUDGET=2.
        for topic in ("kubernetes ingress wiring", "postgres vacuum cadence",
                      "redis eviction tuning"):
            self._make_chain("handoff protocol", topic)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.schema import connect
        from storelib.recall import recall_memory
        conn = connect()
        try:
            with mock.patch.dict(os.environ, {"ZMEM_UNFOLD_BUDGET": "2"}):
                rows = recall_memory(
                    conn, query="what changed about the handoff protocol",
                    namespace=NS, no_bump=False, no_telemetry=True,
                    link_hops=1, link_budget=0, no_mmr=True)
            extras = [r for r in rows if r.get("unfold_hop")]
            self.assertLessEqual(len(extras), 2)
        finally:
            conn.close()

    def test_env_knob_garbage_falls_back(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.recall import _env_int
        with mock.patch.dict(os.environ, {"ZMEM_UNFOLD_TEST_K": "banana"}):
            self.assertEqual(_env_int("ZMEM_UNFOLD_TEST_K", 3, lo=1, hi=10), 3)
        with mock.patch.dict(os.environ, {"ZMEM_UNFOLD_TEST_K": "99"}):
            self.assertEqual(_env_int("ZMEM_UNFOLD_TEST_K", 3, lo=1, hi=10), 10,
                             "out-of-range values clamp, never fail")


class UnfoldSafetyTests(UnfoldFixtureBase):
    CI_QUERY = "what changed about the lint gate"

    def test_extras_never_bump_retrieval_count(self):
        before = self._counts()
        r = self._recall("--query", self.CI_QUERY, "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self._counts()
        # Explicit recall bumps ONLY the query-matched head; the predecessor
        # extra (and its history) must stay untouched. (last_retrieved may
        # legitimately carry a timestamp from the update path's own internal
        # dedup machinery; the telemetry contract is the COUNTER.)
        self.assertEqual(after[self.pred_id][0], before[self.pred_id][0])
        self.assertEqual(after[self.pred_id][0], 0,
                         "unfold extras must never enter the bump set")
        self.assertEqual(after[self.head_id][0], before[self.head_id][0] + 1)

    def _counts(self) -> dict[str, tuple[int, object]]:
        conn = sqlite3.connect(self.store)
        try:
            return {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT id, retrieval_count, last_retrieved FROM memory")
            }
        finally:
            conn.close()

    def test_namespace_isolation(self):
        # Hand-plant a cross-namespace predecessor link: the walk must refuse
        # to follow it even though update_of points outward.
        conn = sqlite3.connect(self.store)
        try:
            conn.execute(
                "UPDATE memory SET update_of=? WHERE id=?",
                ("cross-ns-foreign-id", self.head_id))
            conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    source_hash, confidence, signal, valid_from, valid_until,
                    update_of, taint, superseded_at, ingestion_ts)
                   VALUES ('cross-ns-foreign-id', 'project:unfolding-other',
                           'fact', 'foreign namespace policy about gates',
                           'eval', '', '', 0.9, 'test',
                           '2026-01-01T00:00:00Z', '', '', 'trusted_internal',
                           NULL, '2026-01-01T00:00:00Z')""")
            conn.commit()
        finally:
            conn.close()
        doc = self._recall_json("--query", self.CI_QUERY, "--namespace", NS)
        ids = {r["id"] for r in doc["results"]}
        self.assertNotIn("cross-ns-foreign-id", ids,
                         "a lineage walk must never cross namespaces")

    def test_untrusted_predecessor_is_prefixed_not_dropped(self):
        # Make the scraper row the change-intent hit by querying it, with a
        # lineage under it — the explicit path keeps taint visible.
        subprocess.run(
            [PYTHON, str(STORE_PY), "update", "--id", self.scraper_id,
             "--content", "the scraper digest now quotes the vendor blog"],
            env=self.env, capture_output=True, text=True, timeout=60)
        r = self._recall("--query",
                         "what changed about the scraper digest",
                         "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[PREVIOUSLY]", r.stdout)
        prev_block = r.stdout.split("[PREVIOUSLY]")[1]
        self.assertIn("[UNTRUSTED WEB]", prev_block,
                      "the operator asked what changed; taint is prefixed, "
                      "never silently dropped")

    def test_unfold_fail_open_swallows_internal_errors(self):
        # PRR-008: the unfold's fail-open contract — a lineage-read failure
        # must degrade to "no extras", never fail the explicit recall (the
        # analogous explain fail-open is pinned in test_explain_recall.py).
        # Runs against the module-pinned INPROC store with a hand-seeded
        # chain head so the unfold gate actually fires.
        r = subprocess.run(
            [PYTHON, str(STORE_PY), "init"],
            env=_pin_env(INPROC_STORE), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.schema import connect
        conn = connect()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO memory
                   (id, namespace, type, content, tags, source_ref,
                    source_hash, confidence, signal, valid_from,
                    valid_until, update_of, taint, superseded_at,
                    ingestion_ts)
                   VALUES ('unfold-failopen-head', ?, 'fact',
                           'foldout checklist gates the release train',
                           'eval', '', '', 0.9, 'test',
                           '2026-01-01T00:00:00Z', '', 'dead-lineage-id',
                           'trusted_internal', NULL,
                           '2026-01-01T00:00:00Z')""",
                (NS,),
            )
            conn.commit()
        finally:
            conn.close()
        from storelib import recall as recall_mod
        conn = connect()
        original = recall_mod._fetch_lineage_rows

        def _boom(*a, **k):
            raise RuntimeError("lineage read exploded")

        recall_mod._fetch_lineage_rows = _boom
        try:
            rows = recall_mod.recall_memory(
                conn, query="what changed about the foldout checklist",
                namespace=NS, no_bump=False, no_telemetry=True,
                link_hops=1, link_budget=0, no_mmr=True)
        finally:
            recall_mod._fetch_lineage_rows = original
            conn.close()
        self.assertIsInstance(rows, list)
        self.assertIn("unfold-failopen-head", [r["id"] for r in rows],
                      "the recall itself must survive the lineage failure")
        self.assertFalse(any(r.get("unfold_hop") for r in rows),
                         "a lineage-read failure must yield no extras, "
                         "not a failed recall")

    def test_source_scan_no_unfold_on_passive_surfaces(self):
        """Passive argv must never carry --no-unfold: the no_bump gate already
        excludes them structurally; the flag is an operator opt-out."""
        offenders = []
        for path in list((REPO_ROOT / "hooks").rglob("*.py")) + \
                list((REPO_ROOT / "hooks").rglob("*.sh")) + \
                [REPO_ROOT / "hermes-plugin" / "__init__.py"] + \
                list((REPO_ROOT / "hermes-plugin" / "server").rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "--no-unfold" in text or "--explain" in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [],
                         f"passive surfaces must not pass unfold/explain "
                         f"flags: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
