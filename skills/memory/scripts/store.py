#!/usr/bin/env python
"""ZMem store — Tier 2 semantic memory for ZCode.

A local-first, FTS5-backed, tombstone-supersession memory store. Operated via
subcommands so it can be called from the memory SKILL and from hook scripts.

Path resolution (in priority order):
  1. ZMEM_STORE env var (explicit override; hooks set this)
  2. ${ZCODE_PLUGIN_DATA}/store.sqlite (plugin data dir, when running as a plugin)
  3. ~/.zcode/memory/store.sqlite (legacy/manual install fallback)

Usage:
  python store.py init
  python store.py add --namespace NS --type T --content "..." [--tags a,b] \\
         [--source-ref REF] [--confidence 0.8] [--signal test|compile|lint|reviewer|user|none]
  python store.py recall --query "..." [--namespace NS] [--limit 5] [--json]
  python store.py recent [--namespace NS] [--limit 5] [--min-confidence 0.5] [--json]
  python store.py search --text "..." [--namespace NS] [--limit 10]
  python store.py supersede --id <id> [--reason "..."]
  python store.py get --id <id>
  python store.py list [--namespace NS] [--limit 50] [--include-superseded]
  python store.py stats

Design (see the memory skill's design doc):
  - Tombstone supersession (superseded_at), NOT full bi-temporal (YAGNI for single user).
  - Signal tiers set default confidence: test/compile/lint=0.9, reviewer/user=0.6, none=0.3.
  - Advisory secret filter (regex + entropy) — logs a warning, does NOT block writes.
  - Dedup-on-write: if a near-identical live memory exists in the same namespace,
    refresh its last_retrieved instead of inserting a duplicate.
  - Source-staleness: source_hash stored for mutable markdown refs; checked on recall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
import calendar
from pathlib import Path


def _resolve_store_path() -> Path:
    """Resolve the store location. Priority: ZMEM_STORE > ZCODE_PLUGIN_DATA > ~/.zcode/memory."""
    explicit = os.environ.get("ZMEM_STORE")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "store.sqlite"
    return Path(os.path.expanduser("~/.zcode/memory/store.sqlite"))


def _resolve_core_md_path() -> Path:
    """Resolve the Tier 0 core.md location. Priority: ZMEM_CORE_MD > ZCODE_PLUGIN_DATA > ~/.zcode/memory."""
    explicit = os.environ.get("ZMEM_CORE_MD")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "core.md"
    return Path(os.path.expanduser("~/.zcode/memory/core.md"))


STORE_PATH = _resolve_store_path()
CORE_MD_PATH = _resolve_core_md_path()

SIGNAL_CONFIDENCE = {
    "test": 0.9,
    "compile": 0.9,
    "lint": 0.85,
    "reviewer": 0.6,
    "user": 0.6,
    "none": 0.3,
}

CONFIDENCE_FLOOR = 0.25

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect() -> sqlite3.Connection:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STORE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create schema if absent. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id              TEXT PRIMARY KEY,
            namespace       TEXT NOT NULL,
            type            TEXT NOT NULL,
            content         TEXT NOT NULL,
            tags            TEXT NOT NULL DEFAULT '',
            source_ref      TEXT NOT NULL DEFAULT '',
            source_hash     TEXT NOT NULL DEFAULT '',
            confidence      REAL NOT NULL DEFAULT 0.5,
            signal          TEXT NOT NULL DEFAULT 'none',
            valid_from      TEXT NOT NULL DEFAULT '',
            superseded_at   TEXT,
            ingestion_ts    TEXT NOT NULL,
            retrieval_count INTEGER NOT NULL DEFAULT 0,
            last_retrieved  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_namespace ON memory(namespace);
        CREATE INDEX IF NOT EXISTS idx_memory_live ON memory(superseded_at) WHERE superseded_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type);

        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content, tags, namespace,
            content='memory', content_rowid='rowid',
            tokenize='unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
            INSERT INTO memory_fts(rowid, content, tags, namespace)
            VALUES (new.rowid, new.content, new.tags, new.namespace);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
            VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
            VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
            INSERT INTO memory_fts(rowid, content, tags, namespace)
            VALUES (new.rowid, new.content, new.tags, new.namespace);
        END;

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
        """
    )
    # executescript() does not accept parameter binding, so set created_at separately.
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_iso(),))
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Versioned migration. Runs after init_db(). Idempotent and crash-safe.

    Each version block is guarded by a version check so it runs exactly once.
    DDL statements use IF (NOT) EXISTS so they are safe to repeat if a crash
    interrupts the migration before the version bump. The busy_timeout set in
    connect() serializes concurrent hook processes; the version guard makes
    the second one a no-op once the first commits.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    ver = int(row[0]) if row else 1

    if ver < 2:
        # v2: ranking-support indexes + FTS trigger fix (stop write amplification).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_confidence ON memory(confidence)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_ingestion ON memory(ingestion_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_retrieval ON memory(retrieval_count)")
        # Replace the unguarded UPDATE trigger with one that only fires when
        # FTS-indexed columns (content, tags, namespace) actually change.
        # Without this, every telemetry UPDATE (retrieval_count bump on recall)
        # triggers a full FTS delete+reinsert of that row.
        conn.execute("DROP TRIGGER IF EXISTS memory_au")
        conn.execute(
            """
            CREATE TRIGGER memory_au AFTER UPDATE ON memory
            WHEN old.content IS NOT new.content
              OR old.tags IS NOT new.tags
              OR old.namespace IS NOT new.namespace
            BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, content, tags, namespace)
                VALUES ('delete', old.rowid, old.content, old.tags, old.namespace);
                INSERT INTO memory_fts(rowid, content, tags, namespace)
                VALUES (new.rowid, new.content, new.tags, new.namespace);
            END
            """
        )
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        conn.commit()


