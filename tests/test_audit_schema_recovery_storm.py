"""Issue #506 audit, AC2 storm companion: 32 lexical CLI writers against one
isolated Windows store.

AC2 -- WHEN 32 lexical CLI writers start together against one isolated Windows store, THE SYSTEM
SHALL recover all 32 distinct writes without a spurious restore refusal.  RED at base, GREEN
after the fix.

The children are held at a REAL child-side start barrier: every child is a
tiny launcher process written to the case's scratch dir that (1) imports the
public store.py CLI shim, (2) signals that it is running by writing its own
ready file, (3) blocks until the parent -- after seeing all 32 ready files --
creates the single go file, and only then (4) calls the CLI entry IN-PROCESS
(``store.main()`` with a rewritten sys.argv) for its own distinct row.
Barrier choice (stated per the brief): the go-file launcher that "calls the
CLI entry in-process" variant is used; the os.execv variant was evaluated and
rejected because on Windows the parent cannot collect the execv'd writer's
real exit code (the launcher exits as soon as it has spawned the replacement,
so refusals would be invisible).

The recovery readback reads the store with read-only SQLite
(``file:...?mode=ro``, ``SELECT id, content ... WHERE namespace = ?``) and
requires every row's FULL content to equal what its child was told to add.
``store.py list --json`` is deliberately NOT the readback: its rows carry a
``preview`` and no ``content`` key, so a full-content equality check built on
that JSON shape would fail at its own readback even when every write
succeeded.  The lone-writer CONTROL runs the same readback on its own single
row and must be green on any healthy tree -- that is what proves the rounds'
recovery assertions are never the failure point.

Lexical writers only: ZMEM_MODELS_DIR points at an empty scratch dir and
ZMEM_MODEL_AUTODOWNLOAD=0, so no vector model is loaded.  The PUBLIC
maintenance knobs are the same values the frozen check used
(ZMEM_MAINTENANCE_WAIT_SECONDS=0.05, ZMEM_MAINTENANCE_POLL_SECONDS=0.005,
imported from test_audit_schema_recovery so the files cannot drift): the
tightened window is ample for the single-writer control, which is what makes
the storm's refusals spurious (no restore is running -- writer-vs-writer
contention on the exclusive maintenance-lock probe).

Every child's stdout AND stderr are captured into its own files under the
case's scratch dir; on failure the message lists, for EVERY failing child,
its exit code and the last 300 characters of its stderr so the failure can be
classified (a maintenance refusal, a schema/SQLite error, or something else).
The wait is bounded; on timeout every still-running child is killed and named.
The storm runs THREE times, each round against a fresh namespace and fresh
store state, and each round must pass on its own.

Hermetic: every child pins a fresh throwaway store (ZMEM_STORE, ZMEM_DATA,
ZMEM_BACKUP_DIR, models dir empty, autodownload off, UTF-8); the operator
store is never opened (the shared base module pins the guard before anything
imports storelib here).

Run: python tests/test_audit_schema_recovery_storm.py   (repo convention, no
pytest; the published pytest node id
tests/test_audit_schema_recovery.py::test_concurrent_writers selects this
TestCase through the wrapper kept in that file).
"""

from __future__ import annotations
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_audit_schema_recovery as _recovery  # noqa: E402  (env pin + guard + shared helpers)
from test_audit_schema_recovery import (  # noqa: E402
    AuditSchemaRecoveryBase, NS, PY, REPO_ROOT, SCRIPTS_DIR,
    _TIGHT_POLL, _TIGHT_WAIT,
)
import _store_guard  # noqa: E402,F401  (#244 wiring census: this companion
#                      names store.py/storelib in its child-writer source;
#                      the shared base module imported above already pinned
#                      the env and the guard — this literal import satisfies
#                      the census's per-file wiring requirement)

_N_CHILDREN = 32
_N_ROUNDS = 3
# The barrier build (32 concurrent storelib imports) is bounded generously --
# small CI runners serialize the imports.
_BARRIER_WAIT_S = 120.0
_STORM_WAIT_S = 180.0

_ROUND_CONTENT = "concurrent writer row %02d uniquetoken%02d"

