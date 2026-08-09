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

See the repo README's "Hermes Agent" section for Windows Task Scheduler
persistence, TLS options, and the full env-var reference.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
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
# Cap on a single add() content payload. Matches store.py's MAX_CONTENT_CHARS
# (65536) so a memory written over the network imports cleanly via Tier-3 sync
# on another box — previously MCP silently truncated at 32000 while ingest-jsonl
# rejected at 65536, so a 32k–65k row written here was silently mangled (#36 M17).
_MAX_CONTENT_CHARS = 65536
_DEFAULT_LIMIT = 5
_HARD_LIMIT_MAX = 50
# Concurrency cap on simultaneous store.py subprocesses. Each tool call spawns a
# fresh store.py; without a bound an authorized but chatty remote client could
# exhaust the process table / FDs / memory. Default 8; override via env (#36 M6).
_MAX_CONCURRENT_STORE = max(1, int(os.environ.get("ZMEM_MCP_MAX_CONCURRENT", "8")))
# How long a queued tool call waits to acquire the concurrency slot before
# returning a 503-style overload error (vs. waiting forever).
_QUEUE_TIMEOUT_S = float(os.environ.get("ZMEM_MCP_QUEUE_TIMEOUT_S", "60"))


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


def _log_embedding_availability() -> None:
    """Probe embedding availability at server startup and log it.

    Surface the degraded state LOUDLY at boot rather than discovering it
    hundreds of unembedded rows later (issue #22). Best-effort and fail-open:
    a probe failure logs a warning and never blocks startup. Imports the
    embeddings module from the SAME checkout the `add` subprocesses run, under
    THIS server's interpreter (== the `sys.executable` _run_store uses), so
    the reported state matches what writes will actually experience.
    """
    # Resolve the store checkout inside the fail-open guard. _resolve_store_py
    # -> _resolve_zmem_home can abort the probe (and thus server boot) three
    # ways: sys.exit(2) on a missing/invalid ZMEM_HOME (SystemExit — NOT caught
    # by `except Exception` since it's a BaseException); PermissionError (OSError)
    # from its own is_file() probes on an access-denied dir; or RuntimeError from
    # Path.expanduser() when home is undeterminable. All three are
    # misconfiguration, not reasons to kill boot — the same bad config previously
    # failed on first tool use, not at startup. (Tool calls still fail loudly via
    # _run_store.) Catch the specific set; do NOT catch BaseException (Ctrl-C must
    # still terminate the server).
    try:
        store_py = _resolve_store_py()
    except (SystemExit, OSError, RuntimeError):
        logger.warning(
            "zmem embeddings: store checkout could not be resolved "
            "(ZMEM_HOME unset/invalid/inaccessible); skipping the availability "
            "probe and assuming degraded. add() may store rows without embeddings."
        )
        return
    scripts_dir = str(store_py.parent)
    try:
        import importlib

        saved_path = sys.path[:]
        sys.path.insert(0, scripts_dir)
        try:
            emb = importlib.import_module("embeddings")
            status = emb.availability_status()
        finally:
            sys.path[:] = saved_path
        if status.get("available"):
            logger.info("zmem embeddings: available (semantic recall/dup active)")
        else:
            reason = status.get("reason")
            if reason == "imports_missing":
                missing = status.get("missing_imports") or []
                pkgs = ", ".join(missing) if missing else "onnxruntime/tokenizers/numpy"
                remedy = (
                    f"install the missing package(s): {pkgs} "
                    "(see hermes-plugin/server/requirements-embeddings.txt)"
                )
            else:
                remedy = (
                    "ensure both a checksum-verified minilm.onnx AND "
                    "tokenizer.json are present at the resolved models dir "
                    "(or set ZMEM_MODEL_URL to a source matching the pinned "
                    "SHA-256 plus ZMEM_MODEL_AUTODOWNLOAD=1)"
                )
            logger.warning(
                "zmem embeddings: UNAVAILABLE (reason=%s, models_dir=%s) — "
                "add() will store rows WITHOUT embeddings. %s, then run "
                "`reembed` to backfill.",
                reason,
                status.get("models_dir"),
                remedy,
            )
    except Exception as exc:  # never block startup on a diagnostic
        logger.warning(
            "zmem embeddings: probe failed (%s: %s) — assuming degraded; "
            "add() may store rows without embeddings.",
            type(exc).__name__,
            exc,
        )


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


# Process-wide concurrency cap. Created lazily inside the running event loop
# (an asyncio.Semaphore binds to the loop active at construction time), so the
# first async tool call builds it. Bounds simultaneous store.py subprocesses to
# prevent process-table/FD exhaustion from an authorized but chatty client (#36 M6).
_store_semaphore: "asyncio.Semaphore | None" = None


def _get_store_semaphore() -> "asyncio.Semaphore":
    global _store_semaphore
    if _store_semaphore is None:
        _store_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_STORE)
    return _store_semaphore


