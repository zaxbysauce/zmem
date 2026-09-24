from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TS = "2026-01-01T00:00:00Z"

TIER_ROWS = (
    ("project", "project:demo"),
    ("domain", "domain:demo"),
    ("fleet", "fleet:dgx"),
    ("host", "host:spark1"),
    ("cross", "project:other"),
    ("global", "user:global"),
)


def rows() -> list[dict]:
    result = []
    ordinal = 1
    for family, namespace in TIER_ROWS:
        for number in range(1, 7):
            key = f"{family}-{number:02d}"
            confidence = 0.90
            if key == "fleet-01":
                confidence = 0.99
            elif key == "project-06":
                confidence = 0.10
            result.append({
                "id": str(uuid.UUID(int=ordinal)),
                "key": key,
                "namespace": namespace,
                "type": "lesson",
                "content": f"reserved tier recall acceptance candidate {key}",
                "tags": "fixture,recall-tiers",
                "source_ref": f"fixture:{key}",
                "confidence": confidence,
                "signal": "test",
                "valid_from": TS,
                "ingestion_ts": TS,
            })
            ordinal += 1
    return result


def projection(keys: list[tuple[str, str]]) -> dict:
    counts = {name: 0 for name in
              ("project", "domain", "fleet_host", "cross_project", "user_global")}
    result = []
    for key, tier in keys:
        counts[tier] += 1
        result.append({"key": key, "tier": tier})
    return {"counts": counts, "rows": result}


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    fixture_rows = rows()
    without_cross = [
        *((f"project-{i:02d}", "project") for i in range(1, 6)),
        *((f"domain-{i:02d}", "domain") for i in range(1, 3)),
        *((f"fleet-{i:02d}", "fleet_host") for i in range(1, 3)),
        *((f"global-{i:02d}", "user_global") for i in range(1, 4)),
    ]
    with_cross = [
        *without_cross[:9],
        ("cross-01", "cross_project"),
        ("cross-02", "cross_project"),
        ("global-01", "user_global"),
        ("global-02", "user_global"),
        ("global-03", "user_global"),
    ]
    assert [key for key, _tier in without_cross] == [
        "project-01", "project-02", "project-03", "project-04", "project-05",
        "domain-01", "domain-02", "fleet-01", "fleet-02",
        "global-01", "global-02", "global-03",
    ]
    assert len(with_cross) == 14
    write_json(ROOT / "reserved_slots.json", fixture_rows)
    write_json(ROOT / "expected_without_cross.json", projection(without_cross))
    write_json(ROOT / "expected_with_cross.json", projection(with_cross))
    for name in ("expected_without_cross.json", "expected_with_cross.json"):
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        print(f"{name}: {digest}")


if __name__ == "__main__":
    main()
