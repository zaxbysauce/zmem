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
from storelib.schema import CONFIDENCE_FLOOR, GLOBAL_NAMESPACE, STORE_PATH, _commit, _embeddings, _format_recency, _parse_iso_to_epoch, now_iso
from storelib.write import _has_injection_risk_tag, _source_hash

W_BM25 = 0.55

W_CONFIDENCE = 0.20

W_RECENCY = 0.15

W_POPULARITY = 0.10
# Recency half-life: a memory from RECENCY_HALF_LIFE_DAYS ago contributes half.

RECENCY_HALF_LIFE_DAYS = 90



def _uses_count(row: sqlite3.Row | dict) -> int:
    """Total times a memory was surfaced into context = retrieval_count + surfaced_count.

    Issue #21: the two counters are mutually exclusive per recall event (explicit recall
    bumps retrieval_count, passive `--no-bump` recall bumps surfaced_count), so their sum
    is a non-double-counted usefulness metric. Used everywhere retrieval_count was
    previously the SOLE usefulness signal.

    Compute score accepts sqlite3.Row OR dict; both raise (KeyError / IndexError / absent
    column) on a missing/mismatched key, so default to 0 via try/except — not dict-style
    `.get`, which sqlite3.Row does not have.
    """
    total = 0
    try:
        total += int(row["retrieval_count"] or 0)
    except (KeyError, IndexError, TypeError):
        pass
    try:
        total += int(row["surfaced_count"] or 0)
    except (KeyError, IndexError, TypeError):
        pass
    return total

def compute_score(row: sqlite3.Row | dict, fts_rank: float | None, now_epoch: float,
                  vec_sim: float | None = None) -> float:
    """Composite score: BM25 relevance + confidence boost + recency + popularity.

    fts_rank is the raw FTS5 rank value (lower = better match). For memories
    that came from the vector path only (no FTS match), fts_rank is None — in
    that case vec_sim (cosine similarity, 0..1) is used as the relevance proxy.
    All other factors come from the memory row itself.
    """
    # Relevance component: BM25 if available, else vector similarity as proxy.
    if fts_rank is not None:
        ar = abs(fts_rank)
        relevance = ar / (1.0 + ar)
    elif vec_sim is not None:
        relevance = max(0.0, vec_sim)  # cosine sim already 0..1 for normalized vecs
    else:
        relevance = 0.0

    # Confidence component: already 0..1.
    confidence = float(row["confidence"]) if row["confidence"] is not None else 0.3

    # Recency component: exponential decay from ingestion_ts.
    ingested = _parse_iso_to_epoch(row["ingestion_ts"] or "")
    if ingested > 0 and now_epoch > 0:
        age_days = max(0.0, (now_epoch - ingested) / 86400.0)
        recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    else:
        recency = 0.5  # unknown age — neutral

    # Popularity component: total surface events (retrieval_count + surfaced_count)
    # with diminishing returns — blends the passive signals that retrieval_count alone
    # missed (issue #21).
    rc = _uses_count(row)
    popularity = min(1.0, 0.15 * (rc ** 0.5))

    return (
        W_BM25 * relevance
        + W_CONFIDENCE * confidence
        + W_RECENCY * recency
        + W_POPULARITY * popularity
    )

def _vector_knn(conn: sqlite3.Connection, embedding: bytes, k: int) -> list[str]:
    """Query the vec0 table for k nearest neighbors. Returns memory_id list."""
    try:
        results = conn.execute(
            "SELECT memory_id, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [embedding, k],
        ).fetchall()
        return [r["memory_id"] for r in results]
    except sqlite3.OperationalError:
        return []

