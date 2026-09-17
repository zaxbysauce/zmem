"""Deterministic ambiguity classification and passive-query rewriting.

This module deliberately has no model, clock, network, or write dependency.
The classifier and rewriter are pure; the one store helper is a bounded,
read-only lookup of recent edit evidence used by a caller that already owns
the store process boundary.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Iterable

from storelib.recall import QUERY_STOPWORDS


AMBIG_MIN_TERMS_DEFAULT = 4
AMBIG_MIN_TERMS_ENV = "ZMEM_AMBIG_MIN_TERMS"
REWRITE_MAX_CHARS = 500
OPS_CONTEXT_LIMIT = 12
EDIT_BASENAME_LIMIT = 3
REWRITE_CONTEXT_MAX_CHARS = 150
_PROMPT_INPUT_MAX_CHARS = 4096

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ERROR_IDENTIFIER_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*)?(?:Error|Exception)$"
)
_SAFE_OP_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
_TERM_EDGE_PUNCT = "\"'`;,:(){}[]<>|&$!?*+=\u201c\u201d\u2018\u2019\u201a\u201e\u2013\u2014\u2026"


def _fallback_min_terms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return AMBIG_MIN_TERMS_DEFAULT
    return value


def _minimum_terms(value: int | None) -> int:
    if value is not None:
        return _fallback_min_terms(value)
    raw = os.environ.get(AMBIG_MIN_TERMS_ENV)
    if raw is None or raw == "":
        return AMBIG_MIN_TERMS_DEFAULT
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return AMBIG_MIN_TERMS_DEFAULT
    return _fallback_min_terms(parsed)


def _prompt_tokens(prompt: str) -> list[str] | None:
    if (
        not isinstance(prompt, str)
        or len(prompt) > _PROMPT_INPUT_MAX_CHARS
        or _CONTROL_RE.search(prompt)
    ):
        return None
    tokens = prompt.strip().split()
    return tokens


def _has_exact_token(tokens: Iterable[str]) -> bool:
    for token in tokens:
        # Namespace anchors are meaningful even when they occur at a token
        # edge; do this check before punctuation trimming because ':' is also
        # an edge character for the other exact-token forms.
        if "::" in token:
            return True
        candidate = token.strip(_TERM_EDGE_PUNCT)
        if (
            candidate.startswith("-")
            or any(marker in candidate for marker in ("/", "\\", ".", "_"))
            or _ERROR_IDENTIFIER_RE.fullmatch(candidate) is not None
        ):
            return True
    return False


def _content_terms(tokens: Iterable[str]) -> set[str]:
    terms: set[str] = set()
    for token in tokens:
        normalized = token.strip(_TERM_EDGE_PUNCT).casefold()
        if not normalized or normalized in QUERY_STOPWORDS:
            continue
        terms.add(normalized)
    return terms


def is_ambiguous_prompt(prompt: str, *, min_terms: int | None = None) -> bool:
    """Return whether a prompt lacks a strong exact retrieval anchor."""
    tokens = _prompt_tokens(prompt)
    if tokens is None:
        return False
    if _has_exact_token(tokens):
        return False
    return len(_content_terms(tokens)) < _minimum_terms(min_terms)


def _safe_basename(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > REWRITE_MAX_CHARS:
        return ""
    if _CONTROL_RE.search(value) or "/" in value or "\\" in value:
        return ""
    if value in {".", ".."} or value.strip() != value:
        return ""
    return value


def read_recent_edit_basenames(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    limit: int = EDIT_BASENAME_LIMIT,
    strict_errors: bool = False,
) -> list[str]:
    """Read newest distinct safe edit basenames without mutating ``conn``.

    The historical default fails open on unavailable/malformed evidence.  A
    strict caller such as the store-owned rewrite boundary may request the
    original SQLite error so it can emit its own sanitized diagnostic while
    still returning the original prompt.
    """
    if not isinstance(session_id, str) or not session_id:
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    limit = min(limit, EDIT_BASENAME_LIMIT)
    try:
        rows = conn.execute(
            "SELECT ref_path FROM evidence "
            "WHERE session_id=? AND kind='edit' "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    except sqlite3.Error:
        if strict_errors:
            raise
        return []
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        try:
            ref_path = row[0]
        except (IndexError, KeyError, TypeError):
            continue
        basename = _safe_basename(
            re.split(r"[\\/]", ref_path)[-1] if isinstance(ref_path, str) else None
        )
        key = basename.casefold()
        if not basename or key in seen:
            continue
        seen.add(key)
        result.append(basename)
        if len(result) >= limit:
            break
    return result


def _safe_context_token(value: object, *, basename: bool) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if len(value) > REWRITE_CONTEXT_MAX_CHARS or _CONTROL_RE.search(value):
        return ""
    if basename:
        return _safe_basename(value)
    token = value.strip()
    if not token or not _SAFE_OP_TOKEN_RE.fullmatch(token):
        return ""
    return token


def _dedupe_context(
    ops_tokens: object, edited_basenames: object,
) -> list[str]:
    if not isinstance(ops_tokens, list) or not isinstance(edited_basenames, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value, basename in (
        *((value, False) for value in ops_tokens[:OPS_CONTEXT_LIMIT]),
        *((value, True) for value in edited_basenames[:EDIT_BASENAME_LIMIT]),
    ):
        token = _safe_context_token(value, basename=basename)
        key = token.casefold()
        if token and key not in seen:
            seen.add(key)
            result.append(token)
    bounded: list[str] = []
    used = 0
    for token in result:
        needed = len(token) + (1 if bounded else 0)
        if used + needed > REWRITE_CONTEXT_MAX_CHARS:
            continue
        bounded.append(token)
        used += needed
    return bounded


def rewrite_ambiguous_query(
    prompt: str,
    *,
    ops_tokens: list[str],
    edited_basenames: list[str],
    min_terms: int | None = None,
) -> tuple[str, bool]:
    """Add bounded deterministic context to an ambiguous prompt."""
    try:
        if not isinstance(prompt, str):
            return "", False
        base = prompt.strip()[:REWRITE_MAX_CHARS]
        context = _dedupe_context(ops_tokens, edited_basenames)
        if not is_ambiguous_prompt(prompt, min_terms=min_terms) or not context:
            return base, False
        context_text = " ".join(context)
        prompt_budget = max(0, REWRITE_MAX_CHARS - len(context_text) - 1)
        rewritten = (base[:prompt_budget].rstrip() + " " + context_text).strip()
        return rewritten[:REWRITE_MAX_CHARS], True
    except Exception:
        try:
            return prompt.strip()[:REWRITE_MAX_CHARS], False
        except Exception:
            return "", False