# Written to each round's scratch dir.  The child is the writer process: it
# loads the public CLI first, signals ready, blocks on the go file, and only
# then dispatches the actual public add command for its own distinct row.
_STORM_WRITER_SOURCE = """\
import os, sys, time

scripts_dir, ready_path, go_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = [os.path.join(scripts_dir, "store.py")] + sys.argv[4:]
sys.path.insert(0, scripts_dir)
import store  # noqa: E402  (public store.py CLI shim: this writer is fully loaded)

with open(ready_path, "w", encoding="utf-8") as fh:
    fh.write("ready")
deadline = time.time() + 120.0
while not os.path.exists(go_path):
    if time.time() > deadline:
        raise SystemExit(3)  # the barrier was never released
    time.sleep(0.002)
raise SystemExit(store.main() or 0)
"""


class ConcurrentWritersStormTests(AuditSchemaRecoveryBase):
    """AC2 (#506): 32 writers released together at a real child-side start
    barrier must all be acknowledged and their writes recovered, without a
    spurious restore refusal.  RED at base, GREEN after the fix."""

    def test_concurrent_writers(self):
        """Lone-writer control (which also proves the full-content readback)
        under the tightened public knobs, then the 32-writer barrier storm
        three times, each round on a fresh namespace and fresh store.  RED at
        base, GREEN after the fix."""
        self.maxDiff = None  # every failing child must be listed in full
        tight = {"ZMEM_MAINTENANCE_WAIT_SECONDS": _TIGHT_WAIT,
                 "ZMEM_MAINTENANCE_POLL_SECONDS": _TIGHT_POLL}
        lone = self._store_dir("lone-control")
        (lone / "no-models").mkdir(exist_ok=True)
        r = self._cli(lone, "add", "--namespace", NS + "-single", "--type",
                      "fact", "--content", "lone writer row", "--signal",
                      "test", "--json", extra_env=tight)
        self.assertEqual(r.returncode, 0,
            "AC2-506: control - the tightened maintenance window must be ample for a single"
            " uncontended writer (the storm refusals below are writer-vs-writer probe contention"
            " with no restore running, not config): exit %s stderr %r"
            % (r.returncode, r.stderr[-200:]))
        # Readback control: the SAME read-only full-content readback the
        # rounds use must return the lone writer's one row, whole.  Green on
        # any healthy tree by construction; if this is red the readback is
        # wrong, not the writer (fix the readback, not the control).
        lone_rows = self._readback(lone, NS + "-single")
        self.assertEqual(
            (len(lone_rows), [content for _rid, content in lone_rows]),
            (1, ["lone writer row"]),
            "AC2-506: control - the read-only full-content readback must return the lone"
            " writer's single row whole (got %r); if this control is red the readback is"
            " wrong, not the writer -- fix the readback, not the control"
            % (lone_rows,))
        for rnd in range(1, _N_ROUNDS + 1):
            with self.subTest(round=rnd):
                self._storm_round(rnd)

    def _readback(self, store_dir, namespace):
        """The storm's recovery readback: read-only SQLite on the store file,
        selecting (id, content) for the namespace.  `list --json` rows carry
        a `preview` and no `content` key, so full-content equality cannot be
        built on the CLI's JSON shape; this read touches nothing (mode=ro)."""
        return self._ro_sql(store_dir / "store.sqlite",
                            "SELECT id, content FROM memory"
                            " WHERE namespace = ?", (namespace,))

    def _storm_round(self, rnd):
        ns = "%s-storm%d" % (NS, rnd)
        d = self._store_dir("storm-round-%d" % rnd)
        (d / "no-models").mkdir(exist_ok=True)  # lexical writers: empty models dir
        writer = d / "_storm_writer.py"
        writer.write_text(_STORM_WRITER_SOURCE, encoding="utf-8")
        tight = {"ZMEM_MAINTENANCE_WAIT_SECONDS": _TIGHT_WAIT,
                 "ZMEM_MAINTENANCE_POLL_SECONDS": _TIGHT_POLL}
        env = self._child_env(d, tight)
        barrier = d / "barrier"
        barrier.mkdir()
        go = barrier / "go"
        procs = []
        for i in range(_N_CHILDREN):
            out = open(d / ("writer-%02d.out" % i), "wb")
            err = open(d / ("writer-%02d.err" % i), "wb")
            try:
                procs.append((i, subprocess.Popen(
                    [PY, "-X", "utf8", str(writer), str(SCRIPTS_DIR),
                     str(barrier / ("ready-%02d" % i)), str(go),
                     "add", "--namespace", ns, "--type", "fact",
                     "--content", _ROUND_CONTENT % (i, i),
                     "--signal", "test", "--json"],
                    stdout=out, stderr=err, env=env, cwd=str(REPO_ROOT))))
            finally:
                out.close()
                err.close()
        # -- build the barrier: wait until every child signalled ready ------
        deadline = time.time() + _BARRIER_WAIT_S
        unready = self._unready(barrier)
        while unready and time.time() < deadline:
            time.sleep(0.05)
            unready = self._unready(barrier)
        if unready:
            tails = {}
            for i in unready:
                p = d / ("writer-%02d.err" % i)
                if p.exists():
                    tails[i] = p.read_text(encoding="utf-8",
                                           errors="replace")[-300:]
            for _i, p in procs:
                if p.poll() is None:
                    p.kill()
            self.fail(
                "AC2-506: scenario setup: [round %d/%d] the start barrier"
                " could not be built; children that never signalled ready:"
                " %r (stderr tails: %r)" % (rnd, _N_ROUNDS, unready, tails))
        # -- release every child together ----------------------------------
        go.write_text("go", encoding="utf-8")
        finish_by = time.time() + _STORM_WAIT_S
        for i, p in procs:
            try:
                p.wait(timeout=max(0.0, finish_by - time.time()))
            except subprocess.TimeoutExpired:
                running = [(j, q.pid) for j, q in procs if q.poll() is None]
                for _j, q in procs:
                    if q.poll() is None:
                        q.kill()
                self.fail(
                    "AC2-506: [round %d/%d] the storm exceeded its bounded"
                    " wait; children still running (index, pid), now killed:"
                    " %r" % (rnd, _N_ROUNDS, running))
        # -- every child must have been acknowledged ------------------------
        failures = []
        for i, p in procs:
            if p.returncode != 0:
                tail = (d / ("writer-%02d.err" % i)).read_text(
                    encoding="utf-8", errors="replace")[-300:]
                failures.append((i, p.returncode, tail))
        self.assertTrue(not failures,
            "AC2-506: [round %d/%d] all 32 writers released together at the"
            " start barrier must be acknowledged without a spurious restore"
            " refusal (no restore is running); every failing child as (index,"
            " exit code, last 300 chars of stderr) -- the tails classify the"
            " defect (a maintenance refusal, a schema/SQLite error, or"
            " something else): %r" % (rnd, _N_ROUNDS, failures))
        # -- every distinct write must be recovered, whole ------------------
        # Read the rows back through read-only SQLite (the same readback the
        # lone-writer control proved): `list --json` rows carry a `preview`
        # and no `content` key, so full-content equality cannot be built on
        # the CLI's JSON shape.
        rows = self._readback(d, ns)
        ids = [rid for rid, _content in rows]
        contents = [content for _rid, content in rows]
        expected = sorted(_ROUND_CONTENT % (i, i) for i in range(_N_CHILDREN))
        self.assertEqual((len(rows), len(set(ids))), (_N_CHILDREN, _N_CHILDREN),
            "AC2-506: [round %d/%d] all 32 distinct writes must be recovered by the"
            " read-only full-content readback (found %d rows, %d distinct ids)"
            % (rnd, _N_ROUNDS, len(rows), len(set(ids))))
        self.assertEqual(sorted(contents), expected,
            "AC2-506: [round %d/%d] every recovered row's full content must equal"
            " what its child was told to add; contents never added by any child:"
            " %r; child writes absent from the store: %r"
            % (rnd, _N_ROUNDS,
               sorted(set(contents) - set(expected))[:8],
               sorted(set(expected) - set(contents))[:8]))

    @staticmethod
    def _unready(barrier):
        return [i for i in range(_N_CHILDREN)
                if not (barrier / ("ready-%02d" % i)).exists()]


if __name__ == "__main__":
    unittest.main(verbosity=2)
