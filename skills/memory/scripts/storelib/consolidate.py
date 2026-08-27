from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
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
from storelib.entity import relink_memory
from storelib.schema import _embeddings, _env_float, _normalize_content, now_iso, worse_taint
from storelib.sync import INGEST_MAX_CONTENT_CHARS
from storelib.write import _merge_on_dedup, supersede_memory

CONSOLIDATE_DEFAULT_THRESHOLD = _env_float("ZMEM_CONSOLIDATE_THRESHOLD", 0.80)
# Cadence gate knobs (env-overridable for parity with the thresholds above):
# how much time must elapse and how much the live set must have grown since the
# last automatic run before consolidate() proceeds.

CONSOLIDATE_MIN_INTERVAL_DAYS = _env_float("ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS", 7.0)

CONSOLIDATE_GROWTH_THRESHOLD = _env_float("ZMEM_CONSOLIDATE_GROWTH_THRESHOLD", 0.20)

# Lexical (token-overlap) clustering fallback threshold, used by consolidate()
# when embeddings are unavailable (no onnxruntime/tokenizers, or the model file
# is absent — Phase 10). Jaccard similarity of content+tags token sets is a
# coarser signal than cosine similarity of embeddings, so this is deliberately
# a separate, independently-tunable knob rather than reusing the cosine
# threshold's scale.

CONSOLIDATE_LEXICAL_THRESHOLD = _env_float("ZMEM_CONSOLIDATE_LEXICAL_THRESHOLD", 0.60)

# Safety cap on how many live rows per namespace consolidate examines in one
# pass. The lexical fallback is O(n²) and the vec0 KNN escalates k up to
# len(rows); on a pathological store this is unbounded. Capping the INPUT set
# (not dropping candidates mid-scan) bounds the cost while preserving
# completeness WITHIN the batch; namespaces larger than this report a
# `truncated` status so the operator knows not every pair was examined (#36 M8).
# 5000 is far above any realistic curated zmem namespace, so real stores see no
# truncation. Fixed (not env-tunable) to avoid a misconfigured knob silently
# disabling consolidation completeness.

CONSOLIDATE_MAX_ROWS_PER_NAMESPACE = 5000


_LEXICAL_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "as", "by", "from",
    "not", "no", "so", "if", "then", "than", "into", "onto", "when",
}



def _lexical_tokens(text: str | None) -> set[str]:
    """Tokenize text into a lowercase word set for Jaccard-overlap clustering.
    Drops short tokens and common stopwords so similarity reflects content
    words, not incidental grammar overlap."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _LEXICAL_STOPWORDS}

def _lexical_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Jaccard similarity between two token sets. 0.0 if either is empty."""
    if not tokens_a or not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)

_CONSOLIDATE_NEGATOR_RE = re.compile(
    r"\b(?:never|don'?t|do not|doesn'?t|can'?t|cannot|won'?t|not|avoid|stop|no longer)\b",
    re.IGNORECASE,
)



def _polarity_signature(content: str | None) -> bool:
    """True when ``content`` carries negation polarity: it contains a negator
    OUTSIDE code spans and double-quoted strings (the issue's suggested cheap
    refinement — a quoted error message like ``"module not found"`` inside
    otherwise-positive guidance must not flip the polarity). Curly apostrophes
    are normalized so ``don’t`` matches like ``don't``."""
    text = (content or "").replace("\u2019", "'")
    text = re.sub(r"`[^`]*`", " ", text)      # code spans
    text = re.sub(r'"[^"\n]*"', " ", text)    # double-quoted spans
    return bool(_CONSOLIDATE_NEGATOR_RE.search(text))

def dedup_polarity_conflict(
    conn: sqlite3.Connection, existing_id: str, content: str
) -> bool:
    """v11 (issue #61, 6.2): does `content` CONTRADICT the live row
    `existing_id` by negation polarity?

    The single source of the write-time polarity guard shared by
    add_memory, update_memory, and ingest-jsonl's dedup path (PRR-010: the
    three sites previously carried verbatim copies that could drift). A
    missing row is NOT a conflict (callers keep their pre-guard behavior).
    Callers import this function-locally: this module imports write/sync at
    module top, so a top-level import from those modules would cycle.
    """
    row = conn.execute(
        "SELECT content FROM memory WHERE id=?", (existing_id,)
    ).fetchone()
    if row is None:
        return False
    return _polarity_signature(row["content"] or "") != _polarity_signature(content)


def _normalize_text(text: str) -> str:
    """Normalize text for substring/overlap comparison: collapse runs of
    whitespace to a single space and strip+lowercase. Used by
    _absorb_into_keeper to decide whether an absorbed row's content is already
    represented in the keeper (so we don't duplicate exact-duplicate text) and
    by the dry-run preview to label what would be lost."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())

def _unique_tokens(text: str | None) -> set[str]:
    """All ``[a-z0-9]+`` runs in ``text`` (lowercased), regardless of length.

    Unlike ``_lexical_tokens`` (which drops tokens shorter than 3 chars and
    stopwords for Jaccard CLUSTERING), this retains 1-2 char tokens — including
    1-2 digit numbers, version codes, exit codes, IP fragments — so the
    uniqueness check in ``_absorb_decision`` does not silently classify a row
    that differs only in such tokens as "already represented" and lose them.
    Reusing the clustering tokenizer for "what is unique about this row"
    conflated "structurally significant for similarity" with "carries unique
    information" (implementation-review finding #2)."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))

def _absorb_decision(keeper_content: str, absorbed_content: str, absorbed_id: str) -> dict:
    """PURE decision: would `_absorb_into_keeper` append `absorbed_content` to a
    keeper holding `keeper_content`, and what unique tokens would be gained?

    Read-only and side-effect-free so the dry-run preview reports EXACTLY what
    the real merge does (same predicate). Returns:
      {"will_append": bool,
       "reason": "unique"|"already-represented"|"size-cap"|"empty",
       "new_tokens": list[str]}  # tokens in absorbed not in keeper
    """
    norm_keeper = _normalize_text(keeper_content)
    norm_absorbed = _normalize_text(absorbed_content)

    if not norm_absorbed:
        return {"will_append": False, "reason": "empty", "new_tokens": []}
    if norm_absorbed in norm_keeper:
        # The absorbed row's text is fully contained in the keeper -> nothing
        # unique to preserve. (Note: the REVERSE — keeper a substring of
        # absorbed — is NOT already-represented: the absorbed row carries a
        # longer text with a unique tail that must be preserved. Issue #19.)
        return {"will_append": False, "reason": "already-represented", "new_tokens": []}

    # Token-level uniqueness, used for preview bookkeeping and the size-cap
    # "would-lose tokens" list. Length-agnostic tokenizer (NOT the clustering
    # tokenizer) so 1-2 digit numbers / short codes that differ between rows
    # count as unique information to preserve (implementation-review #2).
    new_tokens = sorted(_unique_tokens(absorbed_content) - _unique_tokens(keeper_content))

    # NOTE: an EMPTY new_tokens set is deliberately NOT "already-represented"
    # (swarm-pr-review PRR-001). Two rows can share the same word SET in a
    # different ORDER (e.g. "call foo before bar" vs "call bar before foo") and
    # that ordering carries meaning we must not drop from live recall. The ONLY
    # grounds for suppressing the append is the substring check above
    # (`norm_absorbed in norm_keeper`), which covers exact duplicates plus
    # case- and whitespace-only differences (those normalize to the SAME string,
    # so they are substrings of each other). _normalize_text does NOT strip
    # punctuation, so a punctuation-only difference is a distinct surface form
    # and is also appended. Reordered / identical-token / punctuation-different
    # content all fall through and are appended — deliberate over-preservation
    # (issue #19 AC1): an append is lossless, and the size cap below still
    # bounds keeper growth.

    # Size cap (issue #19 critic finding #3): never let a merged keeper exceed
    # INGEST_MAX_CONTENT_CHARS, or an export-jsonl -> ingest-jsonl round-trip
    # would reject it (the ingest validator enforces this ceiling). Skip the
    # content-append but still record the id (with a `:truncated` marker) and
    # still run the metadata merge + supersede. Never truncate memory content
    # mid-string (see the cap's own rationale at its definition).
    #
    # NOTE: this check is against the keeper content AS PASSED IN.
    # _absorb_into_keeper re-reads the keeper's GROWN content from the DB before
    # calling this, so in a multi-absorb cluster the cap (and the uniqueness
    # check) are evaluated against content already grown by earlier absorbs in
    # the same cluster — matching what the dry-run preview reports. This closes
    # both the cumulative-cap-exceed path (implementation-review finding #1) and
    # the stale-keeper-content path (final-critic finding #1).
    separator = f"\n\n--- merged from {absorbed_id} ---\n"
    projected = len(keeper_content) + len(separator) + len(absorbed_content)
    if projected > INGEST_MAX_CONTENT_CHARS:
        return {"will_append": False, "reason": "size-cap", "new_tokens": new_tokens}

    return {"will_append": True, "reason": "unique", "new_tokens": new_tokens}

