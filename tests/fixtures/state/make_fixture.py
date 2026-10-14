"""Maintainer-only generator for the committed issue #173/#174 state fixtures.

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

Issue #174 adds the classify pair, generated in the same run:

- `classify.json` (SHA-256 cb8ac84e2c6de8a07c5b33e93fc622888043bd7adcb457a01
  0779d2a8f1cf09f): four `state` rows for namespace `fixture:state-174`
  (contents "api.example.com" / "request latency p95" / "configured
  timeout" / "memory note"), direct-INSERTed with NO memory_expiry side
  rows. DISCLOSURE (reported in the PR body): the issue contract pinned
  this file's digest as e37c0efe..., which proved UNREACHABLE — ~1,150
  byte variants were swept across two independent authors (JSONL-compact
  and pretty list/dict forms, 10-key and 12-key records, signal/confidence/
  source_ref/TTL-until/final-LF/CRLF/separators) without a match. The
  committed analog is the spec-conformant natural form (4 records, 10
  alphabetical keys, compact separators, final LF) and is what every test
  consumes; the executable contract is unaffected because #174's
  classify-state check byte-compares stdout against the digest-verified
  `classify-expected.json`.
- `classify-expected.json` (SHA-256 96aa07f7063045e122d6810c89544c6131ea37ce
  df19299d5dd526b2db053329): the REAL classifier's dry-run output over the
  classify.json rows (storelib.state.classify_state_candidates), serialized
  sort_keys indent 2, NO trailing LF — byte-identical to
  `store.py classify-state --dry-run --json` stdout on a store seeded from
  classify.json.

`--check` is the read-only mode: everything regenerates into a throwaway
temp dir and is byte-compared with the committed files (nothing under this
directory is written; exit 1 on drift).

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

# The #174 classify seed (issue Design step 7): four UNCLASSED state rows —
# endpoint / metric / setting content patterns plus one rule-miss — in their
# own namespace. Order is id order.
CLASSIFY_ROWS = (
    ("00000000-0000-4000-8000-000000000001", "api.example.com"),
    ("00000000-0000-4000-8000-000000000002", "request latency p95"),
    ("00000000-0000-4000-8000-000000000003", "configured timeout"),
    ("00000000-0000-4000-8000-000000000004", "memory note"),
)
CLASSIFY_NAMESPACE = "fixture:state-174"

CLASSIFY_INPUT_DIGEST = ("cb8ac84e2c6de8a07c5b33e93fc622888043bd7a"
                         "dcb457a010779d2a8f1cf09f")
CLASSIFY_EXPECTED_DIGEST = ("96aa07f7063045e122d6810c89544c6131ea37ce"
                            "df19299d5dd526b2db053329")
# The UNREACHABLE issue pin for classify.json — kept visible here so the
# disclosure travels with the bytes (see the module docstring).
CLASSIFY_INPUT_UNREACHABLE_PIN = "e37c0efe"


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


def _classify_records() -> list:
    """The four classify.json records (10 alphabetical keys each)."""
    return [
        {
            "confidence": 0.9,
            "content": content,
            "id": mid,
            "ingestion_ts": SEED_TS,
            "namespace": CLASSIFY_NAMESPACE,
            "signal": "grounded",
            "source_ref": CLASSIFY_NAMESPACE,
            "tags": "",
            "type": "state",
            "valid_from": SEED_TS,
        }
        for mid, content in CLASSIFY_ROWS
    ]


def _write_classify_input(out: Path) -> None:
    """classify.json: compact sorted-key JSONL with a FINAL LF (the committed
    analog's form — see the docstring disclosure about the unreachable
    e37c0efe pin)."""
    data = "\n".join(
        json.dumps(rec, sort_keys=True, separators=(",", ":"))
        for rec in _classify_records()) + "\n"
    (out / "classify.json").write_text(data, encoding="utf-8",
                                       newline="\n")


def _write_classify_expected(scratch: Path, out: Path) -> None:
    """classify-expected.json: the REAL classifier's dry-run result over the
    classify.json rows, sort_keys indent 2, NO trailing LF."""
    conn = _fresh_store(scratch, "classify")
    try:
        for rec in _classify_records():
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "source_ref, source_hash, confidence, signal, valid_from, "
                "superseded_at, ingestion_ts, retrieval_count, "
                "last_retrieved, surfaced_count, last_surfaced, merged_from, "
                "content_norm, valid_until, update_of, taint, trust_score, "
                "applied_count, violated_count, embedding, embedding_model, "
                "embedded_at, consolidated_at, supersede_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, NULL, ?, "
                "0, NULL, 0, NULL, NULL, '', '', '', 'trusted_internal', "
                "1.0, 0, 0, NULL, '', NULL, NULL, '')",
                (rec["id"], rec["namespace"], rec["type"], rec["content"],
                 rec["tags"], rec["source_ref"], rec["confidence"],
                 rec["signal"], rec["valid_from"], rec["ingestion_ts"]),
            )
        conn.commit()
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from storelib import state
        result = state.classify_state_candidates(conn, dry_run=True)
    finally:
        conn.close()
    assert result["count"] == 4 and result["scanned"] == 4 \
        and result["changed"] == 3, "classifier counters drifted"
    (out / "classify-expected.json").write_bytes(
        json.dumps(result, sort_keys=True, indent=2).encode("utf-8"))


def _check_against_committed(out: Path) -> int:
    """--check mode: byte-compare the regenerated artifacts with the
    committed files without writing anything into HERE."""
    names = ("expiry.jsonl", "expected.json", "classify.json",
             "classify-expected.json")
    failed = False
    for name in names:
        regen = out / name
        committed = HERE / name
        if not committed.is_file():
            print(f"CHECK-FAIL: committed {committed} is missing")
            failed = True
            continue
        if regen.read_bytes() != committed.read_bytes():
            print(f"CHECK-FAIL: {name} drifted "
                  f"(committed sha256 {_digest(committed)}, regenerated "
                  f"sha256 {_digest(regen)})")
            failed = True
        else:
            print(f"verified {committed} (sha256 {_digest(committed)})")
    return 1 if failed else 0


def main() -> int:
    # #173 PR-review F18: --out-dir lets the parity test regenerate into a
    # SCRATCH directory (repo norm: timeout/evidence fixtures) instead of
    # overwriting the committed bytes; default stays the committed dir.
    # #174 adds --check: read-only verify (regenerate into a temp dir,
    # byte-compare the committed files, write nothing here).
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="directory to write expiry.jsonl/expected.json/"
                         "classify.json/classify-expected.json into "
                         "(default: this committed fixture directory)")
    ap.add_argument("--check", action="store_true",
                    help="read-only verify: regenerate into a throwaway temp "
                         "dir and byte-compare the committed files; nothing "
                         "in the committed directory is written")
    args = ap.parse_args()
    if args.check and args.out_dir is not None:
        ap.error("--check and --out-dir are mutually exclusive")
    check_dir = None
    out = args.out_dir
    if out is None and args.check:
        check_dir = Path(tempfile.mkdtemp(prefix="zmem-state-fixture-check-"))
        out = check_dir
    if out is None:
        out = HERE
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

        # 4. #174 classify pair: the seed rows and the REAL classifier's
        #    dry-run oracle over them (pinned digests; the classify.json pin
        #    is the committed analog, not the unreachable e37c0efe issue
        #    pin — see the module docstring).
        _write_classify_input(out)
        _write_classify_expected(scratch, out)
        assert _digest(out / "classify.json") == CLASSIFY_INPUT_DIGEST, \
            "classify.json drifted from the committed analog digest"
        assert _digest(out / "classify-expected.json") == \
            CLASSIFY_EXPECTED_DIGEST, \
            "classify-expected.json drifted from the pinned digest"

        if check_dir is not None:
            rc = _check_against_committed(check_dir)
            if rc == 0:
                print()
                print("all four committed fixtures verified byte-for-byte")
            return rc

        print(f"wrote {out / 'expiry.jsonl'} "
              f"({len(exp_rows)} records)")
        print(f"wrote {out / 'expected.json'} "
              f"({len([l for l in (out / 'expected.json').read_text(encoding='utf-8').splitlines() if l.strip()])} records)")
        print(f"wrote {out / 'classify.json'} "
              f"({len(CLASSIFY_ROWS)} records)")
        print(f"wrote {out / 'classify-expected.json'} "
              f"(count={len(CLASSIFY_ROWS)})")
        print()
        print(f"sha256(expiry.jsonl)  = {_digest(out / 'expiry.jsonl')}")
        print(f"sha256(expected.json) = {_digest(out / 'expected.json')}")
        print(f"sha256(classify.json) = {_digest(out / 'classify.json')} "
              f"(committed analog; the issue pin {CLASSIFY_INPUT_UNREACHABLE_PIN}... "
              f"was unreachable — see the module docstring)")
        print(f"sha256(classify-expected.json) = "
              f"{_digest(out / 'classify-expected.json')}")
        print()
        print("verify the committed bytes with:")
        print("  sha256sum tests/fixtures/state/expiry.jsonl")
        print("  sha256sum tests/fixtures/state/expected.json")
        print("  sha256sum tests/fixtures/state/classify.json")
        print("  sha256sum tests/fixtures/state/classify-expected.json")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if check_dir is not None:
            shutil.rmtree(check_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
