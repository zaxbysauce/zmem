"""T-05 supplementary lock-wiring pin (issue #284 / #362).

The frozen concurrency check (tests/test_delivery_ledger_concurrency.py)
synchronizes process START, not entry into the critical section; on
Windows the spawn jitter serializes the two writers, so a mutation that
removes the lock acquisition can survive it on timing alone (disclosed in
the trace's 08). This supplementary pin closes that gap deterministically:
record_undelivered MUST acquire the delivery-state lock (via
host.acquire_lock on ops/.delivery-state.lock) before publishing —
proven in-process by recording acquire_lock calls, no timing involved.
GREEN at head; a lock-drop mutant makes acquire_count 0 and fails.

Env discipline per the house pattern. Runs standalone:
python tests/test_t05_ledger_lock_wiring.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(REPO_ROOT / "skills" / "memory" / "scripts")
sys.path.insert(0, SCRIPTS)

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_RESERVATION_TTL_S",
)


@contextlib.contextmanager
def _pinned_scratch():
    tmp = tempfile.mkdtemp(prefix=f"zmem-t05-lockwire-{uuid.uuid4().hex}-")
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


def test_record_undelivered_acquires_the_delivery_lock():
    with _pinned_scratch() as tmp:
        import host
        from storelib import delivery_ledger as dl

        calls = []
        real_acquire = host.acquire_lock

        def recording_acquire(path, stale_s=None):
            calls.append(str(path))
            return real_acquire(path, stale_s)

        host.acquire_lock = recording_acquire
        try:
            sid = f"lockwire-{uuid.uuid4().hex}"
            rows = [{"id": "lockwire-row-1",
                     "content": "lock wiring probe row"}]
            admitted = dl.record_undelivered(tmp, sid, rows, "user_prompt")
            # Second call with the SAME row: admits nothing, so no
            # compaction runs after it (admitted==[] skips _try_compact)
            # — the only acquire this call can produce is the critical
            # section's own lock. This is the leg a lock-drop mutant
            # fails: a compaction-only acquire cannot appear here.
            calls.clear()
            readmitted = dl.record_undelivered(tmp, sid, rows, "user_prompt")
        finally:
            host.acquire_lock = real_acquire

        assert admitted == ["lockwire-row-1"], admitted
        assert readmitted == [], readmitted
        lock_paths = [p for p in calls if p.endswith(".delivery-state.lock")]
        assert lock_paths, (
            "record_undelivered must acquire the delivery-state lock "
            "(host.acquire_lock on ops/.delivery-state.lock) around its "
            "check+publish — the admit-once guarantee rests on it; this "
            "no-admit call runs no compaction, so a lock acquisition can "
            "only come from the critical section. Observed acquire_lock "
            f"calls: {calls}")


def test_expired_ack_reports_expired_not_unknown():
    """Review-round-2 regression pin: an ack whose target's TTL has lapsed
    must report {"expired": true} (exit 0 shape) — never "unknown
    reservation" — because the ack-time reap exempts the target. Also pins
    the duplicate-ack shape surviving past TTL (committed sidecar +
    expired timestamp -> still the duplicate no-op, since the committed
    marker is checked before expiry)."""
    with _pinned_scratch() as tmp:
        from storelib import delivery_ledger as dl

        sid = f"expired-ack-{uuid.uuid4().hex}"
        resid = dl.reserve(
            tmp, sid,
            [{"id": "expired-row-1", "content": "expired ack probe"}],
            "user_prompt", now=time.time() - 1000.0)
        assert resid, "reserve must succeed"
        result = dl.commit_reservation(tmp, resid)
        assert result == {"committed": [], "duplicate": False,
                          "expired": True}, result
        # The exempted target survives the reap until the next reserve/ack
        # of ANOTHER reservation cleans it.
        again = dl.commit_reservation(tmp, resid)
        assert again == {"committed": [], "duplicate": False,
                         "expired": True}, again
        # A committed reservation acked after TTL: the duplicate marker
        # wins over expiry (committed check precedes expiry).
        resid2 = dl.reserve(
            tmp, sid,
            [{"id": "expired-row-2", "content": "expired ack probe two"}],
            "user_prompt")
        first = dl.commit_reservation(tmp, resid2)
        assert first["committed"] == ["expired-row-2"], first
        late = dl.commit_reservation(tmp, resid2, now=time.time() + 10_000.0)
        assert late == {"committed": [], "duplicate": True,
                        "expired": False}, late


_TESTS = (
    test_record_undelivered_acquires_the_delivery_lock,
    test_expired_ack_reports_expired_not_unknown,
)

if __name__ == "__main__":
    failures = []
    for test in _TESTS:
        name = test.__name__
        try:
            test()
            print(f"ok    {name}")
        except AssertionError as exc:
            failures.append((name, f"AssertionError: {exc}"))
            print(f"FAIL  {name}: AssertionError: {exc}")
        except Exception as exc:  # a crashed check is a failed check
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    if failures:
        for name, error in failures:
            print(f"collected: {name}: {error}")
        sys.exit(1)
    print(f"{len(_TESTS)} tests completed")
