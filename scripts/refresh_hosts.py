#!/usr/bin/env python
"""Transactionally refresh the installed zmem host caches.

The updater deliberately has no host-specific installer side effects.  It
validates one release checkout, builds all destination bytes in temporary
files, and then replaces every requested destination as one rollback-capable
transaction.  The only public command-line switches are the four documented
below; ``Path.home()`` is the sole source of the operator home.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DRIFT_SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(DRIFT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(DRIFT_SCRIPTS))

import drift  # noqa: E402
import release_gate  # noqa: E402
import host_registry  # noqa: E402


HOSTS = ("codex", "claude", "zcode")
CANONICAL_MANIFESTS = frozenset(
    {
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".zcode-plugin/plugin.json",
        "hermes-plugin/plugin.yaml",
        "marketplace.json",
    }
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXCLUDED_DIRS = {".git", "graphify-out", "__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
_TEMP_CREATE_ATTEMPTS = 8
_TEMP_RANDOM_BYTES = 16


@dataclass(frozen=True)
class _HostAdapter:
    cache_relative: tuple[str, ...]
    registry_relative: tuple[str, ...] | None = None
    marketplace_sources: tuple[tuple[str, ...], ...] = ()


# Keep all host layout knowledge in this one table.  The final version path is
# appended to ``cache_relative`` so registry installPath and report paths
# always describe the exact destination being replaced.
HOST_ADAPTERS = {
    "codex": _HostAdapter((".codex", "plugins", "cache", "personal", "zmem")),
    "claude": _HostAdapter(
        (".claude", "plugins", "cache", "zmem", "zmem"),
        (".claude", "plugins", "installed_plugins.json"),
    ),
    "zcode": _HostAdapter(
        (".zcode", "cli", "plugins", "cache", "zmem", "zmem"),
        (".zcode", "cli", "plugins", "installed_plugins.json"),
        (("marketplace.json",), (".claude-plugin", "marketplace.json")),
    ),
}


class RefreshError(RuntimeError):
    """A fail-closed validation, staging, commit, or rollback error."""

    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report
        self.mismatches = [
            mismatch
            for row in (report or {}).get("hosts", [])
            for mismatch in row.get("mismatches", [])
        ]


@dataclass
class _Operation:
    host: str
    kind: str
    source: Path
    destination: Path
    backup: Path | None = None
    existed: bool = False
    mutated: bool = False


@dataclass
class _HostPlan:
    host: str
    cache: Path
    registry: Path | None
    marketplaces: list[Path]
    before_digest: str | None
    after_digest: str | None = None
    mismatches: list[str] = field(default_factory=list)
    status: str = "planned"

    def report_row(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "cacheRoot": str(self.cache),
            "registryPath": str(self.registry) if self.registry is not None else None,
            "marketplacePaths": [str(path) for path in self.marketplaces],
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "status": self.status,
            "mismatches": list(self.mismatches),
        }


def _message(exc: BaseException) -> str:
    text = str(exc).strip().replace("\r", " ").replace("\n", " ")
    return text or exc.__class__.__name__


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _reject_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise RefreshError(f"{what} is a symlink: {path}")


def _reject_symlink_components(path: Path, what: str) -> None:
    """Reject symlinks in a lexical path, including existing descendants."""
    candidate = Path(os.path.abspath(str(path)))
    while True:
        if _lexists(candidate):
            _reject_symlink(candidate, what)
        if candidate.parent == candidate:
            return
        candidate = candidate.parent


def _reject_tree_links(root: Path, what: str) -> None:
    """Reject symlinks and non-regular files in an existing destination tree."""
    _reject_symlink(root, what)
    if not root.exists():
        return
    if not root.is_dir():
        raise RefreshError(f"{what} is not a directory: {root}")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        for name in sorted(dirnames):
            child = current / name
            if child.is_symlink():
                raise RefreshError(f"{what} contains a symlink: {child}")
        for name in sorted(filenames):
            child = current / name
            if child.is_symlink():
                raise RefreshError(f"{what} contains a symlink: {child}")
            try:
                mode = os.lstat(child).st_mode
            except OSError as exc:
                raise RefreshError(f"cannot inspect {what} entry {child}: {_message(exc)}") from exc
            if not stat.S_ISREG(mode):
                raise RefreshError(f"{what} contains a non-regular file: {child}")


def _iter_mirror_files(root: Path) -> list[tuple[Path, Path]]:
    """Return a deterministic complete checkout mirror, rejecting links."""
    root = Path(root)
    _reject_symlink(root, "checkout")
    if not root.is_dir():
        raise RefreshError(f"checkout is not a directory: {root}")
    files: list[tuple[Path, Path]] = []

    def onerror(exc: OSError) -> None:
        raise RefreshError(f"cannot enumerate checkout: {_message(exc)}") from exc

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=onerror
    ):
        current = Path(dirpath)
        retained_dirs: list[str] = []
        for name in sorted(dirnames):
            child = current / name
            if child.is_symlink():
                # Symlinked checkout entries are not regular source files and
                # must never be followed into the mirrored host cache.
                continue
            if name in EXCLUDED_DIRS:
                continue
            try:
                mode = os.lstat(child).st_mode
            except OSError as exc:
                raise RefreshError(f"cannot inspect checkout entry {child}: {_message(exc)}") from exc
            if not stat.S_ISDIR(mode):
                raise RefreshError(f"checkout contains a non-directory entry: {child}")
            retained_dirs.append(name)
        dirnames[:] = retained_dirs
        for name in sorted(filenames):
            child = current / name
            # Git worktrees use a root-level ``.git`` file containing a
            # gitdir pointer.  It is checkout metadata just like a .git
            # directory and must not be mirrored into a host cache.
            if current == root and name == ".git":
                continue
            if child.is_symlink():
                continue
            if name.endswith(EXCLUDED_SUFFIXES):
                continue
            try:
                mode = os.lstat(child).st_mode
            except OSError as exc:
                raise RefreshError(f"cannot inspect checkout entry {child}: {_message(exc)}") from exc
            if not stat.S_ISREG(mode):
                raise RefreshError(f"checkout contains a non-regular file: {child}")
            files.append((child, child.relative_to(root)))
    return files


def _nearest_existing_parent(path: Path) -> Path:
    candidate = Path(path)
    _reject_symlink_components(candidate, "staging parent")
    while not candidate.exists():
        if candidate.parent == candidate:
            raise RefreshError(f"no existing parent for {path}")
        candidate = candidate.parent
    _reject_symlink(candidate, "staging parent")
    if not candidate.is_dir():
        raise RefreshError(f"staging parent is not a directory: {candidate}")
    return candidate


def _temp_candidate(prefix: str, parent: Path, suffix: str = "") -> Path:
    return parent / f"{prefix}{secrets.token_hex(_TEMP_RANDOM_BYTES)}{suffix}"


def _collision_exhausted(kind: str, parent: Path) -> RefreshError:
    return RefreshError(
        f"cannot create unique staging {kind} near {parent}: "
        f"collision after {_TEMP_CREATE_ATTEMPTS} attempts"
    )


def _is_permission_failure(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5


def _new_temp_dir(prefix: str, preferred_parent: Path) -> Path:
    parent = _nearest_existing_parent(preferred_parent)
    for _ in range(_TEMP_CREATE_ATTEMPTS):
        path = _temp_candidate(prefix, parent)
        try:
            os.mkdir(str(path), 0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            # Do not delegate to tempfile.mkdtemp: on Windows it may retry
            # access-denied errors when ACLs contradict os.access().
            raise RefreshError(
                f"cannot create staging directory near {preferred_parent}: {_message(exc)}"
            ) from exc
        return path
    raise _collision_exhausted("directory", parent)


def _new_temp_file(prefix: str, parent: Path) -> Path:
    """Create a unique hidden temporary file in an existing destination parent."""
    parent = Path(parent)
    _reject_symlink_components(parent, "staging parent")
    if not _lexists(parent) or not parent.is_dir():
        raise RefreshError(f"staging parent is not a directory: {parent}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(_TEMP_CREATE_ATTEMPTS):
        path = _temp_candidate(prefix, parent, ".tmp")
        try:
            fd = os.open(str(path), flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise RefreshError(
                f"cannot create staging file near {parent}: {_message(exc)}"
            ) from exc
        try:
            os.close(fd)
        except OSError as exc:
            # A successful exclusive open owns this pathname.  Close again in
            # case the first close failed before releasing the descriptor, then
            # remove the reservation before surfacing the failure.
            if not _is_permission_failure(exc):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(str(path))
            except OSError as cleanup_exc:
                raise RefreshError(
                    f"cannot finalize staging file near {parent}: {_message(exc)}; "
                    f"cleanup failed: {_message(cleanup_exc)}"
                ) from exc
            raise RefreshError(
                f"cannot finalize staging file near {parent}: {_message(exc)}"
            ) from exc
        return path
    raise _collision_exhausted("file", parent)


def _new_temp_path(prefix: str, parent: Path) -> Path:
    """Reserve a unique hidden sibling pathname for a directory or file backup."""
    path = _new_temp_file(prefix, parent)
    try:
        os.unlink(str(path))
    except OSError as exc:
        cleanup_exc: OSError | None = None
        if not _is_permission_failure(exc):
            try:
                os.unlink(str(path))
            except OSError as second_exc:
                cleanup_exc = second_exc
        if cleanup_exc is not None and not isinstance(cleanup_exc, FileNotFoundError):
            raise RefreshError(
                f"cannot reserve temporary sibling {path}: {_message(exc)}; "
                f"cleanup failed: {_message(cleanup_exc)}"
            ) from exc
        raise RefreshError(f"cannot reserve temporary sibling {path}: {_message(exc)}") from exc
    return path


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _remove_path(path: Path) -> None:
    if not _lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _runtime_digest(path: Path) -> str | None:
    if not _lexists(path):
        return None
    _reject_tree_links(path, "cache destination")
    if not path.is_dir():
        raise RefreshError(f"cache destination is not a directory: {path}")
    hashes = drift.tree_hashes(path)
    digest = drift.aggregate(hashes)
    if not HEX_DIGEST_RE.fullmatch(digest):
        raise RefreshError(f"invalid runtime digest for {path}: {digest!r}")
    return digest


def _git_head(checkout: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RefreshError(f"cannot read checkout commit with git: {_message(exc)}") from exc
    sha = result.stdout.strip()
    if result.returncode != 0 or not SHA_RE.fullmatch(sha):
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RefreshError(f"cannot read a 40-character checkout commit: {detail}")
    return sha


def _validate_checkout(checkout: Path) -> tuple[str, str, dict[str, str], list[tuple[Path, Path]]]:
    checkout = Path(checkout)
    _reject_symlink(checkout, "checkout")
    if not checkout.exists() or not checkout.is_dir():
        raise RefreshError(f"checkout does not exist or is not a directory: {checkout}")
    try:
        manifests = release_gate.discover_manifests(checkout)
    except Exception as exc:
        raise RefreshError(f"cannot discover host-facing manifests: {_message(exc)}") from exc
    discovered = frozenset(manifests)
    if len(manifests) != len(CANONICAL_MANIFESTS) or discovered != CANONICAL_MANIFESTS:
        missing = sorted(CANONICAL_MANIFESTS - discovered)
        extra = sorted(discovered - CANONICAL_MANIFESTS)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise RefreshError(
            "checkout host-facing manifest set must be the canonical seven paths ("
            + "; ".join(details)
            + ")"
        )
    versions: dict[str, str | None] = {
        rel: release_gate.read_version(rel, checkout) for rel in sorted(manifests)
    }
    unreadable = sorted(rel for rel, version in versions.items() if not version)
    if unreadable:
        raise RefreshError(
            "host-facing manifest has no parseable version: " + ", ".join(unreadable)
        )
    distinct = {version for version in versions.values() if version is not None}
    if len(distinct) != 1:
        details = ", ".join(f"{rel}={versions[rel]!r}" for rel in sorted(versions))
        raise RefreshError(f"host-facing manifests disagree on version: {details}")
    version = next(iter(distinct))
    if not SEMVER_RE.fullmatch(version):
        raise RefreshError(f"host-facing manifests declare a non-semantic version: {version!r}")
    try:
        gate_status = release_gate.verify_manifest(checkout)
    except Exception as exc:
        raise RefreshError(f"release-manifest verification failed: {_message(exc)}") from exc
    if gate_status != 0:
        raise RefreshError("release-manifest verification failed; regenerate the committed manifest")
    commit_sha = _git_head(checkout)
    mirror = _iter_mirror_files(checkout)
    expected_hashes = drift.tree_hashes(checkout)
    if not expected_hashes:
        raise RefreshError("checkout has no runtime-surface files")
    if any(value is None for value in expected_hashes.values()):
        raise RefreshError("checkout contains an unreadable runtime-surface file")
    return version, commit_sha, expected_hashes, mirror


def _adapter_paths(home: Path, host: str, version: str) -> tuple[Path, Path | None, list[Path]]:
    adapter = HOST_ADAPTERS[host]
    cache = home.joinpath(*adapter.cache_relative, version)
    registry = home.joinpath(*adapter.registry_relative) if adapter.registry_relative else None
    marketplace_root = home / ".zcode" / "cli" / "plugins" / "marketplaces" / "zmem"
    marketplaces = [marketplace_root.joinpath(*relative) for relative in adapter.marketplace_sources]
    return cache, registry, marketplaces


def _validate_destinations(plans: list[_HostPlan]) -> None:
    seen: set[str] = set()
    for plan in plans:
        paths: Iterable[Path] = [plan.cache]
        if plan.registry is not None:
            paths = (*paths, plan.registry)
        paths = (*paths, *plan.marketplaces)
        for path in paths:
            _reject_symlink_components(path, "destination")
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen:
                raise RefreshError(f"duplicate destination in host plan: {path}")
            seen.add(key)
            if _lexists(path):
                if path.is_symlink():
                    raise RefreshError(f"destination is a symlink: {path}")
                if path == plan.cache:
                    _reject_tree_links(path, "cache destination")
                elif not path.is_file():
                    raise RefreshError(f"destination is not a regular file: {path}")
        if plan.registry is not None and not _lexists(plan.registry):
            raise RefreshError(f"required host registry is missing: {plan.registry}")
        for marketplace in plan.marketplaces:
            if _lexists(marketplace) and not marketplace.is_file():
                raise RefreshError(f"marketplace destination is not a regular file: {marketplace}")


def _absolute_path(path: Path) -> Path:
    """Make a path absolute without following symlinks."""
    return Path(os.path.abspath(str(path)))


def _path_key(path: Path) -> str:
    # ``realpath`` canonicalizes Windows 8.3 aliases as well as symlink and
    # junction components for comparison.  Callers still validate the
    # original lexical path for symlinks before using this key for safety
    # decisions.
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _path_is_within(path: Path, root: Path) -> bool:
    """Return whether path is root or a descendant, lexically."""
    candidate = _path_key(path)
    root_key = _path_key(root)
    try:
        return os.path.commonpath((candidate, root_key)) == root_key
    except ValueError:
        # Windows drives (or other roots) cannot be descendants of one
        # another when commonpath rejects the comparison.
        return False


def _validate_report_target(
    report_path: Path,
    checkout: Path,
    destinations: list[tuple[str, Path]],
    cache_roots: list[tuple[str, Path]],
) -> None:
    """Reject a report target before it can overwrite any transaction input."""
    report_path = _absolute_path(report_path)
    if _lexists(report_path) and report_path.is_symlink():
        raise RefreshError(f"report path is a symlink: {report_path}")
    if _lexists(report_path) and not report_path.is_file():
        raise RefreshError(f"report path is not a regular file: {report_path}")
    cursor = report_path.parent
    while True:
        if _lexists(cursor):
            _reject_symlink(cursor, "report parent")
            if not cursor.is_dir():
                raise RefreshError(f"report parent is not a directory: {cursor}")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if _path_is_within(report_path, checkout):
        raise RefreshError(f"report path is inside checkout: {report_path}")

    report_key = _path_key(report_path)
    for description, destination in destinations:
        if report_key == _path_key(destination):
            raise RefreshError(f"report path conflicts with {description}: {report_path}")
    for description, cache_root in cache_roots:
        if _path_is_within(report_path, cache_root):
            raise RefreshError(f"report path is inside {description} directory: {report_path}")


def _validate_report_path(report_path: Path, checkout: Path, plans: list[_HostPlan]) -> None:
    """Reject report targets that could overwrite or be nested in inputs."""
    destinations: list[tuple[str, Path]] = []
    cache_roots: list[tuple[str, Path]] = []
    for plan in plans:
        destinations.append((f"{plan.host} cache", plan.cache))
        cache_roots.append((f"{plan.host} cache", plan.cache))
        if plan.registry is not None:
            destinations.append((f"{plan.host} registry", plan.registry))
        destinations.extend((f"{plan.host} marketplace", path) for path in plan.marketplaces)
    _validate_report_target(report_path, checkout, destinations, cache_roots)


def _report_destination_skeleton(
    home: Path,
    hosts: tuple[str, ...],
    version: str | None,
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Build report collision targets without reading or staging destinations."""
    destinations: list[tuple[str, Path]] = []
    cache_roots: list[tuple[str, Path]] = []
    for host in hosts:
        cache, registry, marketplaces = _adapter_paths(home, host, version or "unknown")
        if version is not None:
            destinations.append((f"{host} cache", cache))
            cache_roots.append((f"{host} cache", cache))
        if registry is not None:
            destinations.append((f"{host} registry", registry))
        destinations.extend((f"{host} marketplace", path) for path in marketplaces)
    return destinations, cache_roots


