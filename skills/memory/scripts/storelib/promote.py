from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import glob
from datetime import datetime, timezone
from pathlib import Path
from storelib.schema import STORE_PATH, _resolve_skills_dirs

PROMOTE_CONFIDENCE_FLOOR = 0.85
# Issue #64 (v12): eligibility is the Voyager feedback ladder, enforced here:
# a lesson is promote-eligible only when its explicit usage feedback says it
# WORKS (applied_count >= PROMOTE_APPLIED_FLOOR) and nothing says it doesn't
# (violated_count == 0). retrieval_count/surfaced_count remain passive
# telemetry and are deliberately NOT an eligibility input anymore — surfacing
# a lesson often is exposure, not endorsement.
PROMOTE_APPLIED_FLOOR = 3
# The violated tier: TRUST_VIOLATION_FLOOR_DROP (schema_meta.py) was already
# applied once by `feedback` when violated_count crossed to 2; promote simply
# refuses rows in that state (violated_count = 0 required below).

PROMOTE_SIGNALS = ("test", "compile", "lint")

PROMOTION_REVIEW_DIRNAME = "promotion-candidates"



def _slugify_skill_name(tags: str, fallback_id: str) -> str:
    """Generate a zmem-prefixed skill directory name from tags."""
    import re as _re
    tokens = [t.strip().lower() for t in tags.split(",") if t.strip()]
    # Filter to alphanumeric + hyphen, join with hyphens.
    clean = []
    for t in tokens:
        t = _re.sub(r"[^a-z0-9-]", "", t)
        if t:
            clean.append(t)
    if clean:
        name = "zmem-" + "-".join(clean[:4])  # max 4 tag tokens
    else:
        name = "zmem-promoted-" + fallback_id[:8]
    return name

