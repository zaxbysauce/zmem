from __future__ import annotations

import tempfile
from pathlib import Path

from check_utils import (
    bind_payload,
    capture_delivery_and_ack,
    combined,
    complete_payload,
    json_result,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    stop_at_missing_surface,
    write_payload,
)


PREFERENCE_COLUMNS = [
    "task_id",
    "project_key",
    "session_id",
    "episode_id",
    "prompt",
    "context_fence",
    "chosen",
    "rejected",
    "update_of",
    "supersede_reason",
    "source_memory_ids",
    "source_event_ids",
    "evidence_ref",
    "consent_scope",
    "content_license",
    "redaction_status",
    "redaction_policy_version",
    "split_key",
    "transform_version",
    "row_checksum",
]


def update_memory(temp_root: Path, memory_id: str, content: str, reason: str) -> str:
    result = run_store(
        temp_root,
        "update",
        "--id",
        memory_id,
        "--content",
        content,
        "--reason",
        reason,
        "--json",
    )
    assert result.returncode == 0, combined(result)
    return str(json_result(result)["id"])


def update_memory_default(temp_root: Path, memory_id: str, content: str) -> str:
    """Exercise the ordinary update path, whose default reason is ``updated``."""
    result = run_store(
        temp_root,
        "update",
        "--id",
        memory_id,
        "--content",
        content,
        "--json",
    )
    assert result.returncode == 0, combined(result)
    return str(json_result(result)["id"])