def _stage_plan(
    home: Path,
    checkout: Path,
    hosts: tuple[str, ...],
    version: str,
    commit_sha: str,
    expected_hashes: dict[str, str],
    mirror: list[tuple[Path, Path]],
    created_dirs: list[Path],
) -> tuple[list[_HostPlan], list[_Operation]]:
    plans: list[_HostPlan] = []
    operations: list[_Operation] = []
    expected_digest = drift.aggregate(expected_hashes)

    # Build and validate every destination skeleton before reading an existing
    # cache or registry.  In particular, a destination with a symlinked
    # ancestor must fail before any read through that ancestor can occur.
    for host in hosts:
        cache, registry, marketplaces = _adapter_paths(home, host, version)
        plans.append(_HostPlan(host, cache, registry, marketplaces, None, expected_digest))
    _validate_destinations(plans)
    _ensure_parent_paths(
        (
            path
            for plan in plans
            for path in (
                plan.cache,
                *(tuple([plan.registry]) if plan.registry is not None else ()),
                *plan.marketplaces,
            )
        ),
        created_dirs,
    )

    staged_paths: list[Path] = []
    try:
        for plan in plans:
            plan.before_digest = _runtime_digest(plan.cache)

            staged_cache = _new_temp_dir(
                f".zmem-refresh-{plan.host}-cache-", plan.cache.parent
            )
            staged_paths.append(staged_cache)
            for source, relative in mirror:
                target = staged_cache / relative
                _copy_file(source, target)
            staged_hashes = drift.tree_hashes(staged_cache)
            if staged_hashes != expected_hashes:
                differing = sorted(
                    rel for rel in set(staged_hashes) | set(expected_hashes)
                    if staged_hashes.get(rel) != expected_hashes.get(rel)
                )
                raise RefreshError(
                    f"staged {plan.host} cache runtime digest mismatch: "
                    f"{', '.join(differing[:5]) or 'aggregate mismatch'}"
                )
            if drift.aggregate(staged_hashes) != expected_digest:
                raise RefreshError(f"staged {plan.host} cache aggregate digest mismatch")
            operations.append(_Operation(plan.host, "cache", staged_cache, plan.cache))

            if plan.registry is not None:
                try:
                    schema_version, loaded = host_registry.load_host_registry(
                        plan.registry, plan.host
                    )
                    del schema_version
                    updated = host_registry.update_host_registry(
                        loaded,
                        host=plan.host,
                        version=version,
                        git_commit_sha=commit_sha,
                        install_path=str(plan.cache),
                    )
                    staged_registry = _new_temp_file(
                        f".zmem-refresh-{plan.host}-registry-", plan.registry.parent
                    )
                    staged_paths.append(staged_registry)
                    host_registry.write_host_registry(staged_registry, updated)
                except Exception as exc:
                    raise RefreshError(
                        f"cannot stage {plan.host} registry {plan.registry}: {_message(exc)}"
                    ) from exc
                operations.append(
                    _Operation(plan.host, "registry", staged_registry, plan.registry)
                )

            for index, marketplace in enumerate(plan.marketplaces):
                relative = HOST_ADAPTERS[plan.host].marketplace_sources[index]
                source = checkout.joinpath(*relative)
                if source.is_symlink() or not source.is_file():
                    raise RefreshError(
                        f"required marketplace source is missing or invalid: {source}"
                    )
                staged_marketplace = _new_temp_file(
                    f".zmem-refresh-{plan.host}-marketplace-", marketplace.parent
                )
                staged_paths.append(staged_marketplace)
                try:
                    _copy_file(source, staged_marketplace)
                except Exception as exc:
                    raise RefreshError(
                        f"cannot stage marketplace {source}: {_message(exc)}"
                    ) from exc
                operations.append(
                    _Operation(plan.host, "marketplace", staged_marketplace, marketplace)
                )
    except Exception:
        for path in reversed(staged_paths):
            _remove_path(path)
        raise
    return plans, operations


