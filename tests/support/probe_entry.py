"""One unittest entry point for this PR's tree-resident probe drivers.

The swarm probe runner's contract (2.0.0) accepts exactly ONE target
shape: a unittest MODULE. This PR's probe drivers are plain scripts, so
this module is what the runner loads: its `load_tests` protocol builds
ONE test that runs the driver named by `ZMEM_PROBE_KIND` inside the
revision the runner extracted, and asserts that driver's own verdict.

The drivers keep their command-line entry points, so each is still
runnable directly; this module only adds the unittest route the runner
needs. Nothing here decides a verdict: the driver does, and this module
fails the test when the driver reports a failure.

`ZMEM_PROBE_KIND` selects the driver:

  guard-cases    tests/support/conflict_marker_cases.py
  mutation       tests/support/wave2e_probes.py
  lifetime       tests/support/wave2e_lifetime.py
  frozen-base    tests/support/frozen_base_leg.py
  vec-suite      tests/support/vec_suite_leg.py

The frozen-base driver's own arguments arrive through the environment
(the runner's `env_extra`), because the runner's argv belongs to
unittest:

  ZMEM_FROZEN_CHECK      the check id (C1..C6)
  ZMEM_FROZEN_BASE       the base revision the check must fail against
  ZMEM_FROZEN_EXPECT     the failure signature the base leg must show
  ZMEM_FROZEN_FILES      the frozen files, joined with "|"
  ZMEM_FROZEN_COMMAND    the shell command the base leg runs
  ZMEM_FROZEN_REPO       the repository the base worktree is cut from
  ZMEM_FROZEN_USER_SITE  the real user site-packages (pytest lives there)
"""
from __future__ import annotations

import os
import sys
import unittest

DRIVERS = {
    "guard-cases": "conflict_marker_cases",
    "mutation": "wave2e_probes",
    "lifetime": "wave2e_lifetime",
    "frozen-base": "frozen_base_leg",
    "vec-suite": "vec_suite_leg",
}


def frozen_base_argv() -> list[str]:
    """The frozen-base driver's argv, read from the environment."""
    argv = [
        "frozen_base_leg",
        "--check", os.environ["ZMEM_FROZEN_CHECK"],
        "--base", os.environ["ZMEM_FROZEN_BASE"],
        "--expect", os.environ["ZMEM_FROZEN_EXPECT"],
        "--command", os.environ["ZMEM_FROZEN_COMMAND"],
    ]
    for rel in os.environ.get("ZMEM_FROZEN_FILES", "").split("|"):
        if rel:
            argv += ["--frozen", rel]
    for name, flag in (("ZMEM_FROZEN_REPO", "--repo"),
                        ("ZMEM_FROZEN_USER_SITE", "--user-site")):
        value = os.environ.get(name, "")
        if value:
            argv += [flag, value]
    return argv


def run_driver(kind: str) -> int:
    """Run the named driver and return its exit code."""
    if kind == "frozen-base":
        import frozen_base_leg
        saved = sys.argv
        sys.argv = frozen_base_argv()
        try:
            return frozen_base_leg.main()
        finally:
            sys.argv = saved
    return __import__(DRIVERS[kind]).main()


def load_tests(loader, tests, pattern):
    kind = os.environ.get("ZMEM_PROBE_KIND", "")

    class _Probe(unittest.TestCase):
        def test_probe(self):
            self.assertIn(kind, DRIVERS,
                          "unknown probe kind %r (expected one of %s)"
                          % (kind, ", ".join(sorted(DRIVERS))))
            rc = run_driver(kind)
            self.assertEqual(rc, 0,
                             "the %s driver reported a failure (exit %s)"
                             % (kind, rc))

    return loader.loadTestsFromTestCase(_Probe)
