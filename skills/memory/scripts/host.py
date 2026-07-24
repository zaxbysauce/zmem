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
import re
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


def resolve_skills_dirs() -> list[Path]:
    """Resolve the skills dirs promotion writes SKILL.md into.

    Default (box-wide): BOTH `~/.claude/skills` and `~/.zcode/skills`, so a
    lesson promoted from either host becomes a skill visible to both. This
    is deliberately not host-conditional (unlike resolve_store_path) — a
    promoted skill is meant to be usable by whichever tool is in front of
    the user next, not just the one that promoted it.

    Override: ZMEM_SKILLS_DIRS, an os.pathsep-delimited list of directories
    (matches zmem-launch.js's ZMEM_SKILLS_DIRS export so hook-context and
    skill-context agree on the same set). Each entry is `~`-expanded.
    Order is preserved; duplicates (same resolved path) are dropped.
    """
    explicit = _env("ZMEM_SKILLS_DIRS")
    if explicit:
        raw = [p for p in explicit.split(os.pathsep) if p.strip()]
    else:
        home = Path(os.path.expanduser("~"))
        raw = [str(home / ".claude" / "skills"), str(home / ".zcode" / "skills")]

    seen: set[str] = set()
    dirs: list[Path] = []
    for p in raw:
        expanded = Path(p).expanduser()
        key = _norm(expanded)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(expanded)
    return dirs


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
    without icacls (non-Windows, restricted shell, etc.).

    BUG FIX (discovered during P6 verification): a bare `/grant:r user:F` on a
    DIRECTORY grants rights on the directory object only — it does not carry
    an inheritable ACE, so files created in that directory afterward (core.md,
    a freshly-checkpointed store.sqlite, etc.) can end up with an effectively
    empty DACL and become unreadable to the very same user moments later.
    Reproduced on a real (non-temp) `C:\\Users\\<you>\\...` path: after the
    first `connect()` hardens a brand-new ZMEM_DATA dir, every subsequent read
    of core.md failed with PermissionError(13) — silently dropping Tier 0 from
    every session after the first on a fresh install. Directories get
    `(OI)(CI)` (object-inherit, container-inherit) so new children inherit
    read/write; plain files keep the original bare grant."""
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            return
        grant = f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass


def _get_git_remote_url(project_dir: Path) -> str | None:
    """Return `origin`'s remote URL for project_dir, or None if project_dir is
    not a git checkout (or has no `origin` remote). Works for worktrees and
    second clones alike — `git -C <dir> remote get-url origin` resolves via
    the checkout's own .git pointer, so it does not require project_dir to be
    a repo's top-level root."""
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _normalize_remote(url: str) -> str:
    """Normalize a git remote URL to a bare `host/org/repo` form, lowercased,
    with any `.git` suffix and trailing slash stripped. Handles both the SSH
    scp-like form (`git@host:org/repo.git`) and URL forms
    (`https://host/org/repo.git`, `ssh://git@host/org/repo`), with or without
    a trailing slash or `.git` suffix — all normalize to the same key."""
    u = url.strip().rstrip("/")
    if u.lower().endswith(".git"):
        u = u[: -len(".git")]

    # SSH scp-like shorthand: user@host:path (no scheme).
    m = re.match(r"^[^/@:\s]+@([^/:\s]+):(.+)$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        # Any scheme://[user@]host/path form (https, ssh, git, http, ...).
        m2 = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/]+)/(.+)$", u)
        if m2:
            host, path = m2.group(1), m2.group(2)
        else:
            # Unrecognized shape — normalize what we have rather than guessing.
            host, path = "", u

    path = path.strip("/")
    combined = f"{host}/{path}" if host else path
    return combined.lower()


def _norm_abspath_key(project_dir: Path) -> str:
    """Absolute path form used for the no-remote fallback namespace: drive
    letter lowercased, forward-slashed. Fully lowercased (Windows paths are
    case-insensitive) so casing differences never split one directory into
    two namespaces."""
    ap = os.path.abspath(str(project_dir))
    return ap.replace("\\", "/").lower()


def resolve_namespace(project_dir: str | Path) -> str:
    """The SOLE producer of `project:*` namespace keys — called both by the
    runtime hook launcher (recall) and by the v5 migration (store.py). Never
    hand-type a namespace key; always derive it through this function so
    runtime and migration keys are guaranteed identical.

    - If project_dir is a git checkout with an `origin` remote: normalize the
      remote to `host/org/repo` (lowercased, `.git`/trailing-slash stripped;
      SSH and HTTPS forms collapse to the same key) -> `project:<host/org/repo>`.
      Worktrees and second clones of the same remote yield the same key.
    - Else (no remote / not a checkout): `project:<normalized-abspath>`.
    """
    p = Path(project_dir)
    remote = _get_git_remote_url(p)
    if remote:
        return f"project:{_normalize_remote(remote)}"
    return f"project:{_norm_abspath_key(p)}"


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
