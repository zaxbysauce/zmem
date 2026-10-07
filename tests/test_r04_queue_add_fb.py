"""Feedback-round tests for store.py queue-add (PR #295 review findings).

Covers the surfaces the frozen issue-#260 suite (tests/test_r04_queue_add.py,
byte-locked by the trace anchor) deliberately does not: the --json receipt
shape and the closeout-note source literal, the rc-1 append-failure branch,
the optional flags' round-trip into the queued item, and the input guards
added in the PR #295 feedback round (--confidence finite within [0, 1],
--decay-days >= 1, --message bounded by MAX_CONTENT_CHARS, --patterns and
--sentiment redacted with secret_warning).

This module is NOT part of the frozen checkpoint — it may evolve freely.
Temp dirs are cleaned via addCleanup (the frozen suite predates that habit).

Run: python tests/test_r04_queue_add_fb.py
Plain unittest, no third-party harness — matches the repo convention."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

# Pin the data dir BEFORE any storelib import (storelib freezes STORE_PATH
# from the environment at first import). The in-process size-bound test
# imports storelib.mine; the pin keeps a stray connect() away from ~/.zmem.
_MODULE_TMP = tempfile.mkdtemp(prefix="zmem-qadd-fb-")
os.environ["ZMEM_DATA"] = os.path.join(_MODULE_TMP, "module-data")
os.environ.pop("ZMEM_STORE", None)
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ.setdefault("ZMEM_MODELS_DIR", os.path.join(_MODULE_TMP, "models-absent"))
atexit.register(shutil.rmtree, _MODULE_TMP, True)

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# A SECRET_PATTERNS-matching credential shape, concatenated so the literal
# never appears verbatim in source.
FAKE_GHP_TOKEN = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class _QueueAddFbBase(unittest.TestCase):
    """Shared fixture: throwaway ZMEM_DATA per test, cleaned via addCleanup."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-qadd-fb-case-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.data = os.path.join(self._tmp, "data")
        self.ns = "project:github.com/example/queue-add-fb"

    def _env(self, capture_mode="manual"):
        e = dict(os.environ)
        e["ZMEM_DATA"] = self.data
        e.pop("ZMEM_STORE", None)
        e["ZMEM_CAPTURE_MODE"] = capture_mode
        return e

    def _run(self, env, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def _queue_add(self, message, *extra, type_="lesson", env=None):
        e = env if env is not None else self._env()
        return self._run(e, "queue-add", "--namespace", self.ns,
                         "--message", message, "--type", type_, *extra)

    def _list_items(self, env=None):
        r = self._run(env if env is not None else self._env(),
                      "queue-list", "--namespace", self.ns, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["items"]


class QueueAddFeedbackTest(_QueueAddFbBase):
    def test_json_receipt_shape_and_source_literal(self):
        # PR #295 review: the --json receipt path had no coverage and the
        # closeout-note literal was unpinned anywhere in the test suite.
        r = self._queue_add("receipt shape note", "--json", env=self._env())
        self.assertEqual(r.returncode, 0, r.stderr)
        receipt = json.loads(r.stdout)
        self.assertEqual(set(receipt.keys()), {"ok", "id", "namespace", "source"})
        self.assertIs(receipt["ok"], True)
        self.assertEqual(receipt["namespace"], self.ns)
        self.assertEqual(receipt["source"], "closeout-note")
        # The persisted item carries the same literal.
        items = self._list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "closeout-note")

    def test_append_failure_reports_rc1(self):
        # The documented rc-1 branch: a write that does not land must not
        # fabricate a success receipt (docstring: the caller RELIES on it).
        os.makedirs(self.data)
        blocker = os.path.join(self.data, "queue")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("not a directory")
        r = self._queue_add("doomed note")
        self.assertEqual(r.returncode, 1)
        self.assertIn("queue untouched", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_optional_flags_round_trip(self):
        # The four optional flags were never exercised by the frozen suite;
        # a dropped kwarg (dest mismatch, missing passthrough) must fail.
        r = self._queue_add("flag round trip",
                            "--patterns", "use-X-not-Y",
                            "--confidence", "0.55",
                            "--sentiment", "correction",
                            "--decay-days", "30",
                            env=self._env())
        self.assertEqual(r.returncode, 0, r.stderr)
        items = self._list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["patterns"], "use-X-not-Y")
        self.assertEqual(items[0]["confidence"], 0.55)
        self.assertEqual(items[0]["sentiment"], "correction")
        self.assertEqual(items[0]["decay_days"], 30)

    def test_confidence_rejects_nonfinite_and_out_of_range(self):
        for bad in ("nan", "inf", "-inf", "1.5", "-0.5"):
            r = self._queue_add("bad confidence", "--confidence", bad,
                            env=self._env())
            self.assertEqual(r.returncode, 2, (bad, r.stdout, r.stderr))
            self.assertIn("confidence", r.stderr)
        self.assertEqual(self._list_items(), [])

    def test_decay_days_rejects_nonpositive(self):
        # decay < 1 makes the item permanently immune to --drop-stale.
        for bad in ("0", "-5"):
            r = self._queue_add("bad decay", "--decay-days", bad,
                                env=self._env())
            self.assertEqual(r.returncode, 2, (bad, r.stdout, r.stderr))
            self.assertIn("decay", r.stderr)
        self.assertEqual(self._list_items(), [])

    def test_message_over_store_limit_rejected_in_process(self):
        # The store write path rejects content over MAX_CONTENT_CHARS at
        # promotion time; queue-add must refuse it at write time. In-process
        # because a 64K+ argv is impossible on Windows (WinError 206).
        from storelib.mine import cmd_queue_add
        oversized = "A" * (MAX_CONTENT_CHARS_SNAPSHOT + 1)
        rc = cmd_queue_add(namespace=self.ns, message=oversized, type_="lesson")
        self.assertEqual(rc, 2)
        self.assertEqual(self._list_items(), [])

    def test_patterns_and_sentiment_redacted_with_warning(self):
        # make_item redacts only the message; queue-add must redact the
        # free-text metadata fields itself and flag secret_warning.
        env = self._env(capture_mode="auto")
        r = self._queue_add("clean note text",
                            "--patterns", "token was " + FAKE_GHP_TOKEN,
                            "--sentiment", "leak " + FAKE_GHP_TOKEN,
                            env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        items = self._list_items(env=env)
        self.assertEqual(len(items), 1)
        blob = json.dumps(items)
        self.assertNotIn(FAKE_GHP_TOKEN, blob)
        self.assertTrue(items[0].get("secret_warning"))
        self.assertEqual(items[0]["message"], "clean note text")

    def test_empty_message_exact_rc2_and_stderr(self):
        r = self._queue_add("   ")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--message must be non-empty", r.stderr)
        self.assertEqual(self._list_items(), [])

    def test_bad_type_rejected_rc2(self):
        r = self._queue_add("bad type", type_="bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid choice", r.stderr)
        self.assertEqual(self._list_items(), [])


# Snapshot the store cap for the in-process bound test (resolved lazily in
# cmd_queue_add; the fallback constant matches storelib.schema).
try:
    from storelib.schema import MAX_CONTENT_CHARS as MAX_CONTENT_CHARS_SNAPSHOT
except ImportError:  # pragma: no cover - schema always present in-repo
    MAX_CONTENT_CHARS_SNAPSHOT = 65536


if __name__ == "__main__":
    unittest.main()
