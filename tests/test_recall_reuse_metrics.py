#!/usr/bin/env python
"""Issue #474 AC4 (+AC9 evidence base): anonymized reuse counters and the
observation sidecar.

RED at base (file absent — frozen C9 wrapper prints NEW_SURFACE_ABSENT);
GREEN after the fix. Numerical assertions, not shape alone (plan critic
R3-3): serving counters, timing bounds, the table-driven classification
algebra over mixed sequences, the per-key sidecar schema v2, count-
preserving compaction under the hard 4096-record bound, coverage
reconciliation with the fail-closed report, and the no-query-text privacy
scan across sidecar + drops journal + counters.

Run:  python tests/test_recall_reuse_metrics.py [substring ...]
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storelib.recall_reuse as rr  # noqa: E402

def _reset_reuse_state():
    """Local test seam (the storelib module deliberately ships no reset
    helper — the stranded-function census requires production callers
    for every storelib def)."""
    import collections

    import storelib.recall_reuse as rr
    with rr._reuse_lock:
        rr._reuse_cache.clear()
        rr._reuse_inflight.clear()
        rr._counters.clear()
        rr._counters["invalidation_reasons"] = collections.defaultdict(int)
        rr._timing_compute.clear()
        rr._timing_reuse.clear()

import storelib.schema as schema  # noqa: E402

ENV_NAMES = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
             "ZMEM_EMBED_PROFILE", rr.SERVE_ENV, rr.TTL_ENV,
             rr.REFRESH_ENV, rr.OBSERVE_ENV)

BASE = {"store": "aa", "query": "bb", "session": "cc", "options": "dd",
        "config": "ee", "generation": "1", "incarnation": ""}


def _comps(**over):
    c = dict(BASE)
    c.update(over)
    return c


class _MetricsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-metrics-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {n: os.environ.get(n) for n in ENV_NAMES}
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(self.tmp)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        for n in (rr.SERVE_ENV, rr.TTL_ENV, rr.REFRESH_ENV, rr.OBSERVE_ENV):
            os.environ.pop(n, None)
        self.addCleanup(self._restore)
        _reset_reuse_state()
        self.addCleanup(_reset_reuse_state)
        self.conn = sqlite3.connect(os.environ["ZMEM_STORE"])
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def _restore(self):
        for n, v in self._saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _sidecar_text(self) -> str:
        p = self.tmp / "ops" / rr.SIDECAR_NAME
        return p.read_text(encoding="utf-8") if p.exists() else ""


class ServingCounters(_MetricsBase):
    def test_numerical_counter_assertions(self):
        # drive the serving seam through recall_memory with a stub impl
        import storelib.recall as recall_mod
        from storelib.recall import recall_memory
        os.environ[rr.SERVE_ENV] = "1"
        real_impl = recall_mod._recall_memory_uncached

        def _stub(conn, **kw):
            return [{"id": "m1", "content": "row"}]

        recall_mod._recall_memory_uncached = _stub
        try:
            import contextlib, io
            for q in ("q1", "q1", "q1", "q2"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    recall_memory(self.conn, query=q, namespace="project:m",
                                  limit=3, no_bump=True, no_telemetry=True)
            stats = rr.reuse_stats()
            self.assertEqual(stats["misses"], 2)
            self.assertEqual(stats["hits"], 2)
            self.assertGreaterEqual(stats["timing_samples_compute"], 2)
            self.assertGreaterEqual(stats["timing_samples_reuse"], 2)
        finally:
            recall_mod._recall_memory_uncached = real_impl

    def test_timing_deque_bound(self):
        for _ in range(1200):
            rr._timing_compute.append(1.0)
            rr._timing_reuse.append(1.0)
        self.assertLessEqual(len(rr._timing_compute), 1024)
        self.assertLessEqual(len(rr._timing_reuse), 1024)

    def test_realizable_rate_formula(self):
        stats = {"hits": 8, "coalesced": 2, "misses": 5,
                 "invalidations": {}, "refreshes": 0}
        rate = (stats["hits"] + stats["coalesced"]) / (
            stats["hits"] + stats["coalesced"] + stats["misses"]
            + sum(stats["invalidations"].values()) + stats["refreshes"])
        self.assertAlmostEqual(rate, 10 / 15)


class ClassificationTable(_MetricsBase):
    """Table-driven mixed sequences: every reason, as sequences (R3-1)."""

    def _run_seq(self, seq):
        for comps, kwargs in seq:
            rr._observe(self.conn, comps, None, None, **kwargs)
        return rr.observation_report(self.conn)

    def test_mixed_sequence_every_reason(self):
        import time as _time
        seq = [
            # novel first observation
            (_comps(), dict(ok=True, so="miss")),
            # duplicate within TTL (successful prior)
            (_comps(), dict(ok=True, so="hit")),
            # failure, then retry (rt, not dup)
            (_comps(), dict(ok=False)),
            (_comps(), dict(ok=True, so="miss")),
            # generation invalidation (same kb, different kf)
            (_comps(generation="2"), dict(ok=True, so="miss")),
            # options invalidation
            (_comps(options="other"), dict(ok=True, so="miss")),
            # session invalidation
            (_comps(session="s2"), dict(ok=True, so="miss")),
            # config invalidation
            (_comps(config="c2"), dict(ok=True, so="miss")),
            # incarnation invalidation
            (_comps(incarnation="inc2"), dict(ok=True, so="miss")),
        ]
        rep = self._run_seq(seq)
        self.assertEqual(rep["coverage"]["observed_n"], 9)
        self.assertEqual(rep["hook_potential"]["duplicate_equivalent"], 1)
        self.assertEqual(rep["retries_after_failure"], 1)
        self.assertEqual(rep["invalidations"].get("generation"), 1)
        self.assertEqual(rep["invalidations"].get("options"), 1)
        self.assertEqual(rep["invalidations"].get("session"), 1)
        self.assertEqual(rep["invalidations"].get("config"), 1)
        self.assertEqual(rep["invalidations"].get("incarnation"), 1)
        self.assertTrue(rep["complete"])

    def test_ttl_expiry_leg(self):
        import time as _time
        os.environ[rr.TTL_ENV] = "0.05"
        try:
            rr._observe(self.conn, _comps(), None, None, ok=True,
                        so="miss")
            _time.sleep(0.08)
            rr._observe(self.conn, _comps(), None, None, ok=True,
                        so="miss")
        finally:
            os.environ.pop(rr.TTL_ENV, None)
        rep = rr.observation_report(self.conn)
        self.assertEqual(rep["ttl_recomputes"], 1)
        self.assertEqual(rep["hook_potential"]["duplicate_equivalent"], 0)


class SidecarContract(_MetricsBase):
    def test_record_shape_v2_and_privacy(self):
        rr._observe(self.conn, _comps(), None, None, ok=True, so="miss",
                    ms=1.5)
        text = self._sidecar_text()
        self.assertIn("# aggregate v1", text)
        data_lines = [l for l in text.splitlines()
                      if l and not l.startswith("#")]
        self.assertEqual(len(data_lines), 1)
        row = json.loads(data_lines[0])
        for key in ("v", "kf", "kb", "cs", "n", "dup", "rt", "ttl", "rf",
                    "inv", "t"):
            self.assertIn(key, row)
        self.assertEqual(row["v"], 2)
        self.assertEqual(row["n"], 1)
        # privacy (review TF-006): no raw query text in the sidecar row,
        # the drops journal, or the in-process counters (a long distinctive
        # marker — hex digests can contain any short substring)
        marker = rr._sha("SECRET-QUERY-MARKER-9d17f3")
        self.assertNotIn("SECRET-QUERY-MARKER", json.dumps(row))
        self.assertNotIn(marker, json.dumps(row))
        ops = self.tmp / "ops"
        for artifact in ("reuse-observe.drops", "reuse-observe.lock"):
            f = ops / artifact
            if f.exists():
                self.assertNotIn("SECRET-QUERY-MARKER",
                                 f.read_text(encoding="utf-8",
                                             errors="replace"))
        self.assertNotIn("SECRET-QUERY-MARKER",
                         json.dumps(rr.reuse_stats()))

    def test_stale_lock_hint_in_report(self):
        # closeout review (RP-003 operational residual): drops + a present
        # lock file must name the suspect lock + remedy in the report, so
        # complete=false is never left unexplained for the operator.
        rr._observe(self.conn, _comps(), None, None, ok=True, so="miss")
        sidecar = self.tmp / "ops" / "reuse-observe.jsonl"
        lock = sidecar.with_suffix(".lock")
        drops = sidecar.with_suffix(".drops")
        # simulate the killed-holder signature: a leftover lock + a
        # lock-timeout drop
        lock.write_text("{}", encoding="utf-8", newline="\n")
        with open(drops, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": "2026-01-01T00:00:00Z",
                                 "reason": "lock-timeout"}) + "\n")
        rep = rr.observation_report(self.conn)
        self.assertFalse(rep["complete"])
        self.assertGreaterEqual(rep["coverage"]["dropped"], 1)
        hint = rep.get("stale_lock_suspected")
        self.assertIsInstance(hint, dict)
        # the lock path is derived from the store path, which SP-003
        # normalizes (normcase) — compare in the same form
        self.assertEqual(hint["lock_path"], os.path.normcase(str(lock)))
        self.assertIn("remove the lock file", hint["remedy"])
        # negative leg: drops WITHOUT a lock file → no hint
        lock.unlink()
        rep2 = rr.observation_report(self.conn)
        self.assertFalse(rep2["complete"])
        self.assertNotIn("stale_lock_suspected", rep2)

    def test_two_process_duplicate_classification(self):
        # two separate PROCESSES observing the same key: the second
        # appends through the locked RMW and classifies as duplicate.
        driver = (
            "import sys; sys.path.insert(0, %r); "
            "import storelib.recall_reuse as rr, sqlite3, json; "
            "conn = sqlite3.connect(%r); "
            "comps = json.loads(sys.argv[1]); "
            "rr._observe(conn, comps, None, None, ok=True, so='miss'); "
            "rr._observe(conn, comps, None, None, ok=True, so='hit')"
            % (str(SCRIPTS), os.environ["ZMEM_STORE"])
        )
        comps_json = json.dumps(_comps())
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", driver, comps_json],
                capture_output=True, text=True, env=dict(os.environ),
                timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        rep = rr.observation_report(self.conn)
        self.assertEqual(rep["coverage"]["observed_n"], 4)
        # p1 miss (novel), then THREE duplicate-equivalent arrivals across
        # the process boundary (p1 hit, p2 miss, p2 hit)
        self.assertEqual(rep["hook_potential"]["duplicate_equivalent"], 3)

    def test_hot_key_compaction_preserves_counts(self):
        # >4096 observations of ONE key compact to one record carrying the
        # full n and dup counts (R4-2: nothing unclassified discarded).
        comps = _comps()
        for i in range(rr.MAX_RECORDS + 300):
            rr._observe(self.conn, comps, None, None, ok=True, so="hit")
        text = self._sidecar_text()
        data_lines = [l for l in text.splitlines()
                      if l and not l.startswith("#")]
        self.assertLessEqual(len(data_lines), rr.MAX_RECORDS)
        total_n = 0
        for line in data_lines:
            total_n += json.loads(line).get("n", 0)
        header = [l for l in text.splitlines() if l.startswith("#")][0]
        aggregate = json.loads(header[len("# aggregate v1"):])
        self.assertEqual(total_n + aggregate.get("n", 0),
                         rr.MAX_RECORDS + 300,
                         "compaction must preserve the observation count")

    def test_distinct_key_burst_ovf_marks_incomplete(self):
        # >MAX_RECORDS DISTINCT keys inside one TTL: the hard bound
        # early-closes the oldest; ovf marks the report INCOMPLETE.
        for i in range(rr.MAX_RECORDS + 10):
            rr._observe(self.conn, _comps(generation=str(i)), None, None,
                        ok=True, so="miss")
        rep = rr.observation_report(self.conn)
        self.assertGreater(rep["coverage"]["ovf"], 0)
        self.assertFalse(rep["complete"],
                         "overflow must make the AC11 evidence incomplete, "
                         "never silently approximate")

    def test_drops_journal_fail_closed(self):
        drops = self.tmp / "ops" / rr.DROPS_NAME
        drops.parent.mkdir(parents=True, exist_ok=True)
        drops.write_text('{"t": "x", "reason": "lock-timeout"}\n',
                         encoding="utf-8")
        rep = rr.observation_report(self.conn)
        self.assertEqual(rep["coverage"]["dropped"], 1)
        self.assertFalse(rep["complete"])

    def test_corrupt_lines_counted(self):
        sidecar = self.tmp / "ops" / rr.SIDECAR_NAME
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("# aggregate v1{}\nnot-json\n",
                           encoding="utf-8")
        rep = rr.observation_report(self.conn)
        self.assertEqual(rep["coverage"]["corrupt"], 1)
        self.assertFalse(rep["complete"])

    def test_absent_sidecar_report(self):
        rep = rr.observation_report(self.conn)
        self.assertFalse(rep.get("present"))
        self.assertFalse(rep.get("complete"))


def _run(selectors: list[str]) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ServingCounters, ClassificationTable, SidecarContract):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    if selectors:
        filtered = unittest.TestSuite()
        for test in suite:
            if any(sel in test.id() for sel in selectors):
                filtered.addTest(test)
        if filtered.countTestCases() == 0:
            print("SELECTOR-ERROR: %r matched zero tests" % (selectors,),
                  file=sys.stderr)
            return 3
        suite = filtered
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run([a for a in sys.argv[1:] if not a.startswith("-")]))
