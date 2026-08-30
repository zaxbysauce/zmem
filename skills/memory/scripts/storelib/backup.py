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
from storelib.schema import MAINTENANCE_LOCK_STALE_SECONDS, SCHEMA_LOCK_POLL_SECONDS, SCHEMA_LOCK_STALE_SECONDS, SCHEMA_LOCK_WAIT_SECONDS, STORE_PATH, _cleanup_stale_writer_leases, _commit, _env_float, _host, _parse_iso_to_epoch, _read_schema_version, _release_named_lock, _strict_acquire_lock, now_iso

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

SENTINEL_PREFIXES = (".capture-prompted-", ".convention-prompted-", ".convention-commit-prompted-")

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

def cmd_sweep(marker_dir: str | None = None,
              max_age_days: float | None = None,
              dry_run: bool = False) -> int:
    """Remove stale per-session cooldown sentinel files (issue #23).

    Idempotent, fail-open, bounded: sweeps only the (few) directories the
    capture/convention hooks can write markers into (or a single explicit
    --marker-dir), and within each only considers files whose name starts with a
    known sentinel prefix — everything else in those dirs (store, lock files,
    backups, unrelated dot-files) is strictly left alone. A marker older than
    `max_age_days` is garbage: it only ever gates a re-prompt within the session
    named in its filename.

    No advisory lock is needed: listdir + per-file unlink is idempotent, so two
    sessions sweeping the same dir at once are safe by construction (any
    FileNotFoundError on unlink is caught as an OSError below). Returns a process
    exit code; never raises on a missing dir or a permission error.
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
    cutoff = time.time() - max_age_days * 86400
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
        # survives as long as its session does).
        ops_dir = d / "ops"
        if ops_dir.is_dir():
            try:
                ring_names = os.listdir(ops_dir)
            except OSError:
                ring_names = []
            for rname in ring_names:
                if not rname.endswith((".log", ".delivered", ".pending")):
                    continue
                rp = ops_dir / rname
                try:
                    if not rp.is_file() or rp.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                if not dry_run:
                    try:
                        rp.unlink()
                    except OSError:
                        continue
                removed += 1
    if removed:
        verb = "would prune" if dry_run else "pruned"
        print(f"[zmem] sweep: {verb} {removed} stale session "
              "sentinel(s)/sidecar(s)")
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
