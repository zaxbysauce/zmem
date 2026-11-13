"""Metadata-only crash-recovery journal for knowledge publications.

The approved plan (2026-11-13) orders every publication: the INTENT is
durable BEFORE the create touches a target, so a crash between journal and
target (or between target and pointer) is always recoverable.  This module
owns that journal and nothing else:

- METADATA ONLY: identity (target id, logical key), the immutable locator,
  the digest, and a state.  The raw note body is NEVER accepted and NEVER
  persisted — there is no API here that takes content.  Predecessor
  chaining between versions of a note is the pointer worker's concern
  (its own sidecar carries predecessor ids); this journal keeps only
  identity / version / state.
- States: ``doc_pending`` (intent recorded) -> ``doc_verified`` (immutable
  version exists and read back) -> ``pointer_queued`` (pointer op handed to
  the store) -> ``applied`` (pointer landed).  ``quarantined`` is the
  escape hatch from any non-terminal state.  Transitions are validated;
  ``applied`` and ``quarantined`` are terminal.
- Version advance: once an identity's LATEST entry is terminal
  (``applied``/``quarantined``), :meth:`record_intent` accepts a NEW
  intent (a new digest — a new immutable version of the same note) and
  appends a fresh ``doc_pending`` entry; history per identity is retained,
  bounded by ``max_entries`` globally.  While the latest entry is still
  ACTIVE (non-terminal), a different digest is an explicit conflict, and
  the same digest is an idempotent return.  :meth:`mark` and
  :meth:`entry_for` coordinate on the LATEST entry; ``mark`` refuses when
  the latest entry is terminal (record a new intent first).
- Bounded: at most ``max_entries`` entries are retained (oldest dropped by
  sequence).
- Private + atomic + durable: entries live in one JSON file written to a
  same-directory temporary, flushed with ``fsync``, then moved onto the
  path with ``os.replace`` (atomic on POSIX and Windows).  The file is
  created 0600 where the platform honors it.
- Locking: an OS-level byte lock (``msvcrt.locking`` on Windows,
  ``fcntl.flock`` elsewhere) on a persistent ``<journal>.lock`` file,
  acquired NON-BLOCKING with a bounded retry.  A live lock is never
  deleted and never taken over by age — the kernel releases it
  automatically when its holder exits or crashes, which removes the
  unlink-a-fresh-lock race entirely.  ``stale_lock_s`` is retained in the
  constructor signature for compatibility and has no effect.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from knowledge.documents import (
    parse_locator,
    validate_logical_key,
)
from knowledge.locking import try_lock_nb as _try_lock_nb, unlock as _unlock

__all__ = [
    "JournalEntry",
    "JournalError",
    "KnowledgeJournal",
    "STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "default_journal_path",
]

STATES = (
    "doc_pending",
    "doc_verified",
    "pointer_queued",
    "applied",
    "quarantined",
)
TERMINAL_STATES = ("applied", "quarantined")
TRANSITIONS = {
    "doc_pending": ("doc_verified", "quarantined"),
    "doc_verified": ("pointer_queued", "quarantined"),
    "pointer_queued": ("applied", "quarantined"),
    "applied": (),
    "quarantined": (),
}

_JOURNAL_VERSION = 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_ENTRIES = 1000
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024  # metadata is small; refuse growth


class JournalError(RuntimeError):
    """A journal failure.  Safe detail only — never note content."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class JournalEntry:
    """One recorded intent's current recovery state (metadata only).

    An identity (namespace + target id + logical key) may accumulate
    entries over time — one per published version — ordered by ``seq``;
    the highest-``seq`` entry is the latest.  ``namespace`` defaults to
    ``""`` so journals written before the namespace became part of the
    identity load unchanged (their entries read as namespace ``""``)."""

    seq: int
    target_id: str
    logical_key: str
    ref: str
    locator: str
    digest: str
    state: str
    updated_at: str
    namespace: str = ""


