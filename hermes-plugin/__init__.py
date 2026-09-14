"""ZMem memory provider for Hermes Agent.

Bridges Hermes to the local-first ZMem cross-session memory store
(``~/.zmem/store.sqlite``) by shelling out to the ZMem ``store.py`` CLI. One
store, one schema, one code path — shared across Hermes, ZCode, Claude Code,
and Codex.

Surface 1 of the integration (passive recall + explicit memory tools + Tier-0
``core.md`` injection). The reflection loop is Surface 2 — three standalone
Python shell hooks under ``hooks/``. The network-access surface for a remote
Hermes is Surface 3 — the MCP server under ``server/``.

Install: drop this directory into ``~/.hermes/plugins/memory/zmem/`` (or set
``ZMEM_HOME`` to point at a zmem checkout). The provider auto-detects
``store.py`` relative to its own location when shipped inside the zmem repo,
so ``ZMEM_HOME`` is optional for a standalone install.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider
except ModuleNotFoundError as _exc:
    # Only rewrap when the missing module is agent/agent.memory_provider itself.
    # A ModuleNotFoundError raised TRANSITIVELY (a broken sub-dependency inside
    # agent.memory_provider) carries a different `.name` and must propagate with
    # its original traceback so the operator sees the real cause — wrapping it
    # as "host module missing" here would misreport and hide it (PRR-003).
    if _exc.name not in {"agent", "agent.memory_provider"}:
        raise
    raise ImportError(
        "zmem: 'agent.memory_provider' could not be imported. It is provided by "
        "the Hermes host runtime (it is NOT a pip package and is intentionally "
        "not declared in requirements.txt), so this provider can only be loaded "
        "inside a Hermes host process that places it on sys.path. If you are "
        "seeing this outside Hermes, this import is expected to fail."
    ) from None

logger = logging.getLogger(__name__)


# -- constants ---------------------------------------------------------------

# Path to store.py relative to the zmem checkout root.
_STORE_PY_REL = Path("skills") / "memory" / "scripts" / "store.py"
# Path to core.md (Tier-0) relative to the zmem data dir.
_CORE_MD_REL = Path("core.md")
# Subprocess cap for store.py calls — recall is fast; this is a safety net.
_STORE_TIMEOUT_S = 20
# Max recall results surfaced by prefetch (keeps context lean).
_PREFETCH_LIMIT = 5
# Max chars of a query passed to store.py recall.
_MAX_QUERY_CHARS = 500


def _resolve_zmem_home() -> Optional[Path]:
    """Resolve the zmem checkout root.

    Tries, in order:
      1. ``ZMEM_HOME`` env var (explicit operator override)
      2. The zmem repo root inferred from THIS file's location — when the
         plugin ships inside the zmem checkout (``hermes-plugin/`` is a
         sibling of ``skills/``), the repo root is two levels up from this
         file. This is the common case for a standalone install and means
         ``ZMEM_HOME`` is NOT required.

    Returns ``None`` when neither resolves so :meth:`is_available` can return
    ``False`` with a clear message rather than crashing agent init.
    """
    raw = os.environ.get("ZMEM_HOME", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
    # In-tree fallback: this file is at <repo>/hermes-plugin/__init__.py,
    # so the repo root is two parents up.
    here = Path(__file__).resolve().parent
    candidate_root = here.parent
    if (candidate_root / _STORE_PY_REL).is_file():
        return candidate_root
    return None


def _resolve_store_py() -> Optional[Path]:
    """Locate ``store.py``. None if not found."""
    home = _resolve_zmem_home()
    if home is None:
        return None
    candidate = home / _STORE_PY_REL
    return candidate if candidate.is_file() else None


def _fallback_store_path() -> Path:
    """Match storelib.schema's inline path chain when host.py is unavailable."""
    explicit = os.environ.get("ZMEM_STORE")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data) / "store.sqlite"
    home = Path(os.path.expanduser("~"))
    plugin_data_pattern = home / ".zcode" / "cli" / "plugins" / "data"
    try:
        if plugin_data_pattern.is_dir():
            for directory in plugin_data_pattern.iterdir():
                if "zmem" in directory.name.lower():
                    return directory / "store.sqlite"
    except OSError:
        pass
    return home / ".zcode" / "memory" / "store.sqlite"


def _resolve_store_data_dir() -> Path:
    """Resolve the zmem data dir holding ``store.sqlite`` and ``core.md``.

    Delegates to zmem's own ``host.py`` so the provider's view NEVER diverges
    from the ``store.py`` subprocess it shells out to. host.py honors the full
    chain: ``ZMEM_STORE`` > ``ZMEM_DATA`` > ``CLAUDE_PLUGIN_DATA`` >
    ``ZCODE_PLUGIN_DATA`` > ``~/.zmem`` (+ legacy). Previously this was
    reimplemented with a truncated chain (only ``ZMEM_DATA`` > ``~/.zmem``),
    which silently broke core.md injection, init gating, and backup_paths on
    boxes where ``ZMEM_STORE`` / plugin-data env vars are set.
    """
    try:
        return _host().resolve_store_path().parent
    except Exception as exc:
        # host.py absent/broken, or a module-name collision in sys.modules.
        # Fall back to storelib.schema's dependency-free legacy chain so the
        # provider degrades without pointing telemetry at a different store.
        fallback = _fallback_store_path()
        logger.warning("zmem: host.py resolution failed (%s); falling back to %s", exc, fallback)
        return fallback.parent


def _resolve_core_md() -> Path:
    """Resolve core.md via host.py (honors ZMEM_CORE_MD + store-path parent)."""
    try:
        return _host().resolve_core_md_path()
    except Exception:
        return _resolve_store_data_dir() / _CORE_MD_REL


