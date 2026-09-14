"""Deterministic fixture generator for issue #159 (query-aware passive prefetch).

Builds the two-row ``project:parity`` store through the REAL ``store.py``
subprocess path (``init`` + two ``add`` calls), then pins the rows' UUIDs and
timestamps to fixed sentinels via direct sqlite — the repo's
``tests/fixtures/store_builder.py`` convention.  ``store.py add`` has no
``--id``/timestamp flag (ids are fresh ``uuid4`` per row and stamps are
wall-clock at write time), so the post-add sqlite pin IS the supported
mechanism for deterministic ids/timestamps; the ``--source-ref`` flag exists
but does not stamp ``valid_from``/``ingestion_ts``.

The generator then runs the CLI ``prefetch`` ONCE against a fresh empty
delivery ledger and freezes the full selector envelope as
``expected-envelope.json`` (sorted keys, indent 2, one trailing LF).  Unlike
``tests/fixtures/injection-parity/generate.py`` (a hand-written SQL oracle),
this fixture deliberately exercises the real CLI write path so the expected
bytes carry the add pipeline's actual row shape (content_norm, entities, ...).

Usage (from the repo root)::

    python tests/fixtures/prefetch/generate.py \
        --store <scratch>/store.sqlite \
        --expected tests/fixtures/prefetch/expected-envelope.json

Both flags are optional: with no args the store lands in a fresh scratch dir
under the system temp and the expected envelope lands at the repo-relative
fixture path.  The generator is deterministic and idempotent — every run
builds a brand-new scratch store, so re-running always reproduces the same
``expected-envelope.json`` bytes.

Env isolation: the four ZMEM_* isolation vars are set (and injected into every
child) BEFORE any store.py subprocess runs; this module never imports
storelib, so there is no storelib import to race.  ``ZMEM_TEST_NOW`` is pinned
to the fixture timestamp so every now-derived value inside the selector is
frozen (the repo's ProviderEnvelopeTest seam).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"

FIXTURE_TS = "2026-06-01T00:00:00Z"
NAMESPACE = "project:parity"
QUERY = "stash pop"
# The prefetch session id doubles as row one's id (pinned on purpose so the
# seeded-ledger tests can name both from one constant set).
SESSION_ID = "e0000000-0000-4000-8000-000000000001"
ROW_ONE = "e0000000-0000-4000-8000-000000000001"
ROW_TWO = "e0000000-0000-4000-8000-000000000002"
CONTENT_ONE = "stash pop recovery note one"
CONTENT_TWO = "stash pop recovery note two"

FIXTURE_DIR = Path(__file__).resolve().parent
LEDGER_FIXTURE = FIXTURE_DIR / "parity-ledger.json"
EXPECTED_FIXTURE = FIXTURE_DIR / "expected-envelope.json"
# Exact bytes of the empty delivery ledger fixture: {"entries":[]} + one LF.
LEDGER_BYTES = b'{"entries":[]}\n'

# Keys that must never leak from the ambient environment into a build (the
# kill switch, the budget override, the recent floor window, the provider
# namespace override, and the MCP token config are all prefetch inputs).
_STRIP_ENV = (
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S",
    "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
)


def ledger_name(session_id: str) -> str:
    """The hashed delivery-ledger file name for one session id."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".ledger"


def isolation_env(store_path: str) -> dict:
    """Child env pinned to the scratch around ``store_path`` (issue #159).

    Sets the four isolation vars (ZMEM_STORE / ZMEM_DATA / ZMEM_MODELS_DIR /
    ZMEM_MODEL_AUTODOWNLOAD) plus the determinism seams (ZMEM_TEST_NOW,
    PYTHONIOENCODING) and strips every prefetch-affecting ambient override.
    """
    data_dir = str(Path(store_path).resolve().parent)
    env = dict(os.environ)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": str(Path(store_path).resolve()),
        "ZMEM_DATA": data_dir,
        "ZMEM_MODELS_DIR": os.path.join(data_dir, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_TEST_NOW": FIXTURE_TS,
        "PYTHONIOENCODING": "utf-8",
    })
    for key in _STRIP_ENV:
        env.pop(key, None)
    return env


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STORE_PY), *args],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )


