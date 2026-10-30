"""AC5 (#416-2): the deepseek-plugin package surface must agree with
plugin.json (frozen acceptance check C5).

Pure file reads, no env, no store, no network: the four committed version
fields must all be EQUAL, derived entirely from the files (no hardcoded
version):

- deepseek-plugin/package.json ``version``
- deepseek-plugin/package-lock.json top-level ``version``
- deepseek-plugin/package-lock.json ``packages[""].version``
- deepseek-plugin/plugin.json ``version`` (the canonical release-bound
  manifest)

At base 471ea99 package.json/package-lock report 0.127.0 while plugin.json
reports 0.130.0 -> RED.  (release-manifest.json is printed for context in
the failure message only — the four package surfaces are the pinned set.)

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_deepseek_package_version_parity.py``
"""

from __future__ import annotations
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "deepseek-plugin"


def _load(name: str) -> dict:
    path = PLUGIN_DIR / name
    if not path.is_file():
        raise AssertionError(f"committed surface missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _surface_versions() -> dict:
    pkg = _load("package.json")
    lock = _load("package-lock.json")
    plugin = _load("plugin.json")
    release = json.loads((REPO_ROOT / "release-manifest.json")
                         .read_text(encoding="utf-8"))
    return {
        "deepseek-plugin/package.json version": pkg.get("version"),
        "deepseek-plugin/package-lock.json version (top level)":
            lock.get("version"),
        'deepseek-plugin/package-lock.json packages[""] version':
            (lock.get("packages", {}).get("", {}) or {}).get("version"),
        "deepseek-plugin/plugin.json version (canonical)":
            plugin.get("version"),
        "_release-manifest.json version (context)": release.get("version"),
    }


class DeepseekPackageVersionParityTest(unittest.TestCase):
    """Every package surface carries plugin.json's version."""

    def test_all_package_surfaces_equal_plugin_json(self):
        versions = _surface_versions()
        canonical = versions["deepseek-plugin/plugin.json version (canonical)"]
        self.assertTrue(isinstance(canonical, str) and canonical,
                        f"plugin.json version is not a usable string: "
                        f"{canonical!r}")
        drift = {surface: value for surface, value in versions.items()
                 if not surface.startswith("_") and value != canonical}
        self.assertEqual(
            drift, {},
            "deepseek-plugin package surfaces drift from plugin.json "
            f"{canonical!r} (release-manifest reports "
            f"{versions['_release-manifest.json version (context)']!r}): "
            f"{drift}")

    def test_every_surface_reports_a_version(self):
        versions = _surface_versions()
        for surface, value in versions.items():
            if surface.startswith("_"):
                continue
            self.assertTrue(isinstance(value, str) and value,
                            f"{surface} reports no version: {value!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
