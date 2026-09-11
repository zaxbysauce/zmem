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
- the fallback pending sidecar (env-gated by the caller for older host builds
  that ignore pre-tool additionalContext): the same hashed naming and atomic
  write, but append-with-dedup — N parked fences between prompts all survive.
- the compaction sidecar (issue #118, Workstream D-2):
  ``<data>/ops/<sha256(session_id)[:32]>.compact`` — the handoff between the
  three compaction moments. PreCompact snapshots the ledger into it (then
  clears the ledger); PostCompact merges the host's ``compact_summary`` into
  it; the post-compaction SessionStart consumes it exactly once (compose a
  query-aware recall from summary + the pre-compaction delivered texts) and
  unlinks it. A second compaction overwrites it via a fresh snapshot, and the
  backup sweep reaps any stash a dead session left behind.

Atomicity: every write is tmp-file + ``os.replace`` (the correction_queue
pattern, inlined here — storelib never imports the scripts-layer module).
Every function is fail-open: ledger state is an optimization over the pre-#117
behavior (re-delivery), never a correctness gate, so an OSError degrades to
"no dedup this event" instead of blocking a hook.

Bounding: entries older than the suppression window are pruned on every load
and the store is capped (oldest dropped) on every write, so a file can never
grow without bound; the backup sweep reaps orphans (``.ledger`` joined the
swept ops suffixes). The file's mtime refreshes on every record() write, so a
live session is never the oldest thing in the ops dir.

The suppression window and cap are env-tunable (``ZMEM_DELIVER_WINDOW_S``,
default 6 h; ``ZMEM_LEDGER_CAP``, default 256). Clearing: the precompact and
session_end moments call :func:`clear_delivery_state` — context summarized
away or session over means "already delivered" is false.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

WINDOW_DEFAULT_S = 6 * 3600
CAP_DEFAULT = 256
# Escalation matcher fuel: content (+tags/entities) snapshot per entry, so
# PreToolUse can re-admit a delivered row when the operation about to run
# token-matches it strongly — without a store round-trip on the hot path.
TEXT_MAX = 400
# Issue #118: bound on the stashed compact_summary. The summary is host
# prose (unbounded upstream); the stash is only ever recall QUERY fuel, so
# a generous headroom that still cannot balloon the ops file suffices.
COMPACT_SUMMARY_MAX = 2000

# Issue #119: bounds for the subagent task-text stash — the delegating
# prompt/description parked at PreToolUse(Agent) and consumed at the
# child's SubagentStart.
TASK_TEXT_MAX = 800
TASK_TEXT_CAP = 16


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
    """Path of the session's compaction sidecar (issue #118, same hash key).

    Written by PreCompact (snapshot) and PostCompact (summary merge); read
    and unlinked once by the post-compaction SessionStart. NOT part of
    ``clear_delivery_state`` on purpose: at PreCompact the snapshot must
    SURVIVE the delivery-state clear that follows in the same hook run.
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
    os.replace(tmp, path)
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
            except (TypeError, ValueError):
                kept.append(e)
    return kept


def delivered(data_dir: str, session_id: str,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The session's in-window delivered entries (pruned view; no write)."""
    if now is None:
        now = time.time()
    return _load_entries(ledger_path(data_dir, session_id), now)


def delivered_ids(data_dir: str, session_id: str,
                  now: Optional[float] = None) -> List[str]:
    return [e["id"] for e in delivered(data_dir, session_id, now=now)]


def record(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
           moment: str, now: Optional[float] = None) -> None:
    """Record delivered rows (envelope result dicts) as ledger entries.

    Upsert by id (a re-delivery via the escalation path refreshes the entry),
    prune expired, cap oldest-last. Fail-open: any error leaves the previous
    state on disk untouched or absent — dedup degrades, delivery never breaks.
    """
    if not rows:
        return
    path = ledger_path(data_dir, session_id)
    if not path:
        return
    if now is None:
        now = time.time()
    entries = _load_entries(path, now)
    by_id = {e["id"]: e for e in entries}
    for r in rows:
        rid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(rid, str) or not rid:
            continue
        by_id[rid] = {
            "id": rid,
            "moment": str(moment or ""),
            "ts": now,
            "text": entry_text(r),
        }
    merged = list(by_id.values())
    merged.sort(key=lambda e: float(e.get("ts", 0) or 0))
    cap = _cap()
    if len(merged) > cap:
        merged = merged[-cap:]
    try:
        _atomic_write_json(path, {"entries": merged})
    except OSError:
        pass


def clear(data_dir: str, session_id: str) -> None:
    """Remove the session's ledger (compaction / session end)."""
    path = ledger_path(data_dir, session_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def park_pending(data_dir: str, session_id: str, rows: List[Dict[str, Any]],
                 fence: str, moment: str,
                 now: Optional[float] = None) -> None:
    """Fallback sidecar (older host builds): append-with-dedup, atomic.

    Unlike the pre-#117 ``open(path, "w")`` sidecar, N parked fences between
    two prompts ALL survive; a fence whose memory ids are all already parked
    is not appended twice (dedup by id, not by text).
    """
    path = pending_path(data_dir, session_id)
    if not path or not fence:
        return
    if now is None:
        now = time.time()
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
    try:
        _atomic_write_json(path, {"entries": entries})
    except OSError:
        pass


def consume_pending(data_dir: str, session_id: str) -> str:
    """Concatenated parked fences (each id once), clearing the sidecar."""
    path = pending_path(data_dir, session_id)
    if not path:
        return ""
    entries = _load_entries(path, time.time())
    try:
        os.unlink(path)
    except OSError:
        pass
    fences = [e.get("fence", "") for e in entries if e.get("fence")]
    ctx = "\n\n".join(f for f in fences if isinstance(f, str) and f.strip())
    return ctx


def clear_delivery_state(data_dir: str, session_id: str) -> None:
    """Compaction / session end: "already delivered" is false again."""
    clear(data_dir, session_id)
    path = pending_path(data_dir, session_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _load_compact(path: Optional[str]) -> Dict[str, Any]:
    """Read the compaction sidecar, degrading to the empty stash."""
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
    """Issue #118 (D-2 scope 3): PreCompact snapshots the ledger BEFORE the
    delivery-state clear, so the post-compaction SessionStart can compose a
    query-aware recall from what this session was actually delivered.

    Overwrites any prior stash unconditionally — a second compaction in the
    same session supersedes the first one's leftovers (PreCompact strictly
    precedes PostCompact on every host, so nothing else can be mid-write).
    Fail-open: a snapshot error changes nothing downstream (the clear still
    runs; the compact session-start simply finds no stash and degrades to
    the recency lane).
    """
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
    """Issue #118 (D-2 scope 2): PostCompact merges the host's
    ``compact_summary`` into the sidecar (creating it when PreCompact never
    ran on this host). Read-modify-write is safe: the host fires PostCompact
    strictly after PreCompact, never concurrently. Fail-open."""
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
    """Issue #118: read the compaction stash WITHOUT consuming it — returns
    ``(summary, entries)`` and leaves the file in place, so the caller can
    discard it only after the moment actually completed (PR #190 review
    PRR-002/011: an unlink-before-the-recall-subprocess lost the summary
    and snapshot permanently whenever all recall retries failed).
    Fail-open: absent/unreadable stash returns the empty shape."""
    path = compact_path(data_dir, session_id)
    if not path:
        return (None, [])
    stash = _load_compact(path)
    return (stash.get("summary"), stash.get("entries") or [])


def discard_compact_context(data_dir: str, session_id: str) -> None:
    """Issue #118: drop the compaction stash once its moment completed.
    Fail-open: a missing file or unlink error changes nothing."""
    path = compact_path(data_dir, session_id)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def consume_compact_context(data_dir: str, session_id: str):
    """Read-and-discard convenience (the original #118 consume-once shape):
    :func:`read_compact_context` followed by
    :func:`discard_compact_context`. Hooks should prefer the split pair so
    a failed recall can leave the stash in place."""
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
            except (TypeError, ValueError):
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
    """The subset of rows whose fence bullet ``- [<id>]`` appears in the
    FINAL emitted context (issue #151 review: a char-budget cut can drop
    tail rows AFTER scoring — recording them anyway would suppress rows
    the model never saw). The renderer always embeds ``- [<id>]`` per row,
    so the bullet form is the reliable marker (bare id substring could
    false-positive across prefix ids like r1/r10)."""
    if not text or not rows:
        return rows
    present = [r for r in rows
               if ("- [" + str(r.get("id", "")) + "]") in text]
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
