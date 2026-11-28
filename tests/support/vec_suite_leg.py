"""The vec-suite driver: the two vec-gated legs, with the dependency's
provenance recorded.

The runner's leg interpreter is a fresh `python -m venv --without-pip`,
so it carries NO third-party packages -- which is why the supplementary
suite's runner receipt cannot run the two vec-gated legs (they need the
`sqlite_vec` extension). This driver runs them inside the leg's own
interpreter and RECORDS what that interpreter could load:

  * the interpreter's executable and version;
  * the extension's file and version;
  * the interpreter's `sys.path`;
  * the environment its child processes inherit.

The runner captures that stdout into the receipt, so the receipt carries
the dependency's provenance rather than the orchestrator's assertion
about it. Nothing here decides a verdict: the two legs do, and the
driver reports their result.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

VEC_LEGS = (
    ("test_audit_x_store_recovery_supp", "LoopBoundShapeTest",
     "test_client_without_sqlite_vec_accepts_vec_store"),
    ("test_audit_x_store_recovery_supp", "Vec0RekeyCasSuppTest",
     "test_vec0_gather_carries_namespace"),
)

CHILD_ENV_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_BACKUP_DIR",
                  "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                  "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONSAFEPATH",
                  "PYTHONUTF8", "PYTHONIOENCODING")


def _dependency_provenance() -> list[str]:
    """What THIS interpreter can load, as lines the runner will capture."""
    lines = [
        "PROBE-INTERPRETER executable=%s version=%s prefix=%s"
        % (sys.executable, sys.version.split()[0], sys.prefix),
        "PROBE-SYSPATH %s" % json.dumps(sys.path),
    ]
    try:
        import sqlite_vec
        lines.append("PROBE-DEPENDENCY module=sqlite_vec file=%s version=%s"
                     % (getattr(sqlite_vec, "__file__", "?"),
                        getattr(sqlite_vec, "__version__", "?")))
    except Exception as exc:
        lines.append("PROBE-DEPENDENCY module=sqlite_vec import_error=%r" % (exc,))
    lines.append("PROBE-CHILD-ENV %s"
                 % json.dumps({k: os.environ.get(k) for k in CHILD_ENV_KEYS}))
    return lines


def main() -> int:
    """Run the two vec-gated legs in this interpreter. 0 = both passed."""
    for line in _dependency_provenance():
        print(line)
    sys.stdout.flush()
    suite = unittest.TestSuite()
    for module_name, class_name, method in VEC_LEGS:
        module = __import__(module_name)
        suite.addTest(getattr(module, class_name)(method))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("PROBE-VEC-RESULT ran=%d failures=%d errors=%d"
          % (result.testsRun, len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1
