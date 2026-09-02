"""zmem MCP client — call one zmem MCP tool over StreamableHTTP and print it.

Issue #71 A: the remote passive-prefetch transport. A Hermes hook
(``zmem-hermes-reflect.py`` on ``pre_llm_call``) runs this file as a
SUBPROCESS when ``ZMEM_MCP_URL`` is set, so the hook itself stays sync and
stdlib-only while the ``mcp`` client library and its asyncio event loop live
(and die) in this child process. ``subprocess.run(timeout=...)`` in the hook
is the wedge-proof backstop.

No second protocol: this speaks the SAME StreamableHTTP MCP surface as
``mcp_server.py`` (it is in the same directory and covered by the same
``requirements.txt``), and by default calls the ``session_start`` tool — the
passive ``--no-bump`` prefetch that inherits the fence, the token budget, and
the silent-reason contract server-side.

Usage:
    python mcp_client.py --url http://host:8765/mcp \
        [--token <secret> | --token-file <path>] \
        call session_start [--namespace user:global]

Output: the tool's text content on stdout (exit 0), or a diagnostic on
stderr with a non-zero exit. ANY failure is the caller's fail-open signal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    token_file = args.token_file or os.environ.get("ZMEM_MCP_TOKEN_FILE", "")
    if token_file:
        p = Path(token_file).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    env_token = os.environ.get("ZMEM_MCP_TOKEN", "")
    return env_token.strip()


async def _call(url: str, token: str, tool: str, arguments: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    parts = []
    for block in (result.content or []):
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(prog="mcp_client.py",
                                     description="call one zmem MCP tool")
    parser.add_argument("--url", required=True,
                        help="MCP endpoint URL (ZMEM_MCP_URL)")
    parser.add_argument("--token", default="",
                        help="bearer token (defaults to ZMEM_MCP_TOKEN)")
    parser.add_argument("--token-file", default="",
                        help="file holding the bearer token "
                             "(defaults to ZMEM_MCP_TOKEN_FILE)")
    sub = parser.add_subparsers(dest="action", required=True)
    call = sub.add_parser("call", help="call a tool")
    call.add_argument("tool", help="tool name, e.g. session_start")
    call.add_argument("--namespace", default="",
                      help="namespace argument (tool-specific; empty omits it)")
    args = parser.parse_args()

    token = _resolve_token(args)
    if not token:
        print("mcp_client: no token (set ZMEM_MCP_TOKEN or --token-file)",
              file=sys.stderr)
        return 2

    if args.action == "call":
        arguments: dict = {}
        if args.namespace:
            arguments["namespace"] = args.namespace
        try:
            text = asyncio.run(_call(args.url, token, args.tool, arguments))
        except ImportError as exc:
            print(f"mcp_client: the 'mcp' package is required for remote "
                  f"prefetch ({exc}); install hermes-plugin/server/"
                  "requirements.txt on this box", file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"mcp_client: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if not text.strip():
            print("mcp_client: empty tool response", file=sys.stderr)
            return 1
        print(text)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
