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
    from correction_queue import SECRET_PATTERNS  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from correction_queue import SECRET_PATTERNS  # type: ignore # noqa: F401
from storelib.schema import CAPTURE_MODES, GLOBAL_NAMESPACE, MAX_CONTENT_CHARS, PROMPT_INJECTION_PATTERNS, SIGNAL_CONFIDENCE, _commit, _embeddings, _env_float, _normalize_content, now_iso

_degraded_embedding_warned = False

# Shared host adapter: path resolution, local-FS safety guard, perms, retry.
# Imported with a safe inline fallback so store.py still runs (with the old,
# pre-Phase-1 resolution chain) if host.py is somehow missing from the checkout.
try:
    import host as _host
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        import host as _host
    except ImportError:
        _host = None

# Shared single source of truth for the schema version constant AND the
# write-path constants (content cap, allowed type/signal enums). doctor.py and
# the Hermes provider surfaces import the SAME values from schema_meta so they
# can never drift (a stale doctor-side schema copy once made every healthy store
# fail the schema gate — #36 M11; #37 L7/L8 logged the same drift on the content
# cap and signal enum across the MCP/Hermes paths).
# Dependency-free and side-effect-free; resolved from this directory like `host`.
try:
    from schema_meta import (  # noqa: F401
        SUPPORTED_SCHEMA_VERSION,
        SCHEMA_VERSION_KEY,
        MAX_CONTENT_CHARS,
        ALLOWED_TYPES,
        ALLOWED_SIGNALS,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from schema_meta import (  # type: ignore # noqa: F401
        SUPPORTED_SCHEMA_VERSION,
        SCHEMA_VERSION_KEY,
        MAX_CONTENT_CHARS,
        ALLOWED_TYPES,
        ALLOWED_SIGNALS,
    )

# Host-agnostic correction-pattern engine (issue #46). Pure text library, no
# store I/O, stdlib-only. Resolved from this directory like `host`/`schema_meta`.
try:
    from corrections import detect_patterns as _detect_patterns  # noqa: F401
    from corrections import extract_user_messages as _extract_user_messages  # noqa: F401
    from corrections import classify_error_type as _classify_error_type  # noqa: F401
    from corrections import aggregate_errors as _aggregate_errors  # noqa: F401
    from corrections import SAMPLE_EXTRACT_LIMIT as _SAMPLE_EXTRACT_LIMIT  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from corrections import detect_patterns as _detect_patterns  # type: ignore # noqa: F401
    from corrections import extract_user_messages as _extract_user_messages  # type: ignore # noqa: F401
    from corrections import classify_error_type as _classify_error_type  # type: ignore # noqa: F401
    from corrections import aggregate_errors as _aggregate_errors  # type: ignore # noqa: F401
    from corrections import SAMPLE_EXTRACT_LIMIT as _SAMPLE_EXTRACT_LIMIT  # type: ignore # noqa: F401

# Live-correction queue (issue #47). `SECRET_PATTERNS` lives here as the single
# source of truth shared by the store's capture-policy helpers AND the queue's
# write-time redaction (so they can never drift). Stdlib-only, resolved like
# `corrections`.
try:
    from correction_queue import SECRET_PATTERNS  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from correction_queue import SECRET_PATTERNS  # type: ignore # noqa: F401



class CapturePolicyRefusal(ValueError):
    """Automatic capture could not safely preserve the record contract."""

class ContentTooLarge(ValueError):
    """Content exceeds MAX_CONTENT_CHARS. A dedicated subclass (rather than a
    bare ValueError) so the CLI dispatch can catch it SPECIFICALLY — a broad
    ``except ValueError`` would also swallow unrelated ValueErrors such as
    UnicodeEncodeError (a ValueError subclass), changing stdio-encoding failure
    behavior (#36 M17)."""

def _check_secrets(content: str, source_ref: str, tags: str = "") -> list[str]:
    """Advisory only. Returns list of warnings. Never blocks. Scans both content
    source_ref, and tags (a token in metadata would otherwise slip through)."""
    warnings = []
    combined = " ".join((content, source_ref, tags))
    for pat in SECRET_PATTERNS:
        m = pat.search(combined)
        if m:
            warnings.append(f"possible secret-like text matched pattern {pat.pattern[:40]!r}: {m.group(0)[:20]!r}...")
    return warnings

def _merge_tag_strings(*tag_sets: str) -> str:
    merged: set[str] = set()
    for raw in tag_sets:
        merged.update(t.strip() for t in (raw or "").split(",") if t.strip())
    return ",".join(sorted(merged))

def _has_injection_risk_tag(tags: str) -> bool:
    """True if the comma-separated `tags` string carries the
    `prompt-injection-risk` tag. Used at READ time to surface the flag in
    recall/recent/list results so the detector is actionable, not write-only
    (#36 M5)."""
    return "prompt-injection-risk" in {
        t.strip() for t in (tags or "").split(",") if t.strip()
    }

def _normalize_capture_mode(mode: str | None) -> str:
    value = (mode or os.environ.get("ZMEM_CAPTURE_MODE") or "manual").strip().lower()
    return value if value in CAPTURE_MODES else "manual"

def _redact_secret_like_text(text: str) -> tuple[str, int]:
    redacted = text or ""
    count = 0
    for pat in SECRET_PATTERNS:
        redacted, changed = pat.subn("[REDACTED_SECRET]", redacted)
        count += changed
    return redacted, count

def _has_prompt_injection_risk(*values: str) -> bool:
    combined = " ".join(v for v in values if v)
    return any(p.search(combined) for p in PROMPT_INJECTION_PATTERNS)

def _apply_capture_policy(
    *,
    content: str,
    source_ref: str,
    tags: str,
    capture_mode: str,
) -> tuple[str, str, str, list[str]]:
    """Apply capture-time redaction/labeling while preserving provenance."""
    mode = _normalize_capture_mode(capture_mode)
    warnings = _check_secrets(content, source_ref, tags)
    out_content = content
    out_source_ref = source_ref
    out_tags = tags
    source_warnings = _check_secrets("", source_ref)
    if mode == "auto" and source_warnings:
        raise CapturePolicyRefusal(
            "refusing automatic capture because source_ref contains secret-like "
            "text; review it manually so provenance and staleness tracking are "
            "not silently destroyed"
        )
    if mode == "auto" and warnings:
        out_content, content_redactions = _redact_secret_like_text(content)
        out_tags, tag_redactions = _redact_secret_like_text(tags)
        total = content_redactions + tag_redactions
        if total <= 0:
            raise RuntimeError(
                "zmem: refusing automatic capture with likely secrets that could "
                "not be safely redacted"
            )
        warnings = [
            f"automatic capture redacted {total} secret-like value(s); review the "
            "stored memory before trusting it"
        ]
        out_tags = _merge_tag_strings(out_tags, "auto-redacted")
    # Scan content, source_ref, AND tags for injection risk: tags are free-form
    # text that is FTS-indexed and surfaced verbatim into model context via
    # recall, so injection text confined to tags must also be tagged. (Symmetric
    # with _check_secrets, which already scans all three for secrets.)
    if _has_prompt_injection_risk(out_content, out_source_ref, out_tags):
        out_tags = _merge_tag_strings(out_tags, "prompt-injection-risk")
    return out_content, out_source_ref, out_tags, warnings

def _to_win_path(p: str) -> str:
    """Normalize a Cygwin path (/c/..., /tmp/..., /home/...) to Windows form so
    Windows Python can open it. Mirrors to_win_path() in the hook scripts.

    Tries `cygpath -w` first (handles all Cygwin mounts); falls back to a regex
    for /<drive>/ paths (single backslash). If neither applies, returns p unchanged.
    """
    if not p or not p.startswith("/"):
        return p
    # Prefer cygpath when available (Git Bash / Cygwin) — it knows all mounts.
    try:
        import subprocess
        out = subprocess.run(
            ["cygpath", "-w", p], capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # Regex fallback for /<drive>/... paths. Single backslash, not doubled.
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1)}:" + "\\" + m.group(2).replace("/", "\\")
    return p

