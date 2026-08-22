"""Schema-version doc agreement ratchet (issue #56, task 1.8).

SKILL.md and CUTOVER.md must state the CURRENT schema version, sourced from
``schema_meta.SUPPORTED_SCHEMA_VERSION`` — the single source of truth shared
with doctor.py and the store. Before this ratchet, SKILL.md claimed "current
v6" and CUTOVER.md "current v7" while the runtime was at v8, so any agent
implementing a later phase from the docs built against the wrong system.

Every later schema bump (v9+) must update the docs in the same change or this
test fails — that is the point.

Run: python tests/test_schema_version.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from schema_meta import SUPPORTED_SCHEMA_VERSION  # noqa: E402

# How both docs state the runtime schema version in their doctor-check
# bullets: "<...> against current v<N>".
CLAIM_RE = re.compile(r"current v(\d+)")

DOCS = (
    "skills/memory/SKILL.md",
    "CUTOVER.md",
)


class SchemaVersionDocAgreementTest(unittest.TestCase):

    def test_supported_schema_version_is_an_int(self):
        # Guards the import itself: if schema_meta stops exposing the constant
        # (rename/move), the module-level import already fails loudly; this
        # pins the type the doc comparison relies on.
        self.assertIsInstance(SUPPORTED_SCHEMA_VERSION, int)

    def test_docs_state_the_supported_schema_version(self):
        for rel in DOCS:
            with self.subTest(doc=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                claims = CLAIM_RE.findall(text)
                # Non-vacuous: the doc must make at least one "current v<N>"
                # claim for the pin to mean anything.
                self.assertTrue(
                    claims,
                    f"{rel} must state the runtime schema version as "
                    f"'current v<N>' (it is how the doctor bullet reads)")
                for n in claims:
                    self.assertEqual(
                        int(n), SUPPORTED_SCHEMA_VERSION,
                        f"{rel} claims current v{n} but "
                        f"schema_meta.SUPPORTED_SCHEMA_VERSION is "
                        f"{SUPPORTED_SCHEMA_VERSION} — update the doc (or the "
                        f"constant, together with every surface that reads it)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
