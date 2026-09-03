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
  4. Emit the resolved version, the release title, and the extracted
     CHANGELOG notes so the workflow can publish the tag + GitHub Release
     at the MAIN commit that carries the version (never a PR-branch head —
     squash merges orphan branch-head tags). Tag existence is reported
     informationally only; publish idempotency is RELEASE existence, checked
     by the workflow's `gh release view` step (a bare tag without a Release
     heals on the next merge).

Issue #106 adds a second mode, `--check-unreleased-drift`, wired into ci.yml
for pushes to main only: fail when `## [Unreleased]` carries content on a
merge whose HEAD does not carry a version bump. That is the exact history that
produced the issue — PRs #86–#104 (2026-08-28 → 09-02) merged user-facing
work under `[Unreleased]` while every manifest sat static at 0.13.1, so no
installed client ever received eight merged PRs (clients discover new versions
by comparing the manifest version against the marketplace entry). Escape:
`[skip release]` in the merge subject — chosen over a path-filter allowlist
because docs/CI-only merges would need an evolving allowlist, while the marker
is an explicit operator choice and works at any checkout depth. With no flags
the default mode's behavior and stdout are byte-identical to the pre-#106
gate (the Release workflow and its tests depend on that).

Exit codes: 0 gate passed (release OR skip), 1 contract violation.
Network-free: only `git ls-files` / `git rev-parse` / `git show` / `git log`
against the local clone.
Runnable standalone for local pre-flight: `python scripts/release_gate.py`.
"""

from __future__ import annotations

import argparse
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

# The drift gate's escape marker (issue #106). Chosen over a path-filter
# allowlist: docs/CI-only merges would need an evolving allowlist, while the
# marker is an explicit operator choice in the merge subject and works at any
# checkout depth. Threat-model note: on this repo main history is squash
# merges whose subjects are PR-title-derived, so the marker text is technically
# contributor-influenced — but the gate runs POST-merge on push to main only,
# so a marker can at most delay a release, never bypass a code-integrity or
# security boundary.
SKIP_RELEASE_MARKER = "[skip release]"


def discover_manifests(repo_root: Path | None = None) -> list[str]:
    """Tracked manifest paths, repo-relative, sorted (deterministic order).

    ``repo_root`` defaults to this file's repo (the default gate mode never
    overrides it); the drift mode passes a synthetic root for tests.
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(root),
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
    return _version_from_text(text, path)


def _version_from_text(text: str, path: Path) -> str | None:
    """Parse a manifest version from its text (path only picks the shape)."""
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


# --- Unreleased-drift mode (issue #106) --------------------------------------

_UNRELEASED_RE = re.compile(r"^## \[Unreleased\][^\n]*$", re.MULTILINE)


def unreleased_body(text: str) -> str:
    """Body of the `## [Unreleased]` section, or "" when the section is absent.

    The body runs to the next `## ` header (or EOF), matching
    latest_changelog_section's termination rule.
    """
    m = _UNRELEASED_RE.search(text)
    if not m:
        return ""
    start = m.end()
    nxt = text.find("\n## ", start)
    return text[start:] if nxt < 0 else text[start:nxt]


def unreleased_has_content(text: str) -> bool:
    """True when the [Unreleased] section carries any non-blank line."""
    return any(line.strip() for line in unreleased_body(text).splitlines())


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _version_at(repo_root: Path, ref: str, rel_path: str) -> str | None:
    out = _git(repo_root, "show", f"{ref}:{rel_path}")
    if out is None or out.returncode != 0:
        return None
    return _version_from_text(out.stdout, Path(rel_path))


def head_subject(repo_root: Path) -> str:
    """HEAD's commit subject ("" when git is unavailable)."""
    out = _git(repo_root, "log", "-1", "--format=%s")
    if out is None or out.returncode != 0:
        return ""
    return out.stdout.strip()