def _source_hash(source_ref: str) -> str:
    """Hash the current content of a mutable markdown source_ref, for staleness checks.

    source_ref format: 'file:<path>' or 'task:<slug>:<file>' or 'db:<table>:<rowid>'.
    Only 'file:' refs are hashed (mutable). db: refs are immutable (episodic). Others: ''.

    Handles Cygwin-style paths (/c/Users/...) by normalizing to Windows format,
    since Windows Python cannot open /c/... paths. If a file: ref cannot be opened
    even after normalization, emits a stderr warning so the staleness feature
    fails LOUD (visible) instead of silent (a no-op that looks like it works).
    """
    if not source_ref.startswith("file:"):
        return ""
    raw = source_ref[5:]
    p = Path(_to_win_path(raw))
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        print(f"[zmem] WARNING: could not read source_ref for staleness hash: {raw} "
              f"(staleness detection disabled for this memory)", file=sys.stderr)
        return ""

def _warn_degraded_embeddings_once(content: str) -> None:
    """Emit a one-time-per-process warning when a LIVE row will be stored
    without an embedding, naming the reason and the remedy.

    Fires iff `content` is non-empty (so empty/whitespace adds — which produce
    no embedding by design — stay silent) AND a live, embeddable row is about
    to land unembedded. Covers BOTH triggers: embeddings unavailable
    (`is_available()` False) and `embed_text()` returning None despite
    availability (model load/inference failure). The tombstone-history import
    path (which deliberately stores NULL embedding for dead rows) does not go
    through `_detect_duplicate` and so never reaches this warning.

    Scope is one-per-process (see `_degraded_embedding_warned`): hooks/CLI/
    Hermes-MCP each spawn a fresh process per operation, so this is one warning
    per write batch — correct, because each such batch IS landing unembedded.
    """
    global _degraded_embedding_warned
    if _degraded_embedding_warned:
        return
    if not content or not content.strip():
        return
    _degraded_embedding_warned = True
    if _embeddings:
        try:
            st = _embeddings.availability_status()
        except Exception:
            st = None
        if st and not st.get("available"):
            reason = st.get("reason") or "unknown"
            models_dir = st.get("models_dir") or "(unknown)"
            missing = st.get("missing_imports") or []
            detail = ""
            if reason == "imports_missing" and missing:
                detail = f" (missing python modules: {', '.join(missing)})"
            elif reason == "model_file_missing":
                detail = " (minilm.onnx absent from the resolved models dir)"
            elif reason == "tokenizer_missing":
                detail = " (tokenizer.json absent from the resolved models dir)"
            print(
                "[zmem] WARNING: memory stored without an embedding — semantic "
                "dedup-on-write, vector recall, and embedding-seeded "
                f"consolidation are disabled for this row. Reason: {reason}{detail}. "
                f"Resolved models_dir: {models_dir}. To enable embeddings, place a "
                "checksum-verified minilm.onnx there (or set ZMEM_MODEL_URL to a "
                "source matching the pinned SHA-256 plus ZMEM_MODEL_AUTODOWNLOAD=1), "
                "install onnxruntime/tokenizers/numpy, then run `reembed` to "
                "backfill. Degraded FTS5/lexical operation remains supported.",
                file=sys.stderr,
            )
            return
    # _embeddings module itself not importable, or availability_status failed.
    print(
        "[zmem] WARNING: memory stored without an embedding — the embeddings "
        "module could not be imported, so semantic dedup-on-write, vector "
        "recall, and embedding-seeded consolidation are disabled. Reinstall "
        "the plugin (or fix sys.path so skills/memory/scripts is importable), "
        "then run `reembed` to backfill. Degraded FTS5/lexical operation "
        "remains supported.",
        file=sys.stderr,
    )

