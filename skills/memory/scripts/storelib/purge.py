"""purge --id: durable, byte-level removal of a memory's content (issue #255).

Every other "delete" in zmem is tombstone-and-hide (``supersede_memory``):
the row, its FTS terms, its side-table rows, derived copies and the ledger
files keep the plaintext. ``purge`` is the operator primitive that actually
removes content:

  * resolve the target plus its verified ``update_of`` predecessor chain;
  * delete the ``memory`` rows and every id-keyed side-table row;
  * rewrite, delete, or refuse derived copies that carry the text verbatim
    (consolidation keepers, belief heads, extractive episode summaries),
    re-linking entities for rewritten keepers in the same transaction;
  * compact the store (FTS ``'optimize'``, ``VACUUM``, WAL checkpoint) under
    the full maintenance lock ladder and byte-verify the result, failing
    loudly on any residue;
  * scrub the ids and any needle-bearing entries from ``<data>/ops/*.ledger``
    delivery ledgers;
  * record the ids in the ``purged_id`` deny-list so ``ingest-jsonl`` cannot
    re-insert them from a peer export;
  * optionally rewrite (never delete or truncate) backup snapshots, including
    ``prerestore-*``, via ``--scrub-backups``.

Purge takes ids ONLY. Never pass secret text on a command line in a hooked
session: the host evidence writer records tool-call input into
``evidence.excerpt`` and would re-insert the fragment (issue #255, claim 14).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from storelib.schema import (
    STORE_PATH,
    MAINTENANCE_LOCK_STALE_SECONDS,
    SCHEMA_LOCK_POLL_SECONDS,
    SCHEMA_LOCK_STALE_SECONDS,
    SCHEMA_LOCK_WAIT_SECONDS,
    _cleanup_stale_writer_leases,
    _normalize_content,
    _load_vec,
    _release_named_lock,
    _strict_acquire_lock,
    now_iso,
)
from storelib.backup import (
    BACKUP_LOCK_STALE_SECONDS,
    CONSOLIDATE_LOCK_STALE_SECONDS,
    _acquire_lock,
    _release_lock,
)

# Tokens shorter than this are ignored when deciding whether a derived row
# still quotes the purged text and when computing byte-verify needles: short
# tokens ("deploy", "token") are shared vocabulary that must not flag
# unrelated rows, while FTS-term residue of a secret is what the scan hunts.
_MIN_TOKEN_LEN = 8

# Side tables whose rows name a deleted memory id in a plain column. Every
# delete is guarded by a sqlite_master probe: old snapshots (and stores
# created before a table existed) legitimately lack some of them.
_ID_SIDE_TABLES = (
    ("memory_link", "src_id"),
    ("memory_link", "dst_id"),
    ("memory_entity", "memory_id"),
    ("episode_memory", "memory_id"),
    ("memory_evidence", "memory_id"),
    ("belief_head_source", "source_id"),
    ("belief_head_evidence", "source_id"),
)

RETRACTED_META_PREFIX = "belief_retracted:"


def _tokens(text: str) -> set[str]:
    """Whitespace tokens of ``text`` worth treating as content fingerprints."""
    return {
        t for t in re.split(r"\s+", (text or "").lower())
        if len(t) >= _MIN_TOKEN_LEN
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _denylisted_ids(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "purged_id"):
        return set()
    return {r[0] for r in conn.execute("SELECT id FROM purged_id")}


def _placeholders(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def _base_id(entry: str) -> str:
    """consolidate.py records absorbed ids as ``id`` or ``id:truncated``."""
    return entry.split(":", 1)[0]


# --------------------------------------------------------------------------
# Resolution (read-only)
# --------------------------------------------------------------------------

def _resolve(conn: sqlite3.Connection, ids: list[str]) -> dict[str, Any]:
    requested = list(dict.fromkeys(ids))
    missing = [i for i in requested if conn.execute(
        "SELECT 1 FROM memory WHERE id=?", (i,)).fetchone() is None]

    chain: list[str] = []
    chain_seen: set[str] = set()
    for rid in requested:
        root = conn.execute(
            "SELECT namespace FROM memory WHERE id=?", (rid,)).fetchone()
        root_namespace = root["namespace"] if root is not None else None
        cur = rid
        path_seen: set[str] = set()
        while cur:
            # A shared predecessor is legitimate when several requested ids
            # belong to the same lineage.  Cycle detection is a property of
            # each root-to-predecessor walk; using the global result set here
            # rejects that harmless overlap as a cycle.
            if cur in path_seen:
                raise RuntimeError(
                    "purge REFUSED: cyclic update_of chain at %s" % cur)
            path_seen.add(cur)
            row = conn.execute(
                "SELECT id, update_of, namespace, superseded_at "
                "FROM memory WHERE id=?", (cur,)).fetchone()
            if row is None:
                # A missing predecessor carries no local content to purge:
                # default exports drop tombstoned rows while the live
                # successor still names them, so synced lineages legitimately
                # dangle here (and rekey skips tombstones too). End the walk;
                # predecessors that DO exist are still verified below.
                break
            if cur != rid and (
                    row["namespace"] != root_namespace
                    or not row["superseded_at"]):
                raise RuntimeError(
                    "purge REFUSED: update_of predecessor %s is not a "
                    "superseded row in namespace %s" % (cur, root_namespace))
            if cur not in chain_seen:
                chain_seen.add(cur)
                chain.append(cur)
            cur = row["update_of"] or ""

    rows = {}
    if chain:
        for r in conn.execute(
                "SELECT * FROM memory WHERE id IN (%s)" % _placeholders(chain),
                chain):
            rows[r["id"]] = dict(r)

    ph = _placeholders(chain) if chain else "NULL"
    # Bind-safe on an empty chain (all ids unknown / already purged): the
    # two IN () queries are skipped entirely so _resolve RETURNS an empty
    # result instead of raising a binding-count error — exit 3 and the
    # snapshot "skipped (no purged id present)" path must stay reachable.
    successor_updates: list[dict[str, Any]] = []
    if chain:
        successor_updates = [dict(r) for r in conn.execute(
            "SELECT id, content FROM memory WHERE update_of IN (%s)" % ph,
            chain)]

    keepers: list[dict[str, Any]] = []
    all_keepers: list[dict[str, Any]] = []
    for r in conn.execute(
            "SELECT id, content, content_norm, merged_from FROM memory "
            "WHERE merged_from IS NOT NULL AND merged_from != ''"):
        mids = [m.strip() for m in (r["merged_from"] or "").split(",")
                if m.strip()]
        all_keepers.append({"row": dict(r), "ids": mids})
    absorbed = set(chain_seen)
    remaining = list(all_keepers)
    while remaining:
        next_remaining: list[dict[str, Any]] = []
        progressed = False
        for entry in remaining:
            hit = [m for m in entry["ids"] if _base_id(m) in absorbed]
            if not hit:
                next_remaining.append(entry)
                continue
            keepers.append({"row": entry["row"], "purged": hit,
                            "merged": entry["ids"]})
            absorbed.add(entry["row"]["id"])
            progressed = True
        if not progressed:
            break
        remaining = next_remaining

    heads: list[dict[str, Any]] = []
    if chain and _table_exists(conn, "belief_head_source"):
        q = ("SELECT DISTINCT h.* FROM belief_head h "
             "JOIN belief_head_source s ON s.head_id = h.id "
             "WHERE s.source_id IN (%s)" % ph)
        for r in conn.execute(q, chain):
            heads.append(dict(r))

    summary_deletes: list[dict[str, Any]] = []
    if _table_exists(conn, "episode"):
        for pid in chain:
            ptok = _tokens(rows.get(pid, {}).get("content", ""))
            if not ptok:
                continue
            for ep in conn.execute(
                    "SELECT id, summary_memory_id FROM episode "
                    "WHERE summary_memory_id != '' AND id IN "
                    "(SELECT episode_id FROM episode_memory WHERE memory_id=?)",
                    (pid,)):
                srow = conn.execute(
                    "SELECT id, content FROM memory WHERE id=?",
                    (ep["summary_memory_id"],)).fetchone()
                if srow is not None and (_tokens(srow["content"]) & ptok):
                    summary_deletes.append(
                        {"episode_id": ep["id"], "summary_id": srow["id"]})

    return {
        "requested": requested,
        "missing": missing,
        "chain": chain,
        "rows": rows,
        "successor_updates": successor_updates,
        "keepers": keepers,
        "heads": heads,
        "summary_deletes": summary_deletes,
    }


def _plan_keeper(keeper: dict[str, Any], rows: dict[str, Any]):
    """(new_content, new_merged_from) for a keeper, or None => delete it.

    PR-review fix (PRR-002): remove the STORED merged-from blocks by their
    header (whatever body they hold — the stored text may have drifted from
    the purged row's current content), then refuse when the purged row's
    full text is still present verbatim in the stripped content (merged_from
    names it but the content block header names something else, or the strip
    was incomplete). Shared short fragments between the keeper's own base
    text and the absorbed text are unattributable after the merge — the
    byte-verify's surviving-row token suppression plus the full-content
    needle are the enforceable bar for those (documented in SKILL.md). The
    previous leak test compared against the post-merge content's own tokens
    and could never fire."""
    content = keeper["row"]["content"] or ""
    merged = list(keeper["merged"])
    removed_tokens: set[str] = set()
    for pid in keeper["purged"]:
        base = re.escape(_base_id(pid))
        block = re.search(
            r"\n*--- merged from %s(?::truncated)? ---\n(?P<body>.*?)"
            r"(?=\n*--- merged from |\Z)" % base,
            content, flags=re.DOTALL)
        if block is None:
            # `merged_from` is provenance, so a missing block means the
            # keeper's content was compressed or otherwise drifted. Refuse
            # before deleting the target rather than silently clearing the
            # provenance while leaving an unverified copy behind.
            return None
        removed_tokens |= _tokens(block.group("body"))
        content = content[:block.start()] + "\n" + content[block.end():]
        content = re.sub(
            r"\n*--- merged from %s(?::truncated)? ---\n?" % base,
            "", content, count=1)
        merged = [m for m in merged if _base_id(m) != pid]
    # Residue test 1 (removed-block tokens): any >=8-char token that lived
    # inside a removed block and still appears in the stripped content is
    # residue the byte-verify could never see (its survivor-token suppression
    # drops needles a surviving row holds), so refuse. Tokens below the
    # 8-char floor are unattributable after a merge and stay legal — a real
    # lexical consolidate cluster shares its short vocabulary, and refusing
    # those would block ordinary consolidations.
    if removed_tokens & _tokens(content):
        return None
    # Residue test 2 (PRR-002 follow-up): if the purged row's full current
    # text is still present verbatim (merged_from names it but the content
    # block header names something else, or the strip was incomplete), the
    # rewrite would silently keep the text — refuse naming the row.
    for pid in keeper["purged"]:
        absorbed = (rows[pid]["content"] or "") if pid in rows else ""
        if absorbed and absorbed.lower() in content.lower():
            return None
    return content, ",".join(merged)


def _head_surviving_source(conn: sqlite3.Connection, head_id: str,
                           chain: list[str]):
    """Newest surviving source row for a head (beliefs.py:251-257 shape)."""
    live = [dict(r) for r in conn.execute(
        "SELECT s.source_id, s.role, m.content, m.ingestion_ts, m.id "
        "FROM belief_head_source s LEFT JOIN memory m ON m.id = s.source_id "
        "WHERE s.head_id=?", (head_id,))
        if r["source_id"] not in chain and r["content"] is not None]
    if not live:
        return None
    return max(live, key=lambda s: ((s["ingestion_ts"] or ""), s["id"]))


# --------------------------------------------------------------------------
# Transaction (reusable for snapshots)
# --------------------------------------------------------------------------

def _apply_purge_transaction(conn: sqlite3.Connection,
                             res: dict[str, Any]) -> dict[str, Any]:
    """Apply one purge. Caller holds the maintenance ladder (live store) or
    an exclusively-opened snapshot. Raises on refusal; rolls back on error."""
    from storelib.entity import relink_memory

    chain = res["chain"]
    rows = res["rows"]
    ph = _placeholders(chain)
    chain_set = set(chain)

    has_vec = _table_exists(conn, "memory_vec")
    if has_vec:
        try:
            _load_vec(conn)
        except Exception as exc:
            raise RuntimeError(
                "purge REFUSED: memory_vec exists but sqlite-vec could not "
                "be loaded; refusing to claim vector erasure (%s)" % exc
            ) from exc

    keeper_rewrites: list[tuple[str, str, str]] = []
    for k in res["keepers"]:
        if k["row"]["id"] in chain_set:
            continue
        plan = _plan_keeper(k, rows)
        if plan is None:
            raise RuntimeError(
                "purge REFUSED: derived row %s still quotes the purged "
                "text after rewrite; purge that row too, or delete it "
                "manually and re-run" % k["row"]["id"])
        keeper_rewrites.append((k["row"]["id"], plan[0], plan[1]))

    head_rebuilds: list[str] = []
    head_deletes: list[str] = []
    for h in res["heads"]:
        if h["id"] in chain_set:
            continue
        if _head_surviving_source(conn, h["id"], chain) is None:
            head_deletes.append(h["id"])
        else:
            head_rebuilds.append(h["id"])

    summary_ids = [sd["summary_id"] for sd in res["summary_deletes"]]
    # Keeper residue cases REFUSE (raise) rather than delete, so the deleted
    # set is exactly the chain plus derived summary rows.
    all_deleted = chain + summary_ids
    dph = _placeholders(all_deleted) if all_deleted else "NULL"
    dargs = all_deleted or [None]

    # Capture affected side-table ids BEFORE their rows disappear.
    affected_evidence: set[str] = set()
    if _table_exists(conn, "memory_evidence"):
        for r in conn.execute(
                "SELECT DISTINCT evidence_id FROM memory_evidence "
                "WHERE memory_id IN (%s)" % ph, chain):
            affected_evidence.add(r["evidence_id"])
    if _table_exists(conn, "belief_head_evidence"):
        for r in conn.execute(
                "SELECT DISTINCT evidence_id FROM belief_head_evidence "
                "WHERE source_id IN (%s)" % ph, chain):
            affected_evidence.add(r["evidence_id"])

    meta_scrubs: list[tuple[str, str]] = []
    if _table_exists(conn, "meta"):
        for r in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE ?",
                (RETRACTED_META_PREFIX + "%",)):
            try:
                id_list = json.loads(r["value"])
            except (TypeError, ValueError):
                continue
            if not isinstance(id_list, list):
                continue
            kept = [i for i in id_list if i not in chain_set]
            if len(kept) != len(id_list):
                meta_scrubs.append((r["key"], json.dumps(kept)))

    try:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute("DELETE FROM memory WHERE id IN (%s)" % ph, chain)
        if summary_ids:
            conn.execute("DELETE FROM memory WHERE id IN (%s)"
                         % _placeholders(summary_ids), summary_ids)
        if has_vec:
            conn.execute(
                "DELETE FROM memory_vec WHERE memory_id IN (%s)" % dph,
                dargs)
            remaining_vec = conn.execute(
                "SELECT COUNT(*) FROM memory_vec WHERE memory_id IN (%s)"
                % dph, dargs).fetchone()[0]
            if remaining_vec:
                raise RuntimeError(
                    "purge REFUSED: memory_vec still contains %d row(s) "
                    "for the purged id(s)" % remaining_vec)

        for table, col in _ID_SIDE_TABLES:
            if _table_exists(conn, table):
                conn.execute(
                    "DELETE FROM %s WHERE %s IN (%s)" % (table, col, dph),
                    dargs)
        if _table_exists(conn, "episode"):
            conn.execute(
                "UPDATE episode SET summary_memory_id='' "
                "WHERE summary_memory_id IN (%s)" % dph, dargs)

        # Evidence orphans: only evidence that lost its LAST reference.
        deleted_evidence: list[str] = []
        for ev in sorted(affected_evidence):
            if conn.execute(
                    "SELECT 1 FROM memory_evidence WHERE evidence_id=?",
                    (ev,)).fetchone() is not None:
                continue
            if _table_exists(conn, "belief_head_evidence") and conn.execute(
                    "SELECT 1 FROM belief_head_evidence WHERE evidence_id=?",
                    (ev,)).fetchone() is not None:
                continue
            conn.execute("DELETE FROM evidence WHERE id=?", (ev,))
            if _table_exists(conn, "episode_evidence"):
                conn.execute(
                    "DELETE FROM episode_evidence WHERE evidence_id=?", (ev,))
            if _table_exists(conn, "belief_head_evidence"):
                conn.execute(
                    "DELETE FROM belief_head_evidence WHERE evidence_id=?",
                    (ev,))
            deleted_evidence.append(ev)

        # Keepers: rewrite + relink FIRST (entity invariant, entity.py:11-14)
        # -- belief-head rebuilds below must read the keeper's POST-strip
        # content, or the rebuilt head re-carries the absorbed text (C6).
        for kid, new_content, new_merged in keeper_rewrites:
            conn.execute(
                "UPDATE memory SET content=?, content_norm=?, merged_from=? "
                "WHERE id=?",
                (new_content, _normalize_content(new_content), new_merged,
                 kid))
            relink_memory(conn, kid)

        # Belief heads: rebuild survivors, delete the sourceless.
        for hid in head_deletes:
            conn.execute("DELETE FROM belief_head WHERE id=?", (hid,))
        for hid in head_rebuilds:
            src = _head_surviving_source(conn, hid, chain)
            conn.execute(
                "UPDATE belief_head SET content=?, head_source_id=?, "
                "support_count=MAX(support_count-1, 0) WHERE id=?",
                (src["content"], src["id"], hid))

        for sd in res["summary_deletes"]:
            conn.execute("UPDATE episode SET summary_memory_id='' WHERE id=?",
                         (sd["episode_id"],))
        for s in res["successor_updates"]:
            if s["id"] not in chain_set:
                conn.execute("UPDATE memory SET update_of='' WHERE id=?",
                             (s["id"],))
        for key, value in meta_scrubs:
            if value == "[]":
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                conn.execute("UPDATE meta SET value=? WHERE key=?",
                             (value, key))

        # Reap orphaned entities before the transaction commits.  Canonical
        # names and aliases can carry the purge needle, so an orphan cleanup
        # failure must roll back the memory deletion instead of returning a
        # successful purge that verification later suppresses as a survivor.
        gone_entities: list[str] = []
        if _table_exists(conn, "entity"):
            gone_entities = [r["id"] for r in conn.execute(
                "SELECT id FROM entity WHERE id NOT IN "
                "(SELECT entity_id FROM memory_entity)")]
            if gone_entities and _table_exists(conn, "entity_alias"):
                conn.execute(
                    "DELETE FROM entity_alias WHERE entity_id IN (%s)"
                    % _placeholders(gone_entities), gone_entities)
            for entity_id in gone_entities:
                conn.execute("DELETE FROM entity WHERE id=?", (entity_id,))

        if _table_exists(conn, "purged_id"):
            # Every id deleted by this purge joins the deny-list, not just the
            # requested chain: derived-deleted rows (summaries, keepers) carry
            # the purged text too, and a pre-purge peer export still holds them.
            for pid in [*all_deleted, *deleted_evidence]:
                conn.execute(
                    "INSERT OR IGNORE INTO purged_id (id, purged_at) "
                    "VALUES (?, ?)", (pid, now_iso()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "chain": chain,
        "rows": rows,
        "keeper_rewrites": keeper_rewrites,
        "head_rebuilds": head_rebuilds,
        "head_deletes": head_deletes,
        "summary_deletes": res["summary_deletes"],
        "successor_updates": res["successor_updates"],
        "deleted_evidence": deleted_evidence,
        "gone_entities": gone_entities,
        "meta_scrubs": meta_scrubs,
    }


# --------------------------------------------------------------------------
# Needles, compaction, verification
# --------------------------------------------------------------------------

def _needles(conn: sqlite3.Connection, applied: dict[str, Any]) -> list[str]:
    """Byte-verify needles: full content + tokens absent from SANCTIONED
    SURVIVORS (kept memory rows, kept evidence excerpts, kept entity names,
    kept belief-head content). Deleted-derived rows cannot suppress a needle
    because they are already gone when this runs."""
    chain = applied["chain"]
    rows = applied["rows"]
    surviving: set[str] = set()
    for r in conn.execute("SELECT content, content_norm FROM memory"):
        surviving |= _tokens(r["content"] or "")
        surviving |= _tokens(r["content_norm"] or "")
    if _table_exists(conn, "evidence"):
        for r in conn.execute("SELECT excerpt FROM evidence"):
            surviving |= _tokens(r["excerpt"] or "")
    if _table_exists(conn, "entity"):
        for r in conn.execute(
                "SELECT canonical_name FROM entity"):
            surviving |= _tokens(r["canonical_name"] or "")
    if _table_exists(conn, "entity_alias"):
        for r in conn.execute("SELECT alias_norm FROM entity_alias"):
            surviving |= _tokens(r["alias_norm"] or "")
    if _table_exists(conn, "belief_head"):
        for r in conn.execute("SELECT content FROM belief_head"):
            surviving |= _tokens(r["content"] or "")
    # The store's own DDL vocabulary (CREATE TABLE/INDEX/VIRTUAL TABLE text in
    # sqlite_schema) outlives every purge and byte-matches ordinary words
    # ("namespace", "confidence", "evidence", ...) — those tokens are not
    # purge residue by definition, or purging any content that mentions a
    # schema word would false-exit 5 on a fully-removed store.
    for r in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL"):
        surviving |= _tokens(r["sql"] or "")

    # Full-content needle: a memory whose ENTIRE content is a substring of
    # the store's own DDL text (contrived single-word contents like
    # "namespace") would false-exit 5 on bytes that are schema, not residue.
    schema_blob = " ".join(
        (r["sql"] or "") for r in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL")).lower()
    needles: set[str] = set()
    for pid in chain:
        content = rows[pid].get("content") or ""
        if not content:
            continue
        if content.lower() not in schema_blob:
            needles.add(content.lower())
        needles |= (_tokens(content) - surviving)
    needles.discard("")
    return sorted(needles)


def compact_and_verify(store_path: Path, needles: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """optimize -> VACUUM -> checkpoint (busy flag checked) -> byte-verify."""

    def _open():
        c = sqlite3.connect(str(store_path), timeout=30.0)
        c.execute("PRAGMA busy_timeout=30000")
        return c

    c = _open()
    try:
        # Old snapshots may predate the FTS table entirely; the delete trigger
        # is what keeps a real store in sync, so optimize only when present.
        has_fts = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone() is not None
        if has_fts:
            c.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
            c.commit()
    finally:
        c.close()
    c = _open()
    try:
        c.execute("VACUUM")
        c.commit()
    finally:
        c.close()
    c = _open()
    try:
        row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is not None and row[0] != 0:
            raise sqlite3.OperationalError(
                "wal_checkpoint busy=%r (log=%r, checkpointed=%r)" %
                (row[0], row[1], row[2]))
    finally:
        c.close()

    residue: dict[str, int] = {}
    residue_files: dict[str, str] = {}
    for name in (store_path, Path(str(store_path) + "-wal")):
        if not name.exists():
            continue
        data = name.read_bytes().lower()
        for n in needles:
            c = data.count(n.encode("utf-8"))
            if c:
                residue[n] = residue.get(n, 0) + c
                residue_files.setdefault(n, name.name)
    return residue, residue_files


# --------------------------------------------------------------------------
# Ledger scrub
# --------------------------------------------------------------------------

def scrub_ledgers(data_dir: Path, drop_ids: set[str],
                  needles: list[str]) -> dict[str, int]:
    """Drop purged/derived-deleted ids' entries and any needle-bearing entry
    from <data>/ops/*.ledger; delete orphaned .ledger.tmp.* partial writes.
    FAIL-CLOSED: an unreadable or unwritable ledger raises (AC7 is
    unconditional); a file with no matching entries is left untouched."""
    ops = data_dir / "ops"
    stats = {"files_scrubbed": 0, "entries_dropped": 0, "tmp_removed": 0}
    if not ops.is_dir():
        return stats
    for path in ops.glob("*.ledger.tmp.*"):
        path.unlink()
        stats["tmp_removed"] += 1
    for path in ops.glob("*.ledger"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc.get("entries")
        if not isinstance(entries, list):
            continue
        kept = []
        for e in entries:
            if isinstance(e, dict):
                if e.get("id") in drop_ids:
                    stats["entries_dropped"] += 1
                    continue
                text = str(e.get("text") or "").lower()
                if any(n in text for n in needles):
                    stats["entries_dropped"] += 1
                    continue
            kept.append(e)
        if len(kept) == len(entries):
            continue
        doc["entries"] = kept
        # PRR-001: reuse the delivery-ledger atomic writer so the scrubbed
        # ledger keeps its 0600-at-open + fsync + chmod contract (a plain
        # write_text + replace regressed the file to umask perms).
        from storelib.delivery_ledger import _atomic_write_json
        _atomic_write_json(str(path), doc)
        stats["files_scrubbed"] += 1
    return stats


def _verify_ledgers_clean(data_dir: Path, needles: list[str]) -> dict[str, int]:
    residue: dict[str, int] = {}
    ops = data_dir / "ops"
    if not ops.is_dir():
        return residue
    for path in ops.glob("*.ledger*"):
        data = path.read_bytes().lower()
        for n in needles:
            c = data.count(n.encode("utf-8"))
            if c:
                residue[str(path.name)] = residue.get(str(path.name), 0) + c
    return residue


# --------------------------------------------------------------------------
# Backup snapshot scrub
# --------------------------------------------------------------------------

def _scrub_snapshot(path: Path, chain: list[str],
                    needles: list[str]) -> dict[str, str]:
    """Apply the SAME remediation inside one snapshot file, in place.

    Old-schema snapshots skip tables they do not have. The scrub never
    schema-migrates a snapshot: purged_id rows are inserted only when the
    table already exists."""
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # Pre-v9 snapshots lack the lineage/dedup columns the resolution
        # reads; fail that snapshot with a named remediation, not a traceback.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
        missing_cols = {"update_of", "merged_from", "content_norm"} - cols
        if missing_cols:
            raise RuntimeError(
                "snapshot %s predates the v9 schema (missing %s); cannot "
                "scrub it in place - restore it to a scratch store, or "
                "delete it and re-run --scrub-backups" % (
                    path.name, ", ".join(sorted(missing_cols))))
        res = _resolve(conn, chain)
        res["missing"] = []  # ids absent from an older snapshot are fine
        res["chain"] = [i for i in res["chain"]
                        if conn.execute("SELECT 1 FROM memory WHERE id=?",
                                        (i,)).fetchone() is not None]
        if not res["chain"]:
            return {"snapshot": path.name, "status": "skipped",
                    "detail": "no purged id present"}
        applied = _apply_purge_transaction(conn, res)
        # A retry may reach a snapshot after the live store no longer has the
        # deleted row, so the caller cannot supply its plaintext needle. The
        # snapshot still has the original row; derive its local fingerprints
        # before closing it rather than accepting a false clean result.
        local_needles = sorted(set(needles) | set(_needles(conn, applied)))
    finally:
        conn.close()

    compact_and_verify(path, local_needles)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if integrity != "ok":
        raise RuntimeError(
            "snapshot %s integrity_check=%s after scrub" % (path.name, integrity))
    data = path.read_bytes().lower()
    residue = sum(data.count(n.encode("utf-8")) for n in local_needles)
    wal = Path(str(path) + "-wal")
    if wal.exists():
        wdata = wal.read_bytes().lower()
        residue += sum(wdata.count(n.encode("utf-8"))
                       for n in local_needles)
    if residue:
        raise RuntimeError(
            "snapshot %s holds %d needle byte(s) after scrub" % (
                path.name, residue))
    return {"snapshot": path.name, "status": "scrubbed",
            "detail": "integrity ok, no residue"}


# --------------------------------------------------------------------------
# CLI entry
# --------------------------------------------------------------------------

def cmd_purge(*, ids: list[str], scrub_backups: bool = False,
              out_dir: str | None = None, as_json: bool = False) -> int:
    """`store.py purge --id <id> [...] [--scrub-backups --out-dir DIR]`.

    Exit ladder: 0 clean; 2 bad usage; 3 unknown id; 4 maintenance, schema,
    backup, consolidate, or live-writer refusal; 5 residue detected; 6
    compaction/scrub failure."""
    from storelib import schema as schema_mod

    if not ids:
        print("[zmem] purge: --id is required (repeatable); pass ids ONLY, "
              "never secret text - hooked sessions record tool input into "
              "evidence rows", file=sys.stderr)
        return 2
    if scrub_backups and not out_dir:
        print("[zmem] purge: --scrub-backups requires --out-dir", file=sys.stderr)
        return 2
    if scrub_backups:
        # PRR-006/F3: validate the backup dir BEFORE any lock or mutation so
        # a UNC/network/OneDrive path is a clean pre-flight refusal (matching
        # cmd_restore's overwrite guard) instead of a post-mutation failure.
        from storelib.schema import _host
        if _host is not None:
            try:
                _host.assert_local_fs(Path(out_dir))
            except ValueError as e:
                print("[zmem] purge REFUSED: %s" % e, file=sys.stderr)
                return 4

    # Full cmd_restore ladder (backup.py:647-701): maintenance -> schema ->
    # backup -> consolidate -> live-writer refusal. Maintenance gates writers
    # at their START only, so the lock + lease + backup/consolidate locks are
    # what makes the byte-verify window exclusive.
    m_token = _strict_acquire_lock(
        "maintenance", MAINTENANCE_LOCK_STALE_SECONDS, wait_seconds=0.0)
    if m_token is None:
        print("[zmem] purge REFUSED: another maintenance operation "
              "(restore, purge) is active; re-run when it finishes",
              file=sys.stderr)
        return 4
    s_token = b_token = c_token = None
    try:
        s_token = _strict_acquire_lock(
            "schema", SCHEMA_LOCK_STALE_SECONDS,
            wait_seconds=SCHEMA_LOCK_WAIT_SECONDS,
            poll_seconds=SCHEMA_LOCK_POLL_SECONDS)
        if s_token is None:
            print("[zmem] purge REFUSED: store initialization or migration "
                  "is running; re-run when it finishes", file=sys.stderr)
            return 4
        b_token = _acquire_lock("backup", BACKUP_LOCK_STALE_SECONDS)
        if b_token is None:
            print("[zmem] purge REFUSED: a backup is currently running - "
                  "re-run when it finishes", file=sys.stderr)
            return 4
        c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
        if c_token is None:
            print("[zmem] purge REFUSED: a consolidation is currently "
                  "running - re-run when it finishes", file=sys.stderr)
            return 4
        live = _cleanup_stale_writer_leases()
        if live:
            print("[zmem] purge REFUSED: a normal writer is currently "
                  "active; re-run when it finishes", file=sys.stderr)
            for lease in live[:5]:
                print("[zmem]   active writer lease: %s" % lease.name,
                      file=sys.stderr)
            return 4

        conn = schema_mod.connect()
        try:
            schema_mod.init_db(conn)
            # PRR-X2: purge may be the very first command run on a store
            # upgraded from pre-0.71 — init_db does not create purged_id
            # (only migrate does), and a silent deny-list no-op here would
            # lose the recording behind a success exit.
            schema_mod._ensure_purged_table(conn)
            try:
                res = _resolve(conn, ids)
            except RuntimeError as e:
                # AC6 refusal: a keeper row still quotes the purged text
                # and can neither be rewritten nor deleted autonomously.
                # Nothing was modified; the named row needs operator action
                # first.
                print("[zmem] %s" % e, file=sys.stderr)
                return 4
            denylisted = _denylisted_ids(conn)
            unknown = [i for i in res["missing"] if i not in denylisted]
            if unknown:
                print("[zmem] purge REFUSED: unknown id(s): %s"
                      % ", ".join(unknown), file=sys.stderr)
                return 3
            retry_ids = [i for i in ids if i in denylisted]
            if res["chain"]:
                try:
                    applied = _apply_purge_transaction(conn, res)
                except (RuntimeError, sqlite3.Error) as e:
                    # A transaction refusal or SQLite failure rolls back all
                    # writes, including the target deletion and entity reaping.
                    print("[zmem] %s" % e, file=sys.stderr)
                    return 4
            else:
                # A prior purge may have committed the row deletion before a
                # later compaction, ledger, or snapshot step failed. Treat a
                # deny-listed id as a scrub retry: there is no transaction to
                # repeat, but the remaining cleanup phases still run.
                applied = {
                    "chain": [], "rows": {}, "keeper_rewrites": [],
                    "head_rebuilds": [], "head_deletes": [],
                    "summary_deletes": [], "successor_updates": [],
                    "deleted_evidence": [], "gone_entities": [],
                    "meta_scrubs": [], "retry_ids": retry_ids,
                }
            needles = _needles(conn, applied)
            applied["retry_ids"] = retry_ids
        finally:
            conn.close()

        try:
            residue, residue_files = compact_and_verify(STORE_PATH, needles)
        except (sqlite3.Error, OSError) as e:
            print("[zmem] purge FAILED: compaction step failed (%s); the "
                  "purge is incomplete - do NOT assume the content is gone"
                  % e, file=sys.stderr)
            return 6
        if any(residue.values()):
            worst = max(residue.items(), key=lambda kv: kv[1])
            print("[zmem] purge FAILED: %s still holds %d byte(s) of purged "
                  "content (needle %r) - the text still lives somewhere "
                  "outside the memory rows; do NOT assume it is gone" % (
                      residue_files.get(worst[0], STORE_PATH.name),
                      worst[1], worst[0]), file=sys.stderr)
            print("[zmem] purge: the purged ids ARE recorded in the "
                  "purged_id deny-list; after cleaning up the remaining "
                  "surface, clear those rows from purged_id (or re-run "
                  "purge on the surviving carrier) so peer imports stay "
                  "denied only while content exists", file=sys.stderr)
            return 5

        data_dir = STORE_PATH.parent
        drop_ids = set(applied["chain"])
        drop_ids |= {sd["summary_id"] for sd in applied["summary_deletes"]}
        drop_ids |= set(applied.get("retry_ids", []))
        drop_ids |= denylisted
        try:
            # Round-3: the passive lane's ledger.record holds NO writer lease,
            # so it is invisible to the live-writer refusal -- but record()
            # fails OPEN on _delivery_state_lock contention, so holding that
            # lock here makes any concurrent delivery write drop harmlessly
            # instead of re-recording needle text after the scrub/scan.
            from storelib import delivery_ledger as _dl
            led: dict[str, int] = {"files_scrubbed": 0, "entries_dropped": 0,
                               "tmp_removed": 0}
            lres: dict[str, int] = {}
            with _dl._delivery_state_lock(str(data_dir)):
                led = scrub_ledgers(data_dir, drop_ids, needles)
                lres = _verify_ledgers_clean(data_dir, needles)
        except (OSError, ValueError, RuntimeError) as e:
            print("[zmem] purge FAILED: ledger scrub failed (%s); the purge "
                  "is incomplete - inspect <data>/ops before re-running" % e,
                  file=sys.stderr)
            return 5
        if lres:
            print("[zmem] purge FAILED: ledger residue remains: %s" % ", "
                  .join("%s=%d" % kv for kv in sorted(lres.items())),
                  file=sys.stderr)
            return 5

        snapshots: list[dict[str, str]] = []
        if scrub_backups:
            bdir = Path(out_dir)
            try:
                snapshot_targets = list(dict.fromkeys(
                    list(applied["chain"])
                    + list(applied.get("retry_ids", []))))
                for snap in sorted(list(bdir.glob("store-*.sqlite"))
                                   + list(bdir.glob("prerestore-*.sqlite"))):
                    snapshots.append(
                        _scrub_snapshot(snap, snapshot_targets, needles))
            except (sqlite3.Error, OSError, RuntimeError) as e:
                print("[zmem] purge FAILED: backup scrub failed (%s)"
                      % e, file=sys.stderr)
                return 6

        # AC2-vs-AC3 tension made visible: survivors that still carry
        # purged-content tokens (mid-chain successors) are by design kept.
        warnings: list[str] = []
        for s in applied["successor_updates"]:
            if s["id"] in set(applied["chain"]):
                continue
            if _tokens(s.get("content") or "") & set(needles):
                warnings.append("row " + s["id"])
        # Round-3: sanctioned survivors outside the memory table (kept
        # evidence excerpts, kept entity/alias names) may carry the needle by
        # design (AC4/AC5 negative controls); say so instead of silence.
        conn2 = schema_mod.connect()
        try:
            needle_tok = set()
            for n in needles:
                needle_tok |= _tokens(n)
            for r in conn2.execute("SELECT id, excerpt FROM evidence"):
                if _tokens(r["excerpt"] or "") & needle_tok:
                    warnings.append("evidence " + r["id"])
            for r in conn2.execute("SELECT id, canonical_name FROM entity"):
                if _tokens(r["canonical_name"] or "") & needle_tok:
                    warnings.append("entity " + r["id"])
        except sqlite3.Error:
            pass
        finally:
            conn2.close()

        # A scrub retry has no live rows, so no live needles can be derived
        # and the byte-verify below would be vacuous — report that honestly
        # instead of printing a clean-verify claim (review round 5, probe A).
        is_scrub_retry = bool(retry_ids) and not applied["chain"]
        payload = {
            "result": "scrub-retry" if is_scrub_retry else "purged",
            "purged": applied["chain"],
            "rewritten_keepers": [k[0] for k in applied["keeper_rewrites"]],
            "rebuilt_heads": applied["head_rebuilds"],
            "deleted_heads": applied["head_deletes"],
            "deleted_summaries": [sd["summary_id"]
                                  for sd in applied["summary_deletes"]],
            "cleared_successors": [s["id"] for s in applied["successor_updates"]],
            "deleted_evidence": applied["deleted_evidence"],
            "gone_entities": applied["gone_entities"],
            "meta_scrubs": len(applied["meta_scrubs"]),
            "ledger": led,
            "snapshots": snapshots,
            "survivors_still_quoting": warnings,
        }
        if is_scrub_retry:
            payload["scrub_retry_ids"] = retry_ids
            payload["live_byte_verify"] = (
                "skipped: purged rows absent, no live needles derivable")
        elif retry_ids:
            # Mixed batch: fresh ids were purged, deny-listed ids rode along.
            # Their residue is not re-scannable here (no needles derivable),
            # so scope the clean claim (review round 5, residual edge).
            payload["scrub_retry_ids"] = retry_ids
            payload["scrub_retry_note"] = (
                "deny-listed id(s) rode along; their live residue is not "
                "re-scanned in this run")
        if as_json:
            print(json.dumps(payload, indent=2))
        else:
            if is_scrub_retry:
                print("[zmem] purge: scrub retry for deny-listed id(s): %s"
                      % ", ".join(retry_ids))
                print("[zmem] purge: recovery phases completed; live "
                      "byte-verify NOT re-run (rows absent, no live needles "
                      "derivable; --scrub-backups verifies each snapshot "
                      "against its own copy of the row)")
            else:
                print("[zmem] purge: removed %d row(s): %s" % (
                    len(applied["chain"]), ", ".join(applied["chain"])))
                print("[zmem] purge: store compacted and byte-verified clean")
                if retry_ids:
                    print("[zmem] purge: note: deny-listed id(s) %s rode "
                          "along; their live residue is not re-scanned in "
                          "this run" % ", ".join(retry_ids))
            if led["entries_dropped"]:
                print("[zmem] purge: scrubbed %d ledger entr%s across %d "
                      "file(s)" % (led["entries_dropped"],
                                   "y" if led["entries_dropped"] == 1 else "ies",
                                   led["files_scrubbed"]))
            for s in snapshots:
                print("[zmem] purge: %s %s (%s)" % (
                    s["status"], s["snapshot"], s["detail"]))
            for w in warnings:
                print("[zmem] purge: WARNING - surviving %s still quotes "
                      "purged-content tokens (kept by design; remove it too "
                      "if that is not intended)" % w)
        return 0
    finally:
        if c_token is not None:
            _release_lock("consolidate", c_token)
        if b_token is not None:
            _release_lock("backup", b_token)
        if s_token is not None:
            _release_named_lock("schema", s_token)
        _release_named_lock("maintenance", m_token)
