import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import REPO_ROOT, connect, init_store, insert_memory, store_env

_WORKER = r"""
import json
import os
import sys
import time
from pathlib import Path

scripts, store, ready, go, result = map(Path, sys.argv[1:])
sys.path.insert(0, str(scripts))
import storelib.schema as schema

# Prime per-process imports and optional extension loading before the barrier.
# The timed open below uses a new SQLite connection and runs schema preparation.
schema.connect().close()

samples = {}
original_acquire = schema._acquire_schema_lock_with_liveness
original_release = schema._release_named_lock

def timed_acquire(*args, **kwargs):
    started = time.perf_counter()
    token = original_acquire(*args, **kwargs)
    samples["wait_s"] = time.perf_counter() - started
    samples["hold_started"] = time.perf_counter()
    return token

def timed_release(name, token):
    if name == "schema" and "hold_started" in samples:
        samples["hold_s"] = time.perf_counter() - samples["hold_started"]
    return original_release(name, token)

schema._acquire_schema_lock_with_liveness = timed_acquire
schema._release_named_lock = timed_release
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 30.0
while not go.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("benchmark start signal did not arrive")
    time.sleep(0.005)
started = time.perf_counter()
try:
    conn = schema.connect()
    schema._prepare_store(conn)
    conn.close()
    samples["open_s"] = time.perf_counter() - started
    result.write_text(json.dumps({"ok": True, **samples}), encoding="utf-8")
except BaseException as exc:
    result.write_text(json.dumps({"ok": False, "error": repr(exc)}), encoding="utf-8")
    raise
"""


def _wait_for(paths: list[Path], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        time.sleep(0.01)
    missing = [str(path) for path in paths if not path.exists()]
    raise AssertionError(f"workers did not become ready: {missing}")


def _measure_opens(store, root, count):
    root.mkdir()
    scripts = REPO_ROOT / "skills" / "memory" / "scripts"
    workers = []
    ready = []
    results = []
    observations = []
    go = root / "go"
    worker_env = store_env(store)
    # sqlite-vec can import NumPy, but opening the store uses no BLAS work.
    worker_env["OPENBLAS_NUM_THREADS"] = "1"
    try:
        for index in range(count):
            ready_path = root / f"ready-{index}.txt"
            result_path = root / f"result-{index}.json"
            command = [sys.executable, "-X", "utf8", "-c", _WORKER,
                       str(scripts), str(store), str(ready_path), str(go), str(result_path)]
            workers.append(subprocess.Popen(
                command, cwd=str(REPO_ROOT), env=worker_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            ready.append(ready_path)
            results.append(result_path)
        _wait_for(ready)
        go.write_text("go", encoding="utf-8")
        _wait_for(results, timeout=45.0)
        for worker in workers:
            assert worker.wait(timeout=10) == 0
        observations = [json.loads(path.read_text(encoding="utf-8")) for path in results]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
        for worker in workers:
            worker.wait(timeout=10)
    assert all(row.get("ok") for row in observations)
    return observations


def test_24_concurrent_clean_opens_measure_schema_open_and_lock_hold():
    with tempfile.TemporaryDirectory(prefix="zmem-546-latency-") as raw:
        root = Path(raw)
        store = root / "store.sqlite"
        init_store(store)
        conn = connect(store)
        for index in range(50_000):
            insert_memory(conn, f"latency-{index:05d}", "x" * 1024, "x" * 1024)
        # The fixture writes canonical text and norm together, like supported writers.
        conn.execute("DELETE FROM memory_content_norm_dirty WHERE memory_id LIKE 'latency-%'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM memory_content_norm_dirty").fetchone()[0] == 0
        state = conn.execute("SELECT value FROM meta WHERE key = 'content_norm_pass_state'").fetchone()
        assert state is not None and state[0] == "done"
        assert conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 50_000
        conn.close()

        single = _measure_opens(store, root / "single", 1)[0]
        if single["open_s"] > 0.050:
            raise AssertionError(f"runner too slow: single clean open took {single['open_s']:.3f}s, over 0.050s")

        observations = _measure_opens(store, root / "parallel", 24)
        assert statistics.median(row["open_s"] for row in observations) <= 0.250
        assert max(row["hold_s"] for row in observations) <= 1.0
