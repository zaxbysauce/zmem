#!/usr/bin/env python
"""SessionStart payload builder with a subprocess-only Tier-2 lane.

Tier 0 is emitted before the store call. The store subprocess returns the
canonical rendered fence; this adapter never imports or renders memory rows.

Args (all optional beyond 1, IndexError-tolerant):
  1 core.md path          2 AGENTS.md path       3 store.py path
  4 data dir (native)     5 project dir          6 data dir (bash-resolved)
  7 namespace             8 ctx budget           9 host
 10 settings dir         11 nudge marker        12 session id
 13 drift JSON           14 session source (issue #118)
 15 validated host lane (issue #153, optional)

Decision lines carry the optional issue #153 suffix ``lane``, ``ver``, and
``t_ms`` after ``moment`` when the host lane and release manifest validate;
otherwise the complete legacy line is retained.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time

_SENTINEL_START = "<<<ZMEM_JSON>>>"
_SENTINEL_END = "<<<END>>>"
_NEUTRALIZE = (
    ("<<<ZMEM_JSON>>>", "<<<ZMEM_JSON_NEUTRALIZED>>>"),
    ("<<<END>>>", "<<<END_NEUTRALIZED>>>"),
    ("<<<ZMEM_UNTRUSTED_FENCE>>>", "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
    ("<<<END_ZMEM_UNTRUSTED_FENCE>>>", "<<<END_ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>"),
)

# Issue #153: decision-line attribution fails closed to the legacy line
# shape.  The lane vocabulary is the same closed set the store selector
# validates (``--lane``) and matches ``_lane`` below.
_ATTR_LANES = ("claude", "codex", "zcode", "hermes-provider", "hermes-compat")
_ATTR_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _validated_attribution_lane(host):
    """Return a closed-set host lane, or ``None`` for legacy output."""
    if not isinstance(host, str):
        return None
    value = host.strip()
    return value if value in _ATTR_LANES else None


def _release_version():
    """Read the served tree semver; malformed manifests retain legacy lines."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        candidates = [os.path.join(root, "release-manifest.json")]
        home = os.environ.get("ZMEM_HOME", "").strip()
        if home:
            candidates.append(os.path.join(os.path.expanduser(home),
                                           "release-manifest.json"))
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as fh:
                    value = json.load(fh).get("version")
                if isinstance(value, str) and _ATTR_VERSION_RE.fullmatch(value):
                    return value
            except (OSError, ValueError, TypeError):
                continue
    except Exception:
        pass
    return None


def _rounded_elapsed_ms(started):
    """Return a nonnegative rounded duration for one store attempt."""
    try:
        return max(0, int(round((time.perf_counter() - started) * 1000)))
    except Exception:
        return 0


def _format_attribution(lane, version, t_ms):
    """Serialize validated issue #153 attribution fields, or nothing."""
    if ((lane is None or lane in _ATTR_LANES)
            and isinstance(version, str)
            and _ATTR_VERSION_RE.fullmatch(version)
            and isinstance(t_ms, int) and not isinstance(t_ms, bool)
            and t_ms >= 0):
        lane_field = " lane=%s" % lane if lane is not None else ""
        return "%s ver=%s t_ms=%d" % (lane_field, version, t_ms)
    return ""


def _emit(payload):
    text = json.dumps(payload, ensure_ascii=False) if payload else "{}"
    for marker, replacement in _NEUTRALIZE:
        text = text.replace(marker, replacement)
    sys.stdout.write(_SENTINEL_START + text + _SENTINEL_END + "\n")
    sys.stdout.flush()


def _budget_default_s(key, fallback_s):
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "timeout-budget.json")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value / 1000.0
    except Exception:
        pass
    return fallback_s


_store_timeout_warned = False


def _store_timeout_s():
    global _store_timeout_warned
    raw = os.environ.get("ZMEM_STORE_RECALL_TIMEOUT_S", "")
    value = _budget_default_s("store_recall_ms", 8.0)
    warned = False
    if raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value, warned = 8.0, True
        if not math.isfinite(value) or value <= 0 or value > 8.0:
            value, warned = 8.0, True
    if warned and not _store_timeout_warned:
        _store_timeout_warned = True
        try:
            sys.stderr.write("zmem: invalid ZMEM_STORE_RECALL_TIMEOUT_S=%r; using 8.0\n" % raw)
        except Exception:
            pass
    return value


def _data_dir():
    store = os.environ.get("ZMEM_STORE", "")
    if store and os.path.dirname(store):
        return os.path.expanduser(os.path.dirname(store))
    for key in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        value = os.environ.get(key, "")
        if value:
            return os.path.expanduser(value)
    return os.path.join(os.path.expanduser("~"), ".zmem")


