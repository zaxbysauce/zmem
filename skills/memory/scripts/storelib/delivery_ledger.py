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
- the fallback pending sidecar (legacy, env-gated callers only; the current
  #158 adapters consume the rendered selector envelope): the same hashed
  naming and atomic write, but append-with-dedup — N parked fences between
  prompts all survive.
- the compaction sidecar (issue #118, Workstream D-2):
  ``<data>/ops/<sha256(session_id)[:32]>.compact`` — retained as a
  backward-compatible storelib API for older callers. The #158 adapters no
  longer produce or consume it: PreCompact clears the ledger and the
  following SessionStart uses the ordinary selector.

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
shared ``ops/.delivery-state.lock``; legacy helpers remain fail-open when
that lock cannot be acquired, and the terminal ``end_delivery_state`` path
still propagates the failure so the CLI cannot claim a cleanup that did not
happen. Residual, stated plainly: a segment lost to a genuine filesystem
failure (ENOSPC, unusable dir) is still lost — the fail-open posture this
module has always had — and a segment published while a marker-less
``clear()`` is mid-flight (after the clear's unlink glob) survives that
clear, leaving already-delivered rows visible (over-suppression only; the
6 h window prune self-heals it).

Bounding: entries older than the suppression window are pruned on every
reduce and the store is capped (oldest dropped) on every reduce, so the
merged view can never grow without bound; the backup sweep reaps orphans
(``.ledger``, ``.ledger.seg.`` and ``.delivery-ended`` joined the swept ops
names). Segment growth between successful compactions is bounded by the
number of ``record()`` calls since the last one (one segment per call), and
compaction is re-attempted on every record. The base ledger's mtime
refreshes on every successful compaction; liveness of an actively-recording
session is carried by its fresh segments, which keep ``delivered()``
correct even while the base sits untouched. Terminal markers suppress
same-id writers for a bounded 120 seconds so a resumed session can reopen
its ledger after the short race window.

The suppression window and cap are env-tunable (``ZMEM_DELIVER_WINDOW_S``,
default 6 h; ``ZMEM_LEDGER_CAP``, default 256). Clearing: PreCompact uses
:func:`clear` to remove only the session ledger; generic
:func:`clear_delivery_state` removes the ledger and legacy pending sidecar
without closing the session. The terminal CLI ``delivery-clear`` uses
:func:`end_delivery_state` to add the bounded marker before removing both.
Context summarized away or a session ending means "already delivered" is
false.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
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
# Issue #118: bound on the stashed compact_summary. This remains for the
# backward-compatible storelib compact helpers; #158 adapters do not call
# them.
COMPACT_SUMMARY_MAX = 2000

# Issue #119: bounds for the subagent task-text stash — the delegating
# prompt/description parked at PreToolUse(Agent) and consumed at the
# child's SubagentStart.
TASK_TEXT_MAX = 800
TASK_TEXT_CAP = 16

_DELIVERY_LOCK_TIMEOUT_S = 0.25
_DELIVERY_LOCK_POLL_S = 0.005
_DELIVERY_LOCK_STALE_S = 30.0
_DELIVERY_END_MARKER_TTL_S = 120.0

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


def cap() -> int:
    """Public accessor for the ledger cap (callers bound their --exclude
    argv with it so no delivered id can fall off the exclusion list)."""
    return _cap()


def _hashed_name(session_id: str, suffix: str) -> Optional[str]:
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + suffix


def ledger_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the session's delivery ledger, or None without inputs."""
    name = _hashed_name(session_id, ".ledger")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def pending_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the session's fallback pending sidecar (same hash key)."""
    name = _hashed_name(session_id, ".pending")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def compact_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the legacy compaction sidecar (issue #118).

    Kept for backward-compatible storelib callers. The #158 hook and
    SessionStart adapters no longer write or read this path.
    """
    name = _hashed_name(session_id, ".compact")
    if not name or not data_dir:
        return None
    return os.path.join(data_dir, "ops", name)


def tasktext_path(data_dir: str, session_id: str) -> Optional[str]:
    """Path of the session's subagent task-text stash (issue #119, same
    hash key). Parked at PreToolUse(Agent), consumed (entry-removed) at the
    child's SubagentStart. NOT part of ``clear_delivery_state``: a
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


def _delivery_lock_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ops", ".delivery-state.lock")


@contextlib.contextmanager
def _delivery_state_lock(data_dir: str):
    """Serialize delivery mutations per ZMEM_DATA without unlocked fallback.

    ``host.acquire_lock`` supplies the repository's cross-platform exclusive
    lock implementation.  It is deliberately wrapped with a short retry
    window and its unusable-lock sentinel is rejected instead of degrading to
    an unlocked mutation.
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryStateLockError(
                    f"timed out acquiring delivery lock: {lock_path}"
                )
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
            ended_at = float(json.load(marker).get("ended_at", 0))
    except (OSError, TypeError, ValueError, AttributeError):
        try:
            ended_at = os.stat(path).st_mtime
        except OSError:
            return False
    return (time.time() - ended_at) <= _DELIVERY_END_MARKER_TTL_S


def _unlink_delivery_path(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _clear_delivery_sidecars_locked(data_dir: str, session_id: str) -> None:
    _unlink_delivery_path(ledger_path(data_dir, session_id))
    _unlink_delivery_path(pending_path(data_dir, session_id))
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


def _seg_paths(data_dir: str, session_id: str) -> List[str]:
    """This session's live segment files, oldest-written first.

    Sorted by ``(st_mtime_ns, name)`` so equal-ts upserts in the shared
    reducer resolve to the LATEST-WRITTEN entry (the refresh law the
    locked read-modify-write used to provide); the name is only a
    deterministic tie-break for segments written within one clock tick.
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
        try:
            hits.append((os.stat(p).st_mtime_ns, n, p))
        except OSError:
            continue  # published-and-replaced or swept between listdir and stat
    hits.sort()
    return [p for _, _, p in hits]


def _read_seg_file(path: str) -> Optional[List[Dict[str, Any]]]:
    """Parse one JSONL segment. Returns None when the file is unreadable
    (caller skips it — a crashed writer must never break a read). Lines are
    decoded per-line with ``errors="replace"`` (issue #252 review group D):
    a crash-torn multi-byte tail corrupts only its own line, and the intact
    lines before it stay usable — the torn line simply fails json.loads."""
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
            try:
                newer = (float(e.get("ts", 0) or 0)
                         >= float(cur.get("ts", 0) or 0))
            except (TypeError, ValueError, OverflowError):
                newer = True
            if newer:
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
    below the stale-lock window even under a large backlog (a live holder
    whose lock is broken mid-section would let a second compactor
    last-writer-wins the base); a bigger backlog is drained by subsequent
    record() calls. When the glob found segments but NONE were readable,
    the base is left untouched (nothing to merge; no pointless replace).
    """
    try:
        with _delivery_state_lock(data_dir):
            if _delivery_state_ended(data_dir, session_id):
                for p in _seg_paths(data_dir, session_id):
                    _unlink_delivery_path(p)
                return
            segs = _seg_paths(data_dir, session_id)
            if not segs:
                return
            base = _load_entries(ledger_path(data_dir, session_id), now)
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
            _atomic_write_json(ledger_path(data_dir, session_id),
                               {"entries": merged})
            for p in read_ok:
                _unlink_delivery_path(p)
    except (OSError, ValueError, TypeError, OverflowError,
            DeliveryStateLockError):
        # ValueError/TypeError/OverflowError: a corrupt ts reaching an
        # unguarded coercion must degrade this pass to "segments linger"
        # (issue #252 review P-001), never escape toward record()'s
        # fail-open boundary.
        pass


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


def _atomic_write_json(path: str, obj: Any) -> None:
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
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The session's in-window delivered entries (pruned view; no write).

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
    seg_lists: List[List[Dict[str, Any]]] = []
    for p in _seg_paths(data_dir, session_id):
        entries = _read_seg_file(p)
        if entries is not None:
            seg_lists.append(entries)
    base = _load_entries(ledger_path(data_dir, session_id), now)
    return _reduce_entries(base, seg_lists, now)


def delivered_ids(data_dir: str, session_id: str,
                  now: Optional[float] = None) -> List[str]:
    return [e["id"] for e in delivered(data_dir, session_id, now=now)]


def record(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
           moment: str, now: Optional[float] = None,
           bump_eligible_ids: Optional[Iterable[str]] = None) -> None:
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
    path = ledger_path(data_dir, session_id)
    if not path:
        return
    if now is None:
        now = time.time()
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
    if not entries:
        return
    seg = _publish_segment(data_dir, session_id, entries)
    if seg is None:
        return
    _try_compact(data_dir, session_id, now)
    if _delivery_state_ended(data_dir, session_id):
        # Self-unlink after a fresh terminal marker. Wrapped (issue #252
        # review group G): a Windows sharing violation on the unlink must
        # degrade to "the segment lingers for the sweep", never escape
        # record()'s fail-open boundary.
        try:
            os.unlink(seg)
        except OSError:
            pass


def clear(data_dir: str, session_id: str) -> None:
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
    if not ledger_path(data_dir, session_id):
        return
    try:
        with _delivery_state_lock(data_dir):
            _unlink_delivery_path(ledger_path(data_dir, session_id))
            for p in _seg_paths(data_dir, session_id):
                _unlink_delivery_path(p)
    except DeliveryStateLockError:
        raise
    except OSError:
        pass


def park_pending(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
                 fence: str, moment: str,
                 now: Optional[float] = None) -> None:
    """Fallback sidecar (older host builds): append-with-dedup, atomic.

    Unlike the pre-#117 ``open(path, "w")`` sidecar, N parked fences between
    two prompts ALL survive; a fence whose memory ids are all already parked
    is not appended twice (dedup by id, not by text).

    Issue #252 scope note: this legacy sidecar keeps the locked
    read-modify-write whose deadline-loss silently drops a parked fence —
    the same loss class record() just shed. Deliberately out of #252's
    scope ("make record() safe"): the #158 adapters no longer call this
    (env-gated legacy callers only), so the race has no live trigger on
    current hosts. Disclosed in the PR; do not silently re-derive it as
    fixed."""
    path = pending_path(data_dir, session_id)
    if not path or not fence:
        return
    if now is None:
        now = time.time()
    try:
        with _delivery_state_lock(data_dir):
            if _delivery_state_ended(data_dir, session_id):
                return
            entries = _load_entries(path, now)
            parked_ids = {e["id"] for e in entries}
            new_rows = [r for r in rows
                        if isinstance(r, dict) and isinstance(r.get("id"), str)
                        and r["id"] not in parked_ids]
            if not new_rows:
                return  # every id in this fence is already parked — dedup
            for i, r in enumerate(new_rows):
                entries.append({
                    "id": r["id"],
                    "moment": str(moment or ""),
                    "ts": now,
                    # Issue #151 review (COPILOT-2): the fence covers ALL new rows
                    # of this park call — store it ONCE (on the first entry) so
                    # consume cannot join the identical fence N times for an
                    # N-row event; consume_pending drops empty fences.
                    "fence": fence if i == 0 else "",
                })
            _atomic_write_json(path, {"entries": entries})
    except (OSError, DeliveryStateLockError):
        pass


def consume_pending(data_dir: str, session_id: str) -> str:
    """Concatenated parked fences (each id once), clearing the sidecar."""
    path = pending_path(data_dir, session_id)
    if not path:
        return ""
    try:
        with _delivery_state_lock(data_dir):
            entries = _load_entries(path, time.time())
            _unlink_delivery_path(path)
    except (OSError, DeliveryStateLockError):
        return ""
    fences = [e.get("fence", "") for e in entries if e.get("fence")]
    ctx = "\n\n".join(f for f in fences if isinstance(f, str) and f.strip())
    return ctx


def clear_delivery_state(data_dir: str, session_id: str) -> None:
    """Compaction / session end: "already delivered" is false again."""
    if not ledger_path(data_dir, session_id):
        return
    try:
        with _delivery_state_lock(data_dir):
            _clear_delivery_sidecars_locked(data_dir, session_id)
    except (OSError, DeliveryStateLockError):
        pass


def end_delivery_state(data_dir: str, session_id: str) -> None:
    """Terminal SessionEnd clear with an idempotent per-session marker."""
    if not delivery_ended_path(data_dir, session_id):
        return
    with _delivery_state_lock(data_dir):
        _write_delivery_ended_locked(data_dir, session_id)
        _clear_delivery_sidecars_locked(data_dir, session_id)


def _load_compact(path: Optional[str]) -> Dict[str, Any]:
    """Read the legacy compaction sidecar, degrading to an empty stash."""
    if not path:
        return {"entries": [], "summary": None, "ts": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"entries": [], "summary": None, "ts": 0}
    if not isinstance(raw, dict):
        return {"entries": [], "summary": None, "ts": 0}
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    summary = raw.get("summary")
    if not isinstance(summary, str):
        summary = None
    return {"entries": entries, "summary": summary, "ts": raw.get("ts", 0) or 0}


def snapshot_for_compact(data_dir: str, session_id: str,
                         now: Optional[float] = None) -> None:
    """Legacy #118 PreCompact snapshot helper.

    Retained for older direct storelib callers. The #158 PreCompact adapter
    intentionally uses ``ledger-clear`` instead and never calls this helper.
    Issue #252 note: this reads the BASE ledger only — rows still sitting in
    unpublished segments (compaction skipped) are not snapshotted. Legacy-
    only path, disclosed, not carried further."""
    path = compact_path(data_dir, session_id)
    if not path:
        return
    if now is None:
        now = time.time()
    try:
        entries = _load_entries(ledger_path(data_dir, session_id), now)
        _atomic_write_json(path, {
            "entries": entries,
            "summary": None,
            "ts": now,
        })
    except OSError:
        pass


def park_compact_summary(data_dir: str, session_id: str, summary: str,
                         now: Optional[float] = None) -> None:
    """Legacy #118 PostCompact summary helper, retained for compatibility.

    #158 adapters do not call this function, so live PostCompact events never
    create the old compact sidecar.
    """
    path = compact_path(data_dir, session_id)
    if not path or not summary or not summary.strip():
        return
    if now is None:
        now = time.time()
    stash = _load_compact(path)
    stash["summary"] = summary[:COMPACT_SUMMARY_MAX]
    stash["ts"] = now
    try:
        _atomic_write_json(path, stash)
    except OSError:
        pass


def read_compact_context(data_dir: str, session_id: str):
    """Read the legacy compact stash without consuming it."""
    path = compact_path(data_dir, session_id)
    if not path:
        return (None, [])
    stash = _load_compact(path)
    return (stash.get("summary"), stash.get("entries") or [])


def discard_compact_context(data_dir: str, session_id: str) -> None:
    """Drop the legacy compact stash once its old caller completes."""
    path = compact_path(data_dir, session_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def consume_compact_context(data_dir: str, session_id: str):
    """Read-and-discard convenience for legacy compact callers."""
    summary, entries = read_compact_context(data_dir, session_id)
    discard_compact_context(data_dir, session_id)
    return (summary, entries)


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
    child's SubagentStart to consume. Append, cap oldest-first, atomic
    write, fail-open.

    ``agent_id`` is wiring for a future host that supplies it at PARK time:
    neither Claude Code nor Codex does today (the id is assigned at
    SubagentStart), so on every probed host consumption is FIFO. Accepted
    race (PR #191 review F-002, mirrored on the pre-#117 .pending
    sidecar): two parallel Agent calls in one session race the
    read-modify-write. Empirically the loser is DROPPED OUTRIGHT (not
    swapped, as an earlier revision of this docstring claimed) — the next
    child finds no entry and falls through to the transcript-tail rung.
    Degraded relevance, never a crash or store corruption; a file lock
    would add a failure mode to a fail-open hot path for a rare,
    non-corrupting race. A cancelled delegation also leaves its entry at
    the FIFO head until the window prune — the next child may consume
    stale text (accepted, bounded by the 6h window / 16-entry cap)."""
    path = tasktext_path(data_dir, session_id)
    if not path or not text or not text.strip():
        return
    if now is None:
        now = time.time()
    # Issue #253 (restoring the #191 F-001 posture store-side): the parked
    # text is untrusted host prose. Route it through THE canonical
    # secret-redaction helper before it touches disk — advisory
    # (token-shaped secrets only), fail-open. Lazy import: write.py pulls
    # entity/links/schema at module load.
    try:
        from storelib.write import redact_text
        text = redact_text(text)[0]
    except Exception:
        pass
    entries = _load_tasktext(path, now)
    entries.append({
        "agent_id": str(agent_id or ""),
        "text": text[:TASK_TEXT_MAX],
        "ts": now,
    })
    if len(entries) > TASK_TEXT_CAP:
        entries = entries[-TASK_TEXT_CAP:]
    try:
        _atomic_write_json(path, {"entries": entries})
    except OSError:
        pass


def consume_task_text(data_dir: str, session_id: str,
                      agent_id: str = "") -> str:
    """Issue #119: the child's SubagentStart takes one task text — an
    exact ``agent_id`` match wins when a host parks one; otherwise the
    OLDEST unconsumed entry (host dispatch order: serially-spawned children
    start in the order they were delegated; out-of-order arrival of truly
    concurrent children is the documented FIFO limitation). Removes the
    consumed entry; returns "" when nothing is stashed. Fail-open."""
    path = tasktext_path(data_dir, session_id)
    if not path:
        return ""
    now = time.time()
    entries = _load_tasktext(path, now)
    if not entries:
        return ""
    idx = 0
    if agent_id:
        for i, e in enumerate(entries):
            if e.get("agent_id") == agent_id:
                idx = i
                break
    text = str(entries[idx].get("text", ""))
    del entries[idx]
    try:
        if entries:
            _atomic_write_json(path, {"entries": entries})
        else:
            os.unlink(path)
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
        marker = re.compile(
            r"(?m)^(?: \[(?:PREVIOUSLY|INJECTION RISK|UNTRUSTED TOOL|"
            r"UNTRUSTED WEB|CONTESTED LINK)\])*-\s+"
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
    tmp = path + ".tmp." + uuid.uuid4().hex
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            existing = _load_feedback_records(path)
            for rec in existing:
                f.write(json.dumps(rec, sort_keys=True,
                                   separators=(",", ":"),
                                   ensure_ascii=False) + "\n")
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise FeedbackSidecarError(f"feedback sidecar write failed: {exc}") from exc
