"""Decision-log split, rotation, and the moment= field (issue #129).

Proves the #129 contract end to end:
- decision lines live in ``<data_dir>/zmem-decisions.log``; rotation (via
  ``storelib.log_rotate``) REPLACES the destructive truncate-to-empty cap —
  history survives in ``.1``..``.N`` segments with ``# zmem-seq=`` markers,
  and ``parse_bg_log`` is rotation-aware (reads every segment);
- the split path resolution: ``run_miss_report`` prefers the decisions file
  when both it and a legacy ``zmem-bg.log`` exist, falls back to the legacy
  file alone, and an explicit ``bg_log_path`` wins over both;
- the additive ``moment=`` field: the body stamps its mode, session-start
  stamps ``moment=session_start`` (kill-switch line included), the parser
  returns ``None`` for pre-#129 lines, a hostile moment value is an inert
  bucket string that cannot forge other fields, and CRLF endings parse;
- drift.py's ``_append_bg_line`` rotates ``zmem-bg.log`` instead of
  truncating it.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (including the rotation knobs). The operator's real
store is never touched.

Runs standalone: python tests/test_decision_log.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
SESSION_START = REPO_ROOT / "hooks" / "zmem-session-start.sh"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "storelib"))

from storelib import log_rotate  # noqa: E402
from storelib import miss_rate  # noqa: E402
import drift  # noqa: E402

_MARKER_RE = re.compile(r"^# zmem-seq=\d+ rotated_at=\d+$")

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


def _seed(env: dict, ns: str, content: str) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "store.py"), "add",
         "--namespace", ns, "--type", "lesson", "--content", content,
         "--signal", "test"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"


def _decision_lines(tmp: str) -> list:
    path = Path(tmp) / "zmem-decisions.log"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if "zmem-hook" in ln]


def _run_body(tmp: str, mode: str, event: dict, ns: str,
              **extra_env: str) -> str:
    env = _clean_env(tmp, **extra_env)
    r = subprocess.run(
        [sys.executable, str(BODY), str(SCRIPTS / "store.py"),
         ns, "25000", mode],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stdout
    return r.stdout


def _old_line(i: int) -> str:
    return (f"[174000000{i}] zmem-hook status=silent reason=empty-pool "
            f"ids=[] all=[] sid=old-{i} moment=user_prompt")


class _SeededStore(unittest.TestCase):
    """Throwaway store seeded with one injectable lesson."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-dl129-")
        self.ns = "project:dl129"
        _seed(_clean_env(self._tmp), self.ns,
              "dlcanary: git stash pop conflicts need stash drop after "
              "resolve, verified by test")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _report(self, **kwargs):
        kwargs.setdefault("db_path", None)  # path resolution needs no db
        return miss_rate.run_miss_report(
            store_path=os.path.join(self._tmp, "store.sqlite"), **kwargs)


