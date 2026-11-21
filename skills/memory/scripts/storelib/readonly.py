"""WAL/journal-aware read-only store opens (issue #345).

``?mode=ro`` is not a no-side-effect open on a WAL store: SQLite creates the
``-wal``/``-shm`` sidecars (and mutates ``-shm`` read-marks) to service any
read, so a "read-only" surface can write to the operator's data directory.
This module is the THIRD read-only open in the repo — its copy-and-verify
policy is derived from the two prior implementations (decision_report.py,
rekey.py) but deliberately differs where noted (arm B opens the private
copy with a NORMAL connection so recovery completes, where
decision_report documents a read-only open as load-bearing; the guard is
``quick_check`` here vs decision_report's ``integrity_check``). The two
prior modules are NOT refactored onto this one (their callers pin
different consistency contracts); unifying them is tracked as design
follow-up, not silently claimed:

* Arm A — the store is quiesced (no ``-wal`` AND no ``-journal`` file):
  open ``?mode=ro&immutable=1``. Correct (nothing to recover or miss) and
  creates nothing. The journal check is load-bearing: with a hot rollback
  journal, ``immutable=1`` skips recovery and can read uncommitted pages
  already spilled into the main file.
* Arm B — any residue (``-wal`` and/or ``-journal``, including a zero-length
  ``-wal``): stage a PRIVATE copy of the store plus whichever residue files
  exist (never ``-shm`` — it is derived state, and copying it raised
  PermissionError under a live writer on Windows) in a throwaway temp dir
  OUTSIDE the operator's data dir, open the copy with a normal connection so
  hot-journal/WAL recovery completes on the private copy, then immediately
  pin it with ``PRAGMA query_only=1``. The operator's directory is never
  touched beyond the initial read of the bytes being copied.

Arm B reads a private SNAPSHOT: under a live writer the copy is not atomic
(decision_report's disclosed caveat), so the copy is guarded by
``PRAGMA quick_check`` and a torn copy refuses with :class:`ReadOnlyStoreError`
instead of serving garbage. When arm B is taken a one-line notice goes to
stderr; stdout contracts (e.g. classify-state's byte-pinned JSON) are never
touched.

Cleanup is explicit — there is deliberately NO weakref finalizer
(``weakref.finalize`` cannot reference ``sqlite3.Connection`` on this
runtime, and a finalizer that skips close-then-remove silently leaks a full
copy of the store). :meth:`ReadOnlyHandle.close` closes the connection FIRST,
then removes the staging dir, and reports a loud leftover warning when
anything survives (decision_report's ``_discard_stage`` contract).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


class ReadOnlyStoreError(RuntimeError):
    """A read-only open refused the store (unreadable or torn copy)."""


def _snapshot_digest(path: Path) -> bytes:
    """Fingerprint a staged main file with memory independent of its size."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _is_wal_store(store: Path) -> bool:
    """True when the database header declares WAL journal mode.

    Read from the raw header (bytes 18-19 == 0x02 0x02) so the check itself
    opens nothing and creates no sidecars.
    """
    try:
        with open(store, "rb") as fh:
            head = fh.read(20)
    except OSError:
        return False
    return len(head) >= 20 and head[18:20] == b"\x02\x02"


_STAGE_PREFIX = "zmem-readonly-copy-"
_STAGE_TTL_S = 3600.0


def _sweep_stale_stages(now: float | None = None) -> None:
    """Reclaim staging directories abandoned by hard-killed readers.

    Cleanup otherwise lives only in-process (close/except), which a
    TerminateProcess/SIGKILL never runs, so a full plaintext copy of the
    operator's store would survive in the shared temp dir forever. Bounded:
    only this module's prefix, only directories older than the TTL, and
    removal failures are ignored (a still-locked dir is retried next open).
    """
    import time as _time

    now = _time.time() if now is None else now
    root = Path(tempfile.gettempdir())
    try:
        entries = list(root.glob(_STAGE_PREFIX + "*"))
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            if now - entry.stat().st_mtime <= _STAGE_TTL_S:
                continue
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _discard_stage(stage: Path | None) -> str | None:
    """Remove a staging directory, reporting whether it actually went away.

    ``rmtree(ignore_errors=True)`` cannot distinguish "removed" from
    "silently refused", so a leftover full copy of the operator's store
    would be invisible. Returns a warning when anything survived, None on a
    clean removal. (Same contract as decision_report._discard_stage.)
    """
    if stage is None:
        return None
    shutil.rmtree(stage, ignore_errors=True)
    if os.path.isdir(stage):
        return ("a temporary copy of the store staged at %s could not be "
                "removed and still holds the operator's memory rows; delete "
                "that directory by hand" % stage)
    return None


