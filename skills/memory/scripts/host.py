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

    # Once the box-wide store exists, it always wins — this is what keeps the
    # legacy probes below from re-defeating the migration (the P1 concern).
    if zmem_default.exists():
        return zmem_default

    # No box-wide store yet. Prefer a real pre-migration store over handing back
    # an empty new path, so an un-migrated install is never silently stranded
    # with an invisible store (PR review PRR-002).
    legacy_manual = home / ".zcode" / "memory" / "store.sqlite"
    if legacy_manual.exists():
        return legacy_manual

    legacy_plugin = _legacy_plugin_store(home)
    if legacy_plugin is not None:
        return legacy_plugin

    return zmem_default


def _legacy_plugin_store(home: Path) -> Path | None:
    """Newest `~/.zcode/cli/plugins/data/*zmem*/store.sqlite`, or None.

    This is the pre-box-wide per-plugin location. P1 removed the original
    unconditional scan because it out-ranked `~/.zmem` and so would have
    silently defeated the migration. It is restored here strictly as a
    LAST resort — only reached when no env var is set AND no box-wide store
    exists yet — which preserves P1's intent while fixing the regression it
    introduced: before this, a user who had not yet run `import-store.py` got
    a brand-new empty store and their existing memory simply vanished from
    every bare `store.py` invocation.

    Newest-by-mtime when several plugin dirs match (e.g. the same plugin
    installed from two marketplaces, or ZCode and Claude Code each carrying a
    live legacy store). Never raises.

    AMBIGUITY IS REPORTED, NOT SWALLOWED: choosing silently would hide every
    memory in the store(s) not chosen, with nothing to tell the user that half
    their history had gone quiet. A bare invocation still needs one
    deterministic answer, so newest-by-mtime stays — but the losers are named
    on stderr along with the supported fix (`import-store.py` merges a legacy
    store into the box-wide one). stderr rather than an exception: this is the
    last-resort limb of a fail-open resolution chain, and hooks must not begin
    crashing merely because a second plugin dir exists.
    """
    try:
        data_dir = home / ".zcode" / "cli" / "plugins" / "data"
        matches = [p for p in data_dir.glob("*zmem*/store.sqlite") if p.exists()]
    except OSError:
        return None
    if not matches:
        return None
    try:
        chosen = max(matches, key=lambda p: p.stat().st_mtime)
    except OSError:
        chosen = matches[0]

    if len(matches) > 1:
        try:
            import sys as _sys
            others = "".join(
                f"[zmem]   ignored: {p}\n" for p in matches if p != chosen
            )
            print(
                f"[zmem] WARNING: {len(matches)} legacy plugin stores found; using the "
                f"most recently modified.\n"
                f"[zmem]   using:   {chosen}\n"
                f"{others}"
                f"[zmem] Memories in the ignored store(s) are NOT visible. Merge each with:\n"
                f"[zmem]   python import-store.py --source <store.sqlite> "
                f"--dest-dir ~/.zmem --force",
                file=_sys.stderr,
            )
        except Exception:
            pass  # a warning must never break resolution
    return chosen


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


def _guard_forms(path: Path) -> list[str]:
    """Normalized forms of `path` that assert_local_fs must inspect: the literal
    path AND, when obtainable, the symlink/junction-resolved path.

    Both, not just the resolved one, deliberately:
      * A symlink or NTFS junction pointing into OneDrive or a share would
        otherwise sail through the guard — os.path.abspath (what _norm uses)
        normalizes `..` and casing but does NOT follow links, so the literal
        form of `C:\\local\\link` looks perfectly local. Resolving catches it.
      * The literal form still has to be checked because Path.resolve() is
        non-strict for paths that don't exist yet (the normal case for a store
        we are about to create) and simply hands the input back — and because
        keeping it means a bad literal path is rejected even when resolution
        is unavailable.

    A .resolve() failure is SWALLOWED (we fall back to the literal form) rather
    than propagated. Resolution can fail for reasons that have nothing to do
    with the two floor cases this guard exists for — an ACL we cannot traverse,
    a link whose target we lack permission to stat. The function's contract is
    "conservative, but never hard-fail on a valid local path that merely can't
    be stat'ed": failing closed here would wedge ordinary local stores on
    restricted boxes, which is strictly worse than losing the ability to see
    through a link we are not allowed to follow. The literal-form checks below
    still run in that case.
    """
    forms = [_norm(path)]
    try:
        resolved = _norm(Path(path).resolve())
    except Exception:
        return forms
    if resolved not in forms:
        forms.append(resolved)
    return forms


