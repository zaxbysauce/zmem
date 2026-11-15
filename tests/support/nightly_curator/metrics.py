"""Frozen scoring rubric for the nightly-curator evaluation (#377 AC2).

The rubric is digest-stamped BEFORE any model-configured run (asserted by C2). Four
DISTINCT disagreement surfaces, never conflated (plan Rev 6; critic Rounds 3-6):
- judge_vs_contract_oracle  (six dedup pairs + lineage/retraction)
- judge_vs_authored_oracle  (richer quality cases; authored expectations)
- judge_vs_model_reviewed   (held-out PR-review verdict lane, model-attributed)
- judge_vs_human            (computes ONLY over human_endorsement rows; otherwise
                             awaiting_human_labels with the endorsed-case count —
                             the AC2 measurement is declared PARTIAL, not satisfied)

critical_findings can never be absorbed by an average: any entry fails the report.
Variant declarations: lexical executed; quantized not_claimed (nothing claims one).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

RUBRIC_VERSION = "nightly-curator-rubric-v1"

METRIC_FAMILIES = (
    "false_merges", "missed_duplicates", "unsupported_statements",
    "evidence_id_precision", "temporal_updates", "abstention_quality",
    "useful_accepted_lessons", "storage_growth", "latency_s", "token_cost",
    "disagreement.judge_vs_contract_oracle",
    "disagreement.judge_vs_authored_oracle",
    "disagreement.judge_vs_model_reviewed",
    "disagreement.judge_vs_human",
    "critical_findings",
)

VARIANT_DECLARATIONS = {
    "lexical-only/no-model": "executed",
    "quantized": "not_claimed",
}


def rubric_digest() -> str:
    payload = RUBRIC_VERSION + "|" + "|".join(METRIC_FAMILIES)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Rubric:
    metrics: dict[str, Any] = field(default_factory=dict)
    critical_findings: list[str] = field(default_factory=list)
    awaiting_human_labels: bool = False
    endorsed_case_count: int = 0

    def add_critical(self, finding: str) -> None:
        """A destroyed/merged-away critical fact fails the report outright."""
        self.critical_findings.append(finding)

    @property
    def failed(self) -> bool:
        return bool(self.critical_findings)

    def to_json(self) -> dict:
        return {
            "rubric_version": RUBRIC_VERSION,
            "rubric_digest": rubric_digest(),
            "metric_families": list(METRIC_FAMILIES),
            "metrics": self.metrics,
            "critical_findings": self.critical_findings,
            "awaiting_human_labels": self.awaiting_human_labels,
            "endorsed_case_count": self.endorsed_case_count,
            "variant_declarations": dict(VARIANT_DECLARATIONS),
        }


def judge_pair(expected: str, decision: str) -> str:
    """Judge one dedup pair: agrees | false_merge | missed_duplicate."""
    if decision == expected:
        return "agrees"
    if expected == "two_rows" and decision == "one_row":
        return "false_merge"
    if expected == "one_row" and decision == "two_rows":
        return "missed_duplicate"
    return "indeterminate"


def judge_reviewed_case(case: dict, judged_label: str) -> str:
    """Judge a model-reviewed lane case against the verdict of record.

    judged_label vocabulary mirrors the fixture label_vocabulary; disagreement is
    per-case (never averaged).
    """
    return "agrees" if judged_label == case["verdict"] else "disagrees"


def score_contract_lane(corpus, run) -> dict[str, Any]:
    out = {
        "false_merges": [],
        "missed_duplicates": [],
        "per_pair": {},
    }
    for label, *_ in corpus.contract_pairs:
        expected = corpus.contract_expected[label]
        decision = run.decisions.get(label, "no_decision")
        verdict = judge_pair(expected, decision)
        out["per_pair"][label] = {"expected": expected, "decision": decision,
                                  "verdict": verdict}
        if verdict == "false_merge":
            out["false_merges"].append(label)
        elif verdict == "missed_duplicate":
            out["missed_duplicates"].append(label)
    return out


AUTHORED_PROPOSALS = {
    # Final-critic F-C1: authored cases are scored through REAL ProposalBoundary
    # dispositions (tests/support/nightly_curator/proposals.py), never by feeding
    # the expectations back. Each authored case maps to the proposal a curator
    # writer would emit; the boundary's action is the measured outcome.
    "AQ-001": {"kind": "dedup_merge", "reason_hint": "update_not_duplicate"},
    "AQ-002": {"kind": "dedup_merge", "reason_hint": "abstain_to_review"},
    "AQ-003": {"kind": "novel_lesson", "reason_hint": "reject_ungrounded_evidence_id"},
    "AQ-004": {"kind": "dedup_merge", "reason_hint": "collate_ok"},
}

# authored-case expectation -> the boundary action that satisfies it (the
# mapping is part of the frozen rubric; a mutant that swaps the mapping or the
# boundary labels fails C2 by name).
AUTHORED_EXPECTED_ACTIONS = {
    "AQ-001": "review",   # temporal update: never auto-merged; routed to review
    "AQ-002": "review",   # ambiguity abstains to review
    "AQ-003": "refuse",   # fabricated evidence id is refused outright
    "AQ-004": "review",   # a genuine shared lesson is a review-routed collation
}


def score_authored_lane(boundary_outcomes: dict[str, str]) -> dict[str, Any]:
    """boundary_outcomes: authored-case id -> the REAL ProposalBoundary action
    ('refuse' | 'quarantine' | 'review' | 'accept_deterministic')."""
    per_case = {}
    agrees = []
    disagrees = []
    unsupported = []
    for cid, action in boundary_outcomes.items():
        expected = AUTHORED_EXPECTED_ACTIONS.get(cid)
        ok = expected is not None and action == expected
        per_case[cid] = {"action": action, "expected": expected, "verdict":
                         "agrees" if ok else "disagrees"}
        (agrees if ok else disagrees).append(cid)
        if cid == "AQ-003" and action == "refuse":
            unsupported.append(cid)  # ungrounded statement refused = precision hit
    return {
        "per_case": per_case,
        "agrees": agrees,
        "disagrees": disagrees,
        "unsupported_statements_refused": unsupported,
        "judge_vs_authored_oracle": {
            "cases": len(boundary_outcomes),
            "agrees": len(agrees),
            "disagrees": len(disagrees),
        },
    }


def score_model_reviewed_lane(judgments: dict[str, str], cases: list[dict]) -> dict[str, Any]:
    agrees = sum(1 for c in cases if judgments.get(c["id"]) == c["verdict"])
    return {
        "cases": len(cases),
        "agrees": agrees,
        "disagrees": len(cases) - agrees,
        "per_case": {
            c["id"]: judgments.get(c["id"], "no_judgment") for c in cases
        },
        "attribution": "model-lane verdicts of record (never human)",
    }


def score_human_lane(judgments: dict[str, str], cases: list[dict]) -> dict[str, Any]:
    endorsed = [c for c in cases if c.get("human_endorsement")]
    if not endorsed:
        return {
            "status": "awaiting_human_labels",
            "endorsed_case_count": 0,
            "note": "AC2 judge/human measurement PARTIAL: computes only over "
                    "human_endorsement rows; none at ship (owner asked twice "
                    "2026-11-14, no answer; plan-critic interim contract)",
        }
    agrees = sum(
        1 for c in endorsed
        if judgments.get(c["id"]) == c["human_endorsement"]["label"]
    )
    return {
        "status": "measured",
        "endorsed_case_count": len(endorsed),
        "agrees": agrees,
        "disagrees": len(endorsed) - agrees,
    }


def score_telemetry(run) -> dict[str, Any]:
    t = run.telemetry or {}
    return {
        "latency_s": t.get("latency_s_total"),
        "storage_bytes": t.get("storage_bytes_total"),
        "tokens": t.get("tokens", 0),
        "requests": t.get("requests", 0),
        "rss_mb_writer_side": t.get("rss_mb_writer_side"),
    }