def _safe_sid(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "")[:128] or "unknown"


def _safe_label(value, cap=128):
    """Keep audit-list values on one parseable decision-log line."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))[:cap] or "unknown"


def _excluded_count(value):
    """Accept the selector's ID list and legacy numeric envelopes."""
    try:
        return len(value) if isinstance(value, list) else int(value or 0)
    except (TypeError, ValueError):
        return 0


def _anonymous_session_id():
    """Isolate manual/legacy invocations that have no host session id."""
    return "anonymous-%d-%d" % (os.getpid(), time.time_ns())


def _rotate_log(path):
    """Rotate before append through the stdlib-only hook adapter."""
    try:
        rotator = os.path.join(os.path.dirname(__file__), "zmem-log-rotate.py")
        subprocess.run([sys.executable, rotator, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2, check=False)
    except Exception:
        pass


def _write_decision_line(text, data_dir=""):
    """Append a SessionStart decision line, fail-open.

    The audit tail carries a sanitized ``sid=`` value; host events without a
    session id are represented as ``sid=unknown``.
    """
    try:
        # The environment is canonical in launched hooks.  The argv data-dir
        # remains the back-compat fallback for direct payload invocations where
        # no host resolver exported a data location.
        configured = any(os.environ.get(key, "") for key in
                         ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
                          "ZCODE_PLUGIN_DATA"))
        log_dir = _data_dir() if configured else (os.path.expanduser(data_dir)
                                                   if data_dir else _data_dir())
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "zmem-decisions.log")
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        pass


def _read_file(path):
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _soft_trim(text: str, budget: int) -> str:
    if not text or budget <= 0 or len(text) <= budget:
        return text
    closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
    if closer in text:
        head = max(0, budget - len(closer) - 28)
        return text[:head].rstrip() + "\n" + closer + "\n[recall truncated]"
    return text[:budget].rstrip() + "\n[context truncated]"


def build_tier0_context(core, agents, host):
    parts = []
    core_text = _read_file(core)
    if core_text:
        parts.append("# Loaded from memory (Tier 0 — core.md, user-level):\n\n" + core_text)
    if host != "claude":
        agents_text = _read_file(agents)
        if agents_text:
            parts.append("# Loaded from memory (Tier 0 — AGENTS.md, project-level):\n\n" + agents_text)
    return "\n\n".join(parts)


def _native_nudge(host, settings_dir, marker):
    if host != "claude" or not marker:
        return ""
    try:
        already = os.path.isfile(marker)
        disabled = bool(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
        if not disabled and settings_dir:
            for name in ("settings.json", "settings.local.json"):
                try:
                    with open(os.path.join(settings_dir, name), encoding="utf-8") as handle:
                        if json.load(handle).get("autoMemoryEnabled") is False:
                            disabled = True
                            break
                except (OSError, ValueError, AttributeError):
                    pass
        if already or disabled:
            return ""
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, b"shown\n")
            finally:
                os.close(fd)
        except FileExistsError:
            return ""
        except OSError:
            pass
        return ("# ZMem notice (one-time): Claude Code native memory still looks enabled. "
                "ZMem is replacing it as your sole memory system - add "
                '\"autoMemoryEnabled\": false to ~/.claude/settings.json so the two '
                "systems do not double-run (a plugin cannot set this for you).")
    except Exception:
        return ""


def _lane(host):
    return {"claude": "claude", "codex": "codex", "zcode": "zcode",
            "hermes": "hermes-provider", "hermes-provider": "hermes-provider",
            "hermes-compat": "hermes-compat"}.get((host or "zcode").lower(), "")


def _run_store(store_py, args):
    command = [sys.executable, store_py, *args]
    try:
        raw = subprocess.check_output(command, stderr=subprocess.DEVNULL,
                                      timeout=_store_timeout_s())
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except subprocess.CalledProcessError:
        return {}
    except Exception:
        return {}