def _check_secrets(content: str, source_ref: str) -> list[str]:
    """Advisory only. Returns list of warnings. Never blocks. Scans both content
    and source_ref (a token in a file path would otherwise slip through)."""
    warnings = []
    combined = content + " " + source_ref
    for pat in SECRET_PATTERNS:
        m = pat.search(combined)
        if m:
            warnings.append(f"possible secret-like text matched pattern {pat.pattern[:40]!r}: {m.group(0)[:20]!r}...")
    return warnings


def _to_win_path(p: str) -> str:
    """Normalize a Cygwin path (/c/..., /tmp/..., /home/...) to Windows form so
    Windows Python can open it. Mirrors to_win_path() in the hook scripts.

    Tries `cygpath -w` first (handles all Cygwin mounts); falls back to a regex
    for /<drive>/ paths (single backslash). If neither applies, returns p unchanged.
    """
    if not p or not p.startswith("/"):
        return p
    # Prefer cygpath when available (Git Bash / Cygwin) — it knows all mounts.
    try:
        import subprocess
        out = subprocess.run(
            ["cygpath", "-w", p], capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # Regex fallback for /<drive>/... paths. Single backslash, not doubled.
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1)}:" + "\\" + m.group(2).replace("/", "\\")
    return p


def _source_hash(source_ref: str) -> str:
    """Hash the current content of a mutable markdown source_ref, for staleness checks.

    source_ref format: 'file:<path>' or 'task:<slug>:<file>' or 'db:<table>:<rowid>'.
    Only 'file:' refs are hashed (mutable). db: refs are immutable (episodic). Others: ''.

    Handles Cygwin-style paths (/c/Users/...) by normalizing to Windows format,
    since Windows Python cannot open /c/... paths. If a file: ref cannot be opened
    even after normalization, emits a stderr warning so the staleness feature
    fails LOUD (visible) instead of silent (a no-op that looks like it works).
    """
    if not source_ref.startswith("file:"):
        return ""
    raw = source_ref[5:]
    p = Path(_to_win_path(raw))
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        print(f"[zmem] WARNING: could not read source_ref for staleness hash: {raw} "
              f"(staleness detection disabled for this memory)", file=sys.stderr)
        return ""


