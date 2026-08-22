#!/usr/bin/env python
"""ZMem legacy-store import — Phase 1 (box-wide unified memory, PLAN.md P1).

Copies the existing per-plugin ZCode store into the new box-neutral location
(~/.zmem by default) WITHOUT ever opening the source read-write. The legacy
store may be live (an active ZCode session writing to it) so this script:

  1. Transfers store.sqlite with SQLite's ONLINE BACKUP API, from a source
     connection opened STRICTLY read-only (`mode=ro` URI) into a fresh
     destination connection. A raw file copy of a live WAL-mode database can
     capture a torn snapshot — recent commits live in `-wal`, so the main
     file's bytes can be byte-identical while the copy is missing committed
     data, and a main-file-only fingerprint check would still report success.
     The backup API copies pages under SQLite's own locking and yields a
     consistent snapshot regardless of concurrent WAL activity. `core.md` is
     not a database and is still a plain file copy.
  2. Verifies the destination copy is intact (`PRAGMA integrity_check`) and
     reports its live/total row counts. Only the destination is ever opened
     writable.
  3. Hashes the source store.sqlite (sha256+size+mtime) before AND after the
     whole run and asserts they are IDENTICAL — belt-and-suspenders proof the
     source was never touched. If they differ (e.g. a live session wrote
     mid-run), the script reports the mismatch loudly; that is a "re-run when
     quiescent" signal, not a bug in this script, and the assertion must never
     be weakened.

The destination directory is checked with host.assert_local_fs() (no UNC, no
network drive, no OneDrive-synced dir) before anything is created in it, and
`--force` clears any pre-existing destination `-wal`/`-shm`/`-journal` sidecar
BEFORE the transfer, so a leftover journal from a previous store can never be
replayed onto the freshly imported one.

Refuses to overwrite a non-empty existing destination store.sqlite unless
--force is passed.

Usage:
  python import-store.py --source "C:\\path\\to\\store.sqlite" --dest-dir "C:\\Users\\<user>\\.zmem" [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import host as _host
except ImportError:
    _host = None


# Sidecars a sqlite database can leave beside its main file. Duplicated here
# rather than imported from store.py on purpose: importing store.py resolves
# STORE_PATH from the environment at import time, which this script must not do.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _file_fingerprint(path: Path) -> dict | None:
    """sha256 + size + mtime_ns of a file. None if the file doesn't exist."""
    if not path.exists():
        return None
    st = path.stat()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _existing_store_is_nonempty(dest_store: Path) -> bool:
    if not dest_store.exists():
        return False
    try:
        return dest_store.stat().st_size > 0
    except OSError:
        return True  # be conservative


def _backup_source_to_dest(source_store: Path, dest_store: Path) -> None:
    """Online-backup `source_store` -> `dest_store`.

    The source connection is opened STRICTLY read-only via a `mode=ro` URI, so
    this script's "never open the source read-write" invariant holds; the backup
    API then gives a transactionally consistent page copy even if a live writer
    is appending WAL frames underneath us.

    A read-only open of a WAL database can MATERIALIZE `-shm`/`-wal` beside the
    source, and a read-only connection cannot checkpoint them away on close.
    We deliberately do NOT clean those up.

    An earlier version snapshotted which sidecars existed before opening and
    deleted any that appeared afterwards, on the theory that those were ours.
    That snapshot is a TOCTOU: a legacy session that begins writing between the
    snapshot and the cleanup creates a `-wal` holding REAL COMMITTED FRAMES,
    which is indistinguishable from one we materialized — and deleting it makes
    that session's committed data unavailable to later connections. This script
    reads a store another process may own, so it leaves the source directory
    exactly as it found it. A stray `-shm`/`-wal` is harmless: SQLite recreates
    and checkpoints them on the next normal open.

    Path -> URI goes through Path.resolve().as_uri(); an f-string
    `file:{path}` does not survive a Windows `C:\\...` path.
    """
    src_uri = source_store.resolve().as_uri() + "?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        dst_conn = sqlite3.connect(str(dest_store))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _clear_dest_sidecars(dest_store: Path) -> None:
    """Delete any `-wal`/`-shm`/`-journal` left beside the destination by a
    PREVIOUS store. They belong to the file we are about to replace; left in
    place, SQLite would happily replay a stale rollback journal onto the newly
    imported database. Must run BEFORE the transfer, not after."""
    for s in SIDECAR_SUFFIXES:
        sib = Path(str(dest_store) + s)
        if sib.exists():
            sib.unlink()
            print(f"[import] removed stale destination {sib.name}")


