#!/usr/bin/env python3
"""Hermes shell hook: reflect nudge DELIVERY (pre_llm_call) — universal surface.

This is the DELIVERY side of the reflection loop. The convention hook
(``post_tool_call``, observational — its results are discarded by Hermes)
records pending-nudge flags in zmem's ``meta`` table; THIS hook fires on
``pre_llm_call`` (whose ``{"context": ...}`` results ARE consumed and injected
into the user message) and delivers them.

Priority (first match wins, each is one-shot per session):
  0. correction capture (issue #71 D) — runs BEFORE delivery in every mode:
     the user turn is classified by the same ``corrections.detect_patterns``
     rules the other hosts use and queued to the correction SIDECAR (never
     the store; closeout stays the write authority)
  1. pending failure nudge → emit failure-capture context, clear flag
  1.5 pending convention nudge (after the ops-ring delivery, see below)
  2. generic reflect: if convention counter > 0 and no lesson captured yet,
     emit a reflect nudge (once per session)
  (+) ops-ring query-context delivery (issue #90 / #85 C) between 1 and 2

REMOTE MODE (issue #71 A): when ``ZMEM_MCP_URL`` is set, the box has no local
store (``_connect()`` finds no file) and the passive prefetch comes from the
LAN zmem MCP server via ``server/mcp_client.py`` (the ``session_start`` tool —
``--no-bump``, fenced, token-budgeted). Fail-open: a missing ``mcp`` lib, a
bad token, a refused connection, or a timeout means no injection and the turn
proceeds.

Works on ALL surfaces (CLI, TUI, Telegram, Discord, Slack) because
``pre_llm_call`` context is injected into the user message regardless of
platform — unlike ``pre_verify`` which Hermes gates on file mutations.

Emits raw JSON on stdout: ``{"context": "..."}`` to inject, ``{}`` silent.
Stdlib only (the ``mcp`` dependency lives in the mcp_client subprocess).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Shared WAL-safety guard (single copy across the three Hermes hooks — #37 L25).
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)
from _zmem_hook_common import assert_local_fs as _assert_local_fs  # noqa: E402

_CONVENTION_COUNT_KEY = "hermes_convention_count_{session}"
_PENDING_CONVENTION_KEY = "hermes_pending_convention_{session}"
_PENDING_FAILURE_KEY = "hermes_pending_failure_{session}"
# Cleared alongside _PENDING_FAILURE_KEY on delivery so a LATER failure in the
# same session can re-arm (without this, the one-shot _FAILURE_CAPTURED_KEY in
# the convention hook would suppress every failure after the first delivered).
_FAILURE_CAPTURED_KEY = "hermes_failure_captured_{session}"
_REFLECT_PROMPTED_KEY = "hermes_reflect_prompted_{session}"
# Issue #90 / #85 C: the query-context delivery marker lives in the ops/
# SIDECAR namespace (<data>/ops/<session>.delivered via ops_tokens), NOT in
# the store's meta table — the read-path delivery surfaces keep all their
# persistence in sidecars (final-critic finding; the nudge flags above are
# the established meta-table pattern and predate this).


def _resolve_store_path() -> Path:
    """Resolve the store path via the SAME authoritative resolver as the
    provider and store.py (host.resolve_store_path). Previously this hook
    hand-rolled a TRUNCATED copy that omitted CLAUDE/ZCODE_PLUGIN_DATA, so on
    plugin-data-dir boxes it resolved a nonexistent ~/.zmem/store.sqlite and
    silently no-op'd all session (#36 M10).

    Imports the real resolver when the scripts dir is reachable (the normal
    case). The inline fallback below is a BEST-EFFORT subset (the env-var chain
    + ~/.zmem) reached only if host.py itself is unimportable; it does NOT
    include host.py's legacy probes (~/.zcode/memory, _legacy_plugin_store) —
    if you need those on a broken-import box, fix the import path instead."""
    _rel = Path("skills") / "memory" / "scripts"
    candidates = [
        Path(__file__).resolve().parents[2] / _rel,            # in-tree (repo/symlink/junction)
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,  # copy install
    ]
    for _scripts_dir in candidates:
        if (_scripts_dir / "host.py").is_file():
            sys.path.insert(0, str(_scripts_dir))
            try:
                import host  # type: ignore  # noqa: F811
                return host.resolve_store_path()
            except Exception:
                pass
    # Inline fallback: the env-var chain + ~/.zmem (host.py's legacy probes
    # omitted — see docstring).
    explicit = os.environ.get("ZMEM_STORE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        d = os.environ.get(var, "").strip()
        if d:
            return Path(d).expanduser() / "store.sqlite"
    return Path.home() / ".zmem" / "store.sqlite"


def _connect() -> sqlite3.Connection | None:
    p = _resolve_store_path()
    if not p.is_file():
        return None
    if not _assert_local_fs(p):
        return None
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


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
    """Issue #71 D: pull the current user turn from a pre_llm_call payload.

    Documented upstream shape is a top-level ``user_message`` (plugin-hook
    catalog), but the shell-hook serializer has a known defect (upstream
    #83281) nesting it under ``extra``; fall back to the last user entry of
    ``conversation_history``, then the generic prompt-ish keys. Returns ""
    when nothing usable is present (caller bails)."""
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


def _capture_correction(user_message: str, session: str, store_path: Path) -> None:
    """Issue #71 D: capture user corrections into the correction SIDECAR queue
    (parity with zmem-capture-correction.sh on Claude/ZCode/Codex).

    Hermes has no user-message hook; ``pre_llm_call`` is the closest real
    event and its payload carries the user turn. The classification rules are
    NOT duplicated here — the same ``corrections.detect_patterns`` /
    ``should_include_message`` the bash hook uses decide what qualifies, and
    ``correction_queue.make_item``/``append_queue`` write the same
    schema-versioned queue the closeout skill reviews (the closeout stays the
    sole store write authority; this hook NEVER touches the store).

    Same <5-char bail and fail-open contract as every other host. Per-session
    dedup via the ops sidecar (``<data>/ops/<session>.corr`` last-hash
    marker) so repeated pre_llm_call fires for the same turn append once.
    Kill switch: ZMEM_HERMES_CORRECTIONS=0. Default ON for parity with the
    other hosts' capture hooks."""
    if os.environ.get("ZMEM_HERMES_CORRECTIONS", "1").strip() == "0":
        return
    text = (user_message or "").strip()
    if len(text) < 5:
        return
    data_dir = store_path.parent
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    marker = data_dir / "ops" / f"{session}.corr"
    if marker.is_file():
        try:
            if marker.read_text(encoding="utf-8").strip() == digest:
                return
        except OSError:
            pass
    _rel = Path("skills") / "memory" / "scripts"
    candidates = [
        Path(__file__).resolve().parents[2] / _rel,            # in-tree / symlink
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,  # copy install
    ]
    scripts_dir = next(
        (c for c in candidates if (c / "correction_queue.py").is_file()), None)
    if scripts_dir is None:
        return
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import corrections  # noqa: E402
        import correction_queue as cq  # noqa: E402
    except Exception:
        return
    try:
        if not corrections.should_include_message(text):
            return
        item_type, patterns, confidence, sentiment, decay_days = \
            corrections.detect_patterns(text)
        if not item_type:
            return
        ns = _resolve_hook_namespace()
        item = cq.make_item(
            message=text, type_=item_type, patterns=patterns,
            confidence=confidence, sentiment=sentiment, decay_days=decay_days,
            session=session, namespace=ns, host="hermes",
        )
        ok = cq.append_queue(ns, item)
    except Exception:
        return
    if ok:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(digest, encoding="utf-8")
        except OSError:
            pass


def _session_id(payload: dict) -> str:
    """Sanitized session id (final-critic finding): the id lands in sidecar
    FILE NAMES (``ops/<session>.corr``, ring names) — a crafted
    ``../``-bearing session id must not escape the ops dir. Keep the common
    id shapes (alnum, dash, underscore, dot), collapse everything else."""
    import re as _re
    sid = (payload.get("session_id") or "").strip()
    sid = _re.sub(r"[^A-Za-z0-9._-]", "_", sid)
    sid = sid.replace("..", "_")
    return sid[:128] or "unknown"


def _emit_context(text: str) -> None:
    print(json.dumps({"context": text}))


def _emit_empty() -> None:
    print("{}")


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_clear(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


def _has_lesson(conn: sqlite3.Connection, session: str) -> bool:
    """Has any (non-superseded) memory been captured for this session?"""
    try:
        return conn.execute(
            "SELECT 1 FROM memory WHERE source_ref = ? AND superseded_at IS NULL "
            "LIMIT 1",
            (f"session:{session}",),
        ).fetchone() is not None
    except sqlite3.Error:
        # Schema drift on an old/foreign store — treat as "no lesson" rather
        # than crashing the agent turn.
        return False


def _convention_nudge(session: str) -> str:
    return (
        "ZMem convention capture: you just completed several tool calls. If you "
        "discovered a reusable convention, pattern, or workaround during this "
        "session — something that would help a future session facing a similar "
        "task — capture it now by calling the zmem_add tool:\n"
        f'  zmem_add with type="convention", content="<the lesson>", '
        f'signal="<test|reviewer|user|none>", source_ref="session:{session}"\n'
        "If nothing generalizable applies, do nothing."
    )


def _failure_nudge(session: str) -> str:
    # The detailed tool+error context was available at post_tool_call time but
    # isn't forwarded here; the nudge is still actionable (the model saw the
    # failure in the same turn).
    return (
        f"ZMem auto-capture: a tool failed earlier this session "
        f"(source_ref=session:{session}). If a generalizable lesson can be "
        "derived from that failure — a gotcha, a misconfiguration, a wrong "
        "assumption — capture it now by calling the zmem_add tool:\n"
        f'  zmem_add with type="lesson", content="<the lesson, with the error '
        f'context>", signal="none", source_ref="session:{session}"\n'
        "If the failure was transient or not generalizable, do nothing."
    )


def _reflect_nudge(session: str, count: int) -> str:
    return (
        "ZMem reflection: this session has completed "
        f"{count} tool call(s) but no lesson has been captured yet "
        f"(source_ref=session:{session}). If you learned something this session "
        "that a future session would benefit from — a workaround, a corrected "
        "assumption, a project convention — capture it now by calling the "
        "zmem_add tool:\n"
        f'  zmem_add with type="lesson", content="<the lesson>", '
        f'signal="<test|reviewer|user|none>", source_ref="session:{session}"\n'
        "If nothing generalizable, do nothing."
    )


def _query_context_delivery(conn: sqlite3.Connection, session: str,
                            store_path: Path) -> str:
    """Issue #90 / #85 C: deliver operation-context recall on pre_llm_call.

    Hermes has no pre-tool event (post_tool_call is observational — its
    results are discarded), so the closest prevention point is here: when the
    session's query-context ring (#88) grew since the last delivery, run
    recall on the ring tail and inject the fenced results before the next
    model call. This still misses the tool invocation that produced the
    verbs — stated in issue #90's matrix; attach to a consumed pre-tool hook
    if Hermes grows one.

    At-most-once per ring CURSOR ``(ts, parsed-event-count)`` — the count
    catches a second event appended in the same second (final-critic
    finding); the marker sidecar is written before the subprocess so a
    crash cannot re-deliver. Kill switch ZMEM_QUERY_CONTEXT=0. Returns ""
    on silence. Persists ONLY under the ops/ sidecar namespace (never the
    store's meta table).
    """
    try:
        if os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0":
            return ""
        _rel = Path("skills") / "memory" / "scripts"
        candidates = [
            Path(__file__).resolve().parents[2] / _rel,
            Path(os.environ.get("ZMEM_HOME", "")).expanduser() / _rel,
        ]
        scripts_dir = next((c for c in candidates if (c / "host.py").is_file()),
                           None)
        if scripts_dir is None:
            return ""
        # #93 C4: scripts_dir ITSELF on sys.path (not just storelib/) — the
        # `from storelib.inject import ...` below is a PACKAGE import that
        # needs the parent dir, removing the implicit dependency on
        # _resolve_store_path having inserted it earlier.
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        sys.path.insert(0, str(scripts_dir / "storelib"))
        import ops_tokens  # noqa: E402  (stdlib-only, like this hook)

        data_dir = str(store_path.parent)
        cursor = ops_tokens.ring_cursor(data_dir, session)
        if cursor <= (0.0, 0):
            return ""
        if cursor <= ops_tokens.read_delivered_cursor(data_dir, session):
            return ""
        events = ops_tokens.read_ops_ring(data_dir, session)
        query = " ".join(ops_tokens.derive_ops_tokens(*events))
        if not query:
            return ""
        # At-most-once: mark before the subprocess so a crash cannot
        # re-deliver (a transient recall failure after the mark skips that
        # cursor's delivery — documented best-effort).
        ops_tokens.write_delivered_cursor(data_dir, session, cursor)

        r = subprocess.run(
            [sys.executable, str(scripts_dir / "store.py"), "recall",
             "--query", query[:500],
             "--namespace", _resolve_hook_namespace(),
             "--limit", "5", "--include-global", "--global-limit", "3",
             "--no-bump", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        stdout = (r.stdout or "").strip()
        if not stdout:
            return ""
        parsed = json.loads(stdout)
        rows = parsed.get("results", []) if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list) or not rows:
            return ""
        try:
            from storelib.inject import apply_token_budget  # noqa: E402
            rows, _est, _dropped = apply_token_budget(rows)
        except Exception:
            pass
        if not rows:
            return ""
        from storelib.recall import _format_fenced_recall  # noqa: E402
        header = (
            "Relevant memories (zmem pre_llm_call operation context, session "
            f"{session}). Consider if they apply; ignore if not."
        )
        return _format_fenced_recall(rows, header)
    except Exception:
        return ""


def _resolve_hook_namespace() -> str:
    """PRR-016: ONE namespace chain for everything this hook does with a
    namespace — remote prefetch, query-context recall, and correction
    capture. Order: ``ZMEM_MCP_NAMESPACE`` → ``ZMEM_NAMESPACE`` →
    ``user:global``. Before this, correction capture skipped
    ``ZMEM_MCP_NAMESPACE``, so a ``ZMEM_MCP_NAMESPACE=project:foo`` box
    prefetched project:foo but queued corrections in user:global — related
    data split across namespaces."""
    return (os.environ.get("ZMEM_MCP_NAMESPACE", "")
            or os.environ.get("ZMEM_NAMESPACE", "")
            or "user:global")


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


def _remote_enabled() -> bool:
    """Issue #71 A: remote mode is opt-in via ZMEM_MCP_URL — the store lives
    across the LAN and context comes from the MCP server, not a local
    store.py."""
    return bool(os.environ.get("ZMEM_MCP_URL", "").strip())


def _remote_context() -> str:
    """Issue #71 A: fetch the passive session_start prefetch over MCP.

    Runs ``hermes-plugin/server/mcp_client.py`` as a SUBPROCESS so the ``mcp``
    client library and its event loop stay out of this (sync, per-turn) hook
    process; the timeout here is the wedge-proof backstop. The tool is the
    server's ``session_start`` — passive ``--no-bump``, fenced, token-budgeted
    — so retrieval_count is never bumped by a prefetch. Fail-open: ANY
    failure (missing mcp lib, bad token, refused connection, timeout, empty
    response) returns "" and the turn proceeds without injection.

    Namespace (final-critic fix): comes ONLY from configuration —
    ``ZMEM_MCP_NAMESPACE``, then ``ZMEM_NAMESPACE``; empty → the server's
    default (``user:global``). NEVER the session id: pre-critic code passed
    the session here, so scoped tokens rejected the request and the prefetch
    queried an accidental empty namespace."""
    url = os.environ.get("ZMEM_MCP_URL", "").strip()
    if not url:
        return ""
    hook_dir = Path(__file__).resolve().parent
    client = hook_dir.parent / "server" / "mcp_client.py"
    if not client.is_file():
        return ""
    timeout_s = _clamp_timeout(os.environ.get("ZMEM_MCP_TIMEOUT", ""))
    ns = _resolve_hook_namespace()
    cmd = [sys.executable, str(client), "--url", url, "call", "session_start"]
    if ns:
        cmd += ["--namespace", ns]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, encoding="utf-8",
                           errors="replace")
    except subprocess.TimeoutExpired:
        return ""
    except OSError:
        return ""
    if r.returncode != 0:
        # One quiet stderr line keeps failures diagnosable without spamming
        # the transcript (the hook itself stays silent on stdout).
        sys.stderr.write(f"zmem-reflect: remote prefetch unavailable: "
                         f"{(r.stderr or '').strip()[:200]}\n")
        return ""
    return (r.stdout or "").strip()


def main() -> int:
    payload = _read_payload()
    session = _session_id(payload)

    store_path = _resolve_store_path()

    # Issue #71 D: correction capture runs BEFORE any delivery return and in
    # BOTH modes — it writes only the local sidecar queue, never the store.
    try:
        _capture_correction(_extract_user_message(payload), session, store_path)
    except Exception:
        pass

    # Issue #71 A: REMOTE mode branches FIRST (final-critic fix): when
    # ZMEM_MCP_URL is set the box's prefetch comes from the LAN MCP server
    # regardless of whether a stale/accidental local store file exists — a
    # leftover local file must never silently downgrade the box to local
    # delivery. Correction capture (above) already ran and stays local.
    if _remote_enabled():
        ctx = _remote_context()
        if ctx:
            _emit_context(ctx)
            return 0
        _emit_empty()
        return 0

    conn = _connect()
    if conn is None:
        _emit_empty()
        return 0

    try:
        # Priority 1: pending failure nudge (armed by the convention hook).
        fail_key = _PENDING_FAILURE_KEY.format(session=session)
        if _meta_get(conn, fail_key):
            _emit_context(_failure_nudge(session))
            _meta_clear(conn, fail_key)
            # Also clear the one-shot captured marker so a subsequent failure
            # in this session can re-arm a nudge. Without this, the
            # convention hook's _FAILURE_CAPTURED_KEY would suppress every
            # failure after the first delivered one.
            _meta_clear(conn, _FAILURE_CAPTURED_KEY.format(session=session))
            return 0

        # Priority 1.5 (issue #90 / #85 C): operation-context recall on fresh
        # ring verbs. Independent at-most-once marker, so it never starves or
        # duplicates the nudges below.
        ctx = _query_context_delivery(conn, session, store_path)
        if ctx:
            _emit_context(ctx)
            return 0

        # Priority 2: pending convention nudge.
        conv_key = _PENDING_CONVENTION_KEY.format(session=session)
        if _meta_get(conn, conv_key):
            _emit_context(_convention_nudge(session))
            _meta_clear(conn, conv_key)
            return 0

        # Priority 3: generic reflect — once per session if there's substantial
        # signal (>= the convention interval) but no lesson captured yet. This
        # catches the case where the session is winding down and the convention
        # nudge was already delivered+cleared, or the interval boundary was
        # never hit. Gated on the interval so it doesn't fire after a single
        # tool call.
        try:
            _interval = max(1, int(os.environ.get("ZMEM_CONVENTION_INTERVAL", "10")))
        except ValueError:
            _interval = 10
        prompted_key = _REFLECT_PROMPTED_KEY.format(session=session)
        if _meta_get(conn, prompted_key):
            _emit_empty()
            return 0
        count_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (_CONVENTION_COUNT_KEY.format(session=session),),
        ).fetchone()
        count = int(count_row[0]) if count_row and str(count_row[0]).isdigit() else 0
        if count < _interval:
            _emit_empty()
            return 0
        if _has_lesson(conn, session):
            # Already captured — mark prompted so we don't re-query every turn.
            _meta_clear(conn, prompted_key)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (prompted_key,),
            )
            conn.commit()
            _emit_empty()
            return 0
        _emit_context(_reflect_nudge(session, count))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'",
            (prompted_key,),
        )
        conn.commit()
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(f"zmem-reflect: sqlite error: {exc}\n")
        _emit_empty()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
