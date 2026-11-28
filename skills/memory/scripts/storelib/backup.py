from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import tempfile
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import glob
from datetime import datetime, timezone
from pathlib import Path
from storelib.opcmds import command_prefix
from storelib.schema import _acquire_schema_lock_with_liveness, MAINTENANCE_LOCK_STALE_SECONDS, SCHEMA_LOCK_POLL_SECONDS, SCHEMA_LOCK_STALE_SECONDS, SCHEMA_LOCK_WAIT_SECONDS, STORE_PATH, _acquire_maintenance_lock_with_liveness, _cleanup_stale_writer_leases, _commit, _env_float, _host, _parse_iso_to_epoch, _read_schema_version, _release_named_lock, _strict_acquire_lock, _wait_for_open_connections, now_iso

SNAPSHOT_PREFIX = "store-"

PRERESTORE_PREFIX = "prerestore-"

SNAPSHOT_SUFFIX = ".sqlite"
# Retention only ever considers files matching THIS glob. `prerestore-*` files
# deliberately fall outside it: the safety copy taken right before a restore is
# the rollback path for that restore and must never be pruned by an unrelated
# automatic backup rotation.

SNAPSHOT_GLOB = SNAPSHOT_PREFIX + "*" + SNAPSHOT_SUFFIX


BACKUP_DEFAULT_RETENTION = 7

# Per-session cooldown sentinel sweep (issue #23 "Minor, related"). The
# capture/convention hooks write one dot-named marker file per session into the
# data dir to de-duplicate their prompt within that session; nothing removed
# them, so they accumulated unboundedly. A marker is only meaningful for the
# session named in its filename, so anything older than this TTL is garbage.
# Default matches backup retention. The SessionStart hook fires `sweep` detached
# each session so the dir stays bounded.

SENTINEL_PREFIXES = (".capture-prompted-", ".convention-prompted-", ".convention-commit-prompted-", ".drift-checked-")

SENTINEL_SWEEP_DAYS_DEFAULT = _env_float("ZMEM_SENTINEL_SWEEP_DAYS", 7.0)


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

def _sweep_candidate_dirs() -> list[Path]:
    """Every directory the capture/convention hooks may write their per-session
    cooldown markers into (union of both hooks' resolution chains, deduped)."""
    dirs: list[Path] = []

    def _add(p: Path) -> None:
        if p not in dirs:
            dirs.append(p)

    store = os.environ.get("ZMEM_STORE")
    if store:
        _add(Path(store).parent)
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        v = os.environ.get(var)
        if v:
            _add(Path(v))
    _add(Path.home() / ".zmem")
    legacy = Path.home() / ".zcode" / "memory"
    if legacy.is_dir():
        _add(legacy)
    scan = Path.home() / ".zcode" / "cli" / "plugins" / "data"
    if scan.is_dir():
        try:
            for d in scan.iterdir():
                if d.is_dir() and "zmem" in d.name.lower():
                    _add(d)
        except OSError:
            pass  # fail-open: an unreadable scan root must never break the sweep
    return dirs

CONVENTION_META_PREFIXES = (
    "convention_count_",          # retired hook-era writer (orphaned rows)
    "convention_count_sess_",     # retired hook-era writer (orphaned rows)
    "hermes_convention_count_",   # live hermes ring writer (0.144.0 JSON shape)
)

# Set by cmd_sweep after every run so callers that drive the sweep in-process
# (session-cadence) can fold the meta counts into their own summary without
# re-deriving them. None means "meta pruning skipped" (absent/locked/
# unreadable/newer-schema store) or "cmd_sweep not yet run".
last_meta_prune: "tuple[int, int, int] | None" = None


CONVENTION_ROW_CAP_DEFAULT = 64


def convention_row_cap() -> int:
    """The per-host bound on retained PARSEABLE convention rows (issue
    #446 AC2). Env-tunable via ZMEM_CONVENTION_ROW_CAP; an invalid,
    negative, or unparsable value falls back to the default (the
    ZMEM_SENTINEL_SWEEP_DAYS finite-check discipline; 0 is a valid
    aggressive setting — everything deletable goes on every call)."""
    raw = os.environ.get("ZMEM_CONVENTION_ROW_CAP", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return CONVENTION_ROW_CAP_DEFAULT
    if value < 0:
        return CONVENTION_ROW_CAP_DEFAULT
    return value


def convention_family_key_count(conn) -> int:
    """Key-only family COUNT over the meta PRIMARY KEY (issue #446): the
    per-hermes-convention-call trigger probe. Prefix RANGE predicates
    (never substr, never value parsing) so the index serves the scan;
    deduped by key because 'convention_count_' is itself a prefix of
    'convention_count_sess_'."""
    seen: "set[str]" = set()
    for prefix in CONVENTION_META_PREFIXES:
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        for row in conn.execute(
                "SELECT key FROM meta WHERE key >= ? AND key < ?",
                (prefix, upper)):
            if row[0] not in seen:
                seen.add(row[0])
    return len(seen)


def _prune_convention_meta_conn(conn, *, cutoff_epoch: float,
                                current_session: str,
                                now_epoch: "float | None" = None,
                                row_cap: "int | None" = None,
                                dry_run: bool = False
                                ) -> "tuple[int, int, int] | None":
    """Connection-agnostic core (issue #446). The CALLER owns the
    transaction on real runs (BEGIN IMMEDIATE before, commit/rollback
    after); a dry run only reads. Returns (removed, retained,
    unparseable), or None when the schema gate refuses (a pre-init or
    newer-than-supported store — the documented skip).

    row_cap (issue #446 accounting law) bounds the RETAINED parseable
    population: a parseable CURRENT row consumes one slot while
    remaining undeletable; retained parseable non-current rows fill the
    rest, deleted oldest first by (last_seen, key) — one-second
    timestamp resolution makes the key tie-breaker load-bearing.
    Non-current legacy rows never survive the TTL pass (stale by
    construction), so the cap order never sees a legacy bucket.
    Malformed rows are NEVER deleted (#128 operator-inspection
    contract), are EXCLUDED from cap arithmetic, and are counted
    unparseable."""
    from schema_meta import (
        SUPPORTED_SCHEMA_VERSION,
        classify_convention_count,
        convention_count_last_seen,
        convention_count_value,
        parse_convention_count,
    )
    try:
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return None
    try:
        version = int(stamp[0]) if stamp else None
    except (TypeError, ValueError):
        version = None
    if version is None or version > SUPPORTED_SCHEMA_VERSION:
        return None

    now = time.time() if now_epoch is None else float(now_epoch)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    current_session = (current_session or "").strip()

    removed = retained = unparseable = 0
    current_parseable = 0
    kept_json: "list[tuple[float, str]]" = []
    rows: "dict[str, object]" = {}
    for prefix in CONVENTION_META_PREFIXES:
        # PRR-006: prefix RANGE predicates (like the trigger count) so the
        # meta PRIMARY KEY index serves the scan — a substr() prefix match
        # here forces a full meta-table SCAN on every classification pass.
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        for row in conn.execute(
                "SELECT key, value FROM meta WHERE key >= ? AND key < ?",
                (prefix, upper)):
            # Dedupe by key: 'convention_count_' is itself a prefix of
            # 'convention_count_sess_', so that family's rows match TWO
            # prefix scans -- without the dict they would be classified
            # (and deleted) twice.
            rows[row[0]] = row[1]
    for key, value in rows.items():
        suffix = None
        for prefix in sorted(CONVENTION_META_PREFIXES, key=len,
                              reverse=True):
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                break
        is_current = bool(current_session) and suffix == current_session
        kind = classify_convention_count(value)
        if is_current:
            retained += 1
            # Implementation-review round 1 finding 1: a MALFORMED
            # current row is excluded from cap arithmetic like every
            # other malformed row (the accounting law) — only a
            # parseable current row consumes one of the row_cap slots.
            if kind != "malformed":
                current_parseable += 1
            else:
                # PRR-009: the docstring law counts EVERY malformed row
                # unparseable — a current malformed row is retained and
                # cap-excluded, and still reported in the census.
                unparseable += 1
            if kind == "legacy" and not dry_run:
                # #128 AC2: upgrade the retained current-session legacy
                # row to canonical JSON, count unchanged. A current row
                # is NEVER a cap-deletion candidate (it consumes a slot
                # and stays), so it never enters kept_json.
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = ?",
                    (convention_count_value(
                        parse_convention_count(value), now_iso), key))
            continue
        if kind == "legacy":
            # Non-current legacy rows carry no age evidence and no live
            # writer exists for any family's integer shape -> stale.
            removed += 1
            if not dry_run:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            continue
        if kind == "json":
            # classify == "json" guarantees last_seen is an exact
            # canonical timestamp (schema_meta._valid_convention_last_seen,
            # impl-review round 1 finding 3), so the ISO parse is exact and
            # a corrupt heartbeat can never fall through to epoch 0.
            epoch = _parse_iso_to_epoch(convention_count_last_seen(value))
            if epoch < cutoff_epoch:
                removed += 1
                if not dry_run:
                    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                retained += 1
                kept_json.append((epoch, key))
            continue
        # malformed: retained, counted, never deleted
        retained += 1
        unparseable += 1
    if row_cap is not None:
        # PRR-008: the cap pass computes (and reports) the doomed set in
        # BOTH arms so a dry run's removed/retained predict the real run;
        # only the DELETEs are guarded by the dry_run flag.
        slots = row_cap - current_parseable
        if slots < 0:
            slots = 0
        if len(kept_json) > slots:
            # Oldest-first (last_seen, key) — kept_json holds ONLY
            # non-current parseable rows (current rows consume slots
            # and never enter the candidate list).
            kept_json.sort()
            doomed = {key for _, key in kept_json[:len(kept_json) - slots]}
            if not dry_run:
                for key in doomed:
                    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            removed += len(doomed)
            retained -= len(doomed)
    return (removed, retained, unparseable)


