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
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

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
    except Exception:
        # host.py absent or broken — fall back to the historical default so
        # the provider degrades rather than crashes.
        return Path.home() / ".zmem"


def _resolve_core_md() -> Path:
    """Resolve core.md via host.py (honors ZMEM_CORE_MD + store-path parent)."""
    try:
        return _host().resolve_core_md_path()
    except Exception:
        return _resolve_store_data_dir() / _CORE_MD_REL


def _host():
    """Lazily import zmem's host.py from the resolved checkout.

    host.py lives at ``$ZMEM_HOME/skills/memory/scripts/host.py`` (or the
    in-tree equivalent). We import it lazily so the provider module loads even
    when host.py isn't on sys.path at import time (e.g. during syntax checks).
    """
    import importlib
    home = _resolve_zmem_home()
    if home is None:
        raise RuntimeError("ZMEM_HOME not resolved")
    scripts_dir = str(home / "skills" / "memory" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("host")


def _python_bin() -> str:
    """Python interpreter for store.py subprocess. Prefer the current one."""
    return sys.executable or "python"


# -- subprocess helper -------------------------------------------------------

def _run_store(args: List[str]) -> Dict[str, Any]:
    """Run ``store.py <args>`` and return ``{ok, stdout, stderr, returncode}``.

    Always returns a dict (never raises) — memory must fail-open. The caller
    decides whether a non-zero returncode is fatal.
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
    try:
        proc = subprocess.run(  # noqa: S603 — argv is constructed, not shell
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
                    "Scope (default: your session's namespace). Pass "
                    "'user:global' or '*' to search across all namespaces."
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
                "enum": ["fact", "lesson", "convention", "preference"],
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
                "enum": ["test", "compile", "lint", "reviewer", "user", "none"],
                "description": "How strongly grounded this memory is.",
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

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    _SEARCH_SCHEMA,
    _ADD_SCHEMA,
    _SUPERSEDE_SCHEMA,
]


def _tool_error(msg: str) -> str:
    """JSON error string for tool-call failures (mirrors tools.registry.tool_error)."""
    return json.dumps({"error": msg})


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
        """Passive recall before each turn. Returns a ``<memory-context>`` block.

        The MemoryManager runs external-provider prefetch in a background thread
        with a bounded join (``memory_manager.py``), so the subprocess cost is
        amortized — no need for queue_prefetch complexity here.
        """
        if not query or not query.strip():
            return ""
        q = query.strip()[:_MAX_QUERY_CHARS]
        r = _run_store(
            [
                "recall",
                "--query", q,
                "--namespace", self._namespace,
                "--limit", str(_PREFETCH_LIMIT),
                "--json",
            ]
        )
        if not r["ok"]:
            logger.debug("zmem prefetch failed: %s", r["stderr"])
            return ""
        stdout = (r["stdout"] or "").strip()
        if not stdout:
            return ""
        try:
            results = json.loads(stdout)
        except json.JSONDecodeError:
            logger.debug("zmem prefetch: non-JSON stdout: %s", stdout[:200])
            return ""
        if not isinstance(results, list) or not results:
            return ""
        lines: List[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            if not content:
                continue
            mtype = item.get("type", "")
            conf = item.get("confidence")
            tag = f"[{mtype}" + (f" conf={conf}" if conf is not None else "") + "] "
            lines.append(f"- {tag}{content}")
        if not lines:
            return ""
        return "## ZMem Memory\n" + "\n".join(lines)

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
            "--json",
        ]
        if ns_arg and ns_arg != "*":
            cli_args += ["--namespace", ns_arg]
        elif not ns_arg:
            # Default to the session namespace (mirror prefetch/add isolation).
            cli_args += ["--namespace", self._namespace]
        # Empty/ns_arg == '*' → no --namespace flag → store.py searches all.
        r = _run_store(cli_args)
        if not r["ok"]:
            return _tool_error(f"Search failed: {r['stderr'] or r['stdout'][:200]}")
        stdout = (r["stdout"] or "").strip()
        if not stdout:
            return json.dumps({"results": [], "count": 0})
        try:
            results = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return _tool_error(f"Search returned non-JSON: {exc}")
        if not isinstance(results, list):
            results = []
        items = [
            {
                "id": it.get("id"),
                "type": it.get("type"),
                "content": it.get("content"),
                "confidence": it.get("confidence"),
                "tags": it.get("tags"),
                "source_ref": it.get("source_ref"),
            }
            for it in results
            if isinstance(it, dict)
        ]
        return json.dumps({"results": items, "count": len(items)})

    def _tool_add(self, args: Dict[str, Any]) -> str:
        mtype = (args.get("type") or "").strip()
        content = (args.get("content") or "").strip()
        if not mtype:
            return _tool_error("Missing required parameter: type")
        if not content:
            return _tool_error("Missing required parameter: content")
        if mtype not in ("fact", "lesson", "convention", "preference"):
            return _tool_error(f"Invalid type: {mtype}")
        ns = (args.get("namespace") or self._namespace).strip()
        signal = (args.get("signal") or "none").strip()
        tags = (args.get("tags") or "").strip()
        source_ref = (args.get("source_ref") or "").strip()
        if not source_ref and self._session_id:
            source_ref = f"session:{self._session_id}"
        cli_args = [
            "add",
            "--namespace", ns,
            "--type", mtype,
            "--content", content,
            "--signal", signal,
        ]
        if tags:
            cli_args += ["--tags", tags]
        if source_ref:
            cli_args += ["--source-ref", source_ref]
        r = _run_store(cli_args)
        if not r["ok"]:
            return _tool_error(f"Add failed: {r['stderr'] or r['stdout'][:200]}")
        # store.py prints "[zmem] added memory <id> ..." to stdout on success.
        return json.dumps({"result": "stored", "raw": r["stdout"].strip()})

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
                f"Supersede failed (id may not exist): {r['stderr'] or r['stdout'][:200]}"
            )
        return json.dumps({"result": "superseded", "id": mid})

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
        """Detached housekeeping — consolidate + backup if due.

        ``consolidate`` runs WITHOUT ``--dry-run``: store.py's own cadence gate
        (meta-key ``last_consolidation``) + single-flight lock make the real
        call cheap when not due, and ``--dry-run`` was an expensive no-op that
        paid the full clustering scan without ever merging.
        """
        try:
            _run_store(["consolidate"])
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
