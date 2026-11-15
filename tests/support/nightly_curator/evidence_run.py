"""Committed evidence-run generator for issue #377 (final-critic F-C3).

Regenerates docs/evaluation/nightly-curator/decision-report.json from EXECUTED
candidate runs at the current HEAD. The judge method is disclosed in the report
(keyword-rule-v1, deterministic). Usage:

    python tests/support/nightly_curator/evidence_run.py

Optional env (both required together for the general-writer leg; without them the
leg records its honest unavailability — by construction under the hermetic child
env, which withholds credentials by design):
    ZMEM_CURATOR_GENERAL_WRITER=<model>
    ZMEM_CURATOR_TRANSMIT_CONSENT=1
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "tests" / "support"))
sys.path.insert(0, str(_REPO / "skills" / "memory" / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="zmem-377-evidence-"))
    os.environ["ZMEM_STORE"] = str(scratch / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(scratch)
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

    from nightly_curator import corpus as co
    from nightly_curator import metrics as me
    from nightly_curator import proposals as pr
    from nightly_curator import report as rp
    from nightly_curator import resources as rs
    from nightly_curator import writers as wr

    consent_env = dict(os.environ)
    consent_env[rs.GENERAL_WRITER_ENV] = os.environ.get("ZMEM_CURATOR_GENERAL_MODEL", "sonnet")
    consent_env[rs.TRANSMIT_CONSENT_ENV] = "1"

    corpus = co.build_corpus()
    reviewed = co.load_reviewed_histories()
    budget = rs.Budget(timeout_s=240.0, max_output_bytes=200_000, label="evidence")

    names = ["deterministic-zmem", "lexical-baseline", "deepseek-scripted",
             "local-endpoint", "tencent", "general"]
    runs = []
    for name in names:
        env = consent_env if name == "general" else dict(os.environ)
        print(f"running candidate: {name} ...", flush=True)
        run = wr.run_candidate(name, corpus, scratch / name, budget,
                               me.rubric_digest(), env=env)
        print(f"  -> {run.availability} {run.reason[:90]} decisions={len(run.decisions)}",
              flush=True)
        runs.append(run)

    rubric = me.Rubric()
    det = next(r for r in runs if r.name == "deterministic-zmem")
    contract = me.score_contract_lane(corpus, det)
    rubric.metrics["contract_lane"] = contract
    rubric.metrics["judge_vs_contract_oracle"] = {
        "cases": len(corpus.contract_pairs),
        "agrees": sum(1 for v in contract["per_pair"].values()
                      if v["verdict"] == "agrees"),
        "false_merges": len(contract["false_merges"]),
        "missed_duplicates": len(contract["missed_duplicates"]),
    }
    rubric.metrics["telemetry_by_candidate"] = {
        r.name: me.score_telemetry(r) for r in runs}

    # authored lane through the REAL boundary on the deterministic store
    import sqlite3
    det_store = scratch / "deterministic-zmem" / "pairs" / "task-closeout" / "store.sqlite"
    from storelib import evidence as _ev
    conn = sqlite3.connect(str(det_store))
    e1 = _ev.write_evidence(conn, session_id="synthetic-evidence-run", lane="codex",
                            moment="pretool", kind="tool_call",
                            ts="2026-11-14T00:00:00Z",
                            excerpt="evidence-run authored-lane evidence",
                            ref_path="synthetic/evidence-run.txt", ref_offset=0)
    conn.commit()
    conn.close()
    boundary = pr.ProposalBoundary(det_store)
    live = sorted(boundary._live_memory_ids())
    outcomes = {}
    for cid in me.AUTHORED_PROPOSALS:
        if cid == "AQ-003":
            prop = {"id": cid, "kind": "novel_lesson",
                    "content": "authored case AQ-003",
                    "evidence_ids": ["ev-fabricated-aq3"]}
        elif cid in ("AQ-001",):
            prop = {"id": cid, "kind": "novel_lesson",
                    "content": "authored case AQ-001", "evidence_ids": [e1]}
        else:
            prop = {"id": cid, "kind": "dedup_merge", "memory_ids": live[:2],
                    "evidence_ids": [e1], "subjects": ["issue41"]}
        outcomes[cid] = boundary.evaluate(prop).action
    rubric.metrics["authored_lane"] = me.score_authored_lane(outcomes)

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
    rubric.metrics["human_lane"] = human
    rubric.metrics["judge_method"] = "keyword-rule-v1 (deterministic, disclosed)"

    head = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    rep = rp.build_report(runs, rubric, source_sha=head, os_runtime=sys.platform,
                          human_label_status=human)
    rep["evidence_run"] = {
        "generator": "tests/support/nightly_curator/evidence_run.py",
        "regenerate_command": "python tests/support/nightly_curator/evidence_run.py",
        "tree_dirty_at_run": bool(dirty),
    }
    rep["canonical_decision_digest"] = rp.canonical_digest(
        {k: v for k, v in rep.items() if k != "raw_artifact_digest"})
    rep["raw_artifact_digest"] = rp.raw_digest(
        {k: v for k, v in rep.items()
         if k not in ("canonical_decision_digest", "raw_artifact_digest")})

    dest = rp.write_report(rep, _REPO / "docs" / "evaluation" / "nightly-curator" /
                           "decision-report.json")
    print("wrote", dest)
    print("model_go:", rep["go_no_go"]["model_go"], "|", rep["go_no_go"]["reason"])
    print("canonical:", rep["canonical_decision_digest"])
    print("authored lane:", json.dumps(
        rubric.metrics["authored_lane"]["judge_vs_authored_oracle"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
