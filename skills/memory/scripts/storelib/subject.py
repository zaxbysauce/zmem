"""Task-subject identity compatibility for dedup admission (issue #376).

 PURE module: no database access, no storelib sibling imports, no I/O.
 The one compatibility predicate defined here is the single source of truth
 for whether two rows' explicitly identified task subjects make a
 similarity-based destructive merge (dedup fold / consolidation absorb /
 tag propagation) inadmissible.

 Identity convention (frozen acceptance contract,
 tests/test_subject_dedup_contract.py):
   * ``task:<key>`` tokens inside the comma-separated ``tags`` string are
     the row's task-subject identities; repository-qualified keys are
     distinct (``owner/repo#N`` vs ``other/repo#N``).
   * Malformed tokens (empty or whitespace-only key) are IGNORED — the row
     has no identity from that token (backward-compatible unknown-subject
     law: absent identity on either side is always compatible).
   * The ``lesson`` row TYPE is exempt: reusable lessons generalize across
     different task origins, so a lesson never conflicts by subject.
   * Everything else (different sessions, source refs, arbitrary tags,
     shared entities, mentioned files) is NOT a subject conflict.
"""

from __future__ import annotations

import re

# Stable, machine-readable veto code. Emitted on subject-veto paths; the
# explanation names ONLY this code, the existing row id and the similarity —
# never content, tags, or source_ref of either row (privacy-safe).
SUBJECT_VETO_CODE = "subject_veto"

# task:<key> token inside a comma-separated tags string. The key must be
# non-empty after trimming; a trailing comma or whitespace-only token is not
# an identity.
_TASK_TAG_RE = re.compile(r"(?:^|,)\s*task:([^,\s][^,]*?)\s*(?=$|,)")


def identities(tags: str | None) -> frozenset[str]:
    """Return the set of task-subject identities carried by a tags string."""
    if not tags:
        return frozenset()
    out: set[str] = set()
    for m in _TASK_TAG_RE.finditer(tags):
        key = (m.group(1) or "").strip()
        if key:
            out.add(key)
    return frozenset(out)


def incompatible(a_tags: str | None, a_type: str | None,
                 b_tags: str | None, b_type: str | None) -> bool:
    """True iff the two rows' explicit task subjects make a merge
    inadmissible.

    Compatible (returns False) when: either row's type is ``lesson`` (the
    reusable-lesson carve-out); either row carries NO identity (unknown
    subject stays backward compatible); or the identity sets intersect.
    Incompatible only when both sides carry identities AND the sets are
    disjoint.
    """
    if (a_type or "") == "lesson" or (b_type or "") == "lesson":
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