def build_tier2_context(store_py, namespace, session_id, budget, *, host="zcode",
                        context_parts=None, data_dir="", log_session_id=None,
                        log_moment="session_start", attribution_lane=None,
                        attribution_version=None):
    """Fetch one store-owned session-start rendered envelope.

    Issue #153: every decision line this adapter writes can carry the
    validated ``lane``/``ver``/``t_ms`` attribution suffix after ``moment``;
    callers resolve the lane/version once per invocation (``main`` below)
    and invalid or absent values keep the complete legacy line.
    """
    _ = context_parts
    audit_sid = session_id if log_session_id is None else log_session_id
    # ``session_start_compact`` is an audit-only label retained for operators
    # who correlate a host compact restart.  The selector itself must use the
    # canonical store moment ``session_start`` (see argv below).
    decision_moment = ("session_start_compact" if log_moment == "session_start_compact"
                       else "session_start")
    attr_no_attempt = _format_attribution(attribution_lane, attribution_version, 0)
    if not store_py or not os.path.isfile(store_py):
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             attr_no_attempt), data_dir)
        return ""
    lane = _lane(host)
    if not lane:
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             attr_no_attempt), data_dir)
        return ""
    argv = [sys.executable, store_py, "recent", "--namespace", namespace,
            "--limit", "3", "--include-global", "--global-limit", "2",
            "--no-bump", "--for-injection",
            "--json", "--session-id",
            session_id, "--moment", "session_start", "--lane", lane]
    attempt_started = time.perf_counter()
    try:
        raw = subprocess.check_output(argv, stderr=subprocess.DEVNULL,
                                      timeout=_store_timeout_s())
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        envelope = json.loads(raw)
    except subprocess.TimeoutExpired:
        attempt_t_ms = _rounded_elapsed_ms(attempt_started)
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s store_timeout=1\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             _format_attribution(attribution_lane, attribution_version,
                                 attempt_t_ms)), data_dir)
        return ""
    except subprocess.CalledProcessError:
        attempt_t_ms = _rounded_elapsed_ms(attempt_started)
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             _format_attribution(attribution_lane, attribution_version,
                                 attempt_t_ms)), data_dir)
        return ""
    except Exception:
        attempt_t_ms = _rounded_elapsed_ms(attempt_started)
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             _format_attribution(attribution_lane, attribution_version,
                                 attempt_t_ms)), data_dir)
        return ""
    attempt_t_ms = _rounded_elapsed_ms(attempt_started)
    if not isinstance(envelope, dict):
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=omitted ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(audit_sid), decision_moment,
             _format_attribution(attribution_lane, attribution_version,
                                 attempt_t_ms)), data_dir)
        return ""
    rendered = envelope.get("rendered")
    has_rendered = isinstance(rendered, str) and bool(rendered)
    candidate_ids = envelope.get("candidate_ids")
    if not isinstance(candidate_ids, list):
        candidate_ids = []
    candidate_ids = [str(value) for value in candidate_ids
                     if isinstance(value, str)]
    # Never reconstruct a delivery from raw result rows.  The store's
    # rendered string is the sole context payload consumed by this adapter;
    # IDs are retained solely as decision-log attribution.
    result_rows = envelope.get("results")
    selected_ids = ([row.get("id") for row in result_rows
                     if isinstance(row, dict) and isinstance(row.get("id"), str)]
                    if isinstance(result_rows, list) else [])
    reason = envelope.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "injected" if has_rendered else "empty-pool"
    status = "injected" if has_rendered else "silent"
    tokens_used = envelope.get("tokens_used")
    tokens_budget = envelope.get("tokens_budget")
    margin = envelope.get("margin")
    if isinstance(margin, bool):
        margin = None
    else:
        try:
            margin = "%.6f" % float(margin)
            if not math.isfinite(float(margin)):
                margin = None
        except (TypeError, ValueError):
            margin = None
    pruned = envelope.get("margin_pruned_ids")
    if not (isinstance(pruned, list)
            and all(isinstance(value, str) for value in pruned)):
        pruned = None
    else:
        pruned = [_safe_label(value, 64) for value in pruned]
    fields = ["[%d] zmem-hook" % int(time.time()),
              "status=%s" % status, "reason=%s" % reason,
              "ids=%s" % selected_ids, "all=%s" % candidate_ids]
    if has_rendered and tokens_used is not None:
        fields.append("tokens=%s/%s" % (tokens_used, tokens_budget
                                        if tokens_budget is not None else "-"))
    fields.extend(("exc=%d" % _excluded_count(envelope.get("excluded", 0)),
               "sid=%s" % _safe_sid(audit_sid),
               "moment=%s" % decision_moment))
    # Issue #153: the attribution suffix is inserted after ``moment`` and
    # before every historical additive tail (margin fields here).
    attr = _format_attribution(attribution_lane, attribution_version,
                               attempt_t_ms)
    if attr:
        fields.append(attr.strip())
    if margin is not None:
        fields.append("margin=" + margin)
    if pruned:
        fields.append("margin_pruned=" + str(pruned))
    _write_decision_line(" ".join(fields) + "\n", data_dir)
    return rendered if isinstance(rendered, str) else ""


