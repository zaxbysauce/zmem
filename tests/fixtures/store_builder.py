"""Build a tiny deterministic store fixture for CLI characterization.

Task 2.1 (issue #57): the characterization suite freezes stdout hashes of
`stats`, `list --json`, `recall --json` and `export-jsonl` against a fixture
store, and must fail if the public CLI surface (subcommands / required flags)
drifts. This builder recreates a small representative store OUT OF BAND (via
the real `store.py` subprocess, model-absent) and then pins every timestamp to
a fixed sentinel so the hashes are reproducible run-to-run.

NOT committed as a binary sqlite — the store is rebuilt on every run. Run with
`python tests/test_store_characterization.py`.

Design notes
------------
* Every CLI write goes through the real `store.py` subprocess so the
  characterization exercises the actual write path, not a hand-rolled insert.
* The embedding runtime is forced absent (see `BASE_ENV`) so no rows carry an
  embedding and `memory_vec` stays empty: recall deterministically falls back to
  FTS5/lexical, identical on a model-present dev box and model-absent CI.
* After seeding, `_pin_timestamps()` rewrites every timestamp / meta value to a
  fixed sentinel via direct sqlite. `stats` also prints `store: <tmp path>` and
  `models_dir=<path>` — inherently location-specific — which the characterization
  test normalizes before hashing (see `NORMALIZE_STATS`). This builder only pins
  *data*; path normalization lives in the test.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# Fixed sentinel every timestamp is collapsed to, so downstream stdout hashes
# are identical across runs (year pinned to 2026 to match repo's schema-era).
PIN_TS = "2026-02-03T04:05:06Z"

# Model-absent + deterministic env for every CLI call (repo convention from
# tests/test_model_fallback.py and tests/test_stats_recency.py). ZMEM_MODELS_DIR
# is a FIXED literal path (not tmp-derived) so the `stats` `models_dir=` token
# is stable before normalization.
BASE_ENV = {
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "ZMEM_MODELS_DIR": "/nonexistent-zmem-models-dir",
    # Force a deterministic child stdout encoding so help/text output hashes are
    # byte-stable across Windows (cp1252) and POSIX (utf-8) CI.
    "PYTHONIOENCODING": "utf-8",
}
# Keys that must never leak from the ambient environment into a fixture build.
_STRIP = ("ZMEM_DATA", "ZMEM_BACKUP_DIR", "ZMEM_BACKUP_INTERVAL_DAYS")


def _env(store: str) -> dict:
    env = {**os.environ, **BASE_ENV, "ZMEM_STORE": store}
    for k in _STRIP:
        env.pop(k, None)
    return env


def _run(store: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        env=_env(store), capture_output=True, text=True, timeout=90,
    )


def _pin_ids(store: str) -> None:
    """Rewrite the random UUID memory ids to fixed deterministic values.

    `add` generates a fresh uuid4 per row, so ids leak into `list`/`recall`/
    `export-jsonl` output and would make stdout hashes non-reproducible across
    builds. Rowids are insertion-order deterministic (same seed sequence), so
    remapping by rowid to a fixed uuid keeps every downstream hash stable.
    No other table references memory.id here (merged_from is NULLed; memory_vec
    is empty in the model-absent fixture), so the ids are safe to rewrite.
    """
    conn = sqlite3.connect(store)
    try:
        rows = conn.execute("SELECT id, rowid FROM memory ORDER BY rowid").fetchall()
        for idx, (old_id, _rowid) in enumerate(rows, start=1):
            new_id = f"10000000-0000-4000-8000-{idx:012d}"
            conn.execute("UPDATE memory SET id=? WHERE id=?", (new_id, old_id))
        conn.commit()
    finally:
        conn.close()


def _seed_rows(store: str) -> None:
    """Seed a representative set of live + superseded memories via the CLI."""
    adds = [
        # (namespace, type, content, tags, signal, source_ref)
        ("project:char", "fact", "Python dicts preserve insertion order",
         "python,language", "compile", "session:seed-1"),
        ("project:char", "fact", "the sqlite CLI uses semicolon line terminators",
         "sqlite,cli", "test", "session:seed-2"),
        ("project:char", "lesson", "run the test loop model-absent before merging",
         "testing,ci", "reviewer", "session:seed-3"),
        ("user:global", "preference", "prefer small behavior-identical refactors",
         "style,refactor", "user", "session:seed-4"),
    ]
    for ns, typ, content, tags, signal, ref in adds:
        r = _run(store, "add", "--namespace", ns, "--type", typ,
                 "--content", content, "--tags", tags, "--signal", signal,
                 "--source-ref", ref)
        if r.returncode != 0:
            raise RuntimeError(f"seed add failed: {r.returncode}\n{r.stdout}\n{r.stderr}")

    # A superseded pair so list/stats/export exercise superseded_at handling.
    # `supersede` keys on --id, so look up the just-added row's id first.
    r = _run(store, "add", "--namespace", "project:char", "--type", "fact",
             "--content", "python dicts keep insertion order on all cpython",
             "--tags", "python,language", "--signal", "user",
             "--source-ref", "session:seed-5")
    if r.returncode != 0:
        raise RuntimeError(f"supersede-source add failed: {r.returncode}")
    conn = sqlite3.connect(store)
    try:
        row = conn.execute(
            "SELECT id FROM memory WHERE source_ref=? LIMIT 1",
            ("session:seed-5",),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("seed supersede-source row not found")
    r = _run(store, "supersede", "--id", row[0])
    if r.returncode != 0:
        raise RuntimeError(f"supersede failed: {r.returncode}\n{r.stdout}\n{r.stderr}")


def _pin_timestamps(store: str) -> None:
    """Rewrite every timestamp-bearing column/meta to PIN_TS via direct sqlite,
    and drop any telemetry that would embed a fresh 'now' on a later read."""
    conn = sqlite3.connect(store)
    try:
        ts_cols = ["valid_from", "superseded_at", "ingestion_ts", "last_retrieved",
                   "last_surfaced"]
        for col in ts_cols:
            conn.execute(f"UPDATE memory SET {col}=? WHERE {col} IS NOT NULL", (PIN_TS,))
        conn.execute("UPDATE memory SET merged_from=NULL, retrieval_count=0, surfaced_count=0")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES "
            "('created_at', ?), ('last_backup', ?), ('last_consolidation', ?)",
            (PIN_TS, PIN_TS, PIN_TS),
        )
        conn.commit()
    finally:
        conn.close()


def build_store(dest_dir: str | None = None) -> str:
    """Build a deterministic seeded store and return its path."""
    tmp = dest_dir or tempfile.mkdtemp(prefix="zmem-char-")
    store = os.path.join(tmp, "store.sqlite")
    r = _run(store, "init")
    if r.returncode != 0:
        raise RuntimeError(f"init failed: {r.returncode}\n{r.stderr}")
    _seed_rows(store)
    _pin_ids(store)
    _pin_timestamps(store)
    # Sanity: the fixture must contain seeded rows and exactly one superseded.
    conn = sqlite3.connect(store)
    try:
        n = conn.execute("SELECT count(*) AS c FROM memory").fetchone()[0]
        n_super = conn.execute(
            "SELECT count(*) AS c FROM memory WHERE superseded_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    if n < 4 or n_super < 1:
        raise AssertionError(f"fixture unexpected: total={n} superseded={n_super}")
    return store


def store_row_json(store: str) -> list[dict]:
    """Return the deterministic rows a store would emit (for building expected
    hashes without depending on unstable ordering/fields). Kept for reference;
    characterization currently hashes raw CLI stdout, not this."""
    conn = sqlite3.connect(store)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM memory").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    p = build_store()
    print(p)
    # Freeze a copy for inspection.
    shutil.copy(p, "fixture-store.sqlite")
    print("wrote fixture-store.sqlite")
