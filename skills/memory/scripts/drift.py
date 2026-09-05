#!/usr/bin/env python
"""Served-code drift detection by content hash (issue #107, Workstream A PR 2).

Version strings cannot identify served code: three materially different trees
on one machine can all declare the same manifest version, and a check of the
form "served version == manifest version" passes on all of them. This module
gives the runtime surface a CONTENT-addressed identity instead:

  - the release emits ``release-manifest.json`` (``scripts/release_gate.py
    --emit-manifest``) with a sha256 per surface file plus an aggregate
    digest, committed at the repo root;
  - the served side (doctor, the session-start hook, the shared recall body)
    recomputes those hashes over ITS OWN tree — the same tree the launcher
    resolves, because this file lives inside it — and reports matched /
    drifted / unknown.

Surface (the runtime surface, nothing else): ``hooks/**``,
``skills/memory/scripts/**``, ``skills/**/SKILL.md``, ``hermes-plugin/**``,
``scripts/**`` (joined in 0.18.0, issue #108 review round — see _SURFACE_DIRS).
Excluded: any ``__pycache__`` path segment and ``.pyc``/``.pyo`` suffixes.
``release-manifest.json`` sits at the repo root, outside every prefix, so it
is never self-hashed.

Hashing normalizes CRLF to LF before hashing (this repo forces LF via
.gitattributes, but checkouts and cache mirrors cross line-ending configs —
normalizing removes the whole conversion-noise class; the same function runs
on both sides so the two are consistent by construction). CR-only files and
UTF-8 BOMs are NOT normalized — those are exotic and, if they ever appear,
they are real content differences, not conversion noise.

This module is STORE-FREE and side-effect-free at import (constants and
function definitions only; the CLI's file writes happen under ``__main__``).
Importing it never opens, creates, or migrates any sqlite store. The CLI is
fail-open by contract: every mode exits 0 so the hook path can never be
blocked by drift reporting (issue #107 acceptance criterion 2).

Bg-log line format (distinct from the ``zmem-hook`` decision lines so every
existing parser — including the 0.14-0.16 miss-rate join, which skips lines
without ``zmem-hook`` — ignores it):

  [<unix ts>] zmem-drift served=<sha8> release=<sha8> files=<n>

CLI:
  check                       evaluate + print JSON (always exit 0)
  log-once --data-dir D --sid S   marker-guarded: evaluate at most once per
                             session id, append the bg-log line only when
                             drifted, print JSON with an operator
                             ``system_message`` when drifted
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

MANIFEST_NAME = "release-manifest.json"
ALGORITHM = "sha256-crlf-norm"

# Repo-relative (POSIX-separator) directory prefixes of the runtime surface.
# scripts/ joined in 0.18.0 (issue #108 review round): the per-host injection
# canary and the release gate live there — a served tree whose canary was
# modified or dropped must be drift-detectable, not invisible. Keep in sync
# with release_gate's runtime-surface description (this tuple is the single
# source of truth both hash through).
_SURFACE_DIRS = ("hooks/", "skills/memory/scripts/", "hermes-plugin/", "scripts/")
# skills/**/SKILL.md — any depth under skills/, but only there (an unrelated
# docs/SKILL.md is not runtime surface).
_SKILL_MD_SELF = "skills/SKILL.md"

# Excluded path segments / suffixes (build artifacts, never runtime identity).
_EXCLUDED_SEGMENTS = {"__pycache__"}
_EXCLUDED_SUFFIXES = (".pyc", ".pyo")

# Canonical ops-lane session-id rule (same as miss_rate / recall-body): the
# marker filename must stay a single safe path component.
_SID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Same bound/env knob as the shared body's decision logger, so the drift line
# obeys the same growth cap as every other line in zmem-bg.log.
_BG_LOG_DEFAULT_MAX_BYTES = 262144


def _marker_key(sid: str) -> str:
    """Collision-proof marker key: readable truncated prefix + short hash of
    the FULL sanitized sid (hashing the truncated form would still collide
    for sids sharing their first 128 sanitized characters)."""
    full = _SID_SAFE_RE.sub("_", (sid or "")) or "unknown"
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    return f"{full[:128]}-{digest[:8]}"


def _is_surface(rel: str) -> bool:
    """True when a POSIX relpath belongs to the runtime surface."""
    parts = rel.split("/")
    if any(seg in _EXCLUDED_SEGMENTS for seg in parts):
        return False
    if rel.endswith(_EXCLUDED_SUFFIXES):
        return False
    if rel.startswith(_SURFACE_DIRS):
        return True
    return rel == _SKILL_MD_SELF or (
        rel.startswith("skills/") and rel.endswith("/SKILL.md"))


def surface_files(root: Path) -> list:
    """Sorted POSIX relpaths of every surface file under ``root`` (disk walk).

    File symlinks are skipped (parity with release_gate's manifest fallback):
    a link could point outside the tree, and a served mirror that swaps a
    runtime file for a link is drift, not identity."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_SEGMENTS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = Path(full).relative_to(root).as_posix()
            if _is_surface(rel):
                out.append(rel)
    return sorted(out)


def normalize(data: bytes) -> bytes:
    """Hash-time normalization: CRLF -> LF (see module docstring)."""
    return data.replace(b"\r\n", b"\n")


def file_hash(path: Path):
    """sha256 hexdigest of the normalized file bytes, or None when unreadable
    or not a regular file (a FIFO/device/special file is drift, not
    identity — hashing it could hang or exhaust memory)."""
    try:
        import stat as _stat
        st = os.stat(path)
        if not _stat.S_ISREG(st.st_mode):
            return None
        with open(path, "rb") as fh:
            return hashlib.sha256(normalize(fh.read())).hexdigest()
    except OSError:
        return None


def tree_hashes(root: Path) -> dict:
    """{relpath: hash-or-None} over the runtime surface of ``root``."""
    root = Path(root)
    return {rel: file_hash(root / rel) for rel in surface_files(root)}


def aggregate(files: dict) -> str:
    """Deterministic aggregate digest over sorted ``relpath hash`` lines."""
    lines = sorted(f"{rel} {h if h is not None else 'unreadable'}"
                   for rel, h in files.items())
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_manifest(root: Path):
    """Parsed release manifest dict, or None when absent/malformed.

    A manifest that is present but fails ANY structural gate (bad JSON,
    files not a dict of strings, wrong algorithm, digest inconsistent with
    its own file hashes) is malformed — callers distinguish this from
    "absent" via ``manifest_status`` in evaluate()'s result."""
    path = Path(root) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    # Schema gate: hash values must be strings. A hand-edited/corrupt manifest
    # with non-string values would otherwise compare unequal to every served
    # hash and report whole-tree drift (wrong remediation path).
    if not all(isinstance(v, str) for v in files.values()):
        return None
    # Algorithm gate: the field is written by emit and must be honored, not
    # ignored — hashes made under another algorithm are not comparable.
    if data.get("algorithm") != ALGORITHM:
        return None
    # Integrity gate: the digest must equal the aggregate of its own file
    # hashes, or the manifest is internally inconsistent (corrupt).
    if data.get("digest") != aggregate(files):
        return None
    return data


def manifest_present(root: Path) -> bool:
    """True when a release-manifest.json exists (even if unreadable)."""
    return (Path(root) / MANIFEST_NAME).exists()


def evaluate(root: Path, served_hashes: dict | None = None) -> dict:
    """Compare the served tree at ``root`` against its release manifest.

    Returns {"status": matched|drifted|unknown, "manifest_status":
    absent|corrupt|ok, "version", "served" (sha8), "release" (sha8 or None),
    "files_compared", "differing_count", "differing" (first 10 paths)}. A
    path differs on hash mismatch, presence on only one side, or an
    unreadable served file. status unknown + manifest_status corrupt means
    the manifest EXISTS but is unreadable/internally inconsistent — a
    damaged cache mirror, not a pre-manifest tree.

    ``served_hashes`` ({relpath: hash-or-None}) replaces the disk walk:
    release_gate's --verify-manifest passes the TRACKED surface hashes so
    repo-side verification compares the same file set --emit-manifest
    recorded — untracked scratch in a maintainer tree is not part of what
    a release ships and must not fail verify."""
    root = Path(root)
    served = tree_hashes(root) if served_hashes is None else dict(served_hashes)
    served_sha8 = aggregate(served)[:8]
    manifest = load_manifest(root)
    if manifest is None:
        return {
            "status": "unknown",
            "manifest_status": "corrupt" if manifest_present(root) else "absent",
            "version": None,
            "served": served_sha8,
            "release": None,
            "files_compared": len(served),
            "differing_count": 0,
            "differing": [],
        }
    release_sha8 = manifest.get("digest")
    if not isinstance(release_sha8, str) or not release_sha8:
        release_sha8 = aggregate(manifest["files"])[:8]
    differing = sorted(
        rel for rel in set(served) | set(manifest["files"])
        if served.get(rel) != manifest["files"].get(rel)
    )
    return {
        "status": "drifted" if differing else "matched",
        "manifest_status": "ok",
        "version": manifest.get("version"),
        "served": served_sha8,
        "release": release_sha8[:8],
        "files_compared": len(set(served) | set(manifest["files"])),
        "differing_count": len(differing),
        "differing": differing[:10],
    }


def _operator_message(result: dict) -> str:
    # Display hardening: the manifest is data, not trusted text — collapse any
    # non-printable byte (newlines, ANSI escapes) so a hostile version string
    # cannot forge log-like lines in the operator's systemMessage render.
    version = re.sub(r"[^\x20-\x7e]", "?", str(result.get("version") or "unknown"))
    return (
        "zmem: served code drifted from release {v} - {n} runtime file(s) "
        "differ (served {s} vs release {r}). Run zmem doctor "
        "(served-drift) and force a refresh per README Upgrade.".format(
            v=version,
            n=result.get("differing_count", 0),
            s=result.get("served", "-"),
            r=result.get("release") or "-",
        )
    )


def _marker_path(data_dir: Path, sid: str) -> Path:
    return Path(data_dir) / f".drift-checked-{_marker_key(sid)}"


def _bg_log_max_bytes() -> int:
    raw = os.environ.get("ZMEM_BG_LOG_MAX_BYTES", "")
    try:
        value = int(raw) if raw else _BG_LOG_DEFAULT_MAX_BYTES
    except ValueError:
        return _BG_LOG_DEFAULT_MAX_BYTES
    return value if value > 0 else _BG_LOG_DEFAULT_MAX_BYTES


def _append_bg_line(data_dir: Path, result: dict) -> bool:
    """Append the zmem-drift line to <data_dir>/zmem-bg.log. Fail-open."""
    try:
        log_path = Path(data_dir) / "zmem-bg.log"
        try:
            if os.path.getsize(log_path) > _bg_log_max_bytes():
                with open(log_path, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                "[{ts}] zmem-drift served={served} release={release} "
                "files={n}\n".format(
                    ts=int(time.time()),
                    served=result.get("served") or "-",
                    release=result.get("release") or "-",
                    n=result.get("differing_count", 0),
                )
            )
        return True
    except OSError:
        return False


def log_once(root: Path, data_dir: Path, sid: str) -> dict:
    """Evaluate drift at most once per session id and log when drifted.

    The exclusive marker is created FIRST (the .native-nudge-shown race
    pattern): only the process that wins the create logs, so concurrent
    first decisions of one session produce at most one line (issue #107
    acceptance criterion 3). When the marker ALREADY exists this call still
    re-evaluates (log=False) so a session-start that lost the race to a
    recall-body fallback can still surface the operator notice. A stray
    EMPTY directory at the marker path is removed so it cannot suppress the
    session's line; a NON-EMPTY one is left untouched (never delete data we
    did not create) and reported as error. Failure-recovery contract: if the
    evaluate or the bg-log append fails AFTER the marker was created, the
    marker is removed so a later call retries, instead of leaving a permanent
    marker with no drift line."""
    marker = _marker_path(data_dir, sid)
    if os.path.isfile(marker):
        # Lost the race (a sibling writer already logged for this session).
        # Re-evaluate WITHOUT logging so the caller can still render the
        # operator notice; the at-most-once line contract is untouched.
        result = evaluate(root)
        payload = dict(result)
        payload["logged"] = False
        payload["already"] = True
        if result["status"] == "drifted":
            payload["system_message"] = _operator_message(result)
        return payload
    if os.path.isdir(marker):
        # Stray EMPTY directory at the marker path (manual intervention, a
        # partial makedirs): remove so detection is not silently suppressed.
        # A NON-EMPTY directory may hold unrelated data — never delete what
        # we did not create.
        import os as _os
        try:
            _os.rmdir(marker)
        except OSError:
            return {"status": "error", "logged": False}
    try:
        os.makedirs(marker.parent, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, b"checked\n")
        finally:
            os.close(fd)
    except OSError:
        return {"status": "error", "logged": False}

    def _release_marker():
        try:
            os.unlink(marker)
        except OSError:
            pass

    try:
        result = evaluate(root)
    except Exception:
        # Marker must not survive a failed evaluation, or the session's drift
        # line is permanently suppressed (crash-window contract).
        _release_marker()
        return {"status": "error", "logged": False}
    payload = dict(result)
    payload["logged"] = False
    if result["status"] == "drifted":
        payload["logged"] = _append_bg_line(data_dir, result)
        payload["system_message"] = _operator_message(result)
        if not payload["logged"]:
            # Log write failed: release the marker so a later call retries
            # instead of leaving a marker with no corresponding line.
            _release_marker()
    return payload


def main(argv=None) -> int:
    # Same depth as doctor.py --repo-root: this file lives at
    # <root>/skills/memory/scripts/drift.py, so parents[3] IS the served tree
    # root (the tree the launcher resolves — dirname(__dirname)).
    root_default = Path(__file__).resolve().parents[3]
    ap = argparse.ArgumentParser(
        description="Served-code drift check (issue #107); always exits 0")
    sub = ap.add_subparsers(dest="cmd")
    check = sub.add_parser(
        "check", help="evaluate and print JSON (matched|drifted|unknown)")
    check.add_argument("--root", default=str(root_default),
                       help="served tree root (default: this module own tree)")
    once = sub.add_parser(
        "log-once", help="evaluate at most once per session id; append the "
                         "zmem-drift bg-log line when drifted")
    once.add_argument("--root", default=str(root_default),
                      help="served tree root (default: this module own tree)")
    once.add_argument("--data-dir", required=True,
                      help="data dir holding zmem-bg.log and the marker")
    once.add_argument("--sid", required=True, help="session id")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        print(json.dumps(evaluate(Path(args.root))))
        return 0
    if args.cmd == "log-once":
        print(json.dumps(log_once(Path(args.root), Path(args.data_dir),
                                  args.sid)))
        return 0
    # Stdout-purity contract: session-start captures this script's stdout as
    # DRIFT_JSON and json.loads parses it. Help text goes to STDERR with a
    # non-zero exit so a mis-invoked drift.py can never put non-JSON on
    # stdout (a silent operator-notice suppressor).
    ap.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