def prune_convention_meta(store_path, *, cutoff_epoch: float,
                          current_session: str,
                          dry_run: bool = False,
                          now_epoch: "float | None" = None,
                          row_cap: "int | None" = None
                          ) -> "tuple[int, int, int] | None":
    """Bound the per-session convention-counter meta rows (issue #128;
    issue #446 adds the maintenance-lock serialization and row_cap).

    Deletes/updates ONLY keys under CONVENTION_META_PREFIXES (true-prefix
    predicates — never a LIKE wildcard match). The real-run arm runs the
    core in ONE write transaction; the dry-run arm reads through the
    sidecar-free readonly opener and deletes nothing.

    Maintenance protocol (filed issue #446 AC3): BOTH arms acquire the
    maintenance lock BEFORE opening the store and release it only AFTER
    the handle is closed — on Windows even an IDLE open handle blocks a
    concurrent restore's os.replace of store.sqlite (reproduced:
    PermissionError WinError 5), so the lock must cover the whole
    connection lifetime, and the dry-run handle is no exception. A live
    holder (e.g. restore mid-replacement) makes this SKIP: return None,
    the same shape as every other unopenable-store arm — fail-open, the
    sweep's file phases and exit code are unaffected. cmd_backup needs
    no such serialization: its online SQLite backup is concurrency-safe
    against writers by design (single-flight backup lock only).

    row_cap passes through to the core (None = TTL-only, the sweep-path
    behavior; the hermes-convention inline prune passes the cap).

    Returns ``(removed, retained, unparseable)``, or ``None`` when the
    store is absent, locked, unreadable, not a database, missing its
    meta table, stamped newer than this client supports, or the
    maintenance lock is held (skip: the sweep must never break on, or
    touch, a store it cannot safely open). Mid-transaction sqlite errors
    roll back and re-raise."""
    from storelib.schema import (
        _acquire_maintenance_lock_with_liveness,
        _release_named_lock,
    )
    store_path = Path(store_path)
    if not store_path.is_file():
        return None
    m_token = _acquire_maintenance_lock_with_liveness()
    if m_token is None:
        return None
    try:
        handle = None
        conn = None
        if dry_run:
            # Sidecar-free read-only open (issue #345 law; review round
            # PRR-005: a bare mode=ro open materializes -wal/-shm on a
            # WAL store). A residue/copy failure raises
            # ReadOnlyStoreError (a RuntimeError, NOT sqlite3.Error) —
            # the caller's fail-open handler skips the meta phase
            # exactly like every other unopenable-store arm, so it is
            # in this catch tuple by contract (delta review r1).
            from storelib.readonly import open_readonly
            try:
                handle = open_readonly(store_path)
            except (sqlite3.Error, OSError, RuntimeError):
                return None
            conn = handle.conn
        else:
            uri = store_path.resolve().as_uri() + "?mode=rw"
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            except sqlite3.Error:
                return None
        try:
            if not dry_run:
                # Locked, garbage, or truncated stores fail HERE
                # (OperationalError / DatabaseError) — the documented
                # None-skip arms, not a raise (delta review r1: the
                # BEGIN IMMEDIATE ran outside the skip contract and the
                # docstring lied about locked/unreadable).
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error:
                    return None
            result = _prune_convention_meta_conn(
                conn, cutoff_epoch=cutoff_epoch,
                current_session=current_session, now_epoch=now_epoch,
                row_cap=row_cap, dry_run=dry_run)
            if not dry_run:
                conn.commit()
            return result
        except sqlite3.Error:
            if not dry_run:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        finally:
            # close() on a ReadOnlyHandle is idempotent and also removes
            # the arm-B staging directory (close-then-remove); a bare
            # conn.close() would leak it (readonly.py contract). The
            # maintenance lock releases only AFTER the close (AC3).
            if handle is not None:
                handle.close()
            elif conn is not None:
                conn.close()
    finally:
        _release_named_lock("maintenance", m_token)


