import json
import tempfile
from pathlib import Path

import _store_guard  # noqa: F401

from _issue546_helpers import init_store, run_store


def test_open_records_lock_wait_hold_and_catchup_timings():
    with tempfile.TemporaryDirectory(prefix="zmem-546-observe-") as raw:
        root = Path(raw)
        store = root / "store.sqlite"
        init_store(store)
        result = run_store(store, "stats")
        assert result.returncode == 0, result.stderr
        records = []
        for path in (root / "ops").glob("*.jsonl"):
            assert path.stat().st_size <= 128 * 1024
            records.extend(
                entry for entry in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
                if entry.get("event") == "store_open"
            )
        assert len(records) == 2
        for record in records:
            for key in ("lock_wait_ms", "lock_hold_ms", "catchup_ms"):
                assert type(record[key]) in (int, float) and record[key] >= 0
            assert type(record["catchup_pending"]) is bool
