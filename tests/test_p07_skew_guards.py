"""Supplementary (non-frozen) guards for the issue #322 skew surfaces.

Complements the frozen checks in tests/test_p07_version_skew.py with the
plan-critic-required pins (05-fix-plan.md REVISION 3, A1/A2/A3):

  PatternParityTest   - the registry's patterns ARE the scrubber's family
                        globs (drift dies loudly).
  SequencePinTest     - default family enumeration order is exactly the
                        historical 8-phase scrub order.
  SeamOrderingTest    - a reduced ZMEM_TEST_LEDGER_FAMILIES set skips that
                        family's phase without reordering the rest; drives
                        scrub_ledgers DIRECTLY (cmd_purge's preflight would
                        refuse first and prove only the refusal path).
  FaultInjectionTest  - (a) a listing failure raised by the enumeration
                        primitive ESCAPES unsupported_delivery_state_files
                        (Path.glob would swallow it; the helper is required
                        to use a non-suppressing primitive); (b) cmd_purge
                        under that failure refuses exit-5 with the store
                        row intact.
  InvalidTokenTest    - a bogus ZMEM_TEST_LEDGER_FAMILIES token is a
                        controlled refusal at both boundaries: purge exits
                        2 naming the token (store untouched); doctor emits
                        a warn check naming the misconfiguration.
  SeamGatedPurgeTest  - a KNOWN segment family under a seam that excludes
                        it: purge exit 5, row intact (the seam is consulted
                        by purge's supported set, not just doctor's).
  RetryPreflightTest  - a deny-listed, empty-chain scrub retry still hits
                        the preflight before any cleanup phase.

Isolation: CLI-level legs drive real subprocesses (store.py / doctor.py)
against scratch stores, env pinned at module import BEFORE any storelib
import; in-process legs (Sequence/Seam/Fault-a) purge sys.modules of
storelib* and re-import under a fresh pin per class. The real ~/.zmem
store is never touched.
"""


import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
DOCTOR_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"

import atexit
import shutil

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-p07-supp-"))
atexit.register(shutil.rmtree, _SCRATCH, True)
_PIN_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
             "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_LEDGER_FAMILIES")
_SAVED_ENV = {key: os.environ.get(key) for key in _PIN_KEYS}
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "no-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _marker in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZMEM_SESSION",
                "ZMEM_NAMESPACE", "ZMEM_TEST_LEDGER_FAMILIES"):
    os.environ.pop(_marker, None)
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))

import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the module-level env pins' originals (pytest citizenship;
    the frozen file's pattern)."""
    for key, val in _SAVED_ENV.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    _purge_storelib_modules()

# The historical scrub-phase order (purge.py scrub_ledgers loop order,
# stable since the families were introduced; the registry must never
# silently reorder it).
EXPECTED_FAMILY_ORDER = [
    "ledger.tmp", "tasktext.tmp", "signals.tmp", "reserve.tmp",
    "reserve", "ledger", "tasktext", "ledger.seg",
]
EXPECTED_PATTERNS = [
    "*.ledger.tmp.*", "*.tasktext.tmp.*", "*.signals.tmp.*",
    "*.reserve.tmp.*", "resv-*.reserve", "*.ledger", "*.tasktext",
    "*.ledger.seg.*",
]


def _purge_storelib_modules():
    for key in [k for k in sys.modules
                if k == "storelib" or k.startswith("storelib.")]:
        del sys.modules[key]


def _env(extra=None):
    # os.environ FIRST, explicit overrides AFTER (an inverted order lets a
    # passed dict REPLACE the environment - subprocesses then lose
    # PATH/HOME/USERPROFILE and crash in unrelated checks).
    env = dict(os.environ)
    env.update({
        "ZMEM_STORE": str(_SCRATCH / "store.sqlite"),
        "ZMEM_DATA": str(_SCRATCH),
        "ZMEM_MODELS_DIR": str(_SCRATCH / "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    # Per-test extras override the module pins (an ops-absent leg needs its
    # own ZMEM_STORE/ZMEM_DATA; module pins run BEFORE extras, never after).
    if extra:
        env.update(extra)
    return env


def _add_row(namespace="project:p07supp", content="p07supp needle alpha beta"):
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "add",
         "--namespace", namespace, "--type", "fact",
         "--content", content, "--tags", "supp", "--json"],
        capture_output=True, text=True, env=_env(), timeout=180,
        cwd=str(REPO_ROOT))
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout)["id"]


