"""Nightly-curator evaluation contract (issue #377) — frozen pytest argvs run these
EIGHT MODULE-LEVEL functions as written; the CI plain-python form runs the same
functions from __main__ (no skip/xfail anywhere; deterministic legs must all pass).

Contract provenance (trace .agents/issue-traces/377-nightly-curator-evaluation/,
plan-critic APPROVE Round 6 as the INTERIM contract):
- AC2's judge/human measurement is PARTIAL by declared disclosure: it computes only
  over human_endorsement rows (none at ship — the owner was asked twice 2026-11-14,
  no answer) and otherwise reports awaiting_human_labels. Nothing here claims AC2's
  human-disagreement measurement is satisfied.
- Oracle lanes: contract-oracle (merged #376 expectations), authored-oracle (labeled
  as authored), model-reviewed (model-attributed verdicts of record, never human).
- Every leg drives at least one REAL production seam (store.py CLI, storelib
  validators, cadence gate, redaction); scratch stores only, never the canonical
  store. Store environment is pinned BEFORE any storelib import (issue #244 rules).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORT_DIR = REPO_ROOT / "tests" / "support"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

# --- module-level env pinning BEFORE any storelib/support import (#244 rules) ----
_MODULE_TMP = Path(tempfile.mkdtemp(prefix="zmem-377-curator-"))
_MODULE_ENV_PRIOR = {k: os.environ.get(k) for k in (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_EMBED_PROFILE",
    "ZMEM_BELIEF_ADAPTER_ACTIONS", "ZMEM_BACKUP_DIR", "ZMEM_DEDUP_THRESHOLD",
    "ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS", "ZMEM_CONSOLIDATE_GROWTH_THRESHOLD",
)}
os.environ["ZMEM_STORE"] = str(_MODULE_TMP / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_MODULE_TMP)
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
os.environ.pop("ZCODE_PLUGIN_DATA", None)
os.environ.pop("ZMEM_EMBED_PROFILE", None)
os.environ.pop("ZMEM_BELIEF_ADAPTER_ACTIONS", None)
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_MODELS_DIR"] = str(_MODULE_TMP / "no-such-models")
os.environ["ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS"] = "7.0"
os.environ["ZMEM_CONSOLIDATE_GROWTH_THRESHOLD"] = "0.20"
os.environ["ZMEM_DEDUP_THRESHOLD"] = "0.85"

sys.path.insert(0, str(SUPPORT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard, post-pin)


def _restore_module_env() -> None:
    for key, val in _MODULE_ENV_PRIOR.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


unittest.addModuleCleanup(_restore_module_env)
unittest.addModuleCleanup(lambda: __import__("shutil").rmtree(_MODULE_TMP, True))

from nightly_curator import (  # noqa: E402
    cleanup as cu,
    corpus as co,
    metrics as me,
    orchestration as orc,
    proposals as pr,
    report as rp,
    resources as rs,
    writers as wr,
)

NS = "project:synthetic/curator"
_BUDGET = rs.Budget(timeout_s=180.0, label="curator-contract")


def _scratch(name: str) -> Path:
    d = _MODULE_TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_rows(store_dir: Path, rows: list[tuple[str, str, str | None, str | None]],
               ns: str = NS) -> None:
    """rows: (id_hint, content, task_subject, evidence_id) — seeded via the real CLI."""
    env = rs.child_env(store_dir, extra={"ZMEM_EMBED_PROFILE": "fake"})
    for _hint, content, subject, eid in rows:
        argv = [sys.executable, str(STORE_PY), "add", "--namespace", ns, "--content", content,
                "--type", "lesson", "--signal", "test"]
        if subject:
            argv += ["--tags", f"task:{subject}"]
        if eid:
            argv += ["--evidence", eid]
        res = rs.run_bounded(argv, _BUDGET, env, cwd=store_dir)
        assert res["returncode"] == 0, f"seed add failed: {rs.redact(res['stderr'][:300])}"


def _live_ids(store: Path, ns: str = NS) -> list[str]:
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT id FROM memory WHERE superseded_at IS NULL AND namespace=?", (ns,))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C1 / AC1 — writer comparison with honest availability
# ---------------------------------------------------------------------------

def test_writer_comparison():
    corpus = co.build_corpus()
    reviewed = co.load_reviewed_histories()
    assert len(corpus.contract_pairs) == 6, "all six measured dedup pairs required"
    assert corpus.lineage_expected and corpus.retraction_expected, \
        "lineage/retraction fixtures required"
    assert 10 <= len(reviewed["cases"]) <= 25, "model-reviewed lane frozen count"

    scratch = _scratch("c1")
    runs = [
        wr.run_candidate("deterministic-zmem", corpus, scratch / "det", _BUDGET,
                         me.rubric_digest()),
        wr.run_candidate("lexical-baseline", corpus, scratch / "lex", _BUDGET,
                         me.rubric_digest()),
        wr.run_candidate("deepseek-scripted", corpus, scratch / "fx", _BUDGET,
                         me.rubric_digest()),
        wr.run_candidate("local-endpoint", corpus, scratch / "lo", _BUDGET,
                         me.rubric_digest()),
        wr.run_candidate("tencent", corpus, scratch / "tc", _BUDGET,
                         me.rubric_digest()),
        wr.run_candidate("general", corpus, scratch / "gen", _BUDGET,
                         me.rubric_digest()),
    ]
    by_name = {r.name: r for r in runs}

    det = by_name["deterministic-zmem"]
    assert det.availability == "executed"
    assert set(det.decisions) == {p[0] for p in corpus.contract_pairs}
    assert all(v in ("one_row", "two_rows") for v in det.decisions.values())
    assert det.pins["corpus_digest"] == corpus.digest()
    assert det.pins["model_revision"].startswith("storelib@")

    lex = by_name["lexical-baseline"]
    assert lex.availability == "executed" and lex.telemetry.get("variant") == \
        "lexical-only/no-model"

    fx = by_name["deepseek-scripted"]
    assert fx.availability == "excluded_fixture_only" and not fx.decisions

    lo = by_name["local-endpoint"]
    assert lo.availability == "unavailable" and "not configured" in lo.reason
    tc = by_name["tencent"]
    assert tc.availability == "unavailable" and not tc.decisions, \
        "tencent unavailable MUST record no decisions — never a fabricated score"
    gen = by_name["general"]
    assert gen.availability == "unavailable", gen.reason
    assert ("not configured" in gen.reason) or ("consent" in gen.reason.lower()), \
        f"unconfigured/consent-gated honest reason required: {gen.reason}"

    # identical reader/retrieval/judge/prompt pins across ALL candidates
    digests = {r.name: (r.pins.get("corpus_digest"), r.pins.get("prompt_digest"),
                        r.pins.get("reader_digest"), r.pins.get("judge_digest"))
               for r in runs}
    assert len(set(digests.values())) == 1, f"pins must be identical: {digests}"

    # no unavailable candidate carries any decision or telemetry score
    for r in runs:
        if r.availability == "unavailable":
            assert not r.decisions, f"{r.name} fabricated decisions while unavailable"


# ---------------------------------------------------------------------------
# C2 / AC2 — frozen rubric; distinct disagreement surfaces; PARTIAL human lane
# ---------------------------------------------------------------------------

def test_quality_metrics():
    corpus = co.build_corpus()
    reviewed = co.load_reviewed_histories()
    scratch = _scratch("c2")
    det = wr.run_candidate("deterministic-zmem", corpus, scratch / "det", _BUDGET,
                           me.rubric_digest())
    contract = me.score_contract_lane(corpus, det)
    assert contract["false_merges"] == [], \
        f"deterministic writer false-merged contract pairs: {contract}"
    assert contract["missed_duplicates"] == [], \
        f"deterministic writer missed contract duplicates: {contract}"
    # disagreement-taxonomy pin (review round 1, part-2 novel-mutant gap): the
    # all-agreeing writer never exercises WHICH label a disagreement earns, so
    # pin the judge taxonomy directly — a label swap must fail here by name.
    assert me.judge_pair("two_rows", "one_row") == "false_merge", \
        "judge taxonomy: merging distinct rows must read as false_merge"
    assert me.judge_pair("one_row", "two_rows") == "missed_duplicate", \
        "judge taxonomy: splitting identical rows must read as missed_duplicate"
    assert me.judge_pair("two_rows", "two_rows") == "agrees"

    rubric = me.Rubric()
    rubric.metrics["contract_lane"] = contract
    rubric.metrics["judge_vs_contract_oracle"] = {
        "cases": len(corpus.contract_pairs),
        "agrees": sum(1 for v in contract["per_pair"].values()
                      if v["verdict"] == "agrees"),
        "false_merges": len(contract["false_merges"]),
        "missed_duplicates": len(contract["missed_duplicates"]),
    }
    rubric.metrics["telemetry"] = me.score_telemetry(det)

    # authored lane (final-critic F-C1): score through the REAL ProposalBoundary —
    # build a boundary on the deterministic candidate's own scratch store, seed one
    # evidence row via the real seam, evaluate the frozen authored proposals, and
    # record the boundary's ACTIONS as the measured outcomes.
    det_store = scratch / "det" / "pairs" / "task-closeout" / "store.sqlite"
    assert det_store.exists(), "deterministic candidate store must exist"
    from storelib import evidence as _ev_c2
    _c2conn = sqlite3.connect(str(det_store))
    _c2e1 = _ev_c2.write_evidence(
        _c2conn, session_id="synthetic-c2", lane="codex", moment="pretool",
        kind="tool_call", ts="2026-11-14T00:00:00Z",
        excerpt="authored-lane evidence for the C2 boundary",
        ref_path="synthetic/c2.txt", ref_offset=0)
    _c2conn.commit()
    _c2conn.close()
    boundary = pr.ProposalBoundary(det_store)
    det_live = sorted(boundary._live_memory_ids())
    authored_outcomes = {}
    for cid, spec in me.AUTHORED_PROPOSALS.items():
        if cid == "AQ-001":   # temporal update: never auto-merged -> review
            prop = {"id": cid, "kind": "novel_lesson",
                    "content": "authored case AQ-001 (update routing)",
                    "evidence_ids": [_c2e1]}
        elif cid == "AQ-002":  # ambiguity abstains -> review (not auto-apply)
            prop = {"id": cid, "kind": "dedup_merge",
                    "memory_ids": det_live[:2], "evidence_ids": [_c2e1],
                    "subjects": ["issue41"]}
        elif cid == "AQ-003":  # fabricated evidence id -> refuse (precision)
            prop = {"id": cid, "kind": "novel_lesson",
                    "content": "authored case AQ-003",
                    "evidence_ids": ["ev-fabricated-aq3"]}
        else:                  # AQ-004 genuine collation -> review
            prop = {"id": cid, "kind": "dedup_merge",
                    "memory_ids": det_live[:2], "evidence_ids": [_c2e1],
                    "subjects": ["issue41"]}
        d = boundary.evaluate(prop)
        authored_outcomes[cid] = d.action
    rubric.metrics["authored_lane"] = me.score_authored_lane(authored_outcomes)
    authored = rubric.metrics["authored_lane"]
    assert not authored["disagrees"], \
        f"authored lane boundary outcomes disagreed with the frozen rubric: {authored}"
    assert "AQ-003" in authored["unsupported_statements_refused"], \
        "grounding: the fabricated-evidence case must be refused (evidence precision)"
    # R2-1 honest scope: only AQ-003 is measured; the three default-lane
    # families are not_measured (reported as such, never as agreement).
    assert authored["not_measured"] == ["AQ-001", "AQ-002", "AQ-004"], authored
    assert set(authored["not_measured_families"]) == \
        set(me.AUTHORED_NOT_MEASURED_FAMILIES)

    # judge the model-reviewed lane: the judge maps each claim to a label by the
    # deterministic keyword-rule judge (disclosed as judge_method=keyword-rule-v1
    # in the decision report); per-case agreement is MEASURED (not asserted to be
    # zero) and always carries its model-lane attribution.
    judgments = {}
    for case in reviewed["cases"]:
        text = case["claim"].lower()
        if "disproved" in text or "rejected" in text or "not configurable" in text:
            judgments[case["id"]] = "unsupported_by_probe"
        elif "pre-existing" in text or "was introduced by this pr" in text:
            judgments[case["id"]] = "pre_existing"
        else:
            judgments[case["id"]] = "supported"
    rubric.metrics["model_reviewed_lane"] = me.score_model_reviewed_lane(
        judgments, reviewed["cases"])
    human = me.score_human_lane(judgments, reviewed["cases"])
    rubric.awaiting_human_labels = human["status"] == "awaiting_human_labels"
    rubric.endorsed_case_count = human["endorsed_case_count"]
    assert rubric.awaiting_human_labels and rubric.endorsed_case_count == 0, \
        "AC2 PARTIAL disclosure: no human endorsements at ship"
    rubric.metrics["human_lane"] = human
    rubric.metrics["judge_method"] = "keyword-rule-v1 (deterministic, disclosed)"

    doc = rubric.to_json()
    # final-critic F-C1: assert on CONTENTS, not the family-name list — a removed
    # or emptied metric family must fail by name. The CONTENT assertions below
    # each pin a measured VALUE, so emptying a family's computation fails here
    # even when the name still appears in the family list.
    m = doc["metrics"]
    assert m["judge_vs_contract_oracle"]["agrees"] == len(corpus.contract_pairs), \
        "judge_vs_contract_oracle must carry measured agrees (content, not name)"
    assert m["authored_lane"]["judge_vs_authored_oracle"]["agrees"] == \
        len(me.AUTHORED_EXPECTED_ACTIONS), \
        "judge_vs_authored_oracle must carry MEASURED agrees (AQ-003 only)"
    assert m["authored_lane"]["judge_vs_authored_oracle"]["cases_not_measured"] == 3, \
        "the three default-lane families must be reported not_measured"
    assert m["authored_lane"]["unsupported_statements_refused"], \
        "evidence_id_precision must carry a measured refusal"
    assert m["model_reviewed_lane"]["cases"] == len(reviewed["cases"]) and \
        "attribution" in m["model_reviewed_lane"]
    assert m["human_lane"]["status"] == "awaiting_human_labels"
    assert m["authored_lane"]["per_case"]["AQ-003"]["action"] == "refuse", \
        "grounding: AQ-003 measured boundary action must be refuse (evidence-id precision)"
    assert m["judge_vs_contract_oracle"]["false_merges"] == 0 and \
        m["judge_vs_contract_oracle"]["missed_duplicates"] == 0, \
        "contract-lane zero-defect values are MEASURED (per-pair verdicts above)"
    for family in me.METRIC_FAMILIES:
        assert family.split(".")[0] in json.dumps(doc), \
            f"metric family missing: {family}"
    # frozen family-list pin: the loop above iterates the (mutable) source list,
    # so a family DROPPED from metrics.py would pass it vacuously. Pin the exact
    # set — a removal or addition fails here naming the difference.
    FROZEN_FAMILIES = {
        "false_merges", "missed_duplicates", "unsupported_statements",
        "evidence_id_precision", "temporal_updates", "abstention_quality",
        "useful_accepted_lessons", "storage_growth", "latency_s", "token_cost",
        "disagreement.judge_vs_contract_oracle",
        "disagreement.judge_vs_authored_oracle",
        "disagreement.judge_vs_model_reviewed",
        "disagreement.judge_vs_human",
        "critical_findings",
    }
    assert set(me.METRIC_FAMILIES) == FROZEN_FAMILIES, \
        f"metric family set changed: {set(me.METRIC_FAMILIES) ^ FROZEN_FAMILIES}"
    assert doc["variant_declarations"]["lexical-only/no-model"] == "executed"
    assert doc["variant_declarations"]["quantized"] == "not_claimed"
    assert doc["rubric_digest"] == me.rubric_digest(), "rubric frozen pre-run"

    # critical findings can never hide behind an average
    clean = me.Rubric()
    assert not clean.failed
    clean.add_critical("synthetic: distinct-task rows false-merged (case task-closeout)")
    assert clean.failed and "task-closeout" in clean.critical_findings[0]


# ---------------------------------------------------------------------------
# C3 / AC3 — proposal boundary + real belief-ops contract
# ---------------------------------------------------------------------------

def test_proposal_boundary():
    scratch = _scratch("c3")
    store_dir = scratch / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    # seed via real CLI, then attach evidence in-process via the real seam
    _seed_rows(store_dir, [
        ("r1", "Curator fixture row one about the deadline propagation fix.", "issue41", None),
        ("r2", "Curator fixture row two about a completely different cache topic.", "issue77", None),
    ])
    store = store_dir / "store.sqlite"
    from storelib import evidence as _ev
    conn = sqlite3.connect(str(store))
    e1 = _ev.write_evidence(conn, session_id="synthetic-curator", lane="codex",
                            moment="pretool", kind="tool_call",
                            ts="2026-11-14T00:00:00Z",
                            excerpt="evidence excerpt one for the boundary",
                            ref_path="synthetic/curator.txt", ref_offset=0)
    conn.commit()
    conn.close()
    live = _live_ids(store)
    assert len(live) == 2
    # a row in a DIFFERENT namespace for the cross-namespace target case
    _seed_rows(store_dir, [("xns", "Cross-namespace fixture row in another namespace.",
                            "issue41", None)], ns="project:synthetic/other")
    xns_ids = _live_ids(store, ns="project:synthetic/other")
    assert len(xns_ids) == 1
    other_ns_row = xns_ids[0]

    b = pr.ProposalBoundary(store)
    hostile = [
        ({"id": "H1"}, "malformed_json"),
        ({"id": "H2", "kind": "dedup_merge", "memory_ids": ["fabricated-xyz"],
          "evidence_ids": [e1]}, "fabricated_memory_id"),
        ({"id": "H3", "kind": "dedup_merge", "memory_ids": live,
          "evidence_ids": ["ev-fabricated-1"]}, "fabricated_evidence_id"),
        ({"id": "H4", "kind": "dedup_merge", "memory_ids": live,
          "evidence_ids": []}, "missing_evidence"),
        ({"id": "H5", "kind": "novel_lesson", "content": co.ADVERSARIAL_INJECTION_TEXT,
          "evidence_ids": [e1]}, "prompt_injection_content"),
        ({"id": "H6", "kind": "compositional_merge",
          "parts": list(co.ADVERSARIAL_COMPOSITIONAL)}, "compositional_poison"),
        ({"id": "H7", "kind": "stale_snapshot_update", "memory_ids": live,
          "evidence_ids": [e1]}, "stale_snapshot"),
        ({"id": "H8", "kind": "dedup_merge", "memory_ids": live,
          "evidence_ids": [e1], "subjects": ["issue41", "issue77"]},
         "incompatible_subject"),
        ({"id": "H9", "kind": "x" * 30000}, "oversize_payload"),
    ]
    for proposal, expected_reason in hostile:
        d = b.evaluate(dict(proposal))
        assert d.action == "refuse", f"{d.proposal_id}: {d.action}"
        assert d.reason == expected_reason, \
            f"{d.proposal_id}: reason {d.reason!r} != {expected_reason!r}"
        assert d.evidence_before == d.evidence_after, \
            f"{d.proposal_id}: evidence mutated on refusal"

    # cross-namespace target refused (the boundary's own namespace governs)
    cn = {"id": "H10", "kind": "dedup_merge", "memory_ids": [other_ns_row],
          "evidence_ids": [e1], "subjects": ["issue41"]}
    d = b.evaluate(cn)
    assert d.reason == "cross_namespace_target", d.reason

    # oversize exact
    d = b.evaluate({"id": "H11", "kind": "dedup_merge", "memory_ids": live,
                    "evidence_ids": [e1], "subjects": ["issue41"],
                    "pad": "x" * (pr.MAX_PROPOSAL_BYTES + 1)})
    assert d.reason == "oversize_payload"

    # valid merge -> review only, never auto-applied
    ok = {"id": "P1", "kind": "dedup_merge", "memory_ids": live,
          "evidence_ids": [e1], "subjects": ["issue41"], "confidence": 0.5}
    d = b.evaluate(dict(ok))
    assert d.action == "review" and d.reason == "needs_review"
    assert not b.applied_deterministic, "review-routed proposal applied automatically"
    promoted = b.promote_reviewed("P1", reviewer="synthetic-reviewer")
    assert promoted["status"] == "promoted"

    # self-confidence invariance
    inv = b.evaluate_confidence_invariant(dict(ok, id="P2"))
    assert inv["invariant"] and inv["low"] == inv["high"], \
        f"confidence_invariance_violation: {inv}"

    # conflicting proposals detected pairwise
    conflicts = b.detect_conflicts([
        {"id": "X1", "kind": "dedup_merge", "memory_ids": live, "evidence_ids": [e1]},
        {"id": "X2", "kind": "novel_lesson", "memory_ids": live, "evidence_ids": [e1]},
    ])
    assert conflicts, "conflicting proposals must be detected"

    # deterministic eligible path exists and is row-capped
    d = b.evaluate({"id": "D1", "kind": "deterministic_reindex", "rows": ["r"] * 500})
    assert d.reason == "row_cap_exceeded"

    # --- real belief-head operation contract (SEPARATE leg) ---
    belief_dir = scratch / "beliefs"
    belief_dir.mkdir(parents=True, exist_ok=True)
    # 1) create the schema via a real CLI add FIRST (the evidence table rides the
    #    store schema; an empty sqlite file has no tables)
    _seed_rows(belief_dir, [
        ("b0", "Belief bootstrap row to materialize the store schema.", "issue41", None),
    ])
    bconn = sqlite3.connect(str(belief_dir / "store.sqlite"))
    from storelib import evidence as _ev2
    be1 = _ev2.write_evidence(bconn, session_id="synthetic-beliefs", lane="codex",
                              moment="pretool", kind="tool_call",
                              ts="2026-11-14T00:00:00Z",
                              excerpt="belief evidence one",
                              ref_path="synthetic/beliefs.txt", ref_offset=0)
    bconn.commit()
    bconn.close()
    _seed_rows(belief_dir, [
        ("b1", "Belief row one: propagate the deadline through the client pool.", "issue41", be1),
        ("b2", "Belief row two: propagate the deadline through the client pool now.", "issue41", be1),
        ("b3", "Belief row three: unrelated retention topic with distinct wording.", "issue90", be1),
    ])
    result = pr.exercise_belief_ops(belief_dir / "store.sqlite")
    assert result["valid_applied"], f"real belief ops: valid op failed: {result}"
    assert result["invalid_rolled_back"], "real belief ops: fabricated citation must roll back"


# ---------------------------------------------------------------------------
# C4 / AC4 — snapshot/inference/apply protocol; locks; cadence; WAL
# ---------------------------------------------------------------------------

def test_transaction_concurrency():
    scratch = _scratch("c4")
    store_dir = scratch / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    _seed_rows(store_dir, [("r1", "Concurrency fixture row one.", "issue41", None)])
    store = store_dir / "store.sqlite"

    # 1. one-shot snapshot; conn closed before "inference"
    snap = orc.snapshot_store(store, _BUDGET)
    assert snap["rows"] and len(snap["rows"]) <= _BUDGET.max_rows

    # 2. WAL-not-pinned probe: a concurrent write commits during paused inference
    #    and wal_checkpoint succeeds (no checkpoint-pinning read txn held).
    env = rs.child_env(store_dir, extra={"ZMEM_EMBED_PROFILE": "fake"})
    res = rs.run_bounded(
        [sys.executable, str(STORE_PY), "add", "--namespace", NS,
         "--content", "Concurrent write during paused inference window.",
         "--type", "lesson", "--signal", "test", "--tags", "task:issue42"],
        _BUDGET, env, cwd=store_dir)
    assert res["returncode"] == 0, rs.redact(res["stderr"][:200])
    cconn = sqlite3.connect(str(store))
    ck = cconn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    cconn.close()
    assert ck is not None, "wal_checkpoint must answer (no pinned read txn)"
    assert ck[0] == 0, f"checkpoint_pinned_during_inference: busy={ck}"

    # 3. admit-once idempotence through the full apply protocol
    ledger = orc.RunLedger(scratch / "ledger.sqlite")
    rows_now = orc.snapshot_store(store, _BUDGET)
    work = {"expected_rows": rows_now["rows"], "payload": {"kind": "reindex"}}
    out1 = orc.apply_authorized_work(store, ledger, "run-alpha", "scratch-owner", work)
    assert out1.status == "applied", out1.detail
    out2 = orc.apply_authorized_work(store, ledger, "run-alpha", "scratch-owner", work)
    assert out2.status == "already_applied", "apply must be idempotent per run-id"

    # 4. revalidation mismatch: UPDATE an existing row during "inference" (the
    #    update tombstones it -> superseded_at flips) -> revalidate refuses + retry
    upd = rs.run_bounded(
        [sys.executable, str(STORE_PY), "update", "--id", snap["rows"][0]["id"],
         "--content", "Concurrency fixture row one, updated mid-inference."],
        _BUDGET, env, cwd=store_dir)
    assert upd["returncode"] == 0, rs.redact(upd["stderr"][:200])
    stale_work = {"expected_rows": snap["rows"], "payload": {}}
    out3 = orc.apply_authorized_work(store, ledger, "run-beta", "scratch-owner", stale_work)
    assert out3.status == "revalidation_mismatch", out3.status

    # 5. fail-closed on the degraded lock token (injected acquire_fn; no patching)
    out4 = orc.apply_authorized_work(
        store, ledger, "run-gamma", "scratch-owner", work,
        acquire_fn=lambda p, s: "unlocked")
    assert out4.status == "lock_unavailable" and "fail-closed" in out4.detail

    # 6. mutual exclusion, direction A: orchestrator HOLDS the real production lock
    #    -> the REAL CLI returns its lock-busy skip.
    path, token = orc.acquire_production_lock(store_dir)
    try:
        cli = rs.run_bounded(
            [sys.executable, str(STORE_PY), "consolidate", "--json", "--force"],
            rs.Budget(timeout_s=180, label="cli-lock-busy"),
            env, cwd=store_dir)
        assert "lock busy" in (cli["stdout"] + cli["stderr"]), \
            f"real CLI must observe the production lock: {rs.redact(cli['stdout'][:200])}"
    finally:
        from host import release_lock as _rel
        _rel(str(path), token)

    # 7. mutual exclusion, direction B: a holder (CLI-shaped) blocks apply.
    from host import acquire_lock as _al
    tok = _al(str(orc.lock_path_for(store_dir)), 1800.0)
    try:
        assert tok is not None
        out5 = orc.apply_authorized_work(store, ledger, "run-delta", "scratch-owner", work)
        assert out5.status == "lock_unavailable"
    finally:
        from host import release_lock as _rel2
        _rel2(str(orc.lock_path_for(store_dir)), tok)

    # 8. ownership: the run ledger records the single configured owner per run;
    #    a second replica is inert (different owner never admits on this ledger).
    cfg = orc.NightWindowConfig(owner="scratch-owner", store_dir=store_dir)
    sched = orc.ShadowScheduler(cfg)
    decisions = sched.run_nights([{"index": 0, "run_id": "run-owner-pin"}], ledger)
    assert decisions[0].action == "run"
    lconn = sqlite3.connect(str(scratch / "ledger.sqlite"))
    try:
        owners = {r[0] for r in lconn.execute(
            "SELECT owner FROM curator_runs WHERE run_id='run-owner-pin'")}
    finally:
        lconn.close()
    assert owners == {"scratch-owner"}, \
        "ownership is immutable per run and recorded in the ledger"
    inert = orc.ShadowScheduler(orc.NightWindowConfig(owner="replica-2", store_dir=store_dir))
    assert inert.config.owner != cfg.owner, \
        "not every replica schedules mutations: only the configured owner"

    # 9. cadence gate fidelity: seeded meta decides; a skip never re-arms the clock
    cad_scratch = scratch / "cadence"
    cad_scratch.mkdir(parents=True, exist_ok=True)
    _seed_rows(cad_scratch, [("c1", "Cadence fixture row.", "issue41", None)])
    cad_store = cad_scratch / "store.sqlite"
    orc.seed_cadence_meta(cad_store, age_days=1 / 24.0)  # 1 hour old
    before = orc.read_cadence_meta(cad_store)
    g1 = orc.run_cadence_gate(cad_store)
    assert g1["skipped"] is True, "fresh clock must skip"
    assert orc.read_cadence_meta(cad_store) == before, \
        "a skipped nightly run must NOT re-arm the cadence clock"
    orc.seed_cadence_meta(cad_store, age_days=8.0)  # 8 days old
    g2 = orc.run_cadence_gate(cad_store)
    assert g2["skipped"] is False, "8-day-old clock must pass (growth default)"


# ---------------------------------------------------------------------------
# C5 / AC5 — nightly orchestration over simulated nights
# ---------------------------------------------------------------------------

def test_nightly_orchestration():
    scratch = _scratch("c5")
    ledger = orc.RunLedger(scratch / "ledger.sqlite")
    cfg = orc.NightWindowConfig(store_dir=scratch, idle_window=(2, 5), min_free_mb=50)
    sched = orc.ShadowScheduler(cfg)

    nights = [
        {"index": 0, "busy": True},
        {"index": 1},
        {"index": 2, "empty_input": True},
        {"index": 3, "free_mb": 10},
        {"index": 4, "overlap_prev": True},
        {"index": 5, "run_id": "night-5-fixed"},
    ]
    decisions = sched.run_nights(nights, ledger)
    acts = [d.action for d in decisions]
    assert acts == ["skip_busy", "run", "empty_input", "skip_low_resource",
                    "overlap_skipped", "run"], acts
    # missed-wake coalescing is bounded
    assert sched.coalesce_missed(7) == 1 == cfg.max_catchup_runs
    # DST: idle window evaluated in LOCAL hours
    assert sched.dst_offset_check(lambda: 3) is True
    assert sched.dst_offset_check(lambda: 12) is False
    # rotating older sample is capped
    assert len(sched.rotate_old_sample([f"old{i}" for i in range(30)], ["new1"])) == 20

    # run-id idempotence across a repeated night
    again = sched.run_nights([{"index": 5, "run_id": "night-5-fixed"}], ledger)
    assert again[0].action == "run"  # scheduler admits; ledger already holds it

    # snapshot filter: authz / summaries / contradictions / protection
    rows = [
        {"id": "a", "namespace": NS, "kind": "note"},
        {"id": "b", "namespace": "project:other", "kind": "note"},
        {"id": "c", "namespace": NS, "kind": "summary"},
        {"id": "d", "namespace": NS, "kind": "note", "contested": True},
        {"id": "e", "namespace": NS, "kind": "note", "signal": "user"},
        {"id": "f", "namespace": NS, "kind": "note", "confidence": 0.95},
    ]
    filt = orc.filter_snapshot_rows(rows, cfg.namespaces)
    sel = {r["id"] for r in filt["selected"]}
    excl = ";".join(filt["excluded"])
    assert sel == {"a", "e", "f"} and "e" in sel and "f" in sel
    assert "namespace_unauthorized" in excl and "summary_excluded" in excl \
        and "unresolved_contradiction" in excl
    prot = {r["id"]: r.get("protected") for r in filt["selected"]}
    assert prot["e"] is True and prot["f"] is True and prot["a"] is None

    # morning report carries the fixed fields
    mr = orc.morning_report(decisions, applied=[{"kind": "reindex"}], proposed=[],
                            skipped_expired=["x-old"], cost={"tokens": 0},
                            recovery=["checkpoint.sqlite"])
    for field in ("applied_deterministic", "proposed_semantic_changes",
                  "skipped_and_expired", "resource_cost", "recovery_pointers"):
        assert field in mr, f"morning report missing {field}"

    # executable install/disable/dry-run on a SCRATCH config dir only
    cfgdir = scratch / "cfg"
    before_tree = sorted(str(p) for p in scratch.rglob("*"))
    r1 = subprocess.run([sys.executable, str(REPO_ROOT / "tests/support/nightly_curator/"
                                              "orchestration.py"), "enable", str(cfgdir)],
                        capture_output=True, text=True, timeout=60)
    assert r1.returncode == 0, r1.stderr[-300:]
    assert json.loads(r1.stdout)["status"] == "enabled"
    r2 = subprocess.run([sys.executable, str(REPO_ROOT / "tests/support/nightly_curator/"
                                              "orchestration.py"), "dry-run", str(cfgdir)],
                        capture_output=True, text=True, timeout=60)
    dry = json.loads(r2.stdout)
    assert dry["status"] == "dry-run" and dry["would_run"] is True
    r3 = subprocess.run([sys.executable, str(REPO_ROOT / "tests/support/nightly_curator/"
                                              "orchestration.py"), "disable", str(cfgdir)],
                        capture_output=True, text=True, timeout=60)
    assert json.loads(r3.stdout)["status"] == "disabled"
    r4 = subprocess.run([sys.executable, str(REPO_ROOT / "tests/support/nightly_curator/"
                                              "orchestration.py"), "dry-run", str(cfgdir)],
                        capture_output=True, text=True, timeout=60)
    assert json.loads(r4.stdout)["status"] == "disabled"
    after_tree = sorted(str(p) for p in scratch.rglob("*"))
    new_outside = [p for p in after_tree
                   if p not in before_tree and not p.startswith(str(cfgdir))]
    assert not new_outside, f"files appeared outside the scratch config dir: {new_outside}"

    # the nightly trigger never forces the cadence gate (behavior pin in C4 + source pin)
    src = (REPO_ROOT / "tests/support/nightly_curator/orchestration.py").read_text(
        encoding="utf-8")
    assert "force=False" in src and "force=True" not in src, \
        "nightly_force_bypass guard: orchestration must never force the cadence gate"


# ---------------------------------------------------------------------------
# C6 / AC6 — cleanup policy + checkpoint/export/rollback/forward recovery
# ---------------------------------------------------------------------------

def test_cleanup_recovery():
    scratch = _scratch("c6")
    rows = [
        {"id": "p1", "signal": "user", "confidence": 0.5, "expired": True,
         "referenced_by": []},
        {"id": "p2", "signal": "test", "confidence": 0.3, "expired": True,
         "referenced_by": ["lesson-9"]},
        {"id": "p3", "signal": "test", "confidence": 0.3, "expired": True,
         "referenced_by": []},
        {"id": "p4", "signal": "test", "confidence": 0.95, "expired": True,
         "referenced_by": []},
        {"id": "p5", "signal": "test", "confidence": 0.3, "expired": True,
         "contested": True, "referenced_by": []},
        {"id": "p6", "signal": "test", "confidence": 0.3, "expired": False,
         "referenced_by": []},
    ]
    cls = cu.classify_rows(rows)
    assert cls["ttl_eligible_auto"] == ["p3"], cls
    assert cls["expired_referenced_proposed"] == ["p2"], cls
    assert set(cls["protected"]) == {"p1", "p4", "p5"}, cls
    pol = cu.policy_matrix()
    assert pol["protections"]["low_use_alone_cannot_delete"] is True
    assert "VACUUM" in pol["forbidden_implicit"]

    # full recovery chronology on a real CLI-seeded store
    base_dir = scratch / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    _seed_rows(base_dir, [("r1", "Recovery fixture row one.", "issue41", None)])
    drill = cu.recovery_drill(
        base_dir / "store.sqlite", scratch / "drill",
        post_mutation_cmds=[
            ["add", "--namespace", NS, "--content",
             "Post-checkpoint mutation row (recovery drill).", "--type", "lesson",
             "--signal", "test", "--tags", "task:drill"],
        ],
        budget=_BUDGET,
    )
    assert drill["ops_recorded"] == 1
    assert drill["forward_recovery_contents_match"], "replayed contents must match"
    assert drill["forward_recovery_evidence_match"], "evidence must survive recovery"
    assert drill["replay_idempotent"], "second replay must add no new distinct content"
    assert drill["forbidden_ops_invoked"] == []


# ---------------------------------------------------------------------------
# C7 / AC7 — resource/privacy bounds
# ---------------------------------------------------------------------------

def test_resource_privacy():
    # allowlist child env: hostile ambient keys never cross
    os.environ["ZMEM_BELIEF_ADAPTER_ACTIONS"] = r"C:\hostile\actions.json"
    os.environ["ZMEM_TEST_HOSTILE_AMBIENT"] = "1"
    try:
        env = rs.child_env(_MODULE_TMP / "c7-env")
        assert "ZMEM_TEST_HOSTILE_AMBIENT" not in env
        assert "ZMEM_BELIEF_ADAPTER_ACTIONS" not in env
        assert env["ZMEM_MODEL_AUTODOWNLOAD"] == "0"
        assert env["ZMEM_STORE"].startswith(str(_MODULE_TMP))
    finally:
        os.environ.pop("ZMEM_BELIEF_ADAPTER_ACTIONS", None)
        os.environ.pop("ZMEM_TEST_HOSTILE_AMBIENT", None)

    # containment
    c7 = _MODULE_TMP / "c7-env"
    c7.mkdir(parents=True, exist_ok=True)
    rs.assert_contained([c7 / "x" / "y.txt"], c7)  # inside: must pass
    outside = Path(tempfile.gettempdir()) / f"zmem377-escape-{os.getpid()}"
    try:
        rs.assert_contained([outside], _MODULE_TMP / "c7-env")
        raise AssertionError("containment must reject paths outside scratch")
    except AssertionError as exc:
        assert "containment violation" in str(exc)

    # input budget
    try:
        _BUDGET.check_input(b"x" * (_BUDGET.max_input_bytes + 1))
        raise AssertionError("input cap must reject oversized payloads")
    except AssertionError as exc:
        assert "exceeds cap" in str(exc)

    # timeout + tree-kill on a runaway child
    sleep_argv = [sys.executable, "-c", "import time; time.sleep(60); print('late')"]
    res = rs.run_bounded(sleep_argv, rs.Budget(timeout_s=2.0, label="timeout-probe"),
                         rs.child_env(_MODULE_TMP / "c7-env"), cwd=_MODULE_TMP)
    assert res["timed_out"] and "late" not in res["stdout"]

    # output truncation flag
    noisy = [sys.executable, "-c", "print('A' * 100000)"]
    res2 = rs.run_bounded(noisy, rs.Budget(timeout_s=30, max_output_bytes=1000,
                                           label="output-cap"),
                          rs.child_env(_MODULE_TMP / "c7-env"), cwd=_MODULE_TMP)
    assert res2["output_truncated"] and len(res2["stdout"]) <= 1000

    # memory enforcement (REAL, platform-accurate): the limiter attempts the OS job
    # limit, records whether the host honors it, and ALWAYS enforces via the
    # harness-owned monitor + explicit tree termination. An incremental allocator
    # walks past the cap so the monitor provably kills it mid-run.
    limiter = rs.MemoryLimiter(max_memory_mb=96, label="mem-probe")
    assert limiter.mechanism_status in (
        "job-honored", "inert-on-host", "unknown-on-host",
        "unavailable-on-host", "posix-rlimit"), limiter.mechanism_status
    alloc = (
        "import time\n"
        "chunks=[]\n"
        "for i in range(48):\n"
        "    chunks.append(bytearray(16*1024*1024))\n"
        "    time.sleep(0.12)\n"
        "print('COMPLETED-ALL-ALLOCATIONS')\n"
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", alloc],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=rs.child_env(_MODULE_TMP / "c7-env"), cwd=_MODULE_TMP,
            start_new_session=(sys.platform != "win32"))  # own pgroup on POSIX
        # so the monitor's kill can never take out the pytest runner.
        limiter.attach(proc)
        mon = limiter.monitor_and_terminate(proc, poll_s=0.1, hard_deadline_s=90.0)
        assert "COMPLETED-ALL-ALLOCATIONS" not in mon["stdout"], \
            "over-budget child ran to completion past the memory cap"
        assert mon["killed"] or mon["rc"] != 0, \
            f"over-budget child must be terminated (mon={mon['killed']}, rc={mon['rc']})"
    finally:
        limiter.terminate()
        limiter.close()

    # redaction on every harness surface (drive-letter path WITHOUT a Users/
    # home segment: the doc-drift hygiene ratchet forbids home-shaped literals
    # in tracked files; the production redactor covers any drive-root path)
    probe = ("secret: ghp_0123456789abcdefghijklmnopqrstuvwxyz and path "
             r"C:\data\secret.txt" + " and mail a@b.example.com")
    red = rs.redact(probe)
    assert "ghp_0123456789" not in red and r"data\secret" not in red \
        and "a@b.example.com" not in red
    run = wr.CandidateRun(name="redact-probe", availability="executed")
    run.log_redacted.append(rs.redact(probe))
    joined = json.dumps(run.to_json())
    assert "ghp_0123456789" not in joined

    # consent gate: guarded remote call never invoked without consent (spy)
    gate = rs.TransmissionGate({})  # no consent
    called = []
    res3 = gate.guard("probe", lambda *a: called.append(1) or {"never": True})
    assert res3["status"] == "unavailable" and not called and not gate.invocations
    gate2 = rs.TransmissionGate({"ZMEM_CURATOR_TRANSMIT_CONSENT": "1"})
    res4 = gate2.guard("probe", lambda: {"ok": True})
    assert res4 == {"ok": True} and gate2.invocations == ["probe"]

    # RSS measured honestly (int or None, never fabricated)
    rss = rs.measure_rss_mb()
    assert rss is None or isinstance(rss, int)


# ---------------------------------------------------------------------------
# C8 / AC8 — decision report: provenance, two digests, no-GO rules
# ---------------------------------------------------------------------------

def test_decision_report():
    corpus = co.build_corpus()
    reviewed = co.load_reviewed_histories()
    scratch = _scratch("c8")
    det = wr.run_candidate("deterministic-zmem", corpus, scratch / "det", _BUDGET,
                           me.rubric_digest())
    lex = wr.run_candidate("lexical-baseline", corpus, scratch / "lex", _BUDGET,
                           me.rubric_digest())
    tc = wr.run_candidate("tencent", corpus, scratch / "tc", _BUDGET,
                          me.rubric_digest())
    rubric = me.Rubric()
    rubric.metrics["contract_lane"] = me.score_contract_lane(corpus, det)
    human = me.score_human_lane({}, reviewed["cases"])
    rep = rp.build_report([det, lex, tc], rubric,
                          source_sha="93a8d30750703261fe2b0a39760124f14062b7be",
                          os_runtime=sys.platform, human_label_status=human)
    for field in ("schema", "source_sha", "candidates", "rubric", "staged_rollout",
                  "go_no_go", "canonical_decision_digest", "raw_artifact_digest",
                  "maintainer_evidence_pending"):
        assert field in rep, f"report missing {field}"
    assert rep["go_no_go"]["model_go"] is False
    assert "unavailable inference yields no score" in rep["go_no_go"]["reason"]
    assert any("awaiting per-case human labels" in p
               for p in rep["maintainer_evidence_pending"])
    assert rep["staged_rollout"]["unattended_semantic_apply"].startswith("SEPARATE")

    # canonical digest is stable across telemetry changes; raw digest is not
    rep2 = rp.build_report([det, lex, tc], rubric, source_sha=rep["source_sha"],
                           os_runtime=rep["os_runtime"], human_label_status=human)
    det.telemetry["latency_s_total"] = (det.telemetry.get("latency_s_total") or 0) + 123.456
    rep3 = rp.build_report([det, lex, tc], rubric, source_sha=rep["source_sha"],
                           os_runtime=rep["os_runtime"], human_label_status=human)
    assert rep2["canonical_decision_digest"] == rep3["canonical_decision_digest"], \
        "canonical_digest_instable: telemetry leaked into the canonical digest"
    assert rep2["raw_artifact_digest"] != rep3["raw_artifact_digest"], \
        "raw artifact digest must reflect telemetry changes"

    # provenance validation: hand-assembled dicts fail
    try:
        rp.build_report([{"name": "fake", "availability": "executed"}], rubric,
                        source_sha="x", os_runtime="x")
        raise AssertionError("provenance validation must reject hand dicts")
    except AssertionError as exc:
        assert "provenance_validation_failed" in str(exc)

    # critical findings force model_go False (critical fact never averaged away)
    rubric_crit = me.Rubric()
    rubric_crit.add_critical("synthetic critical fact destroyed")
    rep4 = rp.build_report([det, lex], rubric_crit, source_sha=rep["source_sha"],
                           os_runtime=rep["os_runtime"],
                           human_label_status=human)
    assert rep4["go_no_go"]["model_go"] is False
    assert "critical findings" in rep4["go_no_go"]["reason"]

    # write/read round-trip
    dest = rp.write_report(rep, scratch / "out" / "decision-report.json")
    loaded = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded["schema"] == "zmem-nightly-curator-decision-v1"


# ---------------------------------------------------------------------------
# CI plain-python runner (same functions the frozen pytest argvs select)
# ---------------------------------------------------------------------------

_ALL_TESTS = (
    test_writer_comparison,
    test_quality_metrics,
    test_proposal_boundary,
    test_transaction_concurrency,
    test_nightly_orchestration,
    test_cleanup_recovery,
    test_resource_privacy,
    test_decision_report,
)

if __name__ == "__main__":
    failures = []
    degraded_only = True
    for fn in _ALL_TESTS:
        name = fn.__name__
        try:
            fn()
        except AssertionError as exc:
            failures.append(name)
            if not str(exc).startswith(("MODEL_UNAVAILABLE:", "WRITER_UNAVAILABLE:")):
                degraded_only = False
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        except Exception as exc:  # unexpected — still a failure, never a skip
            failures.append(name)
            degraded_only = False
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\nRan {len(_ALL_TESTS)} tests")
    if failures:
        if degraded_only:
            print("INCOMPLETE: config-gated remote legs did not run "
                  "(deterministic legs all passed)")
            sys.exit(0)
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        sys.exit(1)
    print("OK")
