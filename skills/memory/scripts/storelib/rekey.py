"""Explicit source-reference namespace rekeying (issue #168)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
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


def _refuse_pointer_collisions(conn: sqlite3.Connection,
                               post_namespace_for) -> None:
    """SINGLE shared authority (PR1): refuse a set of namespace moves that
    would place two LIVE VERIFIED knowledge pointer rows for the same exact
    reserved source_ref into one namespace.

    ``post_namespace_for(row)`` returns the row's post-move namespace (its
    CURRENT namespace when the plan leaves it alone). Every namespace-move
    surface routes through this one implementation — rekey-namespace's map
    (entries), write.rekey_namespace (--from/--near-miss, incl. its
    automatic CLI route), and schema._rekey_namespaces (v5 migration +
    retry) — so the exact-ref verified-pointer uniqueness law cannot drift
    between them.

    Two verified rows for one ref may legitimately coexist while they sit
    in DIFFERENT namespaces (each namespace holds its own authoritative
    pointer); a move that concentrates them into one namespace manufactures
    exactly the duplicate get_pointer/link_pointer treat as unrecoverable
    store corruption (KnowledgePointerConflict on every later read or
    advance). Detected per (post-move namespace, source_ref) BEFORE the
    caller mutates anything, inside the caller's transaction, so a refusal
    leaves the store untouched. A collision that already exists statically
    (no row actually moves into it) is pre-existing corruption get_pointer
    already reports loudly — it is not introduced by this move. Unverified
    imported copies never count: they are inert by design and the handler's
    relink folds them.
    """
    # Lazy imports: rekey stays importable on stdlib-only hosts
    # (embeddings/sqlite-vec optional); only a move that actually touches
    # knowledge rows pays for the storelib.knowledge import chain.
    from storelib.write import KNOWLEDGE_SOURCE_REF_PREFIX
    from storelib.knowledge import parse_payload

    rows = conn.execute(
        "SELECT id, namespace, source_ref, content FROM memory "
        "WHERE superseded_at IS NULL AND source_ref LIKE ?",
        (KNOWLEDGE_SOURCE_REF_PREFIX + "%",),
    ).fetchall()
    # (post-move namespace, ref) -> [(row, moves_under_this_plan), ...] for
    # VERIFIED rows only.
    verified_groups: dict[tuple[str, str], list[tuple[sqlite3.Row, bool]]] = {}
    for row in rows:
        ref = row["source_ref"] or ""
        post_namespace = post_namespace_for(row)
        parsed = parse_payload(row["content"])
        if (parsed is None or not parsed["verified"]
                or parsed["logical_id"] != ref):
            continue  # inert/unverified copy — never authoritative
        verified_groups.setdefault((post_namespace, ref), []).append(
            (row, post_namespace != row["namespace"]))
    for (post_namespace, ref), members in verified_groups.items():
        if len(members) < 2 or not any(m for _, m in members):
            continue
        ids = ", ".join(sorted(r["id"] for r, _ in members))
        raise RuntimeError(
            "namespace rekey would place "
            f"{len(members)} live verified knowledge pointer rows for "
            f"source_ref {ref!r} into namespace {post_namespace!r} "
            f"(rows {ids}); get_pointer treats that as store corruption — "
            "move one of them elsewhere or relink the pointer first")


def _refuse_introduced_pointer_conflicts(conn: sqlite3.Connection,
                                         entries: list[tuple[str, str]]) -> None:
    """Map-form wrapper over :func:`_refuse_pointer_collisions`: the
    post-move namespace is the FIRST map entry whose prefix matches the
    row's source_ref (mirroring _classify_rows / the apply loop order)."""
    def _mapped_namespace(row) -> str:
        ref = row["source_ref"] or ""
        for prefix, target in entries:
            if ref.startswith(prefix):
                return target
        return row["namespace"]
    _refuse_pointer_collisions(conn, _mapped_namespace)


# ---------------------------------------------------------------------------
# FB-031: local coordinated rekey guard (knowledge sidecar coordination)
#
# A namespace move strands every knowledge publication durably in flight
# under the old namespace (journal identities, delivery queue entries and
# intents are all namespaced; the probe in round3-rekey-probe.md observed
# quarantine, orphan duplicate pointers and an eternal pending entry).
# The guard does NOT remap the sidecars (never auto-remap): it REFUSES
# the move while in-flight state exists, coordinating with concurrent
# writers through the sidecars' own OS byte locks.