def run_import(source_store: Path, dest_dir: Path, force: bool = False) -> dict:
    if not source_store.exists():
        raise FileNotFoundError(f"source store not found: {source_store}")

    source_dir = source_store.parent
    source_core_md = source_dir / "core.md"

    # The destination is a live WAL-mode sqlite location. Refuse UNC/network/
    # OneDrive-synced destinations BEFORE creating anything there — same guard
    # store.py's connect() applies to the store dir it is about to open.
    if _host is not None:
        _host.assert_local_fs(dest_dir)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_store = dest_dir / "store.sqlite"
    dest_core_md = dest_dir / "core.md"

    if _existing_store_is_nonempty(dest_store) and not force:
        raise FileExistsError(
            f"destination store already exists and is non-empty: {dest_store} "
            f"(pass --force to overwrite)"
        )

    print(f"[import] source: {source_store}")
    print(f"[import] dest:   {dest_store}")

    # --- Fingerprint the source BEFORE any copy work. Source is read-only
    # (opened for hashing only) from here to the end of the run. ---
    before = _file_fingerprint(source_store)
    if before is None:
        raise FileNotFoundError(f"source store vanished before fingerprinting: {source_store}")
    print(f"[import] source sha256 (before) = {before['sha256']}")

    # --- Clear the destination's stale sidecars, THEN transfer. The main file
    # itself is replaced wholesale by the backup API (it truncates/overwrites
    # the destination database), but a leftover journal from the store that
    # used to live here would outlive it. ---
    _clear_dest_sidecars(dest_store)

    _backup_source_to_dest(source_store, dest_store)
    print(f"[import] online-backup {source_store.name} -> {dest_store} "
          f"(source opened read-only)")

    if source_core_md.exists():
        shutil.copy2(source_core_md, dest_core_md)
        print(f"[import] copied {source_core_md.name} -> {dest_core_md}")
    else:
        print(f"[import] WARNING: no core.md at source ({source_core_md}); skipped")

    # --- Fingerprint the source AFTER copying. Must match `before`. ---
    after = _file_fingerprint(source_store)
    source_unchanged = after is not None and after == before
    print(f"[import] source sha256 (after)  = {after['sha256'] if after else 'MISSING'}")
    if not source_unchanged:
        raise RuntimeError(
            "SOURCE STORE CHANGED DURING IMPORT — a session likely wrote to it "
            "mid-copy. The import did not corrupt the source, but the "
            "before/after proof failed; re-run this import when the source "
            "is quiescent (no active ZCode/zmem session). "
            f"before={before} after={after}"
        )

    # --- Checkpoint + integrity-check ONLY the destination copy. The source
    # is never opened again past this point (and was only ever opened mode=ro).
    # The backup API leaves no WAL sidecar of its own; the checkpoint is kept
    # so the destination is left in the WAL mode store.py expects, with a
    # truncated log. ---
    conn = sqlite3.connect(str(dest_store))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        live = conn.execute("SELECT count(*) FROM memory WHERE superseded_at IS NULL").fetchone()[0]
    finally:
        conn.close()

    print(f"[import] destination integrity_check = {integrity}")
    print(f"[import] destination rows: total={total} live={live}")
    if integrity != "ok":
        raise RuntimeError(f"destination copy failed integrity_check: {integrity}")

    print("[import] source fingerprint unchanged before vs after — source untouched, confirmed.")

    # Harden perms on the freshly-populated box-wide store (owner-only ACL,
    # best-effort). connect()'s first-creation gate never fires for this dir
    # since we created it ourselves rather than through store.py.
    if _host is not None:
        _host.set_owner_only_perms(dest_dir)
        _host.set_owner_only_perms(dest_store)
        if dest_core_md.exists():
            _host.set_owner_only_perms(dest_core_md)

    print(f"[import] done: {dest_store}")

    return {
        "source": str(source_store),
        "dest": str(dest_store),
        "source_sha256_before": before["sha256"],
        "source_sha256_after": after["sha256"],
        "source_unchanged": source_unchanged,
        "dest_integrity_check": integrity,
        "dest_total_rows": total,
        "dest_live_rows": live,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a legacy ZMem/ZCode store into the box-wide location")
    ap.add_argument("--source", required=True, help="path to the legacy store.sqlite")
    ap.add_argument("--dest-dir", required=True, help="destination directory (e.g. ~/.zmem)")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty destination store")
    args = ap.parse_args()

    try:
        run_import(Path(args.source).expanduser(), Path(args.dest_dir).expanduser(), force=args.force)
    except Exception as e:
        print(f"[import] FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
