"""Frozen acceptance check C6 (issue #357): the schema lock must break a
DEAD holder's phantom instead of waiting out the full stale window.

SchemaLockLivenessTest reproduces the defect behaviorally on a hermetic
temp store and pins BOTH directions of the fixed seam:

- test_dead_holder_is_broken — a lock file at the schema-lock path
  (<store_dir>/.zmem-schema.lock, computed the way schema._lock_path does)
  whose content is "<dead-pid>:<32-hex>" with a FRESH mtime must be broken
  by the acquisition seam the fix provides (planned:
  schema._acquire_schema_lock_with_liveness(wait_seconds=...)): the call
  returns a token, not None. At base the seam does not exist, so the check
  fails with:
      AssertionError: the schema lock has no holder-liveness escape at base
- test_live_holder_is_not_broken — the fail-closed contrast: a lock file
  whose pid IS the current process (fresh mtime) must NOT be broken; the
  seam returns None.

The dead pid is produced by spawning and reaping a real child process and
PROVING it is not alive through host.process_alive (referenced inside the
method only), with a high spare pid as fallback.

Hermetic discipline: the store env is pinned in setUpClass BEFORE the
first storelib import anywhere in the process (storelib freezes
STORE_PATH at import — the lock path hangs off STORE_PATH.parent) and
restored in tearDownClass. The fix-added seam is referenced only inside
test methods, so the base leg fails with the intended AssertionError.

Run: python tests/test_schema_lock.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import _store_guard  # noqa: F401  (issue #244 isolation guard)
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")

_FROZEN_MESSAGE = "the schema lock has no holder-liveness escape at base"


class SchemaLockLivenessTest(unittest.TestCase):
    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-c6-lock-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None

    @staticmethod
    def _provably_dead_pid() -> int:
        """A pid guaranteed not to be running, verified via host.process_alive
        (imported inside this method — never at module import time)."""
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import host  # noqa: PLC0415 — scripts dir pinned above

        candidates: list[int] = []
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        candidates.append(proc.pid)
        proc.kill()
        proc.wait()  # reaped: the pid is gone on every platform
        candidates.append(9999999)  # spare: far outside any pid table
        for pid in candidates:
            verdict = host.process_alive(pid)
            print(f"[C6] process_alive({pid}) -> {verdict}")
            if verdict is False:
                return pid
        raise AssertionError(
            "harness could not obtain a provably-dead pid via "
            "host.process_alive")

    def _write_lock_file(self, pid: int) -> Path:
        """Write '<pid>:<32-hex>' at the schema-lock path, mtime = NOW."""
        from storelib import schema as schema_mod

        path = schema_mod._lock_path("schema")  # STORE_PATH.parent/.zmem-schema.lock
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        # acquire_lock writes "{pid}:{uuid4.hex}" — same shape, hand-made.
        path.write_text(f"{pid}:{'ab' * 16}", encoding="utf-8")
        now = time.time()
        os.utime(str(path), (now, now))  # fresh: NOT mtime-stale
        return path

    def _acquire_with_liveness(self, wait_seconds: float):
        """Call the fix's schema-lock liveness seam; at base the attribute is
        absent and the caller re-raises the frozen AssertionError."""
        from storelib import schema as schema_mod

        try:
            return schema_mod._acquire_schema_lock_with_liveness(
                wait_seconds=wait_seconds)
        except AttributeError:
            raise AssertionError(_FROZEN_MESSAGE)

    def test_dead_holder_is_broken(self):
        dead_pid = self._provably_dead_pid()
        self._write_lock_file(dead_pid)

        token = self._acquire_with_liveness(wait_seconds=1.0)

        self.assertIsNotNone(
            token,
            "a schema lock held by a provably-dead pid must be broken for "
            "the next writer (token returned), not waited out")
        from storelib import schema as schema_mod

        if token is not None:
            schema_mod._release_named_lock("schema", token)

    def test_live_holder_is_not_broken(self):
        lock_path = self._write_lock_file(os.getpid())  # we ARE alive

        token = self._acquire_with_liveness(wait_seconds=0.5)

        self.assertIsNone(
            token,
            "a schema lock held by a LIVE pid must NOT be broken "
            "(fail-closed: acquisition returns None)")
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
