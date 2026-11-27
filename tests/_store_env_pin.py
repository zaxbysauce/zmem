"""Shared, process-wide capture of the ORIGINAL store environment.

TG-4 (the round-1 review comment): two store-touching test modules each pin
ZMEM_STORE/ZMEM_DATA (and friends) at import, and the guard judges the
environment at ITS import. The problem with a per-module snapshot is that
whichever module is imported SECOND snapshots the FIRST's scratch values, so
its restore would put a scratch path back instead of the operator's
original environment.

This module captures the original values ONCE per process and hands every
module the same snapshot, so the restore is original-based regardless of
import order. It also refcounts the pins: a module's teardown restores the
originals only when the LAST pinning module has torn down, because a
middle module's teardown must not strip a pin a later module's tests still
need (that would point them at the operator's real store).

Restoring at process exit alone is not enough either: a sibling module
imported into the same process inherits the pins, and the restore must
happen at MODULE teardown so the next module starts from the environment it
expects.
"""
from __future__ import annotations

import atexit
import os

_ORIGINAL: dict[str, str | None] = {}
_ACTIVE: set[int] = set()
_NEXT_TOKEN = 0


def acquire(keys) -> int:
    """Record the ORIGINAL value of every key (once per process) and hold a
    pin.

    The token is UNIQUE per acquisition (two modules pinning the SAME keys
    must not collapse into one holder -- otherwise the first release would
    restore the environment while the other holder is still active), and a
    release is idempotent.
    """
    global _NEXT_TOKEN
    for k in keys:
        if k not in _ORIGINAL:
            _ORIGINAL[k] = os.environ.get(k)
    _NEXT_TOKEN += 1
    token = _NEXT_TOKEN
    _ACTIVE.add(token)
    return token


@atexit.register
def _restore_at_exit():
    """Process-exit backstop: a module that only IMPORTED another pinning
    module never runs that module's teardown, so the original environment
    is restored here as well."""
    for k, v in _ORIGINAL.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def release(token) -> None:
    """Release this module's pin; restore the original environment once the
    last pinning module has released.

    The restore covers the UNION of every key any module captured (the two
    modules pin different key sets), and releasing a token twice is a no-op.
    """
    if token not in _ACTIVE:
        return
    _ACTIVE.discard(token)
    if _ACTIVE:
        return
    for k, v in _ORIGINAL.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
