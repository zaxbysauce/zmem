"""Deterministic fixture generator for issue #98 (cross-project hazard lane).

Emits the two committed data fixtures for the precision-gated cross-project
tier:

* ``cases.json`` — the twelve-row seeding contract.  IDs
  ``00000000-0000-4000-8000-000000000981``..``...986`` are the six contract
  rows (2 admitted foreign hazard rows, 1 wrong-signal, 1 below-floor, 1
  current-namespace, 1 user:global) and ``...987``..``...992`` are the six
  slot-case rows (4 more current + 2 more global), so the seeded store yields
  exactly the 5 project / 2 cross / 3 global delivered partition.
  ``ingestion_ts`` is the fixed ``2026-09-10T00:00:00Z`` sentinel and the
  hazard commands ``git stash pop`` / ``git reset --hard HEAD`` /
  ``git push --force-with-lease`` are reflected in the row contents.

* ``expected.json`` — the frozen expectations: admitted ids, their source
  namespaces, the exact renderer tier markers, the 5/2/3 cap counts, and the
  exact selector envelope key set (sorted).

Serialization is the issue #98 contract shape: compact separators, sorted
keys, ``ensure_ascii=False``, one trailing LF, written with
``newline="\\n"`` so a fresh generation is byte-identical on any platform.
The generator opens no store and imports no storelib — it is pure data, so
re-running can never drift from the committed bytes.

Usage (from the repo root)::

    python tests/fixtures/cross_project/generate.py            # in-place
    python tests/fixtures/cross_project/generate.py --out-dir <tmp>

Both sha256 digests are printed (cases first, then expected) over the exact
written bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

INGESTION_TS = "2026-09-10T00:00:00Z"
QUERY = "git stash pop"
NAMESPACE = "project:current"
MOMENT = "pretool"
OPS_TOKENS = ["git", "stash", "pop"]


def _row(row_id: str, namespace: str, content: str, *, signal: str,
         confidence: float, superseded_at: str | None = None) -> dict:
    """One seeding-contract row (the direct-sqlite INSERT column set)."""
    return {
        "id": row_id,
        "namespace": namespace,
        "type": "lesson",
        "content": content,
        "tags": "",
        "source_ref": "",
        "source_hash": "",
        "confidence": confidence,
        "signal": signal,
        "valid_from": INGESTION_TS,
        "ingestion_ts": INGESTION_TS,
        "superseded_at": superseded_at,
    }


def _id(suffix: str) -> str:
    return f"00000000-0000-4000-8000-0000000009{suffix}"


def build_cases() -> dict:
    rows = [
        # --- six contract rows (981..986) ---------------------------------
        # Admitted foreign hazard row 1 (project:foreign-a, signal=test).
        _row(_id("81"), "project:foreign-a",
             "cross lane case one: before git stash pop run git stash list; "
             "a blind pop can apply a foreign pre-existing stash",
             signal="test", confidence=0.9),
        # Admitted foreign hazard row 2 (project:foreign-b, signal=compile)
        # reflecting the git reset --hard HEAD hazard command.
        _row(_id("82"), "project:foreign-b",
             "cross lane case two: git stash pop after git reset --hard HEAD "
             "leaves nothing to restore when the compile gate fails",
             signal="compile", confidence=0.9),
        # Wrong signal: foreign row whose signal is outside the closed
        # CROSS_PROJECT_SIGNALS set, so it must never admit.
        _row(_id("83"), "project:foreign-a",
             "cross lane wrong signal: ungrounded git stash pop opinion note",
             signal="none", confidence=0.9),
        # Below the confidence floor: foreign row at 0.1.
        _row(_id("84"), "project:foreign-b",
             "cross lane below floor: unverified git stash pop guess",
             signal="test", confidence=0.1),
        # Current-namespace row: never a cross admission (it IS the project).
        _row(_id("85"), NAMESPACE,
             "current project case: git stash pop while the suite runs "
             "flakes the tests",
             signal="test", confidence=0.9),
        # user:global row: the global tier owns it, never the cross tier.
        # Reflects the git push --force-with-lease hazard command.
        _row(_id("86"), "user:global",
             "global case: prefer git push --force-with-lease over plain "
             "force push after git stash pop recovery",
             signal="test", confidence=0.9),
        # --- six slot-case rows (987..992): 4 current + 2 global -----------
        _row(_id("87"), NAMESPACE,
             "slot case current one: git stash pop needs a clean status "
             "first",
             signal="test", confidence=0.9),
        _row(_id("88"), NAMESPACE,
             "slot case current two: git stash pop keeps untracked files "
             "safe",
             signal="compile", confidence=0.9),
        _row(_id("89"), NAMESPACE,
             "slot case current three: lint the worktree before git stash "
             "pop",
             signal="lint", confidence=0.9),
        _row(_id("90"), NAMESPACE,
             "slot case current four: git stash pop restores the indexed "
             "changes",
             signal="test", confidence=0.9),
        _row(_id("91"), "user:global",
             "slot case global one: reviewers expect git stash pop notes in "
             "the handoff",
             signal="reviewer", confidence=0.9),
        _row(_id("92"), "user:global",
             "slot case global two: git stash pop guidance applies across "
             "projects",
             signal="test", confidence=0.9),
    ]
    return {
        "moment": MOMENT,
        "namespace": NAMESPACE,
        "ops_tokens": list(OPS_TOKENS),
        "query": QUERY,
        "rows": rows,
    }


def build_expected() -> dict:
    """The frozen expectations over the cases fixture.

    The envelope key set is the issue #158 selector contract's required key
    set (storelib.inject.INJECTION_ENVELOPE_REQUIRED), spelled out here as a
    sorted list so the fixture pins it without importing storelib.
    """
    envelope_keys = sorted([
        "results", "count", "omitted", "reason", "excluded", "candidate_ids",
        "tokens_used", "tokens_budget", "budget_dropped", "budget_admission",
        "budget_truncated", "budget_dropped_protected", "arms", "rendered",
    ])
    return {
        "admitted_ids": [_id("81"), _id("82")],
        "admitted_source_namespaces": ["project:foreign-a", "project:foreign-b"],
        "cap_counts": {"cross": 2, "global": 3, "project": 5},
        "envelope_keys": envelope_keys,
        "tier_markers": [
            "[ns=project:foreign-a] [tier=cross]",
            "[ns=project:foreign-b] [tier=cross]",
        ],
    }


def _write_fixture(path: Path, payload: dict) -> bytes:
    blob = (json.dumps(payload, separators=(",", ":"), sort_keys=True,
                       ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blob)
    return blob


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate the issue #98 cross-project lane fixtures")
    parser.add_argument(
        "--out-dir", default=str(FIXTURE_DIR),
        help="directory the two fixtures are written to (default: the "
             "fixture's own committed directory)")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser().resolve()
    cases_blob = _write_fixture(out_dir / "cases.json", build_cases())
    expected_blob = _write_fixture(out_dir / "expected.json", build_expected())
    print(hashlib.sha256(cases_blob).hexdigest())
    print(hashlib.sha256(expected_blob).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
