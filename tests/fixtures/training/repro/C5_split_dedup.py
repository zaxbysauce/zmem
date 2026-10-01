from __future__ import annotations

import json
import tempfile
from pathlib import Path

from check_utils import (
    bind_payload,
    capture_delivery_and_ack,
    combined,
    complete_payload,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    stop_at_missing_surface,
)


def main() -> None:
    payloads = load_fixture("split_dedup_captures.json")
    assert isinstance(payloads, list)
    assert len(payloads) >= 4
    assert payloads[0]["task_id"] == payloads[1]["task_id"]
    assert payloads[0]["assistant_response"] == payloads[1]["assistant_response"]
    with tempfile.TemporaryDirectory(prefix="zmem-135-c5-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "export-training", "-h")
        stop_at_missing_surface(probe, "export-training", "AC5_MISSING_EXPORT")

        sources = []
        capture_ids = []
        raw_host_task_ids = []
        for index, raw_payload in enumerate(payloads):
            namespace = "project:split-same" if index < 3 else "project:split-other"
            source = seed_memory_evidence_episode(
                temp_root,
                session_id=str(raw_payload["session_id"]),
                suffix=str(501 + index),
                namespace=namespace,
                content=f"split fixture source {index}",
            )
            payload = bind_payload(dict(raw_payload), source)
            # The first two are exact duplicates. The third shares the same
            # lineage group but has distinct redacted event text. The other
            # project is deliberately given the same event text as the first
            # row so global deduplication would incorrectly delete it.
            if index < 3:
                payload["task_id"] = "task-135-split-group"
                payload["project_key"] = "split-same"
                payload["session_id"] = "session-135-split"
                payload["episode_id"] = sources[0]["episode_id"] if sources else source["episode_id"]
            else:
                payload["task_id"] = "task-135-split-other"
                payload["project_key"] = "split-other"
                payload["prompt"] = payloads[0]["prompt"]
                payload["assistant_response"] = payloads[0]["assistant_response"]
                payload["outcome_kind"] = payloads[0]["outcome_kind"]
                payload["outcome_value"] = payloads[0]["outcome_value"]
            payload["delivery_snapshot_id"] = f"00000000-0000-4000-8000-00000000052{index}"
            payload["ops_tokens"] = ["pytest"]
            sources.append(source)
            raw_host_task_ids.append(str(payload["task_id"]))
            receipt = capture_delivery_and_ack(
                temp_root, payload, sentinel="AC5_MISSING_EXPORT"
            )
            capture_ids.append(str(receipt["capture_id"]))
            result = complete_payload(
                temp_root, payload, sentinel="AC5_MISSING_EXPORT"
            )
            assert result.returncode == 0, combined(result)

        output = temp_root / "training"
        result = run_store(
            temp_root,
            "export-training",
            str(output),
            "--snapshot-id",
            "ac5-source-export",
            "--reviewer-confirmed",
        )
        stop_at_missing_surface(result, "export-training", "AC5_MISSING_EXPORT")
        assert result.returncode == 0, combined(result)
        import pyarrow.parquet as parquet

        sft_table = parquet.read_table(output / "sft-000.parquet")
        rows = sft_table.to_pylist()
        project_groups = {
            project_key: [row for row in rows if row.get("project_key") == project_key]
            for project_key in {row.get("project_key") for row in rows}
        }
        assert len(project_groups) == 2, project_groups
        # Project labels are intentionally opaque, so identify the fixture's
        # lineage groups by their expected post-dedup cardinality rather than
        # by hash ordering.
        same_group = max(project_groups.values(), key=len)
        other_group = min(project_groups.values(), key=len)
        assert same_group and other_group
        # Dedup is lineage scoped.  The other project has identical event text
        # but must survive because its source lineage is independent.
        assert len(other_group) == 1, other_group
        assert (
            other_group[0]["prompt"],
            other_group[0]["assistant_response"],
            other_group[0]["outcome_kind"],
            other_group[0]["outcome_value"],
        ) == (
            payloads[0]["prompt"],
            payloads[0]["assistant_response"],
            payloads[0]["outcome_kind"],
            payloads[0]["outcome_value"],
        )
        assert len({row["split_key"] for row in same_group}) == 1
        assert len({row["split_key"] for row in other_group}) == 1
        assert same_group[0]["split_key"] in {"train", "validation", "test"}
        assert other_group[0]["split_key"] in {"train", "validation", "test"}
        serialized = json.dumps(rows, ensure_ascii=False)
        assert "split-same" not in serialized
        assert "split-other" not in serialized

        same_task = [
            row for row in same_group
            if row.get("task_id") in set(capture_ids[:3])
        ]
        assert len(same_task) == 2, same_task
        assert all(
            row["task_id"] in set(capture_ids[:3])
            for row in same_task
        )
        duplicate_rows = [
            row for row in same_task
            if len(row.get("source_event_ids", [])) >= 2
        ]
        assert len(duplicate_rows) == 1, duplicate_rows

        document = json.loads((output / "deletion-map.json").read_text(encoding="utf-8"))
        deletions = (
            document
            if isinstance(document, list)
            else document.get("deletions", document.get("rows"))
        )
        assert isinstance(deletions, list) and deletions
        assert all(set(item) == {"keeper_id", "deleted_id", "reason"} for item in deletions)
        assert [item["deleted_id"] for item in deletions] == sorted(item["deleted_id"] for item in deletions)
        assert all(item["keeper_id"] != item["deleted_id"] for item in deletions)
        for raw_task_id in raw_host_task_ids:
            assert raw_task_id.encode("utf-8") not in (temp_root / "store.sqlite").read_bytes()
            assert all(
                raw_task_id.encode("utf-8") not in path.read_bytes()
                for path in output.rglob("*")
                if path.is_file()
            )
    print("AC5_OK_lineage_split_exact_dedup_and_deletion_map")


if __name__ == "__main__":
    main()