def _backup_operations(operations: list[_Operation]) -> None:
    for operation in operations:
        destination = operation.destination
        if not _lexists(destination):
            operation.existed = False
            continue
        backup = _new_temp_path(
            f".zmem-refresh-{operation.host}-{operation.kind}-backup-",
            destination.parent,
        )
        try:
            if destination.is_dir() and not destination.is_symlink():
                _reject_tree_links(destination, "destination backup")
                shutil.copytree(destination, backup, symlinks=False)
            elif destination.is_file() and not destination.is_symlink():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            else:
                raise RefreshError(f"destination is not backupable: {destination}")
        except Exception as exc:
            _remove_path(backup)
            raise RefreshError(f"cannot back up {destination}: {_message(exc)}") from exc
        # Do not mark an operation as having a preimage until the complete
        # copy succeeded.  If a backup copy fails, commit has not started and
        # rollback must leave that untouched destination alone.
        operation.existed = True
        operation.backup = backup


def _cleanup_operation_temps(
    operations: list[_Operation], retained_backups: set[Path] | None = None
) -> None:
    """Best-effort removal of staged sources and sibling backup preimages."""
    retained = retained_backups or set()
    for operation in operations:
        for path in (operation.source, operation.backup):
            if path is None or (path == operation.backup and path in retained):
                continue
            try:
                _remove_path(path)
            except OSError:
                pass


