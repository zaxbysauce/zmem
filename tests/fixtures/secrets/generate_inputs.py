#!/usr/bin/env python3
"""Deterministically generate the issue #180 secrets-fixture INPUTS.

Run from the repository root:

    python tests/fixtures/secrets/generate_inputs.py

Writes, under tests/fixtures/secrets/:
  - patterns.json            the exact 12-positive / 12-negative matrix
  - sshpass.jsonl            the two-row five-importer input (fixed ids/ts)
  - legacy-source.sqlite     the same two records as real store rows
  - harvest.json             the same two records as a harvest array
  - quarantine_row.jsonl     row 181 alone (the source-ref refusal input)

Every file is written LF-pinned; one sha256 digest per file is printed.
Fixture content is FIXED (no clock, no uuid4): re-running this script must
reproduce byte-identical files.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
SCRIPTS = REPO / "skills" / "memory" / "scripts"

sys.path.insert(0, str(SCRIPTS))

ROW_180 = "00000000-0000-4000-8000-000000000180"
ROW_181 = "00000000-0000-4000-8000-000000000181"
FIXED_TS = "2026-09-10T00:00:00Z"

# The two fixed records shared by every importer fixture (issue #180).
RECORD_180 = {
    "id": ROW_180,
    "namespace": "user:global",
    "type": "fact",
    "content": "sshpass -p pw180A ssh host",
    "tags": "fixture",
    "source_ref": "file:fixture-safe",
    "signal": "test",
    "confidence": 0.9,
    "valid_from": FIXED_TS,
    "valid_until": "",
    "update_of": "",
    "taint": "untrusted_tool",
    "ingestion_ts": FIXED_TS,
    "superseded_at": "",
    "supersede_reason": "",
    "merged_from": "",
    "trust_score": 1.0,
    "applied_count": 0,
    "violated_count": 0,
}
RECORD_181 = dict(RECORD_180)
RECORD_181.update({
    "id": ROW_181,
    "content": "safe text",
    "source_ref": "sshpass -p pw180B",
})

# The issue's exact 12-positive / 12-negative pattern matrix. p11 carries REAL
# newlines and p02 REAL tabs, exactly as the issue specifies.
POSITIVE = [
    ("p01", "sshpass -p pw180A ssh host",
     "sshpass -p [REDACTED_SECRET] ssh host"),
    ("p02", "sshpass\t-p\tpw180B ssh host",
     "sshpass\t-p\t[REDACTED_SECRET] ssh host"),
    ("p03", "sudo -S -u deploy host", None),
    ("p04", "mysql --password=pw180D db",
     "mysql --password=[REDACTED_SECRET] db"),
    ("p05", "psql --password=pw180E db",
     "psql --password=[REDACTED_SECRET] db"),
    ("p06", "ssh -ppw180F host", "ssh -p[REDACTED_SECRET] host"),
    ("p07", "mysql -ppw180G db", "mysql -p[REDACTED_SECRET] db"),
    ("p08", "psql -ppw180H db", "psql -p[REDACTED_SECRET] db"),
    ("p09", "https://alice:pw180I@example.test/repo",
     "https://alice:[REDACTED_SECRET]@example.test/repo"),
    ("p10", "password: pw180J78", "password: [REDACTED_SECRET]"),
    ("p11", "-----BEGIN RSA PRIVATE KEY-----\nFAKE-KEY-180\n"
            "-----END RSA PRIVATE KEY-----", "[REDACTED_SECRET]"),
    ("p12", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
     "[REDACTED_SECRET]"),
]
NEGATIVE = [
    ("n01", "passwd entry"),
    ("n02", "sshpass -p"),
    ("n03", "sudo -s"),
    ("n04", "--password"),
    ("n05", "https://alice@example.test/repo"),
    ("n06", "curl -psecret https://host"),
    ("n07", "git -psecret host"),
    ("n08", "ssh -Psecret host"),
    ("n09", "ssh -p"),
    ("n10", "mysql --password db"),
    ("n11", "[REDACTED_SECRET]"),
    ("n12", "echo password"),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def write_text_lf(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def generate_patterns_json() -> None:
    doc = {"positive": [], "negative": []}
    for pid, text, expected in POSITIVE:
        doc["positive"].append({
            "id": pid,
            "input": text,
            "expected": expected,
            "outcome": "quarantine" if expected is None else "redacted",
        })
    for nid, text in NEGATIVE:
        doc["negative"].append({
            "id": nid,
            "input": text,
            "expected": text,
            "outcome": "unchanged",
        })
    write_text_lf(FIXTURE_DIR / "patterns.json",
                  json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


JSONL_KEY_ORDER = [
    "id", "namespace", "type", "content", "tags", "source_ref", "signal",
    "confidence", "valid_from", "valid_until", "update_of", "taint",
    "ingestion_ts", "superseded_at", "supersede_reason", "merged_from",
    "trust_score", "applied_count", "violated_count",
]


def compact(record: dict) -> str:
    ordered = {k: record[k] for k in JSONL_KEY_ORDER}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def generate_sshpass_jsonl() -> None:
    lines = [compact(RECORD_180), compact(RECORD_181)]
    write_text_lf(FIXTURE_DIR / "sshpass.jsonl", "\n".join(lines) + "\n")


def generate_quarantine_row_jsonl() -> None:
    write_text_lf(FIXTURE_DIR / "quarantine_row.jsonl", compact(RECORD_181) + "\n")


def generate_harvest_json() -> None:
    # Harvest rows carry the required keys (namespace/type/content/tags/
    # signal/why) plus each record's own source_ref so the harvest lane
    # discriminates exactly like the other four importers (plan review r2).
    rows = [
        {
            "namespace": RECORD_180["namespace"],
            "type": RECORD_180["type"],
            "content": RECORD_180["content"],
            "tags": RECORD_180["tags"],
            "signal": RECORD_180["signal"],
            "why": "issue 180 fixture row 180",
            "source_ref": RECORD_180["source_ref"],
        },
        {
            "namespace": RECORD_181["namespace"],
            "type": RECORD_181["type"],
            "content": RECORD_181["content"],
            "tags": RECORD_181["tags"],
            "signal": RECORD_181["signal"],
            "why": "issue 180 fixture row 181",
            "source_ref": RECORD_181["source_ref"],
        },
    ]
    write_text_lf(FIXTURE_DIR / "harvest.json",
                  json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def generate_legacy_sqlite() -> None:
    # Build a REAL store (the repo's own schema) holding exactly the two
    # fixed records, with fixed ids/timestamps (direct INSERT — a store.py add
    # would mint nondeterministic uuids). FTS triggers populate memory_fts;
    # memory_vec is left empty (embedding-less rows are the degraded-mode
    # norm). The store is checkpointed into DELETE journal mode so the file
    # is a single deterministic artifact.
    tmp = tempfile.mkdtemp(prefix="zmem180-fixsrc-")
    try:
        db_path = Path(tmp) / "store.sqlite"
        # Import AFTER the scratch env is pinned: storelib freezes STORE_PATH
        # at import time and must never see the operator's real store.
        os.environ["ZMEM_STORE"] = str(db_path)
        os.environ["ZMEM_DATA"] = tmp
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(tmp, "missing-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        if "storelib" in sys.modules:
            raise SystemExit("generate_inputs.py must run before any storelib import")
        from storelib.schema import init_db
        conn = sqlite3.connect(str(db_path))
        try:
            init_db(conn)
            for record in (RECORD_180, RECORD_181):
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags,"
                    " source_ref, source_hash, signal, confidence, valid_from,"
                    " taint, ingestion_ts, trust_score, applied_count,"
                    " violated_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record["id"], record["namespace"], record["type"],
                     record["content"], record["tags"], record["source_ref"],
                     "", record["signal"], record["confidence"],
                     record["valid_from"], record["taint"],
                     record["ingestion_ts"], record["trust_score"],
                     record["applied_count"], record["violated_count"]))
            conn.commit()
            # init_db stamps meta.created_at with the wall clock; pin it so
            # the fixture bytes are reproducible (digest-stable regeneration).
            conn.execute("UPDATE meta SET value=? WHERE key='created_at'",
                         (FIXED_TS,))
            conn.commit()
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copyfile(db_path, FIXTURE_DIR / "legacy-source.sqlite")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if REPO.resolve() != Path.cwd().resolve():
        print("run me from the repository root: "
              "python tests/fixtures/secrets/generate_inputs.py")
        return 2
    generate_patterns_json()
    generate_sshpass_jsonl()
    generate_quarantine_row_jsonl()
    generate_harvest_json()
    generate_legacy_sqlite()
    for name in ("patterns.json", "sshpass.jsonl", "legacy-source.sqlite",
                 "harvest.json", "quarantine_row.jsonl"):
        digest = sha256_of(FIXTURE_DIR / name)
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
