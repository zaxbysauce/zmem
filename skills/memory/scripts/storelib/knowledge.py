"""Knowledge-companion pointer state (PR1, plan 2026-11-13).

One authoritative zmem fact row per logical knowledge reference. The row is
addressed by a RESERVED ``source_ref`` — ``knowledge:<target>:<key>`` — and
carries a closed, versioned JSON payload in ``content``:

    {"date": "<ISO-Z>", "fullhash": ["8 hex" x 8], "logical_id": "<ref>",
     "locator": "<key>/<segmented-digest>.md", "summary": "...",
     "verified": true, "version": N}

Grammar alignment (parent direction, 2026-11-13): the ref grammar, the
logical-key/target-id validators, and the deterministic immutable locator
are OWNED by the knowledge package — :func:`knowledge.documents.logical_ref`,
:func:`knowledge.documents.validate_logical_key`,
:func:`knowledge.config.validate_target_id`,
:func:`knowledge.documents.version_locator`. This module imports them
lazily and never restates their regexes, so the handler grammar cannot
diverge from the companion package. ``logical_id`` in the payload IS the
full reserved ref (self-describing on export); the locator is DERIVED from
(key, digest) and a caller-supplied locator must match it byte-for-byte.

Design laws (approved plan, "Required critic corrections" +
"Explicit preimplementation refinements"):

- Stable logical identity is SEPARATE from the immutable version/digest.
  Advancing the pointer is a compare-and-swap on the authoritative zmem
  SQLite store (BEGIN IMMEDIATE / nested SAVEPOINT here), NEVER a KB API
  call and never a read-then-write emulation. The integer ``version`` is
  kept as the CAS token — callers advance with current+1 and retry on CAS
  rejection.
- The 64-hex SHA-256 is stored as eight 8-hex chunks so no >=32-hex run
  exists in the payload text — the generic secret pattern
  (``[0-9a-fA-F]{32,}``) can never trip on (or redact) a pointer payload.
  (The locator's own 16-hex groups mirror the package's scanner-safe
  ``segment_digest``.)
- Only THIS module mints ``verified: true`` payload rows, through
  ``add_memory`` with the private module capability — an identity-compared
  OBJECT registered at import, never a bool/flag any serialized surface can
  carry. Every ordinary writer surface refuses the reserved prefix
  outright (write.py guards), and every import path (sync ingest,
  import-store) demotes the marker to ``false``: an externally supplied
  reference survives a roundtrip only as UNVERIFIED, and re-verification
  requires an explicit relink/readback through this handler.
- Pointer rows are ordinary ``fact`` rows: no new table, type, column, or
  schema version. They carry ``taint=untrusted_tool`` (local and remote
  knowledge is tool-sourced; no trust bonus for being local) and are
  excluded from every near-duplicate selection surface (write-time dedup,
  consolidate, organize, write-time link neighbors) by the reserved prefix
  — a normal fact can never merge into a pointer and a pointer never folds
  an incoming row.
"""

from __future__ import annotations

import datetime as _datetime
import json
import re
import sqlite3

from storelib.schema import now_iso
from storelib import write as _write
from storelib.write import (
    KNOWLEDGE_SOURCE_REF_PREFIX,
    KnowledgePointerRefusal,  # noqa: F401  (re-export: sync/ops catch this)
)
from redaction import redact_secret_like_text

__all__ = [
    "KNOWLEDGE_SOURCE_REF_PREFIX",
    "KnowledgeError",
    "KnowledgeRefError",
    "KnowledgePayloadError",
    "KnowledgePointerConflict",
    "KnowledgePointerCasError",
    "KnowledgePointerRefusal",
    "POINTER_TYPE",
    "POINTER_TAG",
    "POINTER_TAINT",
    "KnowledgePointer",
    "make_source_ref",
    "parse_ref",
    "is_pointer_ref",
    "is_wellformed_pointer_ref",
    "validate_pointer_fields",
    "build_payload",
    "parse_payload",
    "demote_imported_payload",
    "get_pointer",
    "get_pointer_rows",
    "link_pointer",
    "quarantine_pointer_row",
]

