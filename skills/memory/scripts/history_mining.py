#!/usr/bin/env python3
"""Cold-start transcript mining for zmem (issue #48, PR 3/4 of the
claude-reflect port).

Walks a box's existing Claude Code transcripts (~/.claude/projects/**/*.jsonl)
so store.py's `mine-history` subcommand can produce a single merged candidate
report (corrections + rejections + error_patterns) for an agent to REVIEW
before anything enters the store. Read-only against transcripts AND the store;
under `--queue`, the caller writes the #47 review queue and compact progress
checkpoints in the store data directory.

This module owns DISCOVERY + FOLDERING + DEDUP + QUEUE-SYNTHESIS. It never
imports store.py (avoids a cycle: store.py imports this module). The concrete
per-file extraction is orchestrated by store.py's `mine-history` using the
existing #46 extractors (corrections.extract_user_messages + store's
`_failures_from_transcript`); this module receives the shape lists it and
merges/dedupes/queues them.

Host input surface is Claude Code transcripts only by design (zmem host
matrix). Stdlib-only, Python 3.11+, cross-platform (Windows CI). Untrusted
transcript text is sanitized/truncated by the caller before queue synthesis.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

# Claude Code's transcript root (only host with a CC-shape substrate we read).
DEFAULT_TRANSCRIPT_DIR = "~/.claude/projects"


def _checkpoint_identity(transcript: Path, session: str) -> tuple[str, str, Path]:
    normalized = Path(transcript).resolve().as_posix()
    session_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()[:32]
    path_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return normalized, str(session), Path(
        f"{session_hash}-{path_hash}.json"
    )


def _empty_checkpoint(transcript: Path, session: str) -> dict:
    normalized, session_value, _ = _checkpoint_identity(transcript, session)
    return {
        "session": session_value,
        "transcript": normalized,
        "offset": 0,
        "prefix_sha256": hashlib.sha256(b"").hexdigest(),
        "chunk_id": "0",
    }


def read_suffix_checkpoint(root: Path, transcript: Path, session: str) -> dict:
    """Return the validated incremental-mining checkpoint for one transcript.

    Missing, malformed, mismatched, or negative-offset state is treated as an
    initial read. The returned dict always has the stable key order required by
    the on-disk contract.
    """
    fallback = _empty_checkpoint(transcript, session)
    normalized, session_value, filename = _checkpoint_identity(transcript, session)
    path = Path(root) / "history-checkpoints" / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return fallback
        offset = raw.get("offset")
        if (
            raw.get("session") != session_value
            or raw.get("transcript") != normalized
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(raw.get("prefix_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["prefix_sha256"])
            or not isinstance(raw.get("chunk_id"), str)
        ):
            return fallback
        return {
            "session": session_value,
            "transcript": normalized,
            "offset": offset,
            "prefix_sha256": raw["prefix_sha256"],
            "chunk_id": raw["chunk_id"] or "0",
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return fallback


def _latest_chunk(text: str, initial_chunk: str = "0") -> tuple[str, int]:
    latest = str(initial_chunk or "0")
    start = 0
    position = 0
    for line in text.splitlines(keepends=True):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            obj = None
        chunk = obj.get("chunk_id") if isinstance(obj, dict) else None
        if chunk not in (None, ""):
            chunk = str(chunk)
            if chunk != latest:
                latest = chunk
                start = position
        position += len(line.encode("utf-8"))
    return latest, start


def mine_transcript_suffix(transcript: Path, checkpoint: dict) -> tuple[str, dict]:
    """Read only bytes not safely covered by ``checkpoint``.

    UTF-8 is intentionally strict. Shrink, prefix mutation, and a new top-level
    chunk reset the read boundary; a chunk reset starts at the first record of
    the newest chunk rather than replaying older compacted chunks.
    """
    path = Path(transcript)
    try:
        offset = int(checkpoint.get("offset", 0))
    except (AttributeError, TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    stored_digest = checkpoint.get("prefix_sha256", "") \
        if isinstance(checkpoint, dict) else ""
    stored_chunk = checkpoint.get("chunk_id", "0") \
        if isinstance(checkpoint, dict) else "0"

    # A valid checkpoint is the normal append path. Hash the covered prefix in
    # bounded chunks, retaining only its unfinished final JSONL record and the
    # appended bytes. The stored digest proves the prefix is the same strict
    # UTF-8 content accepted previously, so only the append boundary/suffix
    # needs decoding and JSON scanning on this path.
    total_size = 0
    full_digest = ""
    if path.stat().st_size >= offset:
        prefix_digest = hashlib.sha256()
        transcript_digest = hashlib.sha256()
        # Keep only the unfinished final JSONL record. It is needed when an
        # append completes a record that crossed the checkpoint boundary.
        prefix_tail = bytearray()
        suffix_bytes = bytearray()
        with path.open("rb") as handle:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                transcript_digest.update(block)
                block_start = total_size
                block_end = total_size + len(block)
                if block_start < offset:
                    prefix_end = min(len(block), offset - block_start)
                    prefix = block[:prefix_end]
                    prefix_digest.update(prefix)
                    newline = prefix.rfind(b"\n")
                    if newline >= 0:
                        prefix_tail[:] = prefix[newline + 1:]
                    else:
                        prefix_tail.extend(prefix)
                if block_end > offset:
                    suffix_bytes.extend(block[max(0, offset - block_start):])
                total_size = block_end
        full_digest = transcript_digest.hexdigest()
        checkpoint_valid = prefix_digest.hexdigest() == stored_digest
    else:
        checkpoint_valid = False

    if checkpoint_valid:
        suffix_data = bytes(suffix_bytes)
        scan_data = bytes(prefix_tail) + suffix_data
        scan_text = scan_data.decode("utf-8")
        latest_chunk, chunk_start = _latest_chunk(
            scan_text, initial_chunk=str(stored_chunk or "0"))
        if latest_chunk != str(stored_chunk or "0"):
            # A newly completed chunk record may begin in prefix_tail, so
            # replay that one record when necessary; otherwise retain only the
            # true append bytes.
            suffix = scan_data[chunk_start:].decode("utf-8")
        else:
            suffix = suffix_data.decode("utf-8")
    else:
        # Shrink or prefix mutation invalidates the checkpoint. Reset paths may
        # read the whole transcript because the returned suffix is the full
        # strict-decoded transcript and chunk state must be reconstructed.
        data = path.read_bytes()
        text = data.decode("utf-8")
        latest_chunk, chunk_start = _latest_chunk(text)
        start = 0
        if str(stored_chunk or "0") != latest_chunk:
            start = chunk_start
        # ``_latest_chunk`` reports byte offsets so both paths must slice the
        # original bytes before strict decoding. Slicing ``text`` here would
        # treat the offset as characters and truncate the first new record
        # whenever an older chunk contains multibyte UTF-8.
        suffix = data[start:].decode("utf-8")
        total_size = len(data)
        full_digest = hashlib.sha256(data).hexdigest()

    suffix = suffix.replace("\r\n", "\n").replace("\r", "\n")
    next_state = {
        "session": str(checkpoint.get("session", "")) if isinstance(checkpoint, dict) else "",
        "transcript": str(checkpoint.get("transcript", path.resolve().as_posix()))
        if isinstance(checkpoint, dict) else path.resolve().as_posix(),
        "offset": total_size,
        "prefix_sha256": full_digest,
        "chunk_id": latest_chunk,
    }
    return suffix, next_state


def _acquire_checkpoint_lock(lock: Path, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("history checkpoint lock unavailable")
            time.sleep(0.01)


def _state_matches_transcript(transcript: Path, state: dict) -> bool:
    """Return whether *state* describes the transcript's current bytes.

    A confirmed shrink may move a persisted checkpoint backward. Ordinary
    stale concurrent writers may not: they fail this size+digest comparison
    against the current transcript and remain subject to monotonic progress.
    """

    try:
        expected_size = int(state.get("offset", -1))
        expected_digest = str(state.get("prefix_sha256", ""))
        if expected_size < 0 or Path(transcript).stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with Path(transcript).open("rb") as handle:
            while True:
                block = handle.read(64 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest() == expected_digest
    except (OSError, TypeError, ValueError):
        return False


def write_suffix_checkpoint(
    root: Path, transcript: Path, session: str, state: dict
) -> None:
    """Atomically persist one checkpoint without moving progress backward."""
    normalized, session_value, filename = _checkpoint_identity(transcript, session)
    directory = Path(root) / "history-checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    lock = target.with_suffix(target.suffix + ".lock")
    fd = _acquire_checkpoint_lock(lock)
    try:
        os.close(fd)
        current = read_suffix_checkpoint(root, transcript, session)
        requested = int(state.get("offset", 0))
        if (current["offset"] > requested
                and not _state_matches_transcript(transcript, state)):
            return
        payload = {
            "session": session_value,
            "transcript": normalized,
            "offset": requested,
            "prefix_sha256": str(state.get("prefix_sha256", "")),
            "chunk_id": str(state.get("chunk_id", "0") or "0"),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                   + "\n").encode("utf-8")
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def resolve_transcript_root(transcript_dir: Optional[str] = None) -> Path:
    """Transcript root to scan. `--transcript-dir` wins; else ~/.claude/projects."""
    if transcript_dir:
        return Path(transcript_dir).expanduser()
    return Path(os.path.expanduser(DEFAULT_TRANSCRIPT_DIR))


def encode_project_folder(project_dir=None) -> str:
    """Claude Code's project-folder encoding (ported from claude-reflect
    get_project_folder_name): cwd `/` and `\\` -> `-`, leading `-` prefix.

    e.g. /home/<user>/myapp -> -home-<user>-myapp. The cwd is resolved BEFORE
    encoding so a symlinked/synced cwd normalizes to the prefix CC recorded
    (Windows drive letters and UNC forms survive; separators uniformly -> `-`).
    """
    cwd = Path(project_dir).resolve() if project_dir else Path.cwd().resolve()
    folder_name = str(cwd).replace("\\", "-").replace("/", "-")
    if folder_name.startswith("-"):
        folder_name = folder_name[1:]
    return "-" + folder_name


def _cc_windows_variant(canonical: str):
    """Claude Code's real Windows project-folder form, derived from the faithful
    canonical encoding.

    CC maps a drive colon to `-` AND drops the leading dash on a drive path, so
    cwd ``E:\\ZCode\\zmem`` (canonical ``-E:-ZCode-zmem``) is stored on disk as
    ``E--ZCode-zmem``. Pure string math on the canonical string (never resolved
    through the filesystem) so it is unit-testable on any OS. Returns None when
    the canonical has no drive colon (no Windows variant applied)."""
    if ":" not in canonical:
        return None
    body = canonical[1:] if canonical.startswith("-") else canonical
    return body.replace(":", "-")


def project_folder_candidates(project_dir=None) -> List[str]:
    """Discovery candidate folder names for the current project.

    The encoding is LOSSY: a literal hyphen in the path is indistinguishable
    from the `-` separator, Claude Code preserves underscores verbatim while the
    encoded form uses hyphens (claude-reflect's history scan handles the
    underscore/hyphen ambiguity by "try replacing underscores"), and on Windows
    CC maps a drive colon to `-` and drops the leading dash. So we return the
    canonical encoded name PLUS a targeted alternate that swaps `-`/`_` in the
    TRAILING basename, PLUS (on a drive path) the exact on-disk Windows form.
    Candidates are order-preserved/deduped. We deliberately do NOT fall back to
    a broad substring match across folders — that can silently scan the WRONG
    project. An unresolved current project is handled by the caller as a clean
    'no matching project folder' outcome.
    """
    cwd = Path(project_dir).resolve() if project_dir else Path.cwd().resolve()
    canonical = encode_project_folder(str(cwd))
    candidates = [canonical]
    base = cwd.name
    if base and canonical.endswith("-" + base):
        alt_base = base.replace("-", "_")
        if alt_base != base:
            candidates.append(canonical[: -(len(base) + 1)] + "-" + alt_base)
    wvar = _cc_windows_variant(canonical)
    if wvar is not None:
        candidates.append(wvar)
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def discover_transcripts(
    root,
    *,
    all_projects: bool = False,
    project_dir=None,
    days: Optional[int] = None,
) -> Tuple[List[Tuple[Path, str]], bool]:
    """Discover transcript files under ``root``.

    Returns ``(files, missing)`` where ``files`` is a list of
    ``(Path, project_folder)`` sorted newest-mtime-first, and ``missing`` is
    True when the transcript root does not exist (drives store.py's clean
    non-zero exit on a ZCode/Codex-only box).

    - ``all_projects`` walks every project subfolder (best-effort).
    - otherwise only the encoded current-project folder(s) are scanned.
    - ``days`` filters transcripts to those modified within that many days.
    - ``agent-*.jsonl`` sub-agent transcripts are covered by the ``*.jsonl``
      glob (they end in .jsonl), matching claude-reflect.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return [], True

    if all_projects:
        folders = [d for d in root_path.iterdir() if d.is_dir()]
    else:
        folders = []
        for name in project_folder_candidates(project_dir):
            p = root_path / name
            if p.is_dir():
                folders.append(p)

    now = time.time()
    files: List[Tuple[Path, str]] = []
    for folder in folders:
        for f in sorted(folder.glob("*.jsonl")):
            if days:
                if now - _mtime(f) > days * 86400:
                    continue
            files.append((f, folder.name))
    files.sort(key=lambda t: _mtime(t[0]), reverse=True)
    return files, False


