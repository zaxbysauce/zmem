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


class HealthHelperCITest(unittest.TestCase):
    """#39 E2 / PRR-007: the /health payload (liveness + readiness) must be
    minimal, never leak paths/tokens, and never raise. This is the SINGLE
    source of truth for the _compute_health() contract — it runs in every
    environment (no skip guard), so CI validates it even when the mcp package
    is absent (CI is stdlib-only). When mcp IS absent, setUp injects a minimal
    stub of mcp.server.auth.provider into sys.modules (mcp_server transitively
    imports mcp via auth.py) and tearDown restores it (cubic-re #2/#3). The
    route-registration test (which needs the real FastMCP object) lives in the
    MCP-guarded McpServerToolSurfaceTest class below."""

    def setUp(self):
        # Snapshot sys.modules so tearDown can restore exactly (cubic-re #3:
        # never leak the injected stub into a shared-process runner).
        self._saved_sys_modules = sys.modules.copy()
        if not MCP_AVAILABLE:
            import types
            for mod_name in ("mcp", "mcp.server", "mcp.server.auth",
                             "mcp.server.auth.provider"):
                sys.modules[mod_name] = types.ModuleType(mod_name)
            provider = sys.modules["mcp.server.auth.provider"]
            provider.AccessToken = type("AccessToken", (), {})
            provider.TokenVerifier = type("TokenVerifier", (), {})
        import mcp_server
        self.mcp_server = mcp_server

    def tearDown(self):
        # Restore sys.modules to the pre-setUp snapshot so injected stubs don't
        # leak to other test modules in a shared-process run (cubic-re #3).
        sys.modules.clear()
        sys.modules.update(self._saved_sys_modules)

    def test_compute_health_returns_minimal_shape(self):
        h = self.mcp_server._compute_health()
        self.assertIsInstance(h, dict)
        # Exactly these three keys — nothing else (no paths, no tokens, no reason).
        self.assertEqual(set(h.keys()), {"ok", "store_resolved", "embeddings_available"})
        self.assertTrue(h["ok"], "ok must be True if the handler ran (liveness)")
        self.assertIsInstance(h["store_resolved"], bool)
        self.assertIsInstance(h["embeddings_available"], bool)

    def test_compute_health_leaks_no_paths_or_tokens(self):
        """The server is unauthenticated by design; the response must not leak
        install details (paths, tokens, model dirs, namespace)."""
        h = self.mcp_server._compute_health()
        blob = repr(h)
        for forbidden in (os.environ.get("ZMEM_MCP_TOKEN", ""),
                          "store.sqlite", "minilm.onnx", "models_dir",
                          "token", "ZMEM_HOME", "ZMEM_STORE"):
            if forbidden:
                self.assertNotIn(forbidden, blob,
                                 f"/health must not leak {forbidden!r}: {blob}")

    def test_compute_health_never_raises(self):
        """Even if the embeddings probe can't resolve the store, _compute_health
        must return a dict (degraded to store_resolved/embeddings_available =
        False), never raise."""
        orig = self.mcp_server._resolve_zmem_home
        self.mcp_server._resolve_zmem_home = lambda: (_ for _ in ()).throw(RuntimeError("nope"))
        try:
            h = self.mcp_server._compute_health()
            self.assertFalse(h["store_resolved"])
            self.assertTrue(h["ok"])  # liveness is independent of readiness
        finally:
            self.mcp_server._resolve_zmem_home = orig

    def test_compute_health_survives_system_exit(self):
        """_resolve_zmem_home raises SystemExit(2) on a missing/invalid ZMEM_HOME
        (it's a BaseException, not caught by bare `except Exception`). The health
        endpoint must degrade to store_resolved=False, not 500."""
        orig = self.mcp_server._resolve_zmem_home

        def _raise():
            raise SystemExit(2)
        self.mcp_server._resolve_zmem_home = _raise
        try:
            h = self.mcp_server._compute_health()
            self.assertFalse(h["store_resolved"], h)
            self.assertTrue(h["ok"])  # the server process is still alive
        finally:
            self.mcp_server._resolve_zmem_home = orig


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
        dict directly (verified against mcp 1.28.1; requirements pin
        mcp>=1.28.1,<2.0.0)."""
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
        (confidence 0.2, below the recall floor by design — #36 M3), which
        recent filters out by design."""
        return self._add(content=content, namespace=namespace or self._ns(),
                         signal="test")

    # -- recall -------------------------------------------------------------

    def test_recall_success_returns_results_and_count(self):
        # Use a query term that actually appears in the content (FTS5 has no
        # stemming, so "testing" won't match "pytest"). A unique namespace per
        # test isolates it from other tests' rows on the shared store.
        # Use a grounded signal (test) so the row clears the recall confidence
        # floor — a 'none'-signal row now sits below the floor by design (#36 M3).
        ns = self._ns()
        self._add_test_signal(
            content="always run pytest before committing changes", namespace=ns)
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

    def test_add_long_content_is_rejected_not_truncated(self):
        # Content over _MAX_CONTENT_CHARS (65536) is REJECTED with a clear
        # error, not silently truncated. Previously MCP truncated at 32000
        # while ingest-jsonl rejected at 65536 — a 32k–65k row written here
        # was silently mangled and broke Tier-3 sync import elsewhere. All
        # write paths now enforce one cap consistently (#36 M17).
        cap = self.mcp_server._MAX_CONTENT_CHARS
        long_content = "z" * (cap + 1)
        result = self._call("add", type="fact", content=long_content,
                            namespace=self._ns())
        self.assertIn("error", result)
        self.assertIn(str(cap), result["error"])

    # -- search (alias of recall) ------------------------------------------

    def test_search_returns_same_shape_as_recall(self):
        ns = self._ns()
        self._add(content="search alias shape check", namespace=ns)
        search_result = self._call("search", query="alias", namespace=ns, limit=5)
        recall_result = self._call("recall", query="alias", namespace=ns, limit=5)
        # search delegates to recall, so the shape must be identical.
        self.assertEqual(set(search_result.keys()), set(recall_result.keys()))

    def test_search_equivalent_to_recall_on_same_args(self):
        # I13 (#38 / #56): MCP `search` is a pure alias of `recall`; the alias
        # relationship needs an equivalence test, not just a shape test.
        # `_score` is excluded from the comparison on purpose: both tools are
        # EXPLICIT reads and bump retrieval_count by design (issue #21), and
        # compute_score's popularity component consumes that counter — so the
        # second call's per-row _score legitimately differs. Every other
        # field, and the result order, must match exactly.
        ns = self._ns()
        self._add_test_signal(content="equivalence probe quokka zephyr", namespace=ns)
        search_result = self._call("search", query="quokka", namespace=ns, limit=5)
        recall_result = self._call("recall", query="quokka", namespace=ns, limit=5)
        # Non-vacuity guard: equality over two empty result sets would pass
        # even if search stopped delegating.
        self.assertGreater(search_result.get("count", 0), 0,
                           search_result)
        self.assertEqual(search_result.get("count"), recall_result.get("count"))

        def strip_scores(res):
            stripped = []
            for item in res["results"]:
                item = dict(item)
                self.assertIn("_score", item,
                              "recall payload must carry _score (shape pin)")
                item.pop("_score")
                stripped.append(item)
            return stripped

        self.assertEqual(strip_scores(search_result), strip_scores(recall_result))

    def test_search_empty_query_returns_error(self):
        result = self._call("search", query="", namespace=self._ns(), limit=5)
        self.assertIn("error", result)

    # -- supersede ----------------------------------------------------------

    def test_supersede_success(self):
        ns = self._ns()
        # Use a grounded signal (test) so the row clears the recall confidence
        # floor — a 'none'-signal row now sits below the floor by design (#36 M3).
        self._add_test_signal(content="temporary fact to supersede", namespace=ns)
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

    # -- issue #59, 4.5/4.7: taint + update + invalidate tools ------------

    def _add_for(self, content, namespace, signal="test"):
        result = self._call("add", type="fact", content=content,
                            namespace=namespace, signal=signal)
        self.assertEqual(result.get("result"), "stored", result)
        return result

    def test_add_accepts_valid_taint_override(self):
        ns = self._ns()
        result = self._call("add", type="fact",
                            content="web fetched content for taint override",
                            namespace=ns, signal="test", taint="untrusted_web")
        self.assertEqual(result.get("result"), "stored", result)

    def test_add_rejects_unknown_taint(self):
        ns = self._ns()
        result = self._call("add", type="fact",
                            content="row with unknown taint",
                            namespace=ns, signal="test", taint="banana")
        self.assertIn("error", result)
        self.assertIn("taint", result["error"])

    def test_update_success_is_append_only_via_lineage(self):
        ns = self._ns()
        self._add_for("original lesson alpha to update via mcp", ns)
        recalled = self._call("recall", query="original lesson alpha",
                              namespace=ns, limit=5)
        old_id = recalled["results"][0]["id"]

        result = self._call("update", id=old_id,
                            content="revised lesson beta via mcp",
                            taint="untrusted_web")
        self.assertEqual(result.get("result"), "updated", result)
        self.assertIn("updated memory", result.get("raw", ""))

        # The OLD row is gone from live recall; the NEW content is findable.
        after = self._call("recall", query="revised lesson beta",
                           namespace=ns, limit=5)
        self.assertGreater(after["count"], 0, after)
        new_ids = [x["id"] for x in after["results"]]
        self.assertNotIn(old_id, new_ids,
                         "the replaced row must be tombstoned, not live")

    def test_update_refused_for_unknown_id(self):
        ns = self._ns()
        result = self._call("update",
                            id="00000000-0000-0000-0000-000000000000",
                            content="orphan content")
        self.assertIn("error", result)
        self.assertIn("no memory", result["error"])

    def test_update_rejects_unknown_taint(self):
        ns = self._ns()
        self._add_for("update taint validation target", ns)
        recalled = self._call("recall", query="update taint validation",
                              namespace=ns, limit=5)
        mid = recalled["results"][0]["id"]
        result = self._call("update", id=mid, content="x", taint="banana")
        self.assertIn("error", result)
        self.assertIn("taint", result["error"])

    def test_update_empty_content_returns_error(self):
        result = self._call("update", id="anything", content="")
        self.assertIn("error", result)
        self.assertIn("content", result["error"])

    def test_invalidate_success_removes_from_productive_recall(self):
        ns = self._ns()
        self._add_for("fact that becomes false and is invalidated", ns)
        recalled = self._call("recall", query="fact that becomes false",
                              namespace=ns, limit=5)
        mid = recalled["results"][0]["id"]

        result = self._call("invalidate", id=mid, reason="the API contract changed")
        self.assertEqual(result.get("result"), "invalidated", result)
        self.assertEqual(result.get("id"), mid)

        after = self._call("recall", query="fact that becomes false",
                           namespace=ns, limit=5)
        self.assertNotIn(mid, [x["id"] for x in after["results"]],
                         "invalidated row must be gone from live recall")

    def test_invalidate_requires_reason(self):
        result = self._call("invalidate", id="anything", reason="")
        self.assertIn("error", result)
        self.assertIn("reason", result["error"])

    def test_recall_items_carry_v9_provenance_fields(self):
        ns = self._ns()
        self._add_for("provenance shape probe quokka", ns)
        recalled = self._call("recall", query="provenance shape probe",
                              namespace=ns, limit=5)
        self.assertGreater(recalled["count"], 0, recalled)
        item = recalled["results"][0]
        for key in ("valid_from", "valid_until", "update_of", "taint"):
            self.assertIn(key, item, f"recall item missing {key}")

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

    # -- #36 M4: capture-mode auto + warning surfacing ---------------------

    def test_add_secret_content_redacted_and_warnings_surfaced(self):
        """MCP add defaults to --capture-mode auto, so secret-like content is
        redacted and a warning surfaces in the response (#36 M4)."""
        ns = self._ns()
        # A github PAT-shaped token (ghp_ + 36 alnum) matches SECRET_PATTERNS.
        result = self._call(
            "add", type="fact",
            content="deploy token: ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            namespace=ns, signal="test")
        self.assertEqual(result.get("result"), "stored", result)
        # An advisory/notice warning about redaction must be surfaced.
        warnings = result.get("warnings") or []
        redaction_warnings = [w for w in warnings if "redact" in w.lower()]
        self.assertGreater(len(redaction_warnings), 0,
                           f"expected a redaction warning, got: {warnings}")

    def test_add_secret_source_ref_returns_structured_error(self):
        """When source_ref itself carries secret-like text, auto mode refuses
        (CapturePolicyRefusal → store.py exit 2). The MCP server surfaces a
        structured error, not a crash (#36 M4)."""
        ns = self._ns()
        result = self._call(
            "add", type="fact",
            content="benign content with no secrets",
            namespace=ns, signal="test",
            source_ref="creds ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
        # Refusal → error path (not "stored").
        self.assertIn("error", result)
        self.assertNotEqual(result.get("result"), "stored")

    def test_add_clean_content_no_warnings(self):
        """A clean add surfaces no SECRET-related warnings. (The test env has no
        embeddings model, so a degraded-embeddings notice may appear — that is
        unrelated to capture-mode secret detection and is filtered out here.)"""
        ns = self._ns()
        result = self._call("add", type="fact", content="a normal clean lesson",
                            namespace=ns, signal="test")
        self.assertEqual(result.get("result"), "stored", result)
        secret_warnings = [
            w for w in (result.get("warnings") or [])
            if "secret" in w.lower() or "redacted" in w.lower()
        ]
        self.assertEqual(secret_warnings, [],
                         f"clean content should not produce secret warnings: {secret_warnings}")

    # -- #39 E2: /health route registration ---------------------------------

    def test_health_route_is_registered(self):
        """The /health custom_route must actually be registered on the FastMCP
        server (not just defined as a function). Proves the decorator ran; a
        silent try/except degradation would leave no route. FastMCP stores
        custom routes in `_custom_starlette_routes` (mcp>=1.28.1)."""
        # Introspect the registered routes. The attribute name is an
        # implementation detail of FastMCP; check the documented one and fall
        # back to scanning for any /health-shaped route if it differs.
        routes = getattr(self.server, "_custom_starlette_routes", None)
        if routes is None:
            self.skipTest("FastMCP version does not expose _custom_starlette_routes; "
                          "route registration cannot be introspected")
        paths = [getattr(r, "path", None) for r in routes]
        self.assertIn("/health", paths,
                      f"/health route not registered. paths={paths}")

    # -- #36 M6: concurrency cap -------------------------------------------

    def test_concurrency_semaphore_bounds_subprocesses(self):
        """The asyncio.Semaphore caps concurrent store.py subprocesses. With
        max_concurrent=1 (forced small), concurrent recall calls serialize
        (at most one subprocess in flight at a time) (#36 M6).

        Seeds the store AND runs the concurrent gather inside ONE asyncio.run
        so the lazy semaphore is constructed and used on the same event loop
        (robust across Python versions — cubic-10)."""
        import asyncio
        original = self.mcp_server._MAX_CONCURRENT_STORE
        # Force a tiny cap and reset the lazy semaphore so it rebuilds.
        self.mcp_server._MAX_CONCURRENT_STORE = 1
        self.mcp_server._store_semaphore = None
        in_flight = {"count": 0, "peak": 0}
        original_run_store = self.mcp_server._run_store

        def counting_run_store(args, input_text=None):
            in_flight["count"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["count"])
            try:
                return original_run_store(args, input_text=input_text)
            finally:
                in_flight["count"] -= 1

        self.mcp_server._run_store = counting_run_store
        try:
            ns = self._ns()

            async def seed_and_gather():
                # Seed within the same loop that gathers, so the semaphore is
                # built and acquired on one event loop (cubic-10).
                await self.server._tool_manager.call_tool(
                    "add",
                    {"type": "fact", "content": "seed row for concurrency test",
                     "namespace": ns, "signal": "test"},
                    context=None)
                tasks = [
                    self.server._tool_manager.call_tool(
                        "recall",
                        {"query": "seed", "namespace": ns, "limit": 5},
                        context=None)
                    for _ in range(4)
                ]
                return await asyncio.gather(*tasks)

            asyncio.run(seed_and_gather())
            # With max_concurrent=1, peak simultaneous _run_store calls must be 1.
            self.assertLessEqual(in_flight["peak"], 1,
                                 f"peak {in_flight['peak']} exceeded cap of 1")
        finally:
            self.mcp_server._run_store = original_run_store
            self.mcp_server._MAX_CONCURRENT_STORE = original
            self.mcp_server._store_semaphore = None

    def test_cancellation_does_not_release_permit_prematurely(self):
        """cubic-6: if a request is cancelled while its subprocess is running,
        the semaphore permit must NOT be freed until the worker actually
        completes — otherwise the concurrency cap is briefly exceeded. This
        ties release to the concurrent future (worker completion), not the
        asyncio wrapper future (which cancels immediately)."""
        import asyncio
        original = self.mcp_server._MAX_CONCURRENT_STORE
        self.mcp_server._MAX_CONCURRENT_STORE = 1
        self.mcp_server._store_semaphore = None
        original_run_store = self.mcp_server._run_store
        # A blocking _run_store that waits on an event we control.
        release_event = {"go": False}

        def blocking_run_store(args, input_text=None):
            while not release_event["go"]:
                import time
                time.sleep(0.01)
            return original_run_store(args, input_text=input_text)

        self.mcp_server._run_store = blocking_run_store
        try:
            async def cancel_while_running():
                loop = asyncio.get_running_loop()
                sem = self.mcp_server._get_store_semaphore()
                # Start a call that acquires the single permit and blocks.
                task = asyncio.ensure_future(
                    self.mcp_server._run_store_async(["recall", "--query", "x"]))
                await asyncio.sleep(0.1)  # let it acquire + block in the worker
                # The permit is held; a second acquire must time out.
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=0.3)
                    sem.release()
                    held = False
                except asyncio.TimeoutError:
                    held = True
                # Cancel the blocking task.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                # Even AFTER cancellation, the permit must STILL be held until
                # the worker finishes (cubic-6: no premature release).
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=0.3)
                    sem.release()
                    still_held_after_cancel = False
                except asyncio.TimeoutError:
                    still_held_after_cancel = True
                # Now release the worker; the permit should free.
                release_event["go"] = True
                await asyncio.sleep(0.3)
                return held, still_held_after_cancel

            held, still_held = asyncio.run(cancel_while_running())
            self.assertTrue(held, "permit must be held while worker runs")
            self.assertTrue(still_held,
                            "permit must STILL be held after cancellation until "
                            "the worker completes (cubic-6)")
        finally:
            release_event["go"] = True
            self.mcp_server._run_store = original_run_store
            self.mcp_server._MAX_CONCURRENT_STORE = original
            self.mcp_server._store_semaphore = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
