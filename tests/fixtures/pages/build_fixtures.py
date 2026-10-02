"""Build deterministic fixtures for curated pages (issue #138).

The generator is intentionally independent of ``storelib.pages``.  It writes
the source rows and the byte-level page inputs that the tests use as their
reviewed data contract; no production renderer is imported here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


NAMESPACE = "project:test"
TOPIC_TAG = "fixture-topic"
PAGE_ID = "fixture-page"
REFRESH_NOW = "2026-09-10T00:01:00Z"
PAGE_WATERMARK = "2026-09-10T00:00:04Z:fixture-snapshot"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _evidence(evidence_id: str, n: int, ts: str) -> dict:
    excerpt = f"evidence for source {n}"
    return {
        "id": evidence_id,
        "session_id": "s-page-fixture",
        "lane": "codex",
        "moment": "user_prompt",
        "kind": "correction",
        "ts": ts,
        "hash": f"hash-{n}",
        "excerpt": excerpt,
        "ref_path": "fixture",
        "ref_offset": n - 502,
    }


def _row(n: int, content: str, ts: str) -> dict:
    return {
        "evidence": _evidence(f"ev-{n}", n, ts),
        "id": f"00000000-0000-4000-8000-{n:012d}",
        "namespace": NAMESPACE,
        "type": "fact",
        "content": content,
        "tags": TOPIC_TAG,
        "signal": "user",
        "taint": "trusted_internal",
        "confidence": round(0.92 - ((n - 502) * 0.01), 2),
        "trust_score": round(0.95 - ((n - 502) * 0.01), 2),
        "ingestion_ts": ts,
    }


def source_rows() -> list[dict]:
    return [
        _row(502, "Fixture topic source alpha", "2026-09-10T00:00:02Z"),
        _row(503, "Fixture topic source beta", "2026-09-10T00:00:03Z"),
        _row(504, "Fixture topic source tombstone", "2026-09-10T00:00:04Z"),
    ]


PAGE_BASE = (
    "# Fixture Page\n\n"
    "<!-- section:stable -->\n"
    "Stable bytes: cafe\n"
    "<!-- end-section:stable -->\n\n"
    "<!-- section:refresh -->\n"
    "Old bullet\n"
    "<!-- end-section:refresh -->\n"
).encode("utf-8")


def _bullet_id(source_id: str, evidence_id: str) -> str:
    return hashlib.sha256(f"{source_id}\0{evidence_id}".encode()).hexdigest()[:16]


def patches() -> dict:
    return {
        "valid": {
            "operations": [
                {
                    "op": "replace_section",
                    "section_id": "refresh",
                    "markdown": "Patched café",
                    "citations": ["ev-502"],
                },
                {
                    "op": "append_bullet",
                    "section_id": "refresh",
                    "markdown": "Patched bullet",
                    "citations": ["ev-503"],
                },
                {
                    "op": "retract_bullet",
                    "section_id": "refresh",
                    "bullet_id": _bullet_id(
                        "00000000-0000-4000-8000-000000000503", "ev-503"),
                    "citations": ["ev-503"],
                },
            ]
        },
        "invalid": {
            "operations": [
                {
                    "op": "replace_section",
                    "section_id": "unknown",
                    "markdown": "must refuse",
                    "citations": ["ev-502"],
                }
            ]
        },
        "unresolved_citation": {
            "operations": [
                {
                    "op": "replace_section",
                    "section_id": "refresh",
                    "markdown": "must refuse",
                    "citations": ["ev-not-in-candidates"],
                }
            ]
        },
    }


def expected_refresh() -> dict:
    rows = source_rows()
    source_ids = ["belief:fixture-501"] + [row["id"] for row in rows]
    evidence_ids = ["ev-501"] + [row["evidence"]["id"] for row in rows]
    return {
        "evidence_ids": evidence_ids,
        "freshness_watermark": PAGE_WATERMARK,
        "page_checksum": hashlib.sha256(PAGE_BASE).hexdigest(),
        "retracted_source_ids": [rows[-1]["id"]],
        "source_ids": source_ids,
        "version_id": "v000001",
    }


def build(out_dir: Path) -> dict[str, bytes]:
    rows = source_rows()
    files = {
        "page-base.md": PAGE_BASE,
        "page-sources.jsonl": b"".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8") + b"\n"
            for row in rows
        ),
        "patches.json": _json_bytes(patches()),
        "expected-refresh.json": _json_bytes(expected_refresh()),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in sorted(files.items()):
        (out_dir / name).write_bytes(payload)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent),
                        help="output directory (default: fixture directory)")
    args = parser.parse_args()
    files = build(Path(args.out))
    for name in sorted(files):
        print(f"wrote {Path(args.out) / name} ({len(files[name])} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