def _row_exists(mid, store=None):
    store = (store or (_SCRATCH / "store.sqlite"))
    # as_posix: sqlite URI mode mis-parses Windows drive-letter paths with
    # backslashes ("unable to open database file").
    con = sqlite3.connect("file:%s?mode=ro" % Path(store).as_posix(),
                          uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM memory WHERE id = ?",
                           (mid,)).fetchone()[0] == 1
    finally:
        con.close()


def _run_purge(mid, extra_env=None):
    env = _env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), "purge", "--id", mid],
        capture_output=True, text=True, env=env, timeout=600,
        cwd=str(REPO_ROOT))


def _make_glob_recorder():
    """Return (state, plain function) recording Path.glob pattern order.

    The recorder must be a plain FUNCTION so the descriptor protocol binds
    it like a method (a callable OBJECT installed as a class attribute does
    not receive the instance, losing the self/pattern pairing).
    """
    state = {"patterns": [], "orig": Path.glob}

    def _recorder(self_path, pattern):
        state["patterns"].append(pattern)
        return state["orig"](self_path, pattern)

    return state, _recorder


class PatternParityTest(unittest.TestCase):
    """Registry values are exactly the scrubber's historical family globs."""

    def test_registry_patterns_match_historical_literals(self):
        _purge_storelib_modules()
        from storelib import purge as purge_mod
        try:
            reg = purge_mod.SUPPORTED_DELIVERY_FAMILIES
            self.assertEqual(sorted(reg.values()),
                             sorted(EXPECTED_PATTERNS))
            self.assertEqual(sorted(reg.keys()),
                             sorted(EXPECTED_FAMILY_ORDER))
        finally:
            _purge_storelib_modules()


class SequencePinTest(unittest.TestCase):
    """Default enumeration order == the historical 8-phase scrub order."""

    def test_default_glob_sequence_is_historical_order(self):
        _purge_storelib_modules()
        ops = _SCRATCH / "ops"
        ops.mkdir(exist_ok=True)
        # Empty-but-existing ops dir: every family phase still consults its
        # glob; the recorded sequence is the phase order itself.
        from storelib import purge as purge_mod
        try:
            rec, recorder = _make_glob_recorder()
            with mock.patch.object(Path, "glob", recorder):
                purge_mod.scrub_ledgers(_SCRATCH, set(), [])
            self.assertEqual(rec["patterns"], EXPECTED_PATTERNS)
        finally:
            _purge_storelib_modules()


class SeamOrderingTest(unittest.TestCase):
    """Reduced seam skips the family's phase without reordering the rest."""

    def test_reduced_seam_skips_phase_keeps_order(self):
        _purge_storelib_modules()
        ops = _SCRATCH / "ops"
        ops.mkdir(exist_ok=True)
        seg = ops / ("bb44cc55" + ".ledger.seg." + uuid.uuid4().hex)
        seg_bytes = b"envelope line\n"
        seg.write_bytes(seg_bytes)
        try:
            from storelib import purge as purge_mod
            reduced = [t for t in EXPECTED_FAMILY_ORDER
                       if t != "ledger.seg"]
            # Shuffled on purpose (review round 1 nit): an implementation
            # iterating the ENV token order would scrub in that order and
            # fail the expected-sequence assert below.
            shuffled = list(reversed(reduced))
            with mock.patch.dict(os.environ,
                                 {"ZMEM_TEST_LEDGER_FAMILIES":
                                  ",".join(shuffled)}):
                rec, recorder = _make_glob_recorder()
                with mock.patch.object(Path, "glob", recorder):
                    purge_mod.scrub_ledgers(_SCRATCH, set(), [])
                expected = [purge_mod.SUPPORTED_DELIVERY_FAMILIES[t]
                            for t in reduced]
                self.assertEqual(rec["patterns"], expected)
                # The removed family's file is untouched by scrub.
                self.assertEqual(seg.read_bytes(), seg_bytes)
        finally:
            seg.unlink(missing_ok=True)
            os.environ.pop("ZMEM_TEST_LEDGER_FAMILIES", None)
            _purge_storelib_modules()


