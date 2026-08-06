#!/usr/bin/env python
"""ZMem store — Tier 2 semantic memory for ZCode.

A local-first, FTS5-backed, tombstone-supersession memory store. Operated via
subcommands so it can be called from the memory SKILL and from hook scripts.

Path resolution (in priority order; see host.py for the authoritative chain):
  1. ZMEM_STORE env var (explicit full path override)
  2. ${ZMEM_DATA}/store.sqlite (box-wide data dir, shared by Claude Code + ZCode)
  3. ${CLAUDE_PLUGIN_DATA}/store.sqlite (Claude Code plugin data dir)
  4. ${ZCODE_PLUGIN_DATA}/store.sqlite (ZCode plugin data dir)
  5. ~/.zmem/store.sqlite (new box-neutral default)
  6. ~/.zcode/memory/store.sqlite (legacy/manual install fallback, only if it
     already exists and (5) does not yet)

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
  python store.py backup [--retention 7] [--out-dir DIR] [--if-due]
  python store.py restore --from <snapshot.sqlite> [--force] [--out-dir DIR]
  python store.py export-pack --namespace NS [--out FILE] [--project-limit 50] \\
         [--global-limit 15] [--min-confidence 0.6] [--max-bytes 32768]
  python store.py export-jsonl [--out FILE] [--namespace NS] [--include-superseded]
  python store.py ingest-jsonl --in FILE [--source-ref REF] [--allow-tombstones]

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
import math
import os
import re
import shutil
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

# Shared host adapter: path resolution, local-FS safety guard, perms, retry.
# Imported with a safe inline fallback so store.py still runs (with the old,
# pre-Phase-1 resolution chain) if host.py is somehow missing from the checkout.
try:
    import host as _host
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import host as _host
    except ImportError:
        _host = None


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to `default` on absent/garbage input.
    Never raises at import time (a typo'd env var must not break every
    subcommand).

    Defined this early on purpose: every module-level tunable below is parsed
    through it, and a bare float(os.environ[...]) at module scope turns one
    malformed env var into an import-time crash of *every* store.py command
    (including ones that have nothing to do with the knob).
    """
    raw = os.environ.get(name, "")
    try:
        return float(raw.strip())
    except (AttributeError, ValueError):
        return default


def _resolve_store_path() -> Path:
    """Resolve the store location. Delegates to host.py's box-wide chain:
    ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA >
    ~/.zmem (new default) > ~/.zcode/memory (legacy, if already present).
    Falls back to the original ZCODE_PLUGIN_DATA/~/.zcode/memory-only chain
    if host.py could not be imported at all.
    """
    if _host is not None:
        return _host.resolve_store_path()
    # --- inline fallback (host.py absent) ---
    explicit = os.environ.get("ZMEM_STORE")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "store.sqlite"
    home = Path(os.path.expanduser("~"))
    plugin_data_pattern = home / ".zcode" / "cli" / "plugins" / "data"
    if plugin_data_pattern.is_dir():
        for d in plugin_data_pattern.iterdir():
            if "zmem" in d.name.lower():
                return d / "store.sqlite"
    return home / ".zcode" / "memory" / "store.sqlite"


def _resolve_core_md_path() -> Path:
    """Resolve the Tier 0 core.md location. Delegates to host.py; see
    _resolve_store_path() for the fallback rationale."""
    if _host is not None:
        return _host.resolve_core_md_path()
    # --- inline fallback (host.py absent) ---
    explicit = os.environ.get("ZMEM_CORE_MD")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "core.md"
    return Path(os.path.expanduser("~/.zcode/memory/core.md"))


def _resolve_skills_dirs() -> list[Path]:
    """Resolve the skills dirs promotion writes into. Delegates to host.py's
    box-wide default (both ~/.claude/skills and ~/.zcode/skills, overridable
    via ZMEM_SKILLS_DIRS). Falls back to the old single ~/.zcode/skills dir
    if host.py could not be imported at all."""
    if _host is not None:
        return _host.resolve_skills_dirs()
    # --- inline fallback (host.py absent) ---
    explicit = os.environ.get("ZMEM_SKILLS_DIRS")
    if explicit:
        return [Path(p).expanduser() for p in explicit.split(os.pathsep) if p.strip()]
    return [Path(os.path.expanduser("~/.zcode/skills"))]


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

# The two closed enums the store's own writers already enforce (`add`'s
# argparse choices). Named here so the Tier 3 ingest validator enforces the
# SAME sets on remote-authored rows -- a sync file must not be able to widen
# them by writing straight into the table.
ALLOWED_TYPES = ("fact", "lesson", "convention", "preference")
ALLOWED_SIGNALS = ("test", "compile", "lint", "reviewer", "user", "none")
SUPPORTED_SCHEMA_VERSION = 6
SCHEMA_VERSION_KEY = "schema_version"

CONFIDENCE_FLOOR = 0.25

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore (all|any|the|previous|prior) instructions\b"),
    re.compile(r"(?i)\b(system prompt|developer message|tool call|function call)\b"),
    re.compile(r"(?i)</?(system|assistant|developer|tool)>"),
    re.compile(r"```"),
]

CAPTURE_MODES = ("auto", "reviewed", "manual")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_schema_version(path: Path) -> int | None:
    """Best-effort readonly schema_version probe for preflight checks.

    Returns None when the file does not exist yet, is empty, or is not far
    enough initialized to answer the question. Raises RuntimeError only for the
    fail-closed case: a recorded schema_version that is newer than this client
    supports, or a recorded value that is not an integer.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
    except OSError:
        return None

    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (SCHEMA_VERSION_KEY,),
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        conn.close()

    if not row:
        return None
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        raise RuntimeError(
            f"zmem: store {path} has a non-integer schema_version {row[0]!r}; "
            "refusing to modify it"
        )
    if version > SUPPORTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"zmem: store {path} uses schema_version {version}, newer than this "
            f"client's supported version {SUPPORTED_SCHEMA_VERSION}; refusing "
            "to modify it"
        )
    return version


def _commit(conn: sqlite3.Connection) -> None:
    """conn.commit() with a bounded retry on 'database is locked' — belt and
    suspenders past PRAGMA busy_timeout for the multi-writer box-wide store
    (concurrent CC sessions + subagents + ZCode). Falls back to a plain
    commit if host.py is unavailable."""
    if _host is not None:
        _host.busy_retry(conn.commit)
    else:
        conn.commit()


def connect() -> sqlite3.Connection:
    _read_schema_version(STORE_PATH)
    if _host is not None:
        # Refuse UNC/network/OneDrive store locations before touching disk —
        # WAL mode on a network share or a sync-managed dir risks corruption.
        _host.assert_local_fs(STORE_PATH.parent)

    dir_existed = STORE_PATH.parent.is_dir()
    file_existed = STORE_PATH.exists()
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STORE_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    if _host is not None and (not dir_existed or not file_existed):
        # Only harden perms right after we created the dir/file — icacls on
        # every connect() would be a needless perf hit on the hot recall path.
        _host.set_owner_only_perms(STORE_PATH.parent)
        if STORE_PATH.exists():
            _host.set_owner_only_perms(STORE_PATH)
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


SCHEMA_LOCK_STALE_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_STALE_SECONDS", 300.0)
SCHEMA_LOCK_WAIT_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_WAIT_SECONDS", 15.0)
SCHEMA_LOCK_POLL_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_POLL_SECONDS", 0.05)
MAINTENANCE_LOCK_STALE_SECONDS = _env_float("ZMEM_MAINTENANCE_LOCK_STALE_SECONDS", 1800.0)
MAINTENANCE_WAIT_SECONDS = _env_float("ZMEM_MAINTENANCE_WAIT_SECONDS", 5.0)
MAINTENANCE_POLL_SECONDS = _env_float("ZMEM_MAINTENANCE_POLL_SECONDS", 0.05)
WRITER_LEASE_STALE_SECONDS = _env_float("ZMEM_WRITER_LEASE_STALE_SECONDS", 300.0)


def _lock_path(name: str) -> Path:
    return STORE_PATH.parent / f".zmem-{name}.lock"


def _writer_dir() -> Path:
    return STORE_PATH.parent / ".zmem-writers"


def _strict_acquire_lock(
    name: str,
    stale_seconds: float,
    *,
    wait_seconds: float = 0.0,
    poll_seconds: float = 0.05,
) -> str | None:
    """Acquire a host lock with bounded waiting and fail-closed degradation."""
    if _host is None:
        raise RuntimeError("zmem: host lock support unavailable")
    path = _lock_path(name)
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        token = _host.acquire_lock(path, stale_seconds)
        if token == getattr(_host, "_NO_LOCK_TOKEN", "unlocked"):
            raise RuntimeError(
                f"zmem: could not safely acquire the {name} lock at {path}"
            )
        if token is not None:
            return token
        if time.time() >= deadline:
            return None
        time.sleep(poll_seconds)


def _release_named_lock(name: str, token: str | None) -> None:
    if _host is None:
        return
    _host.release_lock(_lock_path(name), token)


def _wait_for_maintenance_clear(op: str) -> None:
    """Block briefly while restore owns maintenance, then fail clearly."""
    if _host is None:
        return
    deadline = time.time() + MAINTENANCE_WAIT_SECONDS
    while True:
        token = _strict_acquire_lock(
            "maintenance",
            MAINTENANCE_LOCK_STALE_SECONDS,
            wait_seconds=0.0,
        )
        if token is not None:
            _release_named_lock("maintenance", token)
            return
        if time.time() >= deadline:
            raise RuntimeError(
                f"zmem: maintenance is active; {op} timed out after "
                f"{MAINTENANCE_WAIT_SECONDS:.1f}s waiting for restore to finish"
            )
        time.sleep(MAINTENANCE_POLL_SECONDS)


def _cleanup_stale_writer_leases() -> list[Path]:
    """Drop obviously-stale writer leases and return the live ones."""
    writer_dir = _writer_dir()
    try:
        entries = list(writer_dir.glob("*.lease"))
    except OSError:
        return []
    live: list[Path] = []
    now = time.time()
    for lease in entries:
        try:
            age = now - lease.stat().st_mtime
        except OSError:
            continue
        if age > WRITER_LEASE_STALE_SECONDS:
            try:
                lease.unlink()
            except OSError:
                pass
            continue
        live.append(lease)
    return live


def _acquire_writer_lease(op: str) -> Path:
    """Claim a live-writer lease that restore must observe before replacing the store."""
    _wait_for_maintenance_clear(op)
    writer_dir = _writer_dir()
    writer_dir.mkdir(parents=True, exist_ok=True)
    lease = writer_dir / f"{os.getpid()}-{uuid.uuid4().hex}-{op}.lease"
    lease.write_text(now_iso(), encoding="utf-8")
    try:
        _wait_for_maintenance_clear(op)
    except Exception:
        try:
            lease.unlink()
        except OSError:
            pass
        raise
    return lease


def _release_writer_lease(lease: Path | None) -> None:
    if lease is None:
        return
    try:
        lease.unlink()
    except OSError:
        pass


def _prepare_store(conn: sqlite3.Connection) -> None:
    """Serialize cold-open WAL/init/migrate work behind the schema lock."""
    token = _strict_acquire_lock(
        "schema",
        SCHEMA_LOCK_STALE_SECONDS,
        wait_seconds=SCHEMA_LOCK_WAIT_SECONDS,
        poll_seconds=SCHEMA_LOCK_POLL_SECONDS,
    )
    if token is None:
        raise RuntimeError(
            "zmem: timed out waiting for another process to finish store "
            "initialization or migration"
        )
    try:
        _read_schema_version(STORE_PATH)
        if _host is not None:
            _host.busy_retry(lambda: conn.execute("PRAGMA journal_mode=WAL").fetchone())
        else:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        init_db(conn)
        migrate(conn)
    finally:
        _release_named_lock("schema", token)


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
            last_retrieved  TEXT,
            -- v6: consolidation provenance. Comma-joined ids of rows absorbed
            -- into this keeper by consolidate() (appends across runs). A
            -- `:truncated` marker after an id means that row's content could
            -- not be appended (the keeper was already at the content-size cap)
            -- the id is still recorded for traceability. Write-only provenance
            -- today, queryable by users/future tooling. See _absorb_into_keeper.
            merged_from     TEXT
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


# Old-style (`project:<basename>`) namespace keys and the live checkout each one
# must be re-derived from. The distributable runtime does NOT ship Brett- or
# machine-specific checkout paths. Instead, operators can provide a portable
# JSON object via ZMEM_NS_MIGRATION_MAP:
#   {"project:oldname": "C:/path/to/current/checkout", ...}
# Tests may also monkeypatch the module-level fallback below.
_NS_MIGRATION_CHECKOUTS: dict[str, str] = {}


def _load_ns_migration_checkouts() -> dict[str, str]:
    raw = os.environ.get("ZMEM_NS_MIGRATION_MAP", "").strip()
    if not raw:
        return dict(_NS_MIGRATION_CHECKOUTS)
    try:
        loaded = json.loads(raw)
    except ValueError:
        print(
            "[zmem] ns migration WARNING: ZMEM_NS_MIGRATION_MAP is not valid JSON "
            "- skipping configured namespace re-key paths",
            file=sys.stderr,
        )
        return dict(_NS_MIGRATION_CHECKOUTS)
    if not isinstance(loaded, dict):
        print(
            "[zmem] ns migration WARNING: ZMEM_NS_MIGRATION_MAP must be a JSON "
            "object of {old_namespace: checkout_path}; skipping it",
            file=sys.stderr,
        )
        return dict(_NS_MIGRATION_CHECKOUTS)
    mapping: dict[str, str] = {}
    for old_ns, checkout in loaded.items():
        if not isinstance(old_ns, str) or not isinstance(checkout, str):
            print(
                "[zmem] ns migration WARNING: ignoring non-string namespace map "
                f"entry {old_ns!r}: {checkout!r}",
                file=sys.stderr,
            )
            continue
        if not old_ns.strip() or not checkout.strip():
            continue
        mapping[old_ns] = checkout
    if not mapping:
        return dict(_NS_MIGRATION_CHECKOUTS)
    return mapping


def _rekey_namespaces(conn: sqlite3.Connection, old_namespaces) -> dict[str, str]:
    """Re-derive and rewrite the namespace key for each entry of
    `old_namespaces` (a subset of _NS_MIGRATION_CHECKOUTS's keys). Returns the
    {old: new} map of the ones actually resolved.

    Never guesses: a key whose checkout is not on disk right now is left
    completely untouched and reported, so it can be retried later (see
    _retry_pending_ns_migration). Rewrites ALL rows under the old key, tombstones
    included — a superseded row left behind under a dead key would be stranded
    from its own namespace's history. Does not commit; the caller does.
    """
    mapping: dict[str, str] = {}
    if _host is None:
        print(
            "[zmem] ns migration: host.py unavailable — cannot derive "
            "namespace keys, skipping re-key (namespaces left unchanged)",
            file=sys.stderr,
        )
        return mapping
    checkouts = _load_ns_migration_checkouts()
    for old_ns in old_namespaces:
        checkout = checkouts.get(old_ns)
        if not checkout:
            print(
                f"[zmem] ns migration WARNING: no configured checkout path for "
                f"{old_ns} - namespace left unchanged",
                file=sys.stderr,
            )
            continue
        checkout_path = Path(checkout)
        if not checkout_path.is_dir():
            print(
                f"[zmem] ns migration WARNING: checkout for {old_ns} "
                f"not found at {checkout} — refusing to guess; "
                f"namespace left unchanged (will be retried on a later run)",
                file=sys.stderr,
            )
            continue
        new_ns = _host.resolve_namespace(checkout_path)
        mapping[old_ns] = new_ns
        if new_ns != old_ns:
            conn.execute(
                "UPDATE memory SET namespace=? WHERE namespace=?",
                (new_ns, old_ns),
            )
    return mapping


def _record_ns_migration(
    conn: sqlite3.Connection, mapping: dict[str, str], *, merge: bool = True
) -> None:
    """Write `mapping` into the `ns_migration_v5` meta record.

    merge=True (the retry pass) folds the mapping into whatever is already
    recorded, so a retry adds its newly-resolved namespaces without erasing the
    original migration's provenance. merge=False (the one-time v5 block)
    REPLACES the record: that block recomputes the entire map from scratch
    every time it runs, and its record is meant to say exactly what THAT run
    resolved — no more.
    """
    existing: dict = {}
    if merge:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='ns_migration_v5'"
        ).fetchone()
        if row and row[0]:
            try:
                loaded = json.loads(row[0])
                if isinstance(loaded, dict):
                    existing = loaded
            except (ValueError, TypeError):
                existing = {}
    existing.update(mapping)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('ns_migration_v5', ?)",
        (json.dumps(existing),),
    )


def _retry_pending_ns_migration(conn: sqlite3.Connection) -> None:
    """Re-attempt the v5 re-key for any namespace the original migration had to
    skip. Runs on EVERY migrate(), independent of schema_version.

    The v5 block is version-gated and therefore fires exactly once. Any
    namespace whose checkout happened to be absent at that instant (unmounted
    drive, not-yet-cloned repo) was skipped — and, because schema_version was
    bumped to 5 regardless, was skipped *permanently*, stranding those rows
    under a dead key forever. Decoupling the retry from the version gate fixes
    that: the cost when there is nothing to do (the overwhelmingly common case)
    is one indexed SELECT against a four-element IN-list, and it is a strict
    no-op unless a row still carries an old-style key AND its checkout is now
    present.
    """
    keys = list(_load_ns_migration_checkouts())
    if not keys:
        return
    placeholders = ",".join("?" * len(keys))
    try:
        stranded = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT namespace FROM memory WHERE namespace IN ({placeholders})",
                keys,
            ).fetchall()
        ]
    except sqlite3.Error:
        return  # never let a retry probe break an otherwise-fine migrate()
    if not stranded:
        return  # nothing left under an old-style key — no-op
    mapping = _rekey_namespaces(conn, stranded)
    if mapping:
        _record_ns_migration(conn, mapping)
        print(f"[zmem] ns migration: re-keyed {len(mapping)} previously-skipped "
              f"namespace(s): {', '.join(sorted(mapping))}", file=sys.stderr)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Versioned migration. Runs after init_db(). Idempotent and crash-safe.

    Each version block is guarded by a version check so it runs exactly once.
    DDL statements use IF (NOT) EXISTS so they are safe to repeat if a crash
    interrupts the migration before the version bump. The busy_timeout set in
    connect() serializes concurrent hook processes; the version guard makes
    the second one a no-op once the first commits.
    """
    row = conn.execute("SELECT value FROM meta WHERE key=?",
                       (SCHEMA_VERSION_KEY,)).fetchone()
    ver = int(row[0]) if row else 1
    if ver > SUPPORTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"zmem: store schema_version {ver} is newer than this client's "
            f"supported version {SUPPORTED_SCHEMA_VERSION}; refusing to modify it"
        )

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

    if ver < 5:
        # v5: box-wide namespace re-key (PLAN.md §2b/§8, CRITIC BLOCKER 1).
        #
        # Old namespaces were `project:<basename>` (e.g. `project:opencode-swarm`),
        # which silently splits one project across multiple checkouts/worktrees
        # (see PLAN.md §1 — the same opencode-swarm remote checked out at both
        # E:\ZCode\opencode-swarm and E:\ClaudeCode\opencode-swarm-dev used to
        # resolve to two different namespaces). The new key is produced by
        # host.resolve_namespace() against each namespace's *live checkout
        # path* — never hand-typed — so it is guaranteed identical to what the
        # runtime derives for that same checkout (and any sibling worktree/
        # clone of the same remote).
        #
        # `source_ref` is `session:<id>` — the origin checkout is NOT
        # recoverable from stored data, so this explicit old-namespace ->
        # checkout-path map is unavoidable. If a mapped checkout is missing
        # from disk, refuse to guess: leave that namespace's rows untouched
        # and report it loudly, rather than fail the whole migration.
        #
        # Schema-gated (this whole block only runs when ver < 5) and built
        # entirely from a fresh recomputation each time it runs, so it is
        # both a no-op on immediate re-run (ver already 5) and reproducible
        # on a fresh v4 re-import at cutover (PLAN.md §10b). Namespaces this
        # pass has to skip (checkout absent) are NOT lost: the unconditional
        # _retry_pending_ns_migration() below picks them up on a later run,
        # which is why bumping schema_version here is safe.
        migration_map = _rekey_namespaces(conn, list(_load_ns_migration_checkouts()))
        # `user:global` and any unmapped namespace (e.g. `project:ZCode`,
        # a spurious parent-dir capture with no git remote of its own) are
        # deliberately left untouched.
        _record_ns_migration(conn, migration_map, merge=False)
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        conn.commit()

    if ver < 6:
        # v6: consolidation content-provenance (issue #19).
        #
        # consolidate() now PRESERVES information unique to an absorbed row by
        # appending it to the keeper's content and recording the absorbed id in
        # `merged_from` (previously the absorbed content was tombstoned and lost
        # from live recall). This column is the queryable provenance trail of
        # which rows were folded into a keeper.
        #
        # init_db() (which runs before migrate() on a fresh store) ALREADY
        # creates this column and sets schema_version=1, so on a fresh store
        # migrate() runs every version block 1->6 and this ALTER would hit an
        # existing column. Guard with a table_info probe so the ALTER is a true
        # no-op when the column already exists (idempotent on re-migrate too).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "merged_from" not in cols:
            conn.execute("ALTER TABLE memory ADD COLUMN merged_from TEXT")
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        conn.commit()

    # Version-INDEPENDENT: retry any old-style namespace the v5 pass had to
    # skip. See _retry_pending_ns_migration for why this cannot live behind the
    # version gate.
    _retry_pending_ns_migration(conn)


