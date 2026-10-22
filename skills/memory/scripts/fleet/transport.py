"""Shared fleet transport — lifted from hermes-plugin/transport.py (issue #287).

One module, three responsibilities, shared by every harness adapter (the
Hermes plugin re-exports it from ``hermes-plugin/transport.py``; the T-08
broker imports it as ``fleet.transport``):

- Mode selection: :func:`resolve_transport_mode` decides, from configuration
  only, whether a provider runs a local ``store.py`` subprocess or an
  MCP-over-HTTP transport.  Precedence: ``ZMEM_HERMES_MODE`` explicit >
  ``ZMEM_MCP_URL`` > ``ZMEM_REMOTE_URL`` (issue #287 adds the third selector;
  ``ZMEM_MCP_URL`` stays the historical alias so existing Hermes configs are
  unchanged) > resolvable local store.  It never opens a socket and never
  spawns a process.
- Deadline: :class:`DeadlineExecutor` runs every transport operation in a
  daemon worker under ``ZMEM_HERMES_DEADLINE_S`` (default 6.0; always below
  the host manager's 8.0 s join).
- Transports: :class:`LocalSubprocess` and :class:`McpHttp` return the
  COMPLETE parsed #158/#159 selector envelope.  Failure classes split into
  two observably distinct reasons (issue #287 AC4): an unreachable hub or a
  deadline hit returns ``reason="hub-unreachable"``; an honestly empty but
  healthy pool keeps ``reason="empty-pool"``.  Both fail open with zero
  numeric fields so a caller merely extracts ``rendered``.

The transport never imports ``storelib``, opens SQLite, or reads/writes the
delivery ledger; the store subprocess (local or hub-side) owns all of those.

Protocol (T-06/T-07): every hub call carries ``client_protocol`` (the client
major) and ``deadline_ms`` (the remaining budget); the hub gates both per
call.  ``hello`` is available for doctor/T-12 negotiation but is NEVER a
pre-flight gate inside the broker hot path.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
import logging
from pathlib import Path
from typing import Any, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

# fleet/ sits at <repo>/skills/memory/scripts/fleet/transport.py — the repo
# root is four levels up (fleet -> scripts -> memory -> skills -> repo).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_STORE_PY_REL = Path("skills") / "memory" / "scripts" / "store.py"
_DEADLINE_ENV = "ZMEM_HERMES_DEADLINE_S"
_DEADLINE_DEFAULT_S = 6.0
_DEADLINE_MAX_S = 8.0
_POST_CANCEL_GRACE_S = 1.0
_INVALID_DEADLINE_WARNING = (
    "transport: invalid ZMEM_HERMES_DEADLINE_S; using 6.0\n")
_INVALID_TOKEN_FILE_WARNING = "transport: invalid token file\n"

# issue #287 AC4: the distinct unreachable reason.  It is a BROKER-side /
# transport-side reason only — the hub can never be unreachable to itself,
# and it is deliberately NOT added to the store's closed inject-silent
# reason vocabulary (schema_meta mirrors); it surfaces in broker-log lines,
# the broker envelope and doctor, never in hub envelopes.
REASON_HUB_UNREACHABLE = "hub-unreachable"
REASON_EMPTY_POOL = "empty-pool"

# The closed #158/#159 envelope contract (mirrors storelib/inject.py:45-52):
# 14 required keys, three optional pass-through keys.  The transports return
# exactly this dict on success and the same shape zeroed on every failure.
_ENVELOPE_REQUIRED_KEYS = (
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
)

# T-06 client protocol major (the broker speaks protocol 1; the hub's
# supported_protocol_majors gates it per call).
CLIENT_PROTOCOL_MAJOR = 1


class TransportMode(str, Enum):
    """How a provider reaches the store: local subprocess or MCP."""

    local = "local"
    mcp = "mcp"


def _resolve_zmem_home() -> Optional[Path]:
    """Locate the zmem checkout; mirrors ``hermes-plugin/__init__.py``.

    ``ZMEM_HOME`` expanded and, when it names an existing directory,
    authoritative — a directory that lacks ``skills/memory/scripts/store.py``
    yields no local store with NO in-tree fallback (a remote-only box must
    not accidentally resolve the plugin's own checkout).  A ``ZMEM_HOME``
    that does not name a directory falls through to the in-tree checkout
    that ships this module, exactly like the provider's own resolver.
    """
    raw = os.environ.get("ZMEM_HOME", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate
    root = _REPO_ROOT
    if (root / _STORE_PY_REL).is_file():
        return root
    return None


def _resolve_store_py() -> Optional[Path]:
    """Absolute ``store.py`` path, or ``None`` when no local store resolves."""
    home = _resolve_zmem_home()
    if home is None:
        return None
    candidate = home / _STORE_PY_REL
    return candidate if candidate.is_file() else None


def resolve_transport_mode(
    env: Mapping[str, str] | None = None,
) -> tuple[Optional[TransportMode], str]:
    """Select the transport from configuration only.

    Precedence: an explicit ``ZMEM_HERMES_MODE`` wins (``local`` requires a
    resolvable local store and never falls back to MCP; ``mcp`` is taken at
    face value; anything else is invalid).  Without an explicit mode a
    nonempty ``ZMEM_MCP_URL`` selects MCP; then a nonempty
    ``ZMEM_REMOTE_URL`` (issue #287) selects MCP; otherwise a resolvable
    local ``store.py`` selects local; otherwise nothing is available.

    Returns ``(mode_or_None, reason)`` where the failure reasons are exactly
    ``mode=<value> invalid``, ``mode=none unavailable``, and
    ``mode=local unavailable: local store missing``.
    """
    source = os.environ if env is None else env
    explicit = (source.get("ZMEM_HERMES_MODE", "") or "").strip()
    if explicit:
        if explicit == TransportMode.local.value:
            if _resolve_store_py() is not None:
                return TransportMode.local, "mode=local"
            return None, "mode=local unavailable: local store missing"
        if explicit == TransportMode.mcp.value:
            return TransportMode.mcp, "mode=mcp"
        return None, "mode={} invalid".format(explicit)
    if (source.get("ZMEM_MCP_URL", "") or "").strip():
        return TransportMode.mcp, "mode=mcp (url)"
    if (source.get("ZMEM_REMOTE_URL", "") or "").strip():
        return TransportMode.mcp, "mode=mcp (remote-url)"
    if _resolve_store_py() is not None:
        return TransportMode.local, "mode=local (auto)"
    return None, "mode=none unavailable"


def resolve_deadline_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve ``ZMEM_HERMES_DEADLINE_S`` with the 6.0 default.

    Unset/empty → 6.0 silently.  Nonnumeric, nonfinite, zero, negative, and
    ``>= 8.0`` values all resolve to 6.0 and emit exactly one stderr warning
    line per resolution.
    """
    source = os.environ if env is None else env
    raw = (source.get(_DEADLINE_ENV, "") or "").strip()
    if not raw:
        return _DEADLINE_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        value = None
    if (value is None or not math.isfinite(value) or value <= 0.0
            or value >= _DEADLINE_MAX_S):
        sys.stderr.write(_INVALID_DEADLINE_WARNING)
        return _DEADLINE_DEFAULT_S
    return value


def _empty_envelope(reason: str = REASON_EMPTY_POOL) -> dict[str, Any]:
    """The full envelope shape returned on every transport failure class.

    ``reason`` distinguishes an honestly empty pool (the hub answered) from
    an unreachable hub / deadline hit (issue #287 AC4); the default keeps
    the historical ``empty-pool`` bytes for every existing caller.
    """
    return {
        "results": [],
        "count": 0,
        "omitted": 0,
        "reason": reason,
        "excluded": [],
        "candidate_ids": [],
        "tokens_used": 0,
        "tokens_budget": 0,
        "budget_dropped": 0,
        "budget_admission": 0,
        "budget_truncated": 0,
        "budget_dropped_protected": 0,
        "arms": {},
        "rendered": "",
    }


def _unreachable_envelope() -> dict[str, Any]:
    """The fail-open envelope for connect failures and deadline hits."""
    return _empty_envelope(REASON_HUB_UNREACHABLE)


def _coerce_envelope(payload: Any) -> dict[str, Any]:
    """Return ``payload`` only when it is a complete envelope with text
    ``rendered``; otherwise the empty envelope."""
    if isinstance(payload, dict) and all(
            key in payload for key in _ENVELOPE_REQUIRED_KEYS):
        if isinstance(payload.get("rendered"), str):
            return payload
    return _empty_envelope()


def _query_args(query: str) -> list[str]:
    """One ``--query`` pair; a leading-dash value rides the ``=`` form."""
    return ["--query=" + query] if query.startswith("-") else ["--query", query]


class _LocalOperation:
    """A cancellable ``store.py`` subprocess run (the deadline kill hook)."""

    def __init__(self, cmd: list[str], env: Optional[dict[str, str]] = None):
        self._cmd = cmd
        self._env = env
        self._proc: Optional[subprocess.Popen] = None

    def __call__(self) -> str:
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=self._env)
        out, _err = self._proc.communicate()
        return out or ""

    def cancel(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()


class DeadlineExecutor:
    """Run one operation under a wall-clock deadline in a daemon worker.

    On timeout the operation's ``cancel()`` hook (when present) is invoked —
    the local child is killed, the MCP coroutine cancelled — and the worker
    is joined with a short grace so a cancelled operation cannot append a
    side effect after ``run`` returns ``None``.  Workers are daemon threads:
    a pathological operation that outlives the grace join can never block
    the provider's return or the host process.
    """

    def run(self, fn: Callable[[], T], deadline_s: float) -> Optional[T]:
        outcome: dict[str, Any] = {}

        def _work() -> None:
            try:
                outcome["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised below
                outcome["error"] = exc

        worker = threading.Thread(target=_work, daemon=True, name="zmem-transport")
        worker.start()
        worker.join(deadline_s)
        if worker.is_alive():
            cancel = getattr(fn, "cancel", None)
            if cancel is not None:
                try:
                    cancel()
                except Exception:  # noqa: BLE001 - best-effort kill
                    pass
            worker.join(_POST_CANCEL_GRACE_S)
            return None
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")


def _load_mcp_call() -> Callable[
        [str, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Load ``hermes-plugin/server/mcp_client.py`` (stdlib-only) and return
    its ``_call``.  Resolved from the shared repo root, not the sibling
    directory the pre-lift module used."""
    path = _REPO_ROOT / "hermes-plugin" / "server" / "mcp_client.py"
    spec = importlib.util.spec_from_file_location("zmem_mcp_client_transport", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_mcp_client_transport"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module._call


class _TokenFileInvalid(Exception):
    """Raised when ``ZMEM_MCP_TOKEN_FILE`` is malformed JSON — the warning is
    emitted at the raise site and the operation fails into the empty
    envelope rather than proceeding with no credentials."""


def _resolve_token(explicit: Optional[str], env: Mapping[str, str]) -> str:
    """Token precedence: constructor token > token file > ``ZMEM_MCP_TOKEN``.

    A token file may be a bare token body or a JSON document with a nonempty
    string ``token`` field.  A malformed JSON file emits the
    ``transport: invalid token file`` warning and raises
    ``_TokenFileInvalid`` so the caller returns the empty envelope.
    """
    if explicit:
        return explicit
    token_file = (env.get("ZMEM_MCP_TOKEN_FILE", "") or "").strip()
    if token_file:
        try:
            body = Path(token_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        stripped = body.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                sys.stderr.write(_INVALID_TOKEN_FILE_WARNING)
                raise _TokenFileInvalid(token_file)
            token = parsed.get("token") if isinstance(parsed, dict) else None
            if isinstance(token, str) and token.strip():
                return token.strip()
            return ""
        if stripped:
            return stripped
    return (env.get("ZMEM_MCP_TOKEN", "") or "").strip()


def _remaining_deadline_ms(deadline_s: float, started: float) -> int:
    """Remaining budget in whole milliseconds, floored at 1 (a zero budget
    reads as a legacy call hub-side; the caller has already decided to go)."""
    remaining_ms = int((deadline_s - (time.monotonic() - started)) * 1000.0)
    return remaining_ms if remaining_ms > 0 else 1


class _McpToolOperation:
    """One MCP tool call: token resolution, the HTTP call, and the
    coroutine's private loop, with a deadline-driven ``cancel`` hook.
    Generalizes the pre-lift ``_McpPrefetchOperation`` to any tool."""

    def __init__(self, *, url: str, explicit_token: str | None,
                 call_fn: Callable[
                     [str, str, str, dict[str, Any]],
                     Awaitable[dict[str, Any]]] | None,
                 tool: str, arguments: dict[str, Any]):
        self._url = url
        self._explicit_token = explicit_token
        self._call_fn = call_fn
        self._tool = tool
        self._arguments = arguments
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    def __call__(self) -> dict[str, Any]:
        token = _resolve_token(self._explicit_token, os.environ)
        call = self._call_fn
        if call is None:
            call = _load_mcp_call()
        self._loop = asyncio.new_event_loop()
        try:
            self._task = self._loop.create_task(
                call(self._url, token, self._tool, self._arguments))
            return self._loop.run_until_complete(self._task)
        finally:
            self._loop.close()
            self._loop = None

    def cancel(self) -> None:
        task, loop = self._task, self._loop
        if task is not None and not task.done() and loop is not None:
            loop.call_soon_threadsafe(task.cancel)


def _is_unreachable_exception(exc: BaseException, _depth: int = 0) -> bool:
    """Classify a transport exception as hub-unreachable (AC4).

    Only connection/timeout classes mean the hub could not be reached:
    builtin ``ConnectionError``/timeouts, and connect/timeout/transport
    errors raised by the HTTP stack (``httpx.*``/``anyio.*``/``mcp.*`` by
    module + name). The async HTTP stack may surface the raw connect error
    WRAPPED — an anyio task-group ``ExceptionGroup`` or a bare ``Group`` —
    so the walk recurses into group sub-exceptions and the ``__cause__``/
    ``__context__`` chain before deciding (a plain group name classifies as
    NOT unreachable, which on POSIX made a refused connect collapse back
    into ``empty-pool`` — caught by frozen C4 on ubuntu CI).  A local
    misconfiguration — a malformed token file (``_TokenFileInvalid``), a
    missing ``mcp`` package, a missing client module — keeps the historical
    ``empty-pool`` fail-open reason (the hub was never attempted), which the
    transport tests pin.
    """
    if _depth > 8:
        return False
    if isinstance(exc, _TokenFileInvalid):
        return False
    if isinstance(exc, (ConnectionError, asyncio.TimeoutError, TimeoutError)):
        return True
    module = type(exc).__module__ or ""
    name = type(exc).__name__
    if module.split(".", 1)[0] in ("httpx", "anyio", "mcp", "http.client"):
        if ("Connect" in name or "Timeout" in name
                or "Transport" in name or name in ("ReadError", "WriteError")):
            return True
    # Wrapped failures: recurse into exception-group sub-exceptions...
    if hasattr(exc, "exceptions"):
        for sub in exc.exceptions:  # type: ignore[attr-defined]
            if _is_unreachable_exception(sub, _depth + 1):
                return True
    # ...and the cause/context chain.
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None and linked is not exc:
            if _is_unreachable_exception(linked, _depth + 1):
                return True
    return False


def _failure_reason(exc: BaseException) -> str:
    """Classify a non-envelope tool-call failure honestly (the
    _envelope_call rule): a connect-level failure is hub-unreachable; a
    LOCAL misconfiguration (missing mcp package, malformed token file)
    keeps the historical empty-pool reason instead of flagging a hub
    outage."""
    return (REASON_HUB_UNREACHABLE if _is_unreachable_exception(exc)
            else REASON_EMPTY_POOL)


class McpHttp:
    """The zmem MCP tools over HTTP; no network before the operation.

    Every method fails open: connect errors and deadline hits return the
    ``hub-unreachable`` envelope (or ``{"ok": False, "reason": ...}`` for
    the non-envelope tools); a healthy-but-empty pool keeps the hub's own
    ``empty-pool`` reason.  Every call carries ``client_protocol`` and
    ``deadline_ms`` (T-06/T-07).
    """

    def __init__(self, *, url: str, executor: DeadlineExecutor,
                 token: str | None = None,
                 call_fn: Callable[
                     [str, str, str, dict[str, Any]],
                     Awaitable[dict[str, Any]]] | None = None,
                 deadline_s: float = 6.0) -> None:
        self._url = url
        self._executor = executor
        self._token = token
        self._call_fn = call_fn
        self._deadline_s = deadline_s

    def _run_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """One tool call under the deadline; raises on failure (callers
        classify), returns the parsed payload otherwise. A caller-supplied
        ``deadline_ms`` is honored verbatim (T-07 spans multi-leg callers);
        otherwise the remaining budget is derived from the constructor
        deadline."""
        started = time.monotonic()
        arguments = dict(arguments)
        arguments.setdefault("client_protocol", CLIENT_PROTOCOL_MAJOR)
        if arguments.get("deadline_ms") is None:
            arguments["deadline_ms"] = _remaining_deadline_ms(
                self._deadline_s, started)
        operation = _McpToolOperation(
            url=self._url, explicit_token=self._token, call_fn=self._call_fn,
            tool=tool, arguments=arguments)
        return self._executor.run(operation, self._deadline_s)

    def _envelope_call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Envelope-tool call with the two-reason failure split."""
        try:
            payload = self._run_tool(tool, arguments)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: %s failed open: %s", tool, exc)
            if _is_unreachable_exception(exc):
                return _unreachable_envelope()
            return _empty_envelope()
        if payload is None or not isinstance(payload, dict):
            logger.debug("zmem transport: %s returned no envelope", tool)
            return _unreachable_envelope()
        return _coerce_envelope(payload)

    def prefetch(self, query: str, *, namespace: str, session_id: str,
                 moment: str, ops_tokens: list[str],
                 lane: str = "hermes-provider",
                 client_protocol: int = CLIENT_PROTOCOL_MAJOR,
                 deadline_ms: int | None = None,
                 reserve: bool = False) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "query": query,
            "namespace": namespace,
            "session_id": session_id,
            "moment": moment,
            "lane": lane,
            "ops_tokens": list(ops_tokens),
            "client_protocol": client_protocol,
            "deadline_ms": deadline_ms,
            "reserve": reserve,
        }
        return self._envelope_call("prefetch", arguments)

    def session_start(self, namespace: str, session_id: str,
                      lane: str = "hermes-provider",
                      client_protocol: int = CLIENT_PROTOCOL_MAJOR,
                      deadline_ms: int | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "namespace": namespace,
            "session_id": session_id,
            "lane": lane,
            "client_protocol": client_protocol,
            "deadline_ms": deadline_ms,
        }
        return self._envelope_call("session_start", arguments)

    def hello(self, client_protocol: int = CLIENT_PROTOCOL_MAJOR,
              deadline_ms: int | None = None) -> dict[str, Any]:
        """T-06 handshake (protocol majors, schema, profiles, capabilities).

        Never used as a pre-flight gate in the broker hot path — a failed
        hello must not block the moment's own call.
        """
        try:
            payload = self._run_tool("hello", {
                "client_protocol": client_protocol,
                "deadline_ms": deadline_ms,
            })
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: hello failed open: %s", exc)
            return {"ok": False, "reason": _failure_reason(exc)}
        if not isinstance(payload, dict):
            return {"ok": False, "reason": REASON_EMPTY_POOL}
        return payload

    def prefetch_ack(self, reservation: str,
                     client_protocol: int = CLIENT_PROTOCOL_MAJOR,
                     deadline_ms: int | None = None) -> dict[str, Any]:
        """T-05 delivery ack; fire-and-forget callers ignore the result."""
        try:
            payload = self._run_tool("prefetch_ack", {
                "reservation": reservation,
                "client_protocol": client_protocol,
                "deadline_ms": deadline_ms,
            })
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: prefetch_ack failed open: %s", exc)
            return {"ok": False, "reason": _failure_reason(exc)}
        return payload if isinstance(payload, dict) else {
            "ok": False, "reason": REASON_EMPTY_POOL}

    def stop_evidence(self, namespace: str, session_id: str | None = None,
                      agent_id: str | None = None, count: int = 0,
                      details: list | None = None,
                      client_protocol: int = CLIENT_PROTOCOL_MAJOR,
                      deadline_ms: int | None = None) -> dict[str, Any]:
        """Issue #287 AC2/AC3: send bounded, redacted, path-free Stop /
        SubagentStop failure evidence to the hub's ``stop_evidence`` tool.

        The first argument is deliberately POSITIONAL in the broker call
        (``http.stop_evidence(namespace, session_id=..., ...)``).
        """
        arguments: dict[str, Any] = {
            "namespace": namespace,
            "session_id": session_id or "",
            "count": count,
            "details": list(details or []),
            "client_protocol": client_protocol,
            "deadline_ms": deadline_ms,
        }
        if agent_id:
            arguments["agent_id"] = agent_id
        try:
            payload = self._run_tool("stop_evidence", arguments)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: stop_evidence failed open: %s", exc)
            return {"ok": False, "reason": _failure_reason(exc)}
        return payload if isinstance(payload, dict) else {
            "ok": False, "reason": REASON_EMPTY_POOL}

    def tier0_get(self, client_protocol: int = CLIENT_PROTOCOL_MAJOR,
                  deadline_ms: int | None = None) -> dict[str, Any]:
        """Issue #287: fetch the hub-owned canonical user-level core.md
        (content + revision) for the broker's Tier-0 cache."""
        try:
            payload = self._run_tool("tier0_get", {
                "client_protocol": client_protocol,
                "deadline_ms": deadline_ms,
            })
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: tier0_get failed open: %s", exc)
            return {"ok": False, "reason": _failure_reason(exc)}
        return payload if isinstance(payload, dict) else {
            "ok": False, "reason": REASON_EMPTY_POOL}


class LocalSubprocess:
    """One ``store.py prefetch`` subprocess per call, under the deadline."""

    def __init__(self, *, store_py: str, executor: DeadlineExecutor,
                 deadline_s: float = 6.0) -> None:
        self._store_py = store_py
        self._executor = executor
        self._deadline_s = deadline_s

    def prefetch(self, query: str, *, namespace: str, session_id: str,
                 moment: str, ops_tokens: list[str],
                 lane: str = "hermes-provider") -> dict[str, Any]:
        argv = [sys.executable or "python", self._store_py, "prefetch"]
        argv.extend(_query_args(query))
        argv.extend(["--namespace", namespace, "--session-id", session_id,
                     "--moment", moment, "--lane", lane])
        for token in ops_tokens:
            argv.extend(["--ops-token", token])
        argv.append("--json")
        try:
            child_env = os.environ.copy()
            child_env.setdefault("ZMEM_CAPTURE", "1")
            stdout = self._executor.run(
                _LocalOperation(argv, child_env), self._deadline_s)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - fail open
            logger.debug("zmem transport: local prefetch failed open: %s", exc)
            return _empty_envelope()
        if stdout is None or not stdout.strip():
            logger.debug("zmem transport: local prefetch returned no output")
            return _empty_envelope()
        try:
            payload = json.loads(stdout)
        except ValueError:
            logger.debug("zmem transport: local prefetch stdout was not JSON")
            return _empty_envelope()
        return _coerce_envelope(payload)


def tier0_revision(content: str) -> str:
    """Stable revision token for a cached Tier-0 core.md body."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
