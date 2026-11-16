"""Issue #486 frozen acceptance: the sync ingest fold must re-rank its dedup
fold target against the store's canonical exact basis (content_norm) before
merging -- the same re-rank the add path (PR #485) and the update gate
already run. PR #485 fixed those two lanes and left the ingest lane taking
the dedup detector's top-ranked candidate as the fold target unchanged.

Contract legs (the discriminating legs are RED at the base tree and GREEN
after the fix; the preserving leg is GREEN at both ends by design):

- AC1 IngestExactRerankTest: a synced row whose content is normalized-equal
  to live keeper B folds into B, never into a colliding outranker A that
  merely embeds above the dedup threshold (the fake-profile papa/romeo
  collision pair reports similarity=1.000 for DIFFERENT content).
- AC2 SubjectVetoWindowTest: the exact-window candidates include a
  subject-vetoed oldest row (task:b beside the incoming task:a identity)
  and a knowledge:% source-ref row; the fold falls through the vetoed row
  to the compatible runner-up exactly like the add/update re-rank
  (_subject_admit over the bounded SUBJECT_MAX_EXACT_CANDIDATES window),
  and the knowledge-pointer row is never chosen.
- AC3 ParityAddIngestKeeperTest: the same content folded through
  add_memory and through the sync ingest fold on the merged head selects
  the same keeper (the _detect_duplicate parity invariant).
- AC4 PreservingNoExactRowTest (GREEN at both ends): with NO
  normalized-equal live row the ingest fold keeps the pre-fix behavior --
  the detector's top-1 semantic merge with the polarity guard intact, and
  the aggregate deduped count unchanged.

Drives the REAL skills/memory/scripts/store.py CLI via subprocess against
hermetic throwaway stores (fake embed profile), exactly like
tests/test_update_fold_no_dedup.py.

Run: python -X utf8 tests/test_sync_ingest_exact_rerank.py
     python -X utf8 tests/test_sync_ingest_exact_rerank.py SubjectVetoWindow
     python -X utf8 -m pytest tests/test_sync_ingest_exact_rerank.py -k parity
     python -X utf8 -m pytest tests/test_sync_ingest_exact_rerank.py -k preserving
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock  # noqa: F401  (detector-stub seam, critic r2)
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

# Fake-profile collision pair (PR #485 probe; verified in
# tests/test_update_fold_no_dedup.py): distinct 5-letter words whose 16-dim
# feature-hash embeddings collide, so the detector reports similarity=1.000
# for DIFFERENT content. Exactness must be decided by normalized content
# equality, never by the similarity ranking.
KEEPER_WORD = "papa"   # normalized-equal to every incoming ingest row here
OTHER_WORD = "romeo"   # collides at sim 1.000 under the fake profile

NS = "project:ingest486"


def _env(tmp: str, **extra: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env.pop("ZMEM_DATA", None)
    env.pop("CLAUDE_PLUGIN_DATA", None)
    env.pop("ZCODE_PLUGIN_DATA", None)
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("ZMEM_DEDUP_THRESHOLD", None)
    env.pop("ZMEM_REMOTE_URL", None)
    env.pop("ZMEM_UPDATE_FOLD_THRESHOLD", None)
    env.update({k: v for k, v in extra.items() if v is not None})
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
    return env


def _summary_counts(text: str) -> dict:
    """Parse the ingest-jsonl summary line into a {name: int} dict."""
    m = re.search(r"\[zmem\] ingest-jsonl: (.*)", text)
    if not m:
        return {}
    counts: dict = {}
    for part in m.group(1).split():
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                counts[k] = int(v)
            except ValueError:
                pass
    return counts


class _IngestCase(unittest.TestCase):
    """Shared CLI harness: hermetic store, real store.py subprocesses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-486ingest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        r = self._run("init")
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))

    def _run(self, *args):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=_env(self.tmp), capture_output=True, text=True,
            timeout=180, cwd=str(REPO_ROOT))

    def _add(self, content, *extra):
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", content, "--signal", "test", *extra)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
        self.assertTrue(m, r.stdout)
        return m.group(1)

    def _sync_row(self, content, tags="", **over):
        row = {
            "id": str(uuid.uuid4()), "namespace": NS, "type": "fact",
            "content": content, "tags": tags, "signal": "test",
            "confidence": 0.9, "source_ref": "",
            "ingestion_ts": "2026-11-16T15:00:00Z",
            "valid_from": "2026-11-16T15:00:00Z", "valid_until": "",
        }
        row.update(over)
        return row

    def _ingest(self, *rows):
        path = os.path.join(self.tmp, "ingest-%d.jsonl" % len(os.listdir(self.tmp)))
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return self._run("ingest-jsonl", "--in", path)

    def _db_rows(self):
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        conn.row_factory = sqlite3.Row
        try:
            return {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, content, tags, source_ref, retrieval_count "
                "FROM memory WHERE namespace=? ORDER BY rowid", (NS,))}
        finally:
            conn.close()

    def _fold_target(self, marker_tag):
        """Ids of rows whose merged tags now carry the marker tag."""
        return [mid for mid, row in self._db_rows().items()
                if marker_tag in (row["tags"] or "")]


class IngestExactRerankTest(_IngestCase):
    """AC1: the ingest fold targets the normalized-equal keeper, never the
    colliding outranker the detector ranked first."""

    def test_ingest_fold_targets_exact_keeper_not_colliding_outranker(self):
        # The executed-reproduction fixture: the TRUE exact keeper B is
        # seeded first, then the colliding outranker A as its own live row
        # (--no-dedup), so A outranks B in the KNN ranking at sim 1.000.
        id_b = self._add(KEEPER_WORD)
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="fold:ac1")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1,
                         f"the fold must fire exactly once; summary: "
                         f"{r.stdout.strip()[-300:]}")
        self.assertEqual(counts.get("added"), 0)
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:ac1" in (rw["tags"] or "")]
        self.assertEqual(
            len(target), 1,
            f"exactly one row must receive the fold; rows: "
            f"{json.dumps(list(rows.values()))[:400]}")
        self.assertEqual(
            target[0], id_b,
            "the ingest fold must merge into the normalized-equal keeper B, "
            f"not into {(target[0][:8])}; the add path re-ranks this exact "
            "shape (PR #485) and the ingest lane must match it")
        # The collider stays untouched; the content was folded, not landed.
        self.assertNotIn("fold:ac1", rows[id_a]["tags"] or "",
                         "the colliding outranker must not receive the fold")
        self.assertEqual(rows[id_a]["content"], OTHER_WORD)
        self.assertEqual(rows[id_b]["content"], KEEPER_WORD)
        self.assertNotIn(row["id"], rows,
                         "a folded ingest row must not land as its own row")


class SubjectVetoWindow(_IngestCase):
    """AC2: vetoed/knowledge rows in the exact window fall through to the
    compatible runner-up, exactly like the add/update re-rank."""

    def test_vetoed_oldest_exact_row_falls_through_to_compatible_runner_up(self):
        # K: the OLDEST content-equal row is a knowledge pointer (source_ref
        # knowledge:%). It is excluded from every exact window (the SQL
        # predicate), so it must NEVER receive the fold; seeded first so a
        # re-rank that forgot the exclusion would visibly fold into it.
        k_row = self._sync_row(KEEPER_WORD,
                               source_ref="knowledge:486ac2:repo/run-1")
        rk = self._ingest(k_row)
        self.assertEqual(rk.returncode, 0, rk.stderr)
        k_counts = _summary_counts(rk.stdout)
        self.assertEqual(k_counts.get("added"), 1,
                         "the knowledge row must land as its own live row "
                         f"(PR1 identity lane); summary: {rk.stdout[-300:]}")
        # V: oldest ORDINARY exact row, subject-incompatible (task:b beside
        # the incoming task:a) -- the #376 veto.
        id_v = self._add(KEEPER_WORD, "--tags", "task:b", "--no-dedup")
        # C: the compatible runner-up (task:a intersects the incoming task:a).
        id_c = self._add(KEEPER_WORD, "--tags", "task:a", "--no-dedup")
        # A: the colliding outranker the detector ranks first (no identity,
        # so the semantic lane admits it at sim 1.000 without a veto).
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="task:a,fold:ac2")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1,
                         f"the fold must fire exactly once; summary: "
                         f"{r.stdout.strip()[-300:]}")
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:ac2" in (rw["tags"] or "")]
        self.assertEqual(len(target), 1,
                         f"exactly one row must receive the fold; rows: "
                         f"{json.dumps(list(rows.values()))[:500]}")
        self.assertEqual(
            target[0], id_c,
            "the exact window must fall through the subject-vetoed oldest "
            f"row to the compatible runner-up; fold landed on "
            f"{target[0][:8]} (expected runner-up {id_c[:8]})")
        # The vetoed row, the knowledge pointer, and the collider are all
        # untouched.
        self.assertNotIn("fold:ac2", rows[id_v]["tags"] or "")
        self.assertEqual(rows[id_v]["retrieval_count"], 0)
        self.assertNotIn("fold:ac2", rows[id_a]["tags"] or "")
        self.assertNotIn("fold:ac2", rows[k_row["id"]]["tags"] or "",
                         "a knowledge:% row is never a fold target")
        self.assertEqual(rows[k_row["id"]]["retrieval_count"], 0)
        # The #376 veto machinery ran for the vetoed candidate.
        combined = r.stdout + r.stderr
        self.assertIn("subject_veto", combined,
                      "the vetoed exact candidate must print the stable "
                      f"subject_veto line; output: {combined[-400:]}")
        self.assertIn(id_v, combined)