def _pin_rows(store_path: str) -> None:
    """Rewrite the random add-path UUIDs/timestamps to fixed sentinels.

    ``add`` generates a fresh uuid4 per row and stamps wall-clock
    valid_from/ingestion_ts (there is no CLI flag for either — see the module
    docstring), so the ids/timestamps are pinned post-write via direct
    sqlite, exactly the tests/fixtures/store_builder.py convention.  Rowids
    are insertion-order deterministic, so row 1 -> ROW_ONE, row 2 -> ROW_TWO.
    """
    conn = sqlite3.connect(store_path)
    try:
        rows = conn.execute("SELECT id, rowid FROM memory ORDER BY rowid").fetchall()
        if len(rows) != 2:
            raise RuntimeError(
                f"expected exactly 2 seeded rows, found {len(rows)}")
        fixed = (ROW_ONE, ROW_TWO)
        for (old_id, _rowid), new_id in zip(rows, fixed):
            conn.execute(
                "UPDATE memory SET id=?, valid_from=?, ingestion_ts=?, "
                "retrieval_count=0, last_retrieved=NULL, "
                "surfaced_count=0, last_surfaced=NULL WHERE id=?",
                (new_id, FIXTURE_TS, FIXTURE_TS, old_id),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('created_at', ?)",
            (FIXTURE_TS,),
        )
        conn.commit()
    finally:
        conn.close()


def build_parity_store(store_path: str, env: dict) -> None:
    """init + two signal=test adds, then the id/timestamp pin.

    Reused by tests/test_passive_prefetch.py so the test store and the
    frozen fixture store are byte-equivalent by construction.
    """
    r = _run(env, "init")
    if r.returncode != 0:
        raise RuntimeError(f"store.py init failed: {r.returncode}\n{r.stderr}")
    for content in (CONTENT_ONE, CONTENT_TWO):
        r = _run(env, "add", "--namespace", NAMESPACE, "--type", "fact",
                 "--content", content, "--signal", "test",
                 "--confidence", "0.9", "--json")
        if r.returncode != 0:
            raise RuntimeError(
                f"store.py add failed ({content!r}): {r.returncode}\n"
                f"{r.stdout}\n{r.stderr}")
    _pin_rows(store_path)


def seed_empty_ledger(store_path: str, session_id: str) -> str:
    """Write a fresh copy of the empty ledger beside the store; return path."""
    ledger = (Path(store_path).resolve().parent / "ops" / ledger_name(session_id))
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(LEDGER_BYTES)
    return str(ledger)


def run_prefetch(store_path: str, env: dict,
                 session_id: str = SESSION_ID) -> dict:
    """Run the CLI prefetch once and return the parsed JSON envelope."""
    r = _run(env, "prefetch", "--query", QUERY, "--namespace", NAMESPACE,
             "--session-id", session_id, "--moment", "user_prompt")
    if r.returncode != 0:
        raise RuntimeError(
            f"store.py prefetch failed: {r.returncode}\n{r.stdout}\n{r.stderr}")
    try:
        envelope = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"prefetch stdout is not JSON: {exc}\n{r.stdout!r}") from exc
    if not isinstance(envelope, dict) or "rendered" not in envelope:
        raise RuntimeError(f"incomplete prefetch envelope: {envelope!r}")
    return envelope


def write_ledger_fixture() -> None:
    """Write parity-ledger.json iff it is not already the exact bytes."""
    if (LEDGER_FIXTURE.exists()
            and LEDGER_FIXTURE.read_bytes() == LEDGER_BYTES):
        return
    LEDGER_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FIXTURE.write_bytes(LEDGER_BYTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate the issue #159 prefetch parity fixtures")
    parser.add_argument(
        "--store", default=None,
        help="absolute path of the scratch store to build (default: a fresh "
             "scratch dir under the system temp)")
    parser.add_argument(
        "--expected", default=str(EXPECTED_FIXTURE),
        help="path of the expected-envelope.json fixture to write")
    args = parser.parse_args(argv)

    own_scratch = False
    if args.store:
        store_path = str(Path(args.store).expanduser().resolve())
    else:
        own_scratch = True
        scratch = tempfile.mkdtemp(prefix="zmem-prefetch-fixture-")
        store_path = os.path.join(scratch, "store.sqlite")

    env = isolation_env(store_path)
    build_parity_store(store_path, env)
    write_ledger_fixture()
    seed_empty_ledger(store_path, SESSION_ID)
    envelope = run_prefetch(store_path, env)

    if not envelope.get("rendered") or int(envelope.get("count", 0)) < 1:
        raise RuntimeError(
            "selector gate rejected the fixture rows (rendered empty); "
            f"envelope: {json.dumps(envelope, sort_keys=True)[:500]}")

    expected_path = Path(args.expected).expanduser().resolve()
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(
        json.dumps(envelope, sort_keys=True, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8", newline="",
    )
    print(hashlib.sha256(expected_path.read_bytes()).hexdigest())

    if own_scratch:
        import shutil
        shutil.rmtree(str(Path(store_path).parent), ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