def _host():
    """Lazily import zmem's host.py from the resolved checkout.

    Uses ``spec_from_file_location`` with a unique module name (``zmem_host``)
    rather than polluting ``sys.path`` with ``import host`` — in a long-lived
    agent process another plugin/module named ``host`` could already occupy
    that sys.modules slot, silently returning the wrong module. The file-path
    import is collision-proof.
    """
    import importlib.util
    home = _resolve_zmem_home()
    if home is None:
        raise RuntimeError("ZMEM_HOME not resolved")
    host_path = home / "skills" / "memory" / "scripts" / "host.py"
    if not host_path.is_file():
        raise RuntimeError(f"host.py not found at {host_path}")
    spec = importlib.util.spec_from_file_location("zmem_host", host_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load host.py spec from {host_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Lazily-resolved write-path constants, imported once from the dependency-free,
# side-effect-free schema_meta module (the SAME source of truth store.py uses).
# Re-resolved on each call so a test that repoints ZMEM_HOME picks up the move;
# returns module-level Python defaults if the module can't be located (so the
# provider degrades rather than crashing agent init — the values are stable
# enough that a stale local copy is safer than a hard failure here).
_STORE_CONSTANTS = {
    "ALLOWED_SIGNALS": ("test", "compile", "lint", "reviewer", "user", "none"),
    "ALLOWED_TYPES": ("fact", "lesson", "convention", "preference", "decision", "constraint"),
    "ALLOWED_TAINTS": ("trusted_internal", "untrusted_tool", "untrusted_web"),
    "MAX_CONTENT_CHARS": 65536,
    # issue #87 / #85 direction 1: closed reason set for silent injects (the
    # session_start twin classifies with the SAME tuple the hook body uses).
    # Issue #153: closed decision-log vocabularies.  Keep this fallback
    # byte-identical with schema_meta/inject.py so an in-tree provider with a
    # temporarily unreachable skills checkout still emits compatible logs.
    "INJECT_LANES": ("claude", "codex", "zcode", "hermes-provider", "hermes-compat"),
    "INJECT_MOMENTS": ("session_start", "user_prompt", "pretool", "subagent", "precompact"),
    "INJECT_SILENT_REASONS": (
        "empty-pool", "omitted", "below-bar", "budget-drop",
        "below-relevance", "already-delivered", "expired",
    ),
    "INJECT_REASON_INJECTED": "injected",
    # issue #110 (P0-5): kill-switch reason, written only by the
    # ZMEM_INJECT=0 short-circuit (never by classification).
    "INJECT_REASON_DISABLED": "disabled",
}


def _store_constants() -> Dict[str, Any]:
    """Best-effort load of ALLOWED_SIGNALS / ALLOWED_TYPES / MAX_CONTENT_CHARS
    from ``schema_meta`` (the single source of truth shared with store.py).

    Importing store.py itself just to read three constants is risky — it is a
    ~250 KB CLI module with env-var reads and embedding/sqlite side effects at
    import time. ``schema_meta`` is deliberately tiny and dependency-free so it
    imports with no side effects. Falls back to the module-level defaults above
    if the file can't be located, and logs the divergence (#37 L7/L8: keeps the
    local Hermes validation in lock-step with the MCP and CLI paths without
    re-typing the literals).
    """
    try:
        import importlib.util
        home = _resolve_zmem_home()
        if home is None:
            return dict(_STORE_CONSTANTS)
        meta_path = home / "skills" / "memory" / "scripts" / "schema_meta.py"
        if not meta_path.is_file():
            return dict(_STORE_CONSTANTS)
        spec = importlib.util.spec_from_file_location("zmem_schema_meta", meta_path)
        if spec is None or spec.loader is None:
            return dict(_STORE_CONSTANTS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {
            "ALLOWED_SIGNALS": getattr(mod, "ALLOWED_SIGNALS", _STORE_CONSTANTS["ALLOWED_SIGNALS"]),
            "ALLOWED_TYPES": getattr(mod, "ALLOWED_TYPES", _STORE_CONSTANTS["ALLOWED_TYPES"]),
            "ALLOWED_TAINTS": getattr(mod, "ALLOWED_TAINTS", _STORE_CONSTANTS["ALLOWED_TAINTS"]),
            "MAX_CONTENT_CHARS": getattr(mod, "MAX_CONTENT_CHARS", _STORE_CONSTANTS["MAX_CONTENT_CHARS"]),
            "INJECT_LANES": tuple(getattr(mod, "INJECT_LANES", _STORE_CONSTANTS["INJECT_LANES"])),
            "INJECT_MOMENTS": tuple(getattr(mod, "INJECT_MOMENTS", _STORE_CONSTANTS["INJECT_MOMENTS"])),
            "INJECT_SILENT_REASONS": tuple(getattr(mod, "INJECT_SILENT_REASONS", _STORE_CONSTANTS["INJECT_SILENT_REASONS"])),
            "INJECT_REASON_INJECTED": getattr(mod, "INJECT_REASON_INJECTED", _STORE_CONSTANTS["INJECT_REASON_INJECTED"]),
            "INJECT_REASON_DISABLED": getattr(mod, "INJECT_REASON_DISABLED", _STORE_CONSTANTS["INJECT_REASON_DISABLED"]),
        }
    except Exception as exc:
        logger.debug("zmem: schema_meta constants load failed (%s); using defaults", exc)
        return dict(_STORE_CONSTANTS)


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _release_version() -> Optional[str]:
    """Return the validated release version used by decision attribution.

    The provider is deliberately fail-open: a missing or malformed manifest
    keeps the decision audit trail in its complete legacy shape instead of
    writing a partially-attributed line.  The in-tree manifest is preferred,
    while ``ZMEM_HOME`` supports a copied plugin installation.
    """
    candidates = [Path(__file__).resolve().parent.parent / "release-manifest.json"]
    home = _resolve_zmem_home()
    if home is not None:
        candidates.append(home / "release-manifest.json")
    for path in candidates:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            version = obj.get("version") if isinstance(obj, dict) else None
            if isinstance(version, str) and _SEMVER_RE.fullmatch(version):
                return version
        except (OSError, ValueError, TypeError):
            continue
    return None


def _elapsed_ms(start: float, end: Optional[float] = None) -> int:
    """Round one local operation duration to a non-negative millisecond value."""
    stop = time.perf_counter() if end is None else end
    return max(0, int(round((stop - start) * 1000)))


def _decision_sid(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))[:128]
    return safe or "unknown"


def _rotate_decision_log(data_dir: Path) -> None:
    """Rotate the decision log before append, preserving partial deployments."""
    try:
        store_py = _resolve_store_py()
        if store_py is None:
            return
        saved = sys.path[:]
        try:
            sys.path.insert(0, str(Path(store_py).resolve().parent))
            from storelib.log_rotate import rotate_on_append
            rotate_on_append(str(data_dir / "zmem-decisions.log"))
        finally:
            sys.path[:] = saved
    except (Exception, SystemExit):
        # Rotation is best effort: a missing storelib must not lose telemetry.
        pass


def _append_session_decision(*, status: str, reason: str, ids: list[Any],
                             all_ids: list[Any], omitted: int = 0,
                             excluded: int = 0, session_id: str = "",
                             moment: str = "session_start",
                             lane: Optional[str] = None,
                             t_ms: int = 0) -> None:
    """Append the local Hermes SessionStart decision line, fail-open.

    ``lane``, ``ver`` and ``t_ms`` are one atomic attribution suffix.  If the
    release manifest cannot be validated, all three are omitted so a legacy
    parser never sees a half-enriched line.  The stable prefix and additive
    tail match the shared hook writer's wire order.
    """
    try:
        data_dir = _resolve_store_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        version = _release_version()
        attrib = ""
        if version:
            consts = _store_constants()
            # ``lane`` is optional for compatibility callers.  A valid
            # version/timing pair may still enrich a lane-less line; only an
            # explicit invalid lane suppresses the atomic suffix (fixed local
            # provider calls always use hermes-provider).
            if lane is None or lane in consts["INJECT_LANES"]:
                lane_f = f" lane={lane}" if lane is not None else ""
                attrib = f"{lane_f} ver={version} t_ms={max(0, int(t_ms))}"
        omitted_f = f" omitted={int(omitted)}" if omitted and omitted > 0 else ""
        exc_f = f" exc={int(excluded)}" if excluded and excluded > 0 else ""
        safe_moment = re.sub(r"[^A-Za-z0-9._-]", "_", str(moment or ""))[:32]
        moment_f = f" moment={safe_moment}" if safe_moment else ""
        line = (
            f"[{int(time.time())}] zmem-hook status={status} reason={reason}"
            f"{omitted_f} ids={list(ids)} all={list(all_ids)}{exc_f}"
            f" sid={_decision_sid(session_id)}{moment_f}{attrib}\n"
        )
        _rotate_decision_log(data_dir)
        with (data_dir / "zmem-decisions.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # pragma: no cover - telemetry must not break tools
        logger.debug("zmem: Hermes decision-log append failed: %s", exc)


def _python_bin() -> str:
    """Python interpreter for store.py subprocess. Prefer the current one."""
    return sys.executable or "python"


def _inject_disabled() -> bool:
    """Issue #110 (P0-5): ZMEM_INJECT=0 disables every passive-injection
    surface of this provider (prefetch, the session_start tool twin, and the
    system-prompt Tier-0 block). Capture paths never consult it. Only the
    literal ``0`` (whitespace-tolerated) disables — the ZMEM_QUERY_CONTEXT
    kill-switch convention; ``false``/``no``/empty keep injection enabled."""
    return os.environ.get("ZMEM_INJECT", "1").strip() == "0"


def _decode_rendered_envelope(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a store-owned passive envelope only when ``rendered`` is text.

    Passive consumers deliberately do not interpret candidate rows, budgets, or
    fence syntax.  The store subprocess owns those details and this adapter
    accepts only its complete, already-rendered envelope.  Any malformed or
    mixed-version response is silent and fail-open.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    stdout = (result.get("stdout") or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("rendered"), str):
        return None
    return payload


def _passive_store_args(
    command: str,
    *,
    query: str,
    namespace: str,
    limit: int,
    global_limit: int,
    session_id: str,
    moment: str,
    lane: str,
) -> List[str]:
    """Build one store-owned passive-injection subprocess invocation."""
    args = [command]
    if command == "recall":
        args.extend(["--query", query])
    args.extend([
        "--limit", str(limit),
        "--include-global", "--global-limit", str(global_limit),
        "--no-bump", "--for-injection", "--json",
        "--session-id", session_id,
        "--moment", moment,
        "--lane", lane,
        "--namespace", namespace,
    ])
    return args


def _run_passive_store(
    args: List[str], timing: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Run a passive command without allowing adapter failures to escape.

    ``timing`` (issue #153) is passed through to ``_run_store`` so the one
    store attempt behind a decision line can carry its measured ``t_ms``.
    """
    try:
        result = _run_store(args, timing=timing)
    except Exception as exc:  # pragma: no cover - defensive seam for hosts
        logger.debug("zmem passive store call failed (%s)", exc)
        return {"ok": False, "stdout": "", "stderr": "", "returncode": 1}
    return result if isinstance(result, dict) else {
        "ok": False, "stdout": "", "stderr": "", "returncode": 1,
    }


# -- subprocess helper -------------------------------------------------------

# PR-review PRR-P (issue #59 review round): Windows CreateProcess argv caps
# near 32k chars while the store's content cap is MAX_CONTENT_CHARS (65536).
# Content longer than this threshold is piped via stdin (`--content -`)
# instead of an argv element, so large-but-valid payloads never hit
# WinError 206 on Windows-primary hosts.
_ARGV_SAFE_CONTENT_CHARS = 30000


def _sanitize_store_error(r: Dict[str, Any], limit: int = 200) -> str:
    """PR-review PRR-M (issue #59 review round): classify + truncate a
    store.py failure for return to a REMOTE client. Known refusals (the
    ``[zmem] …`` stable-error lines) pass through verbatim — they ARE the
    contract; anything else (unexpected tracebacks, argparse blobs, advisory
    text) is collapsed and truncated so raw stderr never leaks wholesale."""
    text = (r.get("stderr") or r.get("stdout") or "").strip()
    if not text:
        return "store command failed (no diagnostic)"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    zmem = [ln for ln in lines if ln.startswith("[zmem]")]
    chosen = " ".join(zmem if zmem else lines)
    if len(chosen) > limit:
        chosen = chosen[: limit - 3].rstrip() + "..."
    return chosen


def _run_store(
    args: List[str], input_text: str | None = None,
    timing: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Run ``store.py <args>`` and return ``{ok, stdout, stderr, returncode}``.

    Always returns a dict (never raises) — memory must fail-open. The caller
    decides whether a non-zero returncode is fatal. ``input_text`` (optional)
    is piped to the child's stdin — used for oversize content (see
    ``_ARGV_SAFE_CONTENT_CHARS``).
    """
    store_py = _resolve_store_py()
    if store_py is None:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "store.py not found (ZMEM_HOME unset or wrong path)",
            "returncode": 127,
        }
    cmd = [_python_bin(), str(store_py), *args]
    # Start at the exact subprocess boundary.  Path resolution is outside this
    # interval, matching the MCP server's attributed timing contract.
    started = time.perf_counter()

    def _record_timing() -> None:
        if timing is not None:
            timing["t_ms"] = _elapsed_ms(started)

    try:
        proc = subprocess.run(  # noqa: S603 — argv is constructed, not shell
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STORE_TIMEOUT_S,
        )
        _record_timing()
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        _record_timing()
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"store.py timed out after {_STORE_TIMEOUT_S}s",
            "returncode": 124,
        }
    except Exception as exc:  # pragma: no cover — defensive
        _record_timing()
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"store.py failed: {exc}",
            "returncode": 1,
        }


# -- tool schemas ------------------------------------------------------------

_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "zmem_search",
    "description": (
        "Semantic + full-text search of cross-session memory (lessons, "
        "conventions, facts, preferences shared across Hermes, ZCode, Claude "
        "Code, and Codex). Use before answering anything that may depend on "
        "past work, decisions, or gotchas. Vary the wording and re-search for "
        "multi-part questions. Defaults to your session's namespace; pass "
        "namespace='*' to search across all namespaces (store-wide)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for (a sentence or keyword works).",
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Scope (default: your session's namespace). Pass '*' "
                    "to search across all namespaces (store-wide); pass a "
                    "specific namespace (e.g. 'project:repo') to scope to "
                    "that tier (cross-project lessons from user:global are "
                    "still surfaced alongside, up to 3)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 5, hard-max 50).",
            },
        },
        "required": ["query"],
    },
}

# Source the type/signal enums from schema_meta (via the same loader _tool_add
# uses) so the tool SCHEMA the agent sees and the runtime VALIDATION share one
# source of truth — previously this was a 5th hard-coded copy of the enums that
# bypassed schema_meta entirely (PRR-014). The schema snapshot is taken once at
# import (MCP tool schemas are static by contract), while _tool_add re-resolves
# per call so a test that repoints ZMEM_HOME picks up the move; in steady state
# (no mid-process ZMEM_HOME change) the two are identical.
_SCHEMA_CONSTANTS = _store_constants()
_ADD_TYPE_ENUM = list(_SCHEMA_CONSTANTS["ALLOWED_TYPES"])
_ADD_SIGNAL_ENUM = list(_SCHEMA_CONSTANTS["ALLOWED_SIGNALS"])
_ADD_TAINT_ENUM = list(_SCHEMA_CONSTANTS["ALLOWED_TAINTS"])

_ADD_SCHEMA: Dict[str, Any] = {
    "name": "zmem_add",
    "description": (
        "Capture a grounded lesson / convention / fact / preference to "
        "cross-session memory. Call this when you discover something reusable: "
        "a workaround, a project convention, a corrected assumption, a stable "
        "preference. Ground it with --signal when known (test > reviewer > "
        "user > none) so future sessions can weigh its reliability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": _ADD_TYPE_ENUM,
                "description": "Memory type.",
            },
            "content": {
                "type": "string",
                "description": "The memory, written to be useful out of context.",
            },
            "namespace": {
                "type": "string",
                "description": "Scope key (default: derived from session).",
            },
            "tags": {
                "type": "string",
                "description": "Comma-separated tags.",
            },
            "signal": {
                "type": "string",
                "enum": _ADD_SIGNAL_ENUM,
                "description": "How strongly grounded this memory is.",
            },
            "taint": {
                "type": "string",
                "enum": _ADD_TAINT_ENUM,
                "description": "Provenance/trust origin (issue #59, 4.7). "
                               "Default is 'untrusted_tool': this agent's "
                               "write is ungrounded self-opinion unless you "
                               "claim more. Use 'untrusted_web' for content "
                               "fetched from the web; 'trusted_internal' only "
                               "when a human/test/closeout grounded it.",
            },
            "source_ref": {
                "type": "string",
                "description": "Provenance (e.g. session:<id>).",
            },
        },
        "required": ["type", "content"],
    },
}

