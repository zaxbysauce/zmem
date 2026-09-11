#!/usr/bin/env python3
"""Shared recall body for the injecting hooks (issue #58, 3.5/3.8/3.9).

Consumers (all invoke this file AS A SCRIPT — the hyphenated filename
cannot be imported):
  - zmem-recall.sh        (UserPromptSubmit)         mode "user_prompt"
  - zmem-precompact.sh    (PreCompact, Claude only)  mode "precompact"
  - zmem-subagent-recall.sh (SubagentStart)          mode "subagent"
    (task-text recall when the host event carries the delegated prompt,
    recent pull otherwise; "recent" remains accepted for back-compat)
  - zmem-pretool-recall.sh (PreToolUse, ZCode+Claude) mode "pretool"
    (issue #90 / #85 C: query derived from the tool input itself)

argv contract (see main()):
  argv[1] = absolute path to store.py (must exist or exit 0)
  argv[2] = canonical namespace
  argv[3] = budget in chars (optional, default 25000)
  argv[4] = mode — "user_prompt" | "precompact" | "recent"
  argv[5] = recent --limit      (recent/precompact modes; default 3)
  argv[6] = recent --global-limit (recent/precompact modes; default 2;
            subagent-recall passes 5/3 to preserve its pull width)

The body:
  1. Calls ``python store.py recall|recent ...`` with --no-bump,
     --for-injection (issue #114: the selective-inject gate and the token
     budget run INSIDE the store subprocess), --json, and the per-mode
     query/limit set. Hooks never write the store. A store subprocess that
     predates --for-injection (mixed-version deployment) fails here and the
     hook degrades fail-closed to a silent decision line — never ungated
     injection.
  2. Reads the JSON envelope from stdout (the rows are already the
     gate+budget survivors; the envelope carries the decision reason and
     the pre-gate candidate ids for the bg-log all= field).
  3. Derives the decision status/reason from the envelope (the closed-set
     classifier below remains only as a fail-open fallback for stores that
     do not stamp a reason).
  4. Renders the rows through ``storelib._format_fenced_recall`` into a
     fenced, provenance-tagged block.
  5. Emits ``{"additionalContext": <ctx>}`` on stdout (the .sh wrappers
     wrap it in the <<<ZMEM_JSON>>>…<<<END>>> sentinel and neutralize
     sentinel/fence tokens as transport defense).
  6. If nothing is injected, names WHICH gate fired (issue #87 / #85
     direction 1) instead of always blaming the bar:
       - retrieval empty (or rows dropped by the passive injection-risk
         filter) → "no durable memories retrieved for this prompt."
       - rows reached the selective-inject gate and none passed →
         "no durable memories met the inject bar." (byte-identical to the
         pre-#87 one-liner so existing greps keep working)
       - the gate passed rows but the token budget emptied the set →
         "memories withheld: the injection token budget
         (ZMEM_INJECT_TOKEN_BUDGET) dropped every candidate row."
  7. Fail-open: an unhandled ``main()`` crash emits nothing (the wrapper's
     ``|| echo '{}'`` handles it) and exits 0; a recall-subprocess failure
     sets ``rows=[]`` and STILL emits the retrieved-empty envelope (with its
     reason classification, per bullet 6); a reason-classification error
     degrades to the retrieved-empty one-liner (never the bar). Every path
     exits 0. (#93 B7: split/reworded — the old text claimed only the
     wrapper-handled case existed.)
  8. Issue #110 (P0-5): ``ZMEM_INJECT=0`` is the passive-injection kill
     switch — before any stdin parsing or store subprocess the body logs
     ``status=silent reason=disabled`` and emits ``{}`` (the empty envelope;
     the launcher treats a payload without additionalContext as a no-op).
     Only the literal ``0`` disables, matching the ZMEM_QUERY_CONTEXT
     convention. Capture paths never consult the switch.

The selective-inject decision is logged to ``<data dir>/zmem-bg.log`` (the
dir resolved by ``_data_dir()``; I5 critic-fix: existing log file, not a new
one). Since issue #87 every line carries ``reason=`` (from schema_meta's
INJECT_SILENT_REASONS tuple, plus ``injected`` on the success line) and
``omitted=N`` when the passive injection-risk filter dropped rows. Since
issue #94 every line also carries ``sid=<sanitized session id>`` (the
stdin event's ``session_id``; ``sid=unknown`` when the host sent none) —
the session key the miss-rate report joins failures against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


# Selective-inject constants are imported from schema_meta (the documented
# single source of truth, PRR-017 fix) once the scripts dir is known — see
# _load_schema_meta(). The literals below are ONLY the import-failure
# fallback so a partially-deployed tree still runs with the documented
# defaults rather than crashing the hook (fail-open).
_FALLBACK_FLOOR_PROMPT = 0.25
_FALLBACK_FLOOR_GATE_NONE = 0.4
_FALLBACK_FLOOR_RECENT = 0.5
# Issue #87 / #85 direction 1: import-failure fallbacks mirroring
# schema_meta.INJECT_SILENT_REASONS / INJECT_REASON_INJECTED (a
# partially-deployed tree still classifies with the documented set).
_FALLBACK_SILENT_REASONS = ("empty-pool", "omitted", "below-bar", "budget-drop", "below-relevance")
_FALLBACK_REASON_INJECTED = "injected"
# Issue #110 (P0-5): mirror of schema_meta.INJECT_REASON_DISABLED for the
# passive-injection kill switch below.
_FALLBACK_REASON_DISABLED = "disabled"

# User-visible silent one-liners (issue #87 / #85 direction 1). The below-bar
# string is byte-identical to the pre-#87 single one-liner on purpose —
# operator greps and muscle memory keep working for the one case it was true.
_SILENT_CTX_RETRIEVED_EMPTY = "no durable memories retrieved for this prompt."
_SILENT_CTX_BELOW_BAR = "no durable memories met the inject bar."
_SILENT_CTX_BUDGET_DROP = (
    "memories withheld: the injection token budget "
    "(ZMEM_INJECT_TOKEN_BUDGET) dropped every candidate row."
)

_schema_meta = None


def _load_schema_meta(store_py: str):
    """Import schema_meta from the scripts dir (next to store.py) so the
    gate reads the SAME constants every other surface imports (PRR-017).
    Returns None on import failure; callers then use the literals above.
    """
    global _schema_meta
    if _schema_meta is not None:
        return _schema_meta
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, scripts_dir)
        import schema_meta  # type: ignore
        _schema_meta = schema_meta
        return schema_meta
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _floor(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # Reject non-finite overrides (nan/inf parse but poison comparisons).
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def _recent_floor(store_py: str) -> float:
    sm = _load_schema_meta(store_py)
    if sm is not None:
        return _floor(
            getattr(sm, "INJECT_FLOOR_RECENT_ENV", "ZMEM_INJECT_FLOOR_RECENT"),
            getattr(sm, "INJECT_FLOOR_RECENT_DEFAULT", _FALLBACK_FLOOR_RECENT),
        )
    return _floor("ZMEM_INJECT_FLOOR_RECENT", _FALLBACK_FLOOR_RECENT)


def _reason_constants(store_py: str):
    """Resolve (silent_reasons, injected_reason) from schema_meta (the
    single source of truth, PRR-017), with literal fallbacks for a
    partially-deployed tree."""
    sm = _load_schema_meta(store_py)
    if sm is not None:
        return (
            tuple(getattr(sm, "INJECT_SILENT_REASONS", _FALLBACK_SILENT_REASONS)),
            getattr(sm, "INJECT_REASON_INJECTED", _FALLBACK_REASON_INJECTED),
        )
    return (_FALLBACK_SILENT_REASONS, _FALLBACK_REASON_INJECTED)


def _reason_disabled(store_py: str) -> str:
    """Issue #110 (P0-5): the kill-switch reason, single-sourced from
    schema_meta.INJECT_REASON_DISABLED with the literal fallback for a
    partially-deployed tree (same discipline as _reason_constants)."""
    sm = _load_schema_meta(store_py)
    if sm is not None:
        return getattr(sm, "INJECT_REASON_DISABLED", _FALLBACK_REASON_DISABLED)
    return _FALLBACK_REASON_DISABLED


def _inject_disabled() -> bool:
    """Issue #110 (P0-5): ZMEM_INJECT=0 is the passive-injection kill
    switch. Only the literal ``0`` (whitespace-tolerated) disables — the
    same convention as ZMEM_QUERY_CONTEXT, so ``false``/``no``/empty keep
    injection ENABLED. Capture paths never consult this switch."""
    return os.environ.get("ZMEM_INJECT", "1").strip() == "0"


def _classify_silent_reason(rows, omitted=0, budget_emptied=False,
                            allowed=_FALLBACK_SILENT_REASONS):
    """Name WHY a silent inject is silent (issue #87 / #85 direction 1).

    Called only when nothing will be injected. Order matters and matches the
    #87 spec: budget-drop wins over below-bar (a budget wipe of a gate-passed
    set is a budget fact, not a gate fact); empty rows with omitted==0 is
    empty-pool even if the prompt was long — do not guess. ``allowed`` is the
    closed set from schema_meta; a drift/unknown value degrades to empty-pool
    rather than inventing a reason.
    """
    if budget_emptied:
        reason = "budget-drop"
    elif rows:
        reason = "below-bar"
    elif omitted > 0:
        reason = "omitted"
    else:
        reason = "empty-pool"
    if reason not in allowed:
        return "empty-pool"
    return reason


# Log bound (PRR-023 fix, superseded by #129 rotation): zmem-bg.log was
# maintenance-only (~lines/day) and is now appended per hook event. Growth
# control is BOUNDED ROTATION via storelib.log_rotate (see
# _rotate_telemetry_logs): past ZMEM_BG_LOG_MAX_BYTES the active content
# becomes a marked .1 segment — history survives, the destructive
# truncate-to-empty behavior this comment used to describe is gone.

def _maybe_log_drift(session_id: str) -> None:
    """Issue #107: run the served-tree drift check once per session id.

    The session-start hook runs the same drift.py ``log-once`` first; this
    choke point covers hosts/wirings where session-start never fired, using
    the SAME per-session marker so the zmem-drift bg-log line is written at
    most once per session regardless of which writer wins. Fail-open and
    bounded: the marker is one stat after the first call, and the drift
    subprocess gets a 5s timeout — on timeout subprocess.run kills the child;
    because drift.py creates the marker BEFORE evaluating, even a killed run
    leaves the marker, so the realistic worst case is one skipped drift check
    per session (pre-0.17 behavior), not a re-spawn per decision. The drift
    subprocess costs ~200ms (surface walk + hashing) once per session; that
    is accepted, documented latency, not a per-decision cost. The drift.py
    path is derived from THIS file own tree (parents[2]) — never from env —
    so a partial refresh can never spawn a drift checker from a different
    tree. The marker name MUST mirror drift.py _marker_path exactly (readable
    sanitized prefix + sha8 of the full sanitized sid); drift.py owns the
    authoritative create, this guard is only a fast-path stat."""
    try:
        data_dir = _data_dir()
        import hashlib as _hashlib
        import re as _re_drift
        # Mirror drift.py _marker_key EXACTLY: readable truncated prefix +
        # sha8 of the FULL sanitized sid (hashing the truncated form would
        # collide for sids sharing their first 128 sanitized chars).
        safe_full = _re_drift.sub(
            r"[^A-Za-z0-9._-]", "_", (session_id or "")) or "unknown"
        marker_key = "{0}-{1}".format(
            safe_full[:128],
            _hashlib.sha256(safe_full.encode("utf-8")).hexdigest()[:8])
        marker = os.path.join(data_dir, ".drift-checked-{0}".format(marker_key))
        if os.path.isfile(marker):
            return
        drift_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "skills", "memory", "scripts", "drift.py")
        if not os.path.isfile(drift_py):
            # Pre-0.17 served tree: drift logging is simply absent.
            return
        subprocess.run(
            [sys.executable, drift_py, "log-once",
             "--data-dir", data_dir, "--sid", session_id or ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass  # fail-open: drift reporting never blocks a decision


def _rotate_telemetry_logs(store_py: str, data_dir: str) -> None:
    """Rotate the decision log and the legacy bg log before an append
    (issue #129). The rotation package imports from the SCRIPTS dir —
    storelib's parent — on sys.path (review PRR-005: the original inserted
    the storelib dir itself, so the import failed on every writer path that
    had not already leaked the parent onto sys.path, silently skipping
    rotation there). Fail-open: any failure leaves the appends proceeding
    uncapped — growth, never loss."""
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return
    saved = sys.path[:]
    try:
        sys.path.insert(0, scripts_dir)
        from storelib.log_rotate import rotate_on_append
        rotate_on_append(os.path.join(data_dir, "zmem-decisions.log"))
        rotate_on_append(os.path.join(data_dir, "zmem-bg.log"))
    except Exception:
        pass
    finally:
        sys.path[:] = saved


def _log_inject_decision(rows, selected, status: str, reason: str,
                         omitted=0, tokens_used=None, tokens_budget=None,
                         ops_count=0, session_id: str = "",
                         all_ids=None, moment: str = "",
                         store_py: str = "",
                         admission_used=None, budget_dropped=None,
                         budget_truncated=None,
                         budget_dropped_protected=None,
                         arms=None,
                         excluded_count=0) -> None:
    """Append the injected|silent decision to the decision log (#129).

    Issue #129 split: decision lines go to ``zmem-decisions.log`` (rotated,
    never truncated) so cadence/maintenance output in ``zmem-bg.log`` can
    never interleave with them and the miss-rate join / false-injection
    counter read a clean stream. Issue #87 / #85 direction 1: every line
    carries ``reason=`` (closed set from schema_meta plus ``injected``),
    and ``omitted=N`` when the passive injection-risk filter dropped rows —
    so an operator can tell an empty-pool silent (query construction
    problem) from a below-bar silent (scoring problem) from a budget-drop
    without log forensics. Field order: ``status``, ``reason``, optional
    ``omitted=N``, ``ids``, ``all``, optional ``tokens=used/budget`` (the
    ``tokens=\\d+/\\d+`` shape pinned by tests/test_token_budget.py is
    unchanged), optional ``ops=N``, ALWAYS ``sid=<sanitized session id>``
    at line end (issue #94), then the additive ``moment=<mode>`` field
    (#129: the injection moment — session_start/user_prompt/pretool/
    subagent/precompact — absent only when unknown). Sanitization is the
    canonical ops-lane rule (``[^A-Za-z0-9._-]`` → ``_``, cap 128) so a
    hostile session id cannot forge log structure; an absent session id
    logs ``sid=unknown`` — the "unknown" fallback is deliberately distinct
    from ``_ring_path``'s filename fallback ("session") because this is a
    log label, not a path component.

    Retention (issue #129): rotation via ``storelib.log_rotate`` keeps N
    bounded segments with sequence markers; the destructive truncate-to-
    empty cap is gone. When the rotation helper cannot be imported the
    append proceeds WITHOUT any size control — unbounded growth is the
    accepted failure direction, never evidence destruction.
    """
    log_path = os.path.join(_data_dir(), "zmem-decisions.log")
    # Issue #107: the first decision of a session also fires the (marker
    # guarded, once-per-session) served-tree drift check — the session-start
    # hook normally wins the race; this covers wirings where it never ran.
    _maybe_log_drift(session_id)
    try:
        # Issue #129: rotate, never truncate. Fail-open to append-without-
        # cap when the helper is unavailable (no storelib path) — the
        # failure direction is growth, not loss. The legacy zmem-bg.log
        # (over-cap, from a pre-split deployment) is folded into bounded
        # rotation here too, so its history becomes a marked segment on
        # the first post-split decision instead of growing forever.
        _rotate_telemetry_logs(store_py, _data_dir())
        ids_all = [r.get("id") for r in rows]
        if all_ids is not None:
            # Issue #114: on the --for-injection lane the hook receives only
            # the RENDERED rows; the pre-gate candidate ids ride the envelope
            # (candidate_ids) so this field keeps its miss-rate-join meaning
            # ("what the recall would have matched") unchanged. The fallback
            # below (rows themselves) is post-gate and only reachable for
            # legacy bare-list stores that predate candidate_ids.
            ids_all = list(all_ids)
        ids_sel = [r.get("id") for r in selected]
        # v13 (issue #65, 10.9): tokens kept/budget ride on the same line so
        # budget behavior is auditable in the existing bg log.
        om = ""
        if omitted and omitted > 0:
            om = " omitted={0}".format(int(omitted))
        tok = ""
        if tokens_used is not None:
            tok = " tokens={used}/{budget}".format(
                used=tokens_used, budget=tokens_budget if tokens_budget is not None else "-"
            )
        # Issue #116: the two numbers the legacy tokens=a/b line used to
        # conflate get their own labels. rendered_estimate = the measured
        # final render (the same value as tokens=a/b's numerator, now
        # guaranteed <= budget); admission_budget = admission's own token
        # accounting for the admitted set. The three omission counts ride
        # whenever admission stats were provided (zero-counts included —
        # byte-stable shape, the addendum's eval-assertable diagnostics),
        # and stay absent on legacy stores that predate the envelope keys.
        rend = ""
        if tokens_used is not None:
            rend = " rendered_estimate={0}".format(int(tokens_used))
        adm = ""
        bcnt = ""
        if admission_used is not None:
            adm = " admission_budget={0}".format(int(admission_used))
            bcnt = " budget_dropped={0} budget_truncated={1} " \
                "budget_dropped_protected={2}".format(
                    int(budget_dropped or 0), int(budget_truncated or 0),
                    int(budget_dropped_protected or 0))
        # Issue #88 / #85 direction 2: when operation tokens augmented the
        # query, say how many — an invisible query lane cannot be debugged
        # (the #85 lesson). Additive; appended at line end.
        ops = ""
        if ops_count and ops_count > 0:
            ops = " ops={0}".format(int(ops_count))
        # Issue #117: the additive exc= field — rows suppressed via the
        # delivery-ledger --exclude (present only when > 0, same additive
        # rule as ops/moment/arms; slot pinned between ops= and sid=).
        exc = ""
        if excluded_count and excluded_count > 0:
            exc = " exc={0}".format(int(excluded_count))
        # Issue #94: always carry the sanitized session id at line end so a
        # mined failure can be bound to the injection decisions of its own
        # session (the miss-rate join key). Same sanitize rule as
        # ops_tokens._ring_path (the delivery ledger is hash-keyed,
        # issue #117 — no sanitize-truncate path component remains).
        import re as _re_sid
        safe_sid = _re_sid.sub(
            r"[^A-Za-z0-9._-]", "_", (session_id or ""))[:128] or "unknown"
        # Issue #129: the additive moment field (the hook mode) rides at
        # line end after sid= — the per-moment false-injection bucket key.
        mom = ""
        if moment:
            safe_moment = _re_sid.sub(r"[^A-Za-z0-9._-]", "_", moment)[:32]
            if safe_moment:
                mom = " moment={0}".format(safe_moment)
        # Issue #136: the additive arms attribution field — per-arm
        # post-cap/cap pairs (P/Q) from the recall envelope's ``arms`` dict,
        # so the B-1 report can see which arm carried a hit. Compact wire
        # format; absent on stores whose envelope predates the key. Wire
        # labels: fts/vec/ent/graph (the envelope key for the entity arm is
        # "entity" — issue #136 review round fixed the silent mismatch).
        armf = ""
        if isinstance(arms, dict) and arms:
            try:
                armf = " arms=" + ",".join(
                    "{0}:{1}/{2}".format(
                        label, int(arms[key].get("post", 0)),
                        int(arms[key].get("cap", 0)))
                    for label, key in (("fts", "fts"), ("vec", "vec"),
                                       ("ent", "entity"), ("graph", "graph"))
                    if key in arms
                )
            except (TypeError, ValueError, AttributeError):
                armf = ""  # malformed envelope — never break the log write
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                "[{ts}] zmem-hook status={status} reason={reason}{om} "
                "ids={ids_sel} all={ids_all}{tok}{rend}{adm}{bcnt}{ops}{exc} "
                "sid={safe_sid}{mom}{armf}\n".format(
                    ts=int(time.time()),
                    status=status,
                    reason=reason,
                    om=om,
                    ids_sel=ids_sel,
                    ids_all=ids_all,
                    tok=tok,
                    rend=rend,
                    adm=adm,
                    bcnt=bcnt,
                    ops=ops,
                    exc=exc,
                    safe_sid=safe_sid,
                    mom=mom,
                    armf=armf,
                )
            )
    except OSError:
        # Fail-open: never let the audit log block the hook.
        pass


def _format_fence(rows, header: str, store_py: str = "",
                  budget_note: str = "") -> str:
    """Render the hook-text fence (issue #58, 3.5). Imports the
    Python helper from storelib so the constants stay in one place.

    ``store_py`` is the absolute path to the caller's store.py; its
    directory (skills/memory/scripts) is where both ``storelib`` and
    ``schema_meta`` are importable from. Deriving the path from this
    file's own location is WRONG — this file lives in hooks/lib, two
    levels away from the scripts dir (caught by the round-2 behavioral
    smoke, not by any source-text assertion). The path insertion is
    restored on exit (review PRR-005: the old un-restored leak accidentally
    rescued the rotation import at the one decision-write site that runs
    after this helper, hiding that the other sites inserted the wrong
    directory). ``budget_note`` (issue #116) rides through to the storelib
    renderer for the machine-readable omission marker line.
    """
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    saved = sys.path[:]
    try:
        if scripts_dir:
            sys.path.insert(0, scripts_dir)
            sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        from storelib import _format_fenced_recall
        try:
            return _format_fenced_recall(rows, header, budget_note=budget_note)
        except TypeError:
            # PR-review hardening: a storelib older than #116 has no
            # budget_note kwarg — degrade to the legacy call instead of
            # crashing the hook (fail-open discipline).
            return _format_fenced_recall(rows, header)
    finally:
        sys.path[:] = saved


def _inject_helpers(store_py: str):
    """Load storelib/inject.py (budget + envelope helpers, issue #65 10.9/10.8).

    Same path derivation as _format_fence (store.py's scripts dir). Returns
    (apply_token_budget, inject_token_budget, estimate_tokens, envelope_results)
    or None on import failure — callers fall back to no-budget/no-unwrap
    legacy behavior (fail-open hook discipline).
    """
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        import inject as _inject_mod
        return _inject_mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _emit_envelope(ctx: str) -> None:
    print(json.dumps({"additionalContext": ctx}))


def _write_pending(session_id: str, ctx: str, rows=None) -> None:
    """Park the pre-tool fence for the fallback sidecar (issue #117).

    Append-with-dedup by memory id under atomic hashed storage: N matched
    pre-tool events between two prompts ALL survive, and a fence whose ids
    are already parked is not appended twice. Fail-open, like before.
    """
    mod = _LEDGER_MOD
    if mod is None or not session_id or not ctx:
        return
    try:
        mod.park_pending(_data_dir(), session_id, rows or [],
                         ctx, moment="pretool")
    except Exception:
        pass  # fail-open: delivery degrades to the pre-tool emit alone


def _consume_pending(session_id: str) -> str:
    """Deliver every parked fence (each id once) and clear the sidecar."""
    mod = _LEDGER_MOD
    if mod is None or not session_id:
        return ""
    try:
        return mod.consume_pending(_data_dir(), session_id)
    except Exception:
        return ""


def _clear_delivery_state(session_id: str) -> None:
    """Issue #117: compaction / session end — "already delivered" is false."""
    mod = _LEDGER_MOD
    if mod is None or not session_id:
        return
    try:
        mod.clear_delivery_state(_data_dir(), session_id)
    except Exception:
        pass


def _ops_helpers(store_py: str):
    """Load storelib/ops_tokens.py (issue #88 / #85 direction 2 —
    operation-token derivation for the inject query). Same path derivation
    as _inject_helpers; returns None on import failure and the caller
    degrades to the prose-only query (fail-open)."""
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        import ops_tokens as _ops_mod
        return _ops_mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _ledger_helpers(store_py: str):
    """Load storelib/delivery_ledger.py (issue #117 D-1 — the per-session
    delivery ledger consulted by every injection moment). Same path
    derivation as _ops_helpers; None on import failure and every ledger
    operation no-ops (fail-open: dedup degrades, delivery never breaks)."""
    scripts_dir = os.path.dirname(os.path.abspath(store_py)) if store_py else ""
    if not scripts_dir:
        return None
    saved = sys.path[:]
    try:
        sys.path.insert(0, os.path.join(scripts_dir, "storelib"))
        import delivery_ledger as _ledger_mod
        return _ledger_mod
    except Exception:
        return None
    finally:
        sys.path[:] = saved


def _sidecar_fallback_enabled() -> bool:
    """Issue #117: the pre-tool pending sidecar is RETIRED by default —
    hosts that honor pre-tool additionalContext (Claude 2.1.9+, ZCode) get
    delivery straight from the emit plus the ledger's dedup. ZMEM_PENDING_SIDECAR=1
    re-enables a narrow fallback for older host builds: append-with-dedup
    under the same atomic, hash-keyed storage as the ledger (no sanitize-
    and-truncate filename, no truncate-on-write loss)."""
    return os.environ.get("ZMEM_PENDING_SIDECAR", "") == "1"


def _transcript_tail(max_lines: int = 8, max_chars: int = 500) -> str:
    """Issue #119 fallback rung: the tail of the PARENT transcript.

    ``ZMEM_TRANSCRIPT`` is the launcher export of the hook payload's
    ``transcript_path`` — on SubagentStart that is the PARENT session's
    transcript (the launcher documents that the subagent's own turns live
    in ``agent_transcript_path`` instead). Deliberately defensive: the
    transcript format carries no compatibility guarantee and the file may
    be mid-write — every failure returns "" and the caller falls through to
    the recency pull. Never raises; output is query FUEL (bounded, never
    rendered raw)."""
    path = os.environ.get("ZMEM_TRANSCRIPT", "")
    if not path:
        return ""
    try:
        # Bounded tail read (PR #191 review): transcripts are append-only
        # JSONL and can be very large — read the last ~64KB from the end
        # instead of the whole file, then split to the last max_lines
        # complete lines (the first fragment after the seek offset is
        # likely partial and is dropped).
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if size > 65536 and lines:
            lines = lines[1:]  # drop the possibly-partial first line
    except OSError:
        return ""
    texts: list = []
    total = 0
    for line in reversed(lines[-max_lines:]):
        line = line.strip()
        if not line:
            continue
        piece = ""
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            # Real transcript lines nest the payload one level deep
            # ({"type": ..., "message": {"content": [...]}}); accept both
            # the flat and the nested shape, plus string or block-list
            # content — the format has no compat guarantee, so every
            # miss just yields no piece for that line. PR #191 review:
            # assistant tool_use blocks carry the delegation in
            # input.prompt / input.description — extract those too, or
            # this rung can never see an Agent delegation.
            candidates = [obj]
            msg = obj.get("message")
            if isinstance(msg, dict):
                candidates.append(msg)
            for cand in candidates:
                for key in ("text", "content"):
                    v = cand.get(key)
                    if isinstance(v, str) and v.strip():
                        piece = v.strip()
                        break
                    if isinstance(v, list):
                        joined = " ".join(
                            p.get("text", "") for p in v
                            if isinstance(p, dict)
                            and isinstance(p.get("text"), str))
                        if joined.strip():
                            piece = joined.strip()
                            break
                if piece:
                    break
                # tool_use shape (PR #192 review, cubic P2): the Agent
                # delegation lives in a content ITEM's input —
                # {"message": {"content": [{"type": "tool_use",
                # "input": {"prompt": ...}}]}} — so gather inputs from
                # the cand itself AND its content items.
                inputs = []
                inp = cand.get("input")
                if isinstance(inp, dict):
                    inputs.append(inp)
                content_items = cand.get("content")
                if isinstance(content_items, list):
                    inputs.extend(
                        p_item.get("input") for p_item in content_items
                        if isinstance(p_item, dict)
                        and isinstance(p_item.get("input"), dict))
                for inp_d in inputs:
                    iv = (inp_d.get("prompt")
                          or inp_d.get("description") or "")
                    if isinstance(iv, str) and iv.strip():
                        piece = iv.strip()
                        break
        elif not line.startswith("{"):
            piece = line
        if not piece:
            continue
        texts.append(piece)
        total += len(piece)
        if total >= max_chars:
            break
    return " ".join(texts)[:max_chars]


def _data_dir() -> str:
    """Resolve the data dir for the ops ring and the bg log — the single
    resolver for every passive-lane read/write in this body.

    Chain: ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA >
    ~/.zmem. ZMEM_STORE-first matches the ring writer (convention-capture.sh)
    so a split ZMEM_STORE/ZMEM_DATA deployment cannot split reader from
    writer (review PRR-91-001); the plugin-data steps give the chain the same
    ORDER as the bash writer's four explicit-env cases (and
    host.resolve_store_path), so a non-launcher environment that only sets a
    plugin-data var still finds the ring instead of silently no-op'ing the
    lane. Normalization: expanduser applies to EVERY branch — host.py
    expands all four explicit-env values and both bash writers route any
    tilde-resolved DATA_DIR through expanduser (shared helper
    hooks/lib/zmem-tilde-expand.sh), so a tilde-valued var resolves to the
    same directory on every side of the lane (a tilde ZMEM_DATA or
    ZMEM_STORE previously split reader from writer — cubic round-2 finding).
    For non-tilde values expanduser is a no-op, so launcher deployments are
    unchanged. host.py's deeper legacy tail (~/.zcode/memory, plugin scan)
    stays approximated by ~/.zmem, as before. Launcher-spawned hooks are
    unaffected: zmem-launch.js always exports ZMEM_DATA."""
    store = os.environ.get("ZMEM_STORE", "")
    if store:
        # #93 B3: a dir-less ZMEM_STORE (bare filename) must fall through to
        # the rest of the chain, not early-return dirname("")==="" (which
        # silently mis-writes every sidecar relative to CWD).
        store_dir = os.path.dirname(store)
        if store_dir:
            return os.path.expanduser(store_dir)
    data_dir = os.environ.get("ZMEM_DATA", "")
    if not data_dir:
        claude_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if claude_data:
            return os.path.expanduser(claude_data)
        zcode_data = os.environ.get("ZCODE_PLUGIN_DATA", "")
        if zcode_data:
            return os.path.expanduser(zcode_data)
        data_dir = os.path.join(os.path.expanduser("~"), ".zmem")
    return os.path.expanduser(data_dir)


def _ops_query_tokens(store_py: str, session_id: str, _ops_cache={}):
    """Derive operation tokens for this session's recent tool events
    (issue #88 / #85 direction 2). ZMEM_QUERY_CONTEXT=0 disables (kill
    switch, spec B). Fail-open: any error or missing ring degrades to []
    (prose-only query, byte-identical to the pre-#88 behavior)."""
    if not session_id:
        return []
    if "mod" in _ops_cache:
        ops_mod = _ops_cache["mod"]
    else:
        ops_mod = _ops_helpers(store_py)
        _ops_cache["mod"] = ops_mod
    if ops_mod is None:
        return []
    try:
        if not ops_mod.query_context_enabled():
            return []
        events = ops_mod.read_ops_ring(_data_dir(), session_id)
        return ops_mod.derive_ops_tokens(*events)
    except Exception:
        return []


_LEDGER_MOD = None  # issue #117: set in main(); None = dedup unavailable


def main() -> int:
    if len(sys.argv) < 4:
        return 0
    store_py = sys.argv[1]
    ns = sys.argv[2]
    try:
        budget = int(sys.argv[3])
    except (IndexError, ValueError):
        budget = 25000
    mode = sys.argv[4] if len(sys.argv) > 4 else "user_prompt"
    global _LEDGER_MOD
    _LEDGER_MOD = _ledger_helpers(store_py)
    # Optional per-mode limits (issue #58 final-critic round 2): callers
    # that previously pulled wider recent windows (subagent-recall used
    # 5 project / 3 global) can pass them instead of forking the render.
    # Defaults 3/2 match session-start Tier 2 / PreCompact.
    try:
        recent_limit = sys.argv[5]
    except IndexError:
        recent_limit = "3"
    try:
        recent_global_limit = sys.argv[6]
    except IndexError:
        recent_global_limit = "2"
    # Optional agent-type label (SubagentStart consumers only): biases
    # the rendered header, preserving the pre-#58 header contract
    # ("... agent <type>") that tests/test_launcher.js pins.
    try:
        agent_label = sys.argv[7]
    except IndexError:
        agent_label = ""
    # Issue #116: cap the label — it feeds the fence HEADER (the one shell
    # component inject.FENCE_SHELL_ALLOWANCE cannot measure per-row) and the
    # decision log; 64 chars is generous for a host agent-type label.
    if len(agent_label) > 64:
        agent_label = agent_label[:64]

    # Issue #117 (D5): session-end cleanup runs BEFORE the kill switch —
    # clearing the delivery state is not an injection and must happen even
    # under ZMEM_INJECT=0. Never recalls; emits the empty envelope; exit 0.
    if mode == "session_end":
        _end_sid = ""
        try:
            if not sys.stdin.isatty():
                _end_obj = json.loads(sys.stdin.read() or "{}")
                if isinstance(_end_obj, dict):
                    _v = _end_obj.get("session_id", "")
                    if isinstance(_v, str):
                        _end_sid = _v
        except Exception:
            pass
        if not _end_sid:
            _end_sid = (os.environ.get("ZMEM_SESSION", "")
                        or os.environ.get("CLAUDE_SESSION_ID", "")
                        or os.environ.get("ZCODE_SESSION_ID", ""))
        _clear_delivery_state(_end_sid)
        print("{}")
        return 0

    # Issue #110 (P0-5): ZMEM_INJECT=0 is the passive-injection kill switch.
    # It gates the whole body BEFORE the store.py existence check, the stdin
    # try block, and every store subprocess, so no exception path can bypass
    # it — and the decision line lands even on a broken install where
    # store.py is missing (exactly when the operator most needs the audit
    # trail; _log_inject_decision needs only the env-resolved data dir, and
    # _reason_disabled falls back to its literal when schema_meta is
    # unreachable). Session id: guarded stdin read first, env chain second —
    # sid=unknown when the host supplied none, the same fallback the other
    # decision lines use. The empty envelope is `{}`: the wrapper
    # crash-fallback shape whose missing additionalContext the launcher
    # already treats as a clean no-injection no-op. Parked pre-tool sidecars
    # are left untouched — the next enabled run consumes them, so nothing is
    # lost. Capture paths never consult this switch.
    if _inject_disabled():
        _sid = ""
        try:
            if not sys.stdin.isatty():
                _obj = json.loads(sys.stdin.read() or "{}")
                if isinstance(_obj, dict):
                    _v = _obj.get("session_id", "")
                    _sid = _v if isinstance(_v, str) else ""
        except Exception:
            _sid = ""
        if not _sid:
            _sid = (os.environ.get("ZMEM_SESSION", "")
                    or os.environ.get("CLAUDE_SESSION_ID", "")
                    or os.environ.get("ZCODE_SESSION_ID", ""))
        _log_inject_decision(
            [], [], "silent", _reason_disabled(store_py),
            session_id=_sid, moment=mode, store_py=store_py)
        print("{}")
        return 0

    if not store_py or not os.path.isfile(store_py):
        return 0

    # Issue #114: the selective gate now runs store-side on the
    # --for-injection lane (storelib.inject.selective_inject_filter, same
    # schema_meta constants), so this hook no longer resolves floors here.
    # issue #65, 10.9: budget helpers (None when storelib is not importable).
    _inj = None
    # issue #87: envelope omitted count (passive injection-risk drops) and the
    # closed reason set, resolved once for the classification below.
    omitted = 0
    ops_tokens = []
    session_id = ""
    pending_ctx = ""
    silent_reasons, injected_reason = _reason_constants(store_py)

    # Query selection per mode. `use_recent_pull` selects the query-less
    # recent lane; otherwise `query` drives the recall lane.
    use_recent_pull = False
    query = None

    try:
        raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
        try:
            stdin_obj = json.loads(raw_stdin)
        except (ValueError, TypeError):
            stdin_obj = None
        if isinstance(stdin_obj, dict):
            _sid = stdin_obj.get("session_id", "")
            session_id = _sid if isinstance(_sid, str) else ""
        # Issue #94 (bot round): a host that omits session_id from the
        # event JSON but launches through the adapter still carries it in
        # the env — the SAME chain the session-start writer uses, so both
        # decision-line writers attribute to the same session on every
        # path (manual/back-compat invocations included).
        if not session_id:
            session_id = (os.environ.get("ZMEM_SESSION", "")
                          or os.environ.get("CLAUDE_SESSION_ID", "")
                          or os.environ.get("ZCODE_SESSION_ID", ""))

        if mode == "precompact" or mode == "recent":
            # PreCompact and subagent-recall: re-inject the
            # high-confidence recent payload. No prompt text.
            use_recent_pull = True
            if mode == "precompact" and _LEDGER_MOD is not None and session_id:
                # Issue #118 (D-2 scope 3): snapshot the delivery ledger
                # into the compact sidecar BEFORE any of this mode's
                # _clear_delivery_state sites run, so the post-compaction
                # SessionStart(source=compact) can compose a query-aware
                # recall from what this session was actually delivered.
                # Fail-open: a snapshot error changes nothing — the clear
                # below still runs and the compact moment degrades to the
                # recency lane.
                try:
                    _LEDGER_MOD.snapshot_for_compact(_data_dir(), session_id)
                except Exception:
                    pass
        elif mode == "pretool":
            # PreToolUse (issue #90 / #85 C): the query is derived from the
            # TOOL INPUT ITSELF — the command or file path about to run.
            # This is the only event that sees `git stash pop` before it
            # executes (the exact #85 failure shape). Non-operation events
            # derive to nothing and stay silent (fail-open, exit 0). The
            # kill switch is GLOBAL (review round 1): ZMEM_QUERY_CONTEXT=0
            # silences every query-context lane, this one included — an
            # operator flipping it expects silence, and this lane costs a
            # subprocess per matched tool call.
            _ops_mod = _ops_helpers(store_py)
            if _ops_mod is None:
                return 0
            try:
                if not _ops_mod.query_context_enabled():
                    return 0
            except Exception:
                pass  # degrade to enabled — the switch itself must not crash
            # Issue #119: the DELEGATION tool. The delegating prompt is the
            # ideal recall query for the child and is observable ONLY here
            # (SubagentStart carries no task text on any probed host). Park
            # it for the child's SubagentStart and stay SILENT for the
            # parent — the child's own moment delivers it, and
            # double-injecting the parent would be noise. Fail-open: a
            # stash error changes nothing (the child degrades to the
            # transcript/recent rungs).
            if isinstance(stdin_obj, dict) and stdin_obj.get("tool_name") in (
                    "Agent", "Task"):
                # "Task" is the pre-rename delegation tool name (community
                # issue 29677, closed stale — not vendor-confirmed); both
                # names are accepted so older hosts are not silently dead.
                _task_text = ""
                _ti_agent = stdin_obj.get("tool_input")
                if isinstance(_ti_agent, dict):
                    # PR #191 review F-003: per-field type+length checks (the
                    # bare or-chain let a whitespace-only or non-string
                    # prompt mask a valid description).
                    for _field in ("prompt", "description"):
                        _v = _ti_agent.get(_field)
                        if isinstance(_v, str) and len(_v.strip()) >= 5:
                            _task_text = _v
                            break
                if (len(_task_text.strip()) >= 5 and session_id
                        and _LEDGER_MOD is not None):
                    try:
                        # PR #191 review F-001: the parked prompt persists
                        # verbatim in the sidecar — apply the same advisory
                        # secret-pattern redaction the capture paths use,
                        # PR #192 review (cubic/Copilot, both confirmed by
                        # an executed probe): the bare
                        # `import correction_queue` here NEVER resolved —
                        # sys.path[0] is hooks/lib and the scripts dir is
                        # two levels away, so the ImportError was silently
                        # swallowed and the prompt parked verbatim. Insert
                        # dirname(store_py) first, exactly like every
                        # other dynamic load in this body. Advisory only:
                        # prose credentials/PII are not pattern-matchable.
                        try:
                            _cq_dir = os.path.dirname(os.path.abspath(
                                store_py))
                            if _cq_dir not in sys.path:
                                sys.path.insert(0, _cq_dir)
                            import correction_queue as _cq_tt
                            _task_text, _ = _cq_tt.redact_secret_like_text(
                                _task_text)
                        except Exception:
                            pass  # fail-open: redaction must never block
                        _LEDGER_MOD.park_task_text(
                            _data_dir(), session_id, _task_text)
                    except Exception:
                        pass
                return 0
            tool_desc = ""
            if isinstance(stdin_obj, dict):
                ti = stdin_obj.get("tool_input")
                if isinstance(ti, dict):
                    tool_desc = (ti.get("command") or ti.get("file_path")
                                 or ti.get("notebook_path") or ti.get("path")
                                 or "")
                    if not isinstance(tool_desc, str):
                        tool_desc = ""
            try:
                ops_tokens = _ops_mod.derive_ops_tokens(str(tool_desc))
            except Exception:
                ops_tokens = []
            if not ops_tokens:
                return 0
            query = " ".join(ops_tokens)
        elif mode == "subagent":
            # SubagentStart (issue #90 / #85 D, ladder amended by #119).
            # Query ladder, each rung fail-opening to the next:
            #   1. payload task text when the host carries it (no probed
            #      host does — the synthetic-field tests pin the shape);
            #   2. the task text stashed by the delegating PreToolUse(Agent)
            #      call (FIFO; exact agent_id match when a host parks one —
            #      neither probed host supplies agent_id at park time, so
            #      this is FIFO in practice);
            #   3. the parent transcript tail (a FALLBACK, never the
            #      primary: the delegating assistant message may not be
            #      flushed when SubagentStart fires, and the format carries
            #      no compatibility guarantee);
            #   4. the queryless recency pull.
            task = ""
            _agent_id = ""
            if isinstance(stdin_obj, dict):
                for field in ("prompt", "task", "description"):
                    _v = stdin_obj.get(field, "")
                    if isinstance(_v, str) and len(_v.strip()) >= 5:
                        task = _v
                        break
                _v = stdin_obj.get("agent_id", "")
                if isinstance(_v, str):
                    _agent_id = _v
            if (not task and _LEDGER_MOD is not None and session_id):
                # Issue #119 rung 2: the stashed delegating task text.
                try:
                    _stashed = _LEDGER_MOD.consume_task_text(
                        _data_dir(), session_id, _agent_id)
                    if isinstance(_stashed, str) and len(_stashed.strip()) >= 5:
                        task = _stashed
                except Exception:
                    task = ""
            if not task:
                # Issue #119 rung 3: the parent transcript tail.
                task = _transcript_tail()
            if task:
                query = task[:500]
            else:
                use_recent_pull = True
        else:
            # UserPromptSubmit: the prompt text is the QUERY.
            # PRR-003 fix: stdin carries the host's JSON EVENT
            # ({"prompt": ..., "session_id": ..., "cwd": ...}); parse out
            # the prompt field (the pre-#58 wrapper contract). Non-JSON
            # stdin (plain text) is used verbatim for manual invocation.
            if isinstance(stdin_obj, dict):
                prompt = stdin_obj.get("prompt", "")
                if not isinstance(prompt, str):
                    prompt = ""
            else:
                prompt = raw_stdin
            if not prompt or len(prompt.strip()) < 5:
                return 0
            # Issue #90 / #85 C: first consume any pending pre-tool fence
            # parked for a host that may not honor additionalContext
            # pre-tool (Claude) — deliver it even if this prompt's own
            # recall is silent, then clear the sidecar.
            # Issue #117: the sidecar is retired by default; this consume
            # half pairs with the ZMEM_PENDING_SIDECAR=1 writer half.
            # Default mode parks nothing, so consuming would be a no-op
            # anyway — and a stray pre-#117 file is left to the sweep.
            if _sidecar_fallback_enabled():
                pending_ctx = _consume_pending(session_id)
            # Issue #88 / #85 direction 2: decision-point prompts are prose
            # with zero lexical overlap with the operation-adjacent lessons
            # that matter; append this session's recent tool-operation tokens
            # (from the PostToolUse ring) to the query. Fail-open: no ring /
            # opt-out / derivation error ⇒ prose-only query, byte-identical
            # to the pre-#88 behavior (compose is the identity then).
            _ops_mod = _ops_helpers(store_py)
            ops_tokens = _ops_query_tokens(store_py, session_id)
            if _ops_mod is not None and ops_tokens:
                query = _ops_mod.compose_inject_query(prompt, " ".join(ops_tokens))
            else:
                query = prompt[:500]

        # Issue #117 (D-1): consult the delivery ledger and pass the
        # delivered ids as --exclude so a row is not re-delivered within
        # the window. PreToolUse escalation: an entry whose recorded text
        # strong-matches the current operation tokens is NOT excluded —
        # the row seen at session start must still fire before the
        # dangerous command. Fail-open: any error = no exclusions.
        excluded_ids = []
        if _LEDGER_MOD is not None and session_id:
            try:
                _entries = _LEDGER_MOD.delivered(_data_dir(), session_id)
                if mode == "pretool" and ops_tokens:
                    excluded_ids = [
                        _e["id"] for _e in _entries
                        if not _LEDGER_MOD.strong_token_match(
                            _e.get("text", ""), ops_tokens)
                    ]
                else:
                    excluded_ids = [_e["id"] for _e in _entries]
            except Exception:
                excluded_ids = []
        _exclude_argv = []
        # Issue #151 review (body-942): bound the argv by the ledger cap —
        # a fixed 200 slice below the cap let delivered ids fall off the
        # exclusion list and re-deliver.
        _exclude_cap = _LEDGER_MOD.cap() if _LEDGER_MOD is not None else 200
        for _eid in excluded_ids[:_exclude_cap]:
            _exclude_argv.extend(["--exclude", _eid])

        # Issue #151 review (CUBIC-body-1135): precompact CONSUMES the parked
        # fence here — clearing it undelivered lost the content (the fallback
        # lane exists precisely for hosts that ignored the pre-tool emit).
        # Delivery happens below: prepended to the injected ctx, or emitted
        # alone on the silent path — then the delivery state clears.
        if mode == "precompact" and _sidecar_fallback_enabled():
            pending_ctx = _consume_pending(session_id)

        if use_recent_pull:
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recent",
                    "--namespace", ns,
                    "--limit", recent_limit,
                    "--min-confidence", str(_recent_floor(store_py)),
                    "--include-global",
                    "--global-limit", recent_global_limit,
                    "--no-bump",
                    "--for-injection",
                    "--json",
                    *_exclude_argv,
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            ).decode("utf-8", "replace")
        else:
            out = subprocess.check_output(
                [
                    sys.executable, store_py, "recall",
                    "--query", query,
                    "--namespace", ns,
                    "--limit", "5",
                    "--include-global",
                    "--global-limit", "3",
                    "--no-bump",
                    "--for-injection",
                    "--json",
                    *_exclude_argv,
                ],
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode("utf-8", "replace")
        rows = json.loads(out) if out.strip() else []
        # v13 (issue #65, 10.8): unwrap the read envelope ({"results": ...});
        # a bare list from a pre-v13 store.py still works. Issue #87: read the
        # envelope's omitted count BEFORE the unwrap discards it — it counts
        # rows the passive --no-bump filter dropped (injection-risk /
        # untrusted_web), the difference between "omitted" and "empty-pool".
        # Issue #114: the --for-injection lane also stamps the closed-set
        # silent reason and the PRE-gATE candidate ids on the envelope; read
        # both here, before envelope_results discards them.
        envelope_reason = None
        envelope_candidates = None
        # Issue #117: rows the store actually dropped via --exclude.
        envelope_excluded = None
        # Issue #116: hard-ceiling accounting from the store lane —
        # admission's own token accounting, protected truncation/drop
        # counts, and the ready-made fence note. None = legacy store
        # without the keys (log fields stay absent, byte-compatible).
        envelope_admission = None
        envelope_bdrop = None
        envelope_btrunc = None
        envelope_bprot = None
        envelope_note = ""
        # Issue #136: the per-arm pre/post-cap attribution dict. None =
        # legacy store without the key (the arms= log field stays absent).
        envelope_arms = None
        if isinstance(rows, dict):
            try:
                omitted = int(rows.get("omitted", 0) or 0)
            except (TypeError, ValueError):
                omitted = 0
            _er = rows.get("reason")
            if isinstance(_er, str) and _er:
                envelope_reason = _er
            _ec = rows.get("candidate_ids")
            if isinstance(_ec, list):
                envelope_candidates = [
                    str(_x) for _x in _ec if isinstance(_x, str)
                ]
            # Issue #136: gate on dict-shape, like the budget fields gate on
            # key presence — a malformed arms value never reaches the log.
            if isinstance(rows.get("arms"), dict):
                envelope_arms = rows.get("arms")
            # Issue #116 (PR-review round): gate on KEY PRESENCE, not `or 0`
            # coercion — `int(None or 0)` is 0, which would fabricate
            # measured-looking zeros into the audit log on legacy (pre-#116)
            # store envelopes instead of leaving the fields absent.
            if "budget_admission" in rows:
                try:
                    envelope_admission = int(rows.get("budget_admission") or 0)
                    envelope_bdrop = int(rows.get("budget_dropped") or 0)
                    envelope_btrunc = int(rows.get("budget_truncated") or 0)
                    envelope_bprot = int(
                        rows.get("budget_dropped_protected") or 0)
                except (TypeError, ValueError):
                    envelope_admission = None
            _bn = rows.get("budget_note")
            if isinstance(_bn, str):
                envelope_note = _bn
            _ee = rows.get("excluded")
            if isinstance(_ee, int) and not isinstance(_ee, bool):
                envelope_excluded = _ee
        _inj = _inject_helpers(store_py)
        if _inj is not None:
            rows = _inj.envelope_results(rows)
        else:
            if isinstance(rows, dict):
                rows = rows.get("results", [])
            if not isinstance(rows, list):
                rows = []
    except Exception as _store_exc:
        rows = []
        omitted = 0
        envelope_reason = None
        envelope_candidates = None
        envelope_excluded = None
        envelope_admission = None
        envelope_bdrop = None
        envelope_btrunc = None
        envelope_bprot = None
        envelope_note = ""
        envelope_arms = None
        # Issue #114 review (PRR-005): a store failure (timeout, crash, or an
        # older store.py that predates --for-injection) must not masquerade
        # as a silent empty pool with no trace. Still fail closed (inject
        # nothing) — but say why on stderr so the launcher debug log carries
        # the cause and mixed-version deployments are diagnosable.
        # Only the exception TYPE + a caller-safe detail: str() of a
        # CalledProcessError embeds the full argv, which would leak query
        # terms into the launcher debug log.
        _detail = getattr(_store_exc, "returncode", None)
        _suffix = ("returncode=" + str(_detail)) if _detail is not None else ""
        print("[zmem] store recall failed ("
              + type(_store_exc).__name__
              + (": " + _suffix if _suffix else "")
              + "); injecting nothing this event", file=sys.stderr)

    # Issue #114 (P2-3): the store subprocess ran the injection lane
    # (--for-injection) — the selective gate and the token budget were applied
    # INSIDE it, so `rows` is already the RENDERED set and the surfaced
    # telemetry was written there for exactly these rows. No local gate, no
    # local budget, no second ack process. Status/reason come from the
    # envelope; `all=` logs the envelope's pre-gate candidate ids.
    selected = rows
    status = "injected" if rows else "silent"
    tokens_budget = None
    tokens_used = None
    if _inj is not None:
        tokens_budget = _inj.inject_token_budget()
    reason = injected_reason
    if not selected:
        # Fail-open mirrors the pre-114 hook: an envelope without a reason
        # (bare-list store, parse hiccup) degrades to the local classifier
        # over the candidates we do have.
        try:
            reason = envelope_reason or _classify_silent_reason(
                rows, omitted=omitted, budget_emptied=False,
                allowed=silent_reasons,
            )
        except Exception:
            reason = "empty-pool"
        if reason == "below-bar":
            ctx = _SILENT_CTX_BELOW_BAR
        elif reason == "budget-drop":
            ctx = _SILENT_CTX_BUDGET_DROP
        else:
            # empty-pool and omitted share the string: do not teach the model
            # that omitted injection-risk rows existed (#87 spec).
            ctx = _SILENT_CTX_RETRIEVED_EMPTY
        # F18: a budget wipe still logs the token fields — on the #114 lane
        # the store already classified it (reason=budget-drop from the
        # envelope), so derive the marker instead of a local flag.
        budget_emptied = reason == "budget-drop"
        _log_inject_decision(
            rows, selected, status, reason,
            omitted=omitted,
            tokens_used=0 if budget_emptied else None,
            tokens_budget=tokens_budget if budget_emptied else None,
            ops_count=len(ops_tokens),
            session_id=session_id,
            all_ids=envelope_candidates,
            moment=mode, store_py=store_py,
            excluded_count=envelope_excluded,
            admission_used=envelope_admission,
            budget_dropped=envelope_bdrop,
            budget_truncated=envelope_btrunc,
            budget_dropped_protected=envelope_bprot,
            arms=envelope_arms,
        )
        if mode == "pretool":
            # Issue #90 / #85 C: a per-tool-call one-liner would inject noise
            # on every unmatched operation — PreToolUse stays fully silent
            # when nothing qualified (the log line above carries the reason).
            # A parked pending fence is still delivered by the NEXT
            # user_prompt run, so nothing is lost.
            return 0
        if pending_ctx:
            # Issue #90 / #85 C: deliver the parked pre-tool fence even when
            # this prompt's own recall is silent — it was never seen.
            _emit_envelope(pending_ctx)
            if mode == "precompact":
                # Issue #151 review (CUBIC-body-1135): the parked fence was
                # DELIVERED above — now clear the delivery state so the
                # post-compaction ledger starts clean (clear-after-deliver,
                # never clear-before).
                _clear_delivery_state(session_id)
            return 0
        if mode == "precompact":
            _clear_delivery_state(session_id)
        _emit_envelope(ctx)
        return 0

    header = (
        f"Relevant memories (zmem {mode}, namespace {ns}"
        + (f", agent {agent_label}" if agent_label else "")
        + "). Consider if they apply to this task; ignore if not."
    )
    ctx = _format_fence(selected, header, store_py=store_py,
                        budget_note=envelope_note)
    if budget > 0 and len(ctx) > budget:
        # PRR-015 fix: actually truncate. The previous branch reconstructed
        # the original string unchanged (no-op), so oversized memories
        # bypassed the budget. Cut the fence BODY at the budget (minus the
        # closer), then re-append the closer — the fence is never left
        # unclosed and the payload respects ZMEM_CTX_BUDGET.
        closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
        body_budget = max(0, budget - len(closer) - 1)
        ctx = ctx[:body_budget].rstrip() + "\n" + closer + "\n[recall truncated]"
    if pending_ctx and ctx:
        # Issue #90 / #85 C: prepend the parked pre-tool fence (same turn's
        # operation context) to this prompt's recall — then re-apply the
        # char budget to the COMBINED block (review round 1): the budget is
        # the outer stop for the emitted context, not per-recall.
        ctx = pending_ctx + "\n\n" + ctx
        if budget > 0 and len(ctx) > budget:
            closer = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
            body_budget = max(0, budget - len(closer) - 1)
            ctx = ctx[:body_budget].rstrip() + "\n" + closer + "\n[recall truncated]"
    # tokens_used is measured on the FINAL emitted context (post budget,
    # post char-truncation) - the honest number (issue #65, 10.9).
    if _inj is not None:
        tokens_used = _inj.estimate_tokens(ctx)
    _log_inject_decision(rows, selected, status, injected_reason,
                         omitted=omitted,
                         tokens_used=tokens_used, tokens_budget=tokens_budget,
                         ops_count=len(ops_tokens),
                         session_id=session_id,
                         all_ids=envelope_candidates,
                         moment=mode, store_py=store_py,
            excluded_count=envelope_excluded,
                         admission_used=envelope_admission,
                         budget_dropped=envelope_bdrop,
                         budget_truncated=envelope_btrunc,
                         budget_dropped_protected=envelope_bprot,
                         arms=envelope_arms)
    if (_LEDGER_MOD is not None and session_id
            and mode not in ("precompact", "session_end")):
        # Issue #117 (D-1): record the delivered ids so the NEXT moment
        # of this session suppresses them (within the window). precompact
        # does not record — it clears right after (the context is about
        # to be summarized; post-compaction delivery must not be
        # suppressed — the D-2 coordination point).
        # Issue #151 review (CUBIC-body-1200): the char-budget cut above
        # can drop tail rows from the emitted fence — recording the full
        # `selected` set would suppress rows the model never saw. Record
        # only rows whose ``- [<id>]`` bullet survived in the final ctx
        # (residual: a cut landing between a bullet and its content line
        # still counts that row — narrow, documented).
        try:
            _LEDGER_MOD.record(_data_dir(), session_id,
                               _LEDGER_MOD.rows_present_in(selected, ctx)
                               if len(ctx) < len(_format_fence(selected, header,
                                                   store_py=store_py,
                                                   budget_note=envelope_note))
                               else selected,
                               mode)
        except Exception:
            pass
    if (mode == "pretool" and os.environ.get("ZMEM_HOST", "") == "claude"
            and _sidecar_fallback_enabled()):
        # Issue #117: RETIRED by default — delivered ids live in the
        # per-session ledger (ops/<sha256>.ledger) every moment consults,
        # so the pre-tool emit is the delivery and the next prompt cannot
        # re-select the same rows. ZMEM_PENDING_SIDECAR=1 re-enables a
        # narrow fallback for older host builds: append-with-dedup under
        # atomic hash-keyed storage (the pre-#117 file was a
        # sanitize-and-truncate name written with truncate-on-write — it
        # both duplicated and lost fences, exactly what #117 removes).
        _write_pending(session_id, ctx, rows=selected)
    _emit_envelope(ctx)
    if mode == "precompact":
        # Issue #117 (D-1 scope 3): compaction — "already delivered" is
        # false once the context has been summarized away. Clear the
        # session's ledger (and any fallback pending) AFTER the emit; the
        # pre-compaction snapshot this mode takes at dispatch time (issue
        # #118, D-2) already holds the entries in the compact sidecar, so
        # nothing is lost for the post-compaction query.
        _clear_delivery_state(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())