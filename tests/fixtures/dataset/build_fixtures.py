"""Deterministic generator for tests/fixtures/dataset (issue #134).

Byte-contract inputs: every file here is compared byte-for-byte by
tests/test_dataset.py, so this generator is an INDEPENDENT ORACLE — it
inlines the canonical serialization (sorted-key compact JSON + LF, SHA-256
row checksums) instead of importing storelib, exactly like the
injection-parity generator. Run from the repo root:

    python tests/fixtures/dataset/build_fixtures.py --out tests/fixtures/dataset

Every write is UTF-8 with LF endings and a final LF byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

FIXTURE_TS = "2026-09-10T00:00:00Z"

SECRET_ROW = {
    "id": "00000000-0000-4000-8000-000000000101",
    "namespace": "project:test",
    "content": "token ghp_fixture_000000000000000000000000000000000000",
    "ingestion_ts": FIXTURE_TS,
}
# The egress fixture is written in the issue's pinned insertion order.
SECRET_ROW_RAW = (
    '{"id":"00000000-0000-4000-8000-000000000101",'
    '"namespace":"project:test",'
    '"content":"token ghp_fixture_000000000000000000000000000000000000",'
    '"ingestion_ts":"2026-09-10T00:00:00Z"}\n'
)

EXPECTED_HELD_BACK_RAW = (
    '{"held_back":[{"id":"00000000-0000-4000-8000-000000000101",'
    '"reason":"secret_scan",'
    '"row_checksum":"9471fb35c39b082bbb9ac6ee30406d731b4acefabd8b4a6ce9'
    'a8fb3ed8e8bfa2"}],"uploaded_rows":0}\n'
)

HUB_CLIENT_STUB_RAW = (
    '{"parents":["old","conflict","new"],"publish_private":true}\n'
)

NAMESPACES = ["project:a", "project:b"]
SCENARIO_IDS = [
    "00000000-0000-4000-8000-000000000201",
    "00000000-0000-4000-8000-000000000202",
    "00000000-0000-4000-8000-000000000203",
    "00000000-0000-4000-8000-000000000204",
    "00000000-0000-4000-8000-000000000205",
]
EPISODE_ID = "00000000-0000-4000-8000-000000000301"


def canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"


def checksum(row: dict) -> str:
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def snapshot_hash(lines: list) -> str:
    h = hashlib.sha256()
    for line in lines:
        h.update((line + "\n").encode("utf-8"))
    return h.hexdigest()


def build_scenario() -> tuple[dict, dict]:
    """A two-namespace dataset (project:a, project:b) with five memory
    rows, one episode, one membership, and one link — the committed import
    fixture (format jsonl, deterministic UTF-8 LF text)."""
    memories = []
    for i, mid in enumerate(SCENARIO_IDS):
        ns = NAMESPACES[0] if i < 3 else NAMESPACES[1]
        memories.append({
            "id": mid,
            "namespace": ns,
            "type": "fact",
            "content": f"scenario row {i} for {ns}",
            "tags": "",
            "source_ref": "",
            "source_hash": "",
            "signal": "none",
            "confidence": 0.5,
            "taint": "trusted_internal",
            "trust_score": 1.0,
            "valid_from": "",
            "valid_until": "",
            "superseded_at": None,
            "supersede_reason": "",
            "update_of": "",
            "merged_from": None,
            "applied_count": 0,
            "violated_count": 0,
            "retrieval_count": 0,
            "surfaced_count": 0,
            "ingestion_ts": FIXTURE_TS,
            "last_retrieved": None,
            "last_surfaced": None,
            "capture_mode": None,
            "redaction_status": None,
            "redaction_policy_version": None,
            "consent_scope": None,
            "content_license": None,
            "deletion_key": None,
            "split_key": None,
        })
    pass1 = [f"memories\0{checksum(m)}" for m in memories]
    episodes = [{
        "id": EPISODE_ID,
        "namespace": NAMESPACES[0],
        "started_at": FIXTURE_TS,
        "ended_at": FIXTURE_TS,
        "summary_memory_id": SCENARIO_IDS[0],
        "token_count": 42,
    }]
    pass1.append(f"episodes\0{checksum(episodes[0])}")
    members = [{
        "episode_id": EPISODE_ID,
        "memory_id": SCENARIO_IDS[0],
        "added_at": FIXTURE_TS,
    }]
    pass1.append(f"episode_members\0{checksum(members[0])}")
    links = [{
        "src": SCENARIO_IDS[0],
        "dst": SCENARIO_IDS[1],
        "relation": "supports",
        "score": 0.75,
        "created_at": FIXTURE_TS,
    }]
    pass1.append(f"links\0{checksum(links[0])}")
    snapshot = snapshot_hash(pass1)

    for m in memories:
        m["export_snapshot_id"] = snapshot
        m["generator_revision"] = "zmem-dataset-v1"
        m["row_checksum"] = checksum(m)
    for family in (episodes, members, links):
        for row in family:
            row["row_checksum"] = checksum(row)

    manifest = {
        "schema_version": 1,
        "source_snapshot_hash": snapshot,
        "namespaces": NAMESPACES,
        "redaction_policy_version": "egress-scan-v1",
        "row_counts": {
            "memories": len(memories),
            "episodes": len(episodes),
            "episode_members": len(members),
            "links": len(links),
        },
        "include_tombstones": False,
        "generator_revision": "zmem-dataset-v1",
        "export_snapshot_id": snapshot,
        "format": "jsonl",
        "governance": {
            "policy": "SQLite authoritative; Parquet disposable; "
                      "publication is explicit-only",
            "egress_scan": "required-at-publish",
        },
    }
    return manifest, {
        "memories": memories,
        "episodes": episodes,
        "episode_members": members,
        "links": links,
    }


def write_bytes(path: Path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # The secret-row egress fixture: the issue's exact UTF-8 LF bytes.
    write_bytes(out / "secret-row.json", SECRET_ROW_RAW.encode("utf-8"))
    # The exact expected held-back report (verified against the canonical
    # checksum math below — abort rather than commit a wrong oracle).
    computed = checksum(SECRET_ROW)
    literal = "9471fb35c39b082bbb9ac6ee30406d731b4acefabd8b4a6ce9a8fb3ed8e8bfa2"
    if computed != literal:
        raise SystemExit(
            f"fixture math drift: canonical checksum {computed} != {literal}")
    write_bytes(out / "expected-held-back.json",
                EXPECTED_HELD_BACK_RAW.encode("utf-8"))
    write_bytes(out / "hub-client-stub.json",
                HUB_CLIENT_STUB_RAW.encode("utf-8"))

    manifest, families = build_scenario()
    write_bytes(out / "manifest-v1.json", canonical(manifest).encode("utf-8"))
    data_dir = out / "data"
    data_dir.mkdir(exist_ok=True)
    for family, rows in families.items():
        stem = {"memories": "memories-000", "episodes": "episodes-000",
                "episode_members": "episode_members-000",
                "links": "links-000"}[family]
        with open(data_dir / f"{stem}.jsonl", "wb") as fh:
            for row in rows:
                fh.write(canonical(row).encode("utf-8"))

    for p in sorted(out.glob("expected*.json")):
        print(p.name, hashlib.sha256(p.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
