"""Injection shaping and passive selector helpers (issues #65 and #158).

The budgeting and filtering primitives remain importable without the recall
module.  The session-aware selector imports recall, the delivery ledger, and
the operation-token helpers lazily at call time; this preserves the existing
``recall -> inject`` import direction while giving passive consumers one
store-owned selection/render/delivery seam.

Token accounting uses the documented 4-chars-per-token heuristic (no tokenizer
is in-tree) for BOTH admission and reporting — one estimator, so the two can
never diverge silently (issue #116). Budget admission control charges each row
its FULL fence-render contribution (``fence_row_cost``: the exact bullet block
``_format_fenced_recall`` would emit, modeled dependency-free here) plus a
fixed ``FENCE_SHELL_ALLOWANCE`` for the fence shell, and enforces the budget
as a HARD CEILING: the final rendered fence never exceeds it. Protected
``decision``/``constraint`` rows are truncated to fit with an explicit
``…[budget-truncated]`` marker (never admitted whole over budget, never
silently dropped — the drop counts ride the envelope and the decision log).
The admission scan CONTINUES past a row that does not fit, so a later smaller
high-value row is not lost. Callers REPORT ``tokens_used`` measured on the
final rendered fenced text, now guaranteed <= budget by construction. The
existing ``ZMEM_CTX_BUDGET`` character truncation in the hooks stays as the
hard outer stop — the token budget stops adding bullets, the character budget
can still cut the tail.
"""

from __future__ import annotations

import inspect
import math
import os
import re
from typing import Any, Optional, Tuple


# Issue #158: the selector's attribution contract is deliberately closed.  A
# compact delivery is represented by the ordinary ``session_start`` moment;
# ``session_start_compact`` remains a decision-log label only.
INJECTION_MOMENTS = (
    "session_start", "user_prompt", "pretool", "subagent", "precompact",
)
INJECTION_LANES = (
    "claude", "codex", "zcode", "deepseek",
    "hermes-provider", "hermes-compat",
)

INJECTION_ENVELOPE_REQUIRED = frozenset({
    "results", "count", "omitted", "reason", "excluded", "candidate_ids",
    "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
    "budget_truncated", "budget_dropped_protected", "arms", "rendered",
})
INJECTION_ENVELOPE_OPTIONAL = frozenset({
    "injection_risk", "candidate_lanes", "budget_note",
    # Issue #256: present only when a credential withhold happened.
    "secret_withheld",
    # Issue #135: capture-only operation metadata, enabled by host adapters.
    "effective_ops",
    # Issue #235: present only when the user:global tier floor withheld.
    "global_withheld",
    # Issue #174: present only when the live-expiry gate ENGAGED (the
    # passive lane saw at least one memory_expiry side row). Registering the
    # key here is mandatory, not cosmetic — the Hermes prefetch and
    # provider validators reject ANY envelope key outside required|optional,
    # so an unregistered key silently blackouts recall on every store that
    # adopts the expiry feature, including with `expired: 0`. Mirrored in
    # hermes-plugin/__init__.py's degraded fallback set.
    "expired",
    # Issue #284 (T-05): present only when the caller passed reserve=True —
    # the reservation id the caller acknowledges via `prefetch-ack` to
    # commit the delivery. Keyed-only-when-present keeps the legacy wire
    # unchanged; mirrored in hermes-plugin/__init__.py's fallback set.
    "reservation",
    # Issue #342: present only when at least one candidate was withheld
    # because the live-expiry side-table read FAILED with the table present
    # (lock contention, interruption) — the count of rows whose expiry
    # state is unknown. Keyed-only-when-present keeps the legacy wire
    # unchanged; mirrored in hermes-plugin/__init__.py's fallback set.
    "expiry_read_failed",
})

def _capture_envelope_enabled() -> bool:
    """Expose capture-only delivery metadata for host adapter subprocesses.

    The launcher sets ``ZMEM_CAPTURE`` in its child environment. Keeping the
    field opt-in preserves the established CLI wire shape for direct callers
    while allowing automatic host capture to receive selected operation tokens.
    """
    return "ZMEM_CAPTURE" in os.environ and os.environ.get("ZMEM_CAPTURE", "").strip() != "0"


# Best-effort single-source-of-truth for the protected type literals; the
# fallbacks keep this module importable with no schema_meta on sys.path.
try:  # pragma: no cover - trivial import guard
    import schema_meta as _schema_meta  # type: ignore

    _PROTECTED_TYPES = tuple(
        getattr(_schema_meta, "PROTECTED_INJECT_TYPES", ("decision", "constraint"))
    )
    # Keep the fallback values byte-identical to schema_meta.  This module is
    # also loaded directly by partially-deployed hook/Hermes surfaces where
    # schema_meta may be unavailable or from an older release.
    INJECT_LANES = tuple(getattr(
        _schema_meta, "INJECT_LANES",
        ("claude", "codex", "zcode", "deepseek",
         "hermes-provider", "hermes-compat"),
    ))
    INJECT_MOMENTS = tuple(getattr(
        _schema_meta, "INJECT_MOMENTS",
        ("session_start", "user_prompt", "pretool", "subagent", "precompact"),
    ))
    INJECT_SILENT_REASONS = tuple(getattr(
        _schema_meta, "INJECT_SILENT_REASONS",
        ("empty-pool", "omitted", "below-bar", "budget-drop",
         "below-relevance", "already-delivered", "expired",
         "hub-unreachable", "snapshot-expired"),
    ))
except Exception:  # noqa: BLE001 - partially-deployed tree: use the literals
    _schema_meta = None
    _PROTECTED_TYPES = ("decision", "constraint")
    INJECT_LANES = ("claude", "codex", "zcode", "deepseek",
                    "hermes-provider", "hermes-compat")
    INJECT_MOMENTS = ("session_start", "user_prompt", "pretool", "subagent",
                      "precompact")
    INJECT_SILENT_REASONS = (
        "empty-pool", "omitted", "below-bar", "budget-drop", "below-relevance",
        "already-delivered", "expired", "hub-unreachable", "snapshot-expired",
    )

DEFAULT_INJECT_TOKEN_BUDGET = 1500
INJECT_TOKEN_BUDGET_ENV = "ZMEM_INJECT_TOKEN_BUDGET"
# Documented approximation of the fence's per-row provenance lines
# (id/signal/ns/type/conf + source_ref) in tokens at 4 chars/token.
# Legacy cost model kept for episodes.py episode token_count and its
# characterization pin — admission no longer uses it (issue #116).
FENCE_OVERHEAD_TOKENS = 12
CHARS_PER_TOKEN = 4
# Fixed reservation for everything the fence renders AROUND the rows
# (issue #116): open line + "# " + header + disclaimer + blank + the
# optional budget-note line + close + newline separators. Arithmetic at
# CHARS_PER_TOKEN=4: header capped by the renderer at 240 chars (60 tok)
# + shell lines ~136 chars (34 tok) + budget note <= ~80 chars (20 tok)
# + rounding/newline margin (~14) = 128. Reserved up front so the note
# line never pushes the render past the ceiling.
FENCE_SHELL_ALLOWANCE = 128
# Marker appended to truncated protected-row content (issue #116 scope 2:
# truncate with an explicit marker rather than admit whole).
TRUNCATION_MARKER = " …[budget-truncated]"
# A truncated protected row must keep at least this many characters of
# content (8 tokens) — below that the row is dropped and counted in
# stats["dropped_protected"] instead of admitted as marker-only noise.
MIN_STUB_CHARS = 32


def estimate_tokens(text: str) -> int:
    """Estimate tokens with the documented 4-chars/token heuristic."""
    return max(0, len(text or "")) // CHARS_PER_TOKEN


def row_token_cost(row: dict[str, Any]) -> int:
    """Legacy admission-control cost (content + flat fence overhead).

    Kept for episodes.py episode token_count and its characterization pin.
    Budget admission uses ``fence_row_cost`` since issue #116.
    """
    content = row.get("content", "") or ""
    return estimate_tokens(content) + FENCE_OVERHEAD_TOKENS


def _row_markers(row: dict[str, Any]) -> list:
    """Marker prefixes the fence renderer would put on this row's bullet."""
    markers = []
    if row.get("unfold_hop"):
        markers.append("[PREVIOUSLY]")
    if row.get("prompt_injection_risk"):
        markers.append("[INJECTION RISK]")
    taint = row.get("taint")
    if taint == "untrusted_tool":
        markers.append("[UNTRUSTED TOOL]")
    elif taint == "untrusted_web":
        markers.append("[UNTRUSTED WEB]")
    if row.get("contested_link"):
        # Issue #254: shared with recall._format_fenced_recall so the
        # admission charge and the rendered bytes cannot drift.
        markers.append(contested_marker(row))
    if row.get("withheld_for_secret"):
        markers.append("[WITHHELD: SECRET]")
    return markers


# Characters that would break the marker's closed single-token shape
# (delivery_ledger.rows_present_in's `CONTESTED LINK(?:: [^\]\s]+)?` arm)
# or the fence's bracket/delimiter grammar. An id carrying any of them
# renders the bare marker instead, so a malformed id can never desync a
# rendered row out of ledger accounting (the #250 re-delivery harm class).
_MARKER_UNSAFE_CHARS = " \t\r\n]<>"

# One-shot flag for the #307 release-block fail-open warning (maintainer
# review F-03: the degraded mode must not be indistinguishable from
# feature-off). Module-level so the nested selector can `global` it.
_CONTESTED_RELEASE_WARNED = False


def contested_marker(row: dict[str, Any]) -> str:
    """The contested-link marker for a row, naming the opposing row.

    Issue #254: the named form carries the row's own `link_of` — no lookup.
    The id is rendered only when it is a single token free of the unsafe
    characters above; anything else (missing, empty, non-string, spaced,
    bracketed) renders the bare `[CONTESTED LINK]`. The renderer
    (recall._format_fenced_recall) and this mirror call this one function,
    so the two sites are byte-identical by construction.
    """
    opponent = row.get("link_of")
    if not opponent:
        return "[CONTESTED LINK]"
    token = opponent if isinstance(opponent, str) else str(opponent)
    if not token or any(c in _MARKER_UNSAFE_CHARS for c in token):
        return "[CONTESTED LINK]"
    return "[CONTESTED LINK: %s]" % token


def _indent_continuations(text: Any, indent: str = "    ") -> str:
    """Render stored free-form text so no CONTINUATION line starts at
    column 0 (issue #352 AC1).

    The delivery ledger's ``rows_present_in`` bullet grammar
    (delivery_ledger.py) honours column-0 ``- [<id>]`` lines, and the fence
    renders stored ``content``/``source_ref``/``tags`` — so a stored value
    containing a newline followed by a renderer-shaped bullet forged delivery
    credit for a row that never rendered (executed probe, PR #350 review;
    re-executed at base 7b9d871). Prefixing every continuation line with the
    field's first-line indent closes the class for the fields this helper
    covers. SCOPE (review round, maintainer F-08): the claim covers the
    content/source_ref/tags call sites — header fields, page-provenance
    lines, and entity names are interpolated elsewhere and are bounded
    upstream (write-path namespace/enum validation; sync namespace validation
    since this PR's review round) for rows admitted through the validated
    paths; legacy rows predating that validation, and the
    import-store/dataset import paths, which do not run the control-char
    check, remain a disclosed residual (issue #443). Single-line
    behaviour is CALL-SITE
    dependent: the canonical content call site already supplies the first
    line's indent via this prefix (byte-identical to the pre-PR
    ``"    {content}"`` form); a caller that renders the first line inline
    must not route it through here (the Hermes degraded twin uses its own
    continuations-only helper). ``fence_row_cost`` applies this same
    transformation before ``estimate_tokens`` so admission accounting cannot
    diverge from the rendered bytes.
    """
    return "\n".join(indent + part for part in str(text or "").split("\n"))


def fence_row_cost(row: dict[str, Any], *,
                   legacy_injection_wire: bool = False) -> int:
    """Admission-control token cost of one row's FULL fence contribution
    (issue #116).

    Models the exact block ``storelib.recall._format_fenced_recall`` renders
    for the row — marker-prefixed bullet header (id/conf/signal/ns/type,
    ``_stale_note`` suffix), the indented content line, and the optional
    source_ref/tags/entities lines — measured with the SAME
    ``estimate_tokens`` the callers report on the final render, so admission
    and reporting can never diverge silently. Row values are charged exactly
    as carried (a long source_ref self-pays); the +2 margin covers only
    join/newline arithmetic variance. Input contract: dict-like rows
    (missing keys cost as empty, matching the renderer's ``.get``-style
    defaults); the +2 margin is what the renderer's line separators cost.
    ``legacy_injection_wire`` mirrors the immutable #183 passive-wire mode:
    tierless rows omit the generic ``[tier=unknown]`` prefix there.
    """
    markers = _row_markers(row)
    inj_prefix = (" " + " ".join(markers)) if markers else ""
    tier = row.get("tier")
    if tier in ("project", "domain", "fleet_host", "cross_project",
                "user_global"):
        tier_prefix = "[tier={}] ".format(tier)
        tier_suffix = ""
    elif tier == "cross":
        tier_prefix = ""
        tier_suffix = " [tier=cross]"
    elif legacy_injection_wire and (tier is None or tier == ""):
        tier_prefix = ""
        tier_suffix = ""
    else:
        tier_prefix = "[tier=unknown] "
        tier_suffix = ""
    header = (
        "{}- {}[{}] [conf={}] [signal={}] [ns={}]{} [type={}]{}".format(
            inj_prefix,
            tier_prefix,
            row.get("id", ""),
            row.get("confidence", ""),
            row.get("signal", ""),
            row.get("namespace", ""),
            tier_suffix,
            row.get("type", ""),
            row.get("_stale_note", "") or "",
        )
    )
    lines = [header]
    # Credential-withheld rows render only their marked id/type bullet.  Keep
    # admission cost exact so a marker never becomes unbudgeted fence text.
    # Issue #352 AC1: content/source_ref/tags continuation lines carry the
    # same 4-space indent as their first line in the renderer, and admission
    # measures the SAME indented bytes (a fixed margin cannot absorb
    # unbounded continuations — plan-critic round 1, blocker 5).
    if not row.get("withheld_for_secret"):
        lines.append(_indent_continuations(row.get("content", "") or ""))
    if not row.get("withheld_for_secret") and row.get("source_ref"):
        # First line rides after the "source_ref: " label; continuation lines
        # keep the 4-space indent (the [4:] strips only the first line's
        # prefix, which the label line already provides).
        lines.append("    source_ref: "
                     + _indent_continuations(row["source_ref"])[4:])
    # Page provenance is intentionally a narrow, page-only extension of the
    # common fence.  Ordinary rows retain their historical bytes.
    if row.get("type") == "page" and not row.get("withheld_for_secret"):
        for key in ("version_id", "freshness_watermark", "page_checksum"):
            if row.get(key):
                lines.append("    {}: {}".format(key, row[key]))
        for key in ("source_ids", "evidence_ids"):
            values = row.get(key)
            if isinstance(values, (list, tuple)) and values:
                lines.append("    {}: {}".format(key, ", ".join(map(str, values))))
    if not row.get("withheld_for_secret") and row.get("tags"):
        lines.append("    tags: " + _indent_continuations(row["tags"])[4:])
    ents = row.get("entities") or []
    if not row.get("withheld_for_secret") and ents:
        lines.append(
            "    entities: " + ", ".join(
                e.get("name", "?") for e in ents[:3]
            )
        )
    # "\n".join separators + the newline that separates the block from the
    # next row in the rendered fence (+2 margin).
    return estimate_tokens("\n".join(lines)) + 1 + 1