def _detect_duplicate(
    conn: sqlite3.Connection, content: str, namespace: str,
    dedup_cache: dict | None = None,
) -> tuple[sqlite3.Row | None, float, bytes | None]:
    """Find a duplicate live memory for `content` in `namespace`: semantic
    similarity (if embeddings are available) with an exact-match fallback.

    Returns (existing_row_or_None, similarity, embedding_or_None). The
    embedding is returned too so a fresh insert (no duplicate found) does not
    have to re-embed the same content a second time. Shared by add_memory()
    and ingest-jsonl's per-row insert path — dedup-on-write must behave
    identically for a locally-authored add and a synced row.

    ``dedup_cache`` is an optional per-ingest exact-match cache mapping
    ``(namespace, normalized_content) -> existing_id``. When supplied (by
    ingest-jsonl's batch loop), the exact-match fallback checks the cache first
    (O(1)) instead of re-scanning the whole namespace per row — converting the
    ingest path from O(n²) full scans to one initial scan + n O(1) lookups
    (#36 M9). ``add_memory`` passes None (single-row path, unchanged).
    """
    emb = None
    if _embeddings and _embeddings.is_available():
        emb = _embeddings.embed_text(content)
    # NOTE: the degraded-embedding warning is NOT emitted here. _detect_duplicate
    # is called before dedup resolution, so warning here would fire (and consume
    # the one-time-per-process flag) even on a duplicate add that inserts NO new
    # row. The warning is emitted at the actual live-row INSERT sites
    # (add_memory + ingest-jsonl) so it only fires when an unembedded row is
    # truly persisted. See _warn_degraded_embeddings_once.

    existing = None
    dedup_sim = 0.0
    if emb is not None:
        # Semantic dedup: query vec0 for nearest neighbor in the same namespace.
        existing, dedup_sim = _find_semantic_duplicate(conn, emb, namespace)
    if existing is None:
        # Fallback: exact-match dedup.
        norm = _normalize_content(content)
        cache_key = (namespace, norm)
        if dedup_cache is not None:
            # The per-ingest cache was pre-populated with EVERY live row's
            # normalized content at ingest start (and is updated as rows
            # insert), so it is AUTHORITATIVE for exact match: a hit means a
            # duplicate exists (fetch it by id), a miss means none exists (no
            # namespace scan needed — this is what converts the ingest path
            # from O(n²) scans to O(1) lookups, #36 M9).
            if cache_key in dedup_cache:
                hit_id = dedup_cache[cache_key]
                existing = conn.execute(
                    "SELECT id, content FROM memory WHERE id=? AND superseded_at IS NULL",
                    (hit_id,),
                ).fetchone()
                dedup_sim = 1.0 if existing else 0.0
            # else: cache miss ⇒ no exact-match duplicate; skip the scan.
        else:
            # No cache (single-row add path): indexed lookup on content_norm
            # (#39 E4). The v8 migration backfilled this column for all rows
            # and created idx_memory_content_norm(namespace, content_norm)
            # WHERE superseded_at IS NULL, so this is O(log N) instead of the
            # former O(n) per-row Python normalize/compare. Fall back to the
            # legacy scan if the column is somehow absent (defensive: a
            # pre-v8 store that has not yet run migrate()).
            try:
                existing = conn.execute(
                    "SELECT id, content FROM memory "
                    "WHERE namespace=? AND content_norm=? AND superseded_at IS NULL "
                    "LIMIT 1",
                    (namespace, norm),
                ).fetchone()
                dedup_sim = 1.0 if existing else 0.0
            except sqlite3.OperationalError:
                # content_norm column absent (pre-v8, unmigrated store): fall
                # back to the legacy per-row scan so dedup still works.
                candidates = conn.execute(
                    "SELECT id, content FROM memory WHERE namespace=? AND superseded_at IS NULL",
                    (namespace,),
                ).fetchall()
                for c in candidates:
                    if _normalize_content(c["content"]) == norm:
                        existing = c
                        dedup_sim = 1.0  # exact match
                        break
    return existing, dedup_sim, emb

