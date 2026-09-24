"""Explicit source-reference namespace rekeying (issue #168)."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from schema_meta import is_valid_namespace
from storelib.backup import (_ensure_backup_dir, _new_snapshot_path,
                             create_snapshot)
from storelib.entity import relink_memory
from storelib.schema import _commit


_MAP_LINE = re.compile(r'^"([^"\\]+)": "([^"\\]+)"$')


class MapError(ValueError):
    """The operator supplied map is outside the deliberately narrow grammar."""


def parse_namespace_map(path: str | Path) -> list[tuple[str, str]]:
    """Parse the documented two-column YAML subset without YAML coercions."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise MapError(f"cannot read map: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise MapError("map must be UTF-8 without a BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MapError("map must be valid UTF-8") from exc

    entries: list[tuple[str, str]] = []
    sources: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _MAP_LINE.fullmatch(line)
        if not match:
            raise MapError(f"map line {number} must be an unindented quoted pair")
        source, target = match.groups()
        if not source or not target:
            raise MapError(f"map line {number} has an empty source or target")
        if source in sources:
            raise MapError(f"map line {number} duplicates source prefix {source!r}")
        # schema_meta intentionally trims for normal interactive admission.  A
        # map is evidence, so its accepted spelling must already be canonical.
        if target != target.strip() or not is_valid_namespace(target):
            raise MapError(f"map line {number} has invalid target scope {target!r}")
        sources.add(source)
        entries.append((source, target))
    if not entries:
        raise MapError("map contains no entries")
    return entries


def open_readonly_store(store_path: Path, *, load_vec: bool = False) -> sqlite3.Connection:
    """Open an existing store with SQLite's no-create, no-write URI mode."""
    if not store_path.is_file():
        raise RuntimeError(f"store not found: {store_path}")
    # An immutable handle is the only SQLite mode that also promises not to
    # create a WAL shared-memory sidecar.  Refuse a hot WAL rather than report
    # a stale census or recover it by writing to the operator's store.
    if Path(f"{store_path}-wal").exists():
        raise RuntimeError("store has a WAL sidecar; checkpoint it before a read-only check")
    try:
        conn = sqlite3.connect(store_path.resolve().as_uri() + "?mode=ro&immutable=1",
                               uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if load_vec:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        return conn
    except Exception as exc:
        raise RuntimeError(f"cannot open existing store read-only: {exc}") from exc


def _classify_rows(rows, entries: list[tuple[str, str]]):
    """Classify live rows once, with source order as the precedence contract."""
    mapped: list[list[sqlite3.Row]] = [[] for _ in entries]
    unmapped: list[sqlite3.Row] = []
    for row in rows:
        source_ref = row["source_ref"] or ""
        for index, (prefix, _) in enumerate(entries):
            if source_ref.startswith(prefix):
                mapped[index].append(row)
                break
        else:
            unmapped.append(row)
    return mapped, unmapped


def preview_namespace_map(conn: sqlite3.Connection,
                          entries: list[tuple[str, str]]) -> int:
    rows = conn.execute(
        "SELECT id, source_ref, namespace FROM memory "
        "WHERE superseded_at IS NULL ORDER BY id"
    ).fetchall()
    mapped, unmapped = _classify_rows(rows, entries)
    for (prefix, target), rows_for_entry in zip(entries, mapped):
        print(f"{prefix} -> {target}: {len(rows_for_entry)}")
    print(f"unmapped: {len(unmapped)}")
    return 0


def embedding_census(conn: sqlite3.Connection, declared_dim: int) -> tuple[int, int, int]:
    """Return missing-vector, orphan-vector, and memory-blob dimension counts."""
    missing = conn.execute(
        "SELECT COUNT(*) FROM memory m WHERE m.superseded_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM memory_vec mv WHERE mv.memory_id=m.id)"
    ).fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM memory_vec mv WHERE NOT EXISTS "
        "(SELECT 1 FROM memory m WHERE m.id=mv.memory_id)"
    ).fetchone()[0]
    dimension = conn.execute(
        "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL "
        "AND embedding IS NOT NULL AND length(embedding) <> ?",
        (4 * declared_dim,),
    ).fetchone()[0]
    return int(missing), int(orphan), int(dimension)


def _namespace_census(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT namespace, COUNT(*) AS n FROM memory "
        "WHERE superseded_at IS NULL GROUP BY namespace ORDER BY namespace"
    ).fetchall()
    return ", ".join(f"{row['namespace']}={row['n']}" for row in rows)


def _decision_lines(entries: list[tuple[str, str]], mapped, unmapped_count: int,
                    snapshot_sha256: str) -> list[str]:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")
    return [
        f"[{stamp}] zmem-rekey kind=namespace source_ref_prefix={prefix} "
        f"target={target} mapped={len(rows_for_entry)} unmapped={unmapped_count} "
        f"snapshot_sha256={snapshot_sha256}"
        for (prefix, target), rows_for_entry in zip(entries, mapped)
    ]


def apply_namespace_map(conn: sqlite3.Connection, *, store_path: Path,
                        entries: list[tuple[str, str]]) -> int:
    """Snapshot then atomically rekey only the live IDs selected by ``entries``."""
    try:
        backup_dir = _ensure_backup_dir()
        snapshot = _new_snapshot_path(backup_dir, "store-")
        info = create_snapshot(store_path, snapshot)
        if Path(info["path"]).resolve() != snapshot.resolve():
            raise RuntimeError("snapshot verification returned an unexpected path")
        snapshot_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    except Exception as exc:
        print(f"[zmem] rekey-namespace: verified backup required: {exc}",
              file=os.sys.stderr)
        return 1

    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, source_ref, namespace FROM memory "
            "WHERE superseded_at IS NULL ORDER BY id"
        ).fetchall()
        mapped, unmapped = _classify_rows(rows, entries)
        before_census = _namespace_census(conn)
        moved_ids: list[str] = []
        for (_, target), rows_for_entry in zip(entries, mapped):
            for row in rows_for_entry:
                if row["namespace"] == target:
                    continue
                cur = conn.execute(
                    "UPDATE memory SET namespace=? WHERE id=? "
                    "AND superseded_at IS NULL AND namespace<>?",
                    (target, row["id"], target),
                )
                if cur.rowcount:
                    moved_ids.append(row["id"])
        for memory_id in moved_ids:
            relink_memory(conn, memory_id)

        # The same transaction must observe every selected row at its exact
        # target before a commit makes the derived FTS/entity state visible.
        for (_, target), rows_for_entry in zip(entries, mapped):
            for row in rows_for_entry:
                current = conn.execute(
                    "SELECT namespace FROM memory WHERE id=? AND superseded_at IS NULL",
                    (row["id"],),
                ).fetchone()
                if current is None or current["namespace"] != target:
                    raise RuntimeError(f"post-update census failed for {row['id']}")
        after_census = _namespace_census(conn)
        _commit(conn)
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        print(f"[zmem] rekey-namespace: transaction rolled back: {exc}",
              file=os.sys.stderr)
        return 1

    print(f"rekey-namespace map before: {before_census}")
    print(f"rekey-namespace map after: {after_census}")
    data_dir = Path(os.environ.get("ZMEM_DATA") or store_path.parent)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "zmem-decisions.log").open("a", encoding="utf-8", newline="\n") as fh:
            for line in _decision_lines(entries, mapped, len(unmapped), snapshot_sha256):
                fh.write(line + "\n")
    except OSError as exc:
        print("[zmem] rekey-namespace: committed, but decision log append failed: "
              f"{exc}", file=os.sys.stderr)
        return 1
    return 0