def _load_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension. Raises if unavailable."""
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)


class CapturePolicyRefusal(ValueError):
    """Automatic capture could not safely preserve the record contract."""


def _check_secrets(content: str, source_ref: str, tags: str = "") -> list[str]:
    """Advisory only. Returns list of warnings. Never blocks. Scans both content
    source_ref, and tags (a token in metadata would otherwise slip through)."""
    warnings = []
    combined = " ".join((content, source_ref, tags))
    for pat in SECRET_PATTERNS:
        m = pat.search(combined)
        if m:
            warnings.append(f"possible secret-like text matched pattern {pat.pattern[:40]!r}: {m.group(0)[:20]!r}...")
    return warnings


def _merge_tag_strings(*tag_sets: str) -> str:
    merged: set[str] = set()
    for raw in tag_sets:
        merged.update(t.strip() for t in (raw or "").split(",") if t.strip())
    return ",".join(sorted(merged))


def _normalize_capture_mode(mode: str | None) -> str:
    value = (mode or os.environ.get("ZMEM_CAPTURE_MODE") or "manual").strip().lower()
    return value if value in CAPTURE_MODES else "manual"


def _redact_secret_like_text(text: str) -> tuple[str, int]:
    redacted = text or ""
    count = 0
    for pat in SECRET_PATTERNS:
        redacted, changed = pat.subn("[REDACTED_SECRET]", redacted)
        count += changed
    return redacted, count


def _has_prompt_injection_risk(*values: str) -> bool:
    combined = " ".join(v for v in values if v)
    return any(p.search(combined) for p in PROMPT_INJECTION_PATTERNS)


def _apply_capture_policy(
    *,
    content: str,
    source_ref: str,
    tags: str,
    capture_mode: str,
) -> tuple[str, str, str, list[str]]:
    """Apply capture-time redaction/labeling while preserving provenance."""
    mode = _normalize_capture_mode(capture_mode)
    warnings = _check_secrets(content, source_ref, tags)
    out_content = content
    out_source_ref = source_ref
    out_tags = tags
    source_warnings = _check_secrets("", source_ref)
    if mode == "auto" and source_warnings:
        raise CapturePolicyRefusal(
            "refusing automatic capture because source_ref contains secret-like "
            "text; review it manually so provenance and staleness tracking are "
            "not silently destroyed"
        )
    if mode == "auto" and warnings:
        out_content, content_redactions = _redact_secret_like_text(content)
        out_tags, tag_redactions = _redact_secret_like_text(tags)
        total = content_redactions + tag_redactions
        if total <= 0:
            raise RuntimeError(
                "zmem: refusing automatic capture with likely secrets that could "
                "not be safely redacted"
            )
        warnings = [
            f"automatic capture redacted {total} secret-like value(s); review the "
            "stored memory before trusting it"
        ]
        out_tags = _merge_tag_strings(out_tags, "auto-redacted")
    if _has_prompt_injection_risk(out_content, out_source_ref):
        out_tags = _merge_tag_strings(out_tags, "prompt-injection-risk")
    return out_content, out_source_ref, out_tags, warnings


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


def _detect_duplicate(
    conn: sqlite3.Connection, content: str, namespace: str
) -> tuple[sqlite3.Row | None, float, bytes | None]:
    """Find a duplicate live memory for `content` in `namespace`: semantic
    similarity (if embeddings are available) with an exact-match fallback.

    Returns (existing_row_or_None, similarity, embedding_or_None). The
    embedding is returned too so a fresh insert (no duplicate found) does not
    have to re-embed the same content a second time. Shared by add_memory()
    and ingest-jsonl's per-row insert path — dedup-on-write must behave
    identically for a locally-authored add and a synced row.
    """
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
    return existing, dedup_sim, emb


# Obvious misspellings/variations of the global namespace. Matched
# case-insensitively after stripping common separators (s, _, -, :) so
# `global`, `globals`, `userglobal`, `users:global`, `user-global`,
# `user_global`, `global:user`, `globals:user` all collapse to one comparison
# key and are rejected at write time with a message naming the canonical form.
# This prevents new "dead letter" rows that no automatic hook could reach
# (issue #18, "Related observation"). It does NOT touch arbitrary namespaces
# (e.g. legitimate `project:global-thing`) — only obvious global near-misses.

# Known normalized stems that are clearly meant to be the global namespace.
# `userglobal` is the canonical stem (from `user:global`); the others are the
# common ways to misspell it without the `user:` prefix, with the words
# swapped, or with the plural `s`. A namespace is a near-miss iff its
# normalized stem (separators stripped, lowercased) is in this set.
_GLOBAL_NEAR_MISS_STEMS = {
    "global", "globals",
    "userglobal", "globaluser",
    "usersglobal", "globalsusers", "usersusersglobal",
    "userglobals", "globalsuser",
}


def _global_near_miss_key(ns: str) -> str:
    """Normalize a namespace for global-near-miss comparison: lowercase, then
    strip separators (space, _, -, :, and .) so the common typos collapse
    together. `.` is included because it is a common typo for `:` on US
    keyboards (`user.global`). `user:global` → `userglobal`;
    `users:global` → `usersglobal`; etc. (PRR-009, swarm-pr-review.)"""
    return re.sub(r"[\s:_\-.]+", "", ns.lower())


def _validate_namespace(conn: sqlite3.Connection, namespace: str) -> str:
    """Validate/canonicalize a namespace at write time.

    - Reject empty/None/whitespace-only namespaces.
    - Reject obvious near-miss variants of the global namespace (e.g.
      ``global``, ``userglobal``, ``users:global``) by raising
      ``CapturePolicyRefusal`` naming the canonical ``user:global``. Such rows
      would be unreachable from every automatic hook (issue #18).
    - Trim surrounding whitespace (intentional canonicalization —
      ``add --namespace "  user:global  "`` stores under ``user:global``).
    - Emit a non-fatal note in the refusal message when existing live rows are
      already stranded under the rejected near-miss namespace, naming the
      count. Reconciliation of those legacy rows is a separate data-hygiene
      task (doctor/consolidate); this guard only prevents NEW ones.

    Arbitrary namespaces (``project:<x>``, custom keys) pass through untouched.
    """
    if namespace is None or not namespace.strip():
        raise CapturePolicyRefusal(
            "refusing write: namespace is empty; use 'user:global' for "
            "cross-project knowledge or 'project:<name>' for project-scoped"
        )
    trimmed = namespace.strip()

    # The canonical form passes through untouched.
    if trimmed == GLOBAL_NAMESPACE:
        return trimmed

    key = _global_near_miss_key(trimmed)
    is_near_miss = key in _GLOBAL_NEAR_MISS_STEMS

    if is_near_miss:
        # Report ALL already-stranded near-miss rows (any variant sharing a
        # global-near-miss stem), not just the exact spelling the operator
        # typed — otherwise the "0 existing" message is misleading when other
        # variants exist. _global_near_miss_key is a Python helper (not a SQL
        # UDF), so pull the distinct live namespaces and count in Python. This
        # is the rare refusal path, so the small scan is acceptable.
        stranded = 0
        try:
            distinct_ns = conn.execute(
                "SELECT namespace FROM memory WHERE superseded_at IS NULL"
            ).fetchall()
            for row in distinct_ns:
                # Count only NON-canonical near-miss rows. The canonical
                # `user:global` also normalizes to "userglobal" (a stem), so
                # without this exclusion healthy global rows would be falsely
                # reported as stranded. (PRR-001, swarm-pr-review.)
                if (row["namespace"] != GLOBAL_NAMESPACE
                        and _global_near_miss_key(row["namespace"]) in _GLOBAL_NEAR_MISS_STEMS):
                    stranded += 1
        except sqlite3.OperationalError:
            stranded = 0
        msg = (
            f"refusing write: namespace {trimmed!r} looks like a misspelling of "
            f"the global namespace; use {GLOBAL_NAMESPACE!r} instead."
        )
        if stranded:
            msg += (
                f" ({stranded} existing live row(s) are already stranded under "
                f"a global-near-miss namespace and are unreachable from the "
                "automatic hooks — rekey them with `rekey-namespace "
                "--near-miss-global --confirm`.)"
            )
        raise CapturePolicyRefusal(msg)
    return trimmed


def rekey_namespace(
    conn: sqlite3.Connection,
    *,
    from_namespace: str | None = None,
    to_namespace: str | None = None,
    near_miss_global: bool = False,
    dry_run: bool = False,
) -> int:
    """Admin re-key: rewrite the ``namespace`` column of live rows.

    This is the remediation path for legacy rows stranded under a global
    near-miss namespace (``global``, ``userglobal``, …) that the write-time
    ``_validate_namespace`` guard now rejects. Such rows are unreachable from
    every automatic hook (issue #18 "Related observation"); this moves them to a
    reachable namespace (default: ``user:global``) so they surface again.

    Two modes:
      - ``near_miss_global=True``: rekeys EVERY live row whose namespace
        normalizes (via ``_global_near_miss_key``) to a stem in
        ``_GLOBAL_NEAR_MISS_STEMS`` to ``to_namespace`` (default ``user:global``).
        ``from_namespace`` is ignored in this mode.
      - ``near_miss_global=False`` (default): rekeys live rows whose namespace
        exactly equals ``from_namespace`` (case-sensitive) to ``to_namespace``.
        ``from_namespace`` is required in this mode.

    ``to_namespace`` is itself validated (must not itself be a near-miss) so the
    command cannot move rows FROM one dead-letter key TO another.

    Returns the number of rows rekeyed. ``dry_run`` reports the count and the
    candidate namespaces without writing. The write is a single UPDATE under a
    BEGIN IMMEDIATE transaction; superseded rows are left untouched (history).
    """
    # Resolve the default lazily — GLOBAL_NAMESPACE is defined later in the
    # module, so it cannot be a parameter default (evaluated at def time).
    if to_namespace is None:
        to_namespace = GLOBAL_NAMESPACE
    # Validate the destination: trim, reject empty/whitespace (mirror
    # _validate_namespace's empty rule), and never move rows to another dead
    # letter. Without this, `--to ""` (e.g. an unset shell var) would write an
    # empty namespace — a one-way door, since `--from ""` is treated as missing
    # and rows stranded under "" cannot be rekeyed back. (PRR-002/PRR-013.)
    to_namespace = to_namespace.strip()
    if not to_namespace:
        raise ValueError(
            "refusing rekey: destination namespace is empty; use "
            f"{GLOBAL_NAMESPACE!r} or a project:<name> namespace"
        )
    dest_key = _global_near_miss_key(to_namespace)
    if to_namespace != GLOBAL_NAMESPACE and dest_key in _GLOBAL_NEAR_MISS_STEMS:
        raise ValueError(
            f"refusing rekey: destination {to_namespace!r} is itself a global "
            f"near-miss; use {GLOBAL_NAMESPACE!r}"
        )

    # Build the set of source namespaces to rekey.
    if near_miss_global:
        # Scan distinct live namespaces and keep those that normalize to a stem.
        # EXCLUDE the canonical GLOBAL_NAMESPACE: it also normalizes to
        # "userglobal" (a stem), so without this guard `--near-miss-global
        # --to project:x` would silently bulk-move every legit user:global row
        # to project:x — data corruption of the exact tier this tool exists to
        # remediate. (PRR-001, swarm-pr-review.)
        try:
            distinct = conn.execute(
                "SELECT DISTINCT namespace FROM memory WHERE superseded_at IS NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            distinct = []
        sources = [r["namespace"] for r in distinct
                   if r["namespace"] != GLOBAL_NAMESPACE
                   and _global_near_miss_key(r["namespace"]) in _GLOBAL_NEAR_MISS_STEMS]
    else:
        if not from_namespace or not from_namespace.strip():
            raise ValueError(
                "rekey-namespace needs either --near-miss-global or "
                "--from <namespace>"
            )
        sources = [from_namespace]

    if not sources:
        print("[zmem] rekey-namespace: no matching live rows found.")
        return 0

    # Count candidates.
    placeholders = ",".join("?" * len(sources))
    count = conn.execute(
        f"SELECT COUNT(*) AS n FROM memory "
        f"WHERE superseded_at IS NULL AND namespace IN ({placeholders})",
        sources,
    ).fetchone()["n"]

    print(f"[zmem] rekey-namespace: {count} live row(s) under "
          f"{', '.join(repr(s) for s in sources)} -> {to_namespace!r}")
    if dry_run:
        print("[zmem] rekey-namespace: --dry-run, no rows written.")
        return count

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        conn.execute(
            f"UPDATE memory SET namespace=? "
            f"WHERE superseded_at IS NULL AND namespace IN ({placeholders})",
            [to_namespace, *sources],
        )
        if started_tx:
            _commit(conn)
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
    print(f"[zmem] rekey-namespace: rekeyed {count} row(s).")
    return count


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
    capture_mode: str = "manual",
) -> str:
    content, source_ref, tags, warns = _apply_capture_policy(
        content=content,
        source_ref=source_ref,
        tags=tags,
        capture_mode=capture_mode,
    )
    # Validate the namespace: reject empty and obvious near-miss variants of
    # the global namespace so they cannot be created silently. A row stored
    # under e.g. `global` (instead of `user:global`) is unreachable from every
    # automatic hook (issue #18 "Related observation"). Whitespace is trimmed
    # (intentional — `add --namespace "  user:global  "` canonicalizes).
    namespace = _validate_namespace(conn, namespace)
    for w in warns:
        prefix = "WARNING (advisory, write proceeded)"
        if _normalize_capture_mode(capture_mode) == "auto":
            prefix = "NOTICE (automatic capture sanitized)"
        print(f"[zmem] {prefix}: {w}", file=sys.stderr)

    if confidence is None:
        confidence = SIGNAL_CONFIDENCE.get(signal, 0.3)

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        # Dedup-on-write: semantic similarity (if embeddings available) or exact
        # match fallback. Semantic dedup catches paraphrases the exact-match miss.
        # Shared with ingest-jsonl (Tier 3 sync import), which must apply the same
        # dedup-on-write semantics to incoming rows without duplicating this logic.
        existing, dedup_sim, emb = _detect_duplicate(conn, content, namespace)

        if existing:
            # Merge: upgrade confidence/signal if the new add is stronger.
            _merge_on_dedup(conn, existing["id"], confidence, signal, tags)
            if started_tx:
                _commit(conn)
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
        if started_tx:
            _commit(conn)
        print(f"[zmem] added memory {mid} (ns={namespace}, type={type_}, signal={signal}, conf={confidence}"
              f"{', embedded' if emb is not None else ''})")
        return mid
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise


# Cosine similarity threshold for semantic dedup (0..1, higher = stricter).
# Override via ZMEM_DEDUP_THRESHOLD env var if false-positive merges occur.
DEDUP_SIMILARITY_THRESHOLD = _env_float("ZMEM_DEDUP_THRESHOLD", 0.85)
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
    merged_tags = _merge_tag_strings(row["tags"], new_tags)

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

    When ``no_bump`` is True the retrieval_count / last_retrieved telemetry write
    is suppressed, making recall READ-ONLY. Hook-driven recall (UserPromptSubmit,
    SubagentStart) passes this so heavy subagent fan-out does not turn every
    delegated agent into a concurrent writer on the shared store (PLAN.md §5).
    Explicit skill-invoked recall keeps the default (bumps).

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

    if results and not no_bump:
        ids = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memory SET retrieval_count=retrieval_count+1, last_retrieved=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
        _commit(conn)

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

    When ``no_bump`` is True the retrieval_count / last_retrieved telemetry write
    is suppressed (READ-ONLY). Hook-driven subagent recall passes this so a
    dispatch fan-out does not make every subagent a concurrent writer on the
    shared store (PLAN.md §5).

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
    if results and not no_bump:
        ids = [r["id"] for r in results]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memory SET retrieval_count=retrieval_count+1, last_retrieved=? "
            f"WHERE id IN ({placeholders})",
            [now_iso(), *ids],
        )
        _commit(conn)
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


def supersede_memory(
    conn: sqlite3.Connection, mid: str, reason: str = "", *, at: str | None = None
) -> bool:
    """Tombstone a memory (mark superseded_at). Does not delete — keeps history.

    `at` overrides the superseded_at timestamp (default: now). ingest-jsonl
    uses this to apply a remote tombstone with the ORIGINATING store's
    superseded_at, not the local ingest time — otherwise two synced copies of
    the same tombstone would disagree on when it happened.
    """
    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        row = conn.execute("SELECT id FROM memory WHERE id=?", (mid,)).fetchone()
        if not row:
            print(f"[zmem] no memory with id {mid}", file=sys.stderr)
            if started_tx and conn.in_transaction:
                conn.rollback()
            return False
        conn.execute("UPDATE memory SET superseded_at=?, supersede_reason=? WHERE id=?",
                     (at or now_iso(), reason, mid))
        # Also remove from the vec0 table to prevent orphaned vectors consuming KNN slots.
        try:
            conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
        except sqlite3.OperationalError:
            pass  # vec0 table not available
        if started_tx:
            _commit(conn)
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
    # The confirmation print interpolates `reason`, which on the ingest-jsonl
    # path is REMOTE-AUTHORED. stdout is strict under a legacy codepage
    # (PYTHONIOENCODING=cp1252 and friends), so a non-representable character
    # in `reason` would raise HERE -- after the commit -- and ingest-jsonl's
    # per-row guard would then count a supersession that actually landed as
    # `malformed`. A cosmetic status line must never be able to misreport a
    # durable write, so its failure is swallowed and retried in ASCII.
    note = f": {reason}" if reason else ""
    try:
        print(f"[zmem] superseded {mid}{note}")
    except UnicodeEncodeError:
        safe_note = note.encode("ascii", "replace").decode("ascii")
        print(f"[zmem] superseded {mid}{safe_note}")
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

    Candidates: type=lesson, signal in (test/compile/lint), confidence>=0.85,
    retrieval_count>3, not superseded. Does NOT supersede the source lesson —
    the lesson and the skill coexist (the lesson costs ~200 bytes; if the skill
    description fails to trigger, the lesson is still in recall).

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

    if not candidates and not memory_id:
        # Only short-circuit when we're *surveying*. An explicit --id is a
        # human override that does its own live-row lookup below and is not
        # bound by the candidate bar (signal/confidence/retrieval_count), so
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
            print(f"\n  [{c['id'][:8]}] (rc={c['retrieval_count']}, conf={c['confidence']}, "
                  f"signal={c['signal']})")
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
- Promoted from zmem lesson `{row['id']}` (retrieval_count={row['retrieval_count']},
  signal={row['signal']}, confidence={row['confidence']})
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


# Consolidation threshold and cadence defaults.
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
    cur = conn.execute("SELECT content FROM memory WHERE id=?", (keeper_id,)).fetchone()
    keeper_content = cur["content"] if cur else (keeper["content"] or "")

    decision = _absorb_decision(keeper_content, absorbed_content, absorbed["id"])
    will_append = decision["will_append"]

    if will_append:
        # _absorb_decision already checked the size cap against the grown keeper
        # content (re-read above), so the projection is current. Append.
        separator = f"\n\n--- merged from {absorbed['id']} ---\n"
        new_content = keeper_content + separator + absorbed_content
        conn.execute("UPDATE memory SET content=? WHERE id=?", (new_content, keeper_id))

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
    merged_from = f"{existing},{id_entry}" if existing else id_entry
    conn.execute("UPDATE memory SET merged_from=? WHERE id=?", (merged_from, keeper_id))

    # Metadata upgrade (confidence/signal/tags + retrieval_count bump). Shared
    # with write-time dedup semantics; no content change here.
    _merge_on_dedup(conn, keeper_id, absorbed["confidence"], absorbed["signal"], absorbed["tags"])

    # Tombstone the absorbed row (kept for history; removed from live recall).
    conn.execute(
        "UPDATE memory SET superseded_at=?, supersede_reason=? WHERE id=?",
        (now_iso(), f"consolidated into {keeper_id}", absorbed["id"]),
    )
    try:
        conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (absorbed["id"],))
    except sqlite3.OperationalError:
        pass

    return decision


def consolidate(
    conn: sqlite3.Connection,
    *,
    threshold: float = CONSOLIDATE_DEFAULT_THRESHOLD,
    prune: bool = False,
    dry_run: bool = False,
    namespace: str | None = None,
) -> None:
    """Merge near-duplicate memories via embedding similarity (or a lexical
    token-overlap fallback when embeddings are unavailable — Phase 10).

    For each live memory with an embedding, query vec0 KNN for nearest neighbors.
    Cluster memories with cosine similarity >= threshold. For each cluster:
    pick the keeper (highest confidence * retrieval_count), merge the absorbed
    members into the keeper (preserving content unique to each absorbed row —
    issue #19), and supersede the absorbed members. Each cluster commits
    atomically — interruption is safe because keeper selection is deterministic.

    The keeper is chosen by the highest ``confidence * retrieval_count`` product.
    Ties are broken by ``confidence`` DESC (so when every retrieval_count is 0 —
    the common fresh-store case where the product is 0 for all rows — the
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

    If prune=True, also supersede memories with retrieval_count=0, signal=none,
    and age>30d (opt-in, never automatic on SessionStart).
    """
    use_lexical = not (_embeddings and _embeddings.is_available())
    if use_lexical:
        print("[zmem] embeddings unavailable — consolidating via lexical token overlap", file=sys.stderr)

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

    # Load all live memories. In embedding mode, only rows with an embedding
    # are candidates (vec0 KNN needs a query vector); in lexical fallback
    # mode every live row is a candidate (Jaccard needs only content/tags).
    ns_clause = "AND namespace = ?" if namespace else ""
    ns_params = [namespace] if namespace else []
    embed_clause = "" if use_lexical else "AND embedding IS NOT NULL"
    rows = conn.execute(
        f"""SELECT id, namespace, content, tags, confidence, signal, retrieval_count,
                  embedding, embedding_model, ingestion_ts
           FROM memory
           WHERE superseded_at IS NULL {embed_clause}
           {ns_clause}
           ORDER BY confidence * retrieval_count DESC, confidence DESC,
                    ingestion_ts ASC, id ASC""",
        ns_params,
    ).fetchall()

    if not rows:
        print("[zmem] no embeddable memories to consolidate")
        return

    # Precompute lexical token sets once per row (only used in fallback mode).
    lexical_tokens = {}
    if use_lexical:
        for r in rows:
            lexical_tokens[r["id"]] = _lexical_tokens(
                (r["content"] or "") + " " + (r["tags"] or "")
            )

    # Track which memories have been absorbed (to skip them as seeds).
    absorbed = set()
    merged_count = 0
    pruned_count = 0

    # Cosine threshold and lexical (Jaccard) threshold live on different
    # scales. If the caller left --threshold at its cosine default while we're
    # in lexical fallback, swap in the lexical default; an explicit override
    # is respected either way.
    effective_threshold = threshold
    if use_lexical and threshold == CONSOLIDATE_DEFAULT_THRESHOLD:
        effective_threshold = CONSOLIDATE_LEXICAL_THRESHOLD

    # NAMESPACE CONTAINMENT (data-integrity invariant, both clustering paths):
    # a cluster is ALWAYS scoped to the seed's own namespace, whether or not the
    # caller passed `namespace`. The `ns_clause`/`ns_params` above only narrow
    # which rows are *considered*; they are not a containment guarantee, because
    # the auto-triggered background run (zmem-session-start.sh) passes no
    # --namespace at all. Without the seed-namespace check below, that run could
    # supersede one project's memory into an unrelated project's memory.
    for seed in rows:
        if seed["id"] in absorbed:
            continue

        neighbors = []
        if use_lexical:
            # Jaccard token-overlap clustering: compare seed against every
            # other not-yet-absorbed row. O(n^2) but consolidate runs on a
            # bounded, infrequent cadence (see the growth-gate above) over a
            # single user's live memory count, not a large corpus.
            seed_tokens = lexical_tokens[seed["id"]]
            for row in rows:
                if row["id"] == seed["id"] or row["id"] in absorbed:
                    continue
                if row["namespace"] != seed["namespace"]:
                    continue  # namespace containment — see the note above
                sim = _lexical_similarity(seed_tokens, lexical_tokens[row["id"]])
                if sim >= effective_threshold:
                    neighbors.append((row, sim))
        else:
            # Query vec0 for nearest neighbors of this seed.
            #
            # vec0's KNN is NAMESPACE-BLIND: it returns the globally nearest k
            # rows, and the namespace filter below then discards the ones
            # belonging to other projects. With a fixed k that silently
            # UNDER-merges — on a box-wide store holding several projects the
            # global top-10 can be filled entirely by other namespaces while
            # the seed's own duplicate sits at rank 11, so consolidation finds
            # nothing and the duplicate survives forever. (Introduced when the
            # namespace filter was added to stop cross-project merging; the
            # filter is right, a fixed k alongside it is not.)
            #
            # Escalate k until the answer is provably complete. Rows come back
            # ORDER BY distance (similarity descending), so the moment we see
            # one below the threshold, no later row can qualify and the
            # qualifying set is closed. Only when EVERY returned row is still
            # above the threshold might more qualify — then widen and re-ask.
            # Bounded by the live row count, so it always terminates.
            results = []
            k = 10
            k_cap = max(len(rows), 10)
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
                    break  # bounded: never scan past the live row count
                k = min(k * 5, k_cap)
            for r in results:
                mid = r["memory_id"]
                if mid == seed["id"] or mid in absorbed:
                    continue
                sim = 1.0 - r["distance"]
                if sim >= effective_threshold:
                    # Verify it's live AND in the seed's own namespace —
                    # namespace containment, see the note above the loop. vec0
                    # KNN is namespace-blind, so this is the only thing standing
                    # between a background (no --namespace) run and a
                    # cross-project merge.
                    row = conn.execute(
                        "SELECT id, confidence, signal, tags, retrieval_count, content "
                        "FROM memory "
                        "WHERE id=? AND superseded_at IS NULL AND namespace=?",
                        (mid, seed["namespace"]),
                    ).fetchone()
                    if row:
                        neighbors.append((row, sim))

        if not neighbors:
            continue

        # The seed is the keeper. With the rows query ordered by
        # (confidence * retrieval_count DESC, confidence DESC, ingestion_ts ASC,
        # id ASC), the seed has the highest confidence*retrieval_count product
        # in its cluster (and the highest confidence on a product tie — the
        # fresh-store case where every retrieval_count is 0) — matching the
        # documented keeper rule (issue #19 defect 2: previously the ORDER BY
        # was lexicographic on confidence, contradicting the rule and destroying
        # higher-product rows). Merge each neighbor into it.
        if dry_run:
            print(f"[zmem] DRY RUN: cluster around [{seed['id'][:8]}] "
                  f"(conf={seed['confidence']}, rc={seed['retrieval_count']}, "
                  f"prod={seed['confidence'] * seed['retrieval_count']:.2f}):")
            print(f"    keeper: {seed['content']}")
            # Dry-run uses the SAME decision predicate as the real merge, so the
            # preview reflects what would actually happen (issue #19 defect 3:
            # the previous dry-run only printed a cluster count, hiding the text
            # that would be lost). Accumulate the keeper's grown content as we
            # go so successive absorbs see the keeper as the real merge would.
            dry_keeper_content = seed["content"] or ""
            for nb_row, nb_sim in neighbors:
                dec = _absorb_decision(dry_keeper_content, nb_row["content"] or "", nb_row["id"])
                print(f"    absorb [{nb_row['id'][:8]}] sim={nb_sim:.3f}: "
                      f"conf={nb_row['confidence']} rc={nb_row['retrieval_count']} "
                      f"prod={nb_row['confidence'] * nb_row['retrieval_count']:.2f}")
                print(f"        content: {nb_row['content']}")
                if dec["reason"] == "unique":
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
            continue

        # Atomic commit per cluster.
        try:
            conn.execute("BEGIN")
            for nb_row, nb_sim in neighbors:
                # Preserve absorbed content + record provenance + merge metadata
                # + supersede (issue #19). consolidate-ONLY helper.
                _absorb_into_keeper(conn, seed, nb_row)
                absorbed.add(nb_row["id"])
            # Mark the keeper as consolidated.
            conn.execute(
                "UPDATE memory SET consolidated_at=? WHERE id=?",
                (now_iso(), seed["id"]),
            )
            conn.execute("COMMIT")
            merged_count += len(neighbors)
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


# ---------------------------------------------------------------------------
# Backup / restore / retention (P11 — PLAN.md §7 P11, §8 "single point of failure")
# ---------------------------------------------------------------------------
# The box-wide store is now the single point of failure for every project and
# both hosts. P11 adds a snapshot with retention, a verified restore path, and
# a single-flight guard so the detached background writers the SessionStart
# hook fires cannot pile up on each other.
#
# SNAPSHOT METHOD — SQLite's Online Backup API (`sqlite3.Connection.backup`).
# A periodic snapshot of the LIVE box-wide store has no quiescent window to
# wait for: hook processes from other sessions may be committing at any moment.
# The Online Backup API is built for exactly that — it copies pages under
# SQLite's own locking with automatic restart/retry on concurrent writes, and
# yields a single self-contained destination file with no WAL sidecars to
# reason about. import-store.py transfers its one-shot legacy migration the
# same way, for the same reason, adding a before/after sha256 of the source as
# an independent "the legacy store was never touched" proof (failure mode:
# "re-run when quiescent"); that extra assertion is specific to migrating
# someone else's live store and is not needed here.

SNAPSHOT_PREFIX = "store-"
PRERESTORE_PREFIX = "prerestore-"
SNAPSHOT_SUFFIX = ".sqlite"
# Retention only ever considers files matching THIS glob. `prerestore-*` files
# deliberately fall outside it: the safety copy taken right before a restore is
# the rollback path for that restore and must never be pruned by an unrelated
# automatic backup rotation.
SNAPSHOT_GLOB = SNAPSHOT_PREFIX + "*" + SNAPSHOT_SUFFIX

BACKUP_DEFAULT_RETENTION = 7


# Stale-lock timeouts. An mtime lease cannot tell "crashed" from "slower than
# the timeout", so both are set far above any realistic run; the worst case if
# a genuinely-slow holder is broken is two concurrent runs (today's behavior),
# never corruption.
#   backup: a snapshot of this store takes well under a second; 10 minutes
#     covers a pathologically large store on a slow/contended disk.
#   consolidate: clustering is O(n^2) over live rows plus optional embedding
#     work, and is the longer of the two by design, so it gets 30 minutes —
#     deliberately longer than backup's, per the P11 spec.
BACKUP_LOCK_STALE_SECONDS = _env_float("ZMEM_BACKUP_LOCK_STALE", 600.0)
CONSOLIDATE_LOCK_STALE_SECONDS = _env_float("ZMEM_CONSOLIDATE_LOCK_STALE", 1800.0)


class SnapshotError(RuntimeError):
    """A snapshot could not be produced, or failed verification. When raised by
    verify_snapshot()/create_snapshot() the bad snapshot file has already been
    deleted — a file left in the backup dir is always a verified one."""


def _lock_path(name: str) -> Path:
    """Advisory lockfile for a single-flight command, in the store dir."""
    return STORE_PATH.parent / f".zmem-{name}.lock"


def _acquire_lock(name: str, stale_seconds: float) -> str | None:
    """Take the single-flight lock for `name`. None => another run holds it and
    the caller must skip. Proceeds unlocked (returns a token) when host.py is
    unavailable — `_host is None` is a supported degraded mode in this file."""
    if _host is None:
        return "unlocked"
    return _host.acquire_lock(_lock_path(name), stale_seconds)


def _release_lock(name: str, token: str | None) -> None:
    if _host is None or not token:
        return
    _host.release_lock(_lock_path(name), token)


def _backup_dir(out_dir: str | None = None) -> Path:
    """--out-dir > $ZMEM_BACKUP_DIR > <store dir>/backups."""
    if out_dir:
        return Path(out_dir).expanduser()
    env_dir = os.environ.get("ZMEM_BACKUP_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir).expanduser()
    return STORE_PATH.parent / "backups"


def _ensure_backup_dir(out_dir: str | None = None) -> Path:
    """Resolve + create the backup dir, hardening perms only on first creation
    (mirrors connect()'s gate — icacls on every run would be a needless cost on
    a path the SessionStart hook touches every session)."""
    d = _backup_dir(out_dir)
    existed = d.is_dir()
    d.mkdir(parents=True, exist_ok=True)
    if not existed and _host is not None:
        # Dirs get (OI)(CI) so snapshots created inside inherit the ACL.
        _host.set_owner_only_perms(d)
    return d


def _snapshot_stamp() -> str:
    """Filename-safe UTC timestamp: now_iso()'s clock, minus the colons that
    are illegal in Windows filenames."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _new_snapshot_path(backup_dir: Path, prefix: str) -> Path:
    """A not-yet-existing snapshot path. Two snapshots inside the same wall
    clock second get a `-N` uniquifier rather than silently overwriting each
    other (retention orders by mtime, so the suffix never confuses rotation)."""
    stamp = _snapshot_stamp()
    p = backup_dir / f"{prefix}{stamp}{SNAPSHOT_SUFFIX}"
    n = 1
    while p.exists():
        p = backup_dir / f"{prefix}{stamp}-{n}{SNAPSHOT_SUFFIX}"
        n += 1
    return p


def _row_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(total, live) row counts of the memory table."""
    total = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    live = conn.execute(
        "SELECT count(*) FROM memory WHERE superseded_at IS NULL"
    ).fetchone()[0]
    return int(total), int(live)


def counts_agree(
    before: tuple[int, int], after: tuple[int, int], snap: tuple[int, int]
) -> tuple[bool, str]:
    """Does the snapshot's (total, live) row count match the source's?

    Pure function so the concurrent-writer branch is directly testable — it is
    otherwise unreachable without racing a real writer.

    Source counts are sampled BEFORE and AFTER the page copy. In the common
    case nothing committed in between (before == after) and the snapshot must
    match EXACTLY — anything else means pages went missing and the snapshot is
    rejected. If a concurrent writer did commit (before != after), the snapshot
    legitimately captured some instant between the two samples, so each count
    only has to land within the observed band. `total` is monotonically
    non-decreasing (nothing ever DELETEs from `memory`; supersession is a
    tombstone UPDATE) while `live` can move either way, hence min/max bounds
    rather than a direction assumption.
    """
    if before == after:
        if snap == before:
            return True, f"exact match (total={snap[0]} live={snap[1]})"
        return False, (
            f"row count mismatch: snapshot total={snap[0]} live={snap[1]} "
            f"vs source total={before[0]} live={before[1]}"
        )
    lo = (min(before[0], after[0]), min(before[1], after[1]))
    hi = (max(before[0], after[0]), max(before[1], after[1]))
    ok = all(lo[i] <= snap[i] <= hi[i] for i in (0, 1))
    band = (
        f"source changed during snapshot (before total={before[0]} live={before[1]}, "
        f"after total={after[0]} live={after[1]}); snapshot total={snap[0]} live={snap[1]}"
    )
    return ok, (band + " - within band" if ok else band + " - OUTSIDE band")


SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _discard_snapshot(path: Path) -> None:
    """Delete a snapshot that failed verification, plus any sidecars it left.
    Best-effort: a file we cannot delete is reported by the caller's error, and
    is never counted as a successful backup."""
    for candidate in [path] + [Path(str(path) + s) for s in SIDECAR_SUFFIXES]:
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


def verify_snapshot(
    snapshot_path: Path, src_before: tuple[int, int], src_after: tuple[int, int]
) -> dict:
    """PRAGMA integrity_check + row-count comparison on a freshly written
    snapshot. On ANY failure the snapshot file is DELETED and SnapshotError is
    raised, so a caller can never mistake a bad snapshot for a good one (and
    the caller must therefore not update `last_backup` or run retention)."""
    try:
        conn = sqlite3.connect(str(snapshot_path))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            snap = _row_counts(conn)
        finally:
            conn.close()
    except Exception as e:  # unreadable / not a database / no memory table
        _discard_snapshot(snapshot_path)
        raise SnapshotError(f"snapshot could not be verified: {e}") from e

    if integrity != "ok":
        _discard_snapshot(snapshot_path)
        raise SnapshotError(f"snapshot integrity_check failed: {integrity}")

    ok, note = counts_agree(src_before, src_after, snap)
    if not ok:
        _discard_snapshot(snapshot_path)
        raise SnapshotError(note)

    return {
        "path": str(snapshot_path),
        "integrity_check": integrity,
        "snapshot_total": snap[0],
        "snapshot_live": snap[1],
        "source_total": src_after[0],
        "source_live": src_after[1],
        "count_note": note,
    }


def create_snapshot(src_path: Path, dest_path: Path) -> dict:
    """Online-backup `src_path` to `dest_path`, then verify it.

    Safe against concurrent writers on the source: the page copy runs under
    SQLite's own locking (busy_timeout + the backup API's built-in retry), and
    the source is never checkpointed, renamed, or otherwise mutated by us.
    Raises SnapshotError (having deleted the partial/bad destination) if the
    copy or the verification fails.
    """
    if not src_path.exists() or src_path.stat().st_size == 0:
        raise SnapshotError(f"source store missing or empty: {src_path}")

    try:
        src = sqlite3.connect(str(src_path))
    except sqlite3.Error as e:
        raise SnapshotError(f"cannot open source store {src_path}: {e}") from e
    try:
        src.execute("PRAGMA busy_timeout=5000")
        before = _row_counts(src)
        dst = sqlite3.connect(str(dest_path))
        try:
            # pages=-1 (default) copies the whole db; sleep=0.25 is the
            # built-in wait between SQLITE_BUSY retries.
            src.backup(dst)
            # The page copy includes page 1, so the snapshot inherits the
            # source's WAL journal mode. Flip it back to the rollback journal:
            # a WAL-mode snapshot makes every later READER (our own verify,
            # the restore pre-flight, a human running sqlite3 on it) create
            # `-wal`/`-shm` sidecars beside it — and a read-only reader cannot
            # checkpoint them away on close, so they persist as orphans in the
            # backup dir and get dragged along by a restore. A rollback-journal
            # snapshot is a single self-contained file with no sidecars at all.
            # `restore` puts the destination back into WAL right after the copy.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
        after = _row_counts(src)
    except Exception as e:
        _discard_snapshot(dest_path)
        raise SnapshotError(f"snapshot copy failed: {e}") from e
    finally:
        src.close()

    return verify_snapshot(dest_path, before, after)


def list_snapshots(backup_dir: Path) -> list[Path]:
    """Snapshots matching the retention glob, oldest first. Ordered by mtime
    (then name) rather than by filename alone, so the same-second `-N`
    uniquifier can never invert chronological order."""
    items: list[tuple[int, str, Path]] = []
    try:
        candidates = list(backup_dir.glob(SNAPSHOT_GLOB))
    except OSError:
        return []
    for p in candidates:
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        items.append((st.st_mtime_ns, p.name, p))
    items.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in items]


def apply_retention(backup_dir: Path, retention: int) -> list[Path]:
    """Delete the OLDEST `store-*.sqlite` snapshots beyond the newest
    `retention`. Never touches anything else in the directory — a file that
    does not match the glob (including every `prerestore-*` safety copy) is
    left strictly alone. `retention <= 0` disables pruning entirely.

    A pruned snapshot's OWN sidecars (`-wal`/`-shm`/`-journal`) go with it:
    they are files we created, not unrelated ones, and leaving them would
    orphan them forever (the glob cannot match them).
    """
    if retention is None or retention <= 0:
        return []
    snaps = list_snapshots(backup_dir)
    if len(snaps) <= retention:
        return []
    removed: list[Path] = []
    for p in snaps[: len(snaps) - retention]:
        try:
            p.unlink()
        except OSError as e:
            print(f"[zmem] backup: could not prune {p}: {e}", file=sys.stderr)
            continue
        removed.append(p)
        for suffix in SIDECAR_SUFFIXES:
            sib = Path(str(p) + suffix)
            try:
                if sib.exists():
                    sib.unlink()
            except OSError:
                pass
    return removed


def _backup_interval_days() -> float:
    """$ZMEM_BACKUP_INTERVAL_DAYS (default 1). Read per call so tests and the
    launcher can change it without re-importing."""
    v = _env_float("ZMEM_BACKUP_INTERVAL_DAYS", 1.0)
    return v if v >= 0 else 1.0


def _backup_due(conn: sqlite3.Connection) -> tuple[bool, float, float]:
    """(due, days_since_last, interval_days) from the `last_backup` meta row.
    A missing/unparseable timestamp means due (fail toward taking a backup)."""
    interval = _backup_interval_days()
    row = conn.execute("SELECT value FROM meta WHERE key='last_backup'").fetchone()
    if not row or not row[0]:
        return True, -1.0, interval
    last_epoch = _parse_iso_to_epoch(row[0])
    if last_epoch <= 0:
        return True, -1.0, interval
    days = (time.time() - last_epoch) / 86400.0
    return days >= interval, days, interval


def cmd_backup(
    conn: sqlite3.Connection,
    *,
    retention: int = BACKUP_DEFAULT_RETENTION,
    out_dir: str | None = None,
    if_due: bool = False,
) -> int:
    """Take a verified snapshot of the store; return a process exit code.

    Single-flighted against other `backup` runs (the SessionStart hook fires
    one detached per session, and a human may run one by hand at the same
    moment). `--if-due` gates on the `last_backup` meta row so the hook's
    automatic trigger is a cheap no-op almost every session; without it the
    backup always runs, which is what direct CLI and verification runs want.
    """
    token = _acquire_lock("backup", BACKUP_LOCK_STALE_SECONDS)
    if token is None:
        print("[zmem] backup: another backup is already running - skipped")
        return 0
    try:
        if if_due:
            due, days, interval = _backup_due(conn)
            if not due:
                print(f"[zmem] backup: not due - last backup {days:.3f}d ago, "
                      f"interval {interval}d; skipped")
                return 0

        try:
            backup_dir = _ensure_backup_dir(out_dir)
            dest = _new_snapshot_path(backup_dir, SNAPSHOT_PREFIX)
            info = create_snapshot(STORE_PATH, dest)
        except (SnapshotError, OSError) as e:
            print(f"[zmem] backup FAILED: {e}", file=sys.stderr)
            print("[zmem] backup: bad snapshot deleted; last_backup NOT updated; "
                  "retention NOT applied", file=sys.stderr)
            return 1

        print(f"[zmem] backup: snapshot {info['path']}")
        print(f"[zmem] backup: integrity_check={info['integrity_check']}")
        print(f"[zmem] backup: rows snapshot total={info['snapshot_total']} "
              f"live={info['snapshot_live']} vs source total={info['source_total']} "
              f"live={info['source_live']} - {info['count_note']}")

        # Only now — after both checks passed — is this a successful backup.
        # This is bookkeeping for `--if-due`, not part of the snapshot: the
        # file on disk is already written and verified. A write/commit failure
        # here must not escape as an unhandled traceback past `finally:
        # _release_lock` and make a good backup look like a failed one. The
        # only consequence of a missed row is that the next `--if-due` run
        # takes an extra snapshot, which is the safe direction to fail.
        try:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_backup', ?)",
                (now_iso(),),
            )
            _commit(conn)
        except sqlite3.Error as e:
            print(f"[zmem] backup: WARNING - snapshot is good but recording "
                  f"last_backup failed ({e}); the next --if-due run will take "
                  f"another snapshot", file=sys.stderr)

        removed = apply_retention(backup_dir, retention)
        kept = len(list_snapshots(backup_dir))
        if removed:
            print(f"[zmem] backup: retention={retention} pruned {len(removed)} "
                  f"old snapshot(s): {', '.join(p.name for p in removed)}")
        print(f"[zmem] backup: {kept} snapshot(s) retained in {backup_dir}")
        return 0
    finally:
        _release_lock("backup", token)


def _integrity_check_readonly(path: Path) -> str:
    """PRAGMA integrity_check on `path` opened STRICTLY read-only (uri mode=ro),
    so a pre-flight check can never mutate the user's snapshot. Raises
    sqlite3.Error / OSError on failure.

    Our own snapshots are rollback-journal files, so a reader creates no
    sidecars. A hand-supplied WAL-mode snapshot is different: opening it (even
    read-only) makes SQLite materialize `-shm`/`-wal`, and a read-only
    connection cannot checkpoint them away on close. Remove exactly the
    sidecars that did NOT exist before we looked — never one that was already
    there, since that one may hold real committed frames.
    """
    pre_existing = {s for s in SIDECAR_SUFFIXES if Path(str(path) + s).exists()}
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
        for s in SIDECAR_SUFFIXES:
            if s in pre_existing:
                continue
            try:
                sib = Path(str(path) + s)
                if sib.exists():
                    sib.unlink()
            except OSError:
                pass


def cmd_restore(*, from_path: str, force: bool = False, out_dir: str | None = None) -> int:
    """Restore the store from a snapshot, under the maintenance locks.

    Two guards wrap the real work in `_restore_locked`:

    LOCAL-FS GUARD — the destination is a live WAL-mode sqlite file; a UNC path,
    a network-mapped drive, or a OneDrive-synced dir risks exactly the
    corruption `connect()` already refuses to court. `restore` writes the very
    same file, so it applies the very same guard.

    SINGLE-FLIGHT — `restore` overwrites store.sqlite wholesale while the two
    automated background writers the SessionStart hook fires (`backup --if-due`
    and `consolidate`) may be mid-run against it. Both are already
    single-flighted on their own lockfiles, so restore takes BOTH, in a fixed
    order, for its whole duration. Losing either means an automated job is
    running right now: restore refuses (exit 2) WITHOUT touching the
    destination, rather than proceeding. Unlike `backup`, whose lock loss is a
    benign "someone else already did it" skip worth exit 0, a silently skipped
    restore would tell a human "done" while their data is untouched.

    MAINTENANCE / WRITER PROTOCOL — restore now acquires a maintenance lock
    first. New store commands wait briefly and then fail clearly while that lock
    is held, and any writer already in flight must carry a live lease file.
    Restore checks those leases before it touches the destination; if any are
    still live, it refuses and leaves the destination untouched.
    """
    dest = STORE_PATH
    try:
        _read_schema_version(dest)
        _read_schema_version(Path(from_path).expanduser())
    except RuntimeError as e:
        print(f"[zmem] restore FAILED: {e}", file=sys.stderr)
        return 2
    if _host is not None:
        try:
            _host.assert_local_fs(dest.parent)
        except ValueError as e:
            print(f"[zmem] restore FAILED: {e}", file=sys.stderr)
            return 1

    m_token = _strict_acquire_lock(
        "maintenance",
        MAINTENANCE_LOCK_STALE_SECONDS,
        wait_seconds=0.0,
    )
    if m_token is None:
        print("[zmem] restore REFUSED: another maintenance operation is currently "
              "running - destination untouched; re-run when it finishes",
              file=sys.stderr)
        return 2
    s_token = _strict_acquire_lock(
        "schema",
        SCHEMA_LOCK_STALE_SECONDS,
        wait_seconds=SCHEMA_LOCK_WAIT_SECONDS,
        poll_seconds=SCHEMA_LOCK_POLL_SECONDS,
    )
    if s_token is None:
        _release_named_lock("maintenance", m_token)
        print("[zmem] restore REFUSED: store initialization or migration is "
              "currently running - destination untouched; re-run when it finishes",
              file=sys.stderr)
        return 2

    # Fixed acquisition order (backup, then consolidate); nothing else in this
    # file takes both, so no deadlock is possible, and a half-acquired pair is
    # always released before returning.
    b_token = _acquire_lock("backup", BACKUP_LOCK_STALE_SECONDS)
    if b_token is None:
        _release_named_lock("schema", s_token)
        _release_named_lock("maintenance", m_token)
        print("[zmem] restore REFUSED: a backup is currently running - "
              "destination untouched; re-run when it finishes", file=sys.stderr)
        return 2
    c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
    if c_token is None:
        _release_lock("backup", b_token)
        _release_named_lock("schema", s_token)
        _release_named_lock("maintenance", m_token)
        print("[zmem] restore REFUSED: a consolidation is currently running - "
              "destination untouched; re-run when it finishes", file=sys.stderr)
        return 2
    try:
        live_writers = _cleanup_stale_writer_leases()
        if live_writers:
            print("[zmem] restore REFUSED: a normal writer is currently active - "
                  "destination untouched; re-run when it finishes", file=sys.stderr)
            for lease in live_writers[:5]:
                print(f"[zmem]   active writer lease: {lease.name}", file=sys.stderr)
            return 2
        return _restore_locked(from_path=from_path, force=force, out_dir=out_dir)
    finally:
        _release_lock("consolidate", c_token)
        _release_lock("backup", b_token)
        _release_named_lock("schema", s_token)
        _release_named_lock("maintenance", m_token)


def _restore_locked(*, from_path: str, force: bool = False, out_dir: str | None = None) -> int:
    """The restore body. Called only by cmd_restore(), which holds the `backup`
    and `consolidate` locks for the whole of it. Returns a process exit code.

    Deliberately NOT routed through main()'s connect()/init_db()/migrate()
    path (see main()'s dispatch, which branches on `restore` before those
    calls, exactly as `failures` does): on Windows an open sqlite3 connection
    holds a file handle on the destination, and overwriting a file another
    connection in this same process has open is precisely the thing to avoid.
    Every connection this function opens is closed before the next step.

    Order is load-bearing: verify the snapshot -> take the pre-restore safety
    copy of the CURRENT store (which must still have its `-wal` frames intact,
    so this happens BEFORE any sidecar removal) -> clear stale sidecars ->
    copy -> checkpoint + re-verify.
    """
    snap = Path(from_path).expanduser()
    dest = STORE_PATH

    if not snap.is_file():
        print(f"[zmem] restore FAILED: snapshot not found: {snap}", file=sys.stderr)
        return 1

    # --- 1. Verify the snapshot BEFORE touching the destination at all. ---
    try:
        snap_integrity = _integrity_check_readonly(snap)
    except Exception as e:
        print(f"[zmem] restore FAILED: cannot read snapshot {snap} read-only: {e}",
              file=sys.stderr)
        print("[zmem] restore: (a snapshot with a hot -wal sidecar must be "
              "checkpointed before it can be restored)", file=sys.stderr)
        return 1
    if snap_integrity != "ok":
        print(f"[zmem] restore FAILED: snapshot integrity_check={snap_integrity} "
              f"- destination untouched", file=sys.stderr)
        return 1
    print(f"[zmem] restore: source snapshot {snap}")
    print(f"[zmem] restore: snapshot integrity_check={snap_integrity}")

    dest_exists = dest.exists()
    if dest_exists and not force:
        print(f"[zmem] restore FAILED: destination store already exists: {dest}\n"
              f"       pass --force to overwrite it (a pre-restore backup of the "
              f"current store is taken automatically).", file=sys.stderr)
        return 1

    # --- 2. Pre-restore safety copy of the CURRENT store, before any sidecar
    # removal so its WAL frames are still part of the online-backup snapshot. ---
    prerestore_path = None
    if dest_exists and dest.stat().st_size > 0:
        try:
            backup_dir = _ensure_backup_dir(out_dir)
            # Prefix is outside SNAPSHOT_GLOB on purpose — automatic rotation
            # must never prune the copy this restore's rollback depends on.
            pre_dest = _new_snapshot_path(backup_dir, PRERESTORE_PREFIX)
            pre_info = create_snapshot(dest, pre_dest)
            prerestore_path = pre_info["path"]
            print(f"[zmem] restore: pre-restore backup {prerestore_path} "
                  f"(integrity_check={pre_info['integrity_check']}, "
                  f"total={pre_info['snapshot_total']} live={pre_info['snapshot_live']})")
        except Exception as e:
            print(f"[zmem] restore ABORTED: pre-restore backup of the current store "
                  f"failed ({e}) — refusing to overwrite the only copy of it.",
                  file=sys.stderr)
            return 1
    elif dest_exists:
        print("[zmem] restore: pre-restore backup skipped - destination store is "
              "an empty file (nothing to preserve)")
    else:
        print("[zmem] restore: pre-restore backup skipped - destination store "
              "does not exist yet")

    # --- 3. Clear stale sidecars, then copy. Old `-wal` frames (and any hot
    # `-journal`) belong to the PREVIOUS store; leaving them next to a restored
    # main file would let SQLite replay them onto it. ---
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in SIDECAR_SUFFIXES:
        sib = Path(str(dest) + suffix)
        try:
            if sib.exists():
                sib.unlink()
                print(f"[zmem] restore: removed stale {sib.name}")
        except OSError as e:
            print(f"[zmem] restore FAILED: could not remove stale {sib.name}: {e}",
                  file=sys.stderr)
            # Sidecar removal is partial at this point, so the destination may
            # already be inconsistent — the user needs the rollback path, same
            # as every other post-pre-restore-backup failure branch below.
            if prerestore_path:
                print(f"[zmem] restore: roll back with "
                      f"`store.py restore --from {prerestore_path} --force`", file=sys.stderr)
            return 1

    try:
        shutil.copy2(snap, dest)
        # A hand-supplied snapshot may carry its own sidecars; ours never do.
        for suffix in ("-wal", "-shm"):
            src_sib = Path(str(snap) + suffix)
            if src_sib.exists():
                shutil.copy2(src_sib, Path(str(dest) + suffix))
                print(f"[zmem] restore: copied snapshot {src_sib.name}")
    except OSError as e:
        print(f"[zmem] restore FAILED: copy failed: {e}", file=sys.stderr)
        # The copy may have been partial, so the destination store is not
        # trustworthy — point at the safety copy taken in step 2.
        if prerestore_path:
            print(f"[zmem] restore: roll back with "
                  f"`store.py restore --from {prerestore_path} --force`", file=sys.stderr)
        return 1

    # --- 4. Post-copy verification on the restored destination. ---
    try:
        conn = sqlite3.connect(str(dest))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            post_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            total, live = _row_counts(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[zmem] restore FAILED: restored store could not be verified: {e}",
              file=sys.stderr)
        if prerestore_path:
            print(f"[zmem] restore: roll back with "
                  f"`store.py restore --from {prerestore_path} --force`", file=sys.stderr)
        return 1

    if _host is not None:
        _host.set_owner_only_perms(dest)

    print(f"[zmem] restore: destination {dest}")
    print(f"[zmem] restore: post-restore integrity_check={post_integrity}")
    print(f"[zmem] restore: restored rows total={total} live={live}")
    if post_integrity != "ok":
        print(f"[zmem] restore FAILED: post-restore integrity_check={post_integrity}",
              file=sys.stderr)
        if prerestore_path:
            print(f"[zmem] restore: roll back with "
                  f"`store.py restore --from {prerestore_path} --force`", file=sys.stderr)
        return 1
    print("[zmem] restore: OK")
    return 0


# ---------------------------------------------------------------------------
# Unified failure detection (`failures` subcommand)
# ---------------------------------------------------------------------------
# Detects failed tool calls for a session from one of two substrates:
#   - a Claude Code transcript JSONL (--transcript) — scanned for tool_result
#     blocks with is_error:true (and/or a top-level toolUseResult "Error…"), OR
#   - the ZCode episodic db.sqlite (--session) — the tool_usage table query the
#     reflect hook used to run inline.
# Returns {"count": N, "details": [...]} JSON. FAIL-OPEN: on ANY error or parse
# failure it returns an empty result — a memory hiccup must never wedge a hook.
#
# The untrusted-error-text fencing lives here (`_sanitize_error_text`): each
# detail's error string is stripped of newlines/CR and truncated. Newline
# removal is the load-bearing fence-integrity mechanism — with no newlines, an
# embedded ``` or fake "SYSTEM:" directive in tool output can never form its own
# line and therefore can never break out of the code fence the consumer wraps it
# in. The consumer (reflect.sh) does the ``` fence-wrap + "untrusted data only"
# framing; this function guarantees the strings it hands back are fence-safe.

def _collapse_line_breaks(text) -> str:
    """Collapse every CR/LF (and Unicode line separator) in `text` to a
    single space.

    The shared fence-integrity primitive: with no newlines, untrusted text can
    never start its own line, so it can never form a fence-close, a markdown
    heading, a list bullet, or a line-oriented directive of any kind. Used by
    _sanitize_error_text (hook output) and _sanitize_pack_content (export-pack
    bullets) — one primitive, two consumers, so the guarantee cannot drift.

    Also collapses U+2028 (LINE SEPARATOR), U+2029 (PARAGRAPH SEPARATOR), and
    U+0085 (NEXT LINE) -- these are treated as line breaks by str.splitlines()
    and some renderers even though they are not \\r/\\n, so a memory row
    carrying one of them could otherwise still open its own visual "line"
    inside a pack bullet.
    """
    if not text:
        return ""
    return (str(text).replace("\r", " ").replace("\n", " ")
            .replace("\u2028", " ").replace("\u2029", " ").replace("\u0085", " "))


def _sanitize_error_text(text, limit: int = 200) -> str:
    """Make an untrusted tool-error string safe to embed in a fenced block:
    collapse CR/newlines to spaces (fence-integrity), then truncate. Preserves
    the error's characters otherwise so it stays diagnostically useful."""
    if not text:
        return ""
    return _collapse_line_breaks(text)[:limit].strip()


def _sanitize_pack_content(text) -> str:
    """Make a stored memory field safe to render as one export-pack bullet.

    Memory content is UNTRUSTED for this purpose: a Tier 3 sync file is
    remote-authored, and the pack it feeds is read verbatim by other agents
    as instructions-adjacent context. Without this, one row could close the
    generated markdown's structure and inject its own — a prompt-injection
    surface, not just a formatting bug.

    Three neutralizations, no truncation (unlike _sanitize_error_text: a pack
    bullet must render the whole memory or the pack silently lies):
      - CR/LF -> spaces, via the shared _collapse_line_breaks primitive. This
        is the load-bearing one: it alone stops a row from emitting its own
        '## heading', '- bullet', or leading-'#' line.
      - ``` -> ''' so a row cannot open/close a code fence a consumer wrapped
        the pack in.
      - '<!--' / '-->' spaced apart, so a row cannot close the pack's own
        auto-generated HTML comment header or comment out the rest of it.
    """
    s = _collapse_line_breaks(text)
    s = s.replace("```", "'''")
    s = s.replace("<!--", "<!- -").replace("-->", "-- >")
    return s.strip()


def _sanitize_tool_name(name, limit: int = 100) -> str:
    """Defense-in-depth (Phase 8): strip CR/newlines from a tool name before it
    is interpolated into a fenced block by reflect.sh/subagent-reflect.sh. Not
    currently exploitable — tool names come from the harness's own tool_use
    blocks / tool_usage rows, not from untrusted tool output — but a newline
    here would let a forged fence-close ('\\n```') slip past the same
    fence-integrity guarantee _sanitize_error_text gives the error text."""
    if not name:
        return "?"
    s = str(name).replace("\r", " ").replace("\n", " ").strip()
    return s[:limit] or "?"


def _result_text(content) -> str:
    """Extract text from a tool_result block's `content`, which CC emits as
    either a plain string or a list of {type:"text", text:"..."} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(p for p in parts if p)
    return ""


def _failures_from_transcript(path: str):
    """Scan a Claude Code transcript JSONL for failed tool calls. Returns a list
    of {tool, error} dicts (one per distinct failed tool_use_id). Fail-open:
    returns [] on any read/parse error. Never raises."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw_lines = [ln for ln in f if ln.strip()]
    except OSError:
        return []

    records = []
    for ln in raw_lines:
        try:
            records.append(json.loads(ln))
        except Exception:
            continue  # skip malformed lines, keep scanning

    # Pass 1: map tool_use_id -> tool name from assistant tool_use blocks.
    tool_names = {}
    for o in records:
        msg = o.get("message") if isinstance(o, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id")
                    if tid:
                        tool_names[tid] = b.get("name") or "?"

    # Pass 2: collect failed tool_result blocks, deduped by tool_use_id so the
    # is_error flag and the sibling toolUseResult "Error…" string on the same
    # record never double-count one failure.
    details = []
    seen = set()
    for o in records:
        if not isinstance(o, dict):
            continue
        msg = o.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        tur = o.get("toolUseResult")
        tur_is_err = isinstance(tur, str) and tur.strip().lower().startswith("error")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            is_err = b.get("is_error") is True
            if not (is_err or tur_is_err):
                continue
            tid = b.get("tool_use_id")
            key = tid if tid else ("anon:%d" % len(seen))
            if key in seen:
                continue
            seen.add(key)
            err_text = _result_text(b.get("content"))
            if not err_text and isinstance(tur, str):
                err_text = tur
            details.append({
                "tool": _sanitize_tool_name(tool_names.get(tid, "?")),
                "error": _sanitize_error_text(err_text),
            })
    return details


def _failures_from_db(db_path: str, session_id: str):
    """Detect failed tool calls for a session from the ZCode episodic db.sqlite.
    Returns (count, details). The load-bearing detection uses ONLY the columns
    the original reflect query used (session_id, read_only, status, exit_code);
    enrichment columns (error_message, error_type, retry_count, destructive) are
    read in a SEPARATE try/except so a schema drift degrades to bare counts but
    never disables detection. Fail-open: (0, []) on any error."""
    if not session_id or not db_path or not os.path.isfile(db_path):
        return 0, []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT count(*) FROM tool_usage
            WHERE session_id = ?
              AND COALESCE(read_only, 0) = 0
              AND (status = 'error' OR (exit_code IS NOT NULL AND exit_code != 0))
            """,
            (session_id,),
        ).fetchone()
        count = row[0] if row else 0
    except Exception:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return 0, []

    if count == 0:
        try:
            conn.close()
        except Exception:
            pass
        return 0, []

    details = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT tool_name, error_message, error_type, retry_count,
                   COALESCE(destructive, 0) AS destructive
            FROM tool_usage
            WHERE session_id = ?
              AND COALESCE(read_only, 0) = 0
              AND (status = 'error' OR (exit_code IS NOT NULL AND exit_code != 0))
            ORDER BY completed_at DESC
            LIMIT 50
            """,
            (session_id,),
        ).fetchall()
        for r in rows:
            details.append({
                "tool": _sanitize_tool_name(r["tool_name"] or "?"),
                "error": _sanitize_error_text(r["error_message"] or ""),
                "error_type": r["error_type"] or "",
                "retry_count": r["retry_count"] or 0,
                "destructive": bool(r["destructive"]),
            })
    except Exception:
        # Enrichment columns absent — detection already succeeded, so surface
        # bare placeholders so the count is still actionable.
        details = [{"tool": "?", "error": ""} for _ in range(count)]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return count, details


def cmd_failures(session: str, transcript: str, db: str) -> None:
    """Print {"count":N,"details":[...]} for the session's failed tool calls.
    Transcript wins when given and present (Claude Code); else the db substrate
    (ZCode). Entirely self-contained — does NOT open the ZMem store — and
    fail-open: prints an empty result on any error, always exits 0."""
    try:
        if transcript and os.path.isfile(transcript):
            details = _failures_from_transcript(transcript)
            result = {"count": len(details), "details": details}
        else:
            count, details = _failures_from_db(db, session)
            result = {"count": count, "details": details}
    except Exception:
        result = {"count": 0, "details": []}
    print(json.dumps(result))


# --- Tier 1 export: markdown memory pack (P.export-pack) -------------------

GLOBAL_NAMESPACE = "user:global"
EXPORT_PACK_DEFAULT_PROJECT_LIMIT = 50
EXPORT_PACK_DEFAULT_GLOBAL_LIMIT = 15
EXPORT_PACK_DEFAULT_MIN_CONFIDENCE = 0.6
EXPORT_PACK_DEFAULT_MAX_BYTES = 32768


def _pack_query(conn: sqlite3.Connection, namespace: str, limit: int, min_confidence: float):
    """Live rows for one export-pack section, best candidates first."""
    return conn.execute(
        """SELECT type, signal, content FROM memory
           WHERE namespace=? AND superseded_at IS NULL AND confidence >= ?
           ORDER BY confidence DESC, retrieval_count DESC, ingestion_ts DESC
           LIMIT ?""",
        (namespace, min_confidence, limit),
    ).fetchall()


def _render_pack(namespace: str, store_path: str, project_rows, global_rows, max_bytes: int) -> str:
    """Render the Tier 1 markdown memory pack.

    Bullets are added greedily in document order (project section, then the
    global one). Each row is tested INDIVIDUALLY against the budget: if adding
    that bullet would push the UTF-8-encoded output past max_bytes it is
    skipped and counted toward the trailing omitted-count note, and the walk
    CONTINUES -- a later, smaller row still gets its bullet. (Skipping the
    rest of the pack after the first oversized row would let one long memory
    silently delete every other memory from the pack.) A bullet is only ever
    added whole, never truncated.

    Structural text (header comment, titles, section headings, the "(none)"
    placeholder, the omitted-count note itself) is exempt from the cap: it is
    small, mandatory framing, not budget-controlled content. max_bytes is
    therefore a budget over emitted bullet lines, not a hard cap on the file.

    Every rendered field goes through _sanitize_pack_content first: pack rows
    can be remote-authored (Tier 3 sync) and the pack is read as context by
    other agents, so no row may break out of its bullet.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    lines: list[str] = [
        f"<!-- Auto-generated by `zmem export-pack` from {store_path} on {today}. "
        "Do not hand-edit -- this file is overwritten by the next export-pack run. -->",
        f"# Memory pack: {namespace}",
        "",
    ]

    state = {"omitted": 0}

    def _emit_section(title: str, rows) -> None:
        lines.append(f"## {title}")
        if not rows:
            lines.append("(none)")
            lines.append("")
            return
        for row in rows:
            bullet = (
                f"- **[{_sanitize_pack_content(row['type'])}"
                f"/{_sanitize_pack_content(row['signal'])}]** "
                f"{_sanitize_pack_content(row['content'])}"
            )
            projected = len(("\n".join(lines + [bullet]) + "\n").encode("utf-8"))
            if projected > max_bytes:
                # Per-row skip, NOT a sticky stop: a single oversized memory
                # must not evict every smaller row behind it (including the
                # whole user:global section).
                state["omitted"] += 1
                continue
            lines.append(bullet)
        lines.append("")

    _emit_section("Project knowledge", project_rows)
    _emit_section(f"Cross-project lessons ({GLOBAL_NAMESPACE})", global_rows)

    if state["omitted"]:
        lines.append(
            f"*({state['omitted']} row(s) omitted to stay within --max-bytes={max_bytes})*"
        )

    return "\n".join(lines) + "\n"


def cmd_export_pack(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    out: str | None = None,
    project_limit: int = EXPORT_PACK_DEFAULT_PROJECT_LIMIT,
    global_limit: int = EXPORT_PACK_DEFAULT_GLOBAL_LIMIT,
    min_confidence: float = EXPORT_PACK_DEFAULT_MIN_CONFIDENCE,
    max_bytes: int = EXPORT_PACK_DEFAULT_MAX_BYTES,
) -> int:
    """Write (or print) the Tier 1 markdown memory pack for `namespace`.
    Returns a process exit code."""
    project_rows = _pack_query(conn, namespace, project_limit, min_confidence)
    global_rows = _pack_query(conn, GLOBAL_NAMESPACE, global_limit, min_confidence)

    if not project_rows and not global_rows:
        print(
            f"[zmem] export-pack: no live memories at/above confidence "
            f"{min_confidence} in namespace={namespace} (or {GLOBAL_NAMESPACE}) "
            "-- an empty pack is almost certainly a wrong --namespace; refusing",
            file=sys.stderr,
        )
        return 2

    text = _render_pack(namespace, str(STORE_PATH), project_rows, global_rows, max_bytes)

    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" disables Python's write-side newline translation so the
        # file keeps LF endings on Windows too (quiet diffs for a regenerated,
        # checked-in pack).
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"[zmem] export-pack: wrote {out_path} ({len(text.encode('utf-8'))} bytes)")
    else:
        sys.stdout.write(text)
    return 0


# --- Tier 3 export/import: JSONL sync (P.export-jsonl / P.ingest-jsonl) ----


def cmd_export_jsonl(
    conn: sqlite3.Connection,
    *,
    out: str | None = None,
    namespace: str | None = None,
    include_superseded: bool = False,
) -> int:
    """Write one JSON object per line for LIVE rows (plus tombstoned rows too
    when include_superseded), sorted by ingestion_ts then id for deterministic
    diffs. No embedding fields -- bytes are not portable across machines;
    receivers rebuild via `reembed`."""
    clauses = []
    params: list = []
    if namespace:
        clauses.append("namespace=?")
        params.append(namespace)
    if not include_superseded:
        clauses.append("superseded_at IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"""SELECT id, namespace, type, content, tags, source_ref, confidence, signal,
                   valid_from, ingestion_ts, superseded_at, supersede_reason, merged_from
            FROM memory {where}
            ORDER BY ingestion_ts, id""",
        params,
    ).fetchall()

    lines = []
    for r in rows:
        obj = {
            "id": r["id"],
            "namespace": r["namespace"],
            "type": r["type"],
            "content": r["content"],
            "tags": r["tags"],
            "source_ref": r["source_ref"],
            "confidence": r["confidence"],
            "signal": r["signal"],
            "valid_from": r["valid_from"],
            "ingestion_ts": r["ingestion_ts"],
            "superseded_at": r["superseded_at"],
            "supersede_reason": r["supersede_reason"],
            "merged_from": r["merged_from"],
        }
        line = json.dumps(obj, ensure_ascii=False)
        # json.dumps already escapes every codepoint < 0x20 (\n, \r, and any
        # other ASCII control char), but U+2028/U+2029/U+0085 are line
        # terminators recognized by str.splitlines() (and some JS/JSONL
        # consumers) yet are NOT escaped by json.dumps since they are >=
        # 0x20. Left raw, one of these inside a content string would split
        # a single JSON object across multiple physical lines, shattering
        # the row for any reader that splits on line boundaries rather than
        # a bare "\n". Escape them explicitly so one JSON object == one line.
        line = (line.replace("\u2028", "\\u2028")
                    .replace("\u2029", "\\u2029")
                    .replace("\u0085", "\\u0085"))
        lines.append(line)
    text = "".join(line + "\n" for line in lines)

    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"[zmem] export-jsonl: wrote {len(rows)} row(s) to {out_path}")
    else:
        sys.stdout.write(text)
    return 0


# Hard ceiling on a synced row's content. Nothing the local writers produce
# comes close; a row past it is either corrupt or a deliberate attempt to
# bloat the store / the packs and prompts built from it, so it is rejected as
# malformed rather than clamped (silently truncating memory content would be
# worse than refusing it).
#
# NOTE: this is a deliberate PROTOCOL-ONLY constant (not env-overridable) —
# it is the contract the export-jsonl -> ingest-jsonl round-trip must satisfy,
# so changing it locally would let a store export rows its own ingest rejects
# (or vice-versa across versions). Keep hardcoded unless the wire format changes.
INGEST_MAX_CONTENT_CHARS = 65536

# Shape guard for an incoming id: UUID-length hex-and-dashes. This is a
# charset/length guard, not a UUID parser -- the point is that an id from a
# remote file can never be an arbitrary string that some later consumer
# interpolates somewhere it shouldn't.
_INGEST_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

# Reject a forged-future ingestion_ts beyond this much clock skew. Recency is
# a ranking input (compute_score), so an unbounded future timestamp is a
# permanent top-of-recall boost for whoever wrote the sync file.
INGEST_MAX_FUTURE_SKEW_SECONDS = 86400

# Hard ceiling on one PHYSICAL line read from a JSONL sync file, in characters
# (str.readline()'s size argument counts characters in text mode, not bytes --
# this is not a byte cap). A hostile single-line file with no newlines at all
# would otherwise buffer the whole file into one Python string before
# _validate_sync_row ever runs, since the old `for line in f` iteration reads
# a full physical line regardless of its length. A legitimate row's content
# is already capped at INGEST_MAX_CONTENT_CHARS (65536) by validation, so 1
# MiB of physical line -- content plus JSON escaping plus every other field --
# is generous headroom for any real row and still bounds worst-case memory
# use per line.
MAX_LINE_CHARS = 1_048_576


def _validate_sync_row(obj: dict, lineno: int | None = None) -> dict:
    """Validate + normalize ONE remote-authored JSONL sync row.

    Returns a dict whose every field is already the right Python type for a
    direct bind into the memory table; raises ValueError (-> the caller counts
    the line malformed and continues) for anything that isn't recoverable.

    This exists because ingest-jsonl writes straight into the table, bypassing
    add_memory() and every guard argparse applies to a local `add`: without it
    a sync file can set any column to any JSON type. The concrete damage that
    motivated it: a STRING confidence is stored by SQLite as TEXT, and TEXT
    sorts above every numeric in SQLite's type ordering -- so it passes the
    `confidence >= ?` floor, hijacks export-pack's `ORDER BY confidence DESC`,
    and then crashes recall's compute_score with a ValueError on float().

    `lineno` is the 1-based physical line this row came from in the sync
    file, used ONLY to attribute the unknown-signal warning below to a line
    a human can go find; it is optional (None) for direct/programmatic
    callers that have no line to report.

    Recoverable (normalized, not rejected):
      - unknown/absent/non-str signal -> "none" (a stderr line is emitted
        whenever the key is present but unusable -- a made-up string, or a
        non-str value like a number; an absent/None signal is the normal,
        unremarkable case and stays silent)
      - non-numeric, non-finite, or out-of-range confidence -> the
        signal-derived default, clamped to [0.0, 1.0]
      - a far-future ingestion_ts -> clamped to now
      - absent/None optional string fields -> ""
    Rejected as malformed:
      - id absent or not UUID-shaped; namespace/content absent, non-str, or
        empty; type not in ALLOWED_TYPES; content over the size cap; any
        optional field present with a non-str, non-null type.
    """
    def _req_str(key: str) -> str:
        v = obj.get(key)
        if not isinstance(v, str):
            raise ValueError(f"field '{key}' must be a string, got {type(v).__name__}")
        if not v.strip():
            raise ValueError(f"field '{key}' is empty")
        return v

    def _opt_str(key: str) -> str:
        v = obj.get(key)
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError(f"field '{key}' must be a string or null, "
                             f"got {type(v).__name__}")
        return v

    mid = obj.get("id")
    if not isinstance(mid, str) or not _INGEST_ID_RE.match(mid):
        raise ValueError("field 'id' must be a 36-char UUID-shaped string")

    namespace = _req_str("namespace")
    # Apply the same global-near-miss rejection as add_memory() so a remote
    # peer (or a hand-authored sync file) cannot strand rows under `global` /
    # `userglobal` / etc. on this store — those rows would be unreachable from
    # every automatic hook (issue #18 "Related observation"). Reject as
    # malformed (caller counts the line and continues) rather than crashing;
    # canonicalize `user:global` to itself (no-op). (Final-critic F2.)
    ns_trimmed = namespace.strip()
    if ns_trimmed != GLOBAL_NAMESPACE:
        ns_key = _global_near_miss_key(ns_trimmed)
        if ns_key in _GLOBAL_NEAR_MISS_STEMS:
            raise ValueError(
                f"field 'namespace' value {ns_trimmed!r} looks like a "
                f"misspelling of the global namespace; use {GLOBAL_NAMESPACE!r}"
            )
    namespace = ns_trimmed
    content = _req_str("content")
    if len(content) > INGEST_MAX_CONTENT_CHARS:
        raise ValueError(f"field 'content' is {len(content)} chars, over the "
                         f"{INGEST_MAX_CONTENT_CHARS} limit")

    type_ = obj.get("type")
    if not isinstance(type_, str) or type_ not in ALLOWED_TYPES:
        raise ValueError(f"field 'type' must be one of {', '.join(ALLOWED_TYPES)}")

    # Signal is normalized, not rejected: an unknown signal costs the row its
    # confidence default, which is a fine outcome; losing the row is not. A
    # present-but-unrecognized value still gets a stderr line -- silent
    # coercion would otherwise hide a remote writer sending a signal this
    # version of zmem doesn't know about. Absent (None) signal is the normal,
    # unremarkable case and stays silent.
    raw_signal = obj.get("signal")
    if isinstance(raw_signal, str) and raw_signal in ALLOWED_SIGNALS:
        signal = raw_signal
    else:
        signal = "none"
        if raw_signal is not None:
            loc = f"line {lineno}: " if lineno is not None else ""
            print(f"[zmem] ingest-jsonl: {loc}unknown signal '{raw_signal}' "
                  f"treated as 'none'", file=sys.stderr)

    fallback_conf = SIGNAL_CONFIDENCE.get(signal, 0.3)
    raw_conf = obj.get("confidence")
    if raw_conf is None:
        confidence = fallback_conf
    else:
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = fallback_conf
        # float() happily accepts "nan"/"inf": both would defeat the clamp
        # below (min/max propagate NaN) and poison the same ORDER BY.
        if not math.isfinite(confidence):
            confidence = fallback_conf
    confidence = max(0.0, min(1.0, confidence))

    tags = _opt_str("tags")
    source_ref = _opt_str("source_ref")
    supersede_reason = _opt_str("supersede_reason")
    valid_from = _opt_str("valid_from")
    ingestion_ts = _opt_str("ingestion_ts")
    superseded_at = _opt_str("superseded_at") or None
    merged_from = _opt_str("merged_from") or None

    if not ingestion_ts:
        ingestion_ts = now_iso()
    else:
        # A timestamp we cannot parse keeps today's graceful behavior (stored
        # verbatim; compute_score already treats an unparsable ingestion_ts as
        # unknown-age/neutral). Only a PARSED, implausibly-future one is
        # clamped -- that is the forgeable case.
        ts_epoch = _parse_iso_to_epoch(ingestion_ts)
        if ts_epoch > 0 and ts_epoch > time.time() + INGEST_MAX_FUTURE_SKEW_SECONDS:
            ingestion_ts = now_iso()

    return {
        "id": mid,
        "namespace": namespace,
        "type": type_,
        "content": content,
        "tags": tags,
        "source_ref": source_ref,
        "signal": signal,
        "confidence": confidence,
        "ingestion_ts": ingestion_ts,
        "valid_from": valid_from or ingestion_ts,
        "superseded_at": superseded_at,
        "supersede_reason": supersede_reason,
        "merged_from": merged_from,
    }


def _ingest_row(conn: sqlite3.Connection, obj: dict, *, allow_tombstones: bool) -> str:
    """Apply one VALIDATED JSONL sync row (a _validate_sync_row result) to the
    local store.

    Returns 'added', 'tombstoned', 'tombstone_refused', 'deduped', or
    'skipped' -- the caller tallies these into the ingest-jsonl summary line.
    Malformed-row handling lives in the caller, so a bad row never reaches
    this function; the caller also catches anything raised here (a row that
    blows up must not abort the rest of the file).

    `allow_tombstones` gates the ONLY destructive thing an import can do to an
    existing local row (see cmd_ingest_jsonl's flag docs).
    """
    mid = obj["id"]
    namespace = obj["namespace"]
    type_ = obj["type"]
    content = obj["content"]
    tags = obj["tags"]
    source_ref = obj["source_ref"]
    signal = obj["signal"]
    confidence = obj["confidence"]
    ingestion_ts = obj["ingestion_ts"]
    valid_from = obj["valid_from"]
    superseded_at = obj["superseded_at"]
    supersede_reason = obj["supersede_reason"]
    merged_from = obj.get("merged_from")

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        local = conn.execute("SELECT superseded_at FROM memory WHERE id=?", (mid,)).fetchone()

        if local is not None:
            # id already known locally: local content is NEVER overwritten by a
            # sync import. The only mutation an existing row can receive here is
            # a tombstone, and only if the incoming row is superseded and the
            # local one is not already -- everything else is a no-op skip.
            if superseded_at and local["superseded_at"] is None:
                if not allow_tombstones:
                    if started_tx and conn.in_transaction:
                        conn.rollback()
                    return "tombstone_refused"
                ok = supersede_memory(conn, mid, supersede_reason, at=superseded_at)
                if started_tx and conn.in_transaction:
                    _commit(conn)
                return "tombstoned" if ok else "skipped"
            if started_tx and conn.in_transaction:
                conn.rollback()
            return "skipped"

        if superseded_at:
            # New locally, but already tombstoned upstream: insert as history so
            # future syncs stay consistent, without letting it participate in
            # dedup-on-write or recall -- it must not resurface, and it must not
            # silently absorb a live row into its (dead) dedup slot either.
            for w in _check_secrets(content, source_ref, tags):
                print(f"[zmem] WARNING (advisory, write proceeded): {w}", file=sys.stderr)
            shash = ""
            conn.execute(
                """INSERT INTO memory
                   (id, namespace, type, content, tags, source_ref, source_hash,
                    confidence, signal, valid_from, superseded_at, supersede_reason,
                    ingestion_ts, retrieval_count, last_retrieved,
                    embedding, embedding_model, embedded_at, merged_from)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,'',NULL,?)""",
                (mid, namespace, type_, content, tags, source_ref, shash,
                 confidence, signal, valid_from, superseded_at, supersede_reason,
                 ingestion_ts, merged_from),
            )
            if started_tx:
                _commit(conn)
            return "added"

        warns = _check_secrets(content, source_ref, tags)
        for w in warns:
            print(f"[zmem] WARNING (advisory, write proceeded): {w}", file=sys.stderr)
        existing, _sim, emb = _detect_duplicate(conn, content, namespace)
        if existing:
            _merge_on_dedup(conn, existing["id"], confidence, signal, tags)
            if started_tx:
                _commit(conn)
            return "deduped"

        shash = ""
        emb_model = "minilm-onnx" if emb is not None else ""
        embedded_at = now_iso() if emb is not None else None
        conn.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, last_retrieved, embedding, embedding_model, embedded_at,
                merged_from)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,0,NULL,?,?,?,?)""",
            (mid, namespace, type_, content, tags, source_ref, shash,
             confidence, signal, valid_from, ingestion_ts, emb, emb_model, embedded_at,
             merged_from),
        )
        if emb is not None:
            try:
                conn.execute(
                    "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                    [emb, mid],
                )
            except sqlite3.OperationalError:
                pass  # vec0 table not available -- embedding stored in memory table only
        if started_tx:
            _commit(conn)
        return "added"
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise


def cmd_ingest_jsonl(conn: sqlite3.Connection, *, in_path: str,
                     source_ref: str | None, allow_tombstones: bool = False) -> int:
    """Import a JSONL sync file written by export-jsonl. Returns a process
    exit code: 2 if `in_path` cannot be read or contains no data lines at
    all, 0 otherwise (malformed/skipped/deduped rows do not fail the run --
    they are tallied and reported in the summary line).

    EVERY row is validated (_validate_sync_row) before it can touch the DB,
    and every row's application is individually guarded: one bad row is
    counted, reported with its line number, and the file keeps going. The
    summary line always prints, so "the run finished" and "every row landed"
    are never confused for each other.

    `allow_tombstones` controls whether an incoming row may kill a LIVE local
    row (see the --allow-tombstones flag help). Default off: a sync file is
    remote-authored data, and deleting local memory is the one irreversible
    thing it could ask for.

    Hostile input (nesting-bomb JSON, an oversized physical line, a bad
    encoding) is contained per-row/per-file: it never aborts the whole run,
    and the summary line is always printed regardless of how many rows were
    rejected. A row that raises mid-apply has any partial DB work rolled back
    via conn.rollback() before the file continues, so one row's failure
    between an INSERT and its own commit can never bleed into the next row's
    commit.
    """
    try:
        f = open(in_path, encoding="utf-8", newline="\n")
    except (OSError, UnicodeDecodeError) as e:
        print(f"[zmem] ingest-jsonl: cannot read {in_path}: {e}", file=sys.stderr)
        return 2

    added = tombstoned = tombstones_refused = deduped = skipped = malformed = 0
    first_refused_id = None
    saw_line = False
    decode_error = None
    try:
        # Stream the file line by line instead of reading it whole into
        # memory first -- a giant hostile file would otherwise exhaust RAM
        # before a single row is even validated. newline="\n" makes readline()
        # split on a bare "\n" ONLY -- NOT the universal-newline default and
        # NOT str.splitlines(), either of which also breaks on U+2028/U+2029/
        # U+0085 (and other Unicode line separators). This file's own writer
        # (export_jsonl) escapes those three inside string values, but a
        # third-party JSONL writer might not; splitlines() would then shatter
        # one JSON object across several bogus "lines". Splitting on a bare
        # "\n" treats every embedded separator as ordinary string content
        # instead. rstrip("\r\n") tolerates CRLF-terminated files (each
        # returned line keeps its "\n", and a CRLF line keeps the "\r" too
        # since "\r" is not a split point). readline(MAX_LINE_CHARS) below
        # additionally bounds how much of ONE physical line is ever buffered.
        lineno = 0
        while True:
            # readline(MAX_LINE_CHARS) bounds how much of ONE physical line we
            # ever hold at once -- unlike `for line in f`, which buffers a
            # full physical line no matter how long it is. An empty return
            # here (as opposed to just "\n") means EOF.
            raw_line = f.readline(MAX_LINE_CHARS)
            if raw_line == "":
                break
            lineno += 1

            if len(raw_line) >= MAX_LINE_CHARS and not raw_line.endswith("\n"):
                # readline stopping at the cap without a trailing "\n" is
                # AMBIGUOUS: it could mean this physical line truly exceeds
                # MAX_LINE_CHARS (more of it still follows), or it could mean
                # the line is exactly MAX_LINE_CHARS chars and simply ends at
                # EOF with no trailing newline -- a perfectly valid final
                # line. One bounded lookahead read resolves it: "" means
                # nothing followed raw_line at all, so the line was complete
                # and just happened to land exactly on the cap -- fall
                # through and process it like any other line instead of
                # rejecting a well-formed final line as malformed.
                lookahead = f.readline(MAX_LINE_CHARS)
                if lookahead != "":
                    # There really is more of this physical line -- it is
                    # genuinely oversized. Reject it as malformed without
                    # ever accumulating it, then drain the rest (still
                    # bounded, chunk by chunk, starting from the lookahead
                    # chunk already read) so the NEXT read starts on the
                    # following line and lineno stays one-per-physical-line.
                    # Same as every other malformed line below: it had real
                    # content, so it counts toward "this file had data" even
                    # though the row itself is rejected -- a file consisting
                    # of nothing but one oversized line must not be reported
                    # as empty (exit 2) instead of malformed=1 (exit 0).
                    saw_line = True
                    print(f"[zmem] ingest-jsonl: malformed line {lineno}: line "
                          f"exceeds {MAX_LINE_CHARS} chars", file=sys.stderr)
                    malformed += 1
                    chunk = lookahead
                    while True:
                        # Stop draining once this physical line actually
                        # ends: either a newline was found, or we hit EOF (a
                        # short, non-full-cap chunk, possibly "", ends the
                        # line either way since no more cap-sized chunks can
                        # follow it).
                        if chunk.endswith("\n") or len(chunk) < MAX_LINE_CHARS:
                            break
                        chunk = f.readline(MAX_LINE_CHARS)
                    continue
                # else: lookahead == "" -- EOF right after raw_line, so the
                # line was complete at exactly MAX_LINE_CHARS chars. Fall
                # through to process raw_line normally below.

            line = raw_line.rstrip("\r\n").strip()
            if not line:
                continue
            saw_line = True
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("line is not a JSON object")
                obj = _validate_sync_row(obj, lineno)
            except Exception as e:
                # Broad on purpose: a per-line parse guard must never let hostile
                # input abort the whole file. A pathologically deeply nested JSON
                # value (a "nesting bomb") raises RecursionError from json.loads,
                # which is not a subclass of ValueError/JSONDecodeError and would
                # otherwise escape this guard, unwind past the per-row try/except
                # below, and kill the run mid-file with no summary line. Malformed
                # counting/reporting stays identical for every exception type.
                print(f"[zmem] ingest-jsonl: malformed line {lineno}: "
                      f"{_sanitize_error_text(str(e))}", file=sys.stderr)
                malformed += 1
                continue

            if source_ref:
                # --source-ref attributes this whole import batch to one place of
                # origin, overriding whatever source_ref the row carried in --
                # the original almost always points at a path that does not
                # exist on this machine.
                obj["source_ref"] = source_ref

            try:
                outcome = _ingest_row(conn, obj, allow_tombstones=allow_tombstones)
            except Exception as e:
                # A row that raises mid-apply must not abort the file and silently
                # drop every row after it. Roll back first: _ingest_row commits at
                # each of its return paths, so an exception between an INSERT and
                # its commit would otherwise leave a partial write open for the
                # NEXT row's commit to land.
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"[zmem] ingest-jsonl: malformed line {lineno}: could not apply row: "
                      f"{type(e).__name__}: {_sanitize_error_text(str(e))}", file=sys.stderr)
                malformed += 1
                continue

            if outcome == "added":
                added += 1
            elif outcome == "tombstoned":
                tombstoned += 1
            elif outcome == "tombstone_refused":
                tombstones_refused += 1
                if first_refused_id is None:
                    first_refused_id = obj["id"]
            elif outcome == "deduped":
                deduped += 1
            else:
                skipped += 1
    except (UnicodeDecodeError, OSError) as e:
        # Invalid UTF-8 can now surface mid-iteration (streaming, not a single
        # up-front read_text()) -- that's the UnicodeDecodeError case. OSError
        # covers a read failing mid-file for reasons that have nothing to do
        # with content: disk error, or the file getting truncated/replaced out
        # from under us while we're still reading it. Either way, record it
        # and fall through to the summary -- rows already applied before the
        # failure still count -- then exit 2, same as an unreadable file.
        decode_error = e
    finally:
        f.close()

    if decode_error is not None:
        print(f"[zmem] ingest-jsonl: cannot read {in_path}: {decode_error}", file=sys.stderr)
    elif not saw_line:
        print(f"[zmem] ingest-jsonl: {in_path} is empty -- nothing to ingest", file=sys.stderr)
        return 2

    if tombstones_refused:
        # ONE note, not one per row: a hostile or corrupt file could otherwise
        # bury every other warning under thousands of lines.
        print(f"[zmem] ingest-jsonl: refused {tombstones_refused} tombstone(s) against "
              f"LIVE local row(s) (first id: {first_refused_id}); those rows are "
              f"untouched. Re-run with --allow-tombstones ONLY if this file is your "
              f"own store's export, not a remote/cloud outbox.", file=sys.stderr)

    print(f"[zmem] ingest-jsonl: added={added} tombstoned={tombstoned} "
          f"tombstones_refused={tombstones_refused} deduped={deduped} "
          f"skipped={skipped} malformed={malformed}")
    return 2 if decode_error is not None else 0


def nonnegative_int(value: str) -> int:
    """argparse type= for flags fed straight into a SQL LIMIT: SQLite treats a
    negative LIMIT as UNBOUNDED, so a negative --project-limit/--global-limit
    would silently defeat the cap instead of erroring. Reject it up front."""
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer, got {value!r}")
    return n


def main():
    ap = argparse.ArgumentParser(prog="store.py", description="ZMem semantic store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store if absent (idempotent)")

    p_add = sub.add_parser("add", help="add a memory")
    p_add.add_argument("--namespace", required=True)
    p_add.add_argument("--type", required=True, choices=list(ALLOWED_TYPES))
    p_add.add_argument("--content", required=True)
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--source-ref", default="")
    p_add.add_argument("--confidence", type=float, default=None)
    p_add.add_argument("--signal", default="none", choices=list(ALLOWED_SIGNALS))
    p_add.add_argument("--capture-mode", default=None, choices=list(CAPTURE_MODES),
                       help="manual/reviewed keep the original text with warnings; "
                            "auto redacts likely secrets by default before writing")

    p_recall = sub.add_parser("recall", help="recall relevant memories")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--namespace", default=None)
    p_recall.add_argument("--limit", type=int, default=5)
    p_recall.add_argument("--json", action="store_true")
    p_recall.add_argument("--hybrid", action="store_true",
                          help="use hybrid BM25+vector recall (requires onnxruntime)")
    p_recall.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write "
                               "(READ-ONLY recall; used by hook-driven recall so "
                               "subagent fan-out does not create N concurrent writers)")
    p_recall.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge; "
                               "a global row never crowds out a project row). The "
                               "automatic hooks pass this so cross-project lessons "
                               "reach project-scoped sessions (issue #18).")
    p_recall.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")

    p_recent = sub.add_parser("recent", help="most recent live memories (no FTS, admin pull)")
    p_recent.add_argument("--namespace", default=None)
    p_recent.add_argument("--limit", type=int, default=5)
    p_recent.add_argument("--min-confidence", type=float, default=0.5)
    p_recent.add_argument("--json", action="store_true")
    p_recent.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write "
                               "(READ-ONLY; used by hook-driven subagent recall)")
    p_recent.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge). "
                               "The automatic hooks pass this so cross-project "
                               "lessons reach project-scoped sessions (issue #18).")
    p_recent.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")

    p_search = sub.add_parser("search", help="keyword search (no confidence floor)")
    p_search.add_argument("--text", required=True)
    p_search.add_argument("--namespace", default=None)
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge). "
                               "Use this instead of going unscoped when you want the "
                               "global tier unioned in but still want a per-tier "
                               "budget (issue #18).")
    p_search.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")
    p_search.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write "
                               "(READ-ONLY search). Search defaults to bumping like "
                               "recall; pass this for a read-only query.")

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
                               default=CONSOLIDATE_DEFAULT_THRESHOLD)
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
    p_promote.add_argument("--description", default=None,
                           help="override the synthesized trigger description verbatim "
                                "(used with --id --confirm)")
    p_promote.add_argument("--confirm", action="store_true",
                           help="REQUIRED to write. Promotion creates a SKILL.md in every dir "
                                "in the review-candidate area; add --install-approved for the "
                                "explicit live install into ZMEM_SKILLS_DIRS (default: both "
                                "~/.claude/skills and ~/.zcode/skills). --id alone refuses "
                                "with exit 2. --dry-run needs no confirmation (and lists ALL "
                                "candidates — it ignores --id).")
    p_promote.add_argument("--install-approved", action="store_true",
                           help="explicitly install the generated SKILL.md into the live "
                                "skills dirs after writing the review candidate")

    p_rekey = sub.add_parser(
        "rekey-namespace",
        help="admin: rewrite the namespace of live rows (remediate stranded "
             "global-near-miss rows so they surface again)")
    p_rekey.add_argument("--from", dest="from_namespace", default=None,
                         help="source namespace to rekey from (required unless "
                              "--near-miss-global is set). Case-sensitive exact match.")
    p_rekey.add_argument("--to", dest="to_namespace", default=GLOBAL_NAMESPACE,
                         help=f"destination namespace (default {GLOBAL_NAMESPACE}). "
                              "Must not itself be a global near-miss.")
    p_rekey.add_argument("--near-miss-global", action="store_true",
                         help="rekey EVERY live row whose namespace is a global "
                              "near-miss (global, userglobal, users:global, ...) to "
                              "--to. Ignores --from. This is the remediation for "
                              "legacy rows stranded before the write-time guard.")
    p_rekey.add_argument("--dry-run", action="store_true",
                         help="report the candidate count and namespaces without writing")
    p_rekey.add_argument("--confirm", action="store_true",
                         help="REQUIRED to actually write. rekey-namespace without "
                              "--confirm (and without --dry-run) refuses with exit 2.")

    p_backup = sub.add_parser(
        "backup", help="take a verified, retention-rotated snapshot of the store")
    p_backup.add_argument("--retention", type=int, default=BACKUP_DEFAULT_RETENTION,
                          help=f"keep the newest N '{SNAPSHOT_GLOB}' snapshots and delete "
                               f"only the oldest beyond that (default "
                               f"{BACKUP_DEFAULT_RETENTION}; 0 disables pruning). "
                               f"Nothing else in the backup dir is ever touched.")
    p_backup.add_argument("--out-dir", default=None,
                          help="backup directory override (default: $ZMEM_BACKUP_DIR, "
                               "else <store dir>/backups)")
    p_backup.add_argument("--if-due", action="store_true",
                          help="no-op unless $ZMEM_BACKUP_INTERVAL_DAYS (default 1) has "
                               "elapsed since the last successful backup; used by the "
                               "SessionStart hook so the automatic trigger is cheap. "
                               "Without this flag the backup always runs.")

    p_restore = sub.add_parser(
        "restore", help="restore the store from a snapshot (verifies first, "
                        "backs up the current store first)")
    p_restore.add_argument("--from", dest="from_path", required=True,
                           help="path to the snapshot .sqlite to restore from")
    p_restore.add_argument("--force", action="store_true",
                           help="required to overwrite an existing destination store")
    p_restore.add_argument("--out-dir", default=None,
                           help="where to put the pre-restore backup (default: same as "
                                "`backup`)")

    p_export_pack = sub.add_parser(
        "export-pack",
        help="render a Tier 1 markdown memory pack for a namespace (project + user:global)")
    p_export_pack.add_argument("--namespace", required=True,
                               help="project namespace to pack (e.g. project:foo)")
    p_export_pack.add_argument("--out", default=None,
                               help="write the pack to this file (UTF-8, LF); default: stdout")
    p_export_pack.add_argument("--project-limit", type=nonnegative_int,
                               default=EXPORT_PACK_DEFAULT_PROJECT_LIMIT,
                               help=f"max rows from --namespace (default {EXPORT_PACK_DEFAULT_PROJECT_LIMIT})")
    p_export_pack.add_argument("--global-limit", type=nonnegative_int,
                               default=EXPORT_PACK_DEFAULT_GLOBAL_LIMIT,
                               help=f"max rows from {GLOBAL_NAMESPACE} (default {EXPORT_PACK_DEFAULT_GLOBAL_LIMIT})")
    p_export_pack.add_argument("--min-confidence", type=float,
                               default=EXPORT_PACK_DEFAULT_MIN_CONFIDENCE,
                               help=f"confidence floor for both sections (default {EXPORT_PACK_DEFAULT_MIN_CONFIDENCE})")
    p_export_pack.add_argument("--max-bytes", type=int,
                               default=EXPORT_PACK_DEFAULT_MAX_BYTES,
                               help="budget (UTF-8 bytes) for the bullet lines; a bullet that "
                                    "would exceed it is omitted whole, never truncated, and "
                                    "later smaller bullets are still emitted. Structural text "
                                    "(header, titles, section headings, '(none)', the "
                                    "omitted-count note) is exempt, so the file itself can "
                                    "exceed this by that framing "
                                    f"(default {EXPORT_PACK_DEFAULT_MAX_BYTES})")

    p_export_jsonl = sub.add_parser(
        "export-jsonl",
        help="export Tier 3 sync JSONL (one memory row per line, no embeddings)")
    p_export_jsonl.add_argument("--out", default=None,
                                help="write to this file (UTF-8, LF); default: stdout")
    p_export_jsonl.add_argument("--namespace", default=None,
                                help="limit to a specific namespace (default: all namespaces)")
    p_export_jsonl.add_argument("--include-superseded", action="store_true",
                                help="also export tombstoned rows (default: live rows only)")

    p_ingest_jsonl = sub.add_parser(
        "ingest-jsonl",
        help="import Tier 3 sync JSONL written by export-jsonl")
    p_ingest_jsonl.add_argument("--in", dest="in_path", required=True,
                                help="JSONL file to ingest")
    p_ingest_jsonl.add_argument("--source-ref", default=None,
                                help="override source_ref on every row inserted this run "
                                     "(default: keep each row's own incoming source_ref)")
    p_ingest_jsonl.add_argument("--allow-tombstones", action="store_true",
                                help="let an incoming superseded row TOMBSTONE a live local "
                                     "row with the same id. Off by default: use it only when "
                                     "the file is an export of a store you trust as "
                                     "authoritative for those ids (e.g. rebuilding a local "
                                     "store from your own export). Ingesting a cloud/remote "
                                     "outbox must NOT use it -- without the flag such rows "
                                     "are counted as tombstones_refused and the local rows "
                                     "are left alone. A brand-new id that arrives already "
                                     "tombstoned is still inserted as history either way.")

    p_fail = sub.add_parser(
        "failures",
        help="detect failed tool calls for a session (transcript JSONL or db.sqlite)")
    p_fail.add_argument("--session", default="",
                        help="session id (used with the db.sqlite substrate)")
    p_fail.add_argument("--transcript", default="",
                        help="Claude Code transcript JSONL path (wins when present)")
    p_fail.add_argument("--db", default=os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
                        help="ZCode episodic db.sqlite path (default ~/.zcode/cli/db/db.sqlite)")

    args = ap.parse_args()

    # `failures` is store-independent (it reads a transcript JSONL or the ZCode
    # episodic db, never the ZMem store) and must be fail-open: branch BEFORE
    # connect()/assert_local_fs()/migrate() so a bad ZMEM_DATA location, a
    # locked store, or a mid-migration state can never break failure detection.
    if args.cmd == "failures":
        cmd_failures(session=args.session, transcript=args.transcript, db=args.db)
        return

    # `restore` overwrites the destination store FILE. It must not hold an open
    # sqlite3 connection on that file while doing so (a Windows file handle can
    # block the overwrite), so — following the `failures` precedent above — it
    # is dispatched BEFORE connect()/init_db()/migrate() and does its own
    # minimal, self-contained, open-close-per-step file work.
    if args.cmd == "restore":
        sys.exit(cmd_restore(from_path=args.from_path, force=args.force,
                             out_dir=args.out_dir))

    try:
        _wait_for_maintenance_clear(args.cmd)
        conn = connect()
        _prepare_store(conn)
    except RuntimeError as e:
        print(f"[zmem] {e}", file=sys.stderr)
        sys.exit(2)

    writer_lease = None
    if (
        args.cmd in {"add", "supersede", "rebuild-fts", "reembed", "ingest-jsonl"}
        or (args.cmd == "recall" and not args.no_bump)
        or (args.cmd == "recent" and not args.no_bump)
        or (args.cmd == "search" and not args.no_bump)
        or (args.cmd == "rekey-namespace" and not args.dry_run and args.confirm)
    ):
        writer_lease = _acquire_writer_lease(args.cmd)

    try:
        if args.cmd == "init":
            print(f"[zmem] store ready at {STORE_PATH}")
        elif args.cmd == "add":
            try:
                add_memory(
                    conn,
                    namespace=args.namespace,
                    type_=args.type,
                    content=args.content,
                    tags=args.tags,
                    source_ref=args.source_ref,
                    confidence=args.confidence,
                    signal=args.signal,
                    capture_mode=args.capture_mode,
                )
            except CapturePolicyRefusal as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(2)
        elif args.cmd == "recall":
            recall_memory(conn, query=args.query, namespace=args.namespace,
                          limit=args.limit, as_json=args.json, hybrid=args.hybrid,
                          no_bump=args.no_bump, include_global=args.include_global,
                          global_limit=args.global_limit)
        elif args.cmd == "recent":
            recent_memory(conn, namespace=args.namespace, limit=args.limit,
                          min_confidence=args.min_confidence, as_json=args.json,
                          no_bump=args.no_bump, include_global=args.include_global,
                          global_limit=args.global_limit)
        elif args.cmd == "search":
            recall_memory(conn, query=args.text, namespace=args.namespace, limit=args.limit,
                          as_json=False, min_confidence=0.0,
                          include_global=args.include_global,
                          global_limit=args.global_limit, no_bump=args.no_bump)
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
            # Single-flight: consolidate() writes, and the SessionStart hook fires a
            # detached one per session. Its meta-key cadence gate is a SOFT gate
            # (read-then-later-write), so without this lock two near-simultaneous
            # runs both pass it and both run the clustering loop. --dry-run writes
            # nothing, so it is deliberately never gated (and never takes the lock).
            c_token = None
            if not args.dry_run:
                c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
                if c_token is None:
                    print("[zmem] consolidate: another consolidation is already "
                          "running - skipped")
                    conn.close()
                    return
            try:
                consolidate(conn, threshold=args.threshold, prune=args.prune,
                            dry_run=args.dry_run, namespace=args.namespace)
            finally:
                _release_lock("consolidate", c_token)
        elif args.cmd == "backup":
            rc = cmd_backup(conn, retention=args.retention, out_dir=args.out_dir,
                            if_due=args.if_due)
            sys.exit(rc)
        elif args.cmd == "rekey-namespace":
            # --confirm (or --dry-run) is a REAL gate: this rewrites the
            # namespace column of live rows. Without either flag it refuses.
            if not args.confirm and not args.dry_run:
                print("[zmem] rekey-namespace: refusing to write without "
                      "--confirm (or --dry-run to preview).", file=sys.stderr)
                sys.exit(2)
            try:
                rekey_namespace(
                    conn, from_namespace=args.from_namespace,
                    to_namespace=args.to_namespace,
                    near_miss_global=args.near_miss_global, dry_run=args.dry_run,
                )
            except ValueError as exc:
                print(f"[zmem] rekey-namespace: {exc}", file=sys.stderr)
                sys.exit(2)
        elif args.cmd == "promote":
            # --confirm is a REAL gate, not decoration. Promotion writes a SKILL.md
            # into the review-candidate area, and live installation now needs the
            # extra explicit --install-approved gate.
            if args.id and not args.dry_run and not args.confirm:
                print("[zmem] refusing to promote without --confirm.", file=sys.stderr)
                print(f"[zmem]   promote --id {args.id} --confirm", file=sys.stderr)
                print("[zmem] (add --description \"...\" to write the trigger line yourself — "
                      "the description is the entire trigger surface)", file=sys.stderr)
            # sys.exit, not return: main()'s return value is discarded by the
            # `if __name__ == "__main__": main()` entrypoint, so a bare `return 2`
            # prints the refusal but still exits 0 — a refusal indistinguishable
            # from success to any caller checking $?. 2 matches cmd_restore's
            # "refused, destination untouched" codes (see its refusal branches);
            # note restore uses 1 for its own missing-flag case, and `failures`
            # surfaces no code at all, so this is deliberately the refused-and-
            # nothing-written convention rather than a blanket house style.
                sys.exit(2)
            rc = promote_memory(conn, memory_id=args.id, dry_run=args.dry_run,
                                namespace=args.namespace, description=args.description,
                                install_approved=args.install_approved)
            if rc:
                sys.exit(rc)
        elif args.cmd == "export-pack":
            rc = cmd_export_pack(
                conn, namespace=args.namespace, out=args.out,
                project_limit=args.project_limit, global_limit=args.global_limit,
                min_confidence=args.min_confidence, max_bytes=args.max_bytes,
            )
            sys.exit(rc)
        elif args.cmd == "export-jsonl":
            rc = cmd_export_jsonl(
                conn, out=args.out, namespace=args.namespace,
                include_superseded=args.include_superseded,
            )
            sys.exit(rc)
        elif args.cmd == "ingest-jsonl":
            rc = cmd_ingest_jsonl(conn, in_path=args.in_path, source_ref=args.source_ref,
                                  allow_tombstones=args.allow_tombstones)
            sys.exit(rc)
    finally:
        _release_writer_lease(writer_lease)
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
