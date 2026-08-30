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
           "fts", "adapter", "retraction", "polarity", "change-intent")


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
    query = obj.get("query")
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
    if not include_ids and not exclude_ids and not include_text:
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
    )


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
    """
    # Imported here so load_gold()/validation stay importable without the
    # recall stack (mirrors doctor.py's lazy-import discipline).
    import io
    import contextlib
    from storelib.recall import _normalize_as_of, recall_memory

    per_item: list[dict] = []
    for item in items:
        k = item.k if item.k is not None else k_default
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
                query=item.query,
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
    "id", "bucket", "query", "as_of", "k", "explicit", "hit", "text_hit",
    "first_hit_rank", "excluded_ids_surfaced", "injection_omitted",
    "ranked_ids", "ok",
)


def _share(population: list[dict], predicate) -> float:
    """Fraction of `population` satisfying `predicate`; 0.0 on an empty
    population (an unevaluable metric must read as 0, not as perfection)."""
    if not population:
        return 0.0
    return sum(1 for it in population if predicate(it)) / len(population)
