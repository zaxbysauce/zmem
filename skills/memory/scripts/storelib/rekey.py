"""Explicit source-reference namespace rekeying (issue #168)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from schema_meta import is_valid_namespace
from storelib.backup import (_ensure_backup_dir, _new_snapshot_path,
                             create_snapshot)
from storelib.entity import relink_memory
from storelib.schema import SCHEMA_VERSION_KEY, _commit, _schema_compat


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
        if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
               or char.isspace() or char == "=" for char in source):
            raise MapError(
                f"map line {number} has control, whitespace, or field-separator "
                "characters in source prefix"
            )
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
    resolved_store = store_path.resolve()
    # An immutable handle is the only SQLite mode that also promises not to
    # create a WAL shared-memory sidecar.  Refuse a hot WAL rather than report
    # a stale census or recover it by writing to the operator's store.
    if Path(f"{resolved_store}-wal").exists():
        raise RuntimeError("store has a WAL sidecar; checkpoint it before a read-only check")
    try:
        conn = sqlite3.connect(resolved_store.as_uri() + "?mode=ro&immutable=1",
                               uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if load_vec:
            load_vec_extension(conn)
        return conn
    except Exception as exc:
        raise RuntimeError(f"cannot open existing store read-only: {exc}") from exc


def load_vec_extension(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec only after read-only schema admission succeeds."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception as exc:
        raise RuntimeError(f"cannot load sqlite-vec: {exc}") from exc


def load_vec_extension_if_available(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec for invariant checks when its Python package is present.

    The standard library-only CLI remains usable on hosts without the optional
    embeddings dependency. Other extension/load failures remain fatal.
    """
    try:
        load_vec_extension(conn)
    except RuntimeError as exc:
        if isinstance(exc.__cause__, ImportError):
            return False
        raise
    return True


def assert_store_schema_compatible(conn: sqlite3.Connection, store_path: Path) -> None:
    """Apply the normal forward-schema gate through an already-open handle.

    Map preview and ``reembed --check`` must not open a second SQLite handle:
    that second probe may recover a WAL or observe a different snapshot.  The
    immutable connection that is about to answer the request is the evidence
    source for this gate.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_VERSION_KEY,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read store schema version: {exc}") from exc
    if row is None:
        return
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"zmem: store {store_path} has a non-integer schema_version {row[0]!r}; "
            "refusing to operate on it"
        ) from exc
    _schema_compat(version, store_path)


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
    """Return missing-vector, orphan-vector, and memory-blob dimension counts.

    One statement pins all three values to one immutable snapshot and avoids
    three independent Python round trips over the store.
    """
    row = conn.execute(
        "WITH live_memory AS ("
        " SELECT id, embedding, EXISTS(SELECT 1 FROM memory_vec mv "
        " WHERE mv.memory_id=memory.id) AS has_vector "
        " FROM memory WHERE superseded_at IS NULL"
        ") SELECT "
        " COALESCE(SUM(CASE WHEN has_vector=0 THEN 1 ELSE 0 END), 0), "
        " (SELECT COUNT(*) FROM memory_vec mv WHERE NOT EXISTS "
        "  (SELECT 1 FROM memory m WHERE m.id=mv.memory_id)), "
        " COALESCE(SUM(CASE WHEN embedding IS NOT NULL "
        "  AND length(embedding)<>? THEN 1 ELSE 0 END), 0) "
        "FROM live_memory",
        (4 * declared_dim,),
    ).fetchone()
    return tuple(int(value) for value in row)


def _namespace_census(conn: sqlite3.Connection, *, entries: list[tuple[str, str]],
                      mapped_counts: list[dict[str, int]],
                      unmapped_count: dict[str, int]) -> dict[str, object]:
    """Return a stable, structured census without re-scanning map candidates."""
    rows = conn.execute(
        "SELECT namespace, COUNT(*) AS total, "
        "SUM(CASE WHEN superseded_at IS NULL THEN 1 ELSE 0 END) AS live "
        "FROM memory GROUP BY namespace ORDER BY namespace"
    ).fetchall()
    totals = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN superseded_at IS NULL THEN 1 ELSE 0 END) AS live "
        "FROM memory"
    ).fetchone()
    return {
        "total": int(totals["total"]),
        "live": int(totals["live"] or 0),
        "by_namespace": [
            {"namespace": row["namespace"], "total": int(row["total"]),
             "live": int(row["live"] or 0)}
            for row in rows
        ],
        "by_prefix": [
            {"source_ref_prefix": prefix, "target": target,
             "total": count["total"], "live": count["live"],
             "moved": count["moved"]}
            for (prefix, target), count in zip(entries, mapped_counts)
        ],
        "unmapped": dict(unmapped_count),
    }


def _stream_sha256(path: Path) -> str:
    """Hash backups incrementally so a large verified snapshot is not retained."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _derived_identity_digest(conn: sqlite3.Connection) -> str:
    """Hash identities that a namespace move must never alter.

    The namespace update is allowed to refresh FTS/entity projections.  Vector
    bytes and link endpoints, however, are the same graph facts before and
    after rekeying.  Keep this check inside the transaction so an accidental
    relinker expansion rolls the whole migration back.
    """
    digest = hashlib.sha256()
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        )
    }
    if "memory_vec" in tables:
        try:
            for row in conn.execute(
                "SELECT memory_id, embedding FROM memory_vec ORDER BY memory_id"
            ):
                digest.update(row[0].encode("utf-8"))
                digest.update(bytes(row[1]))
        except sqlite3.OperationalError as exc:
            # Normal non-embedding CI deliberately lacks sqlite-vec.  The
            # transaction remains safe there; the embedding job exercises the
            # actual vector-byte assertion with the extension loaded.
            if "no such module: vec0" not in str(exc).lower():
                raise
            digest.update(b"memory_vec-unavailable")
    if "memory_link" in tables:
        for row in conn.execute(
            "SELECT src_id, dst_id FROM memory_link ORDER BY src_id, dst_id"
        ):
            digest.update(row[0].encode("utf-8"))
            digest.update(row[1].encode("utf-8"))
    return digest.hexdigest()


