"""In-process unit pins for `decision_report` internals.

These close three fixes that the mutation battery proved UNPINNED: reverting
any of them left every end-to-end suite green, because each is reachable only
through a failure the CLI fixtures cannot provoke reliably (a chunked query
that raises after a successful verify query, an unwritable temp directory, a
staging directory that refuses removal).

In-process import is safe HERE and only here, because the environment is
pinned to a throwaway store at module scope BEFORE storelib is imported --
`storelib.schema` freezes `STORE_PATH` from the ambient environment at first
import, and an unpinned import would resolve the operator's real store.

Run: python tests/test_p01_report_internals.py   (no pytest required)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# --- MUST precede every storelib import -----------------------------------
_PIN = tempfile.mkdtemp(prefix="zmem-p01-internals-")
os.environ["ZMEM_STORE"] = os.path.join(_PIN, "store.sqlite")
os.environ["ZMEM_DATA"] = _PIN
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))

from storelib import decision_report as DR  # noqa: E402


class _ExplodingConn:
    """A connection whose verify query succeeds but whose chunked lookup
    raises -- exactly the shape that made a corrupt store report every real
    row as 'unknown' at exit 0 with no caveat."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, *args):
        self.calls.append(" ".join(sql.split())[:60])
        if "LIMIT 1" in sql:
            return _Rows([("id", "user:global")])
        if "IN (" in sql:
            raise sqlite3.OperationalError("database disk image is malformed")
        return _Rows([])


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class NamespaceTierFailureTest(unittest.TestCase):

    def test_failed_chunk_is_counted_and_reported(self):
        tiers, failed, first_error = DR.namespace_tiers(
            _ExplodingConn(), ["a", "b"])
        self.assertEqual(failed, 1,
                         "a chunk whose lookup raised must be counted")
        self.assertIn("malformed", first_error or "")
        self.assertEqual(set(tiers), {"a", "b"})

    def test_failed_chunk_surfaces_as_a_caveat_in_the_report(self):
        # End to end through the real builder, with the store read forced to
        # fail its chunked lookup. A silent 'unknown' here is indistinguishable
        # from a genuinely absent row, which is the whole defect.
        self.assertTrue(
            hasattr(DR.namespace_tiers, "__call__"),
            "namespace_tiers must stay callable")
        original = DR.namespace_tiers

        def boom(conn, ids):
            tiers = {i: DR.TIER_UNKNOWN for i in ids}
            return tiers, 2, "synthetic read failure"

        DR.namespace_tiers = boom
        try:
            tmp = tempfile.mkdtemp(prefix="zmem-p01-ns-")
            try:
                log = os.path.join(tmp, "zmem-decisions.log")
                with open(log, "w", encoding="utf-8") as fh:
                    fh.write("[1740003000] zmem-hook status=injected "
                             "reason=injected ids=['x'] all=['x'] sid=s "
                             "moment=pretool\n")
                report = DR.build_decision_report(log, tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            DR.namespace_tiers = original
        self.assertTrue(
            any("chunk" in c for c in report["caveats"]),
            "a failed tier lookup must reach the disclosure array; caveats "
            "were %r" % report["caveats"])
        self.assertTrue(
            any("synthetic read failure" in c for c in report["caveats"]),
            "the caveat must name the underlying error")


class StagingFailureTest(unittest.TestCase):

    def test_mkdtemp_failure_is_a_report_error_not_a_traceback(self):
        # A full disk or unwritable TMPDIR must surface as the contracted
        # refusal. Outside the try, it escaped as a raw OSError traceback at
        # exit 1 while the copy2 failure in the same function was pinned.
        original = DR.tempfile.mkdtemp

        def boom(*a, **kw):
            raise OSError(28, "No space left on device")

        DR.tempfile.mkdtemp = boom
        try:
            with self.assertRaises(DR.ReportError) as ctx:
                DR._stage_copy(Path("store.sqlite"))
        finally:
            DR.tempfile.mkdtemp = original
        self.assertIn("cannot create a staging directory", str(ctx.exception))

    def test_leftover_staging_directory_is_reported(self):
        # rmtree(ignore_errors=True) cannot distinguish removed from silently
        # refused, so a full copy of the operator's store was left behind with
        # no warning at all.
        tmp = tempfile.mkdtemp(prefix="zmem-p01-leftover-")
        original = DR.shutil.rmtree
        DR.shutil.rmtree = lambda *a, **kw: None  # removal silently fails
        try:
            warning = DR._discard_stage(tmp)
        finally:
            DR.shutil.rmtree = original
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIsNotNone(
            warning,
            "a staging directory that survived removal must be reported")
        self.assertIn(tmp, warning)
        self.assertIn("delete that directory by hand", warning)

    def test_clean_removal_returns_no_warning(self):
        tmp = tempfile.mkdtemp(prefix="zmem-p01-clean-")
        try:
            self.assertIsNone(DR._discard_stage(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_leftover_on_the_refusal_path_is_reported(self):
        # A refusal has no `caveats` channel, so the success-path warning alone
        # left a leftover unreported on the failure path -- the very case where
        # a full copy of the operator's store is most likely to be stranded.
        tmp = tempfile.mkdtemp(prefix="zmem-p01-refuseleak-")
        store = Path(tmp) / "store.sqlite"
        store.write_bytes(b"not a database" * 200)
        Path(str(store) + "-wal").write_bytes(b"")   # force the staged path
        original_rmtree = DR.shutil.rmtree
        DR.shutil.rmtree = lambda *a, **kw: None      # removal silently fails
        try:
            with self.assertRaises(DR.ReportError) as ctx:
                DR._stage_copy(store)
        finally:
            DR.shutil.rmtree = original_rmtree
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("could not be removed", str(ctx.exception),
                      "a staging directory that survived cleanup must be "
                      "carried on the refusal itself, not discarded")

    def test_discard_of_nothing_is_safe(self):
        self.assertIsNone(DR._discard_stage(None))


class RenderingEscapeTest(unittest.TestCase):

    def test_control_characters_are_escaped(self):
        self.assertEqual(DR._escape_rendered("a\x1bb"), "a\\x1bb")
        self.assertEqual(DR._escape_rendered("a\tb\rc\nd"),
                         "a\\tb\\rc\\nd")
        self.assertEqual(DR._escape_rendered("plain-id"), "plain-id")

    def test_sid_helper_matches_the_writers_convention(self):
        self.assertTrue(DR._counts_as_session("sess-a"))
        self.assertFalse(DR._counts_as_session("unknown"))
        self.assertFalse(DR._counts_as_session(""))
        self.assertFalse(DR._counts_as_session(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)