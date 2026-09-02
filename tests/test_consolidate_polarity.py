"""Issue #71 G: consolidation polarity — contest only same-predicate negation.

Field report: the ANY-negator polarity signature contested false positives on
user:global — a preference restated as a lesson, and the same production
constraint restated with the negation relocated — while true contradictions
("is live" vs "is not live") must KEEP parking, and historical-vs-current
facts must never merge.

Three-band relation (`_negation_relation`), used by the CONSOLIDATE cluster
decision ONLY:
- contradiction: negator-stripped predicates near-identical AND the negation
  directly targets the shared predicate -> contested (as before);
- restatement: near-identical predicates where the negation carries its OWN
  verb ("do not SKIP a rotate"), or enough shared predicate content — MERGES
  in consolidate(); the WRITE-TIME guard keeps the #61 polarity-flip
  contract (restated pairs stay separate rows + contradicts links, which the
  eval corpus's discrimination pairs depend on);
- divergent: everything else — parks (conservative; never merges).

Calibration set (each pair is pinned below; the thresholds
CONTEST_CONTRADICTION_JACCARD / CONTEST_RESTATEMENT_* are chosen so every
pair lands in its band):
  C1 "the hermes gateway is live / is not live on the spark rig"  -> contradiction
  C2 "production-v2 is / is not the live production model"        -> contradiction
  R1 "rotate mcp bearer token every ninety days" vs
     "Operator preference: do not SKIP a rotate ..." (issue repro) -> restatement
  R2 "Restarting the fleet gateway requires operator approval" vs
     "Operator preference: do not restart ... without approval"  -> contradiction
     (PRR-009 trade: direct negation of the shared verb parks — the
     conservative pre-#71-G outcome; tokens cannot separate it from a
     true flip)
  R3 "Must not replace ChatGPTN on :8000 without operator approval
      before cutover" vs "Replacing ChatGPTN ... requires operator
      approval before cutover begins" (issue's constraint restated) -> contradiction
      (same PRR-009 trade as R2)
  D1 "EUGR is no longer the production model" vs
     "DeepSeek-V4-Flash-0731 is the production model"              -> divergent
  D2 "rotate the mcp bearer token every ninety days" vs
     "rotate the api key every ninety days" (different subject
     decoy with high lexical overlap)                              -> divergent
  E1 half-empty content                                            -> divergent

NLI judge interaction (critic #11): restatement clusters SKIP the judge
entirely; ZMEM_NLI_CMD adjudicates contradictions only.

Run: python tests/test_consolidate_polarity.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# ISOLATION FIRST (repo convention — storelib freezes STORE_PATH at first
# import, so the env must be pinned BEFORE any storelib import): everything
# in this module — subprocess stores AND the in-process WriteTimeGuardTest —
# lands in this throwaway dir, never the box store.
_TMP_IMPORT_STORE = tempfile.mkdtemp(prefix="zmem-polarity-import-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP_IMPORT_STORE, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP_IMPORT_STORE
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_TMP_IMPORT_STORE, "no-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ.setdefault("ZMEM_NLI_CMD", "")
# PRR-023: an ambient fake embed profile flips consolidate's lexical
# fallback to the cosine path and the fixtures stop clustering — strip it
# (the subprocess env inherits this process's os.environ).
os.environ.pop("ZMEM_EMBED_PROFILE", None)
sys.path.insert(0, str(SCRIPTS_DIR))

import shutil  # noqa: E402
import subprocess  # noqa: E402

from storelib import schema as schema_mod  # noqa: E402
from storelib.consolidate import (  # noqa: E402
    _light_stem,
    _negation_relation,
    dedup_polarity_conflict,
)


def _atexit_cleanup():
    shutil.rmtree(_TMP_IMPORT_STORE, ignore_errors=True)


import atexit  # noqa: E402
atexit.register(_atexit_cleanup)

C1A = "the hermes gateway is live on the spark rig"
C1B = "the hermes gateway is not live on the spark rig"
C2A = "production-v2 is the live production model on the tower"
C2B = "production-v2 is not the live production model on the tower"
R1A = "rotate the mcp bearer token every ninety days on the tower"
R1B = ("Operator preference: do not skip a rotate of the mcp bearer token "
       "every ninety days on the tower")
R2A = "Restarting the fleet gateway requires operator approval first"
R2B = ("Operator preference: do not restart the fleet gateway without "
       "operator approval first")
R3A = ("Must not replace ChatGPTN on :8000 without operator approval before "
       "cutover")
R3B = ("Replacing ChatGPTN on :8000 requires operator approval before "
       "cutover begins")
D1A = "EUGR is no longer the production model on the tower"
D1B = "DeepSeek-V4-Flash-0731 is the production model on the tower"
D2A = "rotate the mcp bearer token every ninety days on the tower"
D2B = "rotate the api key credential every ninety days on the cluster"


class NegationRelationUnitTest(unittest.TestCase):
    """The calibration set at the helper level — every band boundary the
    thresholds were chosen against."""

    def test_c1_contradiction(self):
        self.assertEqual(
            _negation_relation(C1A, C1B), "contradiction")

    def test_c2_contradiction(self):
        self.assertEqual(
            _negation_relation(C2A, C2B), "contradiction")

    def test_r1_issue_repro_pair_is_restatement(self):
        self.assertEqual(
            _negation_relation(R1A, R1B), "restatement")

    def test_r2_direct_negation_of_shared_verb_parks(self):
        # PRR-009 fix: R2's negator ("do not RESTART …") targets a verb the
        # other row shares, so tokens cannot separate it from a true flip
        # ("never restart …"). It parks (contradiction) — the pre-#71-G
        # behavior for this shape. Merging it safely needs semantics, not
        # tokens; parking never loses a contradiction.
        self.assertEqual(
            _negation_relation(R2A, R2B), "contradiction")

    def test_r3_direct_negation_of_shared_verb_parks(self):
        # PRR-009 fix, same trade as R2: "must not REPLACE …" vs "replacing …
        # requires …" parks. Conservative outcome: no merge, no data loss.
        self.assertEqual(
            _negation_relation(R3A, R3B), "contradiction")

    def test_d1_historical_vs_current_parks(self):
        # Outcome contract: the historical-vs-current pair NEVER merges.
        # The label is band-dependent (D1's negator "no longer" precedes the
        # shared word "production", so the PRR-009 fix labels it
        # contradiction; either parked label satisfies the invariant).
        self.assertIn(
            _negation_relation(D1A, D1B), ("contradiction", "divergent"))
        self.assertNotEqual(
            _negation_relation(D1A, D1B), "restatement",
            "historical-vs-current facts must never classify as a mergeable "
            "restatement")

    def test_prr009_always_never_pair_is_contradiction(self):
        """PRR-009 regression (the reviewer's mechanically-proven pair):
        'always rotate …' vs 'never rotate …' (j≈0.889, one-token symmetric
        difference) previously classified as restatement and MERGED a true
        contradiction. The negation-target discriminator must run in every
        band: 'never' directly precedes the shared verb 'rotate' →
        contradiction (park)."""
        a = "always rotate the api deploy key before every fleet push"
        b = "never rotate the api deploy key before every fleet push"
        self.assertEqual(_negation_relation(a, b), "contradiction")

    def test_d2_different_subject_decoy_parks(self):
        # High lexical overlap, different subject: the containment/jaccard
        # bands must not merge unrelated predicates.
        self.assertEqual(
            _negation_relation(D2A, D2B), "divergent")

    def test_e1_empty_side_is_divergent(self):
        self.assertEqual(
            _negation_relation("", C1B), "divergent")

    def test_stemmer_folds_inflections(self):
        self.assertEqual(
            _light_stem("rotating"),
            _light_stem("rotate"))
        self.assertEqual(
            _light_stem("requires"),
            _light_stem("require"))


class _ClusterStoreCase(unittest.TestCase):
    """Temp-store fixture for end-to-end consolidate runs (model-absent:
    lexical fallback clustering, per repo test convention)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-polarity-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {**os.environ}
        env["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        env["ZMEM_DATA"] = self.tmp
        env["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env["ZMEM_NLI_CMD"] = ""
        env["PYTHONUTF8"] = "1"
        self.env = env
        subprocess_init = __import__("subprocess").run(
            [PYTHON, str(STORE_PY), "stats"], capture_output=True, text=True,
            env=env, check=True)
        import datetime
        import uuid
        self.conn = sqlite3.connect(env["ZMEM_STORE"])
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def seed(self, content: str, ns: str = "user:global") -> str:
        now = "2026-09-01T00:00:00+00:00"
        mid = f"{self.seed_n:08x}-0000-0000-0000-000000000000" if hasattr(
            self, "seed_n") else None
        self.seed_n = getattr(self, "seed_n", 0) + 1
        mid = f"deadbeef-{self.seed_n:04d}-4000-8000-000000000000"
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
            "confidence, signal, ingestion_ts, taint) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, ns, "lesson", content, "[]", "session:polarity", 0.9, "none",
             now, "trusted_internal"))
        return mid

    def run_consolidate(self, *args):
        import subprocess
        return subprocess.run([PYTHON, str(STORE_PY), "consolidate", *args],
                              capture_output=True, text=True, env=self.env)


class RestatementMergesTest(_ClusterStoreCase):
    """R1 (the issue's reproduced false positive) must now MERGE."""

    def test_issue_repro_restatement_pair_merges(self):
        self.seed(R1A)
        self.seed(R1B)
        self.conn.commit()
        self.conn.close()
        r = self.run_consolidate("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("uncontested", r.stdout)
        self.assertIn("restatement", r.stdout)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        live = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(live, 1, "restatement pair must collapse to one row")

    def test_dry_run_restatement_never_double_reports(self):
        """Reviewer-gate pin for the dry-run split (PRR-004 critic follow-up):
        a --dry-run over an R1 pair counts the counterfactual in
        `uncontested_restatements` and must NOT also emit the contested
        preview ("would merge CONTESTED cluster") nor append the cluster to
        `contested_clusters`. Reverting the dry-run `pass` to the shared
        contested-preview path makes BOTH assertions fail."""
        self.seed(R1A)
        self.seed(R1B)
        self.conn.commit()
        self.conn.close()
        r = self.run_consolidate("--force", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("uncontested", r.stdout)
        self.assertNotIn("CONTESTED", r.stdout,
                         "a restatement cluster is not contested; the dry-run "
                         "preview must not emit the contested block")
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        live = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(live, 2, "dry-run merges nothing")

    def test_contradiction_pair_still_contested(self):
        self.seed(C1A)
        self.seed(C1B)
        self.conn.commit()
        self.conn.close()
        r = self.run_consolidate("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CONTESTED", r.stdout, r.stdout + r.stderr)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        live = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(live, 2, "true contradiction must keep both rows")

    def test_prr009_always_never_pair_never_merges_e2e(self):
        """PRR-009 e2e regression: the reviewer's proven pair ("always rotate
        …" / "never rotate …", lexical Jaccard 0.8 — it CLUSTERS) previously
        classified as restatement and merged a true contradiction. It must
        park (contested), with both rows live."""
        self.seed("always rotate the api deploy key before every fleet push")
        self.seed("never rotate the api deploy key before every fleet push")
        self.conn.commit()
        self.conn.close()
        r = self.run_consolidate("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CONTESTED", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("restatement", r.stdout)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        live = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(live, 2,
                         "an always/never flip must never silently merge")

    def test_divergent_pair_parks_and_never_merges(self):
        # PRR-005: the fixture must actually CLUSTER (lexical Jaccard >= the
        # 0.60 fallback threshold) so this test exercises the real decision
        # path instead of passing vacuously on a pair that never formed a
        # cluster. D1NEW: _lexical_tokens inter=6/union=9 → J=0.667 >= 0.60;
        # relation is direct-negation ("no longer" precedes the shared word
        # "serves") → contradiction (parks).
        self.seed("eugr no longer serves production traffic on the tower cluster today")
        self.seed("grok serves production traffic on the tower cluster today")
        self.conn.commit()
        self.conn.close()
        r = self.run_consolidate("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CONTESTED", r.stdout, r.stdout + r.stderr)
        conn = sqlite3.connect(self.env["ZMEM_STORE"])
        live = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(live, 2,
                         "historical-vs-current facts must never merge")


class WriteTimeGuardTest(unittest.TestCase):
    """The write-time guard KEEPS the #61 polarity-flip contract (issue #71 G
    refines only the CONSOLIDATE cluster decision — the issue's scope line is
    'a comment / small patch on #61 or in consolidate'; the eval corpus
    depends on restated pairs coexisting as separate rows + contradicts
    links, so write time stays conservative).

    Uses the module-level frozen import store (see the ISOLATION FIRST block
    above — STORE_PATH froze to the throwaway import dir before any storelib
    import). Unique contents per test keep cases independent."""

    def setUp(self):
        self.conn = schema_mod.connect()
        schema_mod.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def _insert(self, content):
        mid = f"cafebabe-0000-4000-8000-{abs(hash(content)) % 10**12:012d}"
        self.conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
            "confidence, signal, ingestion_ts, taint) VALUES "
            "(?, 'user:global', 'lesson', ?, '[]', 'session:w', 0.9, 'none', "
            "'2026-09-01T00:00:00+00:00', 'trusted_internal')",
            (mid, content))
        self.conn.commit()
        # PRR-006: return the id of the row JUST inserted and pass it
        # explicitly to dedup_polarity_conflict — no LIMIT-1 arbitrary-row
        # pick on the shared frozen store.
        return mid

    def test_contradiction_is_conflict(self):
        mid = self._insert(C1A)
        self.assertTrue(
            dedup_polarity_conflict(self.conn, mid, C1B))

    def test_restatement_is_still_a_write_conflict(self):
        # #61 contract preserved at write time: a restated-constraint add
        # still lands as a conflict (contradicts link / own row) — the #71 G
        # relaxation lives in consolidate() only, so the eval corpus's
        # discrimination pairs keep coexisting.
        mid = self._insert(R1A)
        self.assertTrue(
            dedup_polarity_conflict(self.conn, mid, R1B),
            "write-time guard keeps the #61 polarity-flip contract")

    def test_same_polarity_is_not_conflict(self):
        mid = self._insert("deploy the fleet gateway on a tuesday morning")
        self.assertFalse(
            dedup_polarity_conflict(
                self.conn, mid,
                "deploy the fleet gateway on a tuesday morning again"))


class NliJudgeInteractionTest(unittest.TestCase):
    """Critic #11: restatement resolution skips the NLI judge; the judge
    adjudicates contradiction-classified clusters only (and its None verdict
    keeps the pre-judge park path byte-identical)."""

    def test_restatement_branch_returns_before_judge(self):
        # _nli_judge_all_entail returns None when ZMEM_NLI_CMD is unset; the
        # restatement branch must not reach it at all. Verified by source:
        # the restatement check precedes the judge call in the cluster branch.
        import inspect
        # The package __init__ re-exports the consolidate FUNCTION, shadowing
        # the submodule attribute — import the function itself.
        from storelib.consolidate import consolidate as consolidate_fn
        src = inspect.getsource(consolidate_fn)
        restatement_pos = src.find("pair_relations")
        judge_pos = src.find("_nli_judge_all_entail(member_pols)")
        self.assertGreater(restatement_pos, 0)
        self.assertGreater(judge_pos, 0)
        self.assertLess(restatement_pos, judge_pos,
                        "restatement resolution must precede the NLI judge")


if __name__ == "__main__":
    unittest.main(verbosity=2)
