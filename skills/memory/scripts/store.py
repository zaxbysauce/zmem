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
import struct
from pathlib import Path

# Optional embedding support (degrades gracefully to FTS5-only if unavailable).
try:
    import embeddings as _embeddings
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import embeddings as _embeddings
    except ImportError:
        _embeddings = None


def _resolve_store_path() -> Path:
    """Resolve the store location.

    Priority: ZMEM_STORE > ZCODE_PLUGIN_DATA > auto-detect plugin data dir > ~/.zcode/memory.
    The auto-detect prevents store-splitting when env vars aren't set (e.g.
    when store.py is invoked from a slash command that doesn't inherit hook env).
    """
    explicit = os.environ.get("ZMEM_STORE")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "store.sqlite"
    # Auto-detect the plugin data dir (the canonical location since v2).
    # This prevents sessions without ZCODE_PLUGIN_DATA from writing to the
    # legacy ~/.zcode/memory/ location and splitting the store.
    home = Path(os.path.expanduser("~"))
    plugin_data_pattern = home / ".zcode" / "cli" / "plugins" / "data"
    if plugin_data_pattern.is_dir():
        for d in plugin_data_pattern.iterdir():
            if "zmem" in d.name.lower():
                return d / "store.sqlite"
    return home / ".zcode" / "memory" / "store.sqlite"


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
    # Load sqlite-vec extension for vector search. Failures are non-fatal —
    # the system degrades to FTS5-only recall when vec0 is unavailable.
    try:
        _load_vec(conn)
        # Ensure the vec0 table exists whenever vec loads (handles the case
        # where sqlite_vec was absent during the v3 migration but installed later).
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
            "embedding float[384] distance_metric=cosine, memory_id TEXT"
            ")"
        )
    except Exception:
        pass
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

    if ver < 3:
        # v3: embedding columns + sqlite-vec virtual table for hybrid recall.
        # The vec0 table stores 384-dim float vectors keyed by memory_id.
        # Embeddings are optional — if onnxruntime/model is missing, the
        # embedding column stays NULL and recall degrades to FTS5-only.
        conn.execute("ALTER TABLE memory ADD COLUMN embedding BLOB")
        conn.execute("ALTER TABLE memory ADD COLUMN embedding_model TEXT DEFAULT ''")
        conn.execute("ALTER TABLE memory ADD COLUMN embedded_at TEXT")

        # Try to create the vec0 virtual table. This requires sqlite-vec
        # to be loaded; if it fails, embedding features are disabled but
        # the rest of the system continues to work (FTS5-only recall).
        try:
            _load_vec(conn)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
                "embedding float[384] distance_metric=cosine, memory_id TEXT"
                ")"
            )
        except Exception:
            pass  # sqlite-vec not available — embeddings disabled

        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        conn.commit()

    if ver < 4:
        # v4: consolidation provenance + supersede reason persistence.
        conn.execute("ALTER TABLE memory ADD COLUMN consolidated_at TEXT")
        conn.execute("ALTER TABLE memory ADD COLUMN supersede_reason TEXT DEFAULT ''")
        conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        conn.commit()


