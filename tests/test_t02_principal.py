"""Request-scoped principals: per-machine token map contract (issue #281).

Freezes the acceptance criteria of issue #281 (Workstream T, PR 2 of 12)
into executable checks. The token source under test is the NEW
ZMEM_MCP_TOKENS_FILE JSON map of per-principal tokens; the legacy
ZMEM_MCP_TOKEN / ZMEM_MCP_TOKEN_FILE behavior is unchanged and stays
covered by tests/test_mcp_auth.py (AC4 and AC5 of the issue).

Contract frozen here:
- AC1: a map with token A scoped to project:a and token B scoped to
  project:b authorizes, for a request presenting B, exactly project:b and
  denies project:a with the stable namespace_not_allowed error (and the
  mirror for A). Authorization follows the PRESENTED principal, never a
  startup-resolved single config.
- AC2: marking B revoked true in the map file and changing the file mtime
  makes the LIVE verifier reject B (verify_token returns None, the
  401-equivalent refusal at the verifier level) without a restart or a
  server rebuild, while sibling token A keeps verifying.
- AC3: a successful add for principal P stamps the stored row with P's
  SERVER-SIDE host and harness (tags host:<host> and agent:<harness> from
  the map entry); any client-supplied host: or agent: tag is not trusted.
  On update the same stamping applies to the replacement row, merging with
  the target row's tags: benign tags are preserved, stale or client-
  supplied host: and agent: tags are replaced by the presented principal's.

Map file schema assumed by this contract: a JSON object keyed by the
plaintext bearer token, each value an object with the string fields
principal, host and harness, an optional namespaces list in the existing
namespace grammar, and an optional revoked boolean (absent means false).

RED at base (no token map / principal API), GREEN after issue #281.
The behavioral tests are MCP-gated exactly like the tool-surface class in
tests/test_mcp_auth.py they extend; the module-level import of the new API
below is deliberately NOT gated, so the base signature is a clean
missing-API ImportError on hosts without the mcp package too.

Runs standalone: python tests/test_t02_principal.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
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

# Distinct synthetic module name so this file never collides with the
# zmem_auth_test registration used by tests/test_mcp_auth.py.
_AUTH_MODULE_NAME = "zmem_auth_test_t02"


def _load_auth_module():
    """Load hermes-plugin/server/auth.py standalone (same pattern as
    tests/test_mcp_auth.py).

    Installs the minimal stub for mcp.server.auth.provider when the mcp
    package is absent, so auth.py execs (and the module-level contract
    import below resolves or fails) identically with or without mcp.
    """
    import importlib.util
    import types

    if "mcp.server.auth.provider" not in sys.modules and not MCP_AVAILABLE:
        provider = types.ModuleType("mcp.server.auth.provider")

        class AccessToken:  # attribute bag, matching the SDK shape we use
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class TokenVerifier:  # Protocol stand-in
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
            "mcp": mcp_mod,
            "mcp.server": server_mod,
            "mcp.server.auth": auth_mod,
            "mcp.server.auth.provider": provider,
        })

    spec = importlib.util.spec_from_file_location(
        _AUTH_MODULE_NAME, SERVER_DIR / "auth.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_AUTH_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_load_auth_module()

# The issue #281 API surface: the per-machine principal map loader. Imported
# at MODULE level so the base signature is a collection-time ImportError
# (missing API) on every host, with or without the mcp package installed —
# never an accidental fixture error. RED at base, GREEN after issue #281.
from zmem_auth_test_t02 import load_principal_map  # noqa: E402,F401

_TOKEN_A = "t02-principal-token-alpha"
_TOKEN_B = "t02-principal-token-beta"
_TOKEN_U = "t02-principal-token-ops"


def _principal_entry(principal, namespaces, host, harness, revoked=False):
    entry = {
        "principal": principal,
        "namespaces": list(namespaces),
        "host": host,
        "harness": harness,
    }
    if revoked:
        entry["revoked"] = True
    return entry


@contextlib.contextmanager
def _presenting(access_token):
    """Simulate one authenticated HTTP request carrying access_token.

    The pinned mcp SDK installs AuthContextMiddleware, which stores the
    AuthenticatedUser in auth_context_var for the duration of a request;
    tools read it back via get_access_token. Setting the same contextvar
    around a direct tool-manager call is the HTTP-free equivalent of the
    request presenting that token.
    """
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    ctx_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(ctx_token)


def asyncio_run(coro):
    return asyncio.run(coro)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class RequestScopedPrincipalTest(unittest.TestCase):
    """The issue #281 acceptance criteria through the real tool surface.

    RED at base (no token map / principal API), GREEN after issue #281.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_t02_server", SERVER_DIR / "mcp_server.py")
        cls.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_t02_server"] = cls.mcp_server
        spec.loader.exec_module(cls.mcp_server)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-t02-principal-")
        self._store = os.path.join(self._tmp, "store.sqlite")
        self._map_path = os.path.join(self._tmp, "principals.json")
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
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self._tmp, "no-such-models")
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

    def _write_map(self, revoke_b=False):
        """Write the two-principal map file; returns its path.

        A is scoped to project:a and B to project:b — two disjoint
        single-namespace scopes, so a guard that resolved any ONE
        startup config cannot satisfy both request presentations below.
        """
        mapping = {
            _TOKEN_A: _principal_entry(
                "alpha", ["project:a"], "dgx-alpha", "claude"),
            _TOKEN_B: _principal_entry(
                "beta", ["project:b"], "dgx-beta", "zcode",
                revoked=revoke_b),
            # An UNSCOPED operator principal (namespaces absent = allow-all):
            # its writes carry ITS server-side stamp, which gives the
            # update-merge test a genuinely stale foreign attribution to
            # strip, and pins that unscoped map principals stamp too.
            _TOKEN_U: _principal_entry("ops", [], "dgx-ops", "hermes"),
        }
        # _principal_entry always writes a namespaces list; the operator
        # entry must omit the key entirely (absent = unscoped).
        mapping[_TOKEN_U].pop("namespaces")
        with open(self._map_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f)
        return self._map_path

    def _build_with_map(self, map_path):
        """build_server with ONLY ZMEM_MCP_TOKENS_FILE configured."""
        os.environ["ZMEM_MCP_TOKENS_FILE"] = map_path
        return self.mcp_server.build_server(host="127.0.0.1", port=0,
                                            use_tls=False)

    def _verifier(self, server):
        verifier = getattr(server, "_token_verifier", None)
        self.assertIsNotNone(
            verifier, "pinned SDK FastMCP keeps its token verifier")
        return verifier

    def _mint(self, verifier, token):
        """The AccessToken the SDK middleware would mint for this token."""
        access_token = asyncio_run(verifier.verify_token(token))
        self.assertIsNotNone(access_token, "map token must verify")
        return access_token

    def _call(self, server, name, **args):
        async def _one():
            return await server._tool_manager.call_tool(name, args, context=None)
        return asyncio_run(_one())

    def _row_tags(self, mid):
        conn = sqlite3.connect(self._store)
        try:
            row = conn.execute(
                "SELECT tags FROM memory WHERE id=?", (mid,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "stored row must exist")
        return {t.strip() for t in (row[0] or "").split(",") if t.strip()}

    # -- AC1 ----------------------------------------------------------------

    def test_guard_uses_request_principal_not_startup_config(self):
        """AC1: authorization follows the token presented WITH the request.

        One server, one map, two principals with disjoint scopes. A guard
        that kept reading a single startup-resolved config would fix one
        scope for every request and necessarily fail one of the two
        mirrored presentations below.
        """
        server = self._build_with_map(self._write_map())
        verifier = self._verifier(server)

        access_b = self._mint(verifier, _TOKEN_B)
        with _presenting(access_b):
            allowed_b = self._call(
                server, "add", type="fact",
                content=f"t02 ac1 b own namespace {uuid.uuid4().hex[:8]}",
                namespace="project:b", signal="test")
            denied_b = self._call(
                server, "add", type="fact",
                content=f"t02 ac1 b foreign namespace {uuid.uuid4().hex[:8]}",
                namespace="project:a", signal="test")
        self.assertEqual(allowed_b.get("result"), "stored", allowed_b)
        self.assertEqual(denied_b.get("error"), "namespace_not_allowed",
                         denied_b)
        self.assertNotIn(_TOKEN_B, json.dumps(denied_b))

        access_a = self._mint(verifier, _TOKEN_A)
        with _presenting(access_a):
            allowed_a = self._call(
                server, "add", type="fact",
                content=f"t02 ac1 a own namespace {uuid.uuid4().hex[:8]}",
                namespace="project:a", signal="test")
            denied_a = self._call(
                server, "add", type="fact",
                content=f"t02 ac1 a foreign namespace {uuid.uuid4().hex[:8]}",
                namespace="project:b", signal="test")
        self.assertEqual(allowed_a.get("result"), "stored", allowed_a)
        self.assertEqual(denied_a.get("error"), "namespace_not_allowed",
                         denied_a)
        self.assertNotIn(_TOKEN_A, json.dumps(denied_a))

    # -- AC2 ----------------------------------------------------------------

    def test_revoked_token_rejected_without_restart(self):
        """AC2: revocation hits the LIVE verifier on map mtime change.

        The server is built exactly once; the map file is rewritten so B
        carries revoked true, the file mtime is bumped, and the verifier
        itself (the same object the SDK middleware consults per request)
        is polled. verify_token returning None is the 401-equivalent
        refusal; sibling token A must keep verifying after the reload.
        """
        server = self._build_with_map(self._write_map())
        verifier = self._verifier(server)
        self.assertIsNotNone(asyncio_run(verifier.verify_token(_TOKEN_A)))
        self.assertIsNotNone(asyncio_run(verifier.verify_token(_TOKEN_B)))

        self._write_map(revoke_b=True)
        # Make the mtime change unambiguous even on coarse-grained clocks.
        st = os.stat(self._map_path)
        os.utime(self._map_path, (st.st_atime, st.st_mtime + 5))

        deadline = time.monotonic() + 5.0
        rejected = asyncio_run(verifier.verify_token(_TOKEN_B))
        while rejected is not None and time.monotonic() < deadline:
            time.sleep(0.05)
            rejected = asyncio_run(verifier.verify_token(_TOKEN_B))
        self.assertIsNone(rejected, "revoked token must stop verifying")
        self.assertIsNotNone(
            asyncio_run(verifier.verify_token(_TOKEN_A)),
            "sibling token keeps working after the reload")

    def test_corrupt_reload_keeps_previous_map(self):
        """Reload robustness (implementation-review round 1, PR finding 1):
        a corrupt map rewrite must keep the PREVIOUS map serving and must
        never raise out of verify_token — the corrupt rewrite only DEFERS
        pending changes until a valid rewrite lands, exactly as the README
        row for ZMEM_MCP_TOKENS_FILE documents.
        """
        server = self._build_with_map(self._write_map())
        verifier = self._verifier(server)
        self.assertIsNotNone(asyncio_run(verifier.verify_token(_TOKEN_A)))

        with open(self._map_path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        st = os.stat(self._map_path)
        os.utime(self._map_path, (st.st_atime, st.st_mtime + 5))
        # Past the 1-second reload window the next verification hits the
        # corrupt file: keep-previous-map, no exception, no process exit.
        time.sleep(1.1)
        try:
            verified = asyncio_run(verifier.verify_token(_TOKEN_A))
        except SystemExit:
            self.fail("corrupt reload raised SystemExit out of verify_token")
        self.assertIsNotNone(verified, "previous map keeps serving")

        # A valid rewrite takes effect again — revocation is deferred, not
        # lost.
        self._write_map(revoke_b=True)
        st = os.stat(self._map_path)
        os.utime(self._map_path, (st.st_atime, st.st_mtime + 5))
        deadline = time.monotonic() + 5.0
        rejected = asyncio_run(verifier.verify_token(_TOKEN_B))
        while rejected is not None and time.monotonic() < deadline:
            time.sleep(0.05)
            rejected = asyncio_run(verifier.verify_token(_TOKEN_B))
        self.assertIsNone(rejected, "valid rewrite applies the revocation")

    # -- AC3 ----------------------------------------------------------------

    def test_write_attribution_is_server_derived(self):
        """AC3: row attribution comes from the presented principal's map
        entry (host dgx-beta, harness zcode); client-supplied host: and
        agent: tags are not trusted.
        """
        server = self._build_with_map(self._write_map())
        verifier = self._verifier(server)
        access_b = self._mint(verifier, _TOKEN_B)

        with _presenting(access_b):
            written = self._call(
                server, "add", type="fact",
                content=f"t02 ac3 attribution row {uuid.uuid4().hex[:8]}",
                namespace="project:b", signal="test",
                tags="host:client-lie,agent:client-lie,benign-client-tag")
        self.assertEqual(written.get("result"), "stored", written)

        row_tags = self._row_tags(written["id"])
        self.assertIn("host:dgx-beta", row_tags, sorted(row_tags))
        self.assertIn("agent:zcode", row_tags, sorted(row_tags))
        self.assertNotIn("host:client-lie", row_tags, sorted(row_tags))
        self.assertNotIn("agent:client-lie", row_tags, sorted(row_tags))

    def test_update_attribution_is_server_derived_and_merges(self):
        """AC3 update leg: the replacement row is re-stamped from the
        PRESENTED principal, merging with the target row's tags — benign
        tags survive re-stamping, stale or client host: tags do not.

        The seed row is written by a DIFFERENT principal (an unscoped
        operator whose own stamp host:dgx-ops is therefore genuinely
        present on the target row), so the inheritance branch exercises
        the strip-and-restamp path a same-principal seed can never reach
        (implementation-review round 1: a verbatim-inherit mutant passed
        when the seed came from the same principal).
        """
        server = self._build_with_map(self._write_map())
        verifier = self._verifier(server)
        access_u = self._mint(verifier, _TOKEN_U)
        access_b = self._mint(verifier, _TOKEN_B)

        # Seed the target row via the UNSCOPED operator principal: its own
        # server-side stamp (host:dgx-ops, agent:hermes) lands on the row,
        # together with the benign topic tag.
        with _presenting(access_u):
            seeded = self._call(
                server, "add", type="fact",
                content=f"t02 ac3 update seed row {uuid.uuid4().hex[:8]}",
                namespace="project:b", signal="test",
                tags="topic:ac3")
        self.assertEqual(seeded.get("result"), "stored", seeded)
        self.assertEqual(
            self._row_tags(seeded["id"]),
            {"topic:ac3", "host:dgx-ops", "agent:hermes"},
            "the seed row carries the OPERATOR principal's stamp")

        # Inheritance branch (the dangerous one): update with tags NOT
        # supplied. The store then inherits the target row's tags verbatim
        # (tags_eff = old tags), so the server must strip the stale
        # operator attribution, stamp the PRESENTED principal's, and keep
        # the benign tag.
        with _presenting(access_b):
            inherited = self._call(
                server, "update", id=seeded["id"],
                content=f"t02 ac3 update inherited tags {uuid.uuid4().hex[:8]}")
        self.assertEqual(inherited.get("result"), "updated", inherited)
        self.assertIn("id", inherited, inherited)
        self.assertNotEqual(inherited["id"], seeded["id"],
                            "update replaces the row with a NEW row")
        merged_tags = self._row_tags(inherited["id"])
        self.assertIn("host:dgx-beta", merged_tags, sorted(merged_tags))
        self.assertIn("agent:zcode", merged_tags, sorted(merged_tags))
        self.assertIn("topic:ac3", merged_tags, sorted(merged_tags))
        self.assertNotIn("host:dgx-ops", merged_tags, sorted(merged_tags))
        self.assertNotIn("agent:hermes", merged_tags, sorted(merged_tags))

        # Client-tags branch: same stripping rule as add — the client's
        # host: tag is not trusted, the client's benign tag survives, and
        # the principal's stamp wins.
        with _presenting(access_b):
            retagged = self._call(
                server, "update", id=inherited["id"],
                content=f"t02 ac3 update client tags {uuid.uuid4().hex[:8]}",
                tags="host:client-lie,benign-2")
        self.assertEqual(retagged.get("result"), "updated", retagged)
        self.assertIn("id", retagged, retagged)
        self.assertNotEqual(retagged["id"], inherited["id"],
                            "update replaces the row with a NEW row")
        client_tags = self._row_tags(retagged["id"])
        self.assertIn("host:dgx-beta", client_tags, sorted(client_tags))
        self.assertIn("agent:zcode", client_tags, sorted(client_tags))
        self.assertIn("benign-2", client_tags, sorted(client_tags))
        self.assertNotIn("host:client-lie", client_tags, sorted(client_tags))


if __name__ == "__main__":
    unittest.main(verbosity=2)
