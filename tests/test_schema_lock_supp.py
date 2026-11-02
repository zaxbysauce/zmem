"""Supplementary schema-lock liveness tests (issue #357, plan-critic R2).

The frozen C6 exercises the seam directly; these add the integration and
wait-preservation coverage the critic required:
  S1  a dead-PID schema lock does not wedge a REAL consumer (store.py stats
      through _prepare_store) — it succeeds once the phantom is broken;
  S2  a LIVE holder still gets the caller's bounded wait (not an instant
      refusal): with ZMEM_SCHEMA_LOCK_WAIT_SECONDS tiny, a live own-PID
      lock must time out (None) at roughly the budget, not immediately;
  S3  routing ratchet — no production site acquires the schema lock through
      a bare _strict_acquire_lock("schema" any more (all three consumers
      route through the liveness wrapper).

Run: python tests/test_schema_lock_supp.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import _store_guard  # noqa: F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCHEMA_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "schema.py"
PURGE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "purge.py"
BACKUP_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "backup.py"

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")


def _lock_dir(store: Path) -> Path:
    return store.parent


def _write_lock(store: Path, pid: int) -> Path:
    p = store.parent / ".zmem-schema.lock"
    p.write_text(f"{pid}:{'0' * 32}", encoding="utf-8")
    os.utime(p, (time.time(), time.time()))
    return p


class SchemaLockSuppTest(unittest.TestCase):
    tmp: Path
    saved: dict

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="zmem-lock-supp-"))
        cls.saved = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(cls.tmp / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(cls.tmp)
        os.environ["ZMEM_MODELS_DIR"] = str(cls.tmp / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, True)

    def test_S1_dead_holder_does_not_wedge_real_consumer(self):
        store = self.tmp / "store.sqlite"
        dead = subprocess.run(
            [sys.executable, "-c", "import sys"],
            capture_output=True)  # any short-lived child; reuse its exit? no
        # Spawn and reap a REAL child to get a provably dead PID.
        child = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait()
        _write_lock(store, child.pid)
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(STORE_PY), "stats"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=120, env=dict(os.environ))
        self.assertEqual(
            proc.returncode, 0,
            f"stats refused under a dead schema-lock holder\n"
            f"stdout: {proc.stdout[-800:]!r}\nstderr: {proc.stderr[-800:]!r}")

    def test_S2_live_holder_keeps_bounded_wait(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        try:
            import storelib.schema as schema  # noqa: E402
        finally:
            sys.path.remove(str(REPO_ROOT / "skills" / "memory" / "scripts"))
        store = Path(schema.STORE_PATH)
        lock = _write_lock(store, os.getpid())  # live: our own PID
        self.addCleanup(lambda: lock.unlink(missing_ok=True))
        budget = 0.4
        began = time.monotonic()
        token = schema._acquire_schema_lock_with_liveness(
            wait_seconds=budget, poll_seconds=0.02)
        elapsed = time.monotonic() - began
        self.assertIsNone(
            token, "a live holder (own PID) must NOT be broken")
        self.assertGreaterEqual(
            elapsed, budget * 0.8,
            f"live-holder wait collapsed to {elapsed:.3f}s — the bounded "
            f"wait must be preserved, not converted to an instant refusal")

    def test_S3_all_schema_consumers_route_through_liveness(self):
        # Full-text match: the wrapper's calls spread "schema" onto the
        # line after the opening paren, so a per-line scan sees only the
        # docstring mention.
        bare = re.compile(r'_strict_acquire_lock\(\s*"schema"', re.S)
        for path in (SCHEMA_PY, PURGE_PY, BACKUP_PY):
            src = path.read_text(encoding="utf-8")
            hits = [m.start() for m in bare.finditer(src)]
            if path == SCHEMA_PY:
                # The ONLY bare acquisitions left in schema.py are the
                # wrapper's own probe/retry/fall-through (three calls) plus
                # its docstring mention — all inside the wrapper function.
                wrapper_at = src.index("def _acquire_schema_lock_with_liveness")
                wrapper_end = src.index("def _acquire_maintenance_lock_with_liveness")
                self.assertGreaterEqual(len(hits), 3)
                for at in hits:
                    self.assertTrue(
                        wrapper_at <= at < wrapper_end,
                        f"bare schema acquisition outside the wrapper at "
                        f"offset {at} of {path.name}")
            else:
                self.assertEqual(
                    hits, [],
                    f"{path.name} still acquires the schema lock directly")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
