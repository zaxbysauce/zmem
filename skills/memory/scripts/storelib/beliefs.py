"""Deterministic belief heads (issue #137, Workstream F PR 5 of 7).

Belief heads are provenance-preserving side tables derived from live memory
rows plus ``supports``/``updates``/``contradicts`` links.  A head is a stable
aggregate over one topic (a connected group of same-namespace live rows):
it carries the source set, the evidence set inherited from ``memory_evidence``,
contested state, weakest-source floors (confidence / signal / taint / trust),
and a refresh watermark.  Recall surfaces heads as VIRTUAL rows (ids
``belief:<head_id>``); a trusted admitted head suppresses its represented
source rows within the same delivered fence only.  Heads are never written
into the canonical ``memory`` table, and the optional local-LLM action path
runs only inside explicit maintenance commands.

Determinism contract: the same store state plus the same ``now`` produce
byte-identical side-table rows.  Every iteration order below is sorted;
``refresh_watermark`` is the single wall-clock input per refresh.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from storelib.schema import now_iso
from storelib.write import _SIGNAL_RANK

import schema_meta

# Bumped only when head-derivation semantics change; stored on every head so
# a future revision can tell which rows need a rebuild.
GENERATOR_REVISION = "belief-heads-v1"

# Hard cap on virtual head rows merged into one recall (issue #137 scope 5).
RECALL_HEAD_LIMIT = 3

# Adapter payload bounds (issue #137 scope 7).
ADAPTER_MAX_SOURCE_ROWS = 20
ADAPTER_MAX_CONTENT_BYTES = 400

BELIEF_ROW_PREFIX = "belief:"
RETRACTED_META_PREFIX = "belief_retracted:"

_HEAD_STATES = ("active", "contested", "retracted")
_VALID_OPS = ("replace_quote", "add_source", "mark_contested", "retract_source")
# (from_state, to_state) pairs the maintenance action path may take; a
# retracted head is terminal.
_STATE_TRANSITIONS = {
    ("active", "contested"),
    ("active", "retracted"),
    ("contested", "retracted"),
}

# In-process head registry: maps raw head_id (topic_identity hex) to the
# metadata suppress_represented_rows needs when the head's virtual row is not
# part of the caller's rows.  Populated by refresh_belief_heads and
# belief_head_rows; production recall passes admitted virtual rows, so the
# registry is a direct-caller/test seam, never a cross-process channel.
_HEAD_REGISTRY: dict[str, dict] = {}


class BeliefActionError(RuntimeError):
    """A maintenance action failed validation; the full set was rolled back."""


class BeliefAdapterError(RuntimeError):
    """The local maintenance adapter itself failed; nothing was applied."""


def topic_identity(namespace: str, member_ids: list[str]) -> str:
    """Stable topic identity: sha256 over lowercase namespace + NUL +
    sorted member ids joined by NUL (UTF-8)."""
    payload = namespace.lower() + "\0" + "\0".join(sorted(member_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def strip_belief_prefix(head_id: str) -> str:
    """Return the raw head id behind a ``belief:<head_id>`` virtual id."""
    if isinstance(head_id, str) and head_id.startswith(BELIEF_ROW_PREFIX):
        return head_id[len(BELIEF_ROW_PREFIX):]
    return head_id


def _delete_head_children(conn: sqlite3.Connection, head_id: str) -> None:
    """Remove a head's source/evidence rows.  Called before any head
    replacement, retraction, or parent tombstone, inside the caller's
    transaction — the side tables have no ON DELETE CASCADE by contract, so
    this helper is the single cleanup path every writer shares."""
    conn.execute("DELETE FROM belief_head_evidence WHERE head_id=?", (head_id,))
    conn.execute("DELETE FROM belief_head_source WHERE head_id=?", (head_id,))


def _record_retracted(conn: sqlite3.Connection, head_id: str,
                      removed_ids: list[str]) -> list[str]:
    """Append removed source ids to the head's sorted retracted version
    record (meta key ``belief_retracted:<head_id>``) and return the record."""
    key = RETRACTED_META_PREFIX + head_id
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    record = set(json.loads(row[0])) if row else set()
    record.update(removed_ids)
    ordered = sorted(record)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, json.dumps(ordered)),
    )
    return ordered


def _retracted_record(conn: sqlite3.Connection, head_id: str) -> list[str]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (RETRACTED_META_PREFIX + head_id,)).fetchone()
    return json.loads(row[0]) if row else []


def _split_tags(tags: str) -> set[str]:
    return {t.strip().lower() for t in (tags or "").split(",") if t.strip()}


def _candidate_rows(conn: sqlite3.Connection, namespace: str | None,
                    topic_ids: list[str] | None) -> list[dict]:
    """Live, non-summary candidate rows in scope.  Organize's synthetic
    summaries (``source_ref`` prefix ``organize:``) are excluded from
    grounding until their ``merged_from`` members resolve."""
    sql = (
        "SELECT id, namespace, type, content, tags, source_ref, confidence, "
        "signal, taint, trust_score, ingestion_ts FROM memory "
        "WHERE superseded_at IS NULL "
        "AND (source_ref IS NULL OR source_ref NOT LIKE 'organize:%')"
    )
    params: list[str] = []
    if namespace is not None:
        sql += " AND namespace = ?"
        params.append(namespace)
    if topic_ids:
        sql += " AND id IN (%s)" % ",".join("?" * len(topic_ids))
        params.extend(topic_ids)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params)]


def _topic_groups(rows: list[dict],
                  links: list[tuple[str, str]]) -> list[list[dict]]:
    """Group candidates into topics.  (a) union-find over memory_link edges
    (any relation) among the candidates; (b) tag merging with INTERSECTION
    semantics — two groups merge only while at least one tag value is carried
    by EVERY member of the merged group (fixed point, deterministic order);
    (c) rows that link or share no tag stand alone as degenerate single-row
    topics (mirroring organize's own topic semantics)."""
    parent: dict[str, str] = {r["id"]: r["id"] for r in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    ids = {r["id"] for r in rows}
    for src, dst in links:
        if src in ids and dst in ids:
            union(src, dst)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    # Intersection-semantics tag merge: merge two groups only when some tag
    # is carried by every member of BOTH groups (and hence of the merge).
    changed = True
    while changed:
        changed = False
        ordered = sorted(groups.values(), key=lambda g: g[0]["id"])
        common = [
            set.intersection(*(_split_tags(m["tags"]) for m in g)) if g else set()
            for g in ordered
        ]
        merged_out: set[int] = set()
        for i in range(len(ordered)):
            if i in merged_out:
                continue
            for j in range(i + 1, len(ordered)):
                if j in merged_out:
                    continue
                if common[i] & common[j]:
                    ordered[i] = ordered[i] + ordered[j]
                    common[i] = common[i] & common[j]
                    merged_out.add(j)
                    changed = True
        if changed:
            next_groups: dict[str, list[dict]] = {}
            for idx, g in enumerate(ordered):
                if idx not in merged_out:
                    next_groups[g[0]["id"]] = sorted(g, key=lambda m: m["id"])
            groups = next_groups

    out = [sorted(g, key=lambda m: m["id"]) for g in groups.values()]
    out.sort(key=lambda g: g[0]["id"])
    return out


def _inherit_weakest(members: list[dict]) -> dict:
    """Weakest-source inheritance: numeric minimum for confidence and trust,
    lowest signal rank, worst taint rank."""
    return {
        "confidence": min(m["confidence"] for m in members),
        "trust_score": min(m["trust_score"] for m in members),
        "signal": min(members, key=lambda m: _SIGNAL_RANK.get(m["signal"], 0))["signal"],
        "taint": max(members, key=lambda m: schema_meta.TAINT_RANK.get(m["taint"], 0))["taint"],
    }


def _refresh_one_topic(conn: sqlite3.Connection, members: list[dict],
                       updates_dst: str | None, contested: bool,
                       watermark: str, head_id: str | None = None,
                       retracted_new: list[str] | None = None) -> dict:
    ns = members[0]["namespace"]
    member_ids = sorted(m["id"] for m in members)
    if head_id is None:
        head_id = topic_identity(ns, member_ids)
    by_id = {m["id"]: m for m in members}

    prior = conn.execute(
        "SELECT id, content, refresh_watermark FROM belief_head WHERE id=?",
        (head_id,)).fetchone()
    prior_sources = sorted(r[0] for r in conn.execute(
        "SELECT source_id FROM belief_head_source WHERE head_id=?",
        (head_id,)))

    # Head content: the newest accepted updates edge's dst row (the
    # correction quote); otherwise the newest member by (ingestion_ts, id).
    if updates_dst is not None and updates_dst in by_id:
        content = by_id[updates_dst]["content"]
    else:
        newest = max(members, key=lambda m: (m["ingestion_ts"] or "", m["id"]))
        content = newest["content"]

    floors = _inherit_weakest(members)
    head_state = "contested" if contested else "active"

    _delete_head_children(conn, head_id)
    conn.execute("DELETE FROM belief_head WHERE id=?", (head_id,))
    conn.execute(
        """INSERT INTO belief_head (id, namespace, topic_identity, content,
           head_state, head_source_id, support_count, refresh_watermark,
           generator_revision, confidence, signal, taint, trust_score)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (head_id, ns, head_id, content, head_state,
         member_ids[-1], len(member_ids), watermark, GENERATOR_REVISION,
         floors["confidence"], floors["signal"], floors["taint"],
         floors["trust_score"]),
    )
    for m in members:
        conn.execute(
            """INSERT INTO belief_head_source (head_id, source_id, role,
               source_ingestion_ts, source_checksum) VALUES (?,?,?,?,?)""",
            (head_id, m["id"], "support", m["ingestion_ts"] or "",
             hashlib.sha256((m["content"] or "").encode("utf-8")).hexdigest()),
        )
    ev_rows = conn.execute(
        "SELECT memory_id, evidence_id FROM memory_evidence "
        "WHERE memory_id IN (%s) ORDER BY memory_id, evidence_id"
        % ",".join("?" * len(member_ids)), member_ids).fetchall()
    for memory_id, evidence_id in ev_rows:
        conn.execute(
            "INSERT INTO belief_head_evidence (head_id, source_id, evidence_id) "
            "VALUES (?,?,?)", (head_id, memory_id, evidence_id))

    removed = sorted(set(prior_sources) - set(member_ids)) if retracted_new is None \
        else sorted(set(retracted_new))
    retracted = _record_retracted(conn, head_id, removed) if removed else \
        _retracted_record(conn, head_id)

    return {
        "head_id": head_id,
        "namespace": ns,
        "content": content,
        "head_state": head_state,
        "support_count": len(member_ids),
        "source_ids": member_ids,
        "evidence_ids": sorted({r[1] for r in ev_rows}),
        "refresh_watermark": watermark,
        "retracted_source_ids": retracted,
        "replaced": bool(prior),
    }


def refresh_belief_heads(conn: sqlite3.Connection, *,
                         namespace: str | None = None,
                         topic_ids: list[str] | None = None,
                         now: str | None = None) -> dict:
    """Derive belief-head side tables from live rows and links.

    One ``now`` value stamps the whole refresh (``refresh_watermark``).
    Runs in the caller's transaction when one is open (savepoint), else owns
    and commits its own.  Never writes into ``memory``.  Returns a report
    dict with refreshed/created/updated counts and the head summaries.

    Head identity is STABLE across membership change: an existing head is
    refreshed in place over the surviving live subset of its recorded
    sources (drops are recorded as retracted), and a live topic group that
    is covered by an existing head's recorded members never forks a second
    head.  New heads take their identity from their founding member set.
    """
    watermark = now if now is not None else now_iso()
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT zmem_beliefs_refresh")
    try:
        candidates = _candidate_rows(conn, namespace, topic_ids)
        by_ns: dict[str, list[dict]] = {}
        for r in candidates:
            by_ns.setdefault(r["namespace"], []).append(r)
        link_rows = conn.execute(
            "SELECT src_id, dst_id FROM memory_link").fetchall()
        links = [(r[0], r[1]) for r in link_rows]

        heads: list[dict] = []
        created = 0
        updated = 0
        covered_member_sets: list[set[str]] = []

        # Phase A — existing heads refresh in place (stable identity) over
        # the live subset of their recorded sources.
        head_sql = "SELECT id FROM belief_head"
        head_params: list[str] = []
        if namespace is not None:
            head_sql += " WHERE namespace = ?"
            head_params.append(namespace)
        head_sql += " ORDER BY id"
        for (existing_id,) in conn.execute(head_sql, head_params).fetchall():
            recorded = sorted(r[0] for r in conn.execute(
                "SELECT source_id FROM belief_head_source WHERE head_id=? "
                "ORDER BY source_id", (existing_id,)))
            placeholders = ",".join("?" * len(recorded)) or "''"
            live_rows = [dict(r) for r in conn.execute(
                "SELECT id, namespace, type, content, tags, source_ref, "
                "confidence, signal, taint, trust_score, ingestion_ts "
                "FROM memory WHERE id IN (%s) AND superseded_at IS NULL "
                "ORDER BY id" % placeholders, recorded)]
            dead = sorted(set(recorded) - {m["id"] for m in live_rows})
            if live_rows:
                summary = _refresh_one_topic(
                    conn, live_rows, _newest_updates_dst(conn, live_rows),
                    _topic_contested(conn, live_rows), watermark,
                    head_id=existing_id, retracted_new=dead)
                summary["replaced"] = True
                heads.append(summary)
                updated += 1
                covered_member_sets.append(set(recorded))
            else:
                # The belief no longer has a live source base: retract.
                _delete_head_children(conn, existing_id)
                conn.execute(
                    "UPDATE belief_head SET head_state='retracted', "
                    "support_count=0, refresh_watermark=? WHERE id=?",
                    (watermark, existing_id))
                _record_retracted(conn, existing_id, dead or recorded)

        # Phase B — brand-new topics over live rows.  A group whose members
        # are all covered by an existing head's recorded set never forks a
        # second head (the parent was just refreshed over its survivors).
        for ns in sorted(by_ns):
            ns_rows = sorted(by_ns[ns], key=lambda r: r["id"])
            ns_ids = {r["id"] for r in ns_rows}
            ns_links = [(s, d) for s, d in links if s in ns_ids and d in ns_ids]
            for group in _topic_groups(ns_rows, ns_links):
                member_ids = {m["id"] for m in group}
                if any(member_ids <= covered for covered in covered_member_sets):
                    continue
                summary = _refresh_one_topic(
                    conn, group, _newest_updates_dst(conn, group),
                    _topic_contested(conn, group), watermark)
                heads.append(summary)
                if summary["replaced"]:
                    updated += 1
                else:
                    created += 1
        report = {
            "refreshed": len(heads),
            "created": created,
            "updated": updated,
            "watermark": watermark,
            "heads": heads,
        }
        if own_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT zmem_beliefs_refresh")
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT zmem_beliefs_refresh")
            finally:
                conn.execute("RELEASE SAVEPOINT zmem_beliefs_refresh")
        raise
    for summary in heads:
        _HEAD_REGISTRY[summary["head_id"]] = {
            "head_id": summary["head_id"],
            "namespace": summary["namespace"],
            "head_state": summary["head_state"],
            "represented_ids": list(summary["source_ids"]),
            "content": summary["content"],
        }
    return report


def _newest_updates_dst(conn: sqlite3.Connection, members: list[dict]) -> str | None:
    """The dst of the newest in-topic ``updates`` edge
    (created_at DESC, src DESC, dst DESC), or None."""
    member_ids = sorted({m["id"] for m in members})
    best = None
    for src, dst, rel, created_at in conn.execute(
        "SELECT src_id, dst_id, relation, created_at FROM memory_link "
        "WHERE src_id IN (%s) AND dst_id IN (%s)"
        % (",".join("?" * len(member_ids)), ",".join("?" * len(member_ids))),
        member_ids + member_ids,
    ):
        if rel != "updates":
            continue
        key = (created_at or "", src, dst)
        if best is None or key > best[0]:
            best = (key, dst)
    return best[1] if best else None


def _topic_contested(conn: sqlite3.Connection, members: list[dict]) -> bool:
    """True when any in-topic ``contradicts`` edge touches the topic."""
    member_ids = sorted({m["id"] for m in members})
    row = conn.execute(
        "SELECT 1 FROM memory_link WHERE relation='contradicts' AND "
        "src_id IN (%s) AND dst_id IN (%s) LIMIT 1"
        % (",".join("?" * len(member_ids)), ",".join("?" * len(member_ids))),
        member_ids + member_ids,
    ).fetchone()
    return row is not None


def _register_virtual_row(row: dict) -> None:
    _HEAD_REGISTRY[strip_belief_prefix(row["id"])] = {
        "head_id": strip_belief_prefix(row["id"]),
        "namespace": row["namespace"],
        "head_state": row["head_state"],
        "represented_ids": list(row["represented_ids"]),
        "content": row["content"],
    }


def _query_terms(query: str) -> list[str]:
    terms = []
    buf = []
    for ch in (query or "").lower():
        if ch.isalnum():
            buf.append(ch)
        elif buf:
            terms.append("".join(buf))
            buf = []
    if buf:
        terms.append("".join(buf))
    return [t for t in terms if len(t) >= 2][:16]


def belief_head_rows(conn: sqlite3.Connection, *, query: str,
                     namespace: str | None, limit: int,
                     as_of: str | None = None) -> list[dict]:
    """Virtual recall rows for query-matched heads.

    Carries the full provenance contract: ``source_ids``, ``evidence_ids``,
    ``represented_ids``, ``support_count``, ``head_state``, weakest-source
    floors, and a ``belief-head:`` source_ref.  Contested heads ARE returned
    (flagged) — they never suppress.  ``as_of`` admits a head only when every
    source predates the instant.
    """
    sql = "SELECT * FROM belief_head"
    params: list[str] = []
    if namespace is not None:
        sql += " WHERE namespace = ?"
        params.append(namespace)
    sql += " ORDER BY support_count DESC, id"
    heads = conn.execute(sql, params).fetchall()
    if as_of:
        heads = [h for h in heads if _head_valid_at(conn, h["id"], as_of)]
    terms = _query_terms(query)
    matched: list[dict] = []
    for h in heads:
        if not terms:
            break
        member_texts = [r[0] for r in conn.execute(
            "SELECT m.content FROM belief_head_source s "
            "JOIN memory m ON m.id = s.source_id WHERE s.head_id=? "
            "ORDER BY s.source_id", (h["id"],))]
        haystack = " ".join([h["content"] or ""] + [
            t or "" for t in member_texts]).lower()
        if not any(t in haystack for t in terms):
            continue
        matched.append(_virtual_row(conn, h, member_texts))
        if len(matched) >= limit:
            break
    for row in matched:
        _register_virtual_row(row)
    return matched


def _head_valid_at(conn: sqlite3.Connection, head_id: str, as_of: str) -> bool:
    row = conn.execute(
        "SELECT MAX(source_ingestion_ts) FROM belief_head_source "
        "WHERE head_id=?", (head_id,)).fetchone()
    return bool(row and row[0] and row[0] <= as_of)


def _virtual_row(conn: sqlite3.Connection, head: sqlite3.Row,
                 member_texts: list[str]) -> dict:
    sources = sorted(r[0] for r in conn.execute(
        "SELECT source_id FROM belief_head_source WHERE head_id=?",
        (head["id"],)))
    evidence = sorted({r[0] for r in conn.execute(
        "SELECT evidence_id FROM belief_head_evidence WHERE head_id=?",
        (head["id"],))})
    # Head floors live on the row; derive them from the live sources so a
    # refreshed membership immediately re-floors the virtual row.
    floors = conn.execute(
        """SELECT MIN(confidence) AS confidence, MIN(trust_score) AS trust_score
           FROM memory WHERE id IN (%s)""" % ",".join("?" * len(sources)),
        sources).fetchone() if sources else None
    signals = [r[0] for r in conn.execute(
        "SELECT signal FROM memory WHERE id IN (%s)" % ",".join("?" * len(sources)),
        sources)] if sources else []
    taints = [r[0] for r in conn.execute(
        "SELECT taint FROM memory WHERE id IN (%s)" % ",".join("?" * len(sources)),
        sources)] if sources else []
    weakest_signal = (
        min(signals, key=lambda s: _SIGNAL_RANK.get(s or "none", 0))
        if signals else "none")
    worst_taint = (
        max(taints, key=lambda t: schema_meta.TAINT_RANK.get(t or "trusted_internal", 0))
        if taints else "trusted_internal")
    return {
        "id": BELIEF_ROW_PREFIX + head["id"],
        "namespace": head["namespace"],
        "type": "belief_head",
        "content": head["content"],
        "tags": "belief-head",
        "source_ref": "belief-head:" + head["generator_revision"],
        "confidence": floors["confidence"] if floors else head["support_count"] * 0.0,
        "signal": weakest_signal or "none",
        "taint": worst_taint,
        "trust_score": floors["trust_score"] if floors else 0.0,
        "head_id": head["id"],
        "head_state": head["head_state"],
        "support_count": head["support_count"],
        "source_ids": sources,
        "evidence_ids": evidence,
        "represented_ids": sources,
        "refresh_watermark": head["refresh_watermark"],
    }


def suppress_represented_rows(rows: list[dict], *,
                              trusted_head_ids: set[str],
                              namespace: str,
                              fence_id: str) -> list[dict]:
    """Drop represented source rows for ADMITTED heads.

    A head contributes its represented ids when its raw id (topic_identity
    hex — no ``belief:`` prefix) is in ``trusted_head_ids`` and the head is
    KNOWN to this seam (its virtual row is in ``rows`` or its metadata is in
    the in-process registry).  The CALLER owns admission policy: the recall
    integration only ever passes ACTIVE heads, so contested, retracted,
    filtered, budget-dropped, and not-admitted heads remove zero rows there.
    A row survives when it is not represented, its namespace differs, or it
    was stamped with a DIFFERENT fence id (rows without a stamp are
    same-fence by default).
    """
    represented: set[str] = set()
    for row in rows:
        if row.get("type") != "belief_head":
            continue
        hid = strip_belief_prefix(row.get("id", ""))
        if hid in trusted_head_ids:
            represented.update(row.get("represented_ids") or [])
            _register_virtual_row(row)
    for hid in trusted_head_ids:
        known = _HEAD_REGISTRY.get(hid)
        if known:
            represented.update(known.get("represented_ids") or [])
    if not represented:
        return rows
    kept = []
    for row in rows:
        stamp = row.get("_fence_id")
        if (row.get("id") in represented
                and row.get("namespace") == namespace
                and stamp in (None, fence_id)):
            continue
        kept.append(row)
    return kept


def apply_belief_actions(conn: sqlite3.Connection, *,
                         head_id: str, actions: list[dict],
                         validator=None) -> dict:
    """Apply validated maintenance actions to one head atomically.

    Every action must name the in-scope head, resolve its source ids to live
    member sources and its evidence ids to known evidence rows, bound its
    text at 400 UTF-8 bytes, and stay inside the state table.  ANY violation
    — or an exception from ``validator`` — rolls back the complete set and
    preserves the prior head content, source set, and watermark.  Only
    explicit maintenance surfaces may call this; no recall path does.
    """
    hid = strip_belief_prefix(head_id)
    head = conn.execute(
        "SELECT * FROM belief_head WHERE id=?", (hid,)).fetchone()
    if head is None:
        raise BeliefActionError("unknown belief head: %s" % head_id)
    state = head["head_state"]
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT zmem_belief_actions")
    try:
        if validator is not None:
            validator(dict(head), [dict(a) for a in actions])
        applied = 0
        for action in actions or []:
            state = _apply_one_action(conn, hid, state, action)
            applied += 1
        if own_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT zmem_belief_actions")
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT zmem_belief_actions")
            finally:
                conn.execute("RELEASE SAVEPOINT zmem_belief_actions")
        raise
    after = conn.execute(
        "SELECT * FROM belief_head WHERE id=?", (hid,)).fetchone()
    return {
        "head_id": hid,
        "applied": applied,
        "head_state": after["head_state"] if after else state,
        "content": after["content"] if after else None,
    }


def _apply_one_action(conn: sqlite3.Connection, hid: str, state: str,
                      action: dict) -> str:
    op = action.get("op")
    if op not in _VALID_OPS:
        raise BeliefActionError("unknown belief action op: %r" % (op,))
    target = strip_belief_prefix(action.get("head_id") or "")
    if target != hid:
        raise BeliefActionError("action targets out-of-scope head: %s" % target)
    text = action.get("markdown")
    if text is not None and len(text.encode("utf-8")) > ADAPTER_MAX_CONTENT_BYTES:
        raise BeliefActionError("action text exceeds %d UTF-8 bytes"
                                % ADAPTER_MAX_CONTENT_BYTES)
    source_ids = sorted({strip_belief_prefix(s) for s in
                         action.get("source_ids") or []})
    evidence_ids = sorted(set(action.get("evidence_ids") or []))

    known_sources = {r[0] for r in conn.execute(
        "SELECT source_id FROM belief_head_source WHERE head_id=?", (hid,))}
    if op == "replace_quote":
        for sid in source_ids:
            if sid not in known_sources:
                raise BeliefActionError("unresolved source citation: %s" % sid)
        for eid in evidence_ids:
            if not conn.execute(
                "SELECT 1 FROM belief_head_evidence WHERE head_id=? AND "
                "evidence_id=?", (hid, eid)).fetchone():
                raise BeliefActionError("unresolved evidence citation: %s" % eid)
        conn.execute("UPDATE belief_head SET content=? WHERE id=?",
                     (text or "", hid))
        return state
    if op == "add_source":
        for sid in source_ids:
            mem = conn.execute(
                "SELECT content, ingestion_ts, superseded_at, namespace FROM memory "
                "WHERE id=?", (sid,)).fetchone()
            if mem is None or mem["superseded_at"] is not None:
                raise BeliefActionError("unresolved source target: %s" % sid)
            if mem["namespace"] != conn.execute(
                "SELECT namespace FROM belief_head WHERE id=?",
                (hid,)).fetchone()["namespace"]:
                raise BeliefActionError("cross-namespace source target: %s" % sid)
            conn.execute(
                "INSERT OR IGNORE INTO belief_head_source (head_id, source_id, "
                "role, source_ingestion_ts, source_checksum) VALUES (?,?,?,?,?)",
                (hid, sid, "support", mem["ingestion_ts"] or "",
                 hashlib.sha256((mem["content"] or "").encode("utf-8")).hexdigest()),
            )
            conn.execute(
                "UPDATE belief_head SET support_count = (SELECT COUNT(*) FROM "
                "belief_head_source WHERE head_id=?) WHERE id=?", (hid, hid))
        for eid in evidence_ids:
            if not conn.execute("SELECT 1 FROM evidence WHERE id=?",
                                (eid,)).fetchone():
                raise BeliefActionError("unresolved evidence citation: %s" % eid)
            for sid in source_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO belief_head_evidence (head_id, "
                    "source_id, evidence_id) VALUES (?,?,?)", (hid, sid, eid))
        return state
    if op == "mark_contested":
        if (state, "contested") not in _STATE_TRANSITIONS:
            raise BeliefActionError("invalid transition %s -> contested" % state)
        conn.execute("UPDATE belief_head SET head_state='contested' WHERE id=?",
                     (hid,))
        return "contested"
    # retract_source
    if not source_ids:
        raise BeliefActionError("retract_source requires source_ids")
    for sid in source_ids:
        if sid not in known_sources:
            raise BeliefActionError("unresolved source citation: %s" % sid)
    for sid in source_ids:
        conn.execute("DELETE FROM belief_head_evidence WHERE head_id=? AND "
                     "source_id=?", (hid, sid))
        conn.execute("DELETE FROM belief_head_source WHERE head_id=? AND "
                     "source_id=?", (hid, sid))
    _record_retracted(conn, hid, source_ids)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM belief_head_source WHERE head_id=?",
        (hid,)).fetchone()[0]
    conn.execute(
        "UPDATE belief_head SET support_count=? WHERE id=?", (remaining, hid))
    if remaining == 0:
        if (state, "retracted") not in _STATE_TRANSITIONS:
            raise BeliefActionError("invalid transition %s -> retracted" % state)
        conn.execute("UPDATE belief_head SET head_state='retracted' WHERE id=?",
                     (hid,))
        return "retracted"
    return state


def build_adapter_payload(conn: sqlite3.Connection, summary: dict) -> dict:
    """Bounded adapter payload for one head: member identity, evidence, and
    at most ``ADAPTER_MAX_SOURCE_ROWS`` source rows with content fields
    truncated to ``ADAPTER_MAX_CONTENT_BYTES`` UTF-8 bytes."""
    source_ids = list(summary["source_ids"])
    placeholders = ",".join("?" * len(source_ids)) if source_ids else "''"
    rows = conn.execute(
        "SELECT id, content FROM memory WHERE id IN (%s) ORDER BY id"
        % placeholders, source_ids).fetchall()
    payload_rows = []
    for r in rows[:ADAPTER_MAX_SOURCE_ROWS]:
        encoded = (r["content"] or "").encode("utf-8")[:ADAPTER_MAX_CONTENT_BYTES]
        payload_rows.append({
            "id": r["id"],
            "content": encoded.decode("utf-8", errors="ignore"),
        })
    return {
        "head_id": summary["head_id"],
        "head_state": summary["head_state"],
        "section_source_ids": source_ids,
        "section_evidence_ids": list(summary["evidence_ids"]),
        "source_rows": payload_rows,
    }


def null_adapter(payload: dict) -> dict:
    """Built-in maintenance adapter (issue #137): the wired default for
    ``--llm-local``. It receives the bounded payload, validates nothing is
    asked of it, and applies zero actions — the conservative behavior when
    no local model-backed adapter is configured. A real local adapter can
    replace it wherever the maintenance caller constructs one; the
    action-validation/rollback contract is identical either way."""
    for key in ("head_id", "section_source_ids",
                "section_evidence_ids", "source_rows"):
        if key not in payload:
            raise BeliefActionError("adapter payload missing %s" % key)
    return {"actions": []}
