"""Gold-set loading, validation, and offline eval metrics (issue #64).

Single implementation shared by both eval surfaces:
  - ``scripts/eval_runner.py`` (the canonical runner, used by CI) and
  - ``storelib/tune.py`` (the ``store.py tune-weights --dry-run`` evaluator).

A gold item is one JSON object per line:
    {"id": "...", "bucket": "...", "query": "...",
     "namespace": "...",          optional
     "as_of": "...",              optional (ISO-8601)
     "k": 5,                      optional per-item top-k cut
     "must_include_ids": [...],   optional
     "must_exclude_ids": [...],   optional
     "must_include_text": "...",  optional
     "explicit": true}            optional (issue #82: run the item on the
                                  explicit path — no_bump=False, so the
                                  change-intent unfold can fire — while
                                  staying zero-write via no_telemetry=True.
                                  Default items stay on the passive path.)

Validation is fail-closed (GoldError): an invalid gold file must refuse the
run with a named item id, never silently degrade the metrics. Keep this module
stdlib-only apart from storelib imports (the runner bootstraps sys.path before
importing it).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# The six issue-mandated fixture buckets + "adapter" (the output bucket of
# scripts/eval_adapters.py, whose items assert must_include_text because the
# target corpus mints ids at import time) + the three issue-#82 honesty
# buckets. Documented exception to the ">= 5 items per bucket" rule: the
# three #82 buckets carry >= 3 items each (tests/test_eval_runner.py pins
# the split: original six >= 5, new three >= 3).
BUCKETS = ("as-of", "injection", "entity-alias", "namespace", "contested",
           "fts", "adapter", "retraction", "polarity", "change-intent",
           "decision-point", "negative-control")

# Issue #111: the four hook query shapes the injection gold scores. Each is a
# different query against the same store (the hook builds a different query
# string per moment — prose, ops tokens, task text, or a query-less recent
# pull), so the gold carries a `moment` per item and the harness reproduces
# exactly what the hook would send.
INJECTION_MOMENTS = ("user-prompt", "pretool", "subagent", "precompact")


class GoldError(ValueError):
    """Raised for any structurally invalid gold set. The runner maps this to
    exit 2 (operational refusal) naming the offending item id."""


@dataclass
class GoldItem:
    id: str
    bucket: str
    query: str
    namespace: str | None = None
    as_of: str | None = None
    k: int | None = None
    must_include_ids: list[str] = field(default_factory=list)
    must_exclude_ids: list[str] = field(default_factory=list)
    must_include_text: str | None = None
    explicit: bool = False
    # Issue #88 / #85 direction 4: prior-turn tool-operation context for
    # decision-point items. When present, the runner composes the query via
    # the SAME compose_inject_query the UserPromptSubmit hook uses (reserved
    # slice inside the 500-char cap) — the eval measures the hook's real
    # query, not a forked one. Items without ops run byte-identical to today.
    ops: str = ""
    # Issue #111: injection-gold fields (additive, defaulted — legacy items
    # never carry `moment` and keep byte-identical behavior). `moment` routes
    # the item through the hook's real per-moment lane; `expect="silent"`
    # marks a negative control (no genuine retrieval need): the rendered set
    # must be empty (silent), never injected.
    moment: str = ""
    expect: str = "inject"


def load_gold(path: str) -> list[GoldItem]:
    """Parse + validate a gold JSONL file. Fail-closed: the first invalid item
    raises GoldError naming that item (and the line number)."""
    items: list[GoldItem] = []
    seen_ids: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        raise GoldError(f"cannot read gold file {path}: {exc}") from exc

    for lineno, line in enumerate(raw_lines, start=1):
        line = line.strip()
        if not line:
            continue  # blank lines are structural noise, not items
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as exc:
            # RecursionError: a deeply-nested hostile row must fail closed
            # with the item line, not escape as a traceback (it is a
            # RuntimeError, not a ValueError, so it would bypass a plain
            # JSONDecodeError handler).
            raise GoldError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise GoldError(f"{path}:{lineno}: gold item must be a JSON object")
        try:
            item = _validate_item(obj)
        except GoldError as exc:
            raise GoldError(f"{path}:{lineno} (item {obj.get('id')!r}): {exc}") from exc
        if item.id in seen_ids:
            raise GoldError(f"{path}:{lineno}: duplicate gold item id {item.id!r}")
        seen_ids.add(item.id)
        items.append(item)

    if not items:
        raise GoldError(f"{path}: contains no gold items")
    return items


def _validate_item(obj: dict[str, Any]) -> GoldItem:
    item_id = obj.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise GoldError("missing required string field 'id'")
    bucket = obj.get("bucket")
    if not isinstance(bucket, str) or bucket not in BUCKETS:
        raise GoldError(
            f"field 'bucket' must be one of {', '.join(BUCKETS)}, got {bucket!r}"
        )
    # Issue #111: parse the injection-gold fields FIRST so the query rule can
    # relax for the query-less precompact lane (a recent pull has no prompt).
    moment = obj.get("moment", "")
    expect = obj.get("expect", "inject")
    if not isinstance(moment, str):
        raise GoldError("field 'moment' must be a string when present")
    if moment and moment not in INJECTION_MOMENTS:
        raise GoldError(
            "field 'moment' must be one of "
            f"{', '.join(INJECTION_MOMENTS)} when present, got {moment!r}"
        )
    if not isinstance(expect, str) or expect not in ("inject", "silent"):
        raise GoldError(
            "field 'expect' must be 'inject' or 'silent' when present, "
            f"got {expect!r}"
        )
    if expect != "inject" and not moment:
        raise GoldError(
            "field 'expect' requires 'moment' (legacy items are positive "
            "recall items)")
    query = obj.get("query", "")
    if moment == "precompact":
        if not isinstance(query, str):
            raise GoldError("field 'query' must be a string when present")
        if query.strip():
            raise GoldError(
                "a precompact item is a query-less recent pull; 'query' must "
                "be empty or omitted")
    else:
        if not isinstance(query, str) or not query.strip():
            raise GoldError("missing required non-empty string field 'query'")

    include_ids = obj.get("must_include_ids", [])
    exclude_ids = obj.get("must_exclude_ids", [])
    include_text = obj.get("must_include_text")
    for name, value in (("must_include_ids", include_ids),
                        ("must_exclude_ids", exclude_ids)):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise GoldError(f"field '{name}' must be a list of strings")
    overlap = sorted(set(include_ids) & set(exclude_ids))
    if overlap:
        raise GoldError(
            "must_include_ids and must_exclude_ids overlap: " + ", ".join(overlap)
        )
    if include_text is not None and not isinstance(include_text, str):
        raise GoldError("field 'must_include_text' must be a string")
    if moment and expect == "silent":
        # A negative control asserts by its silence: labeling relevant ids
        # would contradict the no-retrieval-need premise.
        if include_ids:
            raise GoldError(
                "a negative-control item (moment set, expect 'silent') must "
                "not carry must_include_ids")
    elif moment and not include_ids:
        raise GoldError(
            "an injection-gold positive (moment set, expect 'inject') must "
            "label must_include_ids")
    if not moment and not include_ids and not exclude_ids and not include_text:
        raise GoldError("item asserts nothing: give must_include_ids, "
                        "must_exclude_ids, or must_include_text")

    namespace = obj.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise GoldError("field 'namespace' must be a string")
    as_of = obj.get("as_of")
    if as_of is not None:
        # Fail closed on unparseable timestamps: recall's _normalize_as_of
        # returns unparseable strings UNCHANGED (degrade-don't-raise on the
        # hot path), which here would silently mis-filter the eval. An
        # unparseable as_of is a broken gold item, not degraded data.
        if not isinstance(as_of, str) or not as_of.strip():
            raise GoldError("field 'as_of' must be a non-empty ISO-8601 string")
        try:
            datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise GoldError(
                f"field 'as_of' must be parseable ISO-8601, got {as_of!r}: {exc}"
            ) from exc
    # Per-item top-k cut; None = inherit the caller's k_default (the CLI
    # --k). Deliberately NOT defaulted to 5 here — a hardcoded default would
    # make the runner's/tuner's --k a silent no-op for every item that omits
    # the field (PR-review PRR-009).
    k = obj.get("k")
    if k is not None and (isinstance(k, bool) or not isinstance(k, int) or k < 1):
        raise GoldError("field 'k' must be a positive integer when present")
    # Issue #82: optional explicit-path flag. Only JSON booleans are valid;
    # non-bool values are refused (a gold file with "explicit": "yes" is
    # broken data, not a style choice).
    explicit = obj.get("explicit", False)
    if not isinstance(explicit, bool):
        raise GoldError("field 'explicit' must be a boolean when present")
    # Issue #88 / #85 direction 4: optional prior-turn operation context.
    ops = obj.get("ops", "")
    if not isinstance(ops, str):
        raise GoldError("field 'ops' must be a string when present")
    return GoldItem(
        id=item_id,
        bucket=bucket,
        query=query,
        namespace=namespace,
        as_of=as_of,
        k=k,
        must_include_ids=list(include_ids),
        must_exclude_ids=list(exclude_ids),
        must_include_text=include_text,
        explicit=explicit,
        ops=ops,
        moment=moment,
        expect=expect,
    )


# ---------------------------------------------------------------------------
# Issue #111: the injection-direction precision gold. Where `evaluate_items`
# measures raw recall on a narrowed pipeline, `evaluate_injection_items`
# executes the hook's REAL lane (`for_injection=True` with the hook's flag
# parity: include-global, MMR on, default link expansion) and scores the
# RENDERED set — the rows that would land in the fence the agent sees.

INJECTION_PER_ITEM_REPORT_KEYS = (
    "id", "bucket", "moment", "expect", "namespace", "query", "ops_query",
    "as_of", "reason", "rendered_ids", "candidate_ids", "tokens_used",
    "tokens_budget", "hit", "precision", "first_hit_rank", "fence_ok", "ok",
)


class BypassError(RuntimeError):
    """Raised when the rendered set is inconsistent with the real gate +
    token budget having run (issue #111 no-silent-bypass contract). The
    runner maps this to exit 2 naming the item and the failed invariant.

    Deliberately NOT verified by calling ``selective_inject_filter`` /
    ``apply_token_budget`` back: a caller that stubbed those functions (the
    exact names the lane calls at recall.py's injection branch) would stub
    the verification too. Instead the invariants are re-derived from the
    pure primitives the gate/budget are built on — the floor constants and
    the per-row token cost — so stubbing the gate or budget functions
    cannot silence the check.
    """


def _injection_silent_reasons() -> tuple:
    # schema_meta lives at the TOP of skills/memory/scripts/ next to store.py
    # (same import discipline as inject.py's guarded import above).
    import schema_meta as _sm  # lazy: keep module import cheap
    return tuple(getattr(_sm, "INJECT_SILENT_REASONS",
                         ("empty-pool", "omitted", "below-bar", "budget-drop")))


def _verify_real_lane(item_id: str, rows: list[dict], envelope: dict,
                      fence: str, conn: sqlite3.Connection = None,
                      candidate_ids: list[str] = None,
                      candidate_lanes: dict | None = None) -> None:
    """Re-derive the lane's invariants from pure primitives (see
    BypassError). Raises BypassError naming the item on any violation.

    Threat model: the invariants are checked WITHOUT calling
    ``selective_inject_filter``/``apply_token_budget`` through the names the
    lane binds (``storelib.recall.selective_inject_filter`` /
    ``storelib.recall.apply_token_budget`` — stubbing those exact names is
    the "gate/budget stubbed out" scenario this contract exists to catch).
    The floor/budget arithmetic is re-derived from pure primitives, and the
    EXPECTED selection is independently reconstructed from the envelope's
    pre-gate ``candidate_ids`` by re-reading those rows from the store and
    applying the owning-module (``storelib.inject``) gate/budget — so a
    recall-level stub that drops or inflates the rendered set is detected
    even when every output invariant happens to hold (e.g. a stub that
    empties the set, or one that passes sub-floor rows).

    Known limitation: the reconstruction re-applies the budget to rows
    re-read from SQL, which carry ``confidence`` but not the recall-time
    ``_score`` composite — ``_row_priority`` therefore falls back to
    ``confidence`` ordering. When the token budget actually binds AND
    ``_score`` ordering differs from ``confidence`` ordering, a legitimate
    run could be refused (a false positive, never a false pass). The
    committed fixture never binds the budget (default 1500 vs max observed
    ``tokens_used`` 205); treat the reconstruction as a backstop against
    drop/inflate stubs, not a bit-exact replay under a binding budget."""
    import storelib.inject as _inject
    from storelib.recall import ZMEM_FENCE_CLOSE, ZMEM_FENCE_OPEN

    floor, gate_none_floor, grounded = _inject._gate_constants()

    def _conf(r) -> float:
        try:
            value = float(r.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        if value != value or value in (float("inf"), float("-inf")):
            return 0.0
        return value

    for r in rows:
        conf = _conf(r)
        rid = r.get("id", "?")
        sig = (r.get("signal") or "none").lower()
        if sig == "none":
            if conf < gate_none_floor:
                raise BypassError(
                    f"{item_id}: rendered row {rid} has signal=none "
                    f"confidence {conf} below the gate-none floor "
                    f"{gate_none_floor} — the selective-inject gate did not "
                    "run on the rendered set")
        elif sig in grounded:
            if conf < floor:
                raise BypassError(
                    f"{item_id}: rendered row {rid} has grounded "
                    f"signal {sig} confidence {conf} below the prompt floor "
                    f"{floor} — the selective-inject gate did not run")
        else:
            raise BypassError(
                f"{item_id}: rendered row {rid} carries ungrounded "
                f"signal {sig!r}, which the real gate never admits")
    budget = _inject.inject_token_budget()
    used = sum(_inject.row_token_cost(r) for r in rows)
    protected = getattr(_inject, "_PROTECTED_TYPES",
                        ("decision", "constraint"))
    all_protected = bool(rows) and all(
        (r.get("type") or "") in protected for r in rows)
    if used > budget and not all_protected:
        raise BypassError(
            f"{item_id}: rendered set costs ~{used} tokens, over the "
            f"{budget}-token budget, with non-protected rows present — the "
            "token budget did not run")
    for r in rows:
        if f"- [{r.get('id')}]" not in fence:
            raise BypassError(
                f"{item_id}: rendered row {r.get('id')} is absent from the "
                "fence text — metrics must come off the rendered fence")
    if rows and not (fence.startswith(ZMEM_FENCE_OPEN)
                     and ZMEM_FENCE_CLOSE in fence):
        raise BypassError(f"{item_id}: fence markers missing from the render")
    if envelope.get("tokens_budget") != budget:
        raise BypassError(
            f"{item_id}: envelope tokens_budget "
            f"{envelope.get('tokens_budget')!r} != resolved budget "
            f"{budget} — the envelope did not come from the injection lane")
    if (envelope.get("reason") == "injected") != bool(rows):
        raise BypassError(
            f"{item_id}: envelope reason {envelope.get('reason')!r} "
            f"inconsistent with {len(rows)} rendered rows")

    # Independent reconstruction: rebuild the expected selection from the
    # envelope's pre-gate candidate ids and compare membership with the
    # rendered set. This catches the remaining stub shape — a gate/budget
    # stub that DROPS rows (rendered empty or partial) — which the
    # output-only invariants above cannot see.
    if conn is not None and candidate_ids:
        placeholders = ",".join("?" * len(candidate_ids))
        cand_rows = [
            dict(r) for r in conn.execute(
                f"SELECT id, confidence, signal, type, content, "
                f"trust_score FROM memory "
                f"WHERE id IN ({placeholders})", candidate_ids)
        ]
        if len(cand_rows) != len(set(candidate_ids)):
            raise BypassError(
                f"{item_id}: only {len(cand_rows)} of "
                f"{len(set(candidate_ids))} candidate ids found in the "
                "store — envelope candidates inconsistent with the store")
        # Issue #113: attach the envelope's pre-gate lane values so the
        # reconstruction models the relevance floors too (rows re-read from
        # SQL do not carry recall-time lane keys).
        for r in cand_rows:
            lanes = (candidate_lanes or {}).get(r["id"]) or {}
            r["_rel_lex"] = lanes.get("lex")
            r["_rel_cos"] = lanes.get("cos")
            r["_rel_ent"] = lanes.get("ent")
            # Issue #115: the envelope's pre-gate trust value is what the
            # real gate judged; a lane map without the entry degrades to the
            # exempt 1.0 path inside _row_trust (legacy envelopes).
            r["trust_score"] = lanes.get("trust")
        expected_gate = [r for r in cand_rows
                         if _gate_passes(r, floor, gate_none_floor, grounded,
                                         lane_floors=_inject._lane_floors(),
                                         trust_floor=_inject._trust_floor())]
        expected_kept = _inject.apply_token_budget(expected_gate)[0]
        rendered_set = {r.get("id") for r in rows}
        expected_ids = {r["id"] for r in expected_kept}
        if rendered_set != expected_ids:
            raise BypassError(
                f"{item_id}: rendered set does not match the independently "
                f"reconstructed gate+budget selection "
                f"(rendered={_rendered_ids_sort(rendered_set)} "
                f"expected={sorted(expected_ids)}) — the gate or token "
                "budget did not run on the candidate pool")


def _rendered_ids_sort(ids):
    return sorted(i for i in ids if i)


def _mean_precision(pop: list[dict]) -> float:
    # Mean of the per-item rendered precision (not a truthiness share:
    # a 0.001-precision item must count as 0.001, not as 1.0).
    if not pop:
        return 0.0
    return sum(it["precision"] for it in pop) / len(pop)


def _mrr(pop: list[dict], denom: int) -> float:
    # Reciprocal-rank mean: the denominator must be the same population
    # the numerator sums over (positives-only numerator needs a
    # positives-only denominator).
    if not pop or denom <= 0:
        return 0.0
    return sum((1.0 / it["first_hit_rank"] if it["first_hit_rank"]
                else 0.0) for it in pop) / denom


def _gate_passes(r, floor, gate_none_floor, grounded,
                 lane_floors=None, trust_floor=None) -> bool:
    # Issue #115: mirror the gate's trust_score hard floor FIRST (same
    # normalization via the owning module's _row_trust: missing -> 1.0
    # exempt, unparseable/non-finite -> 0.0, clamped to [0,1]).
    if trust_floor is not None:
        import storelib.inject as _inject_mod
        if _inject_mod._row_trust(r) < trust_floor:
            return False
    conf = _conf_row(r)
    sig = (r.get("signal") or "none").lower()
    if sig == "none":
        trusted = conf >= gate_none_floor
    elif sig in grounded:
        trusted = conf >= floor
    else:
        return False
    if not trusted:
        return False
    # Issue #113: model the per-lane relevance floors — disjunctive, exactly
    # like the real gate: ANY measured lane clearing its own floor passes; a
    # row with no measured lane is exempt. Rows re-read from SQL carry lanes
    # only when the caller attached the envelope's ``candidate_lanes``.
    if lane_floors is not None:
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
                return True
        if measured:
            return False
    return True


def _conf_row(r) -> float:
    try:
        value = float(r.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


def evaluate_injection_items(conn: sqlite3.Connection, items: list[GoldItem],
                             *, k_default: int = 5,
                             ) -> tuple[list[dict], dict]:
    """Run every injection-gold item through the hook's REAL lane and score
    the rendered set (issue #111).

    Per moment the harness reproduces exactly what the hook executes:
    - ``user-prompt``: prose query, ops-composed when the item carries ops
      (the same ``compose_inject_query`` the legacy harness and the hook
      share), ``recall_memory(limit=k, include_global=True, global_limit=3,
      for_injection=True)`` — MMR on, default link expansion (hops 1,
      budget 2), hybrid auto.
    - ``pretool``: the item's ops-token string verbatim (the hook derives
      tokens from the in-flight command and sends them as the query), same
      recall flags.
    - ``subagent``: task text truncated to the hook's 500-char cap, same
      recall flags.
    - ``precompact``: the query-less recent pull the hook sends —
      ``recent_memory(limit=3, min_confidence=0.5, include_global=True,
      global_limit=2, for_injection=True)``.

    Every call runs with ``as_json=True`` under captured stdout so the
    harness scores the same ENVELOPE the hook parses (``reason``,
    ``candidate_ids``, ``tokens_used``, ``tokens_budget``), and with
    ``no_telemetry=True`` so the run is a zero-write read. Metrics are
    computed from the rendered rows after they are verified to appear in
    the rendered fence text (``_format_fenced_recall``), and the lane's
    gate/budget invariants are re-derived from pure primitives
    (``_verify_real_lane``) so a stubbed gate or budget FAILS the harness
    instead of silently scoring.

    Returns (per_item list, metrics dict). Metrics: ``hit_at_k`` (positives:
    every must_include id rendered), ``precision_at_k`` (positives: mean of
    |rendered ∩ labeled| / |rendered|; an empty positive render scores 0),
    ``false_injection_rate`` (negatives with a non-empty render / negatives
    — the same "injected without need" notion the B-2 counter measures in
    production), ``empty_pool_rate`` (all items whose silent reason is
    ``empty-pool`` / all items), ``mrr`` (positives), plus item counts and a
    ``silent_reasons`` tally. Never raises on low scores — a bad SCORE is
    data; only the BypassError invariants and operational failures raise.
    """
    import contextlib
    import io
    import json as _json
    from storelib.ops_tokens import compose_inject_query
    from storelib.recall import (_format_fenced_recall, _normalize_as_of,
                                 recent_memory, recall_memory)

    def _run_lane(item: GoldItem, query: str, k: int) -> tuple[list, dict]:
        if item.moment == "precompact":
            fn, kwargs = recent_memory, {"limit": 3, "min_confidence": 0.5}
        else:
            fn, kwargs = recall_memory, {"limit": k}
        extra: dict = {}
        if item.moment != "precompact":
            extra["query"] = query
        if item.as_of:
            extra["as_of"] = item.as_of
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            fn(conn, namespace=item.namespace,
               include_global=True,
               global_limit=3 if item.moment != "precompact" else 2,
               no_telemetry=True, for_injection=True, as_json=True,
               **kwargs, **extra)
        envelope = _json.loads(captured.getvalue())
        from storelib.inject import envelope_results
        return envelope_results(envelope), envelope

    per_item: list[dict] = []
    for item in items:
        k = item.k if item.k is not None else k_default
        if item.moment == "precompact":
            executed_query = None
        else:
            # Hook parity: every query lane caps the query at 500 chars
            # (UserPromptSubmit prose, derived ops tokens, subagent task
            # text all go through the cap), so the eval must too.
            base_query = (compose_inject_query(item.query, item.ops)
                          if item.ops else item.query)
            executed_query = base_query[:500]
        rows, envelope = _run_lane(item, executed_query or "", k)
        rendered_ids = [r["id"] for r in rows]
        fence = _format_fenced_recall(
            rows, header=f"Relevant memories (namespace {item.namespace or 'unscoped'}).")
        _verify_real_lane(item.id, rows, envelope, fence, conn=conn,
                          candidate_ids=envelope.get("candidate_ids"),
                          candidate_lanes=envelope.get("candidate_lanes"))
        fence_ok = all(f"- [{rid}]" in fence for rid in rendered_ids)

        labeled = set(item.must_include_ids)
        if item.expect == "silent":
            hit = len(rendered_ids) == 0
            precision = 1.0 if not rendered_ids else 0.0
            false_injection = bool(rendered_ids)
        else:
            hit = all(rid in rendered_ids for rid in labeled)
            precision = (len(labeled & set(rendered_ids)) / len(rendered_ids)
                         if rendered_ids else 0.0)
            false_injection = False
        reason = envelope.get("reason")
        first_hit_rank = 0
        for i, rid in enumerate(rendered_ids, start=1):
            if rid in labeled:
                first_hit_rank = i
                break
        per_item.append({
            "id": item.id,
            "bucket": item.bucket,
            "moment": item.moment,
            "expect": item.expect,
            "namespace": item.namespace,
            "query": item.query,
            "ops_query": executed_query if (item.moment != "precompact"
                                            and item.ops) else None,
            "as_of": _normalize_as_of(item.as_of) if item.as_of else None,
            "reason": reason,
            "rendered_ids": rendered_ids,
            "candidate_ids": envelope.get("candidate_ids", []),
            "tokens_used": envelope.get("tokens_used"),
            "tokens_budget": envelope.get("tokens_budget"),
            "hit": hit,
            "precision": precision,
            "fence_ok": fence_ok,
            "first_hit_rank": first_hit_rank,
            "ok": hit and fence_ok,
        })

    positives = [it for it in per_item if it["expect"] == "inject"]
    negatives = [it for it in per_item if it["expect"] == "silent"]
    allowed_reasons = _injection_silent_reasons()
    silent_reasons = {r: 0 for r in allowed_reasons}
    for it in per_item:
        if it["reason"] in silent_reasons:
            silent_reasons[it["reason"]] += 1

    metrics = {
        "hit_at_k": _share(positives, lambda it: it["hit"]),
        "precision_at_k": _mean_precision(positives),
        "false_injection_rate": _share(negatives,
                                       lambda it: bool(it["rendered_ids"])),
        "empty_pool_rate": _share(per_item,
                                  lambda it: it["reason"] == "empty-pool"),
        "mrr": _mrr(positives, len(positives)),
        "items": len(per_item),
        "positive_items": len(positives),
        "negative_items": len(negatives),
        "silent_reasons": silent_reasons,
    }
    return per_item, metrics


def injection_per_moment(per_item: list[dict]) -> dict:
    """Recompute the injection metric block per moment (issue #111).

    Kept as a module function so the runner and any consumer share ONE
    definition of the per-moment rollup (the partition must be exhaustive:
    every item lands in exactly one moment block)."""
    out: dict[str, dict] = {}
    for moment in INJECTION_MOMENTS:
        subset = [it for it in per_item if it["moment"] == moment]
        positives = [it for it in subset if it["expect"] == "inject"]
        negatives = [it for it in subset if it["expect"] == "silent"]
        out[moment] = {
            "items": len(subset),
            "positive_items": len(positives),
            "negative_items": len(negatives),
            "hit_at_k": _share(positives, lambda it: it["hit"]),
            "precision_at_k": _mean_precision(positives),
            "false_injection_rate": _share(
                negatives, lambda it: bool(it["rendered_ids"])),
            "empty_pool_rate": _share(
                subset, lambda it: it["reason"] == "empty-pool"),
            "mrr": _mrr(positives, len(positives)),
        }
    return out


def evaluate_items(conn: sqlite3.Connection, items: list[GoldItem], *,
                   k_default: int = 5,
                   weights: dict | None = None) -> tuple[list[dict], dict]:
    """Run every gold item through the REAL recall pipeline and score it.

    ``weights`` (issue #64, 9.6): optional compute_score override threaded
    through to recall_memory — used ONLY by tune-weights to score candidate
    weight vectors without mutating the W_* module globals.

    Evaluation contract (issue #64, 9.1):
    - ``no_bump=True`` + ``no_telemetry=True``: evaluation takes the passive
      path's injection-omit semantics and records NO telemetry — zero writes;
      the fixture store stays byte-identical and runs are bit-identical.
    - ``link_hops=0``: the runner measures retrieval quality, not the link
      expansion feature (a contradicted neighbor surfacing via a `contradicts`
      edge would otherwise pollute must_exclude assertions).
    - ``no_mmr=True``: MMR diversity reordering is a presentation feature;
      excluding it (plus the pinned clock) makes per-item ranking fully
      deterministic across platforms and dates.

    Issue #82 explicit items: an item with ``explicit=True`` runs on the
    explicit path — ``no_bump=False`` so the change-intent unfold can fire —
    while keeping ``no_telemetry=True`` so the run stays zero-write
    (``_bump_telemetry`` short-circuits on the disabled seam regardless of
    ``no_bump``). ``link_hops=1`` satisfies the unfold gate's search-shape
    exclusion while ``link_budget=0`` keeps link expansion itself OFF, so the
    only extra rows an explicit item can surface are the `[PREVIOUSLY]`
    lineage extras under measurement.

    Returns (per_item list, aggregate metrics dict). Never raises on low
    scores — a bad SCORE is data; only operational failures raise.
    Import errors aside, evaluation contract: ``evaluate_items`` measures the
    RECALL-direction gold only. Injection-gold items (``moment`` set, added
    for issue #111) run on a different pipeline and are REFUSED here —
    point ``scripts/eval_inject_runner.py`` (or ``store.py tune-weights``
    at a recall-direction gold) instead.
    """
    import io
    import contextlib
    from storelib.ops_tokens import compose_inject_query
    from storelib.recall import _normalize_as_of, recall_memory

    for item in items:
        if item.moment:
            raise GoldError(
                f"{item.id!r}: item has moment={item.moment!r} — "
                "injection-gold items must be evaluated with "
                "evaluate_injection_items (scripts/eval_inject_runner.py), "
                "not the recall-direction evaluate_items")

    per_item: list[dict] = []
    for item in items:
        k = item.k if item.k is not None else k_default
        # Issue #88 / #85 direction 4: decision-point items carry their
        # prior-turn operation context; compose through the SAME function the
        # UserPromptSubmit hook uses so the eval measures the real query.
        # Without ops the composition is the byte-exact identity (pinned by
        # tests), so every legacy item's query — and score — is unchanged.
        query = compose_inject_query(item.query, item.ops) if item.ops \
            else item.query
        # recall_memory prints its CLI surface (fences or JSON) regardless of
        # as_json — the runner's stdout is reserved for the JSON report, so
        # the per-query prints are captured and discarded. no_bump supplies
        # the passive (hook-path) injection-omit filter semantics; the eval
        # seam no_telemetry suppresses even the passive surfaced_count write,
        # so evaluation is a true zero-write read. Issue #82: explicit=True
        # flips ONLY the no_bump/link seam (see the contract comment above).
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            results = recall_memory(
                conn,
                query=query,
                namespace=item.namespace,
                limit=k,
                no_bump=not item.explicit,
                no_telemetry=True,
                link_hops=1 if item.explicit else 0,
                link_budget=0 if item.explicit else 2,
                include_global=False,
                no_mmr=True,
                as_of=item.as_of,
                weights=weights,
            )
        ranked_ids = [r["id"] for r in results]
        top_content = " ".join(r["content"] for r in results)

        first_hit_rank = 0
        for i, rid in enumerate(ranked_ids, start=1):
            if rid in item.must_include_ids:
                first_hit_rank = i
                break
        if first_hit_rank == 0 and item.must_include_text:
            for i, r in enumerate(results, start=1):
                if item.must_include_text in r["content"]:
                    first_hit_rank = i
                    break

        hit = all(rid in ranked_ids for rid in item.must_include_ids)
        # Issue #82 (PR-review PRR-024): `hit` is vacuously True for
        # exclude-only items (empty must_include_ids) — the REAL assertion
        # for such items is `excluded_hit`/`excluded_ids_surfaced` below, and
        # the per-bucket `excluded_surfaced` counter in the runner report.
        # hit_at_k therefore counts exclude-only items as hits by design;
        # do not read a retraction regression off hit@k alone.
        text_hit = bool(item.must_include_text) and item.must_include_text in top_content
        excluded_hit = [rid for rid in item.must_exclude_ids if rid in ranked_ids]
        # The injection-omit behavior under measurement is the HOOK path's:
        # with no_bump=True, recall omits injection-risk rows entirely, so an
        # omitted injection row is simply absent from ranked_ids.
        injection_omitted = (item.bucket == "injection" and not excluded_hit)

        per_item.append({
            "id": item.id,
            "bucket": item.bucket,
            "query": item.query,
            # Review PRR-91-006: for ops items the recall ran on the COMPOSED
            # query — record it so report rows reflect what was executed.
            "ops_query": query if item.ops else None,
            "as_of": _normalize_as_of(item.as_of) if item.as_of else None,
            "k": k,
            "explicit": item.explicit,
            "hit": hit,
            "text_hit": text_hit,
            "first_hit_rank": first_hit_rank,
            "excluded_ids_surfaced": excluded_hit,
            "injection_omitted": injection_omitted,
            "ranked_ids": ranked_ids,
            "ok": hit and (text_hit or not item.must_include_text) and not excluded_hit,
        })

    n = len(per_item)
    as_of_items = [it for it in per_item if it["as_of"]]
    injection_items = [it for it in per_item if it["bucket"] == "injection"]
    metrics = {
        "hit_at_k": _share(per_item, lambda it: it["hit"]),
        "mrr": sum(
            (1.0 / it["first_hit_rank"] if it["first_hit_rank"] else 0.0)
            for it in per_item
        ) / n,
        "as_of_accuracy": _share(as_of_items, lambda it: it["hit"]),
        "injection_omit_rate": _share(injection_items,
                                      lambda it: it["injection_omitted"]),
    }
    metrics["items"] = n
    metrics["as_of_items"] = len(as_of_items)
    metrics["injection_items"] = len(injection_items)
    return per_item, metrics


# PRR-009: the reportable per-item key contract, shared by evaluate_items
# (which builds the dicts) and eval_runner.py (which projects the report
# subset). A single constant makes key drift an import-time-visible edit in
# ONE place instead of a runtime KeyError in the runner.
PER_ITEM_REPORT_KEYS = (
    "id", "bucket", "query", "ops_query", "as_of", "k", "explicit", "hit",
    "text_hit", "first_hit_rank", "excluded_ids_surfaced",
    "injection_omitted", "ranked_ids", "ok",
)


def _share(population: list[dict], predicate) -> float:
    """Fraction of `population` satisfying `predicate`; 0.0 on an empty
    population (an unevaluable metric must read as 0, not as perfection)."""
    if not population:
        return 0.0
    return sum(1 for it in population if predicate(it)) / len(population)