def _first_sentence(content: str, max_len: int = 220) -> str:
    """Return the first whole sentence of `content`, never cut mid-word.

    Splits on ". " (and other sentence terminators) rather than a raw
    character slice — the old draft did `content[:120]`, which truncates
    mid-word whenever the 120th character lands inside a token. If even the
    first sentence exceeds max_len, truncate at the last whole-word boundary
    before max_len and mark it with an ellipsis (still never mid-word).
    """
    text = re.sub(r"\s+", " ", content.strip())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    sentence = parts[0] if parts else text
    if len(sentence) <= max_len:
        if not sentence.endswith((".", "!", "?")):
            sentence += "."
        return sentence
    truncated = sentence[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,;:- ") + "…"

def _yaml_dquote(s: str) -> str:
    """Escape a string for a YAML double-quoted scalar, collapsed to one line."""
    s = re.sub(r"\s+", " ", s.strip())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _resolve_promotion_review_dir() -> Path:
    explicit = os.environ.get("ZMEM_PROMOTION_REVIEW_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return STORE_PATH.parent / PROMOTION_REVIEW_DIRNAME

def _synthesize_trigger_description(tags: str, content: str) -> str:
    """Build a clean, single-line, trigger-focused description from a memory.

    Format: "Use when working with <tag>, <tag>, ... - <first full sentence
    of content>." Trigger contexts come from `tags` (the explicit signal for
    when this lesson applies); the lesson itself is the first *whole*
    sentence of `content` (never a mid-word slice). This never emits
    placeholder text — it is meant to be usable verbatim, though
    --description lets a human override it with something punchier.
    """
    tokens = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    lesson = _first_sentence(content)
    if tokens:
        trigger_contexts = ", ".join(tokens[:5])
        return f"Use when working with {trigger_contexts} - {lesson}"
    return f"Use when this situation recurs - {lesson}"

def promote_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str | None = None,
    dry_run: bool = False,
    namespace: str | None = None,
    description: str | None = None,
    install_approved: bool = False,
) -> None:
    """Promote high-confidence lessons to reusable SKILL.md files.

    Candidates (issue #64 ladder): type=lesson, signal in (test/compile/lint),
    confidence>=0.85, applied_count >= PROMOTE_APPLIED_FLOOR (3) via explicit
    `store.py feedback --applied`, violated_count == 0 (any violation blocks;
    a violated_count crossing to 2 has already dropped the row's trust_score
    by TRUST_VIOLATION_FLOOR_DROP), not superseded. Does NOT supersede the
    source lesson — the lesson and the skill coexist (the lesson costs ~200
    bytes; if the skill description fails to trigger, the lesson is still in
    recall). Hooks never submit feedback, so promotion is always a human- or
    agent-initiated CLI act — nothing auto-promotes.

    Human-in-the-loop: --dry-run shows candidates, the generated review-candidate
    path, and the eventual install targets. `--id <uuid> --confirm` writes only
    the review candidate. `--install-approved` is the explicit, programmatic
    opt-in that also installs the generated SKILL.md into the live host skill
    dirs. --description overrides the synthesized trigger line verbatim.
    """
    # Candidate query.
    ns_clause = "AND namespace = ?" if namespace else ""
    ns_params = [namespace] if namespace else []
    candidates = conn.execute(
        f"""SELECT id, namespace, type, content, tags, confidence, signal,
                  applied_count, violated_count, valid_from
           FROM memory
           WHERE superseded_at IS NULL
             AND type = 'lesson'
             AND signal IN ('test', 'compile', 'lint')
             AND confidence >= ?
             AND applied_count >= ?
             AND violated_count = 0
             {ns_clause}
           ORDER BY applied_count DESC, confidence DESC""",
        [PROMOTE_CONFIDENCE_FLOOR, PROMOTE_APPLIED_FLOOR] + ns_params,
    ).fetchall()

    if not candidates and not memory_id:
        # Only short-circuit when we're *surveying*. An explicit --id is a
        # human override that does its own live-row lookup below and is not
        # bound by the candidate bar (signal/confidence/total surface events), so
        # returning here would swallow it — an unknown id would print
        # "no promotion candidates found" and exit 0, i.e. a refusal reported
        # as success, which is exactly what the --confirm gate exists to stop.
        print("[zmem] no promotion candidates found")
        return

    skills_dirs = _resolve_skills_dirs()
    review_root = _resolve_promotion_review_dir()

    if dry_run:
        print(f"[zmem] {len(candidates)} promotion candidate(s):")
        for c in candidates:
            skill_name = _slugify_skill_name(c["tags"], c["id"])
            review_skill = review_root / f"{skill_name}-{c['id'][:8]}" / "SKILL.md"
            print(f"\n  [{c['id'][:8]}] (applied={c['applied_count']}, "
                  f"violated={c['violated_count']}, "
                  f"conf={c['confidence']}, signal={c['signal']})")
            print(f"    content: {c['content'][:80]}...")
            print(f"    tags: {c['tags']}")
            print(f"    description would be: {_synthesize_trigger_description(c['tags'], c['content'])}")
            print(f"    would create review candidate: {review_skill}")
            for d in skills_dirs:
                print(f"    install target: {d / skill_name / 'SKILL.md'}")
        return

    if memory_id:
        # Find the specific memory.
        row = conn.execute(
            "SELECT * FROM memory WHERE id=? AND superseded_at IS NULL", (memory_id,)
        ).fetchone()
        if not row:
            print(f"[zmem] no live memory with id {memory_id}", file=sys.stderr)
            return 2

        skill_name = _slugify_skill_name(row["tags"], row["id"])
        skill_targets = [d / skill_name for d in skills_dirs]
        review_dir = review_root / f"{skill_name}-{row['id'][:8]}"
        review_file = review_dir / "SKILL.md"

        # Collision detection — check every target BEFORE writing to any of
        # them, so a collision in one dir never leaves a partial promotion
        # (a skill in one tool's dir but not the other's).
        collisions = [d for d in skill_targets if (d / "SKILL.md").exists()]
        if install_approved and collisions:
            print(f"[zmem] ERROR: skill already exists in {len(collisions)} target dir(s):", file=sys.stderr)
            for d in collisions:
                print(f"  {d}", file=sys.stderr)
            print(f"  Choose a different memory or rename the existing skill.", file=sys.stderr)
            # Exit 2, same as the no-confirm refusal: refused, nothing written.
            # Previously a bare `return` here exited 0, so CUTOVER's re-promotion
            # loop over ~24 existing zmem-* skills would report success to any
            # caller checking $? while writing nothing.
            return 2

        # Trigger description: explicit --description wins verbatim; else
        # synthesize from tags (trigger contexts) + the first whole sentence
        # of content (the lesson) — never a mid-word slice, never
        # placeholder text.
        trigger_line = description if description else _synthesize_trigger_description(row["tags"], row["content"])

        tags_str = row["tags"] or "general"
        trigger_contexts = ", ".join(t.strip() for t in tags_str.split(",") if t.strip()) or "general"
        display_name = skill_name.replace("zmem-", "").replace("-", " ").title()

        # Body sections deliberately carry different content: "When to use"
        # is the trigger contexts (when this should fire), "The rule" is the
        # full lesson content (what to do) — the old draft repeated the same
        # sentence in both plus the description, tripling one idea instead
        # of conveying three.
        draft = f"""---
name: {skill_name}
description: {_yaml_dquote(trigger_line)}
---

# {display_name}

## When to use
Use when working with: {trigger_contexts}.

## The rule
{row['content']}

## Source
- Promoted from zmem lesson `{row['id']}` (applied_count={row['applied_count']},
  violated_count={row['violated_count']}, signal={row['signal']},
  confidence={row['confidence']})
- Namespace: {row['namespace']}
- Tags: {tags_str}
"""

        review_dir.mkdir(parents=True, exist_ok=True)
        review_file.write_text(draft, encoding="utf-8")

        written = []
        if install_approved:
            for skill_dir in skill_targets:
                skill_dir.mkdir(parents=True, exist_ok=True)
                skill_file = skill_dir / "SKILL.md"
                skill_file.write_text(draft, encoding="utf-8")
                written.append(skill_file)

        print(f"[zmem] promotion review candidate for lesson {row['id'][:8]} ->")
        print(f"  {review_file}")
        if written:
            print("[zmem] approved install targets ->")
            for skill_file in written:
                print(f"  {skill_file}")
            print("  The installed skill will load on next session restart.")
        else:
            print("[zmem] candidate only; re-run with --install-approved to install it "
                  "into the live skills dirs.")
        print("  Source lesson KEPT in store (not superseded).")
        return

    # No --id and not --dry-run: show usage.
    print("[zmem] use --dry-run to see candidates, or --id <uuid> to promote a specific lesson")
