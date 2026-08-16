"""Tests for the live-correction-capture queue (issue #47, PR 2/4).

Covers the correction_queue module (append/load/clear round-trip, atomicity,
corrupt-file fail-open, size cap drop-oldest, stale flagging, secret redaction,
collision-free namespace encoding, concurrent append), the CAPTURE DECISION
logic (correction -> queued, question/task/"can you" -> nothing, zmem's own
injected context -> nothing), and the store.py `queue-list` / `queue-clear`
subcommand JSON shape + clear semantics (store-independent, pre-connect).

Run: python tests/test_correction_queue.py
Plain unittest, no third-party harness — matches the repo convention."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import correction_queue as cq  # noqa: E402


def _make_item(**over):
    base = dict(
        message="no, use uv not pip",
        type_="auto",
        patterns="use-X-not-Y",
        confidence=0.7,
        sentiment="correction",
        decay_days=60,
        session="sess1",
        namespace="project:github.com/zaxbysauce/zmem",
        host="claude",
        capture_mode="manual",
    )
    base.update(over)
    return cq.make_item(**base)


class TestNamespaceEncoding(unittest.TestCase):
    def test_issue_example_round_trip(self):
        ns = "project:github.com/foo/bar"
        enc = cq.encode_namespace(ns)
        # Documented collision-free scheme (not the illustrative __ form).
        self.assertEqual(enc, "project_cgithub.com_sfoo_sbar")
        self.assertEqual(cq.decode_namespace(enc), ns)

    def test_windows_invalid_chars(self):
        for ns in [
            "project:a?b",
            'project:a"b',
            "project:a<b>c",
            "project:a|b",
            "project:a*b",
            "project:tab\there",
            "project:con",  # Windows reserved name
            "project:a:b",
            "project:a\\\\b",
        ]:
            enc = cq.encode_namespace(ns)
            self.assertNotIn("/", enc)
            self.assertNotIn("\\", enc)
            self.assertNotIn(":", enc)
            self.assertNotIn("?", enc)
            self.assertNotIn('"', enc)
            self.assertEqual(cq.decode_namespace(enc), ns, enc)

    def test_collision_distinctness(self):
        # Distinct namespaces must never collide.
        namespaces = [
            "project:github.com/foo/bar",
            "project:github.com/foo/bar2",
            "project:github.com/foobar/bar",
            "project:gitlab.com/foo/bar",
            "user:global",
            "project:a:bb",
            "project:a_sb",  # literal underscore adjacent to s/b must not collide
        ]
        encs = [cq.encode_namespace(n) for n in namespaces]
        self.assertEqual(len(set(encs)), len(encs))
        # For each, a decode round-trip is identity.
        for n in namespaces:
            self.assertEqual(cq.decode_namespace(cq.encode_namespace(n)), n)

    def test_non_ascii_round_trip(self):
        # The encoder must capture the FULL code point (not just its low byte),
        # so Greek/Cyrillic/CJK/accents/astral all round-trip exactly.
        for ns in [
            "project:λambda/папка/repo",
            "project:中文/日本語/repo",
            "project:café/naïve/repo",
            "project:emoji/😀/repo",
        ]:
            enc = cq.encode_namespace(ns)
            self.assertEqual(cq.decode_namespace(enc), ns)

    def test_non_ascii_low_byte_no_collision(self):
        # U+00C0 'À' and U+01C0 'ǀ' share the same low byte (0xC0). A scheme
        # that encoded only the low 8 bits would map BOTH to '_xc0' and mix the
        # two namespaces' queue files. The full-UTF-8 scheme must not.
        self.assertNotEqual(
            cq.encode_namespace("project:À"),
            cq.encode_namespace("project:ǀ"),
        )
        self.assertEqual(
            cq.decode_namespace(cq.encode_namespace("project:À")), "project:À"
        )
        self.assertEqual(
            cq.decode_namespace(cq.encode_namespace("project:ǀ")), "project:ǀ"
        )
        # Every distinct namespace still maps to a distinct filename.
        namespaces = ["project:À", "project:ǀ", "project:中", "project:一", "project:a"]
        encs = [cq.encode_namespace(n) for n in namespaces]
        self.assertEqual(len(encs), len(set(encs)), "collision across non-ASCII namespaces")


class TestQueueRoundTrip(unittest.TestCase):
    def _fresh(self):
        return tempfile.mkdtemp()

    def test_append_load_clear_round_trip(self):
        d = self._fresh()
        ns = "project:github.com/zaxbysauce/zmem"
        item = _make_item()
        self.assertTrue(cq.append_queue(ns, item, queue_dir=d))
        items = cq.load_queue(ns, queue_dir=d)
        self.assertEqual(len(items), 1)
        got = items[0]
        self.assertEqual(got["message"], "no, use uv not pip")
        self.assertEqual(got["type"], "auto")
        self.assertEqual(got["source"], "live-capture")
        self.assertEqual(got["namespace"], ns)
        self.assertEqual(got["session"], "sess1")
        self.assertEqual(got["host"], "claude")
        self.assertEqual(got["schema_version"], 1)
        self.assertTrue(got["id"])
        self.assertTrue(got["timestamp"])
        # clear by id
        removed = cq.clear_queue(ns, ids=[got["id"]], queue_dir=d)
        self.assertEqual(removed, 1)
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])
        # clear all via empty file
        cq.append_queue(ns, _make_item(), queue_dir=d)
        cq.clear_queue(ns, queue_dir=d)
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])

    def test_whole_queue_clear_returns_count(self):
        d = self._fresh()
        ns = "project:countreturn"
        self.assertTrue(cq.append_queue(ns, _make_item(), queue_dir=d))
        self.assertTrue(cq.append_queue(ns, _make_item(), queue_dir=d))
        removed = cq.clear_queue(ns, queue_dir=d)
        self.assertEqual(removed, 2)
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])
        # clearing an already-empty queue returns 0 (not a fabricated count)
        self.assertEqual(cq.clear_queue(ns, queue_dir=d), 0)

    def test_missing_file_is_empty(self):
        d = self._fresh()
        self.assertEqual(cq.load_queue("project:does/not/exist", queue_dir=d), [])

    def test_corrupt_file_fails_open(self):
        d = self._fresh()
        ns = "project:corrupt"
        p = Path(d) / (cq.encode_namespace(ns) + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid json ]", encoding="utf-8")
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])
        # appending over a corrupt file replaces it cleanly
        self.assertTrue(cq.append_queue(ns, _make_item(), queue_dir=d))
        self.assertEqual(len(cq.load_queue(ns, queue_dir=d)), 1)

    def test_non_list_json_fails_open(self):
        d = self._fresh()
        ns = "project:notlist"
        p = Path(d) / (cq.encode_namespace(ns) + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"count": 1}', encoding="utf-8")
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])

    def test_size_cap_drops_oldest(self):
        d = self._fresh()
        ns = "project:cap"
        # Use an artificially tiny cap via monkeypatch to exercise the FIFO rule.
        with mock.patch.object(cq, "MAX_QUEUE_SIZE", 3):
            for i in range(5):
                cq.append_queue(ns, _make_item(message="m%d" % i), queue_dir=d)
            items = cq.load_queue(ns, queue_dir=d)
            self.assertEqual(len(items), 3)
            self.assertEqual([it["message"] for it in items], ["m2", "m3", "m4"])


class TestStaleFlagging(unittest.TestCase):
    def _fresh(self):
        return tempfile.mkdtemp()

    def test_stale_flagged_not_deleted(self):
        d = self._fresh()
        ns = "project:stale"
        old = cq.make_item(
            message="old correction", type_="auto", patterns="p", confidence=0.7,
            sentiment="correction", decay_days=60, session="s", namespace=ns,
            host="z", capture_mode="manual",
        )
        old["timestamp"] = "2020-01-01T00:00:00Z"  # far past decay
        fresh = cq.make_item(
            message="fresh correction", type_="auto", patterns="p", confidence=0.7,
            sentiment="correction", decay_days=60, session="s", namespace=ns,
            host="z", capture_mode="manual",
        )
        cq.append_queue(ns, old, queue_dir=d)
        cq.append_queue(ns, fresh, queue_dir=d)
        items = {it["message"]: it for it in cq.load_queue(ns, queue_dir=d)}
        self.assertTrue(items["old correction"]["stale"])
        self.assertFalse(items["fresh correction"]["stale"])
        # stale is a computed flag, not persisted, and never deletes
        self.assertEqual(len(cq.load_queue(ns, queue_dir=d)), 2)

    def test_drop_stale_removes_only_stale_low_confidence(self):
        d = self._fresh()
        ns = "project:dropstale"
        old = cq.make_item(
            message="old", type_="auto", patterns="p", confidence=0.3,
            sentiment="correction", decay_days=60, session="s", namespace=ns,
            host="z", capture_mode="manual",
        )
        old["timestamp"] = "2020-01-01T00:00:00Z"
        keep = _make_item(namespace=ns)  # fresh, conf 0.7
        cq.append_queue(ns, old, queue_dir=d)
        cq.append_queue(ns, keep, queue_dir=d)
        removed = cq.clear_queue(ns, drop_stale=True, queue_dir=d)
        self.assertEqual(removed, 1)
        remaining = cq.load_queue(ns, queue_dir=d)
        self.assertEqual([it["message"] for it in remaining], ["no, use uv not pip"])

    def test_drop_stale_keeps_stale_high_confidence(self):
        # The --drop-stale rule is "stale AND confidence < 0.6". A stale item
        # at high confidence is retained (the reviewer must still see it).
        d = self._fresh()
        ns = "project:dropstalehi"
        stale_hi = cq.make_item(
            message="stale but high-conf", type_="auto", patterns="p",
            confidence=0.8, sentiment="correction", decay_days=60, session="s",
            namespace=ns, host="z", capture_mode="manual",
        )
        stale_hi["timestamp"] = "2020-01-01T00:00:00Z"
        cq.append_queue(ns, stale_hi, queue_dir=d)
        removed = cq.clear_queue(ns, drop_stale=True, queue_dir=d)
        self.assertEqual(removed, 0)
        self.assertEqual(len(cq.load_queue(ns, queue_dir=d)), 1)


class TestSecrets(unittest.TestCase):
    def test_auto_redacts(self):
        it = _make_item(message="the api_key=0123456789abcdef is bad, use X",
                        capture_mode="auto")
        self.assertTrue(it["secret_warning"])
        self.assertNotIn("api_key", it["message"])
        self.assertIn("REDACTED", it["message"])

    def test_manual_keeps_original_with_warning(self):
        it = _make_item(message="the api_key=0123456789abcdef is bad, use X",
                        capture_mode="manual")
        self.assertTrue(it["secret_warning"])
        self.assertIn("api_key", it["message"])


class TestAtomicity(unittest.TestCase):
    def _fresh(self):
        return tempfile.mkdtemp()

    def test_atomic_write_leaves_valid_file_on_success(self):
        d = self._fresh()
        ns = "project:atomic"
        for i in range(3):
            self.assertTrue(cq.append_queue(ns, _make_item(message="m%d" % i),
                                            queue_dir=d))
            raw = (Path(d) / (cq.encode_namespace(ns) + ".json")).read_text("utf-8")
            json.loads(raw)  # always valid
        self.assertEqual(len(cq.load_queue(ns, queue_dir=d)), 3)
        # no stray temp files left behind
        leftovers = [p.name for p in Path(d).glob("*.tmp.*")]
        self.assertEqual(leftovers, [])

    def test_concurrent_append_no_corruption(self):
        d = self._fresh()
        ns = "project:conc"
        def writer(k):
            for i in range(5):
                cq.append_queue(
                    ns, _make_item(message="w%d-%d" % (k, i), session="s%d" % k),
                    queue_dir=d,
                )
        threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        items = cq.load_queue(ns, queue_dir=d)
        # The file must never be corrupt and ids must be unique, even though the
        # read-modify-write design may lose an item under a write race (accepted
        # fail-open tolerance, documented in append_queue).
        self.assertIsInstance(items, list)
        self.assertEqual(len({it["id"] for it in items}), len(items))
        json.loads((Path(d) / (cq.encode_namespace(ns) + ".json")).read_text("utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX file mode only")
    def test_queue_file_is_owner_only_on_posix(self):
        # The queue can hold verbatim corrections (possibly secret-bearing), so
        # _atomic_write must chmod the file 0o600 like the store, not leave it at
        # the umask default (world-readable).
        d = self._fresh()
        ns = "project:perms"
        self.assertTrue(cq.append_queue(ns, _make_item(), queue_dir=d))
        p = Path(d) / (cq.encode_namespace(ns) + ".json")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        # A re-write re-hardens the freshly-replaced file too.
        self.assertTrue(cq.append_queue(ns, _make_item(), queue_dir=d))
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class TestCaptureDecision(unittest.TestCase):
    """The hook's DECISION gate, tested deterministically (not via bash):
    should_include_message + detect_patterns + the same append call the hook
    makes on a real correction."""

    def _hook_append(self, prompt, d, ns="project:github.com/zaxbysauce/zmem"):
        """Mirror hooks/zmem-capture-correction.sh's decision + append."""
        import corrections
        if not corrections.should_include_message(prompt):
            return 0
        item_type, patterns, confidence, sentiment, decay = corrections.detect_patterns(prompt)
        if not item_type:
            return 0
        cq.append_queue(ns, cq.make_item(
            message=prompt, type_=item_type, patterns=patterns,
            confidence=confidence, sentiment=sentiment, decay_days=decay,
            session="s", namespace=ns, host="claude", capture_mode="manual",
        ), queue_dir=d)
        return 1

    def test_corrections_queue(self):
        d = tempfile.mkdtemp()
        for prompt in ("no, use uv not pip for this repo",
                       "use pyright not mypy here",
                       "remember: pin Python 3.12"):
            self.assertEqual(self._hook_append(prompt, d), 1, prompt)
        items = cq.load_queue("project:github.com/zaxbysauce/zmem", queue_dir=d)
        self.assertEqual(len(items), 3)
        # acceptance: type=auto with patterns containing use-X-not-Y / no, and conf>=0.7
        auto = [it for it in items if it["type"] == "auto"]
        self.assertTrue(auto)
        self.assertTrue(any("use-X-not-Y" in it["patterns"] or "no," in it["patterns"]
                            for it in auto))
        self.assertTrue(any(it["confidence"] >= 0.7 for it in auto))

    def test_questions_and_tasks_queue_nothing(self):
        d = tempfile.mkdtemp()
        for prompt in ("can you fix the bug?",
                       "what is the diff?",
                       "please review this PR for me",
                       "help me understand the error"):
            self.assertEqual(self._hook_append(prompt, d), 0, prompt)
        self.assertEqual(cq.load_queue("project:github.com/zaxbysauce/zmem", queue_dir=d), [])

    def test_strong_correction_beats_task_opener(self):
        # Documented intent: a STRONG correction ("use X not Y") is still
        # captured even when wrapped in a polite opener like "can you" — the
        # false-positive opener only vetoes a NON-correction (corrections.py
        # defers to a strong hit). This pins the behavior the literal
        # acceptance wording ("starting 'can you' queues nothing") implies but
        # detect_patterns intentionally refines.
        d = tempfile.mkdtemp()
        self.assertEqual(self._hook_append("can you use uv not pip for this repo", d), 1)
        items = cq.load_queue("project:github.com/zaxbysauce/zmem", queue_dir=d)
        self.assertEqual(len(items), 1)
        self.assertIn("use-X-not-Y", items[0]["patterns"])

    def test_zmem_injected_context_queue_nothing(self):
        d = tempfile.mkdtemp()
        for prompt in ("# Relevant memories (zmem recall, namespace X). Consider if they apply; ignore if not.",
                       "# Loaded from memory (Tier 0 — core.md, user-level):\ncontent",
                       "<<<ZMEM_JSON>>>{}<<<END>>>"):
            self.assertEqual(self._hook_append(prompt, d), 0, prompt)
        self.assertEqual(cq.load_queue("project:github.com/zaxbysauce/zmem", queue_dir=d),
                         [])

    def test_payload_without_prompt_is_noop(self):
        import corrections
        # Missing prompt -> hook bails before should_include (the <5 guard).
        d = tempfile.mkdtemp()
        prompt = ""
        self.assertEqual(self._hook_append(prompt, d), 0)
        self.assertEqual(cq.load_queue("project:github.com/zaxbysauce/zmem", queue_dir=d), [])


