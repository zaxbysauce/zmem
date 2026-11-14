"""The OS byte-lock discipline shared by the knowledge sidecars.

One implementation for the journal and the delivery sidecar (previously
two byte-identical private copies): a NON-BLOCKING exclusive lock on one
byte of a PERSISTENT lock file — ``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere.  A live lock is never deleted and never taken
over by age; the kernel releases it automatically when its holder exits
or crashes, which removes the unlink-a-fresh-lock race entirely.  The
caller owns opening the lock file (creating its parent directory first)
and the bounded retry loop; these helpers only make/break the lock.

Contention vs fatal (REL-015): ``try_lock_nb`` returns False ONLY for
lock contention — the OSError errno the platform's lock call uses to
say "another writer holds it" (``EACCES``/``EDEADLK`` from
``msvcrt.locking`` on Windows; ``EAGAIN``/``EWOULDBLOCK``/``EACCES``
from ``fcntl.flock`` elsewhere).  Every other OSError (a closed or
otherwise unusable descriptor — ``EBADF``/``EINVAL``/``ENOLCK``/``EPERM``
— or a lock region the platform refuses outright) is FATAL and raises
:class:`LockError`, a content-free typed error: the callers' bounded
retry loops spin only on real contention, and a broken lock setup
refuses loudly instead of masquerading as busy (the old blanket
``except OSError: return False`` let fatal conditions masquerade as
contention and callers refused with the wrong reason).
"""

from __future__ import annotations

import errno
import os

if os.name == "nt":  # pragma: no branch - platform selection
    import msvcrt
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

__all__ = [
    "LockError",
    "try_lock_nb",
    "unlock",
]


class LockError(RuntimeError):
    """A FATAL failure of the byte-lock discipline itself (content-free:
    never paths, never note content).  Lock CONTENTION is not an error —
    ``try_lock_nb`` reports it as ``False`` for the retry loops."""


# The errno values that mean CONTENTION per platform branch: the lock is
# held by another writer and a bounded retry is the correct response.
# Windows ``msvcrt.locking`` reports a held-lock violation as EACCES
# (winerror 33, ERROR_LOCK_VIOLATION) or EDEADLK; POSIX ``flock`` with
# LOCK_NB raises BlockingIOError carrying EAGAIN/EWOULDBLOCK/EACCES.
# EINTR is retryable the same way (the attempt was interrupted before it
# could say anything about the lock).
_CONTENTION_ERRNOS = frozenset((
    errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK,
    errno.EINTR,
))


def try_lock_nb(fd: int) -> bool:
    """Attempt one non-blocking exclusive byte lock.  Never deletes or
    overwrites anything; returns False when another writer holds it.
    A FATAL OSError (a descriptor that cannot be locked at all) raises
    :class:`LockError` instead of masquerading as contention."""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - POSIX hosts
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _CONTENTION_ERRNOS:
            return False
        raise LockError(
            "knowledge sidecar byte lock failed (%s)"
            % exc.__class__.__name__) from exc


def unlock(fd: int) -> None:
    """Release the byte lock (best-effort; the kernel also releases it
    when the holder exits)."""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - POSIX hosts
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
