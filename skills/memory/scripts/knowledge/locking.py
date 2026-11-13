"""The OS byte-lock discipline shared by the knowledge sidecars.

One implementation for the journal and the delivery sidecar (previously
two byte-identical private copies): a NON-BLOCKING exclusive lock on one
byte of a PERSISTENT lock file — ``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere.  A live lock is never deleted and never taken
over by age; the kernel releases it automatically when its holder exits
or crashes, which removes the unlink-a-fresh-lock race entirely.  The
caller owns opening the lock file (creating its parent directory first)
and the bounded retry loop; these helpers only make/break the lock.
"""

from __future__ import annotations

import os

if os.name == "nt":  # pragma: no branch - platform selection
    import msvcrt
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

__all__ = [
    "try_lock_nb",
    "unlock",
]


def try_lock_nb(fd: int) -> bool:
    """Attempt one non-blocking exclusive byte lock.  Never deletes or
    overwrites anything; returns False when another writer holds it."""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - POSIX hosts
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


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
