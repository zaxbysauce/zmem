"""Regression tests for the consolidate cadence gate (issue #26).

Issue #26 reported three compounding defects in the cadence gate at
``store.py``::

1. ``consolidate --dry-run`` skipped the gate entirely (it was guarded by
   ``not dry_run``) and reported "merged N" while a real run inside the cadence
   window silently returned without merging. The two modes answered different
   questions while appearing to answer the same one.
2. The early return was bare — no message — so a skipped real run was
   indistinguishable from a successful no-op merge.
3. The gate was conditioned on ``threshold == CONSOLIDATE_DEFAULT_THRESHOLD``,
   so any non-default ``--threshold`` (e.g. ``0.799``) incidentally disabled the
   gate and forced a real merge.

These tests pin the fix:

- the skip is always announced (real run AND dry-run);
- dry-run models the gate so the two modes agree;
- ``--force`` is the only intentional bypass;
- ``--threshold`` no longer affects the gate;
- the gate is box-wide (not namespace-scoped) and handles negative growth.

Across the suite, gate behaviour is proven rather than inferred: each behaviour
is pinned by a combination of captured stdout (the announced skip / would-skip /
merged message), the live-count delta (merge happened vs not), and the
``last_consolidation`` meta-row state (the canonical signal that a skip left the
cadence clock untouched vs a real merge that reset it). Individual tests assert
the dimensions most relevant to what they prove — e.g. a "writes nothing" test
asserts the meta rows are unchanged AND the would-skip message printed; a force
test asserts the merge ran (live-count dropped) and the message says "merged".
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:consolidate-cadence-26"

# A high-overlap near-duplicate pair (Jaccard well above the 0.60 lexical
# threshold) used wherever a test needs a cluster that WOULD merge.
KEEPER_TEXT = "pytest xdist workers share a tmpdir causing a race condition under flaky quarantine windows"
ABSORB_TEXT = KEEPER_TEXT + " and lane ordering must be deterministic across lane ids"

# Force embeddings deterministically OUT of scope for every op in this module
# (see test_consolidate_lossy for the full rationale): the lazy availability
# check runs under ambient env after the per-store mock env is restored, so a
# host with the shared model cache would flip these consolidate tests to
# embedding semantics.
os.environ["ZMEM_MODELS_DIR"] = str(REPO_ROOT / "no-such-models")


def _load_store_module(store_path: Path, models_dir: Path):
    """A fresh store.py module instance pinned to a throwaway store, with the
    embedding model forced absent (store.py resolves both at import time)."""
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_cadence_{id(store_path)}", STORE_PY
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


def _make_store():
    """Return (mod, conn, tmpdir) for an isolated, model-absent store."""
    tmp = tempfile.mkdtemp(prefix="zmem-cadence26-")
    models = Path(tmp) / "no-models"
    models.mkdir()
    mod = _load_store_module(Path(tmp) / "store.sqlite", models)
    conn = mod.connect()
    mod.init_db(conn)
    mod.migrate(conn)
    return mod, conn, tmp


def _forge_recent_cadence(conn, mod, days_ago=5.0, last_live=100):
    """Write a last_consolidation row `days_ago` days in the past with a
    last_consolidation_count of `last_live`, mimicking a recent prior run on a
    larger store. With a small live set this yields negative growth, so the gate
    fires (days < 7 AND growth < 20%)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation', ?)",
        (ts,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation_count', ?)",
        (str(last_live),),
    )
    conn.commit()
    return ts


def _live(conn):
    return conn.execute(
        "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
    ).fetchone()[0]


def _last_consolidation(conn):
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_consolidation'"
    ).fetchone()
    return row[0] if row else None


def _add_pair(mod, conn, namespace=NS):
    """Add the high-overlap near-duplicate pair that WOULD cluster."""
    mod.add_memory(conn, namespace=namespace, type_="fact", content=KEEPER_TEXT,
                   signal="test", confidence=0.9)
    mod.add_memory(conn, namespace=namespace, type_="fact", content=ABSORB_TEXT,
                   signal="test", confidence=0.85)
    conn.commit()