_GLOBAL_NEAR_MISS_STEMS = {
    "global", "globals",
    "userglobal", "globaluser",
    "usersglobal", "globalsusers", "usersusersglobal",
    "userglobals", "globalsuser",
}



def _global_near_miss_key(ns: str) -> str:
    """Normalize a namespace for global-near-miss comparison: lowercase, then
    strip separators (space, _, -, :, and .) so the common typos collapse
    together. `.` is included because it is a common typo for `:` on US
    keyboards (`user.global`). `user:global` → `userglobal`;
    `users:global` → `usersglobal`; etc. (PRR-009, swarm-pr-review.)"""
    return re.sub(r"[\s:_\-.]+", "", ns.lower())

def _validate_namespace(conn: sqlite3.Connection, namespace: str) -> str:
    """Validate/canonicalize a namespace at write time.

    - Reject empty/None/whitespace-only namespaces.
    - Reject obvious near-miss variants of the global namespace (e.g.
      ``global``, ``userglobal``, ``users:global``) by raising
      ``CapturePolicyRefusal`` naming the canonical ``user:global``. Such rows
      would be unreachable from every automatic hook (issue #18).
    - Trim surrounding whitespace (intentional canonicalization —
      ``add --namespace "  user:global  "`` stores under ``user:global``).
    - Emit a non-fatal note in the refusal message when existing live rows are
      already stranded under the rejected near-miss namespace, naming the
      count. Reconciliation of those legacy rows is a separate data-hygiene
      task (doctor/consolidate); this guard only prevents NEW ones.

    Arbitrary namespaces (``project:<x>``, custom keys) pass through untouched.
    """
    if namespace is None or not namespace.strip():
        raise CapturePolicyRefusal(
            "refusing write: namespace is empty; use 'user:global' for "
            "cross-project knowledge or 'project:<name>' for project-scoped"
        )
    trimmed = namespace.strip()

    # The canonical form passes through untouched.
    if trimmed == GLOBAL_NAMESPACE:
        return trimmed

    key = _global_near_miss_key(trimmed)
    is_near_miss = key in _GLOBAL_NEAR_MISS_STEMS

    if is_near_miss:
        # Report ALL already-stranded near-miss rows (any variant sharing a
        # global-near-miss stem), not just the exact spelling the operator
        # typed — otherwise the "0 existing" message is misleading when other
        # variants exist. _global_near_miss_key is a Python helper (not a SQL
        # UDF), so pull the distinct live namespaces and count in Python. This
        # is the rare refusal path, so the small scan is acceptable.
        stranded = 0
        try:
            distinct_ns = conn.execute(
                "SELECT namespace FROM memory WHERE superseded_at IS NULL"
            ).fetchall()
            for row in distinct_ns:
                # Count only NON-canonical near-miss rows. The canonical
                # `user:global` also normalizes to "userglobal" (a stem), so
                # without this exclusion healthy global rows would be falsely
                # reported as stranded. (PRR-001, swarm-pr-review.)
                if (row["namespace"] != GLOBAL_NAMESPACE
                        and _global_near_miss_key(row["namespace"]) in _GLOBAL_NEAR_MISS_STEMS):
                    stranded += 1
        except sqlite3.OperationalError:
            stranded = 0
        msg = (
            f"refusing write: namespace {trimmed!r} looks like a misspelling of "
            f"the global namespace; use {GLOBAL_NAMESPACE!r} instead."
        )
        if stranded:
            msg += (
                f" ({stranded} existing live row(s) are already stranded under "
                f"a global-near-miss namespace and are unreachable from the "
                "automatic hooks — rekey them with `rekey-namespace "
                "--near-miss-global --confirm`.)"
            )
        raise CapturePolicyRefusal(msg)
    return trimmed