# The pointer row is a fact row — no new type (plan: "no new table, metadata
# column, or schema version").
POINTER_TYPE = "fact"
POINTER_TAG = "knowledge"
# Local and remote knowledge is tool-sourced state: pointer rows take the
# untrusted_tool fence exactly like every other network/tool-fed row, with
# no trust bonus for a local provider (local/remote taint parity).
POINTER_TAINT = "untrusted_tool"

MAX_SUMMARY_CHARS = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_CHUNK_RE = re.compile(r"^[0-9a-f]{8}$")
# 64 hex as 8 chunks of 8: every chunk is far below the 32-hex secret
# pattern threshold, and the JSON separators between chunks break the run.
HASH_CHUNKS = 8
HASH_CHUNK_LEN = 8

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Closed payload field set — exactly these keys, exactly these types.
_PAYLOAD_FIELDS = frozenset(
    {"date", "fullhash", "logical_id", "locator", "summary", "verified", "version"}
)


# --- exceptions ------------------------------------------------------------
# ValueError subclasses so the existing CLI / ops error mapping (exit 2,
# rejected receipt) applies unchanged.
class KnowledgeError(ValueError):
    """Base class for knowledge-pointer refusals."""


class KnowledgeRefError(KnowledgeError):
    """Malformed reserved ref / component (grammar violation)."""


class KnowledgePayloadError(KnowledgeError):
    """Malformed pointer payload input (bounds, types, digest shape)."""


class KnowledgePointerConflict(KnowledgeError):
    """An immutability conflict (same version bound to a different digest,
    an identity inconsistency, or corrupt duplicate verified rows)."""


class KnowledgePointerCasError(KnowledgeError):
    """A stale/lost compare-and-swap: the caller's expected_row_id is not
    the live verified row (or an initial create found one)."""


# --- internal capability ---------------------------------------------------
# The single object add_memory identity-compares against. It is handed to
# write.py once at import; it is a module-private OBJECT, not a bool, flag,
# or string — no CLI argument, ops payload, or JSONL row can carry it, so
# the reserved knowledge: prefix is unforgeable from any serialized writer
# surface. Only knowledge.link_pointer's code path passes it.
_KNOWLEDGE_WRITE_CAPABILITY = object()
_write._register_knowledge_capability(_KNOWLEDGE_WRITE_CAPABILITY)


# --- package-owned grammar (lazy: never restate documents/config regexes) --
def _documents():
    from knowledge import documents

    return documents


def _config():
    from knowledge import config

    return config


def make_source_ref(target: str, key: str) -> str:
    """``knowledge:<target>:<key>`` via the package authority
    (knowledge.documents.logical_ref — target id, logical key, and total
    length all validated there). Raises KnowledgeRefError on any violation
    (the package's DocumentRejected is a ValueError; the handler contract
    narrows it to this type)."""
    try:
        return _documents().logical_ref(target, key)
    except ValueError as exc:
        raise KnowledgeRefError(str(exc)) from exc


def validate_pointer_components(*, target: str, key: str) -> tuple[str, str]:
    """Validate (target, key) through the package validators; returns them
    unchanged. Raises KnowledgeRefError."""
    try:
        tid = _config().validate_target_id(target)
        lkey = _documents().validate_logical_key(key)
    except ValueError as exc:
        raise KnowledgeRefError(str(exc)) from exc
    return tid, lkey


def is_pointer_ref(source_ref: str | None) -> bool:
    """Cheap prefix test — the same predicate every write-path guard uses."""
    return bool(source_ref) and source_ref.startswith(KNOWLEDGE_SOURCE_REF_PREFIX)