def _dedupe_merged_from(value: str | None) -> str:
    """Normalize a merged_from provenance string to a de-duplicated,
    first-seen-order list (issue #61, 6.6).

    Entries are comma-joined ids, optionally suffixed ``:truncated`` (the
    size-cap marker). The same id can historically appear twice (the old
    writer plain-appended with no dedup); this helper keeps each BASE id's
    first occurrence — marker preserved as it first appeared — and drops
    later repeats. No id is ever lost; empty/None stays empty. Idempotent by
    construction, which is what lets the v11 migration run it over every row
    and lets a crash mid-migration safely re-run it.
    """
    if not value:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        base = entry[:-len(":truncated")] if entry.endswith(":truncated") else entry
        if base in seen:
            continue
        seen.add(base)
        out.append(entry)
    return ",".join(out)


def _absorb_into_keeper(
    conn: sqlite3.Connection,
    keeper: sqlite3.Row,
    absorbed: sqlite3.Row,
) -> dict:
    """Fold ONE absorbed near-duplicate row into its cluster keeper (issue #19).

    This is consolidate()-ONLY: it preserves information unique to the absorbed
    row by appending that text to the keeper's content (so it stays
    live-recallable and FTS-indexed) and recording the absorbed id in the
    keeper's ``merged_from`` provenance column. The absorbed row is then
    superseded (tombstoned, kept for history). Metadata (confidence/signal/tags)
    is upgraded via the shared ``_merge_on_dedup``.

    This is deliberately NOT ``_merge_on_dedup``: that helper is also the
    write-time dedup path for ``add``/``ingest-jsonl``, where re-observing a
    paraphrase must refresh metadata only -- growing content there would let
    every re-add bloat a memory. Content preservation is a consolidate-only
    concern (see issue #19 root cause).

    MUST be called inside the caller's transaction (consolidate opens a
    BEGIN/COMMIT per cluster); this helper never commits/rollbacks on its own.

    Returns the dict from ``_absorb_decision`` (will_append / reason / new_tokens)
    so the caller's bookkeeping reflects the real merge decision.
    """
    keeper_id = keeper["id"]
    absorbed_content = absorbed["content"] or ""

    # IMPORTANT (final-critic finding #1): decide against the keeper's content AS
    # GROWN by earlier absorbs in this same cluster, not the seed row's original
    # content. The seed row is fetched ONCE before the cluster loop, so
    # `keeper["content"]` is stale for the 2nd+ absorb. Re-read the current
    # keeper content from the DB each time (the prior _absorb_into_keeper
    # updated it in-tx). This makes the real merge decide on the SAME input the
    # dry-run accumulates, so the preview matches reality — and it prevents
    # appending text already present in the grown keeper (which would both
    # duplicate content and waste size-cap budget, the latter able to push a
    # later genuinely-unique absorb to :truncated and back into issue #19's
    # data-loss class).
    cur = conn.execute("SELECT content, taint FROM memory WHERE id=?", (keeper_id,)).fetchone()
    keeper_content = cur["content"] if cur else (keeper["content"] or "")
    keeper_taint = cur["taint"] if cur else "trusted_internal"

    decision = _absorb_decision(keeper_content, absorbed_content, absorbed["id"])
    will_append = decision["will_append"]

    if will_append:
        # _absorb_decision already checked the size cap against the grown keeper
        # content (re-read above), so the projection is current. Append.
        separator = f"\n\n--- merged from {absorbed['id']} ---\n"
        new_content = keeper_content + separator + absorbed_content
        conn.execute("UPDATE memory SET content=?, content_norm=? WHERE id=?",
                     (new_content, _normalize_content(new_content), keeper_id))

    # Record provenance: append the absorbed id to merged_from (comma-joined,
    # accumulates across runs). `:truncated` marks an id whose content could
    # not be appended due to the size cap; the id is still recorded for
    # traceability. Format is documented for future consumers.
    #
    # SAFETY: the comma-joined format relies on the invariant that every id is
    # a UUID (enforced by _INGEST_ID_RE during import), which cannot contain a
    # comma — so split(",") round-trips losslessly today. If id schemes are
    # ever widened beyond UUIDs, migrate this to a self-delimiting encoding
    # (e.g. JSON array) before allowing non-UUID ids into merged_from.
    marker = ":truncated" if decision["reason"] == "size-cap" else ""
    id_entry = f"{absorbed['id']}{marker}"
    row = conn.execute("SELECT merged_from FROM memory WHERE id=?", (keeper_id,)).fetchone()
    existing = (row["merged_from"] if row and row["merged_from"] else "").strip()
    # v11 (issue #61, 6.6): the append is normalized through the shared
    # de-duplicator, so merged_from can only ever be a first-seen-order list
    # of UNIQUE ids (the pre-v11 writer plain-appended and could repeat one).
    merged_from = _dedupe_merged_from(f"{existing},{id_entry}" if existing else id_entry)
    conn.execute("UPDATE memory SET merged_from=? WHERE id=?", (merged_from, keeper_id))

    # Metadata upgrade (confidence/signal/tags + retrieval_count bump). Shared
    # with write-time dedup semantics; no content change here.
    _merge_on_dedup(conn, keeper_id, absorbed["confidence"], absorbed["signal"], absorbed["tags"])

    # Worst-of taint propagation (issue #59, 4.7): consolidation folds the
    # absorbed row INTO the keeper permanently, so the keeper's taint becomes
    # the lineage's worst — a merged row must never DOWNGRADE a riskier member
    # to a trusted one (that would let an untrusted tool's memory surface
    # unfenced). This is the one place taint travels sideways; plain
    # supersede/invalidate deliberately does NOT get this (that path has no
    # incoming live row to hoist).
    merged_taint = worse_taint(keeper_taint, absorbed["taint"])
    if merged_taint != keeper_taint:
        conn.execute("UPDATE memory SET taint=? WHERE id=?", (merged_taint, keeper_id))

    # Tombstone the absorbed row (kept for history; removed from live recall).
    # valid_until closes the row's validity interval at the same instant as
    # superseded_at, so a point-in-time --as-of (which drops the superseded
    # filter) still sees this row as dead at every T >= the tombstone (issue
    # #59, 4.4 as-of soundness).
    tomb_at = now_iso()
    conn.execute(
        "UPDATE memory SET superseded_at=?, valid_until=?, supersede_reason=? WHERE id=?",
        (tomb_at, tomb_at, f"consolidated into {keeper_id}", absorbed["id"]),
    )
    try:
        conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (absorbed["id"],))
    except sqlite3.OperationalError:
        pass

    # v10 (issue #60): the keeper's content/tags may have grown above, so its
    # entity links are re-derived from the stored row. The ABSORBED row keeps
    # its links untouched: it is history now, and --as-of recall reaches it
    # through the entity lane's temporal predicate — same policy as
    # update_memory's tombstoned row.
    relink_memory(conn, keeper_id)

    return decision


