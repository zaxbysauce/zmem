"""Contested-cluster (contradiction) guard for consolidate — issue #49 A.

Ported concept from claude-reflect's contradiction category: cosine/Jaccard
similarity ranks "always X" and "never X" as near-duplicates, so merging by
similarity alone absorbs a memory's own refutation into the row it
contradicts. These tests pin the guard: mixed negation-polarity clusters are
never auto-merged (not even with --force), are always reported, and
--merge-contested is the explicit override. Both clustering paths (lexical
fallback and cosine via embedded stub vectors) are covered.
"""

import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:consolidate-contested-49"

# The issue's acceptance-criteria pair: identical except for the polarity word
# (Jaccard = 5/6 ~ 0.83, well above the 0.60 lexical threshold).
POS = "always run migrations before deploy"
NEG = "never run migrations before deploy"

# Same-polarity near-duplicate pair (Jaccard = 5/7 ~ 0.71, above threshold).
DUP_A = "always run migrations before deploy"
DUP_B = "always run migrations before deploy every time"

# Positive-polarity row similar to BOTH the POS and NEG rows (Jaccard 4/6 =
# 0.67 each) so a single 3-member mixed cluster forms regardless of seed order.
MIX_C = "rarely run migrations before deploy"

# Force embeddings deterministically OUT of scope for the whole module so the
# lexical-fallback assertions cannot be hijacked by a host model cache (same
# rationale as test_consolidate_lossy.py).
os.environ["ZMEM_MODELS_DIR"] = str(REPO_ROOT / "no-such-models")


def _load_store_module(store_path: Path, models_dir: Path):
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_contested_{id(store_path)}", STORE_PY
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


def _make_store(prefix="zmem-contested49-"):
    tmp = tempfile.mkdtemp(prefix=prefix)
    models = Path(tmp) / "no-models"
    models.mkdir()
    mod = _load_store_module(Path(tmp) / "store.sqlite", models)
    conn = mod.connect()
    mod.init_db(conn)
    mod.migrate(conn)
    return mod, conn, tmp


def _add_raw(mod, conn, content, confidence, rc=0, signal="test"):
    """Insert a row DIRECTLY into memory, bypassing write-time dedup so
    near-identical rows coexist for consolidate's clustering (house pattern
    from test_consolidate_lossy.py)."""
    import uuid
    mid = str(uuid.uuid4())
    ts = mod.now_iso()
    conn.execute(
        """INSERT INTO memory
           (id, namespace, type, content, tags, source_ref, source_hash,
            confidence, signal, valid_from, superseded_at, ingestion_ts,
            retrieval_count, last_retrieved)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)""",
        (mid, NS, "fact", content, "", "", "", confidence, signal, ts, ts, rc),
    )
    conn.commit()
    return mid


def _live_rows(conn, *ids):
    rows = conn.execute(
        "SELECT id, superseded_at FROM memory WHERE id IN (%s)"
        % ",".join("?" * len(ids)), list(ids),
    ).fetchall()
    return {r["id"]: r["superseded_at"] for r in rows}