def cmd_sweep(marker_dir: str | None = None,
              max_age_days: float | None = None,
              dry_run: bool = False,
              now_epoch: "float | None" = None) -> int:
    """Remove stale per-session cooldown sentinel files (issue #23).

    Idempotent, fail-open, bounded: sweeps only the (few) directories the
    capture/convention hooks can write markers into (or a single explicit
    --marker-dir), and within each only considers files whose name starts with a
    known sentinel prefix — everything else in those dirs (store, lock files,
    backups, unrelated dot-files) is strictly left alone. A marker older than
    `max_age_days` is garbage: it only ever gates a re-prompt within the session
    named in its filename.

    No advisory lock is needed for the FILE phases: listdir + per-file unlink
    is idempotent, so two sessions sweeping the same dir at once are safe by
    construction (any FileNotFoundError on unlink is caught as an OSError
    below). The meta phase (issue #128) is best-effort and fail-open: it
    opens the store directly (raw mode=rw with a 1s timeout, or the
    sidecar-free readonly opener for --dry-run) and skips — preserving the
    file-sweep result and exit 0 — whenever the store is absent, locked,
    unreadable, not a database, or newer than this client supports. Returns
    a process exit code; never raises on a missing dir or a permission error.
    """
    if max_age_days is None:
        max_age_days = SENTINEL_SWEEP_DAYS_DEFAULT
    # Reject non-finite or negative TTL up front (PRR-001): `float("nan")` and
    # `float("-inf")`/`-1` parse cleanly through `_env_float` and argparse
    # `type=float`, but a NaN cutoff makes `mtime >= cutoff` always False and a
    # negative cutoff lands in the future — either would prune EVERY sentinel,
    # including the live session's freshly-written marker. 0 is a valid
    # aggressive setting (prune everything older than now). Same finite-check
    # pattern used for `confidence` above.
    if not math.isfinite(max_age_days) or max_age_days < 0:
        print(f"[zmem] sweep: --max-age-days must be a finite, non-negative number "
              f"(got {max_age_days!r})", file=sys.stderr)
        return 2
    # Issue #128: one injected clock (now_epoch) supplies BOTH the file
    # cutoff and the meta-prune/upgrade timestamps; None uses the real clock.
    now = time.time() if now_epoch is None else float(now_epoch)
    cutoff = now - max_age_days * 86400
    dirs = [Path(marker_dir)] if marker_dir else _sweep_candidate_dirs()
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue  # fail-open: a locked/unreadable dir must never break session start
        for name in names:
            if not name.startswith(SENTINEL_PREFIXES):
                continue
            p = d / name
            try:
                if not p.is_file():
                    continue
                # Strict <, NOT <=: a marker just written by the live session has
                # mtime >= now > cutoff, so it is always kept. Loosening to <= risks
                # deleting the live session's marker on the boundary second. The
                # guarantee covers a marker that already exists when sweep stats it;
                # there is a microsecond TOCTOU window between this stat() and the
                # unlink() below where a hook could create a fresh marker that is then
                # unlinked — fail-open in practice (the hook re-creates on the next
                # failure), so no lock is taken, but the comment is not an airtight
                # concurrency proof.
                if p.stat().st_mtime >= cutoff:
                    continue
                # #93 A3: close the stat→unlink TOCTOU — a hook appending in
                # the window above would lose that event. Re-stat immediately
                # before the unlink and keep the file when it moved forward.
                if p.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue  # best-effort: unreadable/racy entry, skip
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    continue
            removed += 1
        # Issue #88 / #85 direction 2 + #90: the per-session query-context
        # sidecars under <data>/ops/ (rings .log, delivery markers
        # .delivered, pre-tool pending fences .pending) are session-scoped —
        # same max-age policy as the sentinels so finished sessions' files
        # cannot accumulate. Same strict-< mtime rule keeps the live
        # session's files (including an undelivered .pending, which only
        # survives as long as its session does). Issue #118: the compaction
        # stash .compact joins the family (a session that dies between
        # PostCompact and SessionStart leaves one behind). Issue #119: the
        # subagent task-text stash .tasktext joins the same family (a
        # delegation whose child never started is reaped on the same
        # max-age policy).
        ops_dir = d / "ops"
        if ops_dir.is_dir():
            try:
                ring_names = os.listdir(ops_dir)
            except OSError:
                ring_names = []
            for rname in ring_names:
                # Issue #252: per-call ledger segments (<hash>.ledger.seg.<hex>)
                # carry no constant suffix, so they need a containment match
                # alongside the endswith tuple.
                # Issue #284 (T-05): the reservation sidecar family
                # (resv-*.reserve) joins the sweep so an unacked/expired
                # reservation can never linger past the max-age policy.
                # Issue #315 (0.118.0): the feedback lock leaf joins too —
                # transient by lifecycle (released after each write), and a
                # killed writer's orphan is phantom-broken by liveness before
                # this age backstop ever matters.
                if (not rname.endswith((".log", ".delivered", ".pending", ".ledger", ".compact", ".tasktext", ".delivery-ended", ".capture-failure.json", ".signals", ".reserve", ".feedback.lock", ".wrote", ".closeout", ".nudged", ".clipped"))
                        and ".ledger.seg." not in rname
                        and ".ledger.tmp." not in rname
                        and ".reserve.tmp." not in rname
                        # issue #336 AC3: killed-write orphans of the
                        # tasktext stash; the age sweep reaps them like the
                        # ledger tmp shapes (purge also scrubs them).
                        and ".tasktext.tmp." not in rname):
                    continue
                rp = ops_dir / rname
                try:
                    if not rp.is_file() or rp.stat().st_mtime >= cutoff:
                        continue
                    # #93 A3: same re-stat backstop for the ops sidecars.
                    if rp.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                if not dry_run:
                    try:
                        rp.unlink()
                    except OSError:
                        continue
                removed += 1
        # Issue #123: incremental history mining writes one compact checkpoint
        # per session/transcript pair. They are session evidence, not durable
        # memory, so apply the same bounded-age policy. Only final `.json`
        # checkpoints are eligible; live `.lock` and `.tmp` files are never
        # touched by this pass.
        checkpoints_dir = d / "history-checkpoints"
        if checkpoints_dir.is_dir():
            try:
                checkpoint_names = os.listdir(checkpoints_dir)
            except OSError:
                checkpoint_names = []
            for cname in checkpoint_names:
                if not cname.endswith(".json"):
                    continue
                cp = checkpoints_dir / cname
                try:
                    if not cp.is_file() or cp.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                if not dry_run:
                    try:
                        cp.unlink()
                    except OSError:
                        continue
                removed += 1
    if removed:
        verb = "would prune" if dry_run else "pruned"
        print(f"[zmem] sweep: {verb} {removed} stale session "
              "sentinel(s)/sidecar(s)")
    # Issue #128: bound the per-session convention-counter meta rows on the
    # SAME cutoff. Best-effort and fail-open by contract: an absent, locked,
    # unreadable, non-database, or newer-schema store skips the meta phase
    # entirely and preserves the successful file-sweep result above. The
    # current session rides the AMBIENT ZMEM_SESSION (exported by the
    # session launcher for cadence runs), so the live launcher session's
    # rows always survive. NOTE (review round PRR-003): the hermes ring
    # writer keys its rows by the hermes payload session_id — a different
    # id space that never matches ZMEM_SESSION — so hermes-family rows are
    # bounded purely by the last_seen TTL (fresh JSON rows survive; stale
    # ones are pruned), and a pre-0.144 legacy hermes row is removed at its
    # first post-upgrade sweep rather than upgraded.
    global last_meta_prune
    last_meta_prune = None
    try:
        _counts = prune_convention_meta(
            STORE_PATH, cutoff_epoch=cutoff,
            current_session=os.environ.get("ZMEM_SESSION", ""),
            dry_run=dry_run, now_epoch=now)
    except Exception:
        _counts = None
    last_meta_prune = _counts
    if _counts is not None:
        verb = "would prune —" if dry_run else "pruned;"
        print(f"[zmem] sweep: {verb} meta convention rows "
              f"removed={_counts[0]} retained={_counts[1]} "
              f"unparseable={_counts[2]}", file=sys.stderr)
    return 0

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
    # Issue #345 AC1 (unconditional): the old bare ``?mode=ro`` open
    # materialized ``-wal``/``-shm`` beside the store and relied on the
    # remove-what-we-created sweep. Route through the sidecar-free helper:
    # a quiesced store takes the immutable arm, a residue store a private
    # snapshot copy — the store directory is never touched. The handle
    # proxies the connection, so the integrity_check call is unchanged.
    try:
        from .readonly import open_readonly  # noqa: PLC0415
    except ImportError:  # standalone probe loads (storelib dir on path)
        from readonly import open_readonly  # noqa: PLC0415

    handle = open_readonly(path)
    try:
        return handle.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        handle.close()

def _report_surviving_work_dir(work_dir: Path | None) -> bool:
    """Report a work directory that survived its cleanup.

    The restore's private working copy holds PLAINTEXT store and WAL
    bytes. Its removal is best-effort (`ignore_errors=True`), so a
    locked file can leave it behind -- and a directory nobody is told
    about is a directory nobody removes.

    The survivor is recorded DURABLY FIRST (a marker file inside the
    surviving directory, next to the bytes it holds) and only then
    printed. Neither step swallows an exception: if the record cannot
    be written, or the warning cannot be printed, the failure
    PROPAGATES -- a restore that cannot report a surviving plaintext
    copy must not report success.

    Returns True when the directory survived.
    """
    if work_dir is None:
        return False
    try:
        survived = work_dir.is_dir()
    except OSError:
        survived = False
    if not survived:
        return False
    record = work_dir / "RESTORE-LEFTOVER.txt"
    # The two mechanisms cover each other: the durable record is the
    # fallback for the warning, and the warning is the fallback for the
    # record. Only if BOTH fail is the survivor genuinely unknowable --
    # and that must not pass silently.
    record_error = None
    try:
        record.write_text(
            "The restore\'s private working copy SURVIVED its cleanup.\n"
            "This directory holds PLAINTEXT store and WAL bytes.\n"
            "Remove it by hand once you have read what you need from it.\n",
            encoding="utf-8")
    except Exception as exc:
        record_error = exc
    warning_error = None
    try:
        print(f"[zmem] restore: WARNING - the private working copy "
              f"{_esc(work_dir)} SURVIVED its cleanup and still holds "
              f"plaintext store and WAL bytes; remove it by hand "
              f"(the record is in {_esc(record)})",
              file=sys.stderr)
    except Exception as exc:
        warning_error = exc
    if record_error is not None and warning_error is not None:
        raise RuntimeError(
            "a private working copy (%s) survived its cleanup and NEITHER "
            "the durable record nor the warning could be written; the "
            "copy holds plaintext store and WAL bytes and must be removed "
            "by hand" % work_dir) from record_error
    return True


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
    # Issue #504 step 1 (supplement Item D): both stamp reads go through the
    # sidecar-free reader — the bare mode=ro open CREATES a 0-byte -wal and a
    # 32 KB -shm on a perfectly quiesced store, so every ordinary restore
    # would find "sidecars present" and make a forensic directory for files
    # it created itself. RuntimeError (non-integer stamp, _schema_compat)
    # still refuses with exit 2, exactly as before.
    from storelib.readonly import open_readonly as _ro_open, ReadOnlyStoreError as _ROErr

    def _stamp_sidecar_free(path: Path):
        try:
            if not path.exists() or path.stat().st_size == 0:
                return None
            h = _ro_open(path)
            try:
                row = h.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            finally:
                h.close()
            if not row:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                # Fail closed exactly like _read_schema_version: a
                # non-integer stamp refuses with the same RuntimeError (exit 2).
                raise RuntimeError(
                    f"zmem: store {_esc(path)} has a non-integer "
                    f"schema_version {_esc(repr(row[0]))}; refusing to modify it")
        except (FileNotFoundError, _ROErr, sqlite3.Error, OSError):
            return None

    try:
        v = _stamp_sidecar_free(dest)
        if v is None and dest.exists() and dest.stat().st_size > 0:
            _read_schema_version(dest)   # fail-closed on unreadable-but-present
        v2 = _stamp_sidecar_free(Path(from_path).expanduser())
        if v2 is None:
            _read_schema_version(Path(from_path).expanduser())
        # The stamp-policy half of the old preflight (issue #504 step 1):
        # _read_schema_version also ran _schema_compat on every readable
        # stamp — the newer-destination refusal (exit 2, destination
        # untouched) lives there. The sidecar-free read must run the SAME
        # gate on the stamps it read.
        from storelib.schema import _schema_compat as _compat
        if v is not None:
            _compat(v, dest)
        if v2 is not None:
            _compat(v2, Path(from_path).expanduser())
    except RuntimeError as e:
        print(f"[zmem] restore FAILED: {_esc(e)}", file=sys.stderr)
        return 2
    if _host is not None:
        try:
            _host.assert_local_fs(dest.parent)
        except ValueError as e:
            print(f"[zmem] restore FAILED: {_esc(e)}", file=sys.stderr)
            return 1

    m_token = _acquire_maintenance_lock_with_liveness()
    if m_token is None:
        print("[zmem] restore REFUSED: another maintenance operation is currently "
              "running - destination untouched; re-run when it finishes",
              file=sys.stderr)
        return 2
    with contextlib.ExitStack() as locks:
        locks.callback(_release_named_lock, "maintenance", m_token)
        # Admitted CLI openers may still need schema to finish. Drain
        # their connection leases before taking schema.
        if _restore_has_active_writers():
            return 2
        if not _wait_for_open_connections():
            print("[zmem] restore REFUSED: a store connection is active - "
                  "destination untouched; re-run when it finishes", file=sys.stderr)
            return 2
        s_token = _acquire_schema_lock_with_liveness(
            wait_seconds=SCHEMA_LOCK_WAIT_SECONDS,
            poll_seconds=SCHEMA_LOCK_POLL_SECONDS,
        )
        if s_token is None:
            print("[zmem] restore REFUSED: store initialization or migration is "
                  "currently running - destination untouched; re-run when it finishes",
                  file=sys.stderr)
            return 2
        locks.callback(_release_named_lock, "schema", s_token)

        b_token = _acquire_lock("backup", BACKUP_LOCK_STALE_SECONDS)
        if b_token is None:
            print("[zmem] restore REFUSED: a backup is currently running - "
                  "destination untouched; re-run when it finishes", file=sys.stderr)
            return 2
        locks.callback(_release_lock, "backup", b_token)
        c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
        if c_token is None:
            print("[zmem] restore REFUSED: a consolidation is currently running - "
                  "destination untouched; re-run when it finishes", file=sys.stderr)
            return 2
        locks.callback(_release_lock, "consolidate", c_token)
        if _restore_has_active_writers():
            return 2
        return _restore_locked(from_path=from_path, force=force, out_dir=out_dir)


