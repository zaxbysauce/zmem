#!/usr/bin/env python
"""Generate the deterministic issue #184 host-refresh fixtures.

The committed fixture root contains three independent trees:

``checkout``
    A small release checkout with the seven host-facing manifests, a valid
    release manifest, served runtime files, and files that must be excluded
    from a host cache mirror.
``home``
    The original drifted/partially absent fake operator home used as the
    refresh preimage.
``expected``
    Complete cache mirrors, compact post-refresh registries, marketplace
    files, and a report template.  ``__CHECKOUT__`` and the cache-path tokens
    are materialized by the integration test, keeping these bytes portable.

No timestamp, machine path, or Git identity is generated here.  The test
creates a temporary Git repository only because the production release reader
uses ``git ls-files``; it patches only the production Git-HEAD boundary to the
fixed identity below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys


FIXTURE_ROOT = Path(__file__).resolve().parent
VERSION = "0.36.0"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"

EXCLUDED_DIRS = frozenset({".git", "graphify-out", "__pycache__"})
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
CLAUDE_CACHE_REL = Path(".claude/plugins/cache/zmem/zmem") / VERSION
ZCODE_CACHE_REL = Path(".zcode/cli/plugins/cache/zmem/zmem") / VERSION
CODEX_CACHE_REL = Path(".codex/plugins/cache/personal/zmem") / VERSION


def _json_bytes(
    value: object, *, compact: bool = False, sort_keys: bool = False
) -> bytes:
    if compact:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=sort_keys,
            separators=(",", ":"),
        )
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, indent=2)
    return (text + "\n").encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _claude_registry() -> dict[str, object]:
    """A rich Claude v2 mapping whose order and unrelated records matter."""
    return {
        "version": 2,
        "plugins": {
            "other@official": [
                {
                    "scope": "user",
                    "installPath": "C:/operator/other",
                    "version": "9.9.9",
                    "gitCommitSha": "f" * 40,
                    "installedAt": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-02T00:00:00Z",
                }
            ],
            "zmem@legacy": [
                {
                    "scope": "project",
                    "installPath": "C:/operator/legacy-zmem",
                    "version": "0.20.0",
                    "gitCommitSha": "d" * 40,
                    "installedAt": "2025-12-01T00:00:00Z",
                    "lastUpdated": "2025-12-02T00:00:00Z",
                }
            ],
            "zmem@zmem": [
                {
                    "scope": "user",
                    "installPath": "C:/operator/old-zmem",
                    "version": "0.32.0",
                    "gitCommitSha": "e" * 40,
                    "installedAt": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-02T00:00:00Z",
                    "extraRecordKey": "preserve-user-record",
                },
                {
                    "scope": "project",
                    "installPath": "C:/operator/project-zmem",
                    "version": "0.32.0",
                    "gitCommitSha": "c" * 40,
                    "installedAt": "2026-02-01T00:00:00Z",
                    "lastUpdated": "2026-02-02T00:00:00Z",
                    "extraRecordKey": "preserve-project-record",
                },
            ],
            "other@team": [
                {
                    "scope": "team",
                    "installPath": "C:/operator/team",
                    "version": "8.8.8",
                    "gitCommitSha": "b" * 40,
                    "installedAt": "2026-03-01T00:00:00Z",
                }
            ],
        },
        "metadata": {
            "keep": True,
            "scopes": ["user", "project", "team"],
            "source": "fixture",
        },
        "extraZmemKey": {
            "name": "zmem@extra",
            "value": "must-remain-unmodified",
        },
    }


def _zcode_registry() -> dict[str, object]:
    """A rich ZCode v1 list with unrelated records and an extra zmem row."""
    return {
        "version": 1,
        "plugins": [
            {
                "name": "other",
                "scope": "user",
                "installPath": "C:/operator/other",
                "version": "9.9.9",
                "gitCommitSha": "f" * 40,
                "enabled": True,
            },
            {
                "name": "zmem",
                "scope": "project",
                "installPath": "C:/operator/project-zmem",
                "version": "0.31.0",
                "gitCommitSha": "d" * 40,
                "channel": "legacy",
            },
            {
                "name": "other-team",
                "scope": "team",
                "installPath": "C:/operator/team",
                "version": "8.8.8",
                "gitCommitSha": "c" * 40,
            },
            {
                "name": "zmem",
                "scope": "user",
                "installPath": "C:/operator/old-zmem",
                "version": "0.32.0",
                "gitCommitSha": "e" * 40,
                "channel": "stable",
            },
        ],
        "metadata": {
            "keep": True,
            "source": "fixture",
        },
    }


def _marketplace(name: str, description: str) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "plugins": [
            {
                "name": "zmem",
                "version": VERSION,
                "source": "./zmem",
            }
        ],
    }


def _host_manifests() -> dict[str, bytes]:
    return {
        ".agents/plugins/marketplace.json": _json_bytes(
            {
                "name": "zmem-agents",
                "plugins": [{"name": "zmem", "version": VERSION}],
            }
        ),
        ".claude-plugin/marketplace.json": _json_bytes(
            _marketplace("zmem-claude", "fixture Claude marketplace")
        ),
        ".claude-plugin/plugin.json": _json_bytes(
            {"name": "zmem", "version": VERSION, "description": "fixture"}
        ),
        ".codex-plugin/plugin.json": _json_bytes(
            {"name": "zmem", "version": VERSION, "description": "fixture"}
        ),
        ".zcode-plugin/plugin.json": _json_bytes(
            {"name": "zmem", "version": VERSION, "description": "fixture"}
        ),
        "hermes-plugin/plugin.yaml": f"name: zmem\nversion: {VERSION}\ndescription: fixture\n".encode("utf-8"),
        "marketplace.json": _json_bytes(
            _marketplace("zmem-zcode", "fixture ZCode marketplace")
        ),
    }


def _runtime_files() -> dict[str, bytes]:
    return {
        "hooks/zmem-recall.sh": "#!/bin/sh\n# deterministic fixture hook\nprintf '%s\\n' recall\n".encode(
            "utf-8"
        ),
        "skills/memory/SKILL.md": "# Fixture memory skill\n\nThis is deterministic UTF-8 content.\n".encode(
            "utf-8"
        ),
        "skills/memory/scripts/store.py": "# deterministic fixture store\nVALUE = 'zmem-fixture'\n".encode(
            "utf-8"
        ),
        "scripts/host_canary.py": "\"\"\"Deterministic host canary fixture.\"\"\"\n\nVALUE = 'canary'\n".encode(
            "utf-8"
        ),
        "scripts/refresh_fixture_helper.txt": "ordinary mirror content\n".encode("utf-8"),
        "docs/fixture-not-runtime.txt": "copied by the complete mirror\n".encode("utf-8"),
        "notes.txt": "checkout root content\n".encode("utf-8"),
    }


def _excluded_files() -> dict[str, bytes]:
    return {
        "graphify-out/should-not-copy.txt": b"excluded graph output\n",
        "scripts/__pycache__/sentinel.pyc": b"excluded pycache bytecode\n",
        "scripts/ignored.pyc": b"excluded pyc\n",
        "scripts/ignored.pyo": b"excluded pyo\n",
    }


def _safe_remove_generated(root: Path) -> None:
    """Remove only the three named generated children, rejecting links."""
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError(f"refusing to generate through symlink: {root}")
    for name in ("checkout", "home", "expected"):
        path = root / name
        if path.is_symlink():
            raise RuntimeError(f"refusing to remove symlink fixture tree: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _build_checkout(root: Path) -> Path:
    checkout = root / "checkout"
    for relative, data in _host_manifests().items():
        _write(checkout / relative, data)
    for relative, data in _runtime_files().items():
        _write(checkout / relative, data)
    for relative, data in _excluded_files().items():
        _write(checkout / relative, data)

    # Import the repository-owned hashing primitive without making the fixture
    # generator depend on the current checkout's release manifest bytes.
    scripts = Path(__file__).resolve().parents[3] / "skills" / "memory" / "scripts"
    sys.path.insert(0, str(scripts))
    import drift  # type: ignore  # noqa: PLC0415

    files = drift.tree_hashes(checkout)
    _write(
        checkout / "release-manifest.json",
        _json_bytes(
            {
                "version": VERSION,
                "algorithm": drift.ALGORITHM,
                "files": files,
                "digest": drift.aggregate(files),
            }
        ),
    )
    return checkout


def _build_home(root: Path) -> Path:
    home = root / "home"
    _write(home / ".claude/plugins/installed_plugins.json", _json_bytes(_claude_registry()))
    _write(home / ".zcode/cli/plugins/installed_plugins.json", _json_bytes(_zcode_registry()))

    # Claude's target version is present but drifted; Codex and ZCode targets
    # are absent.  Older cache roots for every host and unrelated marketplace
    # content prove that dry-run and real refresh preserve state outside the
    # operation list.
    _write(
        home / CLAUDE_CACHE_REL / "hooks/zmem-recall.sh",
        b"#!/bin/sh\n# drifted preimage\nprintf '%s\\n' stale\n",
    )
    _write(home / CLAUDE_CACHE_REL / "leftover.txt", b"drifted cache residue\n")
    _write(
        home / (Path(".claude/plugins/cache/zmem/zmem") / "0.33.0/old.txt"),
        b"old Claude cache\n",
    )
    _write(home / (Path(".codex/plugins/cache/personal/zmem") / "0.32.0/old.txt"), b"old cache\n")
    _write(
        home / (Path(".zcode/cli/plugins/cache/zmem/zmem") / "0.33.0/old.txt"),
        b"old ZCode cache\n",
    )
    _write(
        home / ".zcode/cli/plugins/marketplaces/zmem/marketplace.json",
        b'{"plugins":[{"name":"zmem","version":"0.14.0","source":"./stale-zmem"}],"metadata":{"state":"drifted"}}\n',
    )
    _write(
        home / ".zcode/cli/plugins/marketplaces/zmem/.claude-plugin/marketplace.json",
        b'{"plugins":[{"name":"zmem","version":"0.14.0","source":"./legacy-zmem"}],"metadata":{"state":"stale"}}\n',
    )
    _write(
        home / ".zcode/cli/plugins/marketplaces/zmem/unrelated.txt",
        "do not replace this marketplace content\n".encode("utf-8"),
    )
    return home


def _mirror_files(checkout: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for path in sorted(checkout.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(checkout)
        parts = set(relative.parts)
        if parts & EXCLUDED_DIRS or path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append((path, relative))
    return files


def _copy_mirror(checkout: Path, destination: Path) -> None:
    for source, relative in _mirror_files(checkout):
        _write(destination / relative, source.read_bytes())


def _build_expected(root: Path, checkout: Path, home: Path) -> Path:
    expected = root / "expected"
    for host in ("codex", "claude", "zcode"):
        _copy_mirror(checkout, expected / "cache" / host)

    claude = _claude_registry()
    for record in claude["plugins"]["zmem@zmem"]:  # type: ignore[index]
        record["installPath"] = "__CLAUDE_CACHE__"
        record["version"] = VERSION
        record["gitCommitSha"] = COMMIT_SHA
    zcode = _zcode_registry()
    for record in zcode["plugins"]:  # type: ignore[index]
        if record["name"] == "zmem":  # type: ignore[index]
            record["installPath"] = "__ZCODE_CACHE__"
            record["version"] = VERSION
            record["gitCommitSha"] = COMMIT_SHA
    _write(
        expected / "registry/claude-installed.json",
        _json_bytes(claude, compact=True),
    )
    _write(expected / "registry/zcode-installed.json", _json_bytes(zcode, compact=True))
    _write(
        expected / "marketplace/marketplace.json",
        (checkout / "marketplace.json").read_bytes(),
    )
    _write(
        expected / "marketplace/.claude-plugin/marketplace.json",
        (checkout / ".claude-plugin/marketplace.json").read_bytes(),
    )

    # Compute the only non-null preimage digest without encoding a machine
    # path.  The production report's afterDigest is the checkout runtime
    # aggregate and is identical for every host.
    scripts = Path(__file__).resolve().parents[3] / "skills" / "memory" / "scripts"
    sys.path.insert(0, str(scripts))
    import drift  # type: ignore  # noqa: PLC0415

    before_claude = drift.aggregate(drift.tree_hashes(home / CLAUDE_CACHE_REL))
    after = drift.aggregate(drift.tree_hashes(checkout))
    report = {
        "checkout": "__CHECKOUT__",
        "gitCommitSha": COMMIT_SHA,
        "hosts": [
            {
                "host": "codex",
                "cacheRoot": "__CODEX_CACHE__",
                "registryPath": None,
                "marketplacePaths": [],
                "beforeDigest": None,
                "afterDigest": after,
                "status": "refreshed",
                "mismatches": [],
            },
            {
                "host": "claude",
                "cacheRoot": "__CLAUDE_CACHE__",
                "registryPath": "__CLAUDE_REGISTRY__",
                "marketplacePaths": [],
                "beforeDigest": before_claude,
                "afterDigest": after,
                "status": "refreshed",
                "mismatches": [],
            },
            {
                "host": "zcode",
                "cacheRoot": "__ZCODE_CACHE__",
                "registryPath": "__ZCODE_REGISTRY__",
                "marketplacePaths": [
                    "__ZCODE_MARKETPLACE_FILE__",
                    "__ZCODE_CLAUDE_MARKETPLACE_FILE__",
                ],
                "beforeDigest": None,
                "afterDigest": after,
                "status": "refreshed",
                "mismatches": [],
            },
        ],
        "mismatchCount": 0,
        "ok": True,
        "version": VERSION,
    }
    _write(expected / "report.json", _json_bytes(report, compact=True, sort_keys=True))
    return expected


def _sha_lines(root: Path) -> list[str]:
    lines: list[str] = []
    files = sorted(
        (p.relative_to(root).as_posix(), p)
        for p in root.rglob("*")
        if p.is_file()
    )
    for relative, path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{relative} {digest}")
    return lines


def generate(output_root: Path) -> list[str]:
    output_root = Path(output_root).resolve()
    _safe_remove_generated(output_root)
    checkout = _build_checkout(output_root)
    home = _build_home(output_root)
    _build_expected(output_root, checkout, home)
    lines = _sha_lines(output_root)
    print("\n".join(lines))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=FIXTURE_ROOT,
        help="test-only fixture output root (default: committed fixture root)",
    )
    args = parser.parse_args(argv)
    generate(args.output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
