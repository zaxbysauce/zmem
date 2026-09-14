"""Read-only store hygiene report (issue #97, Workstream E PR 1 of 7).

``store.py hygiene`` opens an operator-supplied SNAPSHOT of the store
read-only and reports: totals, live counts, sorted namespaces/signals, the
reviewed Hermes-origin map, the six known junk namespaces, duplicate logical
keys (live rows sharing a ``content_norm``), and an evidence-gated
``signal=none`` upgrade action plan. It performs NO writes — the emitted
``update`` commands are review artifacts for the operator; Hermes-origin
MUTATIONS remain owned by issue #168's explicit operator map.

Invalid input contract: unreadable files, malformed JSON (including
duplicate JSON keys), duplicate mapped ids, ids unknown to the snapshot, and
SQLite errors print ``[zmem] hygiene: invalid input`` to stderr and exit 2
BEFORE the output file is created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.request import pathname2url

# The six known junk namespaces from the 2026-09-10 audit (issue #97 scope).
# Always reported as exactly this list, in sorted order.
JUNK_NAMESPACES = ("ns1", "ns2", "project:", "test", "unfoldtest", "user:t")

# Grounded signals a none-upgrade may inherit evidence from.
GROUNDED_SIGNALS = ("test", "compile", "lint", "reviewer")

# memory_link relations that count as live evidence for an upgrade; checked
# in either direction between the none row and the grounded row.
TRIAGE_RELATIONS = ("supports", "updates", "extends", "derives")

_UPGRADE_COMMAND = (
    "python skills/memory/scripts/store.py update"
    " --id {none_id} --content {content} --signal {signal}"
    " --source-ref {proof_ref} --json"
)


def _invalid() -> int:
    print("[zmem] hygiene: invalid input", file=sys.stderr)
    return 2


def _load_json_no_dup_keys(path: Path):
    """Parse a JSON file, rejecting duplicate object keys ANYWHERE (this is
    how 'duplicate mapped ids' is detectable given the dict-shaped
    origin_map — a naive json.loads would silently keep the last)."""
    def _pairs(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate key: {key}")
            seen.add(key)
        return dict(pairs)

    text = path.read_text(encoding="utf-8")  # OSError propagates to caller
    return json.loads(text, object_pairs_hook=_pairs)


def _validate_origin_map(origin_map) -> dict[str, dict]:
    if not isinstance(origin_map, dict) or not origin_map:
        raise ValueError("origin map must be a non-empty object")
    for mid, entry in origin_map.items():
        if not isinstance(mid, str) or not mid.strip():
            raise ValueError("origin map id must be a non-empty string")
        if not isinstance(entry, dict) or entry.get("origin") != "hermes":
            raise ValueError(f"origin map entry for {mid} must be origin=hermes")
    return origin_map


def _validate_evidence_map(evidence_map) -> list[dict]:
    if not isinstance(evidence_map, list) or not evidence_map:
        raise ValueError("evidence map must be a non-empty list")
    seen_none_ids = set()
    for row in evidence_map:
        if not isinstance(row, dict):
            raise ValueError("evidence rows must be objects")
        for field in ("none_id", "grounded_id", "proof_ref", "justification"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"evidence row field {field} must be a string")
        if row["none_id"] in seen_none_ids:
            raise ValueError(f"duplicate none_id: {row['none_id']}")
        seen_none_ids.add(row["none_id"])
    return evidence_map


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def build_report(conn: sqlite3.Connection, *, origin_map: dict, evidence_map: list) -> dict:
    """Build the hygiene report dict from a read-only snapshot connection.

    ``evidence_map`` is a LIST of {none_id, grounded_id, proof_ref,
    justification} rows (the issue text's `dict` annotation was a typo; the
    runtime contract was always list-shaped — PR #199 review F5). Multiple
    rows may cite the same grounded_id (one grounded lesson can corroborate
    several none rows); only duplicate none_ids are rejected at parse time.

    No-action cases for an evidence row (each silently omitted from the plan,
    never an error): none target superseded or no longer signal='none' (the
    rerun-omits rule); grounded row superseded; grounded signal outside the
    grounded set; grounded ingestion_ts not strictly later; no live
    memory_link row with an allowed relation between the two ids (either
    direction); empty proof_ref or justification after strip.
    """
    origin_map = _validate_origin_map(origin_map)
    rows = _validate_evidence_map(evidence_map)

    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    has_links = _table_exists(conn, "memory_link")
    live_rows = conn.execute(
        "SELECT id, namespace, signal, ingestion_ts, content, content_norm"
        " FROM memory WHERE superseded_at IS NULL"
    ).fetchall()
    live_by_id = {r["id"]: r for r in live_rows}
    live = len(live_rows)

    namespaces = sorted({r["namespace"] for r in live_rows})
    signals = sorted({r["signal"] for r in live_rows})

    hermes_ids = sorted(origin_map)
    known = conn.execute("SELECT id FROM memory").fetchall()
    known_ids = {r["id"] for r in known}
    unknown = [mid for mid in hermes_ids if mid not in known_ids]
    if unknown:
        raise ValueError(f"origin map ids not in snapshot: {unknown[:3]}")
    for row in rows:
        for field in ("none_id", "grounded_id"):
            if row[field] not in known_ids:
                raise ValueError(f"evidence {field} not in snapshot: {row[field]}")
    hermes_live = sum(1 for mid in hermes_ids if mid in live_by_id)

    junk_counts = {
        ns: sum(1 for r in live_rows if r["namespace"] == ns) for ns in JUNK_NAMESPACES
    }

    dup_groups = []
    norms: dict[str, list] = {}
    for r in live_rows:
        key = r["content_norm"]
        if key:
            norms.setdefault(key, []).append(r)
    for key in sorted(norms):
        members = norms[key]
        if len(members) > 1:
            dup_groups.append({
                "logical_key": key,
                "namespaces": sorted({m["namespace"] for m in members}),
                "ids": sorted(m["id"] for m in members),
            })

    actions = []
    for row in sorted(rows, key=lambda r: r["none_id"]):
        none_id = row["none_id"]
        grounded_id = row["grounded_id"]
        proof_ref = row["proof_ref"].strip()
        justification = row["justification"].strip()
        none_row = live_by_id.get(none_id)
        grounded = live_by_id.get(grounded_id)
        if none_row is None or none_row["signal"] != "none":
            continue
        if grounded is None or grounded["signal"] not in GROUNDED_SIGNALS:
            continue
        if not grounded["ingestion_ts"] > none_row["ingestion_ts"]:
            continue
        if not proof_ref or not justification:
            continue
        linked = None
        if has_links:
            linked = conn.execute(
                "SELECT 1 FROM memory_link WHERE relation IN (?,?,?,?)"
                " AND ((src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?)) LIMIT 1",
                (*TRIAGE_RELATIONS, none_id, grounded_id, grounded_id, none_id),
            ).fetchone()
        if linked is None:
            continue
        actions.append({
            "none_id": none_id,
            "namespace": none_row["namespace"],
            "signal": grounded["signal"],
            "action": _UPGRADE_COMMAND.format(
                none_id=none_id,
                content=grounded["content"],
                signal=grounded["signal"],
                proof_ref=proof_ref,
            ),
            "reason": f"{justification} (proof: {proof_ref})",
        })

    return {
        "totals": {
            "rows": total,
            "live": live,
            "tombstoned": total - live,
        },
        "namespaces": namespaces,
        "signals": signals,
        "hermes": {
            "mapped_ids": hermes_ids,
            "live": hermes_live,
            "tombstoned": len(hermes_ids) - hermes_live,
        },
        "junk_namespaces": {
            "namespaces": list(JUNK_NAMESPACES),
            "counts": junk_counts,
        },
        "duplicates": dup_groups,
        "none_upgrade_plan": actions,
    }


def _render_text(report: dict) -> str:
    lines = [
        f"snapshot_sha256: {report['snapshot_sha256']}",
        "totals: rows={rows} live={live} tombstoned={tombstoned}".format(**report["totals"]),
        "namespaces: " + ", ".join(report["namespaces"]),
        "signals: " + ", ".join(report["signals"]),
        f"hermes mapped: {len(report['hermes']['mapped_ids'])}"
        f" (live {report['hermes']['live']}, tombstoned {report['hermes']['tombstoned']})",
        "junk namespaces: " + ", ".join(
            f"{ns}({report['junk_namespaces']['counts'][ns]})"
            for ns in report["junk_namespaces"]["namespaces"]
        ),
        f"duplicate groups: {len(report['duplicates'])}",
        f"none-upgrade actions: {len(report['none_upgrade_plan'])}",
    ]
    for action in report["none_upgrade_plan"]:
        lines.append(f"  {action['none_id']} [{action['namespace']}] -> {action['signal']}")
        lines.append(f"    {action['action']}")
        lines.append(f"    reason: {action['reason']}")
    lines.append(
        "note: action lines are review artifacts (verbatim, unquoted);"
        " mutations belong to issue #168"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="store.py hygiene",
        description="Read-only store hygiene snapshot report (issue #97)",
    )
    parser.add_argument("--store", dest="store", type=str, required=True,
                        help="SQLite snapshot to inspect")
    parser.add_argument("--origin-map", dest="origin_map", type=str, required=True,
                        help="Reviewed Hermes origin map JSON path")
    parser.add_argument("--evidence-map", dest="evidence_map", type=str, required=True,
                        help="None-upgrade evidence map JSON path")
    parser.add_argument("--out", dest="out", type=str, required=True,
                        help="Canonical report output path")
    parser.add_argument("--format", dest="format", choices=("json", "text"),
                        default="json", help="Report format")
    args = parser.parse_args(argv)

    snapshot = Path(args.store)
    out_path = Path(args.out)

    try:
        origin_map = _load_json_no_dup_keys(Path(args.origin_map))
        evidence_map = _load_json_no_dup_keys(Path(args.evidence_map))
    except (OSError, ValueError, UnicodeDecodeError):
        return _invalid()

    # Validate map shapes before touching the snapshot so a bad map never
    # depends on db state to be rejected.
    try:
        _validate_origin_map(origin_map)
        _validate_evidence_map(evidence_map)
    except ValueError:
        return _invalid()

    digest = hashlib.sha256()
    try:
        with snapshot.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return _invalid()

    uri = "file:" + pathname2url(str(snapshot)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if not _table_exists(conn, "memory"):
                raise sqlite3.DatabaseError("missing memory table")
            report = build_report(conn, origin_map=origin_map, evidence_map=evidence_map)
        finally:
            conn.close()
    except sqlite3.Error:
        return _invalid()
    except ValueError:
        return _invalid()

    report["snapshot_sha256"] = digest.hexdigest()

    # Read-only safety (PR #199 review V3): the report must never destroy one
    # of its own inputs. Refuse an --out that resolves onto the snapshot or
    # either input map BEFORE anything is written.
    try:
        out_resolved = os.path.normcase(str(out_path.resolve()))
        for input_path in (snapshot, Path(args.origin_map), Path(args.evidence_map)):
            if out_resolved == os.path.normcase(str(input_path.resolve())):
                return _invalid()
    except OSError:
        return _invalid()

    if args.format == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        rendered = _render_text(report)
    try:
        out_path.write_text(rendered, encoding="utf-8")
    except OSError:
        # Unwritable --out (missing parent, permission, is-a-directory) is an
        # invalid invocation, not a traceback (PR #199 review V4).
        return _invalid()
    return 0
