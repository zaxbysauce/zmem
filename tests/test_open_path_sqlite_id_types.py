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


def test_raw_writes_preserve_nullable_and_non_utf8_text_ids_with_explicit_conflict_mode():
    with tempfile.TemporaryDirectory(prefix="zmem-546-raw-id-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            insert_memory(conn, None, "First NULL ID", None)
            insert_memory(conn, None, "Second NULL ID", None)
            insert_memory(conn, "convert-me", "Non-UTF8 TEXT ID", None)
            insert_memory(conn, "ordinary", "Ordinary ID", None)
            conn.execute("UPDATE memory SET id=CAST(X'80' AS TEXT) WHERE id='convert-me'")
            conn.execute("UPDATE OR ROLLBACK memory SET content_norm='STALE'")
            conn.commit()

            opened = run_store(store, "stats")
            assert opened.returncode == 0, opened.stderr
            assert [tuple(row) for row in conn.execute(
                "SELECT typeof(id), hex(id), content_norm FROM memory ORDER BY rowid"
            )] == [
                ("null", "", "first null id"),
                ("null", "", "second null id"),
                ("text", "80", "non-utf8 text id"),
                ("text", "6F7264696E617279", "ordinary id"),
            ]
            assert conn.execute("SELECT COUNT(*) FROM memory_content_norm_dirty").fetchone()[0] == 0
        finally:
            conn.close()


def test_forced_pass_resumes_after_a_nullable_rowid():
    with tempfile.TemporaryDirectory(prefix="zmem-546-null-pass-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            conn.execute("DROP TRIGGER memory_cnorm_ai")
            insert_memory(conn, None, "X" * (2 * 1024 * 1024 + 1), None)
            insert_memory(conn, None, "Second NULL ID", None)
            insert_memory(conn, "ordinary", "Ordinary ID", "STALE")
            conn.commit()

            first_open = run_store(store, "stats")
            assert first_open.returncode == 0, first_open.stderr
            report, _ = json.JSONDecoder().raw_decode(doctor_text(store).lstrip())
            progress = next(check for check in report["checks"] if check["id"] == "content-norm-pass")
            assert progress["details"]["remaining_rows"] == 2

            second_open = run_store(store, "stats")
            assert second_open.returncode == 0, second_open.stderr
            assert [row[0] for row in conn.execute(
                "SELECT content_norm FROM memory WHERE length(content) < 100 ORDER BY rowid"
            )] == ["second null id", "ordinary id"]
            assert conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_pass_state'"
            ).fetchone()[0] == "done"
            assert conn.execute(
                "SELECT value FROM meta WHERE key='content_norm_verify_163'"
            ).fetchone()[0] == "done"
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
