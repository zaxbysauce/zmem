#!/usr/bin/env python3
"""Deterministically build the issue #181 live-rescan fixture STORE.

Run from the repository root:

    python tests/fixtures/secrets/build_store.py tests/fixtures/secrets/store.sqlite

Builds a schema-v14 store holding EXACTLY two live rows in namespace
``project:fixture`` (fixed ids / timestamps — direct INSERT, never a store.py
add, which would mint nondeterministic uuids). Each row's content carries a
distinct ghp_-shaped credential token the rescan must flag:

  - ``00000000-0000-4000-8000-000000000181``  token one  source_ref fixture:181:one
  - ``00000000-0000-4000-8000-000000000182``  token two  source_ref fixture:181:two

The store is built under a scratch env pinned BEFORE any storelib import
(storelib freezes STORE_PATH at import time and must never see the operator's
real store), checkpointed into DELETE journal mode so the copied file is a
single deterministic artifact (byte-stable per sqlite build; the committed
binary is the canonical artifact), then ``shutil.copyfile``d out to argv[1].

Asserted BEFORE the copy (issue #181 fixture spec): memory rows == 2, live
rows == 2, zero memory_vec rows, schema_version == 14.

The credential tokens are CONCATENATED from two pieces so the raw literal
never appears verbatim in tracked .py source (house precedent:
tests/test_r04_queue_add_fb.py:48).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))

ROW_181 = "00000000-0000-4000-8000-000000000181"
ROW_182 = "00000000-0000-4000-8000-000000000182"
FIXED_TS = "2026-01-01T00:00:00Z"
NAMESPACE = "project:fixture"

# Split so the raw token never appears verbatim in this tracked source file.
TOKEN_ONE = "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
TOKEN_TWO = "ghp_" + "zYxWvUtSrQpOnMlKjIhGfEdCbA9876543210"

ROWS = (
    (ROW_181, f"fixture credential {TOKEN_ONE} one", "fixture:181:one"),
    (ROW_182, f"fixture credential {TOKEN_TWO} two", "fixture:181:two"),
)

# Every memory column after init_db + migrate (v3 adds the embedding trio,
# v4 adds consolidated_at/supersede_reason). All NOT NULL columns and all
# columns the issue names are set EXPLICITLY; telemetry columns mirror the
# schema DDL defaults (0 / NULL / 1.0) so the fixture never depends on an
# implicit default surviving a future migration.
INSERT_SQL = (
    "INSERT INTO memory (id, namespace, type, content, tags, source_ref,"
    " source_hash, confidence, signal, valid_from, valid_until, update_of,"
    " taint, superseded_at, ingestion_ts, retrieval_count, last_retrieved,"
    " surfaced_count, last_surfaced, merged_from, content_norm, trust_score,"
    " applied_count, violated_count, embedding, embedding_model, embedded_at,"
    " consolidated_at, supersede_reason)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


def build_store(out_path: Path) -> None:
    tmp = tempfile.mkdtemp(prefix="zmem181-build-")
    try:
        db_path = Path(tmp) / "store.sqlite"
        # Import AFTER the scratch env is pinned: storelib freezes STORE_PATH
        # at import time and must never see the operator's real store.
        os.environ["ZMEM_STORE"] = str(db_path)
        os.environ["ZMEM_DATA"] = tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "models-absent")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        if "storelib" in sys.modules:
            raise SystemExit("build_store.py must run before any storelib import")
        from storelib.schema import _normalize_content, init_db, migrate

        conn = sqlite3.connect(str(db_path))
        try:
            init_db(conn)
            migrate(conn)
            ver = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not ver or int(ver[0]) != 14:
                raise SystemExit(
                    f"fixture store is schema v{ver[0] if ver else '?'} — want v14")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)")}
            expected_cols = {
                "id", "namespace", "type", "content", "tags", "source_ref",
                "source_hash", "confidence", "signal", "valid_from",
                "valid_until", "update_of", "taint", "superseded_at",
                "ingestion_ts", "retrieval_count", "last_retrieved",
                "surfaced_count", "last_surfaced", "merged_from",
                "content_norm", "trust_score", "applied_count",
                "violated_count", "embedding", "embedding_model",
                "embedded_at", "consolidated_at", "supersede_reason",
            }
            missing = expected_cols - cols
            if missing:
                raise SystemExit(f"memory table lacks columns: {sorted(missing)}")
            for mid, content, source_ref in ROWS:
                conn.execute(INSERT_SQL, (
                    mid, NAMESPACE, "fact", content, "", source_ref,
                    "", 0.9, "test", FIXED_TS, "", "", "untrusted_tool",
                    None, FIXED_TS, 0, None, 0, None, None,
                    _normalize_content(content), 1.0, 0, 0,
                    None, "", None, None, ""))
            conn.commit()
            # init_db stamps meta.created_at with the wall clock; pin it so
            # the fixture bytes are reproducible (digest-stable regeneration).
            conn.execute("UPDATE meta SET value=? WHERE key='created_at'",
                         (FIXED_TS,))
            conn.commit()
            # Issue #181 fixture spec: exactly two memory rows, both live,
            # and ZERO memory_vec rows (no embeddings planted).
            total = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
            live = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
            ).fetchone()[0]
            if total != 2 or live != 2:
                raise SystemExit(
                    f"fixture row counts wrong: total={total} live={live} (want 2/2)")
            if _table_exists(conn, "memory_vec"):
                vec_rows = conn.execute(
                    "SELECT COUNT(*) FROM memory_vec").fetchone()[0]
                if vec_rows != 0:
                    raise SystemExit(
                        f"memory_vec must be empty, holds {vec_rows} row(s)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copyfile(db_path, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python tests/fixtures/secrets/build_store.py <out.sqlite>")
        return 2
    if REPO.resolve() != Path.cwd().resolve():
        print("run me from the repository root: "
              "python tests/fixtures/secrets/build_store.py "
              "tests/fixtures/secrets/store.sqlite")
        return 2
    out_path = Path(sys.argv[1])
    build_store(out_path)
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"{digest}  {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
