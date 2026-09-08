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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def _gate_constants() -> Tuple[float, float, frozenset]:
    """Floors + grounded set, single-sourced from schema_meta (PRR-017).

    Literals mirror the schema_meta defaults so a partially-deployed tree
    (schema_meta unreachable) keeps the documented gate.
    """
    floor = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_PROMPT_ENV", "ZMEM_INJECT_FLOOR_PROMPT"),
        getattr(_schema_meta, "INJECT_FLOOR_PROMPT_DEFAULT", 0.25),
    )
    gate_none_floor = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_GATE_NONE_ENV",
               "ZMEM_INJECT_FLOOR_GATE_NONE"),
        getattr(_schema_meta, "INJECT_FLOOR_GATE_NONE_DEFAULT", 0.4),
    )
    grounded = getattr(
        _schema_meta, "INJECT_GROUNDED_SIGNALS",
        frozenset({"test", "compile", "lint", "reviewer", "user"}),
    )
    return floor, gate_none_floor, frozenset(grounded)


def _lane_floors() -> Tuple[float, float, float]:
    """Per-lane relevance floors (issue #113), single-sourced from schema_meta.

    A row's PRESENT lane value must clear its own floor; an ABSENT lane
    (None) is exempt. Literals mirror the schema_meta defaults so a
    partially-deployed tree keeps the documented gate.
    """
    lex = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_LEX_ENV", "ZMEM_INJECT_FLOOR_LEX"),
        getattr(_schema_meta, "INJECT_FLOOR_LEX_DEFAULT", 0.30),
    )
    cos = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_COS_ENV", "ZMEM_INJECT_FLOOR_COS"),
        getattr(_schema_meta, "INJECT_FLOOR_COS_DEFAULT", 0.50),
    )
    ent = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_ENT_ENV", "ZMEM_INJECT_FLOOR_ENT"),
        getattr(_schema_meta, "INJECT_FLOOR_ENT_DEFAULT", 0.5),
    )
    # A negative floor would trivially clear every measured lane (relevance
    # values are >= 0), i.e. silently disable the gate. Treat it as operator
    # error and clamp to 0.0 (the honest "disable this lane" value).
    return max(0.0, lex), max(0.0, cos), max(0.0, ent)


