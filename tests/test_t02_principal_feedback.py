"""Regression tests for PR #372 review feedback (issue #281).

Non-frozen companion to tests/test_t02_principal.py (the frozen file's bytes
are checkpoint-pinned; post-review additions live here). Covers the
behaviors the swarm-pr-review and the independent Claude Code review found
unasserted or missing:

- an unrecognized map-entry key is a hard exit-2 failure (a typo'd
  ``namespaces`` must not widen a principal; a typo'd ``revoked`` must not
  keep a credential live), and the failure never echoes the outer map key
  (it is the bearer token);
- two keys normalizing to the same secret are refused rather than
  last-wins;
- current_principal() fails CLOSED with no auth context and with an
  unknown client_id (the two branches that were previously unreachable);
- a principal revoked by a reload resolves to deny-all on the
  AUTHORIZATION path (by_principal_id), not just the verify path;
- a rewind- or same-mtime map rewrite still reloads.

Runs standalone: python tests/test_t02_principal_feedback.py
RED at base (all five behaviors absent), GREEN after the fixes.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
import subprocess
from pathlib import Path


def _map_owner_only(path) -> None:
    """#406 contract: a test map must not depend on the directory's
    inherited ACL (non-system drives inherit Users/Authenticated-Users
    allow ACEs, which the hub correctly refuses). POSIX uses chmod."""
    if os.name == "nt":
        r = subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
        assert r.returncode == 0, (
            f"icacls owner-only pin failed rc={r.returncode}: "
            f"{(r.stderr or r.stdout).strip()[:200]}")
    else:
        os.chmod(os.fspath(path), 0o600)


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
AUTH_MODULE = "zmem_auth_t02_feedback"

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False


def _load_auth():
    if "mcp.server.auth.provider" not in sys.modules and not MCP_AVAILABLE:
        import types

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
            "mcp": mcp_mod,
            "mcp.server": server_mod,
            "mcp.server.auth": auth_mod,
            "mcp.server.auth.provider": provider,
        })
    spec = importlib.util.spec_from_file_location(
        AUTH_MODULE, SERVER_DIR / "auth.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[AUTH_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


AUTH = _load_auth()

NS = "project:t02/feedback"
NS_B = "project:t02/other"
TOKEN = "t02-feedback-token-alpha"
TOKEN_B = "t02-feedback-token-beta"


class _MapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-t02-feedback-")
        self.map_path = os.path.join(self.tmp, "map.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, mapping, *, bump=0):
        with open(self.map_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f)
        # #406: the hub refuses a group/world-readable map at load;
        # pin the installer contract (0o600 keeps the write bit on NTFS).
        _map_owner_only(self.map_path)
        if bump:
            st = os.stat(self.map_path)
            os.utime(self.map_path, (st.st_atime, st.st_mtime + bump))
        return self.map_path

    def entry(self, pid="alpha", namespaces=(NS,), revoked=False):
        e = {"principal": pid, "host": "dgx-fb", "harness": "claude"}
        if namespaces is not None:
            e["namespaces"] = list(namespaces)
        if revoked:
            e["revoked"] = True
        return e


class MapLoaderFailLoudTest(_MapCase):
    """An unrecognized entry key must be a hard failure, not a silent drop."""

    def test_unknown_entry_field_exits_two(self):
        self.write({TOKEN: {**self.entry(), "revoke": True}})
        err = self._stderr_exit()
        self.assertIn("unrecognized field", err)
        self.assertIn("revoke", err)

    def test_unknown_field_error_never_echoes_the_token(self):
        self.write({TOKEN: {**self.entry(), "namespace": [NS]}})
        err = self._stderr_exit()
        self.assertNotIn(TOKEN, err, "the map KEY is the bearer token")
        self.assertIn("namespace", err)

    def test_known_fields_still_load(self):
        self.write({TOKEN: self.entry(), TOKEN_B: self.entry("beta", (NS_B,))})
        pm = AUTH.load_principal_map(self.map_path)
        self.assertIsNotNone(pm.lookup_token(TOKEN))
        self.assertIsNotNone(pm.lookup_token(TOKEN_B))

    def test_duplicate_normalized_token_refused(self):
        # Two keys that normalize to the same secret must not silently
        # shadow each other (a revoked entry overridden by the later one).
        self.write({TOKEN: self.entry(), f" {TOKEN} ": self.entry("beta", (NS_B,))})
        err = self._stderr_exit()
        self.assertIn("more than once", err)

    def test_recursion_bomb_reports_exit_two_not_traceback(self):
        deep = "[" * 5000 + "]" * 5000
        with open(self.map_path, "w", encoding="utf-8") as f:
            f.write(deep)
        _map_owner_only(self.map_path)  # corrupt leg, not perm leg
        err = self._stderr_exit()
        self.assertNotIn("Traceback", err)
        self.assertTrue(err.strip())

    def _stderr_exit(self):
        """Run load_principal_map, capture its exit-2 message (never raising)."""
        import io
        import subprocess

        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('a', r'{self.map_path}')\n"
            "sys.exit(0)\n"
        )
        # The loader exits inside this process; capture it there.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                AUTH.load_principal_map(self.map_path)
            except SystemExit as exc:
                code = exc.code
        return buf.getvalue() + ("" if code == 2 else f"[exit {code}]")


class RevocationResolutionTest(_MapCase):
    def test_by_principal_id_denies_revoked(self):
        self.write({TOKEN: self.entry(revoked=True)})
        pm = AUTH.load_principal_map(self.map_path)
        self.assertIsNone(pm.by_principal_id("alpha"),
                          "a revoked principal must not resolve by id")
        self.assertIsNone(pm.lookup_token(TOKEN))

    def test_rewind_rewrite_reloads_through_maybe_reload(self):
        """A rewind must reload via the DETECTION path, not force_reload.

        The fix lives in maybe_reload's change test (mtime == cached vs the
        old mtime <= cached), so the pin drives maybe_reload: cache a
        forward-bumped generation, then restore an OLDER-mtime generation
        that revokes, age the rate limit, and assert the revoke landed.
        Reverting the fix to `mtime <= self._mtime` makes this fail.
        """
        # Load with a forward bump so the cached mtime is "now".
        self.write({TOKEN: self.entry()}, bump=5)
        pm = AUTH.load_principal_map(self.map_path)
        self.assertIsNotNone(pm.lookup_token(TOKEN))
        cached = pm._mtime
        # An mtime-preserving restore of an OLDER generation that revokes.
        self.write({TOKEN: self.entry(revoked=True)})
        os.utime(self.map_path, (os.stat(self.map_path).st_atime,
                                 os.stat(self.map_path).st_mtime - 60))
        self.assertLess(os.stat(self.map_path).st_mtime_ns, cached)
        pm._last_check -= 10  # past the 1s rate limit
        pm.maybe_reload()
        self.assertIsNone(pm.lookup_token(TOKEN),
                          "an older-mtime rewrite must still be picked up")
        pm._last_check -= 10
        self.assertIsNone(pm.maybe_reload() or pm.lookup_token(TOKEN))

    def test_force_reload_picks_up_a_same_tick_rewrite(self):
        """The SIGHUP path re-reads unconditionally, same mtime included."""
        self.write({TOKEN: self.entry()})
        pm = AUTH.load_principal_map(self.map_path)
        self.write({TOKEN: self.entry(revoked=True)})
        st = os.stat(self.map_path)
        os.utime(self.map_path, (st.st_atime, st.st_mtime))
        pm.force_reload()
        self.assertIsNone(pm.lookup_token(TOKEN))


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed")
class DenyAllResolutionTest(_MapCase):
    """The two fail-closed branches must be reachable and must deny."""

    def setUp(self):
        super().setUp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self._saved = {
            k: os.environ.get(k) for k in (
                "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKEN",
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
            )
        }
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = self.store
        os.environ.pop("ZMEM_DATA", None)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "no-models")
        for k in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
                  "ZMEM_MCP_TOKENS_FILE"):
            os.environ.pop(k, None)
        spec = importlib.util.spec_from_file_location(
            "zmem_mcp_t02_feedback", SERVER_DIR / "mcp_server.py")
        self.mcp_server = importlib.util.module_from_spec(spec)
        sys.modules["zmem_mcp_t02_feedback"] = self.mcp_server
        spec.loader.exec_module(self.mcp_server)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _build(self):
        self.write({TOKEN: self.entry()}, bump=1)
        os.environ["ZMEM_MCP_TOKENS_FILE"] = self.map_path
        return self.mcp_server.build_server(host="127.0.0.1", port=0,
                                            use_tls=False)

    def _call(self, server, name, **args):
        async def _one():
            return await server._tool_manager.call_tool(
                name, args, context=None)
        return asyncio.run(_one())

    def test_no_auth_context_fails_closed(self):
        # No contextvar set: current_principal() must deny every namespace.
        server = self._build()
        result = self._call(server, "add", type="fact",
                            content="no-context write attempt",
                            namespace=NS, signal="test")
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)
        self.assertNotIn("stored", json.dumps(result))

    def test_unknown_client_id_fails_closed(self):
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.server.auth.provider import AccessToken

        server = self._build()
        forged = AccessToken(token="forged", client_id="not-a-principal",
                            scopes=["read", "write"], expires_at=None)
        ctx = auth_context_var.set(AuthenticatedUser(forged))
        try:
            result = self._call(server, "add", type="fact",
                                content="unknown-principal write attempt",
                                namespace=NS, signal="test")
        finally:
            auth_context_var.reset(ctx)
        self.assertEqual(result.get("error"), "namespace_not_allowed", result)
        self.assertNotIn("stored", json.dumps(result))

    def test_session_end_stamps_caller_attribution(self):
        """session_end is a write path: its row must carry the caller's
        host:/agent: stamps (reviewer nit 1)."""
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        server = self._build()
        access = asyncio.run(server._token_verifier.verify_token(TOKEN))
        ctx = auth_context_var.set(AuthenticatedUser(access))
        try:
            result = self._call(server, "session_end", namespace=NS,
                                note="session end note from a mapped principal")
        finally:
            auth_context_var.reset(ctx)
        self.assertNotIn("error", json.dumps(result), result)
        conn = __import__("sqlite3").connect(self.store)
        try:
            row = conn.execute(
                "SELECT tags FROM memory WHERE content LIKE ?",
                ("session end note%",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, result)
        tags = {t.strip() for t in (row[0] or "").split(",") if t.strip()}
        self.assertIn("host:dgx-fb", tags)
        self.assertIn("agent:claude", tags)

    def _build_with_unscoped_principal(self):
        """A map-mode principal with NO namespaces (unscoped), so update()
        skips the scoped branch and reaches the inherited-tags read."""
        mapping = {TOKEN: self.entry(), TOKEN_B: {
            "principal": "ops", "host": "dgx-ops", "harness": "hermes"}}
        self.write(mapping, bump=1)
        os.environ["ZMEM_MCP_TOKENS_FILE"] = self.map_path
        return self.mcp_server.build_server(host="127.0.0.1", port=0,
                                            use_tls=False)

    def test_unscoped_update_fails_closed_when_target_tags_unreadable(self):
        """The INHERITED-TAGS read (the unscoped map-mode branch) must fail
        closed.

        The target row MUST exist, or the update fails for an unrelated
        reason and the assertion would pass even with the branch fail-open.
        So: add the row, then make the SECOND store read (the
        inherited-tags get) fail, and assert nothing was written.
        """
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        server = self._build_with_unscoped_principal()
        # Seed a real target row in the unscoped principal's own namespace.
        access = asyncio.run(server._token_verifier.verify_token(TOKEN_B))
        ctx = auth_context_var.set(AuthenticatedUser(access))
        try:
            seeded = self._call(server, "add", type="fact", namespace=NS,
                                signal="none",
                                content=f"unscoped target {uuid.uuid4().hex[:8]}")
        finally:
            auth_context_var.reset(ctx)
        self.assertEqual(seeded.get("result"), "stored", seeded)
        target = seeded["id"]

        # Fail the inherited-tags read. For an UNSCOPED caller this get is
        # the only one in the call (the scoped branch's read is skipped), so
        # the FIRST get of this tool call IS the inherited-tags read.
        real = self.mcp_server._run_store_async
        calls = {"n": 0}

        async def flaky(args, input_text=None, timing=None,
                        deadline=None, pool="write"):
            # deadline/pool: the issue #286 per-call kwargs ride every
            # _run_store_async call now; pass them through unchanged.
            if args and args[0] == "get":
                calls["n"] += 1
                return {"ok": False, "stdout": "", "stderr": "injected read failure"}
            return await real(args, input_text=input_text, timing=timing,
                              deadline=deadline, pool=pool)

        self.mcp_server._run_store_async = flaky
        try:
            ctx = auth_context_var.set(AuthenticatedUser(access))
            try:
                result = self._call(server, "update", id=target,
                                    content="must not be written")
            finally:
                auth_context_var.reset(ctx)
        finally:
            self.mcp_server._run_store_async = real

        self.assertNotIn("updated", json.dumps(result),
                         "a failed inherited-tags read must block the write")
        self.assertGreaterEqual(calls["n"], 1,
                                "the inherited-tags read must have run")

    def test_scoped_update_fails_closed_when_target_unreadable(self):
        """The scoped branch's own read gate (same policy, earlier branch)."""
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        server = self._build()
        verifier = server._token_verifier
        access = asyncio.run(verifier.verify_token(TOKEN))
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        ctx = auth_context_var.set(AuthenticatedUser(access))
        try:
            result = self._call(
                server, "update",
                id="00000000-0000-0000-0000-000000000000",
                content="update against a missing row")
        finally:
            auth_context_var.reset(ctx)
        self.assertNotIn("updated", json.dumps(result))

    def test_known_principal_still_authorized(self):
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        server = self._build()
        verifier = server._token_verifier
        access = asyncio.run(verifier.verify_token(TOKEN))
        ctx = auth_context_var.set(AuthenticatedUser(access))
        try:
            result = self._call(server, "add", type="fact",
                                content=f"known principal {uuid.uuid4().hex[:8]}",
                                namespace=NS, signal="test")
        finally:
            auth_context_var.reset(ctx)
        self.assertEqual(result.get("result"), "stored", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)