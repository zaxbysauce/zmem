#!/usr/bin/env python3
"""Subprocess-only passive injection adapter.

The host hooks own event decoding and transport wrapping. ``store.py`` owns
selection, budgeting, rendering, surfaced telemetry, and delivery state. This
adapter consumes the envelope's string ``rendered`` member and, when capture is
enabled, forwards the store-owned ``effective_ops`` list as delivery metadata.
Decision lines carry ``sid=<sanitized session id>`` (or ``sid=unknown`` when
the host supplies no session id) and a closed-set ``moment=`` attribution.

Issue #153 adds the optional suffix ``lane=<closed host lane> ver=<manifest
semver> t_ms=<nonnegative rounded store-attempt milliseconds>`` after
``moment=`` and before historical additive tails. A missing or invalid release
manifest preserves the complete legacy line.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import sys
import time


_POSTTOOLBATCH_QUERY_CAP = 500
_USER_PROMPT_REWRITE_INPUT_CAP = 4096
_POSTTOOLBATCH_FIELD_CAP = 150
_POSTTOOLBATCH_SUMMARY_CAP = 12
_HOOK_INPUT_MAX_BYTES = 256 * 1024
# The private pretool payload is a strict subset of the already bounded hook
# event.  Keep its independent cap at that envelope ceiling: this protects an
# older store process that never consumes stdin while avoiding an argv or log
# transport for raw tool input.
_PRETOOL_INPUT_MAX_BYTES = _HOOK_INPUT_MAX_BYTES
_PRIVATE_PRETOOL_STDIN_MARKER = "ZMEM_PRIVATE_PRETOOL_STDIN"
# Issue #253: the delegating prompt rides the store argv (same surface as the
# --query free text).  The bound is an ARGV bound, not the parked-byte size:
# the store redacts first and only then caps at TASK_TEXT_MAX, so truncating
# here would let a secret straddling the parked-size boundary persist as a
# cleartext prefix.  4000 keeps argv far inside Windows' 32767-char limit.
_DELEGATION_ARG_MAX = 4000

# Issue #153: decision-line attribution is deliberately small and
# dependency-free.  The schema module is the canonical source for the
# vocabulary, but this hook must still run from a partially served tree, so
# imports and manifest reads fail closed to the pre-attribution line shape.
# The lane values match the store selector's INJECTION_LANES closed set and
# the host mapping used for the selector argv below.
_ATTR_LANES = ("claude", "codex", "zcode", "hermes-provider", "hermes-compat")
_ATTR_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _validated_attribution_lane(lane=None):
    """Return a closed-set host lane, or ``None`` for legacy output."""
    value = lane
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value in _ATTR_LANES else None


def _release_version():
    """Read the served tree's semver from its release manifest.

    A malformed or absent manifest is a compatibility deployment.  Writers
    must retain the complete audit line, but omit all attribution fields.
    """
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
    """Return a nonnegative integer duration for one subprocess attempt."""
    try:
        return max(0, int(round((time.perf_counter() - started) * 1000)))
    except Exception:
        return 0


def _budget_default_s(key, fallback_s):
    """Read the hook timeout table, retaining a seconds fallback."""
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
    """Bound store subprocesses using the shared env/table contract."""
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


def _classify_silent_reason(rows, omitted=0, budget_emptied=False,
                            allowed=None):
    """Legacy classifier retained for log readers; store reason is canonical.

    Historical wording retained for compatibility: "no durable memories met the inject bar.";
    current adapters receive the structured reason from the store and do not
    render this prose locally.
    """
    valid = set(("empty-pool", "omitted", "below-bar", "budget-drop",
                 "below-relevance") if allowed is None else allowed)
    candidate = ("budget-drop" if budget_emptied else
                 "below-bar" if rows else
                 "omitted" if omitted else "empty-pool")
    return candidate if candidate in valid else "empty-pool"


def _data_dir() -> str:
    """Resolve the sidecar/log directory using the host's store chain."""
    store = os.environ.get("ZMEM_STORE", "")
    if store and os.path.dirname(store):
        return os.path.expanduser(os.path.dirname(store))
    for key in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        value = os.environ.get(key, "")
        if value:
            return os.path.expanduser(value)
    return os.path.join(os.path.expanduser("~"), ".zmem")


def _safe_label(value: object, cap: int = 128) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))[:cap] or "unknown"


def _anonymous_session_id() -> str:
    """Give unattributed manual invocations isolated ledger scope."""
    return "anonymous-%d-%d" % (os.getpid(), time.time_ns())