def is_wellformed_pointer_ref(source_ref: str | None) -> bool:
    """Full-grammar test without raising (import-side triage)."""
    if not is_pointer_ref(source_ref):
        return False
    rest = source_ref[len(KNOWLEDGE_SOURCE_REF_PREFIX):]
    target, sep, key = rest.partition(":")
    if not sep or not key or ":" in key:
        return False
    try:
        validate_pointer_components(target=target, key=key)
        canonical = make_source_ref(target, key)
    except KnowledgeRefError:
        return False
    return source_ref == canonical


def parse_ref(source_ref: str) -> tuple[str, str]:
    """Split a well-formed reserved ref into (target, key)."""
    if not is_wellformed_pointer_ref(source_ref):
        raise KnowledgeRefError(
            f"not a well-formed knowledge ref: {source_ref!r} "
            f"(expected knowledge:<target>:<key> per knowledge.documents)"
        )
    rest = source_ref[len(KNOWLEDGE_SOURCE_REF_PREFIX):]
    target, _, key = rest.partition(":")
    return target, key


# --- payload codec ---------------------------------------------------------
def _valid_date(value: object) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        _datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _derived_locator(key: str, full_sha256: str) -> str:
    """The deterministic immutable locator, derived through the package
    authority (knowledge.documents.version_locator) so handler and
    companion can never disagree on its bytes."""
    return _documents().version_locator(key, full_sha256)


def validate_pointer_fields(
    *, target: str, key: str, version: int, full_sha256: str,
    summary: str, date: str | None = None, locator: str | None = None,
) -> dict:
    """Validate pointer fields against the closed bounds, returning the
    normalized dict (logical_id = the full ref; locator derived from
    (key, digest); date defaulted to now when omitted). Raises
    KnowledgePayloadError / KnowledgeRefError on any violation."""
    target, key = validate_pointer_components(target=target, key=key)
    source_ref = make_source_ref(target, key)
    derived = _derived_locator(key, full_sha256)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise KnowledgePayloadError(
            f"version must be an integer >= 1 (got {version!r})"
        )
    if not isinstance(full_sha256, str) or not _SHA256_RE.match(full_sha256):
        raise KnowledgePayloadError(
            "full_sha256 must be exactly 64 lowercase hex chars "
            f"(got {full_sha256!r})"
        )
    if locator is not None and locator != derived:
        raise KnowledgePayloadError(
            f"locator {locator!r} does not match the deterministic immutable "
            f"locator for this key/digest ({derived!r}); the handler records "
            "knowledge.documents.version_locator(key, digest) only"
        )
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
        raise KnowledgePayloadError(
            f"summary must be a non-empty string of at most "
            f"{MAX_SUMMARY_CHARS} chars"
        )
    # Summary screen (PR1 review round): the summary is authored text that
    # rides the pointer payload into the store — and link_pointer writes via
    # capture_mode="manual", which is advisory-only, so nothing downstream
    # redacts or refuses it. The document BODY is screened at admission
    # (documents.canonical_content rejects any detection); the summary must
    # clear the SAME real capture-pattern scanner before it can persist.
    # A detection REJECTS (rejection over silent redaction — the payload's
    # summary is exact authored state, and a scrubbed copy would desync from
    # the source store). The message carries the count only, never the
    # value. The chunked digest form keeps the payload's own bytes
    # scanner-safe, so a hit here is always summary text.
    _scr_summary, summary_hits = redact_secret_like_text(summary)
    if summary_hits:
        raise KnowledgePayloadError(
            f"summary rejected: secret-like pattern detected "
            f"({summary_hits} hit(s)); a pointer summary is refused rather "
            "than silently altered or stored unredacted"
        )
    if date is None:
        date = now_iso()
    if not _valid_date(date):
        raise KnowledgePayloadError(
            f"date must be a canonical UTC timestamp YYYY-MM-DDTHH:MM:SSZ "
            f"(got {date!r})"
        )
    return {
        "logical_id": source_ref,
        "version": version,
        "locator": derived,
        "full_sha256": full_sha256,
        "summary": summary,
        "date": date,
    }


