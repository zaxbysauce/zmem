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
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The drift module owns the runtime-surface definition and hashing (the
# same import the release gate itself uses).
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
import drift  # noqa: E402

DOCS = ("README.md", "skills/memory/SKILL.md")
REPORT_KEYS = ("total_applied", "total_violated", "nonzero_applied",
               "nonzero_violated", "matched_applied", "matched_violated",
               "unmatched_operations")
MANIFESTS = (
    "deepseek-plugin/plugin.json",
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


class HostManifestDriftTest(unittest.TestCase):
    """Release-gate integrity: the host-facing manifests ARE runtime surface.

    drift's surface set covers hooks/**, skills/memory/scripts/**,
    skills/**/SKILL.md, hermes-plugin/** and scripts/** — but NOT the seven
    host-facing manifests (only hermes-plugin/plugin.yaml is incidentally
    covered by its directory prefix). A served mirror whose marketplace
    entry or plugin descriptor was tampered with is therefore reported as
    "matched" today. Post-fix contract: each of the seven manifests is
    runtime surface, and a tampered manifest byte shows up as drift.
    """

    def test_each_manifest_is_runtime_surface(self):
        for rel in MANIFESTS:
            self.assertTrue(
                drift._is_surface(rel),
                f"{rel} must be runtime surface — a served mirror whose "
                f"host manifest was tampered with must be drift, not "
                f"'matched'")

    def test_tampered_manifest_is_drift_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="zmem-hostdrift-") as td:
            scratch = Path(td)
            for rel in MANIFESTS:
                dst = scratch / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / rel, dst)
            (scratch / "hooks").mkdir(parents=True, exist_ok=True)
            (scratch / "hooks" / "a.txt").write_text(
                "surface\n", encoding="utf-8")
            recorded = {
                rel: drift.file_hash(scratch / rel)
                for rel in drift.surface_files(scratch)
            }
            self.assertIn(
                "hooks/a.txt", recorded,
                "fixture rot: the hooks surface file must be recorded")
            for rel in MANIFESTS:
                self.assertIn(
                    rel, recorded,
                    f"fixture rot: {rel} must be in the recorded surface "
                    f"set for the manifest to be tamper-detectable")
            (scratch / drift.MANIFEST_NAME).write_text(
                json.dumps({
                    "version": "0.0.0",
                    "algorithm": drift.ALGORITHM,
                    "files": recorded,
                    "digest": drift.aggregate(recorded),
                }, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
            # Tamper one manifest's bytes after the manifest was recorded.
            (scratch / "marketplace.json").write_text(
                (scratch / "marketplace.json").read_text(encoding="utf-8")
                + "\n", encoding="utf-8")
            result = drift.evaluate(scratch)
            self.assertEqual(
                result["status"], "drifted",
                f"a tampered marketplace.json must be drift, got "
                f"{result!r}")
            self.assertIn(
                "marketplace.json", result["differing"],
                f"the tampered manifest must be named among the differing "
                f"files: {result!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
