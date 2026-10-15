"""Deterministic state-candidate classifier (issue #174, Workstream K PR 3).

Read-only dry-run support for the #173 `state` type: scan live rows, match
combined content+tags against the three deterministic expressions, and
propose `state` plus the matching expiry class for each hit. This module
never writes: ``classify_state_candidates`` refuses a non-dry-run call
before it reads a single row, and the CLI opens the store through a
read-only URI with ``PRAGMA query_only=ON`` so no code path can mutate the
database. The real (writing) reclassification command belongs to #176.
"""
from __future__ import annotations

import re
import sqlite3

ENDPOINT = re.compile(
    r"(?i)\b(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::[0-9]{1,5})?\b")
METRIC = re.compile(
    r"(?i)\b(?:metric|latency|count|rate|percent|p95)\b")
SETTING = re.compile(
    r"(?i)\b(?:current|active|configured|default|enabled|disabled|timeout|version)\b")

# Precedence: endpoint, then metric, then setting (issue #174 Design step 7).
_RULES = (
    ("endpoint", ENDPOINT),
    ("metric", METRIC),
    ("setting", SETTING),
)


def classify_state_candidates(
    conn: sqlite3.Connection, *, dry_run: bool = True,
) -> dict:
    """Propose state + expiry-class for live rows matching the rule set.

    Requires ``dry_run is True`` (this command ships dry-run only; the
    writing form is #176's) and raises ``ValueError`` otherwise BEFORE
    reading any row. Scans live rows only (``superseded_at IS NULL``),
    matches combined content and tags in endpoint > metric > setting
    precedence, and returns ``proposals`` sorted by memory id with
    ``id``/``current_type``/``proposed_type``/``proposed_expiry_class``/
    ``matched_rule`` per row, plus ``count``/``scanned``/``changed``
    counters. A match proposes ``state`` and its class; no match keeps the
    row's current type with ``proposed_expiry_class=None`` and
    ``matched_rule="none"``. Writes nothing, commits nothing.

    ``changed`` counts rows whose content/tags MATCHED a rule — the
    issue-#174 contract's definition (its frozen golden pins
    ``changed=3`` over a fixture whose rows are all already ``state``),
    so it is deliberately NOT a count of type transitions; a row that is
    already ``state`` and matches still counts. The per-row
    ``current_type``/``proposed_type`` pair carries that distinction.
    """
    if not dry_run:
        raise ValueError("classify_state_candidates requires dry_run=True")
    rows = conn.execute(
        "SELECT id, type, content, tags FROM memory "
        "WHERE superseded_at IS NULL ORDER BY id").fetchall()
    proposals: list[dict] = []
    changed = 0
    for row in rows:
        current_type = row["type"]
        haystack = " ".join(
            part for part in (row["content"] or "", row["tags"] or "") if part)
        matched_rule = "none"
        proposed_class = None
        for name, pattern in _RULES:
            if pattern.search(haystack):
                matched_rule = name
                proposed_class = name
                break
        proposal = {
            "id": row["id"],
            "current_type": current_type,
            "proposed_type": "state" if proposed_class else current_type,
            "proposed_expiry_class": proposed_class,
            "matched_rule": matched_rule,
        }
        proposals.append(proposal)
        if matched_rule != "none":
            changed += 1
    proposals.sort(key=lambda p: p["id"])
    return {
        "proposals": proposals,
        "count": len(proposals),
        "scanned": len(rows),
        "changed": changed,
    }