class BodyRotationTest(_SeededStore):
    """Rotation mechanics through the REAL hook body writer."""

    def _prefill(self, n: int = 6) -> list:
        lines = [_old_line(i) for i in range(n)]
        Path(self._tmp, "zmem-decisions.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        return lines

    def test_overcap_log_rotates_history_survives(self):
        old = self._prefill()  # 6 lines x ~105 bytes >> the 200-byte cap
        out = _run_body(
            self._tmp, "user_prompt",
            {"prompt": "how do I handle git stash pop conflicts here?",
             "session_id": "sess-rot"},
            self.ns, ZMEM_BG_LOG_MAX_BYTES="200")
        self.assertIn("dlcanary", out)  # the row actually injected
        seg = Path(self._tmp, "zmem-decisions.log.1")
        self.assertTrue(seg.is_file(),
                        "an over-cap decisions log must rotate, not append "
                        "unbounded")
        seg_lines = seg.read_text(encoding="utf-8").splitlines()
        # The rotated segment is self-describing: a marker line on top that
        # never matches the decision-line regex.
        self.assertRegex(seg_lines[0], _MARKER_RE)
        self.assertTrue(seg_lines[0].startswith("# zmem-seq="))
        # History survived: every pre-filled line is intact in the segment
        # (the pre-#129 behavior destroyed exactly this evidence).
        for line in old:
            self.assertIn(line, seg_lines)
        # The active file holds exactly the fresh decision line...
        active = _decision_lines(self._tmp)
        self.assertEqual(len(active), 1, active)
        self.assertIn("status=injected", active[0])
        self.assertIn(" sid=sess-rot", active[0])
        self.assertIn(" moment=user_prompt", active[0])
        # ...and the rotation-aware parser reads ALL lines across segments.
        parsed = miss_rate.parse_bg_log(
            Path(self._tmp, "zmem-decisions.log"))
        self.assertEqual(len(parsed), len(old) + 1,
                         "parse_bg_log must join rotated segments")
        self.assertEqual([p["sid"] for p in parsed],
                         [f"old-{i}" for i in range(len(old))] + ["sess-rot"])


class LogRotateUnitTest(unittest.TestCase):
    """Unit layer: storelib/log_rotate.py (new in #129)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-dl129-unit-")
        self._saved = {k: os.environ.get(k) for k in
                       ("ZMEM_BG_LOG_MAX_BYTES", "ZMEM_LOG_ROTATIONS")}
        os.environ["ZMEM_BG_LOG_MAX_BYTES"] = "10"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _log(self, name="x.log"):
        return os.path.join(self._tmp, name)

    def test_n1_rotation_keeps_exactly_one_segment(self):
        os.environ["ZMEM_LOG_ROTATIONS"] = "1"
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("A" * 20)
        self.assertTrue(log_rotate.rotate_on_append(p))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("B" * 20)
        self.assertTrue(log_rotate.rotate_on_append(p))
        segments = log_rotate.iter_segments(p)
        self.assertEqual(len(segments), 1,
                         "ZMEM_LOG_ROTATIONS=1 bounds the family to one "
                         "kept segment")
        self.assertTrue(segments[0].endswith("x.log.1"))
        # The kept segment holds the NEWER content ("B"); the oldest ("A")
        # was dropped by the bound — bounded depth, never evidence loss
        # without a bound.
        with open(segments[0], encoding="utf-8") as fh:
            self.assertIn("B" * 20, fh.read())
        # Fresh empty active file after rotation.
        self.assertEqual(os.path.getsize(p), 0)

    def test_invalid_env_values_fall_back_to_defaults(self):
        for bad in ("wat", "0", "-5", ""):
            with self.subTest(value=bad):
                os.environ["ZMEM_BG_LOG_MAX_BYTES"] = bad
                os.environ["ZMEM_LOG_ROTATIONS"] = bad
                self.assertEqual(log_rotate.max_bytes(),
                                 log_rotate.BG_LOG_DEFAULT_MAX_BYTES)
                self.assertEqual(log_rotate.rotations(),
                                 log_rotate.DEFAULT_ROTATIONS)
        # Valid values are honored (operator tuning survives).
        os.environ["ZMEM_BG_LOG_MAX_BYTES"] = "4096"
        os.environ["ZMEM_LOG_ROTATIONS"] = "5"
        self.assertEqual(log_rotate.max_bytes(), 4096)
        self.assertEqual(log_rotate.rotations(), 5)

    def test_iter_segments_discovers_family_in_number_order(self):
        # Rotation ages content toward HIGHER numbers (active -> .1 -> .2),
        # so .1 holds the newest rotated content and .2 the oldest.
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("A" * 20)
        log_rotate.rotate_on_append(p)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("B" * 20)
        log_rotate.rotate_on_append(p)
        # A decoy from a DIFFERENT log family must not be discovered.
        with open(self._log("other.log.1"), "w", encoding="utf-8") as fh:
            fh.write("foreign")
        found = log_rotate.iter_segments(p)
        self.assertEqual([os.path.basename(f) for f in found],
                         ["x.log.1", "x.log.2"],
                         "segments are discovered in ascending segment-"
                         "number order; the active file and foreign bases "
                         "are excluded")
        with open(found[0], encoding="utf-8") as fh:
            self.assertIn("B" * 20, fh.read())  # .1 = newest rotated
        with open(found[1], encoding="utf-8") as fh:
            self.assertIn("A" * 20, fh.read())  # .2 = oldest rotated
        # The join's ts-keyed logic is order-independent; what is load-
        # bearing is that EVERY segment is discovered exactly once.

    def test_marker_line_format(self):
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("A" * 20)
        log_rotate.rotate_on_append(p)
        first = Path(p + ".1").read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("# zmem-seq="))
        self.assertRegex(first, _MARKER_RE)

    def test_noop_under_cap(self):
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("tiny")
        self.assertFalse(log_rotate.rotate_on_append(p))
        self.assertFalse(os.path.exists(p + ".1"))
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "tiny")

    def test_oserror_fail_open_leaves_content_intact(self):
        # The rotation failure direction is growth, NEVER loss: a failed
        # shift must leave the log exactly as it was (the caller appends
        # anyway, over cap).
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("KEEPME" * 10)
        with mock.patch.object(log_rotate.os, "replace",
                               side_effect=OSError("locked")):
            self.assertFalse(log_rotate.rotate_on_append(p))
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "KEEPME" * 10,
                             "a failed rotation must not destroy content")
        self.assertEqual(log_rotate.iter_segments(p), [],
                         "no partial segment may survive a failed rotation")

    def test_missing_file_is_noop_not_error(self):
        self.assertFalse(log_rotate.rotate_on_append(
            os.path.join(self._tmp, "absent.log")))


class SplitPathResolutionTest(_SeededStore):
    """run_miss_report resolves the decisions file first (#129)."""

    @staticmethod
    def _write_log(path: Path, n: int, sid_prefix: str) -> None:
        path.write_text("".join(
            f"[174000000{i}] zmem-hook status=injected reason=injected "
            f"ids=['row-{sid_prefix}-{i}'] all=['row-{sid_prefix}-{i}'] "
            f"sid={sid_prefix}-{i} moment=user_prompt\n"
            for i in range(n)), encoding="utf-8")

    def test_both_logs_present_decisions_file_wins(self):
        self._write_log(Path(self._tmp, "zmem-decisions.log"), 3, "dec")
        self._write_log(Path(self._tmp, "zmem-bg.log"), 7, "legacy")
        rep = self._report()
        self.assertNotIn("error", rep)
        self.assertTrue(str(rep["bg_log_path"]).endswith(
            "zmem-decisions.log"))
        self.assertEqual(rep["bg_log_decision_lines"], 3,
                         "the decisions file is read when present, even "
                         "with a legacy zmem-bg.log alongside")

    def test_legacy_only_falls_back(self):
        self._write_log(Path(self._tmp, "zmem-bg.log"), 7, "legacy")
        rep = self._report()
        self.assertNotIn("error", rep)
        self.assertTrue(str(rep["bg_log_path"]).endswith("zmem-bg.log"))
        self.assertEqual(rep["bg_log_decision_lines"], 7,
                         "a legacy-only deployment still reads its log")

    def test_explicit_bg_log_path_wins_over_both(self):
        self._write_log(Path(self._tmp, "zmem-decisions.log"), 3, "dec")
        self._write_log(Path(self._tmp, "zmem-bg.log"), 7, "legacy")
        explicit = Path(self._tmp, "custom-decisions.log")
        self._write_log(explicit, 5, "expl")
        rep = self._report(bg_log_path=str(explicit))
        self.assertNotIn("error", rep)
        self.assertEqual(str(rep["bg_log_path"]), str(explicit))
        self.assertEqual(rep["bg_log_decision_lines"], 5,
                         "an explicit bg_log_path beats both defaults")


class MomentFieldBodyTest(_SeededStore):
    """The body writer stamps its mode as the moment (#129)."""

    def test_user_prompt_mode_moment(self):
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "how do I handle git stash pop conflicts?",
                   "session_id": "sess-m1"},
                  self.ns)
        line = _decision_lines(self._tmp)[-1]
        self.assertIn("status=injected", line)
        self.assertRegex(line, r" sid=\S+ moment=user_prompt$")

    def test_pretool_mode_moment(self):
        _run_body(self._tmp, "pretool",
                  {"session_id": "sess-m2",
                   "tool_input": {"command": "git stash pop"}},
                  self.ns)
        line = _decision_lines(self._tmp)[-1]
        self.assertRegex(line, r" sid=\S+ moment=pretool$")

    def test_kill_switch_body_line_carries_mode_moment(self):
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "unrelated", "session_id": "sess-m3"},
                  self.ns, ZMEM_INJECT="0")
        line = _decision_lines(self._tmp)[-1]
        self.assertIn("status=silent reason=disabled", line)
        self.assertRegex(line, r" sid=\S+ moment=user_prompt$")


