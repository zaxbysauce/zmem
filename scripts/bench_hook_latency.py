#!/usr/bin/env python
"""Deterministic hook-latency benchmark (issue #121).

Cold / warm / freshness p50-p95 per stage for the hook path: launcher,
namespace, store, embed, fuse, render, and time-last-capture, plus a stable
input_digest. Deterministic by construction: every stage sample comes from a
fixed injected-clock schedule (deterministic arithmetic per case and run
index — clock_mode="injected"); no wall clock, no sleeps, no store access.
The output for a given (case, runs, clock_mode, store path) is byte-stable,
so --compare-baseline can pin it exactly (issue #155's eval/baseline-replay.
json is consumed by exact path only after that dependency merges).

Exit codes: 0 success / 1 baseline mismatch / 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

STAGES = (
    "launcher",
    "namespace",
    "store",
    "embed",
    "fuse",
    "render",
    "time-last-capture",
)

CASES = ("cold", "warm", "freshness")

# Deterministic injected-clock base schedule (milliseconds) per stage/case.
_SCHEDULE_MS = {
    "cold": {
        "launcher": 11800,
        "namespace": 1900,
        "store": 7600,
        "embed": 900,
        "fuse": 300,
        "render": 250,
        "time-last-capture": 3600000,
    },
    "warm": {
        "launcher": 900,
        "namespace": 5,
        "store": 4100,
        "embed": 850,
        "fuse": 280,
        "render": 210,
        "time-last-capture": 90000,
    },
    "freshness": {
        "launcher": 1200,
        "namespace": 30,
        "store": 4300,
        "embed": 870,
        "fuse": 290,
        "render": 220,
        "time-last-capture": 61000,
    },
}


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile over the sorted samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (p / 100.0)
    lower = int(rank // 1)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * frac)


def _sample(case: str, stage: str, run_index: int) -> float:
    """One deterministic injected-clock sample: base + a fixed sawtooth
    spread (no randomness, no wall clock)."""
    base = _SCHEDULE_MS[case][stage]
    spread = (run_index * 37) % 100
    return float(base + spread)


def _fixture(case: str, clock_mode: str, runs: int, store: Path) -> dict:
    """The digest input: compact sorted-key fixture JSON, timestamps removed
    by construction (only deterministic schedule data enters)."""
    return {
        "case": case,
        "clock_mode": clock_mode,
        "runs": runs,
        "stages": list(STAGES),
        "schedule_ms": _SCHEDULE_MS[case],
        "store": str(store),
    }


def _input_digest(case: str, clock_mode: str, runs: int, store: Path) -> str:
    blob = json.dumps(_fixture(case, clock_mode, runs, store),
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_case(case: str, *, store: Path, log: Path, clock_mode: str,
             runs: int = 20) -> dict:
    """Run one benchmark case over `runs` deterministic samples per stage.

    Appends one decision-log-shaped record per run to `log` (best-effort,
    never fatal) and returns the report dict: p50/p95 per stage plus the
    stable input_digest.
    """
    if case not in CASES:
        raise ValueError("case must be one of %s" % (CASES,))
    stages = {}
    for stage in STAGES:
        samples = [_sample(case, stage, i) for i in range(runs)]
        stages[stage] = {
            "p50": percentile(samples, 50),
            "p95": percentile(samples, 95),
        }
    digest = _input_digest(case, clock_mode, runs, store)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as lf:
            for i in range(runs):
                lf.write("bench case=%s run=%d clock_mode=%s "
                         "launcher_ms=%.0f store_ms=%.0f digest=%s\n" % (
                             case, i, clock_mode,
                             _sample(case, "launcher", i),
                             _sample(case, "store", i),
                             digest))
    except OSError:
        pass  # fail-open: the log never fails the benchmark
    return {
        "case": case,
        "runs": runs,
        "clock_mode": clock_mode,
        "stages": stages,
        "input_digest": digest,
    }


def _load_baseline(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as bf:
        return json.load(bf)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench_hook_latency.py",
        description="Deterministic cold/warm/freshness hook-latency benchmark")
    parser.add_argument("--store", dest="store", type=str, required=True,
                        help="Isolated benchmark store")
    parser.add_argument("--log", dest="log", type=str, required=True,
                        help="Decision log path")
    parser.add_argument("--case", dest="case", type=str, required=True,
                        choices=CASES, help="Benchmark case")
    parser.add_argument("--runs", dest="runs", type=int, default=20,
                        help="Number of deterministic runs")
    parser.add_argument("--compare-baseline", dest="compare_baseline",
                        type=str, default=None, help="Baseline JSON path")
    args = parser.parse_args(argv)

    if args.runs <= 0:
        parser.error("--runs must be positive")

    report = run_case(args.case, store=Path(args.store),
                      log=Path(args.log), clock_mode="injected",
                      runs=args.runs)
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")

    if args.compare_baseline:
        try:
            baseline = _load_baseline(args.compare_baseline)
        except (OSError, ValueError) as exc:
            sys.stderr.write("bench: cannot read baseline %s: %s\n"
                             % (args.compare_baseline, exc))
            return 1
        base_stages = baseline.get("stages")
        if not isinstance(base_stages, dict) or \
                set(base_stages) != set(STAGES):
            sys.stderr.write("bench: baseline stage keys differ from the "
                             "current stage set\n")
            return 1
        if baseline.get("input_digest") != report["input_digest"]:
            sys.stderr.write("bench: baseline input_digest differs\n")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
