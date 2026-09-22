"""Deterministic generator for tests/fixtures/feedback_expected.json
(issue #124).

Reads the session fixture, runs the REAL issue-#156 observational action
matcher (window_s=1800, min_overlap=2, never overridden) over the fixture's
delivered/evidence rows, checks every non-ignored match against the
fixture's association map the way storelib.feedback does, and writes the
expected compact sorted-key JSON bytes plus one final LF.

The generator refuses (exit 1, before replacing the output) a non-UUID
fixture id, a timestamp outside the fixture's fixed values, a matcher result
other than the two expected rows, or an association mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURE_TS = {
    "2026-09-10T10:00:00Z", "2026-09-10T10:01:00Z",
    "2026-09-10T10:02:00Z", "2026-09-10T10:03:00Z",
    "2026-09-10T11:01:00Z",
}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXPECTED_EVENTS = (
    "00000000-0000-4000-8000-000000000127",
    "00000000-0000-4000-8000-000000000128",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate tests/fixtures/feedback_expected.json")
    parser.add_argument("--input", required=True,
                        help="path to feedback_session.json")
    parser.add_argument("--output", required=True,
                        help="path to write feedback_expected.json")
    args = parser.parse_args(argv)

    source = Path(args.input)
    blob = source.read_bytes().decode("utf-8")
    if blob.count("\r") or not blob.endswith("\n"):
        print("make_feedback_expected: input must be UTF-8 LF with one "
              "final LF", file=sys.stderr)
        return 1
    fixture = json.loads(blob)

    session_id = fixture["session_id"]
    association_map = fixture["association_map"]
    for row in fixture["delivered_rows"]:
        for value in (row["id"], row["session_id"]):
            if not UUID_RE.match(value):
                print(f"make_feedback_expected: non-UUID fixture id {value!r}",
                      file=sys.stderr)
                return 1
        if row["timestamp"] not in FIXTURE_TS:
            print(f"make_feedback_expected: unexpected timestamp "
                  f"{row['timestamp']!r}", file=sys.stderr)
            return 1
    for row in fixture["evidence_rows"]:
        for value in (row["event_id"], row["evidence_id"], row["session_id"]):
            if not UUID_RE.match(value):
                print(f"make_feedback_expected: non-UUID fixture id {value!r}",
                      file=sys.stderr)
                return 1
        if row["timestamp"] not in FIXTURE_TS:
            print(f"make_feedback_expected: unexpected timestamp "
                  f"{row['timestamp']!r}", file=sys.stderr)
            return 1

    sys.path.insert(0, str(SCRIPTS))
    saved = sys.path[:]
    try:
        scripts_dir = str(REPO_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import importlib
        replay = importlib.import_module("eval_replay")
    finally:
        sys.path[:] = saved

    delivered = [
        {"id": row["id"], "session_id": row["session_id"],
         "timestamp": row["timestamp"], "operation": row["operation"]}
        for row in fixture["delivered_rows"]
    ]
    results = replay.match_observational_actions(
        delivered, fixture["evidence_rows"], window_s=1800, min_overlap=2)

    out_rows = []
    event_by_delivered = {}
    for row in fixture["evidence_rows"]:
        event_by_delivered[row["operation"]] = row
    for result in results:
        if result["action"] not in ("applied", "violated"):
            continue
        memory_id = result["delivered_id"]
        associated = association_map.get(memory_id) or []
        # The event that won: reconstruct it from the fixture's evidence rows
        # by session and kind, matching the matcher's earliest-in-window rule.
        winners = [row for row in fixture["evidence_rows"]
                   if row["session_id"] == result["session_id"]
                   and ((result["action"] == "applied"
                         and row["event_kind"] == "success")
                        or (result["action"] == "violated"
                            and row["event_kind"] == "failure"))]
        winner = min(
            winners,
            key=lambda row: __import__("datetime").datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")))
        evidence_id = winner["evidence_id"]
        if evidence_id not in associated:
            print(f"make_feedback_expected: association mismatch for "
                  f"{memory_id}: {evidence_id} not linked", file=sys.stderr)
            return 1
        out_rows.append({
            "event_id": winner["event_id"],
            "evidence_id": evidence_id,
            "memory_id": memory_id,
            "overlap": int(result["overlap_count"]),
            "session_id": result["session_id"],
            "verdict": result["action"],
        })
    out_rows.sort(key=lambda row: (row["memory_id"], row["event_id"]))
    if [row["event_id"] for row in out_rows] != list(EXPECTED_EVENTS):
        print("make_feedback_expected: matcher result is not the two "
              "expected rows", file=sys.stderr)
        return 1

    text = json.dumps(out_rows, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False) + "\n"
    out_path = Path(args.output)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    tmp.replace(out_path)
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"make_feedback_expected: wrote {out_path} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
