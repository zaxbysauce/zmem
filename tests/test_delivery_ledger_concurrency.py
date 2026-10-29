"""Issue #362 (absorbed by #284) — admit-once concurrent ledger recording.

Drives the LEDGER MODULE directly (storelib.delivery_ledger, sys.path
pinned to skills/memory/scripts; fresh scratch data dir; env discipline):
two concurrent writers — two real child processes, started through a
deterministic file barrier both poll on — attempt to record the SAME
(session, memory) pair. After the race exactly ONE admission must have
won, and the LOSER must observe the pair as already-delivered.

AC6/C5 (new surface) + AC7/C6 of issue #284's Phase 2.5: the module must
expose an admit-once API whose return value (or observable state) names
the winner.

Authored module contract (the implementer must satisfy it)::

    storelib.delivery_ledger.record_undelivered(
        data_dir, session_id, rows, moment) -> list[str]

``rows`` is the envelope-row list (the same shape ``record()`` takes);
``moment`` the injection moment string. The return value is the list of
memory ids THIS call admitted (absent from the ledger at admit time).
Two concurrent writers racing for the SAME (session_id, memory id) pair
resolve cross-process to exactly one winner — the id appears in the
winner's returned list — while the loser gets an empty list back and the
id is already present in ``delivered_ids(data_dir, session_id)`` (the
"already-delivered" observation). This test is RED at base (the API does
not exist) and GREEN after the fix.

Env discipline: fresh GUID scratch dir, ZMEM_HOME=repo root,
ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR pinned, ZMEM_MODEL_AUTODOWNLOAD=0,
PYTHONIOENCODING=utf-8 BEFORE any storelib import (the parent imports
storelib only inside the pinned context; the children get the same
scrubbed env), ledger knobs (ZMEM_DELIVER_WINDOW_S, ZMEM_LEDGER_CAP) and
every other delivery-affecting ambient override stripped, environment
restored afterwards. Unique session id per run.

Runs standalone: python tests/test_delivery_ledger_concurrency.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import contextlib
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

ROW_ID = "t05-race-row-1"

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
    "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_QUERY_CONTEXT",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "ZMEM_PENDING_SIDECAR",
)

# Self-contained child writer: rendezvous at a file barrier, then race the
# SAME (session, memory) pair through the admit-once API, then report both
# the own admission result and the already-delivered observation.
_CHILD_CODE = r'''
import json
import os
import sys
import time

scripts_dir, data_dir, sid, barrier_dir, writer = sys.argv[1:6]
sys.path.insert(0, scripts_dir)
from storelib import delivery_ledger as dl

# Deterministic start barrier: publish my readiness, then poll until the
# other writer has provably reached the same start line.
with open(os.path.join(barrier_dir, "ready-" + writer), "w",
          encoding="utf-8") as f:
    f.write("1")
other = "ready-" + str(1 - int(writer))
deadline = time.time() + 60.0
while not os.path.exists(os.path.join(barrier_dir, other)):
    if time.time() > deadline:
        raise SystemExit("start barrier timeout")
    time.sleep(0.002)

admitted = dl.record_undelivered(
    data_dir, sid,
    [{"id": "t05-race-row-1", "content": "concurrent admit-once race row"}],
    "user_prompt")
sees_delivered = "t05-race-row-1" in dl.delivered_ids(data_dir, sid)
print("RESULT " + json.dumps({
    "admitted": [str(i) for i in (admitted or [])],
    "sees_delivered": bool(sees_delivered),
}))
'''


@contextlib.contextmanager
def _pinned_scratch():
    """Fresh GUID scratch + env pin BEFORE any storelib import."""
    tmp = tempfile.mkdtemp(prefix=f"zmem-t05-race-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _child_env() -> dict:
    """The pinned env for child writers: the ledger knobs stay stripped."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP")}
    env["PYTHONUTF8"] = "1"
    return env


def test_concurrent_record_admits_one():
    """AC6/C5 + AC7/C6 (issue #284 absorbing #362): two concurrent writers
    (real subprocesses behind a deterministic file barrier) attempt to
    record the SAME (session, memory) pair through the ledger module's
    admit-once API; exactly ONE admission wins and the LOSER observes the
    pair as already-delivered. RED at base
    (storelib.delivery_ledger.record_undelivered does not exist), GREEN
    after the fix (the documented contract in this file's docstring)."""
    with _pinned_scratch() as tmp:
        # Re-pin the import path INSIDE the test body: pytest-randomly
        # shuffles module/test order, and a sibling module's sys.path
        # restore can wipe this file's module-level insert before this
        # test runs (observed as a bare ModuleNotFoundError under an
        # 8-module session that never reproduces per-file).
        import sys as _sys
        if SCRIPTS not in _sys.path:
            _sys.path.insert(0, SCRIPTS)
        import storelib.delivery_ledger as dl  # noqa: E402 (after env pin)

        assert hasattr(dl, "record_undelivered"), (
            "the admit-once ledger API storelib.delivery_ledger."
            "record_undelivered(data_dir, session_id, rows, moment) -> "
            "list[str] (this check's documented contract) does not exist")

        barrier = os.path.join(tmp, "barrier")
        os.makedirs(barrier, exist_ok=True)
        sid = f"t05-race-{uuid.uuid4()}"

        children = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_CODE,
                 SCRIPTS, tmp, sid, barrier, str(w)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=_child_env())
            for w in range(2)
        ]
        results = []
        for w, child in enumerate(children):
            out, err = child.communicate(timeout=120)
            assert child.returncode == 0, (
                f"writer {w} exited {child.returncode}: {err.strip()}")
            line = next((l for l in out.splitlines()
                         if l.startswith("RESULT ")), "")
            assert line, f"writer {w} printed no RESULT line: {out} {err}"
            results.append(json.loads(line[len("RESULT "):]))

        winners = [i for i, r in enumerate(results) if ROW_ID in r["admitted"]]
        assert len(winners) == 1, (
            f"exactly one writer must win admission of {ROW_ID}; "
            f"results: {results}")
        loser = results[1 - winners[0]]
        assert loser["admitted"] == [], (
            f"the losing writer also claims admission: {results}")
        assert loser["sees_delivered"], (
            "the losing writer must observe the pair as already-delivered "
            f"(delivered_ids after its call): {results}")

        # The merged ledger view holds the pair exactly once.
        assert dl.delivered_ids(tmp, sid) == [ROW_ID], (
            "the ledger view after the race must hold exactly the one "
            f"admitted id: {dl.delivered_ids(tmp, sid)}")


_TESTS = (
    test_concurrent_record_admits_one,
)


if __name__ == "__main__":
    failures = []
    for test in _TESTS:
        name = test.__name__
        try:
            test()
        except unittest.SkipTest as exc:
            print(f"skip  {name}: {exc}")
            continue
        except AssertionError as exc:
            failures.append((name, f"AssertionError: {exc}"))
            print(f"FAIL  {name}: AssertionError: {exc}")
            continue
        except Exception as exc:  # a crashed check is a failed check
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"ok    {name}")
    if failures:
        print(f"\n{len(failures)} failing test(s):")
        for name, error in failures:
            print(f"  {name}: {error}")
        sys.exit(1)
    print("\nall tests passed")
    sys.exit(0)