_SUPERSEDE_SCHEMA: Dict[str, Any] = {
    "name": "zmem_supersede",
    "description": (
        "Mark a stored memory obsolete (corrected, OBE, or wrong). Future "
        "recall skips superseded memories. Use when a new lesson contradicts "
        "an older one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The memory id returned by zmem_search.",
            },
            "reason": {
                "type": "string",
                "description": "Why it's obsolete.",
            },
        },
        "required": ["id"],
    },
}

_UPDATE_SCHEMA: Dict[str, Any] = {
    "name": "zmem_update",
    "description": (
        "Append-only update of a stored memory (issue #59, 4.2): replace its "
        "content (and optionally metadata) with a NEW live row, tombstone the "
        "old row (keeping full history), and link the new row back to the old "
        "via update_of. Point-in-time recall (--as-of) before the update still "
        "returns the OLD content; after returns the NEW. The id must be a LIVE "
        "memory (use zmem_search to find one); an unknown or already-superseded "
        "id is refused. Prefer this over add-then-supersede when revising a fact."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "id of the live memory to update.",
            },
            "content": {
                "type": "string",
                "description": "The new content (replaces the old row's content).",
            },
            "type": {
                "type": "string",
                "enum": _ADD_TYPE_ENUM,
                "description": "Override the memory type (default: inherit).",
            },
            "tags": {
                "type": "string",
                "description": "Override comma-separated tags (default: inherit).",
            },
            "source_ref": {
                "type": "string",
                "description": "Override provenance (default: inherit).",
            },
            "signal": {
                "type": "string",
                "enum": _ADD_SIGNAL_ENUM,
                "description": "Override grounding signal (default: inherit).",
            },
            "taint": {
                "type": "string",
                "enum": _ADD_TAINT_ENUM,
                "description": "Provenance/trust origin override (issue #59, "
                               "4.7). Default 'untrusted_tool'; the surviving "
                               "row keeps the WORST of this and the replaced "
                               "row's taint.",
            },
        },
        "required": ["id", "content"],
    },
}

