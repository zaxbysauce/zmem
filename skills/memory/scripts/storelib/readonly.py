"""WAL/journal-aware read-only store opens (issue #345).

``?mode=ro`` is not a no-side-effect open on a WAL store: SQLite creates the
``-wal``/``-shm`` sidecars (and mutates ``-shm`` read-marks) to service any
read, so a "read-only" surface can write to the operator's data directory.
This module generalizes the policy the repo converged on twice
(decision_report.py, rekey.py):

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

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


class ReadOnlyStoreError(RuntimeError):
    """A read-only open refused the store (unreadable or torn copy)."""


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
                  lock_aware: bool = False) -> ReadOnlyHandle:
    """Open an existing store strictly read-only, sidecar-free.

    Raises FileNotFoundError when the store does not exist, and
    ReadOnlyStoreError when a residue copy is torn/unreadable. The caller
    owns cleanup: ``handle.close()`` in a finally block.

    ``lock_aware=True`` (fail-closed surfaces, e.g. doctor's expiry scan —
    issue #342): a NON-WAL store with no journal residue opens through a
    plain ``?mode=ro`` connection instead of immutable. On a non-WAL store a
    reader never creates ``-wal``/``-shm`` (they are WAL machinery), so the
    sidecar-free guarantee holds — AND the open stays lock-observable: a
    ``BEGIN EXCLUSIVE`` holder fails the read with OperationalError, which
    the immutable/copy arms structurally cannot surface. WAL-mode stores
    always take the snapshot arms (readers do not block writers in WAL, and
    the arm-B notice discloses the snapshot).
    """
    store = Path(store_path)
    if not store.is_file():
        raise FileNotFoundError(store)
    wal = Path(str(store) + "-wal")
    journal = Path(str(store) + "-journal")
    has_residue = wal.exists() or journal.exists()

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
        conn = sqlite3.connect(
            store.resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA busy_timeout=5000")
        return ReadOnlyHandle(conn, None)

    # Arm B: read a private copy so the operator's directory is untouched.
    print("[zmem] readonly: store has WAL/journal residue; "
          "read a private snapshot", file=sys.stderr)
    stage = Path(tempfile.mkdtemp(prefix="zmem-readonly-copy-"))
    conn = None
    try:
        staged_store = stage / store.name
        shutil.copyfile(store, staged_store)
        for residue in (wal, journal):
            if residue.exists():
                shutil.copyfile(residue, Path(str(staged_store)
                                              + residue.name[len(store.name):]))
        # A normal (writable) connection on the PRIVATE copy lets hot-journal
        # / WAL recovery complete there; query_only pins every statement.
        conn = sqlite3.connect(str(staged_store), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        conn.execute("PRAGMA busy_timeout=5000")
        check = conn.execute("PRAGMA quick_check").fetchone()
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