def assert_local_fs(path: Path) -> None:
    """Refuse a store location that risks WAL corruption or unwanted sync:
    UNC/network paths, and paths under the OneDrive root. Raises ValueError.
    Symlinks/junctions are followed (see _guard_forms) so a local-looking link
    into OneDrive or a share cannot bypass the check.

    Conservative by design: reject UNC + under-OneDrive unconditionally;
    network-mapped-drive detection is best-effort (skipped silently if it
    can't be determined) so this never hard-fails for reasons unrelated to
    the two floor cases the tests exercise.
    """
    forms = _guard_forms(path)

    for norm in forms:
        if _is_unc(norm):
            raise ValueError(f"zmem: refusing UNC/network store path: {path}")

    onedrive = _env("OneDrive") or _env("OneDriveConsumer") or _env("OneDriveCommercial")
    if onedrive:
        # Resolve the ROOT too: a temp/8.3-short-name or symlinked OneDrive root
        # would otherwise fail to prefix-match an already-resolved candidate.
        roots = _guard_forms(Path(onedrive))
        for norm in forms:
            for root in roots:
                if _is_under(norm, root):
                    raise ValueError(
                        f"zmem: refusing store path under OneDrive root ({onedrive}): {path}"
                    )

    for norm in forms:
        if _drive_type_is_remote(norm):
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
    read/write; plain files keep the original bare grant.

    On non-Windows the equivalent is a plain chmod: 0o700 for directories
    (owner rwx, nothing for group/other) and 0o600 for files. Without it the
    store — which holds box-wide plaintext memory — was left at the process
    umask default and typically group/world-readable on Linux/Mac. The
    platform branch is checked BEFORE the USERNAME/USER lookup because chmod
    needs no username, and a POSIX environment with USER unset (containers,
    some CI) would otherwise skip hardening entirely."""
    try:
        if os.name != "nt":
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
            return
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
    a trailing slash or `.git` suffix — all normalize to the same key.

    Loopback-proxy rewrite: Claude Code cloud/remote (CCR) sessions see their
    GitHub repo through a local HTTP proxy (`http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>`),
    so the same repo would otherwise normalize to a different, per-session
    key (host includes the ephemeral proxy port) than the same repo cloned
    locally. When the resolved host (port stripped) is a loopback address —
    `127.0.0.1`, `localhost`, `::1`, or `[::1]`, case-insensitively — and the
    path's first two segments after a leading `git/` (matched
    case-insensitively, like the host check) are `<org>/<repo>`, rewrite the
    key to `<forge_host>/<org>/<repo>` instead. A loopback path with fewer
    than two segments after `git/`, or that doesn't start with `git/` at all,
    falls through unchanged — it's either malformed or a genuinely local git
    server, which must keep its existing key.

    `ZMEM_PROXY_FORGE_HOST` has THREE distinct states, not two:
      - unset            -> rewrite to `github.com` (CCR proxies GitHub only
                            today; this is the default).
      - set, non-empty   -> rewrite to that host (escape hatch for other
                            forges, e.g. `gitlab.example.com`).
      - set but EMPTY    -> DISABLE the rewrite entirely; the loopback remote
        (`""` or whitespace)  keeps its legacy `127.0.0.1:<port>/git/...` key.
                            This is the opt-out for a genuine LOCAL git server
                            that happens to serve repos under a `/git/`
                            prefix (Gitea's default layout, for one): those
                            are not CCR proxies and must not be collapsed
                            onto a public forge's namespace."""
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

    host_no_port = re.sub(r":\d+$", "", host)
    if host_no_port.lower() in ("127.0.0.1", "localhost", "::1", "[::1]"):
        forge_env = os.environ.get("ZMEM_PROXY_FORGE_HOST")
        # Set-but-empty is the explicit opt-out (see docstring): skip the
        # rewrite entirely and fall through to the legacy loopback key.
        # Unset (None) is NOT the same thing — that means "use the default".
        if forge_env is None or forge_env.strip():
            forge_host = forge_env.strip() if forge_env else "github.com"
            gm = re.match(r"^git/([^/]+)/([^/]+)", path, re.IGNORECASE)
            if gm:
                org, repo = gm.group(1), gm.group(2)
                return f"{forge_host}/{org}/{repo}".lower()

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
# BREAKS ARE SERIALIZED (the fix for the residual the previous comment
# described). Identity re-confirmation alone was not enough: a breaker that had
# moved a live lock aside had to put it back, and between its rename-out and
# its rename-back the path was momentarily EMPTY, so a third process could
# acquire there and coexist with the rightful holder. Measured, not theorized —
# 16 threads racing one planted stale lock produced 2-3 simultaneous holders in
# 277 of 300 runs on Windows, and the ubuntu CI job failed the same stress test
# intermittently.
#
# So a break now runs under a short-lived O_EXCL claim file (`<lock>.break`,
# _BREAK_CLAIM_STALE_SECONDS, far below the real locks' 600s/1800s), and the
# claim holder RE-STATS the real lock before touching it — if it is no longer
# over-age, the break is abandoned without a rename. A live lock is therefore
# never renamed aside, which is what removes the empty-path window rather than
# merely narrowing it.
#
# The old objection to a claim file was that it "can wedge the lock permanently
# if its owner dies mid-break". It cannot, for two independent reasons:
#   * the claim carries its own (much shorter) stale lease, so a claim orphaned
#     by a crash is reclaimed after _BREAK_CLAIM_STALE_SECONDS; and
#   * a claim mechanism that is unusable for any other reason (EACCES, a
#     read-only fs) does NOT block the break — _acquire_break_claim reports
#     UNAVAILABLE and the break proceeds unserialized, i.e. exactly the
#     pre-change behavior. Losing serialization is a degradation; losing stale
#     recovery would be a wedge, and this API promises never to wedge.
# (Precisely: a stray non-file object at the claim path — a directory, say —
# presents as EEXIST, so it reads as a live claim for the first
# _BREAK_CLAIM_STALE_SECONDS and as UNAVAILABLE thereafter, once the reclaim's
# unlink of it fails. Bounded degradation, still not a wedge.)
# A caller that finds the claim held by a LIVE breaker returns None (skip), not
# _NO_LOCK_TOKEN: someone is actively breaking-and-taking, so proceeding
# unlocked would be the very double-worker the lock exists to prevent.
#
# Residual, stated honestly (two windows remain; neither is the one that was
# failing CI):
#   1. Between the re-stat under the claim and the rename two syscalls later, a
#      slow-but-alive holder can release and a third process create a fresh
#      lock — so the rename can still land on a live lock, and the put-back gap
#      opens for that attempt. Narrowed from "the whole interval since the
#      caller's first stat" to those two syscalls, not eliminated: there is no
#      portable atomic compare-and-delete. This is the scenario
#      test_failed_put_back_still_never_grants_the_lock pins.
#   2. The claim's own lease has the same limitation the real locks' does. Two
#      processes that both judge one abandoned claim reclaimable can both end
#      up holding it (the reclaim is only mtime-verified), and a breaker
#      suspended for longer than _BREAK_CLAIM_STALE_SECONDS can have its live
#      claim reclaimed out from under it. Either way two breakers run at once
#      and that break attempt degrades to the pre-change (unserialized)
#      behavior. NOT harmless, just rare — the claim is held across ~4
#      syscalls, so reaching a 30s lease requires a crash or a stopped
#      process, and a plain _release_break_claim unlink may then remove the
#      successor's claim.
# In both cases the failure mode is bounded by the identity confirmation in
# _break_stale_lock, which is retained as the correctness backstop.
#
# Cost: the claim adds syscalls only on the stale-break path, which is rare by
# construction. The uncontended acquire (single O_EXCL create) is unchanged.
#
# Honest caveat: an mtime lease cannot distinguish "crashed" from "slower than
# the timeout". A live holder that runs longer than its stale timeout WILL have
# its lock broken. Timeouts are therefore set far above any realistic run
# (see store.py's *_LOCK_STALE_SECONDS), and the worst case degrades to
# today's behavior (two concurrent runs), not to corruption.

