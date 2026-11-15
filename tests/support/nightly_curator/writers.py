"""Candidate-writer registry with honest availability (#377 AC1).

Availability states (never a fabricated score):
- executed                        — the candidate ran and produced measured decisions.
- ineligible_for_pinned_comparison — ran or runnable, but cannot pin fields the AC1
                                    contract requires (e.g. claude-cli decoding pins);
                                    recorded with the exact unpinned-field list.
- unavailable(reason)             — no configured endpoint; the adapter path EXISTS but
                                    nothing was configured/consented (tencent, local
                                    loopback, claude without consent).
- excluded_fixture_only           — a scripted fixture, excluded from model-quality
                                    lanes by name (deepseek-scripted).

The deterministic writer drives the REAL store.py CLI (add path incl. the shipped #376
subject-aware dedup) against ISOLATED scratch stores through resources.run_bounded.
The lexical baseline is the executed no-model variant. Pinning digests (corpus/prompt/
reader/retrieval/judge) are identical across candidates by construction.
"""

from __future__ import annotations

import hashlib
import sys
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import resources

PROMPT_TEMPLATE = (
    "You are a memory-curation writer. For each numbered pair of memory rows below, "
    "decide whether they are ONE memory (same fact/task) or TWO distinct memories. "
    "Answer with strict JSON: {{\"decisions\": [{{\"id\": \"<pair-id>\", "
    "\"rows\": 1|2}}]}}\n\nPAIRS:\n{pairs}\n"
)


@dataclass
class CandidateConfig:
    name: str
    kind: str  # deterministic | lexical | fixture_only | general | tencent | local_endpoint
    provider: str
    model_revision: str
    decoding: dict[str, Any]
    endpoint_env: str = ""
    consent_required: bool = False
    fixture_only: bool = False


@dataclass
class CandidateRun:
    name: str
    availability: str = "unavailable"  # executed | ineligible | unavailable | excluded_fixture_only
    reason: str = ""
    unpinned_fields: list[str] = field(default_factory=list)
    pins: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)  # pair-id -> one_row|two_rows
    telemetry: dict[str, Any] = field(default_factory=dict)
    log_redacted: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "name": self.name, "availability": self.availability, "reason": self.reason,
            "unpinned_fields": self.unpinned_fields, "pins": self.pins,
            "decisions": self.decisions, "telemetry": self.telemetry,
            "log_redacted": self.log_redacted,
        }


def registry() -> dict[str, CandidateConfig]:
    return {
        "deterministic-zmem": CandidateConfig(
            name="deterministic-zmem", kind="deterministic", provider="zmem-storelib",
            model_revision="storelib@HEAD (deterministic write path, subject-aware dedup #376)",
            decoding={"deterministic": True},
        ),
        "lexical-baseline": CandidateConfig(
            name="lexical-baseline", kind="lexical", provider="none",
            model_revision="exact-text comparison (no model)",
            decoding={"deterministic": True},
        ),
        "deepseek-scripted": CandidateConfig(
            name="deepseek-scripted", kind="fixture_only",
            provider="deepseek scripted adapter (tests/fixtures/deepseek/helpers.mjs)",
            model_revision="scripted (no inference)", decoding={"deterministic": True},
            fixture_only=True,
        ),
        "general": CandidateConfig(
            name="general", kind="general", provider="claude-cli",
            model_revision="claude-cli@installed (--model pinned at run time)",
            decoding={"temperature": None, "top_p": None},
            endpoint_env=resources.GENERAL_WRITER_ENV, consent_required=True,
        ),
        "local-endpoint": CandidateConfig(
            name="local-endpoint", kind="local_endpoint", provider="loopback",
            model_revision="unconfigured", decoding={},
            endpoint_env=resources.LOCAL_ENDPOINT_ENV,
        ),
        "tencent": CandidateConfig(
            name="tencent", kind="tencent", provider="tencent",
            model_revision="unconfigured", decoding={},
            endpoint_env=resources.TENCANT_WRITER_ENV,
        ),
    }


def _pins_for(cfg: CandidateConfig, corpus_digest: str, judge_digest: str) -> dict[str, Any]:
    return {
        "corpus_digest": corpus_digest,
        "prompt_digest": hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest(),
        "reader_digest": hashlib.sha256(b"store.py recall defaults (fixed)").hexdigest(),
        "retrieval_digest": hashlib.sha256(b"store.py recall defaults (fixed)").hexdigest(),
        "judge_digest": judge_digest,
        "kind": cfg.kind,
        "provider": cfg.provider,
        "model_revision": cfg.model_revision,
        "decoding": cfg.decoding,
    }


