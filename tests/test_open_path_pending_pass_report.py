import json
import tempfile
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import connect, doctor_text, init_store, insert_memory, run_store


def test_doctor_reports_pending_pass_and_remaining_batches():
    with tempfile.TemporaryDirectory(prefix="zmem-546-doctor-") as raw:
        store = Path(raw) / "store.sqlite"
        init_store(store)
        conn = connect(store)
        try:
            conn.execute("DROP TRIGGER memory_cnorm_ai")
            for index in range(8):
                insert_memory(conn, f"pending-{index}", "x" * 550_000, None)
            conn.commit()
        finally:
            conn.close()
        opened = run_store(store, "stats")
        assert opened.returncode == 0, opened.stderr
        report, _ = json.JSONDecoder().raw_decode(doctor_text(store).lstrip())
        check = next(item for item in report["checks"] if item["id"] == "content-norm-pass")
        assert check["status"] == "warn"
        assert check["details"]["state"] == "forced"
        remaining = check["details"]["remaining_batches"]
        assert type(remaining) is int and 1 <= remaining < 8