_INVALIDATE_SCHEMA: Dict[str, Any] = {
    "name": "zmem_invalidate",
    "description": (
        "Tombstone a memory BECAUSE THE FACT IS NO LONGER TRUE, with a REQUIRED "
        "reason so the correction is auditable (issue #59, 4.3). Future recall "
        "skips it (history preserved). Prefer this over zmem_supersede when the "
        "old memory is wrong or obsolete — a contradiction correction; "
        "zmem_supersede remains for general tombstones with no reason."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "id of the live memory to invalidate.",
            },
            "reason": {
                "type": "string",
                "description": "Why the fact is no longer true (REQUIRED).",
            },
        },
        "required": ["id", "reason"],
    },
}

_SESSION_START_SCHEMA: Dict[str, Any] = {
    "name": "zmem_session_start",
    "description": (
        "Passive session prefetch (issue #65, 10.5 — MCP session_start twin). "
        "Returns a fenced, provenance-tagged context block of this session's "
        "namespace recent high-confidence memories. Never bumps "
        "retrieval_count (--no-bump), omits injection-risk and untrusted_web "
        "rows, and honors ZMEM_INJECT_TOKEN_BUDGET (decision/constraint rows "
        "are never dropped). Call once at session start; pair with "
        "zmem_session_end."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": (
                    "Scope (default: this session's namespace)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to consider (default 3, hard-max 50).",
            },
        },
        "required": [],
    },
}

_SESSION_END_SCHEMA: Dict[str, Any] = {
    "name": "zmem_session_end",
    "description": (
        "End-of-session pairing tool (issue #65, 10.5 — MCP session_end twin). "
        "Without a note it is a pure NO-WRITE acknowledgement (nothing stored, "
        "no organize/consolidate). With a note, exactly one memory row is "
        "written via the standard add path (type fact, signal none, taint "
        "untrusted_tool, capture-mode auto so secret redaction runs)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "Optional durable session note (omit for a no-write ack).",
            },
            "namespace": {
                "type": "string",
                "description": "Scope (default: this session's namespace).",
            },
        },
        "required": [],
    },
}

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _SEARCH_SCHEMA,
    _ADD_SCHEMA,
    _SUPERSEDE_SCHEMA,
    _UPDATE_SCHEMA,
    _INVALIDATE_SCHEMA,
    _SESSION_START_SCHEMA,
    _SESSION_END_SCHEMA,
]


def _tool_error(msg: str) -> str:
    """JSON error string for tool-call failures (mirrors tools.registry.tool_error)."""
    return json.dumps({"error": msg})


