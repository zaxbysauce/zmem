"""Finding 4 of issue #35: runtime tests for the MCP server's security
boundary modules — ``auth.py`` (Bearer token verification) and
``bind_guard.py`` (wildcard/TLS refusal).

These two modules are the security controls for the network-exposed MCP server
and previously had ZERO test coverage (confirmed: no test in tests/ imported
from hermes-plugin/server/). Token verification, the missing-token exit(2)
path, wildcard-bind refusal, ``_is_wildcard``, ``detect_lan_ip``, and
``resolve_tls_kwargs`` both-or-neither refusal can now regress visibly.

CI constraint (issue #35): CI runs ``python tests/test_*.py`` with stdlib only
(no ``pip install``; see .github/workflows/ci.yml). ``bind_guard`` is pure
stdlib, so its tests run UNCONDITIONALLY. ``auth`` imports
``from mcp.server.auth.provider import ...`` — that import fails in stdlib CI,
so the auth tests are skip-guarded on the presence of the ``mcp`` package
(the tests still run wherever ``mcp`` is installed: the deployment env, dev
machines, and any future CI that installs deps).

Run: python tests/test_auth_bind_guard.py
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
sys.path.insert(0, str(SERVER_DIR))

# bind_guard is pure stdlib — always importable.
from bind_guard import (  # noqa: E402
    _is_wildcard,
    detect_lan_ip,
    enforce_bind_host,
    resolve_tls_kwargs,
)

# auth requires the mcp package; skip-guard its tests so stdlib CI stays green.
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    from auth import StaticTokenVerifier, load_expected_token  # noqa: E402


# ===========================================================================
# bind_guard — pure stdlib, runs unconditionally in CI
# ===========================================================================
class IsWildcardTest(unittest.TestCase):
    """_is_wildcard classifies which bind hosts are unspecified (wildcard)."""

    def test_explicit_wildcards_are_true(self):
        for host in ("0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0"):
            with self.subTest(host=host):
                self.assertTrue(_is_wildcard(host), f"{host!r} should be wildcard")

    def test_empty_string_is_wildcard(self):
        # "" hits the explicit set at line 34 — important: enforce_bind_host("")
        # must be treated as a wildcard (so it exits 2 unless opted in).
        self.assertTrue(_is_wildcard(""))

    def test_inet_aton_shorthands_are_wildcard(self):
        # socket.inet_aton accepts "0" and "0.0" as 0.0.0.0; uvicorn/socket accept
        # these binds, so the guard must catch them (ipaddress rejects them).
        for host in ("0", "0.0"):
            with self.subTest(host=host):
                self.assertTrue(_is_wildcard(host), f"{host!r} should be wildcard")

    def test_whitespace_and_case_normalized(self):
        self.assertTrue(_is_wildcard("  0.0.0.0  "))
        self.assertTrue(_is_wildcard("0.0.0.0".upper()))  # digits, but covers the lower() path

    def test_specific_addresses_are_not_wildcard(self):
        for host in ("127.0.0.1", "::1", "192.168.1.1", "10.0.0.5", "fe80::1"):
            with self.subTest(host=host):
                self.assertFalse(_is_wildcard(host), f"{host!r} must NOT be wildcard")

    def test_hostnames_are_not_wildcard(self):
        # Hostnames can't be statically classified; the operator named them, so
        # they pass (the guard does not block them).
        for host in ("localhost", "example.com", "my-box"):
            with self.subTest(host=host):
                self.assertFalse(_is_wildcard(host), f"{host!r} must NOT be wildcard")

    def test_star_literal_is_not_wildcard(self):
        # "*" is NOT treated as a wildcard: it fails ipaddress and inet_aton and
        # falls through to return False. This is intentional (it's not a real
        # bind address uvicorn accepts), but it's a subtle edge worth pinning.
        self.assertFalse(_is_wildcard("*"))


class DetectLanIpTest(unittest.TestCase):
    def test_success_returns_getsockname_address(self):
        with mock.patch("bind_guard.socket.socket") as ms:
            ms.return_value.getsockname.return_value = ("192.168.1.42", 0)
            self.assertEqual(detect_lan_ip(), "192.168.1.42")
            ms.return_value.close.assert_called_once()

    def test_oserror_falls_back_to_loopback(self):
        with mock.patch("bind_guard.socket.socket") as ms:
            ms.return_value.connect.side_effect = OSError("no route")
            self.assertEqual(detect_lan_ip(), "127.0.0.1")
            # socket still closed even on failure
            ms.return_value.close.assert_called_once()

    def test_socket_closed_even_on_unexpected_exception(self):
        """The finally block must close the socket even when a NON-OSError
        propagates out of the try body. detect_lan_ip only catches OSError, so
        a different exception (e.g. RuntimeError from a mocked socket) must
        still run finally -> close before re-raising. This exercises the
        finally-on-non-OSError path directly (a no-raise call would just
        re-exercise the success path and give false coverage)."""
        with mock.patch("bind_guard.socket.socket") as ms:
            ms.return_value.connect.side_effect = RuntimeError("unexpected boom")
            with self.assertRaises(RuntimeError):
                detect_lan_ip()
            # finally still closed the socket despite the propagating exception
            ms.return_value.close.assert_called_once()


class EnforceBindHostTest(unittest.TestCase):
    def test_wildcard_without_opt_in_exits_2(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_MCP_ALLOW_INSECURE_BIND", None)
            with self.assertRaises(SystemExit) as cm:
                enforce_bind_host("0.0.0.0")
            self.assertEqual(cm.exception.code, 2)

    def test_wildcard_with_opt_in_returns_host(self):
        with mock.patch.dict(os.environ, {"ZMEM_MCP_ALLOW_INSECURE_BIND": "1"}, clear=False):
            self.assertEqual(enforce_bind_host("0.0.0.0"), "0.0.0.0")
            self.assertEqual(enforce_bind_host("::"), "::")

    def test_non_wildcard_returns_host_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_MCP_ALLOW_INSECURE_BIND", None)
            self.assertEqual(enforce_bind_host("192.168.1.10"), "192.168.1.10")

    def test_empty_host_without_opt_in_exits_2(self):
        # "" is a wildcard (see _is_wildcard), so empty host without opt-in must
        # refuse — it does NOT silently fall through to detect_lan_ip().
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_MCP_ALLOW_INSECURE_BIND", None)
            with self.assertRaises(SystemExit) as cm:
                enforce_bind_host("")
            self.assertEqual(cm.exception.code, 2)

    def test_empty_host_with_opt_in_returns_detected_lan_ip(self):
        # With opt-in, the wildcard check is bypassed and "" is falsy, so it
        # returns detect_lan_ip().
        with mock.patch.dict(os.environ, {"ZMEM_MCP_ALLOW_INSECURE_BIND": "1"}, clear=False):
            with mock.patch("bind_guard.detect_lan_ip", return_value="172.16.0.3"):
                self.assertEqual(enforce_bind_host(""), "172.16.0.3")

    def test_refusal_message_written_to_stderr(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_MCP_ALLOW_INSECURE_BIND", None)
            with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
                with self.assertRaises(SystemExit):
                    enforce_bind_host("0.0.0.0")
            self.assertIn("refusing to bind", fake_err.getvalue())


class ResolveTlsKwargsTest(unittest.TestCase):
    def test_both_returns_kwargs(self):
        kwargs = resolve_tls_kwargs("/path/key.pem", "/path/cert.pem")
        self.assertEqual(kwargs, {"ssl_keyfile": "/path/key.pem",
                                  "ssl_certfile": "/path/cert.pem"})

    def test_neither_returns_empty(self):
        self.assertEqual(resolve_tls_kwargs(None, None), {})

    def test_both_empty_strings_returns_empty(self):
        self.assertEqual(resolve_tls_kwargs("", ""), {})

    def test_key_only_exits_2(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            with self.assertRaises(SystemExit) as cm:
                resolve_tls_kwargs("/path/key.pem", None)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("together", fake_err.getvalue())

    def test_cert_only_exits_2(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            with self.assertRaises(SystemExit) as cm:
                resolve_tls_kwargs(None, "/path/cert.pem")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("together", fake_err.getvalue())

    def test_one_empty_one_set_exits_2(self):
        # "" is falsy, so ("", "/cert") looks like cert-only -> exit 2.
        with self.assertRaises(SystemExit):
            resolve_tls_kwargs("", "/path/cert.pem")


# ===========================================================================
# auth — requires the mcp package; skip-guarded so stdlib CI stays green.
# These run wherever mcp is installed (deployment env, dev machines).
# ===========================================================================
@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed (auth tests need it)")
class LoadExpectedTokenTest(unittest.TestCase):
    """load_expected_token reads ZMEM_MCP_TOKEN (or _FILE) and exits 2 when no
    usable token is resolvable. Auth is mandatory for a network-exposed store."""

    def test_env_token_returned_stripped(self):
        with mock.patch.dict(os.environ, {"ZMEM_MCP_TOKEN": "  my-secret  "},
                             clear=False):
            self.assertEqual(load_expected_token(), "my-secret")

    def test_file_fallback_when_env_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as f:
            f.write("  file-token\n")
            path = f.name
        try:
            env = {"ZMEM_MCP_TOKEN": "", "ZMEM_MCP_TOKEN_FILE": path}
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertEqual(load_expected_token(), "file-token")
        finally:
            os.unlink(path)

    def test_unreadable_file_exits_2(self):
        env = {"ZMEM_MCP_TOKEN": "", "ZMEM_MCP_TOKEN_FILE": "/no/such/file.xyz"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as cm:
                    load_expected_token()
        self.assertEqual(cm.exception.code, 2)

    def test_both_missing_exits_2(self):
        env = {"ZMEM_MCP_TOKEN": "", "ZMEM_MCP_TOKEN_FILE": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as cm:
                    load_expected_token()
        self.assertEqual(cm.exception.code, 2)

    def test_whitespace_only_resolved_token_exits_2(self):
        # "   ".strip() == "" → treated as empty → exit 2 (an empty token would
        # reject every request, leaving a silently-unusable server).
        env = {"ZMEM_MCP_TOKEN": "   ", "ZMEM_MCP_TOKEN_FILE": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SystemExit) as cm:
                load_expected_token()
        self.assertEqual(cm.exception.code, 2)


@unittest.skipUnless(MCP_AVAILABLE, "mcp package not installed (auth tests need it)")
class StaticTokenVerifierTest(unittest.TestCase):
    """Constant-time Bearer-token check. Match → AccessToken; else None."""

    def test_matching_token_returns_access_token(self):
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        result = asyncio.run(v.verify_token("correct-secret"))
        self.assertIsNotNone(result)
        self.assertEqual(result.client_id, "zmem-operator")
        self.assertEqual(result.scopes, ["read", "write"])
        self.assertIsNone(result.expires_at)

    def test_mismatching_token_returns_none(self):
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        self.assertIsNone(asyncio.run(v.verify_token("wrong-secret")))

    def test_empty_string_returns_none(self):
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        self.assertIsNone(asyncio.run(v.verify_token("")))

    def test_none_returns_none(self):
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        self.assertIsNone(asyncio.run(v.verify_token(None)))  # type: ignore[arg-type]

    def test_non_string_returns_none(self):
        # The isinstance(token, str) guard means a non-str (e.g. int) is None.
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        self.assertIsNone(asyncio.run(v.verify_token(12345)))  # type: ignore[arg-type]

    def test_trailing_space_does_not_match(self):
        # hmac.compare_digest is exact; a trailing space must not match.
        v = StaticTokenVerifier("correct-secret")
        import asyncio
        self.assertIsNone(asyncio.run(v.verify_token("correct-secret ")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