_NO_LOCK_TOKEN = "unlocked"

# A break claim is held only across a handful of syscalls. The lease exists
# purely so a process killed mid-break cannot park the claim forever; it is
# deliberately orders of magnitude below the real locks' stale timeouts
# (600s backup / 1800s consolidate) so an orphaned claim costs at most this
# long of "stale locks cannot be broken", never a permanent wedge.
_BREAK_CLAIM_STALE_SECONDS = 30.0
_BREAK_CLAIM_SUFFIX = ".break"
# Consecutive non-EEXIST create failures before the claim mechanism is declared
# unusable. See _create_break_claim — this is a classifier, not a backoff: it
# never sleeps, and the transient it exists for resolved within 2 attempts in
# every one of 473 measured occurrences.
_CLAIM_CREATE_ATTEMPTS = 5

# _acquire_break_claim outcomes.
_CLAIM_ACQUIRED = "acquired"      # we own it; we must release it
_CLAIM_HELD = "held"              # another breaker is mid-break; skip
_CLAIM_UNAVAILABLE = "unavailable"  # mechanism unusable; break unserialized


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
    # os.stat(str(p)) rather than p.stat(): pathlib's stat call site moved
    # between 3.10 and 3.11, and the regression tests patch os.stat to force
    # the "replaced between the two stats" interleaving deterministically.
    try:
        st = os.stat(str(p))
    except FileNotFoundError:
        # Released between our create attempt and the stat — one more try.
        return token if _try_create_lock(p, token) is True else None
    except OSError:
        return None

    if (time.time() - st.st_mtime) <= stale_seconds:
        return None  # live holder — skip cleanly

    # Stale. The break must remove THIS file instance, not merely "whatever is
    # at the path" — see _break_stale_lock. A refused break means skip.
    if not _break_stale_lock(p, st.st_mtime_ns, stale_seconds):
        return None

    # The break winner still has to win a normal acquisition: a third process
    # may legitimately have created a fresh lock in the gap. If so, skip.
    return token if _try_create_lock(p, token) is True else None


