import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS, connect, init_store, insert_memory, run_store, store_env


OBSERVE_OPEN = """
import sys
sys.path.insert(0, sys.argv[1])
from storelib import schema

original = schema._normalize_content
seen = []
def observe(content):
    seen.append(content)
    return original(content)
schema._normalize_content = observe
conn = schema.connect()
try:
    schema._prepare_store(conn)
finally:
    conn.close()
assert set(seen) == {"  New   raw BODY  ", "  Updated   BODY  "}, seen
"""


def test_raw_writes_mark_and_repair_changed_rows_only():
    with tempfile.TemporaryDirectory(prefix="zmem-546-change-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        with connect(store) as conn:
            insert_memory(conn, "changed", "before", "before")
            insert_memory(conn, "untouched", "untouched body", "untouched body")
        conn.close()
        assert run_store(store, "stats").returncode == 0

        with connect(store) as conn:
            insert_memory(conn, "raw", "  New   raw BODY  ", None)
            conn.execute("UPDATE memory SET content = ? WHERE id = ?", ("  Updated   BODY  ", "changed"))
            conn.execute("UPDATE memory SET surfaced_count = 1 WHERE id = 'untouched'")
            dirty = conn.execute(
                "SELECT memory_id FROM memory_content_norm_dirty ORDER BY memory_id"
            ).fetchall()
            assert [row[0] for row in dirty] == ["changed", "raw"]
        conn.close()

        subprocess.run(
            [sys.executable, "-X", "utf8", "-c", OBSERVE_OPEN, str(SCRIPTS)],
            env=store_env(store), timeout=30, check=True,
        )
        with connect(store) as conn:
            norms = dict(conn.execute("SELECT id, content_norm FROM memory"))
            assert norms == {"changed": "updated body", "raw": "new raw body", "untouched": "untouched body"}
            assert conn.execute("SELECT count(*) FROM memory_content_norm_dirty").fetchone()[0] == 0
        conn.close()


def test_dirty_backlog_is_byte_bounded_and_keeps_unprocessed_marks():
    with tempfile.TemporaryDirectory(prefix="zmem-546-dirty-budget-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            for index in range(3):
                insert_memory(conn, f"dirty-{index}", "x" * 1_250_000, None)
            conn.commit()
        finally:
            conn.close()

        result = run_store(store, "stats")
        assert result.returncode == 0, result.stderr
        conn = connect(store)
        try:
            dirty = [row[0] for row in conn.execute(
                "SELECT memory_id FROM memory_content_norm_dirty ORDER BY memory_id"
            )]
            pending = [row[0] for row in conn.execute(
                "SELECT id FROM memory WHERE content_norm IS NULL ORDER BY id"
            )]
            assert len(dirty) == 2
            assert dirty == pending
        finally:
            conn.close()