class ReadOnlyHandle:
    """A read-only connection plus its cleanup owner.

    The handle PROXIES the underlying connection (``execute``, ``cursor``,
    ``row_factory`` … via ``__getattr__``/``__setattr__``), so existing
    ``conn = open_...(); conn.execute(...); conn.close()`` call sites work
    unchanged — with the one contract change that ``close()`` ALSO removes
    the arm-B staging directory (close-then-remove; an open handle makes
    rmtree fail silently on Windows). ``handle.conn`` exposes the raw
    connection and ``handle.close()`` is idempotent. New call sites should
    prefer ``try: handle... finally: handle.close()``.
    """

    __slots__ = ("_conn", "_stage", "_closed")

    def __init__(self, conn: sqlite3.Connection, stage: Path | None):
        self._conn = conn
        self._stage = stage
        self._closed = False

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def __getattr__(self, name):
        # Delegate everything connection-like to the underlying conn; the
        # slots (_conn/_stage/_closed) never reach here.
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ReadOnlyHandle.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def close(self) -> None:
        """Close the connection, then remove any staging directory.

        Idempotent. Never raises for the close; a surviving staging
        directory is reported loudly on stderr instead of being silently
        leaked.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        leftover = _discard_stage(self._stage)
        if leftover:
            print("[zmem] readonly: %s" % leftover, file=sys.stderr)


def open_readonly(store_path: Path | str,
                  lock_aware: bool = False,
                  shallow: bool = False) -> ReadOnlyHandle:
    """Open an existing store strictly read-only, sidecar-free.

    Raises FileNotFoundError when the store does not exist, and
    ReadOnlyStoreError when the store cannot be served sidecar-free:
    a residue copy is torn/unreadable, ``lock_aware`` finds residue (the
    fail-closed contract — issue #342 — holds on residue stores too, not
    only non-WAL holders), or ``shallow=True`` finds residue (shallow
    callers, e.g. the per-recall embedding-health probe, must never stage a
    store-sized copy on a seconds-long budget). The caller owns cleanup:
    ``handle.close()`` in a finally block.

    ``lock_aware=True`` (fail-closed surfaces, e.g. doctor's expiry scan —
    issue #342): a NON-WAL store with no journal residue opens through a
    plain ``?mode=ro`` connection instead of immutable. On a non-WAL store a
    reader never creates ``-wal``/``-shm`` (they are WAL machinery), so the
    sidecar-free guarantee holds — AND the open stays lock-observable: a
    ``BEGIN EXCLUSIVE`` holder fails the read with OperationalError, which
    the immutable/copy arms structurally cannot surface. A residue store
    under ``lock_aware`` RAISES rather than silently serving a private
    snapshot (the flag's fail-closed contract outranks the snapshot arms).
    WAL-mode stores without residue still take the snapshot arms (readers
    do not block writers in WAL, and the arm-B notice discloses the
    snapshot).
    """
    store = Path(store_path)
    if not store.is_file():
        raise FileNotFoundError(store)
    wal = Path(str(store) + "-wal")
    journal = Path(str(store) + "-journal")

    def _residue_exists() -> bool:
        return wal.exists() or journal.exists()

    # Reclaim staging dirs abandoned by hard-killed readers before possibly
    # staging a new one (bounded sweep, see _sweep_stale_stages).
    _sweep_stale_stages()

    has_residue = _residue_exists()

    def _refuse_on_residue() -> None:
        # Reads the CURRENT has_residue, so the re-check fall-throughs below
        # re-apply the same contracts the pre-open checks made (review
        # delta re-gate: a late-residue race must not bypass shallow or
        # lock_aware by falling through to arm B).
        if lock_aware and has_residue:
            # Fail-closed contract (issue #342): a lock-observable read must
            # not silently degrade to a private snapshot when a
            # holder/residue is present — surface it instead.
            raise ReadOnlyStoreError(
                "store has WAL/journal residue; the lock-aware fail-closed "
                "read refuses to serve a private snapshot (retry when the "
                "store is quiet)")
        if shallow and has_residue:
            # Shallow callers (per-recall probe) must never stage a
            # store-sized copy on a seconds-long budget; refuse and let the
            # caller report degraded.
            raise ReadOnlyStoreError(
                "store has WAL/journal residue; the shallow read refuses to "
                "stage a full private copy (retry when the store is quiet)")

    _refuse_on_residue()

    if (lock_aware and not has_residue and not _is_wal_store(store)):
        conn = sqlite3.connect(
            store.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA busy_timeout=5000")
        return ReadOnlyHandle(conn, None)

    if not has_residue:
        # Arm A: nothing to recover, nothing to miss — immutable reads the
        # same bytes a mode=ro open would, without materializing sidecars.
        # The residue re-check is load-bearing: a writer that created
        # -wal/-journal between the first probe and this open would make the
        # immutable read silently miss committed rows, so fall through to
        # arm B instead (issue-review 422R-005) — re-applying the shallow /
        # lock_aware refusals at the new state first (delta re-gate).
        if _residue_exists():
            has_residue = True
            _refuse_on_residue()
        else:
            conn = sqlite3.connect(
                store.resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=1")
            conn.execute("PRAGMA busy_timeout=5000")
            check = conn.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]).lower() != "ok":
                conn.close()
                raise ReadOnlyStoreError(
                    "store failed the read-only consistency check "
                    "(quick_check: %s)" % (check[0] if check else "no result"))
            # After-open residue re-check (422R-005 residual, final-critic):
            # a writer that created -wal/-journal between the pre-open probe
            # and this connection would be invisible to the immutable read.
            # Fall through to arm B instead of serving possibly-stale rows —
            # re-applying the shallow / lock_aware refusals at the new state
            # first (delta re-gate: the guards key on has_residue, so a
            # fall-through without them would stage a store-sized copy on a
            # shallow per-prompt caller or let lock_aware serve a snapshot).
            if _residue_exists():
                conn.close()
                has_residue = True
                _refuse_on_residue()
            else:
                return ReadOnlyHandle(conn, None)

    # Arm B: read a private copy so the operator's directory is untouched.
    print("[zmem] readonly: store has WAL/journal residue; "
          "read a private snapshot", file=sys.stderr)
    stage = Path(tempfile.mkdtemp(prefix="zmem-readonly-copy-"))
    conn = None
    try:
        staged_store = stage / store.name
        # Copy consistency (issue-review 422R-004): a checkpoint+close that
        # lands between the main-file copy and the residue copy resets or
        # removes the residue, so replaying it onto the pre-checkpoint main
        # copy would silently omit committed rows while quick_check stays
        # ok. Detect it: re-copy the main file after the residue and refuse
        # (bounded retries) when the two main copies differ — a difference
        # proves the main file changed mid-copy.
        for _attempt in range(3):
            # Review round 2 probe: a stale staged -journal from a previous
            # attempt is auto-replayed by SQLite onto the fresh main copy,
            # rolling back pages committed after that journal was written —
            # clear the staged sidecars before every attempt so the served
            # main is opened alone.
            for residue in (wal, journal):
                staged_residue = Path(str(staged_store)
                                      + residue.name[len(store.name):])
                if staged_residue.exists():
                    os.remove(str(staged_residue))
            shutil.copyfile(store, staged_store)
            first_digest = _snapshot_digest(staged_store)
            for residue in (wal, journal):
                if residue.exists():
                    shutil.copyfile(
                        residue,
                        Path(str(staged_store)
                             + residue.name[len(store.name):]))
            if not _residue_exists():
                # Residue vanished mid-copy: the committed frames landed in
                # the main file (checkpoint) or were discarded (rollback).
                # A fresh main copy is authoritative either way — but only
                # if the main file itself stopped moving.
                shutil.copyfile(store, staged_store)
                if _snapshot_digest(staged_store) == first_digest:
                    break
                continue
            verify_path = Path(str(staged_store) + ".verify")
            shutil.copyfile(store, verify_path)
            moved = _snapshot_digest(verify_path) != first_digest
            os.remove(str(verify_path))
            if not moved:
                break
        else:
            raise ReadOnlyStoreError(
                "store kept changing while the private snapshot was staged; "
                "retry when the store is quiet")
        # A normal (writable) connection on the PRIVATE copy lets hot-journal
        # / WAL recovery complete there; query_only pins every statement.
        conn = sqlite3.connect(str(staged_store), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            # A torn copy (truncated/malformed pages) makes quick_check
            # RAISE DatabaseError instead of returning a bad string — the
            # same refusal contract, same message shape (422R-010: the
            # torn-copy refusal is the error class cli.py maps to "store
            # unreadable").
            raise ReadOnlyStoreError(
                "store residue produced an unreadable private copy "
                "(%s); retry when the store is quiet" % exc) from exc
        if check is None or str(check[0]).lower() != "ok":
            raise ReadOnlyStoreError(
                "store residue produced an unreadable private copy "
                "(quick_check: %s); retry when the store is quiet"
                % (check[0] if check else "no result"))
        return ReadOnlyHandle(conn, stage)
    except BaseException:
        # Close BEFORE removing (Windows open-handle semantics), and carry
        # any leftover-staging disclosure on the refusal/exception path.
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        leftover = _discard_stage(stage)
        if leftover:
            print("[zmem] readonly: %s" % leftover, file=sys.stderr)
        raise
