"""The explicit knowledge capture workflow (PR1, plan 2026-11-13).

This module owns the END-TO-END publication pipeline and nothing else —
the pieces are the other PR1 modules: admission/identity/targets live in
``knowledge.documents``, the crash journal in ``knowledge.journal``, the
separate delivery queue in ``knowledge.delivery``, and the authoritative
pointer CAS in ``storelib.knowledge.link_pointer`` (local store and hub
engine share that ONE handler, so local and remote pointers get identical
taint, grammar and immutability semantics).

Capture order (frozen by the plan's crash-recovery law — do not reorder):

1. ``prepare_document`` — secret scan FIRST (any detection rejects the
   publication), LF + one terminal newline, full SHA-256, deterministic
   locator.
2. ``journal.record_intent`` — the metadata-only intent is durable BEFORE
   the create touches a target.
3. Observe the predecessor pointer BEFORE the create — ``get_pointer``
   locally; remotely the hub's AUTHORITATIVE ``knowledge_pointer`` read
   (capability-gated), with this host's delivery-intent sidecar as the
   fallback stake when that read is unavailable.  The CAS stake
   (``expected_row_id``) and the next version come from this observation,
   and ``link_pointer`` re-verifies it atomically.
4. ``create`` on the target (exclusive, readback-verified, never
   overwrites); ``doc_verified``.
5. ``link_pointer`` — the dedicated atomic SQLite compare-and-swap on the
   AUTHORITATIVE store (locally: this process; remotely: the hub's ops
   engine applies the same handler).  ``pointer_queued`` before the hand-off,
   ``applied`` after the receipt.
6. Retries with the SAME screened content are idempotent everywhere: the
   journal re-records nothing, the create answers ``existed_match``, and
   the link answers ``noop``.

Journal contract: the journal IS the intent record — no workflow-side
bypass.  While an identity's latest entry is ACTIVE, a changed-digest
capture fails honestly BEFORE the create; once the latest entry is
TERMINAL (``applied``/``quarantined``), a changed digest records a fresh
entry (version progression).  A hub rejection or a pointer CAS conflict
drives THIS attempt's entry to terminal ``quarantined`` — never left
ACTIVE forever, and never clobbering an entry another writer already
drove to ``applied``.  The delivery sidecar stays exactly what the plan
sanctioned it for: the fields the journal does not carry (op_id, delivery
receipts, the observed predecessor/expected_row_id), never a second
intent record.

Mode law: ``local`` opens the authoritative store connection passed by the
CLI; ``remote`` NEVER opens or creates a local store — the pointer op goes
to the capability-gated knowledge delivery queue (``hello`` +
``knowledge_link_v1`` BEFORE any enqueue; an old or unreachable hub means
an explicit pending status, never a core-queue fallback).

Honesty law (feedback fix, 2026-11-13): a pending or queued pointer means
the WORKFLOW is not complete — the envelope reports ``ok: false``,
``completed: false`` with ``doc_published: true`` (the immutable version
exists at the target), and the CLI maps pending/queued to a distinct exit
code (3) instead of success.  Only an applied/created/noop/advanced
pointer is ``ok``.  The predecessor stake (``expected_row_id`` + version
for THIS digest) is persisted durably BEFORE the create and REUSED
verbatim across retries — a retry never silently re-observes a pointer
another writer advanced (that is an honest cas_stale); in remote mode the
predecessor comes from the hub's authoritative, capability-gated
``knowledge_pointer`` read, so a second host stakes the pointer the hub
actually holds.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from knowledge.documents import (
    ConflictError as _DocConflictError,
    CreateResult,
    DocumentRejected,
    TargetError as _TargetError,
    open_target,
    prepare_document,
)
from knowledge.journal import (
    TERMINAL_STATES,
    JournalError,
    KnowledgeJournal,
    default_journal_path,
)
from redaction import redact_secret_like_text

__all__ = [
    "WorkflowError",
    "build_hub_transport",
    "capture",
    "check",
    "default_journal",
    "flush",
    "read",
    "status",
]

_DEFAULT_NAMESPACE = "user:global"


class WorkflowError(RuntimeError):
    """A workflow refusal/failure.  Safe detail only — never note content,
    never a token value."""


# ---------------------------------------------------------------------------
# helpers


def build_hub_transport(env: Mapping[str, str] | None = None):
    """The configured hub transport for remote mode (the existing fleet
    resolver: URL from ``ZMEM_REMOTE_URL``, token from the transport's own
    env chain, shared deadline resolution — no new auth surface)."""
    source = os.environ if env is None else env
    url = (source.get("ZMEM_REMOTE_URL", "") or "").strip()
    if not url:
        raise WorkflowError(
            "remote knowledge mode requires ZMEM_REMOTE_URL")
    import sys as _sys

    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in _sys.path:
        _sys.path.insert(0, scripts)
    from fleet import transport  # noqa: PLC0415 - lazy by design

    return transport.McpHttp(
        url=url,
        executor=transport.DeadlineExecutor(),
        deadline_s=transport.resolve_deadline_s(dict(source)),
    )


def _storelib_knowledge():
    from storelib import knowledge as store_knowledge  # noqa: PLC0415

    return store_knowledge


def _now_iso() -> str:
    from storelib.schema import now_iso  # noqa: PLC0415

    return now_iso()


def _default_summary(document) -> str:
    """First non-empty line of the canonical content, bounded to the
    pointer summary cap (the store module owns the exact bound)."""
    cap = getattr(_storelib_knowledge(), "MAX_SUMMARY_CHARS", 512)
    for line in document.content.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:cap]
    return "knowledge note %s" % document.logical_key


def _digest_from_chunks(chunks: Any) -> str | None:
    """Reassemble the wire form (eight 8-hex chunks) into one digest —
    None for ANY malformed shape (the chunks arrive from the sidecar file
    and hub answers, so type errors must not escape as tracebacks)."""
    if not isinstance(chunks, list) or len(chunks) != 8:
        return None
    if any(not isinstance(c, str) or len(c) != 8 for c in chunks):
        return None
    joined = "".join(chunks)
    if len(joined) != 64:
        return None
    try:
        int(joined, 16)
    except ValueError:
        return None
    return joined.lower()


def _chunks_from_digest(digest: str) -> list[str]:
    return [digest[i * 8:(i + 1) * 8] for i in range(8)]


def _field(source: Any, name: str) -> Any:
    """Read one field from a pointer mapping (hub answers) or object
    (the local store's row) — the two authoritative pointer shapes."""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _landed_match(pointer: Any, *, version: int, digest: str,
                  locator: str) -> bool:
    """Did THIS attempt's pointer already land (crash after the commit,
    before the verdict was recorded)?  EXACT match required: same version,
    FULL digest and locator — anything else (another writer's different or
    newer row, no live row) is not ours and stays an honest cas_stale."""
    if pointer is None:
        return False
    try:
        landed_version = int(_field(pointer, "version"))
    except (TypeError, ValueError):
        return False
    return (landed_version == version
            and _field(pointer, "full_sha256") == digest
            and _field(pointer, "locator") == locator
            and bool(_field(pointer, "row_id")))


