"""Regression tests for issue #371: request-scoped principal hardening.

AC1 (as widened by the issue comments): a map reload that drops the
presenting principal between a write tool's namespace guard and its
attribution stamp must never leak client host:/agent: tags (add) nor lose
the caller's stamp (session_end/update). The fix captures the principal
once per tool call (the #284 `caller=` seam) AND strips attribution
prefixes under map mode even when the resolved principal yields no stamps.

AC3: the loader/verifier guard family (non-bool revoked, oversize token,
non-UTF-8-encodable token) is pinned; the SIGHUP handler path is pinned
where the platform provides SIGHUP.

AC4: legacy mode str()-coerces non-string tags at the argv boundary.

Run: python tests/test_t371_principal_hardening.py   (no pytest required)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
NS = "project:t371/race"
NS_B = "project:t371/other"
TOKEN = "t-t371-alpha"

saved_env = {k: os.environ.get(k) for k in
             ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_MCP_TOKEN",
              "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
              "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
              "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}


def _map_owner_only(path) -> None:
    if os.name == "nt":
        r = subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
        assert r.returncode == 0, "icacls owner-only pin failed"
    else:
        os.chmod(os.fspath(path), 0o600)


try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False


def _load_auth():
    if "mcp.server.auth.provider" not in sys.modules and not MCP_AVAILABLE:
        provider = types.ModuleType("mcp.server.auth.provider")

        class AccessToken:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class TokenVerifier:
            pass

        provider.AccessToken = AccessToken
        provider.TokenVerifier = TokenVerifier
        mcp_mod = types.ModuleType("mcp")
        server_mod = types.ModuleType("mcp.server")
        auth_mod = types.ModuleType("mcp.server.auth")
        auth_mod.provider = provider
        server_mod.auth = auth_mod
        mcp_mod.server = server_mod
        sys.modules.update({
            "mcp": mcp_mod, "mcp.server": server_mod,
            "mcp.server.auth": auth_mod,
            "mcp.server.auth.provider": provider,
        })
    spec = importlib.util.spec_from_file_location(
        "zmem_auth_t371", SERVER_DIR / "auth.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["zmem_auth_t371"] = mod
    spec.loader.exec_module(mod)
    return mod


AUTH = _load_auth()


def _entry(pid="alpha", namespaces=(NS,), revoked=False):
    e = {"principal": pid, "host": "dgx-fb", "harness": "claude"}
    if namespaces is not None:
        e["namespaces"] = list(namespaces)
    if revoked:
        e["revoked"] = True
    return e


class _MapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-t371-")
        self.map_path = os.path.join(self.tmp, "map.json")
        self.store = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_STORE"] = self.store
        os.environ["ZMEM_DATA"] = self.tmp
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-models")
        for k in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
                  "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            os.environ.pop(k, None)
        self._load_server_module()

    def tearDown(self):
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _load_server_module(self):
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_t371", SERVER_DIR / "mcp_server.py")
        self.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_t371"] = self.mcp_server
        spec.loader.exec_module(self.mcp_server)

    def write_map(self, mapping):
        with open(self.map_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f)
        _map_owner_only(self.map_path)
        return self.map_path

    def build(self):
        os.environ["ZMEM_MCP_TOKENS_FILE"] = self.map_path
        return self.mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)

    def _call(self, server, name, **args):
        async def _one():
            return await server._tool_manager.call_tool(name, args,
                                                        context=None)
        return asyncio.run(_one())

    def _row_tags(self, like):
        if not os.path.exists(self.store):
            return ""
        conn = sqlite3.connect(self.store)
        try:
            row = conn.execute(
                "SELECT tags FROM memory WHERE content LIKE ?",
                (like,)).fetchone()
        finally:
            conn.close()
        return (row[0] if row and row[0] else "")


@unittest.skipUnless(MCP_AVAILABLE, "mcp package unavailable")
class ReloadRaceTest(_MapCase):
    """AC1 over add, update, and session_end (the three stamping paths)."""

    def _server_with_dropping_map(self, drop_after=1):
        self.write_map({TOKEN: _entry()})
        server = self.build()
        real_map = server._token_verifier._map
        real_fn = real_map.by_principal_id
        state = {"calls": 0}

        def dropping(pid):
            state["calls"] += 1
            if state["calls"] <= drop_after:
                return real_fn(pid)
            return None  # the map 'reloaded' and the principal is gone

        real_map.by_principal_id = dropping
        return server, state

    def _authed(self, server):
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        access = asyncio.run(server._token_verifier.verify_token(TOKEN))
        return auth_context_var, auth_context_var.set(
            AuthenticatedUser(access))

    def test_add_never_stores_forged_attribution(self):
        server, state = self._server_with_dropping_map()
        ctx_var, ctx = self._authed(server)
        try:
            result = self._call(server, "add", type="fact",
                                content="t371 add race row", namespace=NS,
                                signal="test",
                                tags="host:evil,agent:evil,keepme")
        finally:
            ctx_var.reset(ctx)
        self.assertNotIn("error", json.dumps(result), result)
        tags = self._row_tags("t371 add race row%")
        self.assertNotIn("host:evil", tags)
        self.assertNotIn("agent:evil", tags)
        self.assertIn("host:dgx-fb", tags)
        self.assertIn("keepme", tags)

    def test_session_end_keeps_stamp_across_reload(self):
        server, state = self._server_with_dropping_map()
        ctx_var, ctx = self._authed(server)
        try:
            result = self._call(server, "session_end", namespace=NS,
                                note="t371 session_end race row")
        finally:
            ctx_var.reset(ctx)
        self.assertNotIn("error", json.dumps(result), result)
        tags = self._row_tags("t371 session_end race row%")
        self.assertIn("host:dgx-fb", tags)
        self.assertIn("agent:claude", tags)

    def test_update_never_stores_forged_attribution(self):
        # Seed a row as the live principal (no drop), then update under the
        # dropping map: the client-supplied forged tags must not land.
        seed_server, _ = self._server_with_dropping_map(drop_after=99)
        ctx_var, ctx = self._authed(seed_server)
        try:
            seeded = self._call(seed_server, "add", type="fact",
                                content="t371 update race row", namespace=NS,
                                signal="test")
        finally:
            ctx_var.reset(ctx)
        self.assertNotIn("error", json.dumps(seeded), seeded)
        row = sqlite3.connect(self.store).execute(
            "SELECT id FROM memory WHERE content LIKE ?",
            ("t371 update race row%",)).fetchone()

        server, state = self._server_with_dropping_map()
        ctx_var, ctx = self._authed(server)
        try:
            result = self._call(server, "update", id=row[0],
                                content="t371 update race row v2",
                                namespace=NS,
                                tags="host:evil,agent:evil,keepme")
        finally:
            ctx_var.reset(ctx)
        self.assertNotIn("error", json.dumps(result), result)
        tags = self._row_tags("t371 update race row v2%")
        self.assertNotIn("host:evil", tags)
        self.assertNotIn("agent:evil", tags)
        self.assertIn("host:dgx-fb", tags)

    def test_single_capture_one_resolution_per_call(self):
        server, state = self._server_with_dropping_map(drop_after=99)
        ctx_var, ctx = self._authed(server)
        try:
            result = self._call(server, "add", type="fact",
                                content="t371 resolutions row", namespace=NS,
                                signal="test")
        finally:
            ctx_var.reset(ctx)
        self.assertNotIn("error", json.dumps(result), result)
        self.assertEqual(
            state["calls"], 1,
            "add must resolve the presenting principal exactly once "
            f"(observed {state['calls']})")


class LoaderGuardTest(_MapCase):
    """AC3: the loader/verifier family pins (guards exist; now tested)."""

    def _stderr_exit(self):
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stderr(buf):
            try:
                AUTH.load_principal_map(self.map_path)
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue() + ("" if code == 2 else f"[exit {code}]")

    def test_non_bool_revoked_exits_two(self):
        self.write_map({TOKEN: {**_entry(), "revoked": "yes"}})
        err = self._stderr_exit()
        self.assertIn("revoked", err)
        self.assertIn("boolean", err)

    def test_oversize_presented_token_refused(self):
        self.write_map({TOKEN: _entry()})
        pm = AUTH.load_principal_map(self.map_path)
        verifier = AUTH.MapTokenVerifier(pm)
        oversized = "t" * (AUTH._MAX_BEARER_TOKEN_UTF8_BYTES + 10)
        self.assertIsNone(asyncio.run(verifier.verify_token(oversized)))

    def test_non_utf8_encodable_token_refused(self):
        self.write_map({TOKEN: _entry()})
        pm = AUTH.load_principal_map(self.map_path)
        verifier = AUTH.MapTokenVerifier(pm)
        got = asyncio.run(verifier.verify_token("\ud800surrogate"))
        self.assertIsNone(got)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"),
                         "SIGHUP is POSIX-only")
    def test_sighup_handler_reloads_map(self):
        # The build registers _reload_principal_map; drive the real signal.
        self.write_map({TOKEN: _entry()})
        server = self.build()
        access = asyncio.run(server._token_verifier.verify_token(TOKEN))
        self.assertIsNotNone(access)
        # Drop alpha from the map, bump mtime past the 1s granularity, SIGHUP.
        self.write_map({"t-t371-beta": _entry("beta", (NS_B,))})
        st = os.stat(self.map_path)
        os.utime(self.map_path, (st.st_atime, st.st_mtime + 5))
        os.kill(os.getpid(), signal.SIGHUP)
        # force_reload is synchronous: alpha's token must now verify to None.
        self.assertIsNone(
            asyncio.run(server._token_verifier.verify_token(TOKEN)))


@unittest.skipUnless(MCP_AVAILABLE, "mcp package unavailable")
class LegacyCoercionTest(_MapCase):
    """AC4: legacy mode coerces non-string tags at the argv boundary."""

    def test_legacy_non_string_tags_coerced(self):
        # Bypass build() (which always sets the plural map var) and build
        # straight against the legacy single-token env.
        os.environ.pop("ZMEM_MCP_TOKENS_FILE", None)
        os.environ["ZMEM_MCP_TOKEN"] = "t371-legacy-token"
        server = self.mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        tools = server._tool_manager._tools
        add_fn = tools.get("add").fn
        result = asyncio.run(add_fn(
            type="fact", content="t371 coercion row", namespace="user:t371",
            signal="test", tags=12345))
        self.assertNotIn("error", json.dumps(result), result)
        tags = self._row_tags("t371 coercion row%")
        self.assertIn("12345", tags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