def _queue_note(store_py, namespace):
    if not store_py or not os.path.isfile(store_py):
        return ""
    try:
        result = subprocess.run(
            [sys.executable, store_py, "queue-list", "--namespace", namespace, "--json"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return ""
        payload = json.loads(result.stdout)
        count = payload.get("count", 0) if isinstance(payload, dict) else 0
        if isinstance(count, int) and count > 0:
            return ("zmem: %d captured correction candidate(s) pending review — "
                    "run the closeout skill to process." % count)
    except Exception:
        pass
    return ""


def _promotion_note(store_py):
    if not store_py or not os.path.isfile(store_py):
        return ""
    try:
        result = subprocess.run([sys.executable, store_py, "promote", "--dry-run"],
                                capture_output=True, text=True, timeout=5, check=False)
        for line in result.stdout.splitlines():
            if "promotion candidate" in line.lower():
                return line.strip()
    except Exception:
        pass
    return ""


def main():
    core = sys.argv[1] if len(sys.argv) > 1 else ""
    agents = sys.argv[2] if len(sys.argv) > 2 else ""
    store_py = sys.argv[3] if len(sys.argv) > 3 else ""
    project = sys.argv[5] if len(sys.argv) > 5 else ""
    data_dir = sys.argv[6] if len(sys.argv) > 6 else ""
    namespace = sys.argv[7] if len(sys.argv) > 7 else "user:global"
    try:
        budget = int(sys.argv[8]) if len(sys.argv) > 8 else 25000
    except (TypeError, ValueError):
        budget = 25000
    host = sys.argv[9] if len(sys.argv) > 9 else ""
    settings_dir = sys.argv[10] if len(sys.argv) > 10 else ""
    marker = sys.argv[11] if len(sys.argv) > 11 else ""
    session_id = sys.argv[12] if len(sys.argv) > 12 else ""
    log_session_id = session_id
    drift_json = sys.argv[13] if len(sys.argv) > 13 else ""
    source = sys.argv[14] if len(sys.argv) > 14 else ""
    _ = (project, data_dir)
    try:
        validated_host_lane = sys.argv[15]
    except IndexError:
        validated_host_lane = host
    if not isinstance(validated_host_lane, str):
        validated_host_lane = host

    # Issue #153: the runtime host name is the local lane identity.  Keep the
    # functional host value untouched (native-memory nudges still depend on
    # it), but only a closed-set value with a valid release semver can opt
    # into enriched decision lines.  An absent argv[15] (direct or legacy
    # invocation) falls back to the host argument itself.
    attribution_lane = _validated_attribution_lane(validated_host_lane)
    attribution_version = _release_version()

    log_moment = "session_start_compact" if source == "compact" else "session_start"
    if not session_id:
        session_id = os.environ.get("ZMEM_SESSION", "") or _anonymous_session_id()
        log_session_id = os.environ.get("ZMEM_SESSION", "")

    drift_message = ""
    try:
        parsed_drift = json.loads(drift_json) if drift_json else {}
        if isinstance(parsed_drift, dict) and parsed_drift.get("status") == "drifted":
            value = parsed_drift.get("system_message")
            if isinstance(value, str):
                drift_message = value
    except (TypeError, ValueError):
        pass

    if os.environ.get("ZMEM_INJECT", "1").strip() == "0":
        _write_decision_line(
            "[%d] zmem-hook status=silent reason=disabled ids=[] all=[] "
            "exc=0 sid=%s moment=%s%s\n" %
            (int(time.time()), _safe_sid(log_session_id), log_moment,
             _format_attribution(attribution_lane, attribution_version, 0)),
            data_dir)
        _emit({"systemMessage": drift_message} if drift_message else {})
        return

    # The host must receive Tier 0 before any store-owned subprocess can block.
    # Keep queue/recent/promotion work on the second emission so a slow or
    # unavailable store never prevents the core memory fast path.
    tier0 = build_tier0_context(core, agents, host)
    store_note = ("# Memory skill: invoke `%s <subcommand>` to recall/add/search memories."
                  % store_py) if store_py and os.path.isfile(store_py) else ""
    nudge = _native_nudge(host, settings_dir, marker)
    first_ctx = _soft_trim("\n\n".join(x for x in (tier0, store_note, nudge) if x), budget)
    first_payload = {}
    if first_ctx:
        first_payload["additionalContext"] = first_ctx
    if drift_message:
        first_payload["systemMessage"] = drift_message
    _emit(first_payload)

    correction = _queue_note(store_py, namespace)
    tier2 = build_tier2_context(store_py, namespace, session_id, budget,
                                 host=host, data_dir=data_dir,
                                log_session_id=log_session_id,
                                log_moment=log_moment,
                                attribution_lane=attribution_lane,
                                attribution_version=attribution_version)
    promotion = _promotion_note(store_py)
    final_ctx = _soft_trim("\n\n".join(x for x in
                            (correction, tier0, tier2, promotion, store_note, nudge)
                            if x), budget)
    payload = {}
    if final_ctx:
        payload["additionalContext"] = final_ctx
    if drift_message:
        payload["systemMessage"] = drift_message
    _emit(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _emit({})