def selective_inject_filter(
    rows: list[dict[str, Any]],
    floor: Optional[float] = None,
    gate_none_floor: Optional[float] = None,
    grounded_signals: Optional[frozenset] = None,
    *,
    lane_floors: Optional[Tuple[float, float, float]] = None,
    with_stats: bool = False,
) -> Any:
    """Store-side twin of the hook selective-inject gate (issue #58, 3.8; #114).

    Issue spec: tighten ONLY ``signal=none`` (the agent's self-opinion) to
    ``gate_none_floor`` (default 0.4). Every GROUNDED signal
    (test/compile/lint/reviewer/user) passes at the prompt floor (default
    0.25). Semantics are byte-identical to the hook body's
    ``_selective_inject_filter`` so the ``--for-injection`` lane (issue #114)
    applies the same decision the hook used to apply after the subprocess
    returned — one gate, one source of truth, counted before the write.

    Issue #113: rows may also carry per-lane relevance values
    (``_rel_lex`` / ``_rel_cos`` / ``_rel_ent``). The floors are DISJUNCTIVE
    across lanes: a trusted row is admitted when ANY MEASURED lane clears its
    own floor (default 0.30 / 0.50 / 0.50); a measured-but-failing lane does
    not veto a row another lane admits. An ABSENT lane (the key missing or
    None — query-less surfaces, link-expansion rows, model-absent cosine) is
    exempt: it is neither measured nor judged. If at least one lane was
    measured and NO measured lane clears its floor, the row is not relevant
    enough and counts in ``relevance_failed``. The trust gate is NOT
    replaced: a row must pass BOTH the trust conditions AND the relevance
    disjunction.

    Returns ``(selected, status)`` where status is ``"injected"`` (anything
    qualified) or ``"silent"`` (nothing passed). With ``with_stats=True``
    returns ``(selected, status, stats)`` where stats is
    ``{"trust_passed": int, "relevance_failed": int, "trust_failed": int}`` —
    the inputs the silent-reason classifier needs to separate
    ``below-relevance`` ("nothing relevant") from ``below-bar`` ("nothing
    trusted"). The default branch stays a 2-tuple.
    """
    if floor is None or gate_none_floor is None or grounded_signals is None:
        c_floor, c_gate_none, c_grounded = _gate_constants()
        floor = c_floor if floor is None else floor
        gate_none_floor = c_gate_none if gate_none_floor is None else gate_none_floor
        grounded_signals = (c_grounded if grounded_signals is None
                            else frozenset(grounded_signals))
    if lane_floors is None:
        lane_floors = _lane_floors()
    selected = []
    trust_passed = 0
    relevance_failed = 0
    trust_failed = 0
    for r in rows:
        try:
            conf = float(r.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        # Review round (PRR-009): NaN/inf confidence must fail the gate like
        # _row_priority normalizes them — inf would otherwise compare >= any
        # floor. Same normalization as inject._row_priority.
        if conf != conf or conf in (float("inf"), float("-inf")):
            conf = 0.0
        sig = (r.get("signal") or "none").lower()
        if sig == "none":
            trusted = conf >= gate_none_floor
        elif sig in grounded_signals:
            trusted = conf >= floor
        else:
            trusted = False
        if not trusted:
            trust_failed += 1
            continue
        trust_passed += 1
        # Relevance lanes (issue #113): the row passes the relevance gate iff
        # ANY measured lane clears ITS OWN floor — a lane strong on one
        # signal is relevant regardless of a weaker second signal, and a row
        # with NO measured lane (all absent) is exempt (query-less surfaces,
        # link-expansion rows). A measured-but-failing lane (e.g. lex 0.0
        # for a single-generic-token row) simply does not clear its floor;
        # if NO lane clears, the row is not relevant enough to inject.
        lane_ok = False
        measured = False
        for key, fl in (("_rel_lex", lane_floors[0]),
                        ("_rel_cos", lane_floors[1]),
                        ("_rel_ent", lane_floors[2])):
            val = r.get(key)
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                val = 0.0
            if val != val or val in (float("inf"), float("-inf")):
                val = 0.0
            measured = True
            if val >= fl:
                lane_ok = True
                break
        if lane_ok or not measured:
            selected.append(r)
        else:
            relevance_failed += 1
    status = "injected" if selected else "silent"
    if with_stats:
        stats = {"trust_passed": trust_passed,
                 "relevance_failed": relevance_failed,
                 "trust_failed": trust_failed}
        return selected, status, stats
    return selected, status


def classify_silent_reason(rows: list, omitted: int = 0,
                           budget_emptied: bool = False,
                           lane_stats: Optional[dict] = None) -> str:
    """Name WHY a silent inject is silent (issue #87; store-side twin).

    Same precedence as the hook body's classifier: budget-drop wins over
    below-bar (a budget wipe of a gate-passed set is a budget fact, not a
    gate fact); empty rows with omitted==0 is empty-pool even if the prompt
    was long — do not guess. The closed set comes from schema_meta
    (INJECT_SILENT_REASONS); a drift/unknown value degrades to empty-pool
    rather than inventing a reason.

    Issue #113: when the caller passes the gate's ``lane_stats`` (the
    ``with_stats=True`` third element), a silent decision where at least one
    row passed the TRUST gate but every trust-passing row failed a relevance
    floor names ``below-relevance`` ("nothing relevant") instead of
    ``below-bar`` ("nothing trusted"). Without lane_stats the legacy
    behavior is byte-identical.
    """
    allowed = tuple(getattr(
        _schema_meta, "INJECT_SILENT_REASONS",
        ("empty-pool", "omitted", "below-bar", "budget-drop",
         "below-relevance"),
    ))
    if budget_emptied:
        reason = "budget-drop"
    elif rows and lane_stats is not None \
            and lane_stats.get("trust_passed", 0) >= 1:
        reason = "below-relevance"
    elif rows:
        reason = "below-bar"
    elif omitted > 0:
        reason = "omitted"
    else:
        reason = "empty-pool"
    if reason not in allowed:
        return "empty-pool"
    return reason