def _ensure_parent_paths(destinations: Iterable[Path], created: list[Path]) -> None:
    seen: set[str] = set()
    for destination in destinations:
        parent = destination.parent
        _reject_symlink_components(parent, "destination parent")
        missing: list[Path] = []
        cursor = parent
        while not _lexists(cursor):
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RefreshError(f"cannot create destination parent for {destination}")
            cursor = cursor.parent
        _reject_symlink(cursor, "destination parent")
        if not cursor.is_dir():
            raise RefreshError(f"destination parent is not a directory: {cursor}")
        for path in reversed(missing):
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen:
                continue
            try:
                path.mkdir()
            except OSError as exc:
                raise RefreshError(f"cannot create destination parent {path}: {_message(exc)}") from exc
            _reject_symlink_components(path, "destination parent")
            created.append(path)
            seen.add(key)


def _ensure_destination_parents(operations: list[_Operation], created: list[Path]) -> None:
    _ensure_parent_paths((operation.destination for operation in operations), created)


def _ensure_report_parent(path: Path, created: list[Path]) -> None:
    """Create a report parent while recording every directory we create."""
    parent = path.parent
    _reject_symlink_components(parent, "report parent")
    missing: list[Path] = []
    cursor = parent
    while not _lexists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            raise RefreshError(f"cannot create report parent for {path}")
        cursor = cursor.parent
    _reject_symlink(cursor, "report parent")
    if not cursor.is_dir():
        raise RefreshError(f"report parent is not a directory: {cursor}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except OSError as exc:
            raise RefreshError(f"cannot create report parent {directory}: {_message(exc)}") from exc
        created.append(directory)


def _cleanup_created_dirs(created_dirs: list[Path]) -> list[str]:
    failures: list[str] = []
    for path in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if _lexists(path) and path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                path.rmdir()
        except Exception as exc:
            failures.append(f"rollback cleanup failed for {path}: {_message(exc)}")
    return failures


def _rollback(
    operations: list[_Operation],
    created_dirs: list[Path],
    retained_backups: set[Path] | None = None,
) -> list[str]:
    failures: list[str] = []
    retained = retained_backups if retained_backups is not None else set()
    for operation in reversed(operations):
        if not operation.mutated:
            continue
        destination = operation.destination
        try:
            if operation.existed:
                if operation.backup is None or not _lexists(operation.backup):
                    raise RefreshError(f"preimage backup is missing for {destination}")
                _remove_path(destination)
                if operation.backup.is_dir():
                    shutil.copytree(operation.backup, destination, symlinks=False)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(operation.backup, destination)
            else:
                _remove_path(destination)
        except Exception as exc:
            failure = f"rollback failed for {destination}: {_message(exc)}"
            backup = operation.backup
            if operation.existed and backup is not None and _lexists(backup):
                retained.add(backup)
                failure += f"; preimage retained at {backup}"
            failures.append(failure)
    # Temporary sources and successfully restored backups must be gone before
    # created destination parents are considered for removal.  Retained
    # preimages are deliberately excluded so they remain recoverable.
    _cleanup_operation_temps(operations, retained)
    failures.extend(_cleanup_created_dirs(created_dirs))
    return failures


def _atomic_write_report(
    path: Path,
    report: dict[str, Any],
    created_dirs: list[Path] | None = None,
) -> None:
    _validate_report(report)
    path = _absolute_path(Path(path))
    if _lexists(path) and path.is_symlink():
        raise RefreshError(f"report path is a symlink: {path}")
    tracked_dirs = created_dirs if created_dirs is not None else []
    _ensure_report_parent(path, tracked_dirs)
    data = (
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), delete=False
        ) as fh:
            temp_name = fh.name
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