def _require_target(config, target_id: str):
    if not config.enabled:
        raise WorkflowError(
            "knowledge companion is disabled: no enabled 'knowledge' object "
            "in the store config (config.json); see the operator guide")
    target = config.targets.get(target_id)
    if target is None:
        raise WorkflowError(
            "unknown knowledge target %r (configured: %s)"
            % (target_id, ", ".join(sorted(config.targets)) or "none"))
    return target


def _ref_for(target_id: str, logical_key: str) -> str:
    return "knowledge:%s:%s" % (target_id, logical_key)


# Intent states that mean THIS attempt's pointer already landed: a stake
# recorded for one of them is spent, and a retry re-observes (noop path)
# instead of reusing it.
_STAKE_SPENT_STATES = ("applied", "created", "noop", "advanced")

# The monotone forward chain of ONE attempt's publication states
# (quarantined is deliberately absent — an operator quarantine is never a
# reconciliation away).
_STATE_RANK = {"doc_pending": 0, "doc_verified": 1, "pointer_queued": 2,
               "applied": 3}


def _read_hub_pointer(http: Any, cap_status: str, namespace: str,
                      target_id: str, logical_key: str) -> dict | None:
    """The AUTHORITATIVE predecessor read for remote capture.

    Only through the hub's capability-gated ``knowledge_pointer`` read
    route (the transport wrapper around the hub MCP tool; the hub must
    advertise ``knowledge_link_v1`` in ``hello`` first).  This is how a
    SECOND host stakes the pointer the hub actually holds instead of its
    own local sidecar.  Any failure is an honest None — never a raw-DB or
    side-channel fallback — because the pointer CAS at apply time remains
    the safety net: a wrong stake can only produce an honest cas_stale.
    """
    if cap_status != "supported":
        return None
    reader = getattr(http, "knowledge_pointer", None)
    if not callable(reader):
        return None  # transport without the read route: fall back honestly
    try:
        answer = reader(target_id, logical_key, namespace=namespace)
    except Exception:  # noqa: BLE001 - the read is an optimization, not truth
        return None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return None
    pointer = answer.get("pointer")
    if not isinstance(pointer, dict):
        return None
    row_id = pointer.get("row_id")
    version = pointer.get("version")
    if not row_id or not isinstance(version, int) or version < 1:
        return None
    return pointer


# ---------------------------------------------------------------------------
# journal orchestration (first-attempt tracking + changed-version escape)


def default_journal() -> KnowledgeJournal:
    """The default journal at ``<store_dir>/knowledge/journal.json`` (the
    journal's own locking creates the sidecar directory on first use)."""
    path = default_journal_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # the journal's own error reporting speaks for a bad dir
    return KnowledgeJournal(path)


def _journal_record(journal: KnowledgeJournal | None, *, target_id: str,
                    logical_key: str, locator: str, digest: str,
                    namespace: str = "") -> KnowledgeJournal | None:
    """record_intent, verbatim — the journal is the intent record.

    Same-digest retries stay idempotent whatever the entry's state, EXCEPT
    a quarantined entry: the operator pulled this publication, so
    re-publishing the same content is refused BEFORE the create.  Any
    other journal refusal (an ACTIVE entry with a different digest, a
    corrupt file, a busy lock) surfaces as an honest
    :class:`WorkflowError` BEFORE the create — no partial publication, no
    silent replacement.
    """
    if journal is None:
        return None
    try:
        journal.record_intent(target_id=target_id, logical_key=logical_key,
                              locator=locator, digest=digest,
                              namespace=namespace)
        entry = journal.entry_for(target_id, logical_key, namespace)
        if (entry is not None and entry.digest == digest
                and entry.state == "quarantined"):
            raise WorkflowError(
                "journal entry for knowledge:%s:%s (namespace %r) is "
                "quarantined; the operator must clear it before this "
                "content is re-published" % (target_id, logical_key,
                                             namespace))
        return journal
    except JournalError as exc:
        entry = journal.entry_for(target_id, logical_key, namespace)
        if (entry is not None and entry.digest == digest
                and entry.locator == locator):
            return journal  # same attempt (any state): still tracked
        raise WorkflowError(
            "journal refused the intent for knowledge:%s:%s (%s); the "
            "corrected multi-version journal is required for changed-"
            "version publication" % (target_id, logical_key, exc)) from exc


def _journal_mark(journal: KnowledgeJournal | None, *, target_id: str,
                  logical_key: str, state: str,
                  namespace: str = "") -> None:
    """Advance the journal; only a CHECKED reconciliation may absorb a
    refusal: the recorded state is identical, or it is strictly further
    along THIS attempt's monotone chain (a retry re-marking doc_verified
    when the journal already reached pointer_queued/applied).  Anything
    else — corrupt file, busy lock, quarantined entry, a state not on the
    forward path — is re-raised as :class:`WorkflowError` — a journal
    failure is never swallowed, so a capture can never report completed
    against a journal that did not record it."""
    if journal is None:
        return
    try:
        journal.mark(target_id=target_id, logical_key=logical_key,
                     state=state, namespace=namespace)
    except JournalError as exc:
        entry = journal.entry_for(target_id, logical_key, namespace)
        if entry is not None and (entry.state == state
                                  or (state in _STATE_RANK
                                      and entry.state in _STATE_RANK
                                      and _STATE_RANK[entry.state]
                                      > _STATE_RANK[state])):
            return  # identical or already-further-along the same attempt
        raise WorkflowError(
            "journal refused the %r transition for knowledge:%s:%s "
            "(namespace %r, latest state %r); the publication state is "
            "NOT recorded — resolve the journal before retrying" %
            (state, target_id, logical_key, namespace,
             entry.state if entry is not None else "unknown")) from exc


