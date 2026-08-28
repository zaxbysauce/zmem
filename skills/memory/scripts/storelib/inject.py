"""Injection shaping helpers shared by hooks, Hermes, and the MCP server (issue #65, 10.9).

Deliberately dependency-free (stdlib only, plus a best-effort schema_meta import
for the protected-type literals): this module is loaded four different ways —
``import storelib.inject`` from the hooks body, ``importlib`` file-location load
from ``mcp_server.py`` (which never imports store.py in-process), and a plain
import inside store.py itself. Anything heavier than stdlib would break one of
those paths.

Token accounting uses the documented 4-chars-per-token heuristic (no tokenizer
is in-tree). Budget admission control charges each row its content tokens plus
``FENCE_OVERHEAD_TOKENS`` to approximate the fence's provenance lines; callers
REPORT ``tokens_used`` measured on the final rendered fenced text, so the
reported number is honest even when the estimate under- or over-counts the
render. The existing ``ZMEM_CTX_BUDGET`` character truncation in the hooks
stays as the hard outer stop — the token budget stops adding bullets, the
character budget can still cut the tail.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

# Best-effort single-source-of-truth for the protected type literals; the
# fallbacks keep this module importable with no schema_meta on sys.path.
try:  # pragma: no cover - trivial import guard
    import schema_meta as _schema_meta  # type: ignore

    _PROTECTED_TYPES = tuple(
        getattr(_schema_meta, "PROTECTED_INJECT_TYPES", ("decision", "constraint"))
    )
except Exception:  # noqa: BLE001 - partially-deployed tree: use the literals
    _schema_meta = None
    _PROTECTED_TYPES = ("decision", "constraint")

DEFAULT_INJECT_TOKEN_BUDGET = 1500
INJECT_TOKEN_BUDGET_ENV = "ZMEM_INJECT_TOKEN_BUDGET"
# Documented approximation of the fence's per-row provenance lines
# (id/signal/ns/type/conf + source_ref) in tokens at 4 chars/token.
FENCE_OVERHEAD_TOKENS = 12
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate tokens with the documented 4-chars/token heuristic."""
    return max(0, len(text or "")) // CHARS_PER_TOKEN


def row_token_cost(row: dict[str, Any]) -> int:
    """Admission-control token cost of one recall row (content + fence overhead)."""
    content = row.get("content", "") or ""
    return estimate_tokens(content) + FENCE_OVERHEAD_TOKENS


def inject_token_budget() -> int:
    """Resolve ``ZMEM_INJECT_TOKEN_BUDGET`` (default 1500).

    Garbage, zero, or negative values fall back to the default — a budget knob
    must never crash a hook (fail-open), and 0 would otherwise admit nothing.
    """
    raw = os.environ.get(INJECT_TOKEN_BUDGET_ENV, "")
    try:
        value = int(raw) if raw else DEFAULT_INJECT_TOKEN_BUDGET
    except ValueError:
        return DEFAULT_INJECT_TOKEN_BUDGET
    return value if value > 0 else DEFAULT_INJECT_TOKEN_BUDGET


def _row_priority(row: dict[str, Any], index: int) -> Tuple[float, int, int]:
    """Sort key for admission: higher score first; ``signal=none`` last within
    equal scores; stable on the caller's original order."""
    try:
        score = float(row.get("_score", row.get("confidence", 0)) or 0)
    except (TypeError, ValueError):
        score = 0.0
    # NaN/inf sort keys compare unreliably — treat as no signal (RB-010).
    if score != score or score in (float("inf"), float("-inf")):
        score = 0.0
    none_last = 1 if (row.get("signal") or "none") == "none" else 0
    return (-score, none_last, index)


def apply_token_budget(
    rows: list[dict[str, Any]], budget: Optional[int] = None
) -> Tuple[list[dict[str, Any]], int, int]:
    """Admit rows under ``budget`` tokens (issue #65, 10.9).

    Policy: ``decision``/``constraint`` rows are PROTECTED — never dropped to
    stay under budget, and kept even when they alone exceed it (once only they
    remain, budget enforcement stops). Everything else is admitted in
    descending score order (``signal=none`` after grounded rows at the same
    score) until the next row would exceed the budget. Admission stops there;
    already-admitted rows are never evicted to fit a later row.

    Returns ``(kept, tokens_estimate, dropped)``. ``kept`` preserves the
    caller's original row order (admission decides membership, not order) so
    the fence render stays score-ranked. ``tokens_estimate`` is the sum of
    admission costs of the kept rows — callers report ``tokens_used`` measured
    on their final rendered text instead.
    """
    if budget is None:
        budget = inject_token_budget()
    protected_ids = set()
    normal: list[Tuple[Tuple[float, int, int], int]] = []
    for i, row in enumerate(rows):
        if (row.get("type") or "") in _PROTECTED_TYPES:
            protected_ids.add(i)
        else:
            normal.append((_row_priority(row, i), i))
    normal.sort(key=lambda pair: pair[0])

    admitted = set(protected_ids)
    used = sum(row_token_cost(rows[i]) for i in protected_ids)
    for _key, i in normal:
        cost = row_token_cost(rows[i])
        if used + cost > budget:
            break
        admitted.add(i)
        used += cost

    kept = [row for i, row in enumerate(rows) if i in admitted]
    return kept, used, len(rows) - len(kept)


def envelope_results(parsed: Any) -> list:
    """Normalize a parsed ``recall/recent/search --json`` payload to a row list.

    v13 (issue #65, 10.8) emits an envelope dict ``{"results": [...], ...}``;
    pre-v13 stores and partially-upgraded trees emit a bare list. Both shapes
    must keep working everywhere a hook or provider consumes the JSON, so this
    one helper is THE shim — it is reused by the hooks body, session-start,
    Hermes prefetch/_tool_search, and the MCP server rather than forked.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        results = parsed.get("results", [])
        return results if isinstance(results, list) else []
    return []
