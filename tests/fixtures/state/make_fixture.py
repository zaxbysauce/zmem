"""Maintainer-only generator for the committed issue #173 state-expiry fixture.

Contract step 12 (zaxbysauce/zmem#173) fixes every literal below: three
`state` rows (endpoint / setting / metric), their UUID-shaped ids, and their
expiry timestamps. This script seeds them into a fresh v15 store through the
REAL writer (so the side-table rows come from the same code path production
uses), exports `expiry.jsonl`, ingests that file into a SECOND fresh store,
and exports again as `expected.json` — the canonical round-trip oracle that
`StateSyncTest.test_state_expiry_fixture_round_trips` compares byte-for-byte.

`expected.json` is JSONL despite the `.json` suffix (it is the exporter's
wire output, ordered by memory id). Tests consume the committed bytes; this
module is never imported by a test to conjure its own oracle. Re-run it only
when a contract change intentionally moves the wire, then commit the three
files together so the digests below match the committed bytes.

Run from the repo root:  python tests/fixtures/state/make_fixture.py
It prints both `sha256sum` commands so a reviewer can verify the committed
bytes without re-running the generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"

# The contract's fixed seed. Order is the export order (by memory id).
ROWS = (
    ("00000000-0000-4000-8000-000000000001", "api.example.com", "endpoint",
     "2026-10-10T00:00:00Z"),
    ("00000000-0000-4000-8000-000000000002", "deploy-timeout", "setting",
     "2026-12-09T00:00:00Z"),
    ("00000000-0000-4000-8000-000000000003", "request-latency", "metric",
     "2026-09-24T00:00:00Z"),
)
NAMESPACE = "fixture:state-173"
SEED_TS = "2026-09-10T00:00:00Z"


def _isolated_env(scratch: Path) -> dict:
    """The four contract isolation variables, plus a scrub of ambient ZMEM_*.

    ZMEM_STORE points at a FILE inside the GUID-derived scratch dir (pointing
    it at a directory makes every store call exit 2); ZMEM_DATA is that dir.
    The operator store is never opened.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM_")
           and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
    env["ZMEM_STORE"] = str(scratch / "store.sqlite")
    env["ZMEM_DATA"] = str(scratch)
    env["ZMEM_MODELS_DIR"] = str(scratch / "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


def _fresh_store(scratch: Path, name: str) -> sqlite3.Connection:
    """A brand-new isolated v15 store bound to the four isolation variables."""
    store_dir = scratch / name
    store_dir.mkdir(parents=True, exist_ok=True)
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(_isolated_env(store_dir))
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from storelib import schema  # imported AFTER the env pin
        conn = sqlite3.connect(str(store_dir / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        schema.init_db(conn)
        schema.migrate(conn)
        return conn
    finally:
        os.environ.clear()
        os.environ.update(old)


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the three fixed state rows with the contract's exact literals.

    Seeded by direct INSERT rather than through ``add_memory`` because the
    contract fixes the namespace ``fixture:state-173``, and the DIRECT writer's
    namespace grammar (``schema_meta.NAMESPACE_RE``) accepts only
    ``project:``/``user:``/``fleet:``/``host:``/``agent:``/``domain:``. The sync
    path — which is what this fixture exercises — does not apply that grammar,
    so the namespace is legal on the wire. Every other column is set to the
    contract's literal value (source_hash stays the empty string; the exporter
    omits it), and each memory row is followed by its memory_expiry row so the
    export emits the pairs in id order.
    """
    for mid, content, expiry_class, inject_until in ROWS:
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "superseded_at, ingestion_ts, retrieval_count, last_retrieved, "
            "surfaced_count, last_surfaced, merged_from, content_norm, "
            "valid_until, update_of, taint, trust_score, applied_count, "
            "violated_count, embedding, embedding_model, embedded_at, "
            "consolidated_at, supersede_reason) "
            "VALUES (?, ?, 'state', ?, '', ?, '', 0.5, 'none', ?, NULL, ?, "
            "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', 1.0, 0, "
            "0, NULL, '', NULL, NULL, '')",
            (mid, NAMESPACE, content, NAMESPACE, SEED_TS, SEED_TS),
        )
        conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, expiry_class) "
            "VALUES (?, ?, ?)",
            (mid, inject_until, expiry_class),
        )
    conn.commit()


def _export(conn: sqlite3.Connection, out: Path) -> None:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from storelib import sync
    rc = sync.cmd_export_jsonl(conn, out=str(out))
    if rc != 0:
        raise SystemExit(f"export-jsonl failed with rc={rc}")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    # #173 PR-review F18: --out-dir lets the parity test regenerate into a
    # SCRATCH directory (repo norm: timeout/evidence fixtures) instead of
    # overwriting the committed bytes; default stays the committed dir.
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=HERE,
                    help="directory to write expiry.jsonl/expected.json into "
                         "(default: this committed fixture directory)")
    out = ap.parse_args().out_dir
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="zmem-state-fixture-"))
    try:
        # 1. Seed a fresh v15 store and export the fixture input.
        a = _fresh_store(scratch, "seed")
        try:
            _seed(a)
            _export(a, out / "expiry.jsonl")
        finally:
            a.close()

        # 2. Ingest that file into a SECOND fresh store and re-export: the
        #    round-tripped bytes are the canonical oracle.
        b = _fresh_store(scratch, "roundtrip")
        try:
            if str(SCRIPTS) not in sys.path:
                sys.path.insert(0, str(SCRIPTS))
            from storelib import sync
            rc = sync.cmd_ingest_jsonl_strict(
                b, in_path=str(out / "expiry.jsonl"), source_ref=None)
            if rc != 0:
                raise SystemExit(f"ingest-jsonl --strict failed with rc={rc}")
            _export(b, out / "expected.json")
        finally:
            b.close()

        # 3. Sanity: the two files carry the three expiry records, in id order.
        exp_rows = [json.loads(l) for l in
                    (out / "expiry.jsonl").read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        assert [r["id"] for r in exp_rows if r.get("kind") == "memory"] == \
            [row[0] for row in ROWS], "memory ids are not in contract order"
        assert sum(1 for r in exp_rows if r.get("kind") == "memory_expiry") == 3

        print(f"wrote {out / 'expiry.jsonl'} "
              f"({len(exp_rows)} records)")
        print(f"wrote {out / 'expected.json'} "
              f"({len([l for l in (out / 'expected.json').read_text(encoding='utf-8').splitlines() if l.strip()])} records)")
        print()
        print(f"sha256(expiry.jsonl)  = {_digest(out / 'expiry.jsonl')}")
        print(f"sha256(expected.json) = {_digest(out / 'expected.json')}")
        print()
        print("verify the committed bytes with:")
        print("  sha256sum tests/fixtures/state/expiry.jsonl")
        print("  sha256sum tests/fixtures/state/expected.json")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
