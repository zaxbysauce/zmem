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

v13 (issue #65, 10.2): a token may carry an OPTIONAL namespace allow-list.
- ``ZMEM_MCP_TOKEN`` (env) is always an UNSCOPED operator token — full
  access, exactly the pre-v13 behavior.
- ``ZMEM_MCP_TOKEN_FILE`` accepts two formats:
  * a bare text file whose first non-whitespace character is NOT ``{`` —
    the whole (stripped) content is the token, UNSCOPED (pre-v13 behavior);
  * a JSON object file starting with ``{``: ``{"token": "...", "namespaces":
    ["project:a", "user:global"]}`` — a SCOPED token. ``namespaces`` absent
    or null means unscoped; when present it MUST be a non-empty list of
    valid namespace shapes (``project:*``, ``user:*``, or the canonical
    ``user:global`` — near-miss globals like ``global`` are refused). A file
    that STARTS with ``{`` but does not parse as such a JSON object is a
    hard startup error (exit 2) — never a silent fallback to bare-token
    mode, which would silently un-scope a scoped deployment.
Requests outside the allow-list fail closed with the stable error token
``namespace_not_allowed`` (see NamespaceDenied). No token rotation, CORS,
or rate limits — out of scope by design (issue #65).
"""

from __future__ import annotations

import hmac
import json as _json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier

# Stable error token for scope denials (issue #65, 10.2). A stable machine-
# readable constant, not a human message, so clients can branch on it.
NAMESPACE_NOT_ALLOWED = "namespace_not_allowed"

# Namespace shape accepted in a token allow-list: the canonical global, or a
# project:/user: prefixed namespace. Deliberately mirrors the CLI's
# validation (storelib.write._validate_namespace) WITHOUT importing storelib —
# auth.py runs in the server process and must stay dependency-free.
_NS_SHAPE_RE = re.compile(r"^(user:global|project:[^\s:][^:]*|user:[^\s:][^:]*)$")
# Near-miss forms the store itself rejects (storelib.write near-miss stems):
# a token scoped to "global" would silently not match any real namespace.
_NS_NEAR_MISS_RE = re.compile(
    r"^(global|userglobal|users:global|user\.global|global:user|user-global)$",
    re.IGNORECASE,
)


class NamespaceDenied(Exception):
    """A scoped token was used for a namespace outside its allow-list."""


@dataclass
class TokenConfig:
    """The single configured token + its optional namespace allow-list.

    ``namespaces`` is None for an UNSCOPED operator token (full access — the
    only pre-v13 mode) or a frozenset of namespace strings for a scoped one.
    """

    token: str
    namespaces: Optional[frozenset] = None
    source: str = "ZMEM_MCP_TOKEN"

    @property
    def scoped(self) -> bool:
        return self.namespaces is not None

    def check_namespace(self, namespace: Optional[str]) -> None:
        """Fail closed unless ``namespace`` is inside the allow-list.

        ``namespace=None`` means "no explicit namespace" — for a scoped token
        that is OUTSIDE the list (a read without --namespace spans every
        namespace; a write defaults to user:global): neither can be proven
        in-scope, so both are denied. Unscoped tokens allow everything.
        Raises NamespaceDenied; never returns a reason containing the token.
        """
        if self.namespaces is None:
            return
        ns = (namespace or "").strip()
        if ns in self.namespaces:
            return
        raise NamespaceDenied(
            f"{NAMESPACE_NOT_ALLOWED}: this token is scoped to "
            f"{len(self.namespaces)} namespace(s); pass an allowed "
            "namespace explicitly"
        )


def _valid_scope_namespace(ns: object) -> bool:
    if not isinstance(ns, str):
        return False
    v = ns.strip()
    if not v or v == "*":
        return False  # a scope of '*' is the unscoped case — not expressible
    if _NS_NEAR_MISS_RE.match(v):
        return False
    # F15: reject C0 control chars and DEL — Python's \s does not cover
    # all of them, and they cannot survive a subprocess argv safely.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        return False
    return bool(_NS_SHAPE_RE.match(v))


def _parse_token_file(raw: str, source: str) -> TokenConfig:
    """Parse ZMEM_MCP_TOKEN_FILE content per the pinned sniff rule (10.2)."""
    if not raw.lstrip().startswith("{"):
        tok = raw.strip()
        if not tok:
            _fail_config(f"{source} is empty")
        return TokenConfig(token=tok, namespaces=None, source=source)
    try:
        obj = _json.loads(raw)
    except ValueError as exc:
        _fail_config(
            f"{source} starts with '{{' but is not valid JSON ({exc}); fix the "
            "file or remove the leading '{' if it is meant to be a bare token"
        )
    if not isinstance(obj, dict):
        _fail_config(f"{source} must be a JSON object with a 'token' field")
    tok = obj.get("token")
    if not isinstance(tok, str) or not tok.strip():
        _fail_config(f"{source} JSON object must carry a non-empty string 'token'")
    tok = tok.strip()
    scopes = obj.get("namespaces")
    if scopes is None:
        return TokenConfig(token=tok, namespaces=None, source=source)
    if not isinstance(scopes, list) or not scopes:
        _fail_config(
            f"{source} 'namespaces' must be a NON-EMPTY list of namespace "
            "strings (omit the key entirely for an unscoped operator token)"
        )
    cleaned: list[str] = []
    for ns in scopes:
        if not _valid_scope_namespace(ns):
            _fail_config(
                f"{source} 'namespaces' entry {ns!r} is not a valid namespace "
                "shape (expected project:<name>, user:<name>, or user:global)"
            )
        cleaned.append(str(ns).strip())
    return TokenConfig(
        token=tok, namespaces=frozenset(cleaned), source=source
    )


def _fail_config(message: str) -> None:
    sys.stderr.write(f"zmem-mcp: {message}\n")
    sys.exit(2)


def load_token_config() -> TokenConfig:
    """Load the token (and optional namespace scope) from env or token file.

    Env ``ZMEM_MCP_TOKEN`` wins (always bare/unscoped); otherwise
    ``ZMEM_MCP_TOKEN_FILE`` is parsed per the sniff rule in the module
    docstring. Aborts startup (exit 2) on missing/empty/malformed config —
    an unusable auth config must be loud, not silent.
    """
    tok = os.environ.get("ZMEM_MCP_TOKEN", "").strip()
    if tok:
        return TokenConfig(token=tok, namespaces=None, source="ZMEM_MCP_TOKEN")
    tok_file = os.environ.get("ZMEM_MCP_TOKEN_FILE", "").strip()
    if tok_file:
        p = Path(tok_file).expanduser()
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            _fail_config(f"ZMEM_MCP_TOKEN_FILE ({tok_file}) unreadable: {exc}")
        return _parse_token_file(raw, f"ZMEM_MCP_TOKEN_FILE ({tok_file})")
    sys.stderr.write(
        "zmem-mcp: no token configured. Set ZMEM_MCP_TOKEN (or "
        "ZMEM_MCP_TOKEN_FILE) before starting the server — auth is "
        "mandatory for a network-exposed store.\n"
    )
    sys.exit(2)


def load_expected_token() -> str:
    """Load the expected Bearer token from env or token file.

    Backward-compatible wrapper over :func:`load_token_config` — returns just
    the secret. Aborts startup (exit 2) if neither is set OR the resolved
    value is empty — an empty token would let the server boot but reject
    every request (every Bearer comparison against ``""`` is False), leaving
    the operator with a silently-unusable server. Auth is mandatory for a
    network-exposed store.
    """
    return load_token_config().token


class StaticTokenVerifier(TokenVerifier):
    """Constant-time Bearer-token check against a single expected secret.

    Implements the SDK's ``TokenVerifier`` Protocol (one method,
    ``verify_token``). Returns an ``AccessToken`` on match, ``None`` on
    mismatch — the SDK's middleware turns None into a 401. Namespace
    scoping is NOT enforced here: verification happens before the tool call
    exists, so scope checks live at the tool layer closing over the one
    configured TokenConfig (mcp_server._guard_namespace).
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
