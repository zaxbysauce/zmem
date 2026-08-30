"""Offline eval runner for the zmem memory store (issue #64, 9.1 + 9.5).

THE canonical eval command — CI (`.github/workflows/ci.yml`) runs exactly
this, and SKILL.md documents exactly this:

    python scripts/eval_runner.py --store <path> [--gold eval/gold.jsonl] \\
        [--k 5] [--fail-under X] [--json-out PATH]

Reads a gold JSONL (query / optional as_of / must_include_ids,
must_exclude_ids and/or must_include_text / namespace), runs every item
through the REAL recall pipeline against the store at --store, and prints one
JSON report (hit@k, MRR, as-of accuracy, injection-omit rate) to stdout.

Isolation + determinism contract:
* ``--store`` is REQUIRED. The runner never resolves the default home store —
  an operator store cannot be evaluated (or migrated) by accident.
* A missing store is built at the exact --store path by
  ``tests/fixtures/eval_store.py`` (a deterministic 64-row corpus — the
  original 50 rows of the #64 contract plus the #82 retraction/polarity/
  change-intent rows 51-64 — whose ids are the ones eval/gold.jsonl names).
  The builder prints a notice on stderr.
* The FAKE embedder (ZMEM_EMBED_PROFILE=fake) and ZMEM_MODEL_AUTODOWNLOAD=0
  are forced BEFORE storelib is imported: the run is model-absent by
  construction, no downloads, byte-identical ranking on any machine.
* ZMEM_TEST_NOW is pinned to the corpus sentinel so recency decay is fixed;
  evaluation calls recall with no_telemetry=True on EVERY item (counters
  untouched — the store stays byte-identical). Default items are passive
  (no_bump=True, link_hops=0, no_mmr=True: retrieval quality, not link
  expansion or MMR presentation). An item with ``"explicit": true`` (issue
  #82) runs the explicit path — no_bump=False with link_hops=1/link_budget=0
  so the change-intent unfold fires while link expansion stays off — and is
  STILL zero-write via the no_telemetry seam (see SKILL.md).

Exit codes: 0 on a completed run REGARDLESS of scores (CI collects metrics,
it does not gate on quality), 1 on a --fail-under breach, 2 on operational
errors (missing --store value, unreadable/invalid gold, unbuildable store).
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
DEFAULT_GOLD = REPO_ROOT / "eval" / "gold.jsonl"


def _bootstrap_env(store: str) -> None:
    """Force the model-absent/deterministic env BEFORE storelib is imported.

    storelib freezes STORE_PATH and env-derived tunables at import time (the
    per-load contract), so the store override must be in place first. Setting
    os.environ here (not a child-env dict) is what makes the in-process
    `connect()` resolve the caller's explicit --store path. ZMEM_STORE is
    OVERWRITTEN unconditionally: any ambient value (e.g. an operator's shell
    export) must not leak the home store into an eval run.
    """
    os.environ["ZMEM_STORE"] = store
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
    os.environ.setdefault("ZMEM_MODELS_DIR", "/nonexistent-zmem-models-dir")
    # Issue #82: pin UTF-8 text I/O on Windows consoles (cp1252 would crash
    # mid-report on non-ASCII fixture content); a no-op on UTF-8 hosts.
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.path.insert(0, str(FIXTURES_DIR))
    from eval_store import EVAL_PIN_TS  # noqa: E402  (fixtures dir on path)
    os.environ["ZMEM_TEST_NOW"] = EVAL_PIN_TS

def _ensure_store(store: str) -> None:
    """Build the deterministic corpus at `store` when it does not exist yet."""
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


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="eval_runner.py",
        description="Run the gold-set eval against an isolated store "
                    "(model-absent; the report is JSON on stdout)")
    ap.add_argument("--store", required=True,
                    help="path to the eval store. REQUIRED — the runner never "
                         "touches the default home store. A missing store is "
                         "built as the deterministic eval corpus at this path.")
    ap.add_argument("--gold", default=str(DEFAULT_GOLD),
                    help=f"gold JSONL path (default: {DEFAULT_GOLD})")
    def _positive_int(value: str) -> int:
        n = int(value)
        if n < 1:
            raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
        return n

    ap.add_argument("--k", type=_positive_int, default=5,
                    help="default top-k cut for hit@k (default 5; a gold item "
                         "may override with its own 'k'; applied to items that "
                         "do not set one)")
    ap.add_argument("--fail-under", type=float, default=None,
                    help="OPTIONAL: exit 1 when hit@k falls below this value. "
                         "Off by default and OFF in CI — the build never fails "
                         "on recall quality.")
    ap.add_argument("--json-out", default=None,
                    help="also write the JSON report to this path (CI uploads "
                         "it as a workflow artifact)")
    args = ap.parse_args()
    # PRR-024 adjacent (cubic P2): `os.environ` alone doesn't switch the
    # ALREADY-CREATED console streams on a cp1252 Windows host, and
    # setdefault must honor an explicit PYTHONUTF8=0 — so reconfigure the
    # report streams directly (no-op on UTF-8 hosts).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    _bootstrap_env(args.store)
    _ensure_store(args.store)

    sys.path.insert(0, str(SCRIPTS_DIR))
    from storelib.eval_gold import (  # noqa: E402
        PER_ITEM_REPORT_KEYS, GoldError, evaluate_items, load_gold)
    from storelib.schema import connect  # noqa: E402

    try:
        items = load_gold(args.gold)
    except GoldError as exc:
        print(f"[eval] invalid gold set: {exc}", file=sys.stderr)
        return 2

    # Operational failures here (an existing-but-invalid --store file,
    # sqlite corruption, mid-eval errors) must exit 2 with a clear message —
    # never a traceback (whose exit 1 would collide with --fail-under).
    try:
        conn = connect()
        per_item, metrics = evaluate_items(conn, items, k_default=args.k)
    except Exception as exc:
        print(f"[eval] evaluation failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    per_bucket: dict[str, dict] = {}
    for it in per_item:
        agg = per_bucket.setdefault(
            it["bucket"], {"items": 0, "hits": 0, "excluded_surfaced": 0})
        agg["items"] += 1
        agg["hits"] += 1 if it["hit"] else 0
        agg["excluded_surfaced"] += 1 if it["excluded_ids_surfaced"] else 0

    report = {
        "runner": "scripts/eval_runner.py",
        "gold_path": args.gold,
        "store": args.store,
        "k": args.k,
        "profile": "fake (model-absent)",
        "clock": os.environ.get("ZMEM_TEST_NOW"),
        "metrics": metrics,
        "per_bucket": per_bucket,
        "per_item": [
            {key: it[key] for key in PER_ITEM_REPORT_KEYS}
            for it in per_item
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    # Same line-terminator escaping sync.py's export applies: U+2028/2029/0085
    # inside memory content are NOT escaped by json.dumps but DO terminate
    # lines for splitlines()-based consumers of the artifact.
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

    if args.fail_under is not None and metrics["hit_at_k"] < args.fail_under:
        print(f"[eval] FAIL: hit@{args.k}={metrics['hit_at_k']:.4f} below "
              f"--fail-under {args.fail_under}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
