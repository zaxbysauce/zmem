"""Deterministic belief-head fixture generator (issue #137 acceptance suite).

Every fixture byte is a pure function of the constants in this file: fixed
UUIDs, fixed timestamps, fixed sentences. No clock, no randomness, no
environment. Running this generator twice produces byte-identical files
(the acceptance check re-runs it and asserts zero churn).

Fixture shape (one JSON object per line, UTF-8, LF newlines):

    {
      "id": "00000000-0000-4000-8000-0000000004NN",   # memory row id
      "namespace": "project:test",
      "type": "fact",
      "content": "...",               # every row mentions "fixture topic"
      "tags": "fixture-topic",        # the shared topic tag
      "source_ref": "",
      "signal": "user", "taint": "trusted_internal",
      "confidence": 0.8, "trust_score": 0.9,
      "ingestion_ts": "2026-09-10T00:00:0NZ",
      "evidence": { ... full `evidence`-table row ... },   # optional
      "links": [ { "dst": ..., "relation": ..., "created_at": ... } ]  # optional
    }

The `evidence` payload carries every NOT NULL column of the v14 `evidence`
side table (id/session_id/lane/moment/kind/ts/hash/excerpt/ref_path) plus
ref_offset, with `hash` computed exactly like storelib.evidence.write_evidence
(sha256 over "kind|ts|excerpt") so the rows are valid inserts. The ids are
deliberately "ev-40N"-shaped (NOT UUIDs): memory_evidence.evidence_id only
REFERENCES evidence(id), and the belief contract carries evidence ids
verbatim — the fixture pins that shape.

Run: python tests/fixtures/beliefs/build_fixtures.py --out tests/fixtures/beliefs
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

NAMESPACE = "project:test"
TOPIC_TAG = "fixture-topic"
REFRESH_NOW = "2026-09-10T00:00:00Z"
EVIDENCE_SESSION = "s-belief-fixture-137"
EVIDENCE_LANE = "zcode"
EVIDENCE_MOMENT = "user_prompt"
EVIDENCE_KIND = "correction"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _evidence(evidence_id: str, ts: str, excerpt: str, ref_path: str) -> dict:
    digest = hashlib.sha256(
        f"{EVIDENCE_KIND}|{ts}|{excerpt}".encode("utf-8")
    ).hexdigest()
    return {
        "id": evidence_id,
        "session_id": EVIDENCE_SESSION,
        "lane": EVIDENCE_LANE,
        "moment": EVIDENCE_MOMENT,
        "kind": EVIDENCE_KIND,
        "ts": ts,
        "hash": digest,
        "excerpt": excerpt,
        "ref_path": ref_path,
        "ref_offset": 0,
    }


def _row(n: int, content: str, *, ts: str, signal: str = "user",
         taint: str = "trusted_internal", confidence: float = 0.8,
         trust_score: float = 0.9, evidence_id: str | None = None,
         links: list[dict] | None = None, ref_path: str = "") -> dict:
    row = {
        "id": _uuid(n),
        "namespace": NAMESPACE,
        "type": "fact",
        "content": content,
        "tags": TOPIC_TAG,
        "source_ref": "",
        "signal": signal,
        "taint": taint,
        "confidence": confidence,
        "trust_score": trust_score,
        "ingestion_ts": ts,
    }
    if evidence_id is not None:
        row["evidence"] = _evidence(
            evidence_id, ts,
            f"evidence excerpt for memory {_uuid(n)}", ref_path,
        )
    if links:
        row["links"] = links
    return row


def grounded_three_row() -> list[dict]:
    ref = "tests/fixtures/beliefs/grounded-three-row.jsonl"
    return [
        _row(401, "The fixture topic cache warms on the first query of each day.",
             ts="2026-09-10T00:00:01Z", evidence_id="ev-401", ref_path=ref),
        _row(402, "The fixture topic index rebuilds whenever the store migrates forward.",
             ts="2026-09-10T00:00:02Z", evidence_id="ev-402", ref_path=ref),
        _row(403, "The fixture topic head quotes the newest grounded member row.",
             ts="2026-09-10T00:00:03Z", evidence_id="ev-403", ref_path=ref),
    ]


def correction_and_contradiction() -> list[dict]:
    return [
        _row(404, "Legacy entry: the fixture topic cache was warmed by hand before each release.",
             ts="2026-09-10T00:00:01Z",
             links=[{"dst": _uuid(405), "relation": "updates",
                     "created_at": "2026-09-10T00:00:04Z", "score": 0.0}]),
        _row(405, "Correction: the fixture topic cache warms itself on the first query of each day.",
             ts="2026-09-10T00:00:02Z"),
        _row(406, "Contradiction: the fixture topic cache never warms itself on any query.",
             ts="2026-09-10T00:00:03Z",
             links=[{"dst": _uuid(405), "relation": "contradicts",
                     "created_at": "2026-09-10T00:00:05Z", "score": 0.0}]),
    ]


def taint_floor() -> list[dict]:
    return [
        _row(407, "The fixture topic watchdog reports a grounded taint floor sample.",
             ts="2026-09-10T00:00:06Z", signal="test",
             taint="trusted_internal", confidence=0.9, trust_score=0.9),
        _row(408, "The fixture topic scanner reports an ungrounded taint floor sample.",
             ts="2026-09-10T00:00:06Z", signal="none",
             taint="untrusted_tool", confidence=0.5, trust_score=0.1),
    ]


BAD_ACTIONS_BYTES = (
    b'{"actions":[{"op":"replace_quote","head_id":"belief:missing",'
    b'"source_ids":["missing"],"evidence_ids":["ev-missing"],'
    b'"markdown":"bad"},{"op":"add_source","head_id":"belief:fixture",'
    b'"source_ids":["outside"],"evidence_ids":["ev-outside"],'
    b'"markdown":"bad"}]}\n'
)


def expected_files() -> dict[str, bytes]:
    head_rows = grounded_three_row()
    contested_rows = correction_and_contradiction()
    expected_head = {
        "head_state": "active",
        "support_count": 3,
        "source_ids": sorted(r["id"] for r in head_rows),
        "evidence_ids": ["ev-401", "ev-402", "ev-403"],
        "content": head_rows[2]["content"],
        "refresh_watermark": REFRESH_NOW,
        "retracted_source_ids": [],
    }
    expected_contested = {
        "head_state": "contested",
        "content": contested_rows[1]["content"],
        "suppression_count": 0,
        "source_ids": sorted(r["id"] for r in contested_rows),
        "evidence_ids": [],
    }
    expected_bad_actions = {
        "exit_code": 1,
        "stderr_line": "[zmem] belief-heads: invalid action",
        "prior_head_checksum": hashlib.sha256(
            head_rows[2]["content"].encode("utf-8")
        ).hexdigest(),
        "prior_watermark": REFRESH_NOW,
    }
    expected_taint_floor = {
        "head_trust": 0.1,
        "recall_count": 0,
        "reason": "below-bar",
    }
    return {
        "expected-head.json": _json_bytes(expected_head),
        "expected-contested.json": _json_bytes(expected_contested),
        "expected-bad-actions.json": _json_bytes(expected_bad_actions),
        "expected-taint-floor.json": _json_bytes(expected_taint_floor),
    }


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False,
                       sort_keys=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: list[dict]) -> bytes:
    return "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n"
        for r in rows
    ).encode("utf-8")


def build(out_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "grounded-three-row.jsonl": _jsonl_bytes(grounded_three_row()),
        "correction-and-contradiction.jsonl": _jsonl_bytes(
            correction_and_contradiction()),
        "bad-actions.json": BAD_ACTIONS_BYTES,
        "taint-floor.jsonl": _jsonl_bytes(taint_floor()),
    }
    files.update(expected_files())
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in sorted(files.items()):
        (out_dir / name).write_bytes(payload)
    return files


def main() -> int:
    default_out = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(default_out),
                        help="output directory (default: the fixture dir)")
    args = parser.parse_args()
    files = build(Path(args.out))
    for name in sorted(files):
        print(f"wrote {Path(args.out) / name} ({len(files[name])} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
