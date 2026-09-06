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

    def test_silent_path_rotates_without_format_fence_leak(self):
        # Review PRR-005: the silent (kill-switch) decision-write path runs
        # BEFORE _format_fence, so its rotation import used to fail on the
        # wrong sys.path depth and the log grew unbounded there. Rotation
        # must actually fire on this path — .1 appears with the history.
        old = self._prefill()  # ~630 bytes >> the 200-byte cap
        _run_body(self._tmp, "user_prompt",
                  {"prompt": "unrelated", "session_id": "sess-rot2"},
                  self.ns, ZMEM_INJECT="0", ZMEM_BG_LOG_MAX_BYTES="200")
        seg = Path(self._tmp, "zmem-decisions.log.1")
        self.assertTrue(seg.is_file(), "rotation must run on the silent "
                        "writer path too, not only on the injected one")
        seg_lines = seg.read_text(encoding="utf-8").splitlines()
        for line in old:
            self.assertIn(line, seg_lines)
        active = _decision_lines(self._tmp)
        self.assertEqual(len(active), 1, active)
        self.assertIn("status=silent reason=disabled", active[0])


class LogRotateUnitTest(unittest.TestCase):
    """Unit layer: storelib/log_rotate.py (new in #129)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-dl129-unit-")
        self._saved = {k: os.environ.get(k) for k in
                       ("ZMEM_BG_LOG_MAX_BYTES", "ZMEM_LOG_ROTATIONS")}
        os.environ["ZMEM_BG_LOG_MAX_BYTES"] = "10"
        # Review PRR-012: the knobs must be ABSENT in-process too, not just
        # restored afterwards — an ambient ZMEM_LOG_ROTATIONS (the exact
        # hazard _STRIP_ENV warns about for children) used to leak into
        # rotation() here and break segment-count assertions.
        os.environ.pop("ZMEM_LOG_ROTATIONS", None)

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


class RotationReviewHardeningTest(unittest.TestCase):
    """Regressions for the execution-proven rotation defects found by the
    independent PR #144 review (PRR-001/002/003 + the stale-stamp finding):
    each test reproduces the defect shape against the CURRENT algorithm.
    All rotation calls pass the knobs explicitly so no ambient env can
    steer the fixtures."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-dl129-hard-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _log(self):
        return os.path.join(self._tmp, "fam.log")

    def _rotate(self, content):
        p = self._log()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        self.assertTrue(log_rotate.rotate_on_append(
            p, max_bytes_value=10, rotations_value=3))

    def test_eviction_drops_oldest_generation_not_newest(self):
        # PRR-001: the pre-review code sliced the FRONT of the ascending
        # list, destroying the newest rotated generation while ancient
        # content survived forever (proven by a 5-generation drive: the
        # surviving numbers drifted .1/.3/.4...). Five generations at
        # keep=3 must leave gens 5/4/3 — never a surviving gen 1.
        for gen in range(1, 6):
            self._rotate("GEN%d;" % gen + "x" * 30)
        segs = log_rotate.iter_segments(self._log())
        self.assertEqual([os.path.basename(s) for s in segs],
                         ["fam.log.1", "fam.log.2", "fam.log.3"])
        bodies = [Path(s).read_text(encoding="utf-8") for s in segs]
        self.assertIn("GEN5;", bodies[0])
        self.assertIn("GEN4;", bodies[1])
        self.assertIn("GEN3;", bodies[2])
        self.assertNotIn("GEN1;", "".join(bodies))
        self.assertNotIn("GEN2;", "".join(bodies))

    def test_marker_seq_is_a_monotonic_generation(self):
        # Review finding (stale stamps): only .1 was ever stamped, so every
        # segment claimed seq=1. The stamp on each new .1 is now one higher
        # than the highest marker in the family — a copied-out segment
        # self-describes its rotation generation with a UNIQUE seq, and
        # gaps mean evicted generations.
        for gen in range(1, 6):
            self._rotate("G%d;" % gen + "x" * 30)
        seqs = []
        for seg in log_rotate.iter_segments(self._log()):
            first = Path(seg).read_text(encoding="utf-8").splitlines()[0]
            m = re.match(r"^# zmem-seq=(\d+) rotated_at=\d+$", first)
            self.assertTrue(m, first)
            seqs.append(int(m.group(1)))
        self.assertEqual(seqs, [5, 4, 3], seqs)

    def test_gapped_family_retention_stays_bounded(self):
        # PRR-001 corollary: a hand-pruned family (.1/.3/.5) must keep the
        # NEWEST three generations after one rotation regardless of number
        # gaps — retention is positional (ascending-number list order),
        # never by literal number.
        p = self._log()
        for n, body in ((1, "G1;"), (3, "G3;"), (5, "G5;")):
            Path(p + ".%d" % n).write_text(body + "y" * 30, encoding="utf-8")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("NEW;" + "z" * 30)
        self.assertTrue(log_rotate.rotate_on_append(
            p, max_bytes_value=10, rotations_value=3))
        segs = log_rotate.iter_segments(p)
        self.assertEqual(len(segs), 3, segs)
        bodies = "".join(Path(s).read_text(encoding="utf-8") for s in segs)
        self.assertIn("NEW;", bodies)      # the fresh rotation
        self.assertIn("G1;", bodies)       # newest surviving old generation
        self.assertIn("G3;", bodies)
        self.assertNotIn("G5;", bodies)    # the OLDEST content is evicted

    def test_mid_shift_oserror_loses_no_bytes(self):
        # PRR-002: eviction used to run BEFORE the shift, so a rename
        # failure destroyed .1 while the call reported False ("nothing
        # happened"). Eviction now only runs after the family is complete,
        # and a mid-shift failure loses no byte at all.
        p = self._log()
        Path(p + ".1").write_text("OLD1;" + "a" * 30, encoding="utf-8")
        Path(p + ".2").write_text("OLD2;" + "b" * 30, encoding="utf-8")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("ACTIVE;" + "c" * 30)
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("locked mid-shift")
            return real_replace(src, dst)

        with mock.patch.object(log_rotate.os, "replace", flaky_replace):
            self.assertFalse(log_rotate.rotate_on_append(
                p, max_bytes_value=10, rotations_value=3))
        names = sorted(os.listdir(self._tmp))
        blobs = "".join(
            Path(self._tmp, n).read_text(encoding="utf-8") for n in names)
        for marker in ("OLD1;", "OLD2;", "ACTIVE;"):
            self.assertIn(marker, blobs,
                          "a failed rotation must lose no byte (got %r)"
                          % names)
        # The pre-call .1 in particular was NOT deleted (the old code
        # evicted it before the failing rename).
        self.assertIn("fam.log.1", names)

    def test_listdir_failure_aborts_without_touching_anything(self):
        # PRR-003: a failed directory listing used to read as an empty
        # family, so the shift no-op'd and os.replace clobbered a precious
        # .1 with the active file. The listing error now aborts the
        # rotation with nothing touched.
        p = self._log()
        Path(p + ".1").write_text("PRECIOUS;" + "p" * 30, encoding="utf-8")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("ACTIVE;" + "q" * 30)
        with mock.patch.object(log_rotate.os, "listdir",
                               side_effect=OSError("listing denied")):
            self.assertFalse(log_rotate.rotate_on_append(
                p, max_bytes_value=10, rotations_value=3))
        self.assertEqual(
            Path(p + ".1").read_text(encoding="utf-8"),
            "PRECIOUS;" + "p" * 30,
            "a listing failure must never clobber the existing segment")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ACTIVE;" + "q" * 30)

    def test_rotation_import_needs_the_scripts_dir_not_storelib(self):
        # PRR-005: `from storelib.log_rotate import ...` resolves against
        # the PARENT of the storelib package. The writers insert the
        # scripts dir; inserting the storelib dir itself (the old bug, now
        # fixed in both writers) must keep failing — this documents WHY the
        # parent is the correct insertion.
        prog_ok = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from storelib.log_rotate import rotate_on_append; "
            "assert callable(rotate_on_append); print('OK')" % SCRIPTS)
        r = subprocess.run([sys.executable, "-c", prog_ok],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)
        prog_bad = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from storelib.log_rotate import rotate_on_append"
            % (SCRIPTS / "storelib"))
        r = subprocess.run([sys.executable, "-c", prog_bad],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0,
                            "the storelib-dir-on-path shape must not import")


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

    def test_kill_switch_rotates_over_cap_decisions_log(self):
        # Review PRR-005 (session-start site): the kill-switch block used
        # to insert the storelib dir itself on sys.path, so its rotation
        # import failed silently and an over-cap decisions log grew
        # unbounded. The .1 segment must appear when the disabled line is
        # appended.
        old = [_old_line(i) for i in range(6)]
        Path(self._tmp, "zmem-decisions.log").write_text(
            "\n".join(old) + "\n", encoding="utf-8")
        r = self._run_session_start({"ZMEM_INJECT": "0",
                                     "ZMEM_SESSION": "sess-ss129",
                                     "ZMEM_BG_LOG_MAX_BYTES": "200"})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        seg = Path(self._tmp, "zmem-decisions.log.1")
        self.assertTrue(seg.is_file(), "session-start kill-switch rotation "
                        "must run (import depth fix)")
        seg_text = seg.read_text(encoding="utf-8")
        for line in old:
            self.assertIn(line, seg_text)
        lines = _decision_lines(self._tmp)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("status=silent reason=disabled", lines[0])
        self.assertIn(" sid=sess-ss129", lines[0])


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
        # And the counter buckets it under the SANITIZED literal (review
        # PRR-014: the reader applies the same charset rule as the writers,
        # so metacharacters cannot become report keys or leak into the
        # doctor summary text) — no field of the report is forged.
        from storelib import false_inject
        report = false_inject.build_false_injection_report(
            parsed, conn=None, data_dir=None, failure_rows=[])
        self.assertNotIn(hostile, report["per_moment"])
        self.assertIn("evil_-_status_silent", report["per_moment"])
        self.assertEqual(
            report["per_moment"]["evil_-_status_silent"]["injected"], 1)
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
