"""Offline INJECTION eval runner for the zmem memory store (issue #111).

The precision gold that runs the REAL inject gate and token budget and scores
the rendered set — Workstream B, PR 3 of 4. Where ``scripts/eval_runner.py``
measures raw recall on a narrowed pipeline (issue #64), this runner drives
the hook's actual injection lane for every gold item:

    user-prompt  -> recall_memory(..., limit=k, include_global=True,
                                  global_limit=3, for_injection=True)
                    (MMR on, default link expansion; ops items compose via
                    the SAME compose_inject_query the hook and the legacy
                    harness use)
    pretool      -> same recall flags, query = the item's ops-token string
                    (what the hook derives from the in-flight command)
    subagent     -> same recall flags, query = task text truncated to the
                    hook's 500-char cap
    precompact   -> recent_memory(limit=3, min_confidence=0.5,
                                  include_global=True, global_limit=2,
                                  for_injection=True)

Every call runs ``as_json=True`` (the harness scores the same envelope the
hook parses: reason / candidate_ids / tokens_used / tokens_budget) and
``no_telemetry=True`` (zero-write read). Metrics are computed from the
rendered rows after verifying they appear in the rendered fence text, and
the lane's gate/budget invariants are re-derived from pure primitives —
a stubbed gate or token budget makes the harness REFUSE (exit 2), never
silently score.

Metrics (all from the rendered set, reported overall and per moment):
hit_at_k, precision_at_k, false_injection_rate, empty_pool_rate, mrr —
side by side, per issue #111.

Isolation + determinism contract (mirrors scripts/eval_runner.py):
* ``--store`` is REQUIRED — the operator home store is never touched.
* A missing store is built at the exact --store path by
  ``tests/fixtures/eval_store.py`` (the same deterministic 70-row corpus the
  recall gold uses — the four moments are different queries against the
  SAME store).
* The fixture BASE_ENV determinism pins (fake embedder, no model
  downloads) are forced BEFORE storelib is imported; ZMEM_TEST_NOW is
  pinned to the corpus sentinel.
* Record-only by default: exit 0 on a completed run REGARDLESS of scores;
  exit 1 only on an explicit ratchet flag breach (--fail-under-precision,
  --fail-under-false-injection, or a --compare-baseline delta); exit 2 on
  operational errors (missing --store value, invalid gold, unbuildable
  store, a bypass-invariant violation, an unreadable baseline).

Provenance note (issue #111 scope 2): the ~100 labeled positives and the
negative-control seed in ``eval/injection_gold.jsonl`` are committed,
deterministic fixtures. The maintainer's REAL prompt log window is operator
content and stays uncommitted (same convention as ``self-corpus-results.json``
in .gitignore); pass ``--gold <local labeled jsonl>`` to evaluate it with
zero code changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DEFAULT_GOLD = REPO_ROOT / "eval" / "injection_gold.jsonl"
DEFAULT_BASELINE = REPO_ROOT / "eval" / "baseline-injection.json"

# The rate keys compared against a baseline (and gated by the ratchet
# flags). Deterministic floats in [0, 1].
BASELINE_RATE_KEYS = ("hit_at_k", "precision_at_k", "false_injection_rate",
                      "empty_pool_rate", "mrr")


def _bootstrap_env(store: str) -> None:
    """Force the model-absent/deterministic env BEFORE storelib is imported
    (identical contract to scripts/eval_runner.py: storelib freezes
    STORE_PATH and env-derived tunables at import time). The determinism
    pins come from the fixture's own BASE_ENV — ONE source of truth shared
    with the corpus builder instead of a re-typed copy."""
    os.environ["ZMEM_STORE"] = store
    sys.path.insert(0, str(FIXTURES_DIR))
    from eval_store import BASE_ENV, EVAL_PIN_TS  # noqa: E402
    for key, value in BASE_ENV.items():
        os.environ.setdefault(key, value)
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS
    os.environ.setdefault("PYTHONUTF8", "1")


def _ensure_store(store: str) -> None:
    """Build the deterministic corpus at `store` when it does not exist."""
    if Path(store).exists():
        return
    from eval_store import build_eval_store  # noqa: E402  (path set above)
    print(f"[eval] store not found; building deterministic eval corpus at {store}",
          file=sys.stderr)
    try:
        build_eval_store(store)
    except Exception as exc:
        print(f"[eval] cannot build eval store at {store}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)


def _load_baseline(path: str) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[eval] cannot read baseline {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    metrics = doc.get("metrics") if isinstance(doc, dict) else None
    if not isinstance(metrics, dict):
        print(f"[eval] baseline {path} has no metrics object", file=sys.stderr)
        sys.exit(2)
    missing = [k for k in BASELINE_RATE_KEYS if k not in metrics]
    if missing:
        print(f"[eval] baseline {path} metrics missing keys: "
              + ", ".join(missing), file=sys.stderr)
        sys.exit(2)
    return metrics


def _compare_baseline(metrics: dict, baseline_path: str) -> int:
    """Print per-metric deltas against the baseline. Exit 1 when any shared
    rate differs — this flag IS the future one-flag CI ratchet."""
    baseline = _load_baseline(baseline_path)
    drift = []
    for key in BASELINE_RATE_KEYS:
        base_v = float(baseline[key])
        cur_v = float(metrics[key])
        if base_v != cur_v:
            drift.append((key, base_v, cur_v))
    if not drift:
        print(f"[eval] baseline match: all {len(BASELINE_RATE_KEYS)} metrics "
              f"equal {baseline_path}", file=sys.stderr)
        return 0
    for key, base_v, cur_v in drift:
        print(f"[eval] DELTA {key}: baseline={base_v:.6f} current={cur_v:.6f} "
              f"delta={cur_v - base_v:+.6f}", file=sys.stderr)
    print(f"[eval] baseline drift: {len(drift)}/{len(BASELINE_RATE_KEYS)} "
          f"metrics differ from {baseline_path}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="eval_inject_runner.py",
        description="Run the injection-direction precision gold through the "
                    "REAL inject gate + token budget (rendered-set scoring; "
                    "JSON report on stdout)")
    ap.add_argument("--store", required=True,
                    help="path to the eval store. REQUIRED — the runner never "
                         "touches the default home store. A missing store is "
                         "built as the deterministic eval corpus at this path.")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD),
                    help="injection gold JSONL path "
                         f"(default: {DEFAULT_GOLD}); point it at a local "
                         "decision-log-derived labeled file to evaluate the "
                         "real prompt log window without code changes")
    ap.add_argument("--k", type=int, default=5,
                    help="default top-k cut for the query lanes (default 5; "
                         "the hook's UserPromptSubmit/PreToolUse/Subagent "
                         "limit; a gold item may override with its own 'k')")
    ap.add_argument("--fail-under-precision", type=float, default=None,
                    help="OPTIONAL ratchet: exit 1 when precision@k falls "
                         "below this value. Off by default and OFF in CI.")
    ap.add_argument("--fail-under-false-injection", type=float, default=None,
                    help="OPTIONAL ratchet: exit 1 when the negative-control "
                         "false-injection rate rises above this value. Off by "
                         "default and OFF in CI.")
    ap.add_argument("--compare-baseline", default=None,
                    help="OPTIONAL: compare the run's metrics against the "
                         "committed baseline (exit 1 on any delta, exit 2 on "
                         "an unreadable baseline). Off by default and OFF in "
                         "CI; this flag is the one-flag ratchet.")
    ap.add_argument("--json-out", default=None,
                    help="also write the JSON report to this path (CI uploads "
                         "it as a workflow artifact)")
    args = ap.parse_args()
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    _bootstrap_env(args.store)
    _ensure_store(args.store)

    sys.path.insert(0, str(SCRIPTS_DIR))
    from storelib.eval_gold import (  # noqa: E402
        INJECTION_PER_ITEM_REPORT_KEYS, BypassError, GoldError,
        evaluate_injection_items, injection_per_moment, load_gold)
    from storelib.schema import connect  # noqa: E402

    try:
        items = load_gold(args.gold)
    except GoldError as exc:
        print(f"[eval] invalid gold set: {exc}", file=sys.stderr)
        return 2

    try:
        conn = connect()
        per_item, metrics = evaluate_injection_items(conn, items, k_default=args.k)
    except BypassError as exc:
        print(f"[eval] BYPASS INVARIANT FAILED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[eval] evaluation failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    report = {
        "runner": "scripts/eval_inject_runner.py",
        "gold_path": args.gold,
        "store": args.store,
        "k": args.k,
        "profile": "fake (model-absent)",
        "clock": os.environ.get("ZMEM_TEST_NOW"),
        "lane": "for-injection",
        "metrics": metrics,
        "per_moment": injection_per_moment(per_item),
        "per_item": [
            {key: it[key] for key in INJECTION_PER_ITEM_REPORT_KEYS}
            for it in per_item
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    # Same line-terminator escaping sync.py's export applies (U+2028/2029/0085
    # are not escaped by json.dumps but terminate lines for splitlines-based
    # artifact consumers).
    text = (text.replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029")
                .replace("\u0085", "\\u0085"))
    print(text)
    if args.json_out:
        try:
            out = Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"[eval] cannot write --json-out {args.json_out}: {exc}",
                  file=sys.stderr)
            return 2

    exit_code = 0
    if args.compare_baseline:
        try:
            drift = _compare_baseline(metrics, args.compare_baseline)
        except SystemExit as exc:
            return int(exc.code or 0)
        exit_code = max(exit_code, drift)
    if (args.fail_under_precision is not None
            and metrics["precision_at_k"] < args.fail_under_precision):
        print(f"[eval] FAIL: precision@k={metrics['precision_at_k']:.4f} "
              f"below --fail-under-precision {args.fail_under_precision}",
              file=sys.stderr)
        exit_code = 1
    if (args.fail_under_false_injection is not None
            and metrics["false_injection_rate"]
            > args.fail_under_false_injection):
        print(f"[eval] FAIL: false_injection_rate="
              f"{metrics['false_injection_rate']:.4f} above "
              f"--fail-under-false-injection "
              f"{args.fail_under_false_injection}", file=sys.stderr)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
