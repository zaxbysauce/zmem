from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from check_utils import (
    bind_payload,
    capture_delivery_and_ack,
    combined,
    complete_payload,
    export_empty_contract,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    surface_exists_or_exit,
)


def main() -> None:
    fixture = load_fixture("missing_governance.json")
    assert isinstance(fixture, dict)
    assert "content_license" not in fixture
    with tempfile.TemporaryDirectory(prefix="zmem-135-c6-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "capture-training-completion", "-h")
        surface_exists_or_exit(
            probe,
            "capture-training-completion",
            "AC6_MISSING_CAPTURE_REFUSAL",
            strict=True,
        )
        if probe.returncode == 0:
            # Head path: all prerequisites are valid except the one governance field.
            source = seed_memory_evidence_episode(
                temp_root,
                session_id=str(fixture["session_id"]),
                suffix="601",
                namespace="project:refusal",
                content="valid refusal prerequisite memory",
            )
            payload = bind_payload(dict(fixture), source)
            payload["delivery_snapshot_id"] = "00000000-0000-4000-8000-000000000613"
            payload["task_id"] = "task-135-missing-license"
            payload["source_event_id"] = "event-135-missing-license"
            payload["project_key"] = "refusal"
            payload["content_license"] = "internal-review"
            receipt = capture_delivery_and_ack(
                temp_root, payload, sentinel="AC6_MISSING_CAPTURE_REFUSAL"
            )
            capture_id = str(receipt["capture_id"])
            incomplete = dict(payload)
            incomplete.pop("content_license")
            result = complete_payload(
                temp_root,
                incomplete,
                sentinel="AC6_MISSING_CAPTURE_REFUSAL",
            )
            assert result.returncode == 1, combined(result)
            assert result.stderr == "missing governance field: content_license\n", (
                "missing exact governance refusal stderr: " + combined(result)
            )

            conn = sqlite3.connect(temp_root / "store.sqlite")
            try:
                completion = conn.execute(
                    "SELECT 1 FROM training_capture_completion WHERE capture_id=?",
                    (capture_id,),
                ).fetchone()
                associations = conn.execute(
                    "SELECT memory_id FROM memory_evidence WHERE evidence_id=?",
                    (source["evidence_id"],),
                ).fetchall()
            finally:
                conn.close()
            assert completion is None, "refusal committed a completion row"
            assert not associations, "refusal committed a memory/evidence association"

            # An emitted/acknowledged orphan may remain, but no complete event
            # or memory/evidence export binding may be committed.
            output = export_empty_contract(temp_root, "ac6-refused")
            assert output.is_dir()
            # export_empty_contract already proves schema-only Parquet, zero
            # rows, zero manifest counts, and the deletion map.  A schema-only
            # Parquet file is intentionally nonzero bytes.
    print("AC6_OK_missing_governance_refusal_without_exportable_partial")


if __name__ == "__main__":
    main()