def _structured_write_response(r: Dict[str, Any], *, ok_result: str) -> str:
    """Shape an add/update tool response from store.py ``--json`` output.

    v13 (issue #65, 10.8): the CLI prints ``{"id", "result", "warnings"}``
    (structured warnings; redaction carries a count). A legacy non-JSON stdout
    (pre-v13 store.py) degrades to the old ``{"result", "raw"}`` shape.
    """
    if not r["ok"]:
        return _tool_error(f"{ok_result.capitalize()} failed: {_sanitize_store_error(r)}")
    stdout = (r["stdout"] or "").strip()
    parsed = None
    if stdout:
        try:
            maybe = json.loads(stdout)
            if isinstance(maybe, dict):
                parsed = maybe
        except json.JSONDecodeError:
            parsed = None
    if parsed is not None:
        resp: Dict[str, Any] = {"result": parsed.get("result", ok_result), "id": parsed.get("id")}
        if parsed.get("created_new") is not None:
            resp["created_new"] = parsed.get("created_new")
        if parsed.get("warnings"):
            resp["warnings"] = parsed.get("warnings")
        return json.dumps(resp)
    return json.dumps({"result": ok_result, "raw": stdout})


# -- provider ----------------------------------------------------------------

class ZmemMemoryProvider(MemoryProvider):
    """ZMem local-first memory — subprocess-bridges Hermes to ``store.py``."""

    def __init__(self) -> None:
        self._session_id: str = ""
        self._namespace: str = "user:global"
        self._initialized: bool = False

    @property
    def name(self) -> str:
        return "zmem"

    # -- core lifecycle -----------------------------------------------------

    def is_available(self) -> bool:
        """True iff ``ZMEM_HOME`` is set and points at a checkout with store.py.

        No subprocess, no network — pure file checks (per ABC contract).
        """
        store_py = _resolve_store_py()
        return store_py is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        self._namespace = self._resolve_namespace(**kwargs)

        # First-run safety: ensure the store exists. store.py init is idempotent
        # (CREATE TABLE IF NOT EXISTS). Only run if store.sqlite is absent so we
        # don't spawn a subprocess on every agent startup.
        store_sqlite = _resolve_store_data_dir() / "store.sqlite"
        if not store_sqlite.exists():
            r = _run_store(["init"])
            if not r["ok"]:
                logger.warning("zmem: store.py init failed: %s", r["stderr"])
        self._initialized = True

    def _resolve_namespace(self, **kwargs) -> str:
        """Namespace precedence: ZMEM_NAMESPACE env → user:<user_id> → user:global.

        Mirrors the mem0 user_id pattern. Gateway sessions (Telegram/Discord/
        Slack) have no cwd, so cwd-based project:* is meaningless here.
        """
        env_ns = os.environ.get("ZMEM_NAMESPACE", "").strip()
        if env_ns:
            return env_ns
        user_id = (kwargs.get("user_id") or "").strip()
        if user_id:
            return f"user:{user_id}"
        return "user:global"

    # -- recall -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """Inject Tier-0 ``core.md`` (stable rules) into the system prompt."""
        # Issue #110 (P0-5): the kill switch silences ALL passive context on
        # every surface — the bash SessionStart hook likewise suppresses its
        # Tier 0 under ZMEM_INJECT=0 — so the Hermes system-prompt Tier 0
        # goes quiet too (documented in README "Operations notes").
        if _inject_disabled():
            logger.info(
                "zmem system prompt tier0: status=silent "
                "reason=disabled (ZMEM_INJECT=0)")
            return ""
        try:
            core = _resolve_core_md()
            if core.is_file():
                text = core.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("zmem: core.md read failed: %s", exc)
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Passive recall before each turn from the store-rendered envelope.

        The MemoryManager runs external-provider prefetch in a background thread
        with a bounded join (``memory_manager.py``), so the subprocess cost is
        amortized — no need for queue_prefetch complexity here.  The provider
        intentionally has no local selection, ledger, budget, or render path.
        """
        # Issue #110 (P0-5): passive-injection kill switch — no store
        # subprocess, empty delivery, one log line carrying the marker.
        if _inject_disabled():
            logger.info(
                "zmem prefetch: status=silent reason=disabled (ZMEM_INJECT=0)")
            return ""
        q = (query or "").strip()[:_MAX_QUERY_CHARS]
        command = "recall" if q else "recent"
        sid = (session_id or self._session_id or "").strip()
        result = _run_passive_store(_passive_store_args(
            command,
            query=q,
            namespace=self._namespace,
            limit=_PREFETCH_LIMIT,
            global_limit=3,
            session_id=sid,
            moment="user_prompt",
            lane="hermes-provider",
        ))
        payload = _decode_rendered_envelope(result)
        if payload is None:
            logger.debug("zmem prefetch: missing or malformed rendered envelope")
            return ""
        return payload["rendered"]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op — the manager already background-caches external prefetch."""
        return None

    # -- tools --------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "zmem_search":
            return self._tool_search(args)
        if tool_name == "zmem_add":
            return self._tool_add(args)
        if tool_name == "zmem_supersede":
            return self._tool_supersede(args)
        if tool_name == "zmem_update":
            return self._tool_update(args)
        if tool_name == "zmem_invalidate":
            return self._tool_invalidate(args)
        if tool_name == "zmem_session_start":
            return self._tool_session_start(args)
        if tool_name == "zmem_session_end":
            return self._tool_session_end(args)
        return _tool_error(f"Unknown tool: {tool_name}")

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return _tool_error("Missing required parameter: query")
        limit = _clamp_limit(args.get("limit"))
        # Namespace: explicit arg wins; '*' means "search everything"; default
        # is the session namespace (per-user isolation on shared gateway boxes).
        ns_arg = (args.get("namespace") or "").strip()
        cli_args = [
            "recall",
            "--query", query[:_MAX_QUERY_CHARS],
            "--limit", str(limit),
            # PRR-007 fix (issue #58 3.3): search is keyword/lexical BY
            # CONTRACT on every surface (CLI search pins --no-hybrid);
            # without this the flipped hybrid default silently changed
            # this tool's semantics when embeddings are installed.
            "--no-hybrid",
            # v13 (issue #65, 10.4): pin the CLI search subcommand contract —
            # keyword-only AND never link-expanded (--link-hops 0), exactly
            # like the MCP search tool. Without this the recall default
            # (hops=1, budget=2) would append neighbor rows past --limit.
            "--link-hops", "0",
            "--json",
        ]
        if ns_arg and ns_arg != "*":
            cli_args += ["--namespace", ns_arg]
            # When scoped to a specific namespace, union the user:global tier
            # (parity with the CLI search subcommand and prefetch, issue #18).
            # The store treats it as a no-op when ns_arg is user:global.
            cli_args += ["--include-global", "--global-limit", "3"]
        elif not ns_arg:
            # Default to the session namespace (mirror prefetch/add isolation).
            cli_args += ["--namespace", self._namespace]
            cli_args += ["--include-global", "--global-limit", "3"]
        # Empty/ns_arg == '*' → no --namespace flag → store.py searches all
        # (unscoped already covers every namespace, so no --include-global).
        r = _run_store(cli_args)
        if not r["ok"]:
            return _tool_error(f"Search failed: {_sanitize_store_error(r)}")
        stdout = (r["stdout"] or "").strip()
        if not stdout:
            return json.dumps({"results": [], "count": 0})
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return _tool_error(f"Search returned non-JSON: {exc}")
        # Explicit search retains its public structured response.  Passive
        # injection never uses this compatibility path: it accepts only the
        # store-owned ``rendered`` field above.
        if isinstance(parsed, dict):
            raw_results = parsed.get("results", [])
            results = raw_results if isinstance(raw_results, list) else []
        elif isinstance(parsed, list):
            results = parsed
        else:
            results = []
        items = [
            {
                "id": it.get("id"),
                "type": it.get("type"),
                "content": it.get("content"),
                "confidence": it.get("confidence"),
                "tags": it.get("tags"),
                "source_ref": it.get("source_ref"),
                # Lineage + provenance trust travel with the row so the agent
                # can see whether a result is an update of another row or an
                # untrusted-origin note (issue #59, 4.2/4.7).
                "valid_from": it.get("valid_from"),
                "valid_until": it.get("valid_until"),
                "update_of": it.get("update_of"),
                "taint": it.get("taint"),
            }
            for it in results
            if isinstance(it, dict)
        ]
        return json.dumps({"results": items, "count": len(items)})

    def _tool_add(self, args: Dict[str, Any]) -> str:
        consts = _store_constants()
        mtype = (args.get("type") or "").strip()
        content = (args.get("content") or "").strip()
        if not mtype:
            return _tool_error("Missing required parameter: type")
        if not content:
            return _tool_error("Missing required parameter: content")
        if mtype not in consts["ALLOWED_TYPES"]:
            return _tool_error(
                "type must be one of: " + ", ".join(consts["ALLOWED_TYPES"])
            )
        # Reject oversize content at the boundary with a clean message, mirroring
        # the MCP path — without this the local Hermes path forwarded raw,
        # unclamped content to store.py and surfaced an opaque stderr blob on
        # the cap (#37 L8). Both paths now enforce the same MAX_CONTENT_CHARS.
        if len(content) > consts["MAX_CONTENT_CHARS"]:
            return _tool_error(
                f"content is {len(content)} chars, over the "
                f"{consts['MAX_CONTENT_CHARS']} limit"
            )
        ns = (args.get("namespace") or self._namespace).strip()
        signal = (args.get("signal") or "none").strip()
        # Validate --signal against the allowed enum at the boundary (mirrors the
        # MCP path) so an invalid signal gets a clean message instead of an opaque
        # store.py argparse `invalid choice` blob wrapped in "Add failed: ..."
        # (#37 L7).
        if signal not in consts["ALLOWED_SIGNALS"]:
            return _tool_error(
                "signal must be one of: " + ", ".join(consts["ALLOWED_SIGNALS"])
            )
        # Taint (issue #59, 4.7 / plan M5): the agent surface's default is
        # EXPLICIT untrusted_tool — an agent's write is ungrounded self-opinion
        # unless the caller claims more. An explicit taint (e.g. untrusted_web
        # for a web fetch) overrides; validated at the boundary for a clean
        # error message (mirrors the signal validation above, #37 L7).
        taint = (args.get("taint") or "").strip()
        if not taint:
            taint = "untrusted_tool"
        elif taint not in consts["ALLOWED_TAINTS"]:
            return _tool_error(
                "taint must be one of: " + ", ".join(consts["ALLOWED_TAINTS"])
            )
        tags = (args.get("tags") or "").strip()
        source_ref = (args.get("source_ref") or "").strip()
        if not source_ref and self._session_id:
            source_ref = f"session:{self._session_id}"
        # PR-review PRR-L (issue #59 review round): Hermes writes are agent
        # surface traffic — pass --capture-mode auto so secret-like content is
        # redacted exactly as the MCP add path does (#36 M4 parity).
        cli_args = [
            "add",
            "--namespace", ns,
            "--type", mtype,
            "--content", content,
            "--signal", signal,
            "--taint", taint,
            "--capture-mode", "auto",
            # v13 (issue #65, 10.8): structured write result — stdout is pure
            # JSON {id, result, warnings[]} so redaction warnings surface as
            # structured data (parity with the MCP add tool).
            "--json",
        ]
        if tags:
            cli_args += ["--tags", tags]
        if source_ref:
            cli_args += ["--source-ref", source_ref]
        # PR-review PRR-P: pipe oversize content via stdin (`--content -`) so
        # large-but-valid payloads never hit the Windows argv cap.
        input_text = None
        if len(content) > _ARGV_SAFE_CONTENT_CHARS or content == "-":
            # F8: pipe literal '-' via stdin so it is stored verbatim
            # instead of hitting the CLI stdin sentinel.
            cli_args[cli_args.index("--content") + 1] = "-"
            input_text = content
        r = _run_store(cli_args, input_text=input_text)
        # v13 (issue #65, 10.8): structured result + warnings from --json.
        return _structured_write_response(r, ok_result="stored")

    def _tool_supersede(self, args: Dict[str, Any]) -> str:
        mid = (args.get("id") or "").strip()
        if not mid:
            return _tool_error("Missing required parameter: id")
        reason = (args.get("reason") or "").strip()
        cli_args = ["supersede", "--id", mid]
        if reason:
            cli_args += ["--reason", reason]
        r = _run_store(cli_args)
        if not r["ok"]:
            return _tool_error(
                f"Supersede failed (id may not exist): {_sanitize_store_error(r)}"
            )
        return json.dumps({"result": "superseded", "id": mid})

    def _tool_update(self, args: Dict[str, Any]) -> str:
        """Append-only knowledge update (issue #59, 4.2). See _UPDATE_SCHEMA.

        Override params are validated at the boundary (clean error messages,
        mirroring _tool_add's #37 L7 pattern) before they reach store.py. The
        taint default/override rule matches _tool_add (plan M5): the agent
        surface defaults to EXPLICIT untrusted_tool; the store widens it to
        the worst-of with the replaced row's taint.
        """
        consts = _store_constants()
        mid = (args.get("id") or "").strip()
        content = (args.get("content") or "").strip()
        if not mid:
            return _tool_error("Missing required parameter: id")
        if not content:
            return _tool_error("Missing required parameter: content")
        if len(content) > consts["MAX_CONTENT_CHARS"]:
            return _tool_error(
                f"content is {len(content)} chars, over the "
                f"{consts['MAX_CONTENT_CHARS']} limit"
            )
        cli_args = ["update", "--id", mid, "--content", content]
        # v13 (issue #65, 10.3): optional namespace override — parity with the
        # CLI ``update --namespace`` and the MCP update tool. The replacement
        # row is re-keyed to the target namespace; empty means inherit.
        ns_override = (args.get("namespace") or "").strip()
        if ns_override:
            cli_args += ["--namespace", ns_override]
        mtype = (args.get("type") or "").strip()
        if mtype:
            if mtype not in consts["ALLOWED_TYPES"]:
                return _tool_error(
                    "type must be one of: " + ", ".join(consts["ALLOWED_TYPES"])
                )
            cli_args += ["--type", mtype]
        tags = (args.get("tags") or "").strip()
        if tags:
            cli_args += ["--tags", tags]
        source_ref = (args.get("source_ref") or "").strip()
        if source_ref:
            cli_args += ["--source-ref", source_ref]
        signal = (args.get("signal") or "").strip()
        if signal:
            if signal not in consts["ALLOWED_SIGNALS"]:
                return _tool_error(
                    "signal must be one of: " + ", ".join(consts["ALLOWED_SIGNALS"])
                )
            cli_args += ["--signal", signal]
        taint = (args.get("taint") or "").strip()
        if not taint:
            taint = "untrusted_tool"
        elif taint not in consts["ALLOWED_TAINTS"]:
            return _tool_error(
                "taint must be one of: " + ", ".join(consts["ALLOWED_TAINTS"])
            )
        cli_args += ["--taint", taint]
        # PR-review PRR-L: agent-surface update redacts secrets like MCP (#36
        # M4 parity). PR-review PRR-P: oversize content is piped via stdin.
        # v13 (issue #65, 10.8): --json for the structured write result.
        cli_args += ["--capture-mode", "auto", "--json"]
        input_text = None
        if len(content) > _ARGV_SAFE_CONTENT_CHARS or content == "-":
            # F8: see _tool_add — pipe literal '-' via stdin.
            cli_args[cli_args.index("--content") + 1] = "-"
            input_text = content
        r = _run_store(cli_args, input_text=input_text)
        # store.py update exits 2 for refused ids (unknown / already-superseded)
        # — _structured_write_response sanitizes that into a clean error.
        return _structured_write_response(r, ok_result="updated")

    def _tool_invalidate(self, args: Dict[str, Any]) -> str:
        """Tombstone with a REQUIRED reason (issue #59, 4.3). See _INVALIDATE_SCHEMA."""
        mid = (args.get("id") or "").strip()
        reason = (args.get("reason") or "").strip()
        if not mid:
            return _tool_error("Missing required parameter: id")
        if not reason:
            return _tool_error(
                "Missing required parameter: reason — invalidation records why "
                "the fact is no longer true and REQUIRES a reason (issue #59, 4.3)"
            )
        r = _run_store(["invalidate", "--id", mid, "--reason", reason])
        if not r["ok"]:
            # PR-review PRR-M: sanitized diagnostic (never raw stderr). The
            # PR-review PRR-B guard makes a second invalidate exit 2 with the
            # stable "[zmem] … already superseded …" line, which passes
            # through _sanitize_store_error verbatim.
            return _tool_error(
                f"Invalidate failed (id may not exist or is already "
                f"superseded): {_sanitize_store_error(r)}"
            )
        return json.dumps({"result": "invalidated", "id": mid})

    def _tool_session_start(self, args: Dict[str, Any]) -> str:
        """Compatibility SessionStart tool backed by one store envelope.

        The structured response shape remains for Hermes callers, but all
        passive selection, delivery state, budgeting, and rendering stay in
        the ``store.py`` subprocess.  In particular, this adapter never
        unwraps candidate rows or reconstructs a fence locally.
        """
        ns = (args.get("namespace") or self._namespace).strip() or "user:global"
        if ns == "*":
            ns = self._namespace
        # Issue #110 (P0-5): passive-injection kill switch — same 9-key
        # envelope as the enabled path (clients parse the shape), with the
        # distinguishing reason/context values. No store subprocess runs.
        if _inject_disabled():
            logger.info(
                "zmem session_start: status=silent "
                "reason=disabled (ZMEM_INJECT=0)")
            _append_session_decision(
                status="silent", reason=_store_constants()["INJECT_REASON_DISABLED"],
                ids=[], all_ids=[], session_id=self._session_id,
                lane="hermes-provider", t_ms=0,
            )
            return json.dumps({
                "result": "session_started",
                "namespace": ns,
                "ids": [],
                "omitted": 0,
                "budget_dropped": 0,
                "reason": _store_constants()["INJECT_REASON_DISABLED"],
                "context": "",
                "tokens_used": None,
                "tokens_budget": None,
            })
        try:
            limit = max(1, min(int(args.get("limit") or 3), 50))
        except (TypeError, ValueError):
            limit = 3
        # Issue #158: one store-owned passive attempt.  Issue #153 keeps the
        # decision line attributed: the local provider twin always carries
        # the ``hermes-provider`` lane (the remote MCP compatibility twin in
        # ``server/mcp_server.py`` writes ``hermes-compat``), and ``t_ms``
        # measures only the subprocess attempt — ``_run_store`` records that
        # interval while store-path resolution stays outside it.
        timing: Dict[str, int] = {}
        session_id = (args.get("session_id") or self._session_id or "").strip()
        result = _run_passive_store(_passive_store_args(
            "recent",
            query="",
            namespace=ns,
            limit=limit,
            global_limit=2,
            session_id=session_id,
            moment="session_start",
            lane="hermes-provider",
        ), timing=timing)
        elapsed = timing.get("t_ms", 0)
        payload = _decode_rendered_envelope(result)
        if payload is None:
            # A legacy or mixed-version envelope without a rendered member
            # still feeds the structured response and the decision line; the
            # delivered context stays empty rather than being reconstructed
            # locally (the store owns selection and rendering).
            stdout = ((result.get("stdout") or "").strip()
                      if isinstance(result, dict) else "")
            try:
                legacy = json.loads(stdout) if stdout else {}
            except (TypeError, json.JSONDecodeError):
                legacy = {}
            payload = legacy if isinstance(legacy, dict) else {}
        rendered = payload.get("rendered")
        if not isinstance(rendered, str):
            rendered = ""
        has_rendered = bool(rendered)
        omitted = payload.get("omitted", 0)
        if isinstance(omitted, bool) or not isinstance(omitted, int):
            omitted = 0
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            candidate_ids = []
        candidate_ids = [str(mid) for mid in candidate_ids
                         if isinstance(mid, str)]
        excluded = payload.get("excluded", 0)
        if not isinstance(excluded, int) or isinstance(excluded, bool):
            excluded = 0
        result_rows = payload.get("results")
        selected_ids = ([row.get("id") for row in result_rows
                         if isinstance(row, dict)
                         and isinstance(row.get("id"), str)]
                        if isinstance(result_rows, list) else [])
        store_reason = payload.get("reason")
        if not isinstance(store_reason, str):
            store_reason = None
        # Issue #87/#85 direction 1 precedence, twin of the MCP server (do
        # not fork): the store envelope's own reason is authoritative when
        # present and inside the closed set; older envelopes fall back to
        # budget-drop > already-delivered > omitted > empty-pool.
        reasons = _store_constants()
        allowed = reasons["INJECT_SILENT_REASONS"]
        if has_rendered:
            reason = reasons["INJECT_REASON_INJECTED"]
        else:
            budget_dropped = payload.get("budget_dropped", 0)
            if (isinstance(budget_dropped, bool)
                    or not isinstance(budget_dropped, int)):
                budget_dropped = 0
            try:
                if budget_dropped:
                    reason = "budget-drop"
                elif store_reason and store_reason in allowed:
                    reason = store_reason
                elif candidate_ids:
                    reason = "already-delivered"
                elif omitted > 0:
                    reason = "omitted"
                else:
                    reason = "empty-pool"
                if reason not in allowed:
                    reason = "empty-pool"
            except Exception:
                reason = "empty-pool"
        _append_session_decision(
            status="injected" if has_rendered else "silent", reason=reason,
            ids=selected_ids,
            all_ids=candidate_ids or selected_ids,
            omitted=omitted, excluded=excluded,
            session_id=session_id or self._session_id,
            moment="session_start", lane="hermes-provider", t_ms=elapsed,
        )

        return json.dumps({
            "result": "session_started",
            "namespace": ns,
            # Candidate rows are intentionally opaque to this adapter.  The
            # legacy key remains present for callers that expect the shape;
            # the canonical rendered fence is the only passive payload.
            "ids": [],
            "omitted": payload.get("omitted", 0),
            "budget_dropped": payload.get("budget_dropped", 0),
            "budget_admission": payload.get("budget_admission"),
            "budget_truncated": payload.get("budget_truncated", 0),
            "budget_dropped_protected": payload.get("budget_dropped_protected", 0),
            "reason": reason,
            "context": rendered,
            "tokens_used": payload.get("tokens_used"),
            "tokens_budget": payload.get("tokens_budget"),
        })

    def _tool_session_end(self, args: Dict[str, Any]) -> str:
        """End-of-session pairing (issue #65, 10.5 — MCP session_end twin).

        No note ⇒ no-write ack (never organizes/consolidates). Note ⇒ exactly
        one add via the standard path (fact / signal none / untrusted_tool /
        capture auto so the shared redaction helper runs).
        """
        note = (args.get("note") or "").strip()
        if not note:
            return json.dumps({"result": "session_ended", "written": False})
        consts = _store_constants()
        if len(note) > consts["MAX_CONTENT_CHARS"]:
            return _tool_error(
                f"note is {len(note)} chars, over the "
                f"{consts['MAX_CONTENT_CHARS']} limit"
            )
        ns = (args.get("namespace") or self._namespace).strip() or "user:global"
        if ns == "*":
            ns = self._namespace
        cli_args = [
            "add",
            "--namespace", ns,
            "--type", "fact",
            "--content", note,
            "--signal", "none",
            "--taint", "untrusted_tool",
            "--capture-mode", "auto",
            "--source-ref", "session_end",
            "--json",
        ]
        input_text = None
        if len(note) > _ARGV_SAFE_CONTENT_CHARS or note == "-":
            # F8: pipe literal '-' via stdin (CLI stdin sentinel).
            cli_args[cli_args.index("--content") + 1] = "-"
            input_text = note
        r = _run_store(cli_args, input_text=input_text)
        resp = _structured_write_response(r, ok_result="session_ended")
        try:
            parsed = json.loads(resp)
            if isinstance(parsed, dict) and "error" not in parsed:
                parsed["written"] = True
                parsed["result"] = "session_ended"
                return json.dumps(parsed)
        except json.JSONDecodeError:
            pass
        return resp

    # -- session / shutdown -------------------------------------------------

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = new_session_id or ""
        # Namespace may change if the new session is a different gateway user.
        self._namespace = self._resolve_namespace(**kwargs)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Detached housekeeping — organize + backup if due.

        Session-end maintenance is the SAME act as SessionStart's sleep-time
        job (issue #62, 7.7): ``organize``, not bare ``consolidate`` (claude
        Code F-009 — this plugin was the one shipped surface still calling
        consolidate after the 7.7 rewire, so Hermes users received none of
        organize's deliverables while a Hermes session-end could arm the shared
        cadence clock and starve the next SessionStart organize).

        ``organize`` runs WITHOUT ``--dry-run``: it shares consolidate's single-
        flight "consolidate" lock and the shared meta-key cadence gate, so on a
        store that is not due it is a cheap announce-only no-op (the gate and
        any skips are printed to stdout, which this caller discards — the
        announcement is for the interactive closeout user, not the background
        hook). organize's episode is BOUNDED (ZMEM_ORGANIZE_EPISODE_BOUND,
        default 256), so its wall-clock is strictly lower than the full-store
        ``consolidate`` it replaces — comfortably inside the plugin's
        ``_STORE_TIMEOUT_S`` (20s) subprocess cap.
        """
        try:
            _run_store(["organize"])
            _run_store(["backup", "--if-due"])
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("zmem on_session_end housekeeping failed: %s", exc)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """No-op. zmem and Hermes' built-in memory are independent stores.

        Built-in memory writes (MEMORY.md / USER.md) are NOT mirrored to zmem.
        They serve different purposes: built-in is session-scoped notes; zmem
        is cross-session, cross-agent lessons. Do not wire mirroring here.
        """
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Deferred — no pre-compress extraction in v1."""
        return ""

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs
    ) -> None:
        """Deferred — no subagent delegation capture in v1."""
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """No-op. zmem captures lessons via explicit agent action (zmem_add),
        not passive turn ingestion — unlike mem0/honcho which do server-side
        extraction. Turning here would duplicate the reflection loop's job.
        """
        return None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """No secrets; ZMEM_HOME is an env var. Empty list is correct."""
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """No-op — zmem is env-var-only (ZMEM_HOME, ZMEM_DATA, ZMEM_NAMESPACE)."""
        return None

    def backup_paths(self) -> List[str]:
        """``hermes backup`` captures the shared store."""
        try:
            return [str((_resolve_store_data_dir() / "store.sqlite").resolve())]
        except Exception:
            return []

    def shutdown(self) -> None:
        """No background threads to drain in this provider."""
        return None


# -- helpers -----------------------------------------------------------------

def _clamp_limit(raw: Any, default: int = 5, hard_max: int = 50) -> int:
    """Coerce a tool-call ``limit`` arg to a safe integer."""
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, hard_max))


# -- registration ------------------------------------------------------------

def register(ctx) -> None:
    """Register ZMem as a memory provider plugin."""
    ctx.register_memory_provider(ZmemMemoryProvider())
