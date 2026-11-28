"""Wave-2e mutation probes for the frozen acceptance checks (PR #541).

Two probes, each reverting ONE fix in place and requiring that the probe
FAILS. The probes are source-bound: they read the frozen check file and
EVALUATE ITS OWN filtering comprehensions, so a change to the source's
predicate changes the probe's outcome.

This module lives under `tests/support/` because the swarm probe runner
executes its target inside the tree it extracts from the revision -- a
script outside the tree cannot be run by the runner. The runner's
isolation (a `git archive` copy, never the reviewed working tree) is what
makes the in-place mutation safe.

Run: python tests/support/wave2e_probes.py   (from the repository root)

The working root is the CURRENT DIRECTORY, so the same module runs
against the repository root or against a runner-extracted copy.

Probes:

R-4  the exact-id filter regresses to a substring match. The filter's
     oracle is the source's own comprehension, compiled from the file.
     Under the fixed source the store-facing id `node-liveness` (which
     merely CONTAINS "node") is reported and the host-tooling id `node`
     is not; under the substring mutant the store-facing id is swallowed
     and the probe fails.

R-5  the exit backstop restores the stale snapshot. The fix has TWO
     halves and each alone already prevents the stale restore (the
     backstop checks for an OUTSTANDING pin; the final release clears the
     snapshot), so the mutant reverts BOTH halves -- reverting one alone
     would leave the other protecting, and the probe would not bite.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()  # the runner sets cwd to the EXTRACTED tree
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-wave2e-probes-"))

PIN = ROOT / "tests/_store_env_pin.py"
RECOVERY = ROOT / "tests/test_audit_schema_recovery.py"
TARGETS = [PIN, RECOVERY]
SNAPS = {p: SCRATCH / (p.name + ".snap") for p in TARGETS}

# ---------------------------------------------------------------- R-4
# The oracle EXECUTES the source's own comprehensions. It finds every
# list comprehension whose FILTER uses the _HOST_STATE constants and
# evaluates each one against a synthetic check list, with the tuples read
# from the source's own assignments.
R4_PROBE = '''
import ast, sys

PATH = "tests/test_audit_schema_recovery.py"
src = open(PATH, encoding="utf-8").read()
tree = ast.parse(src)

consts = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id.startswith("_HOST_STATE"):
            consts[target.id] = ast.literal_eval(node.value)
assert consts, "the source declares no _HOST_STATE* constants"


def check_ids(reported):
    """The reported ids: an element may be the bare id or an (id, status)
    tuple."""
    out = []
    for item in reported:
        if isinstance(item, tuple):
            out.append(str(item[0]))
        else:
            out.append(str(item))
    return out


comps = []
for node in ast.walk(tree):
    if not isinstance(node, ast.ListComp):
        continue
    conds = " ".join(ast.unparse(cond)
                     for gen in node.generators for cond in gen.ifs)
    if "_HOST_STATE" not in conds:
        continue
    comps.append(node)
assert len(comps) >= 2, (
    "expected the source's host-state filters, found %d" % len(comps))


class _Payload:
    def __init__(self, checks):
        self.checks = checks

    def get(self, key, default=None):
        return getattr(self, key, default)


HOST_ID = "node"              # the host-tooling id: must NOT be reported
STORE_ID = "node-liveness"    # store-facing, merely CONTAINS "node": must be

for index, node in enumerate(comps):
    synthetic = [{"id": STORE_ID, "status": "fail"},
                 {"id": HOST_ID, "status": "fail"}]
    env = dict(consts)
    env["checks"] = synthetic
    env["payload"] = _Payload(synthetic)
    expr = ast.Expression(body=node)
    ast.fix_missing_locations(expr)
    ids = check_ids(eval(compile(expr, PATH, "eval"), env))
    assert STORE_ID in ids, (
        "filter %d did not report the store-facing check %r: %r"
        % (index, STORE_ID, ids))
    assert HOST_ID not in ids, (
        "filter %d reported the host-tooling check %r: %r"
        % (index, HOST_ID, ids))
'''

# ---------------------------------------------------------------- R-5
R5_PROBE = '''
import os, sys
sys.path.insert(0, "tests")
import _store_env_pin as pin

KEY_A = "ZMEM_PIN_EXIT_A"
os.environ[KEY_A] = "operator"
token_a = pin.acquire((KEY_A,))
os.environ[KEY_A] = "pinned"
pin._restore_at_exit()
assert os.environ.get(KEY_A) == "operator", os.environ.get(KEY_A)
pin.release(token_a)

KEY_B = "ZMEM_PIN_EXIT_B"
os.environ[KEY_B] = "operator"
token_b = pin.acquire((KEY_B,))
os.environ[KEY_B] = "pinned"
pin.release(token_b)
assert os.environ.get(KEY_B) == "operator", os.environ.get(KEY_B)
os.environ[KEY_B] = "legit-later-change"
pin._restore_at_exit()
assert os.environ.get(KEY_B) == "legit-later-change", (
    "the exit backstop restored the stale snapshot: %r" % os.environ.get(KEY_B))

KEY_C = "ZMEM_PIN_EXIT_C"
os.environ[KEY_C] = "cycle-one"
first = pin.acquire((KEY_C,))
os.environ[KEY_C] = "pinned"
pin.release(first)
assert os.environ.get(KEY_C) == "cycle-one", os.environ.get(KEY_C)
os.environ[KEY_C] = "cycle-two"
second = pin.acquire((KEY_C,))
os.environ[KEY_C] = "pinned-again"
pin.release(second)
assert os.environ.get(KEY_C) == "cycle-two", (
    "the second cycle restored the FIRST cycle's snapshot: %r" % os.environ.get(KEY_C))
pin._restore_at_exit()
assert os.environ.get(KEY_C) == "cycle-two", (
    "the exit backstop restored a stale snapshot: %r" % os.environ.get(KEY_C))
'''

# the GENUINE substring regression, at BOTH filtering sites: the exact-id
# membership test becomes a substring test over the same tuple
SUBSTRING_MUTANT_1 = (
    b"                     and str(c.get(\"id\") or \"\") not in _HOST_STATE_IDS\n",
    b"                     and not any(m in str(c.get(\"id\") or \"\").lower()\n"
    b"                                 for m in _HOST_STATE_IDS)\n")
SUBSTRING_MUTANT_2 = (
    b"                      and str(c.get(\"id\") or \"\") not in _HOST_STATE_IDS]\n",
    b"                      and not any(m in str(c.get(\"id\") or \"\").lower()\n"
    b"                                  for m in _HOST_STATE_IDS)]\n")

PROBES = [
    ("R-4 the exact-id filter regresses to a substring match",
     RECOVERY,
     [SUBSTRING_MUTANT_1, SUBSTRING_MUTANT_2],
     R4_PROBE),

    ("R-5 the exit backstop restores the stale snapshot",
     PIN,
     [(b"    if not _ACTIVE:\n"
       b"        return\n"
       b"    for k, v in _ORIGINAL.items():\n",
       b"    for k, v in _ORIGINAL.items():\n"),
      (b"    # The snapshot has been restored: clear it, so nothing stale survives\n"
       b"    # into a later cycle or into the process-exit backstop.\n"
       b"    _ORIGINAL.clear()\n",
       b"")],
     R5_PROBE),
]


def snapshot() -> None:
    for p in TARGETS:
        SNAPS[p].write_bytes(p.read_bytes())


def restore() -> None:
    for p in TARGETS:
        p.write_bytes(SNAPS[p].read_bytes())


def run_probe(code: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "tests")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                          capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr)[-900:]


def main() -> int:
    snapshot()
    failures = []
    try:
        for label, _target, _mutant, code in PROBES:
            rc, tail = run_probe(code)
            print("%-18s %s" % ("FIXED-PASSES" if rc == 0 else "FIXED-FAILS", label))
            if rc != 0:
                failures.append(label + " (the fixed source failed the probe)")
                print("                 tail: %s" % tail[-400:])

        for label, target, mutant, code in PROBES:
            data = target.read_bytes()
            counts = []
            for old, new in mutant:
                n = data.count(old)
                if n == 0:
                    raise SystemExit("%s: an anchor was not found" % label)
                counts.append(n)
                data = data.replace(old, new)
            target.write_bytes(data)
            try:
                rc, tail = run_probe(code)
            finally:
                restore()
            ok = rc != 0
            print("%-18s %s" % ("DISCRIMINATING" if ok else "NOT-DISCRIMINATING", label))
            print("                 executed probe rc=%s (%s anchor(s) mutated)"
                  % (rc, "+".join(str(c) for c in counts)))
            # the ASSERTION that killed the mutant, so a reviewer can see the
            # mutant failed for the intended reason and not another
            print("                 mutant failure tail: %s"
                  % " | ".join(tail.strip().splitlines()[-2:]))
            if not ok:
                failures.append(label)
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