def _journal_quarantine(journal: KnowledgeJournal | None, *, target_id: str,
                        logical_key: str, namespace: str,
                        digest: str) -> str | None:
    """Drive the identity's ACTIVE entry for THIS attempt (digest match)
    to terminal ``quarantined``.

    A hub rejection or a pointer CAS conflict is final for this attempt:
    the entry must not stay ACTIVE forever (the identity could never
    record a new version).  The guard preserves everything else — an
    entry already driven to ``applied`` by a concurrent writer (the same
    screened content landed elsewhere) and any entry belonging to a
    NEWER attempt are left untouched.  Returns the entry's state after
    the attempt (``"quarantined"`` on a fresh transition, the untouched
    state, or None when no entry/journal exists)."""
    if journal is None:
        return None
    entry = journal.entry_for(target_id, logical_key, namespace)
    if entry is None:
        return None
    if entry.digest != digest or entry.state in TERMINAL_STATES:
        return entry.state
    _journal_mark(journal, target_id=target_id, logical_key=logical_key,
                  state="quarantined", namespace=namespace)
    return "quarantined"


# ---------------------------------------------------------------------------
# capture


def capture(*, target_id: str, logical_key: str, text: str,
            summary: str | None = None,
            namespace: str = _DEFAULT_NAMESPACE,
            mode: str = "local",
            conn=None,
            config=None,
            journal: KnowledgeJournal | None = None,
            delivery=None,
            http=None,
            env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Publish one immutable version and advance the pointer.

    Returns an explicit status envelope (never raises for target-side or
    CAS outcomes — those are reported; only programmer/usage errors and
    the screening refusal raise :class:`WorkflowError`).
    """
    if mode not in ("local", "remote"):
        raise WorkflowError("mode must be 'local' or 'remote'")
    if config is None:
        from knowledge.config import load_knowledge_config  # noqa: PLC0415

        config = load_knowledge_config(env=env)
    target = _require_target(config, target_id)
    if journal is None:
        journal = default_journal()
    if delivery is None:
        from knowledge.delivery import (  # noqa: PLC0415
            KnowledgeDelivery, default_delivery_path)

        delivery = KnowledgeDelivery(default_delivery_path())

    # 1. Admission: screen FIRST, normalize, hash, identify.
    try:
        document = prepare_document(target_id, logical_key, text)
    except DocumentRejected as exc:
        raise WorkflowError(str(exc)) from exc

    note_summary = (summary or "").strip() or _default_summary(document)
    # A user-supplied summary rides the pointer row and the delivery
    # sidecar VERBATIM (nothing downstream re-scans it), so it is screened
    # HERE with the same single pattern source as the content — any
    # detection rejects the publication BEFORE the journal intent, the
    # create and the queue (count only; the matched text never travels).
    if summary:
        _, summary_hits = redact_secret_like_text(note_summary)
        if summary_hits:
            raise WorkflowError(
                "summary rejected: secret-like pattern detected (%d "
                "hit(s)); rewrite the summary without the secret" %
                summary_hits)
    # Same admission bound the pointer handler enforces (MAX_SUMMARY_CHARS)
    # — checked HERE so an oversized summary is a clean refusal before any
    # write, not an uncaught payload error after the journal and create.
    summary_cap = getattr(_storelib_knowledge(), "MAX_SUMMARY_CHARS", 512)
    if len(note_summary) > summary_cap:
        raise WorkflowError(
            "summary must be at most %d characters (got %d); shorten it"
            % (summary_cap, len(note_summary)))
    ref = _ref_for(target_id, logical_key)

    # 2. Durable intent BEFORE the create.
    tracked_journal = _journal_record(
        journal, target_id=target_id, logical_key=logical_key,
        locator=document.locator, digest=document.digest,
        namespace=namespace)

    # 3. Predecessor observation BEFORE the create — and the stake is
    #    DURABLE before the create too (crash law): the observed
    #    expected_row_id/version for THIS digest is persisted in the
    #    delivery intent sidecar before the target create, and a retry of
    #    the same screened content REUSES that fixed stake instead of
    #    silently re-observing a pointer another writer has advanced (a
    #    moved pointer then means an honest cas_stale — never a silent
    #    supersede of a newer publication).
    store_knowledge = _storelib_knowledge()
    expected_row_id: str | None = None
    version = 1
    cap_status: str | None = None
    cap_detail: str | None = None
    prior_intent = (delivery.get_intent(ref, namespace=namespace)
                    if delivery is not None else None)
    # A staked sidecar stake is PRESERVED verbatim across retries even
    # when the journal already reached applied for this digest: the
    # attempt's own verdict is only provable against the AUTHORITATIVE
    # pointer (exact version+digest+locator — see the cas_stale
    # reconciliation below); a pointer another writer moved is never
    # silently superseded by a re-observation.
    if (prior_intent is not None
            and prior_intent.get("digest") == document.digest
            and prior_intent.get("staked")
            and prior_intent.get("state") not in _STAKE_SPENT_STATES):
        # Fixed stake from the first attempt (crash/retry): reuse verbatim.
        stake_row = prior_intent.get("expected_row_id")
        expected_row_id = str(stake_row) if stake_row else None
        try:
            version = int(prior_intent.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
    else:
        if mode == "local":
            if conn is None:
                raise WorkflowError("local capture requires an open store")
            current_pointer = store_knowledge.get_pointer(
                conn, namespace, target_id, logical_key)
            if current_pointer is not None:
                expected_row_id = current_pointer.row_id
                version = (current_pointer.version
                           if current_pointer.full_sha256 == document.digest
                           else current_pointer.version + 1)
        else:
            from knowledge.delivery import (  # noqa: PLC0415
                hub_capability_status)

            if http is None:
                http = build_hub_transport(env)
            cap_status, cap_detail = hub_capability_status(http)
            hub_pointer = _read_hub_pointer(
                http, cap_status, namespace, target_id, logical_key)
            if hub_pointer is not None:
                expected_row_id = str(hub_pointer["row_id"])
                version = (hub_pointer["version"]
                           if hub_pointer.get("full_sha256") == document.digest
                           else hub_pointer["version"] + 1)
            elif prior_intent is not None:
                # No authoritative read available (old hub, or a transport
                # without the read route): this host's sidecar predecessor
                # is the best stake available — the hub CAS still verifies.
                known_digest = prior_intent.get("digest")
                if prior_intent.get("row_id"):
                    expected_row_id = str(prior_intent["row_id"])
                try:
                    known_version = int(prior_intent.get("version") or 0)
                except (TypeError, ValueError):
                    known_version = 0
                if known_version >= 1:
                    version = (known_version
                               if known_digest == document.digest
                               else known_version + 1)
        # The stake outlives a crash at ANY later point: durable BEFORE
        # the create, keyed by the FULL identity (namespace|ref).
        delivery.set_intent(ref=ref, namespace=namespace, fields={
            "target": target_id, "key": logical_key, "namespace": namespace,
            "digest": document.digest, "locator": document.locator,
            "version": version, "expected_row_id": expected_row_id,
            "row_id": None, "state": "staked", "op_id": None,
            "staked": True,
        })

    # 4. Immutable create (readback-verified inside the target client).
    client = open_target(target, env=config.env)
    try:
        create_result: CreateResult = client.create(document)
    except (_TargetError, _DocConflictError) as exc:
        # The intent stays recorded (doc_pending) — a retry with the same
        # screened content resumes from here.
        raise WorkflowError(str(exc)) from exc
    _journal_mark(tracked_journal, target_id=target_id,
                  logical_key=logical_key, state="doc_verified",
                  namespace=namespace)

    envelope: dict[str, Any] = {
        "target": target_id,
        "key": logical_key,
        "ref": ref,
        "locator": document.locator,
        "digest": document.digest,
        "version": version,
        "existed_match": bool(create_result.existed_match),
        "mode": mode,
        "namespace": namespace,
        # The immutable version exists at the target from here on; ok /
        # completed below say whether the POINTER landed.
        "doc_published": True,
    }

    # 5. The pointer.
    if mode == "local":
        _journal_mark(tracked_journal, target_id=target_id,
                      logical_key=logical_key, state="pointer_queued",
                      namespace=namespace)
        try:
            result = store_knowledge.link_pointer(
                conn,
                namespace=namespace,
                target=target_id,
                key=logical_key,
                version=version,
                locator=document.locator,
                full_sha256=document.digest,
                summary=note_summary,
                expected_row_id=expected_row_id,
            )
        except store_knowledge.KnowledgePointerCasError as exc:
            # Crash-window reconciliation: a stale CAS against a pointer
            # that EXACTLY matches what THIS attempt sent means our link
            # already landed (the crash was after the SQLite commit, before
            # the verdict reached the intent/journal) — reconcile to the
            # noop result instead of failing cas_stale forever.  A pointer
            # another writer moved (different/newer version or content) is
            # NOT ours and keeps the honest cas_stale below.
            try:
                landed = store_knowledge.get_pointer(
                    conn, namespace, target_id, logical_key)
            except Exception:  # noqa: BLE001 - unreadable: keep cas_stale
                landed = None
            if _landed_match(landed, version=version,
                             digest=document.digest,
                             locator=document.locator):
                _journal_mark(tracked_journal, target_id=target_id,
                              logical_key=logical_key, state="applied",
                              namespace=namespace)
                delivery.set_intent(ref=ref, namespace=namespace, fields={
                    "target": target_id, "key": logical_key,
                    "namespace": namespace,
                    "digest": document.digest,
                    "locator": document.locator,
                    "version": landed.version,
                    "expected_row_id": expected_row_id,
                    "row_id": landed.row_id, "state": "applied",
                    "op_id": None, "staked": True,
                })
                envelope.update({
                    "ok": True,
                    "completed": True,
                    "pointer": {"status": "noop", "row_id": landed.row_id,
                                "reconciled_after_cas_stale": True},
                    "journal_state": "applied",
                    "journal_tracked": tracked_journal is not None,
                })
                return envelope
            entry = journal.entry_for(target_id, logical_key, namespace)
            envelope.update({
                "ok": False,
                "completed": False,
                "pointer": {"status": "cas_stale", "detail": str(exc)},
                "journal_state": entry.state if entry is not None else None,
            })
            delivery.set_intent(ref=ref, namespace=namespace, fields={
                "target": target_id, "key": logical_key,
                "namespace": namespace,
                "digest": document.digest, "locator": document.locator,
                "version": version, "expected_row_id": expected_row_id,
                "row_id": None, "state": "cas_stale", "op_id": None, "staked": True,
            })
            # Terminal, never ACTIVE-forever: the stale CAS is final for
            # THIS attempt (guard: an entry another writer already drove
            # to applied, or a newer attempt's entry, is untouched).
            envelope["journal_state"] = _journal_quarantine(
                tracked_journal, target_id=target_id,
                logical_key=logical_key, namespace=namespace,
                digest=document.digest) or envelope["journal_state"]
            return envelope
        except store_knowledge.KnowledgePointerConflict as exc:
            entry = journal.entry_for(target_id, logical_key, namespace)
            envelope.update({
                "ok": False,
                "completed": False,
                "pointer": {"status": "conflict", "detail": str(exc)},
                "journal_state": entry.state if entry is not None else None,
            })
            delivery.set_intent(ref=ref, namespace=namespace, fields={
                "target": target_id, "key": logical_key,
                "namespace": namespace,
                "digest": document.digest, "locator": document.locator,
                "version": version, "expected_row_id": expected_row_id,
                "row_id": None, "state": "conflict", "op_id": None, "staked": True,
            })
            envelope["journal_state"] = _journal_quarantine(
                tracked_journal, target_id=target_id,
                logical_key=logical_key, namespace=namespace,
                digest=document.digest) or envelope["journal_state"]
            return envelope
        _journal_mark(tracked_journal, target_id=target_id,
                      logical_key=logical_key, state="applied",
                      namespace=namespace)
        delivery.set_intent(ref=ref, namespace=namespace, fields={
            "target": target_id, "key": logical_key, "namespace": namespace,
            "digest": document.digest, "locator": document.locator,
            "version": result["version"], "expected_row_id": expected_row_id,
            "row_id": result["row_id"], "state": result["status"],
            "op_id": None,
        })
        entry = journal.entry_for(target_id, logical_key, namespace)
        envelope.update({
            "ok": True,
            "completed": True,
            "pointer": {
                "status": result["status"],
                "row_id": result["row_id"],
                "previous_row_id": result.get("previous_row_id"),
                "replaced_unverified": result.get("replaced_unverified", 0),
            },
            "journal_state": entry.state if entry is not None else None,
            "journal_tracked": tracked_journal is not None,
        })
        return envelope

    # Remote: capability BEFORE any enqueue; explicit pending otherwise.
    from knowledge.delivery import (  # noqa: PLC0415
        CAPABILITY, hub_capability_status)

    if http is None:
        http = build_hub_transport(env)
    if cap_status is None:
        cap_status, cap_detail = hub_capability_status(http)
    entry = journal.entry_for(target_id, logical_key, namespace)
    if cap_status != "supported":
        delivery.set_intent(ref=ref, namespace=namespace, fields={
            "target": target_id, "key": logical_key, "namespace": namespace,
            "digest": document.digest, "locator": document.locator,
            "version": version, "expected_row_id": expected_row_id,
            "row_id": None, "state": "pending", "op_id": None, "staked": True,
        })
        envelope.update({
            # The version IS published (doc_published) but the workflow is
            # NOT complete: ok/completed stay false until the pointer
            # lands (the CLI maps pending to its distinct exit code).
            "ok": False,
            "completed": False,
            "pointer": {
                "status": "pending",
                "reason": "hub-%s" % cap_status,
                "detail": cap_detail,
                "capability": CAPABILITY,
                "enqueued": False,
            },
            "journal_state": entry.state if entry is not None else None,
            "journal_tracked": tracked_journal is not None,
        })
        return envelope

    queued = delivery.enqueue({
        "namespace": namespace,
        "target": target_id,
        "key": logical_key,
        "version": version,
        "locator": document.locator,
        "fullhash": _chunks_from_digest(document.digest),
        "summary": note_summary,
        "date": _now_iso(),
        "expected_row_id": expected_row_id,
    })
    _journal_mark(tracked_journal, target_id=target_id,
                  logical_key=logical_key, state="pointer_queued",
                  namespace=namespace)
    delivery.set_intent(ref=ref, namespace=namespace, fields={
        "target": target_id, "key": logical_key, "namespace": namespace,
        "digest": document.digest, "locator": document.locator,
        "version": version, "expected_row_id": expected_row_id,
        "row_id": None, "state": "queued", "op_id": queued["op_id"], "staked": True,
    })
    # Best-effort immediate delivery through the SHARED flush (the same
    # reconciliation as the standalone ``knowledge flush``): the drain may
    # carry EARLIER queued entries from this store too, and every one of
    # them gets its journal/intent reconciled — not just this capture's
    # op.  Failure leaves the explicit queued status (knowledge flush
    # retries).
    applied_op_ids: list[str] = []
    flush_result: dict[str, Any] = {"ok": False, "reason": "not-attempted"}
    try:
        flush_result = flush(journal=journal, delivery=delivery, http=http)
        applied_op_ids = list(flush_result.get("applied") or [])
    except Exception as exc:  # noqa: BLE001 - transport failure is a status
        flush_result = {"ok": False,
                        "reason": exc.__class__.__name__}
    pointer_status = ("applied" if queued["op_id"] in applied_op_ids
                      else "rejected"
                      if flush_result.get("rejected") and any(
                          r.get("op_id") == queued["op_id"]
                          for r in flush_result["rejected"])
                      else "queued")
    reconciled_row = None
    if pointer_status == "rejected":
        receipt_error = next(
            (str(r.get("error") or "")
             for r in (flush_result.get("rejected") or [])
             if r.get("op_id") == queued["op_id"]), "")
        if receipt_error == "pointer_cas_stale":
            # Crash-window reconciliation (remote): a cas-stale against a
            # hub pointer that EXACTLY matches what THIS attempt queued
            # means the op already landed through an earlier flush (crash
            # after the engine commit, before the verdict reached the
            # journal/intent).  Provable ONLY through the AUTHORITATIVE
            # hub read — never this host's sidecar.
            read_state, hub_pointer = _hub_pointer_for_read(
                http, namespace, target_id, logical_key)
            if (read_state == "ok" and _landed_match(
                    hub_pointer, version=version,
                    digest=document.digest,
                    locator=document.locator)):
                reconciled_row = hub_pointer
                pointer_status = "applied"
    if pointer_status == "applied":
        _journal_mark(tracked_journal, target_id=target_id,
                      logical_key=logical_key, state="applied",
                      namespace=namespace)
        receipt = delivery.receipts().get(queued["op_id"]) or {}
        prior_row = (delivery.get_intent(ref, namespace=namespace)
                     or {}).get("row_id")
        landed_row = (receipt.get("row_id") if receipt.get("row_id")
                      is not None else prior_row)
        delivery.set_intent(ref=ref, namespace=namespace, fields={
            "target": target_id, "key": logical_key, "namespace": namespace,
            "digest": document.digest, "locator": document.locator,
            "version": (int(_field(reconciled_row, "version") or 0)
                        if reconciled_row is not None
                        else receipt.get("version", version)),
            "expected_row_id": expected_row_id,
            "row_id": landed_row,
            "state": "applied",
            "staked": True,
            "op_id": queued["op_id"],
        })
    else:
        delivery.set_intent(ref=ref, namespace=namespace, fields={
            "target": target_id, "key": logical_key, "namespace": namespace,
            "digest": document.digest, "locator": document.locator,
            "version": version, "expected_row_id": expected_row_id,
            "row_id": None, "state": pointer_status,
            "op_id": queued["op_id"], "staked": True,
        })
        if pointer_status == "rejected":
            # The queue entry is already gone (the flush removed it with
            # its receipt), so a later flush can never quarantine it: the
            # rejected attempt's journal entry is terminal HERE or never.
            _journal_quarantine(tracked_journal, target_id=target_id,
                                logical_key=logical_key,
                                namespace=namespace,
                                digest=document.digest)
    entry = journal.entry_for(target_id, logical_key, namespace)
    rejected_receipt = None
    if pointer_status == "rejected":
        rejected_receipt = next(
            (r for r in (flush_result.get("rejected") or [])
             if r.get("op_id") == queued["op_id"]), None)
    envelope.update({
        "ok": pointer_status == "applied",
        "completed": pointer_status == "applied",
        "pointer": {
            "status": pointer_status,
            "op_id": queued["op_id"],
            "enqueued": True,
            "flush": {
                "ok": bool(flush_result.get("ok")),
                "reason": flush_result.get("reason"),
                "pending": flush_result.get("pending", 0),
            },
            **({"receipt": rejected_receipt}
               if rejected_receipt is not None else {}),
            **({"reconciled_after_rejection": True}
               if reconciled_row is not None else {}),
        },
        "journal_state": entry.state if entry is not None else None,
        "journal_tracked": tracked_journal is not None,
    })
    return envelope


# ---------------------------------------------------------------------------
# read / check

# Hub-read refusal REASONS that must NEVER fall back to local state (the
# hub denied the caller — reading local sidecars would bypass that).
# Exact stable machine codes from the transport's typed classification;
# never matched by substring.
_READ_REFUSAL_REASONS = ("namespace_not_allowed", "authorization_denied",
                         "capability_denied", "principal_mismatch")


def _hub_pointer_for_read(http: Any, namespace: str, target_id: str,
                          logical_key: str) -> tuple[str, dict | None]:
    """The AUTHORITATIVE current pointer for a remote reader.

    Returns ``(state, pointer)``:

    - ``("ok", pointer_or_None)`` — the hub answered; pointer None means
      the hub holds no verified pointer for the identity.
    - ``("refused", None)`` — the hub DENIED the read (authorization or
      namespace guard): never fall back past this.
    - ``("unavailable", None)`` — old/unreachable hub or a failed call:
      local sidecars may then be used, labeled stale.
    """
    from knowledge.delivery import hub_capability_status  # noqa: PLC0415

    cap_status, _detail = hub_capability_status(http)
    reader = getattr(http, "knowledge_pointer", None)
    if cap_status != "supported" or not callable(reader):
        return "unavailable", None
    try:
        answer = reader(target_id, logical_key, namespace=namespace)
    except Exception:  # noqa: BLE001 - a failed read is a state, not a raise
        return "unavailable", None
    if not isinstance(answer, dict):
        return "unavailable", None
    if answer.get("ok") is not True:
        reason = str(answer.get("reason") or answer.get("error") or "")
        if reason in _READ_REFUSAL_REASONS:
            return "refused", None
        return "unavailable", None
    pointer = answer.get("pointer")
    if pointer is None:
        return "ok", None
    if (not isinstance(pointer, dict)
            or not isinstance(pointer.get("locator"), str)
            or not isinstance(pointer.get("full_sha256"), str)
            or len(pointer["full_sha256"]) != 64):
        return "unavailable", None
    return "ok", pointer


def _resolve_current(*, mode: str, conn, namespace: str, delivery,
                     journal, target_id: str, logical_key: str,
                     locator: str | None, http=None,
                     env: Mapping[str, str] | None = None):
    """Resolve (locator, expected_digest, provenance) for read/check.

    Order: an explicit locator argument; then the AUTHORITATIVE pointer —
    the local store's in local mode, the hub's ``knowledge_pointer`` read
    in remote mode (so a second host with empty/stale local sidecars
    reads the current version; a DENIED hub read never falls back, an
    unavailable one falls back to local sidecars labeled ``-stale``);
    then the workflow intent sidecar (pending/unapplied publications);
    then the journal entry.  Returns None when nothing names a version."""
    if locator is not None:
        from knowledge.documents import parse_locator  # noqa: PLC0415

        key, digest = parse_locator(locator)
        if key != logical_key:
            raise WorkflowError(
                "locator %r does not address key %r" % (locator, logical_key))
        return locator, digest, "locator-arg"
    stale = ""
    if mode == "remote":
        from knowledge.documents import parse_locator  # noqa: PLC0415

        if http is None:
            http = build_hub_transport(env)
        state, hub_pointer = _hub_pointer_for_read(
            http, namespace, target_id, logical_key)
        if state == "refused":
            raise WorkflowError(
                "the hub refused the knowledge pointer read for "
                "knowledge:%s:%s (namespace %r); local fallback would "
                "bypass the hub's access decision" %
                (target_id, logical_key, namespace))
        if hub_pointer is not None:
            try:
                key, digest = parse_locator(hub_pointer["locator"])
            except ValueError as exc:
                raise WorkflowError(
                    "hub pointer locator malformed for knowledge:%s:%s"
                    % (target_id, logical_key)) from exc
            if key != logical_key or digest != hub_pointer["full_sha256"]:
                raise WorkflowError(
                    "hub pointer locator does not address key %r"
                    % logical_key)
            return (hub_pointer["locator"], hub_pointer["full_sha256"],
                    "hub-pointer")
        if state == "unavailable":
            stale = "-stale"
    ref = _ref_for(target_id, logical_key)
    if mode == "local" and conn is not None:
        store_knowledge = _storelib_knowledge()
        pointer = store_knowledge.get_pointer(
            conn, namespace, target_id, logical_key)
        if pointer is not None:
            return pointer.locator, pointer.full_sha256, "pointer"
    intent = (delivery.get_intent(ref, namespace=namespace)
              if delivery is not None else None)
    if intent is not None and intent.get("locator"):
        digest = intent.get("digest")
        if isinstance(digest, str) and len(digest) == 64:
            return str(intent["locator"]), digest, "intent" + stale
    if journal is not None:
        entry = journal.entry_for(target_id, logical_key, namespace)
        if entry is not None:
            return entry.locator, entry.digest, "journal" + stale
    return None


def read(*, target_id: str, logical_key: str, locator: str | None = None,
         namespace: str = _DEFAULT_NAMESPACE, mode: str = "local",
         conn=None, config=None, journal=None, delivery=None, http=None,
         env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read one immutable version's canonical bytes from the target.

    In remote mode the CURRENT version is the hub's authoritative pointer
    (``http`` defaults to the configured hub transport)."""
    if config is None:
        from knowledge.config import load_knowledge_config  # noqa: PLC0415

        config = load_knowledge_config(env=env)
    target = _require_target(config, target_id)
    if journal is None:
        journal = default_journal()
    if delivery is None:
        from knowledge.delivery import (  # noqa: PLC0415
            KnowledgeDelivery, default_delivery_path)

        delivery = KnowledgeDelivery(default_delivery_path())
    resolved = _resolve_current(
        mode=mode, conn=conn, namespace=namespace, delivery=delivery,
        journal=journal, target_id=target_id, logical_key=logical_key,
        locator=locator, http=http, env=env)
    if resolved is None:
        raise WorkflowError(
            "no version recorded for knowledge:%s:%s (capture first, or "
            "pass --locator)" % (target_id, logical_key))
    use_locator, expected_digest, provenance = resolved
    client = open_target(target, env=config.env)
    try:
        content = client.read(use_locator)
    except _TargetError as exc:
        raise WorkflowError(str(exc)) from exc
    from knowledge.documents import content_digest  # noqa: PLC0415

    return {
        "ok": True,
        "target": target_id,
        "key": logical_key,
        "locator": use_locator,
        "expected_digest": expected_digest,
        "actual_digest": content_digest(content),
        "provenance": provenance,
        "content": content.decode("utf-8"),
    }


def check(*, target_id: str, logical_key: str, locator: str | None = None,
          namespace: str = _DEFAULT_NAMESPACE, mode: str = "local",
          conn=None, config=None, journal=None, delivery=None, http=None,
          env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Integrity check: re-read the recorded version and compare digests."""
    result = read(target_id=target_id, logical_key=logical_key,
                  locator=locator, namespace=namespace, mode=mode,
                  conn=conn, config=config, journal=journal,
                  delivery=delivery, http=http, env=env)
    matches = result["actual_digest"] == result["expected_digest"]
    return {
        "ok": matches,
        "target": target_id,
        "key": logical_key,
        "locator": result["locator"],
        "expected_digest": result["expected_digest"],
        "actual_digest": result["actual_digest"],
        "matches": matches,
        "provenance": result["provenance"],
    }


# ---------------------------------------------------------------------------
# flush (remote delivery) + status


def flush(*, journal=None, delivery=None, http=None,
          env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Drain the knowledge delivery queue to the hub and reconcile BOTH
    recovery records:

    - journal: ``pointer_queued -> applied`` for applied op ids; a
      REJECTED attempt's entry is driven to ``quarantined`` (terminal) so
      the identity is never stuck ACTIVE forever — history is retained,
      and the next capture with NEW content records a fresh entry.
    - delivery intents: the applied receipt's ``row_id``/``version``
      land in the intent (state ``applied``), so the NEXT capture stakes
      the pointer the hub actually holds even when the authoritative hub
      read route is unavailable.  A newer in-flight stake for the same
      identity is never clobbered.

    Restart recovery: a flush that crashed AFTER its receipts were
    persisted and its entries removed, but BEFORE this reconciliation,
    left the journal ACTIVE and the intent queued with the op's receipt
    on disk — the empty queue must not end recovery.  Such orphans are
    re-reconciled from the PERSISTED receipt + intent (the intent carries
    the closed metadata: digest, locator, version, stake).
    """
    from knowledge.delivery import (  # noqa: PLC0415
        KnowledgeDelivery, default_delivery_path, flush_to_hub)

    if delivery is None:
        delivery = KnowledgeDelivery(default_delivery_path())
    if journal is None:
        journal = default_journal()
    before = {e["op_id"]: e for e in delivery.pending()}
    if before and http is None:
        http = build_hub_transport(env)
    result: dict[str, Any] = (flush_to_hub(delivery, http) if before else
                              {"ok": True, "applied": [], "rejected": [],
                               "pending": 0})
    receipts = delivery.receipts()
    reconciled: list[str] = []
    quarantined: list[str] = []

    def _digest_of(record: Mapping[str, Any]) -> str | None:
        """The attempt digest from either shape: a queue entry (chunked
        fullhash) or an intent (whole digest)."""
        digest = record.get("digest")
        if isinstance(digest, str) and len(digest) == 64:
            try:
                int(digest, 16)
            except ValueError:
                return None
            return digest.lower()
        return _digest_from_chunks(record.get("fullhash"))

    def _attempt_is_current(ref: str, ns: str, digest: str | None,
                            op_id: str) -> bool:
        """The intent still names THIS attempt (no newer capture replaced
        the stake in the meantime)."""
        current = delivery.get_intent(ref, namespace=ns)
        return (current is None
                or (current.get("digest") == digest
                    and current.get("op_id") in (None, op_id)))

    def _reconcile_applied(view: Mapping[str, Any], digest: str,
                           receipt: Mapping[str, Any]) -> None:
        entry_ns = str(view.get("namespace", "") or "")
        target = str(view.get("target", ""))
        key = str(view.get("key", ""))
        jentry = journal.entry_for(target, key, entry_ns)
        if jentry is not None and jentry.digest == digest:
            if jentry.state == "doc_pending":
                _journal_mark(journal, target_id=target, logical_key=key,
                              state="doc_verified", namespace=entry_ns)
            if jentry.state in ("doc_pending", "doc_verified"):
                _journal_mark(journal, target_id=target, logical_key=key,
                              state="pointer_queued", namespace=entry_ns)
            _journal_mark(journal, target_id=target, logical_key=key,
                          state="applied", namespace=entry_ns)
            reconciled.append(str(view["op_id"]))
        ref = _ref_for(target, key)
        if _attempt_is_current(ref, entry_ns, digest,
                               str(view["op_id"])):
            delivery.set_intent(ref=ref, namespace=entry_ns, fields={
                "target": target, "key": key, "namespace": entry_ns,
                "digest": digest,
                "locator": str(view.get("locator", "")),
                "version": receipt.get("version", view.get("version")),
                "expected_row_id": view.get("expected_row_id"),
                "row_id": receipt.get("row_id"),
                "state": "applied", "op_id": str(view["op_id"]),
                "staked": True,
            })

    def _reconcile_rejected(view: Mapping[str, Any], digest: str,
                            recorded: Mapping[str, Any]) -> None:
        entry_ns = str(view.get("namespace", "") or "")
        target = str(view.get("target", ""))
        key = str(view.get("key", ""))
        # The hub rejected THIS attempt's pointer op: drive its journal
        # entry to terminal quarantined (history retained; an entry
        # already applied, or a newer attempt's entry, is untouched) so
        # the identity is not ACTIVE forever — the next capture with NEW
        # content records a fresh entry.
        if _journal_quarantine(journal, target_id=target,
                               logical_key=key, namespace=entry_ns,
                               digest=digest) == "quarantined":
            quarantined.append(str(view["op_id"]))
        ref = _ref_for(target, key)
        if _attempt_is_current(ref, entry_ns, digest,
                               str(view["op_id"])):
            delivery.set_intent(ref=ref, namespace=entry_ns, fields={
                "target": target, "key": key, "namespace": entry_ns,
                "digest": digest,
                "locator": str(view.get("locator", "")),
                "version": view.get("version"),
                "expected_row_id": view.get("expected_row_id"),
                "row_id": recorded.get("row_id"),
                "state": "rejected", "op_id": str(view["op_id"]),
                "staked": True,
            })

    def _cas_stale_landed(view: Mapping[str, Any], digest: str,
                          error_code: str):
        """A pointer_cas_stale rejection proven to be the crash window:
        the AUTHORITATIVE hub pointer exactly matches what THIS attempt
        sent (version + FULL digest + locator), so the op landed through
        an earlier flush — return the landed hub row, else None.  The
        proof is the hub read only, never this host's sidecar."""
        if error_code != "pointer_cas_stale":
            return None
        nonlocal http
        if http is None:
            http = build_hub_transport(env)
        read_state, hub_pointer = _hub_pointer_for_read(
            http, str(view.get("namespace", "") or ""),
            str(view.get("target", "")), str(view.get("key", "")))
        try:
            sent_version = int(view.get("version") or 0)
        except (TypeError, ValueError):
            return None
        if (read_state == "ok" and _landed_match(
                hub_pointer, version=sent_version, digest=digest,
                locator=str(view.get("locator", "")))):
            return hub_pointer
        return None

    for op_id in result.get("applied") or []:
        entry = before.get(op_id)
        if entry is None:
            continue
        digest = _digest_of(entry)
        if digest is not None:
            _reconcile_applied(entry, digest,
                               receipts.get(op_id) or {})
    reconciled_rejections: list[str] = []
    for recorded in list(result.get("rejected") or []):
        op_id = recorded.get("op_id")
        entry = before.get(op_id)
        if entry is None:
            continue
        digest = _digest_of(entry)
        if digest is None:
            continue
        landed = _cas_stale_landed(
            entry, digest, str(recorded.get("error") or ""))
        if landed is not None:
            # The rejection was the crash window: reconcile as APPLIED
            # (and report it on the applied side of the result).
            _reconcile_applied(entry, digest, {
                "row_id": _field(landed, "row_id"),
                "version": _field(landed, "version")})
            reconciled_rejections.append(str(op_id))
            result["rejected"] = [r for r in result["rejected"]
                                  if r.get("op_id") != op_id]
            result["applied"] = list(result.get("applied") or []) \
                + [op_id]
            continue
        _reconcile_rejected(entry, digest, recorded)

    # Restart recovery: receipts persisted by a flush that crashed before
    # this reconciliation.  The queue replay cannot cover these (the
    # entry left the queue WITH its receipt) — the still-queued intent
    # naming the op is the recovery key.
    recovered = 0
    for op_id, receipt in receipts.items():
        if op_id in before or not isinstance(receipt, dict):
            continue
        intent = next(
            (r for r in delivery.snapshot()["intents"].values()
             if isinstance(r, dict) and r.get("op_id") == op_id
             and r.get("state") not in _STAKE_SPENT_STATES
             and r.get("state") not in ("rejected", "cas_stale",
                                        "conflict", "pending")),
            None)
        if intent is None:
            continue
        digest = _digest_of(intent)
        if digest is None:
            continue
        status_value = str(receipt.get("status", ""))
        if status_value in ("committed", "duplicate"):
            _reconcile_applied(intent, digest, receipt)
            recovered += 1
            continue
        landed = _cas_stale_landed(
            intent, digest, str(receipt.get("error") or ""))
        if landed is not None:
            _reconcile_applied(intent, digest, {
                "row_id": _field(landed, "row_id"),
                "version": _field(landed, "version")})
            recovered += 1
        else:
            _reconcile_rejected(intent, digest, receipt)
            recovered += 1
    result["journal_reconciled"] = reconciled
    result["journal_quarantined"] = quarantined
    result["recovered_from_receipts"] = recovered
    remaining = delivery.pending()
    result["pending"] = len(remaining)
    result["ok"] = (not remaining and not (result.get("rejected") or [])
                    and not quarantined)
    return result


def status(*, target_id: str | None = None, logical_key: str | None = None,
           namespace: str = _DEFAULT_NAMESPACE, mode: str = "local",
           conn=None, config=None, journal=None, delivery=None,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The operator's one-stop view: config, journal, delivery queue,
    receipts, and (local mode) the authoritative pointers."""
    if config is None:
        from knowledge.config import load_knowledge_config  # noqa: PLC0415

        config = load_knowledge_config(env=env)
    if journal is None:
        journal = default_journal()
    if delivery is None:
        from knowledge.delivery import (  # noqa: PLC0415
            KnowledgeDelivery, default_delivery_path)

        delivery = KnowledgeDelivery(default_delivery_path())

    def _wanted(tid: str, key: str) -> bool:
        if target_id is not None and tid != target_id:
            return False
        if logical_key is not None and key != logical_key:
            return False
        return True

    journal_rows = [
        {"seq": e.seq, "target": e.target_id, "key": e.logical_key,
         "ref": e.ref, "locator": e.locator, "digest": e.digest,
         "state": e.state, "updated_at": e.updated_at,
         "namespace": e.namespace}
        for e in journal.entries() if _wanted(e.target_id, e.logical_key)
    ]
    snap = delivery.snapshot()
    queue_rows = [dict(e) for e in snap["queue"]
                  if _wanted(str(e.get("target", "")),
                             str(e.get("key", "")))]
    receipt_rows = [r for r in snap["receipts"].values()
                    if _wanted(str(r.get("target", "")),
                               str(r.get("key", "")))]

    out: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "enabled": bool(config.enabled),
        "targets": {
            tid: {"kind": t.kind} for tid, t in config.targets.items()
        },
        "namespace": namespace,
        "journal": journal_rows,
        "delivery": {
            "queue": queue_rows,
            "receipts": sorted(
                receipt_rows, key=lambda r: str(r.get("received_at", "")),
                reverse=True),
        },
    }
    if mode == "remote":
        out["pointers"] = None
        out["hub_note"] = ("pointer rows live on the hub; local view shows "
                           "journal + delivery sidecars only")
        return out

    store_knowledge = _storelib_knowledge()
    identities: set[tuple[str, str]] = set()
    for row in journal_rows:
        identities.add((row["target"], row["key"]))
    for row in queue_rows:
        identities.add((str(row.get("target", "")),
                        str(row.get("key", ""))))
    for ref_record in snap["intents"].values():
        identities.add((str(ref_record.get("target", "")),
                        str(ref_record.get("key", ""))))
    if target_id is not None and logical_key is not None:
        identities.add((target_id, logical_key))
    pointers = []
    if conn is not None:
        for tid, key in sorted(identities):
            try:
                pointer = store_knowledge.get_pointer(
                    conn, namespace, tid, key)
                rows = store_knowledge.get_pointer_rows(
                    conn, namespace, tid, key)
            except ValueError:
                continue  # grammar-refused identity: shown via journal rows
            pointers.append({
                "target": tid,
                "key": key,
                "pointer": (None if pointer is None else {
                    "row_id": pointer.row_id,
                    "version": pointer.version,
                    "logical_id": pointer.logical_id,
                    "locator": pointer.locator,
                    "full_sha256": pointer.full_sha256,
                    "summary": pointer.summary,
                    "date": pointer.date,
                }),
                "rows": [{"row_id": r["row_id"],
                          "verified": r["verified"]} for r in rows],
            })
    out["pointers"] = pointers
    return out