class TestStoreSubcommands(unittest.TestCase):
    """queue-list / queue-clear via store.py (subprocess), proving they are
    store-independent (no store.sqlite is ever created) and work before
    connect."""

    def _run(self, env, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def _env(self, data):
        e = dict(os.environ)
        e["ZMEM_DATA"] = data
        e.pop("ZMEM_STORE", None)
        return e

    def _queue_dir(self, data):
        return os.path.join(data, "queue")  # store parent/queue, as resolve_queue_dir

    def test_queue_list_and_clear_json(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "data")
        ns = "project:github.com/example/repo"
        cq.append_queue(ns, _make_item(namespace=ns), queue_dir=self._queue_dir(d))

        env = self._env(d)
        # queue-list --json (no store.sqlite should be created)
        r = self._run(env, "queue-list", "--namespace", ns, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(obj["count"], 1)
        self.assertEqual(obj["items"][0]["source"], "live-capture")

        # queue-clear --all
        r2 = self._run(env, "queue-clear", "--namespace", ns, "--all")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(cq.load_queue(ns, queue_dir=d), [])

        # Still no store.sqlite ever created (pre-connect proof)
        self.assertFalse(os.path.exists(os.path.join(d, "store.sqlite")))

    def test_queue_clear_by_id(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "data")
        ns = "project:github.com/example/repo2"
        cq.append_queue(ns, _make_item(namespace=ns), queue_dir=self._queue_dir(d))
        cq.append_queue(ns, _make_item(namespace=ns, message="second"), queue_dir=self._queue_dir(d))
        items = cq.load_queue(ns, queue_dir=self._queue_dir(d))
        target = items[0]["id"]
        env = self._env(d)
        r = self._run(env, "queue-clear", "--namespace", ns, "--id", target)
        self.assertEqual(r.returncode, 0, r.stderr)
        remain = cq.load_queue(ns, queue_dir=self._queue_dir(d))
        self.assertEqual(len(remain), 1)
        self.assertEqual(remain[0]["id"] != target, True)
        self.assertFalse(os.path.exists(os.path.join(d, "store.sqlite")))

    def test_queue_clear_all_is_exclusive(self):
        # --all combined with --id must be a hard argparse error (rc 2), not a
        # silent --all drop.
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "data")
        ns = "project:github.com/example/repo3"
        env = self._env(d)
        r = self._run(env, "queue-clear", "--namespace", ns, "--all", "--id", "x")
        self.assertEqual(r.returncode, 2)
        r2 = self._run(env, "queue-clear", "--namespace", ns, "--all", "--drop-stale")
        self.assertEqual(r2.returncode, 2)

    def test_queue_clear_no_selector_rejected(self):
        # A flag-less `queue-clear --namespace X` must be a hard argparse error
        # (rc 2), NOT a silent whole-queue wipe (F-A must-fix).
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "data")
        ns = "project:github.com/example/noselector"
        cq.append_queue(ns, _make_item(namespace=ns), queue_dir=self._queue_dir(d))
        cq.append_queue(ns, _make_item(namespace=ns, message="second"), queue_dir=self._queue_dir(d))
        env = self._env(d)
        r = self._run(env, "queue-clear", "--namespace", ns)
        self.assertEqual(r.returncode, 2, r.stdout)
        # The queue must be left fully intact.
        self.assertEqual(len(cq.load_queue(ns, queue_dir=self._queue_dir(d))), 2)
        self.assertFalse(os.path.exists(os.path.join(d, "store.sqlite")))

    @unittest.skipIf(os.name == "nt", "POSIX dir-permission semantics only")
    def test_queue_clear_all_reports_failure_not_false_clear(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root unlink is not blocked by dir perms")
        # When the whole-queue unlink cannot execute (write-protected dir), the
        # CLI `--all` path must NOT fabricate a "cleared N"; it must report the
        # failure and leave the queue intact (F-H wired end-to-end).
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "data")
        ns = "project:github.com/example/clearblock"
        qdir = self._queue_dir(d)
        cq.append_queue(ns, _make_item(namespace=ns), queue_dir=qdir)
        qfile = os.path.join(qdir, cq.encode_namespace(ns) + ".json")
        self.assertTrue(os.path.exists(qfile))
        os.chmod(qdir, 0o500)  # dir no longer writable -> unlink fails
        try:
            env = self._env(d)
            r = self._run(env, "queue-clear", "--namespace", ns, "--all")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("failed (queue untouched)", r.stdout)
            self.assertNotIn("cleared", r.stdout)
            # The candidate must still be queued.
            self.assertEqual(len(cq.load_queue(ns, queue_dir=qdir)), 1)
        finally:
            os.chmod(qdir, 0o700)


class TestStoreSecretPatternsAlias(unittest.TestCase):
    """store.py's SECRET_PATTERNS must be the SAME object as
    correction_queue.SECRET_PATTERNS (single source of truth, never a drift
    copy)."""

    @staticmethod
    def _load_store():
        tmp = Path(tempfile.mkdtemp()) / "store.sqlite"
        spec = importlib.util.spec_from_file_location(
            "zmem_store_qtest", SCRIPTS_DIR / "store.py")
        with mock.patch.dict(os.environ, {"ZMEM_STORE": str(tmp)}, clear=False):
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def test_secret_patterns_identical_object(self):
        store = self._load_store()
        self.assertIs(store.SECRET_PATTERNS, cq.SECRET_PATTERNS)
        # And the store's redaction helper behaves against the shared list.
        redacted, count = store._redact_secret_like_text("api_key=0123456789abcdef42 bad")
        self.assertGreater(count, 0)
        self.assertIn("REDACTED", redacted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
