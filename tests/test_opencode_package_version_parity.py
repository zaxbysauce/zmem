"""The opencode-plugin package surface must agree with the canonical
marketplace entry (plan-critic round 2 gap: no existing test forces these
surfaces, so a partial bump could pass every named gate; release-manifest
parity is covered separately by the release gate).

Pure file reads, no env, no store, no network: the four committed version
surfaces must all be EQUAL, derived entirely from the files (no hardcoded
version):

- opencode-plugin/package.json ``version``
- opencode-plugin/package-lock.json top-level ``version``
- opencode-plugin/package-lock.json ``packages[""].version``
- marketplace.json ``plugins[zmem].version`` (the canonical anchor —
  same anchor spirit as the deepseek parity test's plugin.json)

GREEN at the pre-retarget anchor 5c4755ea (all four report 0.154.0) and
RED at the merged base 93a8d307 (marketplace 0.155.0 vs the stranded
0.154.0 opencode pair — the real drift this test caught during the
retarget); RED the moment any surface drifts from the canonical release
version again (partial or forgotten bump).

Repo tests are explicitly "No pytest / third-party harness required"
(unittest-only convention); this file follows it and CI runs it as plain
``python -X utf8 tests/test_opencode_package_version_parity.py``.
"""

from __future__ import annotations
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "opencode-plugin"


def _load(path: Path) -> dict:
    if not path.is_file():
        raise AssertionError(f"committed surface missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_market_version(market: dict) -> object:
    """First entry named zmem wins (deterministic first-wins; a duplicate
    zmem entry would be a malformed marketplace, covered by release-gate
    manifest checks, not silently last-wins here)."""
    for entry in market.get("plugins", []):
        if entry.get("name") == "zmem":
            return entry.get("version")
    return None


def _surface_versions() -> dict:
    pkg = _load(PLUGIN_DIR / "package.json")
    lock = _load(PLUGIN_DIR / "package-lock.json")
    market = _load(REPO_ROOT / "marketplace.json")
    return {
        "opencode-plugin/package.json version": pkg.get("version"),
        "opencode-plugin/package-lock.json version (top level)":
            lock.get("version"),
        'opencode-plugin/package-lock.json packages[""] version':
            (lock.get("packages", {}).get("", {}) or {}).get("version"),
        "marketplace.json plugins[zmem] version (canonical)":
            _canonical_market_version(market),
    }


class OpencodePackageVersionParityTest(unittest.TestCase):
    """Every opencode package surface carries the canonical release version."""

    def test_all_package_surfaces_equal_canonical_release(self):
        versions = _surface_versions()
        canonical = versions["marketplace.json plugins[zmem] version (canonical)"]
        self.assertTrue(
            canonical, f"canonical marketplace.json version missing: {versions}")
        drifted = {
            surface: version for surface, version in versions.items()
            if surface != "marketplace.json plugins[zmem] version (canonical)"
            and version != canonical
        }
        self.assertEqual(
            {}, drifted,
            "opencode-plugin package surfaces disagree with the canonical "
            f"release version {canonical!r}: {drifted}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