class FaultInjectionTest(unittest.TestCase):
    """Inspection failures escape the helper and close the purge."""

    def test_listing_failure_escapes_helper(self):
        _purge_storelib_modules()
        # The ops dir MUST exist: an absent dir is legitimately "clean" and
        # never consults the listing primitive at all.
        (_SCRATCH / "ops").mkdir(exist_ok=True)
        from storelib import purge as purge_mod
        try:
            with mock.patch.object(
                    purge_mod.os, "scandir",
                    side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    purge_mod.unsupported_delivery_state_files(_SCRATCH)
        finally:
            _purge_storelib_modules()

    def test_purge_refuses_with_store_intact_on_listing_failure(self):
        _purge_storelib_modules()
        (_SCRATCH / "ops").mkdir(exist_ok=True)
        from storelib import purge as purge_mod
        try:
            mid = _add_row(content="p07supp faultinjection needle row")
            self.assertTrue(_row_exists(mid))
            with mock.patch.object(
                    purge_mod.os, "scandir",
                    side_effect=PermissionError("denied")):
                rc = purge_mod.cmd_purge(ids=[mid])
            self.assertEqual(rc, 5)
            self.assertTrue(_row_exists(mid))
        finally:
            _purge_storelib_modules()


class InvalidTokenTest(unittest.TestCase):
    """Bogus seam token: purge exit 2; doctor warn; never a traceback."""

    def _assert_exit_2(self, r, mid):
        self.assertEqual(r.returncode, 2, r.stderr[-400:])
        self.assertIn("bogus", r.stderr)
        self.assertTrue(_row_exists(mid))

    def test_purge_refuses_exit_2_naming_token(self):
        # Self-contained ops-present leg (review round 1: the shared
        # scratch's ops dir must not be an ordering dependency).
        (_SCRATCH / "ops").mkdir(exist_ok=True)
        mid = _add_row(content="p07supp invalidtoken needle row")
        self.assertTrue(_row_exists(mid))
        self._assert_exit_2(
            _run_purge(mid, {"ZMEM_TEST_LEDGER_FAMILIES": "bogus"}), mid)

    def test_purge_refuses_exit_2_with_ops_absent(self):
        # Ops-absent leg (review round 1 MAJOR): the seam must validate
        # BEFORE the absent-dir early return, or the token error surfaces
        # post-deletion inside scrub_ledgers (exit 5, row gone) instead of
        # the pre-transaction exit 2.
        fresh = Path(tempfile.mkdtemp(prefix="zmem-p07-tok-"))
        try:
            r = subprocess.run(
                [sys.executable, "-X", "utf8", str(STORE_PY), "add",
                 "--namespace", "project:p07supp", "--type", "fact",
                 "--content", "p07supp invalidtoken absent needle row",
                 "--tags", "supp", "--json"],
                capture_output=True, text=True,
                env=_env({"ZMEM_STORE": str(fresh / "store.sqlite"),
                          "ZMEM_DATA": str(fresh)}),
                timeout=180, cwd=str(REPO_ROOT))
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            mid = json.loads(r.stdout)["id"]
            ops = fresh / "ops"
            if ops.exists():
                (ops / "store-open.jsonl").unlink(missing_ok=True)
                ops.rmdir()
            self.assertFalse(ops.exists(),
                             "fixture must start with no ops dir")
            pr = subprocess.run(
                [sys.executable, "-X", "utf8", str(STORE_PY), "purge",
                 "--id", mid],
                capture_output=True, text=True,
                env=_env({"ZMEM_STORE": str(fresh / "store.sqlite"),
                          "ZMEM_DATA": str(fresh),
                          "ZMEM_TEST_LEDGER_FAMILIES": "bogus"}),
                timeout=600, cwd=str(REPO_ROOT))
            self.assertEqual(pr.returncode, 2, pr.stderr[-400:])
            self.assertIn("bogus", pr.stderr)
            self.assertTrue(_row_exists(mid, fresh / "store.sqlite"))
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

    def test_doctor_warns_naming_misconfiguration(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(DOCTOR_PY),
             "--format", "json"],
            capture_output=True, text=True,
            env=_env({"ZMEM_TEST_LEDGER_FAMILIES": "bogus"}),
            timeout=600, cwd=str(REPO_ROOT))
        # doctor's EXIT CODE mirrors overall install health and legitimately
        # varies with the environment (sandboxed HOME fails install checks);
        # the contract under test is the JSON report's skew-warn check, so
        # parse the report regardless of rc (rc==2 usage errors still fail
        # the json.loads below with a clear error).
        doc = json.loads(r.stdout)
        matches = [c for c in doc.get("checks", [])
                   if "delivery-state-skew" in str(c.get("id", "")).lower()
                   or ("delivery-state" in json.dumps(c).lower()
                       and "skew" in json.dumps(c).lower())]
        self.assertTrue(matches, "expected a skew warn check")
        blob = json.dumps(matches).lower()
        self.assertIn("zmem_test_ledger_families", blob)
        self.assertIn("bogus", blob)


