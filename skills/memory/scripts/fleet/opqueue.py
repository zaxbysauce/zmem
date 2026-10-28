"""Durable typed operation queue — the client half (issue #288, T PR 9).

Append-only JSONL under ``<store parent>/queue/ops-<principal>.jsonl``:
entries are redacted BEFORE they touch disk (every string arg is run
through ``redact_training_text`` — secrets AND emails AND paths), ASCII-
escaped on write and split on ``\\n`` on read (a U+2028/U+2029 in content
can never tear the journal the way ``splitlines`` would), bounded by a
PROJECTED size check and an age bound with LOUD refusal on overflow
(``QueueOverflowError`` — the caller relies on the write landing; never a
silent drop), and carry a monotonic per-principal ``client_seq``.
Enqueue and the read-modify-write rewrites hold an inter-process lock so
a concurrent broker flush cannot destroy a concurrent hook append, and a
torn tail (crash mid-append) is tolerated on read, never fused by the
next append, and preserved across rewrites. ``pending``/``mark_receipts``
give the broker read-your-writes; ``flush`` sends the pending batch to
the hub's ``apply_ops`` tool in 256-op chunks (the hub's batch limit)
under the caller's REMAINING deadline.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

QUEUE_DIR_NAME = "queue"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_AGE_DAYS = 7.0
_PENDING_LIMIT = 5
_LINE_CAP = 200
# The hub refuses any batch over 256 ops (mcp_server apply_ops) — the
# flush chunks to that limit so a large backlog drains instead of wedging.
_FLUSH_CHUNK = 256

# Bootstrap sibling imports (host.py, redaction.py) the same way broker.py
# does, so `import fleet.opqueue` works standalone.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


class QueueOverflowError(RuntimeError):
    """The queue is at its bound — the enqueue is REFUSED, never dropped."""


def _source(env=None):
    return os.environ if env is None else env


def queue_dir(env=None) -> Path:
    # Self-healing path bootstrap: a caller may have removed SCRIPTS_DIR
    # from sys.path after importing this module (the loader discipline in
    # the t-series tests does exactly that), and host.py lives there.
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import host  # noqa: PLC0415 - lazy: resolves the pinned store parent
    source = _source(env)
    # Sync ONLY the vars host.resolve_store_path actually reads, and CLEAR
    # the lower-precedence ones the caller's env leaves unset — syncing
    # ZMEM_HOME (which host never reads) or leaving ambient
    # CLAUDE_PLUGIN_DATA/ZCODE_PLUGIN_DATA in place makes a pinned env
    # resolve into the real user profile instead of the sandbox.
    keys = ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
            "ZCODE_PLUGIN_DATA")
    saved = None
    if env is not None:
        saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            if source.get(k) is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = source[k]
    try:
        base = Path(host.resolve_store_path()).expanduser().parent
    finally:
        if saved is not None:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return base / QUEUE_DIR_NAME


def queue_path(principal: str, env=None) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in principal)
    return queue_dir(env) / f"ops-{safe or 'local'}.jsonl"


def _max_bytes(env=None) -> int:
    raw = _source(env).get("ZMEM_OPQUEUE_MAX_BYTES", "").strip()
    try:
        value = int(raw)
        return value if value > 0 else _DEFAULT_MAX_BYTES
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _max_age_days(env=None) -> float:
    raw = _source(env).get("ZMEM_OPQUEUE_MAX_AGE_DAYS", "").strip()
    try:
        value = float(raw)
        return value if value > 0 else _DEFAULT_MAX_AGE_DAYS
    except ValueError:
        return _DEFAULT_MAX_AGE_DAYS


def _harden(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


@contextlib.contextmanager
def _queue_lock(path: Path):
    """An inter-process advisory lock over one queue file's read-modify-
    write window (enqueue's read+append and the whole-file rewrites). The
    hook and the broker are separate processes racing on the SAME file; a
    plain read-then-replace silently destroys a concurrent append. The
    lock file is per-queue and never deleted (delete-on-close would race
    with waiters)."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_file(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Read the queue as (entries, torn_tail): well-formed lines parse as
    entries; a torn line (crash mid-append) is skipped and its RAW text
    carried as the tail so a rewrite preserves it — but the walk RESYNCS
    on the next newline, so one torn record never masks (and a rewrite
    never deletes) the intact entries after it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], ""
    entries: list[dict[str, Any]] = []
    torn_parts: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            torn_parts.append(line)
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries, "\n".join(torn_parts)


