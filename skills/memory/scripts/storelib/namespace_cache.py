"""Namespace cache (issue #97): remember the last remote-derived namespace for
each checkout so a transient Git failure can fall back to the cached key
instead of inventing a path-shaped namespace.

Pure stdlib, no storelib sibling imports (standalone-storelib convention, cf.
ops_tokens.py). Every filesystem failure — missing dir, unreadable file,
corrupt JSON, wrong types, unwritable path — FAILS OPEN: ``get`` returns None
and ``put`` is a silent no-op. host.py calls this from hook contexts where
stderr noise and raised exceptions are both unacceptable (issue #97 scope:
"Cache read, write, and JSON corruption failures fail open").

Layout: ``<data_dir>/namespace-cache/<sha256(abs-path-key)>.json`` holding
``{"namespace": str, "written": float}``. The abs-path key runs through
``os.path.normcase`` (identity on POSIX, lowercased on Windows) plus a
forward-slash form, so casing/drive-letter differences never split one
directory into two entries on case-insensitive filesystems while distinct
casings stay distinct on case-sensitive ones (PR #199 review).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path

# Successful remote-derived namespaces stay trusted for one hour (issue #97
# design). After that an ``error`` resolution falls back to ``user:global``
# rather than a possibly stale key.
NAMESPACE_CACHE_TTL_SECONDS = 3600


def _cache_path(data_dir: Path, project_dir: Path) -> Path:
    key = os.path.normcase(os.path.abspath(str(project_dir))).replace("\\", "/")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(data_dir) / "namespace-cache" / f"{digest}.json"


def get_cached_namespace(
    data_dir: Path, project_dir: Path, *, now: float, ttl_seconds: float
) -> str | None:
    """Return the cached namespace for project_dir, or None.

    Expired iff ``now - written > ttl_seconds`` — at exactly ``ttl_seconds``
    of age the entry is still valid (pinned by NamespaceCacheTest). A
    non-finite ``written`` (NaN/Infinity would never satisfy the TTL
    comparison) is treated as corrupt. Any read, parse, or type failure
    returns None (fail open).
    """
    path = _cache_path(data_dir, project_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)
        namespace = entry["namespace"]
        written = float(entry["written"])
    except Exception:
        return None
    if not isinstance(namespace, str) or not namespace:
        return None
    if not math.isfinite(written):
        return None
    if now - written > ttl_seconds:
        return None
    return namespace


def put_cached_namespace(
    data_dir: Path, project_dir: Path, namespace: str, *, now: float
) -> None:
    """Record the successful namespace for project_dir. Best-effort: any
    failure (mkdir, serialize, write, replace) is swallowed. The write goes
    to a UNIQUE per-writer ``*.tmp`` sibling first and ``os.replace``s onto
    the final name, so concurrent hook processes resolving the same checkout
    never clobber each other's in-progress file and a reader never parses
    torn JSON (PR #199 review)."""
    path = _cache_path(data_dir, project_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"namespace": namespace, "written": float(now)}, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def drop_cached_namespace(data_dir: Path, project_dir: Path) -> None:
    """Forget any cached namespace for project_dir. Called when a checkout
    resolves as ``absent`` (its git status is known-good and remote-less), so
    a stale remote key from before ``git remote remove origin`` can never
    resurface through a later ``error`` fallback (PR #199 review). Fail-open:
    a missing or undeletable file is not an error."""
    try:
        _cache_path(data_dir, project_dir).unlink(missing_ok=True)
    except Exception:
        pass
