#!/usr/bin/env python3
"""Hermes shell hook: compatibility-mode passive delivery (pre_llm_call).

Issue #122 (Workstream D, PR 7 of 8) — Hermes parity with the other hosts:
this hook is now a thin, fail-open adapter over ONE query-aware selector
call per invocation. It never opens the store, never imports store-side
Python modules, and never touches the correction queue directly:

- Correction capture and the pending-failure/operation-ring preparation run
  inside the store process behind one ``hermes-reflect --json`` call. The
  older ``hermes-context`` bridge remains only for post-stdout acknowledge
  and cursor commit actions::

      python skills/memory/scripts/store.py hermes-reflect --json
      python skills/memory/scripts/store.py hermes-context \
          --action ack-failure|commit-cursor --namespace <ns> --session-id <sid> \
          [--cursor-ts <ts> --cursor-count <n>]

- The prefetch is ONE selector call (initial attempt plus one retry):
  remote mode runs ``server/mcp_client.py call prefetch``; local mode runs
  ``store.py prefetch``. Both always pass the current user message as the
  query, ``--moment user_prompt`` and ``--lane hermes-compat``, plus one
  repeated ``--ops-token <token>`` per operation-ring token in returned
  order. The selector owns the shared relevance/trust gate and the one
  1,500-token budget; its ``rendered`` fence is the ONLY injected memory
  text (a successful empty ``rendered`` is a silent selector result, not a
  cursor delivery).

- The namespace is DERIVED, never hand-typed: ``ZMEM_MCP_NAMESPACE`` →
  ``ZMEM_NAMESPACE`` → ``ZMEM_PROJECT`` → ``ZCODE_PROJECT_DIR`` →
  ``CLAUDE_PROJECT_DIR`` → the current directory, resolved through
  ``host.resolve_namespace`` (the sole producer of ``project:*`` keys);
  resolver failure falls back to ``user:global``.

- Fail-open: ANY failure (missing bridge, bad token, refused connection,
  timeout, malformed envelope, exhausted retry budget) injects nothing and
  the turn proceeds. A remote failure still delivers the pending failure
  nudge. The operation cursor is committed only AFTER a rendered response.
  No ``pre_tool_call`` event is registered or consumed here (the
  measurement gate is issue #111), and no explicit recall runs on this
  compatibility lane.

Emits raw JSON on stdout: ``{"context": "..."}`` to inject, ``{}`` silent.
Stdlib only (the ``mcp`` dependency lives in the mcp_client subprocess).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# The closed selector-envelope key set (issue #122): a response missing any
# of these is a failed prefetch, never a partial injection.
_SELECTOR_KEYS = frozenset({
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
})


def _read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_user_message(payload: dict) -> str:
    """Pull the current user turn from a pre_llm_call payload.

    Documented upstream shape is a top-level ``user_message`` (plugin-hook
    catalog), but the shell-hook serializer has a known defect (upstream
    #83281) nesting it under ``extra``; fall back to the last user entry of
    ``conversation_history``, then the generic prompt-ish keys. Returns ""
    when nothing usable is present (the prefetch then queries an empty
    string and the selector decides)."""
    for container in (payload, payload.get("extra") if isinstance(payload.get("extra"), dict) else {}):
        v = container.get("user_message")
        if isinstance(v, str) and v.strip():
            return v
    history = payload.get("conversation_history")
    if isinstance(history, list):
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") in ("user", "human"):
                v = msg.get("content")
                if isinstance(v, str) and v.strip():
                    return v
    for key in ("prompt", "input", "text"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _session_id(payload: dict) -> str:
    """Normalized session id — it names the store-side meta keys (the
    convention hook writes ``hermes_pending_failure_{session}``) and the
    hashed ops sidecars, so it must match the writer's normalization."""
    import re as _re
    sid = (payload.get("session_id") or "").strip()
    sid = _re.sub(r"[^A-Za-z0-9._-]", "_", sid)
    sid = sid.replace("..", "_")
    return sid[:128] or "unknown"


def _emit_context(text: str) -> None:
    print(json.dumps({"context": text}))


def _emit_empty() -> None:
    print("{}")


def _clamp_timeout(raw: str) -> float:
    """PRR-019: parse + clamp ZMEM_MCP_TIMEOUT (seconds). Garbage → the 8s
    default; anything outside [1, 30] is clamped (the hook's own shell-side
    timeout is 15s, so >15 would never be honored anyway)."""
    raw = (raw or "").strip()
    if not raw:
        return 8.0
    try:
        return max(1.0, min(30.0, float(raw)))
    except ValueError:
        return 8.0


def _scripts_dir() -> Path | None:
    """The plugin's skills/memory/scripts directory (in-tree, then a copy
    install under ZMEM_HOME)."""
    _rel = Path("skills") / "memory" / "scripts"
    candidates = [
        Path(__file__).resolve().parents[2] / _rel,
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,
    ]
    return next((c for c in candidates if (c / "store.py").is_file()), None)


def _capture_enabled() -> bool:
    """Apply the canonical pure capture switch at the Hermes boundary.

    Installed-tree damage must not turn capture back on when the operator set
    the kill switch, so the exact local comparison remains the fail-open
    fallback when the helper cannot be imported.
    """
    fallback = os.environ.get("ZMEM_CAPTURE", "1").strip() != "0"
    scripts = _scripts_dir()
    if scripts is None:
        return fallback
    inserted = str(scripts)
    try:
        sys.path.insert(0, inserted)
        from capture_quality import capture_enabled

        return capture_enabled()
    except Exception:
        return fallback
    finally:
        try:
            sys.path.remove(inserted)
        except ValueError:
            pass


def _resolve_hook_namespace() -> str:
    """ONE namespace chain for everything this hook does (issue #122):
    ``ZMEM_MCP_NAMESPACE`` → ``ZMEM_NAMESPACE`` → ``ZMEM_PROJECT`` →
    ``ZCODE_PROJECT_DIR`` → ``CLAUDE_PROJECT_DIR`` → the current directory;
    every project-dir source is resolved through ``host.resolve_namespace``
    (the exact ``project:<normalized-remote>`` / ``project:<normalized-absolute-path>``
    derivation). Resolver failure returns ``user:global``."""
    ns = os.environ.get("ZMEM_MCP_NAMESPACE", "").strip()
    if ns:
        return ns
    ns = os.environ.get("ZMEM_NAMESPACE", "").strip()
    if ns:
        return ns
    source = (os.environ.get("ZMEM_PROJECT", "").strip()
              or os.environ.get("ZCODE_PROJECT_DIR", "").strip()
              or os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
              or os.getcwd())
    try:
        scripts = _scripts_dir()
        if scripts is None:
            return "user:global"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import host  # type: ignore
        return host.resolve_namespace(source) or "user:global"
    except Exception:
        return "user:global"


def _run_hermes_reflect(payload: dict) -> dict:
    """Run the one-payload ``hermes-reflect`` capture/prepare bridge.

    The hook serializes the already-normalized namespace, session, and current
    user message once. The store process owns correction state and returns the
    existing prepare object; any bridge failure is deliberately silent.
    """
    scripts = _scripts_dir()
    if scripts is None:
        return {}
    cmd = [sys.executable, str(scripts / "store.py"), "hermes-reflect", "--json"]
    timeout_s = _clamp_timeout(os.environ.get("ZMEM_MCP_TIMEOUT", ""))
    try:
        r = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                           text=True, timeout=timeout_s, encoding="utf-8",
                           errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0:
        return {}
    try:
        obj = json.loads((r.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict) or obj.get("error"):
        return {}
    return obj


def _run_hermes_context(args: list[str]) -> dict:
    """Run the post-output ``hermes-context`` bridge in the store process (inherited
    env, so ZMEM_DATA/ZMEM_STORE resolve identically) and return one JSON
    object; {} on timeout, nonzero exit, malformed JSON, or an error
    object."""
    scripts = _scripts_dir()
    if scripts is None:
        return {}
    cmd = [sys.executable, str(scripts / "store.py"), "hermes-context", *args]
    timeout_s = _clamp_timeout(os.environ.get("ZMEM_MCP_TIMEOUT", ""))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, encoding="utf-8",
                           errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0:
        return {}
    try:
        obj = json.loads((r.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict) or obj.get("error"):
        return {}
    return obj


# ---------------------------------------------------------------------------
# Prefetch retry budget (issue #122): a cursor gets two total prefetch
# attempts — one initial and one retry — recorded in the hashed
# ``.attempts`` sidecar this hook owns (same path and ``"<ts> <attempts>\n"``
# format the store-side module writes; the hook computes the sha256 stem
# itself because it must not import store-side modules). Later invocations
# skip an exhausted cursor until the ring cursor changes.
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    store = os.environ.get("ZMEM_STORE", "").strip()
    if store:
        return Path(store).expanduser().parent
    for var in ("ZMEM_DATA",):
        d = os.environ.get(var, "").strip()
        if d:
            return Path(d).expanduser()
    for var in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        d = os.environ.get(var, "").strip()
        if d:
            return Path(d).expanduser()
    return Path.home() / ".zmem"


def _ops_stem(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:32]


def _attempts_path(session: str) -> Path:
    return _data_dir() / "ops" / (_ops_stem(session) + ".attempts")


def _read_retry_state(session: str, cursor_ts: float) -> int:
    if not cursor_ts:
        return 0
    try:
        parts = _attempts_path(session).read_text(
            encoding="utf-8", errors="replace").split()
        if len(parts) >= 2 and float(parts[0]) == float(cursor_ts):
            return int(parts[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _write_retry_state(session: str, cursor_ts: float, attempts: int) -> None:
    """Atomic (temp + fsync + replace): an interrupted write must never
    leave partial marker bytes."""
    path = _attempts_path(session)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("{0} {1}\n".format(float(cursor_ts), int(attempts)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _valid_envelope(obj: object) -> bool:
    return (isinstance(obj, dict)
            and all(k in obj for k in _SELECTOR_KEYS)
            and isinstance(obj.get("rendered"), str))


def _prefetch(user_message: str, namespace: str, session_id: str,
               operation_terms: list[str], cursor: tuple[float, int]) -> dict:
    """ONE query-aware selector call per invocation (initial attempt plus
    one retry). Remote mode runs mcp_client.py ``call prefetch``; local mode
    runs ``store.py prefetch --for-injection --no-bump --json``. Both carry
    the user message as the query, ``user_prompt``/``hermes-compat``, and
    the ring tokens in order. Returns the validated envelope dict, or {}
    on any failure (fail-open)."""
    url = os.environ.get("ZMEM_MCP_URL", "").strip()
    timeout_s = _clamp_timeout(os.environ.get("ZMEM_MCP_TIMEOUT", ""))
    attempts = _read_retry_state(session_id, cursor[0])
    if attempts >= 2:
        return {}
    if url:
        hook_dir = Path(__file__).resolve().parent
        client = hook_dir.parent / "server" / "mcp_client.py"
        if not client.is_file():
            return {}
        base = [sys.executable, str(client), "--url", url, "call", "prefetch"]
    else:
        scripts = _scripts_dir()
        if scripts is None:
            return {}
        base = [sys.executable, str(scripts / "store.py"), "prefetch"]
    cmd = base + [
        "--query", user_message,
        "--namespace", namespace,
        "--session-id", session_id,
        "--moment", "user_prompt",
        "--lane", "hermes-compat",
    ]
    if not url:
        # Explicit markers of the only mode the compatibility lane runs
        # (the selector path is inherently passive; the store-side ledger
        # is keyed by the session id).
        cmd += ["--for-injection", "--no-bump", "--json"]
    for tok in operation_terms:
        cmd += ["--ops-token", tok]
    for attempt in (attempts + 1, attempts + 2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout_s, encoding="utf-8",
                               errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            r = None
        envelope = None
        if r is not None and r.returncode == 0:
            try:
                candidate = json.loads((r.stdout or "").strip())
            except (json.JSONDecodeError, ValueError):
                candidate = None
            if _valid_envelope(candidate):
                envelope = candidate
        if envelope is not None:
            return envelope
        _write_retry_state(session_id, cursor[0], attempt)
    sys.stderr.write(
        "zmem-reflect: prefetch failed after 2 attempts; cursor unchanged\n")
    return {}


def main() -> int:
    # Capture is separately parent-controlled and deliberately independent of
    # ZMEM_INJECT. Check it before payload parsing, namespace resolution, or a
    # subprocess so the disabled path cannot write or inspect any state.
    if not _capture_enabled():
        _emit_empty()
        return 0

    payload = _read_payload()
    session = _session_id(payload)
    user_message = _extract_user_message(payload)
    namespace = _resolve_hook_namespace()

    # One store-owned capture/prepare call runs before the independent delivery
    # switch. ZMEM_INJECT=0 suppresses delivery but preserves this one capture.
    prep = _run_hermes_reflect({
        "namespace": namespace,
        "session_id": session,
        "user_message": user_message,
    })

    # Issue #110 (P0-5): passive-injection kill switch — capture above
    # already ran; every DELIVERY path below is silenced. Pending markers
    # stay armed: they fire on the first enabled run instead of being lost.
    if os.environ.get("ZMEM_INJECT", "1").strip() == "0":
        sys.stderr.write(
            "zmem-reflect: status=silent reason=disabled (ZMEM_INJECT=0)\n")
        _emit_empty()
        return 0

    cursor_raw = prep.get("cursor") if isinstance(prep, dict) else None
    try:
        cursor = (float(cursor_raw[0]), int(cursor_raw[1])) \
            if isinstance(cursor_raw, list) and len(cursor_raw) >= 2 \
            else (0.0, 0)
    except (TypeError, ValueError):
        cursor = (0.0, 0)
    tokens_key = "_".join(("ops", "tokens"))
    tokens_raw = prep.get(tokens_key) if isinstance(prep, dict) else None
    operation_terms = ([t for t in tokens_raw if isinstance(t, str)]
                       if isinstance(tokens_raw, list) else [])
    nudge_raw = prep.get("failure_nudge") if isinstance(prep, dict) else None
    failure_nudge = nudge_raw if isinstance(nudge_raw, str) else ""

    # ONE query-aware selector call (the #159 tool; shared gate + budget).
    envelope = _prefetch(user_message, namespace, session, operation_terms, cursor)
    rendered = envelope.get("rendered", "") if envelope else ""

    # Local failure text first, then the selector-owned fence; joined by
    # exactly two LF bytes (pinned by the hermes_compat fixtures).
    parts = [p for p in (failure_nudge, rendered) if p]
    if parts:
        _emit_context("\n\n".join(parts))
    else:
        _emit_empty()

    # Side effects happen only AFTER the bytes are on stdout: acknowledge a
    # delivered failure nudge; commit the cursor only after a rendered
    # response (a crash between render and commit can re-deliver, never
    # drop — the inverse of the pre-#122 ordering).
    if failure_nudge:
        _run_hermes_context(["--action", "ack-failure",
                             "--namespace", namespace,
                             "--session-id", session])
    if rendered:
        _run_hermes_context(["--action", "commit-cursor",
                             "--namespace", namespace,
                             "--session-id", session,
                             "--cursor-ts", repr(float(cursor[0])),
                             "--cursor-count", str(int(cursor[1]))])
    return 0


if __name__ == "__main__":
    sys.exit(main())