_REPORT_KEYS = frozenset(
    {"checkout", "version", "gitCommitSha", "hosts", "mismatchCount", "ok"}
)
_HOST_ROW_KEYS = frozenset(
    {
        "host",
        "cacheRoot",
        "registryPath",
        "marketplacePaths",
        "beforeDigest",
        "afterDigest",
        "status",
        "mismatches",
    }
)


def _validate_report(report: dict[str, Any]) -> None:
    """Validate the closed report contract without a jsonschema dependency."""
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        raise ValueError("refresh report has an unexpected top-level shape")
    for key in ("checkout", "version"):
        if not isinstance(report[key], str) or not report[key]:
            raise ValueError(f"refresh report {key} must be a nonempty string")
    commit_sha = report["gitCommitSha"]
    if not isinstance(commit_sha, str) or not SHA_RE.fullmatch(commit_sha):
        raise ValueError("refresh report gitCommitSha must be a 40-character SHA")
    hosts = report["hosts"]
    if not isinstance(hosts, list) or not hosts or len(hosts) > len(HOSTS):
        raise ValueError("refresh report hosts must be a nonempty list of hosts")
    names: list[str] = []
    for row in hosts:
        if not isinstance(row, dict) or set(row) != _HOST_ROW_KEYS:
            raise ValueError("refresh report host record has an unexpected shape")
        name = row["host"]
        if name not in HOSTS or name in names:
            raise ValueError("refresh report hosts must be unique supported hosts")
        names.append(name)
        if names != sorted(names, key=HOSTS.index):
            raise ValueError("refresh report hosts must use canonical order")
        for key in ("cacheRoot", "status"):
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError(f"refresh report {name} {key} must be nonempty")
        registry_path = row["registryPath"]
        if registry_path is not None and (not isinstance(registry_path, str) or not registry_path):
            raise ValueError(f"refresh report {name} registryPath is invalid")
        marketplace_paths = row["marketplacePaths"]
        if not isinstance(marketplace_paths, list) or any(
            not isinstance(path, str) or not path for path in marketplace_paths
        ):
            raise ValueError(f"refresh report {name} marketplacePaths are invalid")
        for key in ("beforeDigest", "afterDigest"):
            digest = row[key]
            if digest is not None and (not isinstance(digest, str) or not HEX_DIGEST_RE.fullmatch(digest)):
                raise ValueError(f"refresh report {name} {key} is not a SHA-256 digest")
        mismatches = row["mismatches"]
        if not isinstance(mismatches, list) or any(not isinstance(item, str) for item in mismatches):
            raise ValueError(f"refresh report {name} mismatches are invalid")
    mismatch_count = report["mismatchCount"]
    if not isinstance(mismatch_count, int) or isinstance(mismatch_count, bool) or mismatch_count < 0:
        raise ValueError("refresh report mismatchCount must be a nonnegative integer")
    if mismatch_count != sum(len(row["mismatches"]) for row in hosts):
        raise ValueError("refresh report mismatchCount does not match host mismatches")
    if not isinstance(report["ok"], bool):
        raise ValueError("refresh report ok must be boolean")
    if report["ok"] and mismatch_count:
        raise ValueError("successful refresh report cannot contain mismatches")


