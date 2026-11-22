"""Per-session delivery ledger (issue #117, Workstream D-1).

A memory row delivered at one hook moment (SessionStart, UserPromptSubmit,
PreToolUse, SubagentStart) must not re-deliver at the next moment of the same
session — and the retired Claude pending sidecar must not duplicate or lose
parked fences. This module is the delivery-state substrate BOTH lanes share:

- the ledger: ``<data>/ops/<sha256(session_id)[:32]>.ledger`` — a bounded JSON
  list of ``{"id", "moment", "ts", "text"}`` entries, one per delivered memory
  id. The key hashes the FULL session id, ending the sanitize-and-truncate
  collision class of the old ``.pending`` filename (two distinct 130-char ids
  shared one file before #117).
- the fallback pending sidecar (legacy, retired with the #158 adapters):
  no storelib call API remains for them (``pending_path`` stays public;
  the terminal clear uses it and the backup sweep reaps ``.pending`` by
  suffix); a file left by a pre-#158 install under the RAW
  session-id hash is removed by a keyed terminal clear only when no
  principal/harness is presented (the composite key hashes differently -
  the same raw-id caveat documented for ``.tasktext`` below), and is
  otherwise reaped by the backup sweep.
- the compaction sidecar (issue #118, Workstream D-2):
  ``<data>/ops/<sha256(session_id)[:32]>.compact`` — the storelib compact
  API is retired (issue #323); PreCompact clears the ledger, the following
  SessionStart uses the ordinary selector, and stale files from older
  installs are reaped by the backup sweep.

Atomicity: every durable surface is tmp-file + ``os.replace`` (the
correction_queue pattern, inlined here — storelib never imports the
scripts-layer module). Issue #252: ``record()`` no longer takes any lock on
its write path — each call publishes its rows as a complete JSONL *segment*
(``<hash>.ledger.seg.<hex>``, staged through ``<hash>.ledger.tmp.<hex>``,
the tmp family purge's orphan reap already covers), so a concurrent burst of
subagent writers can never lose rows to a missed lock deadline. A
best-effort locked compaction merges segments into the base ledger; when it
cannot run, segments persist and every read merges them instead. Ledger
clears, the legacy pending sidecar, and compaction remain serialized by the
shared ``ops/.delivery-state.lock``, and the terminal ``end_delivery_state``
path still reports failure when that LOCK cannot be acquired — unlink failures
degrade to sweep-deferred best effort (review round 2: the previous claim
that unlink failures propagate is no longer true). Residual, stated plainly:
a segment lost to a genuine filesystem failure (ENOSPC, unusable dir) is
still lost — the fail-open posture this module has always had — and a
segment published while a marker-less
``clear()`` is mid-flight (after the clear's unlink glob) survives that
clear, leaving already-delivered rows visible (over-suppression only; the
6 h window prune self-heals it).

Issue #362 / #315 / #320 / #321 (0.118.0) hardening, in one place: the
non-reserve record path is admitted through :func:`record_undelivered`
(exactly-one first admission, refresh-on-re-delivery via the shared upsert
law) — since 0.118.0 ``record()`` has no production caller and is retained
only for compatibility with direct storelib callers and the installed test
pins; the feedback
sidecar's rewrite is serialized by a per-session leaf lock that fails
closed; segment reads refuse non-regular files; fully-expired segments are
reaped on the write path even when compaction cannot run; a failed
publication no longer skips the compaction drain; corrupt (including
non-finite) terminal markers follow the st_mtime fallback law instead of
escaping; the drain is a loop of bounded passes that stops at the first
failed acquire; compaction revalidates its merge against base changes,
defers unlinks when its acquire may have broken a stale lock, and verifies
its write; unlink failures degrade to sweep-deferred best effort; a lock
whose holder PID is provably dead and aged past the grace is broken instead
of waited out; and a retried ack of an already-committed admission reports
the honest duplicate label.

Bounding: entries older than the suppression window are pruned on every
reduce and the store is capped (oldest dropped) on every reduce, so the
merged view can never grow without bound; the backup sweep reaps orphans
(``.ledger``, ``.ledger.seg.`` and ``.delivery-ended`` joined the swept ops
names). Segment growth between successful compactions is bounded by the
number of record-path calls since the last one (one segment per call), and
compaction is re-attempted on every record-path call (record() and
record_undelivered() alike — review round 2 restored the drain on
refresh-only admissions). The base ledger's mtime
refreshes on every successful compaction; liveness of an actively-recording
session is carried by its fresh segments, which keep ``delivered()``
correct even while the base sits untouched. Terminal markers suppress
same-id writers for a bounded 120 seconds so a resumed session can reopen
its ledger after the short race window.

The suppression window and cap are env-tunable (``ZMEM_DELIVER_WINDOW_S``,
default 6 h; ``ZMEM_LEDGER_CAP``, default 256). Clearing: PreCompact uses
:func:`clear` to remove only the session ledger. The terminal CLI
``delivery-clear`` uses :func:`end_delivery_state` to add the bounded marker
before removing the ledger and the legacy pending sidecar.
Context summarized away or a session ending means "already delivered" is
false.

Reservations (issue #284, T-05): ``reserve()`` writes
``ops/resv-<id>.reserve`` — per-id ``{id, text, bump}`` fuel plus the keyed
identity — WITHOUT touching the ledger, and ``commit_reservation()``
(the ``prefetch-ack`` CLI / MCP ``prefetch_ack`` tool) commits the sidecar's
ids through :func:`record_undelivered` exactly once. A sidecar expires by
TTL (``ZMEM_RESERVATION_TTL_S``, default 120 s) and each ledger key holds at
most ``_RESERVATION_CAP`` (32) active reservations, oldest unacked evicted;
a committed sidecar is never evicted before its TTL reap, so a duplicate ack
stays a no-op even at cap pressure. Expired sidecars are reaped lazily on
every reserve/ack; the ack's own classification (expired / duplicate) stays
observable past its TTL by exempting the target from that reap.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

WINDOW_DEFAULT_S = 6 * 3600
CAP_DEFAULT = 256
# Escalation matcher fuel: content (+tags/entities) snapshot per entry, so
# PreToolUse can re-admit a delivered row when the operation about to run
# token-matches it strongly — without a store round-trip on the hot path.
TEXT_MAX = 400

# Issue #119: bounds for the subagent task-text stash — the delegating
# prompt/description parked at PreToolUse(Agent) and consumed at the
# child's SubagentStart.
TASK_TEXT_MAX = 800
TASK_TEXT_CAP = 16
# Issue #329: consume_task_text skips a leading entry older than this band
# ONLY while a strictly newer entry exists behind it. Conservative: above
# plausible park->SubagentStart latency (incl. permission prompts), far
# below the 6h delivery window.
TASK_TEXT_ORPHAN_S = 300.0
# Issue #428: bound on a host-supplied delegation correlation id.
TASK_TEXT_CORRELATION_ID_MAX = 128


def normalize_correlation_id(value: Any) -> str:
    """Issue #428: the delegation correlation id a host may supply at park
    time (PreToolUse ``tool_input.agent_id``) and at consume time (the
    child's SubagentStart ``agent_id``). Valid = a string that, stripped, is
    non-empty, at most TASK_TEXT_CORRELATION_ID_MAX chars, free of control
    characters (C0, DEL and the C1 range U+0080-U+009F), free of unpaired
    surrogates (U+D800-U+DFFF — a POSIX argv encoder raises on them, so an
    id the normalizer admitted would silently kill the whole store call;
    PR #453 review PRR-002), and not option-shaped (no leading ``-``).
    Anything else is treated as ABSENT ("") so a malformed id degrades to
    the untagged legacy path instead of minting an unmatchable reservation.
    hooks/lib/zmem-recall-body.py mirrors this rule (the #158 boundary keeps
    storelib out of the hook adapter); a parity test pins the two."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if (not value or len(value) > TASK_TEXT_CORRELATION_ID_MAX
            or value.startswith("-")):
        return ""
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            return ""
        if 0xD800 <= code <= 0xDFFF:
            return ""
    return value

_DELIVERY_LOCK_TIMEOUT_S = 0.25
_DELIVERY_LOCK_POLL_S = 0.005
_DELIVERY_LOCK_STALE_S = 30.0
_DELIVERY_END_MARKER_TTL_S = 120.0

# Issue #321 (PR review crosslink, 0.118.0): a holder whose PID is provably
# dead leaves the lock file orphaned for the whole stale window; with
# caller-deadline kills (issue #286) that window is reachable from routine
# operation. A lock aged past this grace whose recorded holder is dead is
# broken via the host's phantom-lock path (the schema maintenance-lock
# precedent). Default 2.0 s: at or under record_undelivered's 2 s retry
# ladder, so a phantom past the grace breaks in-moment; younger orphans are
# covered by the ladder's lock-free fallback (and the feedback path's
# fail-closed re-apply).
_DELIVERY_LOCK_GRACE_DEFAULT_S = 2.0

# Issue #315 (0.118.0): the feedback sidecar's read-modify-write is
# serialized by a per-session leaf lock with its own bounded wait; on timeout
# the write fails closed (FeedbackSidecarError) — a lost verdict is worse
# than a loud failure, though the failure is NOT always re-appliable: rows
# whose sidecar records landed before a mid-loop failure keep those records
# while their counter bumps roll back, so a retry skips them. 5.0 s: wide
# enough that the parallel-
# subagent contention shape (each sibling holding ~50 ms per event across a
# 30-event burst, times CI-class load slowdown) cannot starve a writer into
# the fail-closed path (review round 1, C3 stability).
_FEEDBACK_LOCK_TIMEOUT_S = 5.0
_FEEDBACK_LOCK_POLL_S = 0.005

# Issue #252: per-call ledger segments. ``record()`` publishes one JSONL
# segment (``<hash>.ledger.seg.<hex>``) per call with NO lock held — the
# write path's durability no longer depends on acquiring
# ``ops/.delivery-state.lock`` within any deadline — and a best-effort
# locked compaction merges segments into the base ledger. Publication is
# staged through ``<hash>.ledger.tmp.<hex>`` (the tmp family purge's orphan
# reap already covers) and lands via ``os.replace`` onto a uuid-fresh name,
# so a segment only ever exists COMPLETE: no reader or compactor can observe
# a created-but-unwritten file, and no reader can hold the published name
# open when it appears. Segments match the ``*.ledger*`` globs (purge
# residue verify, canary evidence) but not the exact ``*.ledger`` glob the
# JSON-document scrub targets.
_LEDGER_SEG_INFIX = ".ledger.seg."
_LEDGER_TMP_INFIX = ".ledger.tmp."

# Issue #252 review (P-003/P-005): compaction merges at most this many
# segments per lock hold, bounding the in-lock critical section (glob +
# reads + fsync replace + unlinks) well below the stale-lock window; a
# larger backlog drains across subsequent record() calls.
_MAX_SEGMENTS_PER_COMPACT = 64

# Issue #321 (0.118.0): the drain re-enters the bounded pass above at most
# this many times per _try_compact call (8 x 64 = 512 segments), each pass in
# its OWN bounded acquisition; the loop stops at the first failed acquire, so
# a wedged lock adds exactly one bounded wait to the caller. Every pass runs
# the merge revalidation; the per-pass hold bound is unchanged.
_MAX_COMPACT_PASSES = 8


def _entry_ts(entry: Dict[str, Any]) -> float:
    """The entry's ts as a float, degrading corrupt values to 0.0.

    ``_load_entries`` and ``_read_seg_file`` deliberately tolerate entries
    whose ts is missing or non-numeric, so every sort/compare over merged
    entries must tolerate them too (issue #252 review P-001: the reducer's
    unguarded sort key once turned one corrupt entry into a dead read
    lane)."""
    try:
        return float(entry.get("ts", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


class DeliveryStateLockError(RuntimeError):
    """The process could not acquire the shared delivery-state lock."""


def _window_s() -> int:
    raw = os.environ.get("ZMEM_DELIVER_WINDOW_S", "")
    try:
        v = int(raw) if raw else WINDOW_DEFAULT_S
    except ValueError:
        return WINDOW_DEFAULT_S
    return v if v > 0 else WINDOW_DEFAULT_S


def _cap() -> int:
    raw = os.environ.get("ZMEM_LEDGER_CAP", "")
    try:
        v = int(raw) if raw else CAP_DEFAULT
    except ValueError:
        return CAP_DEFAULT
    return v if v > 0 else CAP_DEFAULT


def _hashed_name(session_id: str, suffix: str) -> Optional[str]:
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + suffix


def _session_key(session_id: str, principal: Optional[str] = None,
                 harness: Optional[str] = None) -> str:
    """Composite ledger key (issue #284, T-05).

    When either identity dimension is present the hashed key material is
    ``principal|harness|session_id`` so two machines or harnesses that
    present the same native session id keep separate ledgers. With no
    identity the key is EXACTLY the session id — every legacy caller,
    on-disk ledger, and pinned test stays byte-identical.
    """
    if not (principal or harness):
        return session_id or ""
    return "|".join((principal or "", harness or "", session_id or ""))


def ledger_path(data_dir: str, session_id: str,
                principal: Optional[str] = None,
                harness: Optional[str] = None) -> Optional[str]:
    """Path of the session's delivery ledger, or None without inputs."""
    name = _hashed_name(_session_key(session_id, principal, harness),
                        ".ledger")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def pending_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the session's fallback pending sidecar (same hash key)."""
    name = _hashed_name(session_id, ".pending")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def tasktext_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the session's subagent task-text stash (issue #119, same
    hash key). Parked at PreToolUse(Agent), consumed (entry-removed) at the
    child's SubagentStart. NOT part of the mid-session ledger clear: a
    subagent's task text must survive another moment's delivery-state
    clear; the suppression-window prune and the backup sweep own its
    lifecycle instead."""
    name = _hashed_name(session_id, ".tasktext")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def delivery_ended_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the terminal per-session delivery marker."""
    name = _hashed_name(session_id, ".delivery-ended")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def clipped_path(data_dir: str, session_id: str,
                 principal: Optional[str] = None,
                 harness: Optional[str] = None) -> Optional[str]:
    """Path of the session's clipped-protected-row sidecar.

    Issue #501 follow-up (PR #534): a protected row that the budget
    clipped is rendered as a stub but NEVER acknowledged, so it
    re-renders on every prompt. This sidecar records the ids of the
    protected rows already shown as stubs in this session, so the next
    prompt can let one yield the budget to the rows that fit (the
    fair cross-prompt selection policy) while it still cannot be
    delivered whole.

    Review FB-01203: the key is the SAME composite identity the
    delivery ledger uses (``_session_key``), so two principals or
    harnesses presenting the same native session id never inherit each
    other's yields. With no identity the key is exactly the session id
    — the legacy, byte-identical shape."""
    name = _hashed_name(_session_key(session_id, principal, harness),
                        ".clipped")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def _load_clipped_payload(path: Optional[str]) -> dict:
    """Read the sidecar's payload, fail-open to an empty mapping."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def record_clipped(data_dir: str, session_id: str,
                  row_ids: Iterable[str],
                  now: Optional[float] = None,
                  principal: Optional[str] = None,
                  harness: Optional[str] = None) -> None:
    """Record the ids of protected rows rendered as clipped stubs.

    Bounded (the newest 64 ids win) and fail-open: a lost write costs
    one extra stub render, never a crash or a lost delivery.

    Review FB-01205: the read-modify-write runs under the delivery-state
    lock and re-checks the terminal marker INSIDE it, so an in-flight
    writer cannot republish the sidecar after ``end_delivery_state``
    cleared it. A lock failure stays fail-open (no write)."""
    key = _session_key(session_id, principal, harness)
    path = clipped_path(data_dir, session_id, principal, harness)
    if not path:
        return
    ids = [str(i) for i in row_ids if isinstance(i, str) and i]
    if not ids:
        return
    if now is None:
        now = time.time()
    try:
        with _delivery_state_lock(data_dir):
            if _delivery_state_ended(data_dir, key):
                return
            payload = _load_clipped_payload(path)
            stamps = dict(payload.get("stamps") or {})
            legacy_ts = payload.get("ts")
            # Review FB-01601: the WRITE side needs the same rule as the
            # read - only a payload with NO "stamps" key is legacy. An
            # explicitly empty map means "per-id format, nothing stamped",
            # so repopulating it from the document stamp would resurrect
            # every orphan on the next write.
            if "stamps" not in payload:
                # A legacy stamp may be numeric or a numeric string; the
                # reader accepts both, so the writer must too (round-17
                # reviewer question - the asymmetry was real).
                try:
                    legacy_ts = float(legacy_ts)
                except (TypeError, ValueError, OverflowError):
                    legacy_ts = None
            if ("stamps" not in payload
                    and legacy_ts is not None):
                # A payload written before per-id stamps: every listed id
                # inherits the document stamp.
                stamps = {str(i): legacy_ts for i in
                          (payload.get("ids") or []) if isinstance(i, str)}
            # Review FB-01302: the stamp is PER ID. Refreshing the file for
            # one clipped row must not extend another row's eligibility.
            for rid in ids:
                stamps[rid] = now
            merged = list(dict.fromkeys(
                list(payload.get("ids") or []) + ids))[-64:]
            keep = {rid: stamps[rid] for rid in merged if rid in stamps}
            # An id with no usable stamp is dropped: it cannot expire on its
            # own and carrying it forward would let the fresh document stamp
            # resurrect it (review FB-01302).
            merged = [rid for rid in merged if rid in keep]
            _atomic_write_json(path, {"ids": merged, "stamps": keep,
                                      "ts": now})
    except Exception:
        return


def read_clipped(data_dir: str, session_id: str,
                 principal: Optional[str] = None,
                 harness: Optional[str] = None) -> List[str]:
    """The ids of the protected rows already shown as clipped stubs.

    Fail-open ([] on any unreadable shape) and BOUNDED IN TIME (review
    FB-01204): a sidecar whose stamp is older than the delivery window,
    missing, or non-finite reads as the empty set, so an abandoned
    session cannot leave persistent defer state behind."""
    path = clipped_path(data_dir, session_id, principal, harness)
    if not path:
        return []
    payload = _load_clipped_payload(path)
    ids = payload.get("ids")
    if not isinstance(ids, list):
        return []
    # Review FB-01502: PRESENCE of the key selects the per-id contract; an
    # explicitly empty map means "per-id format, nothing stamped" and must
    # NOT fall back to the document stamp (that resurrected orphans).
    has_stamps_key = "stamps" in payload
    stamps = payload.get("stamps")
    if not isinstance(stamps, dict):
        stamps = {}
    try:
        legacy = float(payload.get("ts"))
    except (TypeError, ValueError, OverflowError):
        legacy = None
    # Review FB-01402: the document stamp is a LEGACY fallback only. Once a
    # payload carries a per-id map, an id missing from that map is NOT
    # inherited from the document stamp - it has no own age, so it is not
    # eligible (carrying it forward was how a stale id got resurrected).
    per_id = has_stamps_key
    window = _window_s()
    now = time.time()
    live = []
    for raw in ids:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            stamp = float(stamps[raw] if per_id else legacy)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        # Review FB-01302: each id expires on ITS OWN age, so a stale row
        # cannot ride another row's refresh.
        if math.isfinite(stamp) and (now - stamp) <= window:
            live.append(raw)
    return live


def _delivery_lock_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ops", ".delivery-state.lock")


def _delivery_lock_grace_s() -> float:
    raw = os.environ.get("ZMEM_DELIVERY_LOCK_GRACE_S", "")
    try:
        value = float(raw) if raw else _DELIVERY_LOCK_GRACE_DEFAULT_S
    except ValueError:
        return _DELIVERY_LOCK_GRACE_DEFAULT_S
    return value if value >= 0 else _DELIVERY_LOCK_GRACE_DEFAULT_S


def _maybe_break_phantom_lock(lock_path: str) -> bool:
    """Break the named lock when its holder is PROVABLY dead and the lock's
    age already exceeds the grace window (issue #321; the schema
    maintenance-lock liveness precedent). Only a dead PID implicates: a live
    slow holder is never broken here. Returns True when a break was
    attempted (the next acquire should proceed)."""
    try:
        import host
    except ImportError:
        return False
    try:
        lock_age = time.time() - os.stat(lock_path).st_mtime
    except OSError:
        return False
    if lock_age <= _delivery_lock_grace_s():
        return False
    holder = host.read_lock_holder_pid(lock_path)
    if not holder:
        return False
    holder_token, holder_pid = holder
    if holder_pid is None or host.process_alive(holder_pid) is not False:
        return False
    # expected-token form: the FULL stripped lock content, exactly the
    # maintenance lock's usage — an acquire-side token would never match.
    # Review round 2 (PR #396): the host's result is honored — False (claim
    # held, token changed hands, rename lost) means the break did NOT happen,
    # and the caller must back off instead of hot-spinning on syscalls.
    return bool(host.break_phantom_lock(lock_path, holder_token,
                                        _delivery_lock_grace_s()))


@contextlib.contextmanager
def _delivery_state_lock(data_dir: str):
    """Serialize delivery mutations per ZMEM_DATA without unlocked fallback.

    ``host.acquire_lock`` supplies the repository's cross-platform exclusive
    lock implementation.  It is deliberately wrapped with a short retry
    window and its unusable-lock sentinel is rejected instead of degrading to
    an unlocked mutation. Issue #321: a losing acquire checks for a PHANTOM
    holder (recorded PID provably dead, lock aged past the grace) and breaks
    it via the host's identity-checked path instead of waiting out the full
    stale window.
    """
    try:
        import host
    except ImportError as exc:
        raise DeliveryStateLockError("host lock support unavailable") from exc

    ops_dir = os.path.join(data_dir, "ops")
    os.makedirs(ops_dir, exist_ok=True)
    lock_path = _delivery_lock_path(data_dir)
    deadline = time.monotonic() + _DELIVERY_LOCK_TIMEOUT_S
    token = None
    try:
        while token is None:
            token = host.acquire_lock(lock_path, _DELIVERY_LOCK_STALE_S)
            if token == getattr(host, "_NO_LOCK_TOKEN", "unlocked"):
                raise DeliveryStateLockError(
                    f"could not safely acquire delivery lock: {lock_path}"
                )
            if token is not None:
                break
            # The deadline check comes FIRST: the loop is wall-clock bounded
            # no matter what. A SUCCESSFUL phantom break re-enters the
            # acquire immediately; a FAILED one (break claim held, token
            # changed hands) backs off one poll interval instead of
            # hot-spinning on stat/read/process syscalls (review round 2).
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryStateLockError(
                    f"timed out acquiring delivery lock: {lock_path}"
                )
            if _maybe_break_phantom_lock(lock_path):
                continue
            time.sleep(min(_DELIVERY_LOCK_POLL_S, remaining))
        yield
    finally:
        if token is not None:
            host.release_lock(lock_path, token)


def _delivery_state_ended(data_dir: str, session_id: str) -> bool:
    path = delivery_ended_path(data_dir, session_id)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as marker:
            raw = json.load(marker)
        if not isinstance(raw, dict) or "ended_at" not in raw:
            # Review round 2 (PR #396): a MISSING key is corrupt like any
            # other shape — float(0) would parse as finite, read as
            # long-expired, and skip the st_mtime fallback the other corrupt
            # shapes take.
            raise ValueError("missing or non-object ended_at")
        ended_at = float(raw.get("ended_at", 0))
        if not math.isfinite(ended_at):
            # Issue #320 (0.118.0): json parses 1e400/Infinity to inf
            # WITHOUT raising; a non-finite marker would read as ended
            # forever ((time.time() - inf) <= ttl is always True). Corrupt
            # per the same law below.
            raise ValueError("non-finite ended_at")
    except (OSError, TypeError, ValueError, AttributeError, OverflowError):
        # OverflowError joins the tuple (issue #320): float() of a huge JSON
        # integer raises it, and it is not a ValueError subclass — without
        # this the corrupt-marker law (fall back to st_mtime, self-healing
        # via the TTL) leaks an exception through record()'s terminal
        # re-check and out of its documented fail-open boundary.
        try:
            ended_at = os.stat(path).st_mtime
        except OSError:
            return False
    return (time.time() - ended_at) <= _DELIVERY_END_MARKER_TTL_S


def _unlink_delivery_path(path: Optional[str]) -> None:
    if not path:
        return
    # Issue #321 (0.118.0): on Windows a concurrent reader holding the file
    # open (plain open() does not request FILE_SHARE_DELETE) makes the unlink
    # raise PermissionError (winerror 32/33). Bounded retry, then give up
    # SILENTLY — the state is deferred to the 7-day sweep, and
    # end_delivery_state's terminal contract (marker atomic with the outcome,
    # no unlink failure escaping after the marker) holds.
    for _attempt in range(3):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if _attempt == 2:
                return
            time.sleep(0.01)


def _clear_delivery_sidecars_locked(data_dir: str, session_id: str) -> None:
    _unlink_delivery_path(ledger_path(data_dir, session_id))
    _unlink_delivery_path(pending_path(data_dir, session_id))
    _unlink_delivery_path(clipped_path(data_dir, session_id))
    for p in _seg_paths(data_dir, session_id):
        _unlink_delivery_path(p)


def _write_delivery_ended_locked(data_dir: str, session_id: str) -> None:
    _atomic_write_json(
        delivery_ended_path(data_dir, session_id),
        {"ended_at": time.time()},
    )


def _seg_prefix(session_id: str) -> Optional[str]:
    name = _hashed_name(session_id, "")
    if not name:
        return None
    return name + _LEDGER_SEG_INFIX


def _seg_file_readable(path: str) -> bool:
    """True only for a REGULAR file (issue #320, 0.118.0).

    Symlinks fail on POSIX (lstat mode is S_ISLNK); on Windows any reparse
    point — symlink or junction — fails via FILE_ATTRIBUTE_REPARSE_POINT, and
    directories/FIFOs/device nodes fail S_ISREG everywhere. The ops dir is a
    shared directory: a planted name must never be followed or parsed.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if os.name == "nt" and (st.st_file_attributes
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    return stat.S_ISREG(st.st_mode)


def _seg_paths(data_dir: str, session_id: str) -> List[str]:
    """This session's live segment files, oldest-written first.

    Sorted by ``(st_mtime_ns, name)`` so equal-ts upserts in the shared
    reducer resolve to the LATEST-WRITTEN entry (the refresh law the
    locked read-modify-write used to provide); the name is only a
    deterministic tie-break for segments written within one clock tick.
    Issue #320: names that are not regular files (planted symlinks, reparse
    points, FIFOs, directories) are skipped, never followed.
    """
    prefix = _seg_prefix(session_id)
    if not prefix:
        return []
    ops = os.path.join(data_dir, "ops")
    try:
        names = os.listdir(ops)
    except OSError:
        return []
    hits = []
    for n in names:
        if not n.startswith(prefix):
            continue
        p = os.path.join(ops, n)
        if not _seg_file_readable(p):
            continue
        try:
            hits.append((os.stat(p).st_mtime_ns, n, p))
        except OSError:
            continue  # published-and-replaced or swept between listdir and stat
    hits.sort()
    return [p for _, _, p in hits]


def _read_seg_file(path: str) -> Optional[List[Dict[str, Any]]]:
    """Parse one JSONL segment. Returns None when the file is unreadable
    (caller skips it — a crashed writer must never break a read) or is not a
    regular file (issue #320: defense in depth — a planted symlink/reparse/
    FIFO is refused before the open, not merely tolerated on error). Lines
    are decoded per-line with ``errors="replace"`` (issue #252 review group
    D): a crash-torn multi-byte tail corrupts only its own line, and the
    intact lines before it stay usable — the torn line simply fails
    json.loads."""
    if not _seg_file_readable(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    out: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            out.append(e)
    return out


def _entry_replaces(existing: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    """The shared upsert law: a candidate entry replaces the stored one when
    its ts is >= the stored one (equal-ts refresh from a later-written
    segment wins), and a ts that fails float coercion counts as newer
    (keep-on-failure — issue #252 review P-001). This is THE implementation:
    the reducer and the admission path (record_undelivered's
    refresh_delivered split) must never grow a second ts comparison."""
    try:
        return (float(candidate.get("ts", 0) or 0)
                >= float(existing.get("ts", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        return True


def _reduce_entries(base: List[Dict[str, Any]],
                    seg_lists: List[List[Dict[str, Any]]],
                    now: float) -> List[Dict[str, Any]]:
    """The single merge implementation for the ledger view.

    Upsert by id: a later entry replaces when its ts is >= the current one
    (>= so an equal-ts refresh from a later-written segment wins, preserving
    the last-writer-wins law the locked read-modify-write provided). Note
    the drift from the old behavior, stated deliberately: ts dominates, so
    re-recording an id with an OLDER ``now`` no longer overwrites the newer
    entry. Then window-prune, stable-sort by ts (oldest first — insertion
    order breaks ties exactly as the pre-#252 base file did), and cap
    oldest-last. Both the read path and compaction call this, so the two
    can never drift.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in base:
        eid = e.get("id")
        if isinstance(eid, str) and eid:
            by_id[eid] = e
    for seg in seg_lists:
        for e in seg:
            eid = e.get("id")
            if not isinstance(eid, str) or not eid:
                continue
            cur = by_id.get(eid)
            if cur is None:
                by_id[eid] = e
                continue
            if _entry_replaces(cur, e):
                by_id[eid] = e
    cutoff = now - _window_s()
    merged: List[Dict[str, Any]] = []
    for e in by_id.values():
        try:
            fresh = float(e.get("ts", 0) or 0) >= cutoff
        except (TypeError, ValueError, OverflowError):
            fresh = True
        if fresh:
            merged.append(e)
    merged.sort(key=_entry_ts)
    cap = _cap()
    if len(merged) > cap:
        merged = merged[-cap:]
    return merged


def _publish_segment(data_dir: str, session_id: str,
                     entries: List[Dict[str, Any]]) -> Optional[str]:
    """Atomically publish one call's rows as a complete JSONL segment.

    Staged through ``<hash>.ledger.tmp.<hex>`` then ``os.replace``d onto a
    uuid-fresh ``<hash>.ledger.seg.<hex>`` name (a name no reader can
    already hold open, so the replace cannot hit a Windows sharing
    violation). Two attempts, then fail-open — a genuine filesystem failure
    (ENOSPC, unusable dir) loses this call's rows; that residual is the
    module's documented fail-open posture. No fsync: the ledger is a
    regenerable dedup cache, and a crash costs at most recent deliveries'
    dedup, never delivery itself.

    json.dumps keeps its default ``ensure_ascii=True`` (issue #252 review
    group A): escaped output is pure ASCII, so a lone surrogate or a raw
    U+2028/U+2029 in row content can neither crash the encode nor be split
    into two lines by the readers' ``splitlines``.
    """
    prefix = _seg_prefix(session_id)
    if not prefix:
        return None
    ops = os.path.join(data_dir, "ops")
    try:
        os.makedirs(ops, exist_ok=True)
    except OSError:
        return None
    payload = "".join(json.dumps(e) + "\n" for e in entries)
    stem = prefix[:-len(_LEDGER_SEG_INFIX)]
    for _ in range(2):
        seg = os.path.join(ops, prefix + uuid.uuid4().hex)
        tmp = os.path.join(ops, stem + _LEDGER_TMP_INFIX + uuid.uuid4().hex)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
            os.replace(tmp, seg)
            return seg
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            time.sleep(0.002)
    return None


def _base_identity(path: Optional[str]) -> Optional[tuple]:
    """The base ledger's (st_mtime_ns, st_size) identity for merge
    revalidation, or None when it does not exist."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_entries_checked(path: Optional[str],
                          now: float) -> Optional[List[Dict[str, Any]]]:
    """:func:`_load_entries` plus failure discrimination for the compaction
    write path (PR #396 review round 2): "base is empty" must be distinct
    from "the base could not be read" — the swallowing variant would let a
    transient read failure re-merge from an authoritative-looking empty base
    and permanently drop base-only rows. Delegates to ``_load_entries`` first
    (so interleaving hooks that patch it keep firing — pinned by
    test_p04_ledger_reducer) and re-validates only an EMPTY result for an
    EXISTING file: a missing file is a legitimate empty base (first
    compaction), while an existing file that fails the checked re-read means
    "unreadable" → None (defer the pass)."""
    if not path:
        return []
    result = _load_entries(path, now)
    if result or not os.path.exists(path):
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except FileNotFoundError:
        return result
    except (OSError, ValueError):
        return None
    return result


def _compact_one_pass(data_dir: str, session_id: str, now: float,
                      defer_unlinks: bool) -> None:
    """One in-lock compaction pass (at most _MAX_SEGMENTS_PER_COMPACT
    segments — the P-003/P-005 hold bound, unchanged). Issue #321
    (0.118.0): three ordered defenses against a stale-broken second
    compactor:
    - merge-intent revalidation: if the base file's identity changed between
      our read and our write, re-read the base and re-merge (bounded rounds);
    - unlink-deferral: when the caller's pre-acquire stat showed a lock
      already past the stale window (our acquire may have broken a live-slow
      holder), the merge lands but NOTHING is unlinked — every writer's
      merge inputs survive for a later safe pass;
    - post-write verify: re-read the base and confirm every merged entry is
      present; on a torn/lost write, re-merge from the current base once.
    Review round 2 (PR #396): every base read on this path uses the checked
    loader — a read FAILURE degrades to "skip the write / skip the unlinks,
    segments linger for the next pass", never to "re-merge from an
    authoritative-looking empty base".
    Residual, disclosed: the microscopic stat->replace gap cannot be closed
    without a compare-and-swap primitive; the revalidation + deferral +
    phantom-liveness layers close every demonstrated interleaving.
    """
    segs = _seg_paths(data_dir, session_id)
    base_path = ledger_path(data_dir, session_id)
    base_stat = _base_identity(base_path)
    base = _load_entries_checked(base_path, now)
    if base is None:
        return  # base unreadable: merge nothing, unlink nothing this pass
    seg_lists: List[List[Dict[str, Any]]] = []
    read_ok: List[str] = []
    for p in segs[:_MAX_SEGMENTS_PER_COMPACT]:
        entries = _read_seg_file(p)
        if entries is None:
            continue
        seg_lists.append(entries)
        read_ok.append(p)
    if not read_ok:
        return
    merged = _reduce_entries(base, seg_lists, now)
    for _ in range(3):
        current = _base_identity(base_path)
        if current == base_stat:
            break
        base_stat = current
        reread = _load_entries_checked(base_path, now)
        if reread is None:
            return  # cannot revalidate: leave segments for the next pass
        base = reread
        merged = _reduce_entries(base, seg_lists, now)
    _atomic_write_json(base_path, {"entries": merged})
    final = _load_entries_checked(base_path, now)
    if final is not None:
        want = {e.get("id") for e in merged if isinstance(e, dict)}
        have = {e.get("id") for e in final if isinstance(e, dict)}
        if not want.issubset(have):
            merged = _reduce_entries(final, seg_lists, now)
            _atomic_write_json(base_path, {"entries": merged})
    if defer_unlinks:
        return
    for p in read_ok:
        _unlink_delivery_path(p)


def _reap_dead_segments(data_dir: str, session_id: str, now: float,
                        limit: int = _MAX_SEGMENTS_PER_COMPACT) -> None:
    """Lock-free best-effort reaper (issue #320, 0.118.0): unlink segments
    whose EVERY entry is out-of-window under the reducer's own freshness
    rule (in-window iff float(ts) succeeds AND ts >= cutoff; a coercion
    failure keeps the segment; NaN compares False so it is out-of-window
    exactly as the reducer drops it). Deleting dead state only: no reader
    depends on the unlinked rows. Unlinks are sharing-violation-tolerant
    (best-effort; the 7-day sweep remains the backstop).

    Review bound (PR #396 review round 1): the scan reads at most ``limit``
    segments per call — OLDEST-FIRST, so aged segments are examined before
    fresh ones. Honest cost statement (review round 2): the cap bounds the
    segment READS; the preceding ``_seg_paths`` listing still stats every
    live segment, so a call's cost is O(live segments) in listing work —
    the remainder converges across subsequent calls and the drain, and the
    7-day sweep is the backstop."""
    cutoff = now - _window_s()
    for p in _seg_paths(data_dir, session_id)[:max(0, limit)]:
        entries = _read_seg_file(p)
        if entries is None or not entries:
            # None: unreadable/non-regular. []: no valid entries — "no
            # entries" is not evidence that every entry is dead (review
            # round 2), so a vacuous-empty segment is left for the sweep.
            continue
        dead = True
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                ts = float(e.get("ts", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                dead = False
                break
            if ts >= cutoff:
                dead = False
                break
        if dead:
            _unlink_delivery_path(p)


def _try_compact(data_dir: str, session_id: str, now: float) -> None:
    """Best-effort merge of published segments into the base ledger.

    Best-effort in every direction: a lock deadline, an unreadable segment,
    or a base write failure leaves the segments on disk for the next call —
    correctness never depends on this running (reads merge segments). Under
    the lock: a fresh terminal marker means the session ended — drop every
    segment of this session instead of merging (defer to the terminal
    clear; never resurrect). Otherwise glob ONCE, read the base plus
    exactly those paths, reduce, atomic-write the base, and unlink STRICTLY
    the paths read into the merge — never re-glob at unlink time, because a
    segment published after the glob (record() does not take this lock)
    must survive to the next pass.

    Issue #252 review (P-003/P-005): one pass merges at most
    ``_MAX_SEGMENTS_PER_COMPACT`` segments, so the in-lock hold stays far
    below the stale-lock window; a bigger backlog is drained by subsequent
    record() calls. When the glob found segments but NONE were readable,
    the base is left untouched (nothing to merge; no pointless replace).

    Issue #321 (0.118.0): the backlog drain is a loop of such passes — each
    pass in its OWN bounded acquisition (the per-pass hold bound is
    unchanged), at most ``_MAX_COMPACT_PASSES`` passes per call, STOPPING at
    the first failed acquire so a wedged lock adds exactly one bounded wait
    to the caller. Every pass runs the merge revalidation and honors
    unlink-deferral (see _compact_one_pass)."""
    try:
        for _pass in range(_MAX_COMPACT_PASSES):
            defer_unlinks = _delivery_lock_looks_stale(data_dir)
            try:
                with _delivery_state_lock(data_dir):
                    if _delivery_state_ended(data_dir, session_id):
                        for p in _seg_paths(data_dir, session_id):
                            _unlink_delivery_path(p)
                        return
                    if not _seg_paths(data_dir, session_id):
                        return
                    _compact_one_pass(data_dir, session_id, now,
                                      defer_unlinks=defer_unlinks)
            except DeliveryStateLockError:
                # Wedged lock: stop draining — the caller has already paid
                # exactly one bounded wait for this pass's attempt.
                return
            if len(_seg_paths(data_dir, session_id)) <= _MAX_SEGMENTS_PER_COMPACT:
                return
    except (OSError, ValueError, TypeError, OverflowError,
            DeliveryStateLockError):
        # ValueError/TypeError/OverflowError: a corrupt ts reaching an
        # unguarded coercion must degrade this pass to "segments linger"
        # (issue #252 review P-001), never escape toward record()'s
        # fail-open boundary.
        pass


def _delivery_lock_looks_stale(data_dir: str) -> bool:
    """True when the delivery-state lock file already exists past the stale
    window — the caller's acquire is about to (or just did) break a possibly
    live holder, so this pass must defer its segment unlinks (issue #321)."""
    try:
        st = os.stat(_delivery_lock_path(data_dir))
    except OSError:
        return False
    return (time.time() - st.st_mtime) > _DELIVERY_LOCK_STALE_S


def entry_text(row: Dict[str, Any]) -> str:
    """Lowercased matcher fuel for one row: content plus tags/entities names.

    Tags may be a list of strings or a comma/word-separated string (the row
    shape differs between the recall envelope and recent rows); entities may
    be ``[{name}]`` dicts. Everything degrades to "" — the escalation matcher
    treats an empty text as non-matching.
    """
    parts: List[str] = [str(row.get("content", "") or "")]
    tags = row.get("tags")
    if isinstance(tags, str):
        parts.append(tags)
    elif isinstance(tags, (list, tuple)):
        parts.extend(str(t) for t in tags)
    ents = row.get("entities")
    if isinstance(ents, (list, tuple)):
        for e in ents:
            if isinstance(e, dict) and e.get("name"):
                parts.append(str(e["name"]))
            elif isinstance(e, str):
                parts.append(e)
    return " ".join(" ".join(parts).lower().split())[:TEXT_MAX]


def _atomic_write_json(path: str, obj: Any, *, fsync: bool = True) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp." + uuid.uuid4().hex
    # PR #192 review (cubic P2): create the tmp at 0600 AT OPEN — a
    # plain open("w") plus a post-write chmod left the fully-written
    # content group/other-readable in between (and plain chmod on
    # Windows only toggles the readonly attribute, it does not restrict
    # access; the ops dir lives under the operator profile, accepted
    # residual on Windows).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        # Issue #340: fsync is OPT-IN-OFF for the two .tasktext sidecar
        # writes only (park/consume pass fsync=False) — the same posture
        # the segment writer documents at its write site: a regenerable
        # prompt cache must not spend the caller's shared store budget on
        # durable flushes. Every other caller keeps the durable default.
        if fsync:
            os.fsync(f.fileno())
    # Issue #252 review (P-004): a concurrent reader holding `path` open
    # (plain open() does not request FILE_SHARE_DELETE on Windows) fails
    # the replace with a sharing violation — the same transient class
    # host.release_lock retries. Bounded retries, then re-raise with the
    # tmp unlinked so no *.tmp.* residue outlives the failure (AC6).
    try:
        os.replace(tmp, path)
    except OSError:
        replaced = False
        for _ in range(2):
            time.sleep(0.01)
            try:
                os.replace(tmp, path)
                replaced = True
                break
            except OSError:
                continue
        if not replaced:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_entries(path: Optional[str], now: float) -> List[Dict[str, Any]]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw.get("entries", []) if isinstance(raw, dict) else []
        if not isinstance(entries, list):
            return []
    except (OSError, ValueError):
        return []
    cutoff = now - _window_s()
    kept = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            try:
                if float(e.get("ts", 0) or 0) >= cutoff:
                    kept.append(e)
            except (TypeError, ValueError, OverflowError):
                kept.append(e)
    return kept


def delivered(data_dir: str, session_id: str,
              now: Optional[float] = None,
              principal: Optional[str] = None,
              harness: Optional[str] = None) -> List[Dict[str, Any]]:
    """The session's in-window delivered entries (pruned view; no write).

    Issue #284 (T-05): optional ``principal``/``harness`` select the
    composite ledger key; omitted they keep the legacy session-id key.

    Issue #252: merges the base ledger with any live segments through the
    shared reducer, so rows recorded by concurrent writers whose compaction
    has not run yet are visible here. An unreadable segment is skipped
    silently (fail-open read); this function performs zero writes. The
    segments are read BEFORE the base (issue #252 review group B): a
    compaction landing between the two reads writes the segment rows into
    the base before unlinking them, so reading segments first means a row
    is never missing from both snapshots at once (the pre-fix order could
    transiently hide a row — over-delivery direction, now closed)."""
    if now is None:
        now = time.time()
    key = _session_key(session_id, principal, harness)
    seg_lists: List[List[Dict[str, Any]]] = []
    for p in _seg_paths(data_dir, key):
        entries = _read_seg_file(p)
        if entries is not None:
            seg_lists.append(entries)
    base = _load_entries(ledger_path(data_dir, key), now)
    return _reduce_entries(base, seg_lists, now)


def _entries_for(rows: List[Dict[str, Any]], moment: str, now: float,
                 bump_eligible_ids: Optional[Iterable[str]]
                 ) -> List[Dict[str, Any]]:
    """Ledger entries for the given rows (shared by record paths)."""
    eligible = None if bump_eligible_ids is None else set(bump_eligible_ids)
    entries: List[Dict[str, Any]] = []
    for r in rows:
        rid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(rid, str) or not rid:
            continue
        entry = {
            "id": rid,
            "moment": str(moment or ""),
            "ts": now,
            "text": entry_text(r),
        }
        # Only stamp the marker when the caller supplied the subset.
        # A caller that does not know which rows were query-matched
        # keeps the legacy four-key entry shape (pinned by the #182
        # training acceptance repro), and readers already treat an
        # absent key as eligible -- the correct reading for a ledger
        # that only ever recorded query-matched rows.
        if eligible is not None:
            entry["bump_eligible"] = rid in eligible
        entries.append(entry)
    return entries


def record(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
           moment: str, now: Optional[float] = None,
           bump_eligible_ids: Optional[Iterable[str]] = None,
           principal: Optional[str] = None,
           harness: Optional[str] = None) -> None:
    """Record delivered rows (envelope result dicts) as ledger entries.

    Upsert by id (a re-delivery via the escalation path refreshes the entry),
    prune expired, cap oldest-last. Fail-open: any error leaves the previous
    state on disk untouched or absent — dedup degrades, delivery never breaks.

    Issue #250: the recorded set widened to every RENDERED row, which is
    larger than the set allowed to be rewarded (#136). ``bump_eligible_ids``
    names the subset that was query-matched, so a downstream consumer can tell
    the two apart from the entry itself instead of re-deriving arrival shape.
    Omit it (the default) and every row is recorded as bump-eligible, which is
    the correct meaning for a caller that only ever records query-matched rows
    and keeps older ledger files readable.

    Issue #252: the call's rows are published as one complete JSONL segment
    with NO lock held, then a best-effort locked compaction merges segments
    into the base ledger. Durability no longer depends on acquiring the
    delivery-state lock within any deadline (the pre-#252 bounded wait
    silently dropped rows under concurrent subagent writers); the only
    residual loss channel is a genuine filesystem failure on the segment
    publication itself (two attempts, then give up silently — the fail-open
    posture this module documents). After the compaction attempt the
    terminal marker is re-checked and the just-published segment is removed
    if the session ended while we worked, so a terminal clear is never
    undone. The marker-less ``clear()`` strips base and segments under the
    lock (PreCompact re-admission), but a segment published mid-clear —
    after its unlink glob — survives it: already-delivered rows stay
    visible (over-suppression only; the 6 h window prune self-heals)."""
    if not rows:
        return
    key = _session_key(session_id, principal, harness)
    path = ledger_path(data_dir, key)
    if not path:
        return
    if now is None:
        now = time.time()
    entries = _entries_for(rows, moment, now, bump_eligible_ids)
    if not entries:
        return
    seg = _publish_segment(data_dir, key, entries)
    # Issue #320 (0.118.0): the compaction drain runs even when THIS call's
    # publication failed — a failed step cancels nothing else; segments
    # already on disk still merge. The lock-free reaper then bounds on-disk
    # retention of fully-expired segments even when compaction cannot run.
    _try_compact(data_dir, key, now)
    _reap_dead_segments(data_dir, key, now)
    if seg is not None and _delivery_state_ended(data_dir, key):
        # Self-unlink after a fresh terminal marker. Wrapped (issue #252
        # review group G): a Windows sharing violation on the unlink must
        # degrade to "the segment lingers for the sweep", never escape
        # record()'s fail-open boundary.
        try:
            os.unlink(seg)
        except OSError:
            pass



def record_undelivered(data_dir: str, session_id: str,
                       rows: List[Dict[str, Any]], moment: str,
                       now: Optional[float] = None,
                       bump_eligible_ids: Optional[Iterable[str]] = None,
                       principal: Optional[str] = None,
                       harness: Optional[str] = None,
                       refresh_delivered: bool = False) -> List[str]:
    """Admit-once record (issue #362 via issue #284, T-05).

    Returns the ids THIS call admitted (first deliveries); a concurrent
    winner's ids come back as [] (the loser observes already-delivered).
    LOCKING CONTRACT: the delivery-state lock is acquired once and the
    in-lock critical section is the delivered() read + `_publish_segment`
    for the kept entries ONLY — never `_try_compact` (host.acquire_lock
    is O_EXCL and non-reentrant; a nested acquire would burn the bounded
    wait). Compaction runs after release, exactly as record() does. On
    lock-timeout the acquire retries for up to 2 s total, then proceeds
    lock-free with a fresh delivered() re-read immediately before
    publishing — so the loser either admits nothing AND observes the
    winner's entries, or the retry acquired; in the lock-pathology corner
    (both callers exhausting the retry behind a crashed holder) the call
    degrades to record()-level behavior, the module's documented fail-open
    posture. The child can never crash on the lock path. A dead lock holder
    is broken in-moment by the phantom-liveness check (issue #321).

    ``refresh_delivered`` (issue #362 remainder, 0.118.0): when True the
    call also publishes REFRESH entries — ids ALREADY delivered whose new
    entry replaces the stored one under the shared upsert law
    (:func:`_entry_replaces`) — so the escalation re-delivery and the
    bridged-subagent paths keep refreshing suppression through the
    serialized admission. They are NOT counted as admitted. Default False
    keeps the T-05 admit-once contract byte-identical.
    """
    if not rows:
        return []
    key = _session_key(session_id, principal, harness)
    if not ledger_path(data_dir, key):
        return []
    if now is None:
        now = time.time()
    entries = _entries_for(rows, moment, now, bump_eligible_ids)
    if not entries:
        return []

    def _critical() -> List[str]:
        current = {e["id"]: e
                   for e in delivered(data_dir, key, now=now)
                   if isinstance(e, dict) and isinstance(e.get("id"), str)}
        fresh: List[Dict[str, Any]] = []
        refresh: List[Dict[str, Any]] = []
        for e in entries:
            cur = current.get(e["id"])
            if cur is None:
                fresh.append(e)
            elif refresh_delivered and _entry_replaces(cur, e):
                refresh.append(e)
        keep = fresh + refresh if refresh_delivered else fresh
        if not keep:
            return []
        seg = _publish_segment(data_dir, key, keep)
        if seg is None:
            # Publication failed (the module's fail-open posture): report
            # the ids as admitted so the caller does not retry-record.
            return [e["id"] for e in fresh]
        return [e["id"] for e in fresh]

    deadline = time.monotonic() + 2.0
    while True:
        try:
            with _delivery_state_lock(data_dir):
                admitted = _critical()
            break
        except DeliveryStateLockError:
            if time.monotonic() >= deadline:
                admitted = _critical()  # lock-free fallback, fresh re-read
                break
            time.sleep(0.05)
    # PR #396 review round 2: the drain runs on EVERY routed record call —
    # including refresh-only calls (escalation / bridged-subagent re-records
    # admit nothing), whose segments otherwise accumulated one per fire with
    # zero compaction for the whole session. record()'s cadence — compact
    # after every record — is the restored law; a failed lock acquisition
    # still costs exactly one bounded wait, and correctness never depends on
    # the drain running (reads merge segments).
    _try_compact(data_dir, key, now)
    _reap_dead_segments(data_dir, key, now)
    return admitted


_RESERVE_SUFFIX = ".reserve"
_RESERVATION_TTL_DEFAULT_S = 120.0
_RESERVATION_CAP = 32


def _reservation_ttl_s() -> float:
    """ZMEM_RESERVATION_TTL_S override (positive float; default 120 s —
    the _DELIVERY_END_MARKER_TTL_S expiring-sidecar precedent)."""
    raw = os.environ.get("ZMEM_RESERVATION_TTL_S", "").strip()
    if not raw:
        return _RESERVATION_TTL_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        return _RESERVATION_TTL_DEFAULT_S
    return value if value > 0 else _RESERVATION_TTL_DEFAULT_S


def _reservation_sidecar_path(data_dir: str, reservation_id: str) -> Optional[str]:
    # Shape-check the id (review round 2, LOW recommended in-PR): ids are
    # always self-minted uuid4().hex, so anything else is caller error —
    # and on Windows a crafted id like "a/../../../evil" would otherwise
    # resolve the sidecar path OUTSIDE ops/ and let commit_reservation
    # read+mark a planted file. Malformed ids return None, which
    # commit_reservation reports as "unknown reservation" (exit 2).
    if (not data_dir or not reservation_id
            or not re.fullmatch(r"[0-9a-f]{32}", reservation_id)):
        return None
    return os.path.join(data_dir, "ops", "resv-" + reservation_id + _RESERVE_SUFFIX)


def _reservation_files(data_dir: str) -> List[str]:
    ops = os.path.join(data_dir, "ops")
    try:
        names = os.listdir(ops)
    except OSError:
        return []
    return [os.path.join(ops, n) for n in sorted(names)
            if n.startswith("resv-") and n.endswith(_RESERVE_SUFFIX)]


def _load_reservation(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _reap_expired_reservations(data_dir: str, now: float,
                               keep: Optional[set] = None) -> None:
    """Lazy TTL reap: an expired reservation commits nothing and its
    sidecar is removed (issue #284: reservations expire if unacked).
    ``keep`` names ids whose sidecars must survive this pass (the ack's
    own target, so its expired/duplicate classification stays
    observable)."""
    keep = keep or set()
    for path in _reservation_files(data_dir):
        data = _load_reservation(path)
        if data is None:
            _unlink_delivery_path(path)
            continue
        try:
            expires_at = float(data.get("ts", 0)) + float(data.get("ttl", 0))
        except (TypeError, ValueError):
            _unlink_delivery_path(path)
            continue
        if now >= expires_at and data.get("id") not in keep:
            _unlink_delivery_path(path)


def reserve(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
            moment: str, now: Optional[float] = None,
            bump_eligible_ids: Optional[Iterable[str]] = None,
            principal: Optional[str] = None,
            harness: Optional[str] = None) -> Optional[str]:
    """Reserve the rendered rows WITHOUT recording delivery (issue #284).

    Writes ``ops/resv-<id>.reserve`` (#252 atomic write, 0o600) holding
    per-id ``{id, entry_text, bump}`` fuel plus the keyed-session
    identity, so ``prefetch-ack`` can commit full-fidelity entries without
    re-opening the store. ``bump_eligible_ids`` names the query-matched
    subset (#136/#250 reward law): the flag rides the sidecar so the ack
    records the same bump_eligible markers a direct record() would have. Returns the reservation id, or None when every requested
    id is ALREADY delivered (the caller reports the already-delivered
    turn as today). Retries mint a NEW reservation and re-select (nothing
    was delivered, so the same ids come back). Expired sidecars are
    reaped lazily; the per-key active cap evicts the OLDEST UNACKED
    reservation first — a committed sidecar is never evicted before its
    TTL reap (the duplicate-ack exit-0 guarantee holds at cap pressure).
    """
    key = _session_key(session_id, principal, harness)
    if not ledger_path(data_dir, key) or not rows:
        return None
    if now is None:
        now = time.time()
    ttl = _reservation_ttl_s()
    _reap_expired_reservations(data_dir, now)
    current = {e["id"] for e in delivered(data_dir, key, now=now)}
    eligible = (None if bump_eligible_ids is None
                else set(bump_eligible_ids))
    fuel = []
    for r in rows:
        rid = r.get("id") if isinstance(r, dict) else None
        if isinstance(rid, str) and rid and rid not in current:
            entry = {"id": rid, "text": entry_text(r)}
            if eligible is not None:
                entry["bump"] = rid in eligible
            fuel.append(entry)
    if not fuel:
        return None
    # Per-key cap: evict the oldest UNACKED reservation of this key.
    same_key = []
    for path in _reservation_files(data_dir):
        data = _load_reservation(path)
        if data and data.get("session_key") == key and not data.get("committed"):
            try:
                same_key.append((float(data.get("ts", 0)), path))
            except (TypeError, ValueError):
                continue
    same_key.sort()
    while len(same_key) >= _RESERVATION_CAP:
        _unlink_delivery_path(same_key.pop(0)[1])
    reservation_id = uuid.uuid4().hex
    payload = {
        "id": reservation_id,
        "session_key": key,
        "session_id": session_id,
        "principal": principal,
        "harness": harness,
        "moment": str(moment or ""),
        "ts": now,
        "ttl": ttl,
        "rows": fuel,
    }
    path = _reservation_sidecar_path(data_dir, reservation_id)
    if not path:
        return None
    try:
        _atomic_write_json(path, payload)
    except OSError:
        return None
    return reservation_id


def commit_reservation(data_dir: str, reservation_id: str,
                       now: Optional[float] = None) -> Dict[str, Any]:
    """Acknowledge a reservation: commit exactly its ids, once (idempotent).

    Returns ``{"committed": [...], "duplicate": bool, "expired": bool}``.
    A duplicate ack is a no-op (the sidecar is retained until its TTL
    reap, never deleted on commit); an expired reservation commits
    nothing; an unknown id raises ValueError (the CLI's exit-2 refusal
    convention).
    """
    if now is None:
        now = time.time()
    # The plan's lazy-reap letter: every reserve/ack reaps expired
    # reservations — EXCEPT the target: the ack's own classification
    # (expired -> {"committed": [], "expired": true}; duplicate ->
    # exit-0 no-op) must stay observable even when the target's TTL has
    # lapsed, so the target sidecar is exempt from this reap and dies by
    # its own TTL on the next reserve/ack.
    _reap_expired_reservations(data_dir, now,
                               keep={reservation_id})
    path = _reservation_sidecar_path(data_dir, reservation_id)
    if not path or not os.path.exists(path):
        raise ValueError(f"unknown reservation: {reservation_id}")
    data = _load_reservation(path)
    if data is None:
        raise ValueError(f"unreadable reservation: {reservation_id}")
    if data.get("committed"):
        return {"committed": [], "duplicate": True, "expired": False}
    try:
        expired = now >= float(data.get("ts", 0)) + float(data.get("ttl", 0))
    except (TypeError, ValueError):
        expired = True
    if expired:
        return {"committed": [], "duplicate": False, "expired": True}
    sidecar_rows = [row for row in data.get("rows", [])
                    if isinstance(row, dict)
                    and isinstance(row.get("id"), str)]
    if not sidecar_rows:
        # Review round 2 (PR #396): a sidecar with no admissible rows is
        # degenerate (reserve() never mints one) — refusing it keeps the
        # honest-duplicate label below unambiguous (empty admission can then
        # only mean the ids are already delivered). Exit-2 refusal
        # convention; the sidecar is NOT marked committed.
        raise ValueError("reservation has no committable rows")
    rows = [{"id": row.get("id"), "content": row.get("text", "")}
            for row in sidecar_rows]
    # Reconstruct the #136/#250 query-matched subset from the sidecar's
    # bump flags. A sidecar with no flag at all is a reserve() call that
    # was not told the subset — pass None so every admitted row stays
    # bump-eligible, the same legacy reading _entries_for gives an absent
    # marker (never the inversion: absent-key-means-eligible applies only
    # when the caller genuinely did not know the split).
    if any("bump" in row for row in sidecar_rows):
        bump_ids = [row["id"] for row in sidecar_rows if row.get("bump")]
    else:
        bump_ids = None
    admitted = record_undelivered(
        data_dir, str(data.get("session_id", "")), rows,
        str(data.get("moment", "")), now=now,
        bump_eligible_ids=bump_ids,
        principal=data.get("principal"), harness=data.get("harness"))
    # Issue #321 (0.118.0): an empty admission on an UNCOMMITTED sidecar can
    # only mean the ids are already delivered — a previous incarnation of
    # this same ack died (caller-deadline kill) between its ledger admission
    # and the marker write, or another path delivered them. The honest label
    # for the retry is the duplicate no-op, not a fresh empty commit.
    data["committed"] = True
    data["committed_ts"] = now
    try:
        _atomic_write_json(path, data)
    except OSError:
        pass  # the ledger commit already holds; the marker is best-effort
    if not admitted:
        return {"committed": [], "duplicate": True, "expired": False}
    return {"committed": admitted, "duplicate": False, "expired": False}


def clear(data_dir: str, session_id: str,
          principal: Optional[str] = None,
          harness: Optional[str] = None) -> None:
    """Remove the session's ledger — base file AND any live segments
    (compaction / PreCompact re-admission). Segments ARE ledger state
    since #252: a PreCompact clear that left un-compacted segments behind
    would keep rows suppressed deterministically, defeating re-admission.
    The legacy pending sidecar is NOT touched (clear's historical
    contract; only the terminal path removes it).

    Issue #252 review (P-006): a lock deadline here PROPAGATES as
    ``DeliveryStateLockError`` — matching ``end_delivery_state`` — because
    re-admission is a state mutation the caller acts on: reporting success
    while base and segments survive is the silent over-suppression this
    function exists to prevent. (OSError during the unlinks stays
    swallowed: pre-existing partial-clear posture, tracked separately.)"""
    key = _session_key(session_id, principal, harness)
    if not ledger_path(data_dir, key):
        return
    try:
        with _delivery_state_lock(data_dir):
            _unlink_delivery_path(ledger_path(data_dir, key))
            for p in _seg_paths(data_dir, key):
                _unlink_delivery_path(p)
    except DeliveryStateLockError:
        raise
    except OSError:
        pass


def end_delivery_state(data_dir: str, session_id: str,
                       principal: Optional[str] = None,
                       harness: Optional[str] = None) -> None:
    """Terminal SessionEnd clear with an idempotent per-session marker."""
    key = _session_key(session_id, principal, harness)
    if not delivery_ended_path(data_dir, key):
        return
    with _delivery_state_lock(data_dir):
        _write_delivery_ended_locked(data_dir, key)
        _clear_delivery_sidecars_locked(data_dir, key)
    # Issue #336 AC1: the session is OVER, so the raw-id-keyed .tasktext
    # stash dies with it. Keyed on the RAW session id (tasktext_path hashes
    # the raw id at park time — cli passes args.session_id raw), NOT the
    # composite ledger key above; a keyed-session clear that derived the
    # hash from principal|harness|sid would silently miss the parked file.
    # Fail-open: an unlink failure never fails the terminal clear.
    try:
        tp = tasktext_path(data_dir, session_id)
        if tp and os.path.exists(tp):
            os.unlink(tp)
    except OSError:
        pass


def _load_tasktext(path: Optional[str], now: float) -> List[Dict[str, Any]]:
    """Load the task-text stash, pruning entries older than the same
    suppression window the ledger uses (an orphaned delegation — a stash
    whose child never started — expires with the window, and the backup
    sweep reaps the file)."""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw.get("entries", []) if isinstance(raw, dict) else []
        if not isinstance(entries, list):
            return []
    except (OSError, ValueError):
        return []
    cutoff = now - _window_s()
    kept = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("text"), str):
            try:
                if float(e.get("ts", 0) or 0) >= cutoff:
                    kept.append(e)
            except (TypeError, ValueError, OverflowError):
                kept.append(e)
    return kept


def park_task_text(data_dir: str, session_id: str, text: str,
                   agent_id: str = "", now: Optional[float] = None) -> None:
    """Issue #119: park a delegating task text at PreToolUse(Agent) for the
    child's SubagentStart to consume. Append, tag-aware cap (untagged
    entries evicted first — PR #453 review PRR-001), atomic write,
    fail-open.

    ``agent_id`` is the delegation correlation id (issue #428), normalized
    by normalize_correlation_id: a host that names the child's id in the
    delegating call (PreToolUse ``tool_input.agent_id``, forwarded by the
    hook body as ``--delegation-agent-id``) parks a TAGGED entry that only
    the matching SubagentStart ``agent_id`` consumes (see
    consume_task_text's reservation rule). Neither Claude Code nor Codex
    supplies one at park time today (the id is assigned at SubagentStart),
    so on every probed host entries stay untagged and consumption is the
    FIFO + aged-head rule below, byte-identical to 0.138.0. Accepted
    race (PR #191 review F-002, mirrored on the pre-#117 .pending
    sidecar): two parallel Agent calls in one session race the
    read-modify-write. Empirically the loser is DROPPED OUTRIGHT (not
    swapped, as an earlier revision of this docstring claimed) — the next
    child finds no entry and falls through to the query-less recent lane.
    Degraded relevance, never a crash or store corruption; a file lock
    was rejected for this sidecar (#330, recorded decision): the 0.25 s
    fail-open lock family drops whole prompts under the same contention,
    and the measured unlocked losses are bounded degradation —
    synchronized N=4 and N=8 parks each left ONE survivor; 0-200 ms
    jittered N=8 left 2, 7 and 5 survivors. The same race exists
    park-against-consume in BOTH directions (#330): a consume completing
    between a park's read and its replace re-delivers the already-
    consumed head (resurrection), and a park completing between a
    consume's read and its replace loses the freshly parked entry — the
    consume's stale rewrite (or sole-entry unlink) overwrites it. All
    four interleavings are unlocked read-modify-write races on a
    regenerable prompt cache; torn writes are impossible (UUID tmp +
    os.replace).

    An ORPHANED park is the other case: a delegation whose child never
    starts (permission denied, cancelled, tool errored) leaves its entry at
    the FIFO head and shifts every later prompt-less child by one. For
    UNTAGGED entries (every probed host today) consume_task_text applies
    an AGED-HEAD SKIP (issue #329): a leading entry older than
    TASK_TEXT_ORPHAN_S is dropped in favor of a strictly newer one. Two
    residuals are accepted and disclosed for untagged entries: an orphan
    YOUNGER than the band still shifts by one until it ages out, and a
    legitimate child delayed past the band (e.g. a long permission prompt)
    can lose its prompt once a strictly newer delegation exists. A host
    that supplies the correlation id on both sides retires both residuals
    (issue #428): its tagged entries are never aged, shifted or taken by
    another child — and (PR #453 review PRR-001) they are evicted by the
    16-entry cap ONLY after every untagged entry; an ALL-TAGGED stash
    over the cap still drops its oldest entry (the disk bound holds, that
    residual is disclosed here and in SKILL.md). The 6h window /
    16-entry cap bound how long any shift persists, NOT how many children
    it affects."""
    path = tasktext_path(data_dir, session_id)
    if not path or not text or not text.strip():
        return
    if now is None:
        now = time.time()
    # Issue #253 (restoring the #191 F-001 posture store-side): the parked
    # text is untrusted host prose. Route it through THE canonical
    # secret-redaction helper before it touches disk — advisory
    # (token-shaped secrets only). Issue #334 AC2: FAIL-CLOSED — a raising
    # redactor parks NOTHING rather than persisting raw host prose (the
    # sibling source.py posture); the park's caller (cli) keeps its own
    # fail-open guard so the parent recall is never lost either way.
    # Lazy import: write.py pulls entity/links/schema at module load.
    try:
        from storelib.write import redact_text
        text = redact_text(text)[0]
    except Exception as exc:
        # Issue #334 AC2 (fail-closed) + 0.138.0 review PRR-014: the refusal
        # must be auditable - one bounded stderr line, never a raise.
        print("[zmem] task-text park refused: redaction failed (%s)"
              % exc.__class__.__name__, file=sys.stderr)
        return
    entries = _load_tasktext(path, now)
    # PR #453 review PRR-017: the parked id must clear the SAME canonical
    # redactor the text clears. A secret-shaped id (token shapes pass the
    # validity rule) would otherwise persist cleartext in the sidecar that
    # prose would have had replaced. A redacted id is unmatchable anyway,
    # so degrade to the untagged legacy path and keep one bounded stderr
    # line for the audit trail.
    park_id = normalize_correlation_id(agent_id)
    if park_id:
        try:
            from storelib.write import redact_text
            if redact_text(park_id)[0] != park_id:
                print("[zmem] task-text park: agent_id carried a "
                      "secret-shaped value; parked untagged",
                      file=sys.stderr)
                park_id = ""
        except Exception:
            park_id = ""
    entries.append({
        "agent_id": park_id,
        "text": text[:TASK_TEXT_MAX],
        "ts": now,
    })
    if len(entries) > TASK_TEXT_CAP:
        # PR #453 review PRR-001: the oldest-first cap is TAG-AWARE — a
        # tagged entry is reserved for the child whose SubagentStart
        # carries its id, so UNTAGGED entries are evicted first; a tagged
        # entry is evicted only when the stash holds NO untagged entry to
        # drop (an all-tagged over-cap stash still drops its oldest entry
        # so the 16-entry disk bound holds — that residual is disclosed in
        # the docstring and SKILL.md). Without this a 17-park session
        # could evict a tagged park before its child arrives and hand that
        # child a sibling's prompt — the exact misdelivery class the
        # reservation rule exists to retire.
        untagged_idx = [i for i, e in enumerate(entries)
                        if not normalize_correlation_id(e.get("agent_id"))]
        if untagged_idx:
            drop = set(untagged_idx[:len(entries) - TASK_TEXT_CAP])
            entries = [e for i, e in enumerate(entries) if i not in drop]
        else:
            entries = entries[-TASK_TEXT_CAP:]
    try:
        # Issue #340: no fsync on this sidecar — a regenerable prompt
        # cache must not spend the caller's shared store budget on a
        # durable flush (the segment writer's documented posture).
        _atomic_write_json(path, {"entries": entries}, fsync=False)
    except OSError:
        pass


def consume_task_text(data_dir: str, session_id: str,
                      agent_id: str = "") -> str:
    """Issue #119 + #329 + #428: the child's SubagentStart takes one task
    text — an exact ``agent_id`` match (normalize_correlation_id) wins when
    the host tagged the park and the child carries the same id; otherwise
    FIFO with an AGED-HEAD SKIP over the UNTAGGED entries only (tagged
    entries are reserved for their id — the #428 reservation rule):
    leading untagged entries older than ``TASK_TEXT_ORPHAN_S`` are dropped
    in favor of a strictly newer untagged one, so an orphaned park stops
    shifting later children once it ages past the band (residuals, untagged
    only: a younger orphan still shifts until it ages out, and a live child
    delayed past the band can lose its prompt to a strictly newer
    delegation — see park_task_text). The consume is CALLER-GATED since
    0.138.0 (#339): the selector passes it only for granted SubagentStart
    lanes. Removes the consumed entry (and any skipped aged heads);
    returns "" when nothing is stashed. Fail-open. No rung after the
    stash but the query-less recent lane.

    Accepted race (#253 review, mirror of the park-side F-002 posture):
    two children whose SubagentStarts fire concurrently can both load the
    same head entry before either rewrite lands. On UNTAGGED entries both
    receive that same delegation (duplicated delivery — the text is served
    twice, not lost). On TAGGED entries each concurrent consume drops its
    own index and rewrites the whole file, so the loser's stale rewrite
    RESURRECTS the entry the winner already consumed (last-writer-wins:
    an untagged entry can be delivered twice, a tagged entry stays inert
    until its own id arrives); degraded relevance either way, never a
    crash or store corruption; a lock would add a failure mode to this
    fail-open hot path."""
    path = tasktext_path(data_dir, session_id)
    if not path:
        return ""
    now = time.time()
    entries = _load_tasktext(path, now)
    if not entries:
        # Issue #336 AC2: every entry expired. The in-memory prune above is
        # the only expiry mechanism, so an all-expired stash would otherwise
        # keep its stale payload on disk until the age sweep; unlink it now
        # (fail-open — the next park rewrites the file regardless). The
        # bounded retry mirrors the consumed-head branch below: on Windows a
        # concurrent reader holding the file open fails the delete with a
        # sharing violation, and a surviving file here means the NEXT
        # consume re-reads the same expired stash once before the sweep.
        for _attempt in range(2):
            try:
                if os.path.exists(path):
                    os.unlink(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                if _attempt:
                    break
                time.sleep(0.01)
        return ""
    idx = None
    drop: set[int] = set()
    want = normalize_correlation_id(agent_id)
    # Issue #119 contract first: an exact agent_id match outranks BOTH the
    # aging heuristic and FIFO (the correlation key is exact — it must
    # never be aged away; pinned by OrphanAgingBandTest guards).
    if want:
        for i, e in enumerate(entries):
            if normalize_correlation_id(e.get("agent_id")) == want:
                idx = i
                break
    if idx is None:
        # Issue #428 RESERVATION RULE: without a matching id (no id, or an
        # id that tags nothing here) the FIFO + aged-head rule runs over the
        # UNTAGGED entries only — a TAGGED entry is reserved for the child
        # whose SubagentStart carries its id: never served to another
        # child, never aged out by one, and never counted as the "strictly
        # newer" successor that ages an untagged head. With no tagged
        # entries (every probed host today) the pool is the whole stash and
        # this is byte-identical to 0.138.0. Nothing untagged -> "" and the
        # stash is left untouched (no write).
        pool = [i for i, e in enumerate(entries)
                if not normalize_correlation_id(e.get("agent_id"))]
        if not pool:
            # PR #453 review PRR-017: the stash holds ONLY tag-reserved
            # entries and this consume cannot take any. Emit one bounded
            # stderr line so a stranded reservation is observable (the
            # decision line would otherwise record a generic empty-pool
            # and the reserved prompts would be silently undeliverable
            # until the window prune). Fail-open, never a raise.
            print("[zmem] task-text stash holds %d tag-reserved entr%s; "
                  "nothing consumable without a matching id"
                  % (len(entries), "y" if len(entries) == 1 else "ies"),
                  file=sys.stderr)
            return ""

        # Issue #329 aged-head skip: drop leading entries with FINITE ts
        # older than TASK_TEXT_ORPHAN_S, but ONLY while a strictly newer
        # (finite-ts) entry exists behind them. Guards (issue #329
        # review): entries with malformed/non-finite ts are never aged
        # (conservative — treat as live; a MISSING ts reads as 0 at the
        # loader's window check and is pruned there, never reaching this
        # loop); a sole aged entry is served (no strictly newer
        # successor); each skipped head is dropped from the write-back so
        # the next child is not shifted by it either.
        # PR #453 review PRR-014: the malformed-ts case combined with the
        # reservation rule means a TAGGED entry with an unparseable ts
        # (corrupted/hand-edited sidecar only — park always writes a
        # finite float) is never aged, never pool-served, and survives
        # the window prune in memory; it remains bounded by the session
        # end unlink, the 16-entry cap and the ops age sweep, and its own
        # id can still consume it.
        # Residuals (disclosed, untagged entries only): a younger-than-band
        # orphan still shifts by one; a live child delayed past the band can
        # lose its prompt once a strictly newer delegation exists. Issue
        # #428: a host supplying the correlation id retires both.
        def _finite_ts(entry: Any) -> float | None:
            try:
                v = float(entry.get("ts"))
            except (TypeError, ValueError, OverflowError):
                # Issue #320 precedent (see _entry_ts): OverflowError is not a
                # ValueError subclass; a huge numeric ts is malformed like any
                # other — treat as live, never age it, never raise.
                return None
            return v if math.isfinite(v) else None

        while len(pool) > 1:
            head_ts = _finite_ts(entries[pool[0]])
            if head_ts is None or (now - head_ts) <= TASK_TEXT_ORPHAN_S:
                break
            newer = any(
                (lambda t: t is not None and t > head_ts)(
                    _finite_ts(entries[j]))
                for j in pool[1:]
            )
            if not newer:
                break
            drop.add(pool.pop(0))
        idx = pool[0]
    text = str(entries[idx].get("text", ""))
    drop.add(idx)
    entries = [e for i, e in enumerate(entries) if i not in drop]
    try:
        if entries:
            # Issue #340: no fsync on this sidecar (park-side rationale).
            _atomic_write_json(path, {"entries": entries}, fsync=False)
        else:
            # Same transient class the replace path retries: on Windows a
            # concurrent reader holding the file open (plain open() does not
            # request FILE_SHARE_DELETE) fails the delete with a sharing
            # violation. Without the retry the consumed head stays on disk
            # and the NEXT child re-reads the same delegation — so a
            # surviving file is exactly the harm case, not a harmless
            # leftover.
            for _attempt in range(2):
                try:
                    os.unlink(path)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    if _attempt:
                        pass
                    else:
                        time.sleep(0.01)
    except OSError:
        pass
    return text


def rows_present_in(rows: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """The subset of rows whose fenced bullet contains its ``[<id>]`` token.

    Scoped recall prefixes bullets with ``[tier=<name>]`` and injection-risk
    rows can carry additional known markers before the dash, so the id is no
    longer always the first bracketed token after ``-``.  Restrict the match
    to the renderer's marker shape and one bullet line so arbitrary prose
    cannot count as delivery and a short id cannot match a longer prefix.

    The subset of rows whose fence bullet appears in the FINAL emitted context
    (issue #151 review: a char-budget cut can drop
    tail rows AFTER scoring — recording them anyway would suppress rows
    the model never saw). The renderer always embeds a bracketed ``[<id>]``
    token on its bullet, so this marker is reliable (a bare id substring could
    false-positive across prefix ids like r1/r10)."""
    if not text or not rows:
        return rows
    present = []
    for row in rows:
        rid = str(row.get("id", ""))
        if not rid:
            continue
        # The CONTESTED arm accepts the named marker (issue #254):
        # `[CONTESTED LINK: <id>]`. The `: <one token>` shape is closed
        # by construction on the RENDER side, not by the id's provenance:
        # inject.contested_marker emits the named form only for a single
        # token free of whitespace, brackets and angle brackets, and the
        # bare form otherwise — so prose containing spaces or brackets still
        # cannot pose as a marker. (Ids are uuid4-minted on the write path,
        # but sync ingest and store imports can carry other shapes; the
        # render-side guard is what keeps accounting exact for those.)
        marker = re.compile(
            r"(?m)^(?: \[(?:PREVIOUSLY|INJECTION RISK|UNTRUSTED TOOL|"
            r"UNTRUSTED WEB|CONTESTED LINK(?:: [^\]\s]+)?)\])*-\s+"
            r"(?:\[tier=(?:project|domain|fleet_host|cross_project|"
            r"user_global|unknown)\]\s+)?\["
            + re.escape(rid) + r"\](?:\s|$)"
        )
        if marker.search(text):
            present.append(row)
    return present


def strong_token_match(text: str, tokens: List[str]) -> bool:
    """The issue's escalation rule: the operation tokens match the row
    STRONGLY when every derived token appears in the row's recorded text
    as a WHOLE token (non-alphanumeric boundaries — issue #151 review:
    bare substring semantics let "popular" satisfy the token "pop").

    Deliberately conservative in the direction of NOT escalating: a single
    missing token keeps the row suppressed (the row had its chance this
    session); all tokens present means the command about to run is exactly
    the hazard this row describes, so the repeat delivery is the point.
    """
    toks = [str(t).lower() for t in (tokens or []) if str(t).strip()]
    if not toks:
        return False
    hay = str(text or "").lower()
    for t in toks:
        pat = _boundary_pattern(t)
        if not pat.search(hay):
            return False
    return True


_BOUNDARY_CACHE: Dict[str, "re.Pattern"] = {}


def _boundary_pattern(token: str) -> "re.Pattern":
    """(?<![A-Za-z0-9])token(?![A-Za-z0-9]) — the token must not be glued
    to an alphanumeric on either side (punctuation-adjacent is fine, so
    path/flag-shaped tokens like -rf or ./x still match)."""
    pat = _BOUNDARY_CACHE.get(token)
    if pat is None:
        import re as _re
        pat = _re.compile(
            r"(?<![A-Za-z0-9])" + _re.escape(token) + r"(?![A-Za-z0-9])")
        _BOUNDARY_CACHE[token] = pat
    return pat


# ---------------------------------------------------------------------------
# Issue #124 (Workstream E): per-session operation-feedback sidecar.
# ``<data>/ops/<sha256(session_id)[:32]>.feedback.jsonl`` — one compact
# sorted-key JSON object per line, LF-terminated. The sidecar makes one host
# operation event count at most once per memory: the command-level loader
# checks the (event_id, memory_id, session_id) tuple before a verdict is
# applied again, regardless of verdict. Like every write in this module it
# is atomic (tmp + fsync + os.replace); unlike the delivery ledger it is
# NOT fail-open on read — a sidecar failure is a correctness signal for the
# feedback loop and raises FeedbackSidecarError (the orchestration rolls the
# counter transaction back).
# ---------------------------------------------------------------------------


class FeedbackSidecarError(RuntimeError):
    """Raised when the operation-feedback sidecar cannot be read as needed or
    written. The CLI maps this to exit 1 — an operational failure, not a
    usage error."""


_FEEDBACK_VERDICTS = ("applied", "violated", "unmatched")
# One malformed-sidecar warning per path per command (process): a long
# session replaying many events must not spam the same diagnostic.
_MALFORMED_SIDECAR_WARNED: set = set()


def feedback_event_path(data_dir: str, session_id: str) -> str:
    """Sidecar path for one session's operation-feedback records."""
    if not session_id:
        raise ValueError("session_id must be non-empty")
    name = _hashed_name(session_id, ".feedback.jsonl")
    if not name:
        raise ValueError("session_id must be non-empty")
    return os.path.join(data_dir, "ops", name)


def feedback_lock_path(data_dir: str, session_id: str) -> str:
    """Per-session leaf lock for the feedback sidecar's read-modify-write
    (issue #315, 0.118.0). Per-session granularity: feedback for different
    sessions never contends, and this lock is a LEAF — never held while
    taking another lock."""
    name = _hashed_name(session_id, ".feedback.lock")
    if not name:
        raise ValueError("session_id must be non-empty")
    return os.path.join(data_dir, "ops", name)


@contextlib.contextmanager
def _feedback_state_lock(data_dir: str, session_id: str):
    """Serialize one session's feedback-sidecar rewrite (issue #315).

    WITHOUT this lock two hook processes applying operation feedback for the
    same session (parallel subagents share the session id) race the
    load-replace and silently lose a verdict — the #252 defect class on the
    feedback surface. Bounded wait, then FAIL CLOSED
    (FeedbackSidecarError): unlike the delivery ledger this sidecar is a
    correctness signal, and a lost merge is worse than a loud failure —
    though the failure is NOT always re-appliable: rows whose sidecar
    records landed before a mid-loop failure keep those records while their
    counter bumps roll back, so a retry skips them (review round 2, PR
    #396). A killed writer's orphan is broken by
    the same phantom-liveness check the delivery lock uses (issue #321), so
    the orphan window is the grace, not the stale window."""
    try:
        import host
    except ImportError as exc:
        raise FeedbackSidecarError("host lock support unavailable") from exc
    path = feedback_lock_path(data_dir, session_id)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    deadline = time.monotonic() + _FEEDBACK_LOCK_TIMEOUT_S
    token = None
    try:
        while token is None:
            token = host.acquire_lock(path, _DELIVERY_LOCK_STALE_S)
            if token == getattr(host, "_NO_LOCK_TOKEN", "unlocked"):
                raise FeedbackSidecarError(
                    f"could not safely acquire feedback lock: {path}")
            if token is not None:
                break
            # Deadline first, then the phantom-break attempt; a FAILED break
            # backs off one poll interval (see the delivery lock loop — the
            # bounded wait is the wall-clock bound, the sleep is the yield).
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FeedbackSidecarError(
                    f"timed out acquiring feedback lock: {path}")
            if _maybe_break_phantom_lock(path):
                continue
            time.sleep(min(_FEEDBACK_LOCK_POLL_S, remaining))
        yield
    finally:
        if token is not None:
            host.release_lock(path, token)


def _load_feedback_records(path: str) -> list:
    """Read feedback sidecar records. A malformed file is treated as empty
    with exactly one stderr warning per path per command (issue #124)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise FeedbackSidecarError(f"feedback sidecar unreadable: {exc}") from exc
    records = []
    ok = True
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            ok = False
            break
        if isinstance(rec, dict):
            records.append(rec)
        else:
            ok = False
            break
    if not ok and path not in _MALFORMED_SIDECAR_WARNED:
        _MALFORMED_SIDECAR_WARNED.add(path)
        print(f"[zmem] WARNING: malformed feedback sidecar ignored: {path}",
              file=sys.stderr)
        records = []
    return records


def _feedback_record_tuple(rec: dict) -> tuple:
    return (rec.get("session_id"), rec.get("event_id"),
            rec.get("memory_id"), rec.get("verdict"))


def feedback_seen(data_dir: str, session_id: str, event_id: str,
                  memory_id: str, verdict: str) -> bool:
    """True when the full (session_id, event_id, memory_id, verdict) tuple is
    already recorded. Never reads another session's hashed file."""
    path = feedback_event_path(data_dir, session_id)
    records = _load_feedback_records(path)
    wanted = (session_id, event_id, memory_id, verdict)
    return any(_feedback_record_tuple(r) == wanted for r in records)


def record_feedback_event(data_dir: str, session_id: str, event_id: str,
                          memory_id: str, verdict: str, overlap: int,
                          evidence_id: str | None,
                          now: str | None = None) -> None:
    """Append one feedback record atomically (tmp + fsync + os.replace).

    The record has exactly seven keys, serialized with sort_keys=True and
    compact separators so line bytes are deterministic (issue #124 fixture
    contract). memory_id may be "" only for verdict "unmatched".
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    if not event_id:
        raise ValueError("event_id must be non-empty")
    if verdict not in _FEEDBACK_VERDICTS:
        raise ValueError("verdict must be one of 'applied', 'violated', "
                         "'unmatched'")
    if verdict == "unmatched":
        if memory_id:
            raise ValueError("unmatched records carry no memory id")
    elif not memory_id:
        raise ValueError("memory_id must be non-empty")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("overlap must be a non-negative integer")
    if evidence_id is not None and (not isinstance(evidence_id, str)
                                    or not evidence_id):
        raise ValueError("evidence_id must be None or a non-empty string")
    if now is None:
        from storelib.schema import now_iso
        now = now_iso()
    path = feedback_event_path(data_dir, session_id)
    record = {
        "event_id": event_id,
        "evidence_id": evidence_id,
        "memory_id": memory_id,
        "overlap": int(overlap),
        "session_id": session_id,
        "timestamp": now,
        "verdict": verdict,
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Issue #315 (0.118.0): the load-all + rewrite-all + replace sequence is
    # serialized per session — the unlocked form lost whole verdicts to the
    # read-modify-write race (the #252 class on this surface).
    with _feedback_state_lock(data_dir, session_id):
        # Review round 2 (PR #396): the existing records load BEFORE the tmp
        # file exists — a FeedbackSidecarError from an unreadable sidecar
        # can no longer strand an empty *.tmp.* residue that no sweep owns.
        existing = _load_feedback_records(path)
        tmp = path + ".tmp." + uuid.uuid4().hex
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                for rec in existing:
                    f.write(json.dumps(rec, sort_keys=True,
                                       separators=(",", ":"),
                                       ensure_ascii=False) + "\n")
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, path)
            except OSError:
                # Bounded sharing-violation retry (concurrent AV/indexer
                # holds), then the fail-closed FeedbackSidecarError.
                replaced = False
                for _ in range(2):
                    time.sleep(0.01)
                    try:
                        os.replace(tmp, path)
                        replaced = True
                        break
                    except OSError:
                        continue
                if not replaced:
                    raise
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise FeedbackSidecarError(
                f"feedback sidecar write failed: {exc}") from exc
