from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from check_utils import (
    assert_redacted_storage,
    bind_payload,
    capture_delivery_and_ack,
    combined,
    complete_payload,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    stop_at_missing_surface,
)


SFT_COLUMNS = [
    "task_id",
    "project_key",
    "session_id",
    "episode_id",
    "prompt",
    "context_fence",
    "ops_tokens",
    "assistant_response",
    "outcome_kind",
    "outcome_value",
    "evidence_ref",
    "source_memory_ids",
    "source_event_ids",
    "consent_scope",
    "content_license",
    "redaction_status",
    "redaction_policy_version",
    "split_key",
    "transform_version",
    "label_status",
    "exclusion_reason",
    "row_checksum",
]


def main() -> None:
    payload_obj = load_fixture("complete_capture.json")
    assert isinstance(payload_obj, dict)
    with tempfile.TemporaryDirectory(prefix="zmem-135-c2-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "capture-training-delivery", "-h")
        stop_at_missing_surface(
            probe, "capture-training-delivery", "AC2_MISSING_CAPTURE_API"
        )

        source = seed_memory_evidence_episode(
            temp_root,
            session_id=str(payload_obj["session_id"]),
            suffix="301",
            namespace="project:training",
            content="verified source memory for complete SFT row",
        )
        raw_bearer = "Bearer sk-training-135-redaction-token"
        raw_entropy = "f" * 48
        payload = bind_payload(dict(payload_obj), source)
        oversized_prompt = ("training prompt " * 2_000)[:16_050]
        payload.update(
            {
                "context_fence": (
                    f"<zmem>\nsecret={raw_bearer}\n{raw_entropy}\n"
                    f"- {source['memory_id']}\n</zmem>\n"
                ),
                "prompt": oversized_prompt,
                "assistant_response": f"The token {raw_bearer} is not retained.",
                "ops_tokens": ["pytest", raw_bearer],
            }
        )
        receipt = capture_delivery_and_ack(
            temp_root,
            payload,
            sentinel="AC2_MISSING_CAPTURE_API",
        )
        capture_id = str(receipt["capture_id"])
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            capture_id,
        )
        assert_redacted_storage(temp_root, [raw_bearer, raw_entropy])
        result = complete_payload(
            temp_root,
            payload,
            sentinel="AC2_MISSING_CAPTURE_API",
        )
        assert result.returncode == 0, combined(result)
        assert_redacted_storage(temp_root, [raw_bearer, raw_entropy])

        output = Path(raw) / "training"
        result = run_store(
            temp_root,
            "export-training",
            str(output),
            "--snapshot-id",
            "ac2-source-export",
            "--reviewer-confirmed",
        )
        stop_at_missing_surface(
            result, "export-training", "AC2_MISSING_CAPTURE_API"
        )
        assert result.returncode == 0, combined(result)
        sft = output / "sft-000.parquet"
        assert sft.is_file(), f"missing {sft}"
        import pyarrow.parquet as parquet

        table = parquet.read_table(sft)
        assert table.schema.names == SFT_COLUMNS
        rows = table.to_pylist()
        row = next(
            (
                candidate
                for candidate in rows
                if candidate.get("task_id") == capture_id
            ),
            None,
        )
        assert row is not None, "complete task was not exported"
        assert row["task_id"] == capture_id
        assert len(row["prompt"].encode("utf-8")) == 16_000
        assert row["prompt"] == oversized_prompt[:16_000]
        assert row["source_memory_ids"] == [source["memory_id"]]
        assert payload["source_event_id"] in row["source_event_ids"]
        assert len(row["prompt"].encode("utf-8")) <= 16000
        assert len(row["assistant_response"].encode("utf-8")) <= 16000
        assert len(row["context_fence"].encode("utf-8")) <= 16000
        assert all(
            isinstance(token, str) and len(token.encode("utf-8")) <= 256
            for token in row["ops_tokens"]
        )
        serialized = json.dumps(row, ensure_ascii=False)
        raw_host_task_id = str(payload["task_id"])
        assert raw_host_task_id not in serialized
        assert raw_host_task_id.encode("utf-8") not in (temp_root / "store.sqlite").read_bytes()
        assert all(
            raw_host_task_id.encode("utf-8") not in path.read_bytes()
            for path in output.rglob("*")
            if path.is_file()
        )
        assert raw_bearer not in serialized
        assert raw_entropy not in serialized
        assert "[REDACTED_SECRET]" in serialized
        assert row["redaction_status"] == "redacted"
        assert re.fullmatch(r"[0-9a-f]{64}", row["row_checksum"]) is not None
    print("AC2_OK_complete_sft_row_contract_and_redaction")


if __name__ == "__main__":
    main()
