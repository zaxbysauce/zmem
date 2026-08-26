"""Sleep-time organization job (issue #62, schema v11).

SOTA PR 7/10 — "Sleep-time organize, wired from SessionStart". ``organize()``
is the session-cadence job that replaced ``consolidate()`` at SessionStart
(7.7): every session it wakes up, checks whether the store has been idle long
enough, bounds an "episode" of work to the most recent live rows, backfills
any entity/link enrichments those rows are missing, runs the EXACT same
``consolidate`` clustering/absorb/contested machinery on that bounded set, and
then adds the sleep-time deliverables on top: a topic hierarchy, hierarchical
extractive summaries (A-MEM lite), and deterministic compression of the rows
consolidation just grew.

Everything is deterministic and LLM-free by default. The only discretionary
piece is the NLI judge (7.5), which is off unless ``ZMEM_NLI_CMD`` is set (it
lives in ``consolidate``'s contested branch, imported here by reference).

Gates and bounds (every one a documented knobs/env-var below):
- Cadence gate: the SAME meta-key gate as ``consolidate`` (7d / 20% growth,
  ``ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS`` / ``ZMEM_CONSOLIDATE_GROWTH_THRESHOLD``).
  Organize reuses consolidate's ``last_consolidation`` meta keys on purpose, so
  an organize run and a manual consolidate run count against the SAME clock —
  they are two entry points to the same maintenance act and must never both
  fire back-to-back on the same store state. ``--force`` bypasses ONLY this.
- Idle gate (7.2): ``ZMEM_ORGANIZE_IDLE_HOURS``, default 0 (disabled). When
  set, organize refuses to run unless the store has seen no live-memory
  activity (max of ingestion_ts / last_retrieved / last_surfaced) for at least
  that many hours — sleep-time jobs should not churn a store that is mid-use.
- Episode bound (7.1): ``ZMEM_ORGANIZE_EPISODE_BOUND``, default 256. The
  working set is the N most recent live rows (``ingestion_ts DESC, id DESC``),
  no namespace filtering — organize is a box-wide maintenance job like the
  background consolidate run. Bounded so consolidate's per-namespace cap and
  the O(n^2) lexical fallback stay cheap.
- Compression cap (7.4): ``ZMEM_KEEPER_COMPRESS_CHARS``, default 4000.
- Unrecalled prune (7.6): ``ZMEM_UNRECALLED_DAYS``, default 30, interpreted in
  ``consolidate``'s prune block (see there); organize only passes ``--prune``
  through.

Invariants (these are load-bearing; do not "simplify" them away):
- NO schema change: the store stays at ``SUPPORTED_SCHEMA_VERSION=11`` and the
  schema v10 installed plugin (0.10.1) keeps opening the v10-stamped operator
  store. Every organize output fits existing columns, including ``merged_from``
  (summary member ids) and ``source_ref`` ("organize:<topic>").
- Summaries are REAL rows so they are FTS/entity/recall-searchable: type=fact,
  tags exactly "summary,topic", signal=none, confidence=SUMMARY_CONFIDENCE
  (0.5 — the concrete floor fallback reads are recallable and the default
  recency floor 0.5). ``merged_from`` carries the member id list (the same
  string doubles as the topic's stable identity for the idempotent lookup).
- Idempotent re-run: the exact-match lookup
  ``superseded_at IS NULL AND type='fact' AND source_ref=? AND merged_from=?``
  (STRUCTURAL identity — the tags column is deliberately NOT part of the key:
  capture policy can append a marker to it and link attribute evolution can
  mutate it on neighbors, but ``source_ref``/``merged_from`` are stable) turns
  a repeat run into an UPDATE (Phase 4 of the issue) instead of a duplicate
  create. When the member SET legitimately changed (a merge, or compression
  replacing a member's id), the new key intentionally differs and the stale
  overlapping summary is superseded, never left as a live orphan (F-002). When
  the update/create FOLDS into a dedup target (update_memory/add_memory return
  a non-new row), organize logs and skips rather than force-rewriting a
  stranger's identity, and the summary's exact ``summary,topic`` tags are
  pinned back on rows organize owns (the 7.3 exact-tags contract).
- ``--dry-run`` writes NOTHING: no meta writes, no backfills, no summaries; the
  report carries per-step would-be counts. It models consolidate's cadence gate
  too, mirroring consolidate's own dry-run contract.
- Single-flight: the CLI holds the shared "consolidate" lock for the whole
  organize run (organize and consolidate are the SAME maintenance act, so the
  same lock). A concurrent organize/consolidate loser exits 0 with a
  lock-busy notice, never a traceback.

Module layout mirrors the sibling submodules: ``organize()`` is the sole public
entry point; the underscore helpers are deterministic, read-only where possible,
and unit-testable in isolation (the repo's test style: standalone unittest
files importing ``storelib.organize`` in-process).
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time

from storelib.consolidate import (
    CONSOLIDATE_MAX_ROWS_PER_NAMESPACE,
    _cadence_gate_skipped,
    _gather_neighbors,
    _lexical_tokens,
    _nli_first_sentence,
    consolidate,
)
# The owning submodule itself, reached via importlib: package attribute
# `storelib.consolidate` is CLOBBERED by the re-exported ``consolidate()``
# function, so `import storelib.consolidate as X` would bind the function.
# Threshold/knob parity with consolidate's cluster logic is read through THIS
# module's refreshed constants at use time — never a by-value import snapshot
# (PRR-007).
import importlib
_consolidate_mod = importlib.import_module("storelib.consolidate")
from storelib.entity import extract_entities, link_memory_entities
from storelib.links import (
    LINK_THRESHOLD,
    _link_neighbors_lexical,
    _link_neighbors_vec,
    generate_links_on_write,
)
from storelib.schema import (
    MAX_CONTENT_CHARS,
    _embeddings,
    _normalize_content,
    _parse_iso_to_epoch,
    now_iso,
)
from storelib.write import add_memory, update_memory

# Summaries are real rows. A signal=none row inherits SIGNAL_CONFIDENCE["none"]
# (0.2) which sits BELOW the CONFIDENCE_FLOOR (0.25) — such a row is invisible
# to floor-filtered recall. Summaries therefore carry an explicit confidence
# above the floor: SUMMARY_CONFIDENCE is high enough to be recallable anywhere
# and low enough to never outrank the source rows it aggregates.
SUMMARY_CONFIDENCE = 0.5
# Tags are meant to be EXACTLY this string (no spaces, comma-joined): the
# idempotent summary contract and SKILL.md both key off the literal value.
SUMMARY_TAGS = "summary,topic"
# STRUCTURAL identity (issue #62 editorial round, Claude Code F-003/F-004): a
# summary's ``source_ref`` always starts with this prefix, and that prefix —
# NOT the mutable ``tags`` column — is what excludes a row from organize's
# episode/topic scope and identifies an existing summary for the idempotent
# lookup. The tags column is user-visible and CAN be mutated by the write-time
# capture policy (``prompt-injection-risk``) or by link attribute evolution, so
# keying identity on it is brittle. ``source_ref`` is never unioned onto
# neighbors and never rewritten by capture policy, so it is a stable key.
SUMMARY_SOURCE_REF_PREFIX = "organize:"
# Extractive bullet cap for a summary body — should never bind given the source
# rows are themselves <= MAX_CONTENT_CHARS, but defensive against pathological
# first-sentence lengths. Bullets are dropped from the TAIL on a whole-sentence
# boundary (never mid-sentence), so every kept bullet is verbatim source text.
_SUMMARY_BULLET_CAP = min(MAX_CONTENT_CHARS, 4000)


def _env_int_lazy(name: str, default: int) -> int:
    """Lazily parse an integer env knob at call time (issue #62). Absent /
    garbage / non-numeric / NEGATIVE -> ``default``. Clamping negatives to the
    default (mirroring ``_env_float_lazy`` and consolidate's
    ``_prune_unrecalled_days``) matters because a negative
    ``ZMEM_ORGANIZE_EPISODE_BOUND`` would reach SQLite's ``LIMIT ?`` as an
    unbounded limit (silently defeating the episode cap) and a negative
    ``ZMEM_KEEPER_COMPRESS_CHARS`` would mangle sentence truncation — a
    misconfiguration must degrade to the safe default, never to "unbounded"
    (final-critic finding, issue #62 round 3)."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        n = int(float(raw))
        return n if n >= 0 else default
    except (TypeError, ValueError, OverflowError):
        # OverflowError: ``int(float('inf'))`` raises — a deliberately extreme
        # knob value must degrade to the default, never crash the pipeline
        # (PRR-006 / Claude Code round 4 coverage list).
        return default


def _env_float_lazy(name: str, default: float) -> float:
    """Lazily parse a float env knob at call time (issue #62). Absent /
    garbage / non-numeric -> ``default`` (and a negative value for the float
    gates is treated as absent, i.e. the gate is DISABLED — "0" means "auto",
    mirroring consolidate's cadence knobs)."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        val = float(raw)
        if val < 0:
            return 0.0
        return val
    except (TypeError, ValueError):
        return default


@contextlib.contextmanager
def _scoped_tx(conn: sqlite3.Connection):
    """Run the body in ONE transaction (BEGIN IMMEDIATE if not already in one).

    Commits only when this helper opened the transaction — a caller's existing
    transaction is left untouched (matching add/update's guarded BEGIN
    pattern). Rolls back only when this helper opened the transaction, so a
    body that re-raises never tears down an outer writer's work.
    """
    owned = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        owned = True
    try:
        yield
    except BaseException:
        if owned and conn.in_transaction:
            conn.rollback()
        raise
    else:
        if owned and conn.in_transaction:
            conn.commit()


def _last_activity_epoch(conn: sqlite3.Connection) -> float | None:
    """Epoch of the most recent live-memory activity (idle-gate input, 7.2).

    "Activity" is the max of the live rows' ingestion_ts / last_retrieved /
    last_surfaced. The maximum is computed IN PYTHON over parseable values,
    never as a SQL ``MAX()`` over the raw TEXT columns: SQLite's ``MAX`` is
    byte-order, so a malformed timestamp like ``'zzz'`` would sort ABOVE every
    valid ISO stamp and shadow real activity. Instead each non-NULL value is
    parsed with ``schema._parse_iso_to_epoch`` (0.0 on failure) and the max
    PARSEABLE epoch wins. NULLs are skipped; a store with no live rows at all
    returns None -> the caller treats that as "idle" (proceed). If NO live row
    carries a parseable timestamp the function returns None (treated as
    "unknown activity", i.e. idle) rather than silently reporting epoch 0 — a
    corrupted-timestamp store can never fabricate a huge idle delta (final-
    critic finding, issue #62 round 3).
    """
    best = 0.0
    for row in conn.execute(
        "SELECT ingestion_ts AS a, last_retrieved AS b, last_surfaced AS c "
        "FROM memory WHERE superseded_at IS NULL"
    ).fetchall():
        for v in (row["a"], row["b"], row["c"]):
            if not v:
                continue
            e = _parse_iso_to_epoch(v)
            if e > best:
                best = e
    return best if best > 0 else None


def _split_sentences(text: str | None) -> list[str]:
    """Order-preserving sentence split on punctuation+whitespace boundaries
    (the same regex family as consolidate's ``_nli_first_sentence``). Blank
    parts are dropped. Deterministic."""
    collapsed = re.sub(r"\s+", " ", (text or ""))
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", collapsed) if p.strip()]


def _compress_unique_sentences(content: str | None, cap: int) -> str:
    """Deterministic extractive compression (7.4): the order-preserving unique
    sentences of ``content``, capped at ``cap`` characters.

    Never drops tokens that appear in only one absorbed source without leaving
    them somewhere: the pre-compress content stays on the SUPERSEDED row
    (append-only history), and this function only ever truncates the LIVING
    keeper copy. Truncation happens ONLY by dropping whole sentences off the
    tail (never mid-sentence): if even the first unique sentence exceeds the
    cap, a word-boundary prefix is kept so the keeper is never emptied.
    """
    sentences = _split_sentences(content)
    seen: set[str] = set()
    unique: list[str] = []
    for s in sentences:
        norm = _normalize_content(s)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        unique.append(s.strip())
    out: list[str] = []
    total = 0
    for s in unique:
        piece = s if not out else " " + s
        if total + len(piece) > cap:
            break  # drop THIS and all later sentences — tail-first, whole-sentence
        out.append(piece)
        total += len(piece)
    if not out and unique:
        first = unique[0]
        if len(first) > cap:
            cut = first.rfind(" ", 0, cap)
            first = first[: cut if cut > 0 else cap]
        out = [first]
        total = len(first)
    return "".join(out)[:cap]


def _build_bullets(members: list[str], rows_by_id: dict) -> str:
    """Extractive topic bullets (7.3): the FIRST WHOLE SENTENCE of each member
    row, deduplicated (first occurrence wins), formatted as one ``- bullet``
    per member. Deterministic: members are visited in sorted(id) order. Empty
    result means the caller creates no summary (nothing to summarize)."""
    bullets: list[str] = []
    seen: set[str] = set()
    for mid in sorted(members):
        row = rows_by_id.get(mid)
        if row is None:
            continue
        # PRR-010: bullets are for human reading and are capped by
        # _SUMMARY_BULLET_CAP at the whole-bullet level — never truncate the
        # first sentence mid-sentence (the shared helper's 2000-char NLI cap is
        # for the judge, not for extractive output).
        first = _nli_first_sentence(row["content"], max_len=None)
        norm = _normalize_content(first)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        bullets.append(f"- {first}\n")
    out: list[str] = []
    total = 0
    for b in bullets:
        if total + len(b) > _SUMMARY_BULLET_CAP:
            break
        out.append(b)
        total += len(b)
    return "".join(out)


def _entity_groups(conn: sqlite3.Connection, ids: list[str]) -> list[list[str]]:
    """Group leftover singleton rows by SHARED entity (7.3, A-MEM lite).

    Rows that share at least one ``memory_entity`` link are unified into a
    topic even though no neighbor similarity edge connected them. Namespace is
    intentionally NOT a boundary here (an entity may legitimately span
    projects); the topic's ``namespace`` field is the most-recent member's.
    Returns only groups of size >= 2, sorted biggest-first; rows with no
    shared entity are not returned (they become their own single-row topics).
    Deterministic: connected components of the "shares an entity" graph."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    links = conn.execute(
        f"SELECT memory_id, entity_id FROM memory_entity "
        f"WHERE memory_id IN ({ph})",
        ids,
    ).fetchall()
    by_entity: dict[str, list[str]] = {}
    for lr in links:
        by_entity.setdefault(lr["entity_id"], []).append(lr["memory_id"])

    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    for entity_id, members in by_entity.items():
        if len(members) >= 2:
            base = members[0]
            for other in members[1:]:
                union(base, other)

    comps: dict[str, list[str]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    groups = [sorted(m) for m in comps.values() if len(m) >= 2]
    groups.sort(key=lambda m: (-len(m), m))
    return groups


def _supersede_stale_summaries(
    conn: sqlite3.Connection, topic_key: str, keep_id: str,
) -> None:
    """Append-only re-key guard (Claude Code F-002 residual).

    A NEW summary key legitimately differs from an older live summary of the
    same logical topic whenever the member SET changed — a member was merged,
    or compression (append-only ``update_memory``) replaced a member's id with
    a fresh row. In that situation the exact-match update path cannot fire
    (different key), so without this guard the old aggregate would linger as a
    live, recallable, FTS-indexed orphan forever and the issue's Phase 4
    "update, never duplicate" contract would be violated.

    This tombstones every live ``organize:``-prefixed summary that shares at
    least one member id with ``topic_key`` and is not the just-written
    summary (``keep_id``). Topics partition the episode (each row is a member of
    exactly one), so two live summaries never legitimately share a member;
    when they do, the older lineage is stale by construction. Runs inside the
    caller's open transaction. Deterministic; a no-op when nothing is stale.
    """
    members = set(topic_key.split(",")) if topic_key else set()
    if not members:
        return
    rows = conn.execute(
        "SELECT id, merged_from FROM memory "
        "WHERE superseded_at IS NULL AND type='fact' "
        "AND source_ref LIKE ?",
        (SUMMARY_SOURCE_REF_PREFIX + "%",),
    ).fetchall()
    tomb_at = now_iso()
    for row in rows:
        if row["id"] == keep_id:
            continue
        # Strip the ``:truncated`` size-cap marker exactly like
        # consolidate._dedupe_merged_from does, so an entry that carries it
        # still matches its base member id (organize's own summaries never
        # write the marker, but the guard must not depend on that).
        old_members = {
            entry[:-len(":truncated")] if entry.endswith(":truncated") else entry
            for entry in (row["merged_from"] or "").split(",") if entry
        }
        if old_members & members:
            conn.execute(
                "UPDATE memory SET superseded_at=?, valid_until=?, "
                "supersede_reason='reorganized into a changed topic' "
                "WHERE id=?",
                (tomb_at, tomb_at, row["id"]),
            )


def _cluster_topics(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    use_lexical: bool,
) -> list[dict]:
    """Phase-7 topic hierarchy over the POST-ABSORB live working rows (7.2-7.3).

    A related-graph is built with the SINGLE shared neighbor predicate
    (``consolidate._gather_neighbors``) at the SAME effective threshold
    consolidate would use by default — the connected components of that graph
    are the topics. Leftover singletons are then grouped by SHARED ENTITY
    (7.3, A-MEM lite); whatever still stands alone becomes a degenerate
    single-row topic. Every live NON-summary working row lands in EXACTLY one
    topic, so the members-union over all topics equals the live working set
    minus organize's own summary rows (a test-pinned invariant).

    Summary rows (``source_ref`` with the ``organize:`` prefix) are
    DELIBERATELY excluded from membership: a summary carries entity links for
    the very members it summarizes, so letting it join a topic would re-key
    that topic's member set on every run and force a duplicate summary on the
    next run — the self-amplification trap the issue's Phase 4 update path is
    designed to prevent. Summaries are the pipeline's OUTPUT, never an input
    to it. Exclusion is keyed on the STRUCTURAL ``source_ref`` prefix, never
    the mutable ``tags`` column (Claude Code F-003/F-004): the capture policy
    and the link attribute-evolution can append to a row's tags, but
    ``source_ref`` is stable, so user rows remain in scope regardless of what
    tags they accumulate.

    Deterministic: rows are seeded in sorted(id) order and the union-find
    partition depends only on the edge set, so two runs on the same store
    produce identical topics. Read-only.
    """
    ordered = sorted(
        (r for r in rows
         if not (r["source_ref"] or "").startswith(SUMMARY_SOURCE_REF_PREFIX)),
        key=lambda r: r["id"],
    )
    tokens: dict[str, set[str]] = {}
    if use_lexical:
        tokens = {
            r["id"]: _lexical_tokens(f"{r['content'] or ''} {r['tags'] or ''}")
            for r in ordered
        }
    effective_threshold = (
        _consolidate_mod.CONSOLIDATE_LEXICAL_THRESHOLD
        if use_lexical
        else _consolidate_mod.CONSOLIDATE_DEFAULT_THRESHOLD
    )

    parent = {r["id"]: r["id"] for r in ordered}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    work_ids = {r["id"] for r in ordered}
    for seed in ordered:
        neighbors, _knn = _gather_neighbors(
            conn, seed, ordered,
            use_lexical=use_lexical,
            lexical_tokens=tokens,
            effective_threshold=effective_threshold,
            absorbed=set(),
            # F-001 (Claude Code): the related-graph is a native episode — the
            # global vec0 KNN must never pull an out-of-episode id in.
            restrict_ids=work_ids,
        )
        for nb, _sim in neighbors:
            if nb["id"] not in parent:
                # Defensive backstop (Claude Code F-001): a neighbor id with no
                # union-find entry would KeyError in find() below. Restrict_ids
                # makes this unreachable today — it is a guard, not a path.
                continue
            union(seed["id"], nb["id"])

    comps: dict[str, list[str]] = {}
    for r in ordered:
        comps.setdefault(find(r["id"]), []).append(r["id"])
    groups = sorted((sorted(m) for m in comps.values()), key=lambda m: (-len(m), m))

    topics: list[list[str]] = [g for g in groups if len(g) >= 2]
    singletons = [m for g in groups if len(g) == 1 for m in g]
    topics.extend(_entity_groups(conn, singletons))

    # Degenerate single-row topics for whatever the entity grouping left alone.
    grouped_now = set(m for g in topics for m in g)
    for s in singletons:
        if s not in grouped_now:
            topics.append([s])
    topics.sort(key=lambda m: (-len(m), m))
    return [topic_to_report(conn, rows_by_id(rows), members) for members in topics]


def rows_by_id(rows: list[sqlite3.Row]) -> dict:
    """Index rows by id (helper for topic reporting; kept module-level so the
    pure parts of the pipeline stay unit-testable without a DB)."""
    return {r["id"]: r for r in rows}


def topic_to_report(
    conn: sqlite3.Connection, by_id: dict, members: list[str]
) -> dict:
    """Render one topic group as a report entry: sorted member ids, the topic's
    shared entity ids (union of every member's links, sorted), and the primary
    namespace (the most-recently-ingested member's — the namespace a summary
    row for this topic would be written under). Deterministic."""
    ns = ""
    best_ts = 0.0
    for mid in members:
        row = by_id.get(mid)
        if row is None:
            continue
        e = _parse_iso_to_epoch(row["ingestion_ts"])
        if e >= best_ts:
            best_ts = e
            ns = row["namespace"]
    entity_ids: set[str] = set()
    if members:
        ph = ",".join("?" * len(members))
        for er in conn.execute(
            f"SELECT entity_id FROM memory_entity WHERE memory_id IN ({ph})",
            members,
        ).fetchall():
            entity_ids.add(er["entity_id"])
    return {
        "members": members,
        "entity_ids": sorted(entity_ids),
        "namespace": ns,
    }


def _count_would_link(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """How many links a REAL ``generate_links_on_write`` would add for ``row``?
    Mirrors that function's branch EXACTLY (emb is not None -> vec, else
    lexical) so a dry-run count equals what a real run would add. Read-only."""
    if row["embedding"] is not None:
        return len(
            _link_neighbors_vec(
                conn, row["id"], row["embedding"], row["namespace"], LINK_THRESHOLD
            )
        )
    return len(
        _link_neighbors_lexical(
            conn, row["id"], row["content"], row["tags"],
            row["namespace"], LINK_THRESHOLD,
        )
    )


def _has_entity_links(conn: sqlite3.Connection, mid: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM memory_entity WHERE memory_id=? LIMIT 1", (mid,)
        ).fetchone()
        is not None
    )


def _has_link_edges(conn: sqlite3.Connection, mid: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM memory_link WHERE src_id=? OR dst_id=? LIMIT 1",
            (mid, mid),
        ).fetchone()
        is not None
    )


def _copy_merged_from(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Propagate the absorbed-members provenance from a row to its replacement
    (compression path): the INSERT in update_memory never writes merged_from,
    and compression must not lose the growth consolidate recorded there."""
    old = conn.execute(
        "SELECT merged_from FROM memory WHERE id=?", (old_id,)
    ).fetchone()
    merged = old["merged_from"] if old and old["merged_from"] else ""
    if merged:
        conn.execute("UPDATE memory SET merged_from=? WHERE id=?", (merged, new_id))


def organize(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    force: bool = False,
    prune: bool = False,
) -> dict:
    """Run the sleep-time organization pipeline (issue #62, 7.1-7.6).

    Session-start cadence job. Returns a machine-readable report dict with
    per-step counts; every exit path — including a gate skip and an empty
    episode — returns a structured report (the CLI prints it under ``--json``).
    Under ``--dry-run`` nothing is written and the counts are would-be (the
    caller must qualify with ``dry_run``, exactly like ``consolidate``). Note
    the ``would_*`` backfill/summary/compression counts are EXTRACTOR/PARITY-
    PREDICTED high-water marks, not exact duplicates of the real counts: a
    real run only increments ``backfilled`` when a link was actually written
    (``INSERT OR IGNORE`` no-ops are not counted), so ``would_post`` may
    exceed ``backfilled`` on a store where enrichment already partially
    exists. Treat ``would_*`` as "what a full run would attempt", never as a
    promise of exactly-N inserts.

    Pipeline order (1-8):
      1. Cadence gate — the SHARED consolidate meta-key gate (7-day / 20%
         growth), modeled under dry-run, bypassed only by ``force``. Implemented
         ONCE in ``consolidate._cadence_gate_skipped`` and called by both entry
         points (two entry points to one maintenance act must never drift; the
         consolidate CLI arming the clock gates this run too).
      2. Idle gate — ``ZMEM_ORGANIZE_IDLE_HOURS`` (default 0 = off); refuses
         to run on a store whose last live-memory activity is too recent.
      3. Working set — the N most recent LIVE NON-SUMMARY rows (summaries are
         the pipeline's OUTPUT — excluded STRUCTURALLY via the ``organize:``
         source_ref prefix, never via the mutable tags column), N from
         ``ZMEM_ORGANIZE_EPISODE_BOUND`` (default 256, clamped to the
         per-namespace consolidation cap).
      4. Entity backfill (7.2) — every working row missing ``memory_entity``
         links gets them via the deterministic extractor.
      5. Link backfill (7.2) — every working row missing ``memory_link`` edges
         gets them via ``generate_links_on_write``.
      6. Consolidation (7.1) — ``consolidate(force=True, ...)`` on EXACTLY the
         working set (shared code path: keeper selection, absorb, contested +
         optional NLI judge, optional prune pass-through). Caller already holds
         the "consolidate" lock. The vec0 neighbor lookup is restricted to the
         episode (no out-of-episode leaks).
      7. Compression (7.4) — the keepers consolidation actually GREW are
         deterministically compressed past ``ZMEM_KEEPER_COMPRESS_CHARS``. Runs
         BEFORE topic identity is keyed (F-002): append-only update_memory
         replaces the keeper id, so keying topics first would leave every
         future run with a different key and a duplicate summary.
      8. Topics + summaries (7.2-7.3) — related-graph via the shared neighbor
         predicate + shared-entity grouping; hierarchical summaries (≥3-member
         topics) created/updated idempotently (Phase 4 update) with COMPRESSION
         already applied, then prune (7.6) via consolidate's ``prune`` flag
         only (never automatic).
    """
    use_lexical = not (_embeddings and _embeddings.is_available())
    report: dict = {
        "dry_run": dry_run,
        "mode": "lexical" if use_lexical else "cosine",
        "skipped_by_cadence_gate": False,
        "idle_skipped": False,
        "bound": 0,
        "episode_ids": [],
        "entity_backfill": {"candidates": 0, "backfilled": 0, "would_backfill": 0},
        "link_backfill": {"candidates": 0, "backfilled": 0, "would_backfill": 0},
        "consolidate": None,
        "topics": [],
        "summaries": {
            "created": 0, "updated": 0,
            "would_create": 0, "would_update": 0, "skipped": 0,
        },
        "compressed": {"count": 0, "would_count": 0, "skipped": 0},
        "pruned": 0,
    }

    # --- 1) Cadence gate (SHARED with consolidate — see module docstring) ---
    # Same maintenance act, one clock, ONE implementation (_cadence_gate_skipped
    # lives in consolidate.py and both entry points call it): an organize run
    # and a manual consolidate run can never drift apart on the gate, and a
    # manual consolidate that armed the clock gates this run exactly as the
    # shared meta-key contract intends (Claude Code round 4 coverage list).
    if _cadence_gate_skipped(conn, force=force, dry_run=dry_run, label="organize"):
        report["skipped_by_cadence_gate"] = True
        return report

    # --- 2) Idle gate (7.2, optional) ---
    idle_hours = _env_float_lazy("ZMEM_ORGANIZE_IDLE_HOURS", 0.0)
    if idle_hours > 0:
        idle_epoch = _last_activity_epoch(conn)
        if idle_epoch is not None:
            idle_delta_h = (time.time() - idle_epoch) / 3600.0
            if idle_delta_h < idle_hours:
                if dry_run:
                    print(f"[zmem] organize: dry-run: would skip by idle gate "
                          f"(last activity {idle_delta_h:.1f}h ago < idle "
                          f"{idle_hours:g}h; ZMEM_ORGANIZE_IDLE_HOURS)")
                else:
                    print(f"[zmem] organize: skipped by idle gate "
                          f"(last activity {idle_delta_h:.1f}h ago < idle "
                          f"{idle_hours:g}h; ZMEM_ORGANIZE_IDLE_HOURS)")
                report["idle_skipped"] = True
                return report

    # --- 3) Working set (7.1): the N most recent live rows, box-wide ---
    bound = _env_int_lazy("ZMEM_ORGANIZE_EPISODE_BOUND", 256)
    if bound > 0:
        # Upper clamp (Claude Code round 4 coverage list): an absurd episode
        # bound would hit SQLite's parameter limit and the O(n^2) lexical
        # clustering; capping at the per-namespace consolidation cap keeps the
        # job provably cheap while 0 stays a supported "off".
        bound = min(bound, CONSOLIDATE_MAX_ROWS_PER_NAMESPACE)
    report["bound"] = bound
    working = conn.execute(
        "SELECT id, namespace, type, content, tags, source_ref, confidence, "
        "signal, retrieval_count, surfaced_count, last_surfaced, embedding, "
        "embedding_model, ingestion_ts, taint, merged_from, superseded_at "
        "FROM memory WHERE superseded_at IS NULL "
        # F-003/PRR-008: the episode deliberately EXCLUDES organize's own
        # summary rows (structural source_ref prefix, never the mutable tags
        # column) — summaries are the pipeline's OUTPUT, and letting them into
        # the working set would let consolidate ingest/absorb the very rows it
        # produced (summary-as-keeper, summary-tombstoned-into-summary).
        "AND (source_ref IS NULL OR source_ref NOT LIKE ?) "
        "ORDER BY ingestion_ts DESC, id DESC LIMIT ?",
        (SUMMARY_SOURCE_REF_PREFIX + "%", bound),
    ).fetchall()
    if not working:
        print(f"[zmem] organize: no live memories to organize "
              f"(episode bound {bound}{' = 0 (ZMEM_ORGANIZE_EPISODE_BOUND)' if bound < 1 else ''})")
        return report
    report["episode_ids"] = sorted(r["id"] for r in working)
    work_ids = set(r["id"] for r in working)

    # --- 4) Entity backfill (7.2) ---
    eb = report["entity_backfill"]
    for r in sorted(working, key=lambda row: row["id"]):
        if _has_entity_links(conn, r["id"]):
            continue
        eb["candidates"] += 1
        if dry_run:
            if extract_entities(r["content"] or "", r["tags"] or "", r["namespace"]):
                eb["would_backfill"] += 1
            continue
        with _scoped_tx(conn):
            linked_now = link_memory_entities(
                conn, r["id"],
                content=r["content"], tags=r["tags"], namespace=r["namespace"],
            )
            if linked_now:
                eb["backfilled"] += 1

    # --- 5) Link backfill (7.2) ---
    lb = report["link_backfill"]
    for r in sorted(working, key=lambda row: row["id"]):
        if _has_link_edges(conn, r["id"]):
            continue
        lb["candidates"] += 1
        if dry_run:
            if _count_would_link(conn, r) > 0:
                lb["would_backfill"] += 1
            continue
        with _scoped_tx(conn):
            link_rep = generate_links_on_write(
                conn, r["id"], content=r["content"], namespace=r["namespace"],
                tags=r["tags"], emb=r["embedding"],
            )
            if link_rep["neighbors"] > 0:
                lb["backfilled"] += 1

    # --- 6) Consolidation on the bounded episode (7.1) ---
    # force=True: the cadence gate above already passed (organize reached here
    # deliberately); consolidate's gate must not double-refuse. consolidate
    # still honors dry_run (writes nothing, returns would-be counts).
    c_report = consolidate(
        conn, force=True, prune=prune, dry_run=dry_run,
        working_ids=work_ids, collect_run_ids=True,
    )
    report["consolidate"] = c_report
    report["mode"] = c_report.get("mode", report["mode"])
    report["pruned"] = c_report.get("pruned", 0)

    # --- 7) Compression of keepers consolidation grew (7.4) ---
    # Runs BEFORE topic identity is recorded (issue #62 editorial round, Claude
    # Code F-002): compression is append-only — update_memory REPLACES the
    # keeper's id — so if topics/summaries were keyed first, the summary would
    # be keyed on pre-compression ids and the very next run (post-compression)
    # would compute a different key, miss the exact-match lookup, and create a
    # duplicate while orphaning the old summary. Compressing first means topic
    # identity is always keyed on the FINAL live row set, so an untouched
    # repeat run re-keys identically and the Phase 4 update path is hit.
    compress_chars = _env_int_lazy("ZMEM_KEEPER_COMPRESS_CHARS", 4000)
    if compress_chars < 1:
        # F-006/PRR-009: PER-KNOB positive floor. ZMEM_KEEPER_COMPRESS_CHARS=0
        # would make _compress_unique_sentences return "" and empty a live
        # keeper; 0 is NOT a supported "off" for this knob (unlike
        # EPISODE_BOUND), so it degrades to the documented default.
        compress_chars = 4000
    comp = report["compressed"]
    consolidated_ids = sorted(set(c_report.get("consolidated_ids") or []))
    for mid in consolidated_ids:
        keeper = conn.execute(
            "SELECT content, tags, source_ref, signal, confidence FROM memory "
            "WHERE id=? AND superseded_at IS NULL",
            (mid,),
        ).fetchone()
        if keeper is None:
            continue
        if len(keeper["content"] or "") <= compress_chars:
            continue
        compressed = _compress_unique_sentences(keeper["content"], compress_chars)
        if dry_run:
            comp["would_count"] += 1
            continue
        with _scoped_tx(conn):
            result_id, created_new = update_memory(
                conn, mid=mid, content=compressed,
                tags=keeper["tags"], source_ref=keeper["source_ref"],
                signal=keeper["signal"], confidence=keeper["confidence"],
            )
            if created_new:
                _copy_merged_from(conn, mid, result_id)
                comp["count"] += 1
                print(f"[zmem] organize: compressed keeper {result_id[:8]} "
                      f"({len(keeper['content'] or '')} -> {len(compressed)} chars)")
            else:
                comp["skipped"] += 1
                print(f"[zmem] organize: compression of {mid[:8]} folded into "
                      f"{result_id[:8]} (dedup); skipping this run")

    # --- 8) Topics + summaries over the POST-COMPRESSION live working rows ---
    # live_work re-derives the episode INCLUDING any compression-replacement
    # row (update_memory is append-only: the new keeper's ``update_of`` points
    # at a member of work_ids). Excluding those would silently drop a member
    # from the topic and re-key the summary for nothing.
    ph = ",".join("?" * len(work_ids))
    work_sorted = sorted(work_ids)
    live_work = conn.execute(
        f"SELECT id, namespace, type, content, tags, source_ref, confidence, "
        f"signal, retrieval_count, surfaced_count, last_surfaced, embedding, "
        f"embedding_model, ingestion_ts, taint, merged_from, superseded_at "
        f"FROM memory WHERE superseded_at IS NULL "
        f"AND (id IN ({ph}) OR update_of IN ({ph}))",
        [*work_sorted, *work_sorted],
    ).fetchall()
    by_id = {r["id"]: r for r in live_work}
    topics = _cluster_topics(conn, live_work, use_lexical=use_lexical)
    report["topics"] = topics

    sm = report["summaries"]
    for topic in topics:
        if len(topic["members"]) < 3:
            continue
        topic_key = ",".join(topic["members"])
        src_ref = f"organize:{topic_key}"
        bullets = _build_bullets(topic["members"], by_id)
        if not bullets:
            continue  # nothing verbatim to summarize — no empty summary rows
        # STRUCTURAL lookup (Claude Code F-004 / own PRR-002): identity is
        # source_ref + merged_from — NEVER the tags column, which the write-time
        # capture policy and (on any write path) attribute evolution can
        # mutate. A summary whose tags were polluted is still the same summary;
        # a user row that absorbed the summary,topic marker is NOT one.
        existing = conn.execute(
            "SELECT id FROM memory WHERE superseded_at IS NULL AND type='fact' "
            "AND source_ref=? AND merged_from=?",
            (src_ref, topic_key),
        ).fetchone()
        ns = topic["namespace"]
        if dry_run:
            if existing:
                sm["would_update"] += 1
            else:
                sm["would_create"] += 1
            continue
        if existing:
            with _scoped_tx(conn):
                result_id, created_new = update_memory(
                    conn, mid=existing["id"], content=bullets, type_="fact",
                    tags=SUMMARY_TAGS, source_ref=src_ref, signal="none",
                    confidence=SUMMARY_CONFIDENCE,
                    # F-004: a summary's tags are a structural marker, not
                    # content tags — never evolve them onto user neighbors.
                    link_attr_propagate=False,
                )
                if created_new:
                    # Pin the 7.3 EXACT-tags contract back (capture policy may
                    # have appended a marker), restore provenance, and retire
                    # any stale overlapping lineage (F-002 residual guard).
                    conn.execute(
                        "UPDATE memory SET tags=?, merged_from=? WHERE id=?",
                        (SUMMARY_TAGS, topic_key, result_id),
                    )
                    _supersede_stale_summaries(conn, topic_key, result_id)
                    sm["updated"] += 1
                    print(f"[zmem] organize: summary {result_id[:8]} updated for "
                          f"topic [{topic['members'][0][:8]} (n={len(topic['members'])})]")
                else:
                    sm["skipped"] += 1
                    print(f"[zmem] organize: summary update folded into "
                          f"{result_id[:8]} (dedup); skipping this run")
        else:
            with _scoped_tx(conn):
                new_id = add_memory(
                    conn, namespace=ns, type_="fact", content=bullets,
                    tags=SUMMARY_TAGS, source_ref=src_ref, signal="none",
                    confidence=SUMMARY_CONFIDENCE,
                    link_attr_propagate=False,
                )
                row = conn.execute(
                    "SELECT content FROM memory WHERE id=?", (new_id,)
                ).fetchone()
                if row and row["content"] == bullets:
                    conn.execute(
                        "UPDATE memory SET tags=?, merged_from=? WHERE id=?",
                        (SUMMARY_TAGS, topic_key, new_id),
                    )
                    _supersede_stale_summaries(conn, topic_key, new_id)
                    sm["created"] += 1
                    print(f"[zmem] organize: summary {new_id[:8]} created for "
                          f"topic [{topic['members'][0][:8]} (n={len(topic['members'])})] "
                          f"in ns={ns or 'user:global'}")
                else:
                    sm["skipped"] += 1
                    print(f"[zmem] organize: summary create folded into "
                          f"{new_id[:8]} (dedup); skipping this run")

    # --- 9) Human-readable summary (all JSON goes via the CLI's --json path) ---
    if dry_run:
        parts = [
            f"DRY RUN: entity backfill would-link {eb['would_backfill']} "
            f"of {eb['candidates']} candidates",
            f"link backfill would-link {lb['would_backfill']} of {lb['candidates']} candidates",
            f"topics {len(topics)}",
            f"summaries would-create {sm['would_create']} / would-update {sm['would_update']}",
            f"compressed would-compress {comp['would_count']}",
            f"pruned would-prune {report['pruned']}",
            "(no changes)",
        ]
    else:
        parts = [
            f"entity backfill {eb['backfilled']} of {eb['candidates']} candidates",
            f"link backfill {lb['backfilled']} of {lb['candidates']} candidates",
            f"topics {len(topics)}",
            f"summaries {sm['created']} created / {sm['updated']} updated",
            f"compressed {comp['count']}",
            f"pruned {report['pruned']}",
        ]
    print(f"[zmem] organize: " + ", ".join(parts))
    return report
