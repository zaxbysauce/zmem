"""Issue #315 — concurrent operation-feedback sidecar writers must lose no
verdict.

Arm's-length acceptance check authored against base 41008fdd. Two REAL
subprocesses (the house file-barrier pattern from
tests/test_delivery_ledger_concurrency.py: both rendezvous at process START
on ready-<w> marker files they poll) each append 30 feedback events with
DISTINCT event_ids (child A: "a-0".."a-29", child B: "b-0".."b-29") via
``dl.record_feedback_event(tmp, SID, event_id, "mem-1", "applied", 1, None,
now=<fixed iso>)``. The parent then reads the sidecar and asserts all 60
distinct event_ids are present.

Race-window widening (child-side ONLY, per the authoring brief): each child
monkeypatches ``dl._load_feedback_records`` (test code) with a wrapper that
performs the real read and then sleeps 50 ms before returning, widening the
read->replace gap inside record_feedback_event so the two writers' cycles
overlap heavily. Each event_id is recorded exactly ONCE per child (no
re-records), so a lost update can never heal. At head this wrapper is
inert: a locking fix merely extends the lock hold; a restructured read path
simply never calls the wrapper.

Base-RED on this authoring host (Windows): 12/12 probe runs failed — one
writer ALWAYS crashes with FeedbackSidecarError (os.replace onto the shared
sidecar path collides: [WinError 5] Access is denied) and the surviving
sidecar holds only the other writer's 30 events (wholesale replacement).
On POSIX the same unsynchronized read-modify-write manifests as silent
lost updates; the parent's all-60 assertion catches that form too.

Env discipline: children get a scrubbed env (every ambient ZMEM_* key
stripped, then ZMEM_DATA=<tmp>, ZMEM_STORE=<tmp>/store.sqlite,
PYTHONUTF8=1, PYTHONIOENCODING=utf-8). The ledger is ops-dir sidecars
only; no sqlite is opened; ~/.zmem is never touched. Generous 120 s child
timeouts. Unique session id per run.

Check id (final; also in the test docstring):
- C3 FeedbackRaceTest::test_concurrent_feedback_writers_lose_no_verdict

Runs standalone: python tests/test_315_feedback_sidecar_race.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(REPO_ROOT / "skills" / "memory" / "scripts")
sys.path.insert(0, SCRIPTS)

# Child writer: start barrier, then 30 distinct-event record_feedback_event
# calls with a widened read->replace gap (test-side monkeypatch).
_CHILD_CODE = r'''
import os
import sys
import time

scripts_dir, data_dir, sid, barrier_dir, writer = sys.argv[1:6]
sys.path.insert(0, scripts_dir)
from storelib import delivery_ledger as dl

# Widen the read->replace race window (test-side only): the real read
# happens, then the wrapper sleeps 50 ms before returning its result, so
# both writers' read-modify-write cycles overlap heavily.
_real_load = dl._load_feedback_records

def _slow_load(path):
    records = _real_load(path)
    time.sleep(0.05)
    return records

dl._load_feedback_records = _slow_load

# Deterministic START barrier: publish readiness, poll for the sibling.
with open(os.path.join(barrier_dir, "ready-" + writer), "w",
          encoding="utf-8") as f:
    f.write("1")
other = "ready-" + str(1 - int(writer))
deadline = time.time() + 60.0
while not os.path.exists(os.path.join(barrier_dir, other)):
    if time.time() > deadline:
        raise SystemExit("start barrier timeout")
    time.sleep(0.002)

tag = "a" if writer == "0" else "b"
for i in range(30):
    dl.record_feedback_event(
        data_dir, sid, f"{tag}-{i}", "mem-1", "applied", 1, None,
        now="2026-01-01T00:00:00Z")
print("WROTE " + tag)
'''


def _child_env(tmp: str) -> dict:
    """Scrubbed child env: strip EVERY ambient ZMEM_* key, then pin the
    throwaway scratch (no ~/.zmem, no ambient store)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_DATA": tmp,
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


class FeedbackRaceTest(unittest.TestCase):

    def setUp(self):
        for key in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
                    "ZMEM_RESERVATION_TTL_S"):
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="zmem-315-fbrace-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_feedback_writers_lose_no_verdict(self):
        """C3 — two concurrent feedback writers (real subprocesses behind a
        process-START file barrier) append 30 events each with distinct
        event_ids; after both finish, the sidecar must hold ALL 60 distinct
        event_ids and neither writer may have crashed. Base-RED: the
        unsynchronized read-modify-write loses updates — on this host one
        writer crashes with FeedbackSidecarError ([WinError 5] on the
        colliding os.replace) and the final sidecar holds only the
        surviving writer's 30 events; on POSIX the same race manifests as
        silent lost updates."""
        from storelib import delivery_ledger as dl

        barrier = os.path.join(self.tmp, "barrier")
        os.makedirs(barrier, exist_ok=True)
        sid = f"315-race-{uuid.uuid4().hex}"

        children = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_CODE,
                 SCRIPTS, self.tmp, sid, barrier, str(w)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=_child_env(self.tmp))
            for w in range(2)
        ]
        try:
            for w, child in enumerate(children):
                out, err = child.communicate(timeout=120)
                self.assertEqual(
                    child.returncode, 0,
                    f"feedback writer {w} exited {child.returncode} — the "
                    f"unsynchronized sidecar write crashed it: "
                    f"{err.strip()[-2000:]}")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                for pipe in (child.stdout, child.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass

        path = dl.feedback_event_path(self.tmp, sid)
        self.assertTrue(os.path.exists(path),
                        "the feedback sidecar must exist after both writers")
        ids = set()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    ids.add(rec["event_id"])
        expected = {f"a-{i}" for i in range(30)} | {f"b-{i}" for i in range(30)}
        missing = sorted(expected - ids)
        self.assertEqual(
            ids, expected,
            f"concurrent feedback writers lost verdict events: the sidecar "
            f"holds {len(ids)} of 60 distinct event_ids (missing: "
            f"{missing[:10]}{'...' if len(missing) > 10 else ''}) — the "
            f"read-modify-write append must be safe under concurrent "
            f"writers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