def _restore_has_active_writers() -> bool:
    live_writers = _cleanup_stale_writer_leases()
    if not live_writers:
        return False
    print("[zmem] restore REFUSED: a normal writer is currently active - "
          "destination untouched; re-run when it finishes", file=sys.stderr)
    for lease in live_writers[:5]:
        # F-002: the lease name is a FILENAME from the writer's lease
        # directory, so it is externally observable -- and Windows forbids
        # control characters in a file name, but this code also runs on
        # POSIX, where it does not.
        print(f"[zmem]   active writer lease: {_esc(lease.name)}",
              file=sys.stderr)
    return True

# ---------------------------------------------------------------------------
# Issue #504 step 3(e): the semantic REQUIRED SHAPE proof (inserted from
# the trace's repro/shape-helpers-impl.py; see the plan for the rulings
# behind every rule below).
# ---------------------------------------------------------------------------
# ASCII-only, per SQLite's tokenizer. Any character not matched here and not
# inside a string literal / quoted identifier / comment REFUSES.
_SHAPE_TOKEN = re.compile(
    r"[ \t\n\f\r]+"
    r"|--[^\n]*|/\*.*?\*/"
    r"|'(?:[^']|'')*'"
    r"|\"(?:[^\"]|\"\")*\""
    r"|\[[^\]]*\]"
    r"|`(?:[^`]|``)*`"   # a doubled backtick is an ESCAPED backtick,
    #                        so the identifier ends at the backtick that
    #                        is NOT doubled -- SQLite's own rule
    r"|[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9]+"
    r"|[A-Za-z_][A-Za-z0-9_$]*"
    r"|\|\||->>|<<|>>|<=|>=|==|!=|<>|[(),.;+\-*/%<>=~&|:\[\]]",
    re.S,
)

_FTS_TRIGGERS = ("memory_ai", "memory_ad", "memory_au")


def _esc(text: str) -> str:
    """F-007 (swarm-pr-review MU-001): escape snapshot-derived text before
    it reaches the operator's terminal. Restore refusal lines echo
    attacker-controlled sqlite_master identifiers and SQLite error text; a
    crafted snapshot name carrying OSC 0/OSC 52 (clipboard-write) or CSI
    sequences must not execute in the ruled output. When any
    non-printable character is present the whole text is rendered in its
    escaped form (\\x1b visible); clean text passes through unchanged so
    ordinary names stay greppable.
    """
    text = str(text)
    if all(ch.isprintable() for ch in text):
        return text
    return text.encode("unicode_escape").decode("ascii")