def budget_note(stats: dict[str, Any]) -> str:
    """Machine-readable omission marker for the rendered fence (issue #116
    addendum, funes byte-stable diagnostics): ``[budget: dropped N rows,
    truncated M]``. Empty string when nothing was omitted — the fence stays
    byte-identical whenever the budget did not bite."""
    dropped = int(stats.get("dropped", 0) or 0)
    truncated = int(stats.get("truncated", 0) or 0)
    if dropped <= 0 and truncated <= 0:
        return ""
    return "[budget: dropped {} rows, truncated {}]".format(dropped, truncated)


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


def inject_recent_floor() -> float:
    """Resolve the documented confidence floor for passive recent pulls.

    The session-aware selector is the store boundary for hook/SessionStart/
    Hermes recent delivery, so it must resolve this knob dynamically instead
    of relying on an adapter's hard-coded CLI default.
    """
    env_name = getattr(_schema_meta, "INJECT_FLOOR_RECENT_ENV",
                       "ZMEM_INJECT_FLOOR_RECENT")
    default = getattr(_schema_meta, "INJECT_FLOOR_RECENT_DEFAULT", 0.5)
    return _env_float(env_name, default)


def inject_score_margin() -> float:
    """Resolve the optional score-margin threshold (issue #182)."""
    env_name = getattr(_schema_meta, "INJECT_MARGIN_ENV", "ZMEM_INJECT_MARGIN")
    default = getattr(_schema_meta, "INJECT_MARGIN_DEFAULT", 0.0)
    return min(1.0, max(0.0, _env_float(env_name, default)))


def apply_score_margin(
    rows: list[dict[str, Any]], *, margin: float | None = None,
) -> tuple[list[dict[str, Any]], float | None, list[dict[str, Any]]]:
    """Apply the opt-in score-margin gate to a stable score-descending view.

    The caller supplies rows in score-descending order.  If the configured
    threshold is disabled or any candidate score is unusable, the helper fails
    open.  Otherwise it reports the relative margin rounded to six decimals for
    diagnostics, while comparing the unrounded value to the threshold.  When
    the raw margin is strictly below the threshold, only the top ordinary row
    is retained.  A leading ``decision`` or ``constraint`` protects the entire
    candidate set while still reporting the observed margin.
    """
    retained = list(rows)
    if len(retained) < 2:
        return retained, None, []

    if margin is None:
        threshold = inject_score_margin()
    else:
        try:
            threshold = float(margin)
        except (TypeError, ValueError, OverflowError):
            return retained, None, []
        if not math.isfinite(threshold):
            return retained, None, []
        threshold = max(0.0, min(1.0, threshold))
    if threshold <= 0.0:
        return retained, None, []

    try:
        scores = [float(row.get("_score")) for row in retained]
    except (AttributeError, TypeError, ValueError, OverflowError):
        return retained, None, []
    if any(not math.isfinite(score) for score in scores):
        return retained, None, []

    top, second = scores[0], scores[1]
    if top <= 0.0:
        return retained, None, []

    raw_margin = (top - second) / top
    if not math.isfinite(raw_margin):
        return retained, None, []
    observed = round(raw_margin, 6)
    if (
        raw_margin < threshold
        and retained[0].get("type") not in _PROTECTED_TYPES
        and retained[1].get("type") not in _PROTECTED_TYPES
    ):
        return [retained[0]], observed, retained[1:]
    return retained, observed, []


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


def _fence_row_cost_for_wire(
    row: dict[str, Any], *, legacy_injection_wire: bool,
) -> int:
    """Call the public seam while retaining old one-argument test doubles."""
    try:
        params = inspect.signature(fence_row_cost).parameters.values()
    except (TypeError, ValueError):
        # An opaque callable gets the current API shape. Never infer argument
        # compatibility from a TypeError message raised inside its body.
        return fence_row_cost(
            row, legacy_injection_wire=legacy_injection_wire)
    accepts_keyword = any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        or (param.name == "legacy_injection_wire"
            and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ))
        for param in params
    )
    if accepts_keyword:
        return fence_row_cost(
            row, legacy_injection_wire=legacy_injection_wire)
    return fence_row_cost(row)


def _truncate_protected_row(
    row: dict[str, Any], remaining: int, *,
    legacy_injection_wire: bool = True,
    cost_cap: Optional[int] = None,
    floor_ok: bool = False,
) -> Optional[Tuple[dict[str, Any], int]]:
    """Fit a protected row into ``remaining`` tokens by clipping its content
    (issue #116 scope 2). Returns ``(new_row, cost)`` with a SHALLOW COPY of
    the row (the caller's dict is never mutated) and an explicit
    ``…[budget-truncated]`` marker, or None when even a minimal stub does not
    fit. The copy is re-measured with the exact admission estimator, so the
    charge always covers what the renderer will emit."""
    if remaining < 1:
        return None
    content = row.get("content", "") or ""
    # Token cost of the row WITHOUT its content: header + optional lines.
    probe = dict(row)
    probe["content"] = ""
    overhead = _fence_row_cost_for_wire(
        probe, legacy_injection_wire=legacy_injection_wire)
    marker = TRUNCATION_MARKER
    # Characters of content that keep the whole block within `remaining`.
    # estimate_tokens = chars // 4, so allowed total chars for the block is
    # remaining * 4; subtract the marker and a safety margin, then floor.
    allowed = (remaining * CHARS_PER_TOKEN) - len(marker) \
        - (overhead * CHARS_PER_TOKEN) - CHARS_PER_TOKEN
    if cost_cap is not None:
        # Bound how much the STUB may claim (issue #501 follow-up,
        # PR #534 feedback FB-001): a clipped row is never acknowledged,
        # so it re-renders on every prompt - an uncapped stub would
        # swallow the whole remaining budget each time and starve the
        # rows that fit. The content slice is derived from the cap, so
        # the row is still TRUNCATED rather than silently dropped; when
        # the cap cannot carry even a minimal stub the row is dropped
        # and COUNTED (the caller gives the first clipped row the floor
        # of one minimal stub - issue #116).
        allowed = min(allowed,
                      (cost_cap - overhead) * CHARS_PER_TOKEN)
    if allowed < MIN_STUB_CHARS:
        # Closeout CS-002: the flat estimate above carries a one-token
        # safety margin, so at the EXACT measured minimal-stub cost it can
        # undershoot MIN_STUB_CHARS even though the measured stub fits.
        # The caller measured that floor as admissible (``floor_ok``), so
        # retry from the minimum instead of dropping the row (#116).
        if not floor_ok or remaining < 1:
            return None
        allowed = MIN_STUB_CHARS
    while allowed >= MIN_STUB_CHARS:
        clipped = row.copy()
        clipped["content"] = content[:allowed].rstrip() + marker
        # Issue #501 (audit D02): a clipped row is an INCOMPLETE delivery —
        # render it (the #116 truncated-not-dropped contract) but never let
        # the delivery ledger acknowledge it (the ledger filter at the
        # withheld_for_secret precedent drops this marker), so a same-session
        # retry with enough budget re-selects and delivers the COMPLETE row.
        clipped["_incomplete_delivery"] = True
        cost = _fence_row_cost_for_wire(
            clipped, legacy_injection_wire=legacy_injection_wire)
        if cost > remaining:
            # does not fit the remaining budget
            pass
        elif cost_cap is not None and cost > cost_cap:
            # The MEASURED cost exceeds the reserved clipped-stub budget:
            # shave more content (the cost is re-measured below) so the
            # reservation is enforced on the real cost, not on the
            # content slice alone.
            pass
        else:
            return clipped, cost
        # The flat chars/4 model above ignores the per-continuation-line
        # indent the renderer adds (issue #352 AC1): a clipped slice that
        # retains K newlines bills K-1 tokens over the flat estimate (the
        # safety margin absorbs one). Shave one token of content and
        # re-measure until the stub fits or nothing is left — a protected
        # row is truncated, never dropped, while it can still carry
        # information (apply_token_budget's documented policy).
        if (floor_ok and allowed > MIN_STUB_CHARS
                and allowed - CHARS_PER_TOKEN < MIN_STUB_CHARS):
            # Closeout CS2-001: the 4-char step would skip the MINIMAL
            # stub altogether (35 -> 31 never measures 32). The caller
            # measured that floor as admissible, so land exactly on it.
            allowed = MIN_STUB_CHARS
            continue
        allowed -= CHARS_PER_TOKEN
    # Fail-closed: even a minimal stub does not fit.
    return None


def _protected_row_min_cost(row: dict[str, Any], *,
                            legacy_injection_wire: bool = True) -> int:
    """The measured cost of the smallest stub a clipped protected row
    can carry (issue #116: a protected row is truncated, never dropped,
    while it can still carry information).

    The cost is MEASURED from the real minimal slice: the renderer
    indents every continuation line, so a flat chars/token estimate
    under-counts a multi-line row — the truncation loop then could not
    reach a stub within the cap and the row was dropped while room
    remained (PR #534 critic FB-002).
    """
    content = row.get("content", "") or ""
    probe = dict(row)
    probe["content"] = (content[:MIN_STUB_CHARS].rstrip()
                         + TRUNCATION_MARKER)
    probe["_incomplete_delivery"] = True
    return _fence_row_cost_for_wire(
        probe, legacy_injection_wire=legacy_injection_wire)


