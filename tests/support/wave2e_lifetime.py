"""Wave-2e lifetime proof for the frozen acceptance checks (PR #541).

Proves, by execution, the three claims the Stage-B review made about the
shared env-pin helper (`tests/_store_env_pin.py`) and the recovery module
that uses it.

This module lives under `tests/support/` because the swarm probe runner
executes its target inside the tree it extracts from the revision -- a
script outside the tree cannot be run by the runner. The working root is
the CURRENT DIRECTORY, so the same module runs against the repository
root or against a runner-extracted copy.

Run: python tests/support/wave2e_lifetime.py   (from the repository root)

Claims proved:

1. The ACTUAL pinned keys (ZMEM_STORE, ZMEM_DATA, ZMEM_BACKUP_DIR) are
   restored at module teardown -- not a side-channel probe variable.
2. The exit backstop restores an OUTSTANDING pin.
3. The exit backstop does NOT restore a RELEASED pin: a later module's
   legitimate environment change survives process exit.
4. A fresh cycle's snapshot does not survive into the next cycle, and the
   exit backstop does not restore a stale one afterwards.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()  # the runner sets cwd to the EXTRACTED tree
SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-wave2e-lifetime-"))

PINNED_KEYS_PROBE = '''
import os, sys
os.environ["ZMEM_STORE"] = "operator-store"
os.environ["ZMEM_DATA"] = "operator-data"
os.environ["ZMEM_BACKUP_DIR"] = "operator-backups"
sys.path.insert(0, "tests")
import _store_env_pin as pin
import test_audit_schema_recovery as recovery

# the module pinned the operator's values and applied its scratch values
assert pin._ACTIVE, "the module holds no pin"
assert os.environ["ZMEM_STORE"] != "operator-store", os.environ["ZMEM_STORE"]

recovery.tearDownModule()
assert not pin._ACTIVE, "the pin survived the teardown: %r" % (sorted(pin._ACTIVE),)
assert os.environ["ZMEM_STORE"] == "operator-store", os.environ["ZMEM_STORE"]
assert os.environ["ZMEM_DATA"] == "operator-data", os.environ["ZMEM_DATA"]
assert os.environ["ZMEM_BACKUP_DIR"] == "operator-backups", os.environ["ZMEM_BACKUP_DIR"]
print("restored", os.environ["ZMEM_STORE"], os.environ["ZMEM_DATA"], os.environ["ZMEM_BACKUP_DIR"])
'''

EXIT_BACKSTOP_PROBE = '''
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
print("all three exit-backstop states hold")
'''


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
    failures = []
    for label, code in (("the ACTUAL pinned keys are restored at module teardown",
                          PINNED_KEYS_PROBE),
                         ("R-5 the exit backstop's three states",
                          EXIT_BACKSTOP_PROBE)):
        rc, out = run_probe(code)
        print("%s:" % label)
        print(out)
        if rc != 0:
            failures.append(label)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print()
    if failures:
        print("FAILED: %r" % failures)
        return 1
    print("EVERY WAVE-2e CLAIM EXECUTED AND VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
