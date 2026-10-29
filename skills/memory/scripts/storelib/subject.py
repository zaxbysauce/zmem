"""Task-subject identity compatibility for dedup admission (issue #376).

 PURE module: no database access, no storelib sibling imports, no I/O.
 The one compatibility predicate defined here is the single source of truth
 for whether two rows' explicitly identified task subjects make a
 similarity-based destructive merge (dedup fold / consolidation absorb /
 tag propagation) inadmissible.

 Identity convention (frozen acceptance contract,
 tests/test_subject_dedup_contract.py):
   * ``task:<key>`` tokens inside the tags string are the row's
     task-subject identities. Tokens are split on the store's canonical
     tag-token grammar (``[,;\\s]+`` — the same separators entity.py's
     tag splitter uses), the ``task:`` prefix is matched CASE-INSENSITIVELY,
     and the key is CASE-FOLDED: ``Task:Repo#1`` and ``task:repo#1`` are the
     same subject. Repository-qualified keys remain distinct
     (``owner/repo#1`` vs ``other/repo#1``).
   * Malformed tokens (empty key after the prefix) are IGNORED — the row has
     no identity from that token (backward-compatible unknown-subject law:
     absent identity on either side is always compatible).
   * The ``lesson`` carve-out is SYMMETRIC: two rows BOTH typed ``lesson``
     are always compatible (reusable lessons generalize across task
     origins). A single lesson row next to a fact row follows the ordinary
     identity rule — a writer cannot opt one row out of subject isolation by
     choosing its type (review finding MX-1/ST-3).
   * Everything else (different sessions, source refs, arbitrary tags,
     shared entities, mentioned files) is NOT a subject conflict.

 Parse cost is LINEAR in the tags length (``re.split`` + one pass; no
 backtracking), and the remote ingest path caps ``tags`` length
 (sync._validate_sync_row) — a hostile tags value cannot amplify CPU
 (review findings ST-1/MX-3/F-06).
"""

from __future__ import annotations

import re

# Stable, machine-readable veto code. Emitted on subject-veto paths; the
# explanation names ONLY this code, the existing row id and the similarity —
# never content, tags, or source_ref of either row (privacy-safe).
SUBJECT_VETO_CODE = "subject_veto"

# Canonical tag-token separators: the store's tags grammar treats commas,
# semicolons and whitespace as token boundaries (mirrors entity.py's
# TAG_TOKEN_SPLIT_RE). Splitting is linear; there is no backtracking.
_TAG_TOKEN_SPLIT_RE = re.compile(r"[,;\s]+")

_TASK_PREFIX = "task:"


def identities(tags: str | None) -> frozenset[str]:
    """Return the set of task-subject identities carried by a tags string."""
    if not tags:
        return frozenset()
    out: set[str] = set()
    for token in _TAG_TOKEN_SPLIT_RE.split(tags):
        if token[:5].lower() != _TASK_PREFIX:
            continue
        key = token[5:].strip().casefold()
        if key:
            out.add(key)
    return frozenset(out)


def incompatible(a_tags: str | None, a_type: str | None,
                 b_tags: str | None, b_type: str | None) -> bool:
    """True iff the two rows' explicit task subjects make a merge
    inadmissible.

    Compatible (returns False) when: BOTH rows are typed ``lesson`` (the
    symmetric reusable-lesson carve-out); either row carries NO identity
    (unknown subject stays backward compatible); or the identity sets
    intersect. Incompatible only when the carve-out does not apply and both
    sides carry disjoint identities.
    """
    if ((a_type or "") == "lesson" and (b_type or "") == "lesson"):
        return False
    a_ids = identities(a_tags)
    if not a_ids:
        return False
    b_ids = identities(b_tags)
    if not b_ids:
        return False
    return a_ids.isdisjoint(b_ids)


def veto_line(existing_id: str, similarity: float) -> str:
    """Stable, privacy-safe subject-veto explanation line."""
    return (
        f"[zmem] dedup skipped: subject conflict with {existing_id} "
        f"(similarity={similarity:.3f}); rows stay separate "
        f"({SUBJECT_VETO_CODE})"
    )
