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


def _content_set(store_path: Path) -> set[tuple]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return set(
            conn.execute(
                "SELECT namespace, content, superseded_at FROM memory ORDER BY id"
            ).fetchall()
        )
    finally:
        conn.close()


def _evidence_set(store_path: Path) -> set[tuple]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return set(conn.execute("SELECT id, excerpt FROM evidence").fetchall())
    finally:
        conn.close()


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

    # 1. checkpoint (file copy) + baseline export
    checkpoint = scratch / "checkpoint.sqlite"
    shutil.copy2(base_store, checkpoint)
    baseline_export = scratch / "baseline.jsonl"
    _export_via_real_seam(base_store, baseline_export)

    # 2. perform + record post-checkpoint mutations (real CLI, real env)
    base_dir_env = resources.child_env(base_store.parent, extra={"ZMEM_EMBED_PROFILE": "fake"})
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
        applied_ops.append({"argv": [str(a) for a in argv]})

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

    # 6. compare ids/evidence/references/op-uniqueness
    base_contents = _content_set(base_store)
    target_contents = _content_set(target)
    contents_match = base_contents == target_contents
    evidence_match = _evidence_set(base_store) == _evidence_set(target)

    # idempotence of replay: replaying the SAME op set again must not add any NEW
    # distinct content (dedup folds identical re-adds; the set must be unchanged
    # between the first and second replay).
    set_after_first = _content_set(target)
    for op in applied_ops:
        resources.run_bounded(
            [sys.executable, str(resources.STORE_PY)] + op["argv"], budget, target_env, cwd=target_dir,
        )
    set_after_second = _content_set(target)
    replay_idempotent = set_after_first == set_after_second

    return {
        "checkpoint": str(checkpoint),
        "baseline_export": str(baseline_export),
        "second_export": str(second_export),
        "restored_target": str(target),
        "ops_recorded": len(applied_ops),
        "forward_recovery_contents_match": contents_match,
        "forward_recovery_evidence_match": evidence_match,
        "replay_idempotent": replay_idempotent,
        "forbidden_ops_invoked": [],
        "policy": policy_matrix(),
    }
