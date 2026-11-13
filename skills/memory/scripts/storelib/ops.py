"""Typed operation-queue engine (issue #288, Workstream T PR 9).

``apply_ops`` applies a batch of typed operations — add / update / supersede
/ invalidate — to the store under ONE transaction, idempotently by
``op_id`` (the ``applied_ops`` PRIMARY KEY is the at-most-once record), with
per-op receipts, dependency resolution through a canonical-id map that
follows dedup folds, a capability gate for the mutating kinds, and
intent-coupled capture mode (agent/user tool writes run ``auto``).

Design contract (frozen by tests/test_t09_op_queue.py; see the approved plan
revision 2 in the trace):

- Receipt shape: ``{"op_id", "op", "status", "canonical_id", "deduped",
  "error"?, "created_new"?}`` — status is exactly one of
  ``committed | rejected | duplicate | blocked``. A dedup FOLD is
  ``committed`` + ``deduped: true``; ``duplicate`` is ONLY the replay of an
  already-committed op_id.
- The PK is checked FIRST: a replayed op_id returns its recorded verdict and
  writes NOTHING (frozen C2 pins the store bytes unchanged on the
  all-duplicate path).
- Per-op SAVEPOINTs: a store refusal (capture policy, unknown target,
  already tombstoned, cross-namespace) is a per-op ``rejected`` receipt, never
  a batch abort.
- The hub-side defence-in-depth scrub (#287 ledger residual F4): op content
  is redacted server-side BEFORE any store write, so a refused op holds no
  unredacted original anywhere on the hub.
- Error codes are content-free (``capability_denied``, ``target_missing``,
  ``store_refused``, ``capture_refused``, ``bad_op``) — never op content.
- PR1 (knowledge companion, 2026-11-13): ``knowledge_link`` is a typed op
  whose args are a CLOSED whitelist of pointer metadata (digest as eight
  8-hex chunks, reconstructed and validated here — never a raw 64-hex
  run, never note content, never a path). Namespace scope is checked
  BEFORE the pointer-existence lookup (no cross-namespace oracle) and the
  op bypasses the #363 identical-content replay lane (add/capture only).
  A first create or idempotent noop needs the ``write`` capability; a
  replacement — an advance, or ANY supplied ``expected_row_id`` — needs
  ``mutate``. Application goes through ``storelib.knowledge.link_pointer``
  (the same handler and untrusted_tool taint as the local CLI capture),
  with ``pointer_cas_stale`` / ``pointer_conflict`` as distinct receipt
  codes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from storelib import write as writelib
from storelib.write import CapturePolicyRefusal
# PR1: the knowledge pointer CAS handler's exception taxonomy — imported
# eagerly so the per-op savepoint handler can distinguish a retryable
# stale CAS from an immutability conflict (storelib.knowledge imports
# storelib.write, which this module already imports; no cycle).
from storelib.knowledge import (
    KnowledgePointerCasError as _KnowledgeCasError,
    KnowledgePointerConflict as _KnowledgeConflictError,
)

_OP_KINDS = ("add", "update", "supersede", "invalidate",
             "capture",  # capture = add-with-auto (hook captures)
             "knowledge_link")  # PR1: the knowledge-pointer CAS op
_MUTATING_KINDS = ("update", "supersede", "invalidate")
_MAX_OPS_PER_BATCH = 256

# PR1 (knowledge companion, 2026-11-13): the CLOSED args whitelist for a
# knowledge_link op.  The op carries POINTER METADATA ONLY — no note
# content, no paths, no caller-controlled writer flags — and the digest
# travels as eight 8-hex chunks (``fullhash``), exactly the pointer
# payload's storage form, so no >=32-hex run exists in an op line.
_KNOWLEDGE_LINK_ARG_FIELDS = frozenset({
    "namespace", "target", "key", "version", "locator", "fullhash",
    "summary", "date", "expected_row_id",
})
_KNOWLEDGE_HASH_CHUNKS = 8
_KNOWLEDGE_HASH_CHUNK_LEN = 8


def _identical_content_row(conn, namespace: str, content: str):
    """Live row id with byte-identical content in this namespace, else None.

    Issue #363: the at-most-once replay key for queued adds. Scoped to the
    effective namespace and LIVE rows only (write.py dedup scoping parity);
    superseded rows never satisfy a replay.
    """
    if not content:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM memory WHERE namespace = ? AND content = ? "
            "AND superseded_at IS NULL LIMIT 1",
            (namespace, content)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return row[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LegacyPrincipal:
    """The operator/legacy identity for capability checks: not revoked, no
    capability restrictions (``capabilities=None`` means "legacy behaviour
    of that token kind" per auth.py) — mirrors ``Principal.from_token_config``
    for the legacy token mode."""

    revoked = False
    capabilities = None
    principal_id = "legacy"
    namespaces = None
    scoped = False


# Back-compat alias (older call sites import the underscore name).
_LegacyPrincipal = LegacyPrincipal


def principal_lookup_from_env(env=None):
    """Resolve a principal id against ``ZMEM_MCP_TOKENS_FILE`` (the same map
    file auth.py loads — a JSON document with a ``principals`` list whose
    entries carry ``principal``/``capabilities``/``revoked``). This is the
    CLI mirror of the hub-side map lookup so the capability gate enforces
    IDENTICALLY on the CLI and MCP routes; the parse is deliberately minimal
    (id, capabilities, revoked) and fail-closed: any unreadable/malformed map
    or unknown id resolves to None => denied for EVERY kind (issue #414:
    the unknown_principal gate rejects a non-empty unresolved principal
    regardless of op kind)."""

    import json as _json
    import os as _os
    source = _os.environ if env is None else env
    path = (source.get("ZMEM_MCP_TOKENS_FILE", "") or "").strip()
    if not path:
        return None
    try:
        raw = _json.loads(Path(path).read_text(encoding="utf-8",
                                               errors="replace"))
    except (OSError, ValueError):
        return None
    return _PrincipalIndex(raw)


class _PrincipalIndex:
    """Tiny by-id index over the tokens-file principal map (read-only)."""

    __slots__ = ("_by_id",)

    def __init__(self, raw):
        entries = raw.get("principals") if isinstance(raw, dict) else raw
        self._by_id = {}
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    pid = entry.get("principal") or entry.get("id")
                    if isinstance(pid, str) and pid.strip():
                        self._by_id[pid.strip()] = entry
        elif isinstance(raw, dict):
            # The token-keyed map shape (auth.py's PrincipalMap file): keys
            # are tokens, each entry names its principal id.
            for entry in raw.values():
                if isinstance(entry, dict):
                    pid = entry.get("principal") or entry.get("id")
                    if isinstance(pid, str) and pid.strip():
                        self._by_id[pid.strip()] = entry

    def __call__(self, principal_id):
        entry = self._by_id.get((principal_id or "").strip())
        if not isinstance(entry, dict):
            return None
        caps = entry.get("capabilities")
        principal = LegacyPrincipal()
        principal.principal_id = (principal_id or "").strip()
        principal.revoked = bool(entry.get("revoked", False))
        if caps is None:
            pass  # legacy token behaviour: no capability restrictions
        elif not isinstance(caps, (list, tuple, set, frozenset)) or not caps:
            # Malformed capability metadata fails CLOSED (auth.py's map
            # discipline), never to the legacy allow.
            principal.capabilities = frozenset()
        else:
            principal.capabilities = frozenset(str(c) for c in caps)
        # Issue #414: carry the entry's namespace scope through the rebuild.
        # auth.py's map discipline verbatim in effect: absent/None namespaces
        # = an UNSCOPED legacy entry (the hub's semantics for entries without
        # scope); a present value must be a non-empty list of non-empty
        # strings (auth.py additionally validates namespace shape and fails
        # the WHOLE map at load; a garbage shape here can never match a real
        # namespace, so the principal is deny-all either way). Any malformed
        # value fails closed for THIS principal — the lookup yields nothing,
        # the same as an unknown id — instead of silently unscoping it.
        scopes = entry.get("namespaces")
        if scopes is None:
            pass  # unscoped legacy entry (class defaults)
        elif (isinstance(scopes, (list, tuple)) and scopes
              and all(isinstance(ns, str) and ns.strip()
                      for ns in scopes)):
            principal.namespaces = frozenset(
                str(ns).strip() for ns in scopes)
            principal.scoped = True
        else:
            return None
        return principal


def _receipt(op_id: str, op: str, status: str, *, canonical_id: Optional[str] = None,
             deduped: bool = False, error: Optional[str] = None,
             created_new: Optional[bool] = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "op_id": op_id,
        "op": op,
        "status": status,
        "canonical_id": canonical_id,
        "deduped": deduped,
    }
    if error is not None:
        out["error"] = error
    if created_new is not None:
        out["created_new"] = created_new
    return out


def mutate_allowed(principal_obj: Any) -> bool:
    """The #288 capability rule (issue #281's deferred enforcement).

    Fail-closed order: revoked first (the deny-all principal carries
    ``capabilities=None`` — a None-means-legacy reading would let it
    through), unknown principal (None) denied, legacy (capabilities None)
    allowed, an explicitly EMPTY capability set denied, then the
    mutate/admin membership check.
    """
    if principal_obj is None:
        return False
    if getattr(principal_obj, "revoked", False):
        return False
    capabilities = getattr(principal_obj, "capabilities", None)
    if capabilities is None:
        return True
    if not capabilities:
        return False
    return "mutate" in capabilities or "admin" in capabilities


def write_allowed(principal_obj: Any) -> bool:
    """The PR1 knowledge_link first-create/noop capability rule.

    Same fail-closed shape as :func:`mutate_allowed`; the accepted set is
    ``write`` (the plain creation capability) plus the stronger
    ``mutate``/``admin``.  A pointer REPLACEMENT (an advance, or any op
    supplying ``expected_row_id``) requires :func:`mutate_allowed`
    instead — see the knowledge_link admission block in
    :func:`apply_ops`.
    """
    if principal_obj is None:
        return False
    if getattr(principal_obj, "revoked", False):
        return False
    capabilities = getattr(principal_obj, "capabilities", None)
    if capabilities is None:
        return True
    if not capabilities:
        return False
    return bool({"write", "mutate", "admin"} & set(capabilities))


def _knowledge_link_fields(args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a knowledge_link op's args against the CLOSED whitelist and
    return the handler fields.

    Raises ``ValueError`` (content-free) for any violation: unknown keys,
    wrong types, a malformed chunked digest, or a PATH-SHAPED ROUTING
    value — the hub never reads a client-supplied path, and the whitelist
    guarantees no path field exists at all (defense in depth on top of
    it: any backslash, drive root, absolute root, or ``..`` segment in a
    routing string refuses the op).  ``summary`` is prose, not routing:
    it stays a required non-empty string but is exempt from the
    path-shape refusal — a summary may legitimately mention a path and
    no file is ever opened from it.
    """
    extra = sorted(set(args) - _KNOWLEDGE_LINK_ARG_FIELDS)
    if extra:
        raise ValueError(
            "knowledge_link: unknown args %s" % ",".join(extra))
    for field in ("namespace", "target", "key", "locator",
                  "summary"):
        if not isinstance(args.get(field), str) or not args[field]:
            raise ValueError(
                "knowledge_link: %s must be a non-empty string" % field)
    version = args.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("knowledge_link: version must be an integer >= 1")
    chunks = args.get("fullhash")
    if (not isinstance(chunks, list)
            or len(chunks) != _KNOWLEDGE_HASH_CHUNKS
            or not all(isinstance(c, str)
                       and len(c) == _KNOWLEDGE_HASH_CHUNK_LEN
                       and all(ch in "0123456789abcdef" for ch in c)
                       for c in chunks)):
        raise ValueError(
            "knowledge_link: fullhash must be %d 8-hex chunks"
            % _KNOWLEDGE_HASH_CHUNKS)
    date = args.get("date")
    if date is not None and not isinstance(date, str):
        raise ValueError("knowledge_link: date must be a string or null")
    expected = args.get("expected_row_id")
    if expected is not None and (not isinstance(expected, str)
                                 or not expected):
        raise ValueError(
            "knowledge_link: expected_row_id must be a row id or null")

    import re as _re

    _drive_root = _re.compile(r"^[A-Za-z]:[\\/]")
    _abs_root = _re.compile(r"^[\\/]")

    def _path_shaped(value: str) -> bool:
        if _drive_root.match(value) or _abs_root.match(value):
            return True
        if "\\" in value:
            return True
        if any(part == ".." for part in value.split("/")):
            return True
        return False

    for field in ("target", "key", "locator", "expected_row_id"):
        value = args.get(field)
        if isinstance(value, str) and _path_shaped(value):
            raise ValueError(
                "knowledge_link: %s must not be path-shaped" % field)

    return {
        "namespace": args["namespace"],
        "target": args["target"],
        "key": args["key"],
        "version": version,
        "locator": args["locator"],
        "full_sha256": "".join(chunks),
        "summary": args["summary"],
        "date": date,
        "expected_row_id": expected,
    }


def parse_ops_line(line: str) -> dict[str, Any]:
    """Parse ONE input line. Raises ValueError with a line-level (content-free)
    message when the line is not a valid op envelope."""
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("op line is not an object")
    op_id = obj.get("op_id")
    op = obj.get("op")
    if not isinstance(op_id, str) or not op_id.strip():
        raise ValueError("op line missing op_id")
    if op not in _OP_KINDS:
        raise ValueError("op line has unknown op kind")
    args = obj.get("args")
    if not isinstance(args, dict):
        raise ValueError("op line missing args object")
    depends_on = obj.get("depends_on")
    if depends_on is not None and not isinstance(depends_on, str):
        raise ValueError("op line has a non-string depends_on")
    client_seq = obj.get("client_seq")
    if client_seq is not None and not isinstance(client_seq, int):
        raise ValueError("op line has a non-integer client_seq")
    return obj


def _effective_namespace(args: Mapping[str, Any]) -> str:
    return str(args.get("namespace") or "user:global")


def _namespace_allowed(effective: Any, namespace: str) -> bool:
    """The #109 scope rule at the engine boundary: an unscoped (legacy)
    principal touches every namespace; a scoped principal only its
    allow-list — INCLUDING the implicit user:global default, so an op that
    OMITS the namespace cannot escape into it (the hub-side guard only sees
    explicitly-declared namespaces)."""
    if effective is None or not getattr(effective, "scoped", False):
        return True
    allowed = getattr(effective, "namespaces", None) or frozenset()
    return namespace in allowed


def _target_namespace(conn: sqlite3.Connection, target: str) -> Optional[str]:
    row = conn.execute("SELECT namespace FROM memory WHERE id=?",
                       (target,)).fetchone()
    if row is None:
        return None
    return row["namespace"]


def _strip_attribution(tags: Any) -> str:
    """Drop caller-forged attribution tags (the hub strips host:/agent:
    client tags on every write path and stamps its own — #281).

    Tokenizes under the consumer grammar (issue #395: the pre-fix
    comma-only split let case-varied and semicolon-delimited forged forms
    survive the strip, exactly the #381 defect class the sync lane fixed).
    """
    from storelib.entity import split_tag_tokens
    if not isinstance(tags, str) or not tags:
        return tags if isinstance(tags, str) else ""
    # Typed entity forms mint host/agent entities exactly like the bare
    # prefixes (issue #395 feedback round) — strip both families.
    kept = [t for t in split_tag_tokens(tags)
            if not t.lower().startswith(("host:", "agent:",
                                         "entity:host:", "entity:agent:"))]
    return ",".join(kept)


def apply_ops(conn: sqlite3.Connection, ops: list[dict[str, Any]],
              principal: str = "",
              principal_lookup: Optional[Callable[[str], Any]] = None,
              default_principal: Any = None) -> dict[str, Any]:
    """Apply the batch; return ``{"ok": True, "receipts": [...]}``.

    ``principal_lookup`` maps a principal id to its principal object (auth
    map); ``default_principal`` is the caller-resolved principal used when
    the batch-level ``principal`` is empty (the MCP layer passes
    ``current_principal()`` semantics; the CLI passes the legacy startup
    principal). Capability enforcement only sees these two — the per-op
    ``principal`` field is advisory metadata, never an identity switch.
    """
    if len(ops) > _MAX_OPS_PER_BATCH:
        raise ValueError("batch exceeds the op limit")

    effective = None
    if principal:
        if principal_lookup is not None:
            effective = principal_lookup(principal)
        else:  # pragma: no cover - the MCP layer always passes a lookup
            effective = None
    else:
        effective = default_principal

    receipts: list[dict[str, Any]] = []
    canonical: dict[str, str] = {}
    committed_ids: set[str] = set()

    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")

    try:
        for index, raw in enumerate(ops):
            op_id = raw["op_id"]
            kind = raw["op"]
            args = raw.get("args") or {}
            depends_on = raw.get("depends_on") or None

            # Knowledge receipts are scoped evidence: authorize before even
            # consulting the replay ledger. A caller must not reuse a prior
            # op id to reveal a canonical row outside its current scope.
            if kind == "knowledge_link":
                if principal and effective is None:
                    receipts.append(_receipt(
                        op_id, kind, "rejected", error="unknown_principal"))
                    continue
                if not _namespace_allowed(effective,
                                          _effective_namespace(args)):
                    receipts.append(_receipt(
                        op_id, kind, "rejected", error="namespace_not_allowed"))
                    continue
                if not write_allowed(effective):
                    receipts.append(_receipt(
                        op_id, kind, "rejected", error="capability_denied"))
                    continue

            # 1. PK FIRST — a replay writes nothing at all. Idempotency is
            # scoped PER PRINCIPAL: one caller's op_id cannot pre-claim or
            # disclose another caller's canonical id (review PRR-016).
            row = conn.execute(
                "SELECT status, canonical_id FROM applied_ops"
                " WHERE op_id=? AND principal=?",
                (op_id, principal or "")).fetchone()
            if row is not None:
                # Review PRR-014: replay the RECORDED status — a recorded
                # 'rejected' op replays as 'rejected' (with the reason it
                # carried), never as a misleading already-applied
                # 'duplicate'. Only a recorded commit answers duplicate.
                if row["status"] == "rejected":
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="previously_rejected_on_replay",
                        canonical_id=row["canonical_id"]))
                    continue
                receipts.append(_receipt(
                    op_id, kind, "duplicate",
                    canonical_id=row["canonical_id"]))
                if row["canonical_id"]:
                    canonical[op_id] = row["canonical_id"]
                committed_ids.add(op_id)
                continue

            # 2. Dependency resolution. A non-string depends_on (an
            # unvalidated client value) is a per-op bad_op, never a
            # TypeError that aborts the batch.
            if depends_on is not None:
                if not isinstance(depends_on, str) or not depends_on:
                    receipts.append(_receipt(op_id, kind, "rejected",
                                             error="bad_op"))
                    continue
                if depends_on not in committed_ids:
                    receipts.append(_receipt(
                        op_id, kind, "blocked",
                        error="dependency_not_committed"))
                    continue

            # 3. Capability gate for the mutating kinds.
            if kind in _MUTATING_KINDS and not mutate_allowed(effective):
                receipts.append(_receipt(
                    op_id, kind, "rejected", error="capability_denied"))
                continue

            # 3a. Issue #414: a NON-EMPTY batch principal that resolved to
            # nothing (no lookup configured, an unknown id, or an entry that
            # fails the map discipline) fails CLOSED for EVERY kind — the
            # hub denies unknown ids on every route; previously only the
            # mutating kinds were denied, leaving add/capture to pass with a
            # vacuous namespace gate (effective=None is always "allowed").
            if principal and effective is None:
                receipts.append(_receipt(op_id, kind, "rejected",
                                         error="unknown_principal"))
                continue

            # 3a-prime. PR1 knowledge_link admission. NAMESPACE AUTH FIRST —
            # the pointer-existence lookup below must never run for a
            # namespace the principal cannot touch (a capability verdict
            # keyed on pointer existence would be a cross-namespace
            # oracle). The #363 identical-content replay lane is an
            # add/capture-only rule and is structurally bypassed here: a
            # knowledge_link op's "content" is the server-minted pointer
            # payload, never caller content. Capability split: a FIRST
            # create or an idempotent noop needs `write`; a REPLACEMENT
            # (an advance, or ANY op supplying expected_row_id — the
            # conservative reading of an explicit CAS stake) needs
            # `mutate`. The same storelib.knowledge handler (and its
            # untrusted_tool taint) runs for local and remote callers.
            if kind == "knowledge_link":
                if not _namespace_allowed(effective,
                                          _effective_namespace(args)):
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="namespace_not_allowed"))
                    continue
                try:
                    link_fields = _knowledge_link_fields(args)
                except ValueError:
                    receipts.append(_receipt(op_id, kind, "rejected",
                                             error="bad_op"))
                    continue
                requires_mutate = link_fields["expected_row_id"] is not None
                if not requires_mutate:
                    from storelib import knowledge as _store_knowledge

                    try:
                        current = _store_knowledge.get_pointer(
                            conn, link_fields["namespace"],
                            link_fields["target"], link_fields["key"])
                    except ValueError:
                        receipts.append(_receipt(
                            op_id, kind, "rejected", error="bad_op"))
                        continue
                    if current is not None:
                        # Replacement unless it is the idempotent noop
                        # (same version AND same digest).
                        requires_mutate = not (
                            current.version == link_fields["version"]
                            and current.full_sha256
                            == link_fields["full_sha256"])
                if requires_mutate:
                    if not mutate_allowed(effective):
                        receipts.append(_receipt(
                            op_id, kind, "rejected",
                            error="capability_denied"))
                        continue
                elif not write_allowed(effective):
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="capability_denied"))
                    continue

            # 3b. Namespace scope for EVERY kind: the declared namespace
            # (add/capture) and the TARGET row's namespace (the mutation
            # kinds — a by-id op must not escape its row's scope). Checked
            # here so omitted-namespace defaults cannot bypass the guard.
            if kind in ("add", "capture"):
                # The namespace gate precedes the replay guard: a
                # not-allowed namespace must get `namespace_not_allowed`
                # even when the content is a byte-identical replay — a
                # guard-first order would hand unauthorized principals a
                # success receipt (and the live row's canonical id, a
                # cross-namespace existence oracle).
                if not _namespace_allowed(effective,
                                          _effective_namespace(args)):
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="namespace_not_allowed"))
                    continue
                # Issue #363 AC2 replay guard: after the namespace gate and
                # scrubbing, a byte-identical live row in the same namespace
                # is a REPLAY, not a new observation — return the fold
                # receipt without re-running the write path (no
                # adjust_trust, no retrieval_count bump). Identical content
                # already passed the write-policy gates on first apply.
                # The fold still participates in the exactly-once
                # bookkeeping (issue #288): record the applied_ops verdict
                # and resolve the op's id for same-batch dependents.
                _replay = _identical_content_row(
                    conn, _effective_namespace(args),
                    _scrub(str(args.get("content", ""))))
                if _replay is not None:
                    # Feedback FB-2: a fold must never report committed while
                    # silently dropping requested expiry metadata. With
                    # expiry args present, the op only commits when the
                    # keeper's stored record already matches (a true replay);
                    # a missing/differing record is a REJECTION naming the
                    # reason -- the caller re-files via update, which owns
                    # expiry transitions.
                    _want_cls = (str(args["expiry_class"])
                                 if args.get("expiry_class") else None)
                    _want_until = (str(args["inject_until"])
                                   if args.get("inject_until") else None)
                    # Issue #420 (until-only deadline drop): the entry guard
                    # must key on EVERY want-field the match predicate below
                    # honors — an add/capture carrying `inject_until` without
                    # `expiry_class` previously bypassed this guard entirely
                    # and folded `committed, deduped=True` while silently
                    # dropping the requested deadline (the FB-2 invariant at
                    # the head of this block). An until-only add/capture can
                    # never commit as a NEW op (write-side _resolve_expiry
                    # requires expiry_class), so the until-only shape reaches
                    # this fold only as a replay — and for that shape the
                    # stored-match predicate can never pass (memory_expiry
                    # .expiry_class is NOT NULL while the request omits the
                    # class), so the SANCTIONED outcome is the named
                    # rejection: the caller re-files via update, which owns
                    # expiry transitions (review PRR-004; issue #420 AC3
                    # permits honor-or-reject).
                    if kind in ("add", "capture") and (_want_cls or _want_until):
                        _keeper_row = conn.execute(
                            "SELECT type FROM memory WHERE id=?",
                            (_replay,)).fetchone()
                        _stored = conn.execute(
                            "SELECT expiry_class, inject_until "
                            "FROM memory_expiry WHERE memory_id=?",
                            (_replay,)).fetchone()
                        if (_keeper_row is None
                                or _keeper_row[0] != "state"):
                            receipts.append(_receipt(
                                op_id, kind, "rejected",
                                error="expiry_requires_state_target",
                                canonical_id=_replay, deduped=True))
                            conn.execute(
                                "INSERT INTO applied_ops (op_id, principal,"
                                " status, canonical_id, applied_at) VALUES"
                                " (?, ?, 'rejected', ?, ?)",
                                (op_id, principal or "", _replay, _now()))
                            canonical[op_id] = _replay
                            continue
                        if (_stored is not None
                                and _stored[0] == _want_cls
                                and (_want_until is None
                                     or _stored[1] == _want_until)):
                            pass  # true replay: the stored record matches
                        else:
                            receipts.append(_receipt(
                                op_id, kind, "rejected",
                                error="expiry_record_mismatch_on_dedup",
                                canonical_id=_replay, deduped=True))
                            conn.execute(
                                "INSERT INTO applied_ops (op_id, principal,"
                                " status, canonical_id, applied_at) VALUES"
                                " (?, ?, 'rejected', ?, ?)",
                                (op_id, principal or "", _replay, _now()))
                            canonical[op_id] = _replay
                            continue
                    receipts.append(_receipt(
                        op_id, kind, "committed",
                        canonical_id=_replay, deduped=True))
                    conn.execute(
                        "INSERT INTO applied_ops (op_id, principal, status,"
                        " canonical_id, applied_at) VALUES (?, ?, 'committed',"
                        " ?, ?)",
                        (op_id, principal or "", _replay, _now()))
                    canonical[op_id] = _replay
                    committed_ids.add(op_id)
                    continue
            elif kind == "knowledge_link":
                # Admission finished in 3a-prime (namespace auth, whitelist,
                # capability); no by-id target resolution applies — the op
                # addresses a reserved source_ref, not a store row id.
                pass
            else:
                target_probe = _resolve_target(args, canonical)
                target_ns = (_target_namespace(conn, target_probe)
                             if target_probe else None)
                if target_ns is not None and not _namespace_allowed(
                        effective, target_ns):
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="namespace_not_allowed"))
                    continue
                # Issue #414: an update's explicit DESTINATION namespace is
                # a re-key — it must be inside the principal's scope exactly
                # as the standalone update tool enforces (the gate above
                # sees only the TARGET row's namespace; the destination was
                # previously forwarded to the writer unguarded).
                if (kind == "update" and args.get("namespace")
                        and not _namespace_allowed(
                            effective, str(args["namespace"]))):
                    receipts.append(_receipt(
                        op_id, kind, "rejected",
                        error="namespace_not_allowed"))
                    continue

            # 4. Apply under a per-op savepoint.
            savepoint = f"zmem_op_{index}"
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                receipt = _apply_one(conn, op_id, kind, args, canonical)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if receipt["status"] == "committed":
                    conn.execute(
                        "INSERT INTO applied_ops (op_id, principal, status,"
                        " canonical_id, applied_at) VALUES (?, ?, 'committed',"
                        " ?, ?)",
                        (op_id, principal or "", receipt.get("canonical_id"),
                         _now()))
                    if receipt.get("canonical_id"):
                        canonical[op_id] = receipt["canonical_id"]
                    committed_ids.add(op_id)
            except CapturePolicyRefusal:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                receipt = _receipt(op_id, kind, "rejected",
                                   error="capture_refused")
            except _KnowledgeCasError:
                # PR1: a stale/lost CAS stake is a RETRYABLE rejection with
                # its own content-free code — mapping it into
                # store_refused/target_missing would hide the one outcome
                # the caller can act on (re-read the pointer, relink).
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                receipt = _receipt(op_id, kind, "rejected",
                                   error="pointer_cas_stale")
            except _KnowledgeConflictError:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                receipt = _receipt(op_id, kind, "rejected",
                                   error="pointer_conflict")
            except ValueError as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                message = str(exc)
                if ("not found" in message or "missing" in message
                        or "no memory with id" in message):
                    code = "target_missing"
                else:
                    code = "store_refused"
                receipt = _receipt(op_id, kind, "rejected", error=code)
            except Exception:  # noqa: BLE001 - a malformed op VALUE (a list
                # where a string is expected, a bad namespace type, an
                # integrity conflict) is a per-op rejected receipt, never a
                # batch abort — the poison-op resend loop died here.
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                receipt = _receipt(op_id, kind, "rejected",
                                   error="store_refused")
            receipts.append(receipt)

        if own_transaction:
            conn.commit()
    except BaseException:
        if own_transaction:
            conn.rollback()
        raise

    return {"ok": True, "receipts": receipts}


