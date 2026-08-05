"""zmem MCP server — exposes the shared zmem store to remote MCP clients.

Runs on the store-host box (the machine with ``~/.zmem/store.sqlite`` and the
zmem checkout). A Hermes agent on a *different* LAN box connects to this server
over HTTP (StreamableHTTP) with a Bearer token and gets ``recall`` / ``add`` /
``search`` / ``supersede`` / ``recent`` tools — the same surface the local
memory provider exposes, but over the network. One store, one schema, shared
across all four agents (ZCode, Claude Code, Codex, local Hermes) plus any
remote MCP client.

Each tool subprocesses ``store.py`` (same code path as the local memory
provider — never imports store.py in-process, so the server inherits zmem's
writer-lease isolation). Result limits are capped server-side to prevent a
misconfigured remote from bloating its own context.

Run:
    ZMEM_HOME=/path/to/zmem \\
    ZMEM_MCP_TOKEN=<secret> \\
    python mcp_server.py --host <lan-ip> --port 8765

See README.md for Windows Task Scheduler persistence and TLS options.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# Add this directory to sys.path so sibling modules import cleanly when the
# server is run directly (``python mcp_server.py``) rather than as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import StaticTokenVerifier, load_expected_token  # noqa: E402
from bind_guard import enforce_bind_host, resolve_tls_kwargs  # noqa: E402

logger = logging.getLogger("zmem-mcp")

# -- store.py location -------------------------------------------------------

_STORE_PY_REL = Path("skills") / "memory" / "scripts" / "store.py"
_STORE_TIMEOUT_S = 30  # network clients can tolerate a longer cap
_MAX_QUERY_CHARS = 1000
# Cap on a single add() content payload — prevents a misconfigured/buggy remote
# from submitting an arbitrarily large string that stalls the subprocess.
_MAX_CONTENT_CHARS = 32000
_DEFAULT_LIMIT = 5
_HARD_LIMIT_MAX = 50


def _resolve_zmem_home() -> Path:
    """Resolve the zmem checkout root.

    Order: ``ZMEM_HOME`` env → this file's repo root (server/ is under
    hermes-plugin/, which is under the zmem checkout). Exits 2 with a clear
    message if neither yields a store.py.
    """
    raw = os.environ.get("ZMEM_HOME", "").strip()
    if raw:
        candidate = Path(raw).expanduser() / _STORE_PY_REL
        if candidate.is_file():
            return Path(raw).expanduser()
        sys.stderr.write(
            f"zmem-mcp: store.py not found at {candidate}. "
            f"Is ZMEM_HOME pointing at a zmem checkout?\n"
        )
        sys.exit(2)
    # In-tree fallback: this file is at <repo>/hermes-plugin/server/mcp_server.py.
    here = Path(__file__).resolve().parent
    candidate_root = here.parent.parent
    if (candidate_root / _STORE_PY_REL).is_file():
        return candidate_root
    sys.stderr.write(
        "zmem-mcp: cannot locate store.py. Set ZMEM_HOME to your zmem "
        "checkout, or run this server from inside the zmem repo.\n"
    )
    sys.exit(2)


def _resolve_store_py() -> Path:
    return _resolve_zmem_home() / _STORE_PY_REL


def _run_store(args: list[str]) -> dict[str, Any]:
    """Run ``store.py <args>``; returns {ok, stdout, stderr, returncode}."""
    store_py = _resolve_store_py()
    cmd = [sys.executable, str(store_py), *args]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STORE_TIMEOUT_S,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"store.py timed out after {_STORE_TIMEOUT_S}s",
            "returncode": 124,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"store.py failed: {exc}",
            "returncode": 1,
        }


def _clamp_limit(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(n, _HARD_LIMIT_MAX))


def _error(message: str) -> dict[str, Any]:
    return {"error": message}


def _build_resource_url(host: str, port: int, use_tls: bool) -> str:
    """Build the auth-metadata URL with the correct scheme + bracketed IPv6.

    Hardcoding ``http://`` was a two-part bug: (1) under TLS the OAuth
    protected-resource metadata advertised the wrong scheme, directing clients
    to send the Bearer token in cleartext; (2) an IPv6 literal host (e.g.
    ``::1``) produced ``http://::1:8765`` which pydantic's AnyHttpUrl rejects
    (unbracketed IPv6), crashing startup. Bracket IPv6 per RFC 2732 and derive
    the scheme from whether TLS is configured.
    """
    scheme = "https" if use_tls else "http"
    # Bracket IPv6 literals (contain ':' but aren't IPv4).
    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{bracketed}:{port}"


def _namespace_flag(namespace: Optional[str]) -> list[str]:
    """Build the ``--namespace`` argv slice, treating ``'*'`` as "all".

    A bare ``'*'`` means "search every namespace" — store.py implements that by
    OMITTING ``--namespace`` (no filter). Passing ``--namespace '*'`` literally
    would search for a namespace named ``*`` (returns nothing). Default
    (``None``) also omits the flag: the MCP server is multi-user/network and has
    no single "session namespace" concept, so the safe default is the full
    store — callers who want isolation pass an explicit namespace.
    """
    ns = (namespace or "").strip()
    if ns and ns != "*":
        return ["--namespace", ns]
    return []


def _parse_results(r: dict[str, Any]) -> dict[str, Any]:
    """Parse store.py's ``--json`` stdout into ``{results, count}``.

    Shared by ``recall`` and ``recent`` (was duplicated). Handles empty stdout,
    non-JSON, and non-list shapes uniformly.
    """
    if not r["ok"]:
        return _error(r["stderr"] or r["stdout"][:200])
    stdout = (r["stdout"] or "").strip()
    if not stdout:
        return {"results": [], "count": 0}
    try:
        results = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _error(f"non-JSON from store.py: {exc}")
    if not isinstance(results, list):
        results = []
    return {"results": results, "count": len(results)}


# -- FastMCP construction ----------------------------------------------------

def build_server(host: str, port: int, use_tls: bool = False) -> "FastMCP":  # type: ignore[name-defined]
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP

    token = load_expected_token()
    verifier = StaticTokenVerifier(token)
    resource_url = _build_resource_url(host, port, use_tls=use_tls)

    mcp = FastMCP(
        "zmem",
        host=host,
        port=port,
        # issuer_url is required by AuthSettings even for static-token RS-only
        # mode; set it to the server's own URL (we're not a real OAuth AS).
        auth=AuthSettings(
            issuer_url=resource_url,
            resource_server_url=resource_url,
            required_scopes=None,
        ),
        token_verifier=verifier,
    )

    @mcp.tool()
    def recall(
        query: str,
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Recall memories relevant to a query (semantic + full-text).

        Returns the top matches from the shared cross-session store. Use this
        to surface lessons, conventions, and facts before answering anything
        that may depend on past work. Pass namespace='*' (or omit) to search
        all namespaces; pass an explicit namespace to scope.
        """
        q = (query or "").strip()
        if not q:
            return _error("query is required")
        n = _clamp_limit(limit)
        args = [
            "recall",
            "--query", q[:_MAX_QUERY_CHARS],
            "--limit", str(n),
            "--json",
        ] + _namespace_flag(namespace)
        return _parse_results(_run_store(args))

    @mcp.tool()
    def add(
        type: str,
        content: str,
        namespace: str = "user:global",
        tags: Optional[str] = None,
        signal: str = "none",
        source_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        """Capture a grounded memory to the shared store.

        type: fact | lesson | convention | preference. signal: test | compile |
        lint | reviewer | user | none (how strongly grounded this is).
        """
        mtype = (type or "").strip()
        body = (content or "").strip()
        if mtype not in ("fact", "lesson", "convention", "preference"):
            return _error(
                "type must be one of: fact, lesson, convention, preference"
            )
        if not body:
            return _error("content is required")
        # Cap content length — recall clamps query; add should clamp content
        # symmetrically to prevent a bloat-DoS on the write path.
        body = body[:_MAX_CONTENT_CHARS]
        sig = (signal or "none").strip()
        if sig not in ("test", "compile", "lint", "reviewer", "user", "none"):
            return _error(
                "signal must be one of: test, compile, lint, reviewer, user, none"
            )
        args = [
            "add",
            "--namespace", str(namespace),
            "--type", mtype,
            "--content", body,
            "--signal", sig,
        ]
        if tags:
            args += ["--tags", str(tags)]
        if source_ref:
            args += ["--source-ref", str(source_ref)]
        r = _run_store(args)
        if not r["ok"]:
            return _error(r["stderr"] or r["stdout"][:200])
        return {"result": "stored", "raw": (r["stdout"] or "").strip()}

    @mcp.tool()
    def search(
        query: str,
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Search the shared store (alias of recall; same semantics)."""
        return recall(query=query, namespace=namespace, limit=limit)

    @mcp.tool()
    def supersede(id: str, reason: Optional[str] = None) -> dict[str, Any]:
        """Mark a stored memory obsolete (corrected or OBE)."""
        mid = (id or "").strip()
        if not mid:
            return _error("id is required")
        args = ["supersede", "--id", mid]
        if reason:
            args += ["--reason", str(reason)]
        r = _run_store(args)
        if not r["ok"]:
            return _error(
                r["stderr"] or r["stdout"][:200] or f"memory id {mid} not found"
            )
        return {"result": "superseded", "id": mid}

    @mcp.tool()
    def recent(
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Return the most recently ingested memories."""
        n = _clamp_limit(limit)
        args = ["recent", "--limit", str(n), "--json"] + _namespace_flag(namespace)
        return _parse_results(_run_store(args))

    return mcp


# -- entrypoint --------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="zmem MCP server — exposes the shared zmem store to remote clients."
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Bind host. Default: auto-detected LAN IP. "
            "Wildcard (0.0.0.0/::) requires ZMEM_MCP_ALLOW_INSECURE_BIND=1."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Bind port (default 8765)."
    )
    parser.add_argument(
        "--tls-keyfile",
        default=None,
        help="Path to TLS key file (enables HTTPS; pair with --tls-certfile).",
    )
    parser.add_argument(
        "--tls-certfile",
        default=None,
        help="Path to TLS cert file (enables HTTPS; pair with --tls-keyfile).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s zmem-mcp %(levelname)s %(message)s",
    )

    host = enforce_bind_host(args.host)
    tls_kwargs = resolve_tls_kwargs(args.tls_keyfile, args.tls_certfile)
    use_tls = bool(tls_kwargs)

    mcp = build_server(host=host, port=args.port, use_tls=use_tls)

    if tls_kwargs:
        # uvicorn honors ssl_keyfile/ssl_certfile via Config kwargs. We use the
        # SDK's streamable_http_app() and run uvicorn directly so we can pass
        # them through.
        import uvicorn

        app = mcp.streamable_http_app()
        config = uvicorn.Config(
            app, host=host, port=args.port, log_level="info", **tls_kwargs
        )
        server = uvicorn.Server(config)
        logger.info("zmem MCP server starting on https://%s:%s/mcp", host, args.port)
        server.run()
    else:
        logger.info("zmem MCP server starting on http://%s:%s/mcp", host, args.port)
        if host == "127.0.0.1":
            logger.warning(
                "bound to loopback — remote LAN clients cannot reach this server; "
                "pass --host <lan-ip> to expose it."
            )
        # No TLS: use the SDK's run() which handles uvicorn internally.
        mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
