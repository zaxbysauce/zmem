import json
import tempfile
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import connect, doctor_text, init_store, insert_memory, run_store


def test_dirty_repair_distinguishes_text_and_blob_ids():
    with tempfile.TemporaryDirectory(prefix="zmem-546-dirty-id-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            insert_memory(conn, "same", "Canonical text", "canonical text")
            insert_memory(conn, b"same", "Repair this BLOB ID", None)
            insert_memory(conn, b"\xff", "Also repair this ID", "outdated")
            conn.commit()
            opened = run_store(store, "stats")
            assert opened.returncode == 0, opened.stderr
            assert {
                row[0]: row[1] for row in conn.execute("SELECT id, content_norm FROM memory")
            } == {
                "same": "canonical text",
                b"same": "repair this blob id",
                b"\xff": "also repair this id",
            }
            assert conn.execute("SELECT COUNT(*) FROM memory_content_norm_dirty").fetchone()[0] == 0
        finally:
            conn.close()


def test_forced_pass_resumes_after_a_blob_id():
    with tempfile.TemporaryDirectory(prefix="zmem-546-pass-id-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            conn.execute("DROP TRIGGER memory_cnorm_ai")
            insert_memory(conn, b"first", "X" * (2 * 1024 * 1024 + 1), None)
            insert_memory(conn, b"\xff", "Second BLOB ID", None)
            conn.commit()

            first_open = run_store(store, "stats")
            assert first_open.returncode == 0, first_open.stderr
            cursor = conn.execute(
                "SELECT value, typeof(value) FROM meta WHERE key='content_norm_pass_after_id'"
            ).fetchone()
            assert cursor is not None
            assert tuple(cursor) == (b"first", "blob")
            report, _ = json.JSONDecoder().raw_decode(doctor_text(store).lstrip())
            progress = next(check for check in report["checks"] if check["id"] == "content-norm-pass")
            assert progress["details"]["remaining_rows"] == 1

            second_open = run_store(store, "stats")
            assert second_open.returncode == 0, second_open.stderr
            assert conn.execute(
                "SELECT content_norm FROM memory WHERE id=?", (b"\xff",)
            ).fetchone()[0] == "second blob id"
            assert conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_pass_state'"
            ).fetchone()[0] == "done"
        finally:
            conn.close()
