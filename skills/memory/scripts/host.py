"""ZMem host adapter — shared, host-neutral facts about where things live.

Owns path resolution (store, core.md), the local-filesystem safety guard,
best-effort file permission hardening, and a small busy-retry helper for
SQLite writes. No I/O happens at import time — every function reads the
environment / filesystem lazily when called, so tests can monkeypatch
os.environ per-call and callers can pick up a launcher's env changes
without re-importing.

Path resolution (store), in priority order:
  1. ZMEM_STORE            - explicit full path to store.sqlite (hard override)
  2. ZMEM_DATA              - box-wide data dir; store lives at <dir>/store.sqlite
  3. CLAUDE_PLUGIN_DATA     - Claude Code plugin data dir (deleted on uninstall)
  4. ZCODE_PLUGIN_DATA      - ZCode plugin data dir (deleted on uninstall)
  5. ~/.zmem/store.sqlite   - NEW box-neutral default (created if nothing else applies)
  6. ~/.zcode/memory/store.sqlite - legacy manual-install fallback, used ONLY if
     it already exists on disk and the new default (5) does not yet exist.

This intentionally drops store.py's old "scan ~/.zcode/cli/plugins/data/*zmem*/"
auto-detection: that scan is precisely what kept resolving to the legacy
per-plugin store even with no env vars set, which would silently defeat the
box-wide migration this phase performs. See PLAN.md P1 / P1 report for the
explicit callout.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def _env(name: str) -> str | None:
    """Read an env var at call time (never cache) so tests can monkeypatch."""
    val = os.environ.get(name)
    return val if val else None


def resolve_store_path() -> Path:
    """Resolve the store.sqlite path. See module docstring for the priority chain."""
    explicit = _env("ZMEM_STORE")
    if explicit:
        return Path(explicit).expanduser()

    zmem_data = _env("ZMEM_DATA")
    if zmem_data:
        return Path(zmem_data).expanduser() / "store.sqlite"

    claude_data = _env("CLAUDE_PLUGIN_DATA")
    if claude_data:
        return Path(claude_data).expanduser() / "store.sqlite"

    zcode_data = _env("ZCODE_PLUGIN_DATA")
    if zcode_data:
        return Path(zcode_data).expanduser() / "store.sqlite"

    home = Path(os.path.expanduser("~"))
    zmem_default = home / ".zmem" / "store.sqlite"
    legacy = home / ".zcode" / "memory" / "store.sqlite"
    if not zmem_default.exists() and legacy.exists():
        return legacy
    return zmem_default


def resolve_core_md_path() -> Path:
    """Resolve the Tier 0 core.md path. ZMEM_CORE_MD override, else <store dir>/core.md."""
    explicit = _env("ZMEM_CORE_MD")
    if explicit:
        return Path(explicit).expanduser()
    return resolve_store_path().parent / "core.md"


def _norm(path: Path) -> str:
    """Normalized, absolute, lowercase string form of a path for comparisons.
    Works even when the path (or its parents) doesn't exist yet."""
    return str(Path(os.path.abspath(str(path)))).replace("/", "\\").lower()


def _is_unc(path_str: str) -> bool:
    return path_str.startswith("\\\\") or path_str.startswith("//")


def _is_under(path_str: str, root_str: str) -> bool:
    root_str = root_str.rstrip("\\") + "\\"
    return path_str.startswith(root_str) or (path_str + "\\") == root_str


def _drive_type_is_remote(path_str: str) -> bool | None:
    """Best-effort: is the drive letter of path_str a network drive (Windows)?
    Returns True/False, or None if it can't be determined (non-Windows, error,
    or no drive letter e.g. already-UNC)."""
    drive, _ = os.path.splitdrive(path_str)
    if not drive or len(drive) < 2 or drive[1] != ":":
        return None
    try:
        import ctypes
        DRIVE_REMOTE = 4
        root = drive + "\\"
        result = ctypes.windll.kernel32.GetDriveTypeW(str(root))
        return result == DRIVE_REMOTE
    except Exception:
        return None


def assert_local_fs(path: Path) -> None:
    """Refuse a store location that risks WAL corruption or unwanted sync:
    UNC/network paths, and paths under the OneDrive root. Raises ValueError.

    Conservative by design: reject UNC + under-OneDrive unconditionally;
    network-mapped-drive detection is best-effort (skipped silently if it
    can't be determined) so this never hard-fails for reasons unrelated to
    the two floor cases the tests exercise.
    """
    norm = _norm(path)

    if _is_unc(norm):
        raise ValueError(f"zmem: refusing UNC/network store path: {path}")

    onedrive = _env("OneDrive") or _env("OneDriveConsumer") or _env("OneDriveCommercial")
    if onedrive:
        if _is_under(norm, _norm(Path(onedrive))):
            raise ValueError(
                f"zmem: refusing store path under OneDrive root ({onedrive}): {path}"
            )

    is_remote = _drive_type_is_remote(norm)
    if is_remote:
        raise ValueError(f"zmem: refusing store path on a network-mapped drive: {path}")


def set_owner_only_perms(path: Path) -> None:
    """Best-effort: restrict a file or directory to the current user (Windows
    ACL via icacls). Never raises — permission hardening is defense-in-depth,
    not a correctness requirement, and must not break the store on a box
    without icacls (non-Windows, restricted shell, etc.)."""
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass


def busy_retry(fn, attempts: int = 5):
    """Call fn() with retry-on-'database is locked'. Small exponential backoff.
    Belt-and-suspenders past PRAGMA busy_timeout (which already handles most
    contention inside SQLite itself) for the rare case a Python-level retry
    around a whole commit() helps. fn should be safe to call more than once
    if it raises partway before committing (callers pass conn.commit-style
    calls, which are idempotent to retry)."""
    import sqlite3
    delay = 0.05
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_exc = e
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 2
    raise last_exc
