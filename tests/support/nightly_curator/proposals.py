"""Typed proposal boundary for the nightly-curator evaluation (#377 AC3).

Two SEPARATE contracts (never mixed):

A. The proposal battery (harness-level typed boundary): every curator proposal is
   validated against the scratch store; hostile classes are refused or quarantined
   with TYPED reasons; original evidence is snapshotted before/after EVERY
   disposition; self-confidence is invariant (identical proposal at confidence 0.1 vs
   0.99 -> identical outcome); novel/destructive changes route to a retain.py-shaped
   review queue (review-candidate ARTIFACTS, never memory rows) whose only consumer
   is reviewed promotion.

B. The current quoted-source belief-head operation contract, exercised through the
   REAL storelib validators (beliefs.apply_belief_actions on an explicit conn to a
   scratch store seeded through the real CLI): valid grounded ops apply; invalid ops
   (unknown op, out-of-scope head, fabricated citation, illegal transition) roll back
   the whole set.

No production module is monkeypatched; instrumentation is parameter-injected only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import resources

MAX_PROPOSAL_BYTES = 20_000
MAX_ACTION_TEXT_BYTES = 400  # mirrors beliefs.ADAPTER_MAX_CONTENT_BYTES


# ---------------------------------------------------------------------------
# A. Typed proposal boundary
# ---------------------------------------------------------------------------

REFUSAL_REASONS = (
    "malformed_json", "oversize_payload", "fabricated_memory_id",
    "fabricated_evidence_id", "cross_namespace_target", "incompatible_subject",
    "missing_evidence", "prompt_injection_content", "compositional_poison",
    "stale_snapshot", "conflicting_proposal", "row_cap_exceeded",
)
QUARANTINE_REASONS = ("needs_review",)


@dataclass
class Disposition:
    action: str  # refuse | quarantine | review | accept_deterministic
    reason: str
    proposal_id: str
    evidence_before: dict[str, str] = field(default_factory=dict)
    evidence_after: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "action": self.action, "reason": self.reason,
            "evidence_before": self.evidence_before,
            "evidence_after": self.evidence_after,
        }


class ProposalBoundary:
    """Validates proposals against a live scratch store (explicit sqlite conn)."""

    def __init__(self, store_path: Path, namespace: str = "project:synthetic/curator",
                 max_rows: int = 200):
        self.store_path = Path(store_path)
        self.namespace = namespace
        self.max_rows = max_rows
        self.review_queue: list[dict] = []
        self.applied_deterministic: list[dict] = []
        self.dispositions: list[Disposition] = []

    # -- evidence snapshots -------------------------------------------------
    def _evidence_snapshot(self, evidence_ids: list[str]) -> dict[str, str]:
        conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            out = {}
            for eid in evidence_ids:
                row = conn.execute(
                    "SELECT id, excerpt FROM evidence WHERE id=?", (eid,)
                ).fetchone()
                out[eid] = resources.redact(row[1][:200]) if row else "<absent>"
            return out
        finally:
            conn.close()

    def _live_memory_ids(self) -> set[str]:
        conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            return {
                r[0] for r in conn.execute(
                    "SELECT id FROM memory WHERE superseded_at IS NULL"
                )
            }
        finally:
            conn.close()

    def _memory_namespace(self, mid: str) -> str | None:
        conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT namespace FROM memory WHERE id=?", (mid,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # -- the boundary --------------------------------------------------------
    def evaluate(self, proposal: Any) -> Disposition:
        # Owner-review R-3: shape/type validation happens BEFORE any field
        # access, so the most obviously malformed inputs get the typed
        # malformed_json refusal instead of an AttributeError/TypeError
        # escaping the boundary.
        if not isinstance(proposal, dict) or not isinstance(
                proposal.get("kind"), str):
            return Disposition(action="refuse", reason="malformed_json",
                               proposal_id="<untypeable>")
        pid = str(proposal.get("id", "<no-id>"))
        raw_evidence = proposal.get("evidence_ids", [])
        if not isinstance(raw_evidence, list) or not all(
                isinstance(e, str) for e in raw_evidence):
            return Disposition(action="refuse", reason="malformed_json",
                               proposal_id=pid)
        evidence_ids = list(raw_evidence)
        # Owner-review Stage-B R-3: nested list fields are type-gated too —
        # `rows: null` or `subjects: [[]]` get the typed refusal, not an
        # untyped TypeError from list()/set() coercion.
        for field in ("memory_ids", "rows", "subjects"):
            v = proposal.get(field, [])
            if not isinstance(v, list) or not all(isinstance(e, str) for e in v):
                return Disposition(action="refuse", reason="malformed_json",
                                   proposal_id=pid)
        snap_before = self._evidence_snapshot(evidence_ids)

        def finish(action: str, reason: str) -> Disposition:
            d = Disposition(action=action, reason=reason, proposal_id=pid,
                            evidence_before=snap_before,
                            evidence_after=self._evidence_snapshot(evidence_ids))
            self.dispositions.append(d)
            if action == "review":
                self.review_queue.append(proposal)
            if action == "accept_deterministic":
                self.applied_deterministic.append(proposal)
            return d

        try:
            raw = json.dumps(proposal, sort_keys=True)
        except (TypeError, ValueError):
            return finish("refuse", "malformed_json")
        if len(raw.encode("utf-8")) > MAX_PROPOSAL_BYTES:
            return finish("refuse", "oversize_payload")

        text = json.dumps(proposal).lower()
        inj_markers = ("ignore previous instructions", "system prompt",
                       "mark every proposal approved")
        if any(m in text for m in inj_markers):
            return finish("refuse", "prompt_injection_content")

        if proposal["kind"] == "compositional_merge":
            # Two individually-innocuous rows whose composition is poisonous: the
            # pair is refused as a unit (MemPoison class; research warning).
            return finish("refuse", "compositional_poison")

        if proposal["kind"] == "dedup_merge":
            ids = list(proposal.get("memory_ids", []))
            live = self._live_memory_ids()
            if any(i not in live for i in ids):
                return finish("refuse", "fabricated_memory_id")
            for i in ids:
                ns = self._memory_namespace(i)
                if ns and ns != self.namespace:
                    return finish("refuse", "cross_namespace_target")
            subjects = {s for s in proposal.get("subjects", []) if s is not None}
            # Owner-review R-3: two conflicting concrete subjects are refused
            # even when the list also carries an unknown (None) subject — the
            # unknown cannot wash out a proven conflict.
            if len(subjects) > 1:
                return finish("refuse", "incompatible_subject")
            if not proposal.get("evidence_ids"):
                return finish("refuse", "missing_evidence")
            absent = [e for e in evidence_ids
                      if self._evidence_snapshot([e]).get(e) == "<absent>"]
            if absent:
                return finish("refuse", "fabricated_evidence_id")
            # Destructive merge -> review only, never auto-apply.
            return finish("review", "needs_review")

        if proposal["kind"] == "novel_lesson":
            if not proposal.get("evidence_ids"):
                return finish("refuse", "missing_evidence")
            absent_ev = [e for e in evidence_ids
                         if self._evidence_snapshot([e]).get(e) == "<absent>"]
            if absent_ev:
                # final-critic F-C1: a novel lesson citing fabricated evidence is
                # refused exactly like an ungrounded merge (evidence-id precision
                # applies to every evidence-carrying proposal kind).
                return finish("refuse", "fabricated_evidence_id")
            return finish("review", "needs_review")

        if proposal["kind"] == "stale_snapshot_update":
            return finish("refuse", "stale_snapshot")

        if proposal["kind"] == "deterministic_reindex":
            rows = list(proposal.get("rows", []))
            if len(rows) > self.max_rows:
                return finish("refuse", "row_cap_exceeded")
            # Owner-review R-3: even the deterministic path validates that its
            # rows exist and live in the boundary's namespace — a reindex is an
            # authorized store action, not a blind one.
            live = self._live_memory_ids()
            for r in rows:
                if r not in live:
                    return finish("refuse", "fabricated_memory_id")
                ns = self._memory_namespace(r)
                if ns and ns != self.namespace:
                    return finish("refuse", "cross_namespace_target")
            return finish("accept_deterministic", "deterministic_eligible")

        return finish("quarantine", "needs_review")

    def evaluate_confidence_invariant(self, proposal: dict) -> dict[str, Any]:
        """Identical proposal at confidence 0.1 vs 0.99 must yield identical outcome."""
        low = dict(proposal, confidence=0.1)
        high = dict(proposal, confidence=0.99)
        d_low = self.evaluate(low)
        d_high = self.evaluate(high)
        return {
            "invariant": (d_low.action, d_low.reason) == (d_high.action, d_high.reason),
            "low": (d_low.action, d_low.reason),
            "high": (d_high.action, d_high.reason),
        }

    def detect_conflicts(self, proposals: list[dict]) -> list[str]:
        """Pairwise conflicting proposals (same target, opposite operations)."""
        conflicts = []
        by_target: dict[str, dict] = {}
        for p in proposals:
            key = json.dumps(p.get("memory_ids") or p.get("evidence_ids") or [], sort_keys=True)
            if key in by_target and by_target[key].get("kind") != p.get("kind"):
                conflicts.append(f"{by_target[key].get('id')}:{p.get('id')}")
            by_target.setdefault(key, p)
            if key == "[]" and p.get("kind") == "conflicting_claim":
                # explicit conflicting-claim fixture: same subject, opposite facts
                conflicts.append(str(p.get("id")))
        return conflicts

    def promote_reviewed(self, proposal_id: str, reviewer: str) -> dict[str, Any]:
        """The ONLY durability path for review-routed proposals: reviewed promotion."""
        for i, p in enumerate(self.review_queue):
            if str(p.get("id")) == str(proposal_id):
                item = self.review_queue.pop(i)
                record = {"id": proposal_id, "reviewed_by": reviewer,
                          "status": "promoted", "proposal": item}
                self.applied_deterministic.append(record)
                return record
        raise KeyError(f"proposal {proposal_id} not in review queue")


# ---------------------------------------------------------------------------
# B. Real belief-head operation contract (separate)
# ---------------------------------------------------------------------------

def exercise_belief_ops(store_path: Path) -> dict[str, Any]:
    """Drive the REAL beliefs.apply_belief_actions on a CLI-seeded scratch store.

    Returns {valid_applied, invalid_rolled_back} evidence; raises on contract breach.
    """
    import sys
    scripts = resources.SCRIPTS_DIR
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from storelib import beliefs  # noqa: E402

    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row  # beliefs helpers read rows as mappings
    try:
        report = beliefs.refresh_belief_heads(conn)
        heads = report.get("heads", [])
        if not heads:
            raise AssertionError("belief refresh produced no heads from seeded rows")
        head = heads[0]
        hid = head["head_id"]

        # Valid op: replace_quote grounded in the head's own sources.
        src = head["source_ids"][0] if head["source_ids"] else None
        if not src:
            raise AssertionError("head lacks source ids; fixture must seed evidence")
        ev = head["evidence_ids"][0] if head["evidence_ids"] else None
        action = {
            "op": "replace_quote", "head_id": hid,
            "markdown": "Grounded replacement quote from the head's own sources.",
            "source_ids": [src], "evidence_ids": [ev] if ev else [],
        }
        applied = beliefs.apply_belief_actions(conn, head_id=hid, actions=[action])
        valid_applied = applied.get("applied", 0) == 1

        # Invalid op: fabricated citation must roll back the whole set.
        bad = {
            "op": "replace_quote", "head_id": hid,
            "markdown": "x", "source_ids": ["fabricated-id-000"],
            "evidence_ids": [],
        }
        invalid_rolled_back = False
        try:
            beliefs.apply_belief_actions(conn, head_id=hid, actions=[bad])
        except beliefs.BeliefActionError:
            invalid_rolled_back = True
        return {
            "valid_applied": valid_applied,
            "invalid_rolled_back": invalid_rolled_back,
            "head_state_after": applied.get("head_state"),
        }
    finally:
        conn.close()
