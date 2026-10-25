"""Per-machine fleet broker (issue #287, Workstream T PR 8).

The broker is a LIBRARY (imported by the hook process through
``fleet/broker_cli.py`` — never a long-lived daemon) that gives remote
Claude Code, Codex and ZCode sessions passive memory against a zmem hub:

- It resolves session identity, the git-derived namespace and the Tier-0
  core.md section CLIENT-SIDE (the hub must never receive a client
  filesystem path, and only the client can read its own git remote and
  transcripts).
- Read moments call the hub's versioned API under a propagated deadline
  with a T-05 reservation, emit the hub envelope's ``rendered`` member
  (fence-neutralized exactly like the bash wrappers do), and acknowledge
  delivery.
- Stop / SubagentStop extract bounded, redacted failure evidence from the
  CLIENT transcript (the subagent's own transcript for SubagentStop) and
  send it as DATA to the hub's ``stop_evidence`` tool — count plus capped
  details, secrets AND paths scrubbed (``redact_training_text``), never a
  path.
- Every failure fails open within the launcher budget: the hook envelope
  is returned with an empty ``additionalContext`` and ONE JSON line is
  appended to ``<data_dir>/broker-log.jsonl`` carrying the DISTINCT
  ``hub-unreachable`` reason for connect/deadline classes (issue #287
  AC4 — never the honest empty-store ``empty-pool``).

Remote mode is selected ONLY by ``ZMEM_REMOTE_URL`` (``ZMEM_MCP_URL`` stays
a Hermes-side alias; with the variable unset every hook runs the unchanged
local path).  ``hello`` is never a pre-flight gate here: protocol safety
rides the per-call ``client_protocol`` and the hub's own gates.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

# Bootstrap sibling imports (host.py, redaction.py, storelib/) — the same
# tree the hooks use for ``store.py``.  Idempotent so an embedding test can
# pre-pin sys.path itself.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fleet import transport  # noqa: E402  (after the sys.path bootstrap)

REMOTE_URL_ENV = "ZMEM_REMOTE_URL"

# The bash wrappers' sentinel/fence neutralization table (zmem-recall.sh:
# 139-142, zmem-session-start-payload.py:34-37): the LOCAL path neutralizes
# these markers in the bash layer, NOT in the launcher, so the broker must
# apply the identical pass or remote envelopes would carry plain markers.
_NEUTRALIZE_TABLE = (
    ("<<<ZMEM_JSON>>>", "<<<ZMEM_JSON_NEUTRALIZED>>>"),
    ("<<<END>>>", "<<<END_NEUTRALIZED>>>"),
    ("<<<ZMEM_UNTRUSTED_FENCE>>>", "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
    ("<<<END_ZMEM_UNTRUSTED_FENCE>>>", "<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
)

# The #257 detail caps the local reflect path enforces (zmem-reflect.sh
# _clean_field :408-409, SUBAGENT_DETAIL_LIMIT :418): whitespace collapse +
# 200-char cap, at most 5 detail entries.
DETAIL_LIMIT = 5
DETAIL_CHAR_CAP = 200
# The raw per-detail read bound BEFORE redaction (redaction must see the
# complete token — a secret straddling the 200-char display cap is only
# caught if the redactor runs on the longer raw text first).
_RAW_EVIDENCE_CAP = 4000

# Per-moment hub-call matrix mirroring hooks/lib/zmem-recall-body.py (the
# query derivation, recall-vs-recent intent, wrapper limits and moment
# attribution; the hub selector owns its own limits/policy — disclosed in
# docs/FLEET.md).
_QUERY_CAP = 500
_USER_PROMPT_REWRITE_INPUT_CAP = 4000

_TIER0_HEADER = "# Loaded from memory (Tier 0 — core.md, user-level):\n\n"
_TIER0_CLOSER = "\n<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"

_BROKER_LOG_NAME = "broker-log.jsonl"
_BROKER_LOG_MAX_LINES = 10000


def is_remote_mode(env: Mapping[str, str] | None = None) -> bool:
    """True iff ``ZMEM_REMOTE_URL`` is set nonempty (hooks-only selector)."""
    source = os.environ if env is None else env
    return bool((source.get(REMOTE_URL_ENV, "") or "").strip())


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _lane(env: Mapping[str, str]) -> str:
    """The host lane the local recall path derives (recall-body :457-462) —
    mirrored so a remote envelope carries the SAME lane (and fence flavor)
    as the local path would."""
    return {
        "claude": "claude", "codex": "codex", "zcode": "zcode",
        "hermes": "hermes-provider", "hermes-provider": "hermes-provider",
        "hermes-compat": "hermes-compat",
    }.get((env.get("ZMEM_HOST", "zcode") or "zcode").strip().lower(), "")


def _neutralize(text: str) -> str:
    """The bash-wrapper fence/sentinel neutralization pass (see table)."""
    for marker, neutralized in _NEUTRALIZE_TABLE:
        text = text.replace(marker, neutralized)
    return text


def _event_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_input_text(tool_input: Any) -> str:
    """The per-call descriptor the local pretool path derives (recall-body
    :474-485: command / file_path / notebook_path / path, backslashes
    normalized)."""
    if not isinstance(tool_input, Mapping):
        return ""
    values = []
    for key in ("command", "file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            values.append(value.replace("\\", "/")[:_QUERY_CAP])
    return " ".join(values)[:_QUERY_CAP]


def _posttoolbatch_query(payload: Mapping[str, Any]) -> str:
    """The local batch derivation (recall-body :507-510): one whitespace-
    collapsed descriptor per tool use, newline-joined."""
    uses = payload.get("tool_uses")
    records = uses if isinstance(uses, list) else []
    if not records and payload.get("tool_name"):
        records = [{"name": payload.get("tool_name"),
                    "input": payload.get("tool_input")}]
    events = []
    for use in records:
        if not isinstance(use, Mapping):
            continue
        name = use.get("name")
        text = _tool_input_text(use.get("input"))
        parts = []
        if isinstance(name, str) and name:
            parts.append(name[:_QUERY_CAP])
        if text:
            parts.append(text)
        if parts:
            events.append(" ".join(parts))
    return "\n".join(events)[:_QUERY_CAP]


def _query_for(event: str, payload: Mapping[str, Any]) -> str:
    """The client-side query derivation per moment (recall-body :640-661):
    precompact is queryless; user_prompt caps at the rewrite input cap;
    subagent reads the prompt/task/task_text/description fields (agent_type
    rides argv and is discarded locally); PreToolUse and PostToolBatch
    derive from the tool input like the local path; everything else caps at
    500."""
    if event == "PreCompact":
        return ""
    if event == "SubagentStart":
        return _event_text(
            payload, "prompt", "task", "task_text", "description")[:_QUERY_CAP]
    if event == "PostToolBatch":
        return _posttoolbatch_query(payload)
    if event == "PreToolUse":
        text = _tool_input_text(payload.get("tool_input"))
        if text:
            return text
        return _delegation_prompt(payload)
    value = _event_text(payload, "prompt", "query")
    if event == "UserPromptSubmit":
        return value[:_USER_PROMPT_REWRITE_INPUT_CAP + 1]
    return value[:_QUERY_CAP]


def _moment_for(event: str) -> str:
    """Decision-moment attribution (recall-body _decision_moment :453-454):
    PostToolBatch attributes as ``pretool``."""
    if event == "PostToolBatch":
        return "pretool"
    return {
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pretool",
        "SubagentStart": "subagent",
        "PreCompact": "precompact",
        "SessionStart": "session_start",
    }.get(event, event.lower())


def _delegation_prompt(payload: Mapping[str, Any]) -> str:
    """PreToolUse Agent/Task delegation prompt (recall-body :797 parity)."""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, Mapping):
        for key in ("prompt", "task", "description"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_QUERY_CAP]
    return ""


def _data_dir() -> Path:
    """The broker's data directory: the resolved store path's parent (the
    same chain the hooks document: ZMEM_STORE > ZMEM_DATA > plugin data >
    ~/.zmem)."""
    import host  # noqa: PLC0415 - lazy: reads env at call time
    return Path(host.resolve_store_path()).expanduser().parent


def _log_record(record: dict[str, Any]) -> None:
    """Append one JSON line to ``<data_dir>/broker-log.jsonl`` (append-only,
    line-capped ring; every write failure is swallowed — logging must never
    break the fail-open hook contract)."""
    try:
        directory = _data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _BROKER_LOG_NAME
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        lines = [l for l in existing.splitlines() if l.strip()]
        lines.append(line)
        if len(lines) > _BROKER_LOG_MAX_LINES:
            lines = lines[-_BROKER_LOG_MAX_LINES:]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - fail-open logging
        pass


def _namespace(payload: Mapping[str, Any],
               env: Mapping[str, str] | None = None) -> str:
    """Client-side namespace resolution (the hub validates it itself).

    The launcher already resolved ``ZMEM_NAMESPACE`` for the moments that
    read it and hands it to this process in the child env — honor it first
    so a remote moment keys to the SAME project the local path would use;
    the cwd fallback below would resolve the PLUGIN checkout (the launcher
    spawns the broker with ``cwd: getPluginRoot()``), not the user's
    project."""
    source = _source(env)
    pinned = (source.get("ZMEM_NAMESPACE", "") or "").strip()
    if pinned:
        return pinned
    try:
        import host  # noqa: PLC0415 - lazy import under the pinned env
        cwd = payload.get("cwd") or os.getcwd()
        return host.resolve_namespace(cwd, write_cache=False)
    except Exception:  # noqa: BLE001 - fail open to the global namespace
        return "user:global"


def _remaining_ms(started: float, env: Mapping[str, str] | None) -> int:
    """The launcher's whole watchdog (12 s) covers EVERYTHING this process
    does — namespace resolution, every hub leg, the ack — so each hub call
    gets the REMAINING wall budget, never a fresh full deadline (a hung hub
    must hit the broker's own fail-open path before the launcher kills us;
    issue #287 AC4)."""
    deadline_s = transport.resolve_deadline_s(_source(env))
    remaining = deadline_s - (time.monotonic() - started)
    if remaining < 0.05:
        return 0
    return max(1, int(remaining * 1000.0))


def _http(env: Mapping[str, str]) -> transport.McpHttp:
    deadline_s = transport.resolve_deadline_s(env)
    return transport.McpHttp(
        url=(env.get(REMOTE_URL_ENV, "") or "").strip(),
        executor=transport.DeadlineExecutor(),
        deadline_s=deadline_s,
    )


# --- T-09 durable op queue: read-your-writes overlay + flush -----------

def pending_overlay(principal: str, env=None) -> str:
    """Read-your-writes (issue #288 AC3): the principal's pending op-queue
    entries rendered as bounded ``[PENDING]`` lines (empty when none)."""
    try:
        from fleet import opqueue  # noqa: PLC0415 - lazy import
        return opqueue.render_pending("", principal, env)
    except Exception:  # noqa: BLE001 - overlay is fail-open
        return ""


def _flush_queue(http: transport.McpHttp, env: Mapping[str, str],
                 started: float) -> None:
    """Fire-and-forget op-queue flush under the REMAINING deadline; broker-log
    lines carry reason + op count only, never op content."""
    try:
        from fleet import opqueue  # noqa: PLC0415 - lazy import
        remaining_ms = _remaining_ms(started, env)
        if remaining_ms <= 0:
            return
        rows = opqueue.pending("local", env)
        if not rows:
            return
        result = opqueue.flush("local", http, env,
                               deadline_ms=remaining_ms)
        if isinstance(result, dict) and result.get("ok") is not True:
            # The hub's refusal shapes carry `error` (not `reason`) — log
            # whichever is present so a stuck queue is diagnosable.
            reason = str(result.get("error") or result.get("reason")
                         or "flush-failed")[:80]
            _log_record({
                "ts": int(time.time()), "event": "opqueue-flush",
                "reason": reason,
                "pending": len(rows),
                "elapsed_ms": int((time.monotonic() - started) * 1000.0),
            })
    except Exception as exc:  # noqa: BLE001 - fire-and-forget
        _log_record({
            "ts": int(time.time()), "event": "opqueue-flush",
            "reason": "flush-error",
            "detail": type(exc).__name__,
            "elapsed_ms": int((time.monotonic() - started) * 1000.0),
        })


def _fail_open(event: str, reason: str, detail: str,
               started: float) -> dict[str, str]:
    _log_record({
        "ts": int(time.time()),
        "event": event,
        "reason": reason,
        "detail": detail[:300],
        "elapsed_ms": int((time.monotonic() - started) * 1000.0),
    })
    return {"additionalContext": ""}


def _emit_envelope(envelope: dict[str, Any]) -> dict[str, str]:
    rendered = envelope.get("rendered")
    text = rendered if isinstance(rendered, str) else ""
    return {"additionalContext": _neutralize(text)}


def _ack_reservation(http: transport.McpHttp, env: Mapping[str, str],
                     envelope: dict[str, Any], started: float) -> None:
    """T-05 fire-and-forget ack: only when a reservation key came back and
    the remaining budget is positive; every failure is swallowed (the
    delivery already happened; the ledger reaps unacked rows lazily)."""
    reservation = envelope.get("reservation")
    if not isinstance(reservation, str) or not reservation:
        return
    remaining_ms = _remaining_ms(started, env)
    if remaining_ms <= 0:
        return
    try:
        http.prefetch_ack(reservation, deadline_ms=remaining_ms)
    except Exception:  # noqa: BLE001 - fire-and-forget
        pass


# --- read moments ------------------------------------------------------

def _handle_read(http: transport.McpHttp, env: Mapping[str, str], event: str,
                 payload: Mapping[str, Any], started: float,
                 namespace: Optional[str] = None) -> dict[str, str]:
    lane = _lane(env)
    session_id = _event_text(payload, "session_id", "sessionId")
    moment = _moment_for(event)
    ns = namespace or _namespace(payload, env)
    remaining_ms = _remaining_ms(started, env)
    # Bound for EVERY event before the branch below: the unreachable
    # serve (issue #290) references it on the SessionStart path too,
    # where no query exists (the live leg pulls recency, not a query —
    # the offline serve degrades to the empty-pool envelope there).
    query = ""
    try:
        if event == "SessionStart":
            envelope = http.session_start(ns, session_id or "", lane=lane,
                                          deadline_ms=remaining_ms)
        else:
            query = _query_for(event, payload)
            if event == "PreToolUse" and not query:
                query = _delegation_prompt(payload)
            envelope = http.prefetch(
                query, namespace=ns, session_id=session_id, moment=moment,
                ops_tokens=[], lane=lane, reserve=True,
                deadline_ms=remaining_ms)
    except Exception as exc:  # noqa: BLE001 - fail open
        return _fail_open(event, "broker-error", str(exc), started)
    # Read-your-writes (issue #288 AC3) holds in BOTH hub states: render
    # the pending overlay FIRST, flush (which may clear some entries), then
    # compose — the overlay is what the operator queued and every line is
    # neutralized, so the hub being unreachable never suppresses it.
    overlay = pending_overlay("local", env)
    if envelope.get("reason") == transport.REASON_HUB_UNREACHABLE:
        # Issue #290 (T-11): the declared offline mode.  Record the
        # failure for the reconnect transition (the persisted stamp is
        # the only cross-moment memory — every broker moment is a fresh
        # broker_cli subprocess), then serve from the snapshot when one
        # is valid: state none keeps the exact T-08 fail-open contract.
        from fleet import snapshot as fleet_snapshot  # noqa: PLC0415
        try:
            fleet_snapshot.write_stamp(env)
        except OSError:
            pass
        envelope_off, state = fleet_snapshot._serve_offline_read(  # noqa: SLF001
            env, query=query, namespace=ns, moment=moment,
            session_id=session_id or "", lane=lane)
        if state == "expired":
            _log_record({
                "ts": time.time(), "event": event,
                "reason": "snapshot-expired",
                "detail": "offline snapshot past expires_at; serving nothing",
                "elapsed_ms": int((time.monotonic() - started) * 1000)})
            text = ""
            if overlay:
                text = _neutralize(overlay)
            return {"additionalContext": text}
        if envelope_off is not None and state == "valid":
            served = _emit_envelope(envelope_off)
            if overlay:
                base_text = served.get("additionalContext", "")
                served["additionalContext"] = _neutralize(
                    base_text + "\n" + overlay if base_text else overlay)
            return served
        fail = _fail_open(event, transport.REASON_HUB_UNREACHABLE,
                          "read moment hub call unreachable", started)
        if overlay:
            fail["additionalContext"] = _neutralize(
                overlay) if not fail["additionalContext"] else _neutralize(
                    fail["additionalContext"] + "\n" + overlay)
        return fail
    _ack_reservation(http, env, envelope, started)
    result = _emit_envelope(envelope)
    _flush_queue(http, env, started)
    # Issue #290 (T-11): a successful hub contact is a (re)connection —
    # best-effort snapshot refresh under a bounded slice AFTER the live
    # result was chosen (never blocks the moment).  _maybe_refresh fires
    # only on none/stale/the persisted transition, and clears the stamp
    # only when a refresh completed.
    from fleet import snapshot as fleet_snapshot  # noqa: PLC0415
    try:
        # Recompute the slice AFTER the live read: the entry-time
        # remaining_ms is stale by the hub call's duration and would let
        # the refresh run past the moment's own deadline.
        fleet_snapshot._maybe_refresh(  # noqa: SLF001
            http, env,
            budget_ms=max(1, min(2000, _remaining_ms(started, env) // 4)))
    except Exception:  # noqa: BLE001 - refresh is best-effort, never fatal
        pass
    if overlay:
        base_text = result.get("additionalContext", "")
        result = {"additionalContext": _neutralize(
            (base_text + "\n" + overlay) if base_text else overlay)}
    return result


# --- Stop / SubagentStop evidence --------------------------------------

def _clean_field(text: Any) -> str:
    """The #257 shape: collapse whitespace, cap at 200 chars."""
    return re.sub(r"\s+", " ", str(text)).strip()[:DETAIL_CHAR_CAP]


def _extract_evidence(payload: Mapping[str, Any], *,
                      agent: bool) -> Optional[tuple[list[dict[str, str]], int]]:
    """Bounded, redacted, path-free failure evidence from the CLIENT
    transcript (the subagent's own transcript when ``agent``; the parent
    transcript is never opened for SubagentStop).

    Returns ``(bounded_details, true_total)`` — the hub's summary must
    carry the TRUE failure count, not the post-cap count (the local path
    reports "showing most recent 5 of N").  Redaction runs BEFORE the cap:
    a token that straddles the 200-char boundary would otherwise survive
    redaction as a recognizable truncated prefix."""
    path_value = (payload.get("agent_transcript_path") if agent
                  else payload.get("transcript_path")) or ""
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    import redaction  # noqa: PLC0415 - lazy import under the pinned env
    from storelib import mine  # noqa: PLC0415 - same
    try:
        details, _rejections = mine._failures_from_transcript(path_value)
    except Exception:  # noqa: BLE001 - a broken transcript is no evidence
        return None
    bounded: list[dict[str, str]] = []
    for detail in details[:DETAIL_LIMIT]:
        if not isinstance(detail, dict):
            continue
        # Redact BEFORE the 200-char cap: a token straddling the cap
        # boundary would otherwise survive as a recognizable truncated
        # prefix (the secret patterns need the full token shape). The raw
        # text is bounded at a generous read cap first so redaction cost
        # stays bounded.
        error_text, _count = redaction.redact_training_text(
            str(detail.get("error", ""))[:_RAW_EVIDENCE_CAP])
        tool_text, _tool_count = redaction.redact_training_text(
            str(detail.get("tool", "?"))[:_RAW_EVIDENCE_CAP])
        bounded.append({"tool": _clean_field(tool_text),
                        "error": _clean_field(error_text)})
    return bounded, len(details)


def _handle_stop(http: transport.McpHttp, env: Mapping[str, str], event: str,
                 payload: Mapping[str, Any], started: float) -> dict[str, str]:
    agent = event == "SubagentStop"
    # The loop guard applies to BOTH Stop verbs (the local wrappers no-op on
    # stop_hook_active — zmem-reflect.sh:241, zmem-subagent-reflect.sh:236 —
    # with the empirically documented stop-loop rationale); a remote Stop
    # with stop_hook_active=true must not re-call the hub and re-inject the
    # summary on every re-fire.
    if payload.get("stop_hook_active"):
        return {"additionalContext": ""}
    # The #204 subagent-marker guard: a Stop payload carrying subagent
    # markers belongs to the subagent lane, not the parent's Stop (the
    # local reflect emits {} for it — zmem-reflect.sh:247-248).
    if not agent and (payload.get("agent_id") or payload.get("agent_transcript_path")):
        return {"additionalContext": ""}
    evidence_pack = _extract_evidence(payload, agent=agent)
    if not evidence_pack:
        # No readable transcript or zero extracted failures: nothing to
        # send (SubagentStop with no agent transcript mirrors subagent-
        # reflect :152-153 — never the parent transcript), and the moment
        # stays silent — a "0 failed tool call(s)" status line would fire
        # the hub write on EVERY Stop, which the local gate never does.
        return {"additionalContext": ""}
    evidence, true_total = evidence_pack
    ns = _namespace(payload, env)
    session_id = _event_text(payload, "session_id", "sessionId")
    agent_id = (payload.get("agent_id") or "") if agent else ""
    try:
        result = http.stop_evidence(
            ns, session_id=session_id, agent_id=agent_id or None,
            count=true_total, details=evidence,
            deadline_ms=_remaining_ms(started, env))
    except Exception as exc:  # noqa: BLE001 - fail open
        return _fail_open(event, "broker-error", str(exc), started)
    # The hub's refusal shapes (`_error`, `_protocol_refusal`,
    # `_deadline_response`) carry an `error` key WITHOUT `ok` — only treat
    # an EXPLICIT ok=True as success, or a refused/failed submission is
    # announced to the model as captured while the hub recorded nothing.
    if not (isinstance(result, dict) and result.get("ok") is True):
        if isinstance(result, dict) and result.get("reason") == (
                transport.REASON_HUB_UNREACHABLE):
            return _fail_open(event, transport.REASON_HUB_UNREACHABLE,
                              "stop_evidence unreachable", started)
        # A structured hub refusal (path-shaped field, namespace guard,
        # deadline, protocol): log it loudly but never block the hook.
        detail = str(result.get("error") or result.get("reason")
                     or "refused") if isinstance(result, dict) else "refused"
        return _fail_open(event, "hub-refused", detail, started)
    summary = _clean_field(
        f"{true_total} failed tool call(s) captured for reflection.")
    if agent:
        # SubagentStop must NEVER inject additionalContext: Claude Code
        # re-runs the subagent turn when a SubagentStop hook returns
        # additionalContext (the issue #204 deliverable-loss hazard the
        # local wrapper documents — zmem-subagent-reflect.sh:10-21 and
        # zmem-launch.js:59-64). The evidence was still delivered to the
        # hub above; the envelope stays empty.
        return {"additionalContext": ""}
    return {"additionalContext": _neutralize(summary)}


# --- Tier 0 (hub-owned core.md) ----------------------------------------

def _tier0_cache_paths() -> tuple[Path, Path]:
    # Under the resolved data dir (the ZMEM_STORE > ZMEM_DATA > plugin data
    # > ~/.zmem chain), NOT Path.home() — a reconfigured box would otherwise
    # keep the cache outside its configured store and share it across
    # distinct stores.
    return _data_dir() / "broker" / "core.md", \
        _data_dir() / "broker" / "core.md.revision"


def _tier0_section(http: transport.McpHttp, env: Mapping[str, str],
                   event: str, started: float) -> str:
    """The hub-owned canonical core.md section with the unreachable-cache
    fallback (issue #287 Tier-0 constraint).  A failed fetch with NO cache
    fails open to no section — Tier 0 never blocks the store envelope.  A
    local ~/.zmem/core.md is IGNORED in remote mode."""
    try:
        result = http.tier0_get(deadline_ms=_remaining_ms(started, env))
    except Exception:  # noqa: BLE001 - fall through to the cache
        result = None
    if isinstance(result, dict) and result.get("ok") is True and isinstance(
            result.get("content"), str) and result["content"].strip():
        # An empty body must NOT clobber a good cache (an empty hub file is
        # a server-side accident, not a cache invalidation).
        content = result["content"]
        revision = result.get("revision")
        try:
            core_path, revision_path = _tier0_cache_paths()
            core_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = core_path.with_name(core_path.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, core_path)
            revision_path.write_text(
                str(revision or transport.tier0_revision(content)),
                encoding="utf-8")
        except Exception:  # noqa: BLE001 - cache write is best-effort
            pass
        return _tier0_render(content)
    # Unreachable (or refused): inject the cached copy when present.
    core_path, _revision_path = _tier0_cache_paths()
    if isinstance(result, dict) and result.get("ok") is False \
            and result.get("error") == "no-core-md":
        # The hub answered and owns the file: it says there is nothing to
        # serve, so a stale local cache must not shadow the hub's answer.
        try:
            core_path.unlink(missing_ok=True)
        except OSError:  # noqa: BLE001 - best-effort invalidation
            pass
        return ""
    try:
        cached = core_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        cached = ""
    if cached.strip():
        return _tier0_render(cached)
    if isinstance(result, dict) and result.get("reason") == (
            transport.REASON_HUB_UNREACHABLE):
        _log_record({
            "ts": int(time.time()), "event": event,
            "reason": transport.REASON_HUB_UNREACHABLE,
            "detail": "tier0 fetch unreachable; no cache yet",
            "elapsed_ms": int((time.monotonic() - started) * 1000.0),
        })
    return ""


def _tier0_render(content: str) -> str:
    # The local SessionStart path neutralizes the ENTIRE payload including
    # core.md (zmem-session-start-payload.py:98-103); the broker must too —
    # a hub core.md carrying a live fence/sentinel marker would otherwise
    # reach model-visible context (fence spoofing) or mis-anchor the
    # launcher's extractPayload (review finding 1).
    text = _TIER0_HEADER + _neutralize(content.rstrip())
    if not text.endswith(_TIER0_CLOSER):
        text += _TIER0_CLOSER
    return text


# --- the per-moment entry ----------------------------------------------

_HANDLED_EVENTS = frozenset({
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolBatch",
    "SubagentStart", "PreCompact", "Stop", "SubagentStop",
})


def handled_events() -> frozenset[str]:
    """The moments the broker covers (the launcher's verb routing table)."""
    return _HANDLED_EVENTS


def handle_event(event: str, payload: Mapping[str, Any],
                 env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Serve one hook moment in remote mode; ALWAYS returns the fail-open
    hook envelope ``{"additionalContext": str}``.

    ``env`` is read at CALL time (``os.environ`` when omitted).  Unknown
    events return the empty envelope unchanged (the launcher only routes
    broker-covered verbs here).
    """
    started = time.monotonic()
    source = _source(env)
    if event not in _HANDLED_EVENTS:
        return {"additionalContext": ""}
    if not is_remote_mode(source):
        return {"additionalContext": ""}
    try:
        return _handle_event_inner(event, payload, source, started)
    except ValueError as exc:
        # Issue #291: a StoreConfigError (a directory variable naming an
        # existing file) can surface from ANY of the seam calls below
        # (pending_overlay's opqueue, the snapshot lane, the queue flush) —
        # the direct-API fail-open contract requires the envelope, never a
        # traceback. Only the config-refusal ValueError degrades; any other
        # ValueError is a real defect and still raises.
        try:
            import host as _host  # noqa: PLC0415
            if not isinstance(exc, _host.StoreConfigError):
                raise
        except ImportError:
            raise
        return _fail_open(event, "store-config-refused", str(exc), started)


def _handle_event_inner(event: str, payload: Mapping[str, Any],
                        source: dict[str, str], started: float) -> dict[str, str]:
    http = _http(source)
    if event == "Stop" or event == "SubagentStop":
        return _handle_stop(http, source, event, payload, started)
    if event == "SessionStart":
        # Tier 0 first (hub-owned core.md, cached fallback), then the store
        # envelope — the local SessionStart flushes Tier 0 ahead of Tier 2
        # (zmem-session-start.sh:390-391); the broker mirrors the order.
        tier0 = _tier0_section(http, source, event, started)
        envelope = _handle_read(
            http, source, event, payload, started)
        context = envelope.get("additionalContext", "")
        if tier0 and context:
            combined = tier0 + "\n\n" + context
        else:
            combined = tier0 or context
        return {"additionalContext": combined}
    return _handle_read(http, source, event, payload, started)