def add_memory(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    type_: str,
    content: str,
    tags: str = "",
    source_ref: str = "",
    confidence: float | None = None,
    signal: str = "none",
    valid_from: str = "",
) -> str:
    warns = _check_secrets(content, source_ref)
    for w in warns:
        print(f"[zmem] WARNING (advisory, write proceeded): {w}", file=sys.stderr)

    if confidence is None:
        confidence = SIGNAL_CONFIDENCE.get(signal, 0.3)

    # Dedup-on-write: fetch candidates (same namespace, live) and normalize in Python
    # for exact match. Normalization must be identical between the query and the check
    # (collapse all whitespace runs to a single space and lowercase).
    norm = re.sub(r"\s+", " ", content.strip().lower())
    candidates = conn.execute(
        "SELECT id, content FROM memory WHERE namespace=? AND superseded_at IS NULL",
        (namespace,),
    ).fetchall()
    existing = None
    for c in candidates:
        c_norm = re.sub(r"\s+", " ", c["content"].strip().lower())
        if c_norm == norm:
            existing = c
            break
    if existing:
        conn.execute(
            "UPDATE memory SET last_retrieved=?, retrieval_count=retrieval_count+1 WHERE id=?",
            (now_iso(), existing["id"]),
        )
        conn.commit()
        print(f"[zmem] dedup: existing memory {existing['id']} refreshed (no duplicate inserted)")
        return existing["id"]

    mid = str(uuid.uuid4())
    shash = _source_hash(source_ref)
    ts = now_iso()
    if not valid_from:
        valid_from = ts
    conn.execute(
        """INSERT INTO memory
           (id, namespace, type, content, tags, source_ref, source_hash,
            confidence, signal, valid_from, superseded_at, ingestion_ts,
            retrieval_count, last_retrieved)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,0,?)""",
        (mid, namespace, type_, content, tags, source_ref, shash,
         confidence, signal, valid_from, ts, ts),
    )
    conn.commit()
    print(f"[zmem] added memory {mid} (ns={namespace}, type={type_}, signal={signal}, conf={confidence})")
    return mid


# --- Ranking formula weights (composite score for recall) ---
# BM25 relevance dominates; confidence/recency/popularity are tiebreakers/boosts.
# These are intentionally simple linear weights — the goal is to turn dead
# telemetry into a signal, not to over-engineer a learning-to-rank system.
W_BM25 = 0.55
W_CONFIDENCE = 0.20
W_RECENCY = 0.15
W_POPULARITY = 0.10
# Recency half-life: a memory from RECENCY_HALF_LIFE_DAYS ago contributes half.
RECENCY_HALF_LIFE_DAYS = 90


def _parse_iso_to_epoch(ts: str) -> float:
    """Parse an ISO-8601 UTC timestamp to epoch seconds. Returns 0 on failure."""
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def compute_score(row: sqlite3.Row | dict, fts_rank: float | None, now_epoch: float) -> float:
    """Composite score: BM25 relevance + confidence boost + recency + popularity.

    fts_rank is the raw FTS5 rank value (lower = better match). All other
    factors come from the memory row itself.
    """
    # BM25 component: FTS5 rank is the negated BM25 value (more negative = better
    # match). Normalize to 0..1 where better matches get higher scores.
    # abs(rank)/(1+abs(rank)) is monotonically increasing in abs(rank).
    if fts_rank is not None:
        ar = abs(fts_rank)
        bm25_score = ar / (1.0 + ar)
    else:
        bm25_score = 0.0

    # Confidence component: already 0..1.
    confidence = float(row["confidence"]) if row["confidence"] is not None else 0.3

    # Recency component: exponential decay from ingestion_ts.
    ingested = _parse_iso_to_epoch(row["ingestion_ts"] or "")
    if ingested > 0 and now_epoch > 0:
        age_days = max(0.0, (now_epoch - ingested) / 86400.0)
        recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    else:
        recency = 0.5  # unknown age — neutral

    # Popularity component: retrieval_count with diminishing returns.
    rc = int(row["retrieval_count"]) if row["retrieval_count"] is not None else 0
    popularity = min(1.0, 0.15 * (rc ** 0.5))

    return (
        W_BM25 * bm25_score
        + W_CONFIDENCE * confidence
        + W_RECENCY * recency
        + W_POPULARITY * popularity
    )


