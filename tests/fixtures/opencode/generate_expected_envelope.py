#!/usr/bin/env python
"""Deterministic #158 envelope fixture for the OpenCode adapter (issue #66
AC1/AC3).

Builds a THROWAWAY scratch store (its own tempfile directory, pinned env —
never the operator store), seeds the two fixed rows exactly as issue #66
specifies (UUID ids ...666/...667, namespace project:opencode, timestamps
2026-09-10T00:00:00Z), runs the REAL ``select_and_budget_for_injection``
with the fixed parameters (query "stash pop", moment user_prompt, NO lane,
budget 1500), and writes the returned envelope as UTF-8 JSON + one LF.

Determinism is proven in-process: the whole build runs TWICE against two
fresh scratch stores and the bytes must be identical before anything is
written. Prints the SHA-256 digest of expected-envelope.json.

Tests import ``seed_opencode_fixture_store`` from this module so the
generator and the parity tests share ONE seeding path (no drift).
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]

NAMESPACE = "project:opencode"
FIXED_TS = "2026-09-10T00:00:00Z"
FIXED_SESSION = "00000000-0000-4000-8000-000000000660"
FIXED_QUERY = "stash pop"
FIXED_MOMENT = "user_prompt"
FIXED_BUDGET = 1500

FIXED_ROWS = [
    {
        "id": "00000000-0000-4000-8000-000000000666",
        "type_": "fact",
        "content": "git stash pop restores the stashed working tree changes",
        "tags": "git,workflow",
        "source_ref": "issue-66-opencode-fixture",
        "confidence": 0.9,
        "signal": "test",
    },
    {
        "id": "00000000-0000-4000-8000-000000000667",
        "type_": "fact",
        "content": "run the launcher suite serially on a loaded developer box",
        "tags": "testing,ci",
        "source_ref": "issue-66-opencode-fixture",
        "confidence": 0.8,
        "signal": "test",
    },
]


def _pin_scratch_env(root: Path) -> None:
    os.environ["ZMEM_STORE"] = str(root / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(root)
    os.environ["ZMEM_MODELS_DIR"] = str(root / "missing-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"


def _import_storelib():
    scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import storelib  # noqa: PLC0415 - env must be pinned before import
    return storelib


def seed_opencode_fixture_store(store_path: str) -> None:
    """Seed ``store_path`` with the fixed #66 fixture rows (idempotent-
    hostile: call on a FRESH store only). Env must already point
    ZMEM_STORE at ``store_path`` BEFORE storelib is first imported."""
    storelib = _import_storelib()
    conn = storelib.connect()
    try:
        storelib.schema.init_db(conn)
        storelib.schema.migrate(conn)
        for row in FIXED_ROWS:
            result = storelib.add_memory(
                conn,
                namespace=NAMESPACE,
                type_=row["type_"],
                content=row["content"],
                tags=row["tags"],
                source_ref=row["source_ref"],
                confidence=row["confidence"],
                signal=row["signal"],
                now=FIXED_TS,
                skip_dedup=True,
            )
            new_id = str(result)
            conn.execute("UPDATE memory SET id = ? WHERE id = ?",
                         (row["id"], new_id))
        conn.commit()
    finally:
        conn.close()


def _build_envelope_bytes() -> bytes:
    storelib = _import_storelib()
    conn = storelib.connect()
    try:
        envelope = storelib.select_and_budget_for_injection(
            conn,
            query=FIXED_QUERY,
            namespace=NAMESPACE,
            moment=FIXED_MOMENT,
            session_id=FIXED_SESSION,
            lane=None,
            limit=5,
            global_limit=3,
            budget_tokens=FIXED_BUDGET,
            no_telemetry=True,
        )
    finally:
        conn.close()
    return json.dumps(envelope, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def _one_build() -> bytes:
    with tempfile.TemporaryDirectory(prefix="zmem-oc-envelope-") as tmp:
        _pin_scratch_env(Path(tmp))
        # storelib freezes the store path at first import per process; a
        # second build in the same process reuses the first scratch store,
        # so purge the module between builds to force re-resolution.
        for mod in [m for m in list(sys.modules)
                    if m == "storelib" or m.startswith("storelib.")]:
            del sys.modules[mod]
        seed_opencode_fixture_store(os.environ["ZMEM_STORE"])
        return _build_envelope_bytes()


def main() -> int:
    first = _one_build()
    second = _one_build()
    if first != second:
        sys.stderr.write("generate_expected_envelope: builds are not "
                         "deterministic; refusing to write\n")
        return 1
    out = FIXTURE_DIR / "expected-envelope.json"
    out.write_bytes(first)
    print("expected-envelope.json sha256 "
          + hashlib.sha256(first).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
