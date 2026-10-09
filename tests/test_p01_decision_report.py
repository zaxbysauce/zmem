"""P01 acceptance checks: `store.py report --decision-log <path> [--json]`.

A NEW read-only reporting subcommand that aggregates the decision log
(``zmem-decisions.log``, rotated segments included) against the store. Five
executable acceptance criteria, each seeded through the REAL CLI into a
throwaway temp store and driven purely via subprocess — storelib is NEVER
imported in this process (it freezes STORE_PATH from ambient env at import
time), so the operator's real store cannot be touched:

  AC1 DecisionReportTest.test_repeat_count_per_row_id
      — ``rows[<id>]`` counts repeat deliveries per row id: ``delivered`` is
        the total delivery count across every parsed line, ``sessions`` the
        number of distinct ``sid=`` sessions that delivered the row, and
        ``max_per_session`` the busiest single session's delivery count.
  AC2 DecisionReportTest.test_delivered_and_withheld_by_tier_and_moment
      — ``by_tier_moment[<tier>][<moment>]`` splits each decision line into
        ``delivered`` (the ``ids=[...]`` entries) and ``withheld``
        (the ``all=[...]`` entries minus the ``ids=[...]`` entries); the tier
        derives from the row's stored namespace — ``project:*`` -> project,
        ``user:global`` -> global, an id absent from the store -> unknown.
  AC3 DecisionReportTest.test_crlf_and_rotated_segment_are_counted
      — deliveries split across a rotated ``<log>.1`` segment and the active
        log, BOTH files with CRLF line endings, still count. Log files are
        written with explicit bytes (``open(path, "wb")`` + ``\\r\\n``) so the
        fixture is byte-identical on the Windows and Linux CI legs.
  AC4 DecisionReportTest.test_hostile_moment_is_sanitized_bucket
      — a hostile ``moment=`` value can never become a report key or forge
        another field: the bucket is the sanitized literal, per the canonical
        rule in storelib/false_inject.py ``_moment_of``
        (``re.sub(r"[^A-Za-z0-9._-]", "_", moment.strip())[:32]``).
  AC5 DecisionReportTest.test_report_is_read_only
      — a whole-directory snapshot (every file's name AND bytes, recursive)
        taken before and after the report run compares equal: no existing
        file changes a byte, no new file appears, none disappears.

Decision-line shape synthesized here (the real writer's, as parsed by
storelib/miss_rate.py ``_BG_LINE_RE``):

  [<ts>] zmem-hook status=<s> reason=<r> ids=['<id>', ...] all=['<id>', ...] sid=<sid> moment=<m>

At base commit cb82ecf (0.89.0) the subcommand does not exist, so every test
fails at its FIRST assertion — argparse exit 2, ``invalid choice: 'report'``.
That RED state is the point of this file: it is the frozen acceptance
contract for the implementer. Do not weaken.

Run: python tests/test_p01_decision_report.py   (no pytest required)
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
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PY = sys.executable

_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_CONVENTION_INTERVAL",
    "ZMEM_SESSION", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    # Rotation knobs: a developer shell exporting a tiny/huge cap must not
    # steer child-process rotation.
    "ZMEM_BG_LOG_MAX_BYTES", "ZMEM_LOG_ROTATIONS",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _ids_literal(ids) -> str:
    """Python-repr id list, byte-shaped like the real writer's ids=[...]."""
    return "[" + ", ".join(repr(str(i)) for i in ids) + "]"


def _decision_line(ts: int, ids, all_ids, sid: str, moment: str,
                   status: str = "injected",
                   reason: str = "injected") -> str:
    """One decision line in the real writer's shape (see _BG_LINE_RE)."""
    return (f"[{ts}] zmem-hook status={status} reason={reason} "
            f"ids={_ids_literal(ids)} all={_ids_literal(all_ids)} "
            f"sid={sid} moment={moment}")