class ParityAddIngestKeeperTest(_IngestCase):
    """AC3: add_memory and the sync ingest fold pick the SAME keeper for
    the same content on the merged head."""

    def test_add_and_ingest_lanes_pick_the_same_keeper(self):
        id_b = self._add(KEEPER_WORD)
        id_a = self._add(OTHER_WORD, "--no-dedup")
        # Lane 1 -- add_memory (re-ranks since PR #485): folds into B.
        r_add = self._run("add", "--namespace", NS, "--type", "fact",
                          "--content", KEEPER_WORD, "--tags", "fold:addlane",
                          "--signal", "test")
        self.assertEqual(r_add.returncode, 0, r_add.stderr)
        self.assertIn("dedup re-ranked", r_add.stdout + r_add.stderr,
                      "the add lane must re-rank this shape (PR #485) -- "
                      "the parity premise of this leg")
        # Lane 2 -- the sync ingest fold, same content on the merged head.
        row = self._sync_row(KEEPER_WORD, tags="fold:ingestlane")
        r_ing = self._ingest(row)
        self.assertEqual(r_ing.returncode, 0, r_ing.stderr)
        counts = _summary_counts(r_ing.stdout)
        self.assertEqual(counts.get("deduped"), 1,
                         f"the ingest fold must fire; summary: "
                         f"{r_ing.stdout.strip()[-300:]}")
        add_target = self._fold_target("fold:addlane")
        ingest_target = self._fold_target("fold:ingestlane")
        self.assertEqual(add_target, [id_b],
                         "the add lane folds into the exact keeper B")
        self.assertEqual(
            ingest_target, [id_b],
            "_detect_duplicate parity: the ingest fold must keep the same "
            f"keeper the add lane chose ({id_b[:8]}); it landed on "
            f"{ingest_target[0][:8] if ingest_target else 'no row'}")
        self.assertNotIn("fold:ingestlane", self._db_rows()[id_a]["tags"] or "",
                         "the colliding outranker must not receive the fold")


class PreservingNoExactRowTest(_IngestCase):
    """AC4 (preserving, GREEN at both ends): with NO normalized-equal live
    row the ingest fold stays byte-identical to the pre-fix sync -- the
    detector's top-1 semantic merge (polarity guard intact), aggregate
    deduped count unchanged."""

    def test_no_exact_row_keeps_top1_semantic_fold(self):
        # The only live row is the collider; nothing is normalized-equal to
        # the incoming content, so the re-rank has no exact basis and the
        # fold must land on the top-1 semantic neighbor exactly as before.
        id_a = self._add(OTHER_WORD)
        row = self._sync_row(KEEPER_WORD, tags="fold:ac4")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1,
                         f"the top-1 semantic fold must keep firing; "
                         f"summary: {r.stdout.strip()[-300:]}")
        self.assertEqual(counts.get("added"), 0)
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:ac4" in (rw["tags"] or "")]
        self.assertEqual(target, [id_a],
                         "with no exact row the fold lands on the semantic "
                         "neighbor -- pre-fix behavior preserved")
        self.assertEqual(rows[id_a]["content"], OTHER_WORD,
                         "the keeper's content is byte-identical")
        self.assertNotIn(row["id"], rows,
                         "a folded ingest row must not land as its own row")