def _hash_to_chunks(full_sha256: str) -> list[str]:
    return [
        full_sha256[i * HASH_CHUNK_LEN:(i + 1) * HASH_CHUNK_LEN]
        for i in range(HASH_CHUNKS)
    ]


def _chunks_to_hash(chunks: object) -> str | None:
    if not isinstance(chunks, list) or len(chunks) != HASH_CHUNKS:
        return None
    if not all(isinstance(c, str) and _HEX_CHUNK_RE.match(c) for c in chunks):
        return None
    joined = "".join(chunks)
    return joined if _SHA256_RE.match(joined) else None


def build_payload(
    *, target: str, key: str, version: int, full_sha256: str,
    summary: str, date: str | None = None, verified: bool = True,
    locator: str | None = None,
) -> str:
    """Canonical payload JSON (sorted keys, compact, ASCII-safe). The
    digest is stored as eight 8-hex chunks; the locator is derived through
    the package authority; ``logical_id`` is the full ref.

    Review delta-4: the FULL serialized payload is screened with the real
    capture detector before it can reach any local write — the chunked
    grammar keeps every legitimate payload scanner-clean (verified by
    tests), so any hit is a regression or a bypassed admission (e.g. a
    credential-keyword ref riding a short-key shape) and the build
    refuses with a count-only message rather than letting a secret-shaped
    pattern enter the raw JSON that auto-mode ingest would then mangle.
    """
    fields = validate_pointer_fields(
        target=target, key=key, version=version, full_sha256=full_sha256,
        summary=summary, date=date, locator=locator,
    )
    payload = {
        "date": fields["date"],
        "fullhash": _hash_to_chunks(fields["full_sha256"]),
        "logical_id": fields["logical_id"],
        "locator": fields["locator"],
        "summary": fields["summary"],
        "verified": bool(verified),
        "version": fields["version"],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    _redacted_payload, payload_hits = redact_secret_like_text(text)
    if payload_hits:
        raise KnowledgePayloadError(
            f"pointer payload rejected: secret-like pattern detected "
            f"({payload_hits} hit(s)); the canonical payload must stay "
            "scanner-clean in every capture mode"
        )
    return text


def parse_payload(content: str | None) -> dict | None:
    """Parse stored content as a pointer payload. Returns the parsed fields
    (with ``full_sha256`` reassembled from the chunks) or None when the
    content is not a valid CLOSED-SET payload — lenient by design: stored
    bytes are data, not caller input, and an unparseable row is simply
    never authoritative. Self-consistency IS enforced where it is cheap and
    total: the locator must parse (package grammar) and agree with the
    payload's own digest and logical key."""
    if not isinstance(content, str) or not content:
        return None
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _PAYLOAD_FIELDS:
        return None
    full_sha256 = _chunks_to_hash(obj["fullhash"])
    if full_sha256 is None:
        return None
    verified = obj["verified"]
    version = obj["version"]
    if not isinstance(verified, bool):
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    logical_id = obj["logical_id"]
    locator = obj["locator"]
    summary = obj["summary"]
    date = obj["date"]
    if not is_wellformed_pointer_ref(logical_id):
        return None
    try:
        key, loc_digest = _documents().parse_locator(locator)
        _, ref_key = parse_ref(logical_id)
    except (KnowledgeRefError, ValueError):
        return None
    if key != ref_key or loc_digest != full_sha256:
        return None
    try:
        fields = validate_pointer_fields(
            target=parse_ref(logical_id)[0], key=ref_key, version=version,
            full_sha256=full_sha256, summary=summary, date=date,
        )
    except (KnowledgePayloadError, KnowledgeRefError):
        return None
    fields["verified"] = verified
    return fields


def demote_imported_payload(content: str | None) -> str | None:
    """Re-emit a parsed pointer payload with ``verified`` forced to false.

    The import-side half of the no-forgery law: an externally supplied
    reference is preserved through a sync/import roundtrip but NEVER newly
    verified — the marker survives only in rows this handler minted.
    Returns None when the content is not a verifiable pointer payload
    (unparseable rows stay as-is; they can never be authoritative because
    get_pointer requires a parsed, verified payload whose logical_id
    matches the row's own source_ref).
    """
    parsed = parse_payload(content)
    if parsed is None or not parsed["verified"]:
        return None
    _, ref_key = parse_ref(parsed["logical_id"])
    return build_payload(
        target=parse_ref(parsed["logical_id"])[0], key=ref_key,
        version=parsed["version"], full_sha256=parsed["full_sha256"],
        summary=parsed["summary"], date=parsed["date"], verified=False,
    )


# --- reads -----------------------------------------------------------------
class KnowledgePointer:
    """The authoritative (verified, live) pointer for one ref."""

    __slots__ = (
        "row_id", "namespace", "target", "key", "source_ref", "logical_id",
        "version", "locator", "full_sha256", "summary", "date",
    )

    def __init__(self, *, row_id: str, namespace: str, target: str, key: str,
                 fields: dict) -> None:
        self.row_id = row_id
        self.namespace = namespace
        self.target = target
        self.key = key
        self.source_ref = make_source_ref(target, key)
        self.logical_id = fields["logical_id"]
        self.version = fields["version"]
        self.locator = fields["locator"]
        self.full_sha256 = fields["full_sha256"]
        self.summary = fields["summary"]
        self.date = fields["date"]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"KnowledgePointer(row_id={self.row_id!r}, "
            f"source_ref={self.source_ref!r}, version={self.version})"
        )


