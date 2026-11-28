import tempfile
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import connect, init_store, insert_memory, run_store

_TRIGGER_NAMES = {"memory_cnorm_ai", "memory_cnorm_au", "memory_cnorm_ad"}


def test_missing_change_detection_triggers_are_recreated_and_schedule_a_pass():
    with tempfile.TemporaryDirectory(prefix="zmem-546-trigger-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            for name in _TRIGGER_NAMES:
                conn.execute(f"DROP TRIGGER {name}")
            insert_memory(conn, "missed", "Needs Repair", None)
            conn.commit()
        finally:
            conn.close()

        result = run_store(store, "stats")
        assert result.returncode == 0, result.stderr
        conn = connect(store)
        try:
            triggers = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            assert _TRIGGER_NAMES <= triggers
            assert conn.execute("SELECT content_norm FROM memory WHERE id = 'missed'").fetchone()[0] == "needs repair"
        finally:
            conn.close()