def _gather_neighbors(
    conn: sqlite3.Connection,
    seed: sqlite3.Row,
    rows: list[sqlite3.Row],
    *,
    use_lexical: bool,
    lexical_tokens: dict[str, set[str]],
    effective_threshold: float,
    absorbed: set[str],
    restrict_ids: set[str] | None = None,
) -> tuple[list[tuple[sqlite3.Row, float]], bool]:
    """Return the qualifying neighbors of ``seed`` among ``rows``, plus whether the
    vec0 KNN hit its k cap with every returned row still above threshold.

    This is the SINGLE source of the cluster/topic neighbor predicate shared by
    the ``consolidate`` seed loop and the phase-7 ``organize`` related-graph
    (issue #62, 7.2): both paths must decide "is this row related to seed at
    this similarity threshold" identically, or organize's topics would drift
    from what consolidate would cluster. Extracted VERBATIM from the former
    inline loop — do not "clean up" either branch here without changing both
    callers in the same edit.

    Semantics preserved from the original loop:
    - Namespace containment: a neighbor is ALWAYS scoped to the seed's own
      namespace (the background no--namespace run must never merge across
      projects).
    - A row already claimed by an earlier cluster (``absorbed``) is skipped.
    - vec0 path: escalate k until the qualifying set is provably complete;
      cap k at ``min(max(len(rows), 10), 500)`` and set ``knn_truncated`` when
      the cap binds with all rows still above threshold. Every candidate is
      re-fetched with a liveness AND same-namespace check (vec0 is
      namespace-blind; the seed loop's correctness depends on this re-fetch).
    - An unembedded seed in cosine mode participates in NO edges (returns
      empty): consolidate pre-filters unembedded rows out of its candidate set,
      while ``organize``'s related-graph deliberately includes every live
      working row — an unembedded row there simply joins a topic only via
      shared-entity grouping, never via a neighbor edge.

    ``restrict_ids`` (issue #62, editorial round — Claude Code F-001): when
    set, the vec0 KNN is a GLOBAL index scan whose result ids are then
    filtered down to ``restrict_ids`` before the liveness re-fetch. This is
    the OPT-IN escape hatch that lets ``organize`` bound the related-graph (and
    ``consolidate`` under ``working_ids``) to exactly its native episode,
    while a bare ``consolidate`` on the full store keeps the historical global
    KNN behaviour byte-identical. The lexical branch iterates ``rows`` and is
    therefore already bounded — the filter is a cosine-only concern. A
    neighbor whose id is absent from ``restrict_ids`` can never be returned,
    so a callers' union-find over ``rows`` can never see an out-of-episode id
    from this function.

    Read-only (no writes, no commits).
    """
    neighbors: list[tuple[sqlite3.Row, float]] = []
    knn_truncated = False

    if use_lexical:
        seed_tokens = lexical_tokens.get(seed["id"]) or set()
        for row in rows:
            if row["id"] == seed["id"] or row["id"] in absorbed:
                continue
            if row["namespace"] != seed["namespace"]:
                continue  # namespace containment — see the note above
            sim = _lexical_similarity(seed_tokens, lexical_tokens.get(row["id"]) or set())
            if sim >= effective_threshold:
                neighbors.append((row, sim))
    else:
        if seed["embedding"] is None:
            return [], False
        results = []
        k = 10
        k_cap = min(max(len(rows), 10), 500)
        while True:
            try:
                results = conn.execute(
                    "SELECT memory_id, distance FROM memory_vec "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    [seed["embedding"], k],
                ).fetchall()
            except sqlite3.OperationalError:
                results = []
                break
            if len(results) < k:
                break  # vec0 exhausted — this is every row it can offer
            if any((1.0 - r["distance"]) < effective_threshold for r in results):
                break  # saw the cutoff — the qualifying set is complete
            if k >= k_cap:
                knn_truncated = True
                break
            k = min(k * 5, k_cap)
        for r in results:
            mid = r["memory_id"]
            if mid == seed["id"] or mid in absorbed:
                continue
            if restrict_ids is not None and mid not in restrict_ids:
                # OUT-OF-EPISODE: the global index offered it, but the caller's
                # episode does not include it — it is not a candidate neighbor.
                continue
            sim = 1.0 - r["distance"]
            if sim >= effective_threshold:
                row = conn.execute(
                    "SELECT id, confidence, signal, tags, retrieval_count, "
                    "surfaced_count, content, taint "
                    "FROM memory "
                    "WHERE id=? AND superseded_at IS NULL AND namespace=?",
                    (mid, seed["namespace"]),
                ).fetchone()
                if row:
                    neighbors.append((row, sim))

    return neighbors, knn_truncated


def _prune_unrecalled_days() -> int:
    """ZMEM_UNRECALLED_DAYS (issue #62, 7.6), default 30, read LAZILY at call
    time. Prune may additionally qualify a live row whose ``last_surfaced`` is
    older than this many days (see the prune block). Lazy read so tests can
    vary it per invocation without reloading; absent/garbage/non-numeric -> 30.
    OverflowError is caught too (`int(float('inf'))` raises it — a deliberately
    extreme env value must degrade to the default, not crash the prune pass)."""
    try:
        raw = os.environ.get("ZMEM_UNRECALLED_DAYS", "").strip()
        if not raw:
            return 30
        n = int(float(raw))
        return n if n >= 0 else 30
    except (TypeError, ValueError, OverflowError):
        return 30