def _selected_rows_are_at_targets(conn: sqlite3.Connection,
                                  entries: list[tuple[str, str]]) -> None:
    """Assert the map's first-prefix contract with aggregate checks only."""
    prior: list[str] = []
    for prefix, target in entries:
        conditions = ["superseded_at IS NULL", "substr(COALESCE(source_ref, ''), 1, length(?))=?"]
        params: list[str] = [prefix, prefix]
        for earlier in prior:
            conditions.append("substr(COALESCE(source_ref, ''), 1, length(?))<>?")
            params.extend((earlier, earlier))
        conditions.append("namespace<>?")
        params.append(target)
        row = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE " + " AND ".join(conditions), params
        ).fetchone()
        if int(row[0]):
            raise RuntimeError(f"post-update census failed for source prefix {prefix!r}")
        prior.append(prefix)


def _decision_lines(entries: list[tuple[str, str]],
                    mapped_counts: list[dict[str, int]], unmapped_count: int,
                    snapshot_sha256: str) -> list[str]:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")
    return [
        f"[{stamp}] zmem-rekey kind=namespace source_ref_prefix={prefix} "
        f"target={quote(target, safe=':/._-')} "
        f"matched={count['live']} moved={count['moved']} "
        f"unmapped={unmapped_count} "
        f"snapshot_sha256={snapshot_sha256}"
        for (prefix, target), count in zip(entries, mapped_counts)
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
        snapshot_sha256 = _stream_sha256(snapshot)
    except Exception as exc:
        print(f"[zmem] rekey-namespace: verified backup required: {exc}",
              file=sys.stderr)
        return 1

    try:
        conn.execute("BEGIN IMMEDIATE")
        identity_before = _derived_identity_digest(conn)
        mapped_counts = [{"total": 0, "live": 0, "moved": 0}
                         for _ in entries]
        unmapped_count = {"total": 0, "live": 0}
        before_census = _namespace_census(
            conn, entries=entries, mapped_counts=mapped_counts,
            unmapped_count=unmapped_count,
        )
        last_id = ""
        while True:
            rows = conn.execute(
                "SELECT id, source_ref, namespace, superseded_at FROM memory "
                "WHERE id>? ORDER BY id LIMIT 256",
                (last_id,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                source_ref = row["source_ref"] or ""
                target = None
                for index, (prefix, candidate) in enumerate(entries):
                    if source_ref.startswith(prefix):
                        mapped_counts[index]["total"] += 1
                        if row["superseded_at"] is None:
                            mapped_counts[index]["live"] += 1
                        target = candidate
                        break
                if target is None:
                    unmapped_count["total"] += 1
                    if row["superseded_at"] is None:
                        unmapped_count["live"] += 1
                    continue
                if row["superseded_at"] is not None:
                    continue
                if row["namespace"] == target:
                    continue
                cur = conn.execute(
                    "UPDATE memory SET namespace=? WHERE id=? "
                    "AND superseded_at IS NULL AND namespace<>?",
                    (target, row["id"], target),
                )
                if cur.rowcount:
                    mapped_counts[index]["moved"] += 1
                    relink_memory(conn, row["id"])
        before_census["by_prefix"] = [
            {"source_ref_prefix": prefix, "target": target,
             "total": count["total"], "live": count["live"],
             "moved": 0}
            for (prefix, target), count in zip(entries, mapped_counts)
        ]
        before_census["unmapped"] = dict(unmapped_count)
        _selected_rows_are_at_targets(conn, entries)
        if _derived_identity_digest(conn) != identity_before:
            raise RuntimeError("namespace rekey changed vector bytes or link endpoints")
        after_census = _namespace_census(
            conn, entries=entries, mapped_counts=mapped_counts,
            unmapped_count=unmapped_count,
        )
        _commit(conn)
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        print(f"[zmem] rekey-namespace: transaction rolled back: {exc}",
              file=sys.stderr)
        return 1

    print("rekey-namespace map before: " + json.dumps(
        before_census, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    print("rekey-namespace map after: " + json.dumps(
        after_census, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    data_dir = Path(os.environ.get("ZMEM_DATA") or store_path.parent)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "zmem-decisions.log").open("a", encoding="utf-8", newline="\n") as fh:
            for line in _decision_lines(
                entries, mapped_counts,
                unmapped_count["live"], snapshot_sha256,
            ):
                fh.write(line + "\n")
    except OSError as exc:
        print("[zmem] rekey-namespace: committed, but decision log append failed: "
              f"{exc}; map changes are committed", file=sys.stderr)
        return 3
    return 0
