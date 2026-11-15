"""Work-PR version guard: ordinary PRs must not reserve release versions.

Release ownership (owner decision 2026-11-15): version assignment belongs to
the designated release step, not to work PRs. A work PR that bumps any
canonical host-facing manifest fails this check with an actionable message,
which removes the version-collision retarget ritual from ordinary merges.

CI wiring: run on pull_request events with the PR base ref:
    python scripts/check_work_pr_no_bump.py --base "$GITHUB_BASE_REF"
Release PRs (base == head) trivially pass; a `release` label may exempt a PR
explicitly (checked by the caller, not here).

Read-only: no writes, no network, no store access.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import release_gate  # noqa: E402


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def base_versions(base_ref: str) -> dict[str, str]:
    """version of each canonical manifest at the base ref ('' = absent/unreadable)."""
    out: dict[str, str] = {}
    for rel in release_gate.discover_manifests(REPO_ROOT):
        blob = _git("show", f"{base_ref}:{rel}")
        out[rel] = release_gate._version_from_text(blob, Path(rel)) if blob else ""
    return out


def head_versions() -> dict[str, str]:
    return {rel: release_gate._version_from_text((REPO_ROOT / rel).read_text(encoding="utf-8"), Path(rel))
            for rel in release_gate.discover_manifests(REPO_ROOT)}


def bumped(base: dict[str, str], head: dict[str, str]) -> list[str]:
    return sorted(rel for rel in head if base.get(rel) not in ("", head[rel]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="base ref (e.g. the PR base branch)")
    ap.add_argument("--label-exempt", action="store_true", default=False,
                    help="exempt this run (set by CI when the release label is present)")
    ns = ap.parse_args(argv)

    if ns.label_exempt:
        print("[work-pr-guard] release-labeled PR: version bumps allowed (designated release step)")
        return 0

    b = base_versions(ns.base)
    h = head_versions()
    hits = bumped(b, h)
    if not hits:
        print("[work-pr-guard] no version bumps in a work PR (good)")
        return 0
    print("[work-pr-guard] FAIL: work PRs must not reserve release versions. "
          "Version assignment happens at the designated release step.")
    for rel in hits:
        print(f"  {rel}: {b.get(rel) or '<absent>'} -> {h[rel]}")
    print("Fix: drop the version bump from this PR; it lands with the release step.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
