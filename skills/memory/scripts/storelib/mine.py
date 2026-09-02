from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import glob
from datetime import datetime, timezone
from pathlib import Path

try:
    from corrections import detect_patterns as _detect_patterns
    from corrections import extract_user_messages as _extract_user_messages
    from corrections import classify_error_type as _classify_error_type
    from corrections import aggregate_errors as _aggregate_errors
    from corrections import SAMPLE_EXTRACT_LIMIT as _SAMPLE_EXTRACT_LIMIT
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from corrections import detect_patterns as _detect_patterns  # type: ignore
    from corrections import extract_user_messages as _extract_user_messages  # type: ignore
    from corrections import classify_error_type as _classify_error_type  # type: ignore
    from corrections import aggregate_errors as _aggregate_errors  # type: ignore
    from corrections import SAMPLE_EXTRACT_LIMIT as _SAMPLE_EXTRACT_LIMIT  # type: ignore
from storelib.schema import _host
from storelib.write import _normalize_capture_mode, redact_text

def _collapse_line_breaks(text) -> str:
    """Collapse every CR/LF (and Unicode line separator) in `text` to a
    single space.

    The shared fence-integrity primitive: with no newlines, untrusted text can
    never start its own line, so it can never form a fence-close, a markdown
    heading, a list bullet, or a line-oriented directive of any kind. Used by
    _sanitize_error_text (hook output) and _sanitize_pack_content (export-pack
    bullets) — one primitive, two consumers, so the guarantee cannot drift.

    Also collapses U+2028 (LINE SEPARATOR), U+2029 (PARAGRAPH SEPARATOR), and
    U+0085 (NEXT LINE) -- these are treated as line breaks by str.splitlines()
    and some renderers even though they are not \\r/\\n, so a memory row
    carrying one of them could otherwise still open its own visual "line"
    inside a pack bullet.
    """
    if not text:
        return ""
    return (str(text).replace("\r", " ").replace("\n", " ")
            .replace("\u2028", " ").replace("\u2029", " ").replace("\u0085", " "))

def _sanitize_error_text(text, limit: int = 200) -> str:
    """Make an untrusted tool-error string safe to embed in a fenced block:
    collapse CR/newlines to spaces (fence-integrity), then truncate. Preserves
    the error's characters otherwise so it stays diagnostically useful."""
    if not text:
        return ""
    return _collapse_line_breaks(text)[:limit].strip()

def _sanitize_pack_content(text) -> str:
    """Make a stored memory field safe to render as one export-pack bullet.

    Memory content is UNTRUSTED for this purpose: a Tier 3 sync file is
    remote-authored, and the pack it feeds is read verbatim by other agents
    as instructions-adjacent context. Without this, one row could close the
    generated markdown's structure and inject its own — a prompt-injection
    surface, not just a formatting bug.

    Three neutralizations, no truncation (unlike _sanitize_error_text: a pack
    bullet must render the whole memory or the pack silently lies):
      - CR/LF -> spaces, via the shared _collapse_line_breaks primitive. This
        is the load-bearing one: it alone stops a row from emitting its own
        '## heading', '- bullet', or leading-'#' line.
      - ``` -> ''' so a row cannot open/close a code fence a consumer wrapped
        the pack in.
      - '<!--' / '-->' spaced apart, so a row cannot close the pack's own
        auto-generated HTML comment header or comment out the rest of it.
    """
    s = _collapse_line_breaks(text)
    s = s.replace("```", "'''")
    s = s.replace("<!--", "<!- -").replace("-->", "-- >")
    return s.strip()

def _sanitize_tool_name(name, limit: int = 100) -> str:
    """Defense-in-depth (Phase 8): strip CR/newlines from a tool name before it
    is interpolated into a fenced block by reflect.sh/subagent-reflect.sh. Not
    currently exploitable — tool names come from the harness's own tool_use
    blocks / tool_usage rows, not from untrusted tool output — but a newline
    here would let a forged fence-close ('\\n```') slip past the same
    fence-integrity guarantee _sanitize_error_text gives the error text."""
    if not name:
        return "?"
    s = (str(name).replace("\r", " ")
         .replace("\n", " ")
         .replace("\u2028", " ")   # line separator
         .replace("\u2029", " ")   # paragraph separator
         .strip())
    return s[:limit] or "?"

def _is_rejection_text(text) -> bool:
    """True when tool-result text is a Claude Code user rejection of a tool.

    A rejected tool_call in CC records a tool_result with is_error:true whose
    text contains the harness marker ``The user doesn't want to proceed``.
    Case-insensitive match (issue #46). This exact CC-harness marker is the
    ONLY thing that routes a record to ``rejections`` — an unrecognized schema
    simply never contains it, so we fail open (no false rejections).
    """
    return bool(text) and "the user doesn't want to proceed" in str(text).lower()

def _rejection_reason(text) -> str:
    """Extract the user's stated reason from a CC rejection text.

    The reason appears after the ``user said:`` marker. CC localizes the marker
    — both ``user said:`` and ``the user said:`` occur across transcript forms
    (the content-block form prefixes "the ", the sibling toolUseResult form
    does not). Matching ``user said:`` covers both, since the longer form
    contains it. The reason may span multiple lines; we join all non-empty lines
    after the marker with single spaces and strip the marker itself. Returns
    ``""`` when there is no marker (a rejection without a stated reason).
    """
    s = str(text or "")
    lower = s.lower()
    marker = "user said:"
    idx = lower.find(marker)
    if idx < 0:
        return ""
    after = s[idx + len(marker):]
    lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
    return " ".join(lines)

