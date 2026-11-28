import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS, connect, init_store, insert_memory, run_store, store_env


_STOP_AFTER_BATCH = """
from pathlib import Path
import sys
import time

scripts, done = sys.argv[1:]
sys.path.insert(0, scripts)
from storelib import schema

original = schema._content_norm_catchup_step
def observed_step(conn):
    pending = original(conn)
    assert pending, "the oversized row should leave the remaining rows pending"
    Path(done).touch(exist_ok=False)
    time.sleep(45)
    return pending

schema._content_norm_catchup_step = observed_step
conn = schema.connect()
try:
    schema._prepare_store(conn)
finally:
    conn.close()
"""


def _null_norm_ids(store: Path) -> list[str]:
    conn = connect(store)
    try:
        return [row[0] for row in conn.execute(
            "SELECT id FROM memory WHERE content_norm IS NULL ORDER BY id"
        )]
    finally:
        conn.close()


def _kill_after_first_batch(store: Path):
    done = store.with_name("first-batch")
    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", _STOP_AFTER_BATCH, str(SCRIPTS), str(done)],
        env=store_env(store), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as child:
        try:
            deadline = time.monotonic() + 30
            while not done.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert done.is_file(), "first durable catch-up batch did not complete"
            assert child.poll() is None, "opener exited before the crash boundary"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_forced_baseline_advances_one_byte_bounded_batch_across_process_death():
    with tempfile.TemporaryDirectory(prefix="zmem-546-catchup-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            insert_memory(conn, "00-oversized", "o" * (3 * 1024 * 1024), None)
            for index in range(1, 9):
                insert_memory(conn, f"{index:02d}-small", "s" * (512 * 1024), None)
            conn.execute("DELETE FROM memory_content_norm_dirty")
            conn.execute("DROP TRIGGER memory_cnorm_ai")
            conn.execute("DROP TRIGGER memory_cnorm_au")
            conn.execute("DROP TRIGGER memory_cnorm_ad")
            conn.commit()
        finally:
            conn.close()

        before = _null_norm_ids(store)
        assert len(before) == 9
        _kill_after_first_batch(store)
        remaining = _null_norm_ids(store)
        assert remaining == before[1:], "the oversized row must be the sole committed batch member"

        for _ in range(20):
            if not remaining:
                break
            result = run_store(store, "stats", timeout=60)
            assert result.returncode == 0, result.stderr
            after = _null_norm_ids(store)
            assert 1 <= len(remaining) - len(after) <= 4
            remaining = after
        assert not remaining
