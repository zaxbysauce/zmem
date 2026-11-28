"""F-011: the two contention guards, mutation-probed IN PLACE.

F-011 (swarm-pr-review round 2) found that ONE test could not cover
BOTH contention guards, because they are DIFFERENT code paths: the
run-start write is the FIRST write, so a blocker taken before the call
makes the RUN-START handler return before the cluster loop is ever
reached. The fix split the pin into two legs -- one per guard -- and
the ledger records that both legs are mutation-probed.

This module IS that mutation probe. It reverts ONE fix in place and
requires the matching leg to FAIL, for the intended reason:

  A. the cluster-loop ROLLBACK becomes unconditional. No transaction is
     open when the loop's BEGIN IMMEDIATE fails, so the bare ROLLBACK
     raises "cannot rollback - no transaction is active" OUT of the
     handler; the ruled retry line never appears and the leg fails.

  B. the run-start rollback guard never fires. pysqlite's implicit BEGIN
     succeeds without taking a lock and SQLite does not end the
     transaction on SQLITE_BUSY, so the connection comes back still
     inside a transaction; the leg fails on `conn.in_transaction`.

It then restores the target byte-for-byte and verifies that.

This module lives under `tests/support/` because the swarm probe runner
executes its target inside the tree it extracts from the revision -- a
script outside the tree cannot be run by the runner. The runner's
isolation (a `git archive` copy, never the reviewed working tree) is
what makes the in-place mutation safe.

Run: python tests/support/f011_probes.py   (from the repository root)

The working root is the CURRENT DIRECTORY, so the same module runs
against the repository root or against a runner-extracted copy.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()  # the runner sets cwd to the EXTRACTED tree
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-f011-probes-"))

CONSOLIDATE = ROOT / "skills" / "memory" / "scripts" / "storelib" / "consolidate.py"
TARGETS = [CONSOLIDATE]
SNAPS = {p: SCRATCH / (p.name + ".snap") for p in TARGETS}

SUPP = "test_audit_x_store_recovery_supp"
CLASS = "ContentionRuledRetryTest"
RUN_START_LEG = "%s.%s.test_run_start_contention_leaves_no_open_transaction" % (
    SUPP, CLASS)
CLUSTER_LEG = "%s.%s.test_cluster_loop_contention_prints_ruled_retry" % (
    SUPP, CLASS)

# MUTANT A: the cluster-loop ROLLBACK becomes unconditional.
CLUSTER_MUTANT = (
    b'            if conn.in_transaction:\n'
    b'                conn.execute("ROLLBACK")\n',
    b'            if True:  # MUTANT: the rollback is unconditional\n'
    b'                conn.execute("ROLLBACK")\n')

# MUTANT B: the run-start rollback guard never fires.
RUN_START_MUTANT = (
    b'            if conn.in_transaction:\n'
    b'                try:\n'
    b'                    conn.rollback()\n',
    b'            if False:  # MUTANT: the guard never fires\n'
    b'                try:\n'
    b'                    conn.rollback()\n')

PROBES = [
    ("F-011 the cluster-loop ROLLBACK is unconditional",
     CLUSTER_LEG,
     [CLUSTER_MUTANT],
     "cannot rollback - no transaction is active"),

    ("F-011 the run-start rollback guard never fires",
     RUN_START_LEG,
     [RUN_START_MUTANT],
     "left the connection inside an open transaction"),
]


def snapshot() -> None:
    for p in TARGETS:
        SNAPS[p].write_bytes(p.read_bytes())


def restore() -> None:
    for p in TARGETS:
        p.write_bytes(SNAPS[p].read_bytes())


def run_leg(leg: str) -> tuple[int, str]:
    """Run one leg of the supplementary suite, in a child process."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(ROOT / "tests"), str(ROOT / "skills" / "memory" / "scripts")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "unittest", leg],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=600)
    return proc.returncode, (proc.stdout + proc.stderr)[-1200:]


def main() -> int:
    snapshot()
    failures = []
    try:
        for label, leg, _mutant, _expect in PROBES:
            rc, tail = run_leg(leg)
            print("%-18s %s" % ("FIXED-PASSES" if rc == 0 else "FIXED-FAILS",
                                label))
            if rc != 0:
                failures.append(label + " (the fixed source failed the leg)")
                print("                 tail: %s" % tail[-400:])

        for label, leg, mutants, expect in PROBES:
            data = CONSOLIDATE.read_bytes()
            counts = []
            for old, new in mutants:
                n = data.count(old)
                if n == 0:
                    raise SystemExit("%s: an anchor was not found" % label)
                if n != 1:
                    raise SystemExit("%s: the anchor matched %d lines; the "
                                     "mutation must be unambiguous" % (label, n))
                counts.append(n)
                data = data.replace(old, new)
            CONSOLIDATE.write_bytes(data)
            try:
                rc, tail = run_leg(leg)
            finally:
                restore()
            ok = rc != 0 and expect in tail
            print("%-18s %s" % ("DISCRIMINATING" if ok else "NOT-DISCRIMINATING",
                                label))
            print("                 executed leg rc=%s (%s anchor(s) mutated)"
                  % (rc, "+".join(str(c) for c in counts)))
            # the ASSERTION that killed the mutant, so a reviewer can see the
            # mutant failed for the intended reason and not another
            print("                 mutant failure tail: %s"
                  % " | ".join(tail.strip().splitlines()[-6:]))
            if expect in tail:
                print("                 the mutant failed for the EXPECTED "
                      "reason: %s" % expect)
            if not ok:
                failures.append(label)
                if expect not in tail:
                    print("                 the mutant did not fail for the "
                          "expected reason %r" % expect)
                print("                 tail: %s" % tail[-400:])
    finally:
        restore()

    for p in TARGETS:
        if p.read_bytes() != SNAPS[p].read_bytes():
            raise SystemExit("RESTORE FAILED for %s" % p.name)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print()
    print("every target restored byte-for-byte")
    if failures:
        print("FAILED: %r" % failures)
        return 1
    print("all %d probes DISCRIMINATING, and the fixed source passes them"
          % len(PROBES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