def is_cc_transcript(path) -> bool:
    """True when ``path`` is readable and yields >=1 valid Claude Code JSONL
    record (a dict with a non-empty string ``type`` key). Used to count
    ``scanned`` vs ``skipped``: an unreadable file, an empty file, a
    foreign-schema file, or a malformed file all count as ``skipped``
    (fail-open), but a valid CC transcript counts as ``scanned`` even if it
    yields no candidates. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("type"), str) and obj.get("type"):
                    return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Cross-session correction dedup
# ---------------------------------------------------------------------------
def _norm_dedup(text) -> str:
    """Normalize a correction message for NEAR-IDENTICAL dedup: lowercase,
    collapse whitespace, remove punctuation. Semantic tokens are untouched, so
    "use foo not bar" vs "use foo not baz" remain DISTINCT candidates
    (semantic/paraphrase dedup is closeout Step 4's job, not this command's)."""
    if not text:
        return ""
    s = " ".join(str(text).strip().lower().split())
    s = re.sub(r"[.,!?;:'\"()\[\]]", "", s)
    return s.strip()


def dedupe_corrections(items) -> List[dict]:
    """Collapse near-identical or exact-duplicate correction candidates across
    transcripts. Within a normalized group we keep the MOST RECENT (max
    transcript timestamp) and record an ``occurrences`` count on it; order is
    first-seen preserved. Returns the collapsed list (callers may then apply
    ``--limit``).

    The dedup key is scoped by ``(project_folder, normalized message)`` so
    identical wording in two DIFFERENT projects stays two candidates — matching
    the project-scoped ``dedup_key`` of the #47 queue (issue #48 provenance is
    not lost by collapsing across projects)."""
    best = {}
    order: List[Tuple[str, str]] = []
    for it in items:
        norm = _norm_dedup(it.get("message") or "")
        if not norm:
            continue
        key = (str(it.get("project_folder") or ""), norm)
        cur = best.get(key)
        if cur is None:
            best[key] = dict(it)
            best[key]["occurrences"] = 1
            order.append(key)
            continue
        cur["occurrences"] = cur.get("occurrences", 1) + 1
        # Keep the most recent within the group (ISO timestamps sort lexically).
        if (it.get("timestamp") or "") > (cur.get("timestamp") or ""):
            repl = dict(it)
            repl["occurrences"] = cur["occurrences"]
            best[key] = repl
    return [best[k] for k in order]


# ---------------------------------------------------------------------------
# Mined-candidate -> #47 review queue items (source: history-mine)
# ---------------------------------------------------------------------------
def _error_pattern_message(e) -> str:
    folder = (e or {}).get("project_folder") or "?"
    count = int((e or {}).get("count") or 0)
    etype = (e or {}).get("error_type") or "unknown"
    guideline = ((e or {}).get("suggested_guideline") or "").strip()
    msg = "Repeated tool error '%s' (%dx) in %s." % (etype, count, folder)
    if guideline:
        msg += " Suggested guideline draft: %s" % guideline
    return msg


def build_mined_items(report, *, namespace: str, host: Optional[str] = None) -> List[dict]:
    """Turn a mined candidate report into #47 queue items (source
    'history-mine') for `--queue` mode.

    Corrections + error_patterns are the reviewable candidates (rejections are
    intentionally report-only — they are a #46 report surface, not a queue
    candidate; documented in README/closeout). Each item carries a stable
    ``dedup_key`` so store.py can skip re-appending on a re-run (idempotent
    queueing). Secrets handled per capture mode: 'auto' redacts, 'manual'
    keeps the original wording and flags ``secret_warning`` (reusing the queue's
    single source of truth SECRET_PATTERNS).

    error_pattern items use ``confidence: 0.6`` (the honest SIGNAL_CONFIDENCE
    floor) and carry the distinct ``review_priority`` field — repeated errors
    are review ORDERING, never a zmem confidence (issue #48).
    """
    import correction_queue as _cq

    mode = _cq.normalize_capture_mode()
    items: List[dict] = []
    for c in report.get("corrections", []):
        message = str(c.get("message") or "")
        redacted, red = _cq.redact_secret_like_text(message)
        stored = redacted if (red and mode == "auto") else message
        it = {
            "schema_version": _cq.SCHEMA_VERSION,
            "id": uuid.uuid4().hex,
            "message": stored,
            "type": c.get("type") or "auto",
            "patterns": c.get("patterns") or "",
            "confidence": float(c.get("confidence") or 0.0),
            "sentiment": c.get("sentiment") or "correction",
            "decay_days": int(c.get("decay_days") or 90),
            "timestamp": _cq.now_iso(),
            "session": str(c.get("transcript") or ""),
            "namespace": namespace,
            "host": host or "cli",
            "source": "history-mine",
            "kind": "correction",
            "project_folder": str(c.get("project_folder") or ""),
            "occurrences": int(c.get("occurrences") or 1),
            "dedup_key": "cor|%s|%s" % (str(c.get("project_folder") or ""), _norm_dedup(stored)),
        }
        if red or c.get("secret_warning"):
            it["secret_warning"] = True
        items.append(it)

    for e in report.get("error_patterns", []):
        msg = _error_pattern_message(e)
        redacted, red = _cq.redact_secret_like_text(msg)
        stored = redacted if (red and mode == "auto") else msg
        folder = str(e.get("project_folder") or "")
        # Redact each sample per capture mode too: a secret in a repeated
        # failing command (e.g. a retried auth/setup invocation) must not reach
        # the queue raw. Same policy as the message — redact in 'auto', keep
        # verbatim + flag in 'manual'. Already-redacted text won't re-match, so
        # this is idempotent even when a caller pre-redacted the report.
        samples = []
        samples_secret = False
        for s in e.get("sample_errors") or []:
            s_red, s_count = _cq.redact_secret_like_text(str(s))
            if s_count:
                samples_secret = True
            samples.append(s_red if (s_count and mode == "auto") else str(s))
        it = {
            "schema_version": _cq.SCHEMA_VERSION,
            "id": uuid.uuid4().hex,
            "message": stored,
            "type": "error_pattern",
            "patterns": "",
            "confidence": 0.6,  # honest floor; review_priority carries ordering
            "sentiment": "error",
            "decay_days": 180,
            "timestamp": _cq.now_iso(),
            "session": "",
            "namespace": namespace,
            "host": host or "cli",
            "source": "history-mine",
            "kind": "error_pattern",
            "review_priority": float(e.get("review_priority") or 0.7),
            "error_type": str(e.get("error_type") or ""),
            "count": int(e.get("count") or 0),
            "suggested_guideline": (e.get("suggested_guideline") or "") or "",
            "sample_errors": samples,
            "project_folder": folder,
            "dedup_key": "err|%s|%s" % (folder, str(e.get("error_type") or "")),
        }
        if red or samples_secret or e.get("secret_warning"):
            it["secret_warning"] = True
        items.append(it)

    return items