def _cap(mod, conn, fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


class CadenceGateTest(unittest.TestCase):
    """The gate itself: announced skip, dry-run agreement, force bypass,
    threshold no longer bypasses, growth/interval thresholds, fresh-store,
    box-wide scoping, negative growth."""

    def setUp(self):
        self.mod, self.conn, self.tmp = _make_store()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    # --- AC1: the real run announces the skip (never silent) ---

    def test_real_run_announces_skip_inside_cadence_window(self):
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)
        ts_before = _last_consolidation(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        # The skip is announced with the cadence reason.
        self.assertIn("skipped by cadence gate", out, out)
        self.assertIn("5.0d since last run", out, out)
        self.assertIn("growth", out, out)
        # The gate FIRES on (days<min AND growth<min); by De Morgan it RELEASES
        # on (days>=min OR growth>=min). The message must state the release
        # condition honestly — "OR", not "AND" — because either condition alone
        # unblocks the next run (proven by test_gate_passes_when_growth_exceeds_
        # threshold and test_gate_passes_when_interval_exceeds_min below).
        self.assertIn("needs more time OR more growth", out, out)
        self.assertNotIn("needs more time AND more growth", out, out)
        # No merge happened.
        self.assertEqual(_live(self.conn), live_before,
                         "a gated run must not merge")
        # The canonical proof the gate fired (not the empty-rows return in
        # consolidate(), which prints a different message): the timestamp is
        # NOT refreshed by a skipped run.
        self.assertEqual(_last_consolidation(self.conn), ts_before,
                         "a skipped run must not refresh last_consolidation")
        # And it must NOT say "merged".
        self.assertNotIn("merged", out, out)

    # --- AC2: dry-run models the gate and agrees with the real run ---

    def test_dry_run_models_gate_and_agrees_with_real_run(self):
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)
        ts_before = _last_consolidation(self.conn)

        dry_out = _cap(self.mod, self.conn,
                       lambda: self.mod.consolidate(self.conn, namespace=NS, dry_run=True))
        real_out = _cap(self.mod, self.conn,
                        lambda: self.mod.consolidate(self.conn, namespace=NS))

        # Dry-run says it WOULD skip — NOT "merged N". This is the direct
        # regression for the issue's headline defect (old code printed
        # "merged 1 memories + (dry run — no changes)").
        self.assertIn("would skip by cadence gate", dry_out, dry_out)
        self.assertNotIn("merged", dry_out, dry_out)
        self.assertNotIn("DRY RUN: cluster", dry_out, dry_out)
        # Real run says it skipped. Both modes agree: neither merges.
        self.assertIn("skipped by cadence gate", real_out, real_out)
        # Neither mode mutated the store or the cadence meta.
        self.assertEqual(_live(self.conn), live_before,
                         "neither dry-run nor gated real run should merge")
        self.assertEqual(_last_consolidation(self.conn), ts_before,
                         "neither mode should refresh last_consolidation")
        # Dry-run points the user at the escape hatch.
        self.assertIn("--force", dry_out, dry_out)

    # --- AC3: --force is the only intentional bypass (real run) ---

    def test_force_bypasses_gate_real_run(self):
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS, force=True))

        self.assertIn("merged 1 memories", out, out)
        self.assertNotIn("skipped by cadence gate", out, out)
        self.assertEqual(_live(self.conn), live_before - 1,
                         "force must let the merge through")

    # --- AC4: --force + --dry-run previews despite the gate ---

    def test_force_with_dry_run_previews_despite_gate(self):
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS,
                                                dry_run=True, force=True))

        # Gate is bypassed, so the cluster preview runs.
        self.assertIn("DRY RUN: cluster", out, out)
        self.assertIn("would APPEND", out, out)
        self.assertNotIn("would skip by cadence gate", out, out)
        # Dry-run never mutates.
        self.assertEqual(_live(self.conn), live_before,
                         "dry-run must not merge even with --force")

    # --- AC5: --threshold no longer bypasses the gate (side-channel removed) ---

    def test_threshold_no_longer_bypasses_gate(self):
        """Direct regression for the side-channel defect. MUST FAIL on the old
        code (which merged on threshold=0.799) and PASS on the new (gate fires
        regardless of threshold)."""
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS, threshold=0.799))

        self.assertIn("skipped by cadence gate", out, out)
        self.assertNotIn("merged", out, out)
        self.assertEqual(_live(self.conn), live_before,
                         "a non-default threshold must NOT bypass the gate")

    # --- AC6: the gate passes when growth exceeds the threshold ---

    def test_gate_passes_when_growth_exceeds_threshold(self):
        # last_live=1 and we add 2 rows -> growth = (2-1)/1 = 100% >= 20%.
        _forge_recent_cadence(self.conn, self.mod, days_ago=1.0, last_live=1)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        self.assertIn("merged 1 memories", out, out)
        self.assertEqual(_live(self.conn), live_before - 1,
                         "growth above threshold must let the merge through")

    # --- AC7: the gate passes when the interval exceeds the minimum ---

    def test_gate_passes_when_interval_exceeds_min(self):
        # > 7 days ago with low growth (last_live large). days_since >= 7 wins.
        _forge_recent_cadence(self.conn, self.mod, days_ago=10.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        self.assertIn("merged 1 memories", out, out)
        self.assertEqual(_live(self.conn), live_before - 1,
                         "interval above minimum must let the merge through")

    # --- AC8: a fresh store (no last_consolidation) is never gated ---

    def test_no_last_consolidation_never_gated(self):
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        self.assertIn("merged 1 memories", out, out)
        self.assertNotIn("skipped by cadence gate", out, out)
        self.assertEqual(_live(self.conn), live_before - 1)
        # The run writes the cadence meta for the next run.
        self.assertIsNotNone(_last_consolidation(self.conn))

    # --- AC9: the gate is box-wide, not namespace-scoped ---

    def test_gate_is_box_wide_not_namespace_scoped(self):
        """The gate's live_count is the BOX-WIDE live count, not the
        --namespace-filtered count. Pin that design decision: a heavy-churn
        namespace B with a quiet namespace A still gates an A-scoped run when
        the box as a whole is recent and quiet relative to A."""
        other_ns = "project:other-namespace-26"
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn, namespace=NS)
        _add_pair(self.mod, self.conn, namespace=other_ns)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        # Box-wide live is small (4) vs last_live=100 -> negative growth; the
        # gate fires even though the run is scoped to NS.
        self.assertIn("skipped by cadence gate", out, out)
        self.assertEqual(_live(self.conn), live_before,
                         "box-wide gate must gate a namespace-scoped run")

    # --- AC10: negative growth is announced cleanly (no crash) ---

    def test_negative_growth_announced_cleanly(self):
        # last_live=1000, live=2 -> growth = (2-1000)/1000 = -99.8%.
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=1000)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS))

        self.assertIn("skipped by cadence gate", out, out)
        # Negative growth formats without crashing and reports a negative %.
        self.assertIn("-99.8% growth", out, out)
        self.assertEqual(_live(self.conn), live_before)

    # --- AC10b: a fractional env-overridden growth threshold renders precisely
    # (regression for cubic-2 / PRR-004: int(threshold*100) and :.0% both lost
    # sub-percent precision; :.1% must render 0.205 as "20.5%"). ---

    def test_fractional_growth_threshold_renders_precisely(self):
        # Reload store.py with a fractional growth threshold so the rendered
        # minimum in the skip message is asserted, not just the growth value.
        tmp = tempfile.mkdtemp(prefix="zmem-cadence26-thr-")
        models = Path(tmp) / "no-models"
        models.mkdir()
        env = {
            **os.environ,
            "ZMEM_STORE": str(Path(tmp) / "store.sqlite"),
            "ZMEM_MODELS_DIR": str(models),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_CONSOLIDATE_GROWTH_THRESHOLD": "0.205",
        }
        # Drop inherited data-dir hints so ZMEM_STORE is authoritative.
        for k in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            env.pop(k, None)
        spec = importlib.util.spec_from_file_location(
            f"zmem_store_cadence_thr_{id(tmp)}", STORE_PY
        )
        with mock.patch.dict(os.environ, env, clear=False):
            tmod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tmod)
        tconn = tmod.connect()
        tmod.init_db(tconn)
        tmod.migrate(tconn)
        try:
            _forge_recent_cadence(tconn, tmod, days_ago=5.0, last_live=1000)
            tmod.add_memory(tconn, namespace=NS, type_="fact", content=KEEPER_TEXT,
                            signal="test", confidence=0.9)
            tconn.commit()
            out = _cap(tmod, tconn,
                       lambda: tmod.consolidate(tconn, namespace=NS))
            self.assertIn("skipped by cadence gate", out, out)
            # The threshold renders at one-decimal precision — NOT truncated to
            # "20%" (int(0.205*100) or :.0%) but "20.5%" (:.1%). This is the
            # direct regression for the cubic-2 / PRR-004 truncation defect.
            self.assertIn("20.5% min", out, out)
            self.assertNotIn("20% min", out, out)
        finally:
            tconn.close()
            shutil.rmtree(tmp, True)

    # --- AC11: a dry-run skip writes nothing (no meta refresh) ---

    def test_dry_run_skip_writes_nothing(self):
        _forge_recent_cadence(self.conn, self.mod, days_ago=5.0, last_live=100)
        _add_pair(self.mod, self.conn)
        live_before = _live(self.conn)
        ts_before = _last_consolidation(self.conn)
        count_before = self.conn.execute(
            "SELECT value FROM meta WHERE key='last_consolidation_count'"
        ).fetchone()[0]

        out = _cap(self.mod, self.conn,
                   lambda: self.mod.consolidate(self.conn, namespace=NS, dry_run=True))

        # The dry-run skip is announced as a would-skip (NOT "merged N") — the
        # headline behaviour of issue #26. Assert stdout so a regression to the
        # old silent-then-"merged" dry-run path fails this test.
        self.assertIn("would skip by cadence gate", out, out)
        self.assertNotIn("merged", out, out)
        self.assertEqual(_live(self.conn), live_before,
                         "dry-run must not merge")
        # Dry-run never writes — even in the skip path the cadence meta is
        # untouched. (The skip returns before the timestamp write block in
        # consolidate(), and dry-run would skip that block anyway.)
        self.assertEqual(_last_consolidation(self.conn), ts_before,
                         "dry-run must not refresh last_consolidation")
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM meta WHERE key='last_consolidation_count'"
            ).fetchone()[0], count_before,
            "dry-run must not refresh last_consolidation_count")


