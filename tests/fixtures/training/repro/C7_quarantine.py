from __future__ import annotations

import json
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


def main() -> None:
    fixture = load_fixture("complete_capture.json")
    assert isinstance(fixture, dict)
    with tempfile.TemporaryDirectory(prefix="zmem-135-c7-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "export-training", "-h")
        stop_at_missing_surface(
            probe, "export-training", "AC7_MISSING_QUARANTINE_EXPORT"
        )
        source = seed_memory_evidence_episode(
            temp_root,
            session_id="session-135-quarantine",
            suffix="701",
            namespace="project:quarantine",
            content="quarantine source",
        )
        secret = "Bearer sk-c7-quarantine-secret"
        payload = bind_payload(dict(fixture), source)
        payload.update(
            {
                "delivery_snapshot_id": "00000000-0000-4000-8000-000000000713",
                "task_id": "task-135-quarantine",
                "source_event_id": "event-135-quarantine",
                "project_key": "quarantine",
                "prompt": f"Use {secret} only for this verified action.",
                "assistant_response": f"Verified; secret={secret}",
                "context_fence": f"<zmem>\n- {source['memory_id']}\nsecret={secret}\n</zmem>\n",
                "ops_tokens": ["pytest", secret],
            }
        )
        capture_delivery_and_ack(
            temp_root, payload, sentinel="AC7_MISSING_QUARANTINE_EXPORT"
        )
        result = complete_payload(
            temp_root, payload, sentinel="AC7_MISSING_QUARANTINE_EXPORT"
        )
        assert result.returncode == 0, combined(result)
        assert_redacted_storage(temp_root, [secret])

        default_output = temp_root / "training-default"
        result = run_store(
            temp_root,
            "export-training",
            str(default_output),
            "--snapshot-id",
            "ac7-default",
            "--reviewer-confirmed",
        )
        assert result.returncode == 0, combined(result)
        assert not (default_output / "quarantine").exists()

        quarantine_output = temp_root / "training-quarantine"
        result = run_store(
            temp_root,
            "export-training",
            str(quarantine_output),
            "--snapshot-id",
            "ac7-quarantine",
            "--reviewer-confirmed",
            "--quarantine-raw",
        )
        assert result.returncode == 0, combined(result)
        quarantine = quarantine_output / "quarantine"
        assert quarantine.is_dir()
        manifest_path = quarantine_output / "quarantine-manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        event_ids = manifest["event_ids"]
        assert event_ids == sorted(event_ids)
        assert event_ids and len(event_ids) <= 50
        event_paths = [
            path
            for path in quarantine.rglob("*")
            if path.is_file()
        ]
        assert event_paths
        assert len(event_paths) == len(event_ids), (len(event_paths), len(event_ids))
        for path in event_paths:
            assert len(path.read_bytes()) <= 400
            text = path.read_text(encoding="utf-8")
            assert secret not in text
            assert "[REDACTED_SECRET]" in text
    print("AC7_OK_quarantine_opt_in_redacted_bounded")


if __name__ == "__main__":
    main()
