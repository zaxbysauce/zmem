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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