def apply_token_budget(
    rows: list[dict[str, Any]], budget: Optional[int] = None,
    *, with_stats: bool = False, legacy_injection_wire: bool = True,
    defer_ids: Optional[Iterable[str]] = None,
    _trace: Optional[list] = None,
) -> Any:
    """Admit rows under ``budget`` tokens as a MEASURED HARD CEILING
    (issue #116; original policy issue #65, 10.9).

    Policy: ``decision``/``constraint`` rows are PROTECTED — they are never
    dropped while they can still carry information: each is admitted whole
    when it fits the remaining budget, else TRUNCATED to fit with an explicit
    ``…[budget-truncated]`` marker on its content, and dropped (counted in
    ``stats["dropped_protected"]``, never silently) only when even a minimal
    stub does not fit. A clipped row is never acknowledged (issue #501), so
    it re-renders on every prompt: the FIRST clipped row's slice is
    therefore bounded by half the free budget (each further clipped row is
    held to a minimal stub, so the clipped aggregate is at most half the
    free budget plus one minimal stub per additional clipped row), and the
    clipped slice leaves room for the largest normal row, so a recurring
    clipped row cannot starve the rows that fit (issue #501 follow-up,
    PR #534). AT THE BOUNDARY - a protected stub and an individually
    deliverable normal row that cannot both fit in one prompt - the
    protected row keeps its stub on THAT prompt (issue #116 priority), so
    the normal row is skipped there. On a LATER prompt the selector records
    the clipped row, and a recorded row that still cannot be delivered whole
    YIELDS the budget (``defer_ids``) on BOTH dispatch lanes. The yield is
    KEPT only when it is a NET GAIN over the admission without it: every
    protected row that pass ADMITTED (whole or as a stub) is still admitted,
    and a normal row is admitted that the other pass did not admit. Otherwise
    the no-defer
    admission is used unchanged, so a yield can neither cascade into dropping
    another protected row nor leave a stub as the only trace that a row
    existed. A yield never guarantees WHICH normal row it frees space for.
    Everything else is admitted in descending
    score order
    (``signal=none`` after grounded rows at the same score); the scan
    CONTINUES past a row that does not fit, so a later smaller high-value row
    is still admitted. ``FENCE_SHELL_ALLOWANCE`` is reserved up front for the
    fence shell (open/close/header/disclaimer/optional budget-note line), so
    the FINAL RENDERED FENCE — measured with the same ``estimate_tokens`` —
    never exceeds the budget.

    Admission charges each row ``fence_row_cost`` (its full rendered fence
    contribution). Its default wire mode is the legacy passive lane; callers
    rendering generic or scoped fences can opt out. Returns
    ``(kept, tokens_estimate, dropped)`` where
    ``tokens_estimate`` is the admission accounting of the kept rows; kept
    preserves the caller's original row order (membership is decided by
    admission, order by the caller's score-ranked render). With
    ``with_stats=True`` returns ``(kept, tokens_estimate, dropped, stats)``
    where stats carries ``admission_used`` (= tokens_estimate), ``dropped``,
    ``truncated``, ``dropped_protected``, and ``budget`` — the inputs the
    envelope/log fields and ``budget_note`` render from.
    """
    if budget is None:
        budget = inject_token_budget()
    available = budget - FENCE_SHELL_ALLOWANCE
    protected_idx = []
    normal: list[Tuple[Tuple[float, int, int], int]] = []
    for i, row in enumerate(rows):
        if (row.get("type") or "") in _PROTECTED_TYPES:
            protected_idx.append(i)
        else:
            normal.append((_row_priority(row, i), i))
    normal.sort(key=lambda pair: pair[0])

    # The CLIPPED protected rows' aggregate budget is half of the free
    # budget (issue #501 follow-up): a clipped protected row is never
    # acknowledged, so it re-renders on every prompt, and an unbounded clip
    # would starve the rows that fit (head delivered 0 of 10 facts on 6
    # consecutive prompts where base delivered all 10 on prompt 2). The
    # clipped slice additionally leaves room for the largest NORMAL row
    # that could ever be admitted, so a recurring clipped row cannot take
    # the whole envelope (PR #534 feedback F-01).
    protected_cap = available // 2
    # Issue #501 follow-up (PR #534): the fair CROSS-PROMPT selection.
    # A protected row that was already rendered as a clipped stub in this
    # session, and that still cannot be delivered whole, YIELDS the budget
    # to the rows that fit - otherwise it re-claims the budget on every
    # prompt and the rows that fit are starved for the session (the F-01
    # boundary).
    #
    # Review FB-01401 / CD-001 (PR #534): the yield's validity is checked on
    # the RESULT, never predicted. Two earlier attempts failed here:
    # reserving space by a cost estimate (FB-003/FB-01202), and deciding
    # deferred rows last (FB-01301/FB-01401). The surviving rule compares
    # the two passes: a deferral survives only when it is a NET GAIN - every
    # protected row the no-defer pass ADMITTED (whole or as a stub) is still
    # admitted, AND at least one normal row is admitted that the no-defer
    # pass did not admit. Anything else falls back to the no-defer result.
    deferred_ids = {str(i) for i in (defer_ids or ()) if isinstance(i, str)}
    normal_costs = {
        i: _fence_row_cost_for_wire(
            rows[i], legacy_injection_wire=legacy_injection_wire)
        for i in (idx for _key, idx in normal)
    }
    # Only rows that could EVER be admitted count: a normal row that
    # costs more than the whole budget is skipped anyway, so counting
    # it made the reservation 0 and pushed every clipped protected row
    # down to its minimal stub (PR #534 critic FB-003).
    max_normal_cost = max(
        (cost for cost in normal_costs.values() if cost <= available),
        default=0)

    def _admit(active_defer):
        """One admission pass under ``active_defer``.

        Returns (admitted, used, truncated, dropped_protected,
        deferred_protected). The caller runs it twice at most (review
        FB-01401): a deferral survives only when the pass that used it also
        admitted a normal row.
        """
        admitted: dict[int, Tuple[dict[str, Any], int]] = {}
        used = 0
        truncated = 0
        dropped_protected = 0
        clipped_used = 0
        deferred_protected = 0
        # Closeout CS-001: NO reordering. Protected rows are admitted in
        # CALLER order, exactly as before this PR - a recorded row that
        # fits must not lose its whole form to a larger protected row that
        # merely happened to be unrecorded. The yield's correctness comes
        # from the post-pass check below, never from the order.
        for i in protected_idx:
            cost = _fence_row_cost_for_wire(
                rows[i], legacy_injection_wire=legacy_injection_wire)
            if cost <= available - used:
                # A protected row that FITS keeps its whole form (issue #116).
                admitted[i] = (rows[i], cost)
                used += cost
                continue
            # A clipped protected row keeps a MINIMAL stub whenever one fits
            # the remaining budget (issue #116: truncated, never dropped while
            # it can still carry information); it is dropped - and COUNTED -
            # only when even a minimal stub does not fit. The FIRST clipped
            # row is the recurring one (clipped on every prompt because it is
            # never acknowledged), so its slice is bounded by the reserve;
            # each further clipped row is held to a minimal stub - the clipped
            # aggregate is therefore at most half the free budget plus one
            # minimal stub per additional clipped row.
            row_min = _protected_row_min_cost(
                rows[i], legacy_injection_wire=legacy_injection_wire)
            remaining_now = available - used
            # Review FB-01202 (PR #534): the guard asks whether a normal row
            # could use what is LEFT, not what the envelope started with.
            # It is a FAST PATH, not the guarantee: if no normal row fits the
            # remainder, `used` can only grow, so the post-pass check below
            # would undo the deferral anyway. Mutation-proven redundant - the
            # probe that drops it is equivalent by construction.
            max_normal_now = max(
                (c for c in normal_costs.values() if c <= available - used),
                default=0)
            if (max_normal_now > 0
                    and str(rows[i].get("id") or "") in active_defer):
                # This row already showed a clipped stub in this session and
                # cannot be delivered whole on this prompt: let it yield the
                # budget to the rows that fit. It is NOT dropped (it is
                # re-selected, and delivered whole, as soon as it fits).
                deferred_protected += 1
                continue
            clip_allowance = max(0, protected_cap - clipped_used)
            room_for_normal = max(0, remaining_now - max_normal_cost)
            slice_budget = min(remaining_now, clip_allowance, room_for_normal)
            if slice_budget < row_min:
                slice_budget = row_min if row_min <= remaining_now else 0
            stub = _truncate_protected_row(
                rows[i], remaining_now,
                legacy_injection_wire=legacy_injection_wire,
                cost_cap=slice_budget,
                # The caller measured the minimal stub as admissible.
                floor_ok=slice_budget >= row_min and row_min <= remaining_now)
            if stub is not None:
                admitted[i] = stub
                used += stub[1]
                clipped_used += stub[1]
                truncated += 1
            else:
                dropped_protected += 1
        for _key, i in normal:
            cost = normal_costs[i]
            if used + cost > available:
                # Issue #116 scope 3: skip and keep scanning - a later smaller
                # row must not be lost to an earlier oversized one.
                continue
            admitted[i] = (rows[i], cost)
            used += cost
        return (admitted, used, truncated, dropped_protected,
                deferred_protected)

    admitted, used, truncated, dropped_protected, deferred_protected = _admit(
        deferred_ids)
    if _trace is not None:
        # Test seam only (default None, zero behaviour change): the
        # final result cannot show whether the undo ran, so the two-pass
        # structure is observable here (PR #534 test-engineer TE-006).
        _trace.append({"pass": 1, "deferred": deferred_protected,
                       "normal_admitted": sum(
                           1 for _k, i in normal if i in admitted)})
    if deferred_protected:
        plain_admitted, plain_used, plain_truncated, plain_dropped, _p = (
            _admit(set()))
        # Review CD-001 / CE-003: the yield is kept only on a NET GAIN.
        #   (a) every protected row the no-defer pass ADMITTED (whole OR as a
        #       stub) that is not itself deferred must still be admitted -
        #       otherwise deferring one row cascaded into dropping or blanking
        #       a DIFFERENT protected row;
        #   (b) a normal row must be admitted that the no-defer pass did
        #       NOT admit, or the freed space bought nothing the prompt was
        #       not already carrying.
        plain_protected = [
            i for i in protected_idx
            if i in plain_admitted
            and str(rows[i].get("id") or "") not in deferred_ids]
        keeps_protected = all(i in admitted for i in plain_protected)
        new_normal = any(i in admitted and i not in plain_admitted
                         for _k, i in normal)
        if not (keeps_protected and new_normal):
            admitted, used, truncated, dropped_protected = (
                plain_admitted, plain_used, plain_truncated, plain_dropped)
            deferred_protected = 0
        if _trace is not None:
            _trace.append({"pass": 2, "deferred": deferred_protected,
                           "normal_admitted": sum(
                               1 for _k, i in normal if i in admitted)})
    kept = [admitted[i][0] for i in sorted(admitted)]
    dropped = len(rows) - len(kept)
    if with_stats:
        stats = {
            "admission_used": used,
            "dropped": dropped,
            "truncated": truncated,
            "dropped_protected": dropped_protected,
            "deferred_protected": deferred_protected,
            "budget": budget,
        }
        return kept, used, dropped, stats
    return kept, used, dropped


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


