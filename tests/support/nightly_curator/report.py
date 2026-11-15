"""Decision-report builder (#377 AC8): provenance-validated, two digests, staged
rollout, and the rule that unavailable inference can never produce a model GO.

Two digests (critic R2-6):
- canonical_decision_digest: over input pins + deterministic output content + quality
  dispositions + availability states + critical findings + go/no-go rationale.
  Latency/RSS/run-ids/absolute timing are EXCLUDED (they vary across valid runs).
- raw_artifact_digest: the full immutable artifact (telemetry included).

Provenance: a report is only valid when built FROM executed artifacts (each candidate
run carries its own pins/decisions/telemetry); a hand-assembled dict without executed
runs fails provenance validation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

TELEMETRY_KEYS = {"latency_s", "storage_bytes", "tokens", "requests",
                  "rss_mb_writer_side", "latency_s_total", "storage_bytes_total"}
RUN_ID_KEYS = {"run_id", "taken_at"}
# Per-run artifacts that vary across identical-input runs (claude CLI session ids
# inside redacted logs, scratch paths) — excluded from the canonical digest
# (final-critic R2-2) but retained in the raw artifact.
PER_RUN_KEYS = {"log_redacted", "scratch", "duration_s", "tree_dirty_at_run"}


def _canonicalize(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _canonicalize(v) for k, v in sorted(node.items())
                if k not in TELEMETRY_KEYS and k not in RUN_ID_KEYS
                and k not in PER_RUN_KEYS}
    if isinstance(node, list):
        return [_canonicalize(v) for v in node]
    return node


def canonical_digest(report: dict) -> str:
    """Digest over the report MINUS both digest keys (final-critic R2-2): compute
    from {k: v for k, v in report.items() if k not in (digest keys)} so the value
    never feeds itself."""
    stripped = {k: v for k, v in report.items()
                if k not in ("canonical_decision_digest", "raw_artifact_digest")}
    payload = json.dumps(_canonicalize(stripped), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def raw_digest(report: dict) -> str:
    stripped = {k: v for k, v in report.items()
                if k not in ("canonical_decision_digest", "raw_artifact_digest")}
    payload = json.dumps(stripped, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_provenance(runs: list[Any]) -> None:
    """Every candidate entry must be an executed run object (not a hand dict)."""
    for r in runs:
        if not hasattr(r, "to_json"):
            raise AssertionError(
                "provenance_validation_failed: report entries must be executed "
                "CandidateRun objects, never hand-assembled dicts"
            )


MODEL_KINDS = ("general", "tencent", "local_endpoint")


def _availability_allows_model_go(runs: list[Any]) -> tuple[bool, str]:
    """Model GO requires at least one EXECUTED MODEL-BACKED writer (kind general/
    tencent/local-endpoint) with pinned decoding. Deterministic/lexical/fixture
    writers can never produce a model GO; unavailable/ineligible runs cannot either."""
    for r in runs:
        j = r.to_json() if hasattr(r, "to_json") else r
        pins = j.get("pins", {}) or {}
        if pins.get("kind") not in MODEL_KINDS:
            continue
        if j.get("availability") != "executed":
            continue
        decoding = pins.get("decoding") or {}
        if any(v is None for v in decoding.values()):
            return False, "executed model writer has unpinned decoding fields"
        return True, "executed pinned model-backed writer present"
    return False, ("no executed model-backed writer: unavailable inference yields "
                   "no score and cannot produce a model GO")


def build_report(
    runs: list[Any],
    rubric: Any,
    source_sha: str,
    os_runtime: str,
    evidence_dir: Path | None = None,
    human_label_status: dict | None = None,
) -> dict:
    validate_provenance(runs)
    go_ok, go_reason = _availability_allows_model_go(runs)
    rubric_json = rubric.to_json() if hasattr(rubric, "to_json") else rubric
    human = human_label_status or {"status": "awaiting_human_labels"}
    report: dict[str, Any] = {
        "schema": "zmem-nightly-curator-decision-v1",
        "source_sha": source_sha,
        "os_runtime": os_runtime,
        "candidates": [r.to_json() if hasattr(r, "to_json") else r for r in runs],
        "rubric": rubric_json,
        "judge_vs_human_status": human,
        "staged_rollout": {
            "stage_1_shadow_reporting": "ready (this harness; no live schedule)",
            "stage_2_policy_approved_deterministic_maintenance":
                "requires maintainer policy approval",
            "stage_3_model_proposals_through_review":
                "requires reviewed-promotion consumption",
            "unattended_semantic_apply":
                "SEPARATE maintainer decision requiring additional evidence "
                "(never enabled by this evaluation)",
        },
        "go_no_go": {
            "model_go": go_ok,
            "reason": go_reason,
            # Owner-review F-13: the shadow GO is DERIVED from measured
            # contract-lane false merges + critical findings, never a constant.
            "deterministic_shadow_go": None,  # set below
            "note": "unavailable inference yields no score and cannot produce a "
                    "model GO; a negative/no-benefit result can close this "
                    "evaluation with a reproducible explanation",
        },
        "maintainer_evidence_pending": [],
    }
    contract = ((rubric_json.get("metrics") or {}).get("contract_lane") or {})
    # Owner-review Stage-B R-6: absence of a measurement is NOT measured-zero.
    # The shadow GO requires a PRESENT, well-formed false-merge measurement
    # (an explicit list, possibly empty) plus no critical findings.
    shadow_blockers: list[str] = []
    false_merges = contract.get("false_merges")
    if not isinstance(false_merges, list):
        shadow_blockers.append("contract lane not measured (no false_merges "
                               "measurement present)")
    elif false_merges:
        shadow_blockers.append(
            "measured false merges: " + ", ".join(false_merges))
    for c in rubric_json.get("critical_findings", []):
        shadow_blockers.append(f"critical finding: {c}")
    report["go_no_go"]["deterministic_shadow_go"] = not shadow_blockers
    if shadow_blockers:
        report["go_no_go"]["shadow_blockers"] = shadow_blockers
    if human.get("status") == "awaiting_human_labels":
        report["maintainer_evidence_pending"].append(
            "AC2 judge/human disagreement: awaiting per-case human labels "
            "(fixture format carries optional human_endorsement; owner asked twice "
            "2026-11-14, no answer)"
        )
    authored = ((rubric_json.get("metrics") or {}).get("authored_lane") or {})
    if authored.get("not_measured"):
        report["maintainer_evidence_pending"].append(
            "AC2 quality families NOT MEASURED (boundary default lane does not "
            "discriminate them): " + ", ".join(authored.get(
                "not_measured_families", [])) + " - measuring them requires "
            "case-discriminating writer output (see PR discussion)"
        )
    model_lane = ((rubric_json.get("metrics") or {}).get("model_reviewed_lane") or {})
    if model_lane:
        report["maintainer_evidence_pending"].append(
            "judge_vs_model_reviewed label-coverage gap: the keyword-rule judge "
            "emits 3 of the fixture's 5 verdict labels (out_of_scope / "
            "was_valid_fixed unreachable), so part of the measured disagreement "
            "is structural label mismatch, not verdict error "
            f"(agrees={model_lane.get('agrees')}, disagrees={model_lane.get('disagrees')})"
        )
    if rubric_json.get("critical_findings"):
        report["go_no_go"]["model_go"] = False
        report["go_no_go"]["reason"] = (
            f"critical findings present: {rubric_json['critical_findings']}"
        )
    report["canonical_decision_digest"] = canonical_digest(report)
    report["raw_artifact_digest"] = raw_digest(report)
    return report


def write_report(report: dict, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return dest
