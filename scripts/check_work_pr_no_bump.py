"""Work-PR version guard: ordinary PRs must not reserve release versions.

Release ownership (owner decision 2026-11-15): version assignment belongs to
the designated release step, not to work PRs. A work PR that reserves a
release version — bumping a canonical host-facing manifest, adding a new
one, or dating a new CHANGELOG release section — fails this check with an
actionable message, which removes the version-collision retarget ritual from
ordinary merges.

CI wiring: run on pull_request events with the PR base COMMIT SHA:
    python scripts/check_work_pr_no_bump.py --base "$base_sha"
Use the COMMIT SHA: a shallow PR checkout has no local branch refs, so a
branch name (GITHUB_BASE_REF) cannot resolve here and would fail the guard
closed on every manifest. `--repo-root` exists for the committed fixture
tests. The `release` label (or --label-exempt) marks the designated release
step's own PR, where version bumps are the point.

Fails closed: an unresolvable base or an unparseable manifest is an error,
never a silent pass. A manifest absent at a resolving base counts as a bump
(a new version-bearing surface), mirroring the freshness gate's new-manifest
arm (release_gate.py:775-798).

Read-only: no writes, no network, no store access.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import release_gate  # noqa: E402

_SECTION_RE = re.compile(
    r"^## \[(?!Unreleased\b)([0-9]+\.[0-9]+\.[0-9]+)\]", re.MULTILINE)


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(repo_root), *args],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


def _newest_section(text: str) -> str | None:
    m = _SECTION_RE.search(text)
    return m.group(1) if m else None


def _manifest_version_at(repo_root: Path, base: str, rel: str):
    """Three-state base read:
    ("ok", version) | ("absent", None) | ("error", None)."""
    rc, _ = _git(repo_root, "cat-file", "-e", f"{base}^{{commit}}")
    if rc != 0:
        return "error", None
    rc, out = _git(repo_root, "show", f"{base}:{rel}")
    if rc != 0:
        return "absent", None
    return "ok", release_gate._version_from_text(out, Path(rel))


def guard(repo_root: Path, base: str, label_exempt: bool = False) -> int:
    if label_exempt:
        print("[work-pr-guard] release-labeled PR: version bumps allowed "
              "(designated release step)")
        return 0
    rc, _ = _git(repo_root, "cat-file", "-e", f"{base}^{{commit}}")
    if rc != 0:
        print(f"[work-pr-guard] FAIL: base {base} does not resolve in this "
              f"checkout - failing closed (fetch it, or pass the base "
              f"commit SHA)")
        return 2
    bumps: list[str] = []
    for rel in release_gate.discover_manifests(repo_root):
        state, base_v = _manifest_version_at(repo_root, base, rel)
        head_text = (repo_root / rel).read_text(encoding="utf-8")
        head_v = release_gate._version_from_text(head_text, Path(rel))
        if state == "error":
            print(f"[work-pr-guard] FAIL: cannot read {rel} at base {base} "
                  f"- failing closed")
            return 2
        if state == "absent":
            bumps.append(rel)  # new version-bearing surface = a reservation
            continue
        if base_v is None or head_v is None:
            print(f"[work-pr-guard] FAIL: {rel} does not parse as an "
                  f"X.Y.Z version at base or head - failing closed")
            return 2
        if head_v != base_v:
            bumps.append(rel)
    rc, base_cl = _git(repo_root, "show", f"{base}:CHANGELOG.md")
    if rc != 0:
        print(f"[work-pr-guard] FAIL: cannot read CHANGELOG.md at base "
              f"{base} - failing closed")
        return 2
    try:
        head_cl = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[work-pr-guard] FAIL: CHANGELOG.md unreadable at head - "
              f"failing closed ({exc})")
        return 2
    base_section = _newest_section(base_cl)
    head_section = _newest_section(head_cl)
    if head_section != base_section:
        bumps.append("CHANGELOG.md (new dated release section)")
    if not bumps:
        print("[work-pr-guard] no version reservation in a work PR (good)")
        return 0
    print("[work-pr-guard] FAIL: work PRs must not reserve release versions. "
          "Version assignment happens at the designated release step.")
    for rel in bumps:
        print(f"  {rel}")
    print("Fix: revert the version reservation in this PR; put release notes "
          "under `## [Unreleased]` - the version bump and the dated section "
          "land with the release step.")
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True,
                    help="base COMMIT SHA (not a branch name)")
    ap.add_argument("--repo-root", default=".",
                    help="repo root (fixture-test seam; default cwd)")
    ap.add_argument("--label-exempt", action="store_true", default=False,
                    help="exempt this run (the designated release step's "
                         "own PR; CI expresses the same exemption by "
                         "skipping the step on release-labeled PRs)")
    ns = ap.parse_args(argv)
    return guard(Path(ns.repo_root).resolve(), ns.base, ns.label_exempt)


if __name__ == "__main__":
    sys.exit(main())