class CadenceGateCLITest(unittest.TestCase):
    """The --force flag is wired through the CLI and the announced skip reaches
    the foreground user via stdout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-cadence26-cli-")
        self.store = Path(self.tmp) / "store.sqlite"
        self.models = Path(self.tmp) / "no-models"
        self.models.mkdir()
        # os.environ FIRST so our explicit keys win (a prior test in the full
        # suite may have left a ZMEM_STORE/ZMEM_DATA in os.environ; if it came
        # last it would silently redirect our CLI subprocess to the wrong store,
        # which is exactly the cross-test pollution that made the raw-sqlite
        # forge hit "no such table: meta").
        self.env = {
            **os.environ,
            "ZMEM_STORE": str(self.store),
            "ZMEM_MODELS_DIR": str(self.models),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        # Drop any inherited data-dir hints so ZMEM_STORE is authoritative.
        for k in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def _run(self, *args):
        import subprocess
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            capture_output=True, text=True, env=self.env, timeout=60,
        )

    def _seed_and_forge(self):
        """Build a store with a near-duplicate pair and a recent cadence row."""
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", KEEPER_TEXT)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", ABSORB_TEXT)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Forge a recent cadence row directly via sqlite3 (no CLI setter).
        import sqlite3
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 86400))
        conn = sqlite3.connect(self.store)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation', ?)",
            (ts,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation_count', ?)",
            ("100",),
        )
        conn.commit()
        conn.close()

    def test_cli_real_run_prints_skip_to_stdout(self):
        self._seed_and_forge()
        r = self._run("consolidate", "--namespace", NS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped by cadence gate", r.stdout, r.stdout)
        self.assertNotIn("merged", r.stdout, r.stdout)

    def test_cli_dry_run_prints_would_skip_to_stdout(self):
        self._seed_and_forge()
        r = self._run("consolidate", "--namespace", NS, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would skip by cadence gate", r.stdout, r.stdout)
        self.assertNotIn("merged", r.stdout, r.stdout)

    def test_cli_force_merges_despite_gate(self):
        self._seed_and_forge()
        r = self._run("consolidate", "--namespace", NS, "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("merged 1 memories", r.stdout, r.stdout)
        self.assertNotIn("skipped by cadence gate", r.stdout, r.stdout)

    def test_cli_force_with_dry_run_previews_despite_gate(self):
        self._seed_and_forge()
        r = self._run("consolidate", "--namespace", NS, "--force", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The gate was bypassed, so the cluster preview ran. Assert the preview
        # markers (NOT a specific absorb decision — the lexical-threshold swap
        # means the absorb row may be "already represented" rather than
        # "would APPEND", which is fine; the point is the preview ran at all).
        self.assertIn("DRY RUN: cluster", r.stdout, r.stdout)
        self.assertIn("absorb", r.stdout, r.stdout)
        self.assertNotIn("would skip by cadence gate", r.stdout, r.stdout)

    def test_cli_threshold_does_not_bypass_gate(self):
        self._seed_and_forge()
        r = self._run("consolidate", "--namespace", NS, "--threshold", "0.799")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped by cadence gate", r.stdout, r.stdout)
        self.assertNotIn("merged", r.stdout, r.stdout)


if __name__ == "__main__":
    unittest.main()
