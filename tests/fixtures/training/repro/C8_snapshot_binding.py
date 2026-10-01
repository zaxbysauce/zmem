from __future__ import annotations

import json
import tempfile
from pathlib import Path

from check_utils import (
    bind_payload,
    capture_delivery_and_ack,
    capture_delivery_only,
    combined,
    complete_payload,
    load_fixture,
    run_store,
    seed_memory_evidence_episode,
    surface_exists_or_exit,
    write_payload,
)


def main() -> None:
    fixture = load_fixture("complete_capture.json")
    assert isinstance(fixture, dict)
    with tempfile.TemporaryDirectory(prefix="zmem-135-c8-") as raw:
        temp_root = Path(raw)
        probe = run_store(temp_root, "capture-training-completion", "-h")
        surface_exists_or_exit(
            probe,
            "capture-training-completion",
            "AC8_MISSING_CAPTURE_COMPLETION",
            strict=True,
        )
        if probe.returncode == 0:
            source = seed_memory_evidence_episode(
                temp_root,
                session_id="session-135-binding",
                suffix="801",
                namespace="project:binding",
                content="stable binding source",
            )
            payload = bind_payload(dict(fixture), source)
            payload.update(
                {
                    "delivery_snapshot_id": "00000000-0000-4000-8000-000000000813",
                    "task_id": "task-135-binding",
                    "source_event_id": "event-135-binding",
                    "project_key": "binding",
                }
            )

            # Emitted is not acknowledged: completion must fail and may leave
            # only the non-exportable delivery snapshot.
            capture_delivery_only(
                temp_root, payload, sentinel="AC8_MISSING_CAPTURE_COMPLETION"
            )
            result = complete_payload(
                temp_root, payload, sentinel="AC8_MISSING_CAPTURE_COMPLETION"
            )
            assert result.returncode != 0, combined(result)

            capture_delivery_and_ack(
                temp_root, payload, sentinel="AC8_MISSING_CAPTURE_COMPLETION"
            )
            result = complete_payload(
                temp_root, payload, sentinel="AC8_MISSING_CAPTURE_COMPLETION"
            )
            assert result.returncode == 0, combined(result)
            completion_path = temp_root / "completion.json"
            replay = run_store(
                temp_root,
                "capture-training-completion",
                "--input",
                str(completion_path),
            )
            assert replay.returncode == 0, combined(replay)

            conflict = dict(payload)
            conflict["assistant_response"] = "conflicting replay response"
            conflict_path = write_payload(temp_root, "conflict.json", {
                key: value
                for key, value in conflict.items()
                if key not in {"context_fence", "ops_tokens", "captured_at"}
            })
            result = run_store(
                temp_root,
                "capture-training-completion",
                "--input",
                str(conflict_path),
            )
            assert result.returncode != 0, "conflicting replay was accepted"

            orphan = dict(payload)
            orphan["delivery_snapshot_id"] = "00000000-0000-4000-8000-000000000899"
            orphan_path = write_payload(temp_root, "orphan.json", {
                key: value
                for key, value in orphan.items()
                if key not in {"context_fence", "ops_tokens", "captured_at"}
            })
            result = run_store(
                temp_root,
                "capture-training-completion",
                "--input",
                str(orphan_path),
            )
            assert result.returncode != 0, "completion bypassed snapshot binding"
    print("AC8_OK_emitted_ack_completion_binding_and_replay")


if __name__ == "__main__":
    main()
