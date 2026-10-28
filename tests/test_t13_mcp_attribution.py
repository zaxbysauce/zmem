"""MCP write-path attribution strip uses the consumer grammar (issue #395
item 1 / trace AC3).

The add/update attribution strip in hermes-plugin/server/mcp_server.py
(`_apply_add_attribution` / `_merge_update_tags`) is comma-only and
case-SENSITIVE at base, while the SAME strip in storelib (issue #381) was
fixed to the consumer tokenizer (`storelib/entity.py split_tag_tokens`,
separators `[,;\\s]+`, case-insensitive prefixes). Trace probe P2 proved
the escape end-to-end at base: an add with tags

    Host:evil,x;host:evil2, agent:legit-ish

stores `Host:evil,x;host:evil2,host:host-a,agent:claude` — both forged
attributions survive (capital-H `Host:` and the semicolon-joined
`x;host:evil2` are never tokenized as attribution), and the trailing
`agent:legit-ish` IS stripped only by luck of case.

Contract frozen here (AC3): when an MCP add/update carries attribution
tags in case-varied or non-comma forms, the server strips them under the
consumer tokenizer, stamping only caller-derived attribution
(`host:host-a` + `agent:claude` from the map entry). RED at base, GREEN
after the fix.

Harness shape copied from tests/test_t02_principal.py: spec-load
hermes-plugin/server/mcp_server.py, build_server in map mode with a
one-principal ZMEM_MCP_TOKENS_FILE, verify the token via
resolve_token_auth()[0], and wrap the direct
`_tool_manager.call_tool("add"/"update", ...)` in the auth_context_var
AuthenticatedUser pattern (`_presenting`). The stored row's tags are read
back from the scratch store (sqlite) — the store is what consumers read,
so the assertion is on the persisted surface, not the tool echo.

RED at base (probe P2), GREEN after issue #395 item 1's fix.

Runs standalone: python tests/test_t13_mcp_attribution.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False

_TOKEN = "t13-attribution-token"
_FORGED_TAGS = "Host:evil,x;host:evil2, agent:legit-ish"
# The only host:/agent: tokens allowed on the stored row: the server-side
# stamps derived from the presented principal's map entry.
_SERVER_STAMPS = ("host:host-a", "agent:claude")


@contextlib.contextmanager
def _presenting(access_token):
    """Simulate one authenticated HTTP request carrying access_token.

    The pinned mcp SDK installs AuthContextMiddleware, which stores the
    AuthenticatedUser in auth_context_var for the duration of a request;
    tools read it back via get_access_token. Setting the same contextvar
    around a direct tool-manager call is the HTTP-free equivalent of the
    request presenting that token. (Copied from tests/test_t02_principal.py.)
    """
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    ctx_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(ctx_token)


def _asyncio_run(coro):
    return asyncio.run(coro)


def _split_tag_tokens(tags):
    """The REAL consumer tokenizer (storelib/entity.py, issue #381) — the
    grammar every consumer uses to read stored tags back. Loaded by file
    location with skills/memory/scripts on sys.path (its flat imports
    require it), so the assertion judges tokenhood under the production
    grammar, never a reimplementation."""
    scripts_dir = str(REPO_ROOT / "skills" / "memory" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from storelib.entity import split_tag_tokens
    return split_tag_tokens(tags)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class McpAttributionGrammarTest(unittest.TestCase):
    """AC3: forged case-varied / non-comma attribution tags must not
    survive the add/update strip; only the server stamps may remain."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_t13_server", SERVER_DIR / "mcp_server.py")
        cls.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_t13_server"] = cls.mcp_server
        spec.loader.exec_module(cls.mcp_server)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-t13-attribution-")
        self._store = os.path.join(self._tmp, "store.sqlite")
        self._map_path = os.path.join(self._tmp, "principals.json")
        with open(self._map_path, "w", encoding="utf-8") as f:
            json.dump({_TOKEN: {
                "principal": "machine-a",
                "host": "host-a",
                "harness": "claude",
                "namespaces": ["project:probe"],
            }}, f)
        # The fix (#406) refuses a group/world-readable map at load; the
        # fstat/DACL verdict on the OPEN descriptor is REAL, so the file
        # itself must satisfy the installer contract everywhere: POSIX
        # umask 022 creates 0o644, and Windows non-system directories
        # inherit Users/Authenticated-Users allow ACEs (0o600 chmod is a
        # DACL no-op on NTFS). Pin owner-only for real.
        import subprocess
        if os.name == "nt":
            subprocess.run(
                ["icacls", self._map_path, "/inheritance:r",
                 "/grant", os.environ.get("USERNAME", "") + ":(F)"],
                capture_output=True, timeout=30)
        else:
            os.chmod(self._map_path, 0o600)
        # Env pinned exactly like tests/test_t02_principal.py setUp: the
        # store can only ever land in the scratch dir.
        self._env = {
            k: os.environ.get(k) for k in (
                "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKEN",
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
            )
        }
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = self._store
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(
            self._tmp, "no-such-models")
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        os.environ.pop("ZMEM_MCP_TOKENS_FILE", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- fixtures -----------------------------------------------------------

    def _build_and_mint(self):
        """build_server in map mode + the AccessToken for the one token."""
        os.environ["ZMEM_MCP_TOKENS_FILE"] = self._map_path
        server = self.mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        verifier = self.mcp_server.resolve_token_auth()[0]
        access_token = _asyncio_run(verifier.verify_token(_TOKEN))
        self.assertIsNotNone(access_token, "map token must verify")
        return server, access_token

    def _call(self, server, name, **args):
        async def _one():
            return await server._tool_manager.call_tool(
                name, args, context=None)
        return _asyncio_run(_one())

    def _stored_tags(self, mid):
        """The persisted tags of the stored row (the consumer surface)."""
        conn = sqlite3.connect(self._store)
        try:
            row = conn.execute(
                "SELECT tags FROM memory WHERE id=?", (mid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, f"stored row {mid} must exist")
        return row[0] or ""

    def _assert_only_server_attribution(self, tags):
        """No forged host/agent attribution survives in ANY form — case
        varied, semicolon-joined, whitespace-joined — and the server's own
        stamps ARE present."""
        self.assertNotIn(
            "Host:evil", tags,
            f"case-varied forged attribution survived the strip: {tags!r}")
        self.assertNotIn(
            "x;host:evil2", tags,
            f"non-comma (semicolon) forged attribution survived the strip: "
            f"{tags!r}")
        self.assertNotIn(
            "host:evil", tags.lower(),
            f"lowercase forged attribution survived the strip: {tags!r}")
        self.assertNotIn(
            "agent:legit", tags.lower(),
            f"forged agent attribution survived the strip: {tags!r}")
        tokens = [t for t in (tags or "").split(",") if t.strip()]
        # Under the consumer grammar (the REAL tokenizer pinned by #381 in
        # storelib/entity.py, separators [,;\s]+) no TOKEN may claim
        # host:/agent: attribution except the server's own stamps.
        for token in _split_tag_tokens(tags):
            self.assertFalse(
                token.lower().startswith(("host:", "agent:"))
                and token not in _SERVER_STAMPS,
                f"forged attribution token {token!r} survived the strip "
                f"(stored tags {tags!r})")
        self.assertIn("host:host-a", tokens,
                      f"server host stamp missing from {tags!r}")
        self.assertIn("agent:claude", tokens,
                      f"server agent stamp missing from {tags!r}")

    # -- AC3 add path -------------------------------------------------------

    def test_attribution_strip_normalizes(self):
        """add(): the strip must use the consumer tokenizer, not a
        comma-only case-sensitive split — `Host:evil` and `x;host:evil2`
        must not reach the stored row."""
        server, access_token = self._build_and_mint()
        with _presenting(access_token):
            written = self._call(
                server, "add", type="fact",
                content=f"t13 ac3 forged attribution row {uuid.uuid4().hex}",
                namespace="project:probe", signal="test",
                tags=_FORGED_TAGS)
        self.assertEqual(written.get("result"), "stored", written)
        self._assert_only_server_attribution(self._stored_tags(written["id"]))

    # -- AC3 update path ----------------------------------------------------

    def test_update_merge_strips_forged(self):
        """update(): the same strip rule on the client-tags branch — the
        replacement row carries only the presented principal's stamps."""
        server, access_token = self._build_and_mint()
        with _presenting(access_token):
            seeded = self._call(
                server, "add", type="fact",
                content=f"t13 ac3 update seed row {uuid.uuid4().hex}",
                namespace="project:probe", signal="test",
                tags="topic:ac3")
        self.assertEqual(seeded.get("result"), "stored", seeded)

        with _presenting(access_token):
            updated = self._call(
                server, "update", id=seeded["id"],
                content=f"t13 ac3 update forged tags {uuid.uuid4().hex}",
                tags=_FORGED_TAGS)
        self.assertEqual(updated.get("result"), "updated", updated)
        self.assertIn("id", updated, updated)
        self._assert_only_server_attribution(
            self._stored_tags(updated["id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
