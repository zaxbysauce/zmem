from __future__ import annotations

import json
import tempfile
from pathlib import Path

from check_utils import (
    add_memory,
    bind_payload,
    capture_delivery_and_ack,
    combined,
    complete_payload,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    stop_at_missing_surface,
)


def lower_trust_below_floor(temp_root: Path, memory_id: str) -> None:
    """Use public contradiction links to cross the configured 0.2 floor."""
    for index in range(9):
        other = add_memory(
            temp_root,
            f"trust-floor contradiction {index}",
            namespace="project:labels",
        )
        result = run_store(
            temp_root,
            "contradict",
            "--id",
            memory_id,
            "--id",
            other,
            "--reason",
            "training trust-floor fixture",
        )
        assert result.returncode == 0, combined(result)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-135-c4-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "export-training", "-h")
        stop_at_missing_surface(probe, "export-training", "AC4_MISSING_LABEL_EXPORT")

        fixture = load_fixture("complete_capture.json")
        assert isinstance(fixture, dict)
        cases = [
            ("positive", 3, 0, "candidate_positive", "401"),
            ("negative", 0, 2, "candidate_negative", "402"),
            ("below-floor", 1, 0, "excluded", "403"),
        ]
        expected: dict[str, str] = {}
        for name, applied, violated, label, suffix in cases:
            source = seed_memory_evidence_episode(
                temp_root,
                session_id="session-135-labels-" + name,
                suffix=suffix,
                namespace="project:labels",
                content=f"label threshold {name}",
            )
            memory_id = source["memory_id"]
            if label == "excluded":
                lower_trust_below_floor(temp_root, memory_id)
            for _ in range(applied):
                result = run_store(
                    temp_root, "feedback", "--id", memory_id, "--applied"
                )
                assert result.returncode == 0, combined(result)
            for _ in range(violated):
                result = run_store(
                    temp_root, "feedback", "--id", memory_id, "--violated"
                )
                assert result.returncode == 0, combined(result)

            payload = bind_payload(dict(fixture), source)
            payload.update(
                {
                    "delivery_snapshot_id": {
                        "positive": "00000000-0000-4000-8000-000000000414",
                        "negative": "00000000-0000-4000-8000-000000000415",
                        "below-floor": "00000000-0000-4000-8000-000000000416",
                    }[name],
                    "task_id": "task-135-labels-" + name,
                    "source_event_id": "event-135-labels-" + name,
                    # Keep the three label candidates semantically distinct so
                    # lineage-scoped deduplication cannot collapse the cases.
                    "prompt": {
                        "positive": "Deploy the signed binary to the canary ring.",
                        "negative": "Reject the malformed fixture and retain the audit trail.",
                        "below-floor": "Explain why stale recall is excluded below the trust floor.",
                    }[name],
                    "assistant_response": {
                        "positive": "Canary verification passed with the signed digest.",
                        "negative": "Violation count reached the safety threshold.",
                        "below-floor": "The low-trust candidate remains unavailable.",
                    }[name],
                    "outcome_kind": "test",
                    "outcome_value": {
                        "positive": "positive_pass",
                        "negative": "negative_violation",
                        "below-floor": "trust_floor_excluded",
                    }[name],
                }
            )
            capture_delivery_and_ack(
                temp_root,
                payload,
                sentinel="AC4_MISSING_LABEL_EXPORT",
            )
            result = complete_payload(
                temp_root,
                payload,
                sentinel="AC4_MISSING_LABEL_EXPORT",
            )
            assert result.returncode == 0, combined(result)
            expected[memory_id] = label

        output = temp_root / "training"
        result = run_store(
            temp_root,
            "export-training",
            str(output),
            "--snapshot-id",
            "ac4-labels",
            "--reviewer-confirmed",
        )
        assert result.returncode == 0, combined(result)
        rows_path = output / "sft-000.parquet"
        assert rows_path.is_file(), f"missing {rows_path}"
        import pyarrow.parquet as parquet

        rows = parquet.read_table(rows_path).to_pylist()
        manifest = json.loads(
            (output / "manifest.json").read_text(encoding="utf-8")
        )
        manifest_text = json.dumps(manifest, sort_keys=True)
        for memory_id, expected_label in expected.items():
            matches = [
                row for row in rows if memory_id in row.get("source_memory_ids", [])
            ]
            if expected_label == "excluded":
                assert not matches, (memory_id, matches)
                # Excluded candidates are accounted for by internal reason in
                # the manifest.  deletion-map.json is reserved for removed
                # dedup rows and has no keeper for this candidate.
                assert "trust_floor" in manifest_text
            else:
                assert len(matches) == 1, (memory_id, matches)
                assert matches[0]["label_status"] == expected_label
                if expected_label == "candidate_negative":
                    assert matches[0]["exclusion_reason"] == "violated_count"
    print("AC4_OK_label_thresholds_and_trust_floor")


if __name__ == "__main__":
    main()