def main() -> None:
    fixture = load_fixture("reviewer_completion.json")
    assert isinstance(fixture, dict)
    with tempfile.TemporaryDirectory(prefix="zmem-135-c3-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "export-training", "-h")
        stop_at_missing_surface(
            probe, "export-training", "AC3_MISSING_REVIEWER_EXPORT"
        )

        source = seed_memory_evidence_episode(
            temp_root,
            session_id="session-135-correction",
            suffix="311",
            namespace="project:preferences",
            content="original reviewed procedure",
            evidence_kind="correction",
        )
        predecessor_id = source["memory_id"]
        chosen_id = update_memory(
            temp_root,
            predecessor_id,
            "corrected reviewed procedure",
            "explicit correction",
        )
        result = run_store(
            temp_root,
            "episode-add",
            "--episode",
            source["episode_id"],
            "--memory",
            chosen_id,
            "--json",
        )
        assert result.returncode == 0, combined(result)
        corrected = dict(fixture)
        corrected.update(
            {
                "delivery_snapshot_id": "00000000-0000-4000-8000-000000000313",
                "task_id": "task-135-correction",
                "project_key": "preferences",
                "episode_id": source["episode_id"],
                "update_of": predecessor_id,
                "supersede_reason": "explicit correction",
                "chosen_memory_id": chosen_id,
                "rejected_memory_id": predecessor_id,
                "correction_closeout": True,
            }
        )
        corrected = bind_payload(corrected, {**source, "memory_id": chosen_id})
        corrected.pop("reviewer_id", None)
        corrected.pop("reviewer_confirmed", None)
        # Reviewer-only fields are consumed by the completion adapter, not delivery.
        delivery = {
            key: value
            for key, value in corrected.items()
            if key not in {
                "reviewer_id", "correction_closeout", "chosen_memory_id",
                "rejected_memory_id", "reviewer_confirmed", "correction_chain_id",
                "update_of", "supersede_reason",
            }
        }
        receipt = capture_delivery_and_ack(
            temp_root, delivery, sentinel="AC3_MISSING_REVIEWER_EXPORT"
        )
        capture_id = str(receipt["capture_id"])
        result = complete_payload(
            temp_root,
            corrected,
            sentinel="AC3_MISSING_REVIEWER_EXPORT",
        )
        assert result.returncode == 0, combined(result)

        # Reviewer acceptance is a separate store transition.  It uses the
        # exact delivery/evidence binding and a distinct configured local
        # reviewer identity; host JSON cannot self-assert this gate.
        review_path = write_payload(
            temp_root,
            "review.json",
            {
                "delivery_snapshot_id": corrected["delivery_snapshot_id"],
                "evidence_id": source["evidence_id"],
                "reviewer_id": "reviewer-135-independent",
            },
        )
        review = run_store(
            temp_root,
            "capture-training-review",
            "--input",
            str(review_path),
            env_overrides={
                "ZMEM_TRAINING_CALLER_ID": "reviewer-135-independent",
                "ZMEM_TRAINING_REVIEWER_IDS": "reviewer-135-independent",
            },
        )
        stop_at_missing_surface(
            review, "capture-training-review", "AC3_MISSING_REVIEWER_EXPORT"
        )
        assert review.returncode == 0, combined(review)
        review_result = json_result(review)
        assert review_result == {
            "capture_id": capture_id,
            "evidence_id": source["evidence_id"],
            "state": "reviewed",
        }, review_result

        output = temp_root / "training"
        result = run_store(
            temp_root,
            "export-training",
            str(output),
            "--snapshot-id",
            "ac3-correction",
            "--reviewer-confirmed",
        )
        stop_at_missing_surface(
            result, "export-training", "AC3_MISSING_REVIEWER_EXPORT"
        )
        assert result.returncode == 0, combined(result)
        import pyarrow.parquet as parquet

        rows = parquet.read_table(output / "preferences-000.parquet").to_pylist()
        assert parquet.read_table(output / "preferences-000.parquet").schema.names == PREFERENCE_COLUMNS
        matches = [
            row for row in rows
            if row.get("task_id") == capture_id
        ]
        assert len(matches) == 1, matches
        row = matches[0]
        assert row["task_id"] == capture_id
        assert row["update_of"] == predecessor_id
        assert row["supersede_reason"] == "explicit correction"
        assert row["chosen"]["memory_id"] == chosen_id
        assert row["rejected"]["memory_id"] == predecessor_id
        assert row["source_memory_ids"] == [chosen_id]
        assert corrected["source_event_id"] in row["source_event_ids"]

        # A temporal refresh through the ordinary update path, even when a
        # reviewer accepts the completion, must never form a pair.  This is a
        # real update chain with the default ``updated`` reason, rather than a
        # chainless outcome-value-only fixture.
        temporal_source = seed_memory_evidence_episode(
            temp_root,
            session_id="session-135-temporal",
            suffix="312",
            namespace="project:preferences",
            content="temporal procedure",
            evidence_kind="turn",
        )
        temporal_predecessor_id = temporal_source["memory_id"]
        temporal_chosen_id = update_memory_default(
            temp_root,
            temporal_predecessor_id,
            "temporal refreshed procedure",
        )
        result = run_store(
            temp_root,
            "episode-add",
            "--episode",
            temporal_source["episode_id"],
            "--memory",
            temporal_chosen_id,
            "--json",
        )
        assert result.returncode == 0, combined(result)
        temporal = dict(fixture)
        temporal.update(
            {
                "delivery_snapshot_id": "00000000-0000-4000-8000-000000000314",
                "task_id": "task-135-temporal",
                "project_key": "preferences",
                "episode_id": temporal_source["episode_id"],
                "outcome_kind": "reviewer_acceptance",
                "outcome_value": "accepted",
                "correction_closeout": False,
                "update_of": temporal_predecessor_id,
                "supersede_reason": "updated",
            }
        )
        temporal = bind_payload(
            temporal, {**temporal_source, "memory_id": temporal_chosen_id}
        )
        temporal.pop("reviewer_id", None)
        temporal.pop("reviewer_confirmed", None)
        temporal_delivery = {
            key: value
            for key, value in temporal.items()
            if key not in {
                "reviewer_confirmed", "correction_chain_id", "update_of",
                "supersede_reason", "reviewer_id", "correction_closeout",
            }
        }
        temporal_receipt = capture_delivery_and_ack(
            temp_root, temporal_delivery, sentinel="AC3_MISSING_REVIEWER_EXPORT"
        )
        temporal_capture_id = str(temporal_receipt["capture_id"])
        result = complete_payload(
            temp_root,
            temporal,
            sentinel="AC3_MISSING_REVIEWER_EXPORT",
        )
        assert result.returncode == 0, combined(result)

        temporal_review_path = write_payload(
            temp_root,
            "temporal-review.json",
            {
                "delivery_snapshot_id": temporal["delivery_snapshot_id"],
                "evidence_id": temporal_source["evidence_id"],
                "reviewer_id": "reviewer-135-temporal",
            },
        )
        temporal_review = run_store(
            temp_root,
            "capture-training-review",
            "--input",
            str(temporal_review_path),
            env_overrides={
                "ZMEM_TRAINING_CALLER_ID": "reviewer-135-temporal",
                "ZMEM_TRAINING_REVIEWER_IDS": "reviewer-135-temporal",
            },
        )
        stop_at_missing_surface(
            temporal_review, "capture-training-review", "AC3_MISSING_REVIEWER_EXPORT"
        )
        assert temporal_review.returncode == 0, combined(temporal_review)

        temporal_output = temp_root / "training-temporal"
        result = run_store(
            temp_root,
            "export-training",
            str(temporal_output),
            "--snapshot-id",
            "ac3-temporal",
            "--reviewer-confirmed",
        )
        assert result.returncode == 0, combined(result)
        temporal_rows = parquet.read_table(temporal_output / "preferences-000.parquet").to_pylist()
        assert not any(
            row.get("task_id") == temporal_capture_id
            for row in temporal_rows
        )
    print("AC3_OK_explicit_correction_preferences_only")


if __name__ == "__main__":
    main()