def _load_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension. Raises if unavailable."""
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)


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

    # Dedup-on-write: semantic similarity (if embeddings available) or exact
    # match fallback. Semantic dedup catches paraphrases the exact-match miss.
    emb = None
    if _embeddings and _embeddings.is_available():
        emb = _embeddings.embed_text(content)

    existing = None
    dedup_sim = 0.0
    if emb is not None:
        # Semantic dedup: query vec0 for nearest neighbor in the same namespace.
        existing, dedup_sim = _find_semantic_duplicate(conn, emb, namespace)
    if existing is None:
        # Fallback: exact-match dedup (original logic).
        norm = re.sub(r"\s+", " ", content.strip().lower())
        candidates = conn.execute(
            "SELECT id, content FROM memory WHERE namespace=? AND superseded_at IS NULL",
            (namespace,),
        ).fetchall()
        for c in candidates:
            c_norm = re.sub(r"\s+", " ", c["content"].strip().lower())
            if c_norm == norm:
                existing = c
                dedup_sim = 1.0  # exact match
                break

    if existing:
        # Merge: upgrade confidence/signal if the new add is stronger.
        _merge_on_dedup(conn, existing["id"], confidence, signal, tags)
        conn.commit()
        print(f"[zmem] dedup: existing memory {existing['id']} refreshed "
              f"(similarity={dedup_sim:.3f}, threshold={DEDUP_SIMILARITY_THRESHOLD})")
        return existing["id"]

    mid = str(uuid.uuid4())
    shash = _source_hash(source_ref)
    ts = now_iso()
    if not valid_from:
        valid_from = ts

    # Determine embedding model name for the embedding_model column.
    emb_model = "minilm-onnx" if emb is not None else ""

    conn.execute(
        """INSERT INTO memory
           (id, namespace, type, content, tags, source_ref, source_hash,
            confidence, signal, valid_from, superseded_at, ingestion_ts,
            retrieval_count, last_retrieved, embedding, embedding_model, embedded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,0,?,?,?,?)""",
        (mid, namespace, type_, content, tags, source_ref, shash,
         confidence, signal, valid_from, ts, ts, emb, emb_model,
         ts if emb is not None else None),
    )
    # Insert into vec0 table if we have an embedding.
    if emb is not None:
        try:
            conn.execute(
                "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                [emb, mid],
            )
        except sqlite3.OperationalError:
            pass  # vec0 table not available — embedding stored in memory table only
    conn.commit()
    print(f"[zmem] added memory {mid} (ns={namespace}, type={type_}, signal={signal}, conf={confidence}"
          f"{', embedded' if emb is not None else ''})")
    return mid


# Cosine similarity threshold for semantic dedup (0..1, higher = stricter).
# Override via ZMEM_DEDUP_THRESHOLD env var if false-positive merges occur.
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("ZMEM_DEDUP_THRESHOLD", "0.85"))
# Signal rank for merge: higher = stronger.
_SIGNAL_RANK = {"test": 5, "compile": 4, "lint": 3, "reviewer": 2, "user": 2, "none": 1}


def _find_semantic_duplicate(
    conn: sqlite3.Connection, embedding: bytes, namespace: str, threshold: float = DEDUP_SIMILARITY_THRESHOLD
) -> sqlite3.Row | None:
    """Find the closest existing memory by embedding cosine similarity."""
    try:
        results = conn.execute(
            "SELECT memory_id, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = 5 ORDER BY distance",
            [embedding],
        ).fetchall()
    except sqlite3.OperationalError:
        return None  # vec0 table not available

    for r in results:
        row = conn.execute(
            "SELECT id, confidence, signal, tags FROM memory "
            "WHERE id=? AND superseded_at IS NULL AND namespace=?",
            (r["memory_id"], namespace),
        ).fetchone()
        if row:
            # sqlite-vec distance is cosine distance (0 = identical, 2 = opposite).
            # Convert to cosine similarity: sim = 1 - distance.
            similarity = 1.0 - r["distance"]
            if similarity >= threshold:
                return row, similarity
    return None, 0.0


def _merge_on_dedup(
    conn: sqlite3.Connection, mid: str, new_confidence: float, new_signal: str, new_tags: str
) -> None:
    """Merge a re-observed memory: upgrade confidence/signal/tags if stronger."""
    row = conn.execute(
        "SELECT confidence, signal, tags FROM memory WHERE id=?", (mid,)
    ).fetchone()
    if not row:
        return

    # Take the higher confidence.
    merged_conf = max(row["confidence"], new_confidence)

    # Upgrade signal if the new one is stronger.
    old_rank = _SIGNAL_RANK.get(row["signal"], 1)
    new_rank = _SIGNAL_RANK.get(new_signal, 1)
    merged_signal = new_signal if new_rank > old_rank else row["signal"]

    # Union the tags.
    old_tags = set(t.strip() for t in row["tags"].split(",") if t.strip())
    new_tags_set = set(t.strip() for t in new_tags.split(",") if t.strip())
    merged_tags = ",".join(sorted(old_tags | new_tags_set))

    conn.execute(
        "UPDATE memory SET confidence=?, signal=?, tags=?, "
        "last_retrieved=?, retrieval_count=retrieval_count+1 WHERE id=?",
        (merged_conf, merged_signal, merged_tags, now_iso(), mid),
    )


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

    # Popularity component: retrieval_count with diminishing returns.
    rc = int(row["retrieval_count"]) if row["retrieval_count"] is not None else 0
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


def _fetch_by_ids(
    conn: sqlite3.Connection, ids: list[str], namespace: str | None, floor: float
) -> list:
    """Fetch full memory rows for a list of IDs, applying the same filters as recall."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    ns_clause = "AND namespace = ?" if namespace else ""
    params = list(ids)
    if namespace:
        params.append(namespace)
    params.append(floor)
    sql = f"""
        SELECT id, namespace, type, content, tags, source_ref,
               source_hash, confidence, signal, valid_from,
               ingestion_ts, retrieval_count, last_retrieved,
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


def recall_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    namespace: str | None = None,
    limit: int = 5,
    as_json: bool = False,
    min_confidence: float | None = None,
    hybrid: bool = False,
) -> list[dict]:
    """FTS5 keyword recall with composite ranking + optional hybrid RRF fusion.

    Candidates are fetched via FTS5 BM25, then re-ranked by a composite score
    that incorporates BM25 relevance, confidence, recency decay, and retrieval
    popularity. If hybrid=True and embeddings are available, candidates are also
    fetched via vector KNN and fused via Reciprocal Rank Fusion (RRF) before the
    composite re-ranking.

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
            # Get vec results WITH distances for the similarity map.
            try:
                vec_results = conn.execute(
                    "SELECT memory_id, distance FROM memory_vec "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    [query_emb, max(limit * 3, limit + 5)],
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
                rows = _fetch_by_ids(conn, fused_ids, namespace, floor)

    # Re-rank by composite score (relevance + confidence + recency + popularity).
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
    conn.execute("UPDATE memory SET superseded_at=?, supersede_reason=? WHERE id=?", (now_iso(), reason, mid))
    # Also remove from the vec0 table to prevent orphaned vectors consuming KNN slots.
    try:
        conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
    except sqlite3.OperationalError:
        pass  # vec0 table not available
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


# --- Skill promotion (T3.1) ---
PROMOTE_CONFIDENCE_FLOOR = 0.85
PROMOTE_RETRIEVAL_FLOOR = 3
PROMOTE_SIGNALS = ("test", "compile", "lint")


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


def promote_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str | None = None,
    dry_run: bool = False,
    namespace: str | None = None,
) -> None:
    """Promote high-confidence lessons to reusable SKILL.md files.

    Candidates: type=lesson, signal in (test/compile/lint), confidence>=0.85,
    retrieval_count>3, not superseded. Does NOT supersede the source lesson —
    the lesson and the skill coexist (the lesson costs ~200 bytes; if the skill
    description fails to trigger, the lesson is still in recall).

    Human-in-the-loop: --dry-run shows candidates; --id <uuid> --confirm writes
    the SKILL.md after the user reviews the description.
    """
    # Candidate query.
    ns_clause = "AND namespace = ?" if namespace else ""
    ns_params = [namespace] if namespace else []
    candidates = conn.execute(
        f"""SELECT id, namespace, type, content, tags, confidence, signal,
                  retrieval_count, valid_from
           FROM memory
           WHERE superseded_at IS NULL
             AND type = 'lesson'
             AND signal IN ('test', 'compile', 'lint')
             AND confidence >= ?
             AND retrieval_count > ?
             {ns_clause}
           ORDER BY retrieval_count DESC, confidence DESC""",
        [PROMOTE_CONFIDENCE_FLOOR, PROMOTE_RETRIEVAL_FLOOR] + ns_params,
    ).fetchall()

    if not candidates:
        print("[zmem] no promotion candidates found")
        return

    if dry_run:
        print(f"[zmem] {len(candidates)} promotion candidate(s):")
        for c in candidates:
            skill_name = _slugify_skill_name(c["tags"], c["id"])
            print(f"\n  [{c['id'][:8]}] (rc={c['retrieval_count']}, conf={c['confidence']}, "
                  f"signal={c['signal']})")
            print(f"    content: {c['content'][:80]}...")
            print(f"    tags: {c['tags']}")
            print(f"    would create: ~/.zcode/skills/{skill_name}/SKILL.md")
        return

    if memory_id:
        # Find the specific memory.
        row = conn.execute(
            "SELECT * FROM memory WHERE id=? AND superseded_at IS NULL", (memory_id,)
        ).fetchone()
        if not row:
            print(f"[zmem] no live memory with id {memory_id}", file=sys.stderr)
            return

        skill_name = _slugify_skill_name(row["tags"], row["id"])
        skill_dir = Path.home() / ".zcode" / "skills" / skill_name

        # Collision detection.
        if skill_dir.exists():
            print(f"[zmem] ERROR: skill directory already exists: {skill_dir}", file=sys.stderr)
            print(f"  Choose a different memory or rename the existing skill.", file=sys.stderr)
            return

        # Generate the SKILL.md draft.
        import textwrap
        tags_str = row["tags"] or "general"
        draft = f"""---
name: {skill_name}
description: >
  {row['content'][:120].replace(chr(10), ' ')}...
  Auto-promoted from zmem lesson {row['id'][:8]} (signal={row['signal']},
  confidence={row['confidence']}, retrieved {row['retrieval_count']}x).
  EDIT THIS DESCRIPTION to be pushy and name the exact trigger contexts
  where this skill should fire — models under-trigger skills with vague
  descriptions.
---

# {_slugify_skill_name(row['tags'], row['id']).replace('zmem-', '').replace('-', ' ').title()}

## When to use
{(row['content'][:200] + '...' if len(row['content']) > 200 else row['content'])}

## The rule
{row['content']}

## Source
- Promoted from zmem lesson `{row['id']}` (retrieval_count={row['retrieval_count']},
  signal={row['signal']}, confidence={row['confidence']})
- Namespace: {row['namespace']}
- Tags: {tags_str}
"""

        # Write the skill file.
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(draft, encoding="utf-8")

        print(f"[zmem] promoted lesson {row['id'][:8]} -> {skill_file}")
        print(f"  Source lesson KEPT in store (not superseded).")
        print(f"  REVIEW AND EDIT the description before relying on this skill.")
        print(f"  The skill will load on next session restart.")
        return

    # No --id and not --dry-run: show usage.
    print("[zmem] use --dry-run to see candidates, or --id <uuid> to promote a specific lesson")


