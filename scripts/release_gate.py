#!/usr/bin/env python
"""Release gate: make the repo's release contract self-enforcing.

The documented contract (README / CHANGELOG) is "released versions are marked
with a git tag (vX.Y.Z) and a GitHub Release". That step was manual, and the
manual step silently rotted: v0.8.5/v0.8.6/v0.8.8 were never tagged and
v0.9.0/v0.10.0 shipped as manifest bumps without any tag or Release, so
every release-tracking downstream stayed on old versions until the releases
were retrofitted by hand. This module is the decision logic the Release
workflow runs on every push to main:

  1. Enumerate EVERY tracked host-facing manifest (plugin.json /
     plugin.yaml / marketplace.json — via `git ls-files`, never a hardcoded
     list, so a newly added surface can never silently escape the check).
  2. Fail loudly if their versions disagree (a partial version bump is a
     release violation, not a warning).
  3. Fail loudly if the CHANGELOG has no `## [X.Y.Z]` section matching the
     manifest version (no naked version bumps).
  4. If tag `vX.Y.Z` already exists → already released; skip (idempotent).
  5. Otherwise emit `should_release=true` plus the extracted CHANGELOG notes
     so the workflow can cut the tag + GitHub Release at the MAIN commit
     that carries the version (never a PR-branch head — squash merges orphan
     branch-head tags).

Exit codes: 0 gate passed (release OR skip), 1 contract violation.
Network-free: only `git ls-files` / `git rev-parse` against the local clone.
Runnable standalone for local pre-flight: `python scripts/release_gate.py`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"

# A "host-facing manifest" is any tracked plugin/marketplace descriptor that
# carries a version. Matched against `git ls-files` output so the inventory
# is discovered, not remembered (the seven-surface lesson: enumerating from
# memory is how a surface gets missed).
MANIFEST_RE = re.compile(r"(?:^|/)(?:plugin\.(?:json|yaml)|marketplace\.json)$")

# CHANGELOG released sections look like `## [0.10.1] — 2026-08-25`.
# `[Unreleased]` is explicitly excluded so it can sit above the newest
# released section without being mistaken for one.
SECTION_RE = re.compile(
    r"^## \[(?!Unreleased\b)([0-9]+\.[0-9]+\.[0-9]+)\][^\n]*$", re.MULTILINE
)

# Fallback floor for the manifest count: the repo ships seven today; the
# point of the floor is to catch an accidentally DELETED surface, while a
# newly added surface is picked up automatically by the enumeration.
MIN_MANIFESTS = 5

_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def discover_manifests() -> list[str]:
    """Tracked manifest paths, repo-relative, sorted (deterministic order)."""
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {out.stderr.strip()}")
    return sorted(
        ln for ln in out.stdout.splitlines() if MANIFEST_RE.search(ln)
    )


def read_version(rel_path: str) -> str | None:
    """The version a manifest declares, or None when it carries none.

    Handles the three shipped shapes: top-level `version` (plugin.json),
    `plugins[0].version` (marketplace.json), and a `version:` line
    (plugin.yaml — parsed with a regex so no yaml dependency is needed).
    """
    path = REPO_ROOT / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix == ".yaml":
        m = _VERSION_RE.search(text)
        if not m:
            return None
        # Tolerate quoted YAML scalars (`version: "0.10.1"`) so a formatting
        # change cannot break parity with a confusing mismatch.
        return m.group(1).strip("\"'")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data.get("version"), str):
        return data["version"]
    plugins = data.get("plugins")
    if isinstance(plugins, list) and plugins and isinstance(plugins[0], dict):
        v = plugins[0].get("version")
        if isinstance(v, str):
            return v
    return None


def latest_changelog_section(text: str) -> tuple[str, str] | None:
    """(version, body) of the NEWEST released `## [X.Y.Z]` section.

    The changelog is newest-first, so the first match wins. The body runs to
    the next `## ` header (or EOF) and is returned verbatim minus the header
    line and leading blank lines.
    """
    m = SECTION_RE.search(text)
    if not m:
        return None
    start = m.end()
    nxt = text.find("\n## ", start)
    body = text[start:] if nxt < 0 else text[start:nxt]
    return m.group(1), body.lstrip("\n").rstrip() + "\n"


def tag_exists(version: str) -> bool:
    out = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/v{version}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    return out.returncode == 0


def _github_output(name: str, value: str) -> None:
    """Append `name=value` to the workflow outputs file when running in CI."""
    gh_file = os.environ.get("GITHUB_OUTPUT")
    if not gh_file:
        return
    with open(gh_file, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def main() -> int:
    manifests = discover_manifests()
    if len(manifests) < MIN_MANIFESTS:
        print(f"::error::only {len(manifests)} host-facing manifests found "
              f"(floor {MIN_MANIFESTS}) — did a release surface get deleted?")
        return 1

    versions: dict[str, str | None] = {p: read_version(p) for p in manifests}
    unreadable = [p for p, v in versions.items() if not v]
    if unreadable:
        for p in unreadable:
            print(f"::error::{p} declares no parseable version")
        return 1
    distinct = set(versions.values())  # type: ignore[arg-type]
    if len(distinct) != 1:
        print("::error::host-facing manifests disagree on version "
              "(partial version bump):")
        for p in manifests:
            print(f"::error::  {p} -> {versions[p]}")
        return 1
    version = next(iter(distinct))  # type: ignore[arg-type]

    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    section = latest_changelog_section(changelog)
    if section is None:
        print(f"::error::CHANGELOG.md has no released `## [X.Y.Z]` section "
              f"but the manifests declare {version}")
        return 1
    cl_version, notes = section
    if cl_version != version:
        print(f"::error::manifests declare {version} but the newest "
              f"CHANGELOG release section is [{cl_version}] — add the "
              f"`## [{version}]` section or fix the manifests")
        return 1

    if tag_exists(version):
        print(f"[release-gate] v{version} is already tagged (merges without "
              f"a version bump never re-release). The publish step's "
              f"`gh release view` check remains the sole idempotency guard "
              f"so a bare tag without a Release still heals.")
    else:
        print(f"[release-gate] v{version} is not tagged yet — the publish "
              f"step will cut it if the Release is missing.")

    notes_path = Path(tempfile.gettempdir()) / f"zmem-release-notes-{version}.md"
    notes_path.write_text(notes, encoding="utf-8", newline="\n")
    print(f"[release-gate] resolved {version} "
          f"(notes: {len(notes.splitlines())} lines from CHANGELOG)")
    _github_output("version", version)
    _github_output("notes_path", str(notes_path))
    _github_output("title", f"zmem {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
