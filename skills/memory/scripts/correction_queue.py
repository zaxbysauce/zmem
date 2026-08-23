#!/usr/bin/env python3
"""Live correction-capture queue for zmem (issue #47, PR 2/4 of the
claude-reflect port).

Ported from claude-reflect (https://github.com/BayramAnnakov/claude-reflect),
MIT-licensed, `scripts/lib/reflect_utils.py` (queue item shape + per-project
queue scoping) and `scripts/capture_learning.py` (hook flow). Attribution
retained per the MIT license.

Why a sidecar queue instead of a direct store write:
  - zmem's design invariant is that HOOKS NEVER WRITE TO THE STORE; hooks nudge,
    and the agent writes via the closeout skill. So the UserPromptSubmit capture
    hook appends candidates to a plaintext sidecar queue here, and the `/closeout`
    skill reviews the queue and calls `add` only for what clears the bar.
  - The queue lives under the SAME data dir as the store (host.resolve_store_path()
    .parent / "queue"), namespace-scoped to one file per namespace, so a candidate
    captured during a Claude Code session is reviewable at the next `/closeout` run
    in ANY host operating on the same repo — the same single-brain model as the
    store.

Design rules (zmem invariants):
  - Stdlib-only, Python 3.11+, cross-platform (Windows CI).
  - The queue is a local plaintext file like the store; the write-time secret
    scan is ADVISORY only (never blocks; redacts in `auto`, annotates
    `secret_warning` in `manual`).
  - Every operation is fail-open: a corrupt/missing file loads as [], a write
    failure is swallowed, an unwritable/refused location disables the queue with
    a single stderr line rather than raising.

This module is named `correction_queue` (NOT `queue`) specifically to avoid
shadowing Python's stdlib `queue` module. `SECRET_PATTERNS` lives here as the
single source of truth; store.py aliases it (`from correction_queue import
SECRET_PATTERNS`) so its existing capture-policy helpers never drift from the
queue's redaction.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Secrets (single source of truth — store.py aliases this exact list)
# ---------------------------------------------------------------------------
# Mirrors what store.py's `add`/`corrections` capture path enforces. The queue
# holds plaintext corrections like the store does, so it gets the same advisory
# scan, not a new one.
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

# Capture modes: "auto" redacts before writing; "manual"/"reviewed" keep the
# original wording and set secret_warning so a reviewer sees it. Matches the
# store's CAPTURE_MODES.
CAPTURE_MODES = ("auto", "reviewed", "manual")

# ---------------------------------------------------------------------------
# Queue constants
# ---------------------------------------------------------------------------
# Cap the queue so a runaway detector cannot grow the file unbounded. Oldest
# (by timestamp, chronological FIFO) items roll off first.
MAX_QUEUE_SIZE = 100

# Subdir of the store's data dir that holds the per-namespace queue files.
QUEUE_DIR_NAME = "queue"

# Namespace filename suffix.
QUEUE_EXTENSION = ".json"

# Item schema version, so the consumer (closeout) can branch if the shape ever
# changes without silently mis-reading an old file.
SCHEMA_VERSION = 1

# A queued item is flagged `stale` when it has sat past its decay_days. Stale
# items are never auto-deleted by load (the cap is the de-facto roll-off; the
# closeout `queue-clear --drop-stale` prunes them). Threshold guard: decay_days
# must be a positive number or the item is never stale.
_MIN_DECAY_DAYS = 1


def _env(name: str) -> str:
    """Read an env var at call time (never cache) so tests can monkeypatch and
    hooks can pick up the launcher's env changes."""
    return os.environ.get(name, "")


# ---------------------------------------------------------------------------
# Capture-mode helpers (mirror store.py's, reading the SAME SECRET_PATTERNS)
# ---------------------------------------------------------------------------
def normalize_capture_mode() -> str:
    """Resolve the effective capture mode: ZMEM_CAPTURE_MODE env, else "manual".
    Any unknown value falls back to "manual" (the store's policy)."""
    value = _env("ZMEM_CAPTURE_MODE").strip().lower()
    return value if value in CAPTURE_MODES else "manual"


def redact_secret_like_text(text: str):
    """Replace secret-like tokens in `text` with [REDACTED_SECRET].

    Returns (redacted, count). Mirrors store.py's `_redact_secret_like_text`.
    Advisory only (pattern-driven), never blocks.
    """
    redacted = text or ""
    count = 0
    for pat in SECRET_PATTERNS:
        redacted, changed = pat.subn("[REDACTED_SECRET]", redacted)
        count += changed
    return redacted, count


