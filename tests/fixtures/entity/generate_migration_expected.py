"""Deterministic v15->v16 entity-migration fixture generator (issue #175).

CLI (from the repository root):

    python tests/fixtures/entity/generate_migration_expected.py \
        --input tests/fixtures/entity/migration-input.jsonl \
        --out   tests/fixtures/entity/migration-expected.json

Phases:
  full  (default) — write the five fixed records to --input, seed the isolated
                    v15 store at --store, trigger the migration through the
                    REAL production path (a writable ``store.py`` command),
                    and write --out with the PRE and POST canonical snapshots.
  seed            — stop after seeding: --store holds a canonicalized v15
                    store and --out receives the PRE snapshot (used by the
                    migration tests, which drive the migration themselves).
  dump            — snapshot the CURRENT --store into --out (used by the
                    tests after they trigger the migration).

Determinism: entity ids are uuid4() and timestamps now_iso() at derivation
time, so the seeding pass canonicalizes them — every entity id becomes
uuid5(NAMESPACE_URL, "zmem-entity-175:" + <its alphabetically-first alias_norm>),
applied coherently across entity / entity_alias / memory_entity, and every
entity timestamp is stamped to the fixed 2026-09-10T00:00:00Z. Two runs over
the same records produce byte-identical input and expected files (pinned by
the frozen C2 check and by MigrationTest in tests/test_entity.py).

Isolation: this module pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR to its own
scratch BEFORE importing storelib (STORE_PATH freezes at first import). It
must run as its own process (subprocess from the tests) — never import it into
a process that has already imported storelib under a different store path.
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
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

NAMESPACE = "fixture:entity-175"
FIXED_TS = "2026-09-10T00:00:00Z"
UUID5_NS_KEY = "zmem-entity-175:"

# The five fixed records (issue #175, Fixtures section). Content/tag pairs are
# exactly the issue's; ids are the fixed UUIDs; every row is type=fact.
RECORDS = [
    ("00000000-0000-4000-8000-000000000001", "person Alice", "person:Alice"),
    ("00000000-0000-4000-8000-000000000002", "project Acme", "project:Acme"),
    ("00000000-0000-4000-8000-000000000003", "tool ripgrep", "tool:ripgrep"),
    ("00000000-0000-4000-8000-000000000004", "preference concise", "preference:concise"),
    ("00000000-0000-4000-8000-000000000005", "other fallback", "other:fallback"),
]

# The LEGACY (v10..v15) entity DDL — the five-kind CHECK this migration
# replaces. Kept here as fixture data: the generator's job is to construct a
# faithful pre-migration store, which means the old constraint.
_LEGACY_ENTITY_DDL = """
CREATE TABLE entity_v15 (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL
        CHECK (kind IN ('person','project','tool','preference','other')),
    canonical_name TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""

# v17 (issue #178): the migration chain continues past the v16 entity rebuild
# and creates the additive `memory_occurrence` side table — the POST snapshot
# must record it (empty: the fixture rows are `fact`, and v17 backfills
# nothing). The PRE (v15) store predates the table, so `snapshot()` includes
# it only when sqlite_master knows it.
_TABLE_SORT_KEY = {
    "entity": lambda r: (r["id"],),
    "entity_alias": lambda r: (r["alias_norm"],),
    "memory_entity": lambda r: (r["memory_id"], r["entity_id"]),
    "memory_occurrence": lambda r: (r["memory_id"],),
}


def _pin_env(store_path: Path) -> None:
    os.environ["ZMEM_STORE"] = str(store_path)
    os.environ["ZMEM_DATA"] = str(store_path.parent)
    os.environ["ZMEM_MODELS_DIR"] = str(store_path.parent / "models-do-not-create")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
    sys.path.insert(0, str(SCRIPTS_DIR))


def write_input(path: Path) -> None:
    lines = []
    for mid, content, tags in RECORDS:
        rec = {
            "content": content,
            "id": mid,
            "ingestion_ts": FIXED_TS,
            "namespace": NAMESPACE,
            "tags": tags,
            "type": "fact",
        }
        lines.append(json.dumps(rec, sort_keys=True, separators=(",", ":")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def seed_v15_store(store_path: Path) -> None:
    """Build the store, then shape it into the canonicalized v15 fixture.

    Sequence: current-code init+migrate (all tables present), rebuild `entity`
    to the legacy five-kind CHECK, roll the marker to 15, insert the five
    memory rows and derive their entities through the REAL production
    extractor (storelib.entity.link_memory_entities), then canonicalize ids
    and timestamps for byte-determinism.
    """
    from storelib import schema
    from storelib.entity import link_memory_entities
    from schema_meta import normalize_content

    conn = schema.connect()
    try:
        schema.init_db(conn)
        schema.migrate(conn)

        # Roll the entity table back to the legacy five-kind v15 shape.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_LEGACY_ENTITY_DDL)
        conn.execute(
            "INSERT INTO entity_v15 (id, kind, canonical_name, created_at, "
            "updated_at) SELECT id, kind, canonical_name, created_at, "
            "updated_at FROM entity"
        )
        conn.execute("DROP TABLE entity")
        conn.execute("ALTER TABLE entity_v15 RENAME TO entity")
        # Faithfulness: init_db above created the CURRENT table set, so the
        # v17 `memory_occurrence` side table (issue #178) is present before
        # the rollback. A real v15 store has no such table — drop it, so the
        # POST snapshot's occurrence table is provably created by the v17
        # migration block, not inherited from the seeding pass.
        conn.execute("DROP TABLE IF EXISTS memory_occurrence")
        conn.execute(
            "UPDATE meta SET value='15' WHERE key='schema_version'"
        )
        conn.commit()

        # Seed the five memory rows and derive entities through the real path.
        for mid, content, tags in RECORDS:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags, "
                "confidence, signal, ingestion_ts, content_norm) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, NAMESPACE, "fact", content, tags, 0.6, "test", FIXED_TS,
                 normalize_content(content)),
            )
            link_memory_entities(
                conn, mid, content=content, tags=tags, namespace=NAMESPACE
            )
        conn.commit()

        # Deterministic canonicalization: stable uuid5 ids keyed by each
        # entity's alphabetically-first alias_norm (alias_norm is globally
        # unique, so the key is stable and unique per entity), applied
        # coherently across all three tables; fixed timestamps.
        aliases = conn.execute(
            "SELECT entity_id, alias_norm FROM entity_alias"
        ).fetchall()
        by_entity: dict[str, list[str]] = {}
        for row in aliases:
            by_entity.setdefault(row["entity_id"], []).append(row["alias_norm"])
        id_map = {
            old: str(uuid.uuid5(uuid.NAMESPACE_URL,
                                UUID5_NS_KEY + sorted(names)[0]))
            for old, names in by_entity.items()
        }
        conn.execute("BEGIN IMMEDIATE")
        for old, new in sorted(id_map.items()):
            conn.execute("UPDATE entity SET id=? WHERE id=?", (new, old))
            conn.execute(
                "UPDATE entity_alias SET entity_id=? WHERE entity_id=?", (new, old)
            )
            conn.execute(
                "UPDATE memory_entity SET entity_id=? WHERE entity_id=?", (new, old)
            )
        conn.execute(
            "UPDATE entity SET created_at=?, updated_at=?", (FIXED_TS, FIXED_TS)
        )
        conn.commit()
    finally:
        conn.close()


def snapshot(store_path: Path) -> dict:
    """Canonical PRE/POST snapshot of the three entity tables."""
    conn = sqlite3.connect(str(store_path))
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        # memory_occurrence exists only from v17 on: a faithful v15 PRE
        # snapshot must not fabricate it, a POST snapshot must not hide it.
        tables_present = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        tables = {}
        for table in ("entity", "entity_alias", "memory_entity",
                      "memory_occurrence"):
            if table not in tables_present:
                continue
            rows = [
                dict(r) for r in conn.execute(f"SELECT * FROM {table}")
            ]
            rows.sort(key=_TABLE_SORT_KEY[table])
            canon = [
                json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                for r in rows
            ]
            blob = "".join(canon).encode("utf-8")
            tables[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "rows": [line.rstrip("\n") for line in canon],
            }
        return {"schema_version": int(version), "tables": tables}
    finally:
        conn.close()


def trigger_migration(store_path: Path) -> None:
    """Trigger the v15->v16 migration the way every real client does."""
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(store_path)
    env["ZMEM_DATA"] = str(store_path.parent)
    env["ZMEM_MODELS_DIR"] = str(store_path.parent / "models-do-not-create")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
    r = subprocess.run(
        [sys.executable, str(STORE_PY), "stats"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    if r.returncode != 0:
        raise SystemExit(
            f"seed store migration trigger failed: exit {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--store", default=None)
    ap.add_argument("--phase", choices=("full", "seed", "dump"), default="full")
    args = ap.parse_args()

    if args.phase != "dump" and not args.input:
        ap.error("--input is required for --phase seed and --phase full")

    in_path = Path(args.input).resolve() if args.input else None
    out_path = Path(args.out).resolve()
    store_path = (
        Path(args.store).resolve() if args.store
        else in_path.parent / "store.sqlite"
    )

    if args.phase == "dump":
        payload = {"post": snapshot(store_path)}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
        return 0

    with tempfile.TemporaryDirectory(prefix="zmem-entity-175-") as td:
        # Always seed inside a scratch store, then (full phase) copy it to the
        # requested store path — repeated runs never inherit prior state.
        scratch_store = Path(td) / "store.sqlite"
        _pin_env(scratch_store)
        write_input(in_path)
        seed_v15_store(scratch_store)
        pre = snapshot(scratch_store)

        if args.phase == "seed":
            if store_path != scratch_store:
                import shutil
                store_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(scratch_store, store_path)
            payload = {"pre": pre}
        else:  # full
            trigger_migration(scratch_store)
            post = snapshot(scratch_store)
            if store_path != scratch_store:
                import shutil
                store_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(scratch_store, store_path)
            payload = {
                "input_sha256": hashlib.sha256(
                    in_path.read_bytes()
                ).hexdigest(),
                "pre": pre,
                "post": post,
            }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(
        f"[generate_migration_expected] phase={args.phase} "
        f"input_sha256={payload.get('input_sha256', '-')} "
        f"out={out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
