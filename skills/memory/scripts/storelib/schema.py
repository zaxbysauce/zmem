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

# Shared single source of truth (dependency-free, resolved from this dir).
try:
    from schema_meta import (SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION_KEY,
        MAX_CONTENT_CHARS, ALLOWED_TYPES, ALLOWED_SIGNALS, ALLOWED_TAINTS,
        TAINT_RANK, TAINT_TRUSTED_SIGNALS, validate_taint, worse_taint,
        normalize_content,
        ZMEM_VEC_NS_OVERFETCH_DEFAULT, ZMEM_VEC_NS_OVERFETCH_ENV)
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from schema_meta import (SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION_KEY,
        MAX_CONTENT_CHARS, ALLOWED_TYPES, ALLOWED_SIGNALS, ALLOWED_TAINTS,
        TAINT_RANK, TAINT_TRUSTED_SIGNALS, validate_taint, worse_taint,
        normalize_content,
        ZMEM_VEC_NS_OVERFETCH_DEFAULT, ZMEM_VEC_NS_OVERFETCH_ENV)  # type: ignore

try:
    import host as _host
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import host as _host
    except ImportError:
        _host = None

try:
    import embeddings as _embeddings
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import embeddings as _embeddings
    except ImportError:
        _embeddings = None

try:
    import embed_profiles as _profiles
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import embed_profiles as _profiles
    except ImportError:
        _profiles = None

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
    # Below CONFIDENCE_FLOOR (0.25) on purpose: `none`-signal memories are
    # ungrounded self-opinion, and the README's trust-tiering design says they
    # sit below the default retrieval floor (the cited research finds
    # ungrounded lessons degrade accuracy). At 0.2 they are excluded from
    # default recall but still retrievable via keyword search (`search --text`
    # applies no confidence floor) or `recent --min-confidence 0`. Previously
    # 0.3 > 0.25 floor, which contradicted the docs and let ungrounded
    # content surface by default (#36 M3).
    "none": 0.2,
}

# ALLOWED_TYPES / ALLOWED_SIGNALS / MAX_CONTENT_CHARS are imported from
# schema_meta above (single source of truth shared with doctor.py and the
# Hermes provider surfaces). The two closed enums are the sets the store's own
# writers already enforce (`add`'s argparse choices); naming them once here lets
# the Tier 3 ingest validator enforce the SAME sets on remote-authored rows --
# a sync file must not be able to widen them by writing straight into the table.


CONFIDENCE_FLOOR = 0.25

# SECRET_PATTERNS is imported from correction_queue (single source of truth for
# the store's capture policy AND the live-capture queue's redaction).


PROMPT_INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore (all|any|the|previous|prior) instructions\b"),
    re.compile(r"(?i)\b(system prompt|developer message|tool call|function call)\b"),
    re.compile(r"(?i)</?(system|assistant|developer|tool)>"),
    re.compile(r"```"),
]


CAPTURE_MODES = ("auto", "reviewed", "manual")



def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _format_recency(ts: str | None) -> str:
    """Humanize an ISO-8601 UTC timestamp from now_iso() as 'Nd/Nh/Nm ago'.

    Used by stats() so backup/consolidation cadence health is one glance away
    (#39 E1). Strict parse: a value that does not match now_iso()'s exact
    format ('%Y-%m-%dT%H:%M:%SZ') is returned verbatim, so writer-format drift
    stays visible rather than being silently mis-aged. None/empty -> '(never)'.
    A future-dated value (clock skew) is also returned verbatim.
    """
    if not ts:
        return "(never)"
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ts
    secs = int((datetime.now(timezone.utc) - then).total_seconds())
    if secs < 0:
        return ts  # future-dated clock skew — don't fabricate a negative age
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"

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
        # Dimension follows the ACTIVE embedding profile (issue #63, 8.2) so a
        # fresh store created under ZMEM_EMBED_PROFILE=fake is born 16-dim; an
        # EXISTING table is untouched (IF NOT EXISTS), and existing stores keep
        # their committed dim regardless of env — a mismatched profile is
        # refused at dispatch by assert_embedding_compatible, never silently
        # coerced here.
        conn.execute(_vec0_create_sql(_active_vec0_dim()))
    except Exception:
        pass
    return conn