def _result_text(content) -> str:
    """Extract text from a tool_result block's `content`, which CC emits as
    either a plain string or a list of {type:"text", text:"..."} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(p for p in parts if p)
    return ""

def _failures_from_transcript(path: str):
    """Scan a Claude Code transcript JSONL for failed tool calls and user
    rejections. Returns ``(details, rejections)`` where each is a list of dicts
    (details: one {tool, error} per distinct failed tool_use_id; rejections: one
    {tool, reason} per distinct rejected tool_use_id). Fail-open: returns
    ([], []) on any read/parse error. Never raises.

    A user rejection (CC: is_error:true with ``The user doesn't want to
    proceed``) is split OUT of ``details`` (where it previously counted as a
    generic failure) and its stated reason is captured instead (issue #46).
    Rejections and genuine failures are mutually exclusive by construction: each
    tool_use_id is classified once (rejection branch first, consuming the key),
    so a record is never both.

    ``details`` are returned NEWEST-first (reversed from the chronological file
    order) to match the db substrate, which returns them ``ORDER BY
    completed_at DESC`` — so the reflect hooks' "showing most recent K of N" on
    ``details[:K]`` is truthful on both substrates. ``rejections`` are kept
    CHRONOLOGICAL (oldest-first) because ``render_rejection_section`` keeps the
    chronological tail (``rejections[-K:]``) as the most recent."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw_lines = [ln for ln in f if ln.strip()]
    except OSError:
        return [], []

    records = []
    for ln in raw_lines:
        try:
            records.append(json.loads(ln))
        except Exception:
            continue  # skip malformed lines, keep scanning

    # Pass 1: map tool_use_id -> tool name from assistant tool_use blocks.
    tool_names = {}
    for o in records:
        msg = o.get("message") if isinstance(o, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id")
                    # Only real string ids belong in the name map; a non-string
                    # id (malformed/foreign record) must not crash the dict
                    # write — it just gets no name (classification falls back to
                    # "?"), consistent with the fail-open contract.
                    if isinstance(tid, str) and tid:
                        tool_names[tid] = b.get("name") or "?"

    # Pass 2: collect failed tool_result blocks (deduped by tool_use_id so the
    # is_error flag and the sibling toolUseResult "Error…" string on the same
    # record never double-count one failure), routing user rejections to a
    # separate list.
    details = []
    rejections = []
    seen = set()
    anon = 0
    for o in records:
        if not isinstance(o, dict):
            continue
        msg = o.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        tur = o.get("toolUseResult")
        tur_is_err = isinstance(tur, str) and tur.strip().lower().startswith("error")
        tur_is_rejection = isinstance(tur, str) and _is_rejection_text(tur)
        classified_record = False
        block_tids = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                is_err = b.get("is_error") is True
                block_text = _result_text(b.get("content"))
                effective = block_text
                if not effective and isinstance(tur, str):
                    effective = tur
                tid = b.get("tool_use_id")
                # A non-string tool_use_id (a malformed/foreign record) must not
                # crash the dedup below; keep only real string ids so key-hash /
                # tool_names lookups stay safe (never-raises fail-open).
                tid = tid if isinstance(tid, str) and tid else None
                if tid:
                    block_tids.append(tid)
                # A block is worth classifying if it is flagged as an error OR
                # its effective text is itself a rejection (covers the sibling
                # toolUseResult fallback for empty block content).
                if not (is_err or tur_is_err or (effective and _is_rejection_text(effective))):
                    continue
                key = tid if tid else ("anon:%d" % anon)
                if key in seen:
                    continue
                seen.add(key)
                if not tid:
                    anon += 1
                classified_record = True
                if _is_rejection_text(effective):
                    rejections.append({
                        "tool": _sanitize_tool_name(tool_names.get(tid, "?")),
                        "reason": _sanitize_error_text(_rejection_reason(effective)),
                    })
                else:
                    details.append({
                        "tool": _sanitize_tool_name(tool_names.get(tid, "?")),
                        "error": _sanitize_error_text(effective),
                    })
        # Pure sibling-string form: the top-level toolUseResult is itself a
        # rejection but no tool_result block classified this record (e.g. the
        # record carried no content array, or its block had no error/rejection
        # signal of its own). Extends (does not regress) the existing
        # toolUseResult path — an unrecognized non-rejection, non-"error…" string
        # remains ignored exactly as today. Reuse the first block's tool_use_id
        # (if any) so the tool name is not needlessly dropped, and honour the
        # `seen` dedup so a sibling on an already-classified id is not double
        # counted.
        if not classified_record and tur_is_rejection:
            tid = block_tids[0] if block_tids else None
            key = tid if tid else ("anon:%d" % anon)
            if key in seen:
                continue
            seen.add(key)
            if not tid:
                anon += 1
            rejections.append({
                "tool": _sanitize_tool_name(tool_names.get(tid, "?")),
                "reason": _sanitize_error_text(_rejection_reason(tur)),
            })
    return details[::-1], rejections

def _failures_from_db(db_path: str, session_id: str):
    """Detect failed tool calls for a session from the ZCode episodic db.sqlite.
    Returns (count, details). The load-bearing detection uses ONLY the columns
    the original reflect query used (session_id, read_only, status, exit_code);
    enrichment columns (error_message, error_type, retry_count, destructive) are
    read in a SEPARATE try/except so a schema drift degrades to bare counts but
    never disables detection.

    A missing path/session is a legitimate "nothing to check" → (0, []). A
    genuine substrate error (corrupt/locked db, schema drift on the count
    query) PROPAGATES so ``cmd_failures`` can distinguish "could not check"
    from "0 failures found" (#36 M7) — it no longer silently swallows every
    error into a misleading (0, [])."""
    if not session_id or not db_path or not os.path.isfile(db_path):
        return 0, []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT count(*) FROM tool_usage
            WHERE session_id = ?
              AND COALESCE(read_only, 0) = 0
              AND (status = 'error' OR (exit_code IS NOT NULL AND exit_code != 0))
            """,
            (session_id,),
        ).fetchone()
        count = row[0] if row else 0
    except Exception:
        # Genuine substrate error (corrupt/locked db, unreadable file). Close
        # the handle and RE-RAISE so cmd_failures reports it as exit 2 instead
        # of masquerading as "0 failures found" (#36 M7). The missing-path /
        # no-session case was handled by the early return above.
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        raise

    if count == 0:
        try:
            conn.close()
        except Exception:
            pass
        return 0, []

    details = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT tool_name, error_message, error_type, retry_count,
                   COALESCE(destructive, 0) AS destructive
            FROM tool_usage
            WHERE session_id = ?
              AND COALESCE(read_only, 0) = 0
              AND (status = 'error' OR (exit_code IS NOT NULL AND exit_code != 0))
            ORDER BY completed_at DESC
            LIMIT 50
            """,
            (session_id,),
        ).fetchall()
        for r in rows:
            details.append({
                "tool": _sanitize_tool_name(r["tool_name"] or "?"),
                "error": _sanitize_error_text(r["error_message"] or ""),
                "error_type": r["error_type"] or "",
                "retry_count": r["retry_count"] or 0,
                "destructive": bool(r["destructive"]),
            })
    except Exception:
        # Enrichment columns absent — detection already succeeded, so surface
        # bare placeholders so the count is still actionable.
        details = [{"tool": "?", "error": ""} for _ in range(count)]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return count, details

def _sanitize_exc_text(text: str, limit: int = 300) -> str:
    """Make an exception message safe to echo in JSON/stderr: collapse filesystem
    paths and DB URIs to placeholders (paths can leak usernames/structure) and
    cap length. Never raises (#36 M7)."""
    s = text or ""
    # Redact anything that looks like a filesystem path (unix or windows).
    s = re.sub(r"(?:[A-Za-z]:)?[\\/](?:[^\s'\":<>|?*]+[\\/])+[^\s'\":<>|?*]+",
               "<path>", s)
    # Redact sqlite URI-style connect strings.
    s = re.sub(r"file:[^\s,]+", "<db>", s, flags=re.IGNORECASE)
    if len(s) > limit:
        s = s[:limit] + "..."
    return s

def cmd_failures(session: str, transcript: str, db: str) -> int:
    """Print {"count":N,"details":[...],"rejections":[...]} for the session's
    failed tool calls and return an exit code. Transcript wins when given and
    present (Claude Code); else the db substrate (ZCode). Entirely
    self-contained — does NOT open the ZMem store.

    ``count``/``details`` mean GENUINE failures only; user rejections are split
    into ``rejections`` [{tool, reason}] (issue #46). On the db substrate there
    is no rejection record, so ``rejections`` is always ``[]`` there.

    Exit-code contract (#36 M7): a *checked* result (empty or not) exits 0; a
    *broken substrate* (the detection itself raised) exits 2 with an ``error``
    field so a caller checking ``$?`` no longer treats "could not check" as
    "0 failures found". The previously-bare ``except`` silently swallowed every
    error into ``{count:0}`` + exit 0, hiding corrupt/locked/missing substrates.
    """
    try:
        if transcript and os.path.isfile(transcript):
            details, rejections = _failures_from_transcript(transcript)
            result = {"count": len(details), "details": details, "rejections": rejections}
        else:
            count, details = _failures_from_db(db, session)
            result = {"count": count, "details": details, "rejections": []}
    except Exception as exc:
        msg = _sanitize_exc_text(str(exc))
        result = {"count": 0, "details": [], "rejections": [], "error": msg}
        print(json.dumps(result))
        print(f"[zmem] failures: detection substrate error: {msg}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0

def _sanitize_correction_message(text, limit: int = 200) -> str:
    """Make a user-correction message safe for the `corrections` JSON output:
    collapse CR/newlines to spaces (fence-integrity, same discipline as
    `failures`), then truncate. Returns "" for empty input."""
    if not text:
        return ""
    return _collapse_line_breaks(text)[:limit].strip()

def cmd_corrections(*, transcript: str) -> int:
    """Mine user corrections from a Claude Code transcript JSONL (issue #46).

    Always read-only: this command NEVER opens the ZMem store (it is dispatched
    before connect(), so a bad/locked/missing store can never break it) and
    never writes anything. Candidates are reviewed by an agent/human before any
    `add` (signal honesty per skills/closeout/SKILL.md).

    Parses Claude Code transcript format only; other hosts' histories are out of
    scope (see the zmem host matrix). Fail-open: an unreadable/unrecognized
    transcript yields {"count": 0, "items": []}.
    """
    items = []
    try:
        raw_texts = _extract_user_messages(transcript)
        mode = _normalize_capture_mode(None)
        for text in raw_texts:
            classified = _classify_correction(text)
            if not classified:
                continue
            classified["message"] = _sanitize_correction_message(text)
            # Secrets: run each emitted message through the store's secret
            # detection (SECRET_PATTERNS). In capture mode "auto" replace the
            # message with the redacted form; otherwise (e.g. manual) keep the
            # ORIGINAL (unredacted) wording so a reviewer can still read it.
            # (Note: corrections.detect_patterns applies its OWN secret
            # confidence penalty during classification — this is a second,
            # independent pass for redaction/annotation only.)
            # Note: the newline-collapse + length-truncation applied above is a
            # fence-integrity/output-safety step that runs in BOTH modes — a
            # "verbatim" manual-mode message is line-break-free and capped at
            # 200 chars, only the secret redaction is omitted.
            redacted, redactions = redact_text(classified["message"])
            if redactions:
                classified["secret_warning"] = True
                if mode == "auto":
                    classified["message"] = redacted
            items.append(classified)
    except Exception:
        items = []
    print(json.dumps({"count": len(items), "items": items}))
    return 0

def _classify_correction(text: str):
    """Run the ported detect_patterns and return a correction item dict (or None
    when the text is not a detectable correction/positive); the `message` key is
    filled in by the caller with the sanitized text. Defined as a small shim so
    cmd_corrections stays readable."""
    item_type, patterns, confidence, sentiment, decay_days = _detect_patterns(text)
    if not item_type:
        return None
    return {
        "type": item_type,
        "patterns": patterns,
        "confidence": confidence,
        "sentiment": sentiment,
        "decay_days": decay_days,
    }

def _transcript_mtime_iso(path) -> str:
    """ISO-8601 UTC timestamp of a transcript file's mtime (used as each mined
    candidate's event time for cross-session 'keep most recent' ordering)."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
    except OSError:
        return ""

def _mine_corrections_from_transcript(transcript, project_folder: str) -> list:
    """Mine correction candidates from a single CC transcript, tagged with
    transcript-side provenance (project_folder/transcript/timestamp). Mirrors
    cmd_corrections' per-message pipeline per message: classify -> sanitize ->
    capture-mode redact/annotate. Fail-open to [] on any read/parse error."""
    items = []
    mode = _normalize_capture_mode(None)
    try:
        raw_texts = _extract_user_messages(transcript)
    except Exception:
        raw_texts = []
    for text in raw_texts:
        classified = _classify_correction(text)
        if not classified:
            continue
        classified["message"] = _sanitize_correction_message(text)
        redacted, redactions = redact_text(classified["message"])
        if redactions:
            classified["secret_warning"] = True
            if mode == "auto":
                classified["message"] = redacted
        classified["project_folder"] = project_folder
        classified["transcript"] = str(transcript)
        classified["timestamp"] = _transcript_mtime_iso(transcript)
        items.append(classified)
    return items

def _redact_error_pattern_samples(error_patterns) -> None:
    """Redact/annotate error_pattern ``sample_errors`` per capture mode for the
    ``--json`` report (issue #48 / PRR-001). A secret embedded in a repeated
    failing command (e.g. a retried auth/setup invocation) must not reach stdout
    raw. In 'auto' each sample is redacted; in 'manual'/'reviewed' it is kept
    verbatim and the pattern is flagged ``secret_warning``. Idempotent:
    already-redacted text won't re-match. Mutates the list in place."""
    mode = _normalize_capture_mode(None)
    for e in error_patterns or []:
        flagged = False
        out = []
        for s in e.get("sample_errors") or []:
            redacted, n = redact_text(str(s))
            if n:
                flagged = True
            out.append(redacted if (n and mode == "auto") else str(s))
        if flagged:
            e["secret_warning"] = True
        e["sample_errors"] = out

def _queue_mined(report, host: str, as_json: bool = False) -> int:
    """Append mined candidates to the #47 sidecar review queue (source
    'history-mine') under the canonical namespace of the box's current
    project. Idempotent: an item whose dedup_key already exists is skipped so a
    re-run never double-appends. Fail-open: a missing/unwritable queue module
    yields a clean non-zero message, never a crash.

    Honest accounting (PRR-005): an ``append_queue`` returning False or raising
    is tracked as a FAILED write (NOT folded into the "already present" count),
    reported, and makes the command return non-zero — a silent write loss must
    not look like success. In ``as_json`` mode the human summary is routed to
    stderr so stdout stays a pure ``json.loads``-able report (PRR-004)."""
    try:
        import correction_queue as _cq
        namespace = _host.resolve_namespace(os.getcwd()) if _host else "user:global"
    except Exception as exc:
        _sanitized = _sanitize_exc_text(str(exc))
        print("[zmem] mine-history: --queue failed (queue unavailable): %s" % _sanitized,
              file=sys.stderr)
        return 2
    try:
        import history_mining as _hm
        items = _hm.build_mined_items(report, namespace=namespace, host=host)
    except Exception as exc:
        _sanitized = _sanitize_exc_text(str(exc))
        print("[zmem] mine-history: --queue failed (candidate build error): %s" % _sanitized,
              file=sys.stderr)
        return 2
    try:
        existing_keys = {
            it.get("dedup_key")
            for it in _cq.load_queue(namespace)
            if isinstance(it, dict) and it.get("dedup_key")
        }
    except Exception:
        existing_keys = set()
    added = 0
    failed = 0
    for it in items:
        key = it.get("dedup_key")
        if key in existing_keys:
            continue
        try:
            if _cq.append_queue(namespace, it):
                added += 1
                existing_keys.add(key)
            else:
                failed += 1
        except Exception:
            failed += 1
    already = len(items) - added - failed
    summary = ("[zmem] mine-history: queued %d candidate(s) to %s (source=history-mine); "
               "%d already present" % (added, namespace, already))
    if failed:
        summary += "; %d write(s) FAILED" % failed
    if as_json:
        print(summary, file=sys.stderr)
    else:
        print(summary)
    return 2 if failed else 0

# ---------------------------------------------------------------------------
# Issue #71 I: Codex + Hermes mine-history adapters. Review-queue candidates
# ONLY — candidates land in the #47 sidecar queue via the same
# dedup_key-idempotent append the Claude adapter uses; closeout stays the
# write authority. NEVER raw_memories.md; never full transcripts.
# ---------------------------------------------------------------------------

_CODEX_SECTIONS = {
    "user preferences": "preference",
    "reusable knowledge": "lesson",
    "failures and how to do differently": "lesson",
}


def _mine_codex_candidates(memory_path: str) -> list[dict]:
    """Parse Codex MEMORY.md sections into review candidates.

    Reads ONLY the curated sections (User preferences / Reusable knowledge /
    Failures and how to do differently) — bullets become candidates with a
    stable dedup_key (sha1 of section+text). raw_memories.md is REFUSED
    outright (the issue's park list: bulk-ingesting raw model memory is the
    noise antipattern)."""
    p = Path(memory_path).expanduser()
    if p.name.lower() == "raw_memories.md":
        raise ValueError(
            "raw_memories.md is never mined (issue #71 I: noise "
            "antipattern); point --transcript-dir at a curated MEMORY.md")
    if not p.is_file():
        raise FileNotFoundError(str(p))
    text = p.read_text(encoding="utf-8", errors="replace")
    candidates: list[dict] = []
    section_type: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            header = stripped.lstrip("#").strip().lower()
            section_type = _CODEX_SECTIONS.get(header)
            continue
        if section_type and stripped.startswith(("-", "*")):
            message = stripped.lstrip("-*").strip()
            if len(message) >= 5:
                candidates.append({
                    "message": message,
                    "type": section_type,
                    "source": "codex-MEMORY.md",
                    "dedup_key": hashlib.sha1(
                        (section_type + "\x00" + message)
                        .encode("utf-8", "replace")).hexdigest(),
                })
    return candidates


_HERMES_MAX_SESSION_FILES = 50
_HERMES_MAX_FILE_BYTES = 8 * 1024 * 1024


def _mine_hermes_candidates(sessions_dir: str) -> list[dict]:
    """Mine user turns from Hermes session JSONL transcripts into review
    candidates (corrections + explicit preferences via the SAME
    corrections.detect_patterns rules the live hook uses). Read-only;
    bounded (newest _HERMES_MAX_SESSION_FILES files, per-file byte cap); an
    unparseable line is skipped — session JSONL shape is upstream-owned."""
    import corrections as _corr
    root = Path(sessions_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    files = sorted(
        (p for p in root.rglob("*.jsonl") if p.is_file()
         and p.stat().st_size <= _HERMES_MAX_FILE_BYTES),
        key=lambda p: p.stat().st_mtime, reverse=True)
    candidates: list[dict] = []
    for path in files[:_HERMES_MAX_SESSION_FILES]:
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            role = str(obj.get("role") or obj.get("type") or "")
            content = obj.get("content") or obj.get("text") or obj.get("message")
            if role not in ("user", "human") or not isinstance(content, str):
                continue
            text = content.strip()
            if len(text) < 5 or not _corr.should_include_message(text):
                continue
            item_type, patterns, confidence, sentiment, decay = \
                _corr.detect_patterns(text)
            if not item_type:
                continue
            candidates.append({
                "message": text,
                "type": item_type,
                "patterns": patterns,
                # PRR-013: keep the detector's per-pattern calibration
                # instead of hard-coding confidence/decay at queue time.
                "confidence": confidence,
                "decay_days": decay,
                "source": "hermes-session:" + path.name,
                "dedup_key": hashlib.sha1(
                    (item_type + "\x00" + text)
                    .encode("utf-8", "replace")).hexdigest(),
            })
    return candidates


def cmd_mine_history_adapters(*, source: str, transcript_dir: str,
                              queue: bool, as_json: bool, limit=None) -> int:
    """Dispatch for `mine-history --source codex|hermes` (issue #71 I).

    Same contract as the Claude lane: read-only on inputs, candidates are
    REVIEWED before any store write, --queue appends to the #47 sidecar with
    dedup_key idempotency, never auto-writes the store."""
    try:
        if source == "codex":
            root = transcript_dir or os.environ.get("ZMEM_CODEX_MEMORY") \
                or str(Path.home() / ".codex" / "MEMORY.md")
            candidates = _mine_codex_candidates(root)
        else:
            root = transcript_dir or os.environ.get("ZMEM_HERMES_SESSIONS") \
                or str(Path.home() / ".hermes" / "sessions")
            candidates = _mine_hermes_candidates(root)
    except FileNotFoundError as exc:
        print("[zmem] mine-history: no %s input found at %s (set "
              "--transcript-dir or the source's env knob)"
              % (source, exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print("[zmem] mine-history: %s" % exc, file=sys.stderr)
        return 1
    if limit is not None:
        candidates = candidates[:limit]
    if as_json:
        print(json.dumps({"source": source, "candidates": candidates,
                          "count": len(candidates)}))
    else:
        print("[zmem] mine-history: %d candidate(s) from %s"
              % (len(candidates), source))
        for c in candidates:
            print("  %s [%s]: %s" % (c["type"], c["source"],
                                     c["message"][:100]))
    if not queue:
        return 0
    try:
        import correction_queue as _cq
        namespace = _host.resolve_namespace(os.getcwd()) if _host \
            else "user:global"
    except Exception as exc:
        print("[zmem] mine-history: --queue failed (queue unavailable): %s"
              % _sanitize_exc_text(str(exc)), file=sys.stderr)
        return 2
    try:
        existing = {it.get("dedup_key") for it in _cq.load_queue(namespace)
                    if isinstance(it, dict) and it.get("dedup_key")}
    except Exception:
        existing = set()
    added = failed = 0
    for c in candidates:
        if c["dedup_key"] in existing:
            continue
        try:
            item = _cq.make_item(
                message=c["message"], type_=c["type"],
                patterns=c.get("patterns", ""),
                # PRR-013: pass the detector's own calibration through
                # (hermes candidates carry it; codex candidates fall back
                # to the documented 0.7/90 defaults).
                confidence=c.get("confidence", 0.7),
                sentiment="correction",
                decay_days=c.get("decay_days", 90), session="",
                namespace=namespace,
                host="codex" if source == "codex" else "hermes",
            )
            item["dedup_key"] = c["dedup_key"]
            item["source"] = "history-mine"
            if _cq.append_queue(namespace, item):
                added += 1
                existing.add(c["dedup_key"])
            else:
                failed += 1
        except Exception:
            failed += 1
    summary = ("[zmem] mine-history: queued %d candidate(s) to %s "
               "(source=%s); %d already present"
               % (added, namespace, source, len(candidates) - added - failed))
    if failed:
        summary += "; %d write(s) FAILED" % failed
    print(summary, file=sys.stderr if as_json else sys.stdout)
    return 2 if failed else 0

def cmd_mine_history(*, transcript_dir, all_projects: bool, days, min_count: int,
                     limit, queue: bool, as_json: bool) -> int:
    """Mine user corrections, tool rejections, and repeated tool-error patterns
    from HISTORICAL Claude Code transcripts (issue #48; PR 3/4 claude-reflect).

    READ-ONLY against transcripts AND the store: this command NEVER opens the
    ZMem store (it is dispatched before connect()), and the only write surface
    is the #47 sidecar review queue when ``--queue`` is given. Candidates are
    REVIEWED by an agent before any row enters the store (signal honesty per
    skills/closeout/SKILL.md).

    Host input surface is Claude Code transcripts only (host matrix; ZCode /
    Codex / Hermes out of scope by design — see README Bootstrap section).
    A missing transcript dir is a CLEAN non-zero exit with a clear message (the
    expected outcome on a ZCode/Codex-only box), not a crash; "scanned N files,
    found nothing" exits 0.
    """
    try:
        import history_mining as _hm
    except Exception:
        _fail = {
            "corrections": [], "rejections": [], "error_patterns": [],
            "scanned": {"files": 0, "skipped": 0},
        }
        if as_json:
            print(json.dumps(_fail))
        print("[zmem] mine-history: history_mining module unavailable", file=sys.stderr)
        return 2

    root = _hm.resolve_transcript_root(transcript_dir)
    files, missing = _hm.discover_transcripts(
        root, all_projects=bool(all_projects), project_dir=os.getcwd(), days=days)

    if missing:
        msg = (
            "[zmem] mine-history: no Claude Code transcript directory found at %s. "
            "Mining reads Claude Code history only (see README 'Bootstrap / cold "
            "start'); run --transcript-dir with a custom path, or expect no "
            "candidates on a ZCode/Codex-only box." % root
        )
        if as_json:
            print(json.dumps({
                "corrections": [], "rejections": [], "error_patterns": [],
                "scanned": {"files": 0, "skipped": 0},
                "error": "no transcript dir",
            }))
        else:
            print(msg, file=sys.stderr)
        return 1

    corrections = []
    rejections = []
    errors = []  # raw classified errors awaiting aggregation
    skipped = 0
    for path, folder in files:
        if not _hm.is_cc_transcript(path):
            skipped += 1
            continue
        corrections.extend(_mine_corrections_from_transcript(path, folder))
        try:
            details, rejs = _failures_from_transcript(str(path))
        except Exception:
            details, rejs = [], []
        for r in rejs:
            r["project_folder"] = folder
            r["transcript"] = str(path)
            rejections.append(r)
        for d in details:
            etext = d.get("error")
            etype, guideline = _classify_error_type(etext) if etext else (None, None)
            if not etype:
                continue
            errors.append({
                "error_type": etype,
                "content": _sanitize_error_text(etext, _SAMPLE_EXTRACT_LIMIT),
                "project_folder": folder,
                "suggested_guideline": guideline,
            })

    corrections = _hm.dedupe_corrections(corrections)
    error_patterns = _aggregate_errors(errors, min_occurrences=min_count)
    _redact_error_pattern_samples(error_patterns)  # PRR-001: --json samples never leak

    if limit is not None:
        corrections = corrections[:limit]

    report = {
        "corrections": corrections,
        "rejections": rejections,
        "error_patterns": error_patterns,
        "scanned": {"files": len(files), "skipped": skipped},
    }

    if not missing and not all_projects and not files:
        # Scoped discovery found nothing for the current project: make the
        # likely cause obvious instead of a bare "scanned 0" rc 0 (PRR-009).
        print("[zmem] mine-history: no Claude Code transcript folder matched the "
              "current project under %s; try --all-projects to scan every project "
              "or --transcript-dir for a custom root." % root, file=sys.stderr)

    if as_json:
        print(json.dumps(report))
    else:
        print("[zmem] mine-history: scanned %d file(s), skipped %d, %d correction(s), "
              "%d rejection(s), %d error pattern(s)"
              % (len(files), skipped, len(corrections), len(rejections), len(error_patterns)))
        if rejections:
            print("  rejections:")
            for r in rejections:
                print("    - %s: %s [%s]" % (r.get("tool"), r.get("reason"),
                                             r.get("project_folder")))
        for c in corrections:
            warn = "[secret] " if c.get("secret_warning") else ""
            print("  %scorrection %s (x%d) [%s]: %s"
                  % (warn, c.get("type"), c.get("occurrences", 1),
                     c.get("project_folder"), c.get("message")))
        for e in error_patterns:
            print("  error %s x%d (priority %.2f) [%s]: %s"
                  % (e.get("error_type"), e.get("count"), e.get("review_priority"),
                     e.get("project_folder"), (e.get("suggested_guideline") or "")))

    if queue:
        return _queue_mined(report, host=os.environ.get("ZMEM_HOST") or "cli",
                            as_json=as_json)
    return 0

def cmd_queue_list(*, namespace: str, as_json: bool) -> int:
    """List a namespace's live-capture correction candidates (issue #47).

    Always read-only and STORE-INDEPENDENT: it reads the sidecar queue file via
    correction_queue, never the ZMem store, so it is dispatched BEFORE
    connect() — a bad/locked/missing store can never block closeout review
    (same policy as `failures`/`corrections`/`sweep`). Fail-open: an unreadable
    queue yields {"count": 0, "items": []} without raising.

    The emitted `items[]` shape is a superset of cmd_corrections' item (it adds
    schema_version/id/timestamp/session/namespace/host/source), so the closeout
    Step 0.5 review rubric is identical to transcript mining.
    """
    try:
        import correction_queue as _cq
        items = _cq.load_queue(namespace)
    except Exception:
        items = []
    if as_json:
        print(json.dumps({"count": len(items), "items": items}))
        return 0
    if not items:
        print("(no correction candidates pending)")
        return 0
    for it in items:
        flag = "[stale] " if it.get("stale") else ""
        warning = "[secret] " if it.get("secret_warning") else ""
        print("- %s%s[%s] %s" % (flag, warning, it.get("type", "?"), it.get("message", "")))
    return 0

def cmd_queue_clear(*, namespace: str, ids, clear_all: bool, drop_stale: bool) -> int:
    """Clear processed/deferred live-capture correction candidates (issue #47).

    Operates on the STORE-INDEPENDENT sidecar queue file (never the store), so
    it dispatches BEFORE connect() like queue-list. --id removes specific
    items (closeout clears processed ones and leaves deferred in place);
    --all empties the whole namespace queue; --drop-stale prunes stale items
    with confidence < 0.6. The three are mutually exclusive (argparse group),
    so --all is never silently dropped. Fail-open: a write failure leaves the
    file untouched and reports 0 removed.
    """
    try:
        import correction_queue as _cq
        if clear_all:
            before = len(_cq.load_queue(namespace))
            # Use the return value: clear_queue reports 0 when the whole-queue
            # unlink FAILED (and 0 when the queue was already empty). Distinguish
            # the two via the pre-count so an unlink failure routes to the
            # honest "failed (queue untouched)" message instead of fabricating a
            # "cleared N" that never deleted anything.
            removed = _cq.clear_queue(namespace)
            if removed != before:
                print("[zmem] queue-clear: failed (queue untouched)")
                return 0
            print("[zmem] queue-clear: cleared %s (%d item(s))" % (namespace, removed))
            return 0
        removed = _cq.clear_queue(namespace, ids=ids or None, drop_stale=drop_stale)
        print("[zmem] queue-clear: removed %d item(s) from %s" % (removed, namespace))
        return 0
    except Exception:
        print("[zmem] queue-clear: failed (queue untouched)")
        return 0


# ---------------------------------------------------------------------------
# Issue #71 E: promote-store — one-shot merge of a leftover second store into
# the canonical one. Optional companion to doctor's `second-stores` check.
# ---------------------------------------------------------------------------

# Columns promoted from the source store, in _validate_sync_row's vocabulary.
# Anything the source lacks is simply absent -> the validator's documented
# default applies (e.g. pre-v11 sources have no taint -> "" -> validator
# normalizes). Never invents values the validator would not accept.
# PRR-014: merged_from / trust_score / applied_count / violated_count are in
# the projection so a v11+ source keeps its dedup lineage, trust, and
# usage-feedback history instead of being silently reset to validator
# defaults; sources that predate those columns simply omit them.
_PROMOTE_COLUMNS = (
    "id", "namespace", "type", "content", "tags", "source_ref", "confidence",
    "signal", "ingestion_ts", "valid_from", "valid_until", "update_of",
    "taint", "superseded_at", "supersede_reason",
    "merged_from", "trust_score", "applied_count", "violated_count",
)

# PRR-015: associated (non-derivable) tables promoted alongside memory rows —
# associative links (v11) and episode containers/memberships (v13). Each
# entry carries its NATURAL key columns: memory_link has no id (its uniqueness
# is the (src_id, dst_id, relation) triple); episode_memory's PK is the
# (episode_id, memory_id) pair; episode has a plain id PK.
_ASSOCIATED_TABLES = (
    ("memory_link", ("src_id", "dst_id", "relation")),
    ("episode", ("id",)),
    ("episode_memory", ("episode_id", "memory_id")),
)


def cmd_promote_store(conn: sqlite3.Connection, *, from_path: str,
                      dry_run: bool = False) -> int:
    """Merge every row of another zmem store into THIS (canonical) one.

    Read-only on the source (opened mode=ro; it may even be an old schema —
    the column intersection below skips columns it does not have, and a
    NEWER source schema is refused outright: promoting v14 rows into a v13
    store would demote trust/lineage the destination cannot represent).
    Source ids are PRESERVED, so re-running is a no-op (_ingest_row's
    existing-local-row short-circuit makes the whole command idempotent —
    the issue's 'capture-mode auto, idempotent IDs' requirement).

    Rows are marshaled through the EXISTING _validate_sync_row contract owner
    (the same gate ingest-jsonl applies to remote data) and applied via
    _ingest_row with capture-mode 'manual': store-originated rows are
    curated by definition, so content stays verbatim while the advisory
    secret scan still reports anything suspicious. No vec rows are invented
    here — the destination's normal reembed/embeddings lane owns vectors
    (same as ingest-jsonl).

    --dry-run reports the per-outcome tallies and the aggregate defaulting
    counts without writing.
    """
    src = Path(from_path).expanduser()
    if not src.is_file():
        print(f"[zmem] promote-store: source store not found: {src}")
        return 1

    # Schema negotiation: refuse a NEWER source (no trust demotion).
    def _read_version(path: Path) -> int:
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = c.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                return int(row[0]) if row else 1
            finally:
                c.close()
        except (sqlite3.Error, ValueError):
            return 1

    src_ver = _read_version(src)
    dst_row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    dst_ver = int(dst_row[0]) if dst_row else 1
    if src_ver > dst_ver:
        print(f"[zmem] promote-store: refusing: source schema v{src_ver} is "
              f"newer than this store's v{dst_ver}; upgrade this client first")
        return 1

    sconn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    sconn.row_factory = sqlite3.Row
    try:
        src_cols = {r["name"] for r in sconn.execute("PRAGMA table_info(memory)")}
        cols = [c for c in _PROMOTE_COLUMNS if c in src_cols]
        rows = sconn.execute(
            f"SELECT {', '.join(cols)} FROM memory").fetchall()
        # PRR-015: capture the source's ASSOCIATED-table shapes and pre-read
        # their rows while the read-only source connection is open. Bounded:
        # links/episode memberships are small relative to memory rows.
        sconn_cols_cache: dict[str, list[str]] = {}
        assoc_rows: dict[str, list[tuple]] = {}
        for table, _pk in _ASSOCIATED_TABLES:
            try:
                tcols = [r["name"] for r in
                         sconn.execute(f"PRAGMA table_info({table})")]
            except sqlite3.Error:
                tcols = []
            if not tcols:
                continue
            sconn_cols_cache[table] = tcols
            assoc_rows[table] = sconn.execute(
                f"SELECT {', '.join(tcols)} FROM {table}").fetchall()
    finally:
        sconn.close()

    # Destination table columns (for the associated-table intersection).
    dst_tables: dict[str, list[str]] = {}
    for table, _pk in _ASSOCIATED_TABLES:
        try:
            dst_tables[table] = [r["name"] for r in
                                 conn.execute(f"PRAGMA table_info({table})")]
        except sqlite3.Error:
            dst_tables[table] = []

    def sconn_read(table: str, common: list[str]) -> list[tuple]:
        """Project the pre-read associated rows onto the destination's common
        column order (PRR-015)."""
        out = []
        for srow in assoc_rows.get(table, []):
            out.append(tuple(srow[c] for c in common))
        return out

    # Lazy import avoids a module-level cycle (sync imports write; mine is
    # imported by cli alongside both).
    from storelib.sync import _ingest_row, _validate_sync_row

    tallies: dict[str, int] = {}
    defaulted: dict[str, int] = {}
    malformed = 0
    # Review-minor fix: project the existing-id short-circuit in dry-run too
    # (the destination's live id set), so the dry-run summary accounts for
    # EVERY source row: would_promote + would_skip + malformed == total.
    dry_skip_ids: set[str] = set()
    if dry_run:
        try:
            dry_skip_ids = {
                r[0] for r in conn.execute("SELECT id FROM memory")}
        except sqlite3.Error:
            dry_skip_ids = set()
    for row in rows:
        obj = {c: row[c] for c in cols if row[c] is not None}
        try:
            validated = _validate_sync_row(obj)
        except ValueError as exc:
            malformed += 1
            if dry_run:
                print(f"[zmem] promote-store: would skip one malformed row "
                      f"({exc})")
            continue
        # Track which optional fields the VALIDATOR defaulted (absent from
        # the source), so --dry-run can summarize the shape change honestly.
        for key in ("taint", "valid_until", "update_of", "supersede_reason"):
            if key not in obj and validated.get(key):
                defaulted[key] = defaulted.get(key, 0) + 1
        if dry_run:
            if validated["id"] in dry_skip_ids:
                tallies["would_skip"] = tallies.get("would_skip", 0) + 1
            else:
                tallies["would_promote"] = tallies.get("would_promote", 0) + 1
            continue
        try:
            outcome = _ingest_row(conn, validated, allow_tombstones=True,
                                  capture_mode="manual")
        except Exception as exc:
            print(f"[zmem] promote-store: row {validated.get('id', '?')[:8]} "
                  f"failed ({type(exc).__name__}: {exc}); skipped")
            tallies["failed"] = tallies.get("failed", 0) + 1
            continue
        tallies[outcome] = tallies.get(outcome, 0) + 1

    # PRR-015: memory rows alone are an incomplete merge — v11+ associative
    # links and v13 episode containers/memberships are NOT derivable from the
    # rows. Promote them too when BOTH stores carry the table, with a column
    # intersection and INSERT OR IGNORE on the primary key so re-runs stay
    # idempotent. Tables absent on either side are skipped with a count.
    assoc_skipped: list[str] = []
    for table, pk in _ASSOCIATED_TABLES:
        src_has = bool(sconn_cols_cache.get(table))
        dst_has = bool(dst_tables.get(table))
        if not (src_has and dst_has):
            if src_has and not dst_has:
                assoc_skipped.append(table)
            continue
        src_tcols = sconn_cols_cache[table]
        dst_tcols = set(dst_tables[table])
        common = [c for c in src_tcols if c in dst_tcols]
        if not common:
            assoc_skipped.append(table)
            continue
        srows = sconn_read(table, common)
        col_list = ", ".join(common)
        placeholders = ", ".join("?" * len(common))
        pk_idxs = [common.index(c) for c in pk if c in common]
        for srow in srows:
            if dry_run:
                # Dry-run purity (reviewer gate on the PRR-015 fix):
                # --dry-run must never write. Report would-insert counts
                # against the destination's existing rows instead.
                exists = conn.execute(
                    f"SELECT 1 FROM {table} WHERE "
                    + " AND ".join(f"{c}=?" for c in pk if c in common),
                    tuple(srow[i] for i in pk_idxs),
                ).fetchone()
                key = table if exists is None else f"{table}_would_skip"
                tallies[key] = tallies.get(key, 0) + 1
                continue
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({col_list}) "
                    f"VALUES ({placeholders})",
                    tuple(srow),
                )
                # The assoc INSERTs run on the CLI connection OUTSIDE
                # _ingest_row's per-row commit — without an explicit commit
                # they roll back at process exit (caught by the
                # carries-associated-tables regression test).
                from storelib.schema import _commit
                _commit(conn)
                tallies[f"{table}"] = tallies.get(f"{table}", 0) + 1
            except sqlite3.Error as exc:
                print(f"[zmem] promote-store: {table} row skipped "
                      f"({type(exc).__name__}: {exc})")
                tallies[f"{table}_failed"] = \
                    tallies.get(f"{table}_failed", 0) + 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(tallies.items())) or "no rows"
    verb = "dry-run" if dry_run else "promoted"
    print(f"[zmem] promote-store ({verb}) from {src}: {summary}"
          + (f"; malformed={malformed}" if malformed else "")
          + (f"; defaulted fields: "
             f"{', '.join(sorted(k) for k in defaulted)}" if defaulted else "")
          + (f"; tables skipped (absent on destination): "
             f"{', '.join(assoc_skipped)}" if assoc_skipped else ""))
    # PRR-012: a partial merge must not look complete — malformed source rows
    # or per-row write failures make the command exit non-zero (the summary
    # above already names the counts; dry-run stays informational).
    # Reviewer follow-up: associated-table write failures count too.
    if not dry_run and (malformed or tallies.get("failed")
                        or any(k.endswith("_failed") for k in tallies)):
        return 1
    return 0
