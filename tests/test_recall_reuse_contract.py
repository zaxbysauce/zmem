#!/usr/bin/env python
"""Issue #474 AC1-AC3 (+AC5 floor): correctness-preserving recall reuse.

RED at base (file absent — frozen C6/C7/C8 wrappers print NEW_SURFACE_ABSENT);
GREEN after the fix. The frozen selectors `coalesce` and `failure_not_cached`
select their families by name.

Contract under test (storelib.recall_reuse):
- identical reuse key within TTL reuses the identical successful result;
  every key-component change (store mutation by another writer,
  options/profile change, explicit refresh, session/fence boundary, expired
  TTL, store incarnation) forces a recompute
- identical in-flight requests coalesce into one computation
- failures are never cached as success; the next eligible call retries
- telemetry/capture/stdout replay: output and side effects identical with
  and without reuse (A/B pins)
- a hit-side effect failure evicts the entry and the next call recomputes

Run:  python tests/test_recall_reuse_contract.py [substring ...]
      (zero matched selectors = failure, never a vacuous pass)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import storelib.recall as recall_mod  # noqa: E402
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
import storelib.write as write_mod  # noqa: E402
from storelib.recall import recall_memory  # noqa: E402

ENV_NAMES = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
             "ZMEM_EMBED_PROFILE", rr.SERVE_ENV, rr.TTL_ENV,
             rr.REFRESH_ENV, rr.OBSERVE_ENV)


class _ReuseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-reuse-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {n: os.environ.get(n) for n in ENV_NAMES}
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(self.tmp)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        os.environ.pop(rr.SERVE_ENV, None)
        os.environ.pop(rr.OBSERVE_ENV, None)
        os.environ.pop(rr.REFRESH_ENV, None)
        os.environ.pop(rr.TTL_ENV, None)
        self.addCleanup(self._restore)
        _reset_reuse_state()
        self.addCleanup(_reset_reuse_state)
        self.conn = sqlite3.connect(os.environ["ZMEM_STORE"])
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        schema.init_db(self.conn)
        schema.migrate(self.conn)
        write_mod.add_memory(
            self.conn, namespace="project:reuse", type_="fact",
            content="reuse contract alpha remembers the beacon",
            source_ref="test", tags="t", confidence=0.9, signal="test")

    def _restore(self):
        for n, v in self._saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _recall(self, query="beacon", **kw):
        kw.setdefault("namespace", "project:reuse")
        kw.setdefault("limit", 3)
        kw.setdefault("no_bump", True)
        kw.setdefault("no_telemetry", True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows = recall_memory(self.conn, query=query, **kw)
        return rows, buf.getvalue()

    def _serving(self, on=True):
        if on:
            os.environ[rr.SERVE_ENV] = "1"
        else:
            os.environ.pop(rr.SERVE_ENV, None)


class HitPath(_ReuseBase):
    def test_hit_reuses_identical_result_and_stdout(self):
        self._serving()
        rows1, out1 = self._recall()
        rows2, out2 = self._recall()
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(rows1, rows2)
        self.assertEqual(out1, out2,
                         "stdout bytes must be identical on a hit")

    def test_default_off_never_serves(self):
        rows1, out1 = self._recall()
        rows2, out2 = self._recall()
        stats = rr.reuse_stats()
        self.assertEqual(stats.get("hits", 0), 0)
        self.assertEqual(stats.get("entries", 0), 0)
        self.assertEqual(out1, out2)  # determinism sanity, but no cache

    def test_options_change_misses(self):
        self._serving()
        self._recall(limit=3)
        rows, _ = self._recall(limit=5)
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats.get("hits", 0), 0)

    def test_query_change_misses(self):
        self._serving()
        self._recall(query="beacon")
        self._recall(query="alpha")
        self.assertEqual(rr.reuse_stats()["misses"], 2)

    def test_fence_change_misses_session_boundary(self):
        # two distinct sessions with identical everything else must NOT
        # alias: the selector's fence is part of the key.
        self._serving()
        self._recall(_fence_id="session-a/turn-1")
        self._recall(_fence_id="session-b/turn-1")
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats.get("hits", 0), 0)

    def test_ttl_expiry_invalidates(self):
        os.environ[rr.TTL_ENV] = "0.05"
        self._serving()
        self._recall()
        time.sleep(0.08)
        self._recall()
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats["invalidations"].get("ttl", 0), 1)
        self.assertEqual(stats.get("hits", 0), 0)

    def test_explicit_refresh_forces_recompute(self):
        self._serving()
        self._recall()
        os.environ[rr.REFRESH_ENV] = "1"
        try:
            self._recall()
        finally:
            os.environ.pop(rr.REFRESH_ENV, None)
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats.get("refresh", 0), 1)
        self.assertEqual(stats.get("hits", 0), 0)

    def test_store_mutation_by_another_writer_visible_promptly(self):
        self._serving()
        rows1, _ = self._recall()
        other = sqlite3.connect(os.environ["ZMEM_STORE"])
        other.row_factory = sqlite3.Row
        self.addCleanup(other.close)
        write_mod.add_memory(
            other, namespace="project:reuse", type_="fact",
            content="fresh beacon memory added by another writer beta",
            source_ref="test", tags="t", confidence=0.9, signal="test")
        rows2, _ = self._recall()
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2,
                         "another writer's add must invalidate the key")
        contents = [r.get("content") if isinstance(r, dict) else r["content"]
                    for r in rows2]
        joined = " ".join(str(c) for c in contents)
        self.assertIn("another writer", joined,
                      "the fresh memory must be visible promptly")

    def test_feedback_mutation_invalidates(self):
        self._serving()
        self._recall()
        mid = self.conn.execute(
            "SELECT id FROM memory LIMIT 1").fetchone()[0]
        write_mod.feedback_memory(self.conn, memory_id=mid,
                                     verdict="applied")
        self._recall()
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 2,
                         "ranking-feeding feedback columns must invalidate")

    def test_incarnation_change_invalidates(self):
        self._serving()
        self._recall()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES ('store_incarnation', 'x')"
            " ON CONFLICT(key) DO UPDATE SET value = 'x'")
        self._recall()
        self.assertEqual(rr.reuse_stats()["misses"], 2)

    def test_recall_does_not_invalidate_telemetry_pin(self):
        # telemetry is not a selection mutation: bump-mode recalls must not
        # invalidate a subsequent identical recall.
        self._serving()
        self._recall(no_bump=True, no_telemetry=False)
        self._recall(no_bump=True, no_telemetry=False)
        stats = rr.reuse_stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 1)


class EffectReplay(_ReuseBase):
    def test_capture_population_on_hit(self):
        # The capture dict is populated ONLY on the injection lane
        # (recall.py's plain path never fills _capture), so this test must
        # drive for_injection=True — a plain-path drive compares two empty
        # dicts and proves nothing (implementation-review finding 1).
        self._serving()
        cap1 = {}
        self._recall(for_injection=True, _capture=cap1,
                     no_bump=True)
        cap2 = {}
        self._recall(for_injection=True, _capture=cap2,
                     no_bump=True)
        self.assertTrue(cap1,
                        "the injection-lane capture must be non-empty "
                        "(else this pin is vacuous)")
        self.assertEqual(cap1, cap2,
                         "the caller's _capture dict must be populated "
                         "identically on a hit")

    def test_telemetry_reapplied_on_hit_with_recorded_ids(self):
        self._serving()
        self._recall(no_bump=True, no_telemetry=False)
        before = self.conn.execute(
            "SELECT COALESCE(SUM(surfaced_count), 0) FROM memory"
        ).fetchone()[0]
        self._recall(no_bump=True, no_telemetry=False)
        after = self.conn.execute(
            "SELECT COALESCE(SUM(surfaced_count), 0) FROM memory"
        ).fetchone()[0]
        self.assertGreater(after, before,
                           "the hit must re-apply telemetry exactly like "
                           "the first call (surfaced_count advances)")

    def test_hit_side_effect_failure_evicts_entry(self):
        self._serving()
        self._recall(no_telemetry=False)
        real_bump = recall_mod._bump_telemetry

        def _boom(conn, ids, **kw):
            raise RuntimeError("hit-side telemetry failure")

        recall_mod._bump_telemetry = _boom
        try:
            # a no_telemetry=False HIT would replay telemetry -> boom
            with self.assertRaises(RuntimeError):
                self._recall(no_telemetry=False)
        finally:
            recall_mod._bump_telemetry = real_bump
        stats = rr.reuse_stats()
        self.assertEqual(stats.get("hit_effect_failures", 0), 1)
        self.assertEqual(stats.get("entries", 0), 0,
                         "the entry must be evicted after the effect "
                         "failure")
        # next eligible call recomputes rather than replaying the failure
        rows, _ = self._recall()
        self.assertTrue(rows)


class CoalesceTests(_ReuseBase):
    """AC2 (`coalesce` selector): two identical in-flight recalls coalesce
    into one computation."""

    def test_coalesce_identical_inflight(self):
        self._serving()
        calls = {"n": 0}
        real_impl = recall_mod._recall_memory_uncached

        def _counting_impl(conn, **kw):
            calls["n"] += 1
            time.sleep(0.15)
            return real_impl(conn, **kw)

        recall_mod._recall_memory_uncached = _counting_impl
        try:
            results = {}
            outs = {}

            def worker(i):
                tconn = sqlite3.connect(os.environ["ZMEM_STORE"])
                tconn.row_factory = sqlite3.Row
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf):
                        results[i] = recall_memory(
                            tconn, query="beacon",
                            namespace="project:reuse",
                            limit=3, no_bump=True, no_telemetry=True)
                    outs[i] = buf.getvalue()
                finally:
                    tconn.close()

            t1 = threading.Thread(target=worker, args=(0,))
            t2 = threading.Thread(target=worker, args=(1,))
            t1.start()
            time.sleep(0.03)
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            recall_mod._recall_memory_uncached = real_impl
        self.assertEqual(calls["n"], 1,
                         "identical in-flight recalls must coalesce into "
                         "ONE computation")
        self.assertEqual(results[0], results[1])
        # NOTE: cross-thread stdout is NOT asserted here — redirect_stdout
        # is process-global, so two concurrently capturing threads swap the
        # same stream out from under each other (a test-harness artifact,
        # not product behavior; the winner's tee may legitimately capture
        # empty when the loser's redirect lands mid-window). Stdout replay
        # is pinned byte-exactly by the single-threaded hit-path test.
        stats = rr.reuse_stats()
        self.assertEqual(stats.get("coalesced", 0), 1)

    def test_coalesce_losers_get_winners_failure(self):
        self._serving()
        calls = {"n": 0}
        real_impl = recall_mod._recall_memory_uncached

        def _failing_impl(conn, **kw):
            calls["n"] += 1
            time.sleep(0.15)
            raise ValueError("compute failed")

        recall_mod._recall_memory_uncached = _failing_impl
        errors = {}

        def worker(i):
            tconn = sqlite3.connect(os.environ["ZMEM_STORE"])
            tconn.row_factory = sqlite3.Row
            try:
                recall_memory(tconn, query="beacon",
                              namespace="project:reuse", limit=3,
                              no_bump=True, no_telemetry=True)
                errors[i] = None
            except BaseException as exc:  # noqa: BLE001
                errors[i] = exc
            finally:
                tconn.close()

        try:
            t1 = threading.Thread(target=worker, args=(0,))
            t2 = threading.Thread(target=worker, args=(1,))
            t1.start()
            time.sleep(0.03)
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        finally:
            recall_mod._recall_memory_uncached = real_impl
        self.assertIsNotNone(errors[0])
        self.assertIsNotNone(errors[1],
                             "the coalesced loser must observe the "
                             "winner's failure, never a synthesized "
                             "success")
        self.assertIsInstance(errors[1], ValueError)


class FailureNotCached(_ReuseBase):
    """AC3 (`failure_not_cached` selector)."""

    def test_failure_not_cached_retries(self):
        self._serving()
        calls = {"n": 0}
        real_impl = recall_mod._recall_memory_uncached

        def _flaky(conn, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient recall failure")
            return real_impl(conn, **kw)

        recall_mod._recall_memory_uncached = _flaky
        try:
            with self.assertRaises(RuntimeError):
                self._recall()
            stats = rr.reuse_stats()
            self.assertEqual(stats.get("failures_not_cached", 0), 1)
            self.assertEqual(stats.get("entries", 0), 0,
                             "no success-shaped entry may survive a "
                             "failure")
            rows, _ = self._recall()
            self.assertTrue(rows)
            self.assertEqual(calls["n"], 2,
                             "the next eligible recall must retry the "
                             "computation")
        finally:
            recall_mod._recall_memory_uncached = real_impl

    def test_failed_then_identical_not_counted_as_duplicate(self):
        # the observation classifier must separate retries from duplicates
        self._serving()
        os.environ[rr.OBSERVE_ENV] = "1"
        calls = {"n": 0}
        real_impl = recall_mod._recall_memory_uncached

        def _flaky(conn, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_impl(conn, **kw)

        recall_mod._recall_memory_uncached = _flaky
        try:
            with self.assertRaises(RuntimeError):
                self._recall()
            self._recall()
        finally:
            recall_mod._recall_memory_uncached = real_impl
        report = rr.observation_report(self.conn)
        self.assertEqual(report.get("retries_after_failure"), 1)
        self.assertEqual(report.get("hook_potential", {})
                         .get("duplicate_equivalent"), 0)


class ABPins(_ReuseBase):
    """Reuse-on vs reuse-off envelope equality (AC5)."""

    def test_ab_identical_output_off_vs_on(self):
        rows_off, out_off = self._recall()
        _reset_reuse_state()
        self._serving()
        rows_on, out_on = self._recall()
        self.assertEqual(rows_off, rows_on)
        self.assertEqual(out_off, out_on)


def _run(selectors: list[str]) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (HitPath, EffectReplay, CoalesceTests, FailureNotCached,
                ABPins):
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