def _ascii_lower(text: str) -> str:
    """SQLite's identifier case folding: ASCII A-Z only.

    Python's str.lower()/casefold() map the Kelvin sign (U+212A) and other
    non-ASCII capitals to ASCII look-alikes; SQLite's identifier folding
    does not, so `"Kind"` (Kelvin) is a DIFFERENT identifier than `kind`
    (the round-5 Kelvin CHECK impostor relied on exactly this confusion).
    """
    return "".join(
        chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


class ShapeError(Exception):
    """The staged copy's shape is not what the running client needs."""


def _shape_tokens(sql):
    """SQL token stream with SQLite's ASCII-only semantics, fail-closed.

    Comments and whitespace are dropped; bare words fold case with SQLite's
    ASCII-only rule (_ascii_lower, NOT str.lower()/casefold(): Python equates
    the Kelvin sign K with k, SQLite does not); quoted identifiers fold the
    same way after unquoting (a DOUBLED backtick is an escaped backtick, so
    `` `a``b` `` is the ONE identifier `a`b`); numbers keep SQLite's own
    kind -- INTEGER or REAL -- and their EXACT value; any character the
    scanner does not consume raises ShapeError (an unconsumed non-ASCII byte
    is an identifier character to SQLite and must not slip through as
    nothing).
    """
    out, i = [], 0
    while i < len(sql):
        m = _SHAPE_TOKEN.match(sql, i)
        if not m:
            raise ShapeError(
                "unconsumed %r at %d (SQLite treats non-ASCII characters as "
                "identifier characters; the definition is not what it looks "
                "like)" % (sql[i], i))
        i = m.end()
        text = m.group()
        if text[0] in " \t\n\f\r" or text.startswith("--") or text.startswith("/*"):
            continue
        if text[0] == "'":
            out.append(("s", text))
        elif text[0] in "\"[`":
            inner = text[1:-1].replace(text[0] * 2, text[0])
            out.append(("w", _ascii_lower(inner)))
        elif text[0].isdigit() or text[0] == ".":
            # SQLite's own rule: a numeric literal with a point or an
            # exponent is a REAL, otherwise it is an INTEGER, and the two
            # kinds are DIFFERENT values to SQLite (`DEFAULT 0` stores
            # integer 0, `DEFAULT 0.0` stores real 0.0) -- so the kind is
            # part of the shape. The INTEGER value is EXACT: a float
            # would make 9007199254740992 and 9007199254740993 the same
            # number (they differ by one ULP), and the proof would then
            # accept a store whose required default is a different value.
            # The proof's tolerance is kept: numbers normalize BY VALUE,
            # so `007` and `7` still compare equal.
            if "." in text or "e" in text or "E" in text:
                try:
                    out.append(("n", "r:%r" % float(text)))
                except ValueError:
                    out.append(("n", text))
            else:
                value = int(text)
                if -(2 ** 63) <= value < 2 ** 63:
                    out.append(("n", "i:%d" % value))
                else:
                    # SQLite's own rule: an integer literal that does NOT
                    # fit in its signed 64-bit range is stored as a REAL,
                    # so tagging it an integer would make the shape proof
                    # compare a REAL against an integer token and refuse
                    # a legitimate store.
                    out.append(("n", "r:%r" % float(text)))
        elif text[0].isalpha() or text[0] == "_":
            out.append(("w", _ascii_lower(text)))
        else:
            out.append(("o", text))
    return out


def _shape_split_elements(tokens):
    """Split a CREATE TABLE token stream at top-level commas.

    Returns (column_elements, constraint_elements, options). The head (the
    CREATE TABLE name part) and anything after the closing paren (the
    WITHOUT ROWID / STRICT options) are handled here.
    """
    depth = 0
    opened = False
    parts, current = [], []
    tail = []
    in_tail = False
    for tok in tokens:
        if in_tail:
            tail.append(tok)
            continue
        if tok == ("o", "(") and not opened:
            opened, depth = True, 1
            continue
        if not opened:
            continue  # the head (CREATE TABLE name)
        if tok == ("o", "("):
            depth += 1
        elif tok == ("o", ")"):
            depth -= 1
            if depth == 0:
                parts.append(current)
                current = []
                in_tail = True
                continue
        elif tok == ("o", ",") and depth == 1:
            parts.append(current)
            current = []
            continue
        current.append(tok)
    columns, constraints = [], []
    for part in parts:
        if not part:
            continue
        head = part[0][1] if part[0][0] == "w" else ""
        if head in ("constraint", "primary", "unique", "check", "foreign"):
            constraints.append(tuple(part))
        else:
            columns.append(tuple(part))
    return columns, constraints, tuple(tail)


def _vtab_decl(sql):
    return re.sub(r"float\[\d+\]", "float[dim]",
                  re.sub(r"\s+", " ", sql or "").strip().lower())


def _sqlite_column_names(staged, table):
    """SQLite's own column-name list for `table` in the staged copy.

    A single-quoted column KEY ('signal' TEXT) resolves to the identifier
    signal — SQLite's semantics, not the scanner's (round 5 Important).
    """
    conn = sqlite3.connect(str(staged))
    try:
        return {row[1] for row in conn.execute(
            'PRAGMA table_xinfo("%s")' % table.replace('"', '""'))}
    finally:
        conn.close()


def _probe_word_for(content, tokenize):
    """The first eligible search term of `content`, via SQLite's tokenizer.

    A private :memory: fts5 table t (same tokenize argument as the fresh
    memory_fts declaration) holds ONLY this row's content (the table is
    DELETED first — an accumulating vocabulary can return an earlier row's
    term, round 4 Critical 1); the first term of 4+ characters by offset,
    read through fts5vocab, is the probe word. None when no term qualifies.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize=%s)" % tokenize)
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        conn.execute("DELETE FROM t")
        conn.execute("INSERT INTO t(x) VALUES (?)", (content,))
        row = conn.execute(
            "SELECT term FROM v WHERE length(term) >= 4 ORDER BY offset LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def prove_required_shape(staged, fresh):
    """The step 3(e) proof. Raises ShapeError with the precise reason.

    `fresh` is a store built by the same code in a temporary directory; the
    staged copy is compared with it object by object. A wrong definition
    refuses; a damaged search index is healed by ONE rebuild and re-proven.
    Returns {"rebuilt_index": bool, "probe_words": [...]}.
    """
    from schema_meta import SUPPORTED_SCHEMA_VERSION
    from storelib import schema as _schema

    def describe(path):
        conn = sqlite3.connect(str(path))
        try:
            try:
                _schema._load_vec(conn)
            except Exception:
                pass
            objs, virtual, tables = {}, set(), {}
            for typ, name, sql in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master"):
                objs[(typ, name)] = sql or ""
                if typ == "table":
                    if (sql or "").strip().lower().startswith("create virtual table"):
                        virtual.add(name)
                    else:
                        tables[name] = sql or ""
            stamp_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            stamp = 0
            if stamp_row:
                try:
                    stamp = int(stamp_row[0])
                except (TypeError, ValueError):
                    stamp = 0
            rowids = [r[0] for r in conn.execute(
                "SELECT rowid FROM memory ORDER BY rowid")]
            docs = {r[0] for r in conn.execute("SELECT id FROM memory_fts_docsize")}
        finally:
            conn.close()
        fts_decl = objs.get(("table", "memory_fts"), "")
        m = re.search(r"tokenize='([^']+)'", fts_decl)
        tokenize = m.group(1) if m else "unicode61"
        return dict(objs=objs, virtual=virtual, tables=tables, stamp=stamp,
                    rowids=rowids, docs=docs, tokenize=tokenize)

    s, f = describe(staged), describe(fresh)
    why = []

    # 1. every fresh object exists by (type, name), autoindexes included.
    for key in sorted(f["objs"]):
        if key not in s["objs"]:
            if key[1].startswith("sqlite_autoindex_"):
                why.append("missing %s %s (the table's PRIMARY KEY or UNIQUE "
                           "constraint is gone)" % key)
            else:
                why.append("missing %s %s" % key)

    newer = s["stamp"] > SUPPORTED_SCHEMA_VERSION

    # 2. every ordinary table element by element.
    for table in sorted(f["tables"]):
        if table not in s["tables"]:
            why.append("table %s is missing" % table)
            continue
        try:
            f_cols, f_cons, f_opts = _shape_split_elements(
                _shape_tokens(f["tables"][table]))
            s_cols, s_cons, s_opts = _shape_split_elements(
                _shape_tokens(s["tables"][table]))
        except ShapeError as exc:
            why.append("table %s: %s" % (table, exc))
            continue
        f_by_name = {}
        for elem in f_cols:
            if not elem or elem[0][0] != "w":
                why.append("table %s: unrecognizable column element" % table)
                continue
            f_by_name[elem[0][1]] = elem
        # The table_xinfo cross-check: column KEYS are never trusted to the
        # token stream alone (an ASCII tokenizer cannot see tainté / ſignal).
        s_xinfo = _sqlite_column_names(staged, table)
        s_by_name = {}
        for elem in s_cols:
            if not elem:
                continue
            key = elem[0][1] if elem[0][0] == "w" else None
            if key is None and elem[0][0] == "s":
                # single-quoted column KEY: SQLite resolves it to the
                # identifier; find the xinfo name it belongs to.
                inner = elem[0][1][1:-1].replace("''", "'")
                key = inner if inner in s_xinfo else elem[0][1]
            if key is None:
                why.append("table %s: unrecognizable column element" % table)
                continue
            s_by_name[key] = elem
        for name in sorted(f_by_name):
            if name not in s_xinfo:
                why.append("table %s: required column %s is missing "
                           "(table_xinfo)" % (table, name))
                continue
            if not newer and name in s_by_name:
                # Compare with the column KEY canonicalized to its name, so
                # an equivalent spelling (a single-quoted key, which SQLite
                # resolves to the identifier) is not a difference — while
                # the REST of the element (type, NOT NULL, DEFAULT, ...)
                # must match token for token.
                s_elem = (("w", name),) + tuple(s_by_name[name][1:])
                f_elem = (("w", name),) + tuple(f_by_name[name][1:])
                if s_elem != f_elem:
                    why.append("table %s: column %s is defined differently" % (table, name))
        if not newer:
            for name in sorted(s_xinfo):
                if name not in f_by_name:
                    why.append("table %s: extra column %s (stamp %d is at or "
                               "below SUPPORTED)" % (table, name, s["stamp"]))
            if sorted(f_cons) != sorted(s_cons):
                why.append("table %s: the table constraints differ" % table)
            if f_opts != s_opts:
                why.append("table %s: the table options differ" % table)

    # 3. the two virtual tables by declaration (dimension free).
    fresh_vec = ("table", "memory_vec") in f["objs"]
    staged_vec = ("table", "memory_vec") in s["objs"]
    if fresh_vec and staged_vec:
        if _vtab_decl(s["objs"][("table", "memory_vec")]) != _vtab_decl(
                f["objs"][("table", "memory_vec")]):
            why.append("virtual table memory_vec: declaration differs")
    # memory_vec is compared only when the FRESH store has it (a client
    # without sqlite-vec must not refuse every normal backup).
    if ("table", "memory_fts") not in s["objs"]:
        why.append("virtual table memory_fts is missing")
    elif _vtab_decl(s["objs"][("table", "memory_fts")]) != _vtab_decl(
            f["objs"][("table", "memory_fts")]):
        why.append("virtual table memory_fts: declaration differs")

    # 4. indexes and triggers token-equal; the three search triggers always.
    for key in sorted(f["objs"]):
        if key[0] not in ("index", "trigger") or key not in s["objs"]:
            continue
        if newer and key[1] not in _FTS_TRIGGERS:
            continue
        try:
            if _shape_tokens(f["objs"][key]) != _shape_tokens(s["objs"][key]):
                why.append("%s %s: the SQL differs" % key)
        except ShapeError as exc:
            why.append("%s %s: %s" % (key[0], key[1], exc))

    # 5. extra indexes/triggers refuse at or below SUPPORTED.
    if not newer:
        for key in sorted(s["objs"]):
            if key[0] in ("index", "trigger") and key not in f["objs"]:
                if key[1].startswith("sqlite_autoindex_"):
                    continue  # extra autoindex: a stronger table definition
                why.append("extra %s %s (stamp %d is at or below SUPPORTED)"
                           % (key[0], key[1], s["stamp"]))

    if why:
        raise ShapeError("; ".join(why[:6]) + ("; ..." if len(why) > 6 else ""))

    # 6. the search index: heal derived data, prove it complete and usable.
    rebuilt = False
    probe_words = []
    equal = s["docs"] == set(s["rowids"])
    if equal:
        conn = sqlite3.connect(str(staged))
        try:
            sample = (s["rowids"] if len(s["rowids"]) <= 256
                      else s["rowids"][:128] + s["rowids"][-128:])
            stale = False
            for rowid in sample:
                row = conn.execute(
                    "SELECT content FROM memory WHERE rowid=?", (rowid,)).fetchone()
                word = _probe_word_for(row[0] if row else "", s["tokenize"])
                if not word:
                    continue
                probe_words.append(word)
                hits = {r[0] for r in conn.execute(
                    "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
                    ('"%s"' % word.replace('"', '""'),))}
                if rowid not in hits:
                    stale = True
                    break
        finally:
            conn.close()
        equal = not stale
    if not equal:
        conn = sqlite3.connect(str(staged))
        try:
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            conn.commit()
        finally:
            conn.close()
        rebuilt = True
        conn = sqlite3.connect(str(staged))
        try:
            docs2 = {r[0] for r in conn.execute("SELECT id FROM memory_fts_docsize")}
        finally:
            conn.close()
        if docs2 != set(s["rowids"]):
            raise ShapeError("search index incomplete after rebuild "
                             "(%d memory rowids, %d indexed)" % (
                                 len(s["rowids"]), len(docs2)))
    # The rolled-back round trip through the triggers.
    conn = sqlite3.connect(str(staged))
    conn.isolation_level = None
    try:
        token = "rtprobe" + uuid.uuid4().hex[:10]
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO memory (id, namespace, type, content, ingestion_ts) "
                "VALUES (?, 'project:shape-probe', 'fact', ?, "
                "'2026-11-25T00:00:00Z')", (token, "roundtrip " + token))
            rowid = cur.lastrowid
            found = {r[0] for r in conn.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?",
                ('"%s"' % token,))}
            if found != {rowid}:
                raise ShapeError(
                    "an inserted row is not indexed by the search triggers "
                    "(found %r, expected %r)" % (sorted(found), [rowid]))
            conn.execute("DELETE FROM memory WHERE id=?", (token,))
            again = conn.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?",
                ('"%s"' % token,)).fetchone()[0]
            if again:
                raise ShapeError("a deleted row left %d index entries" % again)
        finally:
            conn.rollback()
    except sqlite3.Error as exc:
        raise ShapeError("round trip through the triggers failed: %s" % exc)
    finally:
        conn.close()
    return {"rebuilt_index": rebuilt, "probe_words": probe_words[:5]}




def _finish_failed_restore(*, dest, dest_exists, dest_nonempty,
                           prerestore_path, stash_dir, forensic_dir,
                           sidecars) -> int:
    """Issue #504 step 9: roll back a restore that failed AFTER the swap.

    Runs for ANY error after the swap (an exception in the success path, a
    non-ok integrity result, or an exception raised by the swap call itself
    once the staged file is gone — the replacement happened). The stashed
    sidecars are never put back beside the re-installed file; the safety copy
    is never consumed. Returns the process exit code (1).
    """
    if prerestore_path is not None:
        try:
            for p in sidecars:
                if p.exists():
                    p.unlink()
            # F-017/F-020 (swarm-pr-review MS-001/MA-003): stage the rollback
            # copy at a mkstemp name (never the predictable
            # .restore-rollback- name a local actor could pre-plant), unlink
            # it on any failure, and harden the re-installed store BEFORE
            # the verification that may raise (a rollback whose verification
            # fails must not leave the live store at umask-default mode).
            fd_rb, rb_str = tempfile.mkstemp(
                prefix=dest.name + ".restore-rollback-",
                dir=str(dest.parent))
            os.close(fd_rb)
            stage_back = Path(rb_str)
            try:
                shutil.copyfile(prerestore_path, stage_back)
                os.replace(stage_back, dest)
            except BaseException:
                if stage_back.exists():
                    stage_back.unlink()
                raise
            if _host is not None:
                try:
                    _host.set_owner_only_perms(dest)
                except OSError:
                    pass  # hardening is best-effort on the rollback path
            conn = sqlite3.connect(str(dest))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                rb_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
            if rb_integrity != "ok":
                raise RuntimeError(
                    f"re-installed pre-restore backup failed integrity_check: "
                    f"{rb_integrity}")
            print(f"[zmem] restore: rolled back: live store re-installed from the "
                  f"pre-restore backup {_esc(prerestore_path)}", file=sys.stderr)
        except Exception as rb:
            print(f"[zmem] restore FAILED: automatic rollback also failed "
                  f"({_esc(rb)}); the live store is NOT trustworthy",
                  file=sys.stderr)
            # F-002 (swarm-pr-review round 2, Stage-B review round 3):
            # this line is a COPY-PASTE COMMAND, so the path must stay
            # byte-exact -- escaping it here would print a path that does
            # not exist and make the recovery command unusable. The path is
            # shell-quoted, so it cannot inject a shell command either; and
            # it is zmem's OWN backup file inside the operator's own backup
            # directory, not an attacker-supplied snapshot value (every
            # snapshot-derived string on this path IS escaped).
            print(f"[zmem] restore: roll back by hand with "
                  f"{command_prefix()} restore --from "
                  f"{shlex.quote(str(prerestore_path))} --force",
                  file=sys.stderr)
            if forensic_dir is not None:
                print(f"[zmem] restore: the files that were at the destination "
                      f"before the restore are kept byte-for-byte in "
                      f"{_esc(forensic_dir)}; do not copy them back beside the store "
                      f"(inspect them only from a copy outside the store "
                      f"directory)", file=sys.stderr)
            return 1
    else:
        try:
            # Missing or empty destination before the restore: the rollback
            # target is that state. The INSTALLED file (and any sidecars the
            # failed install created) go, whatever dest_exists was.
            if dest.exists():
                dest.unlink()
            for p in sidecars:
                if p.exists():
                    p.unlink()
            if dest_exists and not dest_nonempty:
                dest.touch()
                # F-005 (round-1 review comment): the re-created EMPTY
                # destination is a file this restore writes, so it is
                # hardened like every other one (best-effort: a filesystem
                # that cannot harden must not break the rollback).
                if _host is not None:
                    try:
                        _host.set_owner_only_perms(dest)
                    except Exception:
                        pass
            # The rolled-back target state is destination-SPECIFIC: a
            # destination that was missing must be missing again; one that
            # was an empty file must exist and be 0 bytes again (the touch
            # above recreated it). Checking bare existence would condemn
            # every correctly-rolled-back empty destination (4.5 round-2
            # finding F1: the ruled success line never printed).
            for p in sidecars:
                if p.exists():
                    raise RuntimeError(f"{p.name} survived the rollback")
            if dest.exists() != (dest_exists and not dest_nonempty):
                raise RuntimeError(f"{dest.name} survived the rollback")
            if dest.exists() and dest.stat().st_size != 0:
                raise RuntimeError(f"{dest.name} is not empty again")
            print("[zmem] restore: rolled back: destination was "
                  "missing/empty before the restore and is again", file=sys.stderr)
        except Exception as rb:
            print(f"[zmem] restore FAILED: automatic rollback also failed "
                  f"({_esc(rb)}); the live store is NOT trustworthy",
                  file=sys.stderr)
            if not dest_exists:
                print(f"[zmem] restore: roll back by hand: delete {_esc(dest)} and any "
                      f"{_esc(dest)}-wal, {_esc(dest)}-shm, {_esc(dest)}-journal beside it "
                      f"(the destination store did not exist before the restore)",
                      file=sys.stderr)
            else:
                print(f"[zmem] restore: roll back by hand: delete any {_esc(dest)}-wal, "
                      f"{_esc(dest)}-shm, {_esc(dest)}-journal beside it, then replace "
                      f"{_esc(dest)} with an empty 0-byte file (the destination store "
                      f"was an empty file before the restore)", file=sys.stderr)
            if forensic_dir is not None:
                print(f"[zmem] restore: the files that were at the destination "
                      f"before the restore are kept byte-for-byte in "
                      f"{_esc(forensic_dir)}; do not copy them back beside the store "
                      f"(inspect them only from a copy outside the store "
                      f"directory)", file=sys.stderr)
            return 1
    # Remove the stash (ruling step 6: "Remove the stash"). The stashed
    # sidecars are never put back beside the re-installed file — their bytes
    # are in the forensic directory. A removal failure is reported, not
    # silent: the restore still failed safely, so the exit code stays 1.
    if stash_dir is not None and stash_dir.is_dir():
        shutil.rmtree(stash_dir, ignore_errors=True)
        if stash_dir.is_dir():
            for leftover in sorted(p.name for p in stash_dir.iterdir()):
                print(f"[zmem] restore: {_esc(leftover)} is set aside at "
                      f"{_esc(stash_dir)}", file=sys.stderr)
    return 1


def _restore_locked(*, from_path: str, force: bool = False, out_dir: str | None = None) -> int:
    """The restore body under the ruled order (issue #504, ASK-1 policy B).

    Steps (the plan's `### #504`):
      1. verify the snapshot (unchanged);
      2. stage and prove the replacement on a PRIVATE copy — integrity,
         identity, migrate via schema.connect_path, the REQUIRED SHAPE
         proof; any failure refuses with the destination untouched;
      3. rotate the incarnation on the staged copy;
      4. forensic copy of the destination's file set (whenever any sidecar
         is present);
      5. safety copy from a PRIVATE working copy (only when the destination
         is non-empty);
      6. set sidecars aside by rename (reverse-order put-back on failure,
         continuing past a failed put-back);
      7. swap;
      8. the success path (today's sequence: WAL, checkpoint, integrity,
         row counts; owner permissions; the migration connection; the stash
         removed);
      9. ANY error after the swap rolls back (per destination state);
     10. the ruled operator output.

    Restore never refuses because of a sidecar, and the destination is never
    opened through SQLite in place before the swap.
    """
    from storelib.generation import rotate_incarnation_on_staging
    from storelib import schema as _schema
    from schema_meta import SCHEMA_VERSION_KEY, SUPPORTED_SCHEMA_VERSION
    from storelib.readonly import open_readonly, ReadOnlyStoreError

    snap = Path(from_path).expanduser()
    dest = STORE_PATH

    if not snap.is_file():
        print(f"[zmem] restore FAILED: snapshot not found: {_esc(snap)}",
              file=sys.stderr)
        return 1

    # --- 1. Verify the snapshot BEFORE touching the destination at all. ---
    try:
        snap_integrity = _integrity_check_readonly(snap)
    except Exception as e:
        print(f"[zmem] restore FAILED: cannot read snapshot {_esc(snap)} "
              f"read-only: {_esc(e)}",
              file=sys.stderr)
        print("[zmem] restore: (a snapshot with a hot -wal sidecar must be "
              "checkpointed before it can be restored)", file=sys.stderr)
        return 1
    if snap_integrity != "ok":
        print(f"[zmem] restore FAILED: snapshot integrity_check={_esc(snap_integrity)} "
              f"- destination untouched", file=sys.stderr)
        return 1
    print(f"[zmem] restore: source snapshot {_esc(snap)}")
    print(f"[zmem] restore: snapshot integrity_check={_esc(snap_integrity)}")

    dest_exists = dest.exists()
    dest_nonempty = dest_exists and dest.stat().st_size > 0
    if dest_exists and not force:
        print(f"[zmem] restore FAILED: destination store already exists: "
              f"{_esc(dest)}\n"
              f"       pass --force to overwrite it (a pre-restore backup of the "
              f"current store is taken automatically).", file=sys.stderr)
        return 1

    # --- 2. Stage and PROVE the replacement (destination untouched). ---
    sidecars = [Path(str(dest) + sfx) for sfx in SIDECAR_SUFFIXES]
    present = [p for p in sidecars if p.exists()]
    stash_dir: Path | None = None
    staged: Path | None = None
    forensic_dir: Path | None = None
    prerestore_path: Path | None = None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # F-004 (round-1 review comment) + the closeout reviewer's round 24:
        # the staged copy is CREATED inside the guarded block as well as
        # copied there, so an OSError in the creation (an unwritable
        # directory, a failed descriptor close) takes the same controlled
        # path as a failed validation -- a ruled message and the staged
        # file removed -- instead of escaping as a raw traceback and
        # leaving a .restore-stage-* file behind UNREPORTED.
        fd_stage, stage_str = tempfile.mkstemp(
            prefix=dest.name + ".restore-stage-", dir=str(dest.parent))
        staged = Path(stage_str)
        # a FAILED close is a real failure: it takes the same controlled
        # path as every other failure in this block (the guard's cleanup
        # removes the staged file and the ruled message is printed). A
        # close that failed means the descriptor was already closed, so
        # the file CAN be removed.
        os.close(fd_stage)
        shutil.copy2(snap, staged)
        # A hand-supplied snapshot may carry its own sidecars: recover them on
        # the STAGED copy, never beside the live file.
        for sfx in ("-wal", "-shm", "-journal"):
            src_sib = Path(str(snap) + sfx)
            if src_sib.exists():
                shutil.copy2(src_sib, Path(str(staged) + sfx))
                print(f"[zmem] restore: recovered snapshot {_esc(src_sib.name)} "
                      f"onto the staged copy")
        conn = sqlite3.connect(str(staged))
        try:
            staged_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if staged_integrity != "ok":
            raise RuntimeError(f"integrity_check={staged_integrity}")
        # identity: tables memory and meta; the 14 v1 columns; a positive stamp.
        conn = sqlite3.connect(str(staged))
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "memory" not in names or "meta" not in names:
                raise RuntimeError("not a zmem store (no memory/meta tables)")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
            v1 = ("id", "namespace", "type", "content", "tags", "source_ref",
                  "source_hash", "confidence", "signal", "valid_from",
                  "superseded_at", "ingestion_ts", "retrieval_count",
                  "last_retrieved")
            missing = [c for c in v1 if c not in cols]
            if missing:
                raise RuntimeError(
                    "memory lacks the v1 columns: %s" % ", ".join(missing))
            stamp_row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_VERSION_KEY,)).fetchone()
            if not stamp_row:
                raise RuntimeError("no schema_version row")
            staged_stamp_before = int(stamp_row[0])
            if staged_stamp_before <= 0:
                raise RuntimeError(f"schema_version={staged_stamp_before}")
        finally:
            conn.close()
        # bring the staged copy to the running schema, then prove its shape
        mconn = _schema.connect_path(staged)
        try:
            _schema.init_db(mconn)
            _schema.migrate(mconn)
            staged_stamp = mconn.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_VERSION_KEY,)).fetchone()
            staged_stamp = int(staged_stamp[0]) if staged_stamp else 0
            staged_counts = _row_counts(mconn)
        finally:
            mconn.close()
        fresh_dir = tempfile.mkdtemp(prefix="zmem-restore-fresh-")
        fresh_path = Path(fresh_dir) / "fresh.sqlite"
        try:
            fconn = _schema.connect_path(fresh_path)
            try:
                _schema.init_db(fconn)
                _schema.migrate(fconn)
            finally:
                fconn.close()
            shape_info = prove_required_shape(staged, fresh_path)
        finally:
            shutil.rmtree(fresh_dir, ignore_errors=True)
        for sfx in SIDECAR_SUFFIXES:
            p = Path(str(staged) + sfx)
            if p.exists():
                p.unlink()
    except Exception as e:
        # the staged file may never have been CREATED (the creation is
        # inside this guard now), so the cleanup skips it then
        survivors = []
        if staged is not None:
            for p in ([staged]
                      + [Path(str(staged) + sfx) for sfx in SIDECAR_SUFFIXES]):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
                if p.exists():
                    survivors.append(p)
        # R1-F-004: the removal itself can fail (an AV lock, a Windows
        # file lock), and the staged copy holds PLAINTEXT store bytes --
        # a survivor must be REPORTED, never silently left behind (the
        # same discipline the private working copy gets).
        for p in survivors:
            print(f"[zmem] restore FAILED: the staged copy {_esc(str(p))} "
                  "SURVIVED its cleanup and still holds plaintext store "
                  "bytes - remove it by hand", file=sys.stderr)
        print(f"[zmem] restore FAILED: the staged copy could not be validated "
              f"({_esc(e)}) - destination untouched", file=sys.stderr)
        return 1

    # --- 3+4. Rotate the incarnation on the staged copy and take the
    # forensic copy — guarded like every neighbouring step (F-004,
    # swarm-pr-review RP-003/CS-002/MA-004): a failure here (an unusable
    # --out-dir/ZMEM_BACKUP_DIR, an unwritable backup tree, an AV-locked
    # forensic target, an OSError from the rotation) must print a ruled
    # ABORTED line, clean the staged copy, and leave the destination
    # untouched — never a raw traceback with a leaked full-store stage.
    # The backup dir is only needed when there is something to preserve
    # (sidecars to set aside, or a non-empty destination to back up), so a
    # first-time disaster-recovery restore into an empty location must not
    # fail on an unusable ZMEM_BACKUP_DIR at all (the round-2 critic's
    # second regression). ---
    try:
        rotate_incarnation_on_staging(staged)
        backup_dir = (
            _ensure_backup_dir(out_dir) if (present or dest_nonempty) else None)
        if present:
            forensic_dir = backup_dir / f"restore-forensic-{_snapshot_stamp()}"
            n = 1
            while forensic_dir.exists():
                forensic_dir = backup_dir / f"restore-forensic-{_snapshot_stamp()}-{n}"
                n += 1
            forensic_dir.mkdir(parents=True)
            if _host is not None:
                # F-005 (round-1 review comment): harden the forensic dir
                # BEFORE the pre-restore copies land in it -- those bytes
                # must never exist with default permissions.
                _host.set_owner_only_perms(forensic_dir)
            for p in [dest] + present:
                if p.exists():
                    shutil.copy2(p, forensic_dir / p.name)
    except Exception as e:
        print(f"[zmem] restore ABORTED: preparation failed ({_esc(e)}) — "
              f"destination untouched", file=sys.stderr)
        survivors = []
        for p in ([staged]
                  + [Path(str(staged) + sfx) for sfx in SIDECAR_SUFFIXES]):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            if p.exists():
                survivors.append(p)
        # the staged copy holds PLAINTEXT store bytes: a survivor must be
        # REPORTED, never silently left behind
        for p in survivors:
            print(f"[zmem] restore FAILED: the staged copy "
                  f"{_esc(str(p))} SURVIVED its cleanup and still "
                  "holds plaintext store bytes - remove it by hand",
                  file=sys.stderr)
        return 1

    # --- 5. Safety copy from a PRIVATE working copy (non-empty destination). ---
    if dest_nonempty:
        work_dir = None
        try:
            work_dir = backup_dir / f"restore-work-{_snapshot_stamp()}"
            n = 1
            while work_dir.exists():
                work_dir = backup_dir / f"restore-work-{_snapshot_stamp()}-{n}"
                n += 1
            work_dir.mkdir(parents=True)
            # F-005 (swarm-pr-review round 2): the forensic dir is
            # owner-only hardened; the work dir holds a byte copy of the
            # LIVE store, so it must be too -- otherwise the guarantee
            # depends on how backup_dir happened to be created (a
            # pre-existing, operator-supplied --out-dir is not hardened
            # by _ensure_backup_dir). Best-effort: a filesystem that
            # cannot harden must not break the restore, and the safety
            # copy itself is the priority.
            if _host is not None:
                try:
                    _host.set_owner_only_perms(work_dir)
                except Exception:
                    pass
            working_main = work_dir / dest.name
            shutil.copyfile(dest, working_main)
            for sfx in ("-wal", "-journal"):     # never -shm
                src = Path(str(dest) + sfx)
                if src.exists():
                    shutil.copyfile(src, Path(str(working_main) + sfx))
            pre_dest = _new_snapshot_path(backup_dir, PRERESTORE_PREFIX)
            pre_info = create_snapshot(working_main, pre_dest)
            prerestore_path = Path(pre_info["path"])
            shutil.rmtree(work_dir, ignore_errors=True)
            _report_surviving_work_dir(work_dir)
            print(f"[zmem] restore: pre-restore backup {_esc(prerestore_path)} "
                  f"(integrity_check={_esc(pre_info['integrity_check'])}, "
                  f"total={pre_info['snapshot_total']} live={pre_info['snapshot_live']})")
        except Exception as e:
            print(f"[zmem] restore ABORTED: the pre-restore backup of the "
                  f"current store, or the report of a surviving working copy, "
                  f"failed ({_esc(e)}) — refusing to overwrite the only copy "
                  f"of it.", file=sys.stderr)
            # F-004/MA-001: the private working copy must not survive a
            # failed safety copy (it holds plaintext store + WAL bytes).
            # The staged copy holds the snapshot -- plaintext store bytes --
            # so its cleanup runs even when the survivor report raises. The
            # report's failure still propagates: it is never converted into
            # a success.
            try:
                if work_dir is not None:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    _report_surviving_work_dir(work_dir)
            finally:
                survivors = []
                for p in ([staged]
                          + [Path(str(staged) + sfx)
                             for sfx in SIDECAR_SUFFIXES]):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
                    if p.exists():
                        survivors.append(p)
                # the staged copy holds PLAINTEXT store bytes: a survivor
                # must be REPORTED, never silently left behind
                for p in survivors:
                    print(f"[zmem] restore FAILED: the staged copy "
                          f"{_esc(str(p))} SURVIVED its cleanup and still "
                          "holds plaintext store bytes - remove it by hand",
                          file=sys.stderr)
            return 1
    elif dest_exists:
        print("[zmem] restore: pre-restore backup skipped - destination store is "
              "an empty file (no store content needed preserving; any sidecar "
              "bytes were kept in the forensic copy)")
    else:
        print("[zmem] restore: pre-restore backup skipped - destination store "
              "does not exist yet")

    # --- 6. Set the sidecars aside by rename; reverse-order put-back on any
    # failure before the swap, continuing past a failed put-back. ---
    moved: list[Path] = []
    putback_failed = False
    try:
        if present:
            stash_dir = dest.parent / f"{dest.name}.restore-stash-{uuid.uuid4().hex[:8]}"
            stash_dir.mkdir()
            for p in present:
                p.rename(stash_dir / p.name)
                moved.append(p)
                print(f"[zmem] restore: removed stale {_esc(p.name)} from beside the store "
                      f"(set aside; byte copy kept in {_esc(forensic_dir)})")

        # --- 7. Swap. ---
        os.replace(staged, dest)
        staged = None
    except Exception as e:
        # The plan's step 8: a raising swap is treated as DONE when the
        # staged file is gone (the replacement happened and the failure is a
        # post-swap one -> the rollback of step 9 handles it); when the
        # staged file never moved it is a pre-swap failure (put back, refuse).
        swap_done = staged is None or not staged.exists()
        cause = str(e) or type(e).__name__
        if swap_done:
            # Fall through to the step-9 rollback machinery via the same
            # path the success-path failures take.
            print(f"[zmem] restore FAILED: {_esc(cause)}", file=sys.stderr)
            staged = None
            return _finish_failed_restore(
                dest=dest, dest_exists=dest_exists, dest_nonempty=dest_nonempty,
                prerestore_path=prerestore_path, stash_dir=stash_dir,
                forensic_dir=forensic_dir, sidecars=sidecars)
        # the staged copy is never left on a failure path
        for p in [staged] + [Path(str(staged) + sfx) for sfx in SIDECAR_SUFFIXES]:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if moved:
            for p in reversed(moved):
                try:
                    (stash_dir / p.name).rename(p)
                except OSError:
                    putback_failed = True
        if putback_failed:
            leftovers = sorted(p.name for p in stash_dir.iterdir()) if stash_dir and stash_dir.is_dir() else []
            print(f"[zmem] restore FAILED: {_esc(cause)}", file=sys.stderr)
            for name in leftovers:
                print(f"[zmem] restore: {_esc(name)} is set aside at "
                      f"{_esc(stash_dir)}", file=sys.stderr)
            if forensic_dir is not None:
                print(f"[zmem] restore: byte copies of the files from before the restore "
                      f"are kept in {_esc(forensic_dir)}", file=sys.stderr)
            # F-007: the ordering is deliberately fail-closed -- the
            # pre-restore backup is NOT re-installed automatically -- so
            # the operator must be told how to re-install it BY HAND.
            # The path is a copy-paste command and stays byte-exact, as
            # in the automatic-rollback failure branch above.
            if prerestore_path is not None:
                print(f"[zmem] restore: roll back by hand with "
                      f"{command_prefix()} restore --from "
                      f"{shlex.quote(str(prerestore_path))} --force",
                      file=sys.stderr)
            return 1
        print(f"[zmem] restore FAILED: {_esc(cause)}", file=sys.stderr)
        if stash_dir is not None and stash_dir.is_dir() and not any(stash_dir.iterdir()):
            stash_dir.rmdir()
        return 1

    # --- 8. The success path (today's sequence, unchanged in statements). ---
    try:
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
            raise RuntimeError(f"restored store could not be verified: {e}")
        if staged_counts is not None and (total, live) != staged_counts:
            raise RuntimeError(
                f"restored row counts differ from the validated copy "
                f"(installed total={total} live={live}; validated "
                f"total={staged_counts[0]} live={staged_counts[1]})")
        if _host is not None:
            try:
                _host.set_owner_only_perms(dest)
            except Exception as e:
                raise RuntimeError(
                    f"could not set owner-only permissions on {dest}: {e}")
        if post_integrity != "ok":
            print(f"[zmem] restore: destination {_esc(dest)}")
            print(f"[zmem] restore: post-restore integrity_check={_esc(post_integrity)}")
            raise RuntimeError(f"post-restore integrity_check={post_integrity}")
        try:
            mconn = _schema.connect()
            try:
                _schema.init_db(mconn)
                _schema.migrate(mconn)
                migrated_ver = mconn.execute(
                    "SELECT value FROM meta WHERE key=?",
                    (SCHEMA_VERSION_KEY,)).fetchone()
                migrated_ver = migrated_ver[0] if migrated_ver else "?"
                _schema._commit(mconn)
            finally:
                mconn.close()
        except Exception as e:
            raise RuntimeError(f"post-restore open/migrate failed: {e}")
    except Exception as e:
        # --- 9. ANY error after the swap rolls back. ---
        print(f"[zmem] restore FAILED: {_esc(e)}", file=sys.stderr)
        return _finish_failed_restore(
            dest=dest, dest_exists=dest_exists, dest_nonempty=dest_nonempty,
            prerestore_path=prerestore_path, stash_dir=stash_dir,
            forensic_dir=forensic_dir, sidecars=sidecars)

    # --- 10. Success output. ---
    if stash_dir is not None and stash_dir.is_dir():
        # Ruling step 8: "the stash removed" — on SUCCESS the stashed
        # sidecars' bytes are already preserved in the forensic directory,
        # so the whole stash tree goes (a bare rmdir would never fire: the
        # moved files are still inside it). A removal failure is reported.
        shutil.rmtree(stash_dir, ignore_errors=True)
        if stash_dir.is_dir():
            for leftover in sorted(p.name for p in stash_dir.iterdir()):
                print(f"[zmem] restore: {_esc(leftover)} is set aside at "
                      f"{_esc(stash_dir)}", file=sys.stderr)
    print(f"[zmem] restore: destination {_esc(dest)}")
    print(f"[zmem] restore: post-restore integrity_check={_esc(post_integrity)}")
    print(f"[zmem] restore: restored rows total={total} live={live}")
    print(f"[zmem] restore: migrated shape schema_version={_esc(migrated_ver)}")
    if prerestore_path is not None:
        if forensic_dir is not None:
            print(f"[zmem] restore: sidecars and main file kept byte-for-byte in "
                  f"{_esc(forensic_dir)}; the pre-restore backup "
                  f"{_esc(prerestore_path)} is the store as SQLite read it, "
                  f"including committed -wal frames")
    else:
        if forensic_dir is not None:
            if dest_exists:
                print(f"[zmem] restore: the files that were at the destination before "
                      f"the restore are kept byte-for-byte in {_esc(forensic_dir)}; no "
                      f"pre-restore backup exists because the destination store was "
                      f"an empty file")
            else:
                print(f"[zmem] restore: the files that were at the destination before "
                      f"the restore are kept byte-for-byte in {_esc(forensic_dir)}; no "
                      f"pre-restore backup exists because the destination store did "
                      f"not exist")
    if shape_info.get("rebuilt_index"):
        print("[zmem] restore: the staged copy's search index was rebuilt before "
              "installing (its derived data was stale)")
    print("[zmem] restore: OK")
    return 0
