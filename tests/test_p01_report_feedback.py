"""Feedback-round regression coverage for `store.py report` (issue #249).

Added after swarm-pr-review and the PR's own review threads. Every test here
corresponds to a finding that a mutation or an execution probe proved was real
and UNPINNED: with the corresponding fix reverted, this file (and the shipped
suite) stayed green.

Not part of the frozen checkpoint manifest. The frozen contract remains
`tests/test_p01_decision_report.py`, byte-identical to its pinned blob.

Grouped by the defect class each one closes:

  * silent wrong answers -- an unreadable log, a non-delivery line, and an
    oversized numeric field each produced an exit-0 report indistinguishable
    from a genuinely quiet window.
  * metric corruption -- the writers' `sid=unknown` placeholder was pooled as
    a real session, inflating the headline per-session repeat rate.
  * terminal safety -- row ids reach a human-readable table unsanitized while
    `moment` was already sanitized.
  * staging hygiene -- a read-only store leaked a full staged copy on a
    SUCCESSFUL run because copy2 preserved the read-only bit.
  * uncovered surfaces -- the default (non-JSON) output had zero coverage, and
    several arithmetic details were never asserted.

Run: python tests/test_p01_report_feedback.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import sqlite3
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
    "ZMEM_BG_LOG_MAX_BYTES", "ZMEM_LOG_ROTATIONS",
)


def _clean_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _ids_literal(ids) -> str:
    return "[" + ", ".join(repr(str(i)) for i in ids) + "]"


def _line(ts, ids, all_ids, sid, moment, status="injected", reason="injected",
          omitted=None):
    """One decision line in the real writer's shape (see _BG_LINE_RE).

    ``omitted`` is rendered in its CANONICAL position -- between reason= and
    ids= -- because appending it at the end does not match the end-anchored
    regex and would be skipped as a non-matching line rather than exercising
    the oversized-integer path at all.
    """
    head = "[%d] zmem-hook status=%s reason=%s" % (ts, status, reason)
    if omitted is not None:
        head += " omitted=%s" % omitted
    return ("%s ids=%s all=%s sid=%s moment=%s"
            % (head, _ids_literal(ids), _ids_literal(all_ids), sid, moment))


def _stage_dirs() -> set:
    root = tempfile.gettempdir()
    try:
        return {n for n in os.listdir(root)
                if n.startswith("zmem-report-stage-")}
    except OSError:
        return set()


class FeedbackCase(unittest.TestCase):
    """Throwaway store seeded through the real CLI; subprocess only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-p01-fb-")
        self.env = _clean_env(self.tmp)
        self.log = os.path.join(self.tmp, "zmem-decisions.log")
        self.pid = self._add_row(
            "project:p01fb", "feedback canary: project-scoped report row")
        self.gid = self._add_row(
            "user:global", "feedback canary: globally scoped report row")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, *args, timeout=120):
        return subprocess.run(
            [PY, str(STORE_PY), *args], cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=timeout)

    def _add_row(self, namespace, content):
        r = self._run_cli("add", "--namespace", namespace, "--type", "lesson",
                          "--content", content, "--signal", "test",
                          "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["id"]

    def _write_log(self, lines, newline="\n", name=None) -> None:
        with open(name or self.log, "wb") as fh:
            fh.write("".join(ln + newline for ln in lines).encode("utf-8"))

    def _report(self, *extra, timeout=120):
        return subprocess.run(
            [PY, str(STORE_PY), "report", "--decision-log", self.log,
             *extra], cwd=str(REPO_ROOT), env=self.env,
            capture_output=True, text=True, timeout=timeout)

    def _report_json(self):
        r = self._report("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _make_residue(self):
        r = self._run_cli("source-exists", "--namespace", "project:p01fb",
                          "--source-ref", "feedback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(Path(str(Path(self.tmp) / "store.sqlite") + "-wal").exists())


# ------------------------------------------------- silent wrong answers


class SilentAnswerTest(FeedbackCase):

    def test_missing_decision_log_is_disclosed_not_silently_empty(self):
        # A typo'd --decision-log resolved to a report with zero lines and no
        # caveat at all -- byte-identical to a genuinely decision-free window.
        missing = os.path.join(self.tmp, "typo.log")
        r = self._run_cli("report", "--decision-log", missing, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["decision_lines"], 0)
        self.assertTrue(
            any("no decision log at" in c for c in doc["caveats"]),
            "a missing log must be disclosed; caveats were %r" % doc["caveats"])

    def test_empty_decision_log_is_disclosed(self):
        self._write_log([])
        doc = self._report_json()
        self.assertEqual(doc["decision_lines"], 0)
        self.assertTrue(
            any("contained no decision lines" in c for c in doc["caveats"]),
            "an empty log must be disclosed; caveats were %r" % doc["caveats"])

    def test_oversized_numeric_field_does_not_drop_the_whole_log(self):
        # Python 3.11+ caps decimal->int conversion and raises ValueError past
        # it. The int() calls were unguarded, so ONE oversized omitted= made
        # parse_bg_log raise -- defeating its documented "never raises"
        # contract -- and the report exited 0 having silently discarded EVERY
        # valid line in the log, not just the bad one.
        huge = "9" * 5000
        self._write_log([
            _line(1740002000, [self.pid], [self.pid], "sess-good", "pretool"),
            _line(1740002001, [self.pid], [self.pid], "sess-bad", "pretool",
                  omitted=huge),
        ])
        doc = self._report_json()
        self.assertEqual(doc["decision_lines"], 2,
                         "the oversized field must not cost the valid line")
        self.assertEqual(doc["rows"][self.pid]["delivered"], 2)

    def test_oversized_ops_and_exc_also_survive(self):
        huge = "9" * 5000
        for field in ("ops", "exc"):
            with self.subTest(field=field):
                log = os.path.join(self.tmp, "big-%s.log" % field)
                self._write_log([
                    _line(1740002100, [self.pid], [self.pid],
                          "sess-good", "pretool"),
                    ("[%d] zmem-hook status=injected reason=injected ids=%s "
                     "all=%s %s=%s sid=sess-bad moment=pretool"
                     % (1740002101, _ids_literal([self.pid]),
                        _ids_literal([self.pid]), field, huge)),
                ], name=log)
                r = self._run_cli("report", "--decision-log", log, "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                doc = json.loads(r.stdout)
                self.assertEqual(
                    doc["rows"][self.pid]["delivered"], 2,
                    "an oversized %s= must not cost the whole log" % field)

    def test_oversized_tokens_field_does_not_drop_the_whole_log(self):
        # The sibling fields were guarded but tokens= was not, and isdigit()
        # does NOT cap length -- so a long digit string still raised out of a
        # parser that documents itself as never raising, costing every consumer
        # the whole log. Found by the independent reviewer, not by the first
        # round of probes.
        huge = "9" * 5000
        self._write_log([
            _line(1740002050, [self.pid], [self.pid], "sess-good", "pretool"),
            ("[%d] zmem-hook status=injected reason=injected ids=%s all=%s "
             "tokens=%s/%s sid=sess-bad moment=pretool"
             % (1740002051, _ids_literal([self.pid]),
                _ids_literal([self.pid]), huge, huge)),
        ])
        doc = self._report_json()
        self.assertEqual(doc["decision_lines"], 2,
                         "the oversized tokens= must not cost the valid line")
        self.assertEqual(doc["rows"][self.pid]["delivered"], 2)

    def test_non_delivery_line_ids_are_not_counted_as_deliveries(self):
        # A legacy or mixed-version envelope can carry ids on a status=silent
        # line. Counting those reports deliveries that never happened, and
        # feeds the headline repeat rate.
        self._write_log([
            _line(1740002200, [self.pid], [self.pid], "sess-s", "pretool",
                  status="silent", reason="budget"),
        ])
        doc = self._report_json()
        self.assertNotIn(self.pid, doc["rows"],
                         "a status=silent line delivered nothing, so its ids "
                         "must not appear in rows[]")
        self.assertTrue(
            any("not delivery lines" in c for c in doc["caveats"]),
            "skipped non-delivery lines must be disclosed; caveats were %r"
            % doc["caveats"])

    def test_ids_on_a_status_silent_line_are_still_counted_as_candidates(self):
        # The withheld side is about what was OFFERED, so a silent line's
        # candidate pool is still composition evidence even though its ids are
        # not deliveries.
        other = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self._write_log([
            _line(1740002250, [], [other], "sess-s", "pretool",
                  status="silent", reason="budget"),
        ])
        doc = self._report_json()
        self.assertEqual(doc["by_tier_moment"]["unknown"]["pretool"]["withheld"], 1)


# ----------------------------------------------------- metric corruption


class SessionSentinelTest(FeedbackCase):

    def test_unknown_sid_placeholder_is_not_a_session(self):
        # Every host that supplies no session id makes the writers emit
        # sid=unknown. Pooling those into one bucket invents a session and
        # attributes unrelated deliveries to it, inflating max_per_session --
        # the headline number this whole report exists to produce.
        self._write_log(
            [_line(1740002300 + i, [self.pid], [self.pid], "unknown", "pretool")
             for i in range(3)]
            + [_line(1740002310, [self.pid], [self.pid], "sess-real", "pretool")]
        )
        row = self._report_json()["rows"][self.pid]
        self.assertEqual(
            (row["delivered"], row["sessions"], row["max_per_session"]),
            (4, 1, 1),
            "the 'unknown' placeholder is not a session; only sess-real counts")

    def test_unknown_sid_deliveries_are_disclosed_as_sessionless(self):
        self._write_log([
            _line(1740002320, [self.pid], [self.pid], "unknown", "pretool"),
        ])
        doc = self._report_json()
        self.assertTrue(
            any("no usable sid" in c for c in doc["caveats"]),
            "placeholder-sid deliveries must be disclosed; caveats were %r"
            % doc["caveats"])

    def test_absent_sid_is_still_sessionless(self):
        self._write_log([
            "[1740002330] zmem-hook status=injected reason=injected ids=%s "
            "all=%s moment=pretool"
            % (_ids_literal([self.pid]), _ids_literal([self.pid])),
        ])
        row = self._report_json()["rows"][self.pid]
        self.assertEqual((row["delivered"], row["sessions"]), (1, 0))

    def test_third_namespace_tier_falls_through_to_unknown(self):
        # The else-branch of the tier classifier had no coverage at all.
        r = self._run_cli("add", "--namespace", "domain:acme", "--type",
                          "lesson", "--content",
                          "feedback canary: a domain-scoped row exercises the "
                          "non-project, non-global tier branch",
                          "--signal", "test", "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        did = json.loads(r.stdout)["id"]
        self._write_log([
            _line(1740002340, [did], [did], "sess-t", "pretool"),
        ])
        self.assertEqual(self._report_json()["rows"][did]["tier"], "unknown")


# ------------------------------------------------------- terminal safety


class TerminalEscapeTest(FeedbackCase):

    HOSTILE = "\x1b[31mEVIL\x1b[0m"

    def test_hostile_row_id_is_escaped_in_the_default_text_output(self):
        # moment is sanitized because it becomes a report KEY; a row id was
        # printed raw, so a hand-edited log could drive the operator's terminal.
        self._write_log([
            _line(1740002400, [self.HOSTILE], [self.HOSTILE], "sess-e", "pretool"),
        ])
        r = self._report()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\x1b", r.stdout,
                         "the human table must not emit raw ESC bytes")
        self.assertIn("EVIL", r.stdout, "the id must still be readable")

    def test_json_mode_keeps_the_row_id_byte_exact(self):
        # Deliberately NOT escaped in JSON: these are machine keys a consumer
        # joins on, and json.dumps escapes control characters itself.
        self._write_log([
            _line(1740002410, [self.HOSTILE], [self.HOSTILE], "sess-e", "pretool"),
        ])
        doc = self._report_json()
        self.assertIn(self.HOSTILE, doc["rows"],
                      "the JSON surface must keep ids byte-exact")


# -------------------------------------------------------- staging hygiene


class StagingHygieneTest(FeedbackCase):

    def test_read_only_store_does_not_leak_a_staged_copy_on_success(self):
        # copy2 preserves mode bits, so a read-only store produced a read-only
        # staged copy that rmtree(ignore_errors=True) could not remove on
        # Windows -- leaking a full copy of the operator's store on a run that
        # exited 0 and reported success.
        store = Path(self.tmp) / "store.sqlite"
        self._write_log([
            _line(1740002500, [self.pid], [self.pid], "sess-ro", "pretool"),
        ])
        os.chmod(store, 0o444)
        self._make_residue()
        stage_root = tempfile.mkdtemp(prefix="zmem-fb-stagecheck-")
        env = dict(self.env, TMPDIR=stage_root, TMP=stage_root, TEMP=stage_root)
        try:
            before = set(os.listdir(stage_root))
            r = subprocess.run(
                [PY, str(STORE_PY), "report", "--decision-log", self.log,
                 "--json"], cwd=str(REPO_ROOT), env=env,
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            leaked = sorted(set(os.listdir(stage_root)) - before)
            self.assertEqual(
                leaked, [],
                "a successful run must not leave a staged copy of the "
                "operator's store behind")
        finally:
            try:
                os.chmod(store, 0o666)
            except OSError:
                pass
            shutil.rmtree(stage_root, ignore_errors=True)

    def test_success_path_removes_its_staging_directory(self):
        # The success path's rmtree was unpinned: deleting it leaked a staging
        # dir per run and every suite stayed green.
        self._write_log([
            _line(1740002510, [self.pid], [self.pid], "sess-s", "pretool"),
        ])
        self._make_residue()
        before = _stage_dirs()
        r = self._report()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(_stage_dirs() - before), [],
                         "the residue read path must clean up after itself")

    def test_rollback_journal_forces_the_staged_path(self):
        # immutable=1 ignores a hot rollback journal entirely, so it would
        # report the UNCOMMITTED pages of an in-flight transaction as real
        # rows. The journal must force the staged path, which copies it so
        # SQLite replays the rollback on the copy.
        self._write_log([
            _line(1740002520, [self.pid], [self.pid], "sess-j", "pretool"),
        ])
        journal = Path(str(Path(self.tmp) / "store.sqlite") + "-journal")
        journal.write_bytes(b"")
        doc = self._report_json()
        self.assertTrue(
            any("rollback journal" in c for c in doc["caveats"]),
            "an in-flight journal must be disclosed; caveats were %r"
            % doc["caveats"])
        self.assertEqual(doc["rows"][self.pid]["tier"], "project")
        self.assertTrue(journal.exists(),
                        "reading must not consume or delete the operator's "
                        "rollback journal")

    def test_store_unchanged_by_the_residue_path(self):
        self._write_log([
            _line(1740002530, [self.pid], [self.pid], "sess-r", "pretool"),
        ])
        self._make_residue()
        before = {}
        for dirpath, _sub, files in os.walk(self.tmp):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.tmp).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    before[rel] = fh.read()
        r = self._report()
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {}
        for dirpath, _sub, files in os.walk(self.tmp):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.tmp).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    after[rel] = fh.read()
        self.assertEqual(after, before,
                         "every file in the data dir must stay byte-identical")


# ----------------------------------------------------- uncovered surfaces


class DefaultSurfaceTest(FeedbackCase):
    """The default (non-JSON) output is what an operator actually sees, and
    it had ZERO coverage -- a mutation that printed the wrong numbers into the
    `sessions` slot left both shipped suites green."""

    def test_text_output_reports_the_row_and_its_numbers(self):
        self._write_log(
            [_line(1740002600 + i, [self.pid], [self.pid], "sess-a", "pretool")
             for i in range(3)]
            + [_line(1740002610, [self.pid], [self.pid], "sess-b", "pretool")]
        )
        r = self._report()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("delivered=4 sessions=2 max_per_session=3", r.stdout,
                      "the repeat-rate line must carry the real numbers; "
                      "got: %r" % r.stdout)

    def test_text_output_reports_the_tier_moment_rows(self):
        self._write_log([
            _line(1740002620, [self.pid], [self.pid], "sess-a", "pretool"),
            _line(1740002621, [self.pid], [self.gid], "sess-a", "pretool"),
        ])
        r = self._report()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("moment=pretool", r.stdout)
        self.assertIn("delivered=0 withheld=1", r.stdout,
                      "the global candidate withheld on line 2 must "
                      "appear: %r" % r.stdout)
        self.assertIn("caveats", r.stdout,
                      "the human surface must render the disclosure array")

    def test_text_output_states_the_timestamp_window(self):
        self._write_log([
            _line(1740002630, [self.pid], [self.pid], "sess-a", "pretool"),
            _line(1740002640, [self.pid], [self.pid], "sess-a", "pretool"),
        ])
        r = self._report()
        self.assertIn("timestamps: 1740002630..1740002640", r.stdout,
                      "a before/after comparison is only like-for-like if the "
                      "window is visible on the human surface")

    def test_default_log_path_resolution_without_the_flag(self):
        # Every shipped test passed --decision-log, so the documented default
        # resolution (and its precedence between the two log names) was never
        # executed.
        self._write_log([
            _line(1740002650, [self.pid], [self.pid], "sess-d", "pretool"),
        ])
        r = self._run_cli("report", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["decision_log"], self.log,
                         "with no --decision-log the data dir's "
                         "zmem-decisions.log must be used")

    def test_decisions_log_wins_over_the_legacy_name(self):
        self._write_log([
            _line(1740002660, [self.pid], [self.pid], "sess-d", "pretool"),
        ])
        self._write_log([
            _line(1740002661, [self.gid], [self.gid], "sess-d", "pretool"),
        ], name=os.path.join(self.tmp, "zmem-bg.log"))
        doc = json.loads(self._run_cli("report", "--json").stdout)
        self.assertEqual(doc["decision_log"], self.log)
        self.assertEqual(doc["rows"][self.pid]["delivered"], 1)


class JsonByteContractTest(FeedbackCase):

    def test_json_is_one_line_with_exactly_one_trailing_lf(self):
        # The shipped tests used text=True, which hides CRLF translation on
        # Windows -- dropping the trailing LF or switching to indent= passed.
        self._write_log([
            _line(1740002700, [self.pid], [self.pid], "sess-j", "pretool"),
        ])
        r = subprocess.run(
            [PY, str(STORE_PY), "report", "--decision-log", self.log, "--json"],
            cwd=str(REPO_ROOT), env=self.env, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"\r", r.stdout,
                         "the JSON document must be LF-only on every platform")
        self.assertEqual(r.stdout.count(b"\n"), 1,
                         "exactly one document, so exactly one newline")
        self.assertTrue(r.stdout.endswith(b"\n"))
        json.loads(r.stdout.decode("utf-8"))

    def test_refusal_under_json_still_emits_parseable_stdout(self):
        # A --json refusal used to emit nothing, so json.loads(r.stdout) raised
        # exactly when a caller most needed to read the error.
        Path(self.tmp, "store.sqlite").write_bytes(b"not a database" * 200)
        self._write_log([
            _line(1740002710, [self.pid], [self.pid], "sess-j", "pretool"),
        ])
        r = self._report("--json")
        self.assertEqual(r.returncode, 2)
        doc = json.loads(r.stdout)
        self.assertFalse(doc["ok"])
        self.assertIn("cannot read the store", doc["error"])
        self.assertIn("[zmem] report:", r.stderr,
                      "text-mode callers still get the human line")


class ArithmeticPinTest(FeedbackCase):

    def test_withheld_counts_once_per_line_and_adds_across_lines(self):
        # A mutation changing `miss[row_id] += 1` to `= 1` survived because no
        # fixture repeated a withheld id across lines.
        other = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self._write_log([
            _line(1740002800, [self.pid], [self.pid, other], "sess-w", "pretool"),
            _line(1740002801, [self.pid], [self.pid, other], "sess-w", "pretool"),
        ])
        btm = self._report_json()["by_tier_moment"]
        self.assertEqual(
            btm["unknown"]["pretool"]["withheld"], 2,
            "the same id withheld on two lines is two candidate opportunities")

    def test_intra_line_repeat_counts_once_in_the_tier_table(self):
        self._write_log([
            ("[%d] zmem-hook status=injected reason=injected ids=%s all=%s "
             "sid=sess-i moment=pretool"
             % (1740002810, _ids_literal([self.pid, self.pid]),
                _ids_literal([self.pid, self.pid]))),
        ])
        doc = self._report_json()
        self.assertEqual(doc["rows"][self.pid]["delivered"], 2)
        self.assertEqual(
            doc["by_tier_moment"]["project"]["pretool"]["delivered"], 2,
            "a repeat inside ONE line is one candidate but two delivery "
            "occurrences, and the two tables must still reconcile")
        self.assertEqual(doc["by_tier_moment"]["project"]["pretool"]["withheld"], 0)

    def test_tier_summed_delivered_reconciles_with_rows_delivered(self):
        absent = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self._write_log([
            _line(1740002820, [self.pid], [self.pid, absent], "sess-r", "pretool"),
            _line(1740002821, [self.gid], [self.gid], "sess-r", "user_prompt"),
        ])
        doc = self._report_json()
        rows_sum = sum(r["delivered"] for r in doc["rows"].values())
        tier_sum = sum(c["delivered"]
                       for moment in doc["by_tier_moment"].values()
                       for c in moment.values())
        self.assertEqual(rows_sum, tier_sum,
                         "the composition table must remain a partition of the "
                         "repeat table")

    def test_report_always_carries_its_disclosure_caveats(self):
        self._write_log([
            _line(1740002830, [self.pid], [self.pid], "sess-c", "pretool"),
        ])
        caveats = self._report_json()["caveats"]
        joined = " ".join(caveats)
        for expected in ("delivered counts", "retained decision-log family",
                         "point-in-time snapshot"):
            self.assertIn(expected, joined,
                          "the disclosure array must keep naming %r; got %r"
                          % (expected, caveats))

    def test_counting_bases_caveat_does_not_overclaim_distinctness(self):
        # It said "withheld counts DISTINCT CANDIDATES" while the arithmetic
        # is per-LINE, which reads as cross-line dedup -- the opposite of what
        # the code does.
        self._write_log([
            _line(1740002840, [self.pid], [self.pid], "sess-c", "pretool"),
        ])
        joined = " ".join(self._report_json()["caveats"])
        self.assertIn("WITHIN", joined)
        self.assertIn("two different", joined)


class StoreFailureDisclosureTest(FeedbackCase):

    def test_namespace_lookup_failure_is_disclosed(self):
        # namespace_tiers swallowed sqlite3.Error per chunk, so a corrupt store
        # reported every real row as 'unknown' at exit 0 with only the routine
        # caveats -- indistinguishable from genuinely absent rows.
        self._make_residue()
        self._write_log([
            _line(1740002900, [self.pid], [self.pid], "sess-x", "pretool"),
        ])
        store = Path(str(Path(self.tmp) / "store.sqlite") + "-wal")
        with sqlite3.connect(store) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        # Corrupt the staged COPY's read path by replacing the store body
        # after the residue is in place; the report must not silently succeed.
        body = Path(self.tmp, "store.sqlite").read_bytes()
        Path(self.tmp, "store.sqlite").write_bytes(
            body[:len(body) // 2] + b"\x00" * 4096 + body[len(body) // 2:])
        r = self._report("--json")
        if r.returncode == 2:
            self.assertIn("[zmem] report:", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            return
        doc = json.loads(r.stdout)
        self.assertTrue(
            any("chunk" in c and "unknown" in c for c in doc["caveats"]),
            "a failed tier lookup must be disclosed; caveats were %r"
            % doc["caveats"])


if __name__ == "__main__":
    unittest.main(verbosity=2)