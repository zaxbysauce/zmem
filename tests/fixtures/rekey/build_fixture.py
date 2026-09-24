#!/usr/bin/env python3
"""Build the deterministic issue #168 rekey/reembed fixture.

The fixture deliberately uses source_ref prefixes as the only mapping input.
The prose also contains the bank name so tests can prove that no content or
namespace inference takes place.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"


def build(store_path: Path) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["ZMEM_STORE"] = str(store_path)
    os.environ["ZMEM_DATA"] = str(store_path.parent)
    os.environ["ZMEM_MODELS_DIR"] = str(store_path.parent / "no-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ.pop("ZMEM_EMBED_PROFILE", None)
    sys.path.insert(0, str(SCRIPTS))

    from storelib import schema  # noqa: E402
    from storelib.entity import relink_memory  # noqa: E402

    conn = schema.connect()
    schema._prepare_store(conn)
    dim = schema._active_vec0_dim()
    has_vec = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='memory_vec'"
    ).fetchone() is not None
    ids: list[str] = []
    groups = (
        ("db:spark-kb", 12),
        ("hermes-spark1", 5),
        ("other", 3),
    )
    row_number = 0
    try:
        for prefix, count in groups:
            for offset in range(1, count + 1):
                row_number += 1
                memory_id = f"fixture-{row_number:02d}"
                source_ref = f"{prefix}:lesson-{offset:02d}"
                content = (
                    f"Lesson {row_number} mentions SparkMemory and the bank "
                    f"name spark-kb; source {source_ref}."
                )
                conn.execute(
                    "INSERT INTO memory("
                    "id, namespace, type, content, tags, source_ref, "
                    "source_hash, confidence, signal, valid_from, "
                    "ingestion_ts, taint"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        "user:global",
                        "lesson",
                        content,
                        "tool:zvec entity:project:Spark",
                        source_ref,
                        f"fixture-hash-{row_number:02d}",
                        0.9,
                        "fixture",
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:00Z",
                        "trusted_internal",
                    ),
                )
                blob = struct.pack(
                    f"<{dim}f", *([row_number / 100.0] * dim)
                )
                conn.execute(
                    "UPDATE memory SET embedding=?, embedding_model=?, "
                    "embedded_at=? WHERE id=?",
                    (blob, "fixture", "2026-01-01T00:00:00Z", memory_id),
                )
                relink_memory(conn, memory_id)
                if has_vec:
                    conn.execute(
                        "INSERT INTO memory_vec(embedding, memory_id) "
                        "VALUES (?,?)",
                        (blob, memory_id),
                    )
                ids.append(memory_id)

        for src, dst, relation in (
            (ids[0], ids[1], "related"),
            (ids[1], ids[2], "supports"),
            (ids[12], ids[17], "updates"),
        ):
            conn.execute(
                "INSERT INTO memory_link(src_id,dst_id,relation,score,created_at) "
                "VALUES (?,?,?,?,?)",
                (src, dst, relation, 0.75, "2026-01-01T00:00:00Z"),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("store", type=Path)
    args = parser.parse_args()
    build(args.store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
