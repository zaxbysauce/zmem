"""Tests for the store.py convention-drift subcommand (issue #261, Workstream R PR 5).

`convention-drift --namespace NS --json` is a READ-ONLY detection surface: it
prints one JSON object {"candidates": [{"id": "<row-uuid>"}, ...]} (exit 0)
naming live `convention`-typed memory rows that the namespace's queued
correction items CONTRADICT. It never mutates the store — no invalidate,
update, supersede, or telemetry write — so the store file must stay
byte-identical across a detection run (the ZeroWriteTest intent from
tests/test_zero_write_passive.py). Non-convention rows sharing the namespace
are never candidates, and an unrelated queued note names nothing.

Frozen as the acceptance checks for the issue #261 issue-tracer trace
(.agents/issue-traces/261-convention-drift/). At the pre-fix base the
subcommand does not exist, so every convention-drift invocation exits 2 with
argparse's "invalid choice: 'convention-drift'" — these tests are the red
checkpoint. (If the implementation hosts detection under a different argv
later, the frozen invocation here is amended at that time; these tests are
authored against this exact surface.)

The fixture drives the REAL store.py via subprocess against a throwaway temp
dir (ZMEM_STORE=<tmp>/store.sqlite, ZMEM_DATA=<tmp>, ZMEM_CAPTURE_MODE=manual
in a per-run env dict — never ambient, never ~/.zmem) and seeds queue items
in-process through the real correction_queue functions, the same seam
tests/test_r04_queue_add.py uses.

Run: python tests/test_r05_convention_drift.py
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

# The AC1 fixture pair: a queued correction that contradicts the convention
# row ("always install ... with pip" vs "never install ... with pip, use uv").
CONV_CONTENT = "always install python packages with pip in this repo"
LESSON_CONTENT = "pip install python packages was slow on the runner"
CONTRADICTING_MESSAGE = \
    "no, never install python packages with pip in this repo, use uv"
UNRELATED_MESSAGE = "rename the flange telemetry dashboard widget colour"


class _DriftBase(unittest.TestCase):
    """Shared fixture: throwaway temp dir per test (never ~/.zmem), the real
    store.py CLI via subprocess, and the real correction_queue seeding seam.

    Env per run: ZMEM_STORE=<tmp>/store.sqlite, ZMEM_DATA=<tmp>,
    ZMEM_CAPTURE_MODE=manual — passed only in the subprocess env dict, never
    set ambiently. Because ZMEM_STORE wins host.resolve_store_path()'s chain,
    the store parent is <tmp> and the queue sidecar the store-parent
    derivation resolves to is <tmp>/queue (correction_queue.resolve_queue_dir
    -> _resolve_data_dir() -> resolve_store_path().parent / "queue").
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-drift261-")
        self.ns = "project:github.com/example/convention-drift"
        self.store_path = os.path.join(self._tmp, "store.sqlite")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _env(self):
        e = dict(os.environ)
        e["ZMEM_STORE"] = self.store_path
        e["ZMEM_DATA"] = self._tmp
        e["ZMEM_CAPTURE_MODE"] = "manual"
        return e

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(STORE_PY), *args],
            env=self._env(), capture_output=True, text=True, timeout=120,
        )

    def _add_row(self, type_, content, signal=None, confidence=None):
        """Seed one row through the real `add` and return its full row uuid.

        UUID source: `add --json` prints one JSON object on stdout —
        {"id": <uuid>, "result": "stored"|"deduped", "warnings": [...]}
        (storelib/cli.py's add handler, issue #65 10.8). Parsing that `id` is
        chosen over reading the store sqlite directly because the structured
        write result already exists on this surface; "stored" is asserted so a
        surprise dedup fold can never hand back the wrong row.
        """
        args = ["add", "--namespace", self.ns, "--type", type_,
                "--content", content]
        if signal is not None:
            args += ["--signal", signal]
        if confidence is not None:
            args += ["--confidence", str(confidence)]
        args += ["--json"]
        r = self._run(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["result"], "stored", r.stderr)
        return doc["id"]

    def _seed_rows(self):
        """The two fixture rows (AC1): a convention and a lesson in NS.

        No `init` step is needed: the CLI's dispatch runs connect() +
        _prepare_store() for every ordinary subcommand, so the first `add`
        creates and migrates the throwaway store.
        """
        conv_id = self._add_row("convention", CONV_CONTENT,
                                signal="user", confidence=0.9)
        lesson_id = self._add_row("lesson", LESSON_CONTENT)
        return conv_id, lesson_id

    def _queue_dir(self):
        return Path(self._tmp) / "queue"

    def _seed_queue(self, message):
        """Seed ONE queue item in-process via the real correction_queue
        functions (make_item + append_queue, unchanged), pointed at the same
        queue dir the store-parent derivation resolves to. capture_mode
        "manual" keeps the message verbatim (no redaction drift)."""
        ok = cq.append_queue(
            self.ns,
            cq.make_item(message=message, type_="lesson", patterns="",
                         confidence=0.7, sentiment="note", decay_days=90,
                         session="", namespace=self.ns, host="cli",
                         capture_mode="manual"),
            queue_dir=self._queue_dir())
        self.assertTrue(ok, "queue seeding must succeed for the test to mean "
                            "anything")

    def _drift_ids(self):
        """Run the frozen detection argv and return the candidate id list."""
        r = self._run("convention-drift", "--namespace", self.ns, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        return [c["id"] for c in data["candidates"]]


class ConventionDriftTest(_DriftBase):
    # AC1: a queued correction that contradicts the convention row names that
    # row exactly once in the candidates list.
    def test_contradicting_correction_names_convention_row(self):
        conv_id, _lesson_id = self._seed_rows()
        self._seed_queue(CONTRADICTING_MESSAGE)
        ids = self._drift_ids()
        self.assertEqual(ids.count(conv_id), 1)

    # AC2: an unrelated queued note names nothing (no candidate at all).
    def test_unrelated_correction_yields_no_candidate(self):
        self._seed_rows()
        self._seed_queue(UNRELATED_MESSAGE)
        ids = self._drift_ids()
        self.assertEqual(len(ids), 0)

    # AC3: only `convention`-typed rows are ever candidates — the co-seeded
    # lesson row (same namespace, same fixture) is never named even when the
    # contradicting item IS present.
    def test_non_convention_row_is_never_a_candidate(self):
        _conv_id, lesson_id = self._seed_rows()
        self._seed_queue(CONTRADICTING_MESSAGE)
        ids = self._drift_ids()
        self.assertEqual(ids.count(lesson_id), 0)

    # AC4 (read-only): detection writes NOTHING — the store file is
    # byte-identical before/after the CLI subprocess (no invalidate, update,
    # supersede, or telemetry write), mirroring ZeroWriteTest in
    # tests/test_zero_write_passive.py.
    def test_drift_detection_writes_nothing(self):
        self._seed_rows()
        self._seed_queue(CONTRADICTING_MESSAGE)
        before = Path(self.store_path).read_bytes()
        self._drift_ids()
        self.assertEqual(Path(self.store_path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
