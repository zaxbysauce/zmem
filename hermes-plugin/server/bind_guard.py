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

import ipaddress
import os
import socket
import sys
from typing import Optional


def _is_wildcard(host: str) -> bool:
    """True if ``host`` resolves to an unspecified (wildcard) bind address.

    Covers all equivalent forms — ``0.0.0.0``, ``::``, ``[::]``, ``0``, ``0.0``,
    ``0:0:0:0:0:0:0:0`` — by parsing with :mod:`ipaddress` (for standard
    literals) and :func:`socket.inet_aton` (for the inet_aton shorthands ``0``
    and ``0.0`` that uvicorn/socket accept but ipaddress rejects). Hostnames
    can't be statically classified, so they pass (the operator named it).

    IPv4-mapped IPv6 wildcard forms (``::ffff:0.0.0.0`` and its equivalents)
    are also treated as wildcard: ``IPv6Address.is_unspecified`` is False for a
    mapped address even when the mapped IPv4 IS unspecified, so the mapped form
    would otherwise slip past the guard (#37 L10). Low exploitability — an
    operator must deliberately type the literal, and a Windows bind on it fails
    — but the allowlist should cover the form for completeness.
    """
    h = (host or "").strip().lower().strip("[]")
    if h in {"", "0.0.0.0", "::"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        pass
    else:
        if ip.is_unspecified:
            return True
        # IPv4-mapped IPv6 form: ::ffff:0.0.0.0 is the mapped equivalent of
        # 0.0.0.0. is_unspecified is False for the mapped address, but the
        # mapped IPv4 (0.0.0.0) IS unspecified, so treat the whole thing as a
        # wildcard bind.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and mapped.is_unspecified:
            return True
        return False
    # inet_aton shorthands: socket.inet_aton("0") == 0.0.0.0, ("0.0") == 0.0.0.0.
    # These are accepted by uvicorn's bind but rejected by ipaddress.
    try:
        packed = socket.inet_aton(h)
        return packed == b"\x00\x00\x00\x00"  # 0.0.0.0
    except OSError:
        # Not a literal IP (e.g. a hostname). uvicorn/socket will resolve it;
        # we can't statically know, so don't block — the operator named it.
        return False


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
    h = (host or "").strip()
    if _is_wildcard(h):
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