def _read_entries(path: Path) -> list[dict[str, Any]]:
    return _read_file(path)[0]


def _write_entries(path: Path, entries: list[dict[str, Any]],
                   torn_tail: str = "") -> None:
    tmp = path.with_name(path.name + ".tmp." + uuid.uuid4().hex[:8])
    body = "".join(json.dumps(e, ensure_ascii=True) + "\n" for e in entries)
    if torn_tail:
        body += ("" if torn_tail.startswith("\n") else "\n") + torn_tail
    tmp.write_text(body, encoding="utf-8")
    _harden(tmp)
    os.replace(tmp, path)
    _harden(path)


def _reap_expired(path: Path, max_age_days: float) -> bool:
    """Drop entries older than the age bound (atomic rewrite). True if the
    file changed. The CALLER must hold the queue lock (enqueue is the only
    caller and holds it; re-entering msvcrt.locking on Windows deadlocks)."""
    entries, torn = _read_file(path)
    if not entries:
        return False
    cutoff = time.time() - max_age_days * 86400.0
    kept = []
    for entry in entries:
        try:
            ts = float(entry.get("enqueued_at", 0))
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= cutoff:
            kept.append(entry)
    if len(kept) == len(entries):
        return False
    _write_entries(path, kept, torn)
    return True


def _redact_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Redact EVERY string arg before disk (the module contract says the
    ENTRIES are redacted, not two privileged keys — a secret in tags or
    source_ref would otherwise sit on disk for up to the retention
    window)."""
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    from redaction import redact_training_text
    out = {}
    for key, value in args.items():
        if isinstance(value, str):
            redacted, _count = redact_training_text(value)
            out[key] = redacted
        else:
            out[key] = value
    return out


def _dedup_live(queue_path, dedup_key):
    """True when a live queue entry already carries this dedup key
    (issue #374 capture replay suppression)."""
    if not dedup_key:
        return False
    try:
        with open(queue_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or dedup_key not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("dedup_key") == dedup_key:
                    return True
    except OSError:
        pass
    return False


def enqueue(principal: str, op: str, args: Mapping[str, Any],
            depends_on: Optional[str] = None, client_seq: Optional[int] = None,
            env=None, dedup_key: Optional[str] = None) -> dict[str, Any]:
    """Append one redacted op entry; returns the entry as queued.

    ``dedup_key`` (issue #374 capture replay suppression): when a live
    entry already carries the same key the enqueue is a no-op that returns
    the existing entry."""
    path = queue_path(principal, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A hardened directory must keep the owner +x bit: 0o600 on a POSIX
    # directory removes search permission, so even the owner cannot stat
    # inside it and the next enqueue dies with EACCES at path.exists().
    _harden(path.parent, 0o700)

    with _queue_lock(path):
        if dedup_key and _dedup_live(path, dedup_key):
            for e in _read_entries(path):
                if isinstance(e, dict) and e.get("dedup_key") == dedup_key:
                    return e
        entries = _read_entries(path)
        seqs = [int(e.get("client_seq", 0)) for e in entries
                if isinstance(e.get("client_seq"), int)]
        next_seq = (seqs[-1] + 1) if seqs else 1
        if client_seq is None:
            client_seq = next_seq
        if client_seq <= (seqs[-1] if seqs else 0):
            raise ValueError(
                f"client_seq {client_seq} is not monotonic "
                f"(last is {seqs[-1] if seqs else 0})")

        _reap_expired(path, _max_age_days(env))
        entry = {
            "op_id": uuid.uuid4().hex,
            "op": op,
            "args": _redact_args(args),
            "depends_on": depends_on,
            "client_seq": client_seq,
            "status": "pending",
            "enqueued_at": time.time(),
        }
        if dedup_key:
            entry["dedup_key"] = dedup_key
        size_before = path.stat().st_size if path.exists() else 0
        # PROJECTED bound: refuse when THIS entry would carry the file past
        # its cap (a check-before-append can be overshot by the entry's own
        # size — the docstring promises the bound, so enforce it).
        entry_bytes = len(json.dumps(entry, ensure_ascii=True)
                          .encode("utf-8")) + 1
        if size_before + entry_bytes > _max_bytes(env):
            raise QueueOverflowError(
                f"op queue {path.name} is at the {_max_bytes(env)}-byte "
                "bound; enqueue refused (never silently dropped)")
        with path.open("a", encoding="utf-8") as handle:
            # Torn-tail fusion guard: a crash mid-append can leave the file
            # without its trailing newline — appending would fuse the new
            # record onto the fragment and lose BOTH.
            if size_before > 0:
                with path.open("rb") as probe:
                    probe.seek(-1, os.SEEK_END)
                    if probe.read(1) != b"\n":
                        handle.write("\n")
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _harden(path)
    return entry


def pending(principal: str, env=None) -> list[dict[str, Any]]:
    return [e for e in _read_entries(queue_path(principal, env))
            if e.get("status") == "pending"]


def mark_receipts(principal: str, receipts: list[Mapping[str, Any]],
                  env=None) -> None:
    """Update matching entries' status/receipt by op_id (locked atomic
    rewrite; a torn tail is preserved, never deleted)."""
    path = queue_path(principal, env)
    with _queue_lock(path):
        entries, torn = _read_file(path)
        by_op = {r.get("op_id"): r for r in receipts
                 if isinstance(r, Mapping)}
        changed = False
        for entry in entries:
            receipt = by_op.get(entry.get("op_id"))
            if receipt is not None and entry.get("status") == "pending":
                entry["status"] = receipt.get("status", "committed")
                entry["receipt"] = {k: v for k, v in receipt.items()
                                    if k != "op_id"}
                changed = True
        if not changed:
            return
        _write_entries(path, entries, torn)


def _collapse(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", str(text)).strip()[:_LINE_CAP]


def render_pending(overlay_text: str, principal: str, env=None) -> str:
    """Append bounded ``[PENDING]`` lines for the principal's pending ops."""
    rows = pending(principal, env)[:_PENDING_LIMIT]
    if not rows:
        return overlay_text
    lines = [overlay_text] if overlay_text else []
    lines.append("[PENDING] queued ops not yet confirmed by the hub:")
    for row in rows:
        args = row.get("args") or {}
        head = args.get("content") or args.get("reason") or ""
        lines.append(f"[PENDING] {row.get('op', 'op')}: {_collapse(head)}")
    return "\n".join(lines)


def flush(principal: str, http, env=None,
          deadline_ms: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Send the pending batch to the hub apply_ops tool in 256-op chunks
    under the caller's REMAINING deadline; mark receipts per chunk.

    The hub request carries NO principal: the authenticated request
    context (the machine's token) IS the identity — a synthetic client-
    chosen id like ``local`` is unroutable at the hub in every auth mode
    and deadlocked the whole queue (review B1). Returns the LAST hub
    result, or None when nothing was pending."""
    rows = pending(principal, env)
    if not rows:
        return None
    result: Optional[dict[str, Any]] = None
    for start in range(0, len(rows), _FLUSH_CHUNK):
        chunk = rows[start:start + _FLUSH_CHUNK]
        ops = [{k: row.get(k) for k in
                ("op_id", "op", "args", "depends_on", "client_seq")}
               for row in chunk]
        result = http.apply_ops(ops, principal="",
                                deadline_ms=deadline_ms)
        if isinstance(result, dict) and result.get("ok") is True:
            mark_receipts(principal, result.get("receipts") or [], env)
        else:
            break
    return result
