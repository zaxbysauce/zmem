"""Deterministic fixture generator for the store hygiene report (issue #97).

Run from the repo root:
    python tests/fixtures/store_hygiene/generate.py [--out-dir DIR]

Writes into the output dir (default: this script's own directory):
    rows.jsonl           every memory row in the snapshot, one JSON object per line
    origin-map.json      {memory_id: {"origin": "hermes"}} for exactly 833 ids
    evidence-map.json    the three triage cases (VALID / UNLINKED / EARLIER)
    expected-report.json build_report() output (snapshot_sha256 absent by construction)
    snapshot.sqlite      the store snapshot itself

Everything is fixed: ids are UUID-shaped literals, ingestion_ts comes from
two fixed instants, and outputs are compact sorted-key JSON with final
newlines, so re-runs are byte-identical (digests are pinned in
tests/test_store_hygiene.py::StoreHygieneTest).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Env pins BEFORE storelib import (repo convention: storelib freezes paths at
# first import; keep the fixture build off any real store).
import os as _os  # noqa: E402

_scratch = Path(_os.environ.get("ZMEM_HYGIENE_FIXTURE_TMP", REPO_ROOT / "tmp"))
_os.environ["ZMEM_STORE"] = str(_scratch / "fixture-unused-store.sqlite")
_os.environ.setdefault("ZMEM_DATA", str(_scratch))
_os.environ.setdefault("ZMEM_MODELS_DIR", str(_scratch / "missing-models"))
_os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

from storelib.hygiene import build_report  # noqa: E402
from storelib.schema import SIGNAL_CONFIDENCE, init_db  # noqa: E402

NONE_TS = "2026-09-10T00:00:00Z"       # every none-signal Hermes row
LATER_TS = "2026-09-10T00:01:00Z"      # VALID + UNLINKED grounded rows
EARLIER_TS = "2026-09-09T23:00:00Z"    # EARLIER grounded row (not later)

HERMES_NS = "user:global"
NS_SWARM = "project:github.com/zaxbyhub/opencode-swarm"
NS_ZMEM = "project:github.com/zaxbysauce/zmem"
JUNK = ("ns1", "ns2", "project:", "test", "user:t", "unfoldtest")


def hermes_id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def support_id(n: int) -> str:
    return f"aaaaaaaa-0000-4000-8000-{n:012d}"


def norm(content: str) -> str:
    return " ".join(content.lower().split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent),
                        help="output directory (default: this script's directory)")
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []

    def add(mid: str, namespace: str, content: str, signal: str,
            ts: str = NONE_TS, superseded_at: str | None = None,
            type_: str = "lesson", source_ref: str = "fixture:issue-97") -> None:
        rows.append({
            "id": mid,
            "namespace": namespace,
            "type": type_,
            "content": content,
            "tags": "",
            "source_ref": source_ref,
            "confidence": SIGNAL_CONFIDENCE.get(signal, 0.5),
            "signal": signal,
            "ingestion_ts": ts,
            "superseded_at": superseded_at,
            "content_norm": norm(content),
        })

    # 833 mapped Hermes-origin rows: live, signal=none, user:global.
    for n in range(1, 834):
        add(hermes_id(n), HERMES_NS, f"hermes imported lesson number {n}", "none")

    # Live rows in all six junk namespaces (counts are part of the report).
    for i, ns in enumerate(JUNK, start=1):
        add(support_id(i), ns, f"junk namespace probe row {i} in {ns}", "none")

    # Duplicate logical-key groups (shared content_norm) in both namespaces.
    add(support_id(20), NS_SWARM, "swarm duplicate lesson alpha", "test", LATER_TS)
    add(support_id(21), NS_SWARM, "Swarm   DUPLICATE lesson ALPHA", "test", LATER_TS)
    add(support_id(22), NS_SWARM, "swarm duplicate lesson beta", "none")
    add(support_id(23), NS_SWARM, "swarm duplicate lesson beta", "none")
    add(support_id(24), NS_SWARM, "swarm duplicate lesson beta", "none")
    add(support_id(30), NS_ZMEM, "zmem duplicate lesson alpha", "test", LATER_TS)
    add(support_id(31), NS_ZMEM, "zmem DUPLICATE lesson alpha", "test", LATER_TS)
    add(support_id(32), NS_ZMEM, "zmem duplicate lesson beta", "none")
    add(support_id(33), NS_ZMEM, "zmem duplicate lesson beta", "none")
    # Controls: unique live rows in both project namespaces.
    add(support_id(34), NS_SWARM, "swarm unique lesson", "reviewer", LATER_TS)
    add(support_id(35), NS_ZMEM, "zmem unique lesson", "lint", LATER_TS)

    # Evidence-case grounded rows (none targets are hermes ids 1..3).
    add(support_id(40), HERMES_NS, "grounded test lesson for valid case", "test", LATER_TS)
    add(support_id(41), HERMES_NS, "grounded test lesson for unlinked case", "test", LATER_TS)
    add(support_id(42), HERMES_NS, "grounded compile lesson for earlier case", "compile", EARLIER_TS)

    # Tombstoned supporting rows (counted in totals, excluded from live).
    add(support_id(50), HERMES_NS, "retired lesson one", "none", superseded_at=NONE_TS)
    add(support_id(51), NS_ZMEM, "retired lesson two", "test", superseded_at=NONE_TS)

    origin_map = {hermes_id(n): {"origin": "hermes"} for n in range(1, 834)}
    evidence_map = [
        {
            "none_id": hermes_id(1),
            "grounded_id": support_id(40),
            "proof_ref": "session:fixture-valid-case",
            "justification": "grounded test lesson later confirms this imported row",
        },
        {
            "none_id": hermes_id(2),
            "grounded_id": support_id(41),
            "proof_ref": "session:fixture-unlinked-case",
            "justification": "grounded but NOT linked to the none row",
        },
        {
            "none_id": hermes_id(3),
            "grounded_id": support_id(42),
            "proof_ref": "session:fixture-earlier-case",
            "justification": "linked and grounded but NOT later by ingestion_ts",
        },
    ]

    snapshot_path = out / "snapshot.sqlite"
    if snapshot_path.exists():
        snapshot_path.unlink()
    conn = sqlite3.connect(str(snapshot_path))
    try:
        init_db(conn)
        for r in rows:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
                " confidence, signal, ingestion_ts, superseded_at, content_norm)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["namespace"], r["type"], r["content"], r["tags"],
                 r["source_ref"], r["confidence"], r["signal"], r["ingestion_ts"],
                 r["superseded_at"], r["content_norm"]),
            )
        # VALID case link only: grounded --updates--> none target.
        conn.execute(
            "INSERT INTO memory_link (src_id, dst_id, relation, score, created_at)"
            " VALUES (?, ?, 'updates', 1.0, ?)",
            (support_id(40), hermes_id(1), LATER_TS),
        )
        # EARLIER case link (present; timing is the only disqualifier).
        conn.execute(
            "INSERT INTO memory_link (src_id, dst_id, relation, score, created_at)"
            " VALUES (?, ?, 'extends', 1.0, ?)",
            (support_id(42), hermes_id(3), EARLIER_TS),
        )
        conn.commit()

        expected = build_report(conn, origin_map=origin_map, evidence_map=evidence_map)
    finally:
        conn.close()

    def dump(path: Path, obj) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False) + "\n")

    dump(out / "origin-map.json", origin_map)
    dump(out / "evidence-map.json", evidence_map)
    dump(out / "expected-report.json", expected)
    with (out / "rows.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False) + "\n")

    print(f"fixture written to {out}: {len(rows)} memory rows,"
          f" {len(origin_map)} mapped ids, {len(evidence_map)} evidence rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