def _rrf_fuse(bm25_ids: list[str], vec_ids: list[str], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion: combine ranked lists by 1/(k+rank).

    Returns a fused list of memory_ids ordered by combined RRF score.
    k=60 is the industry-standard smoothing constant (Elasticsearch, Azure).
    """
    scores: dict[str, float] = {}
    for rank, mid in enumerate(bm25_ids, 1):
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    for rank, mid in enumerate(vec_ids, 1):
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=scores.get, reverse=True)

def _ns_migration_map(conn: sqlite3.Connection) -> dict:
    """The {old_namespace: new_namespace} map recorded by the v5 migration
    (meta key 'ns_migration_v5'), or {} if absent/unparseable."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key='ns_migration_v5'"
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}

def _expand_namespace_aliases(conn: sqlite3.Connection, namespace: str | None) -> list[str] | None:
    """Given a namespace as passed by a caller (pre- or post-v5-migration),
    return the list of namespace values to match: the namespace itself, plus
    its pre-migration alias if one exists in either direction (old->new or
    new->old), per the recall compat-alias (PLAN.md §8) kept for one release
    so nothing strands mid-cutover. A row has exactly one namespace, so this
    never double-counts — it only widens the WHERE IN (...) match set."""
    if not namespace:
        return None
    aliases = {namespace}
    migration_map = _ns_migration_map(conn)
    if namespace in migration_map:
        aliases.add(migration_map[namespace])
    else:
        for old_ns, new_ns in migration_map.items():
            if new_ns == namespace:
                aliases.add(old_ns)
    return list(aliases)

