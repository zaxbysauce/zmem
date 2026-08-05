"""SDK-native Bearer-token auth for the zmem MCP server.

Uses the ``mcp`` SDK's built-in ``TokenVerifier`` Protocol rather than custom
Starlette middleware. The SDK wires ``BearerAuthBackend`` +
``RequireAuthMiddleware`` automatically when ``FastMCP(auth=..., token_verifier=...)``
is constructed (verified in mcp/server/fastmcp/server.py:32-34, 217-224), and
this path stays correct as the SDK adds new protocol probe routes
(``.well-known/oauth-protected-resource`` etc.) that custom middleware would
silently miss.

The token is a single shared secret read from ``ZMEM_MCP_TOKEN`` (env or a file
path in ``ZMEM_MCP_TOKEN_FILE``). Comparison is constant-time
(``hmac.compare_digest``). This is intentionally simple static-token auth —
not OAuth — which matches zmem's local-first, single-operator model.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier


def load_expected_token() -> str:
    """Load the expected Bearer token from env or token file.

    Env ``ZMEM_MCP_TOKEN`` wins; otherwise ``ZMEM_MCP_TOKEN_FILE`` is read.
    Aborts startup (exit 2) if neither is set — auth is mandatory for a
    network-exposed store, never optional.
    """
    tok = os.environ.get("ZMEM_MCP_TOKEN", "").strip()
    if tok:
        return tok
    tok_file = os.environ.get("ZMEM_MCP_TOKEN_FILE", "").strip()
    if tok_file:
        p = Path(tok_file).expanduser()
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            sys.stderr.write(
                f"zmem-mcp: ZMEM_MCP_TOKEN_FILE ({tok_file}) unreadable: {exc}\n"
            )
            sys.exit(2)
    sys.stderr.write(
        "zmem-mcp: no token configured. Set ZMEM_MCP_TOKEN (or "
        "ZMEM_MCP_TOKEN_FILE) before starting the server — auth is mandatory "
        "for a network-exposed store.\n"
    )
    sys.exit(2)


class StaticTokenVerifier(TokenVerifier):
    """Constant-time Bearer-token check against a single expected secret.

    Implements the SDK's ``TokenVerifier`` Protocol (one method,
    ``verify_token``). Returns an ``AccessToken`` on match, ``None`` on
    mismatch — the SDK's middleware turns None into a 401.
    """

    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token

    async def verify_token(self, token: str) -> Optional[AccessToken]:  # noqa: D401
        if not isinstance(token, str) or not token:
            return None
        # Constant-time comparison to resist timing probes.
        if hmac.compare_digest(token, self._expected):
            return AccessToken(
                token=token,
                client_id="zmem-operator",
                scopes=["read", "write"],
                expires_at=None,
            )
        return None