class RekeyKnowledgeBusy(RuntimeError):
    """A namespace move is refused because knowledge publication state is
    durably in flight under a source or destination namespace — or the
    sidecars cannot be inspected (corrupt/locked/config-invalid), which
    also fails closed. Safe detail only: namespaces, states and counts;
    never note content."""


# Delivery-intent states that are SETTLED for rekey coordination: the
# publication reached a verdict (the workflow's own stake-spent set plus
# its terminal rejection states). Everything else — "staked", "pending",
# "queued", or anything unrecognized — is IN FLIGHT and blocks the move
# (fail closed on unknown states).
_INTENT_SETTLED_STATES = ("applied", "created", "noop", "advanced",
                          "rejected", "cas_stale", "conflict")


def store_dir_from_conn(conn: sqlite3.Connection) -> Path | None:
    """The file-backed store directory of an EXISTING connection (FB-031
    construction rule): the authoritative sidecar location derived from
    the connection's own ``main`` database via ``PRAGMA database_list``
    — never a second SQLite handle. ``None`` for an in-memory/URI store
    (no knowledge sidecars can exist for it)."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # (seq, name, file); index access works for tuple and Row factories.
        if row[1] == "main":
            name = str(row[2] or "")
            if not name or name.startswith("file:"):
                return None
            return Path(name).resolve().parent
    return None


def _open_sidecar_lock(path: Path, timeout_s: float) -> int:
    """Open and non-blockingly lock ONE persistent sidecar lock file —
    the ``knowledge.locking`` byte-lock discipline (the parent directory
    is created before the open, a live lock is never deleted, the kernel
    releases it when the holder exits). Raises
    :class:`RekeyKnowledgeBusy` when another writer (a capture, flush or
    status in flight) holds the OS lock past the deadline."""
    from knowledge.locking import try_lock_nb

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RekeyKnowledgeBusy(
            "knowledge sidecar lock file unavailable (%s): %r"
            % (exc.__class__.__name__, str(path))) from exc
    deadline = time.monotonic() + timeout_s
    try:
        while not try_lock_nb(fd):
            if time.monotonic() >= deadline:
                raise RekeyKnowledgeBusy(
                    "knowledge sidecar lock busy after %.1fs (another "
                    "writer — a capture, flush or status in flight — "
                    "holds the OS lock; it is released automatically when "
                    "that writer exits; retry the rekey once it finishes): "
                    "%r" % (timeout_s, str(path)))
            time.sleep(0.03)
    except BaseException:
        # The descriptor is owned by THIS attempt: the busy refusal and a
        # FATAL locking.LockError (REL-015) both release it.
        os.close(fd)
        raise
    return fd


class _KnowledgeRekeyCoordination:
    """The FB-031 refusal check over ONE locked sidecar snapshot.

    Constructed only by :func:`knowledge_rekey_fence` while BOTH sidecar
    locks are held, from the sidecars' own internal read helpers (the
    public methods would reacquire the locks this fence already holds).
    The snapshot is immutable for the fence's lifetime — no writer can
    mutate the files under the locks — so the checks do no I/O."""

    def __init__(self, store_dir: Path, journal_entries, queue, intents):
        self._store_dir = store_dir
        self._journal_entries = tuple(journal_entries)
        self._queue = tuple(queue)
        self._intents = dict(intents)

    def refuse_if_active(self, namespaces) -> None:
        """Refuse the move when any of ``namespaces`` (the move's source
        OR destination namespaces) carries durably in-flight knowledge
        state: a non-terminal journal entry, a queued delivery op, or a
        not-yet-settled delivery intent. Terminal-only history does not
        block; namespaces outside the checked set never block (scoped
        check). Raises :class:`RekeyKnowledgeBusy` BEFORE the caller
        mutates anything."""
        from knowledge.journal import TERMINAL_STATES

        checked = {str(n) for n in namespaces if str(n)}
        if not checked:
            return
        journal_hits: dict[str, list[str]] = {}
        for entry in self._journal_entries:
            if (entry.namespace in checked
                    and entry.state not in TERMINAL_STATES):
                journal_hits.setdefault(
                    entry.namespace, []).append(entry.state)
        queued_ops: dict[str, int] = {}
        for record in self._queue:
            if not isinstance(record, dict):
                continue
            ns = str(record.get("namespace") or "")
            if ns in checked:
                queued_ops[ns] = queued_ops.get(ns, 0) + 1
        intent_hits: dict[str, list[str]] = {}
        for key, record in self._intents.items():
            if not isinstance(record, dict):
                continue
            # set_intent always writes the namespace field; the key's
            # prefix before the first "|" is the pre-namespace fallback.
            ns = record.get("namespace")
            ns = str(ns) if ns is not None else str(key).split("|", 1)[0]
            state = str(record.get("state") or "unknown")
            if ns in checked and state not in _INTENT_SETTLED_STATES:
                intent_hits.setdefault(ns, []).append(state)
        if not (journal_hits or queued_ops or intent_hits):
            return
        parts = []
        for ns in sorted(checked):
            bits = []
            if ns in journal_hits:
                bits.append("%d active journal entr%s (%s)" % (
                    len(journal_hits[ns]),
                    "y" if len(journal_hits[ns]) == 1 else "ies",
                    ", ".join(sorted(set(journal_hits[ns])))))
            if ns in queued_ops:
                bits.append("%d queued delivery op%s" % (
                    queued_ops[ns], "" if queued_ops[ns] == 1 else "s"))
            if ns in intent_hits:
                bits.append("%d in-flight delivery intent%s (%s)" % (
                    len(intent_hits[ns]),
                    "" if len(intent_hits[ns]) == 1 else "s",
                    ", ".join(sorted(set(intent_hits[ns])))))
            if bits:
                parts.append("%s: %s" % (ns, "; ".join(bits)))
        raise RekeyKnowledgeBusy(
            "refusing namespace rekey: knowledge publication(s) durably "
            "in flight under namespace(s) %s — %s. Drive them to a "
            "terminal state (knowledge flush / knowledge status) or wait "
            "for the concurrent writer to finish, then retry. This is "
            "LOCAL coordination only: remote-client sidecars are "
            "invisible to this store, so a fleet-wide client drain is "
            "required before a namespace rekey."
            % (", ".join(sorted(checked)), "; ".join(parts)))


@contextlib.contextmanager
def knowledge_rekey_fence(store_dir, *, lock_timeout_s: float = 5.0):
    """FB-031 local coordinated rekey guard: hold the knowledge sidecar
    locks while a namespace move runs, and hand back the refusal check.

    Construction rules (every caller already owns the authoritative store
    directory and the EXISTING SQLite connection/transaction): the
    sidecar accessors are built DIRECTLY from
    ``<store_dir>/knowledge/{journal,delivery}.json`` — never
    ``default_journal()``/``default_journal_path()``/config-path
    resolution/workflow capture-flush/a second SQLite connection — and
    the files are read through the sidecars' internal read helpers under
    this fence's own locks, so no public method reacquires a lock the
    fence holds.

    Lock protocol:

    1. knowledge disabled AND no existing sidecars/locks — yield ``None``
       immediately and create NOTHING (no knowledge directory, no lock
       files): the default-off constraint.
    2. disabled but sidecars exist — still lock and inspect them: those
       are recovery records and must be coordinated.
    3. enabled (including first use, when no sidecar exists yet) — create
       and use the NORMAL persistent journal+delivery lock files, so a
       first-ever capture can never interleave with the guard.
    4. journal lock first, then delivery lock — one fixed order (the
       workflow only ever holds one sidecar lock at a time, so this
       cannot deadlock with a capture).
    5. under both locks the caller's :meth:`refuse_if_active` sees a
       consistent snapshot; corrupt/unreadable sidecars fail closed.
    6. both locks are held until the context exits — callers keep the
       fence open through their SQLite namespace COMMIT, so a capture
       can never observe pre-move state between the UPDATE and the
       commit.

    A busy sidecar lock past ``lock_timeout_s`` and an invalid knowledge
    config both raise :class:`RekeyKnowledgeBusy` (fail closed: safety
    cannot be established). Pure mapping helpers stay I/O-free — this
    fence wraps the namespace-move entry points, it is never called from
    the ``post_namespace_for`` lane.
    """
    from knowledge.config import KnowledgeConfigError, load_knowledge_config
    from knowledge.delivery import (
        DeliveryError, KnowledgeDelivery, delivery_path_for_store)
    from knowledge.journal import (
        JournalError, KnowledgeJournal, journal_path_for_store)

    if store_dir is None:
        # No file-backed store (in-memory/URI): no sidecars can exist.
        yield None
        return
    store = Path(store_dir)
    journal_path = journal_path_for_store(store)
    delivery_path = delivery_path_for_store(store)
    lock_paths = (journal_path.with_name(journal_path.name + ".lock"),
                  delivery_path.with_name(delivery_path.name + ".lock"))
    sidecars_present = any(path.exists() for path in
                           (journal_path, delivery_path, *lock_paths))
    config_file = store / "config.json"
    try:
        enabled = load_knowledge_config(config_file).enabled
    except KnowledgeConfigError as exc:
        raise RekeyKnowledgeBusy(
            "knowledge config %r is invalid, so in-flight knowledge state "
            "cannot be excluded; fix the config before rekeying (%s)"
            % (str(config_file), exc)) from exc
    if not enabled and not sidecars_present:
        # Default-off: no knowledge state and no locks anywhere — return
        # without creating the knowledge directory or any lock file.
        yield None
        return
    # Journal THEN delivery, one fixed order. The normal persistent lock
    # files are created here (enabled first use included, rule 3).
    journal_fd = _open_sidecar_lock(lock_paths[0], lock_timeout_s)
    try:
        delivery_fd = _open_sidecar_lock(lock_paths[1], lock_timeout_s)
    except BaseException:
        os.close(journal_fd)
        raise
    try:
        try:
            # Internal read helpers only (no lock reacquisition); a
            # missing file is the empty state, anything else fails
            # closed — an unreadable sidecar cannot be coordinated.
            entries = KnowledgeJournal(journal_path)._read_entries()
        except JournalError as exc:
            raise RekeyKnowledgeBusy(
                "knowledge journal %r is unreadable; operator review is "
                "required before any namespace rekey: %s"
                % (str(journal_path), exc)) from exc
        try:
            sidecar = KnowledgeDelivery(delivery_path)._read()
        except DeliveryError as exc:
            raise RekeyKnowledgeBusy(
                "knowledge delivery sidecar %r is unreadable; operator "
                "review is required before any namespace rekey: %s"
                % (str(delivery_path), exc)) from exc
        yield _KnowledgeRekeyCoordination(
            store, entries, sidecar["queue"], sidecar["intents"])
    finally:
        from knowledge.locking import unlock

        unlock(delivery_fd)
        os.close(delivery_fd)
        unlock(journal_fd)
        os.close(journal_fd)


def _map_coordination_namespaces(conn: sqlite3.Connection,
                                 entries: list[tuple[str, str]]) -> set[str]:
    """The namespaces a map apply must keep free of in-flight knowledge
    state (FB-031): every entry target (rows land there) plus every live
    namespace that actually loses rows to a target. Read-only, on the
    caller's existing connection."""
    checked = {target for _, target in entries}
    for prefix, target in entries:
        for row in conn.execute(
            "SELECT DISTINCT namespace FROM memory "
            "WHERE superseded_at IS NULL "
            "AND substr(COALESCE(source_ref, ''), 1, length(?))=? "
            "AND namespace<>?",
            (prefix, prefix, target),
        ):
            checked.add(row[0])
    return checked


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

    # FB-031 local coordinated rekey guard: the fence spans BEGIN..COMMIT
    # so both knowledge sidecar locks stay held through the namespace
    # commit, and the refusal fires BEFORE any SQLite mutation (an empty
    # rolled-back transaction would also be safe, but the check is free
    # here). Independent of preview: only the apply lane coordinates.
    try:
        with knowledge_rekey_fence(store_path.parent) as coordination:
            if coordination is not None:
                coordination.refuse_if_active(
                    _map_coordination_namespaces(conn, entries))
            try:
                conn.execute("BEGIN IMMEDIATE")
                # PR1 guard, BEFORE any census or move: a map that would concentrate
                # two live verified knowledge pointer rows for one exact ref into a
                # single namespace refuses here — nothing moves, the transaction
                # rolls back atomically, and the exit is the standard 1.
                _refuse_introduced_pointer_conflicts(conn, entries)
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
    except RekeyKnowledgeBusy as exc:
        print(f"[zmem] rekey-namespace: {exc}", file=sys.stderr)
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
