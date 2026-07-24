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
import uuid
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


# ---------------------------------------------------------------------------
# Single-flight advisory locks (P11)
# ---------------------------------------------------------------------------
# The box-wide store now has multiple unreaped background writers: every
# SessionStart fires a detached `store.py consolidate` (and, from P11, a
# detached `store.py backup --if-due`), and multiple CC sessions + subagents +
# ZCode can start within seconds of each other. `consolidate()`'s meta-key
# cadence gate (`last_consolidation` + growth threshold) is a SOFT gate: it
# reads the timestamp, and only later writes it, so two processes can both
# pass the "not recent enough" check before either commits and both then run
# the clustering loop concurrently.
#
# The guard is a lockfile whose *existence* is the lock — created with
# O_CREAT|O_EXCL|O_WRONLY, which is atomic on every platform we support
# (including Windows, where it maps to CREATE_NEW). The fd is closed
# immediately; nothing holds an open handle, so a stale lock can always be
# unlinked by another process (Windows will not let you delete an open file).
#
# Three properties this API guarantees:
#   * FAIL-OPEN FOR THE LOSER — a caller that cannot get the lock is told to
#     skip its own work and exit 0. It never blocks, never waits, never raises.
#   * FAIL-OPEN FOR THE BROKEN — if the lock dir/file is unusable for reasons
#     unrelated to contention (no permission, read-only fs), acquisition
#     returns a token anyway and the caller proceeds UNLOCKED. Memory hygiene
#     must not be wedged by a lockfile problem.
#   * STALE RECOVERY IS INSTANCE-BOUND — a crashed/killed holder leaves its
#     lockfile behind forever. Any process that sees an over-age lock breaks
#     it, but the break only counts if the file it actually removed IS the
#     over-age file it inspected. See _break_stale_lock() for why "rename it to
#     a unique name" is NOT by itself enough: rename moves whatever file sits
#     at the path when it runs, so two processes that judged one lock stale
#     could both break it, the second landing on the first one's fresh live
#     lock. Identity is re-confirmed (st_mtime_ns) after the rename, and a
#     mismatch is undone.
#
# Residual, stated honestly because the previous version of this comment
# overclaimed: two processes that judge the SAME lock instance stale can no
# longer both end up holding — the loser detects that it moved a different
# instance and puts it back untouched. But between that mistaken breaker's
# rename-out and its rename-back the path is momentarily empty, so a third
# process acquiring inside that window can still coexist with the rightful
# holder. There is no portable atomic compare-and-delete to close it, and every
# scheme that would (e.g. an O_EXCL "break claim" file) can wedge the lock
# permanently if its owner dies mid-break — strictly worse than the floor
# below, given this API's fail-open contract.
#
# Honest caveat: an mtime lease cannot distinguish "crashed" from "slower than
# the timeout". A live holder that runs longer than its stale timeout WILL have
# its lock broken. Timeouts are therefore set far above any realistic run
# (see store.py's *_LOCK_STALE_SECONDS), and the worst case degrades to
# today's behavior (two concurrent runs), not to corruption.

_NO_LOCK_TOKEN = "unlocked"


def acquire_lock(path: str | Path, stale_seconds: float) -> str | None:
    """Try to take the advisory lock at `path`.

    Returns an opaque token on success — pass it to release_lock() — or None
    if another live holder has it, in which case the caller must skip its work
    and exit 0. Never blocks, never raises.
    """
    p = Path(path)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _NO_LOCK_TOKEN  # unusable lock dir — proceed unlocked

    acquired = _try_create_lock(p, token)
    if acquired is True:
        return token
    if acquired is None:
        return _NO_LOCK_TOKEN  # unexpected OSError — proceed unlocked

    # Someone holds it. Is that holder stale?
    try:
        st = p.stat()
    except FileNotFoundError:
        # Released between our create attempt and the stat — one more try.
        return token if _try_create_lock(p, token) is True else None
    except OSError:
        return None

    if (time.time() - st.st_mtime) <= stale_seconds:
        return None  # live holder — skip cleanly

    # Stale. The break must remove THIS file instance, not merely "whatever is
    # at the path" — see _break_stale_lock. A refused break means skip.
    if not _break_stale_lock(p, st.st_mtime_ns):
        return None

    # The break winner still has to win a normal acquisition: a third process
    # may legitimately have created a fresh lock in the gap. If so, skip.
    return token if _try_create_lock(p, token) is True else None


def _break_stale_lock(p: Path, stale_mtime_ns: int) -> bool:
    """Remove the lock file at `p` ONLY IF the file actually sitting there is
    the same instance the caller stat'ed and judged stale. True => the stale
    instance is gone and the caller may try to take the lock; False => it was
    not removed (or we could not prove it was the right one) and the caller
    must skip. Never raises.

    Why the identity re-check is load-bearing (the bug this replaces): renaming
    the lock aside was treated as an atomic claim on the break, on the theory
    that only one process can rename a given existing file. That is true but
    irrelevant — os.rename operates on the PATH, and moves whatever file is
    sitting there when it runs, which need not be the file the caller inspected
    a moment earlier. So process A and process B could both judge one stale
    lock breakable; A renames it away and creates a fresh lock; B's rename then
    moves A's fresh, LIVE lock aside and B creates its own. Both then run,
    which is exactly what the lock exists to prevent — and because A's lock
    file no longer exists, A's release_lock token-compare silently no-ops, so
    when B releases, the path is left completely unguarded while A is still
    working.

    The fix: treat the rename as a claim that must be CONFIRMED. st_mtime_ns of
    the inspected instance is captured before the rename and re-checked on the
    renamed file afterwards. Windows exposes no usable inode and no atomic
    compare-and-delete, so mtime_ns is the identifying attribute available
    here; it is sufficient because a replacement lock is necessarily created
    stale_seconds (minutes) after the instance it replaced, so the two can
    never be confused. Anything other than a positive match — including a stat
    we cannot perform at all — is treated as "not ours": the file is put back
    exactly as it was (rename preserves both mtime and the owner's token, so
    its owner's release_lock still works), and we decline the break.
    """
    victim = p.with_name(p.name + f".stale.{uuid.uuid4().hex}")
    try:
        os.rename(str(p), str(victim))
    except OSError:
        # Lost the rename (already gone, or a Windows sharing violation) —
        # someone else is handling it.
        return False

    try:
        moved_mtime_ns = os.stat(str(victim)).st_mtime_ns
    except OSError:
        moved_mtime_ns = None  # cannot confirm identity => not ours

    if moved_mtime_ns is not None and moved_mtime_ns == stale_mtime_ns:
        try:
            os.unlink(str(victim))
        except OSError:
            pass
        return True

    # Not the instance we judged stale: someone else broke it first and
    # installed a live lock, and we just moved THAT. Put it back.
    if not _rename_noreplace(str(victim), str(p)):
        # A third process took the free path inside that window. Do not clobber
        # it; drop the file we should never have moved and decline the break —
        # we have no evidence whatsoever that the current holder is stale.
        try:
            os.unlink(str(victim))
        except OSError:
            pass
    return False


def _rename_noreplace(src: str, dst: str) -> bool:
    """Rename src -> dst WITHOUT ever clobbering an existing dst. True on
    success; False if dst already existed or the rename otherwise failed.

    Needed because os.rename's semantics are platform-split: on Windows it
    fails when dst exists (what we want), but on POSIX it SILENTLY REPLACES
    dst — which on _break_stale_lock's put-back path would destroy the live
    lock a third process legitimately created in the gap, re-introducing the
    very double-hold this is fixing. os.link is atomic and raises
    FileExistsError when dst exists on both platforms, so POSIX goes through
    link + unlink. CI runs ubuntu-latest as well as windows-latest, so this
    limb is exercised.
    """
    if os.name == "nt":
        try:
            os.rename(src, dst)
            return True
        except OSError:
            return False
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    except OSError:
        # Filesystem without hard links (rare). Restoring the owner's lock
        # matters more than the residual clobber risk of a plain rename.
        try:
            os.rename(src, dst)
            return True
        except OSError:
            return False
    try:
        os.unlink(src)
    except OSError:
        pass
    return True


def _try_create_lock(p: Path, token: str) -> bool | None:
    """True = created (we own it); False = already exists; None = unexpected
    OSError (caller should proceed unlocked)."""
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return None
    try:
        os.write(fd, token.encode("utf-8"))
    except OSError:
        pass
    finally:
        # Close immediately — the lock is the file's existence, not an open
        # handle. Holding it open would block a stale-break unlink on Windows.
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def release_lock(path: str | Path, token: str | None) -> None:
    """Release a lock previously taken with acquire_lock(). Only unlinks the
    file if it still carries OUR token, so a lock that was broken as stale and
    re-taken by someone else is never deleted out from under its new owner.
    No-op for the unlocked-degraded token. Never raises."""
    if not token or token == _NO_LOCK_TOKEN:
        return
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if content != token:
        return
    try:
        os.unlink(str(p))
    except OSError:
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