class KnowledgeJournal:
    """The per-store knowledge intent journal."""

    def __init__(self, path: Path | str, *,
                 max_entries: int = _DEFAULT_MAX_ENTRIES,
                 lock_timeout_s: float = 5.0,
                 stale_lock_s: float = 30.0) -> None:
        """``stale_lock_s`` is accepted for signature compatibility and
        IGNORED: locking is an OS byte lock the kernel releases when its
        holder exits, so age-based takeover no longer exists (a live lock
        is never deleted)."""
        if max_entries < 1:
            raise JournalError("max_entries must be >= 1")
        if lock_timeout_s <= 0:
            raise JournalError("lock_timeout_s must be > 0")
        self._path = Path(path)
        self._max_entries = max_entries
        self._lock_timeout_s = lock_timeout_s

    # -- locking (OS byte lock, non-blocking + bounded retry, never unlinked)

    @property
    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    @contextlib.contextmanager
    def _locked(self):
        lock = self._lock_path
        try:
            # The parent exists BEFORE the lock file is opened: a DIRECT
            # constructor against a fresh nested path can take the lock at
            # all (previously only the first _write mkdir'd the parent, so
            # a first-ever record_intent on a fresh path failed to lock).
            with contextlib.suppress(OSError):
                lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise JournalError(
                "journal lock file unavailable (%s)" %
                exc.__class__.__name__) from exc
        deadline = time.monotonic() + self._lock_timeout_s
        while not _try_lock_nb(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise JournalError(
                    "journal lock busy after %.1fs (another writer holds "
                    "the OS lock; it is released automatically when that "
                    "writer exits): %r" %
                    (self._lock_timeout_s, str(lock)))
            time.sleep(0.03)
        try:
            yield
        finally:
            _unlock(fd)
            os.close(fd)

    # -- persistence (atomic, durable, private, bounded)

    def _read_entries(self) -> list[JournalEntry]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise JournalError(
                "journal unreadable (%s)" % exc.__class__.__name__) from exc
        if not raw:
            return []
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise JournalError("journal exceeds %d bytes; operator review "
                               "required" % _MAX_JOURNAL_BYTES)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise JournalError(
                "journal is not valid JSON; operator review required "
                "(refusing to reset silently)") from exc
        if (not isinstance(obj, dict) or obj.get("version") != _JOURNAL_VERSION
                or not isinstance(obj.get("entries"), list)):
            raise JournalError("journal shape unrecognized; operator review "
                               "required")
        entries: list[JournalEntry] = []
        for item in obj["entries"]:
            entries.append(self._entry_from_json(item))
        entries.sort(key=lambda e: e.seq)
        return entries

    @staticmethod
    def _entry_from_json(item: object) -> JournalEntry:
        if not isinstance(item, dict):
            raise JournalError("journal entry is not an object")
        try:
            entry = JournalEntry(
                seq=int(item["seq"]),
                target_id=str(item["target_id"]),
                logical_key=str(item["logical_key"]),
                ref=str(item["ref"]),
                locator=str(item["locator"]),
                digest=str(item["digest"]),
                state=str(item["state"]),
                updated_at=str(item["updated_at"]),
                namespace=str(item.get("namespace", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalError("journal entry malformed") from exc
        if entry.state not in STATES or entry.seq < 1:
            raise JournalError("journal entry state/seq invalid")
        return entry

    def _write_entries(self, entries: list[JournalEntry]) -> None:
        entries = sorted(entries, key=lambda e: e.seq)[-self._max_entries:]
        payload = json.dumps({
            "version": _JOURNAL_VERSION,
            "entries": [entry.__dict__ for entry in entries],
        }, indent=1).encode("utf-8")
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise JournalError(
                "journal would exceed %d bytes; refusing to write" %
                _MAX_JOURNAL_BYTES)
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JournalError(
                "journal directory unavailable (%s)" %
                exc.__class__.__name__) from exc
        tmp = self._path.with_name(
            "%s.tmp-%d-%s" % (self._path.name, os.getpid(),
                              os.urandom(4).hex()))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(str(tmp), str(self._path))
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise JournalError(
                "journal replace failed (%s)" %
                exc.__class__.__name__) from exc

    # -- validation

    @staticmethod
    def _validate_identity(target_id: str, logical_key: str, locator: str,
                           digest: str) -> tuple[str, str]:
        from knowledge.config import validate_target_id
        from knowledge.documents import logical_ref

        try:
            validate_target_id(target_id)
            validate_logical_key(logical_key)
        except ValueError as exc:
            raise JournalError("invalid identity: %s" % exc) from exc
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise JournalError("digest must be 64 lowercase hex characters")
        try:
            key_from_locator, digest_from_locator = parse_locator(locator)
        except ValueError as exc:
            raise JournalError("locator malformed: %s" % exc) from exc
        if key_from_locator != logical_key or digest_from_locator != digest:
            raise JournalError(
                "locator does not address the given logical key and digest")
        try:
            ref = logical_ref(target_id, logical_key)
        except ValueError as exc:
            raise JournalError("invalid identity: %s" % exc) from exc
        return ref, locator

    # -- entry lookup

    @staticmethod
    def _latest(entries: list[JournalEntry], target_id: str,
                logical_key: str, namespace: str = "") -> JournalEntry | None:
        """The highest-``seq`` entry for an identity, or None.  The
        identity is the FULL (namespace, target id, logical key) tuple —
        two namespaces publishing the same target/key are two identities."""
        latest: JournalEntry | None = None
        for entry in entries:
            if (entry.namespace, entry.target_id,
                    entry.logical_key) == (namespace, target_id,
                                           logical_key):
                if latest is None or entry.seq >= latest.seq:
                    latest = entry
        return latest

    # -- public operations

    def record_intent(self, *, target_id: str, logical_key: str,
                      locator: str, digest: str,
                      namespace: str = "") -> JournalEntry:
        """Durably record the intent to publish, BEFORE any create.

        ``namespace`` (optional, default ``""``) completes the identity:
        (namespace, target id, logical key).  Omitted on legacy journals it
        keeps the pre-namespace behavior, so existing callers and stored
        journals are unaffected.

        Semantics against the identity's LATEST entry:
        - same digest and locator -> idempotent return of that entry,
          whatever its state (crash resume re-supplies the same screened
          input; a completed version is not re-opened);
        - latest entry still ACTIVE (non-terminal) with a different digest
          -> explicit :class:`JournalError` conflict (a version is in
          flight; it must be driven to a terminal state or quarantined
          first);
        - latest entry TERMINAL (applied/quarantined) -> a NEW
          ``doc_pending`` intent is appended (the next version of the
          note; the pointer worker links predecessors via its sidecar).
        History is retained, bounded globally by ``max_entries``.
        """
        ref, locator = self._validate_identity(target_id, logical_key,
                                               locator, digest)
        self._validate_namespace(namespace)
        with self._locked():
            entries = self._read_entries()
            latest = self._latest(entries, target_id, logical_key, namespace)
            if latest is not None and latest.digest == digest \
                    and latest.locator == locator:
                return latest
            if latest is not None and latest.state not in TERMINAL_STATES:
                raise JournalError(
                    "identity %r has an ACTIVE intent in state %r "
                    "(recorded digest %s..); drive it to a terminal state "
                    "or quarantine it before recording a different version"
                    % (ref, latest.state, latest.digest[:8]))
            seq = max((e.seq for e in entries), default=0) + 1
            entry = JournalEntry(
                seq=seq,
                target_id=target_id,
                logical_key=logical_key,
                ref=ref,
                locator=locator,
                digest=digest,
                state="doc_pending",
                updated_at=_utc_now(),
                namespace=namespace,
            )
            entries.append(entry)
            self._write_entries(entries)
            return entry

    def mark(self, *, target_id: str, logical_key: str,
             state: str, namespace: str = "") -> JournalEntry:
        """Advance the identity's LATEST ACTIVE entry along the validated
        transitions.

        The latest entry must be non-terminal: after ``applied`` or
        ``quarantined`` there is nothing active to advance — record a new
        intent first.  Marking the current state is an idempotent no-op.
        """
        if state not in STATES:
            raise JournalError("unknown state %r (known: %s)" %
                               (state, ", ".join(STATES)))
        self._validate_namespace(namespace)
        with self._locked():
            entries = self._read_entries()
            latest = self._latest(entries, target_id, logical_key, namespace)
            if latest is None:
                raise JournalError(
                    "no journal entry for target %r key %r; record_intent "
                    "must come first" % (target_id, logical_key))
            if latest.state in TERMINAL_STATES:
                raise JournalError(
                    "identity %r has no ACTIVE entry (latest state %r at "
                    "seq %d); record_intent for the next version first" %
                    (latest.ref, latest.state, latest.seq))
            if latest.state == state:
                return latest
            if state not in TRANSITIONS[latest.state]:
                raise JournalError(
                    "invalid transition %r -> %r for identity %r" %
                    (latest.state, state, latest.ref))
            index = entries.index(latest)
            updated = JournalEntry(
                seq=latest.seq,
                target_id=latest.target_id,
                logical_key=latest.logical_key,
                ref=latest.ref,
                locator=latest.locator,
                digest=latest.digest,
                state=state,
                updated_at=_utc_now(),
                namespace=latest.namespace,
            )
            entries[index] = updated
            self._write_entries(entries)
            return updated

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if not isinstance(namespace, str) or len(namespace) > 256:
            raise JournalError(
                "namespace must be a string of at most 256 characters")

    def entry_for(self, target_id: str, logical_key: str,
                  namespace: str = "") -> JournalEntry | None:
        """The LATEST entry for an identity (any state), or None."""
        with self._locked():
            return self._latest(self._read_entries(), target_id,
                                logical_key, namespace)

    def entries(self) -> tuple[JournalEntry, ...]:
        with self._locked():
            return tuple(self._read_entries())

    def pending(self) -> tuple[JournalEntry, ...]:
        """Entries not yet terminal (``applied``/``quarantined``).  With
        the one-active-intent-per-identity invariant this is at most one
        entry per identity."""
        return tuple(e for e in self.entries()
                     if e.state not in TERMINAL_STATES)


def default_journal_path() -> Path:
    """``<store_dir>/knowledge/journal.json`` — beside the store config,
    resolved through the same store-dir chain as ``storelib.config``."""
    from storelib.config import config_path

    return config_path().parent / "knowledge" / "journal.json"