def _cadence_gate_skipped(
    conn: sqlite3.Connection, *, force: bool, dry_run: bool, label: str,
) -> bool:
    """Shared 7d / 20%-growth cadence gate (issues #26 / #62) — ONE
    implementation for both entry points to the same maintenance act.

    ``consolidate()`` and ``organize()`` were carrying two byte-identical
    copies of this predicate; that is exactly the drift class this module's
    shared-neighbor extraction exists to prevent (Claude Code round 4
    test-coverage list). ``label`` is the caller's singular noun for the
    human message ('consolidate' / 'organize'); the meta keys, knobs,
    thresholds, and both dry-run/real message frames are shared. The knob
    values are this OWNING module's constants (refreshed together by
    ``_refresh_env_state``), so both entry points always read the same
    instance — the core of PRR-007: organize must never snapshot these by
    value at its own import or the two entry points could drift.

    Returns True when the run must skip (the gate is armed AND the store has
    not grown enough). Callers set their own ``skipped_by_cadence_gate`` flag
    and return early — the clock is left untouched so a skip never arms it.
    """
    last_consolidation = conn.execute(
        "SELECT value FROM meta WHERE key='last_consolidation'"
    ).fetchone()
    if not last_consolidation or force:
        return False
    import calendar as _cal
    last_ts = last_consolidation[0]
    last_epoch = (
        _cal.timegm(time.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ"))
        if last_ts else 0
    )
    days_since = (time.time() - last_epoch) / 86400.0 if last_epoch > 0 else 999
    live_count = conn.execute(
        "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
    ).fetchone()[0]
    last_count = conn.execute(
        "SELECT value FROM meta WHERE key='last_consolidation_count'"
    ).fetchone()
    last_live = int(last_count[0]) if last_count and last_count[0].isdigit() else 0
    growth = (live_count - last_live) / max(last_live, 1)

    # The knob values are the OWNING module's constants (refreshed by
    # store-level _refresh_env_state), not direct env reads: both entry points
    # read the SAME instance, so they can never diverge, and an env override
    # set at process start (the supported surface) governs identically.
    min_days = CONSOLIDATE_MIN_INTERVAL_DAYS
    min_growth = CONSOLIDATE_GROWTH_THRESHOLD
    if days_since >= min_days or growth >= min_growth:
        return False
    # Announce the skip (never silent — issue #26). dry_run and the real run
    # share the gate so the two modes agree. Background callers
    # (zmem-session-start.sh, hermes on_session_end) redirect stdout to
    # /dev/null, so this stays silent there by design; the interactive
    # closeout user reading stdout is who needs to see it.
    if dry_run:
        print(f"[zmem] {label}: dry-run: would skip by cadence gate "
              f"({days_since:.1f}d since last run < "
              f"{min_days:g}d min, {growth:.1%} growth < "
              f"{min_growth:.1%} min; needs more "
              f"time OR more growth; drop --dry-run and pass --force to "
              f"run anyway)")
    else:
        print(f"[zmem] {label}: skipped by cadence gate "
              f"({days_since:.1f}d since last run < "
              f"{min_days:g}d min, {growth:.1%} growth < "
              f"{min_growth:.1%} min; needs more "
              f"time OR more growth)")
    return True


def _nli_first_sentence(text: str | None, max_len: int | None = 2000) -> str:
    """First whole sentence of ``text`` (bounded) sent to the NLI judge.
    Verbatim content is too noisy; the two-sentence contract (issue #62, 7.5)
    is the first sentence of each side. ``max_len=None`` disables the bound so
    a caller that must preserve the WHOLE first sentence verbatim (organize's
    extractive bullets — PRR-010) is never truncated mid-sentence."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if not collapsed:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", collapsed)
    first = parts[0] if parts else collapsed
    if max_len is not None:
        return first[:max_len]
    return first


def _nli_judge_pair(argv: list[str], a: str, b: str) -> str:
    """Run the LOCAL contradiction judge on two texts; return the normalized
    verdict in {entailment, contradiction, neutral}. Any failure — non-zero
    exit, timeout, OSError, or non-matching stdout — degrades to ``neutral``
    (the caller then falls back to polarity and does NOT merge). The exact
    stdin contract: two first-sentences, one newline-separated line each, plus
    a trailing newline. argv is a list passed straight to CreateProcess on
    Windows (never ``shell=True``). The judge's stdin/stdout are decoded as
    UTF-8 (7.5 negotiated content can be any script), and a failure is
    surfaced to stderr as a distinguishable diagnostic instead of silently
    swallowing the cause into "neutral" (Claude Code F-007/F-008)."""
    a_first = _nli_first_sentence(a)
    b_first = _nli_first_sentence(b)
    try:
        proc = subprocess.run(
            argv, input=f"{a_first}\n{b_first}\n",
            capture_output=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[zmem] consolidate: NLI judge could not be invoked "
              f"({argv!r}): {exc}; degrading to neutral", file=sys.stderr)
        return "neutral"
    if proc.returncode != 0:
        print(f"[zmem] consolidate: NLI judge exited {proc.returncode} "
              f"({argv!r}); degrading to neutral", file=sys.stderr)
        return "neutral"
    return proc.stdout.strip().lower()


def _nli_judge_all_entail(member_pols: list[tuple]) -> bool | None:
    """Issue #62, 7.5: does the optional local NLI judge resolve EVERY
    polarity-FLAGGED pair (differing ``_polarity_signature``) in this cluster
    as entailment?

    Returns True (un-park -> merge), False (park), or None when ``ZMEM_NLI_CMD``
    is unset — the caller then behaves BYTE-IDENTICALLY to the pre-judge code
    path (no print, no subprocess import). Only ``entailment`` may un-park;
    ``neutral``/``contradiction``/crash/non-zero/garbage all mean park (fall
    back to polarity, never auto-merge). All-entailment semantics: one
    non-entailing flagged pair parks the whole cluster. Env var read lazily
    each call.

    A "polarity-flagged pair" is ANY pair of members whose negation-polarity
    signatures differ — not only the pair anchored on the keeper (PRR-004): a
    non-keeper pair at opposite polarity is every bit as contradictory as one
    touching the keeper, so all-entailment must hold across every differing
    pair or the cluster parks. Pair order is deterministic (first-seen member
    order). On Windows the argv template's backslashes are normalized to
    forward slashes before the shlex split so ``python C:\path\judge.py``
    survives the POSIX backslash-stripping (Claude Code F-007)."""
    cmd = os.environ.get("ZMEM_NLI_CMD", "").strip()
    if not cmd:
        return None
    if os.name == "nt":
        # subprocess never runs a shell here; backslashes in the template are
        # WindowS path separators, never escape sequences. Forward slashes keep
        # them intact through shlex.split's POSIX backslash handling while
        # leaving quoted-token semantics (and spaces) untouched.
        cmd = cmd.replace("\\", "/")
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = []
    if not argv:
        return None
    flagged = []
    for i in range(len(member_pols)):
        for j in range(i + 1, len(member_pols)):
            if member_pols[i][2] != member_pols[j][2]:
                # Deterministic order: earlier member index first.
                a, b = member_pols[i][1], member_pols[j][1]
                flagged.append((a, b))
    if not flagged:
        return False
    for a, b in flagged:
        verdict = _nli_judge_pair(argv, a, b)
        print(f"[zmem] consolidate: NLI judge: [{a[:40]!r} vs {b[:40]!r}] -> {verdict}")
        if verdict != "entailment":
            return False
    return True

def consolidate(
    conn: sqlite3.Connection,
    *,
    threshold: float = CONSOLIDATE_DEFAULT_THRESHOLD,
    prune: bool = False,
    dry_run: bool = False,
    namespace: str | None = None,
    force: bool = False,
    merge_contested: bool = False,
    working_ids: set[str] | None = None,
    collect_run_ids: bool = False,
) -> dict:
    """Merge near-duplicate memories via embedding similarity (or a lexical
    token-overlap fallback when embeddings are unavailable — Phase 10).

    Returns a machine-readable report dict (issue #49): ``{mode, dry_run,
    merge_contested, threshold, skipped_by_cadence_gate, merged, pruned,
    contested_clusters, truncated, knn_truncated}``. NOTE: in dry-run mode ``merged``/``pruned``
    are COUNTERFACTUAL would-be counts — qualify with ``dry_run`` (PR
    feedback PRR-010), mirroring the hardened human "would merge" verb.
    ``merged`` in a ``contested_clusters`` entry is a plain fact-claim about
    the store: True only after that cluster's transaction COMMITted; False in
    dry runs, after a rollback, and for parked contested clusters.
    ``contested_clusters`` lists clusters whose
    members differ in negation polarity (see ``_polarity_signature``): they
    are NEVER auto-merged — not even with ``force`` (which bypasses only the
    cadence gate) — because similarity alone cannot distinguish "always X"
    from "never X", and merging would absorb a memory's own refutation into
    the row it contradicts. Contested clusters are reported for human/agent
    resolution via ``supersede`` instead (issue #49 Deliverable A; concept
    ported from claude-reflect's contradiction category). ``merge_contested``
    is the explicit override for heuristic false positives; when set, the
    contested clusters merge like any other and the report marks them
    ``merged: true``. Contested members stay live and remain eligible as
    NEIGHBORS of later clusters, but do not re-seed within the same run (no
    duplicate mirror report).

    For each live memory with an embedding, query vec0 KNN for nearest neighbors.
    Cluster memories with cosine similarity >= threshold. For each cluster:
    pick the keeper (highest confidence * total surface events, where total surface
    events = retrieval_count + surfaced_count — issue #21), merge the absorbed
    members into the keeper (preserving content unique to each absorbed row —
    issue #19), and supersede the absorbed members. Each cluster commits
    atomically — interruption is safe because keeper selection is deterministic.

    The keeper is chosen by the highest ``confidence * (retrieval_count +
    surfaced_count)`` product — total surface events, blend-aware for hook-surfaced
    memories (issue #21). Ties are broken by ``confidence`` DESC (so when every count
    is 0 — the common fresh-store case where the product is 0 for all rows — the
    higher-confidence row still wins instead of the order becoming UUID-noise),
    then earliest ``ingestion_ts``, then ``id`` for single-writer determinism.
    The rows query below orders by exactly that key so the first row of a
    cluster is the keeper. (Issue #19 defect 2: the previous ``ORDER BY
    confidence DESC, retrieval_count DESC`` was lexicographic — confidence
    dominated absolutely, contradicting the documented product rule and
    destroying higher-product rows.)

    Content preservation (issue #19 defect 1): an absorbed row's text is
    appended to the keeper under a provenance separator (and its id recorded in
    the keeper's ``merged_from`` column), so information unique to an absorbed
    row stays live-recallable and FTS-indexed rather than being lost when the
    absorbed row is tombstoned. See ``_absorb_into_keeper``.

    When embeddings are unavailable (no onnxruntime/tokenizers, or the model
    file is absent/not yet downloaded), clustering falls back to Jaccard
    similarity of content+tags token sets (CONSOLIDATE_LEXICAL_THRESHOLD)
    instead of cosine — same keeper/merge/supersede mechanics, just a coarser
    similarity signal. `consolidate` never hard-requires the model.

    If prune=True, also supersede memories that were NEVER surfaced and NEVER
    retrieved — retrieval_count=0 AND surfaced_count=0, signal=none, confidence<0.35,
    age>30d (opt-in, never automatic on SessionStart). A memory surfaced by the hook
    path (surfaced_count>0) is protected: `retrieval_count = 0` alone is NOT evidence
    of unused (issue #21).

    Cadence gate (issue #26): a run is skipped when the last consolidation was
    less than ``CONSOLIDATE_MIN_INTERVAL_DAYS`` ago AND the live set has grown
    less than ``CONSOLIDATE_GROWTH_THRESHOLD`` since. The skip is ALWAYS announced
    (never silent), and ``dry_run=True`` models the SAME gate so the two modes
    agree — a dry run that reports "would merge" implies a real run that merges,
    and a gated dry run reports "would skip" rather than "merged N". ``force=True``
    is the only intentional bypass. ``threshold`` no longer affects the gate (the
    previous behaviour where any non-default ``--threshold`` incidentally bypassed
    it was an undocumented side-channel and is removed).

    ``working_ids`` (issue #62, 7.1): when set, the candidate set is narrowed to
    exactly those live rows (still bounded by the per-namespace cap and ordered
    by the documented keeper-priority key). This is how the phase-7 ``organize``
    job bounds its episode to the N most recent live rows and then runs the SAME
    cluster logic 1:1 on that bounded set. Default None keeps the historical
    box-wide semantics byte-identical.

    ``collect_run_ids`` (issue #62, 7.4): when True, ``report["consolidated_ids"]``
    lists the id of every KEEPER in a cluster that actually merged (committed) —
    or, in dry-run, that the preview marked would-APPEND — so ``organize`` can
    compress exactly the keepers its absorb pass grew. Contested/parked clusters
    are never listed (they did not merge). Additive-only; the key is absent when
    False so every existing caller's report is unchanged.

    ``ZMEM_NLI_CMD`` (issue #62, 7.5): when set to an argv template, the optional
    LOCAL contradiction judge is consulted for mixed-polarity clusters before
    they are parked contested; only a verdict of ``entailment`` on EVERY
    polarity-flagged pair un-parks the cluster (merges it). Neutral /
    contradiction / crash / non-zero exit / garbage stdout all fall back to
    today's regex polarity (park, never auto-merge). Unset = byte-identical to
    the pre-judge code path. ``ZMEM_UNRECALLED_DAYS`` (issue #62, 7.6): prune may
    additionally qualify live rows whose ``last_surfaced`` is older than this
    many days (see the prune block).
    """
    use_lexical = not (_embeddings and _embeddings.is_available())
    if use_lexical:
        print("[zmem] embeddings unavailable — consolidating via lexical token overlap", file=sys.stderr)

    # Machine-readable run report (issue #49). Returned from EVERY exit path;
    # the CLI's --json mode prints it as the sole stdout payload (all human
    # prose is redirected to stderr there), so agents on any host — including
    # Hermes via its own tooling — can act on contested clusters without
    # scraping human text.
    report: dict = {
        "mode": "lexical" if use_lexical else "cosine",
        "dry_run": dry_run,
        "merge_contested": merge_contested,
        "threshold": threshold,
        "skipped_by_cadence_gate": False,
        "merged": 0,
        "pruned": 0,
        "contested_clusters": [],
        "truncated": False,
        "knn_truncated": False,
    }
    consolidated_ids: list[str] = []

    # Optional working-set narrowing (issue #62, 7.1): ``working_ids`` bounds
    # the candidate rows to a native episode (organize passes the N most recent
    # live rows). Sorted for a deterministic IN-list. Empty/None -> unchanged.
    ids_clause = ""
    ids_params: list[str] = []
    if working_ids:
        ids_params = sorted(working_ids)
        ids_clause = f" AND id IN ({','.join('?' * len(ids_params))})"

    # Growth-based cadence gate (issue #26): skip if last consolidation was
    # recent AND the store hasn't grown significantly since. The skip is always
    # announced. dry_run models the same gate so the two modes agree; only
    # `force` bypasses it. `threshold` does NOT affect the gate — use --force to
    # override. (Previously any non-default --threshold incidentally bypassed the
    # gate and the skip was silent; both defects are fixed here.) NOTE: the
    # lexical-swap below (effective_threshold) ALSO keys off
    # `threshold == CONSOLIDATE_DEFAULT_THRESHOLD` but for an UNRELATED purpose
    # (picking the Jaccard default in lexical fallback mode); the two predicates
    # are fully decoupled — do not "clean up" one expecting the other to follow.
    # Growth-based cadence gate (issue #26): skip if last consolidation was
    # recent AND the store hasn't grown significantly since. The skip is always
    # announced. dry_run models the same gate so the two modes agree; only
    # `force` bypasses it. Shared with organize() via _cadence_gate_skipped: the
    # two entry points to the same maintenance act must never drift (Claude
    # Code round 4 coverage list). NOTE: the lexical-swap below
    # (effective_threshold) ALSO keys off `threshold ==
    # CONSOLIDATE_DEFAULT_THRESHOLD` but for an UNRELATED purpose (picking the
    # Jaccard default in lexical fallback mode); the two predicates are fully
    # decoupled — do not "clean up" one expecting the other to follow.
    if _cadence_gate_skipped(conn, force=force, dry_run=dry_run, label="consolidate"):
        report["skipped_by_cadence_gate"] = True
        return report  # gate declined — leave last_consolidation untouched

    # Write the consolidation timestamp BEFORE the clustering loop, so a killed
    # run still creates backpressure on the next session. Count is start-of-run
    # (pre-clustering) live count.
    if not dry_run:
        ts = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation', ?)",
            (ts,),
        )
        live_count_now = conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation_count', ?)",
            (str(live_count_now),),
        )
        conn.commit()

    # Load live memories. In embedding mode, only rows with an embedding are
    # candidates (vec0 KNN needs a query vector); in lexical fallback mode every
    # live row is a candidate (Jaccard needs only content/tags). The candidate
    # set is bounded by CONSOLIDATE_MAX_ROWS_PER_NAMESPACE: the lexical fallback
    # is O(n²) and the vec0 KNN escalates k up to len(rows), so an unbounded
    # store would make consolidate pathological. We bound the INPUT set (highest
    # priority first via the existing ORDER BY) — completeness is preserved
    # WITHIN the batch, and a larger namespace reports a `truncated` status so
    # the operator knows not every pair was examined (#36 M8).
    ns_clause = "AND namespace = ?" if namespace else ""
    ns_params = [namespace] if namespace else []
    embed_clause = "" if use_lexical else "AND embedding IS NOT NULL"
    total_eligible = conn.execute(
        f"""SELECT count(*) AS c FROM memory
           WHERE superseded_at IS NULL {embed_clause} {ns_clause}{ids_clause}""",
        [*ns_params, *ids_params],
    ).fetchone()["c"]
    # The cap is PER NAMESPACE (the constant is CONSOLIDATE_MAX_ROWS_PER_NAMESPACE):
    # a window function ranks rows within each namespace by the priority ORDER BY,
    # then keeps the top-N per namespace. This prevents a single large namespace
    # from starving smaller ones in a box-wide (namespace=None) run — every
    # namespace gets its top-N examined regardless of total size (#36 M8).
    rows = conn.execute(
        f"""WITH ranked AS (
               SELECT id, namespace, content, tags, confidence, signal,
                      retrieval_count, surfaced_count, embedding,
                      embedding_model, ingestion_ts, taint,
                      ROW_NUMBER() OVER (
                          PARTITION BY namespace
                          ORDER BY confidence * (retrieval_count + surfaced_count) DESC,
                                   confidence DESC, ingestion_ts ASC, id ASC
                      ) AS rn
               FROM memory
               WHERE superseded_at IS NULL {embed_clause} {ns_clause}{ids_clause}
           )
           SELECT id, namespace, content, tags, confidence, signal, retrieval_count,
                  surfaced_count, embedding, embedding_model, ingestion_ts, taint
           FROM ranked
           WHERE rn <= ?
           ORDER BY confidence * (retrieval_count + surfaced_count) DESC, confidence DESC,
                    ingestion_ts ASC, id ASC""",
        [*ns_params, *ids_params, CONSOLIDATE_MAX_ROWS_PER_NAMESPACE],
    ).fetchall()
    truncated = total_eligible > len(rows)
    # Set True if any seed's vec0 KNN escalation hit the k cap with all-returned
    # rows still above threshold — i.e. the qualifying set may be INCOMPLETE
    # (a same-namespace duplicate could sit beyond the cap). Reported in the
    # summary so the gap is visible, not silent (cubic-4, #36 M8 residual).
    knn_truncated = False

    if not rows:
        print("[zmem] no embeddable memories to consolidate")
        return report

    # Precompute lexical token sets once per row (only used in fallback mode).
    lexical_tokens = {}
    if use_lexical:
        for r in rows:
            lexical_tokens[r["id"]] = _lexical_tokens(
                (r["content"] or "") + " " + (r["tags"] or "")
            )

    # Track which memories have been absorbed (to skip them as seeds).
    absorbed = set()
    # Members of contested (mixed-polarity) clusters excluded from merging this
    # run (issue #49): they do not re-seed (no duplicate mirror report) but are
    # deliberately NOT in `absorbed`, so they stay live and remain eligible as
    # neighbors of later clusters formed around a fresh seed.
    contested_ids = set()
    merged_count = 0
    pruned_count = 0

    # Cosine threshold and lexical (Jaccard) threshold live on different
    # scales. If the caller left --threshold at its cosine default while we're
    # in lexical fallback, swap in the lexical default; an explicit override
    # is respected either way.
    effective_threshold = threshold
    if use_lexical and threshold == CONSOLIDATE_DEFAULT_THRESHOLD:
        effective_threshold = CONSOLIDATE_LEXICAL_THRESHOLD
    report["threshold"] = effective_threshold

    # NAMESPACE CONTAINMENT (data-integrity invariant, both clustering paths):
    # a cluster is ALWAYS scoped to the seed's own namespace, whether or not the
    # caller passed `namespace`. The `ns_clause`/`ns_params` above only narrow
    # which rows are *considered*; they are not a containment guarantee, because
    # the auto-triggered background run (zmem-session-start.sh) passes no
    # --namespace at all. Without the seed-namespace check below, that run could
    # supersede one project's memory into an unrelated project's memory.
    for seed in rows:
        if seed["id"] in absorbed or seed["id"] in contested_ids:
            continue

        # Gather this seed's qualifying neighbors via the SHARED predicate
        # (issue #62, 7.2): consolidate's cluster loop and organize's
        # related-graph must decide "related at this threshold" identically,
        # so the neighbor logic lives in _gather_neighbors (single source).
        neighbors, knn_local = _gather_neighbors(
            conn, seed, rows,
            use_lexical=use_lexical,
            lexical_tokens=lexical_tokens,
            effective_threshold=effective_threshold,
            absorbed=absorbed,
            # Issue #62, 7.1 + editorial round (Claude Code F-001): when the
            # episode is deliberately bounded to ``working_ids``, the vec0 KNN
            # must not leak out-of-episode neighbors in — every neighbor must
            # be a member of the bounded working set. Bare consolidate
            # (working_ids=None) keeps the historical global-KNN behaviour.
            restrict_ids=working_ids,
        )
        if knn_local:
            knn_truncated = True

        if not neighbors:
            continue

        # Contradiction guard (issue #49 Deliverable A): similarity cannot
        # distinguish "always X" from "never X" — they are near-duplicates by
        # cosine/Jaccard — so before merging, check negation polarity across
        # the cluster (keeper + neighbors). A mixed-polarity cluster is
        # CONTESTED: never auto-merged (not even with --force, which bypasses
        # only the cadence gate), always reported. This runs BEFORE the
        # dry_run split so the dry-run preview reports exactly what a real run
        # would do: a contested cluster never appears as a would-APPEND
        # preview, only as the CONTESTED block below.
        member_pols = [(seed["id"], seed["content"], _polarity_signature(seed["content"]))]
        member_pols += [
            (nb["id"], nb["content"], _polarity_signature(nb["content"]))
            for nb, _sim in neighbors
        ]
        contested_override = False
        if len({pol for _, _, pol in member_pols}) > 1:
            # Issue #62, 7.5: optional LOCAL contradiction judge. When
            # ZMEM_NLI_CMD is set, a cluster whose mixed negation polarity is
            # judged ENTAILMENT is a heuristic false positive and un-parks
            # (merges). Unset -> _nli_judge_all_entail returns None and this
            # branch is byte-identical to the pre-judge path; --merge-contested
            # remains the explicit override and still wins over a park verdict.
            _nli_unpark = _nli_judge_all_entail(member_pols)
            if _nli_unpark:
                print(f"[zmem] consolidate: NLI judge: entailment resolves the "
                      f"contested cluster around [{seed['id'][:8]}] - merging "
                      f"(contradiction judged false)")
                contested_override = True
            elif merge_contested:
                # Explicit override for a confirmed heuristic false positive:
                # merge like any other cluster. The report entry is appended
                # only AFTER the outcome is known (dry run → merged: False —
                # nothing happened; COMMIT → merged: True; ROLLBACK → merged:
                # False), so the machine report can never claim a merge that
                # did not happen (PR feedback PRR-001: a premature merged:
                # True lied in --dry-run --merge-contested and after a
                # mid-merge rollback, and suppressed the contested summary
                # line via contested_excluded).
                contested_override = True
            else:
                verb = "would NOT merge (contested)" if dry_run else "NOT merged (contested)"
                print(f"[zmem] consolidate: CONTESTED cluster around [{seed['id'][:8]}] — "
                      f"negation polarity differs; {verb}:")
                for mid, mcontent, mpol in member_pols:
                    print(f"    [{mid[:8]}] {'neg' if mpol else 'pos'}: "
                          f"{(mcontent or '')[:60]}")
                print("    one side likely needs `supersede --id <full-uuid> --reason ...` "
                      "(Step 3 of closeout), not merging; pass --merge-contested only for a "
                      "confirmed heuristic false positive")
                report["contested_clusters"].append({
                    "keeper": seed["id"],
                    "namespace": seed["namespace"],
                    "merged": False,
                    "members": [
                        {"id": mid, "polarity": "neg" if pol else "pos",
                         "content_preview": (mcontent or "")[:60]}
                        for mid, mcontent, pol in member_pols
                    ],
                })
                # Park members for this run: no re-seeding (which would only
                # re-report the same contested cluster from the mirror side),
                # but NOT absorbed — they stay live and neighbor-eligible for
                # later clusters that form around a fresh seed.
                contested_ids.update(mid for mid, _, _ in member_pols)
                continue

        # The seed is the keeper. With the rows query ordered by
        # (confidence * (retrieval_count + surfaced_count) DESC, confidence DESC,
        # ingestion_ts ASC, id ASC), the seed has the highest
        # confidence*total-uses product in its cluster (and the highest confidence
        # on a product tie — the fresh-store case where every count is 0) — matching
        # the documented keeper rule, now blend-aware for hook-surfaced memories
        # (issue #21; issue #19 defect 2: previously the ORDER BY was lexicographic
        # on confidence, contradicting the rule and destroying higher-product rows).
        # Merge each neighbor into it.
        if dry_run:
            seed_uses = (seed["retrieval_count"] or 0) + (seed["surfaced_count"] or 0)
            print(f"[zmem] DRY RUN: cluster around [{seed['id'][:8]}] "
                  f"(conf={seed['confidence']}, rc={seed['retrieval_count']}, "
                  f"sc={seed['surfaced_count']}, "
                  f"prod={seed['confidence'] * seed_uses:.2f}):")
            print(f"    keeper: {seed['content']}")
            # Dry-run uses the SAME decision predicate as the real merge, so the
            # preview reflects what would actually happen (issue #19 defect 3:
            # the previous dry-run only printed a cluster count, hiding the text
            # that would be lost). Accumulate the keeper's grown content as we
            # go so successive absorbs see the keeper as the real merge would.
            dry_keeper_content = seed["content"] or ""
            grew_preview = False
            for nb_row, nb_sim in neighbors:
                dec = _absorb_decision(dry_keeper_content, nb_row["content"] or "", nb_row["id"])
                print(f"    absorb [{nb_row['id'][:8]}] sim={nb_sim:.3f}: "
                      f"conf={nb_row['confidence']} rc={nb_row['retrieval_count']} "
                      f"prod={nb_row['confidence'] * ((nb_row['retrieval_count'] or 0) + (nb_row['surfaced_count'] or 0)):.2f}")
                print(f"        content: {nb_row['content']}")
                if dec["reason"] == "unique":
                    grew_preview = True  # mirror the real-run growth test
                    if dec["new_tokens"]:
                        print(f"        would APPEND (gains tokens: {dec['new_tokens']})")
                    else:
                        # Same token set in a different ORDER — still unique
                        # phrasing that must be preserved (PRR-001).
                        print("        would APPEND (same tokens, different order — preserved)")
                    sep = f"\n\n--- merged from {nb_row['id']} ---\n"
                    dry_keeper_content = dry_keeper_content + sep + (nb_row["content"] or "")
                elif dec["reason"] == "size-cap":
                    print(f"        size-cap: id recorded (:truncated); content NOT appended "
                          f"(would exceed {INGEST_MAX_CONTENT_CHARS}). Would-lose tokens: "
                          f"{dec['new_tokens']}")
                elif dec["reason"] == "empty":
                    print("        empty content; nothing to append (id recorded)")
                else:
                    print("        already represented in keeper; nothing to append")
                absorbed.add(nb_row["id"])  # track in dry-run too
            merged_count += len(neighbors)
            if collect_run_ids and grew_preview:
                consolidated_ids.append(seed["id"])
            if contested_override:
                # Dry run: nothing merged — merged must be False (PRR-001).
                # The print is the only override-preview trace; the report
                # carries dry_run: true as the machine-side qualifier.
                print(f"[zmem] DRY RUN: would merge CONTESTED cluster around "
                      f"[{seed['id'][:8]}] (--merge-contested override)")
                report["contested_clusters"].append({
                    "keeper": seed["id"],
                    "namespace": seed["namespace"],
                    "merged": False,
                    "members": [
                        {"id": mid, "polarity": "neg" if pol else "pos",
                         "content_preview": (mcontent or "")[:60]}
                        for mid, mcontent, pol in member_pols
                    ],
                })
            continue

        # Atomic commit per cluster.
        try:
            conn.execute("BEGIN")
            keeper_grew = False
            for nb_row, nb_sim in neighbors:
                # Preserve absorbed content + record provenance + merge metadata
                # + supersede (issue #19). consolidate-ONLY helper.
                decision = _absorb_into_keeper(conn, seed, nb_row)
                # Issue #62, 7.4 (c76 no-op-absorb blast radius): only a keeper
                # whose content actually GREW this run is a valid compression
                # candidate — a cluster that merged only already-represented /
                # size-capped / empty content appended nothing, and organize
                # must not truncate a keeper this run did not touch (which
                # would also churn the summary member ids for nothing).
                keeper_grew = keeper_grew or decision["will_append"]
                absorbed.add(nb_row["id"])
            # Mark the keeper as consolidated.
            conn.execute(
                "UPDATE memory SET consolidated_at=? WHERE id=?",
                (now_iso(), seed["id"]),
            )
            conn.execute("COMMIT")
            merged_count += len(neighbors)
            if collect_run_ids and keeper_grew:
                consolidated_ids.append(seed["id"])
            if contested_override:
                # Appended only after the successful COMMIT, and printed so a
                # real-run override is never invisible in human output (the
                # JSON report was previously the sole — and lying — audit
                # trail; PRR-001).
                print(f"[zmem] consolidate: merged CONTESTED cluster around "
                      f"[{seed['id'][:8]}] (--merge-contested override; "
                      f"{len(neighbors) + 1} members)")
                report["contested_clusters"].append({
                    "keeper": seed["id"],
                    "namespace": seed["namespace"],
                    "merged": True,
                    "members": [
                        {"id": mid, "polarity": "neg" if pol else "pos",
                         "content_preview": (mcontent or "")[:60]}
                        for mid, mcontent, pol in member_pols
                    ],
                })
        except Exception as exc:
            # A per-cluster failure (e.g. a DB error mid-merge) rolls the whole
            # cluster back and leaves its rows live to be re-clustered next run.
            # Surface it to stderr so content preservation (issue #19) failing
            # for a cluster is visible rather than silently voiding AC1 — the
            # swallowed-exception catch is pre-existing, but the new multi-write
            # _absorb_into_keeper enlarges what this transaction must succeed at
            # (implementation-review finding #3).
            conn.execute("ROLLBACK")
            print(f"[zmem] consolidate: cluster around [{seed['id'][:8]}] failed "
                  f"({type(exc).__name__}: {exc}); rolled back, will retry next run",
                  file=sys.stderr)
            if contested_override:
                # The override did NOT take effect: record it as contested-
                # not-merged (so the summary line counts it) and park the
                # members like the non-override path (PRR-001).
                report["contested_clusters"].append({
                    "keeper": seed["id"],
                    "namespace": seed["namespace"],
                    "merged": False,
                    "members": [
                        {"id": mid, "polarity": "neg" if pol else "pos",
                         "content_preview": (mcontent or "")[:60]}
                        for mid, mcontent, pol in member_pols
                    ],
                })
                contested_ids.update(mid for mid, _, _ in member_pols)
            continue

    # Optional prune: supersede low-value memories that were NEVER surfaced and NEVER
    # retrieved. `surfaced_count = 0` is required so a hook-surfaced memory (issue #21)
    # is protected — `retrieval_count = 0` alone is NOT evidence of unused.
    if prune:
        # Unrecalled-prune extension (issue #62, 7.6): a live row qualifies
        # only when it was NEVER retrieved, is ungrounded + low-confidence +
        # old by ingestion, AND either never surfaced at all OR was last
        # surfaced longer than ZMEM_UNRECALLED_DAYS ago. Rows surfaced recently
        # stay protected; a NULL last_surfaced (never surfaced) still qualifies
        # via surfaced_count=0. v12 (issue #64): the usage-feedback counters
        # (applied_count/violated_count) are deliberately NOT prune inputs —
        # the explicit `feedback` CLI owns them and the promote ladder reads
        # them; feedback-aware pruning would need its own issue + tests.
        # signal!=none is never pruned.
        unrecalled_days = _prune_unrecalled_days()
        prune_rows = conn.execute(
            f"""SELECT id, content FROM memory
               WHERE superseded_at IS NULL
                 AND retrieval_count = 0
                 AND signal = 'none'
                 AND confidence < 0.35
                 AND julianday(ingestion_ts) < julianday('now', '-{int(unrecalled_days)} days')
                 AND (surfaced_count = 0 OR (
                       last_surfaced IS NOT NULL AND
                       julianday(last_surfaced) < julianday('now', '-{int(unrecalled_days)} days')
                     ))
               {ns_clause}""",
            ns_params,
        ).fetchall()
        for r in prune_rows:
            if dry_run:
                print(f"[zmem] DRY RUN: prune [{r['id'][:8]}]: {r['content'][:60]}...")
                pruned_count += 1
                continue
            supersede_memory(conn, r["id"], "pruned: never retrieved, low confidence")
            pruned_count += 1

    # Mode-dependent verb (issue #44): a dry run must read as "would", never as
    # a completed merge. The old fixed "merged N" verb made a dry-run summary
    # indistinguishable from a real run to a skimming reader (only a trailing
    # parenthetical differed), which produced false closeout reports claiming a
    # merge that never happened. "would merge" deliberately lacks the substring
    # "merged", so a dry-run line cannot be skimmed as past-tense success. This
    # mirrors the cmd_sweep idiom (would prune/pruned) and finally makes the code
    # match the contract the docstring (above) already promised.
    merge_verb = "would merge" if dry_run else "merged"
    parts = [f"{merge_verb} {merged_count} memories"]
    if prune:
        prune_verb = "would prune" if dry_run else "pruned"
        parts.append(f"{prune_verb} {pruned_count}")
    if dry_run:
        # Retained as a second, grep-friendly signal and to reinforce the
        # zero-change outcome when merged_count == 0; the verb already conveys
        # the mode, so this is intentionally redundant (defense in depth).
        parts.append("(dry run — no changes)")
    if truncated:
        # The namespace exceeded the per-pass row cap: not every pair was
        # examined (highest-priority rows first). Surfaced honestly so an
        # operator does not mistake a bounded pass for a complete one (#36 M8).
        parts.append(
            f"truncated: examined {len(rows)} of {total_eligible} eligible "
            f"(per-pass cap {CONSOLIDATE_MAX_ROWS_PER_NAMESPACE})"
        )
    if knn_truncated:
        # vec0 KNN hit the k cap (500) with all returned rows above threshold:
        # a same-namespace duplicate could sit beyond the cap and be missed.
        # Fail-safe (the duplicate survives, no corruption) — reported so the
        # completeness gap is visible (cubic-4, #36 M8 residual).
        parts.append("knn_truncated: KNN cap reached; some pairs may be unexamined")
    contested_excluded = sum(1 for c in report["contested_clusters"] if not c["merged"])
    if contested_excluded:
        # Never silent (issue #49): a contested exclusion that only appeared in
        # the per-cluster block above could be missed when skimming the tail of
        # the output, so the summary repeats the count and the resolution path.
        # Mode-aware wording (PR feedback PRR-008 + final-critic round): the
        # dry-run forms deliberately avoid the substring "merged" — the #44
        # discipline (six existing assertNotIn("merged", summary) assertions)
        # forbids it in dry-run summaries — and under --merge-contested the
        # advice must not tell the operator to pass a flag they already passed
        # (a non-merged entry there means "override preview" in dry runs and
        # "merge failed and rolled back" in real runs, never "was skipped").
        if merge_contested and dry_run:
            parts.append(
                f"contested {contested_excluded} cluster(s) (would merge under "
                f"--merge-contested override)"
            )
        elif merge_contested:
            parts.append(
                f"contested {contested_excluded} cluster(s) (not merged — the "
                f"override merge failed and rolled back; will retry next run)"
            )
        else:
            contested_verb = "would not merge" if dry_run else "not merged"
            parts.append(
                f"contested {contested_excluded} cluster(s) ({contested_verb} — resolve via "
                f"supersede, or pass --merge-contested for a confirmed false positive)"
            )
    print(f"[zmem] {' + '.join(parts)}")

    report["merged"] = merged_count
    report["pruned"] = pruned_count
    report["truncated"] = truncated
    report["knn_truncated"] = knn_truncated
    if collect_run_ids:
        report["consolidated_ids"] = consolidated_ids
    return report