def now_iso() -> str:
    """ISO-8601 UTC timestamp (matches the store's `.json` timestamp format)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Namespace → filesystem-safe filename encoding
# ---------------------------------------------------------------------------
# Provably collision-free, reversible, Windows-safe encoding of a namespace
# string ("project:github.com/foo/bar") into a single queue filename.
#
#   '_'  -> '__'          (escape the escape char: a single leading '_' in the
#                          output always begins an escape -> no ambiguity)
#   ':'  -> '_c'
#   '/'  -> '_s'
#   '\\' -> '_b'
#   any other char not in [A-Za-z0-9.-] -> '_x' + 2-digit lowercase hex PER
#                                          UTF-8 BYTE of the code point (1-4
#                                          bytes), e.g. 'é' (U+00E9) ->
#                                          '_xc3_xa9'. Covers Windows-invalid
#                                          < > : " | ? * and control chars, and
#                                          stays collision-free for EVERY
#                                          Unicode code point because the full
#                                          UTF-8 encoding (not just the low
#                                          byte) is represented.
#   [A-Za-z0-9.-] -> literal             ('.' is valid mid-filename on Windows)
#
# Example: 'project:github.com/foo/bar' -> 'project_cgithub.com_sfoo_sbar'.
# Every output char belongs to a unique class (literal / '__' / '_c' / '_s' /
# '_b' / '_x'+2hex for one UTF-8 byte), so two distinct inputs can never map to
# the same filename and the inverse is deterministic.
_SAFE_CH = re.compile(r"[A-Za-z0-9.-]")
_HEX = "0123456789abcdef"


def encode_namespace(namespace: str) -> str:
    out: List[str] = []
    for ch in str(namespace or ""):
        if ch == "_":
            out.append("__")
        elif ch == ":":
            out.append("_c")
        elif ch == "/":
            out.append("_s")
        elif ch == "\\":
            out.append("_b")
        elif _SAFE_CH.match(ch):
            out.append(ch)
        else:
            # One token per UTF-8 byte so the full code point is captured:
            # encoding only the low 8 bits of ord(ch) would let two DIFFERENT
            # chars sharing a low byte (e.g. U+00C0 'À' and U+01C0 'ǀ') collide.
            # 'replace' keeps this total/no-raise even for an unpaired surrogate
            # in a namespace string (a pathological input -> lossy, never a crash).
            for b in ch.encode("utf-8", "replace"):
                out.append("_x" + _HEX[(b >> 4) & 0xF] + _HEX[b & 0xF])
    return "".join(out)


def decode_namespace(filename: str) -> str:
    """Inverse of encode_namespace (for diagnostics). Never raises; returns the
    input unchanged if the encoded form is malformed."""
    f = filename
    if f.endswith(QUEUE_EXTENSION):
        f = f[: -len(QUEUE_EXTENSION)]
    out: List[str] = []
    i = 0
    n = len(f)
    while i < n:
        ch = f[i]
        if ch == "_" and i + 1 < n:
            nxt = f[i + 1]
            if nxt == "_":
                out.append("_"); i += 2; continue
            if nxt in ("c", "s", "b"):
                out.append({"_c": ":", "_s": "/", "_b": "\\"}["_" + nxt]); i += 2; continue
            if nxt == "x":
                # One UTF-8 byte per '_xNN' token; gather the contiguous byte
                # run and decode it back to the original code point(s).
                j = i
                raw = bytearray()
                while j + 3 < n and f[j] == "_" and f[j + 1] == "x":
                    hb = f[j + 2:j + 4]
                    if len(hb) != 2 or not all(_h in _HEX for _h in hb):
                        break
                    raw.append(int(hb, 16))
                    j += 4
                if raw:
                    try:
                        out.append(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        # Fail-soft: never raise on a malformed encoded name.
                        out.append("".join(chr(b) for b in raw))
                    i = j
                    continue
        out.append(ch); i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Queue location + I/O
# ---------------------------------------------------------------------------
def _resolve_data_dir() -> Path:
    """Resolve the data dir the queue lives under. Prefer the resolved store's
    parent (so the queue follows ZMEM_STORE/ZMEM_DATA/legacy chain identically
    to the store), falling back to ~/.zmem."""
    try:
        import host as _host
        return _host.resolve_store_path().parent
    except Exception:
        # Fail-open location: default box-wide data dir.
        return Path(os.path.expanduser("~")) / ".zmem"


def resolve_queue_dir() -> Path:
    """The directory holding per-namespace queue files. Same data dir as the
    store -> cross-host single-brain queue."""
    return _resolve_data_dir() / QUEUE_DIR_NAME


def queue_path_for(namespace: str, queue_dir: Optional[Path] = None) -> Path:
    base = Path(queue_dir) if queue_dir is not None else resolve_queue_dir()
    return base / (encode_namespace(namespace) + QUEUE_EXTENSION)


def _assert_local_fs(queue_dir: Path) -> bool:
    """Best-effort local-FS guard on the queue dir, inheriting the store's
    posture (refuse OneDrive/UNC). Returns True if the queue may proceed, False
    if it must be disabled (a refused location would risk a silently-teared
    rename). Never raises."""
    try:
        import host as _host
        _host.assert_local_fs(queue_dir)
        return True
    except Exception:
        return False


def _harden(path: Path) -> None:
    """Best-effort owner-only permission hardening (chmod 0600/0700 or Windows
    ACL), mirroring the store's `set_owner_only_perms`. Never raises. The queue
    can hold verbatim (possibly secret-bearing) corrections, so it gets the same
    defense-in-depth as the store even though an unwritable location can never
    block capture (the write still happens; only the permission layer is best-
    effort)."""
    try:
        import host as _host
        _host.set_owner_only_perms(path)
    except Exception:
        pass


def _load_raw(path: Path) -> List[dict]:
    """Read + parse a queue file. Fail-open: missing/corrupt/non-list -> []."""
    try:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def _now_epoch() -> float:
    return time.time()


def _parse_iso_ts(ts: str) -> Optional[float]:
    """Parse an ISO-8601 UTC timestamp produced by now_iso(). None on any
    malformed value (a bad timestamp is never stale — fail toward showing)."""
    try:
        import datetime
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        return dt.timestamp()
    except Exception:
        return None


def _flag_stale(items: List[dict], now: float | None = None) -> List[dict]:
    """Annotate each item with `stale: bool` (decay_days elapsed since
    timestamp). Never deletes; each call recomputes against the current clock."""
    if now is None:
        now = _now_epoch()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            decay = float(it.get("decay_days") or 0)
        except (TypeError, ValueError):
            decay = 0
        ts = it.get("timestamp")
        parsed = _parse_iso_ts(ts) if isinstance(ts, str) else None
        stale = False
        if parsed is not None and decay >= _MIN_DECAY_DAYS:
            stale = (now - parsed) > decay * 86400
        it["stale"] = bool(stale)
    return items


def load_queue(namespace: str, queue_dir: Optional[Path] = None) -> List[dict]:
    """Load a namespace's queue items (stale-flagged). Fail-open: missing/corrupt
    -> []."""
    path = queue_path_for(namespace, queue_dir)
    items = _load_raw(path)
    # Defensive: only dict items are valid queue entries.
    items = [it for it in items if isinstance(it, dict)]
    return _flag_stale(items)


def _atomic_write(path: Path, items: List[dict]) -> bool:
    """Write `items` atomically (temp file + os.replace). Returns True on
    success; False (fail-open) on any error. os.replace is atomic on Windows
    for a same-volume rename.

    Inherits the store's local-FS guard: a queue under OneDrive/UNC risks a
    silently-teared rename, so when the location is refused the write is
    DISABLED (returns False; the queue stays untouched) rather than risking a
    torn sidecar. The rejection cost is a lost candidate, never a corrupt store.
    """
    if not _assert_local_fs(path.parent):
        return False
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Harden the queue dir on EVERY write (not just when newly created): a
        # dir created by an earlier/unhardened version would otherwise stay
        # outside owner-only protection. Idempotent and never raises.
        _harden(parent)
        tmp = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f)
                f.flush()
                os.fsync(f.fileno())
            # Harden the temp BEFORE os.replace so no reader can catch verbatim
            # content in the rename-to-final window; retain final-path hardening
            # afterward for defense in depth (perms survive the atomic rename).
            _harden(tmp)
            os.replace(str(tmp), str(path))
            _harden(path)
            return True
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
    except Exception:
        return False


def append_queue(namespace: str, item: dict, queue_dir: Optional[Path] = None) -> bool:
    """Append a queue item, capping the queue at MAX_QUEUE_SIZE by dropping the
    OLDEST (by timestamp). Atomic: temp + os.replace.

    NOTE (concurrency): load-modify-write means two concurrent writers can lose
    one item — one clobbers the other's append. This is ACCEPTED for the fail-open
    sidecar (a lost candidate, never a corrupt file: os.replace is atomic). The
    closeout/capture paths do not need serialization; if a future caller does,
    add a per-namespace O_EXCL lock (host.acquire_lock pattern).
    """
    items = load_queue(namespace, queue_dir)
    items.append(item)
    if len(items) > MAX_QUEUE_SIZE:
        # Drop oldest by timestamp (chronological FIFO). Items without a valid
        # timestamp sort to the front (oldest).
        def _ts_key(it: dict):
            ts = it.get("timestamp")
            parsed = _parse_iso_ts(ts) if isinstance(ts, str) else None
            return parsed if parsed is not None else 0.0
        sorted_items = sorted(items, key=_ts_key)
        items = sorted_items[-MAX_QUEUE_SIZE:]
    return _atomic_write(queue_path_for(namespace, queue_dir), items)


def clear_queue(
    namespace: str,
    ids: Optional[list] = None,
    drop_stale: bool = False,
    queue_dir: Optional[Path] = None,
) -> int:
    """Remove items from a namespace's queue. Returns the number removed.
    - ids set  -> remove items whose `id` is in ids.
    - drop_stale True -> also remove stale items with confidence < 0.6
      (low-confidence manual candidates past their decay window).
    - ids None and not drop_stale -> clear the whole queue (removes the file).
    Atomic + fail-open (a write failure leaves the file untouched)."""
    if ids is None and not drop_stale:
        path = queue_path_for(namespace, queue_dir)
        # Count what is being removed for a symmetric API (whole-queue clear
        # returns the number of items cleared, like the id/stale path).
        before = len([it for it in _load_raw(path) if isinstance(it, dict)])
        try:
            if path.exists():
                path.unlink()
        except OSError:
            # Fail-open: report 0 removed (nothing was actually deleted) so the
            # CLI cannot print a fabricated "cleared N" when the unlink failed.
            return 0
        return before

    items = load_queue(namespace, queue_dir)
    id_set = set(ids or [])
    kept = []
    removed = 0
    for it in items:
        drop_by_id = id_set and it.get("id") in id_set
        drop_by_stale = (
            drop_stale
            and it.get("stale")
            and (it.get("confidence") if isinstance(it.get("confidence"), (int, float)) else 0) < 0.6
        )
        if drop_by_id or drop_by_stale:
            removed += 1
        else:
            kept.append(it)
    if removed and not _atomic_write(queue_path_for(namespace, queue_dir), kept):
        # Fail-open: if the write failed, report 0 removed (nothing persisted).
        return 0
    return removed


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------
def make_item(
    *,
    message: str,
    type_: str,
    patterns: str,
    confidence: float,
    sentiment: str,
    decay_days: int,
    session: str,
    namespace: str,
    host: str,
    capture_mode: Optional[str] = None,
) -> dict:
    """Build a queue item for a captured correction.

    Shape (superset of store.py cmd_corrections' item, so the closeout rubric is
    identical): {schema_version, id, message, type, patterns, confidence,
    sentiment, decay_days, timestamp(ISO-8601 UTC), session, namespace, host,
    source:"live-capture"}.

    Secret handling: `secret_warning` is derived from the ORIGINAL message. In
    `auto` mode the stored `message` is the redacted form (and secret_warning is
    still True, so a reader knows the original contained a secret — matching
    cmd_corrections). In `manual`/`reviewed` mode the ORIGINAL message is kept so
    a reviewer can read it, with secret_warning=True.
    """
    mode = capture_mode if capture_mode else normalize_capture_mode()
    redacted, redactions = redact_secret_like_text(message)
    item = {
        "schema_version": SCHEMA_VERSION,
        "id": uuid.uuid4().hex,
        "message": redacted if (redactions and mode == "auto") else message,
        "type": type_,
        "patterns": patterns,
        "confidence": confidence,
        "sentiment": sentiment,
        "decay_days": decay_days,
        "timestamp": now_iso(),
        "session": session,
        "namespace": namespace,
        "host": host,
        "source": "live-capture",
    }
    if redactions:
        item["secret_warning"] = True
    return item
