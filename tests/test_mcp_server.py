"""Finding 3 of issue #35: runtime tests for the MCP server's 5-tool surface.

``mcp_server.py`` defines 5 ``@mcp.tool()`` closures (recall, add, search,
supersede, recent). The ONLY prior test (test_surface_consistency.py) read the
file as source text and asserted a substring was absent — no tool was ever
invoked, and no output shape was ever asserted. Regressions in the
network-facing surface (malformed args, validation errors, result shapes)
shipped undetected.

These tests construct the real FastMCP server via ``build_server`` and invoke
each tool through the ToolManager (the context-free path), covering success and
error paths for all 5 tools.

CI constraint (issue #35): CI runs ``python tests/test_*.py`` with stdlib only
(no ``pip install``; see .github/workflows/ci.yml). The ``mcp`` package is not
stdlib, so the whole module skip-guards on its availability. The tests run
wherever ``mcp`` is installed (the deployment env, dev machines, any future CI
that installs deps) and skip cleanly in stdlib-only CI.

Run: python tests/test_mcp_server.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
sys.path.insert(0, str(SERVER_DIR))

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(MCP_AVAILABLE,
                     "mcp package not installed (MCP server tests need it)")
class ClampLimitTest(unittest.TestCase):
    """Direct unit tests of the in-process limit clamp (mcp_server._clamp_limit)
    that do NOT depend on a seeded store. The integration clamp tests in
    McpServerToolSurfaceTest below prove the cap binds end-to-end; these pin
    the clamp logic itself."""

    def setUp(self):
        import mcp_server
        self.clamp = mcp_server._clamp_limit
        self.hard_max = mcp_server._HARD_LIMIT_MAX
        self.default = mcp_server._DEFAULT_LIMIT

    def test_none_returns_default(self):
        self.assertEqual(self.clamp(None), self.default)

    def test_large_value_clamped_to_hard_max(self):
        self.assertEqual(self.clamp(999), self.hard_max)
        self.assertEqual(self.clamp(10000), self.hard_max)

    def test_small_value_clamped_to_one(self):
        self.assertEqual(self.clamp(0), 1)
        self.assertEqual(self.clamp(-5), 1)

    def test_in_range_value_passes_through(self):
        self.assertEqual(self.clamp(5), 5)
        self.assertEqual(self.clamp(self.hard_max), self.hard_max)

    def test_non_numeric_returns_default(self):
        self.assertEqual(self.clamp("bogus"), self.default)
        self.assertEqual(self.clamp(None), self.default)


@unittest.skipUnless(MCP_AVAILABLE,
                     "mcp package not installed (MCP server tests need it)")
class McpServerToolSurfaceTest(unittest.TestCase):
    """Invoke all 5 MCP tools through the ToolManager and assert their result
    shapes for success and error paths."""

    @classmethod
    def setUpClass(cls):
        # Isolated store + env. build_server() resolves store.py from ZMEM_HOME
        # (the repo root) and needs ZMEM_MCP_TOKEN (load_expected_token).
        cls.tmp = tempfile.mkdtemp(prefix="zmem-mcp-test-")
        cls.store_path = os.path.join(cls.tmp, "store.sqlite")
        cls._saved_env = {
            k: os.environ.get(k) for k in (
                "ZMEM_HOME", "ZMEM_STORE", "ZMEM_MCP_TOKEN",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_DATA",
            )
        }
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = cls.store_path
        os.environ["ZMEM_MCP_TOKEN"] = "test-token-for-suite"
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(cls.tmp, "no-such-models")
        os.environ.pop("ZMEM_DATA", None)
        import mcp_server  # noqa: E402 — imported lazily after env is set
        cls.mcp_server = mcp_server
        cls.server = mcp_server.build_server(host="127.0.0.1", port=0, use_tls=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def _call(self, name: str, **args):
        """Invoke a tool through the ToolManager (context-free) and return the
        raw result. FastMCP tool closures return a dict, so this returns the
        dict directly (verified against mcp==1.28.1)."""
        return asyncio.run(
            self.server._tool_manager.call_tool(name, args, context=None))

    def _ns(self):
        """A unique namespace per test method so tests never couple through the
        shared store (dedup/content/order-dependence). All 14 tests share one
        store.sqlite (setUpClass), so scoping each test to its own namespace
        makes them order-independent and merge-safe."""
        return f"user:test-{uuid.uuid4().hex[:8]}"

    def _add(self, content="a reusable lesson for tests", type_="fact",
             tags=None, signal="none", namespace=None):
        return self._call("add", type=type_, content=content,
                          namespace=namespace or self._ns(),
                          tags=tags, signal=signal)

    def _add_test_signal(self, content, namespace=None):
        """Add with signal=test so confidence (0.9) clears recent's default
        min_confidence=0.5 filter. The plain add() helper uses signal=none
        (confidence 0.3), which recent filters out by design."""
        return self._add(content=content, namespace=namespace or self._ns(),
                         signal="test")

    # -- recall -------------------------------------------------------------

    def test_recall_success_returns_results_and_count(self):
        # Use a query term that actually appears in the content (FTS5 has no
        # stemming, so "testing" won't match "pytest"). A unique namespace per
        # test isolates it from other tests' rows on the shared store.
        ns = self._ns()
        self._add(content="always run pytest before committing changes", namespace=ns)
        result = self._call("recall", query="pytest", namespace=ns, limit=5)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["results"], list)
        self.assertGreaterEqual(result["count"], 1)
        if result["results"]:
            item = result["results"][0]
            for key in ("id", "type", "content"):
                self.assertIn(key, item)

    def test_recall_empty_query_returns_error(self):
        result = self._call("recall", query="", namespace=self._ns(), limit=5)
        self.assertIn("error", result)
        self.assertIn("query", result["error"])

    def test_recall_limit_is_clamped_to_hard_max(self):
        """limit=999 must be clamped to _HARD_LIMIT_MAX (50), not rejected.
        Seeds enough rows that the cap actually binds (count would exceed 50
        without clamping) — a bare empty-namespace count<=50 would be vacuous."""
        ns = self._ns()
        for i in range(55):
            self._add_test_signal(content=f"clamp seed row {i}", namespace=ns)
        result = self._call("recall", query="clamp", namespace=ns, limit=999)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        # The clamp caps at 50 even though 55 rows match and limit=999.
        self.assertLessEqual(result["count"], 50)
        self.assertEqual(result["count"], 50,
                         "limit=999 must be clamped to exactly the 50 hard max")

    # -- add ----------------------------------------------------------------

    def test_add_success_returns_stored(self):
        result = self._add(content="prefer small reviewable commits",
                           type_="lesson", signal="user")
        self.assertEqual(result.get("result"), "stored")
        self.assertIn("raw", result)

    def test_add_bad_type_returns_error(self):
        result = self._call("add", type="bogus", content="x", namespace=self._ns())
        self.assertIn("error", result)
        self.assertIn("type", result["error"])

    def test_add_empty_content_returns_error(self):
        result = self._call("add", type="fact", content="", namespace=self._ns())
        self.assertIn("error", result)
        self.assertIn("content", result["error"])

    def test_add_bad_signal_returns_error(self):
        result = self._call("add", type="fact", content="x",
                            namespace=self._ns(), signal="bogus")
        self.assertIn("error", result)
        self.assertIn("signal", result["error"])

    def test_add_long_content_is_truncated_not_rejected(self):
        # Content over _MAX_CONTENT_CHARS (32000) is clamped, not errored.
        long_content = "z" * 40000
        result = self._call("add", type="fact", content=long_content,
                            namespace=self._ns())
        self.assertEqual(result.get("result"), "stored")

    # -- search (alias of recall) ------------------------------------------

    def test_search_returns_same_shape_as_recall(self):
        ns = self._ns()
        self._add(content="search alias shape check", namespace=ns)
        search_result = self._call("search", query="alias", namespace=ns, limit=5)
        recall_result = self._call("recall", query="alias", namespace=ns, limit=5)
        # search delegates to recall, so the shape must be identical.
        self.assertEqual(set(search_result.keys()), set(recall_result.keys()))

    def test_search_empty_query_returns_error(self):
        result = self._call("search", query="", namespace=self._ns(), limit=5)
        self.assertIn("error", result)

    # -- supersede ----------------------------------------------------------

    def test_supersede_success(self):
        ns = self._ns()
        self._add(content="temporary fact to supersede", namespace=ns)
        # Recall to find the id of the just-added row.
        recalled = self._call("recall", query="temporary", namespace=ns, limit=5)
        self.assertGreater(recalled["count"], 0, "could not find added row to supersede")
        mid = recalled["results"][0]["id"]
        result = self._call("supersede", id=mid, reason="test supersede")
        self.assertEqual(result.get("result"), "superseded")
        self.assertEqual(result.get("id"), mid)

    def test_supersede_empty_id_returns_error(self):
        result = self._call("supersede", id="")
        self.assertIn("error", result)
        self.assertIn("id", result["error"])

    # -- recent -------------------------------------------------------------

    def test_recent_returns_results(self):
        # signal=test → confidence 0.9, which clears recent's default
        # min_confidence=0.5 filter (signal=none → 0.3 would be filtered out).
        # Unique namespace so the >=3 assertion is about THIS test's rows only.
        ns = self._ns()
        for i in range(3):
            self._add_test_signal(content=f"recent row number {i}", namespace=ns)
        result = self._call("recent", namespace=ns, limit=10)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertGreaterEqual(result["count"], 3)

    def test_recent_limit_clamped(self):
        """limit=999 must be clamped to 50. Seeds enough rows that the cap
        binds (a bare empty-namespace count<=50 would be vacuous)."""
        ns = self._ns()
        for i in range(55):
            self._add_test_signal(content=f"recent clamp row {i}", namespace=ns)
        result = self._call("recent", namespace=ns, limit=999)
        self.assertIsInstance(result, dict)
        self.assertLessEqual(result["count"], 50)
        self.assertEqual(result["count"], 50,
                         "limit=999 must be clamped to exactly the 50 hard max")

    def test_recent_empty_namespace_returns_empty_results(self):
        """An empty namespace returns a well-formed {results: [], count: 0}
        (the 'no-data' edge for recent — it has no required args, so this is
        the realistic non-happy path rather than a validation error)."""
        result = self._call("recent", namespace=self._ns(), limit=5)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_recent_non_numeric_limit_is_rejected_at_schema(self):
        """A non-numeric limit is rejected by FastMCP's pydantic schema
        validation BEFORE _clamp_limit runs (limit is typed `int`). This is the
        realistic error path for recent (it has no required args): an invalid
        limit type raises a validation error rather than reaching the tool."""
        with self.assertRaises(Exception):
            # pydantic raises ValidationError (a ValueError subclass) on the
            # int coercion; the ToolManager propagates it from call_tool.
            self._call("recent", namespace=self._ns(), limit="not-a-number")


if __name__ == "__main__":
    unittest.main(verbosity=2)