def _add_row(scratch: Path, env: dict, text: str, subject: str | None, eid: str | None,
             budget: resources.Budget, type_: str = "fact") -> dict[str, Any]:
    """One store.py add through the bounded runner (real CLI, scratch store).

    The deterministic writer pins ZMEM_EMBED_PROFILE=fake (the hash-bucket
    embedder — the same deterministic profile the eval-store fixtures use) so
    no model files are needed. The six contract pairs are added with
    ``--type fact`` (the #376 harness's own default): under ``fact`` the
    subject-aware dedup VETO is live for disjoint task keys, so the
    distinct-subject pairs (task-closeout, issue-number, different-files) stay
    two rows BY CONTRACT instead of riding the fake-profile cosine — which sits
    at 0.65-0.80 for those pairs and crossed the 0.85 fold boundary on a CI
    windows runner when both rows were typed ``lesson`` (the symmetric lesson
    carve-out disables the veto; observed as a false-merge of the
    different-files contract pair at similarity 0.910).
    """
    tags = []
    if subject:
        # ALWAYS prefix task: (the #376 contract's identity convention — its own
        # harness builds tags the same way). Skipping the prefix for subjects
        # that already look like "file:..."/"lesson:..." defeats the
        # subject-aware dedup veto.
        tags.append(f"task:{subject}")
    argv = [sys.executable, str(resources.STORE_PY), "add", "--namespace", "project:synthetic/curator",
            "--content", text, "--type", type_, "--signal", "test"]
    if tags:
        argv += ["--tags", ",".join(tags)]
    if eid:
        argv += ["--evidence", eid]
    child = dict(env)
    child["ZMEM_EMBED_PROFILE"] = "fake"
    res = resources.run_bounded(
        [str(a) for a in argv], budget, child, cwd=scratch.parent,
    )
    return res


def _live_rows(scratch: Path, env: dict, budget: resources.Budget) -> int:
    import sqlite3
    db = scratch / "store.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
            "AND namespace='project:synthetic/curator'"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def run_deterministic(corpus, scratch: Path, budget: resources.Budget) -> CandidateRun:
    """The always-available writer: real store.py CLI per pair, live-row decisions."""
    run = CandidateRun(name="deterministic-zmem", availability="executed")
    env = resources.child_env(scratch)
    total_s = 0.0
    for label, subj_a, subj_b, text_a, text_b in corpus.contract_pairs:
        pair_dir = scratch / "pairs" / label
        pair_dir.mkdir(parents=True, exist_ok=True)
        penv = resources.child_env(pair_dir)
        r1 = _add_row(pair_dir, penv, text_a, subj_a, None, budget)
        r2 = _add_row(pair_dir, penv, text_b, subj_b, None, budget)
        total_s += r1["duration_s"] + r2["duration_s"]
        if r1["returncode"] != 0 or r2["returncode"] != 0:
            run.log_redacted.append(resources.redact(
                f"pair {label}: add rc {r1['returncode']}/{r2['returncode']}: "
                f"{(r1['stderr'] or r2['stderr'])[:200]}"
            ))
            run.decisions[label] = "error"
            continue
        live = _live_rows(pair_dir, penv, budget)
        run.decisions[label] = "one_row" if live <= 1 else "two_rows"
    storage = sum(
        (scratch / "pairs" / l / "store.sqlite").stat().st_size
        for l, *_ in corpus.contract_pairs
        if (scratch / "pairs" / l / "store.sqlite").exists()
    )
    run.telemetry = {
        "latency_s_total": round(total_s, 3),
        "storage_bytes_total": storage,
        "rows_added": 2 * len(corpus.contract_pairs),
        "rss_mb_writer_side": resources.measure_rss_mb(),
        "requests": 0,
        "tokens": 0,
    }
    return run


def run_lexical(corpus) -> CandidateRun:
    """The executed no-model variant: exact-text equality decisions."""
    run = CandidateRun(name="lexical-baseline", availability="executed")
    for label, _sa, _sb, text_a, text_b in corpus.contract_pairs:
        run.decisions[label] = "one_row" if text_a.strip() == text_b.strip() else "two_rows"
    run.telemetry = {
        "latency_s_total": 0.0, "storage_bytes_total": 0, "rows_added": 0,
        "rss_mb_writer_side": None, "requests": 0, "tokens": 0,
        "variant": "lexical-only/no-model",
    }
    return run


def _probe_loopback(endpoint: str) -> dict[str, Any]:
    """Probe a configured loopback endpoint (no data sent; a HEAD/empty-JSON POST)."""
    import urllib.request
    try:
        req = urllib.request.Request(endpoint, method="GET", data=b"",)
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: F841
            return {"status": "reachable"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "error": resources.redact(str(exc)[:120])}


