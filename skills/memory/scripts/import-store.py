#!/usr/bin/env python
"""ZMem legacy-store import — staged, sanitized, atomic (issue #180).

Imports an existing (legacy) zmem store into a destination directory WITHOUT
ever opening the source read-write, and WITHOUT transferring rows that the
capture policy refuses: the destination is only replaced after every safe row
is redacted, every refused row is quarantined, and integrity + count checks
pass on a STAGING database.

Flow (issue #180, Workstream L PR 1):

  1. Guards from the pre-#180 script are kept: the destination directory is
     checked with host.assert_local_fs() (no UNC/network/OneDrive paths) and a
     non-empty destination store.sqlite without --force is a FileExistsError
     BEFORE anything is staged.
  2. The source is opened STRICTLY read-only (`mode=ro` URI plus
     PRAGMA query_only=1). Its sha256/size/mtime fingerprint is taken before
     any work and asserted IDENTICAL after the whole run — the source is never
     touched, and a live writer changing it mid-run is a loud "re-run when
     quiescent" signal, never a corrupted import.
  3. A staging database is created with tempfile.mkstemp inside dest_dir
     (prefix ".store-180-", suffix ".sqlite.tmp") and populated by SQLite's
     ONLINE BACKUP API from the read-only source (a raw file copy of a live
     WAL database can capture a torn snapshot; the backup API cannot).
  4. Every source memory row, in deterministic `id` order, is run through the
     shared capture policy (apply_capture_policy, capture_mode="auto"; NULL
     content/source_ref/tags are normalized to "" first). Safe rows keep their
     id and relationships and are UPDATEd in place with the redacted
     content/tags (the store's own FTS triggers keep memory_fts in sync;
     memory_vec rows for changed rows are dropped — vec0 has no trigger and a
     stale embedding of pre-redaction text is a leak vector). Refused rows are
     DELETEd from the staging database together with their related rows
     (memory_vec, memory_link both directions, memory_entity, episode_memory,
     memory_evidence, belief_head_source/belief_head_evidence when present),
     episode.summary_memory_id pointers to them are reset to '' (the empty-TEXT
     idiom), and the ORIGINAL row is appended to
     <dest_dir>/quarantine/<UTC-date>.jsonl via quarantine_import_row — a
     durable record, not a silent drop.
  5. The staging transaction commits only after every row is dispositioned and
     every quarantine append succeeded. A quarantine write failure (or any
     other failure) rolls the staging transaction back and removes ONLY the
     staging file: the prior destination — store.sqlite, its sidecars, and
     core.md — is left byte-identical.
  6. Acceptance: the staged database is switched to journal_mode=DELETE (so
     os.replace can never race a WAL sidecar replay), PRAGMA integrity_check
     must report ok, and staged_count = source_count - quarantined_count must
     equal the staged memory row count. Only then are stale destination
     sidecars cleared, the staging file os.replace()d onto the destination,
     core.md copied, and owner-only permissions applied.

Importing storelib here is safe even though storelib resolves STORE_PATH from
the environment at import time: this script uses only the pure policy helpers
(apply_capture_policy, quarantine_import_row) and the purge side-table list,
never a storelib connection or STORE_PATH itself.

Usage:
  python import-store.py --source "C:\\path\\to\\store.sqlite" --dest-dir "C:\\Users\\<user>\\.zmem" [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import host as _host
except ImportError:
    _host = None

from storelib.write import CapturePolicyRefusal, QUARANTINE_REASONS, apply_capture_policy, quarantine_import_row  # noqa: E402
from storelib.purge import _ID_SIDE_TABLES  # noqa: E402


# Sidecars a sqlite database can leave beside its main file. Duplicated here
# rather than imported from store.py on purpose: the store's connection
# machinery is not used by this script (see the module docstring for what IS
# imported).
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# Related-row tables keyed by memory id (mirrors storelib.purge's cleanup set;
# imported from there — this list must never drift from the purge contract).


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


def _clear_dest_sidecars(dest_store: Path) -> None:
    """Delete any `-wal`/`-shm`/`-journal` left beside the destination by a
    PREVIOUS store. They belong to the file we are about to replace; left in
    place, SQLite would happily replay a stale rollback journal onto the newly
    imported database. Runs just BEFORE the atomic replace (issue #180: an
    aborted import must leave the prior destination — sidecars included —
    untouched), not before staging."""
    for s in SIDECAR_SUFFIXES:
        sib = Path(str(dest_store) + s)
        if sib.exists():
            sib.unlink()
            print(f"[import] removed stale destination {sib.name}")


def _open_source_readonly(source_store: Path) -> sqlite3.Connection:
    """Open the source STRICTLY read-only (mode=ro URI + query_only=1)."""
    src_uri = source_store.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(src_uri, uri=True)
    conn.execute("PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


def _sanitize_staged_store(staged: sqlite3.Connection,
                           source: sqlite3.Connection,
                           dest_dir: Path) -> dict:
    """Apply the shared capture policy to every staged memory row.

    Runs inside the caller's open transaction on the staging database. Safe
    rows keep their id and are UPDATEd with redacted content/tags; refused
    rows are deleted with their related rows and quarantined. Returns the
    {added, redacted, quarantined, quarantine_failed} counters; a quarantine
    write failure raises OSError after incrementing quarantine_failed (the
    caller rolls back and aborts — the counter is for the error path).
    """
    source_count = source.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    counters = {"added": 0, "redacted": 0, "quarantined": 0,
                "quarantine_failed": 0}
    has_vec = _table_exists(staged, "memory_vec")
    rows = source.execute("SELECT * FROM memory ORDER BY id").fetchall()
    for row in rows:
        mid = row["id"]
        # NULL fields are legal in a real legacy store; the policy joins
        # strings and must never see None.
        content = row["content"] or ""
        source_ref = row["source_ref"] or ""
        tags = row["tags"] or ""
        try:
            new_content, new_source_ref, new_tags, _warnings = (
                apply_capture_policy(content=content, source_ref=source_ref,
                                     tags=tags, capture_mode="auto"))
        except CapturePolicyRefusal as exc:
            # A genuine capture refusal: quarantine with its stable reason.
            # Only CapturePolicyRefusal is handled here — any other exception
            # (AutoCaptureRuntimeError, a future bug) propagates so the staged
            # transaction rolls back and the import aborts instead of
            # silently quarantining a row the policy never judged.
            reason = exc.reason
            if reason not in QUARANTINE_REASONS:
                reason = "source_ref_secret_like"
            quarantine_row = {
                "id": mid,
                "namespace": row["namespace"],
                "type": row["type"],
                "content": content,
                "tags": tags,
                "source_ref": source_ref,
                "signal": row["signal"],
            }
            try:
                quarantine_import_row(dest_dir, quarantine_row, reason=reason)
            except OSError:
                counters["quarantine_failed"] += 1
                raise
            # Delete the staged memory row and every related row. The FTS
            # triggers (memory_ad) keep memory_fts consistent; memory_vec has
            # no trigger, so it is deleted explicitly.
            staged.execute("DELETE FROM memory WHERE id=?", (mid,))
            if has_vec:
                staged.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
            for table, col in _ID_SIDE_TABLES:
                if _table_exists(staged, table):
                    staged.execute(f"DELETE FROM {table} WHERE {col}=?", (mid,))
            if _table_exists(staged, "episode"):
                staged.execute(
                    "UPDATE episode SET summary_memory_id='' "
                    "WHERE summary_memory_id=?", (mid,))
            counters["quarantined"] += 1
            print(f"[import] quarantined {mid} ({reason})")
            continue
        if new_content != content or new_tags != tags:
            staged.execute(
                "UPDATE memory SET content=?, tags=? WHERE id=?",
                (new_content, new_tags, mid))
            # The stored embedding was computed over the PRE-redaction text —
            # drop it rather than keep a vector of secret content (the FTS
            # triggers handle the text index; `reembed` can backfill).
            if has_vec:
                staged.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
            counters["redacted"] += 1
        counters["added"] += 1
    staged_count = source_count - counters["quarantined"]
    return {"source_count": source_count, "staged_count": staged_count,
            **counters}


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

    # --- Fingerprint the source BEFORE any copy work. The source is only ever
    # opened read-only from here to the end of the run. ---
    before = _file_fingerprint(source_store)
    if before is None:
        raise FileNotFoundError(f"source store vanished before fingerprinting: {source_store}")
    print(f"[import] source sha256 (before) = {before['sha256']}")

    # --- Stage: build a sanitized copy in dest_dir, replace only on success
    # (issue #180). ---
    fd, staged_path_str = tempfile.mkstemp(prefix=".store-180-",
                                           suffix=".sqlite.tmp", dir=dest_dir)
    os.close(fd)
    staged_path = Path(staged_path_str)
    staged_path.unlink()  # mkstemp created an empty file; the backup API
    # wants to initialize the database itself.
    try:
        source_conn = _open_source_readonly(source_store)
        try:
            staged_conn = sqlite3.connect(str(staged_path))
            try:
                staged_conn.row_factory = sqlite3.Row
                print(f"[import] online-backup {source_store.name} -> staging "
                      f"{staged_path.name} (source opened read-only)")
                source_conn.backup(staged_conn)
                staged_conn.execute("BEGIN IMMEDIATE")
                counts = _sanitize_staged_store(staged_conn, source_conn,
                                                dest_dir)
                staged_conn.commit()
                print(f"[import] staged rows: added={counts['added']} "
                      f"redacted={counts['redacted']} "
                      f"quarantined={counts['quarantined']} "
                      f"quarantine_failed={counts['quarantine_failed']}")

                # --- Verify the staged database, then close it before the
                # atomic replace (issue #180: journal_mode=DELETE so no WAL
                # sidecar of the staging file can race os.replace). ---
                staged_conn.execute("PRAGMA journal_mode=DELETE")
                integrity = staged_conn.execute(
                    "PRAGMA integrity_check").fetchone()[0]
                total = staged_conn.execute(
                    "SELECT COUNT(*) FROM memory").fetchone()[0]
                live = staged_conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
                ).fetchone()[0]
            finally:
                staged_conn.close()
        finally:
            source_conn.close()

        print(f"[import] destination integrity_check = {integrity}")
        print(f"[import] destination rows: total={total} live={live}")
        if integrity != "ok":
            raise RuntimeError(
                f"staged copy failed integrity_check: {integrity}")
        if counts["staged_count"] != total:
            raise RuntimeError(
                f"staged count mismatch: source_count="
                f"{counts['source_count']} - quarantined="
                f"{counts['quarantined']} = {counts['staged_count']} but the "
                f"staged memory table holds {total} row(s)")

        # --- Fingerprint the source AFTER all copy work. Must match before. ---
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
        print("[import] source fingerprint unchanged before vs after — source untouched, confirmed.")

        # --- Accept: clear stale destination sidecars (a leftover journal from
        # the store that used to live here must never replay onto the new
        # file), then atomically replace the destination. ---
        _clear_dest_sidecars(dest_store)
        os.replace(staged_path, dest_store)
        if source_core_md.exists():
            shutil.copy2(source_core_md, dest_core_md)
            print(f"[import] copied {source_core_md.name} -> {dest_core_md}")
        else:
            print(f"[import] WARNING: no core.md at source ({source_core_md}); skipped")
    except BaseException:
        # Remove ONLY the staging file: the prior destination — store.sqlite,
        # its sidecars, and core.md — stays byte-identical on every failure
        # path (quarantine write failure, integrity mismatch, count mismatch,
        # source fingerprint mismatch).
        try:
            if staged_path.exists():
                staged_path.unlink()
        except OSError:
            pass
        raise

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
        "added": counts["added"],
        "redacted": counts["redacted"],
        "quarantined": counts["quarantined"],
        "quarantine_failed": counts["quarantine_failed"],
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
