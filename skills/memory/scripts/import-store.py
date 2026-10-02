#!/usr/bin/env python
"""ZMem legacy-store import — staged, sanitized, atomic (issue #180).

Imports an existing (legacy) zmem store into a destination directory WITHOUT
ever opening the source read-write, and WITHOUT transferring rows that the
capture policy refuses: the destination is only replaced after every safe row
is redacted INCLUDING its derived carriers, every refused row is recorded,
and integrity + count checks pass on a STAGING database.

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
     id and are UPDATEd in place with the redacted content/tags PLUS every
     derived carrier recomputed or dropped: content_norm is recomputed from
     the post-redaction content (mirroring storelib.purge's keeper-rewrite
     idiom), the entity links are re-derived from the redacted text
     (entity.relink_memory), belief_head rows sourced from the redacted
     memory are rebuilt from its post-redaction content, the stale
     pre-redaction embedding columns (embedding/embedding_model/embedded_at)
     are cleared on the memory row, and the memory_vec vector is deleted
     (vec0 has no trigger and a stale embedding of pre-redaction text is a
     leak vector; `reembed` backfills from the now-clean text). Refused rows
     are DELETEd from the staging database together with their related rows
     (memory_vec, memory_link both directions, memory_entity,
     episode_memory, memory_evidence, belief_head_source/evidence when
     present), episode.summary_memory_id pointers to them are reset to ''
     (the empty-TEXT idiom), and the ORIGINAL full row is recorded for the
     post-acceptance quarantine flush.
  5. Refused-row records are BUFFERED during the sanitize transaction and
     appended to <dest_dir>/quarantine/<UTC-date>.jsonl only AFTER every
     validation gate has passed — a failed import (integrity mismatch, count
     mismatch, source-fingerprint mismatch, core.md failure, replace failure)
     therefore leaves NO quarantine ledger entries for an import that never
     completed, and a successful re-run never duplicates them. A quarantine
     append failure at flush time still aborts the import (fail-closed).
  6. Acceptance: the staged database is switched to journal_mode=DELETE (so
     os.replace can never race a WAL sidecar replay), its FTS index is
     optimized and the file VACUUMed (clearing FTS tombstone segments that
     could retain pre-redaction bytes), PRAGMA integrity_check must report
     ok, and staged_count = source_count - quarantined_count must equal the
     staged memory row count. Only then are stale destination sidecars
     STASHED (renamed aside; restored on failure, deleted on success), the
     staging file os.replace()d onto the destination, the staged core.md
     swapped into place, and owner-only permissions applied.

Importing storelib here is safe even though storelib resolves STORE_PATH from
the environment at import time: this script uses only the pure policy helpers
(apply_capture_policy, quarantine_import_row), the purge side-table list, the
entity relink helper, and schema's vec loader / content normalizer — never a
storelib connection or STORE_PATH itself.

Usage:
  python import-store.py --source "C:\\path\\to\\store.sqlite" --dest-dir "C:\\Users\\<user>\\.zmem" [--force]
"""

from __future__ import annotations

import argparse
import base64
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

from storelib.entity import relink_memory  # noqa: E402
from storelib.schema import _load_vec, _normalize_content  # noqa: E402
from storelib.purge import _ID_SIDE_TABLES  # noqa: E402
from storelib.write import (  # noqa: E402
    MAX_CONTENT_CHARS,
    CapturePolicyRefusal,
    REASON_UNREDACTABLE_SECRET,
    QUARANTINE_REASONS,
    apply_capture_policy,
    quarantine_import_row,
)


# Sidecars a sqlite database can leave beside its main file. Duplicated here
# rather than imported from store.py on purpose: the store's connection
# machinery is not used by this script (see the module docstring for what IS
# imported).
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# Columns that may carry (or derive from) pre-redaction content on the memory
# row itself. The UPDATE below sets all of them when present; older schemas
# simply lack the later ones and are handled via PRAGMA table_info.
_DERIVED_TEXT_COLUMNS = ("content_norm",)
_DERIVED_EMBEDDING_COLUMNS = ("embedding", "embedding_model", "embedded_at")


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


