"""Issue #290 (Workstream T PR 11): the declared offline mode.

A revisioned, expiring, revocable read-only snapshot of the hub's
memory rows that the broker serves ONLY while the hub is unreachable:

- ``build_snapshot`` (the PUBLIC seam — the only ``…snapshot…``-named
  public callable in fleet; issue #289's T-10 probe discovered the AC5
  builder by exactly that property) builds the scoped pair
  ``<data>/broker/snapshot.sqlite`` + ``snapshot.meta.json`` from a
  local source store: user:global unconditionally plus authorized
  PROJECT namespaces with read-or-wrote activity in the last 30 days
  (the AC5 dual scope law — the hub-side tool applies the same law).
- ``_load`` opens the snapshot ``mode=ro`` + ``query_only=1``; the
  serve path never writes through it, and the zero-write selector mode
  (``no_telemetry=True``) keeps the selector from trying.
- ``_serve_offline_read`` runs the SAME selector over the snapshot and
  prefixes the rendered fence with the ``[OFFLINE SNAPSHOT r<rev>,
  <age> old]`` header line — broker-emitted, never row content.
- ``_maybe_refresh`` pulls a fresh snapshot from the hub's ``snapshot``
  MCP tool when none exists, when the last one is older than the
  hourly cadence, or when the persisted ``hub-unreachable.stamp``
  records an unreachable-to-reachable TRANSITION (every production
  broker moment is a fresh subprocess — only a persisted marker can
  observe the transition; the stamp clears ONLY when a refresh
  completes, so a skipped refresh re-arms for the next moment).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_TTL_S = 72 * 3600
REFRESH_INTERVAL_S = 3600
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
GLOBAL_NAMESPACE = "user:global"
ACTIVITY_WINDOW_S = 30 * 24 * 3600
STAMP_NAME = "hub-unreachable.stamp"

_SNAPSHOT_COLUMNS = (
    "id, namespace, type, content, tags, source_ref, confidence, signal, "
    "valid_from, valid_until, update_of, taint, ingestion_ts, superseded_at, "
    "supersede_reason, merged_from, trust_score, applied_count, "
    "violated_count, embedding, embedding_model, retrieval_count, "
    "last_retrieved, surfaced_count, last_surfaced"
)


def _data_dir(env: dict[str, str]) -> Path:
    import host  # noqa: PLC0415 - lazy: reads env at call time
    sync = {k: env.get(k) for k in (
        "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
        "ZCODE_PLUGIN_DATA") if env.get(k)}
    saved = {k: os.environ.get(k) for k in sync}
    try:
        for k, v in sync.items():
            os.environ[k] = v
        for k in set(os.environ) - set(sync):
            if k in ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
                     "ZCODE_PLUGIN_DATA") and k not in sync:
                os.environ.pop(k, None)
        return Path(host.resolve_store_path()).expanduser().parent
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _broker_dir(env: dict[str, str]) -> Path:
    d = _data_dir(env) / "broker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_paths(env: dict[str, str]) -> tuple[Path, Path]:
    d = _broker_dir(env)
    return d / "snapshot.sqlite", d / "snapshot.meta.json"


def _stamp_path(env: dict[str, str]) -> Path:
    return _broker_dir(env) / STAMP_NAME


def _activity_namespaces(conn: sqlite3.Connection,
                         now: float) -> set[str]:
    """Namespaces with read-or-wrote activity in the AC5 30d window."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
        now - ACTIVITY_WINDOW_S))
    rows = conn.execute(
        "SELECT DISTINCT namespace FROM memory "
        "WHERE max(coalesce(ingestion_ts,''), coalesce(last_retrieved,''), "
        "coalesce(last_surfaced,'')) >= ?", (cutoff,)).fetchall()
    return {r[0] for r in rows}


def _scope_clause(authorized: Optional[frozenset], conn: sqlite3.Connection,
                  now: float) -> tuple[str, list]:
    """The AC5 dual scope law: user:global unconditional + authorized
    project namespaces WITH 30d activity (authorization AND recency)."""
    params: list = []
    if authorized is None:
        # No allow-list supplied (a local builder default): user:global
        # plus every namespace with recent activity — the unscoped shape.
        active = _activity_namespaces(conn, now)
        namespaces = {GLOBAL_NAMESPACE} | active
    else:
        active = _activity_namespaces(conn, now)
        namespaces = {GLOBAL_NAMESPACE} | {
            ns for ns in authorized if ns != GLOBAL_NAMESPACE
            and ns in active}
    if not namespaces:
        return "1=0", params
    placeholders = ",".join("?" * len(namespaces))
    params.extend(sorted(namespaces))
    return f"namespace IN ({placeholders})", params


