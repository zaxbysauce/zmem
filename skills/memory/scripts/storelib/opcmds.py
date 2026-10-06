"""Rendered operator-facing ``store.py`` command prefixes (issue #259).

Every operator-facing suggestion of a ``store.py`` command — the hygiene
report's upgrade action plan, the schema dimension-mismatch fix hint, the
backup rollback hints — must name an existing interpreter executable
(shell-quoted) ahead of the script path, exactly like the hook-injected
commands. A bare ``store.py`` token depends entirely on the caller's shell
resolving ``python`` (no shebang), which on Windows can be the Store stub;
a bare ``python`` token has the same hazard. The interpreter rendered is the
one the store process itself ran under, and the script path is resolved from
this file's location so the command is runnable from any working directory.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

STORE_PY = str(Path(__file__).resolve().parents[1] / "store.py")


def command_prefix() -> str:
    """Shell-quoted ``<interpreter> <store.py>`` prefix for copy-paste
    commands (both tokens quoted: space-safe on every platform)."""
    return shlex.quote(sys.executable) + " " + shlex.quote(STORE_PY)