def _live_rows_for_ref(
    conn: sqlite3.Connection, namespace: str, source_ref: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, content, source_ref, ingestion_ts FROM memory "
        "WHERE namespace=? AND source_ref=? AND superseded_at IS NULL "
        "ORDER BY ingestion_ts, id",
        (namespace, source_ref),
    ).fetchall()


def get_pointer(
    conn: sqlite3.Connection, namespace: str, target: str, key: str,
) -> KnowledgePointer | None:
    """The authoritative pointer for (namespace, target, key), or None.

    Exact source_ref match within the namespace, live rows only, and only
    rows whose payload parses, carries the handler-minted verified marker,
    AND whose payload logical_id equals the row's own source_ref (a
    surgery-mismatched row is not authoritative). More than one such row
    is store corruption and raises KnowledgePointerConflict rather than
    silently picking a winner. Callers validate namespace and capabilities
    BEFORE this lookup (ops-side admission); the ref match is
    namespace-scoped so foreign namespaces never leak.
    """
    source_ref = make_source_ref(target, key)
    verified: list[tuple[sqlite3.Row, dict]] = []
    for row in _live_rows_for_ref(conn, namespace, source_ref):
        parsed = parse_payload(row["content"])
        if (parsed is not None and parsed["verified"]
                and parsed["logical_id"] == row["source_ref"]):
            verified.append((row, parsed))
    if len(verified) > 1:
        raise KnowledgePointerConflict(
            f"{len(verified)} live verified pointer rows share source_ref "
            f"{source_ref!r} in namespace {namespace!r} "
            f"({', '.join(r['id'] for r, _ in verified)}); refusing to pick "
            "a winner — resolve the corruption deliberately"
        )
    if not verified:
        return None
    row, fields = verified[0]
    return KnowledgePointer(
        row_id=row["id"], namespace=namespace, target=target, key=key,
        fields=fields,
    )


def get_pointer_rows(
    conn: sqlite3.Connection, namespace: str, target: str, key: str,
) -> list[dict]:
    """Every LIVE row carrying this ref — verified and unverified alike —
    for relink/reporting UX. Unverified rows are imported copies whose
    reference was preserved but never (re)verified."""
    source_ref = make_source_ref(target, key)
    out = []
    for row in _live_rows_for_ref(conn, namespace, source_ref):
        parsed = parse_payload(row["content"])
        out.append({
            "row_id": row["id"],
            "verified": bool(
                parsed is not None and parsed["verified"]
                and parsed["logical_id"] == row["source_ref"]
            ),
            "payload": parsed,
        })
    return out


