import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS, backup_snapshot, connect, init_store, insert_memory, store_env


_OPENER = r"""
import sys
import time
from pathlib import Path
scripts, entered, resume, read_done = map(Path, sys.argv[1:])
sys.path.insert(0, str(scripts))
from storelib import cli, schema

original_step = schema._content_norm_catchup_step
original_stats = cli.stats

def observed_step(conn):
    entered.touch(exist_ok=False)
    deadline = time.monotonic() + 30.0
    while not resume.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("restore coordination signal did not arrive")
        time.sleep(0.01)
    return original_step(conn)

def observed_stats(conn):
    result = original_stats(conn)
    read_done.touch(exist_ok=False)
    return result

schema._content_norm_catchup_step = observed_step
cli.stats = observed_stats
sys.argv = ["store.py", "stats"]
cli.main()
"""

_RESTORE = r"""
import sys
from pathlib import Path
scripts, maintenance, read_done, snapshot, backup_dir = map(Path, sys.argv[1:])
sys.path.insert(0, str(scripts))
from storelib import backup, cli
original_maintenance = backup._acquire_maintenance_lock_with_liveness
original_schema = backup._acquire_schema_lock_with_liveness

def observed_maintenance():
    token = original_maintenance()
    if token is not None:
        maintenance.touch(exist_ok=False)
    return token

def checked_schema(*args, **kwargs):
    assert read_done.exists(), "restore must drain the pending opener before requesting the schema lock"
    return original_schema(*args, **kwargs)

backup._acquire_maintenance_lock_with_liveness = observed_maintenance
backup._acquire_schema_lock_with_liveness = checked_schema
sys.argv = ["store.py", "restore", "--from", str(snapshot), "--force", "--out-dir", str(backup_dir)]
cli.main()
"""


def _wait_for(path: Path, process, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"signal not observed: {path.name}, child status {process.poll()}")


def test_restore_drains_the_pending_opener_before_schema_and_replaces_complete_state():
    with tempfile.TemporaryDirectory(prefix="zmem-546-restore-") as raw:
        root = Path(raw)
        store = root / "store.sqlite"
        backup_dir = root / "backups"
        init_store(store)
        conn = connect(store)
        try:
            insert_memory(conn, "restore-proof", "live-original", "live-original")
            conn.commit()
        finally:
            conn.close()
        snapshot = backup_snapshot(store, backup_dir)
        conn = connect(snapshot)
        try:
            conn.execute(
                "UPDATE memory SET content = ?, content_norm = ? WHERE id = ?",
                ("restored-state", "restored-state", "restore-proof"),
            )
            conn.commit()
        finally:
            conn.close()

        conn = connect(store)
        try:
            conn.execute("DROP TRIGGER memory_cnorm_ai")
            conn.commit()
        finally:
            conn.close()

        entered, resume, read_done, maintenance = (
            root / name for name in ("entered", "resume", "read-done", "maintenance")
        )
        env = store_env(store)
        opener = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _OPENER, str(SCRIPTS),
             str(entered), str(resume), str(read_done)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        restore = None
        try:
            _wait_for(entered, opener)
            restore = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", _RESTORE, str(SCRIPTS),
                 str(maintenance), str(read_done), str(snapshot), str(backup_dir)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _wait_for(maintenance, restore)
            resume.touch(exist_ok=False)
            assert opener.wait(timeout=45) == 0
            assert restore.wait(timeout=45) == 0
        finally:
            for child in (restore, opener):
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=10)

        conn = connect(store)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            row = conn.execute(
                "SELECT content, content_norm FROM memory WHERE id = 'restore-proof'"
            ).fetchone()
            assert tuple(row) == ("restored-state", "restored-state")
        finally:
            conn.close()
