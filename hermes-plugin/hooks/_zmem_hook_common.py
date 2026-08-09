"""Shared helpers for the zmem Hermes hooks.

The three Hermes hooks (convention / reflect / verify) each previously carried
their own copy of ``_assert_local_fs`` with identical logic and a copy-pasted
sys.path probe. That triplication drifted (one copy had fuller docstrings than
the other two) and the dead-looking ``except Exception: return True`` branch
invited "clean-up" that would have been a regression (see below). This module
is the single shared copy they all import (#37 L25).

Import contract: hooks run as standalone scripts, so a hook MUST put its own
directory on ``sys.path`` before importing this sibling module, e.g.::

    import os, sys
    _HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
    if _HOOK_DIR not in sys.path:
        sys.path.insert(0, _HOOK_DIR)
    from _zmem_hook_common import assert_local_fs  # noqa: E402

Keep this module dependency-free (stdlib only) and side-effect-free at import
so it is cheap and safe to load from every hook.
"""

from __future__ import annotations

import sys
from pathlib import Path


def assert_local_fs(path: Path) -> bool:
    """Reject UNC/network/OneDrive store paths (WAL-corruption guard).

    Mirrors zmem's host.py ``assert_local_fs`` — the Hermes provider gets the
    guard for free (it shells out to store.py), but these hooks open sqlite
    directly and would otherwise bypass it. Returns True if safe, False if the
    path is network-mounted (the hook silently no-ops rather than corrupting
    the store).

    host.py's ``assert_local_fs`` raises ``ValueError`` as its refusal signal
    (host.py:253-284) — that MUST map to ``return False``, not be swallowed.
    Import-failure (host.py absent) degrades to the UNC-prefix check only.

    The trailing ``except Exception: return True`` (fail-open) is INTENTIONAL
    and must NOT be deleted as "dead code". host.py's Windows guards call into
    ``ctypes`` / ``GetDriveTypeW`` (``_drive_type_is_remote``), which can raise
    ``OSError`` on transient conditions (drive unavailable, locked token,
    permission denied on the drive root) that are not a refusal signal. A hook
    wedging session-start on such an error is a worse failure than allowing the
    write through — the whole hook subsystem is fail-open by design
    (``hooks/zmem-session-start.sh``: "recall/maintenance errors never block
    session start"). This branch keeps that contract even if host.py's
    exception surface widens later. (#37 L25)
    """
    s = str(path)
    if s.startswith("\\\\") or s.startswith("//"):
        return False
    try:
        _scripts_dir = str(
            Path(__file__).resolve().parent.parent.parent
            / "skills" / "memory" / "scripts"
        )
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        import host  # type: ignore[import-not-found]
    except Exception:
        # host.py absent — best-effort UNC check only.
        return not (s.startswith("\\\\") or s.startswith("//"))
    # host.py imported — honor its refusal (ValueError) as False.
    try:
        host.assert_local_fs(path)
        return True
    except ValueError:
        # host.py refused: OneDrive, network-mapped drive, etc.
        return False
    except Exception:
        # Unexpected guard error — fail open (never wedge the hook). See the
        # docstring: this is intentional defensive belt-and-suspenders, NOT
        # dead code.
        return True
