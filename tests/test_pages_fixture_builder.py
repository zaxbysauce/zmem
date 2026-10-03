"""Consistency and adapter-replay checks for the curated-page fixtures."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pages"
BUILDER_PATH = FIXTURES / "build_fixtures.py"
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "zmem_pages_fixture_builder", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PageFixtureBuilderTests(unittest.TestCase):
    def test_builder_output_matches_root_fixtures(self):
        builder = _load_builder()
        with tempfile.TemporaryDirectory(prefix="zmem-pages-builder-") as tmp:
            generated = builder.build(Path(tmp))
            for name, payload in generated.items():
                self.assertEqual(payload, (FIXTURES / name).read_bytes(), name)

    def test_valid_append_then_retract_replays_against_production(self):
        builder = _load_builder()
        patches = json.loads((FIXTURES / "patches.json").read_text(encoding="utf-8"))
        operations = patches["valid"]["operations"]
        sys.path.insert(0, str(SCRIPTS))
        try:
            from storelib import pages
        finally:
            sys.path.remove(str(SCRIPTS))

        citation_map = {
            "ev-502": (["00000000-0000-4000-8000-000000000502"], ["ev-502"]),
            "ev-503": (["00000000-0000-4000-8000-000000000503"], ["ev-503"]),
        }
        base = (FIXTURES / "page-base.md").read_text(encoding="utf-8")
        content, authorities = pages._apply_operations(
            base, operations, citation_map, {})

        stable_prefix = base.split("<!-- section:refresh -->", 1)[0]
        self.assertEqual(content.split("<!-- section:refresh -->", 1)[0], stable_prefix)
        self.assertIn("Patched café", content)
        self.assertNotIn("Patched bullet", content)
        self.assertIn("Stable bytes: cafe", content)
        self.assertEqual(len(authorities), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
