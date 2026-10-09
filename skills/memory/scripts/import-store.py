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
     appended to <dest_dir>/quarantine/<UTC-date>.jsonl after every
     validation gate has passed but BEFORE the destination is touched — a
     flush failure therefore aborts fail-closed with the prior destination
     byte-identical. The flush itself is one locked, all-or-nothing batch
     append (a partial failure truncates back to the pre-write size), and
     records the ledger already holds (compared WITHOUT the per-call
     quarantined_at stamp) are skipped — so the only failures that can
     follow a successful flush (core.md swap, store replace) never
     duplicate entries on a successful re-run.
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
(apply_capture_policy, quarantine_import_rows), the purge side-table list and
head-survivor helper, the entity relink helper, and schema's vec loader /
content normalizer — never a storelib connection or STORE_PATH itself.

Usage:
  python import-store.py --source "C:\\path\\to\\store.sqlite" --dest-dir "C:\\Users\\<user>\\.zmem" [--force]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import host as _host
except ImportError:
    _host = None

from storelib.entity import relink_memory  # noqa: E402
from storelib.schema import _load_vec, _normalize_content, init_db, migrate  # noqa: E402
from storelib.purge import _ID_SIDE_TABLES, _head_surviving_source  # noqa: E402
from storelib.sync import (  # noqa: E402
    _INGEST_ID_RE,
    _validate_sync_row,
    cmd_ingest_jsonl_strict,
)
from storelib.write import (  # noqa: E402
    MAX_CONTENT_CHARS,
    CapturePolicyRefusal,
    REASON_UNREDACTABLE_SECRET,
    QUARANTINE_REASONS,
    apply_capture_policy,
    quarantine_import_rows,
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
    unguarded). Returns the (stash_path, original_path) pairs to clean up.
    A rename that fails mid-loop restores the sidecars already stashed before
    re-raising (review round: a partial stash otherwise stranded the first
    sidecar under its stash name, losing the old store's WAL)."""
    stashed: list[tuple[Path, Path]] = []
    try:
        for s in SIDECAR_SUFFIXES:
            sib = Path(str(dest_store) + s)
            if sib.exists():
                stash = sib.with_name(sib.name + ".store-180-stash")
                os.replace(sib, stash)
                stashed.append((stash, sib))
    except OSError:
        for stash, orig in stashed:
            try:
                os.replace(stash, orig)
            except OSError:
                pass
        raise
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


def _scrub_belief_heads_after_refusal(
        staged: sqlite3.Connection, mid: str) -> None:
    """A refused row's text never becomes storable, so no belief_head may
    keep a content copy of it (review round: the refusal branch deleted the
    junction rows but left the head itself serving the refused text through
    recall). Mirrors storelib.purge: heads with a surviving source are
    rebuilt from that source's newest live row (content, head_source_id,
    support_count); heads with none are deleted. No-op when the legacy store
    predates the belief tables."""
    if not _table_exists(staged, "belief_head") or not _table_exists(
            staged, "belief_head_source"):
        return
    head_ids = [r["head_id"] for r in staged.execute(
        "SELECT DISTINCT head_id FROM belief_head_source WHERE source_id=?",
        (mid,)).fetchall()]
    for hid in head_ids:
        src = _head_surviving_source(staged, hid, [mid])
        if src is None:
            staged.execute("DELETE FROM belief_head WHERE id=?", (hid,))
        else:
            staged.execute(
                "UPDATE belief_head SET content=?, head_source_id=?, "
                "support_count=MAX(support_count-1, 0) WHERE id=?",
                (src["content"], src["id"], hid))


def _sanitize_staged_store(staged: sqlite3.Connection,
                           source: sqlite3.Connection) -> tuple[dict, list]:
    """Apply the shared capture policy to every staged memory row.

    Runs inside the caller's open transaction on the staging database. Safe
    rows keep their id and are UPDATEd with redacted content/tags plus every
    derived carrier recomputed or dropped; refused rows are deleted with
    their related rows. Quarantine records are BUFFERED and returned as a
    list of (reason, payload) — the caller flushes them through
    quarantine_import_rows only after every acceptance gate has passed, so a
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
            # belief_head rows themselves carry a content COPY of their
            # source rows — scrub them BEFORE the junction deletes below,
            # which is how the scrub finds this row's heads (the survivor
            # query excludes `mid` via the chain and the already-deleted
            # memory row alike).
            _scrub_belief_heads_after_refusal(staged, mid)
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
    """Append every buffered refusal record as ONE locked, all-or-nothing
    batch write (quarantine_import_rows) that SKIPS records the ledger
    already holds, so a re-run after a failed import never duplicates
    entries. Called only after ALL acceptance gates passed and BEFORE the
    destination is touched, so a flush failure aborts fail-closed with the
    prior destination byte-identical. An OSError here still aborts the
    import (fail-closed) — and leaves no partial batch behind."""
    if pending:
        quarantine_import_rows(dest_dir, pending)


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
    staged_core: Path | None = None  # bound before the guarded block so the
    # failure handler below can always clean the staged core.md temp up.
    try:
        source_conn = _open_source_readonly(source_store)
        try:
            staged_conn = sqlite3.connect(str(staged_path))
            try:
                staged_conn.row_factory = sqlite3.Row
                print(f"[import] online-backup {source_store.name} -> staging "
                      f"{staged_path.name} (source opened read-only)")
                source_conn.backup(staged_conn)
                # Load sqlite-vec AFTER the backup, now that the staged copy
                # carries the source's tables: a vec-bearing source needs the
                # extension for the DELETE FROM memory_vec in sanitize; a
                # vec-less source sanitizes fine without it (review round: a
                # bare connect used to crash the whole import with `no such
                # module: vec0`). If the loader is unavailable for a
                # vec-BEARING source, re-raise the loader's real error instead
                # of a confusing vec0 failure at the first DELETE.
                try:
                    _load_vec(staged_conn)
                except Exception:
                    if _table_exists(staged_conn, "memory_vec"):
                        raise
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
        # touched (fail-closed: a flush failure leaves the prior destination
        # byte-identical). The batch append is all-or-nothing under the
        # writer lock, and records the ledger already holds are skipped, so
        # the only failures that can follow a successful flush (core.md
        # swap, store replace) never duplicate entries on a re-run. ---
        _flush_quarantine(dest_dir, pending)

        # --- Accept. Order matters (review round): stage core.md BEFORE the
        # store replace (a copy failure must abort BEFORE the destination is
        # touched), stash stale destination sidecars INSIDE the guarded block
        # (a partial stash rolls itself back), replace the store, swap
        # core.md. Once the store replace has succeeded the old sidecars are
        # never restored beside the NEW store (mixed generations). ---
        staged_core = None
        if source_core_md.exists():
            fd_core, staged_core_str = tempfile.mkstemp(
                prefix=".core-180-", suffix=".md.tmp", dir=dest_dir)
            os.close(fd_core)
            staged_core = Path(staged_core_str)
            shutil.copy2(source_core_md, staged_core)
        store_replaced = False
        stashed: list[tuple[Path, Path]] = []
        try:
            stashed = _stash_dest_sidecars(dest_store)
            os.replace(staged_path, dest_store)
            store_replaced = True
            if staged_core is not None:
                os.replace(staged_core, dest_core_md)
                staged_core = None
                print(f"[import] copied {source_core_md.name} -> {dest_core_md}")
            else:
                print(f"[import] WARNING: no core.md at source ({source_core_md}); skipped")
        except OSError as exc:
            if store_replaced:
                # Only the core.md swap failed; the store itself landed. The
                # stashed sidecars belong to the replaced-away store — drop
                # them, never re-home them beside the NEW store (review
                # round: that left mixed generations and broke the re-run).
                for stash, _orig in stashed:
                    try:
                        stash.unlink()
                    except OSError:
                        pass
                raise OSError(
                    f"{exc}; the destination store WAS updated but copying "
                    f"{dest_core_md.name} failed — close any zmem session "
                    f"holding it open, then re-run this import (the store "
                    f"re-import is idempotent) or copy the file manually"
                ) from exc
            # Store not yet replaced: put the prior destination back exactly.
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
        # Remove ONLY the staging files: on every failure path up to and
        # including the store replace, the prior destination — store.sqlite,
        # its sidecars, and core.md — stays byte-identical (quarantine write
        # failure, integrity mismatch, count mismatch, source fingerprint
        # mismatch). After the store replace, the store IS the accepted new
        # one and only the staged core.md temp still needs removing.
        try:
            if staged_path.exists():
                staged_path.unlink()
        except OSError:
            pass
        try:
            if staged_core is not None and staged_core.exists():
                staged_core.unlink()
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


# --- Hindsight JSONL import (issue #181, Workstream L PR 2) -----------------
#
# `--source hindsight --input <export.jsonl>` maps a Hindsight export into a
# destination store while PRESERVING the source kind: every record becomes a
# destination `fact` row carrying deterministic `hindsight:*` tags, and the
# supplied Hindsight metadata + occurrence dates ride along in a canonical
# JSONL written beside the store (the memory table has no metadata columns —
# the canonical JSONL is the metadata/occurrence-authoritative artifact; the
# store receives the memory-shaped projection).

# Accepted source kinds, exactly the issue's six.
_HINDSIGHT_KINDS = ("world", "experience", "observation", "kv", "current",
                    "event")

# The parse phase materializes the whole export before the bounded strict
# ingest lane runs, so the input carries its own size gate (mirrors the sync
# lane's STRICT_MAX_BYTES).
HINDSIGHT_MAX_INPUT_BYTES = 64 * 1024 * 1024


def _reject_json_constant(name: str) -> float:
    """json.loads parse_constant hook: NaN/Infinity/-Infinity are malformed
    JSON (the issue lists malformed JSON as a content failure), and the
    canonical serializer would otherwise re-emit them as bare tokens no
    RFC-8259 consumer can parse."""
    raise ValueError(f"non-finite JSON constant {name}")


def _json_object_no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """json.loads object_pairs_hook: reject duplicate keys at every nesting
    level (the strict lane's own _strict_object_pairs semantics), so the
    canonical output is a faithful — not last-wins-collapsed — canonicalization."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON object key {key!r}")
        out[key] = value
    return out


def _parse_hindsight_records(input_path: Path) -> list[dict]:
    """Parse + validate a Hindsight JSONL export into canonical records.

    Every violation raises ValueError BEFORE any destination work (the CLI
    maps it to ``[import] FAILED:`` + exit 1): malformed JSON, a non-object
    record, both or neither of ``kind``/``type``, an unsupported kind, a
    non-UUID-shaped or duplicate id, an empty ``text``, a non-string
    namespace/source_ref, a non-list ``tags`` (or non-string entry), a
    non-object ``metadata``, or a non-string/non-null ``occurred_start``/
    ``occurred_end`` — plus any non-null date STRING that does not parse the
    store's exact ``%Y-%m-%dT%H:%M:%SZ`` writer format (mirrors the sync
    validator's ISO rule: a malformed date string is a content failure, not
    a silent pass-through).
    """
    text = input_path.read_text(encoding="utf-8")
    records: list[dict] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line, parse_constant=_reject_json_constant,
                             object_pairs_hook=_json_object_no_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: malformed JSON: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {lineno}: record must be a JSON object")
        has_kind = "kind" in obj
        has_type = "type" in obj
        if has_kind == has_type:
            raise ValueError(
                f"line {lineno}: record must carry exactly one of "
                f"'kind' or 'type'")
        source_kind = obj["kind"] if has_kind else obj["type"]
        if source_kind not in _HINDSIGHT_KINDS:
            raise ValueError(
                f"line {lineno}: unsupported source kind {source_kind!r} "
                f"(accepted: {', '.join(_HINDSIGHT_KINDS)})")
        mid = obj.get("id")
        if not isinstance(mid, str) or not _INGEST_ID_RE.match(mid):
            raise ValueError(
                f"line {lineno}: 'id' must be a 36-char UUID-shaped string")
        if mid in seen_ids:
            raise ValueError(f"line {lineno}: duplicate id {mid}")
        seen_ids.add(mid)
        content = obj.get("text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"line {lineno}: 'text' must be a non-empty string")
        namespace = obj.get("namespace", "user:global")
        if not isinstance(namespace, str):
            raise ValueError(
                f"line {lineno}: 'namespace' must be a string when present")
        source_ref = obj.get("source_ref", f"hindsight:{mid}")
        if not isinstance(source_ref, str):
            raise ValueError(
                f"line {lineno}: 'source_ref' must be a string when present")
        tags_in = obj.get("tags", [])
        if not isinstance(tags_in, list) or any(
                not isinstance(t, str) for t in tags_in):
            raise ValueError(
                f"line {lineno}: 'tags' must be a list of strings when present")
        metadata = obj.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(
                f"line {lineno}: 'metadata' must be an object when present")
        occurred = {}
        for field in ("occurred_start", "occurred_end"):
            value = obj.get(field, None)
            if value is None:
                occurred[field] = None
                continue
            if not isinstance(value, str):
                raise ValueError(
                    f"line {lineno}: '{field}' must be a string or null")
            try:
                time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                raise ValueError(
                    f"line {lineno}: '{field}' is not a valid ISO-8601 UTC "
                    f"timestamp (expected YYYY-MM-DDTHH:MM:SSZ)") from None
            occurred[field] = value
        # Deterministic, kind-preserving tags: sorted set-union of the input
        # tags with the kind markers. Only tags are sorted — metadata keeps
        # its parsed key order verbatim (a canonical-bytes contract).
        extra = [f"hindsight:{source_kind}"]
        if source_kind == "observation":
            extra.append("hindsight:observation")
        if source_kind in ("kv", "current"):
            extra.append("state-candidate")
        if source_kind == "event":
            extra.append("hindsight:event")
        tags_out = ",".join(sorted(set(list(tags_in) + extra)))
        records.append({
            "kind": "memory",
            "id": mid,
            "namespace": namespace,
            "type": "fact",
            "content": content,
            "tags": tags_out,
            "source_ref": source_ref,
            "metadata": metadata,
            "occurred_start": occurred["occurred_start"],
            "occurred_end": occurred["occurred_end"],
        })
    if not records:
        raise ValueError("no records found in input")
    return records


def _serialize_canonical_record(record: dict) -> str:
    """One canonical JSONL line: fixed key order, compact separators,
    ensure_ascii=False, U+2028/U+2029/U+0085 escaped (mirrors sync.py's
    export serializer), LF-terminated."""
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    line = (line.replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029")
                .replace("\u0085", "\\u0085"))
    return line + "\n"


def run_hindsight_import(input_path: Path, dest_dir: Path,
                         force: bool = False) -> dict:
    """Import a Hindsight JSONL export into a destination directory.

    Fail-closed contract identical in spirit to run_import: the input is
    fully parsed and validated (including a pass/fail gate through
    ``_validate_sync_row`` — its return value is discarded; the canonical
    JSONL is serialized from the canonical records) BEFORE the destination
    is touched; the staged store is built in a staging directory INSIDE
    dest_dir and only ``os.replace``d into place after the strict ingest
    succeeds, the staged count equals the source count, and
    ``PRAGMA integrity_check`` reports ok. Every failure up to that point
    removes ONLY the
    staging directory — an existing destination stays byte-identical, and
    nothing is ever written outside dest_dir (the strict ingest lane raises
    on capture refusals instead of writing quarantine ledgers).

    Ingest runs through the strict all-or-nothing lane with
    ``capture_mode="auto"``: every row is redacted by the single capture
    policy, ids are preserved, and any refusal/malformed row fails the whole
    import (a documented deviation from the issue's literal legacy-lane
    wording — the legacy lane's quarantine ledger writes to
    ``dirname(STORE_PATH)``, outside this command's destination contract).

    Byte-identity scope: every PRE-ACCEPT failure (parse, validation,
    non-empty destination, staging, ingest, count, integrity) leaves an
    existing destination byte-identical and removes only the staging
    directory. The two final ``os.replace`` calls are not jointly atomic —
    exactly like run_import's store/core.md seam, a second-replace failure
    discloses that the store landed and heals via an idempotent ``--force``
    re-run. The canonical ``hindsight-import.jsonl`` is the VERBATIM-faithful
    canonicalization of the operator's input (metadata/occurrence
    authoritative); the STORE is the redacted surface — a credential in the
    input lands redacted in store rows but verbatim in the JSONL, which is
    written owner-only inside dest_dir after publication and reproduced
    verbatim by any re-import, so the file itself must be treated as secret
    material.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"input not found: {input_path}")
    input_size = input_path.stat().st_size
    if input_size > HINDSIGHT_MAX_INPUT_BYTES:
        raise ValueError(
            f"input is {input_size} bytes, over the "
            f"{HINDSIGHT_MAX_INPUT_BYTES} limit")
    records = _parse_hindsight_records(input_path)
    for record in records:
        # Pass/fail gate only: the validator normalizes into the
        # memory-shaped projection and drops metadata/occurred_*.
        _validate_sync_row(dict(record), None)

    if _host is not None:
        _host.assert_local_fs(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_store = dest_dir / "store.sqlite"
    dest_output = dest_dir / "hindsight-import.jsonl"
    if _existing_store_is_nonempty(dest_store) and not force:
        raise FileExistsError(
            f"destination store already exists and is non-empty: {dest_store} "
            f"(pass --force to overwrite)"
        )

    print(f"[import] source: {input_path}")
    print(f"[import] dest:   {dest_store}")

    canonical_text = "".join(
        _serialize_canonical_record(r) for r in records)
    output_sha256 = hashlib.sha256(
        canonical_text.encode("utf-8")).hexdigest()

    stage_dir = Path(tempfile.mkdtemp(prefix=".hindsight-181-", dir=dest_dir))
    staged_store = stage_dir / "store.sqlite"
    staged_output = stage_dir / "hindsight-import.jsonl"
    try:
        # newline="\n" pins LF at the byte level on Windows write side.
        with open(staged_output, "w", encoding="utf-8", newline="\n") as f:
            f.write(canonical_text)
        staged_conn = sqlite3.connect(str(staged_store))
        try:
            staged_conn.row_factory = sqlite3.Row
            init_db(staged_conn)
            migrate(staged_conn)
            staged_conn.commit()
            # The strict lane prints its own diagnostics; keep stdout clean
            # for the count/digest report lines below.
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cmd_ingest_jsonl_strict(
                    staged_conn,
                    in_path=str(staged_output),
                    source_ref=None,
                    allow_tombstones=False,
                    capture_mode="auto",
                )
            if rc != 0:
                raise RuntimeError(
                    "strict ingest failed (return code "
                    f"{rc}); nothing was committed to the destination")
            staged_conn.commit()
            row = staged_conn.execute(
                "SELECT COUNT(*) FROM memory").fetchone()
            destination_count = row[0]
            if destination_count != len(records):
                raise RuntimeError(
                    f"count mismatch: source_count={len(records)} but the "
                    f"staged store holds {destination_count} row(s) — a "
                    "capture refusal or dedup fold made the migration "
                    "non-deterministic; refusing")
            integrity = staged_conn.execute(
                "PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"staged store failed integrity_check: {integrity}")
            # Defensive: ensure no rollback-journal/WAL sidecar can race the
            # atomic replace (run_import precedent).
            staged_conn.execute("PRAGMA journal_mode=DELETE")
            staged_conn.commit()
        finally:
            staged_conn.close()
        # Accept (run_import's guarded-seam precedent, review round: the two
        # replaces are not jointly atomic). The destination's stale
        # -wal/-shm/-journal sidecars are stashed aside FIRST — left in place,
        # SQLite would replay them onto the newly replaced database and
        # silently resurrect the previous generation's rows (the same hazard
        # run_import guards at its own replace). If the OUTPUT replace fails
        # after the store landed, disclose exactly that — an access-denied on
        # the JSONL alone would hide a replaced destination store.
        store_replaced = False
        stashed: list[tuple[Path, Path]] = []
        try:
            stashed = _stash_dest_sidecars(dest_store)
            os.replace(staged_store, dest_store)
            store_replaced = True
            os.replace(staged_output, dest_output)
        except OSError as exc:
            if store_replaced:
                # Only the output swap failed; the store itself landed. The
                # stashed sidecars belong to the replaced-away store — drop
                # them, never re-home them beside the NEW store.
                for stash, _orig in stashed:
                    try:
                        stash.unlink()
                    except OSError:
                        pass
                raise OSError(
                    f"{exc}; the destination store WAS updated but writing "
                    f"{dest_output.name} failed — close any process holding "
                    "it open, then re-run this import with --force (the "
                    "import is idempotent) to restore the matched pair"
                ) from exc
            # Store not yet replaced: put the prior destination back exactly.
            for stash, orig in stashed:
                try:
                    os.replace(stash, orig)
                except OSError:
                    pass
            raise
        for stash, _orig in stashed:
            try:
                stash.unlink()
            except OSError:
                pass
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    if _host is not None:
        _host.set_owner_only_perms(dest_dir)
        _host.set_owner_only_perms(dest_store)
        _host.set_owner_only_perms(dest_output)

    print(f"[import] hindsight: source_count={len(records)} "
          f"destination_count={destination_count}")
    print(f"[import] hindsight: output_sha256={output_sha256}")
    print(f"[import] done: {dest_store}")

    return {
        "source_count": len(records),
        "destination_count": destination_count,
        "output_sha256": output_sha256,
        "dest": str(dest_store),
        "output": str(dest_output),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a legacy ZMem/ZCode store into the box-wide location")
    ap.add_argument("--source", required=True, help="legacy store path or the literal hindsight")
    ap.add_argument("--dest-dir", required=True, help="destination directory (e.g. ~/.zmem)")
    ap.add_argument("--input", dest="input_path", type=Path, default=None,
                    help="Hindsight JSONL export when --source hindsight")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty destination store")
    args = ap.parse_args()

    if args.source == "hindsight":
        if args.input_path is None:
            ap.error("--source hindsight requires --input")
    elif args.input_path is not None:
        ap.error("--input is only valid with --source hindsight")

    try:
        if args.source == "hindsight":
            run_hindsight_import(args.input_path.expanduser(),
                                 Path(args.dest_dir).expanduser(),
                                 force=args.force)
        else:
            run_import(Path(args.source).expanduser(),
                       Path(args.dest_dir).expanduser(), force=args.force)
    except Exception as e:
        print(f"[import] FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