def recall_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    namespace: str | None = None,
    limit: int = 5,
    as_json: bool = False,
    min_confidence: float | None = None,
) -> list[dict]:
    """FTS5 keyword recall with composite ranking.

    Candidates are fetched via FTS5 BM25, then re-ranked by a composite score
    that incorporates BM25 relevance, confidence, recency decay, and retrieval
    popularity. This turns the telemetry data (retrieval_count, last_retrieved)
    into a living ranking signal instead of dead data.

    Confidence is still a hard floor (high-precision-first principle): memories
    below CONFIDENCE_FLOOR (or min_confidence) are dropped before scoring.
    """
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
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
        if namespace:
            ns_clause = "AND m.namespace = ?"
            params.append(namespace)
        floor = min_confidence if min_confidence is not None else CONFIDENCE_FLOOR
        params.append(floor)
        # Fetch more candidates than the limit so the composite re-ranking has
        # a larger pool to choose from (BM25 rank != final rank).
        fetch_limit = max(limit * 3, limit + 5)
        params.append(fetch_limit)
        sql = f"""
            SELECT m.id, m.namespace, m.type, m.content, m.tags, m.source_ref,
                   m.source_hash, m.confidence, m.signal, m.valid_from,
                   m.ingestion_ts, m.retrieval_count, m.last_retrieved,
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

    # Re-rank by composite score (BM25 + confidence + recency + popularity).
    now_epoch = time.time()
    scored = []
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
        score = compute_score(row_fields, r["fts_rank"], now_epoch)
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
            "_stale_note": stale_note,
            "_score": round(score, 4),
        }))

    # Sort by composite score descending, take top `limit`.
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item[1] for item in scored[:limit]]

    if results:
        ids = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memory SET retrieval_count=retrieval_count+1, last_retrieved=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
        conn.commit()

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("[zmem] no matching memories found.")
        for r in results:
            print(f"--- [{r['id']}] (conf={r['confidence']}, signal={r['signal']}, "
                  f"ns={r['namespace']}, type={r['type']}){r['_stale_note']}")
            print(f"    {r['content']}")
            if r["tags"]:
                print(f"    tags: {r['tags']}")
    return results


def recent_memory(
    conn: sqlite3.Connection,
    *,
    namespace: str | None = None,
    limit: int = 5,
    min_confidence: float = 0.5,
    as_json: bool = False,
) -> list[dict]:
    """Cheap admin pull of the most recent live memories (no FTS scoring)."""
    params: list = [min_confidence]
    ns_clause = ""
    if namespace:
        ns_clause = "AND namespace = ?"
        params.append(namespace)
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
            "_stale_note": stale_note,
        })
    if results:
        ids = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memory SET retrieval_count=retrieval_count+1, last_retrieved=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
        conn.commit()
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("[zmem] no recent memories.")
        for r in results:
            print(f"--- [{r['id']}] (conf={r['confidence']}, signal={r['signal']}, "
                  f"ns={r['namespace']}, type={r['type']}){r['_stale_note']}")
            print(f"    {r['content']}")
            if r["tags"]:
                print(f"    tags: {r['tags']}")
    return results


def supersede_memory(conn: sqlite3.Connection, mid: str, reason: str = "") -> bool:
    """Tombstone a memory (mark superseded_at). Does not delete — keeps history."""
    row = conn.execute("SELECT id FROM memory WHERE id=?", (mid,)).fetchone()
    if not row:
        print(f"[zmem] no memory with id {mid}", file=sys.stderr)
        return False
    conn.execute("UPDATE memory SET superseded_at=? WHERE id=?", (now_iso(), mid))
    conn.commit()
    note = f": {reason}" if reason else ""
    print(f"[zmem] superseded {mid}{note}")
    return True


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
        f"signal, superseded_at FROM memory {where} ORDER BY ingestion_ts DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        print("[zmem] (no memories)")
    for r in rows:
        status = "SUPERSEDED" if r["superseded_at"] else "live"
        print(f"[{r['id']}] {status} ns={r['namespace']} type={r['type']} "
              f"conf={r['confidence']} sig={r['signal']} :: {r['preview']}")


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


def get_memory(conn, mid):
    r = conn.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone()
    if not r:
        print(f"[zmem] no memory with id {mid}", file=sys.stderr)
        return
    d = dict(r)
    print(json.dumps(d, indent=2))


def main():
    ap = argparse.ArgumentParser(prog="store.py", description="ZMem semantic store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store if absent (idempotent)")

    p_add = sub.add_parser("add", help="add a memory")
    p_add.add_argument("--namespace", required=True)
    p_add.add_argument("--type", required=True, choices=["fact", "lesson", "convention", "preference"])
    p_add.add_argument("--content", required=True)
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--source-ref", default="")
    p_add.add_argument("--confidence", type=float, default=None)
    p_add.add_argument("--signal", default="none",
                       choices=["test", "compile", "lint", "reviewer", "user", "none"])

    p_recall = sub.add_parser("recall", help="recall relevant memories")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--namespace", default=None)
    p_recall.add_argument("--limit", type=int, default=5)
    p_recall.add_argument("--json", action="store_true")

    p_recent = sub.add_parser("recent", help="most recent live memories (no FTS, admin pull)")
    p_recent.add_argument("--namespace", default=None)
    p_recent.add_argument("--limit", type=int, default=5)
    p_recent.add_argument("--min-confidence", type=float, default=0.5)
    p_recent.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search", help="keyword search (no confidence floor)")
    p_search.add_argument("--text", required=True)
    p_search.add_argument("--namespace", default=None)
    p_search.add_argument("--limit", type=int, default=10)

    p_sup = sub.add_parser("supersede", help="tombstone a memory")
    p_sup.add_argument("--id", required=True)
    p_sup.add_argument("--reason", default="")

    p_get = sub.add_parser("get", help="show a memory by id")
    p_get.add_argument("--id", required=True)

    p_list = sub.add_parser("list", help="list memories")
    p_list.add_argument("--namespace", default=None)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--include-superseded", action="store_true")

    sub.add_parser("stats", help="store statistics")

    sub.add_parser("rebuild-fts", help="rebuild the FTS5 index from scratch")

    args = ap.parse_args()
    conn = connect()
    init_db(conn)
    migrate(conn)

    if args.cmd == "init":
        print(f"[zmem] store ready at {STORE_PATH}")
    elif args.cmd == "add":
        add_memory(
            conn,
            namespace=args.namespace,
            type_=args.type,
            content=args.content,
            tags=args.tags,
            source_ref=args.source_ref,
            confidence=args.confidence,
            signal=args.signal,
        )
    elif args.cmd == "recall":
        recall_memory(conn, query=args.query, namespace=args.namespace, limit=args.limit, as_json=args.json)
    elif args.cmd == "recent":
        recent_memory(conn, namespace=args.namespace, limit=args.limit,
                      min_confidence=args.min_confidence, as_json=args.json)
    elif args.cmd == "search":
        recall_memory(conn, query=args.text, namespace=args.namespace, limit=args.limit, as_json=False, min_confidence=0.0)
    elif args.cmd == "supersede":
        ok = supersede_memory(conn, args.id, args.reason)
        sys.exit(0 if ok else 1)
    elif args.cmd == "get":
        get_memory(conn, args.id)
    elif args.cmd == "list":
        list_memory(conn, namespace=args.namespace, limit=args.limit, include_superseded=args.include_superseded)
    elif args.cmd == "stats":
        stats(conn)
    elif args.cmd == "rebuild-fts":
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        conn.commit()
        print("[zmem] FTS5 index rebuilt")
    conn.close()


if __name__ == "__main__":
    main()