def _fetch_by_ids(
    conn: sqlite3.Connection, ids: list[str], namespaces: list[str] | None, floor: float
) -> list:
    """Fetch full memory rows for a list of IDs, applying the same filters as recall."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    ns_clause = ""
    params = list(ids)
    if namespaces:
        ns_placeholders = ",".join("?" * len(namespaces))
        ns_clause = f"AND namespace IN ({ns_placeholders})"
        params.extend(namespaces)
    params.append(floor)
    sql = f"""
        SELECT id, namespace, type, content, tags, source_ref,
               source_hash, confidence, signal, valid_from,
               ingestion_ts, retrieval_count, surfaced_count, last_retrieved,
               NULL AS fts_rank
        FROM memory
        WHERE id IN ({placeholders})
          {ns_clause}
          AND superseded_at IS NULL
          AND confidence >= ?
    """
    rows = conn.execute(sql, params).fetchall()
    # Preserve the fused order (IN clause does not guarantee order).
    row_map = {r["id"]: r for r in rows}
    return [row_map[mid] for mid in ids if mid in row_map]

def _recall_one_tier(
    conn: sqlite3.Connection,
    *,
    query: str,
    ns_list: list[str] | None,
    limit: int,
    min_confidence: float | None,
    hybrid: bool,
    now_epoch: float,
) -> list[tuple[float, dict]]:
    """FTS5 + composite scoring for ONE namespace set (a single recall tier).

    Returns the scored ``(score, result_dict)`` list (highest score first), up
    to ``limit`` rows. No bump, no print — the caller merges tiers, bumps the
    final set once, and prints.

    ``ns_list`` is the already-expanded namespace match set for this tier (the
    output of ``_expand_namespace_aliases``). ``None`` ⇒ no namespace filter
    (search everything — the unscoped path). The same set is used for both the
    FTS filter and the hybrid RRF ``_fetch_by_ids`` re-fetch namespace filter.
    """
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    floor = min_confidence if min_confidence is not None else CONFIDENCE_FLOOR
    if not terms:
        rows = []
    else:
        safe_terms = []
        for t in terms:
            t_escaped = t.replace('"', '""')
            safe_terms.append(f'"{t_escaped}"*')
        fts_query = " OR ".join(safe_terms)
        params: list = [fts_query]
        ns_clause = ""
        if ns_list:
            ns_placeholders = ",".join("?" * len(ns_list))
            ns_clause = f"AND m.namespace IN ({ns_placeholders})"
            params.extend(ns_list)
        params.append(floor)
        # Fetch more candidates than the limit so the composite re-ranking has
        # a larger pool to choose from (BM25 rank != final rank).
        fetch_limit = max(limit * 3, limit + 5)
        params.append(fetch_limit)
        sql = f"""
            SELECT m.id, m.namespace, m.type, m.content, m.tags, m.source_ref,
                   m.source_hash, m.confidence, m.signal, m.valid_from,
                   m.ingestion_ts, m.retrieval_count, m.surfaced_count, m.last_retrieved,
                   rank AS fts_rank
            FROM memory_fts f
            JOIN memory m ON m.rowid = f.rowid
            WHERE memory_fts MATCH ?
              {ns_clause}
              AND m.superseded_at IS NULL
              AND m.confidence >= ?
            ORDER BY rank
            LIMIT ?
        """
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []

    # --- Hybrid RRF fusion: if enabled and embeddings available, also query
    # the vector store and fuse ranks via Reciprocal Rank Fusion (k=60). ---
    vec_sim_map: dict[str, float] = {}  # memory_id -> cosine similarity (for hybrid scoring)
    fts_rank_map: dict[str, float] = {}  # memory_id -> FTS5 rank (preserved across fusion)
    if hybrid and _embeddings and _embeddings.is_available() and terms:
        query_emb = _embeddings.embed_text(query)
        if query_emb is not None:
            # Preserve FTS ranks before rows are replaced by _fetch_by_ids.
            for r in rows:
                if r["fts_rank"] is not None:
                    fts_rank_map[r["id"]] = r["fts_rank"]
            # Get vec results WITH distances for the similarity map. Per-tier K
            # is generous (limit*5) so the global tier's best vec neighbors are
            # not silently excluded by a small K (issue #18 plan-critic I1).
            try:
                vec_results = conn.execute(
                    "SELECT memory_id, distance FROM memory_vec "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    [query_emb, max(limit * 5, limit + 10)],
                ).fetchall()
                vec_ids = [r["memory_id"] for r in vec_results]
                for vr in vec_results:
                    vec_sim_map[vr["memory_id"]] = max(0.0, 1.0 - vr["distance"])
            except sqlite3.OperationalError:
                vec_ids = []
            if vec_ids:
                fts_ids = [r["id"] for r in rows]
                fused_ids = _rrf_fuse(fts_ids, vec_ids, k=60)
                # Re-fetch full rows for the fused set (may include IDs not in
                # the FTS results — these are semantic matches BM25 missed).
                # The namespace filter is this tier's own ns_list (the same set
                # used for the FTS filter). vec KNN is namespace-agnostic, so a
                # fused id that lives outside this tier is dropped here — that is
                # correct: it is found by ITS OWN tier's run (recall_memory runs
                # the global tier separately when --include-global is on). This
                # keeps the per-tier-budget / hard-floor contract unconditional.
                # (Final-critic F1.)
                rows = _fetch_by_ids(conn, fused_ids, ns_list, floor)

    # Re-rank by composite score (relevance + confidence + recency + popularity).
    scored: list[tuple[float, dict]] = []
    for r in rows:
        conf = r["confidence"]
        stale_note = ""
        if r["source_hash"] and r["source_ref"].startswith("file:"):
            current = _source_hash(r["source_ref"])
            if current and current != r["source_hash"]:
                conf *= 0.5
                stale_note = " [STALE SOURCE — source file changed since extraction]"
        # Build a mutable copy with the demoted confidence so compute_score
        # uses the halved value for stale memories (not just the display field).
        row_fields = dict(r)
        row_fields["confidence"] = conf
        # For vector-only matches (fts_rank is None), use preserved FTS rank or vec_sim.
        fts_r = r["fts_rank"]
        if fts_r is None and r["id"] in fts_rank_map:
            fts_r = fts_rank_map[r["id"]]  # restore rank lost during fusion re-fetch
        vsim = vec_sim_map.get(r["id"]) if fts_r is None else None
        score = compute_score(row_fields, fts_r, now_epoch, vec_sim=vsim)
        scored.append((score, {
            "id": r["id"],
            "namespace": r["namespace"],
            "type": r["type"],
            "content": r["content"],
            "tags": r["tags"],
            "confidence": round(conf, 3),
            "signal": r["signal"],
            "source_ref": r["source_ref"],
            "valid_from": r["valid_from"],
            "stale": bool(stale_note),
            "prompt_injection_risk": _has_injection_risk_tag(r["tags"]),
            "_stale_note": stale_note,
            "_score": round(score, 4),
        }))

    # Sort by composite score descending within this tier, take top `limit`.
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]

def _merge_tiers(
    project_scored: list[tuple[float, dict]],
    global_scored: list[tuple[float, dict]],
    project_limit: int,
    global_limit: int,
) -> list[dict]:
    """Merge two recall tiers per the include-global contract (issue #18).

    Project tier is a HARD FLOOR: it always contributes up to ``project_limit``
    rows first. Global rows then fill up to ``global_limit`` slots. A global row
    can NEVER push out a project row — this mirrors ``export_pack``'s
    project-then-global rendering and the old SubagentStart bridge order, and
    satisfies the issue's "neither tier crowds the other" requirement.

    A row has exactly one namespace, so project and global tiers are disjoint;
    the id-based dedup below is defensive (it also covers the case where a v5
    migration alias made the same logical row appear under two keys).
    """
    results: list[dict] = []
    seen: set[str] = set()
    for _score, item in project_scored[:project_limit]:
        rid = item["id"]
        if rid in seen:
            continue
        seen.add(rid)
        results.append(item)
    for _score, item in global_scored[:global_limit]:
        rid = item["id"]
        if rid in seen:
            continue
        seen.add(rid)
        results.append(item)
    return results

def _bump_telemetry(conn: sqlite3.Connection, ids: list[str], *, no_bump: bool) -> None:
    """Record recall/recent/search telemetry for the returned ids.

    Issue #21: the two counters are mutually exclusive PER EVENT. Explicit recall
    (no_bump=False) advances retrieval_count/last_retrieved — the "deliberate fetch"
    signal. Passive (`--no-bump`) recall advances surfaced_count/last_surfaced — the
    "was surfaced into context" signal that hook-driven recall previously failed to
    record (so promote/prune/ranking inherited a manual-only bias). Their sum is the
    non-double-counted "times surfaced" metric (see _uses_count).
    """
    placeholders = ",".join("?" * len(ids))
    if no_bump:
        conn.execute(
            f"UPDATE memory SET surfaced_count=surfaced_count+1, last_surfaced=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
    else:
        conn.execute(
            f"UPDATE memory SET retrieval_count=retrieval_count+1, last_retrieved=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
    _commit(conn)

def recall_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    namespace: str | None = None,
    limit: int = 5,
    as_json: bool = False,
    min_confidence: float | None = None,
    hybrid: bool = False,
    no_bump: bool = False,
    include_global: bool = False,
    global_limit: int = 3,
) -> list[dict]:
    """FTS5 keyword recall with composite ranking + optional hybrid RRF fusion.

    When ``no_bump`` is True the retrieval_count / last_retrieved write is suppressed —
    recall instead records the passive *surface* on surfaced_count / last_surfaced
    (issue #21). Hook-driven recall (UserPromptSubmit, SubagentStart, SessionStart) passes
    this so heavy subagent fan-out does not turn every delegated agent into a concurrent
    retrieval_count writer on the shared store (PLAN.md §5), while the surface event is
    still counted. Explicit skill-invoked recall keeps the default (bumps retrieval_count).

    Candidates are fetched via FTS5 BM25, then re-ranked by a composite score
    that incorporates BM25 relevance, confidence, recency decay, and retrieval
    popularity. If hybrid=True and embeddings are available, candidates are also
    fetched via vector KNN and fused via Reciprocal Rank Fusion (RRF) before the
    composite re-ranking.

    Confidence is still a hard floor (high-precision-first principle): memories
    below CONFIDENCE_FLOOR (or min_confidence) are dropped before scoring.

    When ``include_global`` is True and ``namespace`` is set to something other
    than ``GLOBAL_NAMESPACE`` ("user:global"), the result ALSO surfaces up to
    ``global_limit`` query-relevant rows from the global tier — so a
    project-scoped session can finally reach cross-project lessons. The merge is
    project-first-then-global (a global row never crowds out a project row),
    mirroring ``export-pack`` (issue #18). Strict-by-default: with
    ``include_global=False`` (the default) behaviour is byte-identical to before.
    """
    now_epoch = time.time()
    ns_list = _expand_namespace_aliases(conn, namespace)

    # The global tier is folded in only when explicitly requested AND a specific
    # non-global namespace was asked for. When namespace is None (unscoped) the
    # project tier already searches everything, so folding global would be
    # redundant; when namespace IS user:global, the project tier already covers
    # it (possibly via a v5 migration alias), so folding would only risk
    # double-counting. This guard is therefore both a correctness and a perf
    # shortcut. (issue #18 plan-critic M2)
    do_global = bool(include_global and namespace and namespace != GLOBAL_NAMESPACE)

    if do_global:
        global_ns_list = _expand_namespace_aliases(conn, GLOBAL_NAMESPACE)
    else:
        global_ns_list = None

    # Project tier: scoped to the project namespace aliases only. It is NOT
    # widened to include global aliases on the hybrid path — the global tier's
    # own _recall_one_tier run below performs the same namespace-agnostic vec
    # KNN and re-fetches filtered to global aliases, so a global-only vec
    # neighbor surfaces via the global tier (counted against global_limit) rather
    # than leaking into the project tier and breaking the per-tier-budget /
    # hard-floor contract. (Final-critic F1: the widening was redundant and
    # contract-violating.)
    project_scored = _recall_one_tier(
        conn, query=query, ns_list=ns_list, limit=limit,
        min_confidence=min_confidence, hybrid=hybrid, now_epoch=now_epoch,
    )

    global_scored: list[tuple[float, dict]] = []
    if do_global:
        global_scored = _recall_one_tier(
            conn, query=query, ns_list=global_ns_list, limit=global_limit,
            min_confidence=min_confidence, hybrid=hybrid, now_epoch=now_epoch,
        )

    if do_global:
        results = _merge_tiers(project_scored, global_scored, limit, global_limit)
    else:
        results = [item for _score, item in project_scored[:limit]]

    if results:
        ids = [r["id"] for r in results]
        _bump_telemetry(conn, ids, no_bump=no_bump)

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("[zmem] no matching memories found.")
        for r in results:
            marker = " \u26a0injection-risk" if r.get("prompt_injection_risk") else ""
            print(f"--- [{r['id']}] (conf={r['confidence']}, signal={r['signal']}, "
                  f"ns={r['namespace']}, type={r['type']}){marker}{r['_stale_note']}")
            print(f"    {r['content']}")
            if r["tags"]:
                print(f"    tags: {r['tags']}")
    return results

def _recent_one_tier(
    conn: sqlite3.Connection,
    *,
    ns_list: list[str] | None,
    limit: int,
    min_confidence: float,
) -> list[dict]:
    """Cheap admin pull of the most recent live memories for ONE namespace set
    (a single recent tier). No FTS, no bump, no print — caller merges, bumps,
    prints.

    ``ns_list`` is the already-expanded namespace match set (the output of
    ``_expand_namespace_aliases``). ``None`` ⇒ no namespace filter (all rows).
    Accepting an expanded set (rather than a single strict value) is the
    defect-2 fix: ``recent`` now honours v5 migration aliases the way
    ``recall`` already did. (issue #18)
    """
    params: list = [min_confidence]
    ns_clause = ""
    if ns_list:
        ns_placeholders = ",".join("?" * len(ns_list))
        ns_clause = f"AND namespace IN ({ns_placeholders})"
        params.extend(ns_list)
    params.append(limit)
    rows = conn.execute(
        f"""SELECT id, namespace, type, content, tags, source_ref, source_hash,
                  confidence, signal, valid_from, ingestion_ts, last_retrieved
            FROM memory
            WHERE superseded_at IS NULL AND confidence >= ?
            {ns_clause}
            ORDER BY ingestion_ts DESC LIMIT ?""",
        params,
    ).fetchall()
    results = []
    for r in rows:
        conf = r["confidence"]
        stale_note = ""
        if r["source_hash"] and r["source_ref"].startswith("file:"):
            current = _source_hash(r["source_ref"])
            if current and current != r["source_hash"]:
                conf *= 0.5
                stale_note = " [STALE SOURCE]"
        results.append({
            "id": r["id"],
            "namespace": r["namespace"],
            "type": r["type"],
            "content": r["content"],
            "tags": r["tags"],
            "confidence": round(conf, 3),
            "signal": r["signal"],
            "source_ref": r["source_ref"],
            "valid_from": r["valid_from"],
            "stale": bool(stale_note),
            "prompt_injection_risk": _has_injection_risk_tag(r["tags"]),
            "_stale_note": stale_note,
        })
    return results

def recent_memory(
    conn: sqlite3.Connection,
    *,
    namespace: str | None = None,
    limit: int = 5,
    min_confidence: float = 0.5,
    as_json: bool = False,
    no_bump: bool = False,
    include_global: bool = False,
    global_limit: int = 3,
) -> list[dict]:
    """Cheap admin pull of the most recent live memories (no FTS scoring).

    When ``no_bump`` is True the retrieval_count / last_retrieved write is suppressed —
    recent instead records the passive *surface* on surfaced_count / last_surfaced
    (issue #21). Hook-driven subagent recall passes this so a dispatch fan-out does not
    make every subagent a concurrent retrieval_count writer on the shared store
    (PLAN.md §5), while the surface event is still counted.

    When ``include_global`` is True and ``namespace`` is set to something other
    than ``GLOBAL_NAMESPACE`` ("user:global"), the result ALSO includes up to
    ``global_limit`` recent rows from the global tier, merged project-first
    (a global row never crowds out a project row). Strict-by-default: with
    ``include_global=False`` behaviour is byte-identical to before — except
    that ``recent`` now ALSO honours v5 migration aliases (the defect-2 fix),
    so ``recent --namespace <old pre-v5 key>`` finds rows migrated to the new
    key. (issue #18)
    """
    project_rows: list[dict] = []
    if namespace:
        project_rows = _recent_one_tier(
            conn, ns_list=_expand_namespace_aliases(conn, namespace),
            limit=limit, min_confidence=min_confidence,
        )
    else:
        # Unscoped: one tier, no namespace filter (searches everything).
        project_rows = _recent_one_tier(
            conn, ns_list=None, limit=limit, min_confidence=min_confidence,
        )

    # Global tier fold-in — same guard/rationale as recall_memory (see M2).
    if include_global and namespace and namespace != GLOBAL_NAMESPACE:
        global_rows = _recent_one_tier(
            conn, ns_list=_expand_namespace_aliases(conn, GLOBAL_NAMESPACE),
            limit=global_limit, min_confidence=min_confidence,
        )
        # Merge project-first (hard floor) then global, dedup by id.
        seen: set[str] = {r["id"] for r in project_rows}
        for r in global_rows:
            if r["id"] not in seen:
                project_rows.append(r)
                seen.add(r["id"])

    results = project_rows
    if results:
        ids = [r["id"] for r in results]
        _bump_telemetry(conn, ids, no_bump=no_bump)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("[zmem] no recent memories.")
        for r in results:
            marker = " \u26a0injection-risk" if r.get("prompt_injection_risk") else ""
            print(f"--- [{r['id']}] (conf={r['confidence']}, signal={r['signal']}, "
                  f"ns={r['namespace']}, type={r['type']}){marker}{r['_stale_note']}")
            print(f"    {r['content']}")
            if r["tags"]:
                print(f"    tags: {r['tags']}")
    return results

def list_memory(conn, *, namespace=None, limit=50, include_superseded=False):
    params = []
    clauses = []
    if namespace:
        clauses.append("namespace=?")
        params.append(namespace)
    if not include_superseded:
        clauses.append("superseded_at IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT id, namespace, type, substr(content,1,80) AS preview, confidence, "
        f"signal, tags, superseded_at FROM memory {where} ORDER BY ingestion_ts DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        print("[zmem] (no memories)")
    for r in rows:
        status = "SUPERSEDED" if r["superseded_at"] else "live"
        # Surface the prompt-injection-risk tag as a visible marker so the
        # detector is actionable at read time, not write-only (#36 M5).
        marker = " \u26a0injection-risk" if _has_injection_risk_tag(r["tags"]) else ""
        print(f"[{r['id']}] {status} ns={r['namespace']} type={r['type']} "
              f"conf={r['confidence']} sig={r['signal']}{marker} :: {r['preview']}")

def stats(conn):
    n_total = conn.execute("SELECT count(*) AS c FROM memory").fetchone()["c"]
    n_live = conn.execute("SELECT count(*) AS c FROM memory WHERE superseded_at IS NULL").fetchone()["c"]
    n_super = n_total - n_live
    by_ns = conn.execute(
        "SELECT namespace, count(*) AS c FROM memory WHERE superseded_at IS NULL GROUP BY namespace ORDER BY c DESC"
    ).fetchall()
    by_signal = conn.execute(
        "SELECT signal, count(*) AS c FROM memory WHERE superseded_at IS NULL GROUP BY signal ORDER BY c DESC"
    ).fetchall()
    print(f"store: {STORE_PATH}")
    print(f"total={n_total} live={n_live} superseded={n_super}")
    print("by namespace (live):")
    for r in by_ns:
        print(f"  {r['namespace']}: {r['c']}")
    print("by signal (live):")
    for r in by_signal:
        print(f"  {r['signal']}: {r['c']}")
    # Embedding coverage (live rows only). A store can be partially or fully
    # unembedded when captures run in an environment without the embedding
    # runtime — unembedded rows skip semantic dedup-on-write, vector recall,
    # and embedding-seeded consolidation. Surfacing the ratio here makes drift
    # one command away from being noticed (issue #22).
    n_live_emb = conn.execute(
        "SELECT count(*) AS c FROM memory "
        "WHERE superseded_at IS NULL AND embedding IS NOT NULL"
    ).fetchone()["c"]
    n_live_noemb = n_live - n_live_emb
    print("embedding coverage (live):")
    print(f"  with_embedding={n_live_emb} without_embedding={n_live_noemb}")
    if _embeddings:
        try:
            st = _embeddings.availability_status()
            print(
                f"  embeddings={'available' if st['available'] else 'unavailable'} "
                f"(reason={st['reason']}, models_dir={st['models_dir']})"
            )
        except Exception:
            print("  embeddings=unknown (availability probe failed)")
    else:
        print("  embeddings=unavailable (embeddings module not importable)")

    # Operational health: surface when maintenance last ran so an operator can
    # tell from one command whether backup/consolidation are healthy (#37 L21).
    # Both timestamps are written to the `meta` table by consolidate/backup;
    # absence means "never" (a fresh store or one where the hooks never fired).
    print("operational health:")
    for label, key in (("last_backup", "last_backup"),
                       ("last_consolidation", "last_consolidation")):
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except Exception:
            row = None
        ts = row[0] if row and row[0] else None
        # Recency first (one-glance cadence health, #39 E1), with the raw
        # ISO timestamp appended so the exact time is still recoverable.
        recency = _format_recency(ts)
        print(f"  {label}: {recency}" + (f" ({ts})" if ts else ""))

def get_memory(conn, mid) -> bool:
    """Print a memory as JSON. Returns True if found, False if not — so the CLI
    dispatch can exit non-zero on a miss (fail-closed, matching `supersede`),
    not silently exit 0 while a caller checking `$?` treats "not found" as
    success (#36 M2). Binary columns (the embedding BLOB on hosts where the
    optional runtime embedded the row) render as a `<N-byte blob>` marker:
    `SELECT *` would otherwise raise `TypeError: Object of type bytes is not
    JSON serializable` on embedding-enabled hosts — a crash the model-absent
    CI matrix can never see (#38 I7 / #56)."""
    r = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    if not r:
        print(f"[zmem] no memory with id {mid}", file=sys.stderr)
        return False
    d = {k: (f"<{len(v)}-byte blob>" if isinstance(v, bytes) else v)
         for k, v in dict(r).items()}
    print(json.dumps(d, indent=2))
    return True

def _has_any_embedding(conn: sqlite3.Connection) -> bool:
    """Check if any live memory has an embedding."""
    row = conn.execute(
        "SELECT 1 FROM memory WHERE superseded_at IS NULL AND embedding IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None

def _reembed(conn: sqlite3.Connection) -> None:
    """Backfill embeddings + vec0 entries for live memories missing them."""
    if not _embeddings or not _embeddings.is_available():
        print("[zmem] embeddings unavailable — install onnxruntime + tokenizers "
              "and ensure the model file is present.", file=sys.stderr)
        return

    # Phase 1: embed memories that have no embedding at all.
    need_embed = conn.execute(
        "SELECT id, content FROM memory WHERE superseded_at IS NULL AND embedding IS NULL"
    ).fetchall()

    # Phase 2: find memories with embeddings but missing from memory_vec.
    # This happens when reembed ran before sqlite-vec was loaded on connect().
    # NOTE: vec_ids is recomputed AFTER Phase 1 to avoid inserting duplicates
    # for memories that Phase 1 just embedded.
    try:
        vec_ids = set(
            r["memory_id"] for r in conn.execute("SELECT memory_id FROM memory_vec").fetchall()
        )
    except sqlite3.OperationalError:
        vec_ids = set()  # vec0 table not available

    if not need_embed and not vec_ids and not _has_any_embedding(conn):
        print("[zmem] all live memories already have embeddings and vec0 entries")
        return

    embed_count = 0
    for r in need_embed:
        emb = _embeddings.embed_text(r["content"])
        if emb is None:
            continue
        conn.execute(
            "UPDATE memory SET embedding=?, embedding_model='minilm-onnx', embedded_at=? WHERE id=?",
            (emb, now_iso(), r["id"]),
        )
        vec_ids.add(r["id"])  # track unconditionally — we embedded it
        try:
            conn.execute(
                "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                [emb, r["id"]],
            )
        except sqlite3.OperationalError:
            pass
        embed_count += 1

    # Phase 2: populate vec0 for memories that have embeddings but are missing
    # from memory_vec (e.g. embedded before sqlite-vec was available on connect).
    # vec_ids was updated during Phase 1 to include newly embedded memories.
    need_vec = conn.execute(
        "SELECT id, embedding FROM memory "
        "WHERE superseded_at IS NULL AND embedding IS NOT NULL"
    ).fetchall()
    need_vec = [r for r in need_vec if r["id"] not in vec_ids]

    vec_count = 0
    for r in need_vec:
        try:
            conn.execute(
                "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                [r["embedding"], r["id"]],
            )
            vec_count += 1
        except sqlite3.OperationalError:
            pass

    conn.commit()
    parts = []
    if embed_count:
        parts.append(f"embedded {embed_count}")
    if vec_count:
        parts.append(f"populated vec0 for {vec_count}")
    print(f"[zmem] {' + '.join(parts)} memories")
