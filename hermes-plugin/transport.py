"""Provider transport abstraction — issue #160 (Workstream H, PR 3 of 8).

One module, three responsibilities:

- Mode selection: :func:`resolve_transport_mode` decides, from configuration
  only (environment + filesystem), whether the Hermes provider runs a local
  ``store.py`` subprocess or an MCP-over-HTTP transport.  It never opens a
  socket and never spawns a process.
- Deadline: :class:`DeadlineExecutor` runs every transport operation in a
  daemon worker under ``ZMEM_HERMES_DEADLINE_S`` (default 6.0, always below
  the host manager's 8.0 s external-provider join).  A deadline hit cancels
  the operation (child kill / coroutine cancel) and joins the worker before
  returning, so no side effect can land after the provider returns.
- Transports: :class:`LocalSubprocess` and :class:`McpHttp` both return the
  COMPLETE parsed #158/#159 selector envelope.  Every failure class — nonzero
  exit, empty stdout, invalid JSON, a missing ``rendered`` field, or the
  deadline — returns the full envelope with ``results=[]``, ``count=0``,
  ``rendered=""``, ``reason="empty-pool"`` and zero numeric fields, so the
  provider merely extracts ``rendered`` and fails open.

The transport never imports ``storelib``, opens SQLite, or reads/writes the
delivery ledger; the store subprocess owns all of those.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TypeVar

T = TypeVar("T")

_STORE_PY_REL = Path("skills") / "memory" / "scripts" / "store.py"
_DEADLINE_ENV = "ZMEM_HERMES_DEADLINE_S"
_DEADLINE_DEFAULT_S = 6.0
_DEADLINE_MAX_S = 8.0
_POST_CANCEL_GRACE_S = 1.0
_INVALID_DEADLINE_WARNING = (
    "transport: invalid ZMEM_HERMES_DEADLINE_S; using 6.0\n")
_INVALID_TOKEN_FILE_WARNING = "transport: invalid token file\n"

# The closed #158/#159 envelope contract (mirrors storelib/inject.py:45-52):
# 14 required keys, three optional pass-through keys.  The transports return
# exactly this dict on success and the same shape zeroed on every failure.
_ENVELOPE_REQUIRED_KEYS = (
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
)


class TransportMode(str, Enum):
    """How the Hermes provider reaches the store: local subprocess or MCP."""

    local = "local"
    mcp = "mcp"


def _resolve_zmem_home() -> Optional[Path]:
    """Locate the zmem checkout; mirrors ``hermes-plugin/__init__.py``.

    ``ZMEM_HOME`` expanded and, when it names an existing directory,
    authoritative — a directory that lacks ``skills/memory/scripts/store.py``
    yields no local store with NO in-tree fallback (a remote-only box must
    not accidentally resolve the plugin's own checkout).  A ``ZMEM_HOME``
    that does not name a directory falls through to the in-tree checkout
    that ships this plugin, exactly like the provider's own resolver.
    """
    raw = os.environ.get("ZMEM_HOME", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate
    root = Path(__file__).resolve().parent.parent
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
    """Select the provider transport from configuration only.

    Precedence: an explicit ``ZMEM_HERMES_MODE`` wins (``local`` requires a
    resolvable local store and never falls back to MCP; ``mcp`` is taken at
    face value; anything else is invalid).  Without an explicit mode a
    nonempty ``ZMEM_MCP_URL`` selects MCP; otherwise a resolvable local
    ``store.py`` selects local; otherwise nothing is available.

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
    if _resolve_store_py() is not None:
        return TransportMode.local, "mode=local (auto)"
    return None, "mode=none unavailable"


def resolve_deadline_s(env: Mapping[str, str] | None = None) -> float:
    """Resolve ``ZMEM_HERMES_DEADLINE_S`` with the 6.0 default.

    Unset/empty → 6.0 silently.  Nonnumeric, nonfinite, zero, negative, and
    ``>= 8.0`` values all resolve to 6.0 and emit exactly one stderr warning
    line per resolution (the manager joins external providers at 8.0 s, so a
    deadline at or above it could never complete in time).
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


def _empty_envelope() -> dict[str, Any]:
    """The full envelope shape returned on every transport failure class."""
    return {
        "results": [],
        "count": 0,
        "omitted": 0,
        "reason": "empty-pool",
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

    def __init__(self, cmd: list[str]):
        self._cmd = cmd
        self._proc: Optional[subprocess.Popen] = None

    def __call__(self) -> str:
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
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
    """Load ``server/mcp_client.py`` (stdlib-only) and return its ``_call``."""
    path = Path(__file__).resolve().parent / "server" / "mcp_client.py"
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


class _McpPrefetchOperation:
    """One MCP ``prefetch`` call: token resolution, the HTTP call, and the
    coroutine's private loop, with a deadline-driven ``cancel`` hook."""

    def __init__(self, *, url: str, explicit_token: str | None,
                 call_fn: Callable[
                     [str, str, str, dict[str, Any]],
                     Awaitable[dict[str, Any]]] | None,
                 query: str, namespace: str, session_id: str, moment: str,
                 ops_tokens: list[str], lane: str):
        self._url = url
        self._explicit_token = explicit_token
        self._call_fn = call_fn
        self._arguments = {
            "query": query,
            "namespace": namespace,
            "session_id": session_id,
            "moment": moment,
            "lane": lane,
            "ops_tokens": list(ops_tokens),
        }
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
                call(self._url, token, "prefetch", self._arguments))
            return self._loop.run_until_complete(self._task)
        finally:
            self._loop.close()
            self._loop = None

    def cancel(self) -> None:
        task, loop = self._task, self._loop
        if task is not None and not task.done() and loop is not None:
            loop.call_soon_threadsafe(task.cancel)


class McpHttp:
    """The MCP ``prefetch`` tool over HTTP; no network before the operation."""

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

    def prefetch(self, query: str, *, namespace: str, session_id: str,
                 moment: str, ops_tokens: list[str],
                 lane: str = "hermes-provider") -> dict[str, Any]:
        operation = _McpPrefetchOperation(
            url=self._url, explicit_token=self._token, call_fn=self._call_fn,
            query=query, namespace=namespace, session_id=session_id,
            moment=moment, ops_tokens=ops_tokens, lane=lane)
        try:
            payload = self._executor.run(operation, self._deadline_s)
        except Exception:  # noqa: BLE001 - ImportError/network/JSON all fail open
            return _empty_envelope()
        if payload is None or not isinstance(payload, dict):
            return _empty_envelope()
        return _coerce_envelope(payload)
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
            stdout = self._executor.run(
                _LocalOperation(argv), self._deadline_s)
        except Exception:  # noqa: BLE001 - every failure class is fail-open
            return _empty_envelope()
        if stdout is None or not stdout.strip():
            return _empty_envelope()
        try:
            payload = json.loads(stdout)
        except ValueError:
            return _empty_envelope()
        return _coerce_envelope(payload)