def _stash_dest_sidecars(dest_store: Path) -> list[tuple[Path, Path]]:
    """RENAME stale destination sidecars aside instead of deleting them, so a
    failed os.replace can restore them and the prior destination stays truly
    byte-identical (review round: sidecar clear used to precede the replace
    unguarded). Returns the (stash_path, original_path) pairs to clean up."""
    stashed = []
    for s in SIDECAR_SUFFIXES:
        sib = Path(str(dest_store) + s)
        if sib.exists():
            stash = sib.with_name(sib.name + ".store-180-stash")
            os.replace(sib, stash)
            stashed.append((stash, sib))
    return stashed


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


def _memory_columns(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(memory)")}


def _json_safe(value):
    """Make a legacy row JSON-serializable: BLOB columns (embedding) become a
    base64 marker so the quarantine record stays faithful without ever
    decoding secrets into a different encoding."""
    if isinstance(value, bytes):
        return {"__blob_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _rebuild_belief_heads(staged: sqlite3.Connection, mid: str,
                          new_content: str) -> None:
    """Rebuild belief_head rows sourced from a redacted memory so their
    content column carries the POST-redaction text (mirrors storelib.purge's
    head_rebuilds). No-op when the legacy store predates belief tables."""
    if not _table_exists(staged, "belief_head") or not _table_exists(
            staged, "belief_head_source"):
        return
    head_ids = [r["head_id"] for r in staged.execute(
        "SELECT DISTINCT head_id FROM belief_head_source WHERE source_id=?",
        (mid,)).fetchall()]
    for hid in head_ids:
        staged.execute(
            "UPDATE belief_head SET content=? WHERE id=?", (new_content, hid))


def _sanitize_staged_store(staged: sqlite3.Connection,
                           source: sqlite3.Connection) -> tuple[dict, list]:
    """Apply the shared capture policy to every staged memory row.

    Runs inside the caller's open transaction on the staging database. Safe
    rows keep their id and are UPDATEd with redacted content/tags plus every
    derived carrier recomputed or dropped; refused rows are deleted with
    their related rows. Quarantine records are BUFFERED and returned as a
    list of (reason, payload) — the caller flushes them through
    quarantine_import_row only after every acceptance gate has passed, so a
    failed import leaves no ledger entries behind (review round: the appends
    used to happen mid-transaction and outlived their aborted run).

    A post-policy content over MAX_CONTENT_CHARS when the original fit is
    itself a quarantineable refusal (reason ``unredactable_secret``): the
    redacted form cannot be stored, so the row must not land silently.
    """
    source_count = source.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    counters = {"added": 0, "redacted": 0, "quarantined": 0,
                "quarantine_failed": 0}
    pending: list[tuple[str, dict]] = []
    cols = _memory_columns(staged)
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
            if len(new_content) > MAX_CONTENT_CHARS:
                # The redacted form no longer fits the storage cap (value-span
                # markers grow the text) — there is no safe storable form.
                raise CapturePolicyRefusal(
                    REASON_UNREDACTABLE_SECRET,
                    f"capture refused: {REASON_UNREDACTABLE_SECRET}")
        except CapturePolicyRefusal as exc:
            # A genuine capture refusal: record the FULL original row for the
            # post-acceptance flush. Only CapturePolicyRefusal is handled
            # here — any other exception (AutoCaptureRuntimeError, a future
            # bug) propagates so the staged transaction rolls back and the
            # import aborts instead of silently quarantining a row the
            # policy never judged.
            reason = exc.reason
            if reason not in QUARANTINE_REASONS:
                reason = "source_ref_secret_like"
            pending.append((reason, _json_safe(dict(row))))
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
            # Keeper rewrite mirroring storelib.purge: content AND every
            # derived carrier must be recomputed or dropped, or the secret
            # survives in content_norm (dedup/recall), the embedding columns
            # (reembed re-propagates it), the entity links, and belief_head
            # content copies. The FTS triggers (memory_au) keep memory_fts
            # current; the post-loop optimize + VACUUM clear tombstone
            # segments that could retain pre-redaction bytes.
            update_cols = ["content=?", "tags=?"]
            update_args: list = [new_content, new_tags]
            if "content_norm" in cols:
                update_cols.append("content_norm=?")
                update_args.append(_normalize_content(new_content))
            for col in _DERIVED_EMBEDDING_COLUMNS:
                if col in cols:
                    update_cols.append(f"{col}=?")
                    update_args.append(None if col == "embedding" else
                                       ("" if col == "embedding_model" else ""))
            update_args.append(mid)
            staged.execute(
                f"UPDATE memory SET {', '.join(update_cols)} WHERE id=?",
                tuple(update_args))
            if has_vec:
                staged.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
            # Entity links were extracted from the PRE-redaction text —
            # re-derive them from the stored (redacted) row (entity.py
            # relink_memory: drop stale links, re-run the extractor).
            relink_memory(staged, mid)
            _rebuild_belief_heads(staged, mid, new_content)
            counters["redacted"] += 1
        counters["added"] += 1
    staged_count = source_count - counters["quarantined"]
    return {"source_count": source_count, "staged_count": staged_count,
            **counters}, pending


def _flush_quarantine(dest_dir: Path, pending: list) -> None:
    """Append every buffered refusal record. Called only after ALL acceptance
    gates passed, so the ledger never holds entries for an import that failed
    validation. An OSError here still aborts the import (fail-closed)."""
    for reason, payload in pending:
        quarantine_import_row(dest_dir, payload, reason=reason)


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
                # Load sqlite-vec on the staging connection when available so
                # DELETE FROM memory_vec works on vec-bearing sources; a
                # source without the table skips the vec deletes entirely
                # (review round: a bare connect crashed the whole import with
                # `no such module: vec0` on modern stores).
                try:
                    _load_vec(staged_conn)
                except Exception:
                    pass
                print(f"[import] online-backup {source_store.name} -> staging "
                      f"{staged_path.name} (source opened read-only)")
                source_conn.backup(staged_conn)
                staged_conn.execute("BEGIN IMMEDIATE")
                counts, pending = _sanitize_staged_store(staged_conn,
                                                         source_conn)
                staged_conn.commit()
                print(f"[import] staged rows: added={counts['added']} "
                      f"redacted={counts['redacted']} "
                      f"quarantined={counts['quarantined']} "
                      f"quarantine_failed={counts['quarantine_failed']}")

                # --- Compact the staged database (FTS optimize clears
                # tombstone segments that could retain pre-redaction bytes;
                # VACUUM rebuilds the file), switch to journal_mode=DELETE so
                # no sidecar of the staging file can race os.replace, then
                # verify. ---
                if _table_exists(staged_conn, "memory_fts"):
                    staged_conn.execute(
                        "INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
                staged_conn.execute("PRAGMA journal_mode=DELETE")
                staged_conn.commit()
                staged_conn.execute("VACUUM")
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

        # --- Flush the buffered quarantine ledger HERE: every validation gate
        # has passed, and the flush must succeed BEFORE the destination is
        # touched (review round: appends after the replace let a failed flush
        # report exit 1 with the new store already in place). A flush failure
        # aborts fail-closed with the prior destination untouched. ---
        _flush_quarantine(dest_dir, pending)

        # --- Accept. Order matters: stage core.md BEFORE the store replace
        # (a copy failure must abort BEFORE the destination is touched), stash
        # stale destination sidecars (restored on failure), then atomically
        # replace. ---
        staged_core = None
        if source_core_md.exists():
            fd_core, staged_core_str = tempfile.mkstemp(
                prefix=".core-180-", suffix=".md.tmp", dir=dest_dir)
            os.close(fd_core)
            staged_core = Path(staged_core_str)
            shutil.copy2(source_core_md, staged_core)
        stashed = _stash_dest_sidecars(dest_store)
        try:
            os.replace(staged_path, dest_store)
            if staged_core is not None:
                os.replace(staged_core, dest_core_md)
                staged_core = None
                print(f"[import] copied {source_core_md.name} -> {dest_core_md}")
            elif source_core_md.exists():
                # Unreachable in practice (staged above); kept for clarity.
                pass
            else:
                print(f"[import] WARNING: no core.md at source ({source_core_md}); skipped")
        except OSError as exc:
            # Restore the prior destination exactly: sidecars back, staged
            # files removed.
            for stash, orig in stashed:
                try:
                    os.replace(stash, orig)
                except OSError:
                    pass
            raise OSError(
                f"{exc}; if the destination store is held open by a running "
                f"zmem session, hook, or MCP server, close it and re-run this "
                f"import (destination was left untouched)") from exc
        for stash, _orig in stashed:
            try:
                stash.unlink()
            except OSError:
                pass
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
