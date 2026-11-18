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
import os
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


def _fixture(case: str, clock_mode: str, runs: int) -> dict:
    """The digest input: compact sorted-key fixture JSON, timestamps removed
    by construction (only deterministic schedule data enters)."""
    return {
        "case": case,
        "clock_mode": clock_mode,
        "runs": runs,
        "stages": list(STAGES),
        "schedule_ms": _SCHEDULE_MS[case],
    }


def _input_digest(case: str, clock_mode: str, runs: int) -> str:
    blob = json.dumps(_fixture(case, clock_mode, runs),
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
    digest = _input_digest(case, clock_mode, runs)
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


# --- Phase-report mode (issue #473 AC4) --------------------------------------
# A schema'd, wall-clock MEASUREMENT of the real phases a recall pays, against
# a FRESH fixture store created at --store. CI enforces the schema and the
# caps policy, never wall-clock numbers (those land in the PR body / the
# GATE-01 evidence on the issue). Model-dependent phases record null + reason
# when the artifacts are absent, LFS-pointer-shaped, or digest-mismatched —
# never a fabricated number. The deterministic injected-clock core above is
# untouched by this mode.

PHASE_KEYS = (
    "interpreter_imports",
    "sqlite_vec",
    "model_tokenizer_read_hash",
    "session_construction",
    "inference",
    "sql",
)

_LFS_POINTER_PREFIX = b"version https://git-lfs"


def _phase_null(reason: str) -> dict:
    return {"ms": None, "reason": reason}


def _subprocess_ms(argv: list[str]) -> float:
    import subprocess as _sp
    import time as _time
    t0 = _time.perf_counter()
    proc = _sp.run(argv, capture_output=True)
    elapsed_ms = (_time.perf_counter() - t0) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(
            "phase leg failed (rc=%d): %s" % (proc.returncode,
                                              proc.stderr[:200]))
    return elapsed_ms


def _scripts_dir() -> "Path":
    return Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"


def _measure_phases(store_path: Path) -> dict:
    """Measure the six disjoint additive phase buckets.

    interpreter/imports and sqlite_vec are measured in SUBPROCESSES so their
    import cost is never double-counted into the in-process buckets: the
    sqlite_vec bucket is (interpreter + sqlite_vec import) minus the bare
    interpreter leg. The remaining buckets are in-process wall-clock spans.
    """
    import platform
    import time as _time

    phases: dict[str, dict] = {}
    scripts = _scripts_dir()

    # Phase mode never reaches the network regardless of ambient env.
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

    # 1) interpreter_imports — bare interpreter startup.
    phases["interpreter_imports"] = {
        "ms": _subprocess_ms([sys.executable, "-c", "pass"])}

    # 2) sqlite_vec — extension import cost on top of the interpreter leg.
    try:
        combined = _subprocess_ms(
            [sys.executable, "-c", "import sqlite_vec"])
        phases["sqlite_vec"] = {
            "ms": max(0.0, combined - phases["interpreter_imports"]["ms"])}
    except Exception as exc:
        phases["sqlite_vec"] = _phase_null("sqlite_vec-unavailable: %s"
                                           % type(exc).__name__)

    # 3) model/tokenizer read+hash — verified-artifact acquisition cost.
    model_bytes: bytes | None = None
    tok_bytes: bytes | None = None
    try:
        sys.path.insert(0, str(scripts))
        import embeddings as _emb
        models_dir = _emb._resolve_models_dir()
        model_path = models_dir / "minilm.onnx"
        tok_path = models_dir / "tokenizer.json"
        t0 = _time.perf_counter()
        if model_path.is_file():
            model_bytes = model_path.read_bytes()
        if tok_path.is_file():
            tok_bytes = tok_path.read_bytes()
        import hashlib as _hl
        if model_bytes is not None:
            _hl.sha256(model_bytes).hexdigest()
        if tok_bytes is not None:
            _hl.sha256(tok_bytes).hexdigest()
        elapsed = (_time.perf_counter() - t0) * 1000.0
        if model_bytes is None and tok_bytes is None:
            phases["model_tokenizer_read_hash"] = _phase_null(
                "artifacts-absent: %s" % models_dir)
        elif (model_bytes or b"").startswith(_LFS_POINTER_PREFIX) or \
                (tok_bytes or b"").startswith(_LFS_POINTER_PREFIX):
            phases["model_tokenizer_read_hash"] = _phase_null(
                "lfs-pointer-artifact")
            model_bytes = None
            tok_bytes = None
        else:
            phases["model_tokenizer_read_hash"] = {"ms": elapsed}
    except Exception as exc:
        phases["model_tokenizer_read_hash"] = _phase_null(
            "read-failed: %s" % type(exc).__name__)

    # 4) session construction (bounded resource policy applied — the policy
    #    this PR installs is what production recalls pay).
    sess = None
    tok = None
    if model_bytes is not None and tok_bytes is not None:
        try:
            import onnxruntime as _ort
            from tokenizers import Tokenizer as _Tok
            opts = _emb._resource_options(_ort)
            t0 = _time.perf_counter()
            if opts is None:
                sess = _ort.InferenceSession(model_bytes)
            else:
                sess = _ort.InferenceSession(model_bytes, sess_options=opts)
            tok = _Tok.from_buffer(tok_bytes)
            tok.enable_padding(length=128)
            tok.enable_truncation(max_length=128)
            phases["session_construction"] = {
                "ms": (_time.perf_counter() - t0) * 1000.0}
        except Exception as exc:
            phases["session_construction"] = _phase_null(
                "construction-failed: %s" % type(exc).__name__)
            sess = None
    else:
        phases["session_construction"] = _phase_null("artifacts-absent")

    # 5) inference — one embed through the constructed session.
    if sess is not None and tok is not None:
        try:
            import numpy as _np
            enc = tok.encode("phase report probe query")
            type_ids = list(getattr(enc, "type_ids", None)
                            or [0] * len(enc.ids))
            t0 = _time.perf_counter()
            out = sess.run(None, {
                "input_ids": _np.asarray([enc.ids], dtype=_np.int64),
                "attention_mask": _np.asarray([enc.attention_mask],
                                               dtype=_np.int64),
                "token_type_ids": _np.asarray([type_ids], dtype=_np.int64),
            })
            _ = out
            phases["inference"] = {"ms": (_time.perf_counter() - t0) * 1000.0}
        except Exception as exc:
            phases["inference"] = _phase_null(
                "inference-failed: %s" % type(exc).__name__)
    else:
        phases["inference"] = _phase_null("artifacts-absent")

    # 6) sql — representative retrieval round-trip on the FRESH fixture store.
    try:
        import sqlite3 as _sq
        sys.path.insert(0, str(scripts))
        from storelib import schema as _schema
        conn = _sq.connect(str(store_path))
        try:
            _schema.init_db(conn)
            insert_sql = (
                "INSERT INTO memory(id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "valid_until, ingestion_ts, retrieval_count, "
                "surfaced_count, update_of, taint, trust_score, "
                "applied_count, violated_count) VALUES "
                "(?, 'phase', 'fact', ?, 'bench', 'bench', '', 0.5, "
                "'test', '2026-01-01T00:00:00Z', '', "
                "'2026-01-01T00:00:00Z', 0, 0, '', 'trusted_internal', "
                "0.5, 0, 0)")
            conn.execute(insert_sql, ("phase-probe-1",
                                      "phase report probe row one"))
            conn.execute(insert_sql, ("phase-probe-2",
                                      "phase report probe row two"))
            conn.commit()
            t0 = _time.perf_counter()
            rows = conn.execute(
                "SELECT id FROM memory WHERE namespace = ? ORDER BY id",
                ("phase",)).fetchall()
            _ = rows
            phases["sql"] = {"ms": (_time.perf_counter() - t0) * 1000.0}
        finally:
            conn.close()
    except Exception as exc:
        phases["sql"] = _phase_null("sql-failed: %s" % type(exc).__name__)

    ort_version = None
    try:
        import onnxruntime as _ort2
        ort_version = getattr(_ort2, "__version__", None)
    except Exception:
        pass
    return {
        "schema_version": 1,
        "mode": "phase-report",
        "generated_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "identity": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "onnxruntime": ort_version,
            "executable": sys.executable,
        },
        "phases": phases,
    }


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
    parser.add_argument("--phase-report", dest="phase_report",
                        type=str, default=None,
                        help="Write a wall-clock phase-cost JSON report to "
                             "this path (measurement mode; requires --store "
                             "to name a NON-EXISTENT fresh fixture path, and "
                             "is mutually exclusive with "
                             "--compare-baseline)")
    args = parser.parse_args(argv)

    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.phase_report and args.compare_baseline:
        parser.error("--phase-report and --compare-baseline are mutually "
                     "exclusive (measurement mode vs deterministic "
                     "baseline replay)")
    if args.phase_report:
        import os as _os
        store_path = Path(args.store)
        if store_path.exists():
            # Fail closed BEFORE opening anything: phase mode creates its own
            # fixture and must never touch an existing store (a stray
            # production path here would be measured AND mutated).
            parser.error("--phase-report requires --store to name a "
                         "non-existent fresh fixture path; refusing to open "
                         "existing %s" % args.store)
        _os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        report = _measure_phases(store_path)
        with open(args.phase_report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, sort_keys=True, indent=2)
            fh.write("\n")
        json.dump({"phase_report": args.phase_report,
                   "schema_version": report["schema_version"],
                   "phases": {k: report["phases"][k].get("ms")
                              for k in PHASE_KEYS}},
                  sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0

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
        # F-014: a well-formed but non-dict baseline is a clean exit 1,
        # never a traceback.
        if not isinstance(baseline, dict):
            sys.stderr.write("bench: baseline root must be a JSON object\n")
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
        # F-006: compare the per-stage p50/p95 VALUES too — the schedule
        # is deterministic, so identical inputs must produce identical
        # numbers; a drifted value means the pinned schedule changed.
        for stage, stats in report["stages"].items():
            base = base_stages.get(stage) or {}
            if base.get("p50") != stats["p50"] or base.get("p95") != stats["p95"]:
                sys.stderr.write("bench: baseline stage values differ: %s\n" % stage)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
