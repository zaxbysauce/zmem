"""Docs-contract pins for the schema-16 release (issue #175).

Strengthening of the frozen repro/c13 script's ground in a NEW unfrozen test
(never an edit to a frozen file): README.md and skills/memory/SKILL.md must
carry the full v16 documentation set — the upgrade requirement, the twelve
v2 kinds, the entity_card rejection decision, the forward ceiling, and the
ZMEM_ALLOW_NEWER_SCHEMA override.

Run: python tests/test_docs_contract.py
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
SKILL = REPO_ROOT / "skills" / "memory" / "SKILL.md"

V2_KINDS = ("person", "project", "tool", "preference", "host", "service",
            "model", "repo", "file", "command", "error_class", "other")


class Schema16DocsContractTest(unittest.TestCase):
    def test_readme_documents_v16_upgrade_contract(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("Schema 16", text)
        self.assertIn("UPGRADE-REQUIRED", text)
        # Anchor to the QUOTED kind list, not a bare substring: `file`,
        # `command`, `model`, `repo`, `service` and `tool` are ordinary
        # English tokens that occur throughout README for unrelated reasons
        # (PR #355 review TST-6).
        for kind in V2_KINDS:
            self.assertIn(f"`{kind}`", text,
                          f"README must name the v2 entity kind {kind!r}")
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA", text)
        self.assertRegex(
            text, r"entity_card`[^.]{0,160}?reject",
            "README must record the entity_card REJECT decision (matched "
            "near the entity_card mention, not anywhere in the file)")
        self.assertIn("FORWARD_COMPAT_SCHEMA_VERSION", text)

    def test_skill_documents_v16_upgrade_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Schema 16", text)
        self.assertIn("entity kinds v2", text)
        for kind in V2_KINDS:
            self.assertIn(f"`{kind}`", text,
                          f"SKILL.md must name the v2 entity kind {kind!r}")
        self.assertIn("ZMEM_ALLOW_NEWER_SCHEMA=1", text)
        self.assertRegex(
            text, r"`entity_card`[^.]{0,160}?REJECT",
            "SKILL.md must record the entity_card REJECT decision (matched "
            "near the entity_card mention, not anywhere in the file)")
        self.assertIn("FORWARD_COMPAT_SCHEMA_VERSION", text)
        self.assertIn("deliberately stays 15", text,
                      "SKILL.md must name the held forward ceiling")


if __name__ == "__main__":
    unittest.main(verbosity=2)
