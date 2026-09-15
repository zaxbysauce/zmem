"""zmem MCP client — call one zmem MCP tool over StreamableHTTP and print it.

Issue #122 (Hermes compatibility parity): the remote passive prefetch
transport for the ``pre_llm_call`` reflect hook. The hook (stdlib-only,
sync) runs this file as a SUBPROCESS with ONE selector call per hook
invocation::

    python hermes-plugin/server/mcp_client.py --url <ZMEM_MCP_URL> \
        call prefetch --query <user_message> --namespace <namespace> \
        --session-id <session_id> --moment user_prompt \
        --lane hermes-compat [--ops-token <token> ...]

``moment=user_prompt`` and ``lane=hermes-compat`` are supplied by the
compatibility caller every time. The server's #159 selector owns the gate,
the one 1,500-token budget, the delivery ledger and the ``rendered`` fence;
this client validates the envelope boundary and prints the COMPLETE
selector envelope (all required keys, ``rendered`` included) as compact
JSON + one final LF. The hook merges its pending local failure text ahead
of ``rendered`` and commits the operation cursor only after a rendered
response — fail-open: ANY client failure (missing ``mcp`` lib, bad token,
refused connection, timeout, malformed envelope) is the hook's signal to
proceed without injection.

Usage:
    python mcp_client.py --url http://host:8765/mcp \
        [--token <secret> | --token-file <path>] \
        call prefetch --query <text> --namespace <ns> --session-id <sid> \
            --moment user_prompt --lane hermes-compat [--ops-token <t> ...]

Exit codes: 0 success · 1 transport/empty/invalid-envelope (fail-open
signal) · 2 usage or missing token · 3 the ``mcp`` package is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# The closed #158/#159 selector-envelope key set; every key must be present
# or the response is a failed prefetch (issue #122).
_SELECTOR_ENVELOPE_KEYS = (
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
)


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    token_file = args.token_file or os.environ.get("ZMEM_MCP_TOKEN_FILE", "")
    if token_file:
        p = Path(token_file).expanduser()
        if p.is_file():
            raw = p.read_text(encoding="utf-8")
            # Same sniff rule as server auth.py: a file starting with '{' is
            # the JSON form {"token": ..., "namespaces": [...]} — take the
            # token field (the server enforces the namespaces); anything else
            # is a bare token file.
            if raw.lstrip().startswith("{"):
                try:
                    obj = json.loads(raw)
                    tok = obj.get("token") if isinstance(obj, dict) else None
                    if isinstance(tok, str) and tok.strip():
                        return tok.strip()
                except ValueError:
                    pass
                print(f"mcp_client: token file {p} starts with '{{' but is "
                      "not a valid {'token': ...} JSON object",
                      file=sys.stderr)
                return ""
            return raw.strip()
    env_token = os.environ.get("ZMEM_MCP_TOKEN", "")
    return env_token.strip()


async def _call(url: str, token: str, tool: str, arguments: dict) -> dict:
    """Call one MCP tool and return the parsed JSON object (the selector
    envelope for ``prefetch``). Raises on transport/tool errors; envelope
    key validation happens in ``main`` so its exit codes stay exact."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    if getattr(result, "isError", False):
        errs = " | ".join(
            getattr(b, "text", "") for b in (result.content or [])
            if getattr(b, "text", None)
        ).strip()
        raise RuntimeError(f"tool returned isError=True: {errs[:200]}")
    parts = []
    for block in (result.content or []):
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
    joined = "\n".join(parts)
    # FastMCP serializes the tool's dict return into a JSON text block; the
    # prefetch envelope must cross this boundary whole (never a bare context
    # string — issue #122).
    stripped = joined.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            raise ValueError("prefetch response is not valid JSON")
        if isinstance(obj, dict):
            if obj.get("error"):
                raise RuntimeError(str(obj["error"])[:200])
            return obj
    raise ValueError("prefetch response was not a JSON object")


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
    call.add_argument("tool", help="tool name, e.g. prefetch")
    call.add_argument("--query", dest="query", type=str, default="",
                      help="query text for prefetch")
    call.add_argument("--namespace", dest="namespace", type=str, default="",
                      help="memory namespace for prefetch")
    call.add_argument("--session-id", dest="session_id", type=str, default="",
                      help="full session id for prefetch")
    call.add_argument("--moment", dest="moment",
                      choices=("session_start", "user_prompt", "pretool",
                               "subagent", "precompact"),
                      default=None,
                      help="runtime injection moment")
    call.add_argument("--lane", dest="lane",
                      choices=("claude", "codex", "zcode", "hermes-provider",
                               "hermes-compat"),
                      default=None,
                      help="runtime host lane")
    call.add_argument("--ops-token", dest="ops_tokens", action="append",
                      default=[],
                      help="Operation token from the pretool ring "
                           "(repeatable)")
    args = parser.parse_args()

    token = _resolve_token(args)
    if not token:
        print("mcp_client: no token (set ZMEM_MCP_TOKEN or --token-file)",
              file=sys.stderr)
        return 2

    if args.action == "call":
        if args.tool == "prefetch":
            if not args.namespace or not args.session_id \
                    or not args.moment or not args.lane:
                print("mcp_client.py: error: prefetch requires --namespace, "
                      "--session-id, --moment, and --lane", file=sys.stderr)
                return 2
        arguments: dict = {}
        if args.tool == "prefetch":
            arguments = {
                "query": args.query,
                "namespace": args.namespace,
                "session_id": args.session_id,
                "moment": args.moment,
                "lane": args.lane,
                "ops_tokens": list(args.ops_tokens),
            }
        elif args.namespace:
            arguments["namespace"] = args.namespace
        try:
            envelope = asyncio.run(_call(args.url, token, args.tool,
                                         arguments))
        except ImportError as exc:
            print(f"mcp_client: the 'mcp' package is required for remote "
                  f"prefetch ({exc}); install hermes-plugin/server/"
                  "requirements.txt on this box", file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"mcp_client: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        except BaseException as exc:  # noqa: BLE001 — see PRR-010 disposition
            # anyio cancellation surfaces a BaseExceptionGroup (a
            # BaseException subclass) that the generic handler above cannot
            # catch. This is still a subprocess whose only job is to die
            # cleanly — map it to the same rc-1 fail-open signal instead of
            # a raw traceback.
            print(f"mcp_client: {type(exc).__name__} during MCP call; "
                  "treating as failure", file=sys.stderr)
            return 1
        if args.tool == "prefetch" and (
                not isinstance(envelope, dict)
                or any(k not in envelope for k in _SELECTOR_ENVELOPE_KEYS)):
            print("mcp_client: invalid prefetch envelope", file=sys.stderr)
            return 1
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