# Consolidation threshold and cadence defaults.
CONSOLIDATE_DEFAULT_THRESHOLD = float(os.environ.get("ZMEM_CONSOLIDATE_THRESHOLD", "0.80"))
CONSOLIDATE_MIN_INTERVAL_DAYS = 7
CONSOLIDATE_GROWTH_THRESHOLD = 0.20  # run when live count grew by >20% since last run


def consolidate(
    conn: sqlite3.Connection,
    *,
    threshold: float = CONSOLIDATE_DEFAULT_THRESHOLD,
    prune: bool = False,
    dry_run: bool = False,
    namespace: str | None = None,
) -> None:
    """Merge near-duplicate memories via embedding similarity.

    For each live memory with an embedding, query vec0 KNN for nearest neighbors.
    Cluster memories with cosine similarity >= threshold. For each cluster:
    pick the keeper (highest confidence x retrieval_count), merge metadata
    into the keeper, supersede the absorbed members. Each cluster commits
    atomically — interruption is safe because keeper selection is deterministic.

    If prune=True, also supersede memories with retrieval_count=0, signal=none,
    and age>30d (opt-in, never automatic on SessionStart).
    """
    if not _embeddings or not _embeddings.is_available():
        print("[zmem] consolidate requires embeddings — install onnxruntime + tokenizers", file=sys.stderr)
        return

    # Growth-based cadence gate: skip if last consolidation was recent AND
    # the store hasn't grown significantly since. Only applies to automatic
    # runs (not dry-run or explicit CLI invocation with changed args).
    last_consolidation = conn.execute(
        "SELECT value FROM meta WHERE key='last_consolidation'"
    ).fetchone()
    last_count = conn.execute(
        "SELECT value FROM meta WHERE key='last_consolidation_count'"
    ).fetchone()

    if last_consolidation and not dry_run and threshold == CONSOLIDATE_DEFAULT_THRESHOLD:
        import calendar as _cal
        last_ts = last_consolidation[0]
        last_epoch = _cal.timegm(time.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ")) if last_ts else 0
        days_since = (time.time() - last_epoch) / 86400.0 if last_epoch > 0 else 999
        live_count = conn.execute(
            "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        last_live = int(last_count[0]) if last_count and last_count[0].isdigit() else 0
        growth = (live_count - last_live) / max(last_live, 1)

        if days_since < CONSOLIDATE_MIN_INTERVAL_DAYS and growth < CONSOLIDATE_GROWTH_THRESHOLD:
            return  # not enough time or growth to warrant consolidation

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

    # Load all live memories with embeddings.
    ns_clause = "AND namespace = ?" if namespace else ""
    ns_params = [namespace] if namespace else []
    rows = conn.execute(
        f"""SELECT id, namespace, content, tags, confidence, signal, retrieval_count,
                  embedding, embedding_model
           FROM memory
           WHERE superseded_at IS NULL AND embedding IS NOT NULL
           {ns_clause}
           ORDER BY confidence DESC, retrieval_count DESC""",
        ns_params,
    ).fetchall()

    if not rows:
        print("[zmem] no embeddable memories to consolidate")
        return

    # Track which memories have been absorbed (to skip them as seeds).
    absorbed = set()
    merged_count = 0
    pruned_count = 0

    for seed in rows:
        if seed["id"] in absorbed:
            continue

        # Query vec0 for nearest neighbors of this seed.
        neighbors = []
        try:
            results = conn.execute(
                "SELECT memory_id, distance FROM memory_vec "
                "WHERE embedding MATCH ? AND k = 10 ORDER BY distance",
                [seed["embedding"]],
            ).fetchall()
            for r in results:
                mid = r["memory_id"]
                if mid == seed["id"] or mid in absorbed:
                    continue
                sim = 1.0 - r["distance"]
                if sim >= threshold:
                    # Verify it's live and in the right namespace.
                    row = conn.execute(
                        "SELECT id, confidence, signal, tags, retrieval_count FROM memory "
                        "WHERE id=? AND superseded_at IS NULL"
                        + (f" AND namespace=?" if namespace else ""),
                        [mid] + ns_params,
                    ).fetchone()
                    if row:
                        neighbors.append((row, sim))
        except sqlite3.OperationalError:
            continue  # vec0 unavailable

        if not neighbors:
            continue

        # The seed is the keeper (it has the highest confidence x retrieval_count
        # because we ordered by that). Merge each neighbor into it.
        if dry_run:
            print(f"[zmem] DRY RUN: cluster around [{seed['id'][:8]}] "
                  f"(conf={seed['confidence']}, rc={seed['retrieval_count']}):")
            print(f"    keeper: {seed['content'][:80]}...")
            for nb_row, nb_sim in neighbors:
                print(f"    absorb [{nb_row['id'][:8]}] sim={nb_sim:.3f}: "
                      f"conf={nb_row['confidence']} rc={nb_row['retrieval_count']}")
                absorbed.add(nb_row["id"])  # track in dry-run too
            merged_count += len(neighbors)
            continue

        # Atomic commit per cluster.
        try:
            conn.execute("BEGIN")
            for nb_row, nb_sim in neighbors:
                # Merge metadata into the keeper.
                _merge_on_dedup(conn, seed["id"], nb_row["confidence"],
                                nb_row["signal"], nb_row["tags"])
                # Supersede the absorbed member.
                conn.execute(
                    "UPDATE memory SET superseded_at=?, supersede_reason=? WHERE id=?",
                    (now_iso(), f"consolidated into {seed['id']}", nb_row["id"]),
                )
                try:
                    conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (nb_row["id"],))
                except sqlite3.OperationalError:
                    pass
                absorbed.add(nb_row["id"])
            # Mark the keeper as consolidated.
            conn.execute(
                "UPDATE memory SET consolidated_at=? WHERE id=?",
                (now_iso(), seed["id"]),
            )
            conn.execute("COMMIT")
            merged_count += len(neighbors)
        except Exception:
            conn.execute("ROLLBACK")
            continue

    # Optional prune: supersede low-value never-retrieved memories.
    if prune:
        prune_rows = conn.execute(
            f"""SELECT id, content FROM memory
               WHERE superseded_at IS NULL
                 AND retrieval_count = 0
                 AND signal = 'none'
                 AND confidence < 0.35
                 AND ingestion_ts < datetime('now', '-30 days')
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

    parts = [f"merged {merged_count} memories"]
    if prune:
        parts.append(f"pruned {pruned_count}")
    if dry_run:
        parts.append("(dry run — no changes)")
    print(f"[zmem] {' + '.join(parts)}")


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
    p_recall.add_argument("--hybrid", action="store_true",
                          help="use hybrid BM25+vector recall (requires onnxruntime)")

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

    sub.add_parser("reembed", help="backfill embeddings for live memories missing them")

    p_consolidate = sub.add_parser("consolidate", help="merge near-duplicate memories")
    p_consolidate.add_argument("--threshold", type=float,
                               default=float(os.environ.get("ZMEM_CONSOLIDATE_THRESHOLD", "0.80")))
    p_consolidate.add_argument("--prune", action="store_true",
                               help="also supersede low-value never-retrieved memories")
    p_consolidate.add_argument("--dry-run", action="store_true",
                               help="show what would be consolidated without changing anything")
    p_consolidate.add_argument("--namespace", default=None,
                               help="limit consolidation to a specific namespace")

    p_promote = sub.add_parser("promote", help="promote high-confidence lessons to SKILL.md files")
    p_promote.add_argument("--dry-run", action="store_true",
                           help="show promotion candidates without creating skills")
    p_promote.add_argument("--id", default=None,
                           help="promote a specific memory by UUID")
    p_promote.add_argument("--namespace", default=None,
                           help="limit candidates to a specific namespace")

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
        recall_memory(conn, query=args.query, namespace=args.namespace,
                      limit=args.limit, as_json=args.json, hybrid=args.hybrid)
    elif args.cmd == "recent":
        recent_memory(conn, namespace=args.namespace, limit=args.limit,
                      min_confidence=args.min_confidence, as_json=args.json)
    elif args.cmd == "search":
        recall_memory(conn, query=args.text, namespace=args.namespace, limit=args.limit,
                      as_json=False, min_confidence=0.0)
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
    elif args.cmd == "reembed":
        _reembed(conn)
    elif args.cmd == "consolidate":
        consolidate(conn, threshold=args.threshold, prune=args.prune,
                    dry_run=args.dry_run, namespace=args.namespace)
    elif args.cmd == "promote":
        promote_memory(conn, memory_id=args.id, dry_run=args.dry_run,
                       namespace=args.namespace)
    conn.close()


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


if __name__ == "__main__":
    main()