class MomentFieldSessionStartTest(unittest.TestCase):
    """The session-start writer stamps moment=session_start (#129)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-dl129-ss-")
        self.ns = "project:dl129ss"
        self._drove_hook = False
        _seed(_clean_env(self._tmp), self.ns,
              "sscanary: recent high confidence row for session start")

    def tearDown(self):
        # The hook spawns a detached cadence worker (~15s delayed) that
        # writes into this temp dir; wait for its completion line so the
        # rmtree never races it (mirrors test_bg_log_sid's poll).
        log = Path(self._tmp) / "zmem-bg.log"
        if self._drove_hook and log.is_file():
            import time as _t
            deadline = _t.time() + 45
            while _t.time() < deadline:
                try:
                    if "session-cadence:" in log.read_text(
                            encoding="utf-8"):
                        break
                except OSError:
                    pass  # transient Windows sharing violation mid-append
                _t.sleep(2)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_session_start(self, extra_env: dict) -> "subprocess.CompletedProcess":
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        env = _clean_env(
            self._tmp,
            ZMEM_ROOT=str(REPO_ROOT),
            ZMEM_HOST="zcode",
            ZMEM_CTX_BUDGET="25000",
            ZMEM_NAMESPACE=self.ns,
            **extra_env)
        r = subprocess.run(
            [bash, str(SESSION_START)],
            input=json.dumps({"session_id": "sess-ss129"}),
            capture_output=True, text=True, env=env, timeout=180,
            cwd=self._tmp)
        self._drove_hook = True
        return r

    def test_session_start_line_moment(self):
        r = self._run_session_start({"ZMEM_SESSION": "sess-ss129"})
        self.assertEqual(r.returncode == 0, True, r.stderr[-800:])
        lines = _decision_lines(self._tmp)
        self.assertTrue(lines, "session-start decision line missing")
        self.assertRegex(lines[-1], r" sid=\S+ moment=session_start$")

    def test_kill_switch_line_carries_session_start_moment(self):
        # The kill-switch block resolves the sid from the env chain (the
        # bash layer threads ZMEM_SESSION into the inline python).
        r = self._run_session_start({"ZMEM_INJECT": "0",
                                     "ZMEM_SESSION": "sess-ss129"})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        lines = _decision_lines(self._tmp)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("status=silent reason=disabled", lines[0])
        self.assertIn(" sid=sess-ss129", lines[0])
        self.assertRegex(lines[0], r" moment=session_start$")


class MomentFieldParserTest(unittest.TestCase):
    """parse_bg_log's additive moment handling + hostile-input inertness."""

    def _parse(self, text: str) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "zmem-decisions.log")
            path.write_text(text, encoding="utf-8")
            return miss_rate.parse_bg_log(path)

    def test_legacy_line_without_moment_parses_none(self):
        parsed = self._parse(
            "[1740000000] zmem-hook status=injected reason=injected "
            "ids=['c'] all=['c'] sid=sess-old\n")
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0]["moment"],
                          "pre-#129 lines must read as legacy (None)")
        # …and the #129 field round-trips when present.
        parsed = self._parse(
            "[1740000001] zmem-hook status=injected reason=injected "
            "ids=['c'] all=['c'] sid=sess-new moment=pretool\n")
        self.assertEqual(parsed[0]["moment"], "pretool")

    def test_hostile_moment_is_inert_bucket_string(self):
        hostile = "evil(+)status=silent"
        parsed = self._parse(
            "[1740000000] zmem-hook status=injected reason=injected "
            f"ids=['h1'] all=['h1'] sid=sess-h moment={hostile}\n")
        self.assertEqual(len(parsed), 1)
        # The moment group is data, never structure: status/sid are exactly
        # what the line said, not what the hostile moment string claims.
        self.assertEqual(parsed[0]["moment"], hostile)
        self.assertEqual(parsed[0]["status"], "injected")
        self.assertEqual(parsed[0]["sid"], "sess-h")
        # And the counter buckets it under the inert literal — no field of
        # the report is forged by the metacharacters.
        from storelib import false_inject
        report = false_inject.build_false_injection_report(
            parsed, conn=None, data_dir=None, failure_rows=[])
        self.assertIn(hostile, report["per_moment"])
        self.assertEqual(report["per_moment"][hostile]["injected"], 1)
        self.assertEqual(report["overall"]["injected"], 1)

    def test_hostile_moment_cannot_append_a_forged_field(self):
        # A moment token followed by more text breaks the line shape
        # entirely (moment= is terminal by the regex) — the line is SKIPPED,
        # so a hostile moment can never smuggle in a second sid=/status=.
        parsed = self._parse(
            "[1740000000] zmem-hook status=silent reason=empty-pool "
            "ids=[] all=[] sid=sess-h "
            "moment=evil sid=fake\n")
        self.assertEqual(parsed, [],
                         "a non-terminal moment must not parse at all")

    def test_crlf_line_endings_still_parse(self):
        parsed = self._parse(
            "[1740000000] zmem-hook status=injected reason=injected "
            "ids=['c'] all=['c'] sid=sess-crlf moment=user_prompt\r\n"
            "[1740000001] zmem-hook status=silent reason=empty-pool "
            "ids=[] all=[] sid=sess-crlf2 moment=pretool\r\n")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["sid"], "sess-crlf")
        self.assertEqual(parsed[0]["moment"], "user_prompt")
        self.assertEqual(parsed[1]["moment"], "pretool")


