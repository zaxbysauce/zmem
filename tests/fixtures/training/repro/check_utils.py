from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[4]
STORE = ROOT / "skills" / "memory" / "scripts" / "store.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _store_env(temp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["ZMEM_STORE"] = str(temp_root / "store.sqlite")
    env["ZMEM_DATA"] = str(temp_root / "data")
    # The exporter must use its in-memory event-text semantic profile for
    # non-empty exports.  The repository's deterministic fake profile is the
    # supported local test capability; no persisted event-vector column is
    # assumed by these checks.
    env["ZMEM_EMBED_PROFILE"] = "fake"
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["ZMEM_MODELS_DIR"] = str(temp_root / "models")
    env["ZMEM_LINK_THRESHOLD"] = "1.01"
    return env


def run_store(
    temp_root: Path,
    *args: str,
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _store_env(temp_root)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(STORE), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_store_input(
    temp_root: Path, input_text: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STORE), *args],
        cwd=ROOT,
        env=_store_env(temp_root),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def combined(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def json_result(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(combined(result).splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError(f"missing JSON result\n{combined(result)}")


def add_memory(
    temp_root: Path,
    content: str,
    *,
    namespace: str = "project:training",
    confidence: str = "0.9",
) -> str:
    result = run_store(
        temp_root,
        "add",
        "--namespace",
        namespace,
        "--type",
        "lesson",
        "--content",
        content,
        "--signal",
        "test",
        "--confidence",
        confidence,
        "--json",
    )
    assert result.returncode == 0, combined(result)
    return str(json_result(result)["id"])


def write_evidence(
    temp_root: Path,
    *,
    evidence_id: str,
    session_id: str,
    excerpt: str = "verified training evidence",
    kind: str = "test_result",
) -> None:
    evidence = {
        "id": evidence_id,
        "session_id": session_id,
        "lane": "codex",
        "moment": "user_prompt",
        "kind": kind,
        "ts": "2026-09-26T12:00:00Z",
        "excerpt": excerpt,
        "ref_path": "fixture://issue-135",
        "ref_offset": 0,
    }
    result = run_store_input(temp_root, json.dumps(evidence), "evidence", "write")
    assert result.returncode == 0, combined(result)


def seed_memory_evidence_episode(
    temp_root: Path,
    *,
    session_id: str,
    suffix: str,
    namespace: str = "project:training",
    content: str = "training source memory",
    evidence_kind: str = "test_result",
) -> dict[str, str]:
    """Seed only through public APIs; completion owns the narrow association."""
    memory_id = add_memory(temp_root, content, namespace=namespace)
    evidence_id = str(
        uuid.UUID(f"00000000-0000-4000-8000-000000000{suffix:0>3}")
    )
    write_evidence(
        temp_root,
        evidence_id=evidence_id,
        session_id=session_id,
        excerpt=f"verified evidence {suffix}",
        kind=evidence_kind,
    )
    result = run_store(
        temp_root,
        "episode-open",
        "--namespace",
        namespace,
        "--json",
    )
    assert result.returncode == 0, combined(result)
    episode_id = str(json_result(result)["id"])
    result = run_store(
        temp_root,
        "episode-add",
        "--episode",
        episode_id,
        "--memory",
        memory_id,
        "--json",
    )
    assert result.returncode == 0, combined(result)
    return {
        "memory_id": memory_id,
        "evidence_id": evidence_id,
        "episode_id": episode_id,
        "session_id": session_id,
        "namespace": namespace,
    }


def bind_payload(
    payload: dict[str, Any], source: dict[str, str], *, include_context: bool = True
) -> dict[str, Any]:
    """Bind placeholders to public seed rows; source ids remain exporter-derived."""
    bound = dict(payload)
    bound["evidence_ref"] = source["evidence_id"]
    # Observations are evidence-bound.  A synthetic host event identifier is
    # not a source row and cannot become exporter lineage.
    bound["source_event_id"] = source["evidence_id"]
    bound["memory_ids"] = [source["memory_id"]]
    bound["session_id"] = source["session_id"]
    if include_context:
        bound["context_fence"] = f"<zmem>\n- {source['memory_id']}\n</zmem>\n"
    return bound


def write_payload(temp_root: Path, name: str, payload: dict[str, Any]) -> Path:
    path = temp_root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def capture_delivery_and_ack(
    temp_root: Path,
    payload: dict[str, Any],
    *,
    sentinel: str,
) -> dict[str, Any]:
    # Requested memory IDs belong to completion's narrow association writer;
    # they must never be accepted as exporter-owned source_memory_ids during
    # the emitted delivery stage.
    delivery_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"memory_ids", "source_memory_ids"}
    }
    delivery_path = write_payload(temp_root, "delivery.json", delivery_payload)
    result = run_store(
        temp_root, "capture-training-delivery", "--input", str(delivery_path)
    )
    stop_at_missing_surface(result, "capture-training-delivery", sentinel)
    assert result.returncode == 0, combined(result)
    receipt = json_result(result)
    assert receipt.get("delivery_snapshot_id") == payload["delivery_snapshot_id"]
    capture_id = receipt.get("capture_id")
    assert isinstance(capture_id, str) and capture_id
    acknowledgement = {
        "delivery_snapshot_id": payload["delivery_snapshot_id"],
        "session_id": payload["session_id"],
        "attestation": "test-host-display-attestation",
    }
    ack_path = write_payload(temp_root, "acknowledge.json", acknowledgement)
    result = run_store(
        temp_root,
        "capture-training-acknowledge",
        "--input",
        str(ack_path),
    )
    stop_at_missing_surface(result, "capture-training-acknowledge", sentinel)
    assert result.returncode == 0, combined(result)
    assert json_result(result).get("delivery_snapshot_id") == payload["delivery_snapshot_id"]
    return {"capture_id": capture_id, "delivery_snapshot_id": payload["delivery_snapshot_id"]}


def capture_delivery_only(
    temp_root: Path,
    payload: dict[str, Any],
    *,
    sentinel: str,
) -> None:
    delivery_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"memory_ids", "source_memory_ids"}
    }
    delivery_path = write_payload(temp_root, "delivery-unacknowledged.json", delivery_payload)
    result = run_store(
        temp_root, "capture-training-delivery", "--input", str(delivery_path)
    )
    stop_at_missing_surface(result, "capture-training-delivery", sentinel)
    assert result.returncode == 0, combined(result)
    assert json_result(result).get("delivery_snapshot_id") == payload["delivery_snapshot_id"]