def head_bumps_version(repo_root: Path) -> bool:
    """True when HEAD carries a version bump relative to HEAD~1.

    A manifest ADDED at HEAD counts as a bump (no HEAD~1 version to compare).
    A manifest DELETED at HEAD does not prove a bump — deletions are policed
    separately by the default mode's manifest-count floor and parity checks.
    When HEAD~1 is unavailable (shallow checkout without depth ≥ 2, or the
    root commit) this returns False — strict: drift then fails unless the
    [skip release] marker is present. ci.yml's checkout sets fetch-depth: 2
    so a push to main always has the parent; release.yml (fetch-depth: 0) is
    untouched by this mode.
    """
    try:
        manifests = discover_manifests(repo_root)
    except RuntimeError:
        return False
    if not manifests:
        return False
    parent = _git(repo_root, "rev-parse", "--verify", "--quiet", "HEAD~1")
    if parent is None or parent.returncode != 0:
        return False
    for rel in manifests:
        cur = _version_at(repo_root, "HEAD", rel)
        prev = _version_at(repo_root, "HEAD~1", rel)
        # Fail closed on a transient git/read error: a manifest whose HEAD
        # version cannot be read proves nothing, so a git hiccup must not
        # flip the drift gate to PASS (a None here is never evidence of a
        # bump). prev is None with a READABLE cur is the manifest-ADDED-at-
        # HEAD case (a real bump; a renamed manifest path is deliberately
        # treated the same — narrow trigger, policed by review). A partial
        # multi-manifest bump DOES satisfy this step; full parity is policed
        # by the default mode (and by the parity test earlier in ci.yml).
        if cur is None:
            return False
        if prev is None or cur != prev:
            return True
    return False


def check_unreleased_drift(repo_root: Path) -> int:
    """Issue #106: fail when merged work sits unserved under [Unreleased].

    Drift = the CHANGELOG's `## [Unreleased]` section carries content AND
    HEAD did not bump a manifest version relative to HEAD~1 AND the merge
    subject carries no [skip release] marker. Order: the CHANGELOG-state
    check runs first (cheap, history-free — the common clean case never
    needs git), then the marker escape, then the bump comparison.
    """
    try:
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        print(f"::error::{repo_root / 'CHANGELOG.md'} is unreadable — "
              f"the drift gate cannot run")
        return 1
    if not unreleased_has_content(changelog):
        print("[release-gate] no unreleased drift: "
              "## [Unreleased] carries no content")
        return 0
    if SKIP_RELEASE_MARKER in head_subject(repo_root):
        print(f"[release-gate] {SKIP_RELEASE_MARKER} in the merge subject — "
              f"drift accepted by explicit operator choice")
        return 0
    if head_bumps_version(repo_root):
        print("[release-gate] HEAD carries a version bump — the [Unreleased] "
              "content rides the new release")
        return 0
    print("::error::unreleased drift: CHANGELOG.md has content under "
          "## [Unreleased] but HEAD carries no version bump — merged work "
          "sits unserved (the PR #86–#104 stall, issue #106). Bump every "
          "host-facing manifest and promote the section, or add "
          f"{SKIP_RELEASE_MARKER} to the merge subject.")
    return 1


def _github_output(name: str, value: str) -> None:
    """Append `name=value` to the workflow outputs file when running in CI."""
    gh_file = os.environ.get("GITHUB_OUTPUT")
    if not gh_file:
        return
    with open(gh_file, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    # Issue #106: argparse dispatch. With NO flags the default release mode
    # below is byte-identical in behavior and stdout to the pre-#106 gate —
    # the Release workflow and its tests depend on that; the drift mode runs
    # only behind --check-unreleased-drift (wired into ci.yml for pushes to
    # main). --repo-root applies ONLY to the drift mode (synthetic-repo
    # tests); the release mode always uses this file's own repo.
    ap = argparse.ArgumentParser(
        description="Release contract gate (see module docstring)")
    ap.add_argument(
        "--check-unreleased-drift", action="store_true",
        help="issue #106: fail when ## [Unreleased] carries content on a "
             "merge whose HEAD carries no version bump")
    ap.add_argument(
        "--repo-root", default=None,
        help="repo root for --check-unreleased-drift (synthetic-repo tests); "
             "ignored by the default release mode")
    args = ap.parse_args(argv)
    if args.check_unreleased_drift:
        root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT
        return check_unreleased_drift(root)

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