def _claim_path(p: Path) -> Path:
    return p.with_name(p.name + _BREAK_CLAIM_SUFFIX)


def _create_break_claim(claim: Path) -> str:
    """Create the claim exclusively. _CLAIM_ACQUIRED / _CLAIM_HELD /
    _CLAIM_UNAVAILABLE. Never raises, never sleeps.

    A non-FileExistsError OSError is retried a few times before the claim
    mechanism is declared unusable, because on Windows it is NOT necessarily
    permanent: unlinking a file leaves its name in a delete-pending state in
    which a concurrent O_EXCL create fails with EACCES instead of EEXIST — and
    a hot break loop unlinks this very claim constantly. Measured on Windows
    (6 threads churning create+unlink against 6 threads creating): 473 of 473
    EACCES failures resolved on the first or second immediate retry — 342 to
    EEXIST, 131 to a successful create, none persisting. POSIX has no
    delete-pending state and so does not produce this at all.

    Misreading that transient as "unusable" is not harmless: UNAVAILABLE drops
    the caller out of the serialized path and back onto the pre-change racy
    break. Measured cost of getting this wrong: 8 double-holds per 300
    16-thread stress runs, every single one of them immediately preceded by one
    of these EACCES.
    """
    for _ in range(_CLAIM_CREATE_ATTEMPTS):
        try:
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return _CLAIM_HELD
        except OSError:
            continue
        # Close immediately, exactly as _try_create_lock does: the claim is the
        # file's existence, and Windows refuses to unlink a file that is still
        # open — leaving it open would make our own best-effort release fail on
        # Windows while succeeding on Linux.
        try:
            os.close(fd)
        except OSError:
            pass
        return _CLAIM_ACQUIRED
    return _CLAIM_UNAVAILABLE


