"""Tokenizer-strip supplements for the #395 attribution surfaces.

Non-frozen companions: (1) storelib.ops._strip_attribution under the
consumer grammar (the ops flush strip the frozen C3 does not cover);
(2) the mcp_server loader's cold-import behavior (sys.path seam + cache
reset, plan-critic round-2 finding R2-F4); (3) the disclosed separator
normalization on the MCP add path in map mode (plan finding F9).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess


def _map_owner_only(path) -> None:
    """#406 contract: a test map must not depend on the directory's
    inherited ACL (non-system drives inherit Users/Authenticated-Users
    allow ACEs, which the hub correctly refuses). POSIX uses chmod."""
    if os.name == "nt":
        subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
    else:
        os.chmod(os.fspath(path), 0o600)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False


class OpsAttributionGrammarTest(unittest.TestCase):
    """ops._strip_attribution drops forged tags under the #381 grammar."""

    def setUp(self):
        # storelib is importable from the scripts dir; purge any frozen
        # module state so this class sees the live tree.
        for name in [k for k in sys.modules
                     if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[name]
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from storelib.ops import _strip_attribution
        self._strip = _strip_attribution

    def test_case_varied_and_noncomma_forms_stripped(self):
        out = self._strip("Host:evil,x;host:evil2, agent:legit-ish,plain")
        lowered = out.lower()
        self.assertNotIn("host:", lowered.replace("host:", "", 0))
        for token in out.split(","):
            self.assertFalse(
                token.lower().startswith(("host:", "agent:")),
                f"forged token survived: {token!r} in {out!r}")
        self.assertIn("plain", out)

    def test_denied_shape_and_empty(self):
        self.assertEqual(self._strip(""), "")
        self.assertEqual(self._strip(None), "")
        self.assertEqual(self._strip("host:only"), "")

    def test_benign_separators_normalize(self):
        # Disclosed normalization: the consumer grammar joins on commas.
        self.assertEqual(self._strip("topic:a;topic:b foo"), "topic:a,topic:b,foo")


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class McpLoaderColdImportTest(unittest.TestCase):
    """The tokenizer loader resolves the store checkout from a cold state
    and restores sys.path exactly (plan-critic round-2 R2-F4)."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_t13_ops_mcp", SERVER_DIR / "mcp_server.py")
        self.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_t13_ops_mcp"] = self.mcp_server
        spec.loader.exec_module(self.mcp_server)

    def tearDown(self):
        sys.modules.pop("zmem_t13_ops_mcp", None)

    def test_cold_load_resolves_and_restores_path(self):
        # Cold state: no cached tokenizer, no storelib modules, a sys.path
        # that does not carry the scripts dir.
        self.mcp_server._split_tag_tokens = None
        for name in [k for k in sys.modules
                     if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[name]
        before = list(sys.path)
        if str(SCRIPTS_DIR) in before:
            before = [p for p in before if p != str(SCRIPTS_DIR)]
            sys.path[:] = before
        fn = self.mcp_server._load_split_tag_tokens()
        self.assertEqual(list(sys.path), before,
                         "the loader must restore sys.path exactly")
        self.assertEqual(fn("a;b c"), ["a", "b", "c"])
        # cached now: a second call must not touch sys.path
        sys.path.append("zmem-t13-probe sentinel-dir")
        try:
            self.assertEqual(self.mcp_server._load_split_tag_tokens()
                             is fn, True)
            self.assertEqual(sys.path[-1], "zmem-t13-probe sentinel-dir")
        finally:
            sys.path.remove("zmem-t13-probe sentinel-dir")

    def test_import_failure_raises_loudly(self):
        self.mcp_server._split_tag_tokens = None
        for name in [k for k in sys.modules
                     if k == "storelib" or k.startswith("storelib.")]:
            del sys.modules[name]
        before = list(sys.path)

        real_resolve = self.mcp_server._resolve_store_py

        def _broken():
            raise RuntimeError("no store checkout")

        self.mcp_server._resolve_store_py = _broken
        try:
            with self.assertRaises(Exception):
                self.mcp_server._load_split_tag_tokens()
            self.assertEqual(list(sys.path), before,
                             "sys.path restored even on failure")
        finally:
            self.mcp_server._resolve_store_py = real_resolve
            self.mcp_server._split_tag_tokens = None


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class McpSeparatorNormalizationPinTest(unittest.TestCase):
    """Map-mode add re-joins tag tokens with commas (disclosed #395 F9
    normalization); legacy mode is untouched (returns pre-tokenizer)."""

    def setUp(self):
        import importlib.util
        self._tmp = tempfile.mkdtemp(prefix="zmem-t13-norm-")
        self._saved = {k: os.environ.get(k) for k in (
            "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKEN",
            "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR")}
        map_path = os.path.join(self._tmp, "principals.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump({"norm-secret-token": {
                "principal": "norm-host", "host": "norm-host",
                "harness": "claude", "namespaces": ["project:norm"]}}, f)
        _map_owner_only(map_path)
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = os.path.join(self._tmp, "store.sqlite")
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self._tmp, "no-models")
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        os.environ["ZMEM_MCP_TOKENS_FILE"] = map_path
        spec = importlib.util.spec_from_file_location(
            "zmem_t13_norm_mcp", SERVER_DIR / "mcp_server.py")
        self.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_t13_norm_mcp"] = self.mcp_server
        spec.loader.exec_module(self.mcp_server)

    def tearDown(self):
        import shutil
        sys.modules.pop("zmem_t13_norm_mcp", None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_benign_multiseparator_tags_normalize_to_commas(self):
        import asyncio
        import sqlite3
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)
        verifier = self.mcp_server.resolve_token_auth()[0]
        access_token = asyncio.run(verifier.verify_token("norm-secret-token"))
        self.assertIsNotNone(access_token)
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        async def call():
            return await server._tool_manager.call_tool(
                "add", {"type": "fact", "content": "norm probe",
                        "namespace": "project:norm",
                        "tags": "topic:a;topic:b tag"},
                context=None)
        ctx = auth_context_var.set(AuthenticatedUser(access_token))
        try:
            out = asyncio.run(call())
        finally:
            auth_context_var.reset(ctx)
        payload = out[0] if isinstance(out, tuple) else out
        row_id = payload.get("id")
        conn = sqlite3.connect(os.path.join(self._tmp, "store.sqlite"))
        tags = conn.execute(
            "SELECT tags FROM memory WHERE id=?", (row_id,)).fetchone()[0]
        conn.close()
        # normalization + server stamps, no forged attribution anywhere
        toks = set(tags.split(","))
        self.assertEqual(toks, {"topic:a", "topic:b", "tag",
                                "host:norm-host", "agent:claude"})


if __name__ == "__main__":
    unittest.main()
