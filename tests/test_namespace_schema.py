"""Finding 1 of issue #35: the zmem_search tool schema must not mislead the
LLM about what ``namespace`` does.

The defect was a parameter-level description claiming ``namespace='user:global'``
"searches across all namespaces" -- but at runtime ``user:global`` scopes to the
global tier only; only ``'*'`` is unscoped. A model following the documented
contract would systematically under-surface project-scoped memories.

This is a SOURCE-TEXT scan (not an import of the provider): the Hermes adapter
imports ``from agent.memory_provider import MemoryProvider``, a Hermes-internal
module not present in the stdlib-only CI environment. The repo already uses this
exact source-scan pattern for the MCP surface (see test_surface_consistency.py).

Run: python tests/test_namespace_schema.py
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERMES_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"
MCP_SERVER = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"


class NamespaceSchemaTest(unittest.TestCase):
    def setUp(self):
        self.hermes_src = HERMES_INIT.read_text(encoding="utf-8")
        self.assertTrue(self.hermes_src, f"could not read {HERMES_INIT}")

    def test_search_schema_namespace_description_does_not_lie_about_global(self):
        """The _SEARCH_SCHEMA namespace description must NOT claim that
        'user:global' searches across all namespaces. That is a schema lie:
        at runtime 'user:global' scopes to the global tier only, and only '*'
        is unscoped (verified in store.py recall_memory + _recall_one_tier)."""
        # Locate the _SEARCH_SCHEMA block and its namespace description.
        self.assertIn("_SEARCH_SCHEMA", self.hermes_src)
        start = self.hermes_src.index("_SEARCH_SCHEMA")
        # The namespace description sits inside the parameters block; slice a
        # generous window covering the whole schema literal.
        schema_block = self.hermes_src[start:start + 2500]
        # Find the namespace property's description string within the block.
        self.assertIn('"namespace"', schema_block)
        ns_desc_start = schema_block.index('"namespace"')
        ns_window = schema_block[ns_desc_start:ns_desc_start + 600]

        # The misleading phrase from the bug: "'user:global' or '*'" both
        # "search across all namespaces". 'user:global' must NOT be presented
        # as a path to "all namespaces".
        self.assertNotIn(
            "'user:global' or '*' to search across all namespaces", ns_window,
            "the misleading schema lie is still present: 'user:global' does NOT "
            "search all namespaces -- only '*' does")

    def test_search_schema_documents_star_as_all_namespaces(self):
        """The corrected description must tell the model that '*' searches all
        namespaces (store-wide), and that a specific namespace scopes (while
        still surfacing a few global rows). The description must be TRUTHFUL:
        _tool_search appends --include-global for non-'*' namespaces, so a
        scoped search is NOT 'that tier only'."""
        start = self.hermes_src.index("_SEARCH_SCHEMA")
        schema_block = self.hermes_src[start:start + 2500]
        ns_desc_start = schema_block.index('"namespace"')
        ns_window = schema_block[ns_desc_start:ns_desc_start + 600]
        self.assertIn("'*'", ns_window)
        self.assertIn("all namespaces", ns_window)
        # And it must clarify a specific namespace scopes (the new contract).
        self.assertIn("scope", ns_window.lower())
        # Must NOT claim a scoped search is "tier only" -- that's inaccurate
        # because --include-global unions up to 3 global rows. The description
        # must acknowledge the global fold-in.
        self.assertNotIn("tier only", ns_window)

    def test_top_level_search_description_remains_correct(self):
        """The top-level tool description (separate from the parameter desc)
        already correctly said only '*' searches all namespaces. Regression
        guard: do not let it drift to claim 'user:global' does too."""
        # The top-level description is the first multi-line string in the schema.
        top_desc_start = self.hermes_src.index("_SEARCH_SCHEMA")
        # The description tuple starts after "description": (
        desc_anchor = self.hermes_src.index(
            '"Semantic + full-text search', top_desc_start)
        top_desc = self.hermes_src[desc_anchor:desc_anchor + 800]
        self.assertIn("namespace='*'", top_desc)
        # It must NOT also claim 'user:global' searches all namespaces.
        self.assertNotIn(
            "user:global", top_desc.split("namespace='*'", 1)[0],
            "top-level description should not present user:global as an "
            "all-namespaces path before the '*' guidance")

    def test_add_and_supersede_schemas_have_no_cross_namespace_claims(self):
        """Only the search tool had the cross-namespace lie. Guard the other two
        schemas against regressing into the same mistake."""
        for schema_name in ("_ADD_SCHEMA", "_SUPERSEDE_SCHEMA"):
            self.assertIn(schema_name, self.hermes_src)
            start = self.hermes_src.index(schema_name)
            block = self.hermes_src[start:start + 1800]
            self.assertNotIn(
                "to search across all namespaces", block,
                f"{schema_name} should not carry a cross-namespace search claim")


class McpServerNamespaceDocsStayCorrectTest(unittest.TestCase):
    """Regression guard for the MCP server (Finding 1 noted it was already
    correct). Its recall/search docstrings and _namespace_flag must keep
    treating only '*' / omitted as 'search all'."""

    def setUp(self):
        self.src = MCP_SERVER.read_text(encoding="utf-8")
        self.assertTrue(self.src, f"could not read {MCP_SERVER}")

    def test_namespace_flag_treats_star_as_all(self):
        self.assertIn("_namespace_flag", self.src)
        start = self.src.index("def _namespace_flag")
        block = self.src[start:start + 700]
        # The flag omits --namespace for '*' (and None), which is what makes
        # store.py search all namespaces.
        self.assertIn("!= \"*\"", block)

    def test_recall_docstring_does_not_present_global_as_all_namespaces(self):
        """The recall tool's docstring must present only '*' / omitted as 'search
        all namespaces' and must NOT claim 'user:global' searches all namespaces.
        Scoped to the recall function's own docstring (NOT a whole-file
        substring, which would pass even if the recall docstring regressed)."""
        # Isolate the recall function definition + its docstring window.
        self.assertIn("def recall(", self.src)
        recall_start = self.src.index("def recall(")
        recall_block = self.src[recall_start:recall_start + 1200]
        # The docstring is the first triple-quoted span after the def.
        self.assertIn('"""', recall_block)
        ds_start = recall_block.index('"""')
        ds_end = recall_block.index('"""', ds_start + 3)
        recall_docstring = recall_block[ds_start + 3:ds_end]
        # '*' is presented as the all-namespaces path.
        self.assertIn("namespace='*'", recall_docstring)
        self.assertIn("all namespaces", recall_docstring)
        # 'user:global' may legitimately appear (e.g. describing the
        # --include-global union), but must NOT be presented as a way to search
        # all namespaces. The bug phrase would tie 'user:global' to
        # 'all namespaces'; assert that exact misleading pairing is absent...
        self.assertNotIn(
            "user:global' or '*'", recall_docstring,
            "recall docstring must not present user:global as equivalent to '*' "
            "for searching all namespaces")
        # ...and guard against a reworded lie: 'user:global' must not appear in
        # the text BEFORE the "all namespaces" clause (which is where '*' is
        # presented as the all-namespaces path).
        before_all = recall_docstring.split("all namespaces")[0] \
            if "all namespaces" in recall_docstring else recall_docstring
        self.assertNotIn(
            "user:global", before_all,
            "recall docstring must not present user:global as a path to "
            "searching all namespaces (reworded-lie guard)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
