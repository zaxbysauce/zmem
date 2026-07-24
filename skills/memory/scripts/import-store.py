#!/usr/bin/env python
"""ZMem legacy-store import — Phase 1 (box-wide unified memory, PLAN.md P1).

Copies the existing per-plugin ZCode store into the new box-neutral location
(~/.zmem by default) WITHOUT ever opening the source read-write. The legacy
store may be live (an active ZCode session writing to it) so this script:

  1. File-copies store.sqlite (+ -wal/-shm if present at copy time) and the
     sibling core.md from the source dir to the destination dir. The source
     is never opened by this script, in any mode, at any point.
  2. Opens ONLY the destination copy and runs `PRAGMA wal_checkpoint(TRUNCATE)`
     on it, folding any copied WAL frames into the main db file.
  3. Verifies the destination copy is intact (`PRAGMA integrity_check`) and
     reports its live/total row counts.
  4. Hashes the source store.sqlite (sha256+size+mtime) before AND after the
     whole run and asserts they are IDENTICAL — proof the source was never
     touched. If they differ (e.g. a live session wrote mid-copy), the script
     reports the mismatch loudly; that is a "re-run when quiescent" signal,
     not a bug in this script, and the assertion must never be weakened.

Refuses to overwrite a non-empty existing destination store.sqlite unless
--force is passed.

Usage:
  python import-store.py --source "C:\\path\\to\\store.sqlite" --dest-dir "C:\\Users\\Brett\\.zmem" [--force]
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


def run_import(source_store: Path, dest_dir: Path, force: bool = False) -> dict:
    if not source_store.exists():
        raise FileNotFoundError(f"source store not found: {source_store}")

    source_dir = source_store.parent
    source_core_md = source_dir / "core.md"

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

    # --- Copy store.sqlite + any WAL/SHM siblings present right now, then core.md. ---
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(source_store) + suffix)
        if src.exists():
            dst = Path(str(dest_store) + suffix)
            shutil.copy2(src, dst)
            print(f"[import] copied {src.name} -> {dst}")

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
    # is never opened again past this point. ---
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
    # since we created it via file-copy rather than through store.py.
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
