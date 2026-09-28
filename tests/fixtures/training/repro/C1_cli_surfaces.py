from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from check_utils import ROOT, combined, export_empty_contract, run_store, stop_at_missing_surface


def tree_digest(path: Path) -> str:
    if not path.exists():
        return "ABSENT"
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _sqlite_value(value):
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def canonical_store_snapshot(path: Path) -> dict[str, object]:
    """Capture domain schema/content while excluding the binding registry."""
    ignored = {"training_export_snapshot_binding", "sqlite_sequence"}
    conn = sqlite3.connect(path)
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if str(row[0]) not in ignored
        ]
        snapshot: dict[str, object] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            try:
                columns = tuple(
                    row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")
                )
                rows = [
                    tuple(_sqlite_value(value) for value in row)
                    for row in conn.execute(f"SELECT * FROM {quoted}")
                ]
            except sqlite3.OperationalError as exc:
                # The optional vec0 extension is unavailable to the validator
                # process.  Keep its schema in the comparison and mark its
                # opaque contents as inaccessible; canonical source tables
                # remain fully content-compared.
                assert "no such module: vec0" in str(exc), (table, exc)
                columns = ("<opaque-virtual-table>",)
                rows = [("<opaque-virtual-table>",)]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
            snapshot[table] = {
                "schema": schema,
                "columns": columns,
                "rows": rows,
            }
        return snapshot
    finally:
        conn.close()


def snapshot_binding(path: Path, snapshot_id: str) -> tuple[str, str]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(training_export_snapshot_binding)"
        )]
        assert columns == ["snapshot_id", "binding_sha256", "created_at"], columns
        rows = conn.execute(
            "SELECT snapshot_id, binding_sha256, created_at "
            "FROM training_export_snapshot_binding"
        ).fetchall()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["snapshot_id"] == snapshot_id, dict(row)
        binding = str(row["binding_sha256"])
        assert re.fullmatch(r"[0-9a-fA-F]{64}", binding), dict(row)
        assert row["created_at"], dict(row)
        serialized = json.dumps(dict(row), sort_keys=True).lower()
        assert not any(marker in serialized for marker in (
            "capture", "prompt", "response", "secret", "bearer", "@"
        )), serialized
        return str(row["snapshot_id"]), binding
    finally:
        conn.close()


def preserve_ledger_contract(temp_root: Path) -> None:
    sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
    from storelib import delivery_ledger

    # This is the same directory exported through ZMEM_DATA by run_store;
    # preserve the real delivery ledger bytes across the export.
    data_dir = str(temp_root / "data")
    session_id = "session-135-ac1"
    row = {
        "id": "00000000-0000-4000-8000-000000000135",
        "content": "preserve ledger matcher",
        "tags": ["test"],
        "entities": [{"name": "ledger"}],
    }
    delivery_ledger.record(data_dir, session_id, [row], "pretool", now=1790424000.0)
    ledger_file = Path(delivery_ledger.ledger_path(data_dir, session_id))
    document = __import__("json").loads(ledger_file.read_text(encoding="utf-8"))
    entries = document["entries"]
    assert set(entries[0]) == {"id", "moment", "ts", "text"}
    assert entries[0]["text"] == "preserve ledger matcher test ledger"

    delivery_ledger.record_feedback_event(
        data_dir,
        session_id,
        "event-135-ac1",
        row["id"],
        "applied",
        2,
        None,
        now="2026-09-26T12:00:00Z",
    )
    feedback_file = Path(delivery_ledger.feedback_event_path(data_dir, session_id))
    record = __import__("json").loads(
        next(line for line in feedback_file.read_text(encoding="utf-8").splitlines() if line)
    )
    assert set(record) == {
        "event_id", "evidence_id", "memory_id", "overlap",
        "session_id", "timestamp", "verdict",
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-135-c1-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "export-training", "-h")
        stop_at_missing_surface(
            probe, "export-training", "AC1_MISSING_EXPORT_TRAINING"
        )

        # Seed the preserving sidecars before the export-only snapshot.
        preserve_ledger_contract(temp_root)
        init_result = run_store(temp_root, "init")
        assert init_result.returncode == 0, combined(init_result)
        store_before = canonical_store_snapshot(temp_root / "store.sqlite")
        data_before = tree_digest(temp_root / "data")

        output = export_empty_contract(temp_root, "ac1-empty")
        assert output.is_dir()
        assert canonical_store_snapshot(temp_root / "store.sqlite") == store_before
        bound_snapshot_id, _binding_sha256 = snapshot_binding(
            temp_root / "store.sqlite", "ac1-empty"
        )
        assert bound_snapshot_id == "ac1-empty"
        assert tree_digest(temp_root / "data") == data_before

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["ZMEM_STORE"] = str(temp_root / "test-store.sqlite")
        env["ZMEM_DATA"] = str(temp_root / "test-data")
        test_result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests/test_training_views.py"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert test_result.returncode == 0, combined(test_result)
    print("AC1_OK_suite_read_only_snapshot_binding_and_ledger_preserved")


if __name__ == "__main__":
    main()
