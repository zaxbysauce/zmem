"""Frozen acceptance check C10 (issue #360, item 1 / AC8): the schema-16
migration tests leave the process-global environment untouched.

EntityEnvRestoreTest runs tests/test_entity.py's MigrationTest — the class
whose two env-mutation blocks (test_failed_v16_rebuild_rolls_back and
test_pre_v10_store_with_v2_kind_tag_backfills, at base lines ~734-746 and
~815-825) capture their restore snapshot AFTER mutating os.environ — in a
SUBPROCESS so this process's environment is never at risk. The child
starts from controlled NON-default marker values for the five ZMEM_* keys
(ZMEM_STORE=<marker-a>, ZMEM_DATA=<marker-b>, ZMEM_MODELS_DIR=<marker-c>,
ZMEM_MODEL_AUTODOWNLOAD=9, ZMEM_ALLOW_NEWER_SCHEMA unset), runs the class
under unittest, and prints the five values as JSON on its way out.

At base the addCleanup restore replays the MUTATED values, so the child's
exit snapshot drifts from the markers and the check fails with:
    AssertionError: process-global env leaked by the schema-16 test module
    at base (drifted keys: ...)
After the fix the snapshot-before-mutation restore puts the markers back
and the check passes. Runtime is bounded (~8s measured at base; hard cap
55s) and the child is driven with sys.executable from the repo root.

Hermetic discipline: this file's own env is pinned in setUpClass before
any storelib import (none happens here — the module under test runs only
inside the child) and restored in tearDownClass.

Run: python tests/test_entity_env_restore.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")
_SNAPSHOT_PREFIX = "ZMEM_ENV_SNAPSHOT "
_BINDING_PREFIX = "ZMEM_BINDING_SNAPSHOT "

# The child: repo root + tests dir on sys.path (for `import _store_guard`
# inside test_entity), run the migration test class, then report the five
# env values AFTER every cleanup has run.
_CHILD_CODE = r"""
import json, os, sys, unittest
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
loader = unittest.TestLoader()
suite = loader.loadTestsFromName("tests.test_entity.MigrationTest")
result = unittest.TextTestRunner(verbosity=0).run(suite)
keys = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
        "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")
print("ZMEM_ENV_SNAPSHOT " + json.dumps({k: os.environ.get(k) for k in keys}))
# Plan-critic round 1 (AC8's second half): storelib freezes STORE_PATH at
# first import; a migration test that imports it under a fixture pin and
# leaves the module cached strands that binding for every later in-process
# user. Report the surviving binding (None when the tests purged their
# storelib imports or never left any).
schema_mod = sys.modules.get("storelib.schema")
binding = None
if schema_mod is not None:
    bp = getattr(schema_mod, "STORE_PATH", None)
    binding = str(bp) if bp is not None else None
print("ZMEM_BINDING_SNAPSHOT " + json.dumps(
    {"storelib_schema_in_modules": schema_mod is not None,
     "store_path": binding}))
sys.exit(0 if result.wasSuccessful() else 3)
"""

_FROZEN_MESSAGE = ("process-global env leaked by the schema-16 test module "
                   "at base")


class EntityEnvRestoreTest(unittest.TestCase):
    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="zmem-c10-env-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)

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

    def test_migration_tests_leave_process_env_unchanged(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-c10-markers-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        expected = {
            "ZMEM_STORE": str(tmp / "marker-a-store.sqlite"),
            "ZMEM_DATA": str(tmp / "marker-b-data"),
            "ZMEM_MODELS_DIR": str(tmp / "marker-c-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "9",
            "ZMEM_ALLOW_NEWER_SCHEMA": None,
        }
        child_env = dict(os.environ)
        for k, v in expected.items():
            if v is None:
                child_env.pop(k, None)
            else:
                child_env[k] = v

        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_CODE],
            cwd=str(REPO_ROOT), env=child_env,
            capture_output=True, text=True, timeout=55)

        if proc.returncode != 0:
            self.fail(
                "the schema-16 migration tests did not pass inside the "
                f"child (exit {proc.returncode}) — harness failure, not an "
                f"env verdict\nstdout tail: {proc.stdout[-800:]!r}\n"
                f"stderr tail: {proc.stderr[-800:]!r}")

        snapshot = None
        binding_snapshot = None
        for line in proc.stdout.splitlines():
            if line.startswith(_SNAPSHOT_PREFIX):
                snapshot = json.loads(line[len(_SNAPSHOT_PREFIX):])
            elif line.startswith(_BINDING_PREFIX):
                binding_snapshot = json.loads(
                    line[len(_BINDING_PREFIX):])
        if snapshot is None or binding_snapshot is None:
            self.fail(
                "child did not print the env/binding snapshots — harness "
                f"failure, not an env verdict\nstdout tail: "
                f"{proc.stdout[-800:]!r}")

        drifted = [k for k in expected
                   if snapshot.get(k) != expected[k]]
        for k in drifted:
            print(f"[C10] DRIFT {k}: start={expected[k]!r} "
                  f"end={snapshot.get(k)!r}")
        # AC8's second half: a surviving storelib.schema import must carry
        # the child's ORIGINAL marker store binding, not a dead fixture
        # store (the module freezes STORE_PATH at first import).
        binding_drift = False
        if binding_snapshot.get("storelib_schema_in_modules"):
            if binding_snapshot.get("store_path") != expected["ZMEM_STORE"]:
                binding_drift = True
                print(f"[C10] DRIFT storelib.schema.STORE_PATH: "
                      f"expected {expected['ZMEM_STORE']!r}, survived "
                      f"{binding_snapshot.get('store_path')!r}")
        if drifted or binding_drift:
            raise AssertionError(
                f"{_FROZEN_MESSAGE} (drifted keys: "
                f"{', '.join(drifted) if drifted else 'env ok'}"
                f"{'; stale storelib binding' if binding_drift else ''}; "
                f"end values: "
                f"{ {k: snapshot.get(k) for k in drifted} })")


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