def _acquire_break_claim(claim: Path) -> str:
    """Take the short-lived claim that serializes stale-lock breaks.

    Returns _CLAIM_ACQUIRED (caller owns it and MUST release it),
    _CLAIM_HELD (another breaker is mid-break — the caller must skip its break
    entirely) or _CLAIM_UNAVAILABLE (the claim mechanism itself is unusable —
    the caller proceeds with an UNSERIALIZED break, i.e. pre-change behavior,
    because refusing to break would wedge stale recovery forever). Never
    raises, never blocks, and makes at most two _create_break_claim rounds (of
    up to _CLAIM_CREATE_ATTEMPTS os.open calls each) so it always terminates —
    there is no recursion and no retry loop around the reclaim.

    An existing claim older than _BREAK_CLAIM_STALE_SECONDS was orphaned by a
    process that died mid-break; it is unlinked and re-created. The unlink is
    guarded by an mtime_ns re-check so a competitor that already reclaimed the
    claim and installed its own fresh one is not clobbered — which narrows, but
    does not close, the double-reclaim noted in the module comment.
    """
    state = _create_break_claim(claim)
    if state != _CLAIM_HELD:
        return state

    try:
        st = os.stat(str(claim))
    except FileNotFoundError:
        # Vanished between the create and the stat — one retry, then give up.
        return _create_break_claim(claim)
    except OSError:
        return _CLAIM_UNAVAILABLE

    if (time.time() - st.st_mtime) <= _BREAK_CLAIM_STALE_SECONDS:
        return _CLAIM_HELD  # a live breaker owns it

    try:
        if os.stat(str(claim)).st_mtime_ns != st.st_mtime_ns:
            return _CLAIM_HELD  # reclaimed by someone else since we judged it
        os.unlink(str(claim))
    except FileNotFoundError:
        pass  # someone else reclaimed it first; the create below decides
    except OSError:
        return _CLAIM_UNAVAILABLE
    return _create_break_claim(claim)


def _release_break_claim(claim: Path) -> None:
    """Best-effort release. A failure here costs at most
    _BREAK_CLAIM_STALE_SECONDS of un-breakable stale locks, never a wedge."""
    try:
        os.unlink(str(claim))
    except OSError:
        pass


