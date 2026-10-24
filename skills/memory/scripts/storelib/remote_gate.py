"""Issue #289 (Workstream T PR 10): the remote-mode CLI gate.

When ``ZMEM_REMOTE_URL`` is set, the user-facing ``store.py`` subcommands
route through the fleet backend instead of a local store — no split brain:

- ROUTE-WRITE (``add``/``update``/``supersede``/``invalidate``): enqueue a
  T-09 typed op (``fleet.opqueue``) and print a receipt; never open a local
  store.
- ROUTE-READ (``recall``/``search``/``recent``/``list``/``get``): call the
  hub's MCP tools through ``fleet.transport.McpHttp``, print the envelope
  plus the pre-flush pending overlay.
- ALLOW-LOCAL: store-independent sidecar / transcript commands and the
  read-only no-create probes keep their local execution (hooks depend on
  them; none writes the memory table).
- REFUSE: everything else exits 2 with ``this command runs on the hub;
  ZMEM_REMOTE_URL is set``.

The hub cannot express every local flag, so the routed bands REFUSE flags
whose semantics would otherwise be silently dropped (the plan-critic
honesty contract): a routed command carries only the flags in its allow
list below, and the allow lists are exhaustive.

Called once from ``storelib.cli.main()`` right after the post-parse
coupling checks and BEFORE any store access; a ``None`` return means the
caller continues on the unchanged local path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

REMOTE_URL_ENV = "ZMEM_REMOTE_URL"
REFUSAL_MESSAGE = "this command runs on the hub; ZMEM_REMOTE_URL is set"

# The routed write band — T-09 op kinds (storelib/ops.py _OP_KINDS).
_ROUTE_WRITE = ("add", "update", "supersede", "invalidate")
# The routed read band — served by the hub's MCP tools.
_ROUTE_READ = ("recall", "search", "recent", "list", "get")
# Store-independent sidecars, transcript miners and read-only no-create
# probes.  Verified at issue #289: failures/signals/corrections read
# transcripts; queue-* are the correction-queue sidecar; source-exists /
# source-ref-exists open mode=ro and answer exists:false on an absent
# store; ops-append writes the operation ring; retain/retain-adapt write
# the correction-queue review sidecar; ledger-clear/delivery-clear touch
# the delivery-ledger sidecar; hermes-reflect and hermes-context
# commit-cursor write ops-token sidecars.  ``hermes-context`` is allowed
# ONLY for --action commit-cursor (ack-failure deletes memory meta rows).
_ALLOW_LOCAL = frozenset({
    "failures", "signals", "corrections", "queue-list", "queue-clear",
    "queue-add", "source-exists", "source-ref-exists", "ops-append",
    "retain", "retain-adapt", "ledger-clear", "delivery-clear",
    "hermes-reflect", "hermes-context",
})
_HERMES_CONTEXT_ALLOWED_ACTIONS = frozenset({"commit-cursor"})

# Exhaustive allow lists (dest -> parser default).  A dest absent from the
# allow map that holds a value DIFFERENT from the recorded default was
# explicitly supplied and is refused.  Keep in sync with the argparse
# definitions in cli.py when flags are added — a future flag missing here
# reverts to silent-drop, which the honesty contract forbids (the
# supp test suite pins one flag per command).
_READ_DEFAULTS: dict[str, dict[str, Any]] = {
    "recall": {
        "hybrid": False, "no_hybrid": False, "no_mmr": False,
        "no_bump": False, "for_injection": False, "prompt_timestamp": None,
        "as_of": None, "include_global": False, "global_limit": 3,
        "min_confidence": None, "link_hops": 1, "link_budget": 2,
        "explain": False, "target": None, "no_unfold": False,
        "exclude": None, "moment": None, "lane": None, "session_id": None,
        "ops_token": [], "principal": None, "harness": None,
        "authorized_namespace": [], "include_cross_project": False,
        "legacy_unscoped": False, "delegation_prompt": "",
    },
    "search": {
        "exact": False, "include_global": False, "global_limit": 3,
        "no_bump": False, "as_of": None, "exclude": None,
    },
    "recent": {
        "min_confidence": None, "no_bump": False, "for_injection": False,
        "include_global": False, "global_limit": 3, "as_of": None,
        "exclude": None, "session_id": None, "moment": None, "lane": None,
        "delegation_prompt": "", "ops_token": [], "principal": None,
        "harness": None, "authorized_namespace": [],
        "include_cross_project": False, "legacy_unscoped": False,
    },
    "list": {},
    "get": {},
}
_WRITE_REFUSALS: dict[str, tuple[tuple[str, Any], ...]] = {
    # add: the T-09 applier carries namespace/type/content/tags/signal/
    # source_ref/capture_mode only; state/event types additionally need
    # expiry/occurrence args the applier drops (write.py would refuse at
    # apply time after a "pending" receipt — refused up front instead).
    "add": (
        ("confidence", None), ("taint", None), ("evidence", None),
        ("no_dedup", False), ("expiry_class", None), ("inject_until", None),
        ("occurred_start", ""), ("occurred_end", ""), ("mentioned_at", ""),
    ),
    "update": (
        ("confidence", None), ("evidence", None), ("reason", None),
        ("expiry_class", None), ("inject_until", None),
        ("expected_old_namespace", None), ("taint", None),
    ),
    "supersede": (("expected_namespace", None),),
    "invalidate": (("expected_namespace", None),),
}
_WRITE_REFUSED_TYPES = frozenset({"state", "event"})


def is_remote_mode(env: Optional[dict[str, str]] = None) -> bool:
    """True iff ``ZMEM_REMOTE_URL`` is set nonempty."""
    source = os.environ if env is None else env
    return bool((source.get(REMOTE_URL_ENV, "") or "").strip())


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _import_fleet():
    scripts = str(_scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from fleet import opqueue, transport  # noqa: PLC0415 - lazy by design
    return opqueue, transport


def _refuse(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[zmem] {msg}", file=sys.stderr)
    sys.exit(2)


def _supplied(args: Any, dest: str, default: Any) -> bool:
    value = getattr(args, dest, default)
    if isinstance(default, bool) or isinstance(value, bool):
        return bool(value) is not bool(default)
    return value != default


def _refuse_write_flags(cmd: str, args: Any) -> None:
    for dest, default in _WRITE_REFUSALS[cmd]:
        if _supplied(args, dest, default):
            flag = dest.replace("_", "-")
            _refuse(
                f"--{flag} is not supported in remote mode (ZMEM_REMOTE_URL "
                f"is set); the queued-op route carries only content, type, "
                f"tags, signal, source-ref and capture-mode semantics")
    if getattr(args, "type", None) in _WRITE_REFUSED_TYPES:
        _refuse(
            f"--type {args.type} is not supported in remote mode "
            f"(ZMEM_REMOTE_URL is set); its expiry/occurrence semantics do "
            f"not travel through the queued-op route")


def _refuse_read_flags(cmd: str, args: Any) -> None:
    for dest, default in _READ_DEFAULTS[cmd].items():
        if _supplied(args, dest, default):
            flag = dest.replace("_", "-")
            _refuse(
                f"--{flag} is not supported in remote mode (ZMEM_REMOTE_URL "
                f"is set); the hub read tools accept only query, namespace "
                f"and limit")


def _resolve_content(args: Any) -> str:
    content = args.content
    if content == "-":
        content = sys.stdin.read()
    return content


def _capture_mode(args: Any) -> str:
    # Same resolution as the local dispatch sites (cli.py add/update):
    # the operator's ZMEM_CAPTURE_MODE intent wins over the unset flag.
    if args.capture_mode:
        return args.capture_mode
    raw = (os.environ.get("ZMEM_CAPTURE_MODE", "") or "").strip().lower()
    if not raw:
        return "auto"
    return raw if raw in ("auto", "manual", "reviewed") else "manual"


def _enqueue_write(cmd: str, args: Any) -> None:
    opqueue, _transport = _import_fleet()
    # PRR-001 (PR #400 review): build the per-command dict FIRST so
    # _resolve_content runs EXACTLY once — the previous shape evaluated the
    # add-shaped dict literal (which reads stdin for `--content -`) before
    # discarding it for update, so a piped `update --content -` queued empty
    # content behind a success receipt.
    if cmd == "update":
        op_args: dict[str, Any] = {
            "id": args.id,
            "content": _resolve_content(args),
            "namespace": args.namespace,
            "type": args.type,
            "tags": args.tags or "",
            "source_ref": args.source_ref or "",
            "signal": args.signal,
            "capture_mode": _capture_mode(args),
        }
    elif cmd in ("supersede", "invalidate"):
        op_args = {"id": args.id, "reason": args.reason or ""}
    else:  # add
        op_args = {
            "namespace": args.namespace,
            "type": args.type,
            "content": _resolve_content(args),
            "tags": args.tags or "",
            "signal": args.signal or "none",
            "source_ref": args.source_ref or "",
            "capture_mode": _capture_mode(args),
        }
    entry = opqueue.enqueue("local", cmd, op_args, env=dict(os.environ))
    queue_path = opqueue.queue_path("local", env=dict(os.environ))
    print(json.dumps({
        "ok": True,
        "op_id": entry.get("op_id"),
        "op": cmd,
        "status": entry.get("status", "pending"),
        "queue": str(queue_path),
        "result": "queued",
    }))
    sys.exit(0)


def _read_error(reason: str, detail: str) -> None:
    print(f"[zmem] {detail}", file=sys.stderr)
    print(json.dumps({"ok": False, "reason": reason}))
    sys.exit(1)


def _hub(transport_module: Any) -> Any:
    env = dict(os.environ)
    return transport_module.McpHttp(
        url=env[REMOTE_URL_ENV].strip(),
        executor=transport_module.DeadlineExecutor(),
        deadline_s=transport_module.resolve_deadline_s(env),
    )


def _expected_read_keys(cmd: str, envelope: dict[str, Any]) -> bool:
    if cmd == "list":
        # The hub list_memories tool returns the list --json object
        # (recall.py list_memory), never a recall envelope.
        return {"namespace", "count", "memories"} <= set(envelope)
    required = {"results", "count", "omitted", "injection_risk",
                "tokens_used", "tokens_budget"}
    if cmd in ("recall", "search"):
        required = required | {"arms"}
    return required.issubset(envelope)


def _route_read(cmd: str, args: Any) -> None:
    opqueue, transport = _import_fleet()
    # Pre-import the heavy MCP client stack in the MAIN thread, BEFORE the
    # flush and the read: the transport worker's function-level imports
    # (mcp_client.py `from mcp import ClientSession` /
    # `streamablehttp_client`) resolve through sys.modules once warmed, so
    # neither executor join ever pays the cold import (round-2/3 review:
    # merely loading mcp_client's stdlib module body warms nothing — the
    # heavy import must be imported HERE, by name).
    try:
        import mcp  # noqa: F401
        import mcp.client.streamable_http  # noqa: F401
    except Exception:  # noqa: BLE001 - a warm-up failure degrades to the
        # executor's own timeout path; it never blocks the read attempt
        pass
    started = time.monotonic()
    # The overlay renders BEFORE the flush: it names the ops that were
    # pending when this command started (disclosed semantics — a flush
    # inside this call may commit them before the envelope prints).
    overlay = opqueue.render_pending("", "local", env=dict(os.environ))
    http = _hub(transport)
    deadline_s = transport.resolve_deadline_s(dict(os.environ))
    remaining_ms = int(max(0.0, deadline_s - (time.monotonic() - started)) * 1000)
    if remaining_ms > 1:
        try:
            opqueue.flush("local", http, env=dict(os.environ),
                          deadline_ms=max(1, min(2000, remaining_ms // 2)))
        except Exception:  # noqa: BLE001 - flush is best-effort, never fatal
            pass
    # The read gets its OWN full deadline window (the flush shares only its
    # bounded slice), so a healthy-hub read is never starved by the flush.
    kw = {"deadline_ms": int(deadline_s * 1000)}
    if cmd == "recall":
        result = http.recall(args.query, namespace=args.namespace,
                             limit=args.limit, **kw)
    elif cmd == "search":
        result = http.search(args.text, namespace=args.namespace,
                             limit=args.limit, **kw)
    elif cmd == "recent":
        result = http.recent(namespace=args.namespace, limit=args.limit, **kw)
    elif cmd == "list":
        result = http.list_memories(
            namespace=args.namespace, limit=args.limit,
            include_superseded=bool(getattr(args, "include_superseded", False)),
            **kw)
    else:  # get
        result = http.get_memory(args.id, **kw)
    if not isinstance(result, dict):
        _read_error("hub-response-invalid",
                    f"hub {cmd} returned a non-object response")
    if cmd == "get":
        # Local `get` prints the row JSON (exit 0) or a stderr line + 1.
        # The transport's reason is preserved: a connect-level failure
        # reports hub-unreachable; hub-refused is reserved for hub-ANSWERED
        # refusals (not found / namespace-guard denial).
        if result.get("ok") is False:
            reason = str(result.get("reason") or "hub-refused")
            _read_error(reason, f"hub get failed: {result}")
        if result.get("error"):
            _read_error("hub-refused",
                        f"hub get failed: {result.get('error')}")
        print(json.dumps(result, indent=2))
        sys.exit(0)
    if result.get("ok") is False or not _expected_read_keys(cmd, result):
        reason = ("hub-unreachable"
                  if result.get("reason") == "hub-unreachable"
                  else "hub-response-invalid")
        _read_error(reason, f"hub {cmd} failed: {json.dumps(result)[:300]}")
    print(json.dumps(result, indent=2))
    if overlay:
        print(overlay)
    sys.exit(0)


def handle_remote(args: Any) -> None:
    """Route a parsed ``store.py`` invocation in remote mode.

    Terminates the process (exit 0/1/2) for every band except ALLOW-LOCAL,
    for which it RETURNS so the caller continues on the unchanged local
    path (the caller only invokes this in remote mode)."""
    cmd = args.cmd
    explicit_store = (os.environ.get("ZMEM_STORE", "") or "").strip()
    if explicit_store:
        _refuse(
            "configuration error: ZMEM_STORE is set together with "
            "ZMEM_REMOTE_URL — a local memory store and a remote hub "
            "cannot both be honored. ZMEM_DATA alone is legal in remote "
            "mode (it names the local data dir: op queue, sidecars, "
            "broker cache); unset ZMEM_STORE to use the hub")
    if cmd in _ALLOW_LOCAL:
        if cmd == "hermes-context":
            action = getattr(args, "action", "")
            if action not in _HERMES_CONTEXT_ALLOWED_ACTIONS:
                # ack-failure deletes memory meta rows — hub-side only.
                _refuse(REFUSAL_MESSAGE)
        return
    if cmd in _ROUTE_WRITE:
        _refuse_write_flags(cmd, args)
        _enqueue_write(cmd, args)
        return
    if cmd in _ROUTE_READ:
        _refuse_read_flags(cmd, args)
        _route_read(cmd, args)
        return
    _refuse(REFUSAL_MESSAGE)