class DriftWriterRotationTest(unittest.TestCase):
    """drift.py rotates zmem-bg.log instead of truncating (#129)."""

    def test_append_bg_line_rotates_history_survives(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-dl129-drift-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        log = tmp / "zmem-bg.log"
        stale = ("[1700000000] zmem-drift served=aaaaaaaa release=bbbbbbbb "
                 "files=3\n" * 8)  # well over a 64-byte cap
        log.write_text(stale, encoding="utf-8")
        saved = os.environ.get("ZMEM_BG_LOG_MAX_BYTES")
        os.environ["ZMEM_BG_LOG_MAX_BYTES"] = "64"
        try:
            ok = drift._append_bg_line(
                tmp, {"served": "cccccccc", "release": "dddddddd",
                      "differing_count": 5})
        finally:
            if saved is None:
                os.environ.pop("ZMEM_BG_LOG_MAX_BYTES", None)
            else:
                os.environ["ZMEM_BG_LOG_MAX_BYTES"] = saved
        self.assertTrue(ok)
        seg = tmp / "zmem-bg.log.1"
        self.assertTrue(seg.is_file(),
                        "an over-cap bg log must rotate, not truncate")
        seg_text = seg.read_text(encoding="utf-8")
        self.assertIn("zmem-drift served=aaaaaaaa", seg_text,
                      "the pre-existing drift history survives rotation "
                      "(the pre-#129 cap destroyed it)")
        self.assertRegex(seg_text.splitlines()[0], _MARKER_RE)
        active = log.read_text(encoding="utf-8")
        self.assertIn("zmem-drift served=cccccccc release=dddddddd "
                      "files=5", active)
        self.assertNotIn("served=aaaaaaaa", active)



class NoTruncateToEmptyGuardrailTest(unittest.TestCase):
    """Issue #129 Phase 4.2 guardrail (source contract): no writer of the
    telemetry logs (zmem-bg.log / zmem-decisions.log) may retain the
    destructive truncate-to-empty growth control, and every Python writer
    must rotate via storelib.log_rotate. This fails on the pre-#129 code
    (which opened the log with mode "w" at the cap) and passes on the
    rotation implementation."""

    def test_writers_rotate_and_never_truncate(self):
        repo = Path(__file__).resolve().parents[1]
        body = (repo / "hooks" / "lib" / "zmem-recall-body.py"
                ).read_text(encoding="utf-8")
        drift = (repo / "skills" / "memory" / "scripts" / "drift.py"
                 ).read_text(encoding="utf-8")
        ss = (repo / "hooks" / "zmem-session-start.sh"
              ).read_text(encoding="utf-8")
        # every python writer routes through the shared rotation helper
        self.assertIn("rotate_on_append", body,
                      "the hook body must rotate the decision log")
        self.assertIn("rotate_on_append", drift,
                      "the drift writer must rotate zmem-bg.log")
        self.assertIn("rotate_on_append", ss,
                      "the session-start inline writer must rotate")
        # the destructive pattern is gone from both python writers: an
        # open-for-write of the LOG PATH was the truncate-to-empty cap
        for name, text in (("zmem-recall-body.py", body), ("drift.py", drift)):
            self.assertNotRegex(
                text, r'open\([^)]*log_path[^)]*"w"',
                "%s still opens a log path with mode w (truncate to "
                "empty) - the #129 contract forbids destructive growth "
                "control on telemetry logs; fix the writer, never weaken "
                "this pin" % name)
        # the decisions log is a distinct file from the maintenance sink
        self.assertIn("zmem-decisions.log", body)
        self.assertIn("zmem-decisions.log", ss)




if __name__ == "__main__":
    unittest.main(verbosity=2)
