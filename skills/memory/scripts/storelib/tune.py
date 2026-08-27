"""Offline recall-weight tuning over a gold set (issue #64, 9.6).

``store.py tune-weights --dry-run --gold <path>`` evaluates the SHIPPED
composite weights (W_BM25/W_CONFIDENCE/W_RECENCY/W_POPULARITY in
storelib/recall.py) against a gold set, then runs a small deterministic
hill-climb over candidate weight vectors that always sum to 1.0, and prints
the suggestion as JSON on stdout. The evaluation is read-only — no weights
are written (there is no --apply; applying weights is a documented manual
edit of the W_* module constants, SKILL.md §tune-weights). Opening the store
runs the standard per-subcommand migration, so a pre-v12 store is migrated
first like for every store.py command.

Candidate weights are passed to evaluate_items/recall_memory's internal
``weights`` keyword (compute_score override) instead of mutating module
globals — a dry-run tool must never temporarily change the ranking another
in-process consumer sees, and globals mutated mid-run are exactly that
(plan-critic revision R3).
"""

from __future__ import annotations

import json
import sqlite3
import sys

from storelib.eval_gold import GoldError, evaluate_items, load_gold

# Hill-climb shape. Deliberately small and deterministic: 6 ordered weight
# pairs x 3 transfer sizes, up to 3 passes, stop on the first pass with no
# improvement. A full grid search would multiply the gold-set recall count by
# ~3 orders of magnitude for negligible suggestion quality on a 30-item set.
_WEIGHT_KEYS = ("bm25", "confidence", "recency", "popularity")
_TRANSFER_SIZES = (0.05, 0.10, 0.15)
_WEIGHT_FLOOR = 0.05
_MAX_PASSES = 3

# The shipped defaults (storelib/recall.py). Duplicated here as the eval
# baseline on purpose: tune-weights reports the CURRENT constants it was run
# against; it must not read them live, because the point of the report is to
# be reproducible against the version that shipped them.
_CURRENT_WEIGHTS = (0.55, 0.20, 0.15, 0.10)

# Objective blends the two ranking metrics the ladder cares about. Equal
# weighting keeps neither metric dominant; hit@k is the operator-visible
# "did it surface at all" signal, MRR the "how high" refinement.
_OBJECTIVE_HIT_WEIGHT = 0.5


def _weights_dict(vec: tuple[float, float, float, float]) -> dict:
    return dict(zip(_WEIGHT_KEYS, (float(v) for v in vec)))


def _objective(metrics: dict) -> float:
    return (_OBJECTIVE_HIT_WEIGHT * metrics["hit_at_k"]
            + (1.0 - _OBJECTIVE_HIT_WEIGHT) * metrics["mrr"])


def tune_weights(conn: sqlite3.Connection, *, gold_path: str, k: int = 5) -> int:
    """CLI body for `tune-weights --dry-run`. Returns the process exit code:
    0 on a completed analysis, 2 on an operational refusal or failure
    (invalid gold set, unreadable/corrupt store, mid-eval error); low scores
    are data, not failures — the run still exits 0 and reports."""
    try:
        items = load_gold(gold_path)
    except GoldError as exc:
        print(f"[zmem] tune-weights: {exc}", file=sys.stderr)
        return 2

    def score(vec: tuple[float, float, float, float]) -> dict:
        _, metrics = evaluate_items(conn, items, k_default=k,
                                    weights=_weights_dict(vec))
        return metrics

    # Operational failures mid-evaluation (sqlite corruption, a locked or
    # unreadable store, recall errors) must exit 2 with a message — never a
    # traceback whose exit 1 is indistinguishable from a completed run to a
    # caller parsing exit codes.
    try:
        current_vec = _CURRENT_WEIGHTS
        current_metrics = score(current_vec)
        best_vec = current_vec
        best_metrics = current_metrics
        best_obj = _objective(current_metrics)
        candidates_evaluated = 1

        for _pass in range(_MAX_PASSES):
            improved = False
            for i in range(len(_WEIGHT_KEYS)):
                for j in range(len(_WEIGHT_KEYS)):
                    if i == j:
                        continue
                    for size in _TRANSFER_SIZES:
                        src = best_vec[i]
                        dst = best_vec[j]
                        if src - size < _WEIGHT_FLOOR:
                            continue
                        candidate = list(best_vec)
                        candidate[i] = round(src - size, 10)
                        candidate[j] = round(dst + size, 10)
                        cand_vec = tuple(candidate)
                        cand_metrics = score(cand_vec)
                        candidates_evaluated += 1
                        cand_obj = _objective(cand_metrics)
                        if cand_obj > best_obj + 1e-9:
                            best_vec = cand_vec
                            best_metrics = cand_metrics
                            best_obj = cand_obj
                            improved = True
            if not improved:
                break
    except Exception as exc:
        print(f"[zmem] tune-weights: evaluation failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report = {
        "command": "tune-weights",
        "dry_run": True,
        "gold_path": gold_path,
        "k": k,
        "objective": "0.5*hit_at_k + 0.5*mrr",
        "current": {
            "weights": _weights_dict(current_vec),
            "metrics": current_metrics,
            "objective": round(_objective(current_metrics), 6),
        },
        "suggested": {
            "weights": _weights_dict(best_vec),
            "weights_sum": round(sum(best_vec), 10),
            "metrics": best_metrics,
            "objective": round(best_obj, 6),
        },
        "candidates_evaluated": candidates_evaluated,
        "note": ("dry-run; nothing written. (The evaluation is read-only; "
                 "opening the store runs the standard per-subcommand "
                 "migration, so a pre-v12 store is migrated first like for "
                 "every store.py command.) Applying the suggested weights is "
                 "a manual edit of the W_* constants in "
                 "skills/memory/scripts/storelib/recall.py — they must keep "
                 "summing to 1.0 (see SKILL.md tune-weights)."),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
