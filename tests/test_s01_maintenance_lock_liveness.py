"""Acceptance checks (S01 / issue #262): maintenance-lock holder liveness.

Red-checkpoint contract for the dead-holder maintenance-lock bug. Every
store.py command probes the store's maintenance lock (``<store
dir>/.zmem-maintenance.lock``) with a non-blocking acquire-then-release
before touching the store. Today that probe classifies liveness by MTIME
ONLY (``host.acquire_lock`` returns None whenever the lock file exists and
is younger than ``ZMEM_MAINTENANCE_LOCK_STALE_SECONDS``, default 1800s).
So a lock whose holder is DEAD — a process killed mid-hold, or an empty
file whose creator never got to write its token — parks EVERY command,
including ``add``, in a 5-second poll (``ZMEM_MAINTENANCE_WAIT_SECONDS``
default 5.0) that ends in exit 2 with ``[zmem] zmem: maintenance is
active; add timed out after 5.0s waiting for restore to finish``.

These checks freeze the post-fix behaviour:

  - DeadHolderLockTest.test_backdated_empty_lock_file_without_holder_does_not_block_add
    An empty lock file backdated 30s (no holder at all; far beyond any
    create-to-token-write latency, far inside the 1800s stale window) must
    NOT block ``add``. RED at the unfixed base, GREEN after the fix.
  - DeadHolderLockTest.test_lock_left_by_killed_holder_does_not_block_add
    A real lock (token ``pid:uuid`` present) whose owning process was
    killed must NOT block ``add``. RED at the unfixed base, GREEN after
    the fix.
  - FreshEmptyLockTest.test_fresh_empty_lock_file_still_blocks_add
    A zero-age empty lock file may be a live acquirer caught between file
    creation and token write, so it must STILL block ``add`` (exit 2, no
    store created). GREEN at the base and after the fix — it guards the
    fix's grace-period boundary.

House rules obeyed throughout: the real store at ~/.zmem is never touched
(every subprocess gets ZMEM_STORE/ZMEM_DATA pinned into a throwaway
tempfile.mkdtemp dir cleaned with ignore_errors=True — no context-managed
TemporaryDirectory, which risks Windows PermissionError teardown while
sqlite handles are open), the real store.py CLI is driven via subprocess
only, and this test process never imports storelib. The maintenance env
knobs are POPPED from the child env so the frozen defaults (1800s stale /
5.0s wait) always apply.

Run: python tests/test_s01_maintenance_lock_liveness.py  (unittest.main;
individual tests: python tests/test_s01_maintenance_lock_liveness.py
DeadHolderLockTest.test_backdated_empty_lock_file_without_holder_does_not_block_add)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

MAINTENANCE_LOCK_NAME = ".zmem-maintenance.lock"
STORE_FILE_NAME = "store.sqlite"

# Child process for the killed-holder check: puts the scripts dir on
# sys.path, takes the maintenance lock via the real host.acquire_lock,
# reports readiness on stdout, then sleeps long enough to be killed while
# holding it. The scripts dir and lock path travel as argv items (never
# interpolated into the code string) to avoid quoting problems.
CHILD_CODE = (
    "import sys\n"
    "import time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import host\n"
    "from pathlib import Path\n"
    "token = host.acquire_lock(Path(sys.argv[2]), 600)\n"
    "print('ready', token is not None, flush=True)\n"
    "time.sleep(600)\n"
)


def _pinned_env(tmp: str) -> dict:
    """Child env pinned to the throwaway store under ``tmp``.

    Built from dict(os.environ) then overridden/popped: ZMEM_STORE and
    ZMEM_DATA isolate the child from the real ~/.zmem store, ZMEM_AUTO_REKEY
    keeps unrelated remediation out of the picture, and every maintenance
    lock knob is removed so the frozen defaults govern the run.
    """
    env = dict(os.environ)
    env["ZMEM_STORE"] = os.path.join(tmp, STORE_FILE_NAME)
    env["ZMEM_DATA"] = tmp
    env["ZMEM_AUTO_REKEY"] = "0"
    for knob in (
        "ZMEM_MAINTENANCE_LOCK_STALE_SECONDS",
        "ZMEM_MAINTENANCE_WAIT_SECONDS",
        "ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS",
    ):
        env.pop(knob, None)
    return env


def _run_add(env: dict) -> subprocess.CompletedProcess:
    """Run the real store.py add CLI against the pinned environment."""
    return subprocess.run(
        [
            sys.executable,
            str(STORE_PY),
            "add",
            "--namespace",
            "project:s01lock",
            "--type",
            "lesson",
            "--content",
            "probe row s01",
            "--signal",
            "test",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


class DeadHolderLockTest(unittest.TestCase):
    """A maintenance lock whose holder is dead must not block store.py add."""

    def test_backdated_empty_lock_file_without_holder_does_not_block_add(self):
        tmp = tempfile.mkdtemp(prefix="zmem-s01-backdated-")
        self.addCleanup(shutil.rmtree, tmp, True)
        lock_path = Path(tmp) / MAINTENANCE_LOCK_NAME
        lock_path.write_bytes(b"")
        # 30 seconds old: no creator is still mid-token-write, yet far
        # inside the 1800s stale window that today treats it as live.
        backdated = time.time() - 30.0
        os.utime(lock_path, (backdated, backdated))

        proc = _run_add(_pinned_env(tmp))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr.strip())
        self.assertTrue(
            (Path(tmp) / STORE_FILE_NAME).exists(),
            "store.sqlite was not created by the successful add",
        )

    def test_lock_left_by_killed_holder_does_not_block_add(self):
        tmp = tempfile.mkdtemp(prefix="zmem-s01-killed-")
        self.addCleanup(shutil.rmtree, tmp, True)
        lock_path = Path(tmp) / MAINTENANCE_LOCK_NAME

        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_CODE, str(SCRIPTS_DIR), str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = child.stdout.readline()
            self.assertTrue(
                line.startswith("ready"),
                f"lock-holder child did not report readiness: {line!r}",
            )
            self.assertIn(
                "True",
                line,
                f"lock-holder child failed to acquire the lock: {line!r}",
            )
            self.assertTrue(
                lock_path.exists(),
                "lock file missing after the child acquired it",
            )
            self.assertGreater(
                lock_path.stat().st_size,
                0,
                "lock file carries no holder token (expected pid:uuid bytes)",
            )
            child.kill()
            child.wait()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if child.stdout is not None:
                child.stdout.close()

        # Phantom precondition: the killed holder never released the lock.
        self.assertTrue(
            lock_path.exists(),
            "phantom precondition lost: lock file vanished after holder died",
        )

        proc = _run_add(_pinned_env(tmp))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr.strip())
        self.assertTrue(
            (Path(tmp) / STORE_FILE_NAME).exists(),
            "store.sqlite was not created by the successful add",
        )


class FreshEmptyLockTest(unittest.TestCase):
    """Suite-only boundary pin: a zero-age empty lock must still block add.

    GREEN both at the unfixed base and after the liveness fix — an empty
    lock file created a moment ago may be a live acquirer between create
    and token write, so it must never be fast-pathed as dead.
    """

    def test_fresh_empty_lock_file_still_blocks_add(self):
        tmp = tempfile.mkdtemp(prefix="zmem-s01-fresh-")
        self.addCleanup(shutil.rmtree, tmp, True)
        lock_path = Path(tmp) / MAINTENANCE_LOCK_NAME
        # mtime left at now: deliberately NOT backdated.
        lock_path.write_bytes(b"")

        proc = _run_add(_pinned_env(tmp))
        self.assertEqual(proc.returncode, 2, msg=proc.stdout)
        self.assertIn("maintenance is active", proc.stderr)
        self.assertFalse(
            (Path(tmp) / STORE_FILE_NAME).exists(),
            "store.sqlite must not be created while maintenance is active",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