class SeamGatedPurgeTest(unittest.TestCase):
    """Known segment family under an excluding seam: exit 5, store intact."""

    def test_known_segment_excluded_by_seam_refuses(self):
        mid = _add_row(content="p07supp seamgate needle row")
        self.assertTrue(_row_exists(mid))
        ops = _SCRATCH / "ops"
        ops.mkdir(exist_ok=True)
        seg = ops / ("cc55dd66" + ".ledger.seg." + uuid.uuid4().hex)
        seg.write_text(
            "delivery envelope p07supp seamgate needle row\n",
            encoding="utf-8")
        try:
            r = _run_purge(mid, {"ZMEM_TEST_LEDGER_FAMILIES": "ledger"})
            self.assertEqual(r.returncode, 5, r.stderr[-400:])
            self.assertTrue(_row_exists(mid))
            self.assertIn("cc55dd66.ledger.seg.", r.stderr)
        finally:
            seg.unlink(missing_ok=True)


class RetryPreflightTest(unittest.TestCase):
    """Empty-chain scrub retry still hits the preflight first."""

    def test_denylisted_retry_with_unsupported_files_refuses(self):
        mid = _add_row(content="p07supp retrypreflight needle row")
        # First purge succeeds cleanly (no unsupported files yet).
        r = _run_purge(mid)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertFalse(_row_exists(mid))
        ops = _SCRATCH / "ops"
        ops.mkdir(exist_ok=True)
        bad = ops / ("dd66ee77" + ".ledger.seg2." + uuid.uuid4().hex)
        bad.write_text("retry p07supp retrypreflight needle row\n",
                       encoding="utf-8")
        try:
            # Re-run: the id is now deny-listed, the chain is empty — the
            # scrub-retry path. The preflight must refuse before any
            # cleanup phase touches the ops dir.
            r2 = _run_purge(mid)
            self.assertEqual(r2.returncode, 5, r2.stderr[-400:])
            self.assertIn("does not support", r2.stderr)
            self.assertTrue(bad.exists(),
                            "retry cleanup must not run past the preflight")
        finally:
            bad.unlink(missing_ok=True)


def _run_filtered(argv):
    module = sys.modules[__name__]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for spec in argv:
        class_name, dot, method = spec.partition(".")
        cls = getattr(module, class_name, None)
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
            print("unknown test class: " + class_name, file=sys.stderr)
            return 2
        if dot:
            if not hasattr(cls, method):
                print("unknown test method: " + spec, file=sys.stderr)
                return 2
            suite.addTest(cls(method))
        else:
            suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    _argv = sys.argv[1:]
    if _argv:
        raise SystemExit(_run_filtered(_argv))
    unittest.main(verbosity=2)