def _build_report(
    checkout: Path,
    version: str | None,
    commit_sha: str | None,
    plans: list[_HostPlan],
    *,
    dry_run: bool,
    ok: bool,
) -> dict[str, Any]:
    hosts = [plan.report_row() for plan in plans]
    mismatch_count = sum(len(row["mismatches"]) for row in hosts)
    return {
        "checkout": str(checkout),
        # The report schema intentionally requires release identity even for a
        # failed preflight.  All-zero values are an explicit unavailable
        # sentinel; the host mismatches carry the actionable reason.
        "gitCommitSha": commit_sha if commit_sha and SHA_RE.fullmatch(commit_sha) else "0" * 40,
        "hosts": hosts,
        "mismatchCount": mismatch_count,
        "ok": bool(ok and mismatch_count == 0),
        "version": version or "unknown",
    }


def _failed_plans(home: Path, hosts: tuple[str, ...], version: str | None, message: str) -> list[_HostPlan]:
    plans: list[_HostPlan] = []
    for host in hosts:
        cache, registry, marketplaces = _adapter_paths(home, host, version or "unknown")
        plans.append(
            _HostPlan(
                host,
                cache,
                registry,
                marketplaces,
                None,
                None,
                [message],
                "failed",
            )
        )
    return plans


def _refresh_transaction(
    home: Path,
    checkout: Path,
    hosts: tuple[str, ...],
    report_path: Path | None = None,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not hosts or any(host not in HOST_ADAPTERS for host in hosts):
        unknown = [host for host in hosts if host not in HOST_ADAPTERS]
        raise RefreshError("unknown host(s): " + ", ".join(unknown or ["<empty>"]))
    if len(set(hosts)) != len(hosts):
        raise RefreshError("host list contains duplicates")
    home = Path(home)
    # Reject the caller's lexical home path before resolve() can erase a
    # symlinked component and make writes escape the requested tree.
    _reject_symlink_components(home, "home")
    home = home.resolve()
    checkout = Path(checkout)
    # Check the spelling supplied by the caller before resolving it.  Resolving
    # first would turn a symlinked checkout root into an apparently ordinary
    # directory and undermine the no-links mirror contract.
    _reject_symlink(checkout, "checkout")
    checkout = checkout.resolve()
    report_path = _absolute_path(Path(report_path)) if report_path is not None else None
    plans: list[_HostPlan] = []
    version: str | None = None
    commit_sha: str | None = None
    created_dirs: list[Path] = []
    report_created_dirs: list[Path] = []
    report_preflight_ok = report_path is None
    operations: list[_Operation] = []
    retained_backups: set[Path] = set()
    try:
        if report_path is not None:
            skeleton, skeleton_caches = _report_destination_skeleton(home.resolve(), hosts, None)
            _validate_report_target(report_path, checkout, skeleton, skeleton_caches)
            # Registry/marketplace collisions are now proven absent.  This
            # permits an actionable failure report if checkout validation
            # itself fails before a release version can be discovered.
            report_preflight_ok = True
        version, commit_sha, expected_hashes, mirror = _validate_checkout(checkout)
        if report_path is not None:
            report_preflight_ok = False
            skeleton, skeleton_caches = _report_destination_skeleton(home.resolve(), hosts, version)
            _validate_report_target(report_path, checkout, skeleton, skeleton_caches)
            report_preflight_ok = True
        plans = []
        plans, operations = _stage_plan(
            home.resolve(),
            checkout,
            hosts,
            version,
            commit_sha,
            expected_hashes,
            mirror,
            created_dirs,
        )
        if dry_run:
            for plan in plans:
                plan.status = "dry-run"
            report = _build_report(checkout, version, commit_sha, plans, dry_run=True, ok=True)
            if report_path is not None:
                _atomic_write_report(report_path, report, report_created_dirs)
            return report

        _backup_operations(operations)
        for operation in operations:
            try:
                # Mark the destination as mutated before removing its
                # preimage.  If os.replace itself fails, rollback still knows
                # that this destination must be restored/removed.
                operation.mutated = True
                _remove_path(operation.destination)
                os.replace(str(operation.source), str(operation.destination))
            except Exception as exc:
                raise RefreshError(
                    f"replacement failed for {operation.destination}: {_message(exc)}"
                ) from exc
        for plan in plans:
            actual_digest = _runtime_digest(plan.cache)
            if actual_digest != plan.after_digest:
                raise RefreshError(
                    f"installed {plan.host} cache runtime digest mismatch: "
                    f"expected {plan.after_digest}, got {actual_digest}"
                )
            plan.after_digest = actual_digest
        for plan in plans:
            plan.status = "refreshed"
        report = _build_report(checkout, version, commit_sha, plans, dry_run=False, ok=True)
        if report_path is not None:
            try:
                _atomic_write_report(report_path, report, report_created_dirs)
            except Exception as exc:
                raise RefreshError(f"final report write failed: {_message(exc)}") from exc
        return report
    except Exception as exc:
        failure = _message(exc)
        plans_were_validated = bool(plans)
        if plans:
            for plan in plans:
                plan.status = "failed"
                plan.mismatches.append(failure)
        else:
            plans = _failed_plans(home.resolve(), hosts, version, failure)
        rollback_failures: list[str] = []
        if not dry_run and operations:
            rollback_failures = _rollback(operations, created_dirs, retained_backups)
            for plan in plans:
                plan.mismatches.extend(rollback_failures)
        elif not dry_run:
            cleanup_failures = _cleanup_created_dirs(created_dirs)
            for plan in plans:
                plan.mismatches.extend(cleanup_failures)
        # The failure report describes the post-rollback reality.  In
        # particular, afterDigest must not retain the planned new digest when
        # the old preimage was restored.
        if plans_were_validated:
            for plan in plans:
                try:
                    plan.after_digest = _runtime_digest(plan.cache)
                except Exception as digest_exc:
                    plan.after_digest = None
                    plan.mismatches.append(
                        f"cannot recompute restored {plan.host} cache digest: "
                        f"{_message(digest_exc)}"
                    )
        report = _build_report(checkout, version, commit_sha, plans, dry_run=dry_run, ok=False)
        if report_path is not None and report_preflight_ok:
            try:
                _atomic_write_report(report_path, report, report_created_dirs)
            except Exception:
                # A report replacement failure must preserve an existing
                # report and must never hide the failed transaction.
                cleanup_failures = _cleanup_created_dirs(report_created_dirs)
                for plan in plans:
                    plan.mismatches.extend(cleanup_failures)
        raise RefreshError(failure, report) from exc
    finally:
        _cleanup_operation_temps(operations, retained_backups)
        if dry_run:
            _cleanup_created_dirs(created_dirs)


def refresh_host(
    checkout: Path,
    host: str,
    home: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Refresh one host and return its report row without writing a report."""
    if host not in HOST_ADAPTERS:
        raise RefreshError(f"unknown host: {host}")
    report = _refresh_transaction(
        Path(home),
        Path(checkout),
        (host,),
        dry_run=bool(dry_run),
    )
    return report["hosts"][0]


def _parser() -> argparse.ArgumentParser:
    # Keep the documented hosts default on one line so scheduled-task logs and
    # the public help contract expose the complete canonical host order.
    parser = argparse.ArgumentParser(
        description="Refresh zmem host caches transactionally",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="default hosts: codex,claude,zcode",
    )
    parser.add_argument(
        "--checkout",
        dest="checkout",
        type=str,
        required=True,
        help="checkout directory to mirror",
    )
    parser.add_argument(
        "--report",
        dest="report",
        type=str,
        required=True,
        help="JSON report output path",
    )
    parser.add_argument(
        "--hosts",
        dest="hosts",
        type=str,
        default=",".join(HOSTS),
        help="comma-separated host caches to refresh",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="validate and report without replacing files",
    )
    return parser


def _parse_hosts(raw: str, parser: argparse.ArgumentParser) -> tuple[str, ...]:
    values = [part.strip() for part in raw.split(",")]
    if not values or any(not value for value in values):
        parser.error("--hosts must contain one or more nonempty host names")
    if len(values) != len(set(values)):
        parser.error("--hosts must not contain duplicates")
    unknown = [value for value in values if value not in HOST_ADAPTERS]
    if unknown:
        parser.error("unknown host(s): " + ", ".join(unknown))
    # Canonical ordering makes reports and replacement order independent of
    # command-line permutation while still permitting a strict subset.
    return tuple(host for host in HOSTS if host in values)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    hosts = _parse_hosts(args.hosts, parser)
    try:
        _refresh_transaction(
            Path.home(),
            Path(args.checkout),
            hosts,
            Path(args.report),
            dry_run=bool(args.dry_run),
        )
    except RefreshError as exc:
        print(f"[refresh-hosts] refresh failed: {_message(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
