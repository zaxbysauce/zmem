"""Cleanup eligibility + checkpoint/export/rollback/forward recovery (#377 AC6).

Policy matrix (declared; enforced here):
- AUTOMATIC (deterministic only): index/enrichment repair; TTL sweep restricted to
  eligible = expired AND unreferenced AND unprotected. The harness NEVER invokes the
  unfiltered whole-store evidence sweep (evidence.py:275-309 deletes unconditionally).
- PROPOSED (review-only): supersession/merge/novel lesson/review-needed; and every
  expired-but-referenced evidence row (lesson/head/page references) — preserved
  pending review.

Recovery chronology (critic R3-3, full sequence):
  checkpoint (file copy) + baseline export (REAL sync.cmd_export_jsonl seam)
  -> perform and record post-checkpoint mutations (real CLI)
  -> SECOND consistent export containing those changes
  -> restore the baseline into a separate scratch target
  -> replay the recorded post-checkpoint operations onto the restored target (real CLI)
  -> compare ids/evidence/references/operation-uniqueness across the sequence.

Forbidden-by-construction: hard purge, DB replacement, VACUUM, deletion of
unacknowledged ops — no automatic path in this module performs any of them.
"""

from __future__ import annotations

import json
import sys
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import resources


def policy_matrix() -> dict[str, Any]:
    return {
        "automatic": [
            "deterministic index/enrichment repair",
            "TTL sweep over eligible rows only (expired AND unreferenced AND unprotected)",
        ],
        "proposed_review_only": [
            "supersession candidates",
            "merge candidates",
            "novel lessons",
            "review-needed items",
            "expired-but-referenced evidence (lesson/head/page refs)",
        ],
        "forbidden_implicit": [
            "hard purge", "database replacement", "VACUUM",
            "deletion of unacknowledged operations",
        ],
        "protections": {
            "low_use_alone_cannot_delete": True,
            "high_signal_floor": 0.8,
            "protected_signals": ["reviewer", "user"],
            "superseded_history_retained": True,
            "referenced_evidence_review_only": True,
        },
    }


def classify_rows(rows: list[dict]) -> dict[str, Any]:
    """Split rows into ttl_eligible_auto / expired_referenced_proposed / protected."""
    auto: list[str] = []
    proposed: list[str] = []
    protected: list[str] = []
    for r in rows:
        rid = r["id"]
        referenced = bool(r.get("referenced_by"))
        expired = bool(r.get("expired"))
        prot = (r.get("signal") in policy_matrix()["protections"]["protected_signals"]
                or r.get("confidence", 0) >= policy_matrix()["protections"]["high_signal_floor"]
                or r.get("contested"))
        if prot:
            protected.append(rid)
        elif expired and referenced:
            proposed.append(rid)
        elif expired and not referenced:
            auto.append(rid)
    return {"ttl_eligible_auto": auto, "expired_referenced_proposed": proposed,
            "protected": protected}


def _export_via_real_seam(store_path: Path, out_path: Path) -> int:
    """One consistent export through the REAL production seam (sync.py:589)."""
    import sys
    if str(resources.SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(resources.SCRIPTS_DIR))
    from storelib import sync as _sync  # noqa: E402
    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row  # the seam reads rows as mappings
    try:
        return _sync.cmd_export_jsonl(conn, out=str(out_path))
    finally:
        conn.close()


