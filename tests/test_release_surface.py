"""Issue #124 release-surface coherence test (AC14 / frozen C14's unittest
mirror).

Pins, WITHOUT any hard-coded version number (the release bump lands in
parallel; this test must only require INTERNAL COHERENCE):
- README.md and skills/memory/SKILL.md name the `operation-feedback` command
  and all seven report keys;
- the seven manifests agree on ONE version;
- CHANGELOG.md carries a `## [<version>]` heading for it;
- release-manifest.json's version equals it.

Run: python tests/test_release_surface.py   (no pytest — repo convention)
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DOCS = ("README.md", "skills/memory/SKILL.md")
REPORT_KEYS = ("total_applied", "total_violated", "nonzero_applied",
               "nonzero_violated", "matched_applied", "matched_violated",
               "unmatched_operations")
MANIFESTS = (
    "marketplace.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".zcode-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    "hermes-plugin/plugin.yaml",
)


def _manifest_versions(rel: str) -> set:
    """The version strings one manifest carries (top-level and/or its
    plugin entries — plugin manifests nest the version under plugins[])."""
    path = REPO_ROOT / rel
    if rel.endswith(".yaml"):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"(?m)^version:\s*(\S+)", text)
        if not m:
            raise ValueError(f"no version in {rel}")
        return {m.group(1).strip()}
    data = json.loads(path.read_text(encoding="utf-8"))
    found = set()
    if isinstance(data.get("version"), str):
        found.add(data["version"])
    plugins = data.get("plugins")
    if isinstance(plugins, list):
        for entry in plugins:
            if isinstance(entry, dict) and isinstance(entry.get("version"),
                                                      str):
                found.add(entry["version"])
    if not found:
        raise ValueError(f"no version in {rel}")
    return found


class ReleaseSurfaceTest(unittest.TestCase):

    def test_docs_name_feedback_and_versions_cohere(self):
        for rel in DOCS:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("operation-feedback", text,
                          f"{rel} must name the operation-feedback command")
            for key in REPORT_KEYS:
                self.assertIn(key, text, f"{rel} must name report key {key}")

        versions = {}
        for rel in MANIFESTS:
            self.assertTrue((REPO_ROOT / rel).is_file(),
                            f"manifest {rel} missing")
            versions[rel] = _manifest_versions(rel)
        union = set().union(*versions.values())
        self.assertEqual(
            len(union), 1,
            f"the seven manifests must agree on ONE version: {versions}")
        version = union.pop()

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog,
                      "CHANGELOG.md lacks the version's section heading")

        release = json.loads(
            (REPO_ROOT / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(release.get("version"), version,
                         "release-manifest.json must match the manifests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