class DecisionReportTest(unittest.TestCase):
    """P01 contract: the decision-log report subcommand, end to end.

    setUp seeds exactly two rows via the real CLI — P in ``project:p01``
    (tier "project") and G in ``user:global`` (tier "global") — capturing
    both ids from the add JSON envelope. Every test then synthesizes a
    decision log at ``<tmp>/zmem-decisions.log`` and drives
    ``store.py report --decision-log <log> --json`` in a subprocess.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p01-")
        self.env = _clean_env(self.tmp)
        self.log = os.path.join(self.tmp, "zmem-decisions.log")
        self.pid = self._add_row(
            "project:p01",
            "p01 report canary: git stash pop conflicts need stash drop "
            "after resolve, verified by test")
        self.gid = self._add_row(
            "user:global",
            "p01 report global canary: the release checklist lives in the "
            "runbook repo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture helpers ---------------------------------------------------

    def _run_cli(self, *args):
        return subprocess.run(
            [PY, str(STORE_PY), *args],
            cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=120,
        )

    def _add_row(self, namespace: str, content: str) -> str:
        """Seed one gate-passing row via the real CLI; return its id."""
        r = self._run_cli("add", "--namespace", namespace, "--type", "lesson",
                          "--content", content, "--signal", "test",
                          "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _write_log(self, lines) -> None:
        # Byte-explicit LF text with a trailing newline — never
        # Path.write_text, so the on-disk bytes are identical on every CI leg.
        with open(self.log, "wb") as fh:
            fh.write("".join(ln + "\n" for ln in lines).encode("utf-8"))

    @staticmethod
    def _write_crlf(path: str, lines) -> None:
        # Byte-explicit CRLF text — the fixture must carry real \r\n bytes,
        # not whatever newline translation a text-mode write would apply.
        with open(path, "wb") as fh:
            fh.write("".join(ln + "\r\n" for ln in lines).encode("utf-8"))

    def _run_report(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(STORE_PY), "report", "--decision-log", self.log,
             "--json"],
            cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=60,
        )

    def _snapshot_dir(self) -> dict:
        """Whole-directory surface snapshot: relative name -> bytes for
        EVERY file under the temp data dir, recursive (subdirs included).
        Comparing two such dicts proves BOTH halves of read-only-ness at
        once: a byte change in any existing file flips a value, and a NEW
        file adds a key (a removed file drops one)."""
        snap = {}
        for dirpath, _subdirs, filenames in os.walk(self.tmp):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.tmp).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    snap[rel] = fh.read()
        return snap

    # -- AC1 ---------------------------------------------------------------

    def test_repeat_count_per_row_id(self):
        # Row P delivered 3x in session sess-a and once in sess-b; row G
        # delivered once in sess-b. Per-row aggregates must dedupe nothing:
        # delivered counts every line, sessions counts distinct sids,
        # max_per_session is the single busiest session's count.
        lines = [
            _decision_line(1740000000 + i, [self.pid], [self.pid],
                           "sess-a", "user_prompt")
            for i in range(3)
        ]
        lines.append(_decision_line(
            1740000100, [self.pid], [self.pid], "sess-b", "user_prompt"))
        lines.append(_decision_line(
            1740000101, [self.gid], [self.gid], "sess-b", "user_prompt"))
        self._write_log(lines)

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)["rows"]
        self.assertEqual(
            (rows[self.pid]["delivered"], rows[self.pid]["sessions"],
             rows[self.pid]["max_per_session"], rows[self.gid]["delivered"]),
            (4, 2, 3, 1),
            "rows[<id>] must report delivered=total lines carrying the id, "
            "sessions=distinct sid count, max_per_session=busiest session")

    # -- AC2 ---------------------------------------------------------------

    def test_delivered_and_withheld_by_tier_and_moment(self):
        # One pretool decision: P (project:*) delivered, while G (user:global)
        # and an id absent from the store were candidates but withheld
        # (all minus ids). Each id lands in the tier derived from its STORED
        # namespace — absent ids are "unknown".
        absent = "ffffffff-ffff-4fff-8fff-ffffffffffff"  # never seeded
        self._write_log([
            _decision_line(1740000200, [self.pid],
                           [self.pid, self.gid, absent],
                           "sess-tier", "pretool"),
        ])

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        btm = json.loads(r.stdout)["by_tier_moment"]
        self.assertEqual(
            (btm["project"]["pretool"]["delivered"],
             btm["global"]["pretool"]["withheld"],
             btm["unknown"]["pretool"]["withheld"]),
            (1, 1, 1),
            "by_tier_moment[<tier>][<moment>] must count delivered (ids) "
            "and withheld (all minus ids) per tier derived from the row's "
            "stored namespace; store-absent ids bucket to unknown")

    # -- AC3 ---------------------------------------------------------------

    def test_crlf_and_rotated_segment_are_counted(self):
        # P delivered once in the rotated <log>.1 segment and once in the
        # active log — both files CRLF-terminated. The report must read the
        # whole rotation family and parse CRLF lines in both segments.
        self._write_crlf(self.log + ".1", [
            _decision_line(1740000300, [self.pid], [self.pid],
                           "sess-crlf", "pretool"),
        ])
        self._write_crlf(self.log, [
            _decision_line(1740000301, [self.pid], [self.pid],
                           "sess-crlf", "pretool"),
        ])

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)["rows"]
        self.assertEqual(
            rows[self.pid]["delivered"], 2,
            "deliveries split across a rotated .1 segment and the active "
            "log, both CRLF-terminated, must all be counted")

    # -- AC4 ---------------------------------------------------------------

    def test_hostile_moment_is_sanitized_bucket(self):
        # A hostile moment value is data, never structure: the raw string
        # must NOT appear as a by_tier_moment key; the bucket is the
        # sanitized literal evil___status_silent (each of ( ) + = -> one
        # underscore, per storelib/false_inject.py _moment_of, cap 32).
        hostile = "evil(+)status=silent"
        self._write_log([
            _decision_line(1740000400, [self.pid], [self.pid],
                           "sess-hostile", hostile),
        ])

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        project = json.loads(r.stdout)["by_tier_moment"]["project"]
        self.assertEqual(
            (hostile in project, "evil___status_silent" in project),
            (False, True),
            "the hostile moment must not surface as a report key; only its "
            "sanitized form may bucket (re.sub(r'[^A-Za-z0-9._-]', '_', "
            "moment.strip())[:32])")

    # -- AC5 ---------------------------------------------------------------

    def test_report_is_read_only(self):
        # The report leaves EVERY file in the data dir byte-identical and
        # creates no new file (nor removes one): a whole-directory snapshot
        # (name -> bytes, recursive) before/after must compare equal. The
        # decision line exercises both the log read and the store tier
        # lookup so neither path may write (no surfaced_count bump, no
        # ledger, no -wal/-shm/-journal sidecar creation, nothing).
        self._write_log([
            _decision_line(1740000500, [self.pid],
                           [self.pid, self.gid], "sess-ro", "user_prompt"),
        ])
        before = self._snapshot_dir()

        r = self._run_report()
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self._snapshot_dir()
        self.assertEqual(
            after, before,
            "the report is read-only: every file in the data dir must stay "
            "byte-identical and no new file may appear (dict equality "
            "catches changed bytes, added files, and removed files alike); "
            "diff keys: added=%s removed=%s changed=%s" % (
                sorted(set(after) - set(before)),
                sorted(set(before) - set(after)),
                sorted(k for k in set(before) & set(after)
                       if before[k] != after[k])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