class CriticRound1SupplementaryTest(_IngestCase):
    """Plan-critic round 1 additions (seven families). These legs are
    supplementary to the frozen drivers C1-C4: byte-different normalized-equal
    discrimination, tombstoned/other-namespace decoys, the exact-top-1 no-op,
    polarity conflict on the re-ranked keeper, the all-vetoed window, the
    bounded-window overflow, the pre-v8 (no content_norm) fallback, and
    repeated-ingest keeper stability."""

    def test_byte_different_normalized_equal_still_retargets(self):
        # Family 1: the keeper's BYTES differ from the incoming row (case +
        # whitespace) while normalize_content collapses both to "papa" -- a
        # raw-content-equality implementation of the re-rank fails this leg.
        id_b = self._add("  Papa  ")
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="fold:sup1")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1, r.stdout[-300:])
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:sup1" in (rw["tags"] or "")]
        self.assertEqual(target, [id_b],
                         "the fold must land on the normalized-equal keeper "
                         "even when its raw bytes differ (canonical basis "
                         "only)")
        self.assertIn(
            "dedup re-ranked", r.stdout + r.stderr,
            "a successful retarget MUST print the re-rank notice (critic r3: "
            "a delete-notice mutant must fail this leg); output: "
            f"{(r.stdout + r.stderr)[-400:]}")
        self.assertEqual(rows[id_b]["content"], "  Papa  ",
                         "the keeper's own content is never rewritten")
        self.assertNotIn("fold:sup1", rows[id_a]["tags"] or "")

    def test_tombstoned_and_other_namespace_decoys_never_selected(self):
        # Family 2 (critic r2 redesign): the decoys are seeded BEFORE the
        # correct keeper B so a re-rank missing either SQL predicate
        # (superseded_at IS NULL / namespace=?) would fold into the OLDER
        # decoy by rowid order instead of B.
        id_dead = self._add(KEEPER_WORD, "--no-dedup")   # decoy 1 (rowid 1)
        r_ns = self._run("add", "--namespace", NS + "2", "--type", "fact",
                         "--content", KEEPER_WORD, "--signal", "test")
        self.assertEqual(r_ns.returncode, 0, r_ns.stderr)
        m_ns = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})",
                         r_ns.stdout)
        self.assertTrue(m_ns, r_ns.stdout)
        id_ns = m_ns.group(1)                             # decoy 2 (rowid 2)
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        conn.execute(
            "UPDATE memory SET superseded_at=? WHERE id=?",
            ("2026-11-16T00:00:00Z", id_dead))
        conn.commit()
        conn.close()
        # B lands only after decoy 1 is tombstoned (else add would fold it).
        id_b = self._add(KEEPER_WORD)                     # keeper (rowid 3)
        id_a = self._add(OTHER_WORD, "--no-dedup")        # collider (rowid 4)
        row = self._sync_row(KEEPER_WORD, tags="fold:sup2")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1, r.stdout[-300:])
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:sup2" in (rw["tags"] or "")]
        self.assertEqual(target, [id_b],
                         "tombstoned and cross-namespace decoys must never "
                         "be fold targets")
        self.assertNotIn("fold:sup2", rows.get(id_dead, {}).get("tags") or "",
                         "the tombstoned decoy is older than B; only the "
                         "superseded_at IS NULL predicate excludes it")
        conn = sqlite3.connect(os.path.join(self.tmp, "store.sqlite"))
        conn.row_factory = sqlite3.Row
        ns_row = conn.execute(
            "SELECT tags FROM memory WHERE id=?", (id_ns,)).fetchone()
        conn.close()
        self.assertNotIn("fold:sup2", ns_row["tags"] or "",
                         "the cross-namespace decoy is older than B; only "
                         "the namespace=? predicate excludes it")

    def test_exact_top1_no_op_folds_without_notice(self):
        # Family 3: the detector's top-1 IS already normalized-equal (no
        # collider outranks it) -- the fold proceeds with NO re-rank notice
        # and NO retarget.
        id_b = self._add(KEEPER_WORD)
        row = self._sync_row(KEEPER_WORD, tags="fold:sup3")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1, r.stdout[-300:])
        combined = r.stdout + r.stderr
        self.assertNotIn("dedup re-ranked", combined,
                         "an already-exact top-1 must not print the re-rank "
                         f"notice; output: {combined[-300:]}")
        target = [mid for mid, rw in self._db_rows().items()
                  if "fold:sup3" in (rw["tags"] or "")]
        self.assertEqual(target, [id_b])

    def test_polarity_conflict_on_retargeted_keeper_inserts_instead(self):
        # Family 4: the re-ranked exact keeper POLARITY-CONFLICTS with the
        # incoming row (the write.py Stage-B counterexample pair: a quoted
        # span with an embedded newline versus the same span whitespace-
        # collapsed are normalized-equal yet conflicting). The rows stay
        # separate (insert), the notice never prints, and both keepers stay
        # untouched by the fold.
        keeper_text = 'rule: "not\nx"'
        id_b = self._add(keeper_text)
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row('rule: "not x"', tags="fold:sup4")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 0,
                         "a polarity conflict on the FINAL keeper must "
                         f"decline the fold; summary: {r.stdout[-300:]}")
        self.assertEqual(counts.get("added"), 1)
        combined = r.stdout + r.stderr
        self.assertNotIn("dedup re-ranked", combined,
                         "the re-rank notice must not print when the guard "
                         "subsequently declines the fold")
        self.assertIn("polarity", combined)
        rows = self._db_rows()
        self.assertNotIn("fold:sup4", rows[id_b]["tags"] or "")
        self.assertNotIn("fold:sup4", rows[id_a]["tags"] or "")
        self.assertIn(row["id"], rows,
                      "the conflicting row lands as its own live row")

    def test_all_vetoed_window_keeps_detector_top1(self):
        # Family 5a: every exact-window candidate is subject-vetoed (task:b
        # beside the incoming task:a) with NO compatible runner-up -- the
        # fall-through-exhausted contract keeps the detector's top-1.
        id_v1 = self._add(KEEPER_WORD, "--tags", "task:b", "--no-dedup")
        id_v2 = self._add(KEEPER_WORD, "--tags", "task:b", "--no-dedup")
        self.assertEqual(id_v1, id_v1)
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="task:a,fold:sup5a")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1, r.stdout[-300:])
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:sup5a" in (rw["tags"] or "")]
        self.assertEqual(
            target, [id_a],
            "an all-vetoed exact window falls back to the detector's top-1 "
            "(the same exhausted-fall-through contract as the add/update "
            "re-ranks); vetoed rows are never targets")

    def test_bounded_window_capped_at_32_rows(self):
        # Family 5b (critic r2 redesign): 33 subject-INCOMPATIBLE exact rows
        # fill the bounded window (SUBJECT_MAX_EXACT_CANDIDATES=32) and the
        # first COMPATIBLE exact row sits BEYOND it. A bounded re-rank sees
        # only vetoed candidates (no admitted keeper) and keeps the
        # detector's top-1; an UNBOUNDED query would find the compatible
        # out-of-window row and fold there instead -- the two shapes
        # disagree, so the bound is load-bearing here.
        for _ in range(33):
            self._add(KEEPER_WORD, "--tags", "task:b", "--no-dedup")
        id_far = self._add(KEEPER_WORD, "--tags", "task:a", "--no-dedup")
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="task:a,fold:sup5b")
        r = self._ingest(row)
        self.assertEqual(r.returncode, 0, r.stderr)
        counts = _summary_counts(r.stdout)
        self.assertEqual(counts.get("deduped"), 1, r.stdout[-300:])
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:sup5b" in (rw["tags"] or "")]
        self.assertEqual(
            target, [id_a],
            "with the 32-slot window full of vetoed candidates the bounded "
            "re-rank admits nothing and keeps the detector top-1; an "
            "unbounded scan would have folded into the compatible "
            f"out-of-window row {id_far[:8]}")

    # Family 6 (pre-v8 column-absent branch) lives in DirectIngestRowSeamTest:
    # the CLI's _prepare_store re-adds content_norm via init_db, so only a
    # direct _ingest_row call on a column-absent connection can observe it.

    def test_repeated_ingest_of_same_row_id_keeps_stable_keeper(self):
        # Family 7 (critic r2 redesign): the SAME row id is replayed twice;
        # both passes must fold (deduped=1, added=0) into the SAME keeper,
        # and the merge counter accumulates to exactly 2.
        id_b = self._add(KEEPER_WORD)
        id_a = self._add(OTHER_WORD, "--no-dedup")
        row = self._sync_row(KEEPER_WORD, tags="fold:sup7")
        path = os.path.join(self.tmp, "replay-sup7.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        r1 = self._run("ingest-jsonl", "--in", path)
        r2 = self._run("ingest-jsonl", "--in", path)
        for r in (r1, r2):
            self.assertEqual(r.returncode, 0, r.stderr)
            counts = _summary_counts(r.stdout)
            self.assertEqual(
                counts.get("deduped"), 1,
                "every replay of the same row must fold again (a "
                f"second-pass skip is NOT this contract); summary: "
                f"{r.stdout.strip()[-300:]}")
            self.assertEqual(counts.get("added"), 0)
        rows = self._db_rows()
        target = [mid for mid, rw in rows.items()
                  if "fold:sup7" in (rw["tags"] or "")]
        self.assertEqual(target, [id_b],
                         "both folds land on the same exact keeper")
        self.assertEqual(len(rows), 2,
                         "no new live row appears on repeated ingest")
        self.assertEqual(rows[id_b]["retrieval_count"], 2,
                         "two folds bump the merge counter exactly twice "
                         "(documented non-idempotent counter)")


class DirectIngestRowSeamTest(unittest.TestCase):
    """Direct _ingest_row legs over the detector-stub seam (critic r2):
    the retarget-then-polarity-decline ordering, and the pre-v8
    column-absent fallback. Both drive storelib in-process, so each test
    pins its scratch env and purges every cached storelib/embeddings module
    BEFORE importing sync (the store-path freeze is per-process-per-import;
    the subprocess-driven classes above never import storelib)."""

    def _fresh_sync(self, tmp):
        # Critic r3 isolation: snapshot env, the module-cache entries this
        # helper purges, and sys.path; restore ALL of them in a guaranteed
        # cleanup so no state bound to this scratch store leaks into
        # later tests sharing the process (pytest worker).
        _PURGE_PREFIXES = ("storelib", "embeddings", "embed_profiles",
                           "schema_meta")
        env_before = dict(os.environ)
        mods_before = {}
        for mod in list(sys.modules):
            if any(mod == p or mod.startswith(p + ".")
                   for p in _PURGE_PREFIXES):
                mods_before[mod] = sys.modules[mod]
        path_before = list(sys.path)

        def _restore():
            os.environ.clear()
            os.environ.update(env_before)
            for mod in list(sys.modules):
                if any(mod == p or mod.startswith(p + ".")
                       for p in _PURGE_PREFIXES):
                    del sys.modules[mod]
            sys.modules.update(mods_before)
            sys.path[:] = path_before
        self.addCleanup(_restore)

        os.environ["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        for mod in list(sys.modules):
            if any(mod == p or mod.startswith(p + ".")
                   for p in _PURGE_PREFIXES):
                del sys.modules[mod]
        scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import storelib.sync as sync_mod  # noqa: E404 (post-purge import)
        return sync_mod

    def test_polarity_conflict_after_retarget_declines_the_fold(self):
        # The detector stub returns the NON-EXACT colliding top-1 A; the
        # real re-rank must retarget to the normalized-equal keeper B, and
        # the real polarity guard must then decline (insert, no notice, A
        # and B untouched). RED at base (base folds straight into A).
        tmp = tempfile.mkdtemp(prefix="zmem-486seam-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = _env(tmp)
        store = os.path.join(tmp, "store.sqlite")
        keeper_text = 'rule: "not\nx"'
        incoming_text = 'rule: "not x"'
        id_b = id_a = None
        for content, extra in ((keeper_text, ()), (OTHER_WORD, ("--no-dedup",))):
            r = subprocess.run(
                [PYTHON, "-X", "utf8", str(STORE_PY), "add", "--namespace",
                 NS, "--type", "fact", "--content", content,
                 "--signal", "test", *extra],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT))
            assert r.returncode == 0, (r.stdout, r.stderr)
            m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
            assert m, r.stdout
            if id_b is None:
                id_b = m.group(1)
            else:
                id_a = m.group(1)
        sync_mod = self._fresh_sync(tmp)
        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        obj = {
            "id": str(uuid.uuid4()), "namespace": NS, "type": "fact",
            "content": incoming_text, "tags": "", "signal": "test",
            "confidence": 0.9, "source_ref": "",
            "ingestion_ts": "2026-11-16T15:00:00Z",
            "valid_from": "2026-11-16T15:00:00Z", "valid_until": "",
        }
        row = sync_mod._validate_sync_row(obj)

        def _stub(conn_, content, ns, dedup_cache=None):
            return {"id": id_a}, 1.0, None

        import io as _io
        import contextlib as _ctx
        buf_out, buf_err = _io.StringIO(), _io.StringIO()
        try:
            with unittest.mock.patch.object(sync_mod, "_detect_duplicate",
                                            _stub), \
                    _ctx.redirect_stdout(buf_out), \
                    _ctx.redirect_stderr(buf_err):
                outcome = sync_mod._ingest_row(
                    conn, dict(row), allow_tombstones=False)
            conn.commit()
            self.assertEqual(
                outcome, "added",
                "after retargeting to the polarity-conflicting exact keeper "
                "the fold must be declined and the row inserted")
            combined = buf_out.getvalue() + buf_err.getvalue()
            self.assertIn("polarity", combined)
            self.assertNotIn("dedup re-ranked", combined,
                             "the re-rank notice must not print when the "
                             "guard subsequently declines the fold")
            for mid in (id_a, id_b):
                r_ = conn.execute(
                    "SELECT retrieval_count, tags FROM memory WHERE id=?",
                    (mid,)).fetchone()
                self.assertEqual(r_["retrieval_count"], 0,
                                 f"keeper {mid[:8]} must stay untouched")
        finally:
            conn.close()

    def test_newest_byte_different_top1_folds_without_window_rerun(self):
        # Impl-review r1 (M3b discriminator): the detector's cache lane
        # serves NEWEST-first, so with TWO subject-compatible normalized-
        # equal live rows the top-1 is the NEWEST one. When its raw bytes
        # differ from the incoming content (case/whitespace variant), the
        # canonical exactness check (normalize_content basis) treats the
        # top-1 as EXACT: fold into the newest, NO re-rank notice. A
        # raw-bytes implementation would deem it not-exact, run the
        # oldest-first window, and retarget to the OLDER sibling -- a
        # keeper divergence from the add lane (write.py's add_fold_exact
        # folds into the newest here). Driven through the detector stub
        # for determinism (the exact-cache lane's newest-first order is
        # the load-bearing input).
        tmp = tempfile.mkdtemp(prefix="zmem-486seam-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = _env(tmp)
        store = os.path.join(tmp, "store.sqlite")
        id_old = id_new = None
        for content in ("papa", "PAPA"):
            r = subprocess.run(
                [PYTHON, "-X", "utf8", str(STORE_PY), "add", "--namespace",
                 NS, "--type", "fact", "--content", content,
                 "--signal", "test", "--no-dedup"],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT))
            assert r.returncode == 0, (r.stdout, r.stderr)
            m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
            assert m, r.stdout
            if id_old is None:
                id_old = m.group(1)
            else:
                id_new = m.group(1)
        sync_mod = self._fresh_sync(tmp)
        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        obj = {
            "id": str(uuid.uuid4()), "namespace": NS, "type": "fact",
            "content": "papa", "tags": "fold:sup8", "signal": "test",
            "confidence": 0.9, "source_ref": "",
            "ingestion_ts": "2026-11-16T15:00:00Z",
            "valid_from": "2026-11-16T15:00:00Z", "valid_until": "",
        }
        row = sync_mod._validate_sync_row(obj)

        def _stub(conn_, content, ns, dedup_cache=None):
            return {"id": id_new}, 1.0, None  # cache lane: NEWEST first

        import io as _io
        import contextlib as _ctx
        buf_out, buf_err = _io.StringIO(), _io.StringIO()
        try:
            with unittest.mock.patch.object(sync_mod, "_detect_duplicate",
                                            _stub), \
                    _ctx.redirect_stdout(buf_out), \
                    _ctx.redirect_stderr(buf_err):
                outcome = sync_mod._ingest_row(
                    conn, dict(row), allow_tombstones=False)
            conn.commit()
            self.assertEqual(outcome, "deduped")
            target = conn.execute(
                "SELECT id FROM memory WHERE tags LIKE ?",
                ("%fold:sup8%",)).fetchall()
            self.assertEqual(len(target), 1)
            self.assertEqual(
                target[0]["id"], id_new,
                "a normalized-equal top-1 IS the exact keeper: fold into it "
                "(the add lane's add_fold_exact behavior); a raw-bytes "
                "check would retarget to the OLDER sibling")
            self.assertEqual(
                conn.execute("SELECT retrieval_count FROM memory WHERE id=?",
                             (id_old,)).fetchone()[0], 0,
                "the older sibling must stay untouched")
            combined = buf_out.getvalue() + buf_err.getvalue()
            self.assertNotIn("dedup re-ranked", combined,
                             "no re-rank notice when the top-1 is already "
                             "normalized-equal")
        finally:
            conn.close()

    def test_pre_v8_column_absent_folds_into_detector_top1(self):
        # The re-rank's exact-window query must take the OperationalError ->
        # empty-window branch on a store whose memory table lacks
        # content_norm (pre-v8 shape). The CLI's _prepare_store re-adds the
        # column (init_db), so this drives _ingest_row DIRECTLY on a
        # connection where the column is verifiably absent, with the
        # detector stubbed to a non-exact top-1 A (the re-rank only runs
        # when the top-1 is not exact). An implementation missing the
        # try/except CRASHES here; a correct one folds into A unchanged.
        # GREEN at base and at the fixed head (regression pin).
        tmp = tempfile.mkdtemp(prefix="zmem-486seam-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = _env(tmp)
        store = os.path.join(tmp, "store.sqlite")
        id_b = id_a = None
        for content, extra in ((KEEPER_WORD, ()), (OTHER_WORD, ("--no-dedup",))):
            r = subprocess.run(
                [PYTHON, "-X", "utf8", str(STORE_PY), "add", "--namespace",
                 NS, "--type", "fact", "--content", content,
                 "--signal", "test", *extra],
                env=env, capture_output=True, text=True, timeout=180,
                cwd=str(REPO_ROOT))
            assert r.returncode == 0, (r.stdout, r.stderr)
            m = re.search(r"\[zmem\] added memory ([0-9a-f-]{36})", r.stdout)
            assert m, r.stdout
            if id_b is None:
                id_b = m.group(1)
            else:
                id_a = m.group(1)
        sync_mod = self._fresh_sync(tmp)
        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        conn.execute("DROP INDEX IF EXISTS idx_memory_content_norm")
        conn.execute("ALTER TABLE memory DROP COLUMN content_norm")
        conn.commit()
        cols = [r_["name"] for r_ in conn.execute("PRAGMA table_info(memory)")]
        self.assertNotIn("content_norm", cols,
                         "precondition: the column is verifiably absent")
        obj = {
            "id": str(uuid.uuid4()), "namespace": NS, "type": "fact",
            "content": KEEPER_WORD, "tags": "fold:sup6", "signal": "test",
            "confidence": 0.9, "source_ref": "",
            "ingestion_ts": "2026-11-16T15:00:00Z",
            "valid_from": "2026-11-16T15:00:00Z", "valid_until": "",
        }
        row = sync_mod._validate_sync_row(obj)

        def _stub(conn_, content, ns, dedup_cache=None):
            return {"id": id_a}, 1.0, None

        try:
            with unittest.mock.patch.object(sync_mod, "_detect_duplicate",
                                            _stub):
                outcome = sync_mod._ingest_row(
                    conn, dict(row), allow_tombstones=False)
            conn.commit()
            self.assertEqual(
                outcome, "deduped",
                "with content_norm absent the exact basis is unavailable "
                "(OperationalError -> empty window) and the detector's "
                "top-1 fold stands")
            target = conn.execute(
                "SELECT id FROM memory WHERE tags LIKE ?",
                ("%fold:sup6%",)).fetchall()
            self.assertEqual(len(target), 1)
            self.assertEqual(
                target[0]["id"], id_a,
                "the sole fold target must be the detector's (stubbed) "
                "top-1 A, not the exact keeper B -- the column-absent "
                "branch has no exact basis to retarget with")
            self.assertEqual(
                conn.execute("SELECT retrieval_count FROM memory WHERE id=?",
                             (id_b,)).fetchone()[0], 0,
                "B must stay untouched: with the column absent the re-rank "
                "cannot see it")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