def _rotate_log(path: str) -> None:
    """Run the stdlib-only bounded rotator; rotation is always fail-open."""
    # The helper exposes the same ``rotate_on_append`` contract as the former
    # implementation, but the hook reaches it through a subprocess boundary.
    try:
        rotator = os.path.join(os.path.dirname(__file__), "zmem-log-rotate.py")
        subprocess.run([sys.executable, rotator, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2, check=False)
    except Exception:
        pass


# Keep the historical seam available to standalone compatibility tests and
# downstream adapters while the implementation now crosses the stdlib-only
# helper subprocess boundary.  Calling through the alias lets old callers
# suppress rotation without reintroducing a storelib import into the hook.
_rotate_telemetry_logs = _rotate_log


def _log_inject_decision(
    rows, selected, status: str, reason: str, omitted=0,
    tokens_used=None, tokens_budget=None, session_id: str = "",
    all_ids=None, moment: str = "", store_py: str = "", admission_used=None,
    budget_dropped=None, budget_truncated=None,
    budget_dropped_protected=None, arms=None, excluded_count=0, batch=False,
    tool_names=None, path_basenames=None, margin=None,
    margin_pruned_ids=None, store_timeout=False,
    lane=None, version=None, t_ms=None, rewrite=False,
    repo_state=None,
) -> None:
    """Append a sanitized decision line; sid and moment are audit joins.

    Every modern line carries ``sid=<sanitized session id>``; missing host
    ids use ``sid=unknown`` and the additive mode field is ``moment=<mode>``.

    Issue #153: attribution is an all-or-nothing writer extension.  A valid
    manifest semver and nonnegative attempt duration are required before any
    of the three fields is emitted; the optional lane must be closed-set.
    The exact order is frozen after ``moment`` and before every historical
    additive tail (arms, batch, tools, paths, and the margin fields).

    The legacy ``zmem-bg.log`` name remains documented for readers migrating
    to the split decision log, and the canonical template is ``reason={reason}``.
    """
    try:
        log_path = os.path.join(_data_dir(), "zmem-decisions.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _maybe_log_drift(session_id)
        _rotate_telemetry_logs(log_path)
        ids = [r.get("id") for r in (selected or []) if isinstance(r, dict)]
        ids_all = ([str(x) for x in all_ids if isinstance(x, str)]
                   if all_ids is not None else
                   [r.get("id") for r in (rows or []) if isinstance(r, dict)])
        fields = ["[%d] zmem-hook" % int(time.time()),
                  "status=%s" % (status or "silent"),
                  "reason=%s" % (reason or "empty-pool")]
        if omitted:
            fields.append("omitted=%d" % int(omitted))
        fields.extend(("ids=%s" % ids, "all=%s" % ids_all))
        if tokens_used is not None:
            fields.append("tokens=%s/%s" %
                          (tokens_used, tokens_budget if tokens_budget is not None else "-"))
            fields.append("rendered_estimate=%d" % int(tokens_used))
        if admission_used is not None:
            fields.extend(("admission_budget=%d" % int(admission_used),
                           "budget_dropped=%d" % int(budget_dropped or 0),
                           "budget_truncated=%d" % int(budget_truncated or 0),
                           "budget_dropped_protected=%d" %
                           int(budget_dropped_protected or 0)))
        # Exclusion attribution is additive only when delivery actually
        # excluded a row; older miss-rate readers already treat absence as 0.
        try:
            # The shared selector reports concrete excluded IDs.  Older
            # envelope producers reported a numeric count, so retain both
            # shapes in this audit-only field.
            excluded_value = (len(excluded_count)
                              if isinstance(excluded_count, list)
                              else int(excluded_count or 0))
        except (TypeError, ValueError):
            excluded_value = 0
        if excluded_value:
            fields.append("exc=%d" % excluded_value)
        fields.append("sid=" + _safe_label(session_id))
        if moment:
            fields.append("moment=" + _safe_label(moment, 32))
        # Issue #153: the attribution suffix rides only when the manifest
        # semver validates; a lane outside the closed set suppresses the
        # whole suffix while an absent lane keeps ver/t_ms enrichment.
        attr = ""
        if ((lane is None or lane in _ATTR_LANES)
                and isinstance(version, str)
                and _ATTR_VERSION_RE.fullmatch(version)
                and isinstance(t_ms, int) and not isinstance(t_ms, bool)
                and t_ms >= 0):
            lane_field = "lane=%s" % lane if lane is not None else ""
            attr = "%s ver=%s t_ms=%d" % (lane_field, version, t_ms)
        if attr:
            fields.append(attr)
        if isinstance(arms, dict) and arms:
            try:
                fields.append("arms=" + ",".join(
                    "%s:%d/%d" % (label, int(arms[key].get("post", 0)),
                                   int(arms[key].get("cap", 0)))
                    for label, key in (("fts", "fts"), ("vec", "vec"),
                                       ("ent", "entity"), ("graph", "graph"),
                                       ("temporal", "temporal"))
                    if key in arms))
            except (AttributeError, TypeError, ValueError):
                pass
        if batch:
            fields.append("batch=1")
        if tool_names:
            fields.append("tools=" + ",".join(_safe_label(x, _POSTTOOLBATCH_FIELD_CAP)
                                               for x in tool_names))
        if path_basenames:
            fields.append("paths=" + ",".join(_safe_label(x, _POSTTOOLBATCH_FIELD_CAP)
                                               for x in path_basenames))
        if not isinstance(margin, bool):
            try:
                margin_value = float(margin)
                if math.isfinite(margin_value):
                    fields.append("margin=%.6f" % margin_value)
            except (TypeError, ValueError):
                pass
        if (isinstance(margin_pruned_ids, list) and margin_pruned_ids
                and all(isinstance(x, str) for x in margin_pruned_ids)):
            fields.append("margin_pruned=" + str([_safe_label(x, 64)
                                                   for x in margin_pruned_ids
                                                   if isinstance(x, str)]))
        if store_timeout:
            fields.append("store_timeout=1")
        if rewrite is True or (type(rewrite) is int and rewrite == 1):
            fields.append("rewrite=1")
        if repo_state in ("dirty", "clean", "unavailable"):
            # Issue #398/#397 audit tail: the repository-state predicate's
            # outcome for the pretool lane. Additive last field; legacy
            # lines (and lines from other lanes) never carry it.
            fields.append("state=" + repo_state)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(" ".join(fields) + "\n")
    except Exception:
        pass


def _degraded_notice_dir() -> str:
    """Directory the embedding-degradation sentinel lives in (issue #131).

    Mirrors ``embeddings.degraded_notice_dir()`` — which this stdlib-only
    adapter cannot import — so the skip check looks exactly where the store
    side writes. The two previously diverged (this used the ZMEM_STORE
    dirname, the writer preferred ZMEM_DATA), so a store/data divergence left
    the probe spawning on every recall even though a sentinel existed.
    ``expanduser`` matches the writer: a ``~``-prefixed ZMEM_DATA must not
    become a literal ``~`` directory here.
    """
    env_data = os.environ.get("ZMEM_DATA", "")
    if env_data:
        return os.path.expanduser(env_data)
    store = os.environ.get("ZMEM_STORE", "")
    if store:
        # expanduser, and dirname-or-cwd for a bare relative name: the writer
        # goes through host.resolve_store_path(), which expandusers an
        # explicit ZMEM_STORE and treats a bare name as cwd-relative, so both
        # resolvers must land on the same directory in every shape.
        return os.path.expanduser(os.path.dirname(store) or os.curdir)
    return _data_dir()


#: The lane-health probe's wall-clock ceiling. The launcher arms a 12s
#: watchdog (hooks/timeout-budget.json launcher_watchdog_ms) around the WHOLE
#: hook process, and the recall subprocess that runs before this probe is
#: itself budgeted at store_recall_ms (8s). A 5s probe could therefore push
#: the process past the watchdog, and the launcher answers a killed hook with
#: an empty envelope — losing the recall context this hook exists to deliver.
#: 2s leaves recall(8s) + probe(2s) + emit well inside the 12s budget while
#: still giving the child (interpreter start + imports + a read-only probe)
#: room to answer.
_LANE_HEALTH_PROBE_TIMEOUT_S = 2.0


def _maybe_log_lane_health(store_py: str) -> None:
    """One bounded post-recall lane-health probe (issue #131).

    Runs ``store.py embedding-health --json`` fail-open (bounded by
    ``_LANE_HEALTH_PROBE_TIMEOUT_S``) after the recall envelope has been
    consumed, the decision line written, and the rendered context emitted —
    the fence, the decision line, and the emitted JSON stay byte-identical.
    The child inherits ZMEM_DATA/ZMEM_STORE/ZMEM_MODELS_DIR/
    ZMEM_MODEL_AUTODOWNLOAD implicitly from ``os.environ`` and runs in shallow
    mode (no model load: five of the six degradation reasons are presence-only,
    and the write path's own load covers the tampered case).

    Two cheap skips keep the probe off the hot path in the cases where it
    cannot produce anything: an absent store (the child would only report
    "store unreadable" and exit 1), and an existing once-per-data-directory
    sentinel (nothing left to record). Only when THIS probe created the
    sentinel does a separate bounded ``lane_health`` line land in the decision
    log; the line deliberately carries no ``zmem-hook`` token so decision-log
    parsers (which anchor on that token) never see it.
    """
    try:
        log_dir = _data_dir()
        # The sentinel lives wherever the store side writes it, which is NOT
        # necessarily the decision-log dir — resolve it with the shared order.
        if os.path.isfile(os.path.join(_degraded_notice_dir(),
                                       "embedding-degraded.notice")):
            return
        # Absent store: the child would spend a whole interpreter start only to
        # report "store unreadable" and exit 1, on every recall, forever.
        store_path = os.environ.get("ZMEM_STORE", "") or os.path.join(
            log_dir, "store.sqlite")
        if not os.path.isfile(store_path):
            return
        env = {k: v for k, v in os.environ.items()
               if k != _PRIVATE_PRETOOL_STDIN_MARKER}
        env["ZMEM_EMBEDDING_HEALTH_SHALLOW"] = "1"
        proc = subprocess.run(
            [sys.executable, store_py, "embedding-health", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_LANE_HEALTH_PROBE_TIMEOUT_S, check=False, env=env)
        if proc.returncode != 0:
            return
        parsed = json.loads(proc.stdout.decode("utf-8", "replace"))
        if not isinstance(parsed, dict) or not parsed.get("notice_created"):
            return
        reason = _safe_label(parsed.get("reason"), 64)
        log_path = os.path.join(log_dir, "zmem-decisions.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(
                "[%d] lane_health=degraded reason=%s notice=created\n"
                % (int(time.time()), reason))
    except Exception:
        pass


def _maybe_log_drift(session_id: str) -> None:
    """Best-effort served-tree drift marker."""
    try:
        data_dir = _data_dir()
        # Keep this byte-for-byte aligned with drift.py's marker algorithm:
        # the readable prefix is bounded, while the digest covers the full
        # sanitized id so long ids cannot alias one another.
        safe_full = re.sub(r"[^A-Za-z0-9._-]", "_", (session_id or "")) or "unknown"
        marker_key = f"{safe_full[:128]}-{hashlib.sha256(safe_full.encode('utf-8')).hexdigest()[:8]}"
        marker = os.path.join(data_dir, ".drift-checked-" + marker_key)
        if os.path.isfile(marker):
            return
        drift = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                             "skills", "memory", "scripts", "drift.py"))
        if os.path.isfile(drift):
            subprocess.run([sys.executable, drift, "log-once", "--data-dir", data_dir,
                            "--sid", session_id or ""], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
    except Exception:
        pass


def _clear_delivery_state(store_py: str, session_id: str,
                          command: str = "delivery-clear") -> None:
    """Clear per-session delivery state through the CLI only.

    SessionEnd owns both sidecars; PreCompact retains the historical
    ledger-only behavior. Keep the command as one argv token so a session id
    beginning with ``-`` remains data rather than an argparse option.
    """
    if not session_id or not store_py or not os.path.isfile(store_py):
        return
    try:
        child_env = os.environ.copy()
        child_env.pop(_PRIVATE_PRETOOL_STDIN_MARKER, None)
        subprocess.run([sys.executable, store_py, command,
                        f"--session-id={session_id}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False, env=child_env)
    except Exception:
        pass


def _decision_moment(mode: str) -> str:
    return "pretool" if mode == "posttoolbatch" else ("subagent" if mode == "recent" else mode)


def _lane() -> str:
    return {"claude": "claude", "codex": "codex", "zcode": "zcode",
            "hermes": "hermes-provider", "hermes-provider": "hermes-provider",
            "hermes-compat": "hermes-compat"}.get(
                os.environ.get("ZMEM_HOST", "zcode").strip().lower(), "")


def _event_text(event: dict, *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_posttoolbatch_events(payload: dict) -> list[str]:
    """Return bounded, non-sensitive descriptors from a batch event."""
    def event_text(name, inp):
        values = []
        if isinstance(inp, dict):
            for key in ("command", "file_path", "notebook_path", "path"):
                value = inp.get(key)
                if isinstance(value, str) and value:
                    values.append(value.replace("\\", "/")[:_POSTTOOLBATCH_FIELD_CAP])
        if not values:
            return ""
        parts = [name[:_POSTTOOLBATCH_FIELD_CAP]] if isinstance(name, str) and name else []
        parts.extend(values)
        return " ".join(parts)

    events = []
    uses = payload.get("tool_uses") if isinstance(payload, dict) else None
    if isinstance(uses, list):
        for use in uses:
            if not isinstance(use, dict):
                continue
            name = use.get("name")
            inp = use.get("input")
            event = event_text(name, inp)
            if event:
                events.append(event)
    if not events:
        name = payload.get("tool_name") if isinstance(payload, dict) else None
        inp = payload.get("tool_input") if isinstance(payload, dict) else None
        event = event_text(name, inp)
        if event:
            events.append(event)
    return events


def build_posttoolbatch_query(payload: dict) -> str:
    # Normalize only within each event; newlines remain the event separator.
    events = [" ".join(event.split()) for event in extract_posttoolbatch_events(payload)]
    return "\n".join(events)[:_POSTTOOLBATCH_QUERY_CAP]


def posttoolbatch_tool_summary(payload: dict) -> dict:
    uses = payload.get("tool_uses") if isinstance(payload, dict) else None
    records = uses if isinstance(uses, list) else []
    if not records and isinstance(payload, dict) and payload.get("tool_name"):
        records = [{"name": payload.get("tool_name"),
                    "input": payload.get("tool_input")}]
    names, paths = [], []
    for use in records:
        if not isinstance(use, dict):
            continue
        name = use.get("name")
        if isinstance(name, str) and name:
            names.append(name[:_POSTTOOLBATCH_FIELD_CAP])
        inp = use.get("input")
        if isinstance(inp, dict):
            for key in ("file_path", "notebook_path", "path"):
                value = inp.get(key)
                if isinstance(value, str) and value:
                    # The audit projection intentionally carries basenames
                    # only.  Batch queries may retain the original path for
                    # retrieval, but a decision log must not expand into a
                    # record of a user's directory layout.
                    paths.append(os.path.basename(
                        value.replace("\\", "/"))[:_POSTTOOLBATCH_FIELD_CAP])
                    break
    return {"tool_count": len(records),
            "names": names[:_POSTTOOLBATCH_SUMMARY_CAP],
            "basenames": paths[:_POSTTOOLBATCH_SUMMARY_CAP]}


def _run_store(store_py: str, args: list[str], timeout=None, *,
               stdin_bytes: bytes | None = None,
               env_overrides: dict[str, str | None] | None = None):
    command = [sys.executable, store_py, *args]
    try:
        effective_timeout = _store_timeout_s() if timeout is None else timeout
        kwargs = {
            "stderr": subprocess.DEVNULL,
            "timeout": effective_timeout,
        }
        # The marker is private to one pretool recall child.  Start every
        # store subprocess with it removed so an ambient host/test value
        # cannot accidentally reach query-rewrite, ledger-clear, or another
        # command; the pretool caller below explicitly restores it alongside
        # the matching stdin bytes.
        child_env = os.environ.copy()
        child_env.pop(_PRIVATE_PRETOOL_STDIN_MARKER, None)
        if env_overrides is not None:
            for key, value in env_overrides.items():
                if value is None:
                    child_env.pop(key, None)
                else:
                    child_env[key] = value
        kwargs["env"] = child_env
        # Supplying ``input`` makes check_output use communicate(), avoiding a
        # pipe-buffer deadlock if a version-skewed child exits before reading
        # this optional private payload.  Leave legacy subprocess construction
        # untouched when there is no payload.
        if stdin_bytes is not None:
            kwargs["input"] = stdin_bytes
        output = subprocess.check_output(command, **kwargs)
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
    except subprocess.CalledProcessError as exc:
        output = exc.output
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return subprocess.CompletedProcess(command, exc.returncode,
                                            stdout=output or "", stderr="")
    except Exception:
        return None


def _free_text_arg(option: str, value: str) -> list[str]:
    """Keep option-looking free text attached to its option name."""
    return [option + "=" + value] if value.startswith("-") else [option, value]


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _rewrite_query(store_py: str, namespace: str, session_id: str,
                   query: str) -> tuple[str, bool]:
    """Best-effort user-prompt rewrite with a dedicated one-second budget."""
    original = query[:_POSTTOOLBATCH_QUERY_CAP]
    if len(query) > _USER_PROMPT_REWRITE_INPUT_CAP:
        return original, False
    rewrite_input = query[:_USER_PROMPT_REWRITE_INPUT_CAP]
    if os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0":
        return original, False
    args = ["query-rewrite"]
    args.extend(_free_text_arg("--prompt", rewrite_input))
    args.extend(("--session-id", session_id, "--namespace", namespace, "--json"))
    try:
        result = _run_store(
            store_py, args, timeout=min(1.0, _store_timeout_s())
        )
        if result is None or result.returncode != 0:
            return original, False
        parsed = json.loads(result.stdout, object_pairs_hook=_strict_json_object)
        if not isinstance(parsed, dict) or set(parsed) != {"query", "rewrite"}:
            return original, False
        rewritten = parsed["query"]
        flag = parsed["rewrite"]
        if (not isinstance(rewritten, str)
                or len(rewritten) > _POSTTOOLBATCH_QUERY_CAP
                or type(flag) is not int or flag not in (0, 1)):
            return original, False
        return rewritten, flag == 1
    except Exception:
        return original, False


# Issue #397: the restore/destructive verbs that arm the repository-state
# probe. Anything else keeps the pretool lane state-blind (and free of the
# extra git subprocess).
_STATE_PROBE_VERBS = (
    "checkout", "restore", "reset", "stash", "clean", "rebase", "merge",
    "push", "cherry-pick", "revert", "filter-branch",
)


def _pretool_repo_state(event: dict, query: str) -> str:
    """Issue #397: bounded, fail-open repository-state predicate.

    Returns "dirty" | "clean" | "unavailable". The TEST SEAM
    ZMEM_TEST_REPO_STATE (closed set) overrides everything; in production
    the probe runs only when the tool-input material carries a
    restore/destructive verb. `git status --porcelain --untracked-files=no`
    reports only TRACKED-change dirt — the #397 hazard is uncommitted
    tracked work being wiped by git checkout -- / git restore. The probe
    runs in the EVENT cwd when it names an existing directory (falling
    back to the hook process cwd), with a hard line cap, and a timeout
    read from ZMEM_STATE_PROBE_TIMEOUT_S (float seconds, default 2.0;
    malformed/negative/non-finite fall back to the default). Every failure
    path returns "unavailable" — the hook never blocks on this probe."""
    seam = os.environ.get("ZMEM_TEST_REPO_STATE", "").strip()
    if seam in ("dirty", "clean", "unavailable"):
        return seam
    if seam:
        pass  # unknown seam values fall through to the production probe
    lowered = (query or "").lower()
    if not any(verb in lowered for verb in _STATE_PROBE_VERBS):
        return "unavailable"  # not a state-hazardous command: no probe
    cwd = None
    raw_cwd = event.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip() and os.path.isdir(raw_cwd):
        cwd = raw_cwd
    try:
        timeout_s = 2.0
        raw_t = os.environ.get("ZMEM_STATE_PROBE_TIMEOUT_S", "").strip()
        if raw_t:
            try:
                val = float(raw_t)
                if val > 0.0 and val != float("inf") and val == val:
                    timeout_s = val
            except ValueError:
                pass  # malformed knob -> default (fail-open, not fail-closed)
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
            errors="replace")
        if proc.returncode != 0:
            return "unavailable"
        lines = (proc.stdout or "").splitlines()
        if len(lines) > 200:
            return "dirty"  # enormous churn is still just dirty
        return "dirty" if any(l.strip() for l in lines) else "clean"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unavailable"


def _query_for(mode: str, event: dict) -> str:
    if mode == "posttoolbatch":
        return build_posttoolbatch_query(event)
    if mode == "pretool":
        inp = event.get("tool_input")
        if isinstance(inp, dict):
            # The store owns operation-token derivation.  Pass only the
            # event's operation/path material so its closed allowlist can
            # derive the same query tail at the store boundary; no operation
            # count crosses the exact envelope, and the adapter never imports
            # or reads the ops ring.
            pieces = []
            for key in ("command", "cmd", "file_path", "notebook_path", "path", "description"):
                if isinstance(inp.get(key), str):
                    pieces.append(inp[key])
            return " ".join(x for x in pieces if x)[:500]
        return _event_text(event, "tool_name", "tool")[:500]
    if mode == "subagent":
        return _event_text(event, "prompt", "task", "task_text", "description")[:500]
    if mode == "precompact":
        return ""
    value = _event_text(event, "prompt", "query")
    if mode == "user_prompt":
        # Keep one sentinel character so _rewrite_query can fail open for an
        # overlong prompt without passing an unbounded string to the store.
        return value[:_USER_PROMPT_REWRITE_INPUT_CAP + 1]
    return value[:_POSTTOOLBATCH_QUERY_CAP]


def _emit(rendered: str, effective_ops: object = None) -> None:
    if not isinstance(rendered, str) or not rendered:
        print("{}")
        return
    payload = {"additionalContext": rendered}
    if isinstance(effective_ops, list) and all(isinstance(item, str)
                                               for item in effective_ops):
        payload["effective_ops"] = effective_ops
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # U+2028/U+2029 are valid JSON but remain line separators in some host
    # transports; keep the output one physical line for hook runners.
    print(encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def main() -> int:
    if len(sys.argv) < 4:
        return 0
    store_py, namespace = sys.argv[1], sys.argv[2]
    try:
        budget = int(sys.argv[3])
    except (IndexError, TypeError, ValueError):
        budget = 25000
    mode = sys.argv[4] if len(sys.argv) > 4 else "user_prompt"
    recent_limit = sys.argv[5] if len(sys.argv) > 5 else "3"
    recent_global_limit = sys.argv[6] if len(sys.argv) > 6 else "2"
    _ = budget  # budget is enforced by the selector; retained for argv compatibility.
    agent_label = sys.argv[7][:64] if len(sys.argv) > 7 else ""
    _ = agent_label
    # Resolve attribution once per hook invocation (issue #153).  A failed
    # version read or invalid host lane intentionally selects the legacy
    # line shape; the lane value is the same closed-set host mapping the
    # selector argv carries below.
    attribution_lane = _validated_attribution_lane(_lane())
    attribution_version = _release_version()
    # The kill switch is evaluated before stdin parsing.  A malformed or
    # blocking event stream must never delay a disabled passive hook.
    # ZMEM_INJECT remains global, while ZMEM_QUERY_CONTEXT belongs only to the
    # operation-context lanes (PreToolUse and PostToolBatch).  Consult both
    # before stdin parsing or any store subprocess so a disabled operation hook
    # is always a cheap empty envelope without silencing prose recall.
    switch_disabled = (
        os.environ.get("ZMEM_INJECT", "1").strip() == "0"
        or (mode in ("pretool", "posttoolbatch") and
            os.environ.get("ZMEM_QUERY_CONTEXT", "1").strip() == "0")
    )
    session_id = os.environ.get("ZMEM_SESSION", "")
    log_session_id = session_id
    if not session_id and mode != "session_end":
        session_id = _anonymous_session_id()
    moment = _decision_moment(mode)
    lane = _lane()
    # Duration of the exact store subprocess attempt used for this decision.
    # Zero is also the intentional value for no-attempt paths (the disabled
    # branch below and every pre-attempt early exit).
    attribution_t_ms = 0
    if switch_disabled and mode != "session_end":
        _log_inject_decision([], [], "silent", "disabled",
                             session_id=log_session_id, moment=moment,
                             lane=attribution_lane, version=attribution_version,
                             t_ms=attribution_t_ms)
        _emit("")
        return 0
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw_event = stream.read(_HOOK_INPUT_MAX_BYTES + 1)
        if isinstance(raw_event, str):
            raw_event = raw_event.encode("utf-8")
        if len(raw_event) > _HOOK_INPUT_MAX_BYTES:
            raise ValueError("hook input too large")
        event = json.loads(raw_event.decode("utf-8")) if raw_event else {}
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}
    event_session_id = _event_text(event, "session_id", "sessionId")
    if event_session_id:
        session_id = event_session_id
        log_session_id = event_session_id
    if mode == "session_end":
        _clear_delivery_state(store_py, session_id, "delivery-clear")
        # Issue #398 item 3: the closeout nudge. Consults ONLY the
        # per-session ops markers (.wrote stamped by the write path,
        # .closeout stamped by `store.py closeout-ack`); never opens the
        # store. When the session wrote rows and no closeout was recorded,
        # surface exactly one bounded reminder line. Fail-open by design.
        try:
            import hashlib as _hashlib
            _sid = session_id or ""
            _stem = _hashlib.sha256(
                _sid.encode("utf-8")).hexdigest()[:32] if _sid else ""
            _ops = os.path.join(_data_dir(), "ops")
            _wrote = _stem and os.path.exists(
                os.path.join(_ops, _stem + ".wrote"))
            _closed = _stem and os.path.exists(
                os.path.join(_ops, _stem + ".closeout"))
            if _wrote and not _closed:
                _emit("[zmem] this session wrote memories but no closeout "
                      "was recorded - run the closeout skill (or "
                      "`store.py closeout-ack --session-id <id>`) so the "
                      "session's captures get their formal review.")
                return 0
        except Exception:
            pass
        _emit("")
        return 0
    if not lane or not os.path.isfile(store_py):
        _log_inject_decision([], [], "silent", "empty-pool",
                             session_id=log_session_id, moment=moment,
                             lane=attribution_lane, version=attribution_version,
                             t_ms=attribution_t_ms)
        _emit("")
        return 0
    query = _query_for(mode, event)
    rewrite_applied = False
    if mode == "user_prompt":
        query, rewrite_applied = _rewrite_query(
            store_py, namespace, session_id, query
        )
    command = "recent" if not query else "recall"
    args = [command, "--namespace", namespace,
            "--limit", recent_limit if command == "recent" else "5",
            "--include-global", "--global-limit",
            recent_global_limit if command == "recent" else "3",
            "--no-bump", "--for-injection", "--json",
            f"--session-id={session_id}", "--moment", moment, "--lane", lane]
    if command == "recall":
        args[1:1] = _free_text_arg("--query", query)
    # Issue #397: pretool-only repository-state hint. Computed once per
    # invocation; the flag is omitted entirely when the state is
    # "unavailable" (fail-open: the store never sees an unknown value and
    # the recall proceeds exactly as today).
    repo_state = ""
    if mode == "pretool" and command == "recall":
        repo_state = _pretool_repo_state(event, query)
        if repo_state in ("dirty", "clean"):
            args.append(f"--state-hint={repo_state}")
    # Issue #253: a delegating PreToolUse(Agent/Task) parks its prompt
    # store-side so the child's prompt-less SubagentStart (Claude Code's real
    # payload carries no task text) gets a task-keyed recall query. The park
    # happens inside the store subprocess this adapter already makes — the
    # #158 boundary keeps this file free of storelib imports; only the raw
    # text crosses on the same argv surface as --query. `prompt` is the only
    # observable carrying the child's task (description fallback), and
    # neither key is new to the parent's own pull: _query_for's pretool
    # branch already read `description` and always ignored `prompt`, so
    # this block changes nothing about what the parent itself receives.
    if (mode == "pretool"
            and _event_text(event, "tool_name", "tool").lower() in (
                "agent", "task")):
        _delegation = ""
        _tool_input = event.get("tool_input")
        if isinstance(_tool_input, dict):
            for _key in ("prompt", "description"):
                _value = _tool_input.get(_key)
                if isinstance(_value, str) and _value.strip():
                    _delegation = _value.strip()
                    break
        if _delegation:
            # A NUL cannot survive argv: CPython raises ValueError before
            # the spawn, which would cost the PARENT its whole recall rather
            # than just this park. Drop it here (the only character argv
            # forbids; other controls ride safely).
            _delegation = _delegation.replace("\x00", "")
            # Transport more than the store's TASK_TEXT_MAX so the store can
            # REDACT first and cap afterwards — truncating here would let a
            # secret straddling the boundary persist as a cleartext prefix.
            # The bound keeps argv well inside Windows' 32767-char limit.
            args.extend(_free_text_arg("--delegation-prompt",
                                       _delegation[:_DELEGATION_ARG_MAX]))
    # Issue #98: forward the cross-project tier flag per its surface matrix.
    # ZMEM_CROSS_PROJECT unset arms pretool only (posttoolbatch included —
    # _decision_moment maps it to pretool); "0" disables every surface (the
    # store enforces this even when the flag is present); "1" arms
    # user_prompt as well. No ops tokens are forwarded from here on any
    # surface: the #158 boundary keeps this adapter free of storelib imports,
    # and the store-side selector owns all operation-token derivation — for
    # an env-enabled user_prompt surface it derives the tokens from the
    # prompt event itself (inject.py), so the hazard gate still arms without
    # this file ever touching the allowlist.
    _cross_env = os.environ.get("ZMEM_CROSS_PROJECT", "").strip()
    if moment == "pretool" or (_cross_env == "1" and moment == "user_prompt"):
        args.append("--include-cross-project")
    _attempt_started = time.perf_counter()
    # Current raw pretool input is private, transient enrichment only.  Its
    # flattened query still travels on the stable argv surface; raw JSON is
    # handed solely to this marked child on stdin and never reaches logs,
    # envelopes, storage, or other store commands.  Always remove a possible
    # ambient marker for every child, then set it atomically with bytes below.
    private_stdin = None
    private_env = {_PRIVATE_PRETOOL_STDIN_MARKER: None}
    if mode == "pretool" and command == "recall":
        tool_input = event.get("tool_input")
        if isinstance(tool_input, dict):
            try:
                encoded_input = json.dumps(
                    tool_input, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if len(encoded_input) <= _PRETOOL_INPUT_MAX_BYTES:
                    private_stdin = encoded_input
                    private_env[_PRIVATE_PRETOOL_STDIN_MARKER] = "1"
            except (TypeError, ValueError, UnicodeError):
                pass
    if private_stdin is None:
        # Preserve the established call shape for every legacy path and for
        # version-skew/fail-open cases with no bounded payload.  `_run_store`
        # still scrubs an ambient private marker from its child environment.
        result = _run_store(store_py, args)
    else:
        result = _run_store(store_py, args, stdin_bytes=private_stdin,
                            env_overrides=private_env)
    attribution_t_ms = _rounded_elapsed_ms(_attempt_started)
    envelope = {}
    if result is not None and result.returncode == 0:
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                envelope = parsed
        except (TypeError, ValueError):
            pass
    rendered = envelope.get("rendered")
    if not isinstance(rendered, str):
        rendered = ""
    reason = envelope.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = ("omitted" if result is None else
                  "injected" if rendered else "empty-pool")
    candidate_ids = envelope.get("candidate_ids")
    if not isinstance(candidate_ids, list):
        candidate_ids = []
    # ``rendered`` is the only delivered context.  The structured result IDs
    # remain useful audit metadata, however: retaining them in the decision
    # line lets miss-rate tooling distinguish a selector result from a
    # transport failure without reconstructing or rendering row text here.
    selected_rows = envelope.get("results")
    selected = ([row for row in selected_rows
                 if isinstance(row, dict) and isinstance(row.get("id"), str)]
                if isinstance(selected_rows, list) else [])
    injected = bool(rendered)
    log_tokens = injected or reason == "budget-drop"
    summary = posttoolbatch_tool_summary(event) if mode == "posttoolbatch" else {}
    _log_inject_decision(
        [], selected, "injected" if injected else "silent", reason,
        omitted=envelope.get("omitted", 0),
        tokens_used=envelope.get("tokens_used") if log_tokens else None,
        tokens_budget=envelope.get("tokens_budget") if log_tokens else None,
        all_ids=candidate_ids, session_id=log_session_id, moment=moment,
        admission_used=envelope.get("budget_admission"),
        budget_dropped=envelope.get("budget_dropped"),
        budget_truncated=envelope.get("budget_truncated"),
        budget_dropped_protected=envelope.get("budget_dropped_protected"),
        arms=envelope.get("arms"), excluded_count=envelope.get("excluded", 0),
        batch=mode == "posttoolbatch", tool_names=summary.get("names"),
        path_basenames=summary.get("basenames"),
        margin=envelope.get("margin"),
        margin_pruned_ids=envelope.get("margin_pruned_ids"),
        store_timeout=result is None,
        lane=attribution_lane, version=attribution_version,
        t_ms=attribution_t_ms,
        rewrite=rewrite_applied,
        repo_state=repo_state or None,
    )
    _emit(rendered, envelope.get("effective_ops"))
    # Issue #131: bounded lane-health probe AFTER the context was emitted —
    # output and the decision line above stay byte-identical; the only visible
    # effect is the once-per-data-dir sentinel + one bounded decision-log line
    # the first time a degraded data directory is observed (fail-open).
    _maybe_log_lane_health(store_py)
    if mode == "precompact":
        _clear_delivery_state(store_py, session_id, "ledger-clear")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)
