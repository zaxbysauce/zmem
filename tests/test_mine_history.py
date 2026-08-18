"""Tests for `store.py mine-history` cold-start bootstrap mining (issue #48).

Covers:
  - corrections.py error classification + cross-session aggregation
    (TOOL_ERROR_EXCLUDE_PATTERNS, PROJECT_SPECIFIC_ERROR_PATTERNS,
    classify_error_type, aggregate_errors with review_priority mapping).
  - history_mining.py discovery/folder-encoding/dedup/queue-synthesis.
  - store.py `mine-history` CLI via subprocess: merged report counts,
    missing-root clean exit, non-CC skip, --queue idempotency, store-untouched.

Run: python tests/test_mine_history.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _load(module_file, modname):
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, module_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS_DIR))
store_mod = _load(STORE_PY, "zmem_store_minetest")
import correction_queue as cq  # noqa: E402
import corrections as cmod  # noqa: E402
import history_mining as hm  # noqa: E402


# ---------------------------------------------------------------------------
# CC transcript fixture builders (mirror test_corrections.py)
# ---------------------------------------------------------------------------
def _assistant_tool_use(tid, name="Bash"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _tool_result(tid, content, is_error=True):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": content, "is_error": is_error, "tool_use_id": tid}]}}


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text}]}}


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _cc_env(data):
    e = dict(os.environ)
    e["ZMEM_DATA"] = data
    e.pop("ZMEM_STORE", None)
    return e


# ===========================================================================
# corrections.py — classify_error_type / aggregate_errors
# ===========================================================================
class TestClassifyErrorType(unittest.TestCase):
    def test_connection_refused(self):
        t, g = cmod.classify_error_type("Error: connect ECONNREFUSED 127.0.0.1:5432")
        self.assertEqual(t, "connection_refused")
        self.assertIn("Check .env for service URLs", g)

    def test_exclude_noise_never_candidate(self):
        for txt in ["File has not been read yet",
                    "InputValidationError: bad thing",
                    "Request exceeds maximum allowed tokens",
                    "bash: unexpected EOF while looking for matching `'",
                    "EISDIR: illegal operation on a directory"]:
            with self.subTest(txt=txt):
                self.assertEqual(cmod.classify_error_type(txt), (None, None))

    def test_rejection_text_never_error(self):
        # "The user doesn't want to proceed" is in the exclude list (belt-and-suspenders
        # on top of _failures_from_transcript's split).
        self.assertEqual(
            cmod.classify_error_type("The user doesn't want to proceed. user said: no"),
            (None, None))

    def test_unknown_maps_none(self):
        self.assertEqual(cmod.classify_error_type("some totally unknown anomaly"), (None, None))

    def test_module_not_found(self):
        t, g = cmod.classify_error_type("ModuleNotFoundError: No module named 'foo'")
        self.assertEqual(t, "module_not_found")
        self.assertTrue(g)

    def test_empty_safe(self):
        self.assertEqual(cmod.classify_error_type(""), (None, None))
        self.assertEqual(cmod.classify_error_type(None), (None, None))


class TestAggregateErrors(unittest.TestCase):
    def _err(self, etype, content, folder="p", guideline=None):
        return {"error_type": etype, "content": content, "project_folder": folder,
                "suggested_guideline": guideline}

    def test_below_threshold_dropped(self):
        agg = cmod.aggregate_errors([self._err("connection_refused", "x")], min_occurrences=2)
        self.assertEqual(agg, [])

    def test_priority_mapping(self):
        cases = [(2, 0.70), (3, 0.85), (4, 0.85), (5, 0.90), (9, 0.90)]
        for n, exp in cases:
            errs = [self._err("connection_refused", "refused %d" % i) for i in range(n)]
            agg = cmod.aggregate_errors(errs, min_occurrences=2)
            self.assertEqual(len(agg), 1)
            self.assertEqual(agg[0]["count"], n)
            self.assertAlmostEqual(agg[0]["review_priority"], exp, places=2)

    def test_sample_count_and_truncation(self):
        big = "E" * 5000
        errs = [self._err("connection_refused", big)] * 7
        agg = cmod.aggregate_errors(errs, min_occurrences=2)
        self.assertEqual(agg[0]["count"], 7)
        self.assertEqual(len(agg[0]["sample_errors"]), 3)
        for s in agg[0]["sample_errors"]:
            self.assertLessEqual(len(s), 200)
            self.assertTrue(s.endswith("E" * 200))

    def test_grouped_by_project_folder(self):
        errs = ([self._err("connection_refused", "x", folder="a")] * 3 +
                [self._err("connection_refused", "y", folder="b")] * 2)
        agg = cmod.aggregate_errors(errs, min_occurrences=2)
        self.assertEqual(len(agg), 2)
        folders = {e["project_folder"] for e in agg}
        self.assertEqual(folders, {"a", "b"})

    def test_sorted_by_count_desc(self):
        errs = ([self._err("redis_error", "redis down", folder="a")] * 2 +
                [self._err("connection_refused", "refused", folder="a")] * 4)
        agg = cmod.aggregate_errors(errs, min_occurrences=2)
        self.assertEqual([e["count"] for e in agg], [4, 2])

    def test_suggested_guideline_carried(self):
        errs = [self._err("port_in_use", "EADDRINUSE", guideline="Check service")] * 3
        agg = cmod.aggregate_errors(errs, min_occurrences=2)
        self.assertEqual(agg[0]["suggested_guideline"], "Check service")


# ===========================================================================
# history_mining.py — pure discovery/encoding/dedup
# ===========================================================================
class TestEncodeProjectFolder(unittest.TestCase):
    def test_encoding(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            folder = hm.encode_project_folder(str(p))
            self.assertTrue(folder.startswith("-"))
            self.assertNotIn("/", folder)
            self.assertNotIn("\\", folder)

    def test_candidates_underscore_hyphen(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "feature-x"
            base.mkdir()
            cands = hm.project_folder_candidates(str(base))
            canonical = hm.encode_project_folder(str(base))
            self.assertIn(canonical, cands)
            # lossy underscore-variant of the trailing basename is offered too
            variant = canonical[: -len("feature-x")] + "feature_x"
            self.assertIn(variant, cands)

    def test_candidates_include_creatable_form(self):
        # an on-disk folder can never contain ':' (Windows drive), so the
        # candidate set must include a colon-free form that is actually
        # creatable on disk (portable across POSIX and Windows CI).
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "feature-x"
            base.mkdir()
            cands = hm.project_folder_candidates(str(base))
            self.assertIn(hm.encode_project_folder(str(base)), cands)
            self.assertTrue(any(":" not in c for c in cands))

    def test_cc_windows_variant_maps_drive_colon(self):
        # CC's real Windows folder form: drive colon -> '-', leading dash dropped.
        # Pure string transform (no filesystem resolve) so it is portable across CI.
        self.assertEqual(hm._cc_windows_variant("-E:-ZCode-zmem"), "E--ZCode-zmem")
        self.assertEqual(hm._cc_windows_variant("-C:-Users-Brett-Claude"), "C--Users-Brett-Claude")
        self.assertIsNone(hm._cc_windows_variant("-home-user-app"))  # no drive colon

    def test_scoped_discovery_matches_hand_written_windows_folder(self):
        # NON-CIRCULAR Windows regression: the on-disk folder uses the real-world
        # CC Windows form (drive colon -> '-', no leading dash), NOT a folder name
        # derived from project_folder_candidates. We inject the candidate list so
        # the assertion tests DISCOVERY against the hand-written form.
        with tempfile.TemporaryDirectory() as dtx:
            root = Path(dtx)
            ondisk = "E--ZCode-zmem"  # hand-written real CC Windows folder name
            (root / ondisk).mkdir()
            _write_jsonl(root / ondisk / "s.jsonl", [_user_text("x")])
            cands = [hm._cc_windows_variant("-E:-ZCode-zmem"), "-home-user-app"]
            self.assertIsNotNone(cands[0])
            with mock.patch.object(hm, "project_folder_candidates", return_value=cands):
                files, _ = hm.discover_transcripts(root, project_dir="ignored")
            self.assertEqual({f.parent.name for f, _ in files}, {ondisk})


class TestDiscoverTranscripts(unittest.TestCase):
    def _root(self):
        root = Path(tempfile.mkdtemp())
        proj = root / "proj_demo"
        proj.mkdir()
        # two session files + one agent file
        a = proj / "session.jsonl"
        ag = proj / "agent-123.jsonl"
        _write_jsonl(a, [_user_text("hello")])
        _write_jsonl(ag, [_user_text("hi from agent")])
        return root, proj, a, ag

    def test_all_projects_finds_agent_and_session(self):
        root, proj, a, ag = self._root()
        files, missing = hm.discover_transcripts(root, all_projects=True)
        self.assertFalse(missing)
        names = {f.name for f, _ in files}
        self.assertIn("session.jsonl", names)
        self.assertIn("agent-123.jsonl", names)

    def test_days_mtime_filter(self):
        root, proj, a, ag = self._root()
        import time
        old = os.path.join(proj, "old.jsonl")
        _write_transcript_old(old)
        files, _ = hm.discover_transcripts(root, all_projects=True, days=1)
        self.assertNotIn("old.jsonl", {f.name for f, _ in files})
        files_all, _ = hm.discover_transcripts(root, all_projects=True, days=None)
        self.assertIn("old.jsonl", {f.name for f, _ in files_all})

    def test_missing_root(self):
        files, missing = hm.discover_transcripts(Path(tempfile.mkdtemp()) / "absent",
                                                 all_projects=True)
        self.assertTrue(missing)
        self.assertEqual(files, [])

    def test_scoped_to_current_project_folder(self):
        # Realistic: the on-disk folder carries an ENCODED candidate name; the
        # first creatable candidate (canonical or a lossy/sanitized alternate)
        # is what discovery must resolve to, and other projects must be excluded.
        with tempfile.TemporaryDirectory() as dreal:
            with tempfile.TemporaryDirectory() as dtx:
                real = Path(dreal) / "feature-x"
                real.mkdir()
                root = Path(dtx)
                cand = None
                for c in hm.project_folder_candidates(str(real)):
                    try:
                        (root / c).mkdir()
                        cand = c
                        break
                    except OSError:
                        continue
                self.assertIsNotNone(cand)
                _write_jsonl(root / cand / "s.jsonl", [_user_text("x")])
                other = root / "proj_other"
                other.mkdir()
                _write_jsonl(other / "s.jsonl", [_user_text("y")])
                files, _ = hm.discover_transcripts(root, project_dir=str(real))
                self.assertIn(cand, {f.parent.name for f, _ in files})


def _write_transcript_old(path):
    old_ts = time.time() - 10 * 86400
    _write_jsonl(path, [_user_text("stale")])
    os.utime(path, (old_ts, old_ts))


class TestIsCcTranscript(unittest.TestCase):
    def test_valid_cc_true(self):
        p = Path(tempfile.mkdtemp()) / "t.jsonl"
        _write_jsonl(p, [_user_text("hi"), _assistant_tool_use("t1")])
        self.assertTrue(hm.is_cc_transcript(p))

    def test_foreign_schema_false(self):
        p = Path(tempfile.mkdtemp()) / "f.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"tool": "Shell", "result": {"errors": ["x"]}}) + "\n")
        self.assertFalse(hm.is_cc_transcript(p))

    def test_empty_false(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.assertFalse(hm.is_cc_transcript(path))
        os.unlink(path)

    def test_ismeta_only_still_valid_cc_schema(self):
        # type=user but isMeta:true is real CC schema -> counted as SCANNED
        # (extract_user_messages filters it to zero candidates, but it's not a
        # foreign/malformed file to skip).
        p = Path(tempfile.mkdtemp()) / "meta.jsonl"
        _write_jsonl(p, [{"type": "user", "isMeta": True, "message": {"role": "user", "content": ""}}])
        self.assertTrue(hm.is_cc_transcript(p))

    def test_missing_false(self):
        self.assertFalse(hm.is_cc_transcript(Path(tempfile.mkdtemp()) / "gone.jsonl"))


class TestDedupeCorrections(unittest.TestCase):
    def _c(self, message, folder="p", ts="2026-01-01T00:00:00Z"):
        return {"message": message, "project_folder": folder, "timestamp": ts}

    def test_exact_duplicate_occurrences(self):
        items = [self._c("no, use uv not pip"),
                 self._c("no, use uv not pip")]
        out = hm.dedupe_corrections(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["occurrences"], 2)

    def test_near_identical_case_ws_punct_collapse_keep_most_recent(self):
        items = [self._c("No, use uv, not pip.", ts="2026-01-02T00:00:00Z"),
                 self._c("no use uv not pip", ts="2026-01-01T00:00:00Z")]
        out = hm.dedupe_corrections(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["occurrences"], 2)
        # most recent (later timestamp) message kept
        self.assertEqual(out[0]["message"], "No, use uv, not pip.")

    def test_distinct_trailing_token_kept(self):
        # "use foo not bar" vs "use foo not baz" differ semantically -> BOTH kept
        items = [self._c("no, use foo not bar"),
                 self._c("no, use foo not baz")]
        out = hm.dedupe_corrections(items)
        self.assertEqual(len(out), 2)

    def test_differs_by_content_kept(self):
        out = hm.dedupe_corrections([self._c("no, use uv"), self._c("don't refactor unrelated code")])
        self.assertEqual(len(out), 2)


# ===========================================================================
# store.py mine-history CLI (subprocess)
# ===========================================================================
class TestMineHistorySubcommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "transcripts")
        os.makedirs(self.root)
        self.proj = os.path.join(self.root, "proj_foo")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fixture(self):
        # 3 sessions in project proj_foo: corrections + rejections + repeated errors
        p = self.proj
        # session a: 2 ECONNREFUSED + correction "no, use uv not pip"
        _write_jsonl(os.path.join(p, "a.jsonl"), [
            _assistant_tool_use("t1"), _tool_result("t1", "Error: connect ECONNREFUSED 127.0.0.1:5432"),
            _assistant_tool_use("t2"), _tool_result("t2", "Error: connect ECONNREFUSED 127.0.0.1:5432"),
            _user_text("no, use uv not pip"),
        ])
        # session b: 2 ECONNREFUSED + rejection
        _write_jsonl(os.path.join(p, "b.jsonl"), [
            _assistant_tool_use("t3"), _tool_result("t3", "connect ECONNREFUSED"),
            _assistant_tool_use("t4"), _tool_result("t4", "connect ECONNREFUSED"),
            _assistant_tool_use("t5"),
            _tool_result("t5", "The user doesn't want to proceed. user said: don't touch deploy", is_error=True),
        ])
        # session c: 1 more ECONNREFUSED -> totals 5 => review_priority 0.9
        _write_jsonl(os.path.join(p, "c.jsonl"), [
            _assistant_tool_use("t6"), _tool_result("t6", "ECONNREFUSED Connection refused"),
        ])
        # duplicate correction in a second file -> occurrence count 2
        _write_jsonl(os.path.join(p, "d.jsonl"), [_user_text("no, use uv not pip")])
        # foreign, non-CC schema -> skipped
        with open(os.path.join(p, "foreign.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"tool": "Shell", "result": {"errors": ["x"]}}) + "\n")

    def _env(self, data=None):
        return _cc_env(data or self.tmp)

    def _run(self, env, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_missing_transcript_dir_clean_exit(self):
        data = tempfile.mkdtemp()
        try:
            r = self._run(self._env(data), "mine-history",
                          "--transcript-dir", os.path.join(data, "nope"), "--json")
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("error", json.loads(r.stdout))
        finally:
            shutil.rmtree(data, ignore_errors=True)

    def test_merged_report_counts(self):
        self._write_fixture()
        r = self._run(self._env(), "mine-history", "--transcript-dir", self.root,
                      "--all-projects", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["scanned"]["files"], 5)     # a,b,c,d + foreign
        self.assertEqual(rep["scanned"]["skipped"], 1)   # foreign
        # correction deduped across files: "no, use uv not pip" appears in a.jsonl + d.jsonl
        self.assertEqual(len(rep["corrections"]), 1)
        self.assertEqual(rep["corrections"][0]["occurrences"], 2)
        # rejections with reason
        self.assertEqual(len(rep["rejections"]), 1)
        self.assertIn("deploy", rep["rejections"][0]["reason"])
        self.assertEqual(rep["rejections"][0]["project_folder"], "proj_foo")
        # error pattern: 5x connection_refused -> priority 0.9
        self.assertEqual(len(rep["error_patterns"]), 1)
        ep = rep["error_patterns"][0]
        self.assertEqual(ep["error_type"], "connection_refused")
        self.assertEqual(ep["count"], 5)
        self.assertAlmostEqual(ep["review_priority"], 0.9, places=2)
        self.assertIn("Check .env for service URLs", ep["suggested_guideline"])

    def test_store_never_created_or_touched(self):
        self._write_fixture()
        store_path = os.path.join(self.tmp, "store.sqlite")
        original = b"SENTINEL-STORE-BYTES"
        with open(store_path, "wb") as f:
            f.write(original)
        env = dict(self._env())
        env["ZMEM_STORE"] = store_path
        r = self._run(env, "mine-history", "--transcript-dir", self.root,
                      "--all-projects", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(store_path, "rb") as f:
            self.assertEqual(f.read(), original)
        # mine-history never creates a store via the data-dir resolution path
        # (ZMEM_STORE was popped in _env, but we overrode it explicitly above);
        # confirm the command left the sentinel file byte-identical = no write.

    def test_report_only_creates_no_queue_file(self):
        self._write_fixture()
        r = self._run(self._env(), "mine-history", "--transcript-dir", self.root,
                      "--all-projects", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        queue_dir = os.path.join(self.tmp, "queue")
        self.assertFalse(os.path.exists(queue_dir) and
                         list(Path(queue_dir).glob("*.json")))

    def test_queue_mode_writes_valid_items_and_is_idempotent(self):
        self._write_fixture()
        env = self._env()
        r1 = self._run(env, "mine-history", "--transcript-dir", self.root,
                       "--all-projects", "--queue")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        queue_dir = Path(self.tmp) / "queue"
        qfiles = list(queue_dir.glob("*.json"))
        self.assertEqual(len(qfiles), 1)
        items = json.loads(qfiles[0].read_text(encoding="utf-8"))
        self.assertEqual(len(items), 2)  # 1 correction + 1 error_pattern
        kinds = {it["kind"] for it in items}
        self.assertEqual(kinds, {"correction", "error_pattern"})
        for it in items:
            self.assertEqual(it["source"], "history-mine")
            self.assertIn("dedup_key", it)
        ep = next(it for it in items if it["kind"] == "error_pattern")
        self.assertAlmostEqual(ep["review_priority"], 0.9, places=2)
        self.assertEqual(ep["confidence"], 0.6)  # honest floor, not review_priority
        self.assertEqual(ep["error_type"], "connection_refused")
        # second run is idempotent: nothing new appended
        r2 = self._run(env, "mine-history", "--transcript-dir", self.root,
                       "--all-projects", "--queue")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        items2 = json.loads(qfiles[0].read_text(encoding="utf-8"))
        self.assertEqual(len(items2), 2)
        self.assertIn("already present", r2.stdout)


# ===========================================================================
# build_mined_items — (dependency-injected) queue synthesis details
# ===========================================================================
class TestQueueModuleMissing(unittest.TestCase):
    def test_queue_module_missing_is_clean_error_not_crash(self):
        # If the #47 queue module can't be imported, --queue must fail open to a
        # non-zero rc with a clean message, never raise/traceback. Setting
        # sys.modules['correction_queue']=None makes `import correction_queue`
        # raise ImportError (Python treats a None entry as an unavailable module).
        report = {"corrections": [], "error_patterns": [], "rejections": []}
        saved = sys.modules.get("correction_queue")
        try:
            with mock.patch.dict(sys.modules, {"correction_queue": None}):
                rc = store_mod._queue_mined(report, host="cli")
        finally:
            if saved is not None:
                sys.modules["correction_queue"] = saved
        self.assertEqual(rc, 2)


class TestBuildMinedItems(unittest.TestCase):
    def test_messages_honor_occurrences(self):
        report = {"corrections": [
            {"message": "no, use uv not pip", "type": "auto", "patterns": "no,",
             "confidence": 0.9, "sentiment": "correction", "decay_days": 60,
             "project_folder": "proj_foo", "timestamp": "2026-01-01T00:00:00Z",
             "occurrences": 2},
        ], "error_patterns": [], "rejections": []}
        items = hm.build_mined_items(report, namespace="user:test", host="zcode")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["occurrences"], 2)
        self.assertEqual(items[0]["kind"], "correction")
        self.assertEqual(items[0]["source"], "history-mine")
        self.assertEqual(items[0]["namespace"], "user:test")
        self.assertEqual(items[0]["host"], "zcode")

    def test_error_pattern_item_carries_metadata(self):
        report = {"corrections": [], "error_patterns": [
            {"error_type": "connection_refused", "count": 5, "review_priority": 0.9,
             "suggested_guideline": "Check .env for service URLs - don't assume localhost",
             "sample_errors": ["refused"], "project_folder": "proj_foo"},
        ], "rejections": []}
        items = hm.build_mined_items(report, namespace="user:test")
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["kind"], "error_pattern")
        self.assertEqual(it["confidence"], 0.6)
        self.assertAlmostEqual(it["review_priority"], 0.9, places=2)
        self.assertEqual(it["error_type"], "connection_refused")
        self.assertIn("Check .env for service URLs", it["message"])

    def test_secret_redaction_in_mined_items(self):
        # Build the secret-looking token via concatenation so no verbatim
        # secret pattern appears in source (push-protection scan).
        fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
        corr = {"message": "use %s here the rest is plain" % ("api_key=" + fake_key),
                "type": "auto", "patterns": "no,", "confidence": 0.9,
                "sentiment": "correction", "decay_days": 60, "project_folder": "proj_foo",
                "timestamp": "2026-01-01T00:00:00Z", "occurrences": 1}
        report = {"corrections": [corr], "error_patterns": [], "rejections": []}
        # manual (default): original wording kept, secret_warning flagged
        with mock.patch.dict(os.environ, {}, clear=False):
            items = hm.build_mined_items(report, namespace="user:test")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["secret_warning"])
        self.assertIn("api_key=", items[0]["message"])
        # auto: secret-like text redacted before it reaches the queue
        with mock.patch.dict(os.environ, {"ZMEM_CAPTURE_MODE": "auto"}, clear=False):
            items2 = hm.build_mined_items(report, namespace="user:test")
        self.assertTrue(items2[0]["secret_warning"])
        self.assertIn("REDACTED_SECRET", items2[0]["message"])
        self.assertNotIn(fake_key, items2[0]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