def complete_payload(
    temp_root: Path,
    payload: dict[str, Any],
    *,
    sentinel: str,
    remove: set[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    excluded = {"context_fence", "ops_tokens", "captured_at"}
    excluded.update(remove or set())
    completion_obj = {
        key: value for key, value in payload.items() if key not in excluded
    }
    completion_path = write_payload(temp_root, "completion.json", completion_obj)
    result = run_store(
        temp_root,
        "capture-training-completion",
        "--input",
        str(completion_path),
    )
    surface_exists_or_exit(result, "capture-training-completion", sentinel)
    return result


def export_empty_contract(temp_root: Path, snapshot_id: str) -> Path:
    output = temp_root / "training-empty"
    result = run_store(
        temp_root,
        "export-training",
        str(output),
        "--snapshot-id",
        snapshot_id,
        "--reviewer-confirmed",
    )
    assert result.returncode == 0, combined(result)
    assert output.is_dir()
    import pyarrow.parquet as parquet

    for name in ("sft-000.parquet", "preferences-000.parquet"):
        table = parquet.read_table(output / name)
        assert table.num_rows == 0, (name, table.num_rows)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("row_counts") == {"preferences": 0, "sft": 0}
    assert (output / "deletion-map.json").is_file()
    return output


def assert_redacted_storage(temp_root: Path, raw_values: list[str]) -> None:
    database = temp_root / "store.sqlite"
    assert database.is_file()
    contents = database.read_bytes()
    for value in raw_values:
        assert value.encode("utf-8") not in contents, value


def stop_at_missing_surface(
    result: subprocess.CompletedProcess[str], command: str, sentinel: str
) -> None:
    surface_exists_or_exit(result, command, sentinel, strict=True)
    assert result.returncode == 0, f"{command}: command failed\n{combined(result)}"


def surface_exists_or_exit(
    result: subprocess.CompletedProcess[str],
    command: str,
    sentinel: str,
    *,
    strict: bool = False,
) -> None:
    text = combined(result)
    if result.returncode == 2 and "invalid choice" in text and command in text:
        print(sentinel)
        raise SystemExit(1)
    if strict and result.returncode != 0:
        raise AssertionError(
            f"{command} help probe failed unexpectedly (rc={result.returncode})\n{text}"
        )


def load_fixture(name: str) -> dict[str, Any] | list[dict[str, Any]]:
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)
