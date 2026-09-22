"""Operation-feedback orchestration (issue #124, Workstream E).

One host operation event (a PostToolUseFailure, or one PostToolBatch
invocation) reaches the store through :func:`apply_operation_feedback`. The
function reads the session delivery ledger (issue #117), asks the existing
observational action matcher (issue #156, ``scripts/eval_replay.py``) which
delivered memories the event matches, requires the event's evidence id to be
associated with each matched memory (schema-14 ``memory_evidence``), and —
only then — increments the Voyager counters through the EXISTING sole writer
:func:`storelib.write.feedback_memory`. Recall, ledger writes, and store.py
commands are never operation outcomes.

One event cannot increment the same memory twice: the per-session feedback
sidecar (issue #124, ``delivery_ledger.feedback_event_path``) records every
``(session_id, event_id, memory_id, verdict)`` tuple and the orchestration
skips any already-recorded ``(session_id, event_id, memory_id)`` triple
regardless of which verdict was recorded. A sidecar failure raises
:class:`~storelib.delivery_ledger.FeedbackSidecarError` and rolls back every
counter update in the invocation; an invalid (missing or superseded) live
target raises the existing ``FeedbackTargetError`` and rolls back too.

A matched failed operation increments ``violated_count``; a matched success
increments ``applied_count``. An unmatched event that is inside the matcher
window of at least one delivered row is recorded once with
``memory_id=""``/``overlap=0``/``verdict="unmatched"``; an event outside the
window of every delivered row (or a session with no ledger) writes nothing.
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import sys
import time
from typing import Literal

import storelib.delivery_ledger as delivery_ledger
from storelib.evidence import evidence_ids_for_memory
from storelib.schema import now_iso
from storelib.write import FeedbackTargetError, feedback_memory

__all__ = ["FeedbackTargetError", "FeedbackSidecarError",
           "apply_operation_feedback"]

_MATCHER_WINDOW_S = 1800
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _validate_now(now: str | None) -> str:
    if now is None:
        return now_iso()
    try:
        time.strptime(now, _TS_FORMAT)
    except (TypeError, ValueError):
        raise ValueError(
            "now must be an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    return now


def _now_epoch(now: str) -> float:
    return float(calendar.timegm(time.strptime(now, _TS_FORMAT)))


def _iso_from_epoch(ts: float) -> str:
    return time.strftime(_TS_FORMAT, time.gmtime(float(ts or 0)))


def _matcher():
    """Import the issue-#156 matcher lazily from scripts/eval_replay.py (the
    same storelib-avoiding pattern that module uses for ops_tokens). Never
    redefined or overridden here."""
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))), "scripts")
    saved = sys.path[:]
    try:
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import importlib
        return importlib.import_module("eval_replay")
    finally:
        sys.path[:] = saved


def _delivered_matcher_rows(data_dir: str, session_id: str,
                            event_epoch: float) -> list:
    """Delivered rows shaped {id, session_id, timestamp, operation} from the
    session ledger: numeric ledger timestamps converted to the UTC format the
    matcher requires; operation is the entry's recorded operation when it has
    one, else its matcher-fuel text."""
    entries = delivery_ledger.delivered(data_dir, session_id, now=event_epoch)
    rows = []
    for entry in entries:
        operation = entry.get("operation") or entry.get("text") or ""
        if not operation:
            continue
        rows.append({
            "id": entry["id"],
            "session_id": session_id,
            "timestamp": _iso_from_epoch(entry.get("ts", 0)),
            "operation": str(operation),
        })
    return rows


def apply_operation_feedback(conn: sqlite3.Connection, *, data_dir: str,
                             session_id: str, event_id: str,
                             operation_tokens: list,
                             outcome: Literal["success", "failure"],
                             evidence_id: str | None = None,
                             now: str | None = None) -> list:
    """Apply one operation event's outcome to the delivered memories it
    matches. Returns rows sorted by memory_id then event_id, each with
    exactly memory_id, verdict, overlap (int), evidence_id, event_id,
    session_id — no timestamp. Counters move only for newly matched,
    previously unseen (session, event, memory) tuples."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be non-empty")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be non-empty")
    if not isinstance(operation_tokens, list):
        raise ValueError("operation_tokens must be a list of non-empty strings")
    if not operation_tokens:
        raise ValueError("operation_tokens must be non-empty")
    for token in operation_tokens:
        if not isinstance(token, str) or not token:
            raise ValueError("operation tokens must be non-empty strings")
    if outcome not in ("success", "failure"):
        raise ValueError("outcome must be 'success' or 'failure'")
    if evidence_id is not None and (not isinstance(evidence_id, str)
                                    or not evidence_id):
        raise ValueError("evidence_id must be None or a non-empty string")
    now = _validate_now(now)
    event_epoch = _now_epoch(now)

    delivered_rows = _delivered_matcher_rows(data_dir, session_id, event_epoch)
    if not delivered_rows:
        return []

    horizon = False
    for row in delivered_rows:
        try:
            delivered_epoch = calendar.timegm(
                time.strptime(row["timestamp"], _TS_FORMAT))
        except (TypeError, ValueError):
            continue
        elapsed = event_epoch - delivered_epoch
        if 0 < elapsed <= _MATCHER_WINDOW_S:
            horizon = True
            break
    if not horizon:
        return []

    evidence_row = {
        "event_id": event_id,
        "evidence_id": evidence_id,
        "session_id": session_id,
        "timestamp": now,
        "event_kind": "success" if outcome == "success" else "failure",
        "operation": " ".join(operation_tokens),
    }
    replay = _matcher()
    try:
        results = replay.match_observational_actions(
            delivered_rows, [evidence_row])
    except replay.ReplayError as exc:
        raise ValueError(str(exc).strip()) from exc

    survivors = []
    for result in results:
        action = result.get("action")
        if action not in ("applied", "violated"):
            continue
        if result.get("session_id") != session_id:
            continue
        memory_id = result.get("delivered_id")
        if not memory_id:
            continue
        if (evidence_id is not None
                and evidence_id not in evidence_ids_for_memory(conn, memory_id)):
            continue
        survivors.append((memory_id, action, int(result.get("overlap_count", 0))))

    started_tx = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        started_tx = True
    try:
        out_rows = []
        pending = []
        if survivors:
            # Count every survivor first; sidecar records are written only
            # after the whole loop succeeds so a mid-loop FeedbackTargetError
            # cannot leave durable sidecar lines ahead of rolled-back
            # counters (swarm-pr-review PRR-003). Still strictly before the
            # commit, per the issue contract.
            for memory_id, verdict, overlap in sorted(
                    survivors, key=lambda item: (item[0], event_id)):
                if delivery_ledger.feedback_seen(
                        data_dir, session_id, event_id, memory_id, "applied") or \
                   delivery_ledger.feedback_seen(
                        data_dir, session_id, event_id, memory_id, "violated"):
                    continue
                feedback_memory(conn, memory_id=memory_id, verdict=verdict)
                pending.append((memory_id, verdict, overlap))
                out_rows.append({
                    "memory_id": memory_id,
                    "verdict": verdict,
                    "overlap": overlap,
                    "evidence_id": evidence_id,
                    "event_id": event_id,
                    "session_id": session_id,
                })
        else:
            if not delivery_ledger.feedback_seen(
                    data_dir, session_id, event_id, "", "unmatched"):
                pending.append(("", "unmatched", 0))
        for memory_id, verdict, overlap in pending:
            try:
                delivery_ledger.record_feedback_event(
                    data_dir, session_id, event_id, memory_id, verdict,
                    overlap, evidence_id, now=now)
            except delivery_ledger.FeedbackSidecarError:
                raise
            except OSError as exc:
                raise delivery_ledger.FeedbackSidecarError(
                    f"feedback sidecar write failed: {exc}") from exc
        if started_tx:
            conn.commit()
        return sorted(out_rows, key=lambda r: (r["memory_id"], r["event_id"]))
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