def _break_stale_lock(p: Path, stale_mtime_ns: int, stale_seconds: float) -> bool:
    """Remove the lock file at `p` ONLY IF the file actually sitting there is
    the same instance the caller stat'ed and judged stale. True => the stale
    instance is gone and the caller may try to take the lock; False => it was
    not removed (or we could not prove it was the right one) and the caller
    must skip. Never raises.

    Runs under the break claim (see _acquire_break_claim), and RE-STATS `p`
    while holding it. That re-stat is the step that closes the double-hold the
    identity check alone could not: a lock that is no longer over-age is left
    completely untouched, so a live lock is never renamed aside and the
    momentarily-empty-path window that a put-back opens never occurs for the
    concurrent-breakers case. The freshly observed st_mtime_ns replaces the
    caller's as the identity witness — it is the stronger one, having been
    observed inside the serialized section.

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
    claim = _claim_path(p)
    state = _acquire_break_claim(claim)
    if state == _CLAIM_HELD:
        return False  # another breaker owns this break — skip, do not touch `p`
    try:
        if state == _CLAIM_ACQUIRED:
            try:
                cur = os.stat(str(p))
            except OSError:
                # Gone (already broken, or released) — nothing for us to break.
                return False
            if (time.time() - cur.st_mtime) <= stale_seconds:
                # Someone broke it and took it while we were queuing for the
                # claim. It is LIVE: leave it exactly where it is.
                return False
            stale_mtime_ns = cur.st_mtime_ns
        return _break_confirmed_instance(p, stale_mtime_ns)
    finally:
        if state == _CLAIM_ACQUIRED:
            _release_break_claim(claim)


def _break_confirmed_instance(p: Path, stale_mtime_ns: int) -> bool:
    """The rename-aside + identity-confirm + put-back body of _break_stale_lock,
    split out so the claim's acquire/release brackets it cleanly. Never raises.
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
    OSError (caller should proceed unlocked).

    A failed token write is NOT swallowed into a success. The lock's existence
    is the lock, but its CONTENT is the owner's identity: release_lock compares
    the file's bytes to the caller's token, so a lock file created empty (write
    failed) can never be matched and never be released — it would sit there
    orphaning the lock until the stale timeout (minutes) elapsed, while its
    "owner" believed it held a releasable lock. Better to unlink the broken
    file and return None, which the rest of this module already means as
    "unexpected OSError -> caller proceeds unlocked".
    """
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return None
    write_failed = False
    try:
        os.write(fd, token.encode("utf-8"))
    except OSError:
        write_failed = True
    finally:
        # Close immediately — the lock is the file's existence, not an open
        # handle. Holding it open would block a stale-break unlink on Windows.
        try:
            os.close(fd)
        except OSError:
            pass
    if write_failed:
        # Unlink AFTER the fd is closed: Windows refuses to delete an open file.
        try:
            os.unlink(str(p))
        except OSError:
            pass
        return None
    return True


def release_lock(path: str | Path, token: str | None) -> None:
    """Release a lock previously taken with acquire_lock(). Only unlinks the
    file if it still carries OUR token, so a lock that was broken as stale and
    re-taken by someone else is never deleted out from under its new owner.
    No-op for the unlocked-degraded token. Never raises.

    Why the unlink is not a plain unlink (the TOCTOU this closes): read-then-
    unlink checks identity at the READ and deletes at the PATH. Between the two
    syscalls our lock can be broken as stale (possible whenever a releaser is
    running later than its own stale timeout while genuinely still working) and
    a brand-new lock installed at the same path by another process — and the
    unlink would then delete that stranger's LIVE lock. This is precisely the
    class of bug _break_stale_lock was rebuilt to avoid; the same rename-then-
    confirm-identity pattern is applied here, just from the release path.

    Divergence from _break_stale_lock, deliberate: no st_mtime_ns is captured
    before the rename. There, identity had to be inferred from a stat because
    the token belonged to somebody else; here we hold the exact token, so
    comparing the moved-aside file's CONTENT to it is a strictly stronger
    identity witness than mtime.

    The pre-read is also deliberate and is NOT the check being relied on: if it
    already shows a different token, our lock was broken long ago and release is
    a pure no-op, so we return without renaming anything. Renaming
    unconditionally would move a stranger's live lock aside on every such
    release, opening (on the common path) the very momentarily-empty-path window
    the module comment above flags as the residual risk. The rename only ever
    happens when we still appear to be the owner.
    """
    if not token or token == _NO_LOCK_TOKEN:
        return
    p = Path(path)
    try:
        content = p.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if content != token:
        return  # already broken and re-taken by someone else — nothing to do

    aside = p.with_name(p.name + f".release.{uuid.uuid4().hex}")
    try:
        os.rename(str(p), str(aside))
    except OSError:
        # Already gone, or a sharing violation — someone else is handling it.
        return

    try:
        moved = Path(aside).read_text(encoding="utf-8").strip()
    except OSError:
        moved = None  # cannot confirm identity => treat as not ours

    if moved == token:
        try:
            os.unlink(str(aside))
        except OSError:
            pass
        return

    # We moved a lock that is not ours: ours had already been broken as stale
    # and this is its live successor. Put it back exactly as _break_stale_lock
    # does (rename preserves mtime and the owner's token, so its owner's own
    # release still works).
    if not _rename_noreplace(str(aside), str(p)):
        # A third process took the free path in that window. Do not clobber it;
        # drop the file we should never have moved.
        try:
            os.unlink(str(aside))
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