def _active_vec0_dim() -> int:
    """Vector dimension used when creating a FRESH memory_vec table: the active
    profile's dim. An invalid/unreadable registry or env value falls back to
    the historical 384 fail-safe — connect() must never raise on env content;
    a genuinely bad profile is refused later at dispatch."""
    if _profiles is None:
        return 384
    try:
        return _profiles.active_dim()
    except Exception:
        return 384


def _vec0_create_sql(dim: int) -> str:
    """Single source of the memory_vec DDL shape (both creation sites use this:
    connect-time ensure + v3 migration). Keep distance_metric/second column in
    lockstep with every KNN/dedup query against the table."""
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
        f"embedding float[{dim}] distance_metric=cosine, memory_id TEXT"
        ")"
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Read one meta key (call-time read, NEVER cached). Returns None when the
    row is absent."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one meta key. Old clients only ever address meta rows by exact
    key (WHERE key=?), so extra keys written by newer clients are inert for
    them — that property is why profile bookkeeping lives here instead of a
    schema bump (issue #63 compat ledger)."""
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
    )


def _live_embedding_dim(conn: sqlite3.Connection) -> int | None:
    """The vector dimension currently COMMITTED to the store.

    Primary oracle: byte-length of a real `memory.embedding` blob (packed
    float32 => len // 4 floats). Fallback for an empty-but-initialized store:
    parse the declared float[N] out of the memory_vec DDL in sqlite_master.
    Returns None when the store has no committed dimension yet (no embedded
    rows AND no declared table) — any profile may then claim it freely.

    A regex miss on the fallback (future sqlite-vec DDL syntax drift) returns
    None as well ONLY when there are no blobs; if blobs exist they decided
    already. This function never raises and never guesses.
    """
    try:
        row = conn.execute(
            "SELECT length(embedding)/4 FROM memory "
            "WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return int(row[0])
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_vec'"
        ).fetchone()
        if row and row[0]:
            m = re.search(r"float\[(\d+)\]", row[0], re.IGNORECASE)
            if m:
                return int(m.group(1))
            raise RuntimeError(_VEC_DIM_UNKNOWN_MSG)
    except RuntimeError:
        raise
    except sqlite3.Error:
        pass
    return None


_VEC_DIM_UNKNOWN_MSG = (
    "[zmem] cannot determine the store's embedding dimension: memory_vec DDL "
    "is not parseable and no embeddings exist yet. Refusing rather than "
    "guessing. Re-run with ZMEM_EMBED_PROFILE unset after backing up."
)


def assert_embedding_compatible(conn: sqlite3.Connection, *, allow_rebuild: bool = False) -> None:
    """Fail-closed dim gate (issue #63, 8.2): refuse any command whose success
    depends on generating or querying vectors when the ACTIVE profile's dim
    differs from the dimension already committed to the store.

    Raises RuntimeError (cli turns into exit 2 with the remediation command)
    instead of letting a wrong-dim INSERT blow up mid-write or a wrong-dim
    query crash recall. `allow_rebuild=True` exempts reembed --all / --dry-run,
    which are precisely the tools that RESOLVE a mismatch.
    """
    prof_name = "minilm"
    if _profiles is not None:
        try:
            prof_name = _profiles.resolve_active_profile()
        except Exception:
            prof_name = ""  # unreachable via CLI; guard mirrors registry
        if not prof_name:
            raise RuntimeError(
                "unknown ZMEM_EMBED_PROFILE value "
                f"{(_profiles.get_env_raw() or '')!r} — valid profiles: "
                f"{', '.join(sorted(_profiles.PROFILES))}"
            )
        prof_dim = _profiles.PROFILES[prof_name]["dim"]
    else:
        prof_dim = 384
    live_dim = _live_embedding_dim(conn)
    if live_dim is None or live_dim == prof_dim:
        return
    if allow_rebuild:
        return
    fix = f"store.py reembed --all --profile {prof_name}"
    raise RuntimeError(
        f"profile '{prof_name}' expects {prof_dim}-dim embeddings but the store "
        f"holds {live_dim}-dim data — refusing to mix dimensions. Run "
        f"`{fix}` to convert the store (this rebuilds every embedding), or "
        f"unset ZMEM_EMBED_PROFILE to stay on the stored dimension."
    )


