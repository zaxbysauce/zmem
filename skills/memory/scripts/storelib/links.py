"""Associative links (A-MEM lite) — issue #61, schema v11.

Owns the ``memory_link`` edge surface: link generation on every add/update,
trust_score deltas, budgeted 1-hop recall expansion, and the ``links`` /
``contradict`` command backends.

Design contracts (issue #61):
- Edges are DIRECTED rows. Symmetric relations (``related`` / ``supports`` /
  ``contradicts``) are stored as two rows (one per direction) via
  ``add_link_pair``; the typed Supermemory relations (``updates`` /
  ``extends`` / ``derives``) are stored as the single directed row the
  operator authored (``add_link``). ``UNIQUE(src_id, dst_id, relation)`` makes
  every insert idempotent.
- No self-links, no cross-namespace links — enforced in SQL AND re-validated
  here (fail-closed on writes).
- trust_score is the contradiction ledger, clamped to [0.0, 1.0] and
  INDEPENDENT of confidence/signal: one ``contradicts`` EVENT = one −0.10 per
  endpoint; one ``supports`` EVENT = one +0.05 per endpoint. A delta applies
  only when a NEW edge row was actually inserted (idempotent re-runs never
  double-drain trust), which is why the delta lives in ``add_link_pair`` — a
  naive double ``add_link`` would insert two DIFFERENT unique keys and apply
  the event twice.
- Attribute evolution only (6.4): linking merges TAGS into the neighbor and
  re-derives its entity links. Content / confidence / signal /
  retrieval_count are never touched, and trust moves only via the deltas
  above. There is deliberately no content-rewrite helper in this module.
- No LLM anywhere: link generation is deterministic (vec cosine via the
  dedup window, or lexical Jaccard when embeddings are unavailable). No
  ``ZMEM_LINK_LLM`` knob is shipped — a knob that gates nothing would be a
  dead flag.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys

from storelib.schema import _env_float, _vec_knn_in_namespace, now_iso

# The closed relation enum. This is the schema CHECK's Python mirror: the
# tuple, the CHECK in storelib/schema.py (init_db + the v11 migrate block),
# and doctor's probe must stay in sync. Do not add a value that cannot be
# stored and read back end-to-end (CLI insert + links --json + sync).
LINK_RELATIONS = (
    "related", "supports", "contradicts", "updates", "extends", "derives",
)

# Relations whose meaning is symmetric: generation and the curated CLI store
# them as BOTH directions so a 1-hop walk from either endpoint finds the edge.
# The typed relations keep their single authored direction.
LINK_PAIR_RELATIONS = ("related", "supports", "contradicts")

# Trust events (issue #61, 6.2): one contradicts EVENT = −0.10 per endpoint;
# one supports EVENT / corroborating re-add = +0.05. Clamped to [0.0, 1.0] by
# adjust_trust, so ten contradicts land at exactly 0.0 (never negative).
TRUST_DELTA_CONTRADICTS = -0.10
TRUST_DELTA_SUPPORTS = 0.05

# Relations that carry a trust event when a new edge is inserted.
_TRUST_EVENT_DELTAS = {
    "contradicts": TRUST_DELTA_CONTRADICTS,
    "supports": TRUST_DELTA_SUPPORTS,
}

# Minimum similarity (vec cosine OR lexical Jaccard — the same knob governs
# both paths) for automatic neighbor linking on add/update. Lower than the
# dedup threshold (0.85): everything that would merge still links, and the
# 0.75..0.85 band links WITHOUT merging.
LINK_THRESHOLD = _env_float("ZMEM_LINK_THRESHOLD", 0.75)

# Relations walked by recall's 1-hop expansion. `contradicts` participates
# too — gated by the confidence floor and tagged [CONTESTED LINK] at emit.
_EXPANSION_RELATIONS = ("related", "supports", "contradicts")


class LinkTargetError(ValueError):
    """A link endpoint is unusable: unknown id, self-link, or cross-namespace
    pair. Message is stable CLI output — the `links`/`contradict` commands
    print it and exit 1 (the `get` not-found contract)."""


def validate_relation(relation: str) -> str:
    """Fail-closed relation check (writes refuse unknown values before SQL)."""
    if relation not in LINK_RELATIONS:
        raise LinkTargetError(
            f"unknown link relation {relation!r}; must be one of "
            f"{', '.join(LINK_RELATIONS)}"
        )
    return relation


def adjust_trust(conn: sqlite3.Connection, mid: str, delta: float) -> None:
    """Apply a trust_score delta to one row, clamped to [0.0, 1.0] in SQL so
    ten contradicts land at exactly 0.0 (never negative, never > 1.0). The
    ROUND(...,10) sheds float dust only (1.0-10*0.10 would otherwise park at
    ~1.4e-16 instead of the issue's exact 0.0); all real deltas are 2-decimal.
    No-op for a missing id; never raises on the hot path."""
    if not delta:
        return
    conn.execute(
        "UPDATE memory SET trust_score = "
        "ROUND(MIN(1.0, MAX(0.0, trust_score + ?)), 10) WHERE id=?",
        (delta, mid),
    )


def _link_row(conn: sqlite3.Connection, mid: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, namespace FROM memory WHERE id=?", (mid,)
    ).fetchone()
    if row is None:
        raise LinkTargetError(f"[zmem] no memory with id {mid}")
    return row


def add_link(
    conn: sqlite3.Connection, src_id: str, dst_id: str, relation: str,
    score: float = LINK_THRESHOLD, created_at: str | None = None,
) -> bool:
    """Insert ONE directed edge. No trust logic lives here (see module doc:
    the mirror row has a different UNIQUE key, so the event delta belongs to
    add_link_pair). ``created_at`` lets ingest-jsonl preserve the exported
    timestamp (exact re-export round-trip); the default is now. Returns True
    iff a new row was inserted.
    """
    validate_relation(relation)
    if src_id == dst_id:
        raise LinkTargetError(
            f"[zmem] refusing self-link: {src_id} -> {src_id}"
        )
    src = _link_row(conn, src_id)
    dst = _link_row(conn, dst_id)
    if src["namespace"] != dst["namespace"]:
        raise LinkTargetError(
            f"[zmem] refusing cross-namespace link: {src_id} "
            f"(ns={src['namespace']}) -> {dst_id} (ns={dst['namespace']}); "
            "links never cross namespaces"
        )
    if not math.isfinite(score):
        score = LINK_THRESHOLD
    score = max(0.0, min(1.0, float(score)))
    cur = conn.execute(
        "INSERT OR IGNORE INTO memory_link"
        "(src_id, dst_id, relation, score, created_at) VALUES (?,?,?,?,?)",
        (src_id, dst_id, relation, score, created_at or now_iso()),
    )
    return bool(cur.rowcount)


def add_link_pair(
    conn: sqlite3.Connection, a: str, b: str, relation: str,
    score: float = LINK_THRESHOLD, *, apply_trust: bool = True,
) -> bool:
    """Insert a symmetric pair (a->b AND b->a) as ONE logical event.

    The trust delta (contradicts −0.10 / supports +0.05) is applied ONCE per
    endpoint, and only when at least one direction was newly inserted —
    re-running the same contradict (or re-generating the same edge from a
    later write) is an exact no-op, so trust can only drain through DISTINCT
    contradiction events. ``apply_trust=False`` is the sync/ingest path:
    restored rows carry their exported trust_score verbatim, so re-applying
    event deltas would double-count history.
    """
    inserted_fwd = add_link(conn, a, b, relation, score)
    inserted_rev = add_link(conn, b, a, relation, score)
    inserted = inserted_fwd or inserted_rev
    if inserted and apply_trust:
        delta = _TRUST_EVENT_DELTAS.get(relation)
        if delta:
            adjust_trust(conn, a, delta)
            adjust_trust(conn, b, delta)
    return inserted


def links_for_memory(conn: sqlite3.Connection, mid: str) -> list[dict]:
    """All edges touching ``mid`` (both directions), machine-ordered.
    Raises LinkTargetError for an unknown id (the `get` contract)."""
    _link_row(conn, mid)
    rows = conn.execute(
        "SELECT src_id, dst_id, relation, score, created_at FROM memory_link "
        "WHERE src_id=? OR dst_id=? "
        "ORDER BY relation, score DESC, src_id, dst_id",
        (mid, mid),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "src": r["src_id"],
            "dst": r["dst_id"],
            # "out" when mid is the source, "in" when it is the destination —
            # typed relations are directed, so the direction is part of the
            # edge's meaning on the inspection surface.
            "direction": "out" if r["src_id"] == mid else "in",
            "other": r["dst_id"] if r["src_id"] == mid else r["src_id"],
            "relation": r["relation"],
            "score": r["score"],
            "created_at": r["created_at"],
        })
    return out


def contradict_memories(
    conn: sqlite3.Connection, a: str, b: str, reason: str = "",
) -> dict:
    """The `contradict` command backend (issue #61, 6.5).

    Inserts a contradicts pair and applies the −0.10 trust event to BOTH rows.
    Never merges, deletes, or rewrites either row (no content change, no
    confidence/signal change). ``reason`` is a REQUIRED CLI guard for
    deliberate use; the issue's v11 schema has no reason column, so it is
    validated and echoed here but deliberately NOT persisted (audit lives in
    the operator's transcript + the edge's created_at).
    """
    if a == b:
        raise LinkTargetError(f"[zmem] refusing self-link: {a} -> {a}")
    inserted = add_link_pair(conn, a, b, "contradicts", score=1.0)
    trust = {}
    for mid in (a, b):
        row = conn.execute(
            "SELECT trust_score FROM memory WHERE id=?", (mid,)
        ).fetchone()
        if row:
            trust[mid] = row["trust_score"]
    return {
        "a": a, "b": b, "reason": reason, "inserted": inserted,
        "trust": trust,
    }


def _link_neighbors_lexical(
    conn: sqlite3.Connection, mid: str, content: str, tags: str,
    namespace: str, threshold: float,
) -> list[tuple[sqlite3.Row, float]]:
    """Model-absent neighbor computation: Jaccard token overlap between the
    new row and every live row in the same namespace (the consolidate lexical
    fallback's similarity, applied at write time). Bounded by the same
    per-namespace cap consolidate uses. Helpers are imported function-locally:
    storelib.consolidate imports from storelib.write (which imports this
    module), so a module-level import would cycle.
    """
    from storelib.consolidate import (
        CONSOLIDATE_MAX_ROWS_PER_NAMESPACE, _lexical_similarity, _lexical_tokens,
    )
    seed = _lexical_tokens(f"{content}\n{tags}")
    if not seed:
        return []
    rows = conn.execute(
        "SELECT id, content, tags, namespace, confidence, signal "
        "FROM memory "
        "WHERE namespace=? AND superseded_at IS NULL AND id != ? "
        "ORDER BY ingestion_ts DESC LIMIT ?",
        (namespace, mid, CONSOLIDATE_MAX_ROWS_PER_NAMESPACE),
    ).fetchall()
    out = []
    for r in rows:
        sim = _lexical_similarity(seed, _lexical_tokens(f"{r['content']}\n{r['tags']}"))
        if sim >= threshold:
            out.append((r, sim))
    return out


def _link_neighbors_vec(
    conn: sqlite3.Connection, mid: str, emb: bytes, namespace: str,
    threshold: float,
) -> list[tuple[sqlite3.Row, float]]:
    """Model-present neighbor computation: the SAME namespace-aware vec0 KNN
    window dedup uses (k=5, overfetch honored), so "the neighbors already
    computed for dedup" are exactly the rows this walks. The helper filters
    to live rows in-namespace; the re-fetch adds the fields generation needs.
    """
    knn = _vec_knn_in_namespace(
        conn, emb, namespaces=[namespace], k=5, overfetch=None,
    )
    out = []
    for nid, distance in knn:
        if nid == mid:
            continue
        sim = 1.0 - distance
        if sim < threshold:
            continue
        row = conn.execute(
            "SELECT id, content, tags, namespace, confidence, signal "
            "FROM memory WHERE id=? AND superseded_at IS NULL AND namespace=?",
            (nid, namespace),
        ).fetchone()
        if row:
            out.append((row, sim))
    return out


def generate_links_on_write(
    conn: sqlite3.Connection, mid: str, *, content: str, namespace: str,
    tags: str, emb: bytes | None, propagate_tags: bool = True,
) -> dict:
    """Generate + persist automatic links for a freshly-inserted row (6.2).

    Runs INSIDE the caller's add/update transaction (never commits): a failed
    link pass rolls back the whole write, same guarantee as entity linking.
    Neighbors above LINK_THRESHOLD link as ``related`` (both directions) — or
    as ``contradicts`` (both directions, with the −0.10 trust event) when the
    consolidate polarity signatures disagree, in which case the rows NEVER
    auto-merge (the write path's dedup check makes the same call before this
    ever runs).

    Attribute evolution (6.4): each linked neighbor unions the new row's tags
    into its own and re-derives its entity links. Content, confidence,
    signal, and retrieval_count are never touched. ``propagate_tags`` (issue
    #62 editorial round — Claude Code F-004) turns THIS off for writes whose
    ``tags`` are structural markers rather than content tags: an
    ``organize`` summary is tagged ``summary,topic``, and evolving that marker
    onto user rows would poison them (organize's own topic scope is never keyed
    on mutable tags, so the pollution would be cosmetic at best — but it is
    still suppressed at the summary-write call site to keep user rows clean).
    Link EDGES and polarity/trust events are unaffected; only the neighbor
    tag-union + relink are skipped.

    Deterministic — no LLM (see module doc). Returns a small report for the
    caller's stdout note.
    """
    # Polarity helper imported function-locally for the same cycle reason as
    # the lexical helpers (consolidate -> write -> links).
    from storelib.consolidate import _polarity_signature

    threshold = LINK_THRESHOLD
    if emb is not None:
        neighbors = _link_neighbors_vec(conn, mid, emb, namespace, threshold)
    else:
        neighbors = _link_neighbors_lexical(
            conn, mid, content, tags, namespace, threshold
        )

    report = {"related": 0, "contradicts": 0, "neighbors": len(neighbors)}
    if not neighbors:
        return report

    new_polarity = _polarity_signature(content)

    # tags merge (function-local: _merge_tag_strings lives in write.py).
    from storelib.write import _merge_tag_strings
    from storelib.entity import relink_memory

    for row, sim in neighbors:
        neighbor_pol = _polarity_signature(row["content"])
        relation = "related" if neighbor_pol == new_polarity else "contradicts"
        if add_link_pair(conn, mid, row["id"], relation, score=sim):
            report[relation] += 1
        # Attribute evolution: tags (and, via relink, entity links) flow into
        # the neighbor. First-seen tags win; empty new tags are a no-op.
        # Suppressed for structural-marker writes (``propagate_tags=False``).
        if propagate_tags:
            new_tags = (tags or "").strip()
            if new_tags:
                merged = _merge_tag_strings(row["tags"] or "", new_tags)
                if merged != (row["tags"] or ""):
                    conn.execute(
                        "UPDATE memory SET tags=? WHERE id=?", (merged, row["id"])
                    )
                    relink_memory(conn, row["id"])
    return report


def expand_recall_links(
    conn: sqlite3.Connection, results: list[dict], *,
    ns_list: list[str] | None, budget: int, as_of: str | None = None,
    min_confidence: float | None = None, no_bump: bool = False,
) -> list[dict]:
    """Budgeted 1-hop link expansion at recall (issue #61, 6.3).

    Called AFTER the tier merge + no_bump filter and BEFORE entity cards, so
    cards/telemetry cover expansion rows too. Walks related/supports/
    contradicts one hop from every result row; appends up to ``budget`` rows
    not already in the result set, ranked by link score (desc) then id.
    Eligibility mirrors the main lanes: same namespace set, live rows (or the
    as-of half-open predicate when set), and — mandated by the issue for
    contradicts only — the confidence floor. Expansion rows carry
    ``link_relation`` / ``link_of`` / ``link_score`` / ``contested_link`` keys
    and NOTHING writes those keys on non-expansion rows, so a link-free store
    keeps byte-identical recall output.
    """
    from storelib.recall import _classify_injection, _fetch_by_ids

    if budget <= 0 or not results:
        return []

    have = {r["id"] for r in results}
    # Best edge per candidate neighbor id: highest score wins; ties break by
    # (relation, parent id) so the choice is deterministic run-to-run.
    best: dict[str, tuple[float, str, str]] = {}
    for r in results:
        rid = r["id"]
        edges = conn.execute(
            "SELECT src_id, dst_id, relation, score FROM memory_link "
            "WHERE (src_id=? OR dst_id=?)",
            (rid, rid),
        ).fetchall()
        for e in edges:
            relation = e["relation"]
            if relation not in _EXPANSION_RELATIONS:
                continue
            other = e["dst_id"] if e["src_id"] == rid else e["src_id"]
            if other in have:
                continue  # never duplicate a row already in the result set
            key = (float(e["score"]), relation, rid)
            cur = best.get(other)
            if cur is None or key[:1] > cur[:1] or (
                key[:1] == cur[:1] and (key[1], key[2]) < (cur[1], cur[2])
            ):
                best[other] = key

    if not best:
        return []

    # The issue gates ONLY contradicts neighbors by the confidence floor;
    # related/supports ride the normal eligibility filters. Two fetches with
    # different floors implement exactly that split while reusing the lane
    # fetcher (namespace containment + live/as-of predicate included).
    from storelib.schema import CONFIDENCE_FLOOR
    floor = min_confidence if min_confidence is not None else CONFIDENCE_FLOOR

    plain_ids = [nid for nid, (_s, rel, _p) in best.items() if rel != "contradicts"]
    contested_ids = [nid for nid, (_s, rel, _p) in best.items() if rel == "contradicts"]
    fetched: dict[str, sqlite3.Row] = {}
    for ids, fl in ((plain_ids, 0.0), (contested_ids, floor)):
        for row in _fetch_by_ids(conn, ids, ns_list, fl, as_of=as_of):
            fetched[row["id"]] = row

    if not fetched:
        return []

    # Rank by link score desc, then id — deterministic cut at the budget.
    ranked = sorted(fetched.items(), key=lambda kv: (-best[kv[0]][0], kv[0]))
    out: list[dict] = []
    for nid, _row in ranked:
        if len(out) >= budget:
            break
        score, relation, parent = best[nid]
        item = dict(fetched[nid])
        item["link_relation"] = relation
        item["link_of"] = parent
        item["link_score"] = score
        item["contested_link"] = relation == "contradicts"
        item["prompt_injection_risk"] = _classify_injection(item)
        # The hook path (no_bump) drops injection-risk + untrusted_web rows
        # from the main set; expansion candidates get the identical drop so a
        # neighbor can never smuggle a row past that filter.
        if no_bump and (
            item["prompt_injection_risk"] or item.get("taint") == "untrusted_web"
        ):
            continue
        out.append(item)
    return out


def cmd_links(
    conn: sqlite3.Connection, *, ids: list[str], as_json: bool = False,
    add: bool = False, relation: str | None = None,
    score: float | None = None, reason: str = "",
) -> int:
    """The `store.py links` command backend (issue #61, 6.5).

    List mode (default): ``links --id UUID [--json]`` prints every edge
    touching the memory (both directions). Missing id exits 1 with the
    stable ``[zmem] no memory with id <id>`` stderr line — the `get`
    contract.

    Add mode (``--add``): ``links --add --id A --id B --relation R [--score S]``
    inserts a curated edge — the CLI insertion path the issue sanctions for
    the typed relations (``updates``/``extends``/``derives``) and for
    operator-curated ``supports``/``contradicts``. Symmetric relations insert
    both directions; typed relations insert the one authored direction.
    Trust-carrying relations (``contradicts``/``supports``) REQUIRE ``--reason``
    — the same deliberate-use guard `contradict` enforces (PRR-001: without
    it, --add was a reasonless trust-drain/inflation bypass). Bad arity or a
    missing required reason exits 2 (the argparse convention).
    """
    if add:
        if len(ids) != 2:
            print(
                f"[zmem] links --add takes exactly two --id values "
                f"(src dst), got {len(ids)}",
                file=sys.stderr,
            )
            return 2
        rel = validate_relation(relation or "related")
        # PRR-001 (swarm PR review): the trust event is the one effect that
        # outlives the command, so it carries contradict's deliberate-use
        # guard. Typed + related relations record no trust and need no reason.
        if rel in _TRUST_EVENT_DELTAS and not (reason or "").strip():
            print(
                f"[zmem] links --add --relation {rel} adjusts trust_score; "
                "--reason is required (the `contradict` deliberate-use "
                "convention)",
                file=sys.stderr,
            )
            return 2
        src, dst = ids
        try:
            if rel in LINK_PAIR_RELATIONS:
                inserted = add_link_pair(
                    conn, src, dst, rel,
                    score if score is not None else LINK_THRESHOLD,
                )
            else:
                inserted = add_link(
                    conn, src, dst, rel,
                    score if score is not None else LINK_THRESHOLD,
                )
        except LinkTargetError as exc:
            # Same contract as list mode and `contradict`: missing id /
            # self-link / cross-namespace refuse with the stable stderr line
            # and exit 1 — never a traceback.
            print(str(exc), file=sys.stderr)
            return 1
        conn.commit()
        trust_note = ""
        if rel in _TRUST_EVENT_DELTAS:
            rows = conn.execute(
                "SELECT id, trust_score FROM memory WHERE id IN (?,?)", (src, dst)
            ).fetchall()
            trust_note = "; trust now: " + ", ".join(
                f"{r['id']}={r['trust_score']}" for r in rows
            )
        print(f"[zmem] link {'inserted' if inserted else 'already present'}: "
              f"{src} -[{rel}]-> {dst}{trust_note}")
        if rel in _TRUST_EVENT_DELTAS:
            print(f"[zmem] reason (validated, not persisted): {reason.strip()}")
        return 0
    if len(ids) != 1:
        print(
            f"[zmem] links takes exactly one --id (got {len(ids)}); "
            "pass --add --id A --id B to insert a link",
            file=sys.stderr,
        )
        return 2
    try:
        edges = links_for_memory(conn, ids[0])
    except LinkTargetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(edges, indent=2))
        return 0
    if not edges:
        print(f"[zmem] no links for memory {ids[0]}")
        return 0
    for e in edges:
        arrow = "->" if e["direction"] == "out" else "<-"
        print(f"[{e['relation']}] {arrow} {e['other']} "
              f"(score={e['score']:.3f}, {e['direction']}, at {e['created_at']})")
    return 0


def cmd_contradict(conn: sqlite3.Connection, *, ids: list[str], reason: str) -> int:
    """The `store.py contradict` command backend (issue #61, 6.5).

    ``contradict --id A --id B --reason ...`` inserts a contradicts pair and
    applies the −0.10 trust event to BOTH rows. Never merges, deletes, or
    rewrites either row. Missing --reason refuses at argparse (exit 2);
    missing ids exit 1 (`get` contract); bad arity exits 2.
    """
    if len(ids) != 2:
        print(
            f"[zmem] contradict takes exactly two --id values, got {len(ids)}",
            file=sys.stderr,
        )
        return 2
    if not (reason or "").strip():
        print("[zmem] contradict requires --reason", file=sys.stderr)
        return 2
    try:
        result = contradict_memories(conn, ids[0], ids[1], reason.strip())
    except LinkTargetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    conn.commit()
    trust = result["trust"]
    print(
        f"[zmem] contradicts pair {'inserted' if result['inserted'] else 'already present'}: "
        f"{result['a']} <-> {result['b']}; trust now: "
        f"{result['a']}={trust.get(result['a'])}, {result['b']}={trust.get(result['b'])}"
    )
    print(f"[zmem] reason (validated, not persisted): {result['reason']}")
    return 0
