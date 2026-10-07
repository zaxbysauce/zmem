"""Deterministic git-checkout fixture for the issue #161 hermes-config tests.

Loaded by path (``tests/fixtures`` is not a package; sibling tests load
fixtures the same way).  No side effects at import.  ``make_checkout``
creates ONLY git metadata — one ``git init`` plus one ``git remote add
origin`` — so namespace derivation from the checkout is a pure function of
``origin_url``: no tracked file, no commit, no store row, no memory id, no
timestamp.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_checkout(path: str | Path, origin_url: str) -> tuple[Path, str]:
    """Create a git checkout at ``path`` whose origin is ``origin_url``.

    Runs exactly ``git init -q --initial-branch main <path>`` and
    ``git -C <path> remote add origin <origin_url>`` (list argv, Windows
    safe).  Returns ``(checkout_path, requested_url)``.
    """
    checkout = Path(path)
    checkout.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--initial-branch", "main", str(checkout)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", origin_url],
        check=True,
    )
    return checkout, origin_url
