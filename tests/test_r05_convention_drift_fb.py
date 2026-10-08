"""Feedback-round tests for store.py convention-drift (PR #297 review findings).

Covers the surfaces the frozen issue-#261 suite (tests/test_r05_convention_drift.py,
byte-locked by the trace anchor) deliberately does not: namespace scoping (the
frozen fixture seeds a single namespace, so a dropped `WHERE namespace=?` or an
ignored `--namespace` survives it), plurality (the frozen fixture seeds one
convention row, so a break-after-first or LIMIT/window mutant survives it), the
`superseded_at IS NULL` liveness filter, and the human (non---json) render path
(the frozen suite only exercises `--json`).

This module is NOT part of the frozen checkpoint — it may evolve freely.
Fixture shape mirrors the frozen suite: real store.py via subprocess against a
throwaway temp dir (ZMEM_STORE/ZMEM_DATA/ZMEM_CAPTURE_MODE pinned in a per-run
env dict — never ambient, never ~/.zmem), queue items seeded in-process through
the real correction_queue functions.

Run: python tests/test_r05_convention_drift_fb.py
Plain unittest, no third-party harness — matches the repo convention."""

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
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import correction_queue as cq  # noqa: E402

CONV_A = "always install python packages with pip in this repo"
CONV_B = "always restart the gateway service after every deploy"
CONTRADICTS_A = "no, never install python packages with pip in this repo, use uv"


class _DriftFbBase(unittest.TestCase):
    """Throwaway temp dir per test (never ~/.zmem); real CLI via subprocess."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-drift261-fb-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.store = os.path.join(self._tmp, "store.sqlite")
        self.queue_dir = Path(self._tmp) / "queue"

    def _env(self):
        e = dict(os.environ)
        e["ZMEM_STORE"] = self.store
        e["ZMEM_DATA"] = self._tmp
        e["ZMEM_CAPTURE_MODE"] = "manual"
        return e

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            env=self._env(), capture_output=True, text=True, timeout=120,
        )

    def _add_row(self, ns, type_, content):
        r = self._run("add", "--namespace", ns, "--type", type_,
                      "--content", content, "--signal", "user",
                      "--confidence", "0.9", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["result"], "stored")
        return doc["id"]

    def _seed_queue(self, ns, message):
        item = cq.make_item(message=message, type_="lesson", patterns="",
                            confidence=0.7, sentiment="note", decay_days=90,
                            session="", namespace=ns, host="cli",
                            capture_mode="manual")
        self.assertTrue(cq.append_queue(ns, item, queue_dir=self.queue_dir))

    def _drift(self, ns, json_mode=True):
        args = ["convention-drift", "--namespace", ns]
        if json_mode:
            args.append("--json")
        r = self._run(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r


class TestNamespaceScoping(_DriftFbBase):
    """PRR-005: a dropped namespace filter must not survive this suite."""

    NS_A = "project:github.com/example/drift-fb-a"
    NS_B = "project:github.com/example/drift-fb-b"

    def test_cross_namespace_item_and_row_never_meet(self):
        conv_a = self._add_row(self.NS_A, "convention", CONV_A)
        # The contradicting message lives ONLY in NS_B's queue.
        self._seed_queue(self.NS_B, CONTRADICTS_A)
        self.assertEqual(self._drift_ids(self.NS_A), [],
                         "row in A flagged by an item queued under B")
        self.assertEqual(self._drift_ids(self.NS_B), [],
                         "scan of B sees no convention rows")
        # Control: the same message queued under A IS seen by the scan of A.
        self._seed_queue(self.NS_A, CONTRADICTS_A)
        self.assertEqual(self._drift_ids(self.NS_A), [conv_a])

    def _drift_ids(self, ns):
        return [c["id"] for c in json.loads(self._drift(ns).stdout)["candidates"]]


class TestPlurality(_DriftFbBase):
    """PRR-007: every matching convention row surfaces (no first-hit break,
    no LIMIT window over a two-row namespace)."""

    NS = "project:github.com/example/drift-fb-multi"

    def test_two_convention_rows_both_surface(self):
        id_a = self._add_row(self.NS, "convention", CONV_A)
        id_b = self._add_row(self.NS, "convention", CONV_B)
        # CONTRADICTS_A shares stems only with A (install/python/package/pip/
        # repo); B's stems (restart/gateway/service/deploy) are disjoint, so
        # the first scan pins {A} exactly — a first-hit break or a LIMIT=1
        # window would also pass this leg, which is why the second note below
        # contradicts BOTH rows and the scan must return both ids, deduplicated.
        self._seed_queue(self.NS, CONTRADICTS_A)
        ids = [c["id"] for c in
               json.loads(self._drift(self.NS).stdout)["candidates"]]
        self.assertEqual(ids, [id_a])
        # A note contradicting BOTH rows: the no-LIMIT contract means both
        # rows surface in one scan.
        self._seed_queue(
            self.NS,
            "no, never install python packages with pip in this repo, "
            "and never restart the gateway service after every deploy, "
            "use uv and systemd sockets")
        ids2 = [c["id"] for c in
                json.loads(self._drift(self.NS).stdout)["candidates"]]
        self.assertIn(id_a, ids2)
        self.assertIn(id_b, ids2)
        self.assertEqual(len(ids2), len(set(ids2)), "ids must be deduplicated")


class TestSupersededExcluded(_DriftFbBase):
    """PRR-011: only LIVE rows are candidates (superseded_at IS NULL)."""

    NS = "project:github.com/example/drift-fb-superseded"

    def test_superseded_convention_not_a_candidate(self):
        conv = self._add_row(self.NS, "convention", CONV_A)
        r = self._run("supersede", "--id", conv, "--reason",
                      "replaced by the uv workflow")
        self.assertEqual(r.returncode, 0, r.stderr)
        self._seed_queue(self.NS, CONTRADICTS_A)
        self.assertEqual(
            [c["id"] for c in
             json.loads(self._drift(self.NS).stdout)["candidates"]],
            [], "a superseded convention row must never be a candidate")


class TestHumanMode(_DriftFbBase):
    """PRR-010/PRR-022: the plain (non---json) render path."""

    NS = "project:github.com/example/drift-fb-human"
    NS_EMPTY = "project:github.com/example/drift-fb-human-empty"

    def test_candidate_line_and_empty_case(self):
        conv = self._add_row(self.NS, "convention", CONV_A)
        self._seed_queue(self.NS, CONTRADICTS_A)
        out = self._drift(self.NS, json_mode=False).stdout
        self.assertIn("- %s :: " % conv, out)
        self.assertIn(CONV_A[:80], out)
        empty = self._drift(self.NS_EMPTY, json_mode=False).stdout
        self.assertEqual(empty.strip(), "(no convention-drift candidates)")


if __name__ == "__main__":
    unittest.main()
