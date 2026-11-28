import subprocess
import sys
import time
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS, store_env


_HOLDER = r"""
import sys
import time
from pathlib import Path

scripts, ready = map(Path, sys.argv[1:])
sys.path.insert(0, str(scripts))
from storelib import schema

token = schema._acquire_schema_lock_with_liveness(wait_seconds=5)
assert token is not None
ready.touch(exist_ok=False)
time.sleep(30)
"""


def test_waiter_takes_over_when_schema_holder_dies_after_contention(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from storelib import schema

    store = tmp_path / "store.sqlite"
    monkeypatch.setattr(schema, "STORE_PATH", store)
    monkeypatch.setattr(schema, "SCHEMA_LOCK_STALE_SECONDS", 60.0)
    ready = tmp_path / "holder-ready"
    lock = schema._lock_path("schema")
    holder = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", _HOLDER, str(SCRIPTS), str(ready)],
        env=store_env(store), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    token = None
    try:
        deadline = time.monotonic() + 15.0
        while not ready.exists():
            assert holder.poll() is None, "fixture holder exited before taking schema"
            assert time.monotonic() < deadline, "fixture holder did not take schema"
            time.sleep(0.01)

        original_acquire = schema._host.acquire_lock
        failed_polls = 0
        holder_exited_at = None

        def observe_contention(path: Path, stale_seconds: float):
            nonlocal failed_polls, holder_exited_at
            acquired = original_acquire(path, stale_seconds)
            if path == lock and acquired is None:
                failed_polls += 1
                if failed_polls == 3:
                    assert holder.poll() is None, "the holder must still be alive during the wait"
                    holder.kill()
                    holder.wait(timeout=5)
                    holder_exited_at = time.monotonic()
                    assert lock.is_file(), "the terminated holder left its lock behind"
            return acquired

        monkeypatch.setattr(schema._host, "acquire_lock", observe_contention)
        token = schema._acquire_schema_lock_with_liveness(wait_seconds=6.0, poll_seconds=0.01)
        assert holder_exited_at is not None
        assert token is not None, "the already-waiting process did not recover the dead holder's lock"
        assert time.monotonic() - holder_exited_at < 3.0
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)
        if token is not None:
            schema._release_named_lock("schema", token)