SCHEMA_LOCK_STALE_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_STALE_SECONDS", 300.0)

SCHEMA_LOCK_WAIT_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_WAIT_SECONDS", 15.0)

SCHEMA_LOCK_POLL_SECONDS = _env_float("ZMEM_SCHEMA_LOCK_POLL_SECONDS", 0.05)

MAINTENANCE_LOCK_STALE_SECONDS = _env_float("ZMEM_MAINTENANCE_LOCK_STALE_SECONDS", 1800.0)

MAINTENANCE_WAIT_SECONDS = _env_float("ZMEM_MAINTENANCE_WAIT_SECONDS", 5.0)

MAINTENANCE_POLL_SECONDS = _env_float("ZMEM_MAINTENANCE_POLL_SECONDS", 0.05)

WRITER_LEASE_STALE_SECONDS = _env_float("ZMEM_WRITER_LEASE_STALE_SECONDS", 300.0)


def _vec_knn_in_namespace(
    conn: sqlite3.Connection,
    embedding: bytes,
    *,
    namespaces: list[str] | None,
    k: int,
    overfetch: int | None = None,
    k_cap: int = 500,
) -> list[tuple[str, float]]:
    """Shared namespace-aware vec0 KNN for recall + dedup (issue #58, 3.1).

    Over-fetches ``max(k * overfetch, k + 1)`` from vec0, joins to ``memory``
    to filter by ``namespace IN (...)`` (skipped when ``namespaces is None``)
    and ``superseded_at IS NULL`` (always applied), and truncates to k. Each
    returned tuple is ``(memory_id, distance)``, ordered by vec0 ascending
    distance (closest first).

    ``namespaces=None`` is the unscoped path (no namespace filter, but
    ``superseded_at IS NULL`` is still applied). The caller decides whether
    ``None`` is meaningful — recall passes the per-tier expanded alias set;
    dedup passes a single namespace.

    ``overfetch`` defaults to the module-level constant
    ``ZMEM_VEC_NS_OVERFETCH_DEFAULT`` (env override ``ZMEM_VEC_NS_OVERFETCH``,
    parsed here at call time so a reload with a different env value takes
    effect). The cap ``k_cap`` bounds the vec0 KNN argument so a runaway
    store cannot OOM the helper.

    Consolidate owns its own escalate-then-verify loop with a 500-row cap
    and a below-threshold cutoff (it tracks a ``truncated`` flag); this
    helper is the simpler shared shape used by recall and dedup and does
    NOT return a truncation flag (issue #58 critic-fix C-4: callers that
    need truncation semantics go through consolidate).

    Lives in ``storelib.schema`` so both recall (which imports from
    schema) and write (which also imports from schema) can share the
    helper without an import cycle.
    """
    if overfetch is None:
        overfetch = ZMEM_VEC_NS_OVERFETCH_DEFAULT
        raw_env = os.environ.get(ZMEM_VEC_NS_OVERFETCH_ENV, "")
        if raw_env:
            try:
                candidate = float(raw_env)
                # PRR-005 fix: reject non-finite overrides — float() accepts
                # "nan"/"inf" but int(overfetch) below raises ValueError/
                # OverflowError outside the SQL guard, crashing recall.
                if candidate == candidate and candidate not in (
                    float("inf"), float("-inf")
                ):
                    overfetch = candidate
            except ValueError:
                pass
    # Belt-and-suspenders: never let a non-finite or non-positive factor
    # reach the int math regardless of how it arrived.
    if overfetch != overfetch or overfetch in (float("inf"), float("-inf")) or overfetch < 1:
        overfetch = float(ZMEM_VEC_NS_OVERFETCH_DEFAULT)
    raw_k = max(int(k) * int(overfetch), int(k) + 1)
    raw_k = min(raw_k, int(k_cap))

    ns_clause = ""
    ns_params: list = []
    if namespaces:
        ns_clause = (
            " AND m.namespace IN ("
            + ",".join("?" * len(namespaces))
            + ") "
        )
        ns_params = list(namespaces)

    try:
        # vec0 returns rows already ordered by distance ascending when
        # ``MATCH ... AND k = ?`` is used; an explicit ``ORDER BY
        # distance`` after a JOIN against the vec0 virtual table raises
        # ``OperationalError: near "BY": syntax error`` (vec0 syntax
        # restriction). The caller iterates by the returned order.
        rows = conn.execute(
            "SELECT mv.memory_id AS memory_id, mv.distance AS distance "
            "FROM memory_vec mv "
            "JOIN memory m ON m.id = mv.memory_id "
            "WHERE mv.embedding MATCH ? AND k = ? "
            "  AND m.superseded_at IS NULL"
            + ns_clause,
            [embedding, raw_k, *ns_params],
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["memory_id"], r["distance"]) for r in rows[:k]]


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
            -- v7 (issue #21): passive/hook-driven surface telemetry. Hook recall passes
            -- `--no-bump`, which must NOT advance retrieval_count (write contention under
            -- subagent fan-out, PLAN.md §5) but SHOULD record that the memory was surfaced
            -- into context. Per recall event exactly ONE of retrieval_count / surfaced_count
            -- advances, so retrieval_count + surfaced_count is a non-double-counted "times
            -- surfaced" metric. Explicit skill-invoked recall bumps retrieval_count; passive
            -- (--no-bump) recall bumps surfaced_count. Consumers that rank by usefulness
            -- (promote, consolidate keeper/prune, recall popularity, export-pack) blend the two.
            surfaced_count INTEGER NOT NULL DEFAULT 0,
            last_surfaced  TEXT,
            -- v6: consolidation provenance. Comma-joined ids of rows absorbed
            -- into this keeper by consolidate() (appends across runs). A
            -- `:truncated` marker after an id means that row's content could
            -- not be appended (the keeper was already at the content-size cap)
            -- the id is still recorded for traceability. Write-only provenance
            -- today, queryable by users/future tooling. See _absorb_into_keeper.
            merged_from     TEXT,
            -- v8 (issue #39 E4): canonical normalized content for O(log N)
            -- exact-match dedup. Populated in application code at every write
            -- path by _normalize_content() — NOT a SQL GENERATED column
            -- (SQLite can't replicate Python's Unicode-aware .lower() + \s+
            -- collapse, so a generated column would silently diverge). Indexed
            -- with a partial index on live rows to match the dedup-query predicate.
            content_norm    TEXT,
            -- v9 (issue #59): append-only knowledge-update lineage + provenance
            -- trust. valid_until: the exclusive end of validity (empty = never
            -- expires). Non-empty valid_until implies the row is tombstoned
            -- (superseded_at NOT NULL): the two are written together and the
            -- as-of predicate treats a set valid_until as the row's real end.
            -- update_of: the id of the row this LIVE row replaces (an `update`
            -- re-creates a fact: old row tombstones, new row carries update_of
            -- pointing back at it). taint: provenance/trust rank of the row's
            -- origin (trusted_internal < untrusted_tool < untrusted_web). The
            -- CHECK is the schema-level enforcement of the closed three-rank
            -- enum; application code validates too (issue #59, 4.7).
            valid_until     TEXT NOT NULL DEFAULT '',
            update_of       TEXT NOT NULL DEFAULT '',
            taint           TEXT NOT NULL DEFAULT 'trusted_internal'
                CHECK (taint IN ('trusted_internal','untrusted_tool','untrusted_web')),
            -- v11 (issue #61, 6.1): associative-memory trust score. Starts at
            -- 1.0; a `contradicts` link event adjusts BOTH rows by -0.10 and a
            -- `supports`/corroborating add by +0.05, clamped to [0.0, 1.0]
            -- (storelib/links.py::adjust_trust). Deliberately independent of
            -- `confidence`/`signal` (provenance inputs): trust_score is the
            -- contradiction ledger, and linking never rewrites those columns.
            trust_score     REAL NOT NULL DEFAULT 1.0
        );
        CREATE INDEX IF NOT EXISTS idx_memory_namespace ON memory(namespace);
        CREATE INDEX IF NOT EXISTS idx_memory_live ON memory(superseded_at) WHERE superseded_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type);
        -- NOTE: idx_memory_content_norm is created separately below (and in the
        -- v8 migrate block) because it references content_norm, which legacy
        -- stores lack until the v8 migration ALTERs the table. Creating it here
        -- inside executescript would fail on a pre-v8 store where the column
        -- does not yet exist.

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

        -- v10 (issue #60, 5.1): entity identity. entity_alias.alias_norm is
        -- GLOBALLY unique — one alias resolves to exactly one entity, so a
        -- paraphrase re-add links to the same entity ids. memory_entity rows
        -- are derived data (see storelib/entity.py): re-derived in the same
        -- transaction by every site that inserts a memory or changes its
        -- content/tags/namespace. The UNIQUE constraints below double as the
        -- lookup indexes; the two extra indexes cover the reverse direction
        -- (entity_id → aliases / links) used by entity-list and recall.
        CREATE TABLE IF NOT EXISTS entity (
            id             TEXT PRIMARY KEY,
            kind           TEXT NOT NULL
                CHECK (kind IN ('person','project','tool','preference','other')),
            canonical_name TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entity_alias (
            entity_id  TEXT NOT NULL,
            alias_norm TEXT NOT NULL,
            UNIQUE(alias_norm)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_alias_entity
            ON entity_alias(entity_id);
        CREATE TABLE IF NOT EXISTS memory_entity (
            memory_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            role      TEXT NOT NULL DEFAULT 'mentions',
            UNIQUE(memory_id, entity_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_entity_entity
            ON memory_entity(entity_id);

        -- v11 (issue #61, 6.1): associative links (A-MEM lite). Directed edge
        -- table; symmetric relations (`related`/`contradicts`/`supports`) are
        -- stored as TWO rows (one per direction) by storelib/links.py; the
        -- typed Supermemory relations (`updates`/`extends`/`derives`) are
        -- stored as the single directed row the operator authored. UNIQUE
        -- makes every insert idempotent; CHECK (src_id != dst_id) enforces no
        -- self-links at the schema level (add_link re-validates in Python).
        -- `score` is the generating similarity (vec cosine or lexical
        -- Jaccard) for auto edges, or the operator's --score for curated ones.
        CREATE TABLE IF NOT EXISTS memory_link (
            src_id     TEXT NOT NULL,
            dst_id     TEXT NOT NULL,
            relation   TEXT NOT NULL
                CHECK (relation IN ('related','supports','contradicts',
                                    'updates','extends','derives')),
            score      REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            UNIQUE(src_id, dst_id, relation),
            CHECK (src_id != dst_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_link_src ON memory_link(src_id);
        CREATE INDEX IF NOT EXISTS idx_memory_link_dst ON memory_link(dst_id);
        """
    )
    # executescript() does not accept parameter binding, so set created_at separately.
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_iso(),))
    conn.commit()
    # Ensure the content_norm column + its partial index exist (v8, #39 E4).
    # On a fresh store the CREATE TABLE above already added the column; on a
    # legacy pre-v8 store it is absent until migrate()'s v8 block runs. Probe
    # + add idempotently so the index (referenced by the dedup read path) is
    # always available regardless of migration ordering.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
    if "content_norm" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN content_norm TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_content_norm "
        "ON memory(namespace, content_norm) WHERE superseded_at IS NULL"
    )
    # v11 (issue #61, 6.1): trust_score column probe — same trap/pattern as
    # content_norm above. On a fresh store the CREATE TABLE already added it;
    # on a legacy pre-v11 store init_db()'s CREATE TABLE is a no-op and the v11
    # migrate block is the one that ALTERs it. Probe + add idempotently here so
    # the column exists regardless of migration ordering (NOT NULL is legal in
    # ALTER because the default is non-NULL; migrated rows read 1.0).
    if "trust_score" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN trust_score REAL NOT NULL DEFAULT 1.0")
    # v9 (issue #59): temporal-history index. Created ONLY when the v9 columns
    # already exist — on a fresh store the CREATE TABLE above added them, but
    # on a legacy pre-v9 store init_db()'s CREATE TABLE is a no-op and the v9
    # migrate block (which runs after init_db) is the one that adds the columns
    # AND this index. Creating it here unbounded would raise "no such column"
    # on that legacy path (same trap as idx_memory_content_norm, see the
    # executecript note above).
    if "valid_until" in cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_time "
            "ON memory(namespace, valid_from, valid_until)"
        )
    conn.commit()

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
            # v10 (issue #60): the namespace suffix is an entity-extraction
            # input, so every moved row's links are re-derived from its NEW
            # namespace in the same uncommitted batch. Covers both callers —
            # the one-time v5 walk (whose rows the v10 backfill would also
            # cover, but this keeps the invariant local) and the
            # every-migrate() retry pass, which runs AFTER the v10 block and
            # would otherwise strand stale links. GUARD: on a legacy pre-v10
            # store walking the versions, this code runs BEFORE the v10 block
            # creates the entity tables — skip then (the v10 backfill derives
            # those rows' links with their final namespace). Local import:
            # entity.py imports this module.
            has_entities = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memory_entity'"
            ).fetchone()
            if has_entities:
                from storelib.entity import relink_memory
                for rid in [r[0] for r in conn.execute(
                    "SELECT id FROM memory WHERE namespace=?", (new_ns,)
                ).fetchall()]:
                    relink_memory(conn, rid)
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
            # Same dynamic-dim rule as connect(): a store BORN under an
            # active non-default profile (e.g. CI fake stores migrating
            # v2->v3) gets its own dim; operator stores mid-profile-switch
            # are gated by assert_embedding_compatible at dispatch instead.
            conn.execute(_vec0_create_sql(_active_vec0_dim()))
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

    if ver < 7:
        # v7 (issue #21): passive surface telemetry.
        #
        # Hook-driven recall passes `--no-bump` and never advanced retrieval_count, so the
        # ONLY usefulness signal (retrieval_count) was biased toward explicit/manual use and
        # promote/prune decisions rested on incomplete data. Add surfaced_count/last_surfaced,
        # the counter that passive (`--no-bump`) recall IS allowed to advance. Same pattern as
        # the v6 block above: init_db() already creates these columns on a fresh store (and sets
        # schema_version=1, so migrate() walks 1->7), so guard the ALTER with a table_info probe
        # to keep it a true no-op when they already exist (idempotent on re-migrate too).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "surfaced_count" not in cols:
            conn.execute("ALTER TABLE memory ADD COLUMN surfaced_count INTEGER NOT NULL DEFAULT 0")
        if "last_surfaced" not in cols:
            conn.execute("ALTER TABLE memory ADD COLUMN last_surfaced TEXT")
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
        conn.commit()

    if ver < 8:
        # v8 (issue #39 E4): indexed content_norm for O(log N) exact-match dedup,
        # replacing the O(n^2) per-row Python normalize/compare that ran in
        # degraded (no-embeddings) mode. The column is populated in APPLICATION
        # code (not a SQL GENERATED column — SQLite can't replicate Python's
        # Unicode-aware .lower() + \s+ collapse). Backfill walks existing rows in
        # batches so a large store does not hold an exclusive write lock for one
        # giant UPDATE (concurrent hook writers would get SQLITE_BUSY). The
        # version bump happens AFTER the backfill so a crash mid-backfill re-runs
        # on the next start (a half-backfilled v8 store would leave NULL
        # content_norms that the indexed dedup lookup would miss).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "content_norm" not in cols:
            conn.execute("ALTER TABLE memory ADD COLUMN content_norm TEXT")
        batch_size = 500
        while True:
            rows = conn.execute(
                "SELECT rowid, content FROM memory WHERE content_norm IS NULL LIMIT ?",
                (batch_size,),
            ).fetchall()
            if not rows:
                break
            conn.executemany(
                "UPDATE memory SET content_norm=? WHERE rowid=?",
                [(_normalize_content(r["content"]), r["rowid"]) for r in rows],
            )
            conn.commit()
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_content_norm "
            "ON memory(namespace, content_norm) WHERE superseded_at IS NULL"
        )
        conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        conn.commit()

    if ver < 9:
        # v9 (issue #59): append-only knowledge-update lineage — valid_until,
        # update_of, taint. Same idempotent pattern as v6/v7/v8: init_db()
        # already creates these on a fresh store (and sets schema_version=1, so
        # migrate() walks 1->9 and the ALTERs below are table_info-probe no-ops
        # there); on a legacy pre-v9 store the probes guard the ALTERs so a
        # re-run (or a fresh-store walk) never double-adds.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "valid_until" not in cols:
            conn.execute(
                "ALTER TABLE memory ADD COLUMN valid_until TEXT NOT NULL DEFAULT ''"
            )
        if "update_of" not in cols:
            conn.execute(
                "ALTER TABLE memory ADD COLUMN update_of TEXT NOT NULL DEFAULT ''"
            )
        if "taint" not in cols:
            conn.execute(
                "ALTER TABLE memory ADD COLUMN taint TEXT NOT NULL DEFAULT "
                "'trusted_internal' CHECK (taint IN "
                "('trusted_internal','untrusted_tool','untrusted_web'))"
            )
        # Backfill valid_until from superseded_at for rows tombstoned under a
        # pre-v9 schema: point-in-time as-of DROPS the superseded_at filter (a
        # historically-superseded row may have been valid at that instant), so
        # a tombstoned row with empty valid_until would otherwise be "valid at
        # every T" and resurrect at as-of. Setting valid_until=superseded_at
        # makes the row's validity interval end exactly when it was tombstoned.
        conn.execute(
            "UPDATE memory SET valid_until=superseded_at "
            "WHERE superseded_at IS NOT NULL AND valid_until=''"
        )
        # Backfill taint from signal (issue #59, 4.7): the write-time default
        # is TRUSTED for grounded signals and untrusted_tool for `none`. A
        # legacy row must not keep the column's blanket 'trusted_internal'
        # default when a same-content row written post-v9 would derive
        # untrusted_tool — same row, same trust, regardless of when it landed.
        # Unconditional recompute is idempotent (recomputes to the same value).
        trusted_sigs = ','.join('?' * len(TAINT_TRUSTED_SIGNALS))
        conn.execute(
            f"UPDATE memory SET taint=CASE WHEN signal IN ({trusted_sigs}) "
            f"THEN 'trusted_internal' ELSE 'untrusted_tool' END",
            list(TAINT_TRUSTED_SIGNALS),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_time "
            "ON memory(namespace, valid_from, valid_until)"
        )
        conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        conn.commit()

    if ver < 10:
        # v10 (issue #60): entity identity tables + backfill. Same idempotent
        # pattern as v6/v7/v8/v9: init_db() already creates these tables on a
        # fresh store (CREATE TABLE IF NOT EXISTS here is a no-op there); on a
        # legacy pre-v10 store this block is the one that creates them. The
        # backfill then re-runs the deterministic extractor over EVERY memory
        # row — tombstoned rows included, because --as-of recall reaches
        # historical rows through the entity lane's temporal predicate, so
        # history must carry its links. Without the backfill every migrated
        # store would have a permanently empty entity table and the third RRF
        # lane would be dead on arrival — the exact "unused table" the issue
        # forbids. Memory rows themselves are never touched (lossless).
        # Version bump AFTER the backfill (v8 pattern): a crash mid-backfill
        # re-runs it on next open, and INSERT OR IGNORE links make the re-run
        # an exact no-op. The extractor import is local because entity.py
        # imports this module (no import cycle at runtime).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entity (
                id             TEXT PRIMARY KEY,
                kind           TEXT NOT NULL
                    CHECK (kind IN ('person','project','tool','preference','other')),
                canonical_name TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entity_alias (
                entity_id  TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                UNIQUE(alias_norm)
            );
            CREATE INDEX IF NOT EXISTS idx_entity_alias_entity
                ON entity_alias(entity_id);
            CREATE TABLE IF NOT EXISTS memory_entity (
                memory_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                role      TEXT NOT NULL DEFAULT 'mentions',
                UNIQUE(memory_id, entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_entity_entity
                ON memory_entity(entity_id);
        """)
        from storelib.entity import backfill_entities
        backfill_entities(conn)
        conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
        conn.commit()

    if ver < 11:
        # v11 (issue #61): associative links + trust_score. Same idempotent
        # pattern as v8/v9/v10: init_db() already creates the table/column on
        # a fresh store (the executescript below is a no-op there); on a
        # legacy pre-v11 store this block is the one that creates them. NO
        # link backfill runs here — links accumulate from new writes, and the
        # phase-7 `organize` command owns bulk backfill (issue #61 "Blocks").
        # The one data change is the lossless merged_from normalization pass
        # (issue #61, 6.6): "a,b,a" -> "a,b" (first-seen order, no id lost),
        # so the de-duplicated-list invariant holds even for stores whose
        # history predates the fix. Version bump AFTER the pass (v8 pattern):
        # a crash mid-pass re-runs it; the helper is idempotent.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_link (
                src_id     TEXT NOT NULL,
                dst_id     TEXT NOT NULL,
                relation   TEXT NOT NULL
                    CHECK (relation IN ('related','supports','contradicts',
                                        'updates','extends','derives')),
                score      REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                UNIQUE(src_id, dst_id, relation),
                CHECK (src_id != dst_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_link_src ON memory_link(src_id);
            CREATE INDEX IF NOT EXISTS idx_memory_link_dst ON memory_link(dst_id);
        """)
        v11_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "trust_score" not in v11_cols:
            conn.execute(
                "ALTER TABLE memory ADD COLUMN trust_score REAL NOT NULL DEFAULT 1.0"
            )
        from storelib.consolidate import _dedupe_merged_from
        for r in conn.execute(
            "SELECT rowid, merged_from FROM memory "
            "WHERE merged_from IS NOT NULL AND merged_from != ''"
        ).fetchall():
            cleaned = _dedupe_merged_from(r["merged_from"])
            if cleaned != r["merged_from"]:
                conn.execute(
                    "UPDATE memory SET merged_from=? WHERE rowid=?",
                    (cleaned, r["rowid"]),
                )
        conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
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

def _normalize_content(s: str) -> str:
    """Alias of schema_meta.normalize_content — kept because every writer
    and several tests import THIS name from this module. The implementation
    moved to schema_meta (issue #63 critic round C5) so embed_profiles' fake
    embedder hashes the identical canonical form without importing this
    module.
    """
    return normalize_content(s)

def _parse_iso_to_epoch(ts: str) -> float:
    """Parse an ISO-8601 UTC timestamp to epoch seconds. Returns 0 on failure."""
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0

def _as_of_temporal_predicate(as_of: str | None, alias: str = "") -> tuple[str, list]:
    """Build the as-of validity predicate for one recall lane (issue #59, 4.4).

    Returns ``(sql_clause, params)``. ``as_of=None`` returns ``("", [])`` — the
    caller KEEPS its hard ``superseded_at IS NULL`` filter (default recall is
    "as of now": live rows only). When ``as_of`` is set, the caller must also
    DROP that hard filter: a historically-superseded row can be valid at an
    instant before its tombstone, and the issue's point-in-time contract is to
    return rows that were valid at that instant.

    Validity-interval semantics (matching the write path): ``valid_from`` is
    INCLUSIVE (the row is born at valid_from) and ``valid_until`` is EXCLUSIVE
    (the row expires at valid_until, so it is still valid at any T strictly
    before valid_until). Empty valid_until means "never expires".

    ``alias`` prefixes the column names (e.g. ``m`` for the FTS lane that
    joins ``memory m``); the other lanes reference the bare table and pass ``""``.
    """
    if not as_of:
        return "", []
    prefix = f"{alias}." if alias else ""
    return (
        f" AND ({prefix}valid_from = '' OR {prefix}valid_from <= ?) "
        f"AND ({prefix}valid_until = '' OR {prefix}valid_until > ?) ",
        [as_of, as_of],
    )

GLOBAL_NAMESPACE = "user:global"
