"""Supplementary regressions for issue #376 (NOT part of the frozen contract).

Covers the plan-rev-5 behaviors the frozen file pins only indirectly:
polarity-nearest + subject-runner-up termination, the link tag-propagation
guard (and its lesson carve-out), consolidate dry-run == real on mixed
subjects (incl. the untagged-keeper bridge), the bounded equal-content walk,
and the sync detector-shim laws (per-call signature binding; execution
TypeErrors propagate; legacy stub results still vetoed; production ->
legacy-stub -> production transition).

CI shape: this repo runs every tests/test_*.py as plain `python <file>` —
the __main__ block below executes every test and exits 1 on failure.
Store isolation follows the #244 rules: env pinned BEFORE the guard import.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="zmem376supp-")
os.environ["ZMEM_STORE"] = str(Path(_TMP) / "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ.pop("ZMEM_EMBED_PROFILE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: F401,E402  (fixture-write guard, #244)

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "skills" / "memory" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from storelib import subject  # noqa: E402
from storelib import sync as sync_mod  # noqa: E402
from storelib import write as write_mod  # noqa: E402
from storelib.schema import connect, init_db, migrate  # noqa: E402

NS = "project:github.com/zaxbysauce/zmem"


def _fresh():
    # storelib freezes STORE_PATH at first import, so per-test env swaps do
    # NOT switch stores — every test in this module shares ONE process
    # store. Isolation is wipe-on-enter: clear the memory/vector tables at
    # context start (the tests here never cross-contaminate through side
    # tables they do not touch).
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        d = tempfile.mkdtemp(prefix="zmem376supp-store-")
        old = os.environ.get("ZMEM_STORE")
        os.environ["ZMEM_STORE"] = str(Path(d) / "store.sqlite")
        try:
            c = connect()
            init_db(c)
            migrate(c)
            c.execute("DELETE FROM memory")
            try:
                c.execute("DELETE FROM memory_vec")
            except Exception:
                pass
            c.commit()
            yield c
        finally:
            os.environ["ZMEM_STORE"] = old or ""
            c.close()
    return _ctx()


class SubjectPredicateTest(unittest.TestCase):
    def test_predicate_laws(self):
        self.assertFalse(subject.incompatible(
            "task:a", "fact", "task:a", "fact"))
        self.assertFalse(subject.incompatible(
            "task:a", "fact", "", "fact"))            # absent identity
        # MX-1 (feedback round): the lesson carve-out is SYMMETRIC — it
        # applies only when BOTH rows are lessons. A single lesson row next
        # to a fact row follows the ordinary identity rule, so a writer
        # cannot opt one row out of subject isolation by choosing its type.
        self.assertTrue(subject.incompatible(
            "task:a", "fact", "task:b", "lesson"))
        self.assertTrue(subject.incompatible(
            "task:a", "lesson", "task:b", "fact"))
        self.assertFalse(subject.incompatible(
            "task:a", "lesson", "task:b", "lesson"))   # both lessons carve-out
        self.assertTrue(subject.incompatible(
            "task:a", "fact", "task:b", "fact"))
        self.assertFalse(subject.incompatible(
            "task:   , task:", "fact", "task:b", "fact"))  # malformed ignored
        # MX-4: canonical grammar (split on [,;\s]+, case-folded keys).
        self.assertEqual(subject.identities("task:a;task:b"),
                         frozenset({"a", "b"}))
        self.assertEqual(subject.identities("Task:Repo#1"),
                         frozenset({"repo#1"}))
        self.assertEqual(subject.identities("x,task:owner/repo#1, task:y"),
                         frozenset({"owner/repo#1", "y"}))


class AddPathSuppTest(unittest.TestCase):
    def test_polarity_nearest_terminates_despite_compatible_runner_up(self):
        with _fresh() as conn:
            a = write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="Always commit before running mutation probes.",
                tags="task:issue41", capture_mode="manual")
            b = write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="Committing the tree before probes keeps restores safe.",
                tags="task:issue41", capture_mode="manual")
            # X is a one-word negation of A (nearest, same subject) while a
            # polarity-AGREEING compatible runner-up (B) sits farther away:
            # the polarity conflict on the SELECTED nearest must TERMINATE
            # dedup — X must not fold into B either.
            r = write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="Never commit before running mutation probes.",
                tags="task:issue41", capture_mode="manual")
            live = conn.execute(
                "SELECT COUNT(*) AS c FROM memory WHERE superseded_at IS NULL"
            ).fetchone()["c"]
            self.assertEqual(live, 3)
            self.assertNotEqual(str(r), str(a))
            self.assertNotEqual(str(r), str(b))

    def test_link_propagation_guard_and_lesson_carveout(self):
        # F-02: link FORMATION depends on the similarity lane — lexically the
        # fixture pairs sit below LINK_THRESHOLD, so the propagation outcomes
        # this test pins are only reachable with embeddings active. Assert
        # the link-dependent outcomes only in that mode; the lexical-mode
        # suppression guarantee is covered by consolidate's parity tests.
        import embeddings as _emb
        if not _emb.is_available():
            self.skipTest(
                "embeddings unavailable: lexical similarity cannot form the "
                "fixture links this test asserts on (see F-02)")
        with _fresh() as conn:
            write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="Queue workers must drain before shutdown drainer.",
                tags="task:issue41", capture_mode="manual", skip_dedup=True)
            nb = conn.execute(
                "SELECT id, tags FROM memory WHERE superseded_at IS NULL"
            ).fetchone()
            self.assertEqual(nb["tags"], "task:issue41")
            # A high-similarity add with a DISJOINT subject: link may form,
            # tags must NOT flow onto the neighbor.
            write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="Queue workers must drain before shutdown drain pool.",
                tags="task:issue42", capture_mode="manual", skip_dedup=True)
            after = conn.execute(
                "SELECT tags FROM memory WHERE id=?", (nb["id"],)
            ).fetchone()["tags"]
            self.assertEqual(after, "task:issue41")
            # Lesson-type neighbor still receives tags (carve-out).
            write_mod.add_memory(
                conn, namespace=NS, type_="lesson",
                content="Queue workers must drain before shutdown drain belt.",
                tags="task:issue43", capture_mode="manual", skip_dedup=True)
            write_mod.add_memory(
                conn, namespace=NS, type_="lesson",
                content="Queue workers must drain before shutdown drain loop.",
                tags="task:issue44", capture_mode="manual", skip_dedup=True)
            lesson_rows = conn.execute(
                "SELECT tags, type FROM memory WHERE superseded_at IS NULL "
                "AND type='lesson' ORDER BY rowid"
            ).fetchall()
            self.assertTrue(any(
                "task:issue44" in (r["tags"] or "")
                for r in lesson_rows if "task:issue43" in (r["tags"] or "")))


class SyncShimTest(unittest.TestCase):
    def test_shim_prefers_kwargs_and_handles_replacement(self):
        real = sync_mod._detect_duplicate
        calls = {"kwargs": 0, "plain": 0}

        def stub_kw(conn_, content, ns, dedup_cache=None, *, tags=None,
                    type_=None, emit=True):
            calls["kwargs"] += 1
            return None, 0.0, None

        def stub_legacy(conn_, content, ns, dedup_cache=None):
            calls["plain"] += 1
            return None, 0.0, None

        try:
            sync_mod._detect_duplicate = stub_kw
            out = sync_mod._detect_duplicate_subject(
                None, "x", NS, dedup_cache=None, tags="task:a", type_="fact")
            self.assertFalse(out[3])
            sync_mod._detect_duplicate = stub_legacy
            out = sync_mod._detect_duplicate_subject(
                None, "x", NS, dedup_cache=None, tags="task:a", type_="fact")
            self.assertTrue(out[3])
        finally:
            sync_mod._detect_duplicate = real
        self.assertEqual(calls, {"kwargs": 1, "plain": 1})

    def test_execution_typeerror_propagates(self):
        real = sync_mod._detect_duplicate

        def boom(conn_, content, ns, dedup_cache=None, *, tags=None,
                 type_=None, emit=True):
            raise TypeError("internal defect")

        sync_mod._detect_duplicate = boom
        try:
            with self.assertRaises(TypeError):
                sync_mod._detect_duplicate_subject(
                    None, "x", NS, tags="task:a", type_="fact")
        finally:
            sync_mod._detect_duplicate = real


class ExactBoundTest(unittest.TestCase):
    def test_equal_content_walk_is_bounded(self):
        with _fresh() as conn:
            for i in range(40):
                write_mod.add_memory(
                    conn, namespace=NS, type_="fact",
                    content="identical body for bound probing",
                    tags="task:issue41", capture_mode="manual",
                    skip_dedup=True)
            before = getattr(write_mod, "SUBJECT_COMPAT_EVALS", 0)
            write_mod.add_memory(
                conn, namespace=NS, type_="fact",
                content="identical body for bound probing",
                tags="task:issue42", capture_mode="manual")
            delta = getattr(write_mod, "SUBJECT_COMPAT_EVALS", 0) - before
            self.assertGreaterEqual(delta, 1)
            # F-13 widened the counter to every decision path: the measured
            # add evaluates the exact walk (<= CAP+1), the semantic window
            # (<= 5) and the link neighbors (<= 5). The envelope is the sum
            # plus slack — the INVARIANT that matters is namespace-size
            # independence, asserted below against a doubled namespace.
            envelope = write_mod.SUBJECT_MAX_EXACT_CANDIDATES + 1 + 16
            self.assertLessEqual(
                delta, envelope,
                f"compatibility evaluations must stay bounded "
                f"(delta={delta}, envelope={envelope})")


class ConsolidateParityTest(unittest.TestCase):
    """Reviewer R2-1: dry-run preview == real run, DISCRIMINATINGLY.

    Fixture (reviewer-specified): gather/priority order diverges from rowid
    order for an identified seed with a bridging dual-identity row —
      seed  task:alpha            (highest confidence -> the keeper)
      bridge task:alpha,task:beta (LOWEST rowid, lowest confidence)
      beta  task:beta             (higher confidence than the bridge)
    Under gather-order filtering (the round-1 defect) the real loop walks
    [beta, bridge] and its per-absorb revalidation vetoes beta (disjoint
    from the alpha keeper) while the dry preview claimed both — the parity
    comparison catches that divergence. Under the unified rowid admission
    order the bridge is admitted first (keeper grows to alpha,beta), beta
    then intersects, both loops walk the identical sequence, and preview
    absorb ids == real superseded ids.
    """

    def _drive(self, workdir, *args):
        import subprocess
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(Path(workdir) / "store.sqlite")
        env["ZMEM_DATA"] = str(workdir)
        env.pop("ZMEM_EMBED_PROFILE", None)
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(_SCRIPTS / "store.py"), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )
        return r

    def _add(self, workdir, content, tags, confidence):
        a = ["add", "--namespace", NS, "--type", "fact", "--content", content,
             "--capture-mode", "manual", "--signal", "test",
             "--confidence", str(confidence), "--no-dedup"]
        if tags:
            a += ["--tags", tags]
        r = self._drive(workdir, *a)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def _seed_and_run(self, d):
        """Seed the divergent-order fixture ONCE; dry-run first (it never
        mutates), then the REAL run on the SAME store — same row ids on both
        sides of the parity comparison by construction."""
        import re as _re
        import sqlite3

        # rowid order: bridge FIRST, then seed, then beta; one-word-variant
        # texts so the cluster actually forms above the consolidate
        # threshold (the round-1 probe construction).
        self._add(d, "Queue workers must drain completely before shutdown alpha beta.",
                  "task:alpha,task:beta", 0.30)
        self._add(d, "Queue workers must drain before shutdown alpha.",
                  "task:alpha", 0.90)
        self._add(d, "Queue workers must drain before shutdown alpha too.",
                  "task:beta", 0.60)
        conn = sqlite3.connect(str(Path(d) / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        rows = {
            r0["tags"]: r0["id"] for r0 in conn.execute(
                "SELECT id, tags FROM memory WHERE superseded_at IS NULL"
            ).fetchall()}
        conn.close()

        dry = self._drive(d, "consolidate", "--force", "--namespace", NS,
                          "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        dry_ids = {m for m in _re.findall(r"absorb \[([0-9a-f]{8})\]",
                                          dry.stdout)}

        real = self._drive(d, "consolidate", "--force", "--namespace", NS,
                           "--json")
        self.assertEqual(real.returncode, 0, real.stderr + real.stdout)
        conn = sqlite3.connect(str(Path(d) / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        superseded = {
            r0["id"] for r0 in conn.execute(
                "SELECT id FROM memory WHERE superseded_at IS NOT NULL"
            ).fetchall()}
        live = {
            r0["tags"]: r0["id"] for r0 in conn.execute(
                "SELECT id, tags FROM memory WHERE superseded_at IS NULL"
            ).fetchall()}
        conn.close()
        # Final-critic round 1: the report's merged count must equal the
        # rows actually superseded (never the admitted-neighbor count).
        import json as _json
        report = None
        try:
            report = _json.loads(real.stdout.strip().splitlines()[-1])
        except Exception:
            report = None
        if not isinstance(report, dict) or "merged" not in report:
            for line in real.stdout.strip().splitlines():
                try:
                    cand = _json.loads(line)
                except Exception:
                    continue
                if isinstance(cand, dict) and "merged" in cand:
                    report = cand
                    break
        self.assertIsNotNone(report, "consolidate --json report not found")
        self.assertEqual(
            report["merged"], len(superseded),
            f"report merged={report['merged']} but superseded rows="
            f"{len(superseded)} — the report claims merges that did not "
            f"happen (report={report})")
        return dry, dry_ids, real, superseded, live, rows

    def test_parity_preview_absorbs_equal_real_superseded(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem376par-") as d:
            dry, dry_ids, real, superseded, live, rows = self._seed_and_run(d)
            full_by_prefix = {rid[:8]: rid for rid in rows.values()}
            claimed = {full_by_prefix[p] for p in dry_ids if p in full_by_prefix}
            # Non-vacuous precondition: the preview must have claimed at
            # least one absorb (a fixture drift that stops clustering would
            # otherwise make this test pass vacuously).
            self.assertTrue(claimed,
                            "fixture drift: the dry preview claimed no "
                            "absorbs (no cluster formed above threshold)")
            # THE parity assertion: every preview-claimed absorb is actually
            # superseded by the real run, and vice versa (on the twin store).
            self.assertEqual(
                claimed, superseded,
                f"dry preview claimed {claimed} but the real run superseded "
                f"{superseded} — preview and real have diverged "
                f"(dry out: {dry.stdout[-400:]}; real out: {real.stdout[-400:]})")
            # The alpha keeper survives live.
            self.assertIn("task:alpha,task:beta", live,
                          "the grown keeper's unioned identity is expected "
                          f"under the bridge admission (live={live})")

    def test_untagged_keeper_bridge_refuses_disjoint_subject(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory(prefix="zmem376br-") as d:
            self._add(d, "Queue workers must drain before shutdown.", "", 0.90)
            self._add(d, "Queue workers must drain before shutdown one.",
                      "task:issue41", 0.60)
            self._add(d, "Queue workers must drain before shutdown two.",
                      "task:issue42", 0.30)
            real = self._drive(d, "consolidate", "--force", "--namespace", NS)
            self.assertEqual(real.returncode, 0, real.stderr + real.stdout)
            conn = sqlite3.connect(str(Path(d) / "store.sqlite"))
            conn.row_factory = sqlite3.Row
            live_rows = conn.execute(
                "SELECT id, tags, content FROM memory "
                "WHERE superseded_at IS NULL ORDER BY rowid").fetchall()
            superseded_n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory "
                "WHERE superseded_at IS NOT NULL").fetchone()["c"]
            conn.close()
            live_tags = sorted(r0["tags"] or "" for r0 in live_rows)
            self.assertIn("task:issue42", live_tags,
                          "the refused disjoint subject must stay live "
                          f"(live={live_tags})")
            # Non-vacuous shape: the untagged keeper ABSORBED the first
            # subject (it is superseded; the keeper now carries its tag and
            # its content) — otherwise the refusal assertion is vacuous.
            self.assertEqual(superseded_n, 1,
                             "exactly one absorb expected: the first "
                             f"subject (superseded={superseded_n})")
            keeper = [r0 for r0 in live_rows
                      if r0["tags"] == "task:issue41"]
            self.assertEqual(len(keeper), 1,
                             "the grown keeper carries task:issue41 "
                             f"(live={live_tags})")
            self.assertIn("shutdown one", keeper[0]["content"],
                          "the keeper's content grew with the absorbed row")


if __name__ == "__main__":
    unittest.main(verbosity=2)