def _run_consolidate(mod, conn, **kwargs):
    """Run consolidate capturing stdout; return (report, captured_stdout)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        report = mod.consolidate(conn, **kwargs)
    return report, buf.getvalue()


class PolaritySignatureTest(unittest.TestCase):
    def _sig(self, content):
        mod = self._mod
        return mod._polarity_signature(content)

    @classmethod
    def setUpClass(cls):
        cls._mod, cls._conn, cls._tmp = _make_store()

    @classmethod
    def tearDownClass(cls):
        cls._conn.close()
        shutil.rmtree(cls._tmp, True)

    def test_negators_flag(self):
        for text in ("never X", "don't do this", "do not commit to main",
                     "doesn't apply", "can't rely on it", "cannot be cached",
                     "won't survive restart", "not supported here",
                     "avoid global state", "stop the server first",
                     "no longer needed"):
            self.assertTrue(self._sig(text), text)

    def test_positive_text_has_no_polarity(self):
        for text in ("always run migrations before deploy",
                     "prefer tabs in this repo",
                     "run tests before pushing"):
            self.assertFalse(self._sig(text), text)

    def test_word_boundaries_no_false_hits(self):
        # "not" inside note/annotated/cannot-adjacent words must not flag.
        self.assertFalse(self._sig("note that annotations are required"))
        self.assertFalse(self._sig("annotate the endpoint"))

    def test_code_spans_ignored(self):
        self.assertFalse(self._sig("always pass `--no-verify` (see `git push not --force`)"))

    def test_double_quoted_spans_ignored(self):
        self.assertFalse(self._sig('when tests fail with "module not found", rerun setup'))

    def test_curly_apostrophe_normalized(self):
        self.assertTrue(self._sig("don’t commit to main"))

    def test_empty(self):
        self.assertFalse(self._sig(""))
        self.assertFalse(self._sig(None))


class LexicalContestedTest(unittest.TestCase):
    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def test_opposite_polarity_not_merged_real_run(self):
        """AC1: the contradiction pair survives consolidate --force with both
        rows live and a contested report printed."""
        pos = _add_raw(self.mod, self.conn, POS, 0.9)
        neg = _add_raw(self.mod, self.conn, NEG, 0.9)
        report, out = _run_consolidate(self.mod, self.conn, force=True)

        live = _live_rows(self.conn, pos, neg)
        self.assertEqual(len(live), 2)
        self.assertIsNone(live[pos])
        self.assertIsNone(live[neg])

        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["contested_clusters"]), 1)
        cluster = report["contested_clusters"][0]
        self.assertFalse(cluster["merged"])
        pols = {m["polarity"] for m in cluster["members"]}
        self.assertEqual(pols, {"pos", "neg"})
        previews = {m["content_preview"] for m in cluster["members"]}
        self.assertEqual(previews, {POS, NEG})

        self.assertIn("CONTESTED cluster", out)
        self.assertIn("NOT merged (contested)", out)
        self.assertIn("contested 1 cluster(s) (not merged", out)

    def test_opposite_polarity_dry_run_shows_only_contested_block(self):
        pos = _add_raw(self.mod, self.conn, POS, 0.9)
        neg = _add_raw(self.mod, self.conn, NEG, 0.9)
        report, out = _run_consolidate(self.mod, self.conn, dry_run=True, force=True)

        self.assertIn("would NOT merge (contested)", out)
        # The contested cluster must NOT be previewed as a mergeable cluster.
        self.assertNotIn("would APPEND", out)
        self.assertNotIn("DRY RUN: cluster", out)
        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["contested_clusters"]), 1)
        # No mutation: both live, no provenance recorded.
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (pos,)).fetchone()[0])
        self.assertIsNone(self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (neg,)).fetchone()[0])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM memory WHERE merged_from IS NOT NULL"
        ).fetchone()[0], 0)

    def test_same_polarity_near_dupes_still_merge(self):
        a = _add_raw(self.mod, self.conn, DUP_A, 0.9)
        b = _add_raw(self.mod, self.conn, DUP_B, 0.85)
        report, out = _run_consolidate(self.mod, self.conn, force=True)

        self.assertEqual(report["merged"], 1)
        self.assertEqual(report["contested_clusters"], [])
        live = _live_rows(self.conn, a, b)
        self.assertEqual(sum(1 for v in live.values() if v is None), 1)

    def test_merge_contested_overrides_and_reports_merged(self):
        pos = _add_raw(self.mod, self.conn, POS, 0.9)
        neg = _add_raw(self.mod, self.conn, NEG, 0.9)
        report, _ = _run_consolidate(self.mod, self.conn, force=True,
                                     merge_contested=True)

        self.assertEqual(report["merged"], 1)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertTrue(report["contested_clusters"][0]["merged"])
        live = _live_rows(self.conn, pos, neg)
        self.assertEqual(sum(1 for v in live.values() if v is None), 1)

    def test_mixed_cluster_reported_once_no_mirror(self):
        """A three-row mutually-similar cluster with mixed polarity is reported
        exactly once (no mirror re-report from the parked members)."""
        a = _add_raw(self.mod, self.conn, POS, 0.9)
        b = _add_raw(self.mod, self.conn, NEG, 0.9)
        c = _add_raw(self.mod, self.conn, MIX_C, 0.8)  # pos, similar to both
        report, out = _run_consolidate(self.mod, self.conn, force=True)

        self.assertEqual(out.count("CONTESTED cluster"), 1)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertEqual(len(report["contested_clusters"][0]["members"]), 3)
        self.assertEqual(report["merged"], 0)
        live = _live_rows(self.conn, a, b, c)
        self.assertEqual(len(live), 3)
        self.assertTrue(all(v is None for v in live.values()))

    def test_cadence_gate_skip_returns_report(self):
        _add_raw(self.mod, self.conn, POS, 0.9)
        # First run passes (no prior consolidation timestamp).
        _run_consolidate(self.mod, self.conn, force=True)
        # Second run without force hits the cadence gate (fresh timestamp, no
        # growth) and must return a report saying so.
        report, out = _run_consolidate(self.mod, self.conn)
        self.assertTrue(report["skipped_by_cadence_gate"])
        self.assertIn("skipped by cadence gate", out)
        self.assertEqual(report["merged"], 0)


class CliJsonTest(unittest.TestCase):
    """CLI --json contract (PRR-004): stdout strictly json.loads-parseable,
    human prose on stderr; --merge-contested round-trip; host-independent
    (subprocess == plain CLI == what any host session runs)."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store(prefix="zmem-contested49-cli-")
        self.ids = (
            _add_raw(self.mod, self.conn, POS, 0.9),
            _add_raw(self.mod, self.conn, NEG, 0.9),
        )
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def _cli(self, *extra):
        env = {
            **os.environ,
            "ZMEM_STORE": self.store_path,
            "ZMEM_MODELS_DIR": os.path.join(self.tmp, "no-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS": "0",
        }
        return subprocess.run(
            [PYTHON, str(STORE_PY), "consolidate", "--force", *extra],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def test_json_stdout_is_pure_and_reports_contested(self):
        result = self._cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)  # must parse with nothing else around
        self.assertEqual(report["mode"], "lexical")
        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertFalse(report["contested_clusters"][0]["merged"])
        # Human prose lives on stderr, never stdout.
        self.assertNotIn("[zmem]", result.stdout)
        self.assertIn("[zmem]", result.stderr)

    def test_json_with_merge_contested_round_trip(self):
        result = self._cli("--merge-contested", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertTrue(report["contested_clusters"][0]["merged"])

    def test_plain_cli_both_rows_live_after_force(self):
        """AC1 host-independence: plain CLI invocation (what a CC session or a
        cron job runs) leaves both contradiction rows live."""
        result = self._cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONTESTED cluster", result.stdout)
        conn = self.mod.connect()
        try:
            rows = conn.execute(
                "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 2)

    def test_json_failure_path_leaves_stdout_clean(self):
        """Under --json an exception inside consolidate() must never put
        non-JSON bytes on stdout: the traceback goes to stderr, stdout stays
        empty, and the exit code is nonzero (reviewer finding #3)."""
        conn = self.mod.connect()
        try:
            conn.execute("DROP TABLE meta")  # cadence-gate query raises mid-run
            conn.commit()
        finally:
            conn.close()
        result = self._cli("--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "", result.stdout[:200])
        self.assertTrue(result.stderr.strip(), "expected a traceback on stderr")


class CosineContestedTest(unittest.TestCase):
    """Cosine-path guard via stub embeddings + real vec0 table (skipped when
    the sqlite build lacks vec0, mirroring test_consolidate_lossy.py)."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="zmem-contested49-cos-")
        self.tmp = tmp
        models = Path(tmp) / "no-models"
        models.mkdir()
        self.mod = _load_store_module(Path(tmp) / "s.sqlite", models)
        self.conn = self.mod.connect()
        self.mod.init_db(self.conn)
        self.mod.migrate(self.conn)
        have_vec = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='memory_vec'"
        ).fetchone()
        if not have_vec:
            self.skipTest("memory_vec (vec0) unavailable in this sqlite build")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def _vec(self, *values):
        dim = 384
        vals = list(values) + [0.0] * (dim - len(values))
        return struct.pack(f"{dim}f", *vals)

    def _add_embedded(self, content, confidence, embedding, rc=0):
        mid = self.mod.add_memory(self.conn, namespace=NS, type_="fact",
                                  content=content, signal="test",
                                  confidence=confidence)
        self.conn.execute(
            "UPDATE memory SET embedding=?, embedding_model='test-stub', "
            "retrieval_count=? WHERE id=?", (embedding, rc, mid))
        self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
        self.conn.execute(
            "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
            (embedding, mid))
        self.conn.commit()
        return mid

    def _consolidate(self):
        stub = mock.Mock()
        stub.is_available.return_value = True
        buf = io.StringIO()
        with mock.patch.object(self.mod, "_embeddings", stub):
            with redirect_stdout(buf):
                report = self.mod.consolidate(self.conn, force=True)
        return report, buf.getvalue()

    def test_cosine_path_contested_not_merged(self):
        import math
        a = self._add_embedded(POS, 0.9, self._vec(1.0, 0.0), rc=0)
        b = self._add_embedded(NEG, 0.85, self._vec(math.cos(0.4), math.sin(0.4)))
        report, out = self._consolidate()

        self.assertEqual(report["mode"], "cosine")
        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertIn("CONTESTED cluster", out)
        live = _live_rows(self.conn, a, b)
        self.assertTrue(all(v is None for v in live.values()))

    def test_cosine_fresh_seed_merges_contested_member(self):
        """Contested members stay NEIGHBOR-eligible: A(pos)/B(neg) contest and
        park; a later fresh seed D(neg), similar to B but NOT to A, still
        merges with B. Geometry: A at 0deg, B at ~23deg (cos(A,B)~0.92),
        D at ~55deg (cos(D,B)~0.87, cos(D,A)~0.57 < 0.80)."""
        import math
        a = self._add_embedded(POS, 0.95, self._vec(1.0, 0.0), rc=5)
        b = self._add_embedded(NEG, 0.9, self._vec(math.cos(0.4), math.sin(0.4)))
        d = self._add_embedded(
            "don't run migrations before deploy",
            0.8, self._vec(math.cos(0.96), math.sin(0.96)))
        report, out = self._consolidate()

        # One contested cluster {A, B} reported; D absorbed B (same polarity).
        self.assertEqual(out.count("CONTESTED cluster"), 1)
        self.assertEqual(len(report["contested_clusters"]), 1)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(
            {m["polarity"] for m in report["contested_clusters"][0]["members"]},
            {"pos", "neg"})
        # A live; B tombstoned into D; D live holding provenance.
        self.assertIsNone(_live_rows(self.conn, a)[a])
        b_state = _live_rows(self.conn, b)[b]
        self.assertIsNotNone(b_state)
        row = self.conn.execute(
            "SELECT merged_from FROM memory WHERE id=?", (d,)).fetchone()
        self.assertEqual(row["merged_from"], b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