# --- the conditional link operation ----------------------------------------
_SUPERSEDE_REASON_ADVANCE = "knowledge pointer superseded by CAS advance"
_SUPERSEDE_REASON_RELINK = "knowledge pointer relink (imported unverified copy)"
_SUPERSEDE_REASON_QUARANTINE = "knowledge pointer quarantined (secret rescan): "


def quarantine_pointer_row(
    conn: sqlite3.Connection, row_id: str, *, reason: str,
    now: str | None = None,
) -> bool:
    """Maintenance-only sanctioned tombstone for ONE pointer row (PR1
    review round).

    The secret-rescan remediation lane: a reserved pointer row can never
    route through ``update_memory`` (its guard correctly refuses pointer
    edits), so a secret-suspect pointer would abort the whole maintenance
    batch. This helper tombstones exactly one LIVE pointer row inside the
    CALLER'S OPEN TRANSACTION — the authenticated knowledge tombstone op
    the supersede guard always anticipated — with the capture reason
    recorded in ``supersede_reason``. A dead row never surfaces in recall
    and never counts as authoritative (``get_pointer`` reads live rows
    only), so the secret-bearing bytes stop being deliverable state while
    the caller's quarantine record preserves the evidence.

    Deliberately narrow: refuses with KnowledgePointerRefusal when the id
    is not a pointer row (this is a dedicated maintenance path, never a
    general bypass of the ordinary supersede/invalidate guards), returns
    False for an unknown or already-dead id. The reason text is stored
    verbatim — callers pass count-only capture labels, never values.
    """
    row = conn.execute(
        "SELECT id, superseded_at, source_ref FROM memory WHERE id=?",
        (row_id,),
    ).fetchone()
    if row is None:
        return False
    if not is_pointer_ref(row["source_ref"]):
        raise KnowledgePointerRefusal(
            f"[zmem] knowledge quarantine for {row_id} refused: not a "
            "pointer row (reserved knowledge: source_ref) — this "
            "maintenance helper tombstones pointer rows only, never "
            "ordinary memories"
        )
    if row["superseded_at"] is not None:
        return False
    tx_now = now if now is not None else now_iso()
    n = _tombstone_pointer_rows(
        conn, [row_id],
        reason=_SUPERSEDE_REASON_QUARANTINE + (reason or "unspecified"),
        tx_now=tx_now,
    )
    return n == 1


def _tombstone_pointer_rows(
    conn: sqlite3.Connection, row_ids: list[str], *, reason: str, tx_now: str,
) -> int:
    """Tombstone pointer rows inside the caller's transaction. Returns the
    number actually tombstoned (an already-dead row counts zero; the CAS
    caller checks the count itself for the row it staked)."""
    n = 0
    for rid in row_ids:
        cur = conn.execute(
            "UPDATE memory SET superseded_at=?, valid_until=?, "
            "supersede_reason=? WHERE id=? AND superseded_at IS NULL",
            (tx_now, tx_now, reason, rid),
        )
        n += cur.rowcount
        try:
            conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (rid,))
        except sqlite3.OperationalError:
            pass  # vec0 table not available
    return n


