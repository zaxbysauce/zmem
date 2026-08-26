"""Tests for store.py's sleep-time `organize` subcommand (issue #62).

The SessionStart cadence job (7.7) and its pipeline — shared cadence gate +
optional idle gate (7.2), bounded episode (7.1), entity/link backfill (7.2),
episode consolidation (7.1), topic clustering (7.2-7.3), hierarchical
summaries (7.3), keeper compression (7.4), unrecalled-prune pass-through
(7.6) — must be correct AND safe for existing schema-v10 clients: organize
writes only into existing columns, never bumping the schema.

The harness mirrors the sibling test files: subprocess runs drive the REAL
store.py CLI (integration), and an in-process module load covers the fold
guards that only mock injection can reach.

Run: python tests/test_organize.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:organize-62"

# NOTE (PRR-012 / Claude Code round 4): the module previously MUTATED
# os.environ["ZMEM_MODELS_DIR"] at import scope, leaking into every sibling
# test in a shared-process run. Removed: every subprocess test pins
# ZMEM_MODELS_DIR explicitly in setUp below, and the in-process loaders patch
# it inside _load_store_module, so the module-level mutation was redundant
# process-global state.


# --- in-process store loader (fold-guard unit tests) ---
def _load_store_module(store_path: Path, models_dir: Path):
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_organize_{uuid.uuid4().hex}", STORE_PY
    )
    env = {
        "ZMEM_STORE": str(store_path),
        "ZMEM_MODELS_DIR": str(models_dir),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _make_store(prefix="zmem-organize62-"):
    tmp = tempfile.mkdtemp(prefix=prefix)
    models = Path(tmp) / "no-models"
    models.mkdir()
    mod = _load_store_module(Path(tmp) / "store.sqlite", models)
    conn = mod.connect()
    mod.init_db(conn)
    mod.migrate(conn)
    return mod, conn, tmp


class OrganizeIntegrationTest(unittest.TestCase):
    """Real-CLI tests. Each test owns a fresh temp store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-organize-62-")
        self.store = str(Path(self.tmp) / "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-such-models"),
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        # Hermetic organize knobs (Claude Code round 4 env-isolation gap): a
        # host-leaked value would silently change episode/idle/compression
        # expectations for ~11 dependent tests. Individual tests re-set the
        # one knob they mean to exercise via env_extra.
        for k in ("ZMEM_ORGANIZE_IDLE_HOURS", "ZMEM_ORGANIZE_EPISODE_BOUND",
                  "ZMEM_KEEPER_COMPRESS_CHARS", "ZMEM_UNRECALLED_DAYS",
                  "ZMEM_NLI_CMD"):
            self.env.pop(k, None)
        r = self._run("init")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env_extra=None):
        env = {**self.env, **(env_extra or {})}
        return subprocess.run([PYTHON, str(STORE_PY), *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def _conn(self):
        return sqlite3.connect(self.store)

    def _add(self, content, tags="", days=0, **kw):
        args = ["add", "--namespace", NS, "--type", "fact",
                "--content", content]
        if tags:
            args += ["--tags", tags]
        args += ["--signal", kw.get("signal", "none"),
                 "--confidence", str(kw.get("confidence", "0.5"))]
        r = self._run(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        if days:
            c = self._conn()
            try:
                c.execute(
                    "UPDATE memory SET ingestion_ts=datetime('now', ?) "
                    "WHERE content LIKE ?",
                    (f"-{days} days", f"%{content}%"))
                c.commit()
            finally:
                c.close()

    def _seed_raw(self, content, tags="", namespace=NS, order=0):
        """Insert a row DIRECTLY (no entities, no links — a backfill
        candidate), bypassing write-time enrichment. ``order`` sets ingestion
        ordering for deterministic episodes."""
        mid = str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1700000000 + order))
        c = self._conn()
        try:
            c.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, ingestion_ts,
                    retrieval_count, last_retrieved)
                   VALUES (?,?,?,?,?,?,'',0.5,'none',?,NULL,?,0,NULL)""",
                (mid, namespace, "fact", content, tags, "", ts, ts))
            c.commit()
        finally:
            c.close()
        return mid

    # --- 7.1 working set / episode bound ---
    def test_episode_bound_limits_working_set(self):
        for content in ("first distinct row", "second distinct row",
                        "third distinct row"):
            self._add(content)
        r = self._run("organize", "--force", "--dry-run", "--json",
                      env_extra={"ZMEM_ORGANIZE_EPISODE_BOUND": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["bound"], 1)
        self.assertEqual(len(report["episode_ids"]), 1)
        self.assertLessEqual(report["entity_backfill"]["candidates"], 1)
        self.assertLessEqual(report["link_backfill"]["candidates"], 1)

    def test_episode_bound_prevents_summaries_outside_episode(self):
        """A ≤2-member episode cannot form a ≥3-member topic: bound is not an
        advisory counter, it gates the summaries (feature completeness)."""
        for content in ("Atlas boot pins the kernel before any fleet upgrade.",
                        "Atlas maintenance timeouts page the on-call engineer.",
                        "Atlas backup window opens on the first weekend monthly."):
            self._add(content, tags="entity:Atlas")
        r = self._run("organize", "--force", "--dry-run", "--json",
                      env_extra={"ZMEM_ORGANIZE_EPISODE_BOUND": "2"})
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["summaries"]["would_create"], 0,
                         f"2-member episode must not create a summary; {report}")

    # --- cadence + idle gates ---
    def test_cadence_gate_skips_without_force(self):
        self._add("cadence gate probe row one")
        first = self._run("organize", "--force")
        self.assertEqual(first.returncode, 0, first.stderr)
        # Immediately re-run WITHOUT --force: the shared cadence gate refuses.
        second = self._run("organize")
        self.assertEqual(second.returncode, 0, second.stderr)
        combined = second.stdout + second.stderr
        self.assertIn("skipped by cadence gate", combined, combined)
        # --force bypasses the gate and runs the episode.
        third = self._run("organize", "--force")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertNotIn("skipped by cadence gate", third.stdout + third.stderr)

    def test_idle_gate_skips_recent_activity(self):
        self._add("idle gate probe row two")
        r = self._run("organize", "--force",
                      env_extra={"ZMEM_ORGANIZE_IDLE_HOURS": "100"})
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertIn("skipped by idle gate", combined, combined)

    def test_idle_gate_off_by_default(self):
        """Idle gate is OFF (default 0): organize must run, not skip, on a
        freshly-active store when ZMEM_ORGANIZE_IDLE_HOURS is unset."""
        self._add("idle off probe row three")
        r = self._run("organize", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("skipped by idle gate", r.stdout + r.stderr)

    def test_idle_gate_passes_when_activity_old_enough(self):
        """Idle-gate PASS case: with ZMEM_ORGANIZE_IDLE_HOURS set and the last
        live-memory activity OLDER than the threshold, organize RUNS (the gate
        is a wait-not-block). Back-dates EVERY activity column with a VALID ISO
        timestamp — schema._parse_iso_to_epoch only accepts %Y-%m-%dT%H:%M:%SZ,
        so SQLite's space-separated 'YYYY-MM-DD HH:MM:SS' would parse to 0.0
        and the gate would instead see the ~now value the add path leaves in
        last_retrieved."""
        self._add("idle pass probe row")
        ten_h_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(time.time() - 10 * 3600))
        c = self._conn()
        try:
            c.execute(
                "UPDATE memory SET ingestion_ts=?, last_retrieved=?, "
                "last_surfaced=? WHERE content LIKE '%idle pass probe%'",
                (ten_h_ago, ten_h_ago, ten_h_ago))
            c.commit()
        finally:
            c.close()
        r = self._run("organize", "--force",
                      env_extra={"ZMEM_ORGANIZE_IDLE_HOURS": "5"})
        self.assertEqual(r.returncode, 0, r.stderr)
        combined = r.stdout + r.stderr
        self.assertNotIn("skipped by idle gate", combined,
                         f"10h-old activity with a 5h idle gate must RUN; {combined}")
        self.assertIn("entity backfill", combined,
                      f"organize must have run the pipeline; {combined}")

    def test_topic_partition_covers_every_episode_row_exactly_once(self):
        """A store of unrelated singletons (distinct non-project namespaces, no
        shared entities) yields one size-1 topic per working row, and the
        members union is an EXACT partition of the episode — every live
        non-summary working row lands in exactly one topic. Cross-namespace
        rows cannot be pulled together: the shared `project:` namespace entity is
        what legitimately groups same-project rows (see entity.py namespace
        extraction), so singletons require distinct, non-project namespaces."""
        self._seed_raw("peach study group focus", namespace="scratch:organize-p")
        self._seed_raw("walnut broker schedule floor", namespace="scratch:organize-w")
        self._seed_raw("quince audit calendar review", namespace="scratch:organize-q")
        r = self._run("organize", "--force", "--dry-run", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        topics = report["topics"]
        self.assertEqual(len(topics), 3, report)
        self.assertTrue(all(len(t["members"]) == 1 for t in topics), report)
        members = sorted(m for t in topics for m in t["members"])
        self.assertEqual(members, sorted(set(members)), "no row may appear twice")
        self.assertEqual(members, sorted(report["episode_ids"]),
                         "members must partition the episode exactly")

    def test_post_absorb_topics_exclude_absorbed_members(self):
        """When consolidation absorbs near-duplicates, the POST-ABSORB topic set
        contains the KEEPER (grown), never the absorbed rows — topics are over
        live rows only."""
        self._seed_raw("mango rollout pins the image before any deploy of the mango service.",
                       order=1)
        self._seed_raw("mango rollout pins the image before any deploy of the mango service now.",
                       order=2)
        self._seed_raw("papaya quota audit runs weekly on the ops calendar.", order=3)
        r = self._run("organize", "--force", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        c = self._conn()
        try:
            live_non_summary = sorted(row[0] for row in c.execute(
                "SELECT id FROM memory WHERE superseded_at IS NULL AND "
                "tags != 'summary,topic'"))
        finally:
            c.close()
        self.assertEqual(len(live_non_summary), 2,
                         "expected keeper + distinct row live after absorb")
        members = sorted(m for t in report["topics"] for m in t["members"])
        self.assertEqual(members, live_non_summary,
                         "absorbed rows must not appear as topic members")
        self.assertEqual(len(set(members)), len(members), "no duplicated member")

    def test_cadence_meta_key_shared_with_consolidate(self):
        """organize and consolidate share the SAME cadence meta keys (two entry
        points, one clock): the timestamp an organize run records must ALSO gate
        a subsequent bare ``consolidate`` invocation."""
        self._add("cadence share meta probe row")
        r = self._run("organize", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        rc = self._run("consolidate")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        combined = rc.stdout + rc.stderr
        self.assertIn("skipped by cadence gate", combined,
                      f"consolidate must honor the meta key organize wrote; {combined}")

    def test_summary_is_recallable(self):
        """Success-criterion check: the summary is a REAL row, so a normal
        recall query surfaces it (confidence 0.5 ≥ floor; type=fact; FTS-
        indexed content). Pins the "summaries are recallable" claim with an
        actual recall execution, not just the confidence field."""
        for content in ("Atlas boot pins the kernel before any fleet upgrade.",
                        "Atlas maintenance timeouts page the on-call engineer.",
                        "Atlas backup window opens on the first weekend monthly."):
            self._add(content, tags="entity:Atlas")
        r = self._run("organize", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self._conn()
        try:
            summ_id = c.execute(
                "SELECT id FROM memory WHERE tags='summary,topic' AND "
                "superseded_at IS NULL").fetchone()[0]
        finally:
            c.close()
        rc = self._run("recall", "--query", "atlas engineer",
                       "--namespace", NS, "--limit", "10", "--json",
                       "--no-hybrid")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertIn(summ_id[:8], rc.stdout,
                      f"summary row must be recallable; recall out={rc.stdout[:400]!r}")

    # --- 7.3 hierarchical summaries ---
    def test_summary_created_for_entity_topic_members_stay_live(self):
        # Three rows about the SAME entity with mutually-dissimilar content: they
        # must form a topic via shared-entity grouping (A-MEM lite) WITHOUT being
        # near enough that consolidate absorbs any of them — otherwise the
        # post-absorb topic would lose members and never reach 3. The Jaccard
        # similarity of every content pair is far below the 0.60 topic/merge
        # threshold, so the only thing tying them is the shared entity.
        for content in ("Atlas boot pins the kernel before any fleet upgrade.",
                        "Atlas maintenance timeouts page the on-call engineer.",
                        "Atlas backup window opens on the first weekend monthly."):
            self._add(content, tags="entity:Atlas")
        c = self._conn()
        try:
            ids = [row[0] for row in c.execute(
                "SELECT id FROM memory WHERE namespace=? ORDER BY ingestion_ts",
                (NS,))]
        finally:
            c.close()
        self.assertEqual(len(ids), 3)

        r = self._run("organize", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self._conn()
        try:
            summ = c.execute(
                "SELECT id, tags, source_ref, confidence, signal, merged_from, "
                "superseded_at FROM memory WHERE tags='summary,topic' AND "
                "superseded_at IS NULL").fetchone()
            self.assertIsNotNone(summ)
            self.assertEqual(summ[1], "summary,topic")
            self.assertAlmostEqual(summ[3], 0.5, places=6)
            self.assertEqual(summ[4], "none")
            # source_ref == organize:<members> == merged_from
            self.assertTrue(summ[2].startswith("organize:"), summ[2])
            self.assertEqual(summ[5], summ[2][len("organize:"):])
            # members = the 3 seeded rows (ORDER-independent set equality)
            self.assertEqual(sorted(summ[5].split(",")), sorted(ids))
            # every member stays live
            live = set(row[0] for row in c.execute(
                "SELECT id FROM memory WHERE superseded_at IS NULL"))
            self.assertTrue(set(ids).issubset(live))
        finally:
            c.close()

    def test_rerun_updates_instead_of_duplicating(self):
        for content in ("Orion deploy health checks inspect the rollout dashboard hourly.",
                        "Orion credentials rotate quarterly and never live in plaintext.",
                        "Orion incident docs require the severity and the owner field."):
            self._add(content, tags="entity:Orion")
        first = self._run("organize", "--force", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        rep1 = json.loads(first.stdout)
        self.assertEqual(rep1["summaries"]["created"], 1, rep1)
        second = self._run("organize", "--force", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        rep2 = json.loads(second.stdout)
        self.assertEqual(rep2["summaries"]["created"], 0, rep2)
        self.assertEqual(rep2["summaries"]["updated"], 1, rep2)
        c = self._conn()
        try:
            n = c.execute(
                "SELECT count(*) FROM memory WHERE tags='summary,topic' "
                "AND superseded_at IS NULL").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 1, "idempotent re-run must leave exactly one live summary")

    # --- 7.4 compression ---
    def test_compression_after_consolidation(self):
        long_a = ("Gatekeeper staging holds the build green before every prod "
                  "rollout inside the nightly maintenance window on the "
                  "shared cluster.")
        long_b = ("Gatekeeper staging always holds the build green before every "
                  "prod rollout inside the nightly maintenance window on the "
                  "shared cluster.")
        self._seed_raw(long_a, order=1)
        self._seed_raw(long_b, order=2)
        r = self._run("organize", "--force", "--json",
                      env_extra={"ZMEM_KEEPER_COMPRESS_CHARS": "60"})
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertGreaterEqual(report["compressed"]["count"], 1,
                                f"expected keeper compression; {report}")
        c = self._conn()
        try:
            live = c.execute(
                "SELECT id, content, length(content) FROM memory "
                "WHERE superseded_at IS NULL").fetchall()
            self.assertGreaterEqual(len(live), 1)
            for _id, content, ln in live:
                self.assertLessEqual(
                    ln, 60,
                    f"keeper after compression must be <= ZMEM_KEEPER_COMPRESS_CHARS; "
                    f"got {ln} chars: {content[:80]!r}")
            # Provenance survived: the survivor carries the absorbed member id.
            merged = c.execute(
                "SELECT merged_from FROM memory WHERE superseded_at IS NULL "
                "AND merged_from IS NOT NULL AND merged_from != ''").fetchall()
            self.assertTrue(any(m[0] for m in merged),
                            "compression must carry consolidated provenance")
        finally:
            c.close()

    def test_compression_dry_run_counts_not_writes(self):
        long_a = ("Gatekeeper staging holds the build green before every prod "
                  "rollout inside the nightly maintenance window on the shared "
                  "cluster, and it must never be skipped.")
        long_b = ("Gatekeeper staging holds the build green before every prod "
                  "rollout inside the nightly maintenance window on the shared "
                  "cluster, and it must never be skipped either.")
        self._seed_raw(long_a, order=1)
        self._seed_raw(long_b, order=2)
        before = Path(self.store).read_bytes()
        r = self._run("organize", "--force", "--dry-run", "--json",
                      env_extra={"ZMEM_KEEPER_COMPRESS_CHARS": "60"})
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertGreaterEqual(report["compressed"]["would_count"], 1, report)
        self.assertEqual(report["compressed"]["count"], 0)
        after = Path(self.store).read_bytes()
        self.assertEqual(before, after, "dry run must write nothing")

    def test_compression_then_rerun_updates_not_duplicates(self):
        """F-002 regression (Claude Code round 4): compression is append-only —
        update_memory REPLACES the keeper's id — so compression must run BEFORE
        topic identity is keyed. Run 1 grows a keeper past a tiny compress cap
        (compression fires, keeper id changes) and creates the topic summary
        keyed on POST-compression ids; run 2, with no new growth, must find
        that exact summary (update) instead of creating a duplicate and
        orphaning the old one — the pre-fix behavior."""
        long_a = ("Atlas staging holds the build green before every prod "
                  "rollout inside the nightly maintenance window on the "
                  "shared cluster.")
        long_b = ("Atlas staging always holds the build green before every "
                  "prod rollout inside the nightly maintenance window on the "
                  "shared cluster.")
        self._seed_raw(long_a, tags="entity:Atlas", order=1)
        self._seed_raw(long_b, tags="entity:Atlas", order=2)
        self._seed_raw("Atlas maintenance timeouts page the on-call engineer.",
                       tags="entity:Atlas", order=3)
        self._seed_raw("Atlas backup window opens on the first weekend monthly.",
                       tags="entity:Atlas", order=4)
        env = {"ZMEM_KEEPER_COMPRESS_CHARS": "80"}

        first = self._run("organize", "--force", "--json", env_extra=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        rep1 = json.loads(first.stdout)
        # The near-duplicate pair merged and the grown keeper was compressed.
        self.assertGreaterEqual(rep1["consolidate"]["merged"], 1, rep1)
        self.assertGreaterEqual(rep1["compressed"]["count"], 1, rep1)
        # 3 live members remain (compressed keeper + 2 distinct) -> 1 summary,
        # keyed on the POST-compression member set.
        self.assertEqual(rep1["summaries"]["created"], 1, rep1)
        c = self._conn()
        try:
            summ = c.execute(
                "SELECT merged_from FROM memory WHERE tags='summary,topic' "
                "AND superseded_at IS NULL").fetchone()
            self.assertIsNotNone(summ)
            live_keep = c.execute(
                "SELECT id FROM memory WHERE superseded_at IS NULL "
                "AND content LIKE 'Atlas staging%'").fetchone()
            self.assertIsNotNone(live_keep)
            self.assertIn(live_keep[0], summ[0],
                          "the summary key must carry the POST-compression "
                          "keeper id (compression ran before topic identity)")
        finally:
            c.close()

        second = self._run("organize", "--force", "--json", env_extra=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        rep2 = json.loads(second.stdout)
        self.assertEqual(rep2["summaries"]["created"], 0,
                         f"run 2 must find the existing summary — a create "
                         f"here is the F-002 duplicate-and-orphan failure: {rep2}")
        self.assertEqual(rep2["summaries"]["updated"], 1, rep2)
        c = self._conn()
        try:
            n = c.execute(
                "SELECT count(*) FROM memory WHERE tags='summary,topic' "
                "AND superseded_at IS NULL").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 1, "exactly one live summary across both runs")

    # --- --dry-run writes nothing ---
    def test_dry_run_writes_nothing_and_reports_would_counts(self):
        for content in ("Nova disaster drills cover storage, compute, and the control plane.",
                        "Nova quotas are reviewed at the start of every engineering cycle.",
                        "Nova maintenance windows avoid the monthly capacity test."):
            self._add(content, tags="entity:Nova")
        before = Path(self.store).read_bytes()
        r = self._run("organize", "--force", "--dry-run", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertTrue(report["dry_run"])
        self.assertGreaterEqual(report["summaries"]["would_create"], 1, report)
        self.assertEqual(report["summaries"]["created"], 0)
        after = Path(self.store).read_bytes()
        self.assertEqual(before, after, "organize --dry-run must write NOTHING")

    # --- 7.2 entity + link backfill for working rows missing them ---
    def test_entity_and_link_backfill(self):
        # Two rows inserted RAW (no enrichment, missing entity+link enrichment).
        self._seed_raw("NimbusStack jobs must pin images before any deploy.", order=1)
        self._seed_raw("NimbusStack jobs must pin images before any deploy now.", order=2)
        r = self._run("organize", "--force", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertGreaterEqual(report["entity_backfill"]["candidates"], 2, report)
        self.assertGreaterEqual(report["entity_backfill"]["backfilled"], 2, report)
        self.assertGreaterEqual(report["link_backfill"]["candidates"], 1, report)
        self.assertGreaterEqual(report["link_backfill"]["backfilled"], 1, report)

    def test_entity_backfill_dry_run_uses_extract_only(self):
        self._seed_raw("HeliosStack watchdog must trip on stale heartbeats.", order=1)
        r = self._run("organize", "--force", "--dry-run", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["entity_backfill"]["backfilled"], 0)
        self.assertGreaterEqual(report["entity_backfill"]["would_backfill"], 1, report)

    # --- 7.6 unrecalled prune pass-through ---
    def test_prune_passthrough_requires_flag(self):
        self._add("prune probe row must not be pruned without --prune")
        c = self._conn()
        try:
            c.execute(
                "UPDATE memory SET retrieval_count=0, surfaced_count=0, "
                "confidence=0.2, signal='none', "
                "ingestion_ts=datetime('now','-60 days') "
                "WHERE content LIKE '%prune probe row%'")
            c.commit()
        finally:
            c.close()
        # Without --prune: the inert row survives.
        r1 = self._run("organize", "--force")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        c = self._conn()
        try:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM memory WHERE superseded_at IS NULL AND "
                "content LIKE '%prune probe row%'").fetchone()[0], 1)
        finally:
            c.close()
        # With --prune: it is superseded (prune is expose-not-automatic).
        r2 = self._run("organize", "--force", "--prune", "--json")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        rep2 = json.loads(r2.stdout)
        self.assertGreaterEqual(rep2["pruned"], 1, rep2)

    # --- lock single-flight (shared with consolidate) ---
    def _hold_consolidate_lock(self):
        """Acquire the shared "consolidate" lock IN-PROCESS against the SAME
        store the subprocess runs against (the lock file path derives from
        ZMEM_STORE, which setUp pinned identically for both). Returns
        (mod, token); caller MUST release when done."""
        mod = _load_store_module(Path(self.store), Path(self.tmp) / "no-such-models")
        token = mod._acquire_lock("consolidate", mod.CONSOLIDATE_LOCK_STALE_SECONDS)
        self.assertIsNotNone(token)
        return mod, token

    def test_lock_busy_plain_cli_exits_zero(self):
        mod, token = self._hold_consolidate_lock()
        try:
            r = self._run("organize")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("another organize/consolidation is already running",
                          r.stdout + r.stderr)
        finally:
            mod._release_lock("consolidate", token)

    def test_lock_busy_json_error_object(self):
        mod, token = self._hold_consolidate_lock()
        try:
            r = self._run("organize", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), '{"error": "organize lock busy"}')
        finally:
            mod._release_lock("consolidate", token)


class OrganizeFoldGuardTest(unittest.TestCase):
    """Issue #62, 7.3/7.4 fold guards: when update_memory/add_memory FOLDS the
    new row into a dedup target (created_new=False), organize must log + skip
    rather than force-rewrite a stranger's identity. Only mock injection can
    reach these paths, so they are driven in-process."""

    def setUp(self):
        mod, conn, tmp = _make_store()
        self.mod = mod
        self.conn = conn
        self.tmp = tmp
        # Hermetic organize knobs for the in-process run (final-critic
        # residual #5): organize reads them lazily at call time, so a
        # host-leaked value would perturb the fixture. Popped for this test,
        # restored after.
        _pop_env = ("ZMEM_ORGANIZE_IDLE_HOURS", "ZMEM_ORGANIZE_EPISODE_BOUND",
                    "ZMEM_KEEPER_COMPRESS_CHARS", "ZMEM_UNRECALLED_DAYS",
                    "ZMEM_NLI_CMD")
        _saved = {k: os.environ.pop(k) for k in _pop_env if k in os.environ}
        self.addCleanup(os.environ.update, _saved)
        # Force the lexical fallback for THIS in-process test, hermetically.
        # embeddings._check_available() CACHES its result in the module global
        # _model_available, and on a dev box with a real model in the resolved
        # default models dir the ambient answer is True — the fixture below
        # (3 dissimilar Rho rows, no embeddings in the store) is only valid in
        # lexical mode (in cosine mode the summary bullets semantically dedup
        # onto the member rows and the create legitimately FOLDS). Pin the
        # cache directly instead of mutating process-global env (PRR-012).
        emb_mod = importlib.import_module("embeddings")
        self._saved_model_available = emb_mod._model_available
        emb_mod._model_available = False
        self.addCleanup(
            setattr, emb_mod, "_model_available", self._saved_model_available)
        # `storelib.organize` PACKAGE attribute is clobbered by the re-exported
        # `organize()` function (same ambiguity as `storelib.consolidate`), so
        # reach the real module through sys.modules.
        self.org_mod = importlib.import_module("storelib.organize")
        self.topic_content = [
            "Rho review archives the deployment logs before rotation.",
            "Rho log retention policy compresses everything after ninety days.",
            "Rho rotation schedules are visible in the ops calendar.",
        ]
        self.member_ids = []
        for content in self.topic_content:
            mid = mod.add_memory(conn, namespace=NS, type_="fact", content=content,
                                 tags="entity:Rho", signal="none", confidence=0.5)
            self.member_ids.append(mid)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_summary_create_fold_is_skipped_not_rewritten(self):
        """add_memory returns a dedup target (content != bullets) -> the summary
        is skipped this run; no identity rewrite of the target."""
        def _folding_add(*args, **kwargs):
            return self.member_ids[0]  # a row whose content != the bullets
        orig = self.org_mod.add_memory
        buf = io.StringIO()
        try:
            with mock.patch.object(self.org_mod, "add_memory", _folding_add):
                with redirect_stdout(buf):
                    report = self.org_mod.organize(self.conn, force=True, dry_run=False)
        finally:
            self.org_mod.add_memory = orig
        self.assertEqual(report["summaries"]["skipped"], 1, report)
        self.assertEqual(report["summaries"]["created"], 0, report)
        self.assertIn("summary create folded", buf.getvalue())
        # The folding target (member[0]) was NOT rewritten: its tags stayed user
        # input (not summary,topic).
        row = self.conn.execute(
            "SELECT tags FROM memory WHERE id=?", (self.member_ids[0],)).fetchone()
        self.assertNotEqual(row["tags"], "summary,topic")

    def test_summary_update_fold_is_skipped_not_rewritten(self):
        """Idempotent re-run path: an EXISTING identical summary is updated via
        update_memory (Phase 4). When update_memory FOLDS the rewrite into a
        dedup target (created_new=False), organize must log + skip the update
        rather than claim it succeeded — the target's identity is not ours to
        rewrite (final-critic finding, issue #62 round 3)."""
        # First, an UNMOCKED run creates the real summary (idempotency base).
        report1 = self.org_mod.organize(self.conn, force=True, dry_run=False)
        self.assertEqual(report1["summaries"]["created"], 1, report1)
        # Second run: the exact-match lookup finds the summary and calls
        # update_memory; mock it to FOLD into a stranger row.
        orig = self.org_mod.update_memory
        buf = io.StringIO()
        try:
            with mock.patch.object(
                self.org_mod, "update_memory",
                lambda *a, **k: (self.member_ids[0], False),
            ):
                with redirect_stdout(buf):
                    report2 = self.org_mod.organize(self.conn, force=True, dry_run=False)
        finally:
            self.org_mod.update_memory = orig
        self.assertEqual(report2["summaries"]["updated"], 0, report2)
        self.assertEqual(report2["summaries"]["skipped"], 1, report2)
        self.assertIn("summary update folded", buf.getvalue())

    def test_last_activity_epoch_ignores_malformed_timestamps(self):
        """Final-critic finding (issue #62 round 3): activity is the max over
        PARSEABLE timestamps (computed in Python), so a malformed value can
        never (a) fabricate an epoch-0 idle delta, nor (b) shadow a valid
        timestamp via byte-order (SQL MAX('zzz') > every ISO stamp)."""
        for mid in self.member_ids:
            self.conn.execute(
                "UPDATE memory SET ingestion_ts='not-a-timestamp', "
                "last_retrieved='also-bad' WHERE id=?", (mid,))
        self.conn.commit()
        self.assertIsNone(self.org_mod._last_activity_epoch(self.conn))
        # A VALID recent timestamp wins EVEN when other live rows carry
        # lexically-greater garbage (the Python-max fix; bytes 'z' > ISO '2026').
        now = self.mod.now_iso()
        self.conn.execute(
            "UPDATE memory SET ingestion_ts=? WHERE id=?",
            (now, self.member_ids[0]))
        self.conn.commit()
        self.assertGreater(self.org_mod._last_activity_epoch(self.conn), 0)
        # All-valid timestamps → the max parseable epoch.
        for mid in self.member_ids:
            self.conn.execute(
                "UPDATE memory SET ingestion_ts=?, last_retrieved=NULL, "
                "last_surfaced=NULL WHERE id=?", (now, mid))
        self.conn.commit()
        self.assertGreater(self.org_mod._last_activity_epoch(self.conn), 0)


class SessionStartHookInvocationTest(unittest.TestCase):
    """Issue #62, 7.7 acceptance guard: the maintenance batch at SessionStart
    must dispatch `session-cadence` (whose sleep-time op organize now replaces
    consolidate), never an inline `store.py consolidate` call. This is a
    SOURCE-LEVEL stick test (mirroring the L22 BG_SINK pattern in
    test_low_findings.py): no store required, and it is the only guard that
    would catch a regression silently reverting the hook (final-critic
    finding, issue #62 round 3)."""

    HOOK = REPO_ROOT / "hooks" / "zmem-session-start.sh"

    def _dispatch_lines(self):
        src = self.HOOK.read_text(encoding="utf-8")
        return [ln.strip() for ln in src.splitlines()
                if '"$STORE_PY_PY"' in ln]

    def test_hook_dispatches_session_cadence(self):
        dispatch = self._dispatch_lines()
        self.assertTrue(dispatch, "hook must contain a store.py dispatch line")
        self.assertTrue(
            any("session-cadence" in ln for ln in dispatch),
            f"session-start maintenance must dispatch `session-cadence` not "
            f"consolidate; got:\n" + chr(10).join(dispatch))

    def test_hook_never_invokes_consolidate_directly(self):
        dispatch = self._dispatch_lines()
        inline_consolidate = [ln for ln in dispatch
                              if "consolidate" in ln and "session-cadence" not in ln]
        self.assertEqual(inline_consolidate, [],
                         f"consolidate must not be called inline at SessionStart "
                         f"(organize is the cadence op now, 7.7): "
                         f"{inline_consolidate!r}")


class CosineOrganizeRestrictTest(unittest.TestCase):
    """F-001 regression + the FIRST cosine-mode organize coverage.

    The in-process client suite forces the lexical fallback at module scope, so
    organize's cosine path had ZERO test coverage here — which is how F-001/
    F-003/F-004 shipped undetected (Claude Code review). These tests stub
    embeddings available and inject real vec0 rows, then assert the
    related-graph is bounded to its native episode: an OUT-OF-EPISODE
    near-duplicate in the global vec0 index must never be absorbed/merged into
    the episode, and the union-find must never KeyError on an out-of-episode
    neighbor id (the pre-fix crash).

    Skipped when the bundled sqlite build lacks the vec0 table (mirrors
    test_consolidate_contested.CosineContestedTest).
    """

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store(prefix="zmem-organize-cos-")
        # Hermetic organize knobs (final-critic residual #5): the per-test
        # bound is re-set by _organize via patch.dict; anything else leaking
        # from the host would perturb the geometry assertions.
        _pop_env = ("ZMEM_ORGANIZE_IDLE_HOURS", "ZMEM_ORGANIZE_EPISODE_BOUND",
                    "ZMEM_KEEPER_COMPRESS_CHARS", "ZMEM_UNRECALLED_DAYS",
                    "ZMEM_NLI_CMD")
        _saved = {k: os.environ.pop(k) for k in _pop_env if k in os.environ}
        self.addCleanup(os.environ.update, _saved)
        have_vec = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_vec'"
        ).fetchone()
        if not have_vec:
            self.skipTest("memory_vec (vec0) unavailable in this sqlite build")
        # Cleanup runs LIFO: close the connection BEFORE the tmp dir is removed.
        self.addCleanup(lambda: shutil.rmtree(self.tmp, True))
        self.addCleanup(self.conn.close)
        # Reach the real submodules (the store shim cannot forward attr writes).
        self.org_mod = importlib.import_module("storelib.organize")
        self.cons_mod = importlib.import_module("storelib.consolidate")

    def _vec(self, *values):
        dim = 384
        vals = list(values) + [0.0] * (dim - len(values))
        return struct.pack(f"{dim}f", *vals)

    def _add_embedded_raw(self, content, embedding, *, days_ago=0, namespace="project:cos-ep"):
        """Insert a row DIRECTLY (no write-time dedup/enrichment) with an
        embedding + vec0 entry, like _seed_raw but in cosine mode. ``days_ago``
        back-dates ingestion_ts (ISO-Z form, the real store format) so the
        episode bound can exclude it."""
        mid = str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))
        c = self.conn
        c.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, last_retrieved, embedding, embedding_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,0,NULL,?,'test-stub')""",
            (mid, namespace, "fact", content, "", "", "",
             0.5, "none", ts, ts, embedding))
        c.execute("INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                  (embedding, mid))
        c.commit()
        return mid

    def _organize(self, bound=3):
        stub = mock.Mock()
        stub.is_available.return_value = True
        buf = io.StringIO()
        with mock.patch.object(self.org_mod, "_embeddings", stub), \
             mock.patch.object(self.cons_mod, "_embeddings", stub), \
             mock.patch.dict(os.environ, {"ZMEM_ORGANIZE_EPISODE_BOUND": str(bound)},
                            clear=False):
            with redirect_stdout(buf):
                report = self.org_mod.organize(self.conn, force=True)
        return report, buf.getvalue()

    def test_cosine_episode_ignores_out_of_episode_near_duplicate(self):
        """Regression for F-001: the out-of-episode near-duplicate (cos ~0.99
        to member 1) must NOT be absorbed into the episode nor crash the
        union-find, even though its vec0 row sits in the global index. The
        episode is bounded to 3 (the 3 recent rows); the stale twin is
        excluded and must stay live."""
        # Three orthonormal rows (cos ~0 between them -> no consolidation) that
        # share the project namespace entity, so a 3-member topic forms.
        m1 = self._add_embedded_raw(
            "Pulsar boot pins the kernel before any fleet upgrade.",
            self._vec(1.0, 0.0, 0.0))
        self._add_embedded_raw(
            "Pulsar credentials rotate quarterly and never live in plaintext.",
            self._vec(0.0, 1.0, 0.0))
        self._add_embedded_raw(
            "Pulsar backup window opens on the first weekend of every month.",
            self._vec(0.0, 0.0, 1.0))
        # The STALE TWIN: identical content to m1, ~0 otherwise, OLD ingestion
        # -> falls outside the 3-row episode.
        stale = self._add_embedded_raw(
            "Pulsar boot pins the kernel before any fleet upgrade.",
            self._vec(0.99, 0.01, 0.0), days_ago=30)

        report, _out = self._organize(bound=3)

        self.assertEqual(report["mode"], "cosine")
        c_rep = report["consolidate"]
        # No out-of-episode absorb: the episode's own rows are orthonormal, so
        # consolidation must merge nothing (the stale twin is NOT an episode
        # neighbor).
        self.assertEqual(c_rep["merged"], 0, c_rep)
        # The stale twin survives untouched (a pre-fix global-KNN leak would
        # have tombstoned it into m1).
        row = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (stale,)).fetchone()
        self.assertIsNone(row["superseded_at"],
                          "out-of-episode near-duplicate must NOT be absorbed")
        self.assertIsNone(
            self.conn.execute("SELECT superseded_at FROM memory WHERE id=?",
                              (m1,)).fetchone()["superseded_at"],
            "episode keeper must stay live")
        # 3-member project-entity topic formed and got a summary (mode-correct
        # behavior, not a crash).
        self.assertGreaterEqual(report["summaries"]["created"], 1, report)
        self.assertLessEqual([len(t["members"]) for t in report["topics"]], [3], report)

    def test_cosine_episode_rows_still_cluster_in_episode(self):
        """Positive control: with the same episode geometry and NO stale twin,
        the 3 live rows still form the shared project-entity topic and a
        summary is created — the restrict filter must not have disabled normal
        in-episode grouping."""
        for i, content in enumerate((
                "Quasar boot pins the kernel before any fleet upgrade.",
                "Quasar credentials rotate quarterly and never live in plaintext.",
                "Quasar backup window opens on the first weekend of every month.")):
            vec = [0.0] * i + [1.0] + [0.0] * (2 - i)
            self._add_embedded_raw(content, self._vec(*vec))
        report, _out = self._organize(bound=3)
        self.assertEqual(report["mode"], "cosine")
        self.assertGreaterEqual(report["summaries"]["created"], 1, report)
        self.assertEqual(report["consolidate"]["merged"], 0, report)


class HermesSessionEndStickTest(unittest.TestCase):
    """F-009 guard (final-critic residual #4): the Hermes plugin's session-end
    housekeeping must dispatch `organize` — the same maintenance act as
    SessionStart — never bare `consolidate` (which would arm the shared
    cadence clock while giving Hermes users none of organize's deliverables).
    Source-level stick test, mirroring SessionStartHookInvocationTest: no store
    required, and it is the only guard that would catch a silent revert."""

    HERMES = REPO_ROOT / "hermes-plugin" / "__init__.py"

    def test_hermes_session_end_dispatches_organize(self):
        src = self.HERMES.read_text(encoding="utf-8")
        self.assertIn('_run_store(["organize"])', src,
                      "hermes on_session_end must dispatch organize (issue #62 "
                      "7.7 / F-009)")
        # The retired bare-consolidate dispatch must be gone from the
        # housekeeping block (a `consolidate` string elsewhere — docstrings,
        # other commands — is fine; the exact dispatch list is not).
        self.assertNotIn('_run_store(["consolidate"])', src,
                         "hermes on_session_end must not dispatch bare "
                         "consolidate anymore")


if __name__ == "__main__":
    unittest.main(verbosity=2)