async def _run_store_async(args: list[str]) -> dict[str, Any]:
    """Async, concurrency-bounded wrapper around the sync ``_run_store``.

    FastMCP invokes tool functions directly in the event-loop thread, so a
    bare sync ``subprocess.run`` would BLOCK the whole loop (serializing every
    request and stalling health/auth handling). This offloads the subprocess to
    a worker thread (``asyncio.to_thread``) AND bounds how many run at once via
    an ``asyncio.Semaphore`` (#36 M6). A queued call that cannot acquire a slot
    within ``_QUEUE_TIMEOUT_S`` returns an overload error instead of waiting
    forever.
    """
    sem = _get_store_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                f"zmem-mcp: overloaded — { _MAX_CONCURRENT_STORE } concurrent "
                "store.py subprocesses in flight; retry shortly"
            ),
            "returncode": 503,
        }
    try:
        # to_thread frees the event loop while subprocess.run blocks.
        return await asyncio.to_thread(_run_store, args)
    finally:
        sem.release()


_WARN_RE = re.compile(r"^\[zmem\].*\b(WARNING|NOTICE)\b.*:")


def _parse_store_warnings(stderr: str) -> list[str]:
    """Pull zmem advisory/notice lines out of store.py stderr into a clean list.

    Matches any ``[zmem] ... WARNING/NOTICE ...:`` line — including topic-
    prefixed ones like ``[zmem] backup: WARNING - ...`` — so a structured
    warning is never silently dropped. Arbitrary stderr (tracebacks, debug
    noise) is ignored so we never surface untrusted text as a structured
    warning (#36 M4)."""
    out: list[str] = []
    for line in (stderr or "").splitlines():
        if _WARN_RE.match(line.strip()):
            # Keep the descriptive part after the first colon, trimmed.
            text = line.split(":", 1)[1].strip() if ":" in line else line.strip()
            out.append(text)
    return out


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
    async def recall(
        query: str,
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Recall memories relevant to a query (semantic + full-text).

        Returns the top matches from the shared cross-session store. Use this
        to surface lessons, conventions, and facts before answering anything
        that may depend on past work. Pass namespace='*' (or omit) to search
        all namespaces; pass an explicit namespace to scope. When a namespace
        is given, user:global memories are also unioned in (project-first, no
        crowding) so cross-project lessons surface — matching the local
        provider's prefetch behavior (PR #24).
        """
        q = (query or "").strip()
        if not q:
            return _error("query is required")
        n = _clamp_limit(limit)
        args = [
            "recall",
            "--query", q[:_MAX_QUERY_CHARS],
            "--limit", str(n),
            "--include-global",
            "--global-limit", "3",
            "--json",
        ] + _namespace_flag(namespace)
        return _parse_results(await _run_store_async(args))

    @mcp.tool()
    async def add(
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
        # Reject oversize content rather than silently truncating it — a silent
        # 32000-char truncation broke Tier-3 sync import elsewhere (the same
        # row written via CLI/ingest was stored whole or rejected at 65536).
        # Now all write paths enforce one cap (65536) consistently (#36 M17).
        if len(body) > _MAX_CONTENT_CHARS:
            return _error(
                f"content is {len(body)} chars, over the {_MAX_CONTENT_CHARS} limit"
            )
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
            # Default network writes to `auto` capture mode so secret-like
            # content is redacted before it is persisted (the local CLI stays
            # `manual` for trusted local use). `auto` refuses when source_ref
            # itself carries secret-like text — that surfaces as a structured
            # error below (provenance with a secret in it must be reviewed, not
            # silently stored). Advisory/notice warnings are surfaced in the
            # response `warnings` field. (#36 M4)
            "--capture-mode", "auto",
        ]
        if tags:
            args += ["--tags", str(tags)]
        if source_ref:
            args += ["--source-ref", str(source_ref)]
        r = await _run_store_async(args)
        warnings = _parse_store_warnings(r.get("stderr", ""))
        if not r["ok"]:
            # returncode 2 == CapturePolicyRefusal (source_ref secret-like) or
            # argparse/validation error. Surface the (redacted) message + any
            # warnings parsed so far. Do NOT echo raw stderr verbatim.
            msg = r.get("stderr") or r.get("stdout", "")[:200] or "add failed"
            resp = _error(msg)
            if warnings:
                resp["warnings"] = warnings
            return resp
        resp = {"result": "stored", "raw": (r["stdout"] or "").strip()}
        if warnings:
            resp["warnings"] = warnings
        return resp

    @mcp.tool()
    async def search(
        query: str,
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Search the shared store (alias of recall; same semantics)."""
        return await recall(query=query, namespace=namespace, limit=limit)

    @mcp.tool()
    async def supersede(id: str, reason: Optional[str] = None) -> dict[str, Any]:
        """Mark a stored memory obsolete (corrected or OBE)."""
        mid = (id or "").strip()
        if not mid:
            return _error("id is required")
        args = ["supersede", "--id", mid]
        if reason:
            args += ["--reason", str(reason)]
        r = await _run_store_async(args)
        if not r["ok"]:
            return _error(
                r["stderr"] or r["stdout"][:200] or f"memory id {mid} not found"
            )
        return {"result": "superseded", "id": mid}

    @mcp.tool()
    async def recent(
        namespace: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Return the most recently ingested memories."""
        n = _clamp_limit(limit)
        args = ["recent", "--limit", str(n), "--json"] + _namespace_flag(namespace)
        return _parse_results(await _run_store_async(args))

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

    # Surface embedding availability at startup so a degraded server is loud
    # immediately, not 700 unembedded rows later (issue #22). Fail-open.
    _log_embedding_availability()

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