def _lane_floors() -> Tuple[float, float, float, float]:
    """Per-lane relevance floors (issue #113; graph lane issue #136),
    single-sourced from schema_meta.

    A row's PRESENT lane value must clear its own floor; an ABSENT lane
    (None) is exempt. Literals mirror the schema_meta defaults so a
    partially-deployed tree keeps the documented gate. The graph floor
    defaults to LINK_THRESHOLD (0.75): the graph arm's lane value is the
    best entry-edge score, and 0.75 is the same bar the write path requires
    before it auto-links two rows as related.
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
    graph = _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_GRAPH_ENV",
                "ZMEM_INJECT_FLOOR_GRAPH"),
        getattr(_schema_meta, "INJECT_FLOOR_GRAPH_DEFAULT", 0.75),
    )
    # A negative floor would trivially clear every measured lane (relevance
    # values are >= 0), i.e. silently disable the gate. Treat it as operator
    # error and clamp to 0.0 (the honest "disable this lane" value).
    return (max(0.0, lex), max(0.0, cos), max(0.0, ent), max(0.0, graph))


def _trust_floor() -> float:
    """The trust_score hard floor (issue #115), single-sourced from schema_meta.

    Rows in the selective-inject gate whose trust_score sits below this floor
    are hard-dropped (the contradiction ledger's end state: nine or more
    distinct contradicts). Literals mirror the schema_meta default so a
    partially-deployed tree keeps the documented gate; a negative value is
    operator error and clamps to 0.0 — and because _row_trust clamps every
    trust value into [0.0, 1.0], a floor of 0.0 means the comparison
    `< 0.0` never fires: the trust gate is fully DISABLED at floor 0.0
    (even trust=0 rows pass). Operators who want "drop nothing" have it;
    nobody gets it by accident without setting the env to 0 explicitly.
    """
    return max(0.0, _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_TRUST_ENV",
                "ZMEM_INJECT_FLOOR_TRUST"),
        getattr(_schema_meta, "INJECT_FLOOR_TRUST_DEFAULT", 0.2),
    ))


def _user_global_floor() -> float:
    """The user:global tier floor (issue #235), single-sourced from
    schema_meta.

    On gated passive moments (pretool/subagent) a global-tier injection
    candidate whose MAX measured relevance lane sits below this floor is
    withheld and its reserved slot returns empty rather than being
    backfilled (enforcement lives in recall.py's global-tier seam; a global
    candidate with NO measured lane keeps the not-measured exemption).
    Literals mirror the schema_meta defaults so a partially-deployed tree
    keeps the documented floor. A negative value is operator error and
    clamps to 0.0; at 0.0 the floor is fully DISABLED (relevance values are
    >= 0, so nothing is ever below it) — the same honest-disable semantics
    as the trust floor.
    """
    return max(0.0, _env_float(
        getattr(_schema_meta, "INJECT_FLOOR_USER_GLOBAL_ENV",
                "ZMEM_INJECT_FLOOR_USER_GLOBAL"),
        getattr(_schema_meta, "INJECT_FLOOR_USER_GLOBAL_DEFAULT", 0.5),
    ))


def _row_trust(row: Any) -> float:
    """The row's trust_score, normalized for ranking/gate use (issue #115).

    Accepts sqlite3.Row AND dict (indexing with try/except — sqlite3.Row has
    no ``.get``). Missing key or None (legacy dict callers, lineage extras,
    any store predating the v11 column) -> 1.0: the neutral identity, so
    every existing caller is byte-identical. A value that cannot be parsed
    as a float, or a non-finite one (NaN/inf), -> 0.0: fail-closed, the same
    normalization the gate applies to NaN/inf confidence. Anything else is
    clamped to [0.0, 1.0] — the schema has NO CHECK on the column, so a
    corrupted out-of-range value must not produce a negative composite score
    or break the sort.
    """
    try:
        raw = row["trust_score"]
    except (KeyError, IndexError, TypeError):
        return 1.0
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a huge int (e.g. 10**400) converts to neither a
        # finite float nor a raised ValueError — fail closed like the rest.
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Issue #498: prompt-assertion analysis (subject-bound suppression +
# question-focus rescue). Pure functions, stdlib only, deterministic. The
# frame parser below is SHARED by the live admission seam, the explain
# replay, and eval_gold's verifier. PR #526 feedback (FB-103): the eval
# verifier re-derives the SELECTION (ordinary gate pass + rescue loop) but
# shares the parser (analyze_prompt / parse_row_assertions), the partition
# (partition_prompt_assertions), the subject-compatibility predicate
# (_pa_subject_compat), and the QUESTION_RESCUE_FLOOR constant — a mutation
# inside those shared pieces moves production and the verifier together;
# partition/floor correctness is pinned by unit tests and the mutation
# battery instead.
# ---------------------------------------------------------------------------

QUESTION_RESCUE_FLOOR = 0.25

_PA_STOPWORDS = frozenset((
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "is",
    "are", "was", "were", "be", "been", "being", "to", "of", "for", "from",
    "in", "on", "at", "by", "with", "and", "or", "but", "if", "then", "so",
    "as", "than", "into", "via", "per", "we", "you", "i", "my", "our", "do",
    "does", "did", "should", "would", "could", "can", "will", "shall", "may",
    "must", "need", "needs", "what", "which", "who", "whom", "whose",
    "where", "when", "why", "how", "there", "here", "again", "about", "over",
    "under", "up", "down", "out", "get", "gets", "got", "have", "has", "had",
    "not", "no", "yes", "current", "every", "each", "any", "all", "some",
    "job", "use", "used",
    # attribute nouns and generic verbs: they name the ASKED relation, never
    # the subject (issue #498 R10 subject binding)
    "destination", "register", "location", "cost", "price", "frequency",
    "ownership", "duration", "approval", "approver", "receive", "receives",
    "handled", "handle", "routed", "route", "routes",
    # PR #526 feedback (P-F3 side-finding): relation VERBS in question
    # clauses name the asked relation, never the subject — leaving them in
    # made "Who approves the job?" carry subject ['approves'], which broke
    # ellipsis binding and forced the unbound fallback for every explicit
    # subject.
    "approves", "approve", "approved", "signs",
    # Filler/request verbs (PR #526 feedback, parallel-review INTA-002):
    # they ask or request, they never name the subject.
    "remember", "please", "tell", "let", "know", "want", "check", "see",
    "look", "review",
))

# Value spans are 1-4 word noun phrases after an optional article (issue
# #498 rows carry both proper spans "Rowan Dock" and common-noun spans
# "senior editor" / "duty roster board"), or a NUM/word-number quantity
# followed by a short noun phrase ("40 marks", "four hours").
_PA_VALUE = r"(?:the\s+|a\s+|an\s+)?((?:[A-Za-z][A-Za-z0-9-]+(?:\s+[A-Za-z][A-Za-z0-9-]+){0,3})|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+(?:\.\d+)?)\s+[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z][A-Za-z0-9-]*){0,2})"

_PA_DEST_CUES = [
    r"\bsent\s+to\s+" + _PA_VALUE,
    r"\bgoes\s+to\s+" + _PA_VALUE,
    r"\bgo\s+to\s+" + _PA_VALUE,
    r"\brouted\s+via\s+" + _PA_VALUE,
    r"\broutes?\s+to\s+" + _PA_VALUE,
    r"\bbelongs\s+in\s+" + _PA_VALUE,
    r"\bbelongs\s+to\s+" + _PA_VALUE,
    r"\benters?\s+" + _PA_VALUE,
    r"\breceived\s+at\s+" + _PA_VALUE,
    r"\bissued\s+at\s+" + _PA_VALUE,
    r"\bsends?\s+[^.]{0,40}?\bto\s+" + _PA_VALUE,
]
_PA_DEST_Q = (r"\bwhat\s+destination\b|\bwhere\s+[^.?]{0,40}\b(?:go|goes|"
              r"get\s+handled|handled|sent|issued|processed)\b|\bwhich\s+"
              r"(?:register|location|dock|yard|desk|siding)\b|\bwhere\s+does\b")
_PA_APPROVER_CUES = [
    # THING ... approved by PERSON  (subject = head, value = group 1)
    r"\b(?:is\s+)?approved\s+by\s+" + _PA_VALUE,
    # PERSON approves THING  (subject = tail, value = group 1 captured BEFORE
    # the verb via the pattern below). The lookbehind prefix keeps the
    # alternation anchored on word boundaries — without it, a long unbroken
    # letter/digit run degrades toward quadratic backtracking (final-critic
    # round 1: 13.2s on a 20K uppercase run; 0.0003s with the guard).
    r"(?<![A-Za-z0-9-])((?:[A-Z][A-Za-z0-9-]+(?:\s+[A-Z][A-Za-z0-9-]+){0,3}))(?![A-Za-z0-9-])\s+approves\b",
    r"\bsigns?\s+off\s+(?:on\s+)?" + _PA_VALUE,
]
_PA_APPROVER_Q = (r"\bwho\s+(?:approves|signs\s+off)|\bwho\s+should\b"
                  r"[^.?]{0,40}\b(?:look|review|check|approve)")
_PA_FREQUENCY_CUES = [r"\bevery\s+((?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+(?:hours?|days?|weeks?|minutes?|months?))\b"]
_PA_FREQUENCY_Q = r"\bhow\s+often\b|\bwhat\s+frequency\b"
_PA_COST_CUES = [r"\bcosts?\s+(?:about\s+)?" + _PA_VALUE]
_PA_COST_Q = r"\bwhat\s+[^.?]{0,80}\bcost\b|\bprice\b|\bhow\s+much\b"
_PA_OWNERSHIP_CUES = [r"\b(?:is\s+)?owned\s+by\s+" + _PA_VALUE]
_PA_OWNERSHIP_Q = r"\bwhose\b|\bwho\s+owns\b"
_PA_DURATION_CUES = [r"\btakes?\s+((?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+(?:hours?|days?|weeks?|minutes?|months?))\b"]
_PA_DURATION_Q = r"\bhow\s+long\b"

_PA_RELATIONS = {
    "destination": (_PA_DEST_CUES, _PA_DEST_Q),
    "approver": (_PA_APPROVER_CUES, _PA_APPROVER_Q),
    "frequency": (_PA_FREQUENCY_CUES, _PA_FREQUENCY_Q),
    "cost": (_PA_COST_CUES, _PA_COST_Q),
    "ownership": (_PA_OWNERSHIP_CUES, _PA_OWNERSHIP_Q),
    "duration": (_PA_DURATION_CUES, _PA_DURATION_Q),
}

# Question-focus cues are matched case-insensitively (question words open
# clauses and are therefore sentence-capitalized); assertion cues stay
# case-sensitive verbs/prepositions in mid-clause position.
_PA_RELATIONS_Q = {
    rel: re.compile(q, re.IGNORECASE) for rel, (_c, q) in _PA_RELATIONS.items()
}

_PA_UNSUPPORTED_FOCI = frozenset(("cost", "ownership", "duration"))

_PA_NEGATION = re.compile(
    r"\b(?:not|never|no\s+longer|is\s+not|are\s+not|does\s+not|"
    # Apostrophe contractions (ASCII ' and U+2019) — implementation review
    # round 1: "isn't sent to Pine Crossing" parsed as an AFFIRMATIVE
    # assertion because the bare "isnt" form never matched the typed text.
    r"isn.?t|aren.?t|wasn.?t|weren.?t|doesn.?t|don.?t|didn.?t|won.?t|"
    r"wouldn.?t|couldn.?t|shouldn.?t|can.?t|cannot|hasn.?t|haven.?t)\b",
    re.IGNORECASE)
_PA_HYPOTHETICAL = re.compile(
    r"\b(?:if|suppose|assuming|hypothetically|were\s+it|in\s+case)\b",
    re.IGNORECASE)


def _pa_fold(text):
    """Unicode-fold a clause for the negation/hypothetical DETECTION only
    (parallel-review MX-UIB-005): NFKD + combining-mark strip + casefold so
    a homoglyph inside 'not' ("no\u0301t") still reads as negation. The
    folded copy never feeds term extraction (subjects keep their original
    graphemes)."""
    import unicodedata as _ud
    folded = _ud.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded
                     if not _ud.combining(ch))
    return folded.casefold()
_PA_SENTENCE_SPLIT = re.compile(
    r"[.!?;]\s+|\s+-\s+|\n+|[;?](?=[A-Za-z])")
_PA_QUESTION_MARK = re.compile(r"\?")
_PA_Q_STARTERS = re.compile(
    r"^(?:who|what|where|which|how|whose|why|when)\b", re.IGNORECASE)
# PR #526 feedback (parallel-review P-F2): a clause OPENING with a
# yes/no auxiliary (interrogative inversion) is a question — the wh-word
# starter list alone missed "Does the CLI send errors to stderr?", whose
# terminal '?' the splitter consumes before the next clause.
_PA_Q_AUX = re.compile(
    r"^(?:is|are|was|were|am|do|does|did|has|have|had|can|could|should|"
    r"would|will|shall|may|might|must)\b", re.IGNORECASE)
_PA_WHETHER = re.compile(r"\bwhether\b", re.IGNORECASE)
_PA_ANAPHOR_WORDS = frozenset(("this", "that", "it", "these", "those", "such"))


def _pa_terms(text):
    """Content terms of a span: casefolded word tokens minus stopwords.

    PR #526 feedback (parallel-review P-F1): digit-bearing tokens are KEPT
    regardless of length ("invoice 42" vs "invoice 17" must not bind), and
    NFC-normalized casefold widens matching past ASCII while a trailing
    possessive "'s" is stripped so "Kennet's" and "Kennet" bind."""
    import unicodedata as _ud
    out = []
    for tok in re.findall(r"[^\W\d_][\w'\u2019-]*|\d[\w-]*", text or "",
                          re.UNICODE):
        low = _ud.normalize("NFC", tok).casefold()
        if low.endswith("'s") or low.endswith("\u2019s"):
            low = low[:-2]
        if low in _PA_STOPWORDS:
            continue
        if len(low) < 3 and not any(ch.isdigit() for ch in low):
            continue
        out.append(low)
    return out


def _pa_clause_is_question(clause):
    """A clause is a question when it still carries its terminal '?' (only
    the FINAL clause of a split retains punctuation), starts with a
    question word, opens with a yes/no auxiliary (interrogative inversion),
    or embeds 'whether' (hedged/indirect question frames)."""
    return bool(_PA_QUESTION_MARK.search(clause)
                or _PA_Q_STARTERS.match(clause.strip())
                or _PA_Q_AUX.match(clause.strip())
                or _PA_WHETHER.search(clause))


def _pa_match_value(m):
    for grp in range(1, m.re.groups + 1):
        if m.group(grp):
            return m.group(grp).strip()
    return None


def _pa_parse_clause(clause):
    """All affirmative assertion triples in one clause. Negated and
    hypothetical clauses yield nothing. Each item is
    {subject (terms), relation, value, clause_index:-1}."""
    found = []
    folded = _pa_fold(clause)
    if _PA_NEGATION.search(folded) or _PA_HYPOTHETICAL.search(folded):
        return found
    for relation, (cues, _q) in _PA_RELATIONS.items():
        for cue in cues:
            m = re.search(cue, clause)
            if not m:
                continue
            value = _pa_match_value(m)
            if not value:
                continue
            if relation == "approver" and " approves" in m.group(0):
                # "PERSON approves THING": the value was captured BEFORE the
                # verb; the subject is the tail after it.
                tail = clause[m.end():]
                subject = _pa_terms(tail)
            else:
                head = clause[:m.start()]
                subject = _pa_terms(head)
            if not subject:
                continue
            found.append({"subject": subject, "relation": relation,
                          "value": value, "clause_index": -1})
            break
    return found


def _pa_parse_assertions(query):
    clauses = [c.strip() for c in _PA_SENTENCE_SPLIT.split(query or "")
               if c.strip()]
    assertions = []
    for idx, clause in enumerate(clauses):
        # A QUESTION clause never contributes an assertion: "What does X
        # route to Pine Crossing cost?" carries a relation cue inside an
        # interrogative, and treating it as declarative would PROMPT_DUPLICATE
        # the very row that answers the question (qualified-fact leg).
        if _pa_clause_is_question(clause):
            continue
        for a in _pa_parse_clause(clause):
            a["clause_index"] = idx
            assertions.append(a)
    return assertions, clauses


def _pa_focus_subject(clause):
    """Question-clause subject terms. The stoplist already removes question
    words and attribute nouns, so NO cue-word stripping is done (stripping the
    matched cue text emptied legitimate subjects — issue #498 probe)."""
    subj = _pa_terms(clause)
    raw = re.findall(r"[A-Za-z]+", clause)
    if raw and raw[0].lower() in _PA_ANAPHOR_WORDS:
        return subj, True
    return subj, False


def _pa_parse_foci(clauses, assertions):
    foci = []
    for idx, clause in enumerate(clauses):
        if not _pa_clause_is_question(clause):
            continue
        for relation, (_cues, _q_pattern) in _PA_RELATIONS.items():
            qm = _PA_RELATIONS_Q[relation].search(clause)
            if not qm:
                continue
            subj, is_anaphor = _pa_focus_subject(clause)
            # PR #526 feedback (P-F3): a focus whose subject is anaphoric or
            # empty (ellipsis) is discourse-BOUND when a preceding assertion
            # supplies it; only such bound foci may use the subject-unbound
            # rescue fallback later.
            bound_ctx = False
            if is_anaphor or not subj:
                # Anaphor OR ellipsis (the question names no subject at all —
                # "What destination should be used?"): bind to the nearest
                # PRECEDING assertion's subject. The mixed prompt binds its
                # approver focus to the destination assertion's subject, which
                # carries no approver assertion — the focus stays unanswered,
                # which is exactly the rescue case.
                bound = None
                for a in assertions:
                    if a["clause_index"] < idx:
                        bound = a
                if bound is not None:
                    subj = list(bound["subject"])
                    bound_ctx = True
            answered = any(
                a["relation"] == relation
                and _pa_subject_compat(subj, a["subject"])
                for a in assertions)
            foci.append({"subject": subj, "relation": relation,
                         "clause_index": idx, "answered": answered,
                         "bound": bound_ctx})
            break
    return foci


def _pa_subject_compat(a_terms, b_terms):
    """Drop-rule affinity: SHARED CLAUSE CONTENT TERMS ONLY (issue #498 R10 —
    cosine never participates in drop affinity).

    PR #526 feedback (parallel-review P-F1): item-identity guard — when
    BOTH sides carry digit-bearing identifiers and those identifier sets are
    disjoint ("invoice 42" vs "invoice 17", "batch 7" vs "batch 9"), the
    shared generic word does NOT bind: these are different items, so the
    conflicting-default drop must not fire. A one-sided reference ("where
    does the invoice go?" after "Invoice 42 ...") still binds."""
    if not a_terms or not b_terms:
        return False
    sa = set(a_terms)
    sb = set(b_terms)
    if not (sa & sb):
        return False
    da = {t for t in sa if any(ch.isdigit() for ch in t)}
    db = {t for t in sb if any(ch.isdigit() for ch in t)}
    if da and db and not (da & db):
        return False
    return True


def analyze_prompt(query):
    """Parse the #498 prompt frame: assertions + question foci."""
    assertions, clauses = _pa_parse_assertions(query or "")
    foci = _pa_parse_foci(clauses, assertions)
    return {"assertions": assertions, "foci": foci, "clauses": clauses}


def parse_row_assertions(content):
    """The row-side grammar: EVERY affirmative assertion parsed from a
    stored fact's content (a qualified fact can carry both a destination and
    a cost). Order preserved; first assertion is the primary one.

    (Final-critic round 1: the singular parse_row_assertion wrapper was
    deleted — it was reachable only from tests, which the stranded-census
    gate correctly flagged as unwired code.)"""
    if not content:
        return []
    return _pa_parse_clause(content)


def _pa_values_overlap(v1, v2):
    t1 = set(_pa_terms(v1))
    t2 = set(_pa_terms(v2))
    if not t1 or not t2:
        return False
    return bool(t1 & t2)


def _pa_row_drop_type(frame, row_asserts):
    """'redundant' (PROMPT_DUPLICATE), 'conflict', 'orthogonal', or None.

    Every parsed row assertion participates: CONFLICT/redundant match on ANY
    relation the row asserts, and ORTHOGONAL spares a row that positively
    asserts the requested (unsupported) focus relation — the qualified-fact
    survival rule (plan-critic round 2 blocker 3).

    Value comparison is term-overlap; two values sharing their generic tail
    noun ("Ash Register" vs "Pearl Register" share "register") compare equal
    — both classes drop the row, so only the diagnostic label differs.
    """
    if not row_asserts:
        return None
    # PR #526 feedback (parallel-review MX-UIB-006): the qualified-fact
    # exemption is checked BEFORE the conflict loop — a row that positively
    # asserts the asked (unsupported-focus) attribute survives even when it
    # ALSO carries a destination the prompt's explicit destination would
    # conflict-drop.
    foci = frame["foci"]
    if len(foci) == 1 and foci[0]["relation"] in _PA_UNSUPPORTED_FOCI:
        if any(ra["relation"] == foci[0]["relation"] for ra in row_asserts):
            return None  # qualified fact: the row answers the asked attribute
    for row_assert in row_asserts:
        r_subj = row_assert["subject"]
        r_rel = row_assert["relation"]
        r_val = row_assert["value"]
        for a in frame["assertions"]:
            if a["relation"] != r_rel:
                continue
            if not _pa_subject_compat(a["subject"], r_subj):
                continue
            if _pa_values_overlap(a["value"], r_val):
                return "redundant"
            return "conflict"
    foci = frame["foci"]
    if len(foci) == 1 and foci[0]["relation"] in _PA_UNSUPPORTED_FOCI:
        focus = foci[0]
        for ra in row_asserts:
            if _pa_subject_compat(focus["subject"], ra["subject"]):
                return "orthogonal"
    return None


_PA_DROPPABLE_TYPES = frozenset((None, "fact", "state", "event", "lesson"))


def partition_prompt_assertions(frame, rows):
    """Facts-only partition. Constraints, decisions, preferences,
    conventions, and belief heads are NEVER dropped (their `type` is checked
    first). Returns (drops {id: label}, survivors)."""
    drops = {}
    survivors = []
    for r in rows:
        if r.get("type") not in _PA_DROPPABLE_TYPES:
            survivors.append(r)
            continue
        drop = _pa_row_drop_type(frame, parse_row_assertions(r.get("content", "")))
        if drop:
            drops[r["id"]] = drop
        else:
            survivors.append(r)
    return drops, survivors


def _pa_row_trust_confidence_ok(row):
    """The filter's own trust/confidence predicates, evaluated standalone so
    rescue nomination can skip an untrusted higher-scorer (R11 step 3)."""
    if _row_trust(row) < _trust_floor():
        return False
    try:
        conf = float(row.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf != conf or conf in (float("inf"), float("-inf")):
        conf = 0.0
    sig = (row.get("signal") or "none").lower()
    _c_floor, c_gate_none, c_grounded = _gate_constants()
    if sig == "none":
        return conf >= c_gate_none
    if sig not in c_grounded:
        return False
    return conf >= _c_floor


def _pa_row_best_lane(row):
    """max over measured relevance lanes, or None when nothing is measured."""
    best = None
    for key in ("_rel_lex", "_rel_cos", "_rel_ent", "_rel_graph"):
        val = row.get(key)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if val != val or val in (float("inf"), float("-inf")):
            continue
        best = val if best is None else max(best, val)
    return best


def _pa_rescue_candidates(survivors, relation, subject_terms, used_ids,
                          subject_bound):
    """Relation-compatible, trust/confidence-eligible, floor-clearing
    candidates for one focus; best first."""
    ranked = []
    for r in survivors:
        if r["id"] in used_ids:
            continue
        row_asserts = parse_row_assertions(r.get("content", ""))
        matching = [ra for ra in row_asserts if ra["relation"] == relation]
        if not matching:
            continue
        if (subject_bound and subject_terms
                and not any(_pa_subject_compat(ra["subject"], subject_terms)
                            for ra in matching)):
            # An EMPTY subject (the question named nothing bindable) is
            # already unbound — the relation gate + floor carry the decision.
            continue
        if not _pa_row_trust_confidence_ok(r):
            continue
        lane = _pa_row_best_lane(r)
        if lane is None or lane < QUESTION_RESCUE_FLOOR:
            continue
        ranked.append((lane, r["id"], r))
    ranked.sort(key=lambda item: -item[0])
    return ranked


def rescue_nominees(frame, survivors, ordinary_selected):
    """R3/R10/R11/R14 fallback rescue: at most ONE row per unanswered,
    ordinarily-unsatisfied focus. Ordinary satisfaction is subject+relation
    bound; when the question subject binds to nothing AND the focus was
    anaphora/ellipsis-BOUND (its subject arrived from discourse context, not
    from the question text — "who approves the job?"), a subject-unbound
    pass restricted to the same relation gate admits the best floor-clearing
    row instead (PR #526 feedback P-F3: an explicit question subject that
    simply fails compat must NOT fall back to an unrelated row — "who
    approves the Ash Register shipment?" must not rescue "Dana Reyes
    approves Crane Repair")."""
    if not frame["foci"]:
        return {}
    ordinary_by_relation = {}
    for r in ordinary_selected:
        for ra in parse_row_assertions(r.get("content", "")):
            ordinary_by_relation.setdefault(ra["relation"], []).append(ra["subject"])
    nominees = {}
    for focus in frame["foci"]:
        if focus["answered"]:
            continue
        relation = focus["relation"]
        satisfied = any(
            _pa_subject_compat(subj, focus["subject"])
            for subj in ordinary_by_relation.get(relation, []))
        if satisfied:
            continue
        # FB-107 / parallel-review MX-PO-002/INTA-007: a row the ordinary
        # gate already admitted is NEVER re-nominated (the rescue exists to
        # lift floor-rejected rows; counting an ordinary delivery as a
        # rescue overstates attribute_rescued).
        used = set(nominees) | {r["id"] for r in ordinary_selected}
        ranked = _pa_rescue_candidates(
            survivors, relation, focus["subject"], used, subject_bound=True)
        if not ranked and (not focus["subject"] or focus.get("bound")
                           or len(focus["subject"]) <= 1):
            # Discourse-ellipsis fallback: same relation gate, no subject
            # gate — restricted to GENERIC subjects: foci whose subject came
            # from anaphora/ellipsis binding, is empty, or is a single bare
            # token ("where does the extra go?"). A MULTI-TERM subject names
            # a specific item and must not be overridden by an unrelated
            # row (P-F3: "who approves the Ash Register shipment?" must not
            # rescue "Dana Reyes approves Crane Repair"; "who approves the
            # forklift inspection?" must not rescue Mallory Vance).
            ranked = _pa_rescue_candidates(
                survivors, relation, focus["subject"], used, subject_bound=False)
        if ranked:
            lane, rid, _row = ranked[0]
            nominees[rid] = {"relation": relation,
                             "floor": QUESTION_RESCUE_FLOOR,
                             "lane": lane}
    return nominees


def selective_inject_filter(
    rows: list[dict[str, Any]],
    floor: Optional[float] = None,
    gate_none_floor: Optional[float] = None,
    grounded_signals: Optional[frozenset] = None,
    *,
    lane_floors: Optional[Tuple[float, float, float, float]] = None,
    admit_ids: Optional[frozenset] = None,
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

    Issue #136: the graph-seed arm stamps ``_rel_graph`` (best entry-edge
    score) on the rows it contributes, so a graph-rescued row is FLOOR-JUDGED
    on its own merits instead of riding the absent-lane exemption — it passes
    only when its edge to the query-anchored seed clears the graph floor
    (default 0.75 = LINK_THRESHOLD).

    Issue #115: BEFORE the confidence check, the row's ``trust_score``
    (``_row_trust``: missing key -> 1.0, unparseable/non-finite -> 0.0,
    clamped to [0, 1]) must clear ``_trust_floor()`` (default 0.2 — nine or
    more distinct contradicts). A row below the floor counts in the EXISTING
    ``trust_failed`` bucket — no new stats key — so a fully floor-drained
    pool reports ``below-bar`` ("nothing trusted"), which is the truthful
    reason. Explicit recall/search never run this gate, so the same row
    stays retrievable there; compute_score separately discounts ranking by
    trust_score on every surface.

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
    trust_floor = _trust_floor()
    for r in rows:
        # Issue #115: the contradiction ledger's hard floor. A row whose
        # trust_score sits below the floor (nine or more distinct contradicts
        # at the 0.2 default) is not trusted, period — it counts in the same
        # trust_failed bucket as a below-confidence row (no new stats key),
        # so an all-drained pool classifies as below-bar ("nothing trusted").
        if _row_trust(r) < trust_floor:
            trust_failed += 1
            continue
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
        # Issue #136 review round: zip (not fixed indexes) so a legacy
        # 3-value lane_floors override keeps working — the graph floor joins
        # the disjunction only when a fourth value is supplied.
        for key, fl in zip(("_rel_lex", "_rel_cos", "_rel_ent", "_rel_graph"),
                           lane_floors):
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
        # Issue #498 (R11 two-phase): ``admit_ids`` carries question-focus
        # rescue nominees chosen AFTER the ordinary pass. The exemption
        # bypasses ONLY the lane disjunction — trust and confidence already
        # passed above — and ``admit_ids=None`` (every existing caller) is
        # byte-identical behavior.
        if lane_ok or not measured or (
                admit_ids is not None and r.get("id") in admit_ids):
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


def classify_silent_reason(
    rows: list,
    omitted: int = 0,
    budget_emptied: bool = False,
    lane_stats: Optional[dict] = None,
    *,
    candidate_ids: Optional[list[str]] = None,
    post_ledger_rows: Optional[list] = None,
    expiry_drained: bool = False,
    attribute_dropped: int = 0,
) -> str:
    """Name WHY a silent inject is silent (issue #87; store-side twin).

    Same precedence as the hook body's classifier: budget-drop wins first;
    then a non-empty pre-ledger candidate set with an empty post-ledger pool
    is already-delivered; then relevance, bar, omitted, and finally an empty
    pool.  ``candidate_ids`` and ``post_ledger_rows`` are additive inputs, so
    callers from older releases can continue passing only ``rows``.  The
    closed set comes from schema_meta (``INJECT_SILENT_REASONS``); a
    drift/unknown value degrades to empty-pool rather than inventing a reason.

    Issue #113: when the caller passes the gate's ``lane_stats`` (the
    ``with_stats=True`` third element), a silent decision where at least one
    row passed the TRUST gate but every trust-passing row failed a relevance
    floor names ``below-relevance`` ("nothing relevant") instead of
    ``below-bar`` ("nothing trusted"). Without lane_stats the legacy
    behavior is byte-identical.
    """
    allowed = tuple(getattr(
        _schema_meta, "INJECT_SILENT_REASONS", INJECT_SILENT_REASONS,
    )) if _schema_meta is not None else INJECT_SILENT_REASONS
    # Legacy callers used ``rows`` as both the candidate pool and the gate
    # pool.  In the new contract ``candidate_ids`` is the pre-ledger set and
    # ``post_ledger_rows`` is the post-ledger gate pool; normalize only when a
    # caller supplies neither new value.
    if candidate_ids is None:
        candidate_ids = [r.get("id") for r in rows if isinstance(r, dict)]
    if post_ledger_rows is None:
        post_ledger_rows = rows
    if budget_emptied:
        reason = "budget-drop"
    elif candidate_ids and not post_ledger_rows:
        reason = "already-delivered"
    elif attribute_dropped and not rows:
        # Issue #498: the pool was non-empty on arrival but every row fell to
        # the prompt-assertion partition (conflict / redundant / orthogonal).
        # "Not relevant GIVEN the prompt's explicit assertions" is the honest
        # below-relevance reading; the trust statistics of the filtered pool
        # (all zeros) are never consulted to fake a below-bar verdict.
        reason = "below-relevance"
    elif rows and lane_stats is not None \
            and lane_stats.get("trust_passed", 0) >= 1:
        reason = "below-relevance"
    elif rows:
        reason = "below-bar"
    elif omitted == 0 and expiry_drained:
        # Issue #174: rows == [] here (every rows-consuming branch fell
        # through), the budget did not empty the pool, nothing was omitted,
        # and the caller's diagnosis says the PRE-expiry pool was eligible
        # after omission, relevance, and ledger — expiry alone drained it.
        reason = "expired"
    elif omitted > 0:
        reason = "omitted"
    else:
        reason = "empty-pool"
    if reason not in allowed:
        return "empty-pool"
    return reason


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    if not isinstance(values, (list, tuple)):
        return result
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _selector_silent(*, budget_tokens: int, reason: str = "empty-pool",
                     excluded: list[str] | None = None) -> dict:
    # Lazy import avoids making inject.py depend on recall at import time.
    from storelib.recall import build_injection_envelope  # type: ignore
    return build_injection_envelope(
        [], omitted=0, reason=reason, excluded=list(excluded or []),
        candidate_ids=[], tokens_used=0, tokens_budget=budget_tokens,
        budget_dropped=0, budget_admission=0, budget_truncated=0,
        budget_dropped_protected=0, arms={}, rendered="",
    )


def _injection_data_dir(data_dir: str | None = None) -> str:
    """Resolve the passive sidecar directory through the store precedence."""
    from pathlib import Path
    # An explicit caller argument is the sole bypass.  Otherwise preserve the
    # canonical resolver precedence (ZMEM_STORE before ZMEM_DATA/plugin
    # fallbacks) by deriving the sidecar directory from its resolved store.
    # Do not create either path here: selector reads remain side-effect free
    # until ledger.record.
    if data_dir:
        return str(Path(data_dir).expanduser().resolve())
    from storelib.schema import _resolve_store_path  # type: ignore
    return str(_resolve_store_path().parent)


def _page_rows_withhold_partition_overlaps(page_rows, dropped_ids):
    """PR #526 feedback (FB-109 / parallel-review MX-UR-08): withhold derived
    pages whose bullet embeds the content of a row the prompt-assertion
    partition dropped — the page would re-deliver the suppressed fact
    verbatim (admit_with_pages removes the page's represented canonical
    sources AFTER the partition runs). ``dropped_ids`` maps the partition's
    dropped row ids to their labels; a page is withheld when ANY of its
    represented/source ids is in that map. Non-string source ids are
    ignored."""
    if not page_rows or not dropped_ids:
        return page_rows
    out = []
    for page in page_rows:
        sources = (page.get("represented_ids")
                   or page.get("source_ids") or [])
        overlap = any(isinstance(sid, str) and sid in dropped_ids
                      for sid in sources)
        if not overlap:
            out.append(page)
    return out


def select_and_budget_for_injection(
    conn,
    *,
    query: str,
    namespace: str,
    moment: str,
    session_id: str,
    lane: str | None = None,
    exclude_ids: list[str] | None = None,
    ops_tokens: list[str] | None = None,
    limit: int = 5,
    global_limit: int = 3,
    budget_tokens: int = 1500,
    data_dir: str | None = None,
    min_confidence: float | None = None,
    pretool_input: dict | None = None,
    user_global_floor: float | None = None,
    authorized_namespaces: frozenset | None = None,
    reserve: bool = False,
    principal: str | None = None,
    harness: str | None = None,
    no_telemetry: bool = False,
    state_hint: str | None = None,
    consume_tasktext: bool = False,
    consume_agent_id: str = "",
    prompt_timestamp: str | None = None,
) -> dict:
    """Select, render, account, and record one passive injection event.

    Attribution is validated before touching delivery state.  ``ops_tokens``
    distinguishes omitted (``None``: derive the pretool ring in storelib) from
    explicitly empty (``[]``: do not read the ring).  The caller owns ``conn``;
    this function never opens or closes SQLite.

    ``authorized_namespaces`` (issue #283, T-04): the caller's namespace
    allow-list.  ``None`` (the default, every local caller) is unscoped —
    behavior is byte-identical to the pre-T-04 selector.  When set, the
    user:global lane runs only if ``user:global`` is a member, and the
    cross-project lane's foreign namespace set is intersected with the
    allow-list, so unauthorized candidates are filtered BEFORE ranking,
    budgeting and the delivery ledger (a denied row can never consume
    budget or be recorded as delivered).  The MCP surface translates its
    principal's allow-list into this input; fail-closed denial for an
    unauthorized REQUESTED namespace stays at the caller's boundary.

    ``user_global_floor`` (issue #235) overrides the env-derived
    user:global tier floor; ``None`` resolves it from the environment on
    gated moments (``pretool``/``subagent`` — hooks' posttoolbatch maps to
    ``pretool`` store-side) and leaves it unset on every other moment.  The
    resolved value is threaded through the shared kwargs dict into BOTH
    ``recall_memory`` and ``recent_memory`` (``_user_global_floor``);
    enforcement lives at recall.py's global-tier seam, where a below-floor
    candidate is withheld and its slot returns empty rather than being
    backfilled.  A global candidate with no measured lane (query-less
    recent pulls) keeps the relevance gate's not-measured exemption.  An
    explicit argument that is not a finite number is treated as unset
    (the env/default resolution applies); a negative value clamps to 0.0
    (floor disabled) — the passive lane never raises on a bad floor value.

    no_telemetry (issue #290, T-11): the zero-write mode for serving
    from a read-only snapshot conn — skips the surfaced-count bump AND
    every delivery-ledger write (reserve/record alike).  Offline serves
    are advisory by contract: a row served offline may be re-delivered
    live after reconnect.

    prompt_timestamp (issue #389, AC8): the prompt's own clock for the
    temporal arm — threaded verbatim into BOTH dispatch branches
    (``recall_memory``/``recent_memory``).  PR #458 review UIB-1 (honest
    contract): an UNPARSEABLE value is NOT replaced by the
    ZMEM_TEST_NOW/now seam — ``_normalize_as_of`` returns it unchanged, so
    ``_temporal_window`` treats it as truthy, skips the seam, and the
    temporal arm silently answers empty.  ``None`` and empty-string keep
    the seam exactly; every production boundary (hook body, fleet
    transport, hermes, MCP server) drops unparseable values before
    threading, so in-process callers must do the same (or pass
    ``None``/``""`` for the seam).
    """
    if moment not in INJECTION_MOMENTS:
        raise ValueError("invalid injection moment: {!r}".format(moment))
    if lane is not None and lane not in INJECTION_LANES:
        raise ValueError("invalid injection lane: {!r}".format(lane))
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required for passive injection")
    try:
        budget = int(budget_tokens)
    except (TypeError, ValueError):
        budget = inject_token_budget()
    if budget <= 0:
        budget = inject_token_budget()

    try:
        resolved_data = _injection_data_dir(data_dir)
    except Exception:
        return _selector_silent(budget_tokens=budget)
    # A path that is already a regular file cannot support ledger reads or
    # writes.  Fail closed before invoking retrieval, as the selector contract
    # treats delivery-state failures as a silent event.
    try:
        if os.path.exists(resolved_data) and not os.path.isdir(resolved_data):
            return _selector_silent(budget_tokens=budget)
    except OSError:
        return _selector_silent(budget_tokens=budget)

    # Lazy imports are essential: recall.py imports this module's gate helpers.
    from storelib import delivery_ledger as ledger  # type: ignore
    from storelib import ops_tokens as ops  # type: ignore
    from storelib import recall as recall_module  # type: ignore
    from storelib.cross_encoder import cli_allowed as _ce_cli_allowed

    # Resolve/derive operation context only on the pretool lane.  The current
    # raw event is a private hook-to-store input, not a public selector
    # surface: when it yields operation tokens those outrank the older ring.
    # Explicitly supplied [] otherwise intentionally suppresses ring reads.
    effective_query = query or ""
    # Issue #253: a prompt-less SubagentStart (Claude Code's real payload)
    # takes the delegating prompt parked store-side at PreToolUse(Agent) as
    # its recall query — event fields keep structural precedence (the
    # consume only fires when the payload ladder produced nothing). Fail-open
    # and moment-scoped. Issue #339: the consume is CALLER-GATED — only the
    # two SubagentStart lanes pass consume_tasktext=True (the local hook's
    # recall/recent argv and the fleet broker's SubagentStart prefetch);
    # every other caller that arrives with moment="subagent" and an empty
    # query (plain prefetch, the MCP surface, the offline snapshot serve) is
    # byte-identical to a no-stash session and can no longer DRAIN the
    # parked delegation. Every caller that arrives with a query is
    # byte-identical regardless of the grant. Issue #428: the granted
    # consume carries the child's SubagentStart agent id (when the host
    # supplied one) so a TAGGED park is delivered to its own child; absent
    # or unmatched ids fall to the untagged FIFO/aged-head rule.
    bridged_query = False
    if consume_tasktext and moment == "subagent" and not effective_query.strip():
        try:
            bridged = ledger.consume_task_text(
                resolved_data, session_id, agent_id=consume_agent_id or "")
            if isinstance(bridged, str) and bridged.strip():
                effective_query = bridged[:500]
                bridged_query = True
        except Exception:
            pass
    effective_ops = list(ops_tokens) if ops_tokens is not None else []
    checkpoint = ""
    query_context_enabled = True
    if moment == "pretool":
        try:
            query_context_enabled = bool(ops.query_context_enabled())
        except Exception:
            query_context_enabled = False
    current_ops = []
    if (moment == "pretool" and query_context_enabled
            and isinstance(pretool_input, dict)):
        try:
            composed = ops.compose_pretool_query(pretool_input)
            if (isinstance(composed, tuple) and len(composed) == 2
                    and isinstance(composed[0], (list, tuple))
                    and isinstance(composed[1], str)):
                current_ops = [token for token in composed[0]
                               if isinstance(token, str)]
                checkpoint = composed[1]
        except Exception:
            # Raw transport is optional enrichment.  A version-skewed or
            # malformed matcher must retain the established query/ring path.
            current_ops = []
            checkpoint = ""
    if moment == "pretool" and current_ops:
        effective_ops = current_ops
    elif moment == "pretool" and ops_tokens is None and query_context_enabled:
        try:
            events = ops.read_ops_ring(resolved_data, session_id)
            effective_ops = ops.derive_ops_tokens(*events)
        except Exception:
            effective_ops = []
        # PreToolUse's event payload is the authoritative fallback when the
        # optional post-tool ring has not recorded this operation yet.  The
        # derivation still runs here in the store boundary, so adapters never
        # need to import or duplicate the allowlist logic.
        if not effective_ops:
            try:
                effective_ops = ops.derive_ops_tokens(effective_query)
            except Exception:
                effective_ops = []
    if (moment == "pretool" and query_context_enabled
            and (effective_ops or checkpoint)):
        try:
            effective_query = ops.compose_inject_query(effective_query,
                                                       " ".join(effective_ops),
                                                       checkpoint)
        except Exception:
            pass

    # Issue #98: the cross-project tier's surface policy is store-side, so
    # the selector derives the flag itself (the public #158 signature is
    # frozen — no new parameter) and forwards the moment/ops context through
    # the internal recall seam below. On an env-enabled user_prompt surface
    # the tier is still hazard-gated, so derive the ops tokens from the
    # prompt event here at the store boundary — the same reason pretool
    # derivation lives in this file (adapters never import or duplicate the
    # allowlist; the #158 boundary keeps the hook body storelib-free). The
    # post-tool ring stays pretool-only by design.
    try:
        include_cross_project = bool(
            recall_module.cross_project_surface_enabled(moment))
    except Exception:
        include_cross_project = False
    if (include_cross_project and moment == "user_prompt"
            and ops_tokens is None and effective_query.strip()):
        try:
            effective_ops = ops.derive_ops_tokens(effective_query)
        except Exception:
            effective_ops = []

    # Query-less passive pulls are the documented recent lane. Resolve its
    # env-tunable floor at the store boundary, while preserving an explicit
    # CLI/API floor (notably session-aware ``recent --min-confidence``).
    effective_min_confidence = min_confidence
    if not effective_query.strip() and effective_min_confidence is None:
        effective_min_confidence = inject_recent_floor()

    # Issue #235: the user:global tier floor is store-side (the hook argv
    # surface is frozen).  An explicit caller argument wins; otherwise the
    # env-derived floor arms only on gated moments — pretool and subagent
    # (hooks' posttoolbatch maps to pretool store-side, so the post-tool
    # batch class is covered).  session_start/user_prompt/precompact keep
    # their current global composition; a resolved 0.0 disables (clamped in
    # _user_global_floor, threaded as 0.0 so recall.py's `> 0` guard skips).
    # An explicit argument that is not a finite number (PR #268 review) is
    # treated as UNSET — the passive lane never raises, so garbage falls
    # through to the env/default resolution exactly like a garbage env
    # value does, instead of escaping as ValueError/TypeError; a negative
    # explicit value is a deliberate operator request and clamps to 0.0
    # (floor disabled).
    normalized_floor = None
    if user_global_floor is not None:
        try:
            parsed_floor = float(user_global_floor)
        except (TypeError, ValueError, OverflowError):
            parsed_floor = None
        if parsed_floor is not None and (
                parsed_floor != parsed_floor
                or parsed_floor in (float("inf"), float("-inf"))):
            parsed_floor = None
        if parsed_floor is not None:
            normalized_floor = max(0.0, parsed_floor)
    if normalized_floor is not None:
        effective_user_global_floor = normalized_floor
    elif moment in ("pretool", "subagent"):
        effective_user_global_floor = _user_global_floor()
    else:
        effective_user_global_floor = None

    # Validation above intentionally precedes this first ledger call.
    try:
        # Issue #252 review (P-005/P-022): one merged read instead of two —
        # delivered_ids is exactly the id projection of the reduce that
        # delivered() returns, so this halves the hot-path cost and the two
        # views can no longer disagree across a compaction landing between
        # them.
        delivered_entries = list(ledger.delivered(
            resolved_data, session_id,
            principal=principal, harness=harness))
        delivered_ids = [e["id"] for e in delivered_entries
                         if isinstance(e, dict)
                         and isinstance(e.get("id"), str)]
    except Exception:
        return _selector_silent(budget_tokens=budget)
    delivered_ids = _ordered_unique(delivered_ids)
    # Issue #501 follow-up (PR #534): the protected rows this session
    # already rendered as BUDGET-CLIPPED stubs. A clipped row is never
    # acknowledged, so without this the same row re-claims the budget on
    # every prompt (the F-01 starvation). The admission lets a recorded
    # row yield while it still cannot be delivered whole.
    try:
        clipped_ids = ledger.read_clipped(
            resolved_data, session_id,
            principal=principal, harness=harness)
    except Exception:
        clipped_ids = []
    delivered_by_id = {
        e.get("id"): e for e in delivered_entries
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    }
    # Issue #253, bridged-consume carve-out: the delegating parent's pretool
    # pull records its rendered rows under the session id the child SHARES
    # (SubagentStart carries the parent's id), and a subagent is a fresh
    # context — rows the parent context saw must not suppress the child's
    # task-keyed recall.  When the selector consumed a parked delegation
    # (bridged_query), the pull drops its delivered-derived exclusions
    # ENTIRELY: the fresh child re-reads rows the parent (or a sibling child)
    # already rendered, which is over-delivery of already-seen rows rather
    # than starvation.  That direction is deliberate — narrowing the set to
    # subagent-moment rows would let the first bridged child suppress the
    # rows the second one needs when their queries are related, which is the
    # starvation this carve-out exists to prevent (see issue #331).  Explicit
    # exclude_ids always apply, and non-bridged pulls keep the full
    # delivered exclusion set byte-identical.
    if bridged_query:
        delivered_ids = []
    # Explicit exclusions precede delivered ids, preserving first-seen order
    # and making duplicate ids deterministic across callers.
    exclusions = _ordered_unique(list(exclude_ids or []) + delivered_ids)
    if moment == "pretool" and ops_tokens is None and effective_ops:
        strong: set = set()
        try:
            strong = {
                rid for rid, entry in delivered_by_id.items()
                if rid and ledger.strong_token_match(entry.get("text", ""), effective_ops)
            }
            exclusions = [rid for rid in exclusions if rid not in strong]
        except Exception:
            pass
        # Issue #307 (decision comment 6639125597): a hazard neighbour that
        # was ledgered as this parent's CONTRADICTS expansion child stays
        # excluded above — its own text never strong-matches, and nothing
        # modelled the delivery relationship — so its [CONTESTED LINK] marker
        # silently vanished from exactly the re-delivery envelope that
        # matters (the operator is about to run the hazardous op the parent
        # describes). Release the ledgered contradicts neighbours of the
        # strong-released parents: delivered-derived ids only (never widens
        # to a row the session has not seen), minus explicit exclude_ids
        # (caller exclusions always apply to THIS arm; the parent arm's
        # strong-match release above deliberately overrides explicit
        # exclusions — the hazard override pinned by
        # ParentStrongReleaseIgnoresExplicitExclusionTest), contradicts-only
        # (a related/supports neighbour carries no marker and re-delivering
        # it is pure budget noise — out of the issue's scope), ONE batched
        # query over all released parents. This block sits in its OWN try,
        # isolated from the parent release above: a pre-v11 store without
        # memory_link (or any failure here) degrades to parent-only release
        # — today's behaviour, never worse. No-op on bridged pulls
        # (delivered_ids was emptied above). The release un-suppresses; it
        # never force-includes — the parent must still be retrieved, and
        # the neighbour re-renders through the expansion walk that stamps
        # contested_link/link_of (a direct-retrieved neighbour renders
        # unmarked by design: the marker is expansion provenance; maintainer
        # review F-02 — re-render is retrieval-driven, and a neighbour that
        # direct-matches the query renders without the marker, which no
        # test pins). Failures here warn ONCE per process (maintainer F-03):
        # the fail-open must not be indistinguishable from feature-off.
        if strong and delivered_ids:
            try:
                _ph = ",".join("?" * len(strong))
                _edges = conn.execute(
                    "SELECT src_id, dst_id FROM memory_link "
                    f"WHERE relation = 'contradicts' "
                    f"AND (src_id IN ({_ph}) OR dst_id IN ({_ph}))",
                    (*strong, *strong),
                ).fetchall()
                _parents = set(strong)
                _release = {
                    (e["src_id"] if e["dst_id"] in _parents else e["dst_id"])
                    for e in _edges
                }
                _release &= set(delivered_ids)
                _release -= set(exclude_ids or [])
                if _release:
                    exclusions = [rid for rid in exclusions
                                  if rid not in _release]
            except Exception as _exc:
                # Fail-open: parent-only release (see comment above). Warn
                # ONCE per process (maintainer F-03) so the degraded mode is
                # not indistinguishable from feature-off; stderr only — the
                # hook envelope's shape is pinned and must not grow keys.
                global _CONTESTED_RELEASE_WARNED
                if not _CONTESTED_RELEASE_WARNED:
                    _CONTESTED_RELEASE_WARNED = True
                    try:
                        import sys as _sys
                        print(
                            "[zmem] warning: contested-link re-delivery "
                            "disabled by a query failure ("
                            + type(_exc).__name__
                            + "); falling back to parent-only release",
                            file=_sys.stderr,
                        )
                    except Exception:
                        pass

    # Discover and gate page candidates before invoking ordinary recall so its
    # budget stays byte-identical on calls where no usable page exists. Only a
    # real combined pool asks recall to retain its pre-budget canonical rows.
    page_candidates_pre = []
    page_candidate_ids_pre = []
    page_rows_pre = []
    try:
        from storelib import pages as _pages  # type: ignore
        page_candidates_pre = _pages._page_candidates_for_selector(
            conn, data_dir=resolved_data, query=effective_query,
            namespace=namespace, moment=moment, lane=lane,
            user_global_floor=effective_user_global_floor)
        page_candidate_ids_pre = [r.get("id") for r in page_candidates_pre
                                  if isinstance(r.get("id"), str)]
        page_rows_pre = [r for r in page_candidates_pre if r.get("id") not in exclusions]
        # Derived pages use the same emit-time prompt-injection classifier as
        # canonical recall rows. A risky page is withheld before budget and
        # never suppresses any represented source.
        page_rows_pre = [r for r in page_rows_pre
                         if not recall_module._classify_injection(r)]
        page_rows_pre, _page_status, _page_stats = selective_inject_filter(
            page_rows_pre, with_stats=True)
    except Exception:
        page_candidates_pre = []
        page_candidate_ids_pre = []
        page_rows_pre = []

    capture: dict = {}
    try:
        # Issue #125: cross_rerank goes ONLY in the query branch — it must
        # NEVER enter this shared dict, because recent_memory has no
        # cross_rerank parameter and the blanket except below would convert
        # that TypeError into a silent empty envelope on every query-less
        # passive pull (plan-critic round-1 finding).
        kwargs = dict(
            namespace=namespace, limit=limit, as_json=False, no_bump=True,
            # Issue #283 (T-04): one authorization-aware selection policy.
            # Unscoped callers keep the unconditional global union; a scoped
            # caller's allow-list gates the user:global lane here (both
            # dispatch branches ride this same dict) and constrains the
            # cross-project lane via _authorized_namespaces.
            include_global=(
                True if authorized_namespaces is None
                else ("user:global" in authorized_namespaces)),
            global_limit=global_limit,
            no_telemetry=True, for_injection=True,
            exclude_ids=exclusions, _capture=capture,
            # Keep the ordinary retrieval/gate candidate set intact only when
            # a validated derived row will join it. Page-free calls retain the
            # exact historic recall budget and ordering.
            _injection_budget_tokens=(max(budget, 1000000) if page_rows_pre else budget),
            min_confidence=effective_min_confidence,
            include_cross_project=include_cross_project,
            _cross_moment=moment,
            _cross_ops_tokens=list(effective_ops) or None,
            # Issue #137: the fence identity is the session id plus the
            # runtime moment — represented-row suppression never crosses it.
            _fence_id="%s:%s" % (session_id, moment),
            # Issue #235: the user:global tier floor rides the SAME dict on
            # both dispatch branches (query -> recall_memory, query-less ->
            # recent_memory); an asymmetric kwarg here is the PR #225
            # fail-open class, pinned by tests/test_user_global_floor.py.
            _user_global_floor=effective_user_global_floor,
            _authorized_namespaces=authorized_namespaces,
            # Issue #389 (AC8): the prompt's own clock, threaded from the
            # CLI prefetch flag / MCP prefetch tool / offline snapshot
            # serve. Both dispatch branches accept it (recall_memory and
            # recent_memory normalize an invalid value to the env seam —
            # never an error); None keeps the ZMEM_TEST_NOW/now seam.
            prompt_timestamp=prompt_timestamp,
            # Issue #501 follow-up (PR #534): the cross-prompt defer set
            # rides the SHARED dict because BOTH dispatch targets own an
            # admission that can clip a protected row (query -> recall,
            # query-less -> recent). Both accept `_defer_ids` (review
            # FB-01201: the query-less lane used to clip without it, so a
            # recorded row never yielded on a prompt without a query).
            _defer_ids=clipped_ids or None,
        )
        if effective_query.strip():
            kwargs["query"] = effective_query
            # Issue #125 passive gate: the selector is the only consumer of
            # the passive cross-encoder lane (CE + PASSIVE=1 + no-bump).
            kwargs["cross_rerank"] = _ce_cli_allowed(
                no_bump=True, no_hybrid=False, for_injection=True)
            # Issue #397: the repository-state hint rides ONLY the query
            # branch — never the shared kwargs dict, whose query-less leg
            # feeds recent_memory (an unexpected kwarg there is swallowed
            # by the selector's fail-open except as a silent empty pool).
            if state_hint:
                kwargs["_state_hazard_hint"] = state_hint
            recall_module.recall_memory(conn, **kwargs)
            # Issue #337: bridged zero-hit fallback. When the bridged query
            # matched NOTHING — the COMBINED query-shaped result is empty
            # (no ordinary capture rows AND no eligible curated pages; a
            # page-only hit through its derived bullet prose is preserved,
            # not replaced) — the child falls back to the recent lane it
            # received before #253 instead of an empty context. State
            # transition (plan rev 3): the first capture is REPLACED (not
            # merged), query-only kwargs (query / cross_rerank /
            # _state_hazard_hint) are dropped, the effective query resets
            # so the "Recent memories" header renders, the recent
            # confidence floor applies exactly as the query-less path
            # computes it, and bridged_query stays True so the #331
            # carve-out keeps the fallback's exclusions empty. Final token
            # admission below reads only this fallback capture.
            if bridged_query:
                probe = capture.get("results") if capture else None
                if not probe and not page_rows_pre:
                    capture.clear()
                    kwargs.pop("query", None)
                    kwargs.pop("cross_rerank", None)
                    kwargs.pop("_state_hazard_hint", None)
                    effective_query = ""
                    if effective_min_confidence is None:
                        effective_min_confidence = inject_recent_floor()
                    kwargs["min_confidence"] = effective_min_confidence
                    recall_module.recent_memory(conn, **kwargs)
        else:
            recall_module.recent_memory(conn, **kwargs)
    except Exception:
        return _selector_silent(budget_tokens=budget, excluded=exclusions)

    try:
        parsed = dict(capture) if capture else {"results": []}
        if not isinstance(parsed, dict):
            parsed = {"results": parsed if isinstance(parsed, list) else []}
        rows = parsed.get("results", [])
        if not isinstance(rows, list):
            rows = []
        candidate_ids = _ordered_unique(parsed.get("candidate_ids", []))
        # If a test seam returns a bare list, retain a useful candidate set.
        if not candidate_ids:
            candidate_ids = [r.get("id") for r in rows
                             if isinstance(r, dict) and isinstance(r.get("id"), str)]
        # Curated pages are discovered at the common selector boundary.  The
        # page helper validates immutable artifacts and rechecks live source
        # and evidence membership; failures simply withhold the derived row.
        # This preserves the existing recall path for ordinary rows.
        ordinary_rows = list(rows)
        rebudget_stats = None
        try:
            page_rows = list(page_rows_pre)
            candidate_ids = _ordered_unique(candidate_ids + page_candidate_ids_pre)
            # PR #526 feedback (FB-109 / parallel-review MX-UR-08): a page
            # whose bullet embeds the content of a row the prompt-assertion
            # partition dropped would re-deliver the suppressed fact verbatim
            # (admit_with_pages removes the page's represented canonical
            # sources AFTER the partition). Withhold such pages here — their
            # canonical sources stay partition-dropped (they are not
            # restored), matching the suppression contract.
            page_rows = _page_rows_withhold_partition_overlaps(
                page_rows, capture.get("attribute_drop_labels") or {})
            if page_rows_pre and not page_rows:
                # PR #526 feedback (Stage-B r1, FB-109 residual): EVERY page
                # was withheld for partition overlap, so the combined
                # real-budget admission below never runs — but the canonical
                # pool arrived under the WIDENED budget. Re-admit it under
                # the real budget now, exactly as the no-page path received
                # it, and keep the REAL-budget statistics for the envelope
                # (Stage-B r2: the widened pass's counters describe a
                # decision that no longer applies).
                rows, _used, _dropped, rebudget_stats = apply_token_budget(
                    ordinary_rows, budget, with_stats=True,
                    legacy_injection_wire=True,
                    defer_ids=clipped_ids)
            if page_rows:
                # Recall retained its canonical pool with a widened budget so
                # the final combined pass is the only admission decision.
                # First let pages compete without rows they represent.  If a
                # page does not survive (including credential withholding),
                # restore its canonical rows and re-admit that fallback pool.
                page_secret_markers = []
                safe_page_rows = []
                for row in page_rows:
                    if (row.get("withheld_for_secret")
                            or recall_module._classify_credential(row)):
                        # Keep the canonical credential marker in the
                        # envelope, admit its exact fenced cost, and never
                        # let it suppress its represented canonical sources.
                        # Review round 2 (R2-1): the shared stub builder is
                        # the single source of truth — namespace-blank law
                        # AND the fixed-vocabulary trust/marker keys (the
                        # literal copy here had already drifted from it
                        # once; maintainer F-04 / PRR-002).
                        page_secret_markers.append(
                            recall_module._withhold_credential_stub(row))
                    else:
                        safe_page_rows.append(row)
                page_rows = safe_page_rows
                # Every repeat uses the original page object, never a row
                # returned from an earlier admission pass.  That matters if a
                # page ever becomes a protected type: a prior clipped copy
                # must not become artificially cheap on the next pass.
                pages_by_id = {
                    str(row["id"]): row for row in page_rows
                    if isinstance(row.get("id"), str)
                }
                page_ids = _ordered_unique([
                    str(row["id"]) for row in page_rows
                    if isinstance(row.get("id"), str)
                ])

                def admit_with_pages(active_page_ids):
                    represented = {
                        str(source_id)
                        for page_id in active_page_ids
                        for source_id in (pages_by_id[page_id].get("represented_ids")
                                          or pages_by_id[page_id].get("source_ids") or [])
                        if isinstance(source_id, str)
                    }
                    pool = [row for row in ordinary_rows
                            if row.get("id") not in represented]
                    pool.extend(pages_by_id[page_id] for page_id in active_page_ids)
                    # Markers are id/type-only rows, but they still consume
                    # their exact final-fence cost.  They represent no source
                    # and therefore never participate in suppression.
                    pool.extend(page_secret_markers)
                    admitted, used, dropped, stats = apply_token_budget(
                        pool, budget, with_stats=True,
                        legacy_injection_wire=True,
                        defer_ids=clipped_ids)
                    survivors = _ordered_unique([
                        str(row["id"]) for row in admitted
                        if row.get("type") == "page"
                        and isinstance(row.get("id"), str)
                        and str(row["id"]) in pages_by_id
                    ])
                    return admitted, used, dropped, stats, survivors

                # Initial admission decides the only page IDs eligible for
                # later passes.  A rejected page is never reintroduced.
                rows, _used, _dropped, _budget_stats, active_page_ids = admit_with_pages(page_ids)
                # Recompute from original canonical rows plus the surviving
                # page set until it is stable.  Each non-stable pass removes at
                # least one ID, so this is bounded by eligible pages + one.
                for _pass in range(len(active_page_ids) + 1):
                    if not active_page_ids:
                        rows, _used, _dropped, _budget_stats, _ignored = admit_with_pages([])
                        break
                    rows, _used, _dropped, _budget_stats, survivors = admit_with_pages(active_page_ids)
                    if survivors == active_page_ids:
                        break
                    active_page_ids = survivors
                else:  # defensive: monotonicity above makes this unreachable
                    rows, _used, _dropped, _budget_stats, _ignored = admit_with_pages([])
                    active_page_ids = []
                # These fields describe the final combined admission, rather
                # than a preliminary widened recall pass.
                omitted_pages = len(page_ids) - len(active_page_ids)
                if omitted_pages:
                    _budget_stats = dict(_budget_stats)
                    _budget_stats["dropped"] = int(_budget_stats.get("dropped", 0) or 0) + omitted_pages
                    _dropped = _budget_stats["dropped"]
                parsed["budget_dropped"] = _dropped
                parsed["budget_admission"] = _budget_stats["admission_used"]
                parsed["budget_truncated"] = _budget_stats["truncated"]
                parsed["budget_dropped_protected"] = _budget_stats["dropped_protected"]
                parsed["budget_note"] = budget_note(_budget_stats)
        except Exception:
            # Page discovery is derived-data enrichment. Existing canonical
            # injection must retain its fail-safe behavior under the original
            # caller budget, never the temporary widened recall budget.
            rows, _used, _dropped, _stats = apply_token_budget(
                ordinary_rows, budget, with_stats=True,
                legacy_injection_wire=True,
                defer_ids=clipped_ids)
        # Issue #256: read-time credential re-scan on the passive lane —
        # the defense-in-depth twin of the prompt-injection re-scan. Every
        # selected row matching SECRET_CREDENTIAL_PATTERNS (content /
        # source_ref / tags) is replaced by an id+type-only withheld marker
        # BEFORE rendering, the bump list, and the ledger. No new store
        # writes: the row is never mutated, only its delivery is filtered.
        withheld_rows = []
        displayed_rows = []
        for row in rows:
            if isinstance(row, dict) and (
                    # F-267-1: rows classified pre-budget in recall.py arrive
                    # already flagged (their clipped content no longer
                    # matches the patterns, so the re-scan alone would miss
                    # them) — trust the flag first.
                    row.get("withheld_for_secret")
                    or recall_module._classify_credential(row)):
                withheld_rows.append(row)
                # Review round 2 (R2-1): the passive hook fence renders this
                # stub — route it through the shared builder so withheld
                # rows keep their trust markers ([tier=…], [INJECTION
                # RISK], [UNTRUSTED TOOL/WEB], contested link) on the
                # model-visible passive surface too, and a credential-shaped
                # namespace is blanked here exactly as on the explicit lanes
                # (maintainer F-04 / PRR-002).
                displayed_rows.append(
                    recall_module._withhold_credential_stub(row))
            else:
                displayed_rows.append(row)
        rows = displayed_rows
        # A page suppresses represented canonical rows only after it survived
        # the credential/withholding pass and was actually rendered. A
        # rejected, budget-dropped, or withheld page therefore hides nothing.
        delivered_page_sources = set()
        for row in rows:
            if row.get("type") == "page" and not row.get("withheld_for_secret"):
                delivered_page_sources.update(row.get("represented_ids") or row.get("source_ids") or [])
        if delivered_page_sources:
            rows = [row for row in rows if row.get("type") == "page" or row.get("id") not in delivered_page_sources]
        if page_rows_pre:
            # Match recall.py's envelope metric: row content only.  The
            # separate ``budget_admission`` field carries full fence costs.
            parsed["tokens_used"] = sum(
                estimate_tokens(row.get("content", "") or "")
                for row in rows if isinstance(row, dict)
            )
        secret_withheld_count = len(withheld_rows)
        excluded = [rid for rid in exclusions if rid in candidate_ids]
        if any(row.get("type") == "page" and not row.get("withheld_for_secret")
               for row in rows if isinstance(row, dict)):
            reason = "injected"
        elif not rows and candidate_ids and excluded and set(candidate_ids) <= set(excluded):
            reason = "already-delivered"
        else:
            reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else "empty-pool"
        header_kind = "Relevant memories" if effective_query.strip() else "Recent memories"
        header = (
            f"{header_kind} (zmem {moment}, namespace {namespace or 'unscoped'}). "
            "Consider if they apply to this task; ignore if not."
        )
        # Resolve the canonical renderer through the module attribute at call
        # time so both the public shim and module-level test seams remain
        # observable.
        rendered = ""
        if rows:
            rendered = recall_module._format_fenced_recall(
                rows, header=header, budget_note=parsed.get("budget_note"),
                legacy_injection_wire=True)
        # Issue #501 follow-up (PR #534): record the protected rows this
        # render clipped, so the NEXT prompt can let them yield the
        # budget to the rows that fit (the fair cross-prompt selection).
        # Fail-open: a lost write costs one extra stub render.
        try:
            ledger.record_clipped(
                resolved_data, session_id,
                [str(r.get("id")) for r in rows
                 if isinstance(r, dict) and r.get("_incomplete_delivery")],
                principal=principal, harness=harness)
        except Exception:
            pass
        # Two accounting sets, one source of truth for "was this rendered".
        #
        #   ledger_rows  DELIVERED (#114): every row proven present in the
        #                rendered fence. Suppression depends on this being
        #                complete — the injection lane's exclusion input is the
        #                caller's exclude_ids merged with delivered_ids, and
        #                only delivered_ids can ever contain a neighbour.
        #   bump_rows    POPULARITY (#136): query-matched rows only.
        #                Link-expansion and graph-only neighbours render but
        #                must not be rewarded, or a supplementary neighbour
        #                outranks a genuine match in later recalls.
        #
        # Issue #250: sharing ONE filtered list silently exempted expansion and
        # graph-only rows from the delivery ledger too, so a neighbour pulled
        # in by an already-delivered parent re-rendered on every subsequent
        # event in the session — the suppression path was correct but starved.
        ledger_rows = ledger.rows_present_in(rows, rendered)
        # Issue #256: a withheld row is not rendered content (#114 rendered-row
        # law) — never bumped, never ledgered. Shape-dependent backstop: today
        # the [WITHHELD: SECRET] prefix already fails rows_present_in's
        # marker whitelist, but that auto-drop depends on the fence shape; if
        # the marker ever matched, this filter is the sole bump/ledger guard
        # (proven by the c-iso mutation probe). Applied as the LAST filter on
        # ledger_rows so a single guard covers both consumers and bump_rows
        # stays a subset of ledger_rows by construction.
        #
        # Issue #501 (audit D02): a budget-clipped row is an incomplete
        # delivery — the [id] marker surviving the clip must NOT count as
        # delivery proof, so it rides the SAME final filter (never bumped,
        # never ledgered; record/reserve/bump all derive from ledger_rows):
        # render, never acknowledge; the complete-row retry re-selects.
        # A clipped row is NEVER RECORDED, so the delivery window (which
        # governs RECORDED rows) does not bound it: it re-renders on every
        # prompt until a budget admits it whole. The admission cap in
        # apply_token_budget keeps that repeat from starving the rows that
        # DO fit (PR #534 feedback F-01).
        ledger_rows = [row for row in ledger_rows
                       if not row.get("withheld_for_secret")
                       and not row.get("_incomplete_delivery")]
        # Expansion rows are rendered but deliberately NOT bumped —
        # popularity rewards query-MATCHED rows only (recall.py bump law).
        bump_rows = [row for row in ledger_rows
                     if not row.get("link_relation")
                     and not row.get("_graph_arrival_only")]
        if bump_rows and not reserve and not no_telemetry:
            try:
                recall_module._bump_telemetry(
                    conn, [r["id"] for r in bump_rows], no_bump=True)
            except Exception:
                pass
        reservation_id = None
        try:
            # Issue #250: the recorded set is every RENDERED row, which is
            # larger than the set #136 allows to be rewarded. Name the
            # query-matched subset so a downstream consumer (operation
            # feedback, the #249 report) can tell the two apart from the entry
            # instead of re-deriving arrival shape.
            if reserve and not no_telemetry:
                # Issue #284 (T-05): the reserving caller acknowledges the
                # reservation via prefetch-ack; delivery is NOT committed
                # here. The sidecar carries the ledger_rows ids and their
                # entry_text fuel (the rendered set — the #114 fence-bound
                # law, never candidate_ids), and reserve() returns None when
                # every id is already delivered (the caller keeps today's
                # already-delivered turn).
                reservation_id = ledger.reserve(
                    resolved_data, session_id, ledger_rows, moment,
                    bump_eligible_ids=[r["id"] for r in bump_rows],
                    principal=principal, harness=harness)
            elif not no_telemetry:
                # Issue #362 remainder (0.118.0): the non-reserve record path
                # goes through the admit-once admission (exactly one winner
                # per (session, memory) pair, loser observes
                # already-delivered) with refresh_delivered=True so the
                # escalation re-delivery and the bridged-subagent re-record
                # still refresh their ledger entries under the shared upsert
                # law. The emission-side window (render before record) stays
                # the disclosed residual bounded by next-turn suppression.
                ledger.record_undelivered(
                    resolved_data, session_id, ledger_rows, moment,
                    bump_eligible_ids=[r["id"] for r in bump_rows],
                    principal=principal, harness=harness,
                    refresh_delivered=True)
        except Exception:
            # Rendering has already produced safe fenced text; a delivery
            # write failure is fail-open and must not discard that text.
            reservation_id = None
        from storelib.recall import build_injection_envelope
        # PR #526 feedback (Stage-B r3, FB-109): apply_token_budget's RAW
        # stats keys are dropped/admission_used/truncated/dropped_protected
        # (this module's stats contract), while the captured envelope
        # carries the mapped budget_* names (recall.py's details builder).
        # Map the real-budget re-admission's counters explicitly so the
        # envelope describes THAT decision.
        if rebudget_stats is not None:
            budget_counters = {
                "budget_dropped": rebudget_stats.get("dropped", 0),
                "budget_admission": rebudget_stats.get("admission_used", 0),
                "budget_truncated": rebudget_stats.get("truncated", 0),
                "budget_dropped_protected": rebudget_stats.get(
                    "dropped_protected", 0),
                "budget_note": budget_note(rebudget_stats),
            }
        else:
            budget_counters = parsed
        return build_injection_envelope(
            rows, omitted=parsed.get("omitted", 0), reason=reason,
            excluded=excluded, candidate_ids=candidate_ids,
            tokens_used=parsed.get("tokens_used", 0), tokens_budget=budget,
            budget_dropped=budget_counters.get("budget_dropped", 0),
            budget_admission=budget_counters.get("budget_admission", 0),
            budget_truncated=budget_counters.get("budget_truncated", 0),
            budget_dropped_protected=budget_counters.get(
                "budget_dropped_protected", 0),
            arms=parsed.get("arms", {}), rendered=rendered,
            effective_ops=list(effective_ops) if _capture_envelope_enabled() else None,
            injection_risk=parsed.get("injection_risk"),
            secret_withheld=secret_withheld_count or None,
            # Issue #235: recall.py's global-tier seam counts floor-withheld
            # user:global candidates into the captured details; forward it
            # so the envelope names the withheld slot (keyed only when a
            # withhold happened, mirroring secret_withheld).
            global_withheld=parsed.get("global_withheld"),
            candidate_lanes=parsed.get("candidate_lanes"),
            budget_note=(budget_counters.get("budget_note")
                         if rebudget_stats is not None
                         else parsed.get("budget_note")) if rows else None,
            # Issue #174 (R3-4): forward the live-expiry count keyed-only-
            # when-engaged — the store lane keys it only when it saw a
            # memory_expiry side row, and None keeps the wire unchanged.
            expired=parsed.get("expired"),
            # Issue #342: forward the withheld-because-unreadable count
            # keyed-only-when-present (a side-table read failure with the
            # table present); None keeps the wire unchanged.
            expiry_read_failed=parsed.get("expiry_read_failed"),
            # Issue #284 (T-05): the reservation id rides the envelope
            # keyed-only-when-present (registered above); the caller
            # acknowledges it via `prefetch-ack` to commit delivery.
            reservation=reservation_id,
        )
    except Exception:
        return _selector_silent(budget_tokens=budget, excluded=exclusions)