def run_candidate(
    name: str,
    corpus,
    scratch: Path,
    budget: resources.Budget,
    judge_digest: str,
    env: dict[str, str] | None = None,
) -> CandidateRun:
    """Dispatch one candidate. `env` defaults to os.environ (test-side consent config)."""
    import os as _os
    cfg = registry().get(name)
    if cfg is None:
        raise KeyError(f"unknown candidate: {name}")
    env = env if env is not None else dict(_os.environ)

    if cfg.fixture_only:
        run = CandidateRun(
            name=name, availability="excluded_fixture_only",
            reason="scripted fixture; excluded from model-quality lanes (plan R2-4)",
        )
        run.pins = _pins_for(cfg, corpus.digest(), judge_digest)
        run.pins["fixture_only"] = True
        return run

    if name == "deterministic-zmem":
        run = run_deterministic(corpus, scratch, budget)
    elif name == "lexical-baseline":
        run = run_lexical(corpus)
    elif name == "local-endpoint":
        endpoint = env.get(cfg.endpoint_env, "")
        run = CandidateRun(name=name, availability="unavailable")
        if not endpoint:
            run.reason = f"{cfg.endpoint_env} not configured (no loopback endpoint)"
        else:
            probe = _probe_loopback(endpoint)
            if probe["status"] == "reachable":
                run.reason = "loopback reachable but no serving model configured on this box"
                run.pins = {"probe": probe}
            else:
                run.reason = f"loopback probe failed: {probe.get('error', '?')}"
                run.pins = {"probe": probe}
    elif name in ("general", "tencent"):
        run = CandidateRun(name=name)
        endpoint = env.get(cfg.endpoint_env, "")
        if not endpoint:
            run.availability = "unavailable"
            run.reason = f"{cfg.endpoint_env} not configured"
        elif name == "tencent":
            # The configured adapter path exists but this box has no Tencent endpoint
            # implementation; record honestly rather than fabricate.
            run.availability = "unavailable"
            run.reason = "tencent endpoint configured but no adapter implementation exists in-repo"
        else:
            gate = resources.TransmissionGate(env)
            if not gate.allowed:
                run.availability = "unavailable"
                run.reason = "transmission consent not granted (ZMEM_CURATOR_TRANSMIT_CONSENT != 1)"
            else:
                run = _run_claude_cli(cfg, corpus, scratch, budget, judge_digest, endpoint, env)
    else:
        raise KeyError(name)

    run.pins = _pins_for(cfg, corpus.digest(), judge_digest)
    return run


def _run_claude_cli(cfg, corpus, scratch, budget, judge_digest, endpoint, env) -> CandidateRun:
    """Invoke claude-cli behind explicit endpoint + consent, with pinned controls.

    Pinning disclosure: model + prompt are pinned; claude-cli exposes no
    temperature/top-p controls, so decoding is UNPINNED and the candidate is recorded
    ineligible_for_pinned_comparison (never a score silently equated with pinned runs).
    """
    claude = shutil.which("claude")
    run = CandidateRun(name="general", availability="ineligible_for_pinned_comparison")
    if not claude:
        run.availability = "unavailable"
        run.reason = "claude CLI not on PATH"
        return run
    run.unpinned_fields = ["decoding.temperature", "decoding.top_p"]
    pairs_text = "\n".join(
        f"{i+1}. id={label} A={ta!r} B={tb!r}"
        for i, (label, _sa, _sb, ta, tb) in enumerate(corpus.contract_pairs)
    )
    prompt = PROMPT_TEMPLATE.format(pairs=pairs_text)
    cfg_dir = scratch / "claude-home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # The prompt rides STDIN: --disallowedTools is a var-arg flag and would swallow
    # a trailing positional prompt.
    argv = [
        claude, "-p", "--output-format", "json", "--model", endpoint,
        "--max-budget-usd", "0.25",
        "--setting-sources", "",
        "--disallowedTools",
        "Bash", "Edit", "Write", "Read", "NotebookEdit",
        "WebFetch", "WebSearch", "Task", "Grep", "Glob",
    ]
    child = resources.child_env(cfg_dir)
    res = resources.run_bounded(argv, budget, child, cwd=cfg_dir,
                                stdin_data=prompt.encode("utf-8"))
    run.log_redacted.append(resources.redact((res["stdout"] or res["stderr"])[:400]))
    run.telemetry = {
        "latency_s_total": res["duration_s"], "requests": 1,
        "output_truncated": res["output_truncated"], "timed_out": res["timed_out"],
        "returncode": res["returncode"],
    }
    decisions: dict[str, str] = {}
    try:
        payload = json.loads(res["stdout"])
        if isinstance(payload, dict) and payload.get("is_error"):
            run.availability = "unavailable"
            err = resources.redact(str(payload.get("result", ""))[:160])
            run.telemetry["endpoint_error"] = True
            run.telemetry["by_construction"] = (
                "the harness child env is hermetic BY DESIGN (scratch HOME/"
                "USERPROFILE, no credential channel), so the CLI cannot see host "
                "credentials; authentication failures are expected under this env"
            )
            run.reason = (
                "unavailable by construction: hermetic child env withholds "
                "credentials (recorded CLI error: " + err + ")"
            )
            return run
        text = payload.get("result", "") if isinstance(payload, dict) else str(payload)
        start = text.find("{")
        end = text.rfind("}")
        parsed = json.loads(text[start:end + 1])
        for d in parsed.get("decisions", []):
            decisions[str(d.get("id"))] = "one_row" if int(d.get("rows", 2)) == 1 else "two_rows"
    except Exception as exc:  # noqa: BLE001
        run.reason = f"response parse failed: {resources.redact(str(exc)[:120])}"
        run.telemetry["parse_error"] = True
    run.decisions = decisions
    run.pins = dict(model=endpoint, prompt_chars=len(prompt), judge_digest=judge_digest)
    return run