def rekey_namespace(
    conn: sqlite3.Connection,
    *,
    from_namespace: str | None = None,
    to_namespace: str | None = None,
    near_miss_global: bool = False,
    dry_run: bool = False,
) -> int:
    """Admin re-key: rewrite the ``namespace`` column of live rows.

    This is the remediation path for legacy rows stranded under a global
    near-miss namespace (``global``, ``userglobal``, …) that the write-time
    ``_validate_namespace`` guard now rejects. Such rows are unreachable from
    every automatic hook (issue #18 "Related observation"); this moves them to a
    reachable namespace (default: ``user:global``) so they surface again.

    Two modes:
      - ``near_miss_global=True``: rekeys EVERY live row whose namespace
        normalizes (via ``_global_near_miss_key``) to a stem in
        ``_GLOBAL_NEAR_MISS_STEMS`` to ``to_namespace`` (default ``user:global``).
        ``from_namespace`` is ignored in this mode.
      - ``near_miss_global=False`` (default): rekeys live rows whose namespace
        exactly equals ``from_namespace`` (case-sensitive) to ``to_namespace``.
        ``from_namespace`` is required in this mode.

    ``to_namespace`` is itself validated (must not itself be a near-miss) so the
    command cannot move rows FROM one dead-letter key TO another.

    Returns the number of rows rekeyed. ``dry_run`` reports the count and the
    candidate namespaces without writing. The write is a single UPDATE under a
    BEGIN IMMEDIATE transaction; superseded rows are left untouched (history).
    """
    # Resolve the default lazily — GLOBAL_NAMESPACE is defined later in the
    # module, so it cannot be a parameter default (evaluated at def time).
    if to_namespace is None:
        to_namespace = GLOBAL_NAMESPACE
    # Validate the destination: trim, reject empty/whitespace (mirror
    # _validate_namespace's empty rule), and never move rows to another dead
    # letter. Without this, `--to ""` (e.g. an unset shell var) would write an
    # empty namespace — a one-way door, since `--from ""` is treated as missing
    # and rows stranded under "" cannot be rekeyed back. (PRR-002/PRR-013.)
    to_namespace = to_namespace.strip()
    if not to_namespace:
        raise ValueError(
            "refusing rekey: destination namespace is empty; use "
            f"{GLOBAL_NAMESPACE!r} or a project:<name> namespace"
        )
    dest_key = _global_near_miss_key(to_namespace)
    if to_namespace != GLOBAL_NAMESPACE and dest_key in _GLOBAL_NEAR_MISS_STEMS:
        raise ValueError(
            f"refusing rekey: destination {to_namespace!r} is itself a global "
            f"near-miss; use {GLOBAL_NAMESPACE!r}"
        )

    # Build the set of source namespaces to rekey.
    if near_miss_global:
        # Scan distinct live namespaces and keep those that normalize to a stem.
        # EXCLUDE the canonical GLOBAL_NAMESPACE: it also normalizes to
        # "userglobal" (a stem), so without this guard `--near-miss-global
        # --to project:x` would silently bulk-move every legit user:global row
        # to project:x — data corruption of the exact tier this tool exists to
        # remediate. (PRR-001, swarm-pr-review.)
        try:
            distinct = conn.execute(
                "SELECT DISTINCT namespace FROM memory WHERE superseded_at IS NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            distinct = []
        sources = [r["namespace"] for r in distinct
                   if r["namespace"] != GLOBAL_NAMESPACE
                   and _global_near_miss_key(r["namespace"]) in _GLOBAL_NEAR_MISS_STEMS]
    else:
        if not from_namespace or not from_namespace.strip():
            raise ValueError(
                "rekey-namespace needs either --near-miss-global or "
                "--from <namespace>"
            )
        sources = [from_namespace]

    if not sources:
        print("[zmem] rekey-namespace: no matching live rows found.")
        return 0

    # Count candidates.
    placeholders = ",".join("?" * len(sources))
    count = conn.execute(
        f"SELECT COUNT(*) AS n FROM memory "
        f"WHERE superseded_at IS NULL AND namespace IN ({placeholders})",
        sources,
    ).fetchone()["n"]

    print(f"[zmem] rekey-namespace: {count} live row(s) under "
          f"{', '.join(repr(s) for s in sources)} -> {to_namespace!r}")
    if dry_run:
        print("[zmem] rekey-namespace: --dry-run, no rows written.")
        return count

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        conn.execute(
            f"UPDATE memory SET namespace=? "
            f"WHERE superseded_at IS NULL AND namespace IN ({placeholders})",
            [to_namespace, *sources],
        )
        if started_tx:
            _commit(conn)
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
    print(f"[zmem] rekey-namespace: rekeyed {count} row(s).")
    return count

def add_memory(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    type_: str,
    content: str,
    tags: str = "",
    source_ref: str = "",
    confidence: float | None = None,
    signal: str = "none",
    valid_from: str = "",
    capture_mode: str = "manual",
) -> str:
    content, source_ref, tags, warns = _apply_capture_policy(
        content=content,
        source_ref=source_ref,
        tags=tags,
        capture_mode=capture_mode,
    )
    # Validate the namespace: reject empty and obvious near-miss variants of
    # the global namespace so they cannot be created silently. A row stored
    # under e.g. `global` (instead of `user:global`) is unreachable from every
    # automatic hook (issue #18 "Related observation"). Whitespace is trimmed
    # (intentional — `add --namespace "  user:global  "` canonicalizes).
    namespace = _validate_namespace(conn, namespace)
    for w in warns:
        prefix = "WARNING (advisory, write proceeded)"
        if _normalize_capture_mode(capture_mode) == "auto":
            prefix = "NOTICE (automatic capture sanitized)"
        print(f"[zmem] {prefix}: {w}", file=sys.stderr)

    if confidence is None:
        confidence = SIGNAL_CONFIDENCE.get(signal, SIGNAL_CONFIDENCE["none"])

    # Enforce the single content-size cap on the CLI/local add path too —
    # previously only ingest-jsonl rejected oversize content, so a >65k row
    # written here broke Tier-3 sync import on another box (#36 M17).
    if len(content) > MAX_CONTENT_CHARS:
        raise ContentTooLarge(
            f"content is {len(content)} chars, over the {MAX_CONTENT_CHARS} limit"
        )

    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        # Dedup-on-write: semantic similarity (if embeddings available) or exact
        # match fallback. Semantic dedup catches paraphrases the exact-match miss.
        # Shared with ingest-jsonl (Tier 3 sync import), which must apply the same
        # dedup-on-write semantics to incoming rows without duplicating this logic.
        existing, dedup_sim, emb = _detect_duplicate(conn, content, namespace)

        if existing:
            # Merge: upgrade confidence/signal if the new add is stronger.
            _merge_on_dedup(conn, existing["id"], confidence, signal, tags)
            if started_tx:
                _commit(conn)
            print(f"[zmem] dedup: existing memory {existing['id']} refreshed "
                  f"(similarity={dedup_sim:.3f}, threshold={DEDUP_SIMILARITY_THRESHOLD})")
            return existing["id"]

        mid = str(uuid.uuid4())
        shash = _source_hash(source_ref)
        ts = now_iso()
        if not valid_from:
            valid_from = ts

        # Determine embedding model name for the embedding_model column.
        emb_model = "minilm-onnx" if emb is not None else ""
        # This insert-site guard is the PRIMARY warning site (the warning was
        # moved out of _detect_duplicate, which runs before dedup resolution and
        # would consume the one-time flag on a no-op duplicate add). See
        # _warn_degraded_embeddings_once.
        if emb is None:
            _warn_degraded_embeddings_once(content)

        conn.execute(
            """INSERT INTO memory
               (id, namespace, type, content, tags, source_ref, source_hash,
                confidence, signal, valid_from, superseded_at, ingestion_ts,
                retrieval_count, last_retrieved, embedding, embedding_model, embedded_at,
                content_norm)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,0,?,?,?,?,?)""",
            (mid, namespace, type_, content, tags, source_ref, shash,
             confidence, signal, valid_from, ts, ts, emb, emb_model,
             ts if emb is not None else None, _normalize_content(content)),
        )
        # Insert into vec0 table if we have an embedding.
        if emb is not None:
            try:
                conn.execute(
                    "INSERT INTO memory_vec(embedding, memory_id) VALUES (?, ?)",
                    [emb, mid],
                )
            except sqlite3.OperationalError:
                pass  # vec0 table not available — embedding stored in memory table only
        if started_tx:
            _commit(conn)
        print(f"[zmem] added memory {mid} (ns={namespace}, type={type_}, signal={signal}, conf={confidence}"
              f"{', embedded' if emb is not None else ''})")
        return mid
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise

DEDUP_SIMILARITY_THRESHOLD = _env_float("ZMEM_DEDUP_THRESHOLD", 0.85)
# Signal rank for merge: higher = stronger.

_SIGNAL_RANK = {"test": 5, "compile": 4, "lint": 3, "reviewer": 2, "user": 2, "none": 1}



def _find_semantic_duplicate(
    conn: sqlite3.Connection, embedding: bytes, namespace: str, threshold: float = DEDUP_SIMILARITY_THRESHOLD
) -> sqlite3.Row | None:
    """Find the closest existing memory by embedding cosine similarity."""
    try:
        results = conn.execute(
            "SELECT memory_id, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = 5 ORDER BY distance",
            [embedding],
        ).fetchall()
    except sqlite3.OperationalError:
        return None  # vec0 table not available

    for r in results:
        row = conn.execute(
            "SELECT id, confidence, signal, tags FROM memory "
            "WHERE id=? AND superseded_at IS NULL AND namespace=?",
            (r["memory_id"], namespace),
        ).fetchone()
        if row:
            # sqlite-vec distance is cosine distance (0 = identical, 2 = opposite).
            # Convert to cosine similarity: sim = 1 - distance.
            similarity = 1.0 - r["distance"]
            if similarity >= threshold:
                return row, similarity
    return None, 0.0

def _merge_on_dedup(
    conn: sqlite3.Connection, mid: str, new_confidence: float, new_signal: str, new_tags: str
) -> None:
    """Merge a re-observed memory: upgrade confidence/signal/tags if stronger."""
    row = conn.execute(
        "SELECT confidence, signal, tags FROM memory WHERE id=?", (mid,)
    ).fetchone()
    if not row:
        return

    # Take the higher confidence.
    merged_conf = max(row["confidence"], new_confidence)

    # Upgrade signal if the new one is stronger.
    old_rank = _SIGNAL_RANK.get(row["signal"], 1)
    new_rank = _SIGNAL_RANK.get(new_signal, 1)
    merged_signal = new_signal if new_rank > old_rank else row["signal"]

    # Union the tags.
    merged_tags = _merge_tag_strings(row["tags"], new_tags)

    conn.execute(
        "UPDATE memory SET confidence=?, signal=?, tags=?, "
        "last_retrieved=?, retrieval_count=retrieval_count+1 WHERE id=?",
        (merged_conf, merged_signal, merged_tags, now_iso(), mid),
    )

def supersede_memory(
    conn: sqlite3.Connection, mid: str, reason: str = "", *, at: str | None = None
) -> bool:
    """Tombstone a memory (mark superseded_at). Does not delete — keeps history.

    `at` overrides the superseded_at timestamp (default: now). ingest-jsonl
    uses this to apply a remote tombstone with the ORIGINATING store's
    superseded_at, not the local ingest time — otherwise two synced copies of
    the same tombstone would disagree on when it happened.
    """
    started_tx = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        row = conn.execute("SELECT id FROM memory WHERE id=?", (mid,)).fetchone()
        if not row:
            print(f"[zmem] no memory with id {mid}", file=sys.stderr)
            if started_tx and conn.in_transaction:
                conn.rollback()
            return False
        conn.execute("UPDATE memory SET superseded_at=?, supersede_reason=? WHERE id=?",
                     (at or now_iso(), reason, mid))
        # Also remove from the vec0 table to prevent orphaned vectors consuming KNN slots.
        try:
            conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (mid,))
        except sqlite3.OperationalError:
            pass  # vec0 table not available
        if started_tx:
            _commit(conn)
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise
    # The confirmation print interpolates `reason`, which on the ingest-jsonl
    # path is REMOTE-AUTHORED. stdout is strict under a legacy codepage
    # (PYTHONIOENCODING=cp1252 and friends), so a non-representable character
    # in `reason` would raise HERE -- after the commit -- and ingest-jsonl's
    # per-row guard would then count a supersession that actually landed as
    # `malformed`. A cosmetic status line must never be able to misreport a
    # durable write, so its failure is swallowed and retried in ASCII.
    note = f": {reason}" if reason else ""
    try:
        print(f"[zmem] superseded {mid}{note}")
    except UnicodeEncodeError:
        safe_note = note.encode("ascii", "replace").decode("ascii")
        print(f"[zmem] superseded {mid}{safe_note}")
    return True