def _content_rows(store_path: Path) -> list[tuple]:
    """Full row identity (id, namespace, content, superseded_at), ordered by id
    (owner-review F-2: identity drift must be visible)."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return list(
            conn.execute(
                "SELECT id, namespace, content, superseded_at FROM memory ORDER BY id"
            ).fetchall()
        )
    finally:
        conn.close()


def compare_recovery_contents(
    base_rows: list[tuple], target_rows: list[tuple], checkpoint_ids: set[str],
) -> dict[str, Any]:
    """Identity-preserving comparison (closeout round): rows that existed at
    checkpoint time keep their ids across restore, so they are compared by
    FULL identity; post-checkpoint rows legitimately regenerate ids on CLI
    replay, so those are compared as a (namespace, content, superseded_at)
    multiset — duplication and drops remain visible in both lanes."""
    base_by_id = {r[0]: r for r in base_rows}
    tgt_by_id = {r[0]: r for r in target_rows}
    # Presence is part of the invariant: a checkpoint id absent from BOTH
    # sides must FAIL the lane (None==None would pass it vacuously).
    missing_in_base = sorted(checkpoint_ids - base_by_id.keys())
    missing_in_target = sorted(checkpoint_ids - tgt_by_id.keys())
    checkpoint_rows_match = (
        not missing_in_base and not missing_in_target and all(
            tgt_by_id[i] == base_by_id[i] for i in checkpoint_ids))
    base_post = sorted(
        r[1:] for r in base_rows if r[0] not in checkpoint_ids)
    tgt_post = sorted(
        r[1:] for r in target_rows if r[0] not in checkpoint_ids)
    post_rows_match = base_post == tgt_post
    return {
        "checkpoint_rows_match": checkpoint_rows_match,
        "post_checkpoint_rows_match": post_rows_match,
        "checkpoint_row_count": len(checkpoint_ids),
        "post_checkpoint_row_count": len(base_post),
        "missing_checkpoint_ids_in_base": missing_in_base,
        "missing_checkpoint_ids_in_target": missing_in_target,
        "contents_match": checkpoint_rows_match and post_rows_match,
    }


def _evidence_set(store_path: Path) -> list[tuple]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return list(conn.execute("SELECT id, excerpt FROM evidence ORDER BY id").fetchall())
    finally:
        conn.close()


_FORBIDDEN_OP_TOKENS = ("purge", "vacuum", "rekey", "restore", "drop")


def _forbidden_ops_in(argv: list[str]) -> list[str]:
    """Derive forbidden-operation usage from the RECORDED argv (owner-review
    F-2: the drill reports what its recorded commands actually invoke, never a
    hardcoded constant)."""
    joined = " ".join(str(a).lower() for a in argv)
    return [tok for tok in _FORBIDDEN_OP_TOKENS if f" {tok}" in f" {joined}"]


def recovery_drill(
    base_store: Path,
    scratch: Path,
    post_mutation_cmds: list[list[str]],
    budget: resources.Budget,
    namespace: str = "project:synthetic/curator",
) -> dict[str, Any]:
    """The full R3-3 chronology on isolated stores. post_mutation_cmds: real store.py
    argv lists (without the interpreter/store path) applied AFTER the checkpoint."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    # 1. checkpoint (file copy) + baseline export; record the checkpoint-time
    #    row ids — those rows must keep their identity across restore/replay
    #    (closeout round: identity-preserving comparison).
    checkpoint = scratch / "checkpoint.sqlite"
    shutil.copy2(base_store, checkpoint)
    checkpoint_ids = {r[0] for r in _content_rows(base_store)}
    baseline_export = scratch / "baseline.jsonl"
    _export_via_real_seam(base_store, baseline_export)

    # 2. perform + record post-checkpoint mutations (real CLI, real env)
    applied_ops: list[dict] = []
    for argv in post_mutation_cmds:
        res = resources.run_bounded(
            [sys.executable, str(resources.STORE_PY)] + [str(a) for a in argv], budget,
            resources.child_env(base_store.parent, extra={"ZMEM_EMBED_PROFILE": "fake"}), cwd=base_store.parent,
        )
        if res["returncode"] != 0:
            raise AssertionError(
                f"post-checkpoint mutation failed: {resources.redact(res['stderr'][:200])}"
            )
        applied_ops.append({"argv": [str(a) for a in argv],
                            "forbidden": _forbidden_ops_in(argv)})

    # 3. second consistent export (contains the mutations)
    second_export = scratch / "after.jsonl"
    _export_via_real_seam(base_store, second_export)

    # 4. restore baseline into a separate scratch target
    target = scratch / "recovered.sqlite"
    shutil.copy2(checkpoint, target)
    target_dir = scratch / "target-home"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_env = resources.child_env(target_dir, extra={"ZMEM_EMBED_PROFILE": "fake"})
    # point the child env's store at the recovered file
    target_env["ZMEM_STORE"] = str(target)

    # 5. replay the recorded post-checkpoint ops onto the restored target
    for op in applied_ops:
        res = resources.run_bounded(
            [sys.executable, str(resources.STORE_PY)] + op["argv"], budget, target_env, cwd=target_dir,
        )
        if res["returncode"] != 0:
            raise AssertionError(
                f"replay op failed: {resources.redact(res['stderr'][:200])}"
            )

    # 6. compare ids/evidence/references/op-uniqueness: checkpoint rows by
    #    FULL identity (ids survive restore); post-checkpoint rows as a
    #    content multiset (replayed CLI adds mint fresh uuids)
    base_rows = _content_rows(base_store)
    target_rows = _content_rows(target)
    comparison = compare_recovery_contents(
        base_rows, target_rows, checkpoint_ids)
    contents_match = comparison["contents_match"]
    evidence_match = _evidence_set(base_store) == _evidence_set(target)

    # idempotence of replay: the SECOND replay must also SUCCEED (owner-review
    # F-2: a failed replay silently changes nothing and would otherwise count
    # as idempotent) and must not change the row identity list.
    rows_after_first = _content_rows(target)
    for op in applied_ops:
        res = resources.run_bounded(
            [sys.executable, str(resources.STORE_PY)] + op["argv"], budget, target_env, cwd=target_dir,
        )
        if res["returncode"] != 0:
            raise AssertionError(
                f"idempotence replay op failed: {resources.redact(res['stderr'][:200])}"
            )
    rows_after_second = _content_rows(target)
    replay_idempotent = rows_after_first == rows_after_second

    forbidden_invoked = sorted({tok for op in applied_ops for tok in op["forbidden"]})

    return {
        "checkpoint": str(checkpoint),
        "baseline_export": str(baseline_export),
        "second_export": str(second_export),
        "restored_target": str(target),
        "ops_recorded": len(applied_ops),
        "checkpoint_rows_match": comparison["checkpoint_rows_match"],
        "post_checkpoint_rows_match": comparison["post_checkpoint_rows_match"],
        "checkpoint_row_count": comparison["checkpoint_row_count"],
        "post_checkpoint_row_count": comparison["post_checkpoint_row_count"],
        "forward_recovery_contents_match": contents_match,
        "forward_recovery_evidence_match": evidence_match,
        "replay_idempotent": replay_idempotent,
        "forbidden_ops_invoked": forbidden_invoked,
        "policy": policy_matrix(),
    }
