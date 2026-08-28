"""Scoped MCP token tests (issue #65, 10.2).

Covers:
- TokenConfig parsing rules (env bare / file bare / file JSON / malformed
  configs exit 2 — the plan-critic C1 matrix)
- NamespaceDenied semantics on the config object
- End-to-end guard behavior through the real tool surface: a scoped token
  cannot read or write a foreign project (stable namespace_not_allowed
  error), an unscoped operator token still can, and the implicit
  user:global union is suppressed for scopes that exclude user:global
- No response ever leaks the token value

Runs standalone: python tests/test_mcp_auth.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False


def _load_auth_module():
    """Load hermes-plugin/server/auth.py standalone (stub-free: under
    MCP_AVAILABLE the real mcp package provides the ABCs)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("zmem_auth_test", SERVER_DIR / "auth.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["zmem_auth_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class TokenConfigParsingTest(unittest.TestCase):
    """The C1 matrix: sniff rule, malformed configs, near-miss namespaces."""

    def setUp(self):
        self.auth = _load_auth_module()
        self._tmp = tempfile.mkdtemp(prefix="zmem-mcp-auth-")
        self._env = {
            k: os.environ.get(k) for k in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE")
        }
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _token_file(self, content: str) -> str:
        p = os.path.join(self._tmp, "token.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_env_var_is_unscoped_operator_token(self):
        os.environ["ZMEM_MCP_TOKEN"] = "env-bare-secret"
        cfg = self.auth.load_token_config()
        self.assertEqual(cfg.token, "env-bare-secret")
        self.assertFalse(cfg.scoped)
        self.assertIsNone(cfg.namespaces)
        # Unscoped allows everything, including None (no namespace).
        cfg.check_namespace(None)
        cfg.check_namespace("project:anything")

    def test_bare_file_token_is_unscoped(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file("  file-bare-secret \n")
        cfg = self.auth.load_token_config()
        self.assertEqual(cfg.token, "file-bare-secret")
        self.assertFalse(cfg.scoped)

    def test_json_file_scoped_token(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file(json.dumps({
            "token": "scoped-secret",
            "namespaces": ["project:zmem", "user:global"],
        }))
        cfg = self.auth.load_token_config()
        self.assertTrue(cfg.scoped)
        self.assertEqual(
            cfg.namespaces, frozenset({"project:zmem", "user:global"}))

    def test_json_file_without_namespaces_key_is_unscoped(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file(
            json.dumps({"token": "json-unscoped"}))
        cfg = self.auth.load_token_config()
        self.assertEqual(cfg.token, "json-unscoped")
        self.assertFalse(cfg.scoped)

    def test_json_looking_bare_token_exits_2(self):
        # A file that STARTS with '{' but does not parse must be a hard
        # startup error — never a silent fallback to bare/unscoped mode.
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file('{"token": "x", ')
        with self.assertRaises(SystemExit) as cm:
            self.auth.load_token_config()
        self.assertEqual(cm.exception.code, 2)

    def test_empty_namespaces_list_exits_2(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file(
            json.dumps({"token": "x", "namespaces": []}))
        with self.assertRaises(SystemExit) as cm:
            self.auth.load_token_config()
        self.assertEqual(cm.exception.code, 2)

    def test_near_miss_global_scope_exits_2(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file(
            json.dumps({"token": "x", "namespaces": ["global"]}))
        with self.assertRaises(SystemExit) as cm:
            self.auth.load_token_config()
        self.assertEqual(cm.exception.code, 2)

    def test_missing_token_in_json_exits_2(self):
        os.environ["ZMEM_MCP_TOKEN_FILE"] = self._token_file(
            json.dumps({"namespaces": ["project:x"]}))
        with self.assertRaises(SystemExit) as cm:
            self.auth.load_token_config()
        self.assertEqual(cm.exception.code, 2)

    def test_no_config_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            self.auth.load_token_config()
        self.assertEqual(cm.exception.code, 2)

    def test_scoped_config_denials(self):
        cfg = self.auth.TokenConfig(
            token="t", namespaces=frozenset({"project:mine"}))
        cfg.check_namespace("project:mine")
        for bad in ("project:other", "user:global", None, "*", ""):
            with self.assertRaises(self.auth.NamespaceDenied, msg=repr(bad)):
                cfg.check_namespace(bad)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class ScopedTokenToolSurfaceTest(unittest.TestCase):
    """End-to-end through the real FastMCP tool manager (same harness as
    tests/test_mcp_server.py, which this extends for the scope guard)."""

    @classmethod
    def setUpClass(cls):
        import asyncio  # noqa: F401
        cls._tmp = tempfile.mkdtemp(prefix="zmem-mcp-auth-srv-")
        cls._store = os.path.join(cls._tmp, "store.sqlite")
        cls._saved_env = {
            k: os.environ.get(k) for k in (
                "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKEN",
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MODEL_AUTODOWNLOAD",
                "ZMEM_MODELS_DIR",
            )
        }
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = cls._store
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(cls._tmp, "no-such-models")
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_auth_server", SERVER_DIR / "mcp_server.py")
        cls.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_auth_server"] = cls.mcp_server
        spec.loader.exec_module(cls.mcp_server)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _build(self, *, scoped: bool):
        """Build a server with an unscoped env token or a scoped file token."""
        import asyncio

        if scoped:
            tok = os.path.join(self._tmp, "scoped-token.json")
            with open(tok, "w", encoding="utf-8") as f:
                json.dump({"token": "scoped-secret",
                           "namespaces": ["project:mine"]}, f)
            os.environ["ZMEM_MCP_TOKEN_FILE"] = tok
            os.environ.pop("ZMEM_MCP_TOKEN", None)
        else:
            os.environ["ZMEM_MCP_TOKEN"] = "unscoped-secret"
            os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)

        async def _call(name, **args):
            return await server._tool_manager.call_tool(name, args, context=None)

        return _call

    def test_scoped_token_cannot_write_foreign_namespace(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("add", type="fact",
                                   content="foreign write attempt",
                                   namespace="project:other"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)
        self.assertNotIn("scoped-secret", json.dumps(result))

    def test_scoped_token_can_write_its_own_namespace(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("add", type="fact",
                                   content="in-scope write",
                                   namespace="project:mine", signal="test"))
        self.assertEqual(result.get("result"), "stored", result)

    def test_scoped_token_cannot_read_foreign_namespace(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("recall", query="anything",
                                   namespace="project:other"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)

    def test_scoped_token_read_without_namespace_denied(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("recent"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)

    def test_scoped_token_add_default_namespace_denied(self):
        # add defaults to user:global, which is NOT in this scope.
        _call = self._build(scoped=True)
        result = asyncio_run(_call("add", type="fact",
                                   content="default ns write"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)

    def test_scoped_token_session_start_own_namespace_allowed(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("session_start", namespace="project:mine"))
        self.assertEqual(result.get("result"), "session_started", result)

    def test_scoped_token_session_start_default_denied(self):
        _call = self._build(scoped=True)
        result = asyncio_run(_call("session_start"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)

    def test_scoped_token_update_namespace_override_guarded(self):
        _call = self._build(scoped=True)
        result = asyncio_run(
            _call("update", id="00000000-0000-0000-0000-000000000000",
                  content="x", namespace="project:other"))
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)

    def test_scoped_update_without_override_checks_inherited_namespace(self):
        # Final-critic B3: without an override the replacement row INHERITS
        # the target's namespace — a scoped token must not be able to
        # rewrite a row living in a foreign namespace.
        _call_unscoped = self._build(scoped=False)
        foreign = asyncio_run(_call_unscoped(
            "add", type="fact", content="foreign ns row for inherited guard",
            namespace="project:other", signal="test"))
        self.assertEqual(foreign.get("result"), "stored", foreign)
        mine = asyncio_run(_call_unscoped(
            "add", type="fact", content="own ns row for inherited guard",
            namespace="project:mine", signal="test"))
        self.assertEqual(mine.get("result"), "stored", mine)

        _call = self._build(scoped=True)
        denied = asyncio_run(_call(
            "update", id=foreign["id"], content="scoped rewrite attempt"))
        self.assertEqual(denied.get("error"), "namespace_not_allowed", denied)

        ok = asyncio_run(_call(
            "update", id=mine["id"], content="scoped rewrite of own row"))
        self.assertEqual(ok.get("result"), "updated", ok)

    def test_unscoped_token_still_full_access(self):
        _call = self._build(scoped=False)
        r1 = asyncio_run(_call("add", type="fact", content="unscoped write ok",
                               namespace="project:anywhere", signal="test"))
        self.assertEqual(r1.get("result"), "stored", r1)
        r2 = asyncio_run(_call("recent"))
        self.assertIn("results", r2)

    def test_no_token_value_ever_leaks_in_responses(self):
        _call = self._build(scoped=True)
        for name, args in (
            ("recall", {"query": "x", "namespace": "project:other"}),
            ("add", {"type": "fact", "content": "x", "namespace": "p:q"}),
            ("search", {"query": "x"}),
            ("recent", {}),
            ("session_start", {}),
        ):
            result = asyncio_run(_call(name, **args))
            blob = json.dumps(result)
            self.assertNotIn("scoped-secret", blob, f"{name} leaked the token")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
