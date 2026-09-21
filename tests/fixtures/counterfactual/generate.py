"""Maintainer-only generator for the committed #157 counterfactual fixtures.

Writes ``tasks.json`` (the five recorded task exchanges), ``store.sqlite``
(the deterministic eval-store corpus plus the five
``project:counterfactual`` rows with pinned ``f0000000-...`` ids), and
``expected.json`` (the exact ``recorded-stub-v1`` report bytes produced by
``scripts/eval_counterfactual.py``). This module is the ONLY writer for the
three files: committed copies are compared first and never silently
replaced, so drift between the implementation and the committed oracle is a
hard error.

Regeneration note: tasks.json and expected.json reproduce byte-identically;
store.sqlite carries SQLite build nondeterminism, so regenerating it
requires deleting the committed file first (the refuse-on-drift guard then
becomes the deliberate-replacement step). CI never regenerates.

The five sessions are fixture-defined (issue #157 assumed the #170 evidence
surface, which does not exist on main in this shape): each prompt carries a
distinctive token shared only with its own memory row, so under
``ZMEM_INJECT=1`` the passive lane surfaces exactly that row - its id
appears in the rendered fence (recall.py renders ``- [<id>]``) - while
under ``ZMEM_INJECT=0`` no fence exists at all. The generator is a
developer-side tool; CI never runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
EVAL_STORE = ROOT / "tests" / "fixtures" / "eval_store.py"
EVALUATOR = ROOT / "scripts" / "eval_counterfactual.py"
PIN_TS = "2026-06-01T00:00:00Z"
NAMESPACE = "project:counterfactual"

# Distinctive per-session pairs: each prompt and its own memory row share a
# UNIQUE PHRASE (plus the session token) that appears in no other pair, so
# rank-1 under injection is always the row itself. Row contents are also
# lexically dissimilar to each other (the CLI dedup guard absorbs
# near-identical shapes even when the token differs).
SESSIONS = (
    {
        "token": "counterfactual-alpha",
        "phrase": "stage the canary and watch the error budget",
        "content": "runbook {token}: {phrase} for ten minutes, then promote the rollout and write the verified ok marker to the audit trail",
    },
    {
        "token": "counterfactual-bravo",
        "phrase": "freeze the queue and snapshot the ledger tables",
        "content": "{token} incident recipe: {phrase}, replay the last twenty transactions, and only then unfreeze with the ok marker",
    },
    {
        "token": "counterfactual-charlie",
        "phrase": "drain the background workers before the schema patch",
        "content": "for the {token} migration: {phrase}, apply the patch in one transaction, verify row counts against the projection, record ok",
    },
    {
        "token": "counterfactual-delta",
        "phrase": "warm the cache from the secondary while the primary stays read-only",
        "content": "when the {token} cache is cold: {phrase}, and finish by stamping the verified marker",
    },
    {
        "token": "counterfactual-echo",
        "phrase": "triple-check the checksum manifest and sign the artifact",
        "content": "the {token} release gate: {phrase}, publish behind the flag, then log the ok marker",
    },
)


def _row_id(index: int) -> str:
    return f"f0000000-0000-4000-8000-{index:012d}"


def _task(index: int) -> dict:
    session = SESSIONS[index - 1]
    token = session["token"]
    return {
        "prompt": f"{token}: {session['phrase']}, then record the outcome",
        "tool_input": f"run {token} rollout --check",
        "tool_output": f"{token} rollout verified: ok",
        "memory_row_id": _row_id(index),
        "recorded_successful_action": f"{token} rollout completed",
        "recorded_no_memory_action": f"{token} rollout failed",
        "namespace": NAMESPACE,
        "timestamp": PIN_TS,
    }


def _tasks_payload() -> dict:
    return {"tasks": [_task(index) for index in range(1, 6)]}


def _env(store: Path, scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(scratch / "data"),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_TEST_NOW": PIN_TS,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_checked(cmd: list[str], env: dict[str, str], *, timeout: int, what: str) -> subprocess.CompletedProcess:
    """Run one generator subprocess, translating every failure mode into RuntimeError."""
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=env,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"{what} could not start: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"{what} failed ({result.returncode}): {result.stderr}")
    return result


def _run_cli(store: Path, env: dict[str, str], *argv: str) -> None:
    _run_checked(
        [sys.executable, str(ROOT / "skills" / "memory" / "scripts" / "store.py"), *argv],
        env, timeout=180, what=f"store.py {argv[0]}",
    )


def _pin_row_ids(store: Path) -> None:
    """Pin the five project:counterfactual row ids (the _pin_ids technique).

    Remaps exactly the derived tables the CLI writes for these rows
    (memory_entity, memory_vec). There is no FTS rebuild; the lexical path
    reads row-local content.
    """
    conn = sqlite3.connect(str(store))
    try:
        rows = conn.execute(
            "SELECT id FROM memory WHERE namespace=? ORDER BY rowid", (NAMESPACE,)
        ).fetchall()
        if len(rows) != 5:
            raise RuntimeError(f"expected 5 {NAMESPACE} rows, found {len(rows)}")
        for index, (old_id,) in enumerate(rows, start=1):
            new_id = _row_id(index)
            conn.execute("UPDATE memory SET id=? WHERE id=?", (new_id, old_id))
            conn.execute("UPDATE memory_entity SET memory_id=? WHERE memory_id=?", (new_id, old_id))
            try:
                conn.execute("UPDATE memory_vec SET memory_id=? WHERE memory_id=?", (new_id, old_id))
            except sqlite3.OperationalError:
                pass  # vec0 unavailable — recall degrades to lexical deterministically
        conn.commit()
    finally:
        conn.close()


def _canonicalize(store: Path) -> None:
    """Finalize a standalone, byte-stable SQLite snapshot."""
    conn = sqlite3.connect(str(store))
    try:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _build_store(candidate: Path, scratch: Path) -> None:
    env = _env(candidate, scratch)
    _run_checked(
        [sys.executable, str(EVAL_STORE), str(candidate)],
        env, timeout=300, what="eval store builder",
    )
    for index in range(1, 6):
        session = SESSIONS[index - 1]
        token = session["token"]
        content = session["content"].format(token=token, phrase=session["phrase"])
        _run_cli(
            candidate, env, "add",
            "--namespace", NAMESPACE,
            "--type", "fact",
            "--content", content,
            "--source-ref", f"counterfactual:{token}",
            "--signal", "test",
            "--json",
        )
    _pin_row_ids(candidate)
    _canonicalize(candidate)


def _write_checked(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() == data:
            return
        raise RuntimeError(
            f"refusing to replace {path}: committed bytes differ from generated "
            f"(existing {len(path.read_bytes())}B, generated {len(data)}B); "
            "regenerate deliberately after reviewing the drift"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def generate(tasks_out: Path, store_out: Path, expected_out: Path) -> dict[str, str]:
    tasks_bytes = (
        json.dumps(_tasks_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="zmem-counterfactual-build-") as raw:
        scratch = Path(raw)
        candidate = scratch / "store.sqlite"
        _build_store(candidate, scratch)
        staged_tasks = scratch / "tasks.json"
        staged_tasks.write_bytes(tasks_bytes)
        report_candidate = scratch / "expected.json"
        _run_checked(
            [sys.executable, str(EVALUATOR),
             "--tasks", str(staged_tasks),
             "--store", str(candidate),
             "--model-id", "recorded-stub-v1",
             "--json-out", str(report_candidate)],
            # Keep the builder's explicit store env out of the evaluator's
            # operator-alias refusal check: the candidate is a disposable
            # snapshot (same shape as the replay generator's evaluator call).
            {**_env(candidate, scratch), "ZMEM_STORE": str(scratch / "ambient.sqlite")},
            timeout=300, what="counterfactual evaluator",
        )
        expected_bytes = report_candidate.read_bytes()
        store_bytes = candidate.read_bytes()
    _write_checked(tasks_out, tasks_bytes)
    _write_checked(store_out, store_bytes)
    _write_checked(expected_out, expected_bytes)
    return {
        "tasks_sha256": hashlib.sha256(tasks_bytes).hexdigest(),
        "store_sha256": hashlib.sha256(store_bytes).hexdigest(),
        "expected_sha256": hashlib.sha256(expected_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="tests/fixtures/counterfactual/generate.py")
    parser.add_argument("--tasks", dest="tasks", type=str, required=True, help="output path for tasks.json")
    parser.add_argument("--store", dest="store", type=str, required=True, help="output path for store.sqlite")
    parser.add_argument("--expected", dest="expected", type=str, required=True, help="output path for expected.json")
    args = parser.parse_args()
    digests = generate(Path(args.tasks), Path(args.store), Path(args.expected))
    print(json.dumps(digests, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