def link_pointer(
    conn: sqlite3.Connection,
    *,
    namespace: str,
    target: str,
    key: str,
    version: int,
    full_sha256: str,
    summary: str,
    locator: str | None = None,
    date: str | None = None,
    expected_row_id: str | None,
    now: str | None = None,
) -> dict:
    """Atomically create or advance the authoritative pointer for one ref.

    ``expected_row_id`` is REQUIRED and explicit:

    - ``None`` — initial create. Refuses (KnowledgePointerCasError) when a
      verified live pointer already exists; any UNVERIFIED imported rows
      carrying the ref are tombstoned in the same transaction (the
      sanctioned explicit-relink path: the operator asked for the link, the
      handler verifies and replaces the inert copies).
    - a row id — CAS advance. The live verified pointer for the ref must be
      EXACTLY that row; anything else (a newer row, no live row) is a stale
      advance and raises KnowledgePointerCasError with nothing written.

    Version law (the immutable-version CAS token): the same version with
    the same digest is an idempotent NOOP (no writes, status "noop"); the
    same version with a DIFFERENT digest raises KnowledgePointerConflict
    (an immutable version is bound to one digest); a version below the live
    one is a stale advance; a version above advances (tombstone + fresh
    row, one transaction). The locator is always DERIVED from
    (key, digest) — an optional ``locator`` argument must match the derived
    value exactly and exists to catch caller/doc inconsistencies, never to
    override the deterministic form.

    Transactions: owns BEGIN IMMEDIATE when the connection is idle; nests
    in a SAVEPOINT when the caller already holds a transaction (ops
    applies handlers under per-op savepoints — this composes without a
    nested BEGIN). A refusal rolls back only this operation's work.
    """
    fields = validate_pointer_fields(
        target=target, key=key, version=version, full_sha256=full_sha256,
        summary=summary, date=date, locator=locator,
    )
    target, key = parse_ref(fields["logical_id"])
    source_ref = fields["logical_id"]
    namespace = _write._validate_namespace(conn, namespace)
    tx_now = now if now is not None else now_iso()

    if expected_row_id is not None and (
            not isinstance(expected_row_id, str) or not expected_row_id):
        raise KnowledgeRefError(
            "expected_row_id must be a row id string or an explicit None "
            "for the initial create"
        )

    started_tx = False
    savepoint = False
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True
        else:
            conn.execute("SAVEPOINT zmem_knowledge_link")
            savepoint = True

        rows = _live_rows_for_ref(conn, namespace, source_ref)
        verified: list[tuple[sqlite3.Row, dict]] = []
        unverified_ids: list[str] = []
        for row in rows:
            parsed = parse_payload(row["content"])
            if (parsed is not None and parsed["verified"]
                    and parsed["logical_id"] == row["source_ref"]):
                verified.append((row, parsed))
            else:
                unverified_ids.append(row["id"])

        if len(verified) > 1:
            raise KnowledgePointerConflict(
                f"{len(verified)} live verified pointer rows share "
                f"source_ref {source_ref!r} in namespace {namespace!r}; "
                "resolve the corruption before linking"
            )

        current: tuple[sqlite3.Row, dict] | None = verified[0] if verified else None

        if expected_row_id is None:
            if current is not None:
                raise KnowledgePointerCasError(
                    f"initial link refused: a verified pointer already "
                    f"exists for {source_ref!r} in namespace {namespace!r} "
                    f"(row {current[0]['id']}, version "
                    f"{current[1]['version']}); advance it with "
                    "expected_row_id (CAS)"
                )
        else:
            if current is None:
                raise KnowledgePointerCasError(
                    f"stale advance: no live verified pointer for "
                    f"{source_ref!r} in namespace {namespace!r} (expected "
                    f"row {expected_row_id}); the pointer was tombstoned, "
                    "advanced, or never created — re-read and relink"
                )
            cur_row, cur_fields = current
            if cur_row["id"] != expected_row_id:
                raise KnowledgePointerCasError(
                    f"stale advance: pointer for {source_ref!r} in "
                    f"namespace {namespace!r} is row {cur_row['id']} "
                    f"(version {cur_fields['version']}), expected "
                    f"{expected_row_id}; re-read the pointer and retry"
                )

        status: str
        previous_row_id: str | None = None
        if current is not None:
            cur_row, cur_fields = current
            previous_row_id = cur_row["id"]
            if cur_fields["logical_id"] != source_ref:
                # Unreachable via the row-selection predicate (defensive:
                # a payload whose logical identity disagrees with the row's
                # own ref is corruption, not an advance input).
                raise KnowledgePointerConflict(
                    f"logical identity of {source_ref!r} is "
                    f"{cur_fields['logical_id']!r}; a knowledge ref is a "
                    "stable logical identity and cannot be rebound"
                )
            if version == cur_fields["version"]:
                if fields["full_sha256"] == cur_fields["full_sha256"]:
                    # Idempotent re-delivery of the same immutable version:
                    # no writes. Imported unverified copies stay inert.
                    if started_tx:
                        _write._commit(conn)
                    elif savepoint:
                        conn.execute("RELEASE SAVEPOINT zmem_knowledge_link")
                    return {
                        "status": "noop",
                        "row_id": cur_row["id"],
                        "previous_row_id": cur_row["id"],
                        "namespace": namespace,
                        "source_ref": source_ref,
                        "version": version,
                        "replaced_unverified": 0,
                    }
                raise KnowledgePointerConflict(
                    f"version {version} of {source_ref!r} is immutable and "
                    f"already bound to digest {cur_fields['full_sha256']}; "
                    f"refusing a different digest "
                    f"{fields['full_sha256']} for the same version"
                )
            if version < cur_fields["version"]:
                raise KnowledgePointerCasError(
                    f"stale advance: live pointer for {source_ref!r} is at "
                    f"version {cur_fields['version']}; refusing regression "
                    f"to version {version} (advance with current+1)"
                )
            # Advance (version > current): tombstone the staked row under
            # CAS — the rowcount backstop covers a concurrent writer that
            # slipped between our read and this UPDATE in a caller-owned
            # non-IMMEDIATE transaction.
            n = _tombstone_pointer_rows(
                conn, [expected_row_id], reason=_SUPERSEDE_REASON_ADVANCE,
                tx_now=tx_now,
            )
            if n != 1:
                raise KnowledgePointerCasError(
                    f"stale advance: expected row {expected_row_id} for "
                    f"{source_ref!r} was superseded or deleted between "
                    "read and write; nothing written"
                )
            status = "advanced"

        # Initial create, or advance: any surviving unverified imported
        # copies of this ref are inert forgeries-by-roundtrip; replace them
        # in the same transaction so exactly one live row carries the ref.
        replaced = 0
        if unverified_ids:
            replaced = _tombstone_pointer_rows(
                conn, unverified_ids, reason=_SUPERSEDE_REASON_RELINK,
                tx_now=tx_now,
            )
        if current is None:
            status = "created"

        payload = build_payload(
            target=target, key=key, version=fields["version"],
            full_sha256=fields["full_sha256"], summary=fields["summary"],
            date=fields["date"], verified=True,
        )
        result = _write.add_memory(
            conn,
            namespace=namespace,
            type_=POINTER_TYPE,
            content=payload,
            tags=POINTER_TAG,
            source_ref=source_ref,
            signal="none",
            taint=POINTER_TAINT,
            capture_mode="manual",
            # The pointer is born as its own row: never dedup-folded (the
            # reserved prefix is excluded from every candidate lane anyway,
            # the bypasses make the intent explicit) and its structural tag
            # never propagates onto neighbors.
            force_append=True,
            skip_dedup=True,
            link_attr_propagate=False,
            now=tx_now,
            _knowledge_cap=_KNOWLEDGE_WRITE_CAPABILITY,
        )
        new_id = str(result)

        if started_tx:
            _write._commit(conn)
        elif savepoint:
            conn.execute("RELEASE SAVEPOINT zmem_knowledge_link")
        return {
            "status": status,
            "row_id": new_id,
            "previous_row_id": previous_row_id,
            "namespace": namespace,
            "source_ref": source_ref,
            "version": fields["version"],
            "replaced_unverified": replaced,
        }
    except Exception:
        if started_tx and conn.in_transaction:
            conn.rollback()
        elif savepoint and conn.in_transaction:
            conn.execute("ROLLBACK TO SAVEPOINT zmem_knowledge_link")
            conn.execute("RELEASE SAVEPOINT zmem_knowledge_link")
        raise