def _scrub(value: Optional[str]) -> str:
    """Hub-side defence-in-depth scrub (#287 ledger residual F4)."""
    from redaction import redact_secret_like_text
    if not isinstance(value, str):
        return ""
    redacted, _count = redact_secret_like_text(value)
    return redacted


def _resolve_target(args: Mapping[str, Any],
                    canonical: Mapping[str, str]) -> Optional[str]:
    """id_or_dep resolution for the mutation kinds: a concrete store id passes
    through; an op_id resolves through the canonical map (post-dedup id)."""
    target = args.get("id_or_dep") or args.get("id") or ""
    if not isinstance(target, str) or not target:
        return None
    return canonical.get(target, target)


def _capture_mode(args: Mapping[str, Any]) -> str:
    """The queued capture intent (issue AC6): the hook threads the
    operator's normalized ZMEM_CAPTURE_MODE value; auto is the default.
    Unknown values fail safe to auto (the redact-and-store mode)."""
    raw = str(args.get("capture_mode") or "").strip().lower()
    return raw if raw in ("auto", "manual", "reviewed", "off") else "auto"


def _apply_one(conn: sqlite3.Connection, op_id: str, kind: str,
               args: Mapping[str, Any],
               canonical: Mapping[str, str]) -> dict[str, Any]:
    if kind == "knowledge_link":
        # PR1: the authoritative pointer CAS. The fields are revalidated
        # here (the admission block's check is the gate; this is the single
        # source of truth for the handler call). link_pointer owns the
        # transaction discipline (BEGIN IMMEDIATE / nested SAVEPOINT), the
        # reserved-ref mint capability, the untrusted_tool taint, and the
        # immutability/CAS laws — the SAME handler serves local CLI
        # captures and hub-applied remote ops (taint parity).
        from storelib import knowledge as _store_knowledge

        fields = _knowledge_link_fields(args)
        result = _store_knowledge.link_pointer(
            conn,
            namespace=fields["namespace"],
            target=fields["target"],
            key=fields["key"],
            version=fields["version"],
            locator=fields["locator"],
            full_sha256=fields["full_sha256"],
            summary=fields["summary"],
            date=fields["date"],
            expected_row_id=fields["expected_row_id"],
        )
        noop = result["status"] == "noop"
        receipt = _receipt(
            op_id, kind, "committed", canonical_id=result["row_id"],
            deduped=noop, created_new=(not noop))
        receipt["link_status"] = result["status"]
        receipt["version"] = result["version"]
        receipt["replaced_unverified"] = result.get("replaced_unverified", 0)
        return receipt

    if kind in ("add", "capture"):
        result = writelib.add_memory(
            conn,
            namespace=_effective_namespace(args),
            type_=str(args.get("type") or "fact"),
            content=_scrub(args.get("content", "")),
            tags=_scrub(_strip_attribution(args.get("tags") or "")),
            signal=str(args.get("signal") or "none"),
            source_ref=_scrub(str(args.get("source_ref") or "")),
            capture_mode=_capture_mode(args),
            taint="untrusted_tool",  # network-fed route: client taint claims
            # are never trusted (the T-03 clamp; the hub's own tools hardcode
            # untrusted_tool for remote submissions).
            # #326: state rows ride the queue with their expiry metadata --
            # dropping these here made apply_ops report committed while the
            # side record silently never landed.
            expiry_class=(str(args["expiry_class"])
                          if args.get("expiry_class") else None),
            inject_until=(str(args["inject_until"])
                          if args.get("inject_until") else None),
        )
        return _receipt(op_id, kind, "committed", canonical_id=str(result),
                        deduped=bool(getattr(result, "deduped", False)))

    target = _resolve_target(args, canonical)
    if not target:
        return _receipt(op_id, kind, "rejected", error="target_missing")

    if kind == "update":
        content = _scrub(args.get("content", ""))
        if not content.strip():
            # Validation parity with the standalone update tool: an
            # empty-content update would tombstone the target and insert an
            # empty replacement.
            return _receipt(op_id, kind, "rejected", error="bad_op")
        target_ns = _target_namespace(conn, target)
        result, created_new = writelib.update_memory(
            conn,
            mid=target,
            content=content,
            namespace=args.get("namespace") or None,
            type_=args.get("type"),
            tags=_scrub(_strip_attribution(str(args.get("tags") or ""))) or None,
            source_ref=_scrub(str(args.get("source_ref") or "")) or None,
            signal=args.get("signal"),
            taint="untrusted_tool",
            capture_mode=_capture_mode(args),
            expected_old_namespace=target_ns,
            # #326: expiry updates ride the queue (the same forwarding the
            # standalone update tool has had since #173).
            expiry_class=(str(args["expiry_class"])
                          if args.get("expiry_class") else None),
            inject_until=(str(args["inject_until"])
                          if args.get("inject_until") else None),
        )
        return _receipt(op_id, kind, "committed", canonical_id=str(result),
                        created_new=bool(created_new))

    reason = args.get("reason") or ""
    if kind == "invalidate" and not str(reason).strip():
        return _receipt(op_id, kind, "rejected", error="store_refused")

    target_ns = _target_namespace(conn, target)
    ok = writelib.supersede_memory(
        conn, target, _scrub(str(reason))
        or "op-queue supersede",
        expected_namespace=target_ns)
    if ok is False:
        return _receipt(op_id, kind, "rejected", error="target_missing")
    return _receipt(op_id, kind, "committed", canonical_id=target)
