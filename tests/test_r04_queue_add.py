"""Tests for the store.py queue-add subcommand (issue #260, Workstream R PR 4).

`queue-add` lets a closeout session append a cheap, low-ceremony note to the
live-capture correction queue sidecar via the EXISTING correction_queue
functions (make_item + append_queue, unchanged): no store connect, no
embedding, no dedup — the same store-independent dispatch queue-list /
queue-clear already use. The item is stamped with a `source` distinct from
"live-capture" and "history-mine" so queue-list consumers can tell a
closeout-authored note from an automatically captured one.

Frozen as acceptance checks C1-C5 for the issue #260 issue-tracer trace
(.agents/issue-traces/260-queue-add/). At the pre-fix base the subcommand does
not exist, so every queue-add invocation exits 2 with argparse's
"invalid choice: 'queue-add'" — these tests are the red checkpoint.

Run: python tests/test_r04_queue_add.py
Plain unittest, no third-party harness — matches the repo convention."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import correction_queue as cq  # noqa: E402

# Distinct from correction_queue's "live-capture" and mine.py's "history-mine".
KNOWN_SOURCES = ("live-capture", "history-mine")

# A SECRET_PATTERNS-matching credential shape (GitHub PAT prefix + mixed-case
# alphanumerics). Fake; never a real credential.
FAKE_GHP_TOKEN = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def _seed_item(**over):
    """Build an in-process queue item for pre-seeding (manual mode, no
    redaction, deterministic fields) via the real make_item."""
    base = dict(
        message="seed item",
        type_="lesson",
        patterns="",
        confidence=0.7,
        sentiment="note",
        decay_days=90,
        session="",
        namespace="project:github.com/example/seed",
        host="cli",
        capture_mode="manual",
    )
    base.update(over)
    return cq.make_item(**base)


class _QueueAddBase(unittest.TestCase):
    """Shared fixture: throwaway ZMEM_DATA temp dir (never ~/.zmem), ZMEM_STORE
    popped, capture mode pinned per-run. Mirrors TestStoreSubcommands in
    tests/test_correction_queue.py."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.data = os.path.join(self._tmp, "data")
        self.ns = "project:github.com/example/queue-add"

    def _env(self, capture_mode="manual"):
        e = dict(os.environ)
        e["ZMEM_DATA"] = self.data
        e.pop("ZMEM_STORE", None)
        # Pin the mode so the redaction outcome is deterministic regardless of
        # the ambient environment (manual keeps the original by design).
        e["ZMEM_CAPTURE_MODE"] = capture_mode
        return e

    def _queue_dir(self):
        return os.path.join(self.data, "queue")  # store parent/queue, as resolve_queue_dir

    def _run(self, env, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def _queue_add(self, message, type_="lesson", env=None):
        e = env if env is not None else self._env()
        r = self._run(e, "queue-add", "--namespace", self.ns,
                      "--message", message, "--type", type_)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _list_items(self, env=None):
        r = self._run(env if env is not None else self._env(),
                      "queue-list", "--namespace", self.ns, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["items"]


class QueueAddTest(_QueueAddBase):
    # C1 (AC1): one queue-add appends exactly one item with that message,
    # visible in queue-list --json.
    def test_queue_add_item_is_listed(self):
        msg = "closeout note: verify the refresh fixture drift before release"
        self._queue_add(msg, type_="lesson")
        items = self._list_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["message"], msg)
        self.assertEqual(items[0]["type"], "lesson")
        self.assertEqual(items[0]["namespace"], self.ns)

    # C2 (AC2): the item carries a non-empty source distinct from the two
    # automatic sources.
    def test_queue_add_source_is_distinct(self):
        self._queue_add("source check note")
        items = self._list_items()
        distinct = [it for it in items
                    if it.get("source") and it.get("source") not in KNOWN_SOURCES]
        self.assertEqual(len(items), 1)
        self.assertEqual(len(distinct), 1)

    # C3 (AC3): no *.sqlite* file is ever created under the data dir — the
    # write path never connects to the store (no embedding, no dedup).
    def test_queue_add_never_creates_store(self):
        self._queue_add("no store touch")
        leftovers = sorted(p.name for p in Path(self.data).rglob("*.sqlite*"))
        self.assertEqual(leftovers, [])

    # C4 (AC4): capture mode auto redacts a credential-shaped message before
    # it is queued (same capture-policy redaction as the hook path).
    def test_queue_add_redacts_credential_in_auto_mode(self):
        env = self._env(capture_mode="auto")
        msg = "closeout note token " + FAKE_GHP_TOKEN
        self._queue_add(msg, env=env)
        items = self._list_items(env=env)
        self.assertEqual(len(items), 1)
        self.assertNotIn(FAKE_GHP_TOKEN, json.dumps(items))
        self.assertTrue(items[0].get("secret_warning"))

    # C5 (AC5): appending past MAX_QUEUE_SIZE keeps the queue at
    # MAX_QUEUE_SIZE — queue-add reuses append_queue's oldest-drop cap.
    def test_queue_add_respects_size_cap(self):
        seed_ns = self.ns
        qdir = self._queue_dir()
        for i in range(cq.MAX_QUEUE_SIZE):
            cq.append_queue(seed_ns,
                            _seed_item(namespace=seed_ns, message="seed %d" % i),
                            queue_dir=qdir)
        self._queue_add("one past the cap")
        items = self._list_items()
        self.assertEqual(len(items), cq.MAX_QUEUE_SIZE)
        # The queue-add item must have survived its own append (it is the
        # newest; the oldest seed is the one dropped).
        distinct = [it for it in items
                    if it.get("message") == "one past the cap"]
        self.assertEqual(len(distinct), 1)

    # Hardening (not frozen): a whitespace-only message is a usage error, and
    # nothing is queued.
    def test_queue_add_empty_message_rejected(self):
        r = self._run(self._env(), "queue-add", "--namespace", self.ns,
                      "--message", "   ", "--type", "lesson")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self._list_items(), [])


if __name__ == "__main__":
    unittest.main()