def build_snapshot(*, source_store: str | None = None,
                   store_path: str | None = None,
                   store: str | None = None,
                   source: str | None = None,
                   data_dir: str | None = None,
                   snapshot_path: str | None = None,
                   dest: str | None = None,
                   out_path: str | None = None,
                   path: str | None = None,
                   snapshot_dir: str | None = None,
                   namespace: str | None = None,
                   namespaces: frozenset | set | None = None,
                   authorized_namespaces: frozenset | set | None = None,
                   principal: str = "local",
                   env: dict[str, str] | None = None,
                   revision: int | None = None) -> Path:
    """Build the scoped snapshot pair from a local source store.

    The PUBLIC build seam (issue #290 AC5 — the only ``…snapshot…``
    public callable in fleet).  Aliases collapse to (source store,
    destination, scope, principal, env, revision).  Writes
    ``snapshot.sqlite`` (atomic, owner-only, <= 64 MB) THEN the meta
    sidecar (72 h expiry); a meta failure after a successful sqlite
    replace restores the previous pair from ``snapshot.prev.sqlite``.
    """
    src = Path(source_store or store_path or store or source or "").expanduser()
    if not src.is_file():
        raise ValueError(f"snapshot source store not found: {src}")
    e = dict(env) if env else dict(os.environ)
    if snapshot_path or dest or out_path or path:
        snap = Path(snapshot_path or dest or out_path or path).expanduser()
        snap.parent.mkdir(parents=True, exist_ok=True)
    elif data_dir or snapshot_dir:
        base = Path(snapshot_dir or data_dir).expanduser()  # type: ignore[arg-type]
        base.mkdir(parents=True, exist_ok=True)
        snap = base / "snapshot.sqlite"
    else:
        snap, _meta = _snapshot_paths(e)
    scope_ns = None
    if authorized_namespaces is not None:
        scope_ns = frozenset(authorized_namespaces)
    elif namespaces is not None:
        scope_ns = frozenset(namespaces)
    elif namespace and namespace != "*":
        scope_ns = frozenset({namespace})

    now = time.time()
    src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True,
                               timeout=5.0)
    src_conn.execute("PRAGMA query_only=1")
    try:
        where, params = _scope_clause(scope_ns, src_conn, now)
        live = src_conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM memory "
            f"WHERE {where} AND superseded_at IS NULL", params).fetchall()
        colnames = [d[0] for d in src_conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM memory LIMIT 0").description]
        revoked = src_conn.execute(
            f"SELECT id FROM memory WHERE {where} "
            f"AND superseded_at IS NOT NULL", params).fetchall()
    finally:
        src_conn.close()

    # Copy the FULL source schema (the selector reads memory,
    # memory_fts, memory_expiry, episode tables and more — a hand-built
    # subset starves it into empty envelopes), then delete out-of-scope
    # rows in place.
    tmp = snap.with_suffix(snap.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    out = sqlite3.connect(str(tmp))
    src_copy = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True,
                               timeout=5.0)
    try:
        out.execute("PRAGMA foreign_keys=OFF")
        src_copy.backup(out)
        out.commit()
    finally:
        src_copy.close()
    try:
        placeholders = ",".join("?" * len(params))
        out.execute(
            f"DELETE FROM memory WHERE namespace NOT IN ({placeholders})",
            params)
        try:
            out.execute(
                "DELETE FROM memory_expiry WHERE memory_id NOT IN "
                f"(SELECT id FROM memory)")
        except sqlite3.OperationalError:
            pass
        try:
            out.execute(
                "DELETE FROM memory_entity WHERE memory_id NOT IN "
                f"(SELECT id FROM memory)")
        except sqlite3.OperationalError:
            pass
        try:
            out.execute(
                "DELETE FROM episode_memory WHERE memory_id NOT IN "
                f"(SELECT id FROM memory)")
        except sqlite3.OperationalError:
            pass
        try:
            out.execute(
                "DELETE FROM memory_link WHERE src_id NOT IN "
                f"(SELECT id FROM memory) AND dst_id NOT IN "
                f"(SELECT id FROM memory)")
        except sqlite3.OperationalError:
            pass
        try:
            # Scope law (final-critic B4): purged ids are unnamespaced
            # post-purge, so copying them would leak OTHER principals'
            # row identifiers into a scoped snapshot.  Snapshots carry
            # superseded-only revocations; a purged row's absence from
            # the live rows IS its revocation.
            out.execute("DELETE FROM purged_id")
        except sqlite3.OperationalError:
            pass
        out.commit()
    finally:
        out.close()
    if tmp.stat().st_size > MAX_SNAPSHOT_BYTES:
        tmp.unlink()
        raise ValueError(
            "snapshot exceeds the 64 MB cap; the previous snapshot is kept")

    prev = snap.with_name("snapshot.prev.sqlite")
    if snap.exists():
        if prev.exists():
            prev.unlink()
        os.replace(snap, prev)
    try:
        os.replace(tmp, snap)
    except OSError:
        if prev.exists():
            os.replace(prev, snap)
        raise
    os.chmod(snap, 0o600)

    rev = int(revision) if revision is not None else (len(live) + len(revoked))
    meta = {
        "snapshot_revision": rev,
        "issued_at": now,
        "expires_at": now + DEFAULT_TTL_S,
        "row_count": len(live),
        "revoked_count": len(revoked),
        "principal": principal,
        "revocations": [r[0] if isinstance(r, tuple) else r
                        for r in revoked],
    }
    meta_path = snap.with_name("snapshot.meta.json")
    meta_tmp = meta_path.with_suffix(".json.tmp")
    try:
        meta_tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        os.replace(meta_tmp, meta_path)
    except OSError:
        # Meta failed after the sqlite replace: restore the previous pair.
        snap.unlink(missing_ok=True)
        if prev.exists():
            os.replace(prev, snap)
        raise
    os.chmod(meta_path, 0o600)
    return snap


def _load(env: dict[str, str]) -> tuple[sqlite3.Connection | None, dict]:
    """Open the snapshot read-only; (None, meta) when absent.

    A snapshot whose sqlite file exists but carries no ``memory`` table
    (corrupt, truncated, or a foreign sqlite dropped at the contract
    path) is treated as ABSENT — returning it to the selector would
    serve a silent empty envelope with no hub-unreachable record, so the
    state stays ``none`` and the caller keeps the error contract.
    """
    snap, meta_path = _snapshot_paths(env)
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    if not snap.is_file() or not meta:
        return None, meta
    conn = sqlite3.connect(f"file:{snap.resolve()}?mode=ro", uri=True,
                           timeout=2.0)
    try:
        has_memory = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'memory'").fetchone()
        if has_memory is None:
            conn.close()
            return None, meta
    except sqlite3.Error:
        conn.close()
        return None, meta
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn, meta


def _state(meta: dict, now: float | None = None) -> str:
    """none | expired | valid (the 72 h default window)."""
    if not meta:
        return "none"
    now = time.time() if now is None else now
    try:
        if float(meta.get("expires_at", 0)) <= now:
            return "expired"
    except (TypeError, ValueError):
        return "none"
    return "valid"


def _age_label(meta: dict, now: float) -> str:
    try:
        age_s = max(0.0, now - float(meta.get("issued_at", now)))
    except (TypeError, ValueError):
        age_s = 0.0
    hours = age_s / 3600.0
    if hours >= 48.0:
        return f"{int(hours // 24)}d"
    if hours >= 1.0:
        return f"{int(hours)}h"
    return f"{int(age_s // 60)}m"


def _label(meta: dict, now: float) -> str:
    return (f"[OFFLINE SNAPSHOT r{int(meta.get('snapshot_revision', 0))}, "
            f"{_age_label(meta, now)} old]")


def write_stamp(env: dict[str, str]) -> None:
    """Record a hub-contact failure for the reconnect transition."""
    stamp = _stamp_path(env)
    tmp = stamp.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    os.replace(tmp, stamp)


def clear_stamp(env: dict[str, str]) -> None:
    _stamp_path(env).unlink(missing_ok=True)


def stamp_present(env: dict[str, str]) -> bool:
    return _stamp_path(env).is_file()


def _build_from_payload(payload: dict, env: dict[str, str]) -> Path:
    """Materialize the hub `snapshot` tool payload via build_snapshot.

    The payload source is created through the REAL store schema
    (storelib.schema.init_db — memory + memory_fts + triggers + expiry +
    entity/episode/link + vec when the extension loads): a hand-built
    subset starves the selector into empty envelopes (the same reason
    build_snapshot copies full stores).  The FTS triggers fire on INSERT,
    so payload rows are searchable; memory_vec carries no chunks (the
    payload ships embedding blobs in memory.embedding only), which IS the
    documented broker-side degrade — a profile-mismatched or
    vector-absent snapshot serves through the lexical lanes.
    """
    import base64

    tmp_dir = _broker_dir(env) / "payload-src"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / "src.sqlite"
    if src.exists():
        src.unlink()
    out = sqlite3.connect(str(src))
    ncols = len(_SNAPSHOT_COLUMNS.split(","))
    embedding_idx = 19  # position of `embedding` in _SNAPSHOT_COLUMNS

    def _decode(row):
        row = list(row)
        if len(row) == ncols and isinstance(row[embedding_idx], str):
            # The hub tool base64-encodes embedding blobs for JSON
            # transport (payload["embedding_b64"]); decode back to the
            # BLOB the store expects.
            row[embedding_idx] = base64.b64decode(row[embedding_idx])
        return tuple(row)

    try:
        from storelib.schema import init_db, migrate  # noqa: PLC0415 - lazy
        # init_db creates the base tables; migrate() brings the LATER
        # columns the selector reads (supersede_reason, embedding, …) —
        # the same pair connect()/_prepare_store runs on a real store.
        # The schema lock is unnecessary here: payload-src is a private
        # single-writer scratch, never a shared store.
        init_db(out)
        migrate(out)
        out.executemany(
            "INSERT INTO memory (" + _SNAPSHOT_COLUMNS + ") VALUES (" +
            ",".join("?" * ncols) + ")",
            [_decode(r) for r in payload.get("rows", [])])
        out.commit()
    finally:
        out.close()
    return build_snapshot(
        source_store=str(src), data_dir=str(_broker_dir(env)),
        principal=str(payload.get("principal", "local")),
        env=env, revision=payload.get("snapshot_revision"))


def _refresh(http: Any, env: dict[str, str],
             deadline_ms: int | None) -> bool:
    """Pull a fresh snapshot from the hub's `snapshot` tool."""
    try:
        payload = http.snapshot(deadline_ms=deadline_ms)
    except Exception:  # noqa: BLE001 - refresh is best-effort
        return False
    # Affirmative-success gate: every hub-ANSWERED refusal ({error: ...}
    # from the 64 MB cap, an unreadable store, a deadline response, or a
    # protocol refusal) carries NO truthy `ok` — treating those as a
    # payload would build an EMPTY snapshot, "complete" the refresh, and
    # clear the reconnect stamp while destroying a good pair.  Only an
    # explicit ok=True proceeds (a legitimately empty scoped store is
    # ok=True with rows == [] and IS a valid refresh).
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    if int(payload.get("payload_bytes", 0)) > MAX_SNAPSHOT_BYTES:
        return False
    try:
        _build_from_payload(payload, env)
        return True
    except (ValueError, OSError, sqlite3.Error):
        return False


def _maybe_refresh(http: Any, env: dict[str, str],
                   budget_ms: int | None = None) -> bool:
    """Refresh ONLY on: none | stale | the persisted transition.

    Ordering: stamp check -> refresh attempt -> clear ONLY on completion
    (a skipped/failed refresh leaves the stamp armed for the next
    moment — the per-moment subprocess model has no other memory).
    """
    _snap, meta_path = _snapshot_paths(env)
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    try:
        interval = float(env.get("ZMEM_SNAPSHOT_REFRESH_S", "")
                         or REFRESH_INTERVAL_S)
    except ValueError:
        interval = REFRESH_INTERVAL_S
    now = time.time()
    stale = (not meta) or (
        now - float(meta.get("issued_at", 0) or 0) > interval)
    transition = stamp_present(env)
    if not stale and not transition:
        return False
    if _refresh(http, env, budget_ms):
        clear_stamp(env)
        return True
    return False


def offline_label(env: dict[str, str]) -> str | None:
    """The bare ``[OFFLINE SNAPSHOT r<rev>, <age> old]`` label (None when
    no valid snapshot exists) — the CLI lane's provenance value."""
    _conn, meta = _load(env)
    if _conn is not None:
        _conn.close()
    if _state(meta) != "valid":
        return None
    return _label(meta, time.time())


def _serve_offline_list(env: dict[str, str], namespace: str | None,
                        limit: int = 50,
                        include_superseded: bool = False) -> dict | None:
    """CLI `list` lane: the offline 3-key object + rows from the snapshot.

    Zero-write direct read (mode=ro + query_only); returns None for the
    none/expired states so the caller keeps the hub-unreachable contract.
    """
    conn, meta = _load(env)
    if conn is None:
        return None
    if _state(meta) != "valid":
        conn.close()
        return None
    try:
        where = ""
        params: list = []
        if namespace and namespace != "*":
            where = "WHERE namespace = ?"
            params = [namespace]
        if not include_superseded:
            where = (where + " AND superseded_at IS NULL") if where \
                else "WHERE superseded_at IS NULL"
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT id, namespace, type, content, tags, source_ref, "
            f"confidence, signal, ingestion_ts, superseded_at "
            f"FROM memory {where} ORDER BY ingestion_ts DESC LIMIT ?",
            params).fetchall()
        memories = [{k: row[k] for k in row.keys()} for row in rows]
        return {
            "namespace": namespace or "*",
            "count": len(memories),
            "memories": memories,
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _serve_offline_get(env: dict[str, str], memory_id: str) -> dict | None:
    """CLI `get` lane: one row from the snapshot (None when absent or the
    snapshot is none/expired — the caller keeps the error contract)."""
    conn, meta = _load(env)
    if conn is None:
        return None
    if _state(meta) != "valid":
        conn.close()
        return None
    try:
        row = conn.execute(
            "SELECT id, namespace, type, content, tags, source_ref, "
            "confidence, signal, ingestion_ts, superseded_at "
            "FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _serve_offline_recent(env: dict[str, str], namespace: str | None,
                          limit: int = 10) -> dict | None:
    """CLI `recent` lane: the 6-key read envelope over the snapshot's most
    recently ingested rows (advisory token accounting — the offline serve
    is a last-resort answer, never a selector substitute for recency)."""
    conn, meta = _load(env)
    if conn is None:
        return None
    if _state(meta) != "valid":
        conn.close()
        return None
    try:
        where = ""
        params: list = []
        if namespace and namespace != "*":
            where = "WHERE namespace = ?"
            params = [namespace]
        where = (where + " AND superseded_at IS NULL") if where \
            else "WHERE superseded_at IS NULL"
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT id, namespace, type, content, tags, source_ref, "
            f"confidence, signal, ingestion_ts, superseded_at "
            f"FROM memory {where} ORDER BY ingestion_ts DESC LIMIT ?",
            params).fetchall()
        results = [{k: row[k] for k in row.keys()} for row in rows]
        from storelib.inject import inject_token_budget  # noqa: PLC0415
        budget = inject_token_budget()
        used = sum(len(str(r.get("content", ""))) // 4 for r in results)
        return {
            "results": results,
            "count": len(results),
            "omitted": 0,
            "injection_risk": 0,
            "tokens_used": min(used, budget),
            "tokens_budget": budget,
        }
    except (sqlite3.Error, Exception):  # noqa: BLE001 - fail to None
        return None
    finally:
        conn.close()


def _serve_offline_read(env: dict[str, str], *, query: str,
                        namespace: str, moment: str, session_id: str,
                        lane: str | None = None,
                        limit: int = 5) -> tuple[dict | None, str]:
    """Serve one read moment from the snapshot via the SAME selector.

    Returns (envelope, state): envelope None when the state is none or
    expired.  Zero-write: the selector runs with ``reserve=False`` and
    ``no_telemetry=True``; the label is prepended to the rendered fence
    as a broker-emitted header line.
    """
    try:
        conn, meta = _load(env)
    except ValueError as exc:
        # Issue #291: a StoreConfigError (a directory variable naming an
        # existing file) reaches _load through _data_dir's env-synced
        # resolver call, which sits BEFORE the local try below — the
        # fail-open contract (envelope None, state none) must hold for the
        # hermes provider's in-process lane too, not just broker_cli's
        # outer catch. Only the config-refusal ValueError degrades; any
        # other ValueError is a real defect and still raises.
        try:
            import host as _host  # noqa: PLC0415
            if not isinstance(exc, _host.StoreConfigError):
                raise
        except ImportError:
            raise
        return None, "none"
    if conn is None:
        return None, "none"
    state = _state(meta)
    if state != "valid":
        conn.close()
        return None, state
    try:
        from storelib.inject import (  # noqa: PLC0415 - lazy by design
            inject_token_budget, select_and_budget_for_injection)
        envelope = select_and_budget_for_injection(
            conn, query=query, namespace=namespace, moment=moment,
            session_id=session_id, lane=lane, limit=limit,
            budget_tokens=inject_token_budget(),
            authorized_namespaces=None, reserve=False, no_telemetry=True)
    except Exception:  # noqa: BLE001 - the offline serve fails open
        return None, state
    finally:
        conn.close()
    now = time.time()
    label = _label(meta, now)
    rendered = envelope.get("rendered")
    if isinstance(rendered, str) and rendered:
        envelope["rendered"] = label + "\n" + rendered
    context = envelope.get("context")
    if isinstance(context, str) and context:
        envelope["context"] = label + "\n" + context
    return envelope, state
