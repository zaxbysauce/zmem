"""Bind-address guard for the zmem MCP server.

Security control (not just docs): refuse to bind to a wildcard address
(``0.0.0.0`` / ``::``) unless the operator has explicitly opted in via
``ZMEM_MCP_ALLOW_INSECURE_BIND=1``. A Bearer-protected store broadcasting on
the whole network is a real exposure — the token is the only authentication,
and on a multi-tenant or public network an attacker who guesses or leaks it
gains read/write access to the user's entire memory store.

Also resolves a sensible default bind host (the box's primary LAN IP) so the
server is reachable from a remote Hermes on the LAN without the operator
having to look up the IP manually.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Optional


_INSECURE_HOSTS = {"0.0.0.0", "::", "[::]"}


def detect_lan_ip() -> str:
    """Best-effort: the box's primary LAN-facing IPv4 address.

    Opens a UDP socket "to" a public IP (no packets sent) and reads the bound
    local address. Falls back to ``127.0.0.1`` if detection fails. This is a
    common trick and is safe — connect() on a UDP socket only sets up the
    routing, it doesn't transmit.
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def enforce_bind_host(host: str) -> str:
    """Return ``host`` if it's safe; refuse to start if it's an insecure wildcard.

    A wildcard bind is allowed only when ``ZMEM_MCP_ALLOW_INSECURE_BIND=1`` is
    set, signaling the operator understands the exposure (e.g. the box is
    behind a reverse proxy or firewall, or the network is fully trusted and
    air-gapped). Absent that opt-in, wildcards abort startup.
    """
    h = (host or "").strip().lower()
    if h in _INSECURE_HOSTS:
        if os.environ.get("ZMEM_MCP_ALLOW_INSECURE_BIND", "").strip() != "1":
            sys.stderr.write(
                "zmem-mcp: refusing to bind to wildcard address '%s'. The store "
                "holds the user's full memory history and the Bearer token is "
                "the only authentication. Bind to a LAN IP instead (the default "
                "auto-detects one), or set ZMEM_MCP_ALLOW_INSECURE_BIND=1 to "
                "acknowledge the exposure.\n" % host
            )
            sys.exit(2)
    return host if host else detect_lan_ip()


def resolve_tls_kwargs(tls_keyfile: Optional[str], tls_certfile: Optional[str]) -> dict:
    """Build uvicorn SSL kwargs if both cert and key are provided.

    Used to pass through to ``uvicorn.Config(ssl_keyfile=..., ssl_certfile=...)``
    so an operator who wants TLS without a reverse proxy can enable it with
    ``--tls-keyfile`` / ``--tls-certfile``. Returns an empty dict when TLS is
    not configured (plain HTTP — acceptable on a trusted LAN, documented as a
    risk in the README).

    Fail-loud on partial config: passing exactly ONE of the two flags silently
    downgrades to plain HTTP, which would expose the Bearer token in cleartext
    while the operator believes TLS is active. Refuse that — both or neither.
    """
    if tls_keyfile and tls_certfile:
        return {"ssl_keyfile": tls_keyfile, "ssl_certfile": tls_certfile}
    if tls_keyfile or tls_certfile:
        sys.stderr.write(
            "zmem-mcp: --tls-keyfile and --tls-certfile must be provided together. "
            "A partial TLS config would silently downgrade to plain HTTP, exposing "
            "the Bearer token in cleartext. Pass both, or neither.\n"
        )
        sys.exit(2)
    return {}
