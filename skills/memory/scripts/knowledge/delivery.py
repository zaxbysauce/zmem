"""Capability-gated knowledge delivery (PR1, plan 2026-11-13).

The knowledge companion NEVER rides the core operation queue
(``fleet.opqueue`` enqueue/flush, the ``add``/``update`` batch lane): the
plan's isolation law says a knowledge op must not poison or be poisoned by
core batches, and an OLD hub must never receive an op kind it does not
know.  Instead this module owns a SEPARATE, bounded, metadata-only queue
plus a delivery-receipt/intent sidecar, both in one JSON file beside the
knowledge journal (``<store_dir>/knowledge/delivery.json``; in remote mode
the store-dir resolver lands in the data dir, exactly like the other fleet
sidecars):

    {"version": 1,
     "queue":     [<knowledge_link op entry>, ...],   # FIFO, metadata only
     "receipts":  {op_id: <receipt>},                 # bounded
     "intents":   {ref: <intent record>}}             # bounded

Laws (approved plan + PR1 integration notes):

- CAPABILITY BEFORE QUEUE: nothing is enqueued until the configured hub
  answers ``hello`` advertising ``knowledge_link_v1``.  An old hub (or an
  unreachable one) means NO enqueue and an explicit pending status — never
  a core-queue fallback, never a protocol bump, never an unknown op in a
  core batch.
- ROLLBACK PAUSE: ``flush_to_hub`` re-checks the capability before every
  apply; a hub that lost it (rollback) leaves every entry queued (paused
  pending) instead of sending the op to a hub that would reject it into
  its applied_ops ledger.
- METADATA ONLY: queue entries carry identity/locator/digest-chunks/summary
  — never the note body.  The 64-hex digest travels as EIGHT 8-hex chunks
  (``fullhash``), exactly the storage form of the pointer payload, so no
  >=32-hex run exists anywhere in the file.
- RECEIPTS ARE NOT SUCCESS: a ``queued`` entry is distinct from an
  ``applied`` one in every JSON surface; a rejected apply removes the entry
  (the hub's applied_ops PK would replay the rejection forever) and keeps
  the receipt for the operator.
- The sidecar is private, atomic (tmp + fsync + os.replace), bounded and
  locked with the SAME OS byte-lock discipline as the knowledge journal
  (msvcrt/fcntl on a persistent .lock file; a live lock is never deleted,
  never taken over by age — the kernel releases it when its holder exits).
- FULL IDENTITY: intents are keyed ``<namespace>|<ref>`` and queue
  idempotence matches (namespace, target, key, locator) — the namespace is
  part of every identity, so two namespaces never share or overwrite a
  stake, an intent, or a queue entry.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from knowledge.locking import try_lock_nb as _try_lock_nb, unlock as _unlock

__all__ = [
    "CAPABILITY",
    "DeliveryError",
    "KnowledgeDelivery",
    "OP_KIND",
    "build_link_op",
    "default_delivery_path",
    "delivery_path_for_store",
    "flush_to_hub",
    "hub_capability_status",
    "new_op_id",
]

# The hub advertises this capability in ``hello`` iff its ops engine really
# implements the knowledge_link op (additive; never a protocol bump).
CAPABILITY = "knowledge_link_v1"
OP_KIND = "knowledge_link"

# Stable machine codes a hub-side DENIAL of a knowledge apply carries (the
# transport's typed classification).  Nonretryable: a denied op is driven
# to terminal rejected, never requeued forever.
NONRETRYABLE_DENIAL_REASONS = ("namespace_not_allowed",
                               "authorization_denied",
                               "capability_denied",
                               "principal_mismatch")

_DELIVERY_VERSION = 1
_DEFAULT_MAX_QUEUE = 256
_DEFAULT_MAX_RECEIPTS = 512
_DEFAULT_MAX_INTENTS = 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
# The engine accepts 256 ops per batch; the knowledge lane stays well under.
_FLUSH_BATCH = 64

_REF_RE = re.compile(r"^knowledge:[^\s:]+:[^\s:]+$")

# Receipt field caps at the sidecar write: hub answers are
# operator-adjacent, not trusted, so ids and field values stay small and
# malformed/oversize fields are a typed refusal — never silently stored.
_MAX_OP_ID_CHARS = 128
_MAX_RECEIPT_KEY_CHARS = 64
_MAX_RECEIPT_FIELD_CHARS = 1024

# Intent states that are SETTLED for the sidecar's own retention law (the
# publication reached a verdict: the workflow's stake-spent set plus its
# terminal rejection states).  Everything else — "staked", "pending",
# "queued" or anything unrecognized — is IN FLIGHT: an in-flight stake is
# never evicted while a settled intent remains (FB-014; the journal's
# _retained law, mirrored for the intent map).  This is the DELIVERY
# module's own vocabulary — never imported from storelib.rekey, which
# keeps its own copy for the rekey fence.
_INTENT_SETTLED_STATES = ("applied", "created", "noop", "advanced",
                          "rejected", "cas_stale", "conflict")


class DeliveryError(RuntimeError):
    """A delivery-side failure.  Safe detail only — ids, states, reasons;
    never note content, never a token."""


def default_delivery_path() -> Path:
    """``<store_dir>/knowledge/delivery.json`` — resolved through the same
    store-dir chain as the journal (``storelib.config.config_path``)."""
    from storelib.config import config_path

    return config_path().parent / "knowledge" / "delivery.json"


def delivery_path_for_store(store_dir: Path | str) -> Path:
    """``<store_dir>/knowledge/delivery.json`` constructed DIRECTLY from
    the authoritative store directory (FB-031 rekey coordination).

    Unlike :func:`default_delivery_path` this never consults the
    store-dir resolver chain: the rekey coordination guard owns the
    exact store directory and must address the exact sidecar the
    workflow uses regardless of the ambient environment.
    """
    return Path(store_dir) / "knowledge" / "delivery.json"


def new_op_id() -> str:
    """A fresh op id for one knowledge_link delivery attempt.

    Dashed UUID form: the longest contiguous hex run is 12, so the id can
    never trip (or be redacted by) the generic 32+ hex secret pattern
    anywhere it is persisted."""
    return "kl-" + str(uuid.uuid4())


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.match(ref) or len(ref) > 256:
        raise DeliveryError("invalid knowledge ref %r" % (ref,))
    return ref


def _validate_intent_namespace(namespace: str) -> str:
    """The intent key's namespace half: bounded, pipe-free (the key
    separator) — the full identity is namespace + ref, never the bare ref
    (cross-namespace crosstalk fix)."""
    if (not isinstance(namespace, str) or len(namespace) > 256
            or "|" in namespace):
        raise DeliveryError(
            "invalid namespace for the knowledge intent key")
    return namespace


def build_link_op(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Render one queue entry as a typed-op envelope for the hub.

    The args object is the CLOSED whitelist the ops engine accepts — no
    content, no paths, no extra fields.  ``fullhash`` stays an array of
    eight 8-hex chunks; the engine reassembles and validates it.
    """
    return {
        "op_id": str(entry["op_id"]),
        "op": OP_KIND,
        "args": {
            "namespace": str(entry["namespace"]),
            "target": str(entry["target"]),
            "key": str(entry["key"]),
            "version": int(entry["version"]),
            "locator": str(entry["locator"]),
            "fullhash": list(entry["fullhash"]),
            "summary": str(entry["summary"]),
            "date": entry["date"],
            "expected_row_id": entry.get("expected_row_id"),
        },
    }


class KnowledgeDelivery:
    """The per-store knowledge delivery sidecar (queue + receipts + intents).

    Every mutating operation takes the advisory cross-process lock, re-reads
    the file, applies its change in memory, and rewrites atomically.
    """

    def __init__(self, path: Path | str, *,
                 max_queue: int = _DEFAULT_MAX_QUEUE,
                 max_receipts: int = _DEFAULT_MAX_RECEIPTS,
                 max_intents: int = _DEFAULT_MAX_INTENTS,
                 lock_timeout_s: float = 5.0,
                 stale_lock_s: float = 30.0) -> None:
        """``stale_lock_s`` is accepted for signature compatibility and
        IGNORED: locking is an OS byte lock the kernel releases when its
        holder exits, so age-based takeover no longer exists (a live lock
        is never deleted)."""
        if max_queue < 1 or max_receipts < 1 or max_intents < 1:
            raise DeliveryError("delivery bounds must be >= 1")
        self._path = Path(path)
        self._max_queue = max_queue
        self._max_receipts = max_receipts
        self._max_intents = max_intents
        self._lock_timeout_s = lock_timeout_s
        self._stale_lock_s = stale_lock_s

    # -- locking (advisory, cross-process, Windows-safe; journal parity)

    @property
    def _lock_path(self) -> Path:
        return self._path.with_name(self._path.name + ".lock")

    @contextlib.contextmanager
    def _locked(self):
        """OS byte lock (``msvcrt``/``fcntl``) on a persistent lock file —
        the knowledge.journal discipline, not a lockfile-unlink scheme.

        A live lock is NEVER deleted and never taken over by age: the
        kernel releases the byte lock automatically when its holder exits
        or crashes, which removes the stale-age unlink race entirely (an
        age-based takeover could delete a lock a live writer had just
        re-created).  The parent directory is created BEFORE the open, so
        a first-ever sidecar write can take the lock at all.  Bounded
        non-blocking retry; a busy lock is an explicit error."""
        lock = self._lock_path
        try:
            with contextlib.suppress(OSError):
                lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise DeliveryError(
                "delivery lock file unavailable (%s)" %
                exc.__class__.__name__) from exc
        deadline = time.monotonic() + self._lock_timeout_s
        try:
            while not _try_lock_nb(fd):
                if time.monotonic() >= deadline:
                    raise DeliveryError(
                        "delivery lock busy after %.1fs (another writer "
                        "holds the OS lock; it is released automatically "
                        "when that writer exits): %r" %
                        (self._lock_timeout_s, str(lock)))
                time.sleep(0.05)
        except BaseException:
            # The descriptor is owned by THIS attempt: the busy timeout
            # and a FATAL locking.LockError (REL-015) both release it.
            os.close(fd)
            raise
        try:
            yield
        finally:
            _unlock(fd)
            os.close(fd)

    # -- persistence (atomic, durable, private, bounded)

    def _read(self) -> dict[str, Any]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {"queue": [], "receipts": {}, "intents": {}}
        except OSError as exc:
            raise DeliveryError(
                "delivery sidecar unreadable (%s)" %
                exc.__class__.__name__) from exc
        if not raw:
            return {"queue": [], "receipts": {}, "intents": {}}
        if len(raw) > _MAX_FILE_BYTES:
            raise DeliveryError(
                "delivery sidecar exceeds %d bytes; operator review required"
                % _MAX_FILE_BYTES)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DeliveryError(
                "delivery sidecar is not valid JSON; operator review "
                "required (refusing to reset silently)") from exc
        if (not isinstance(obj, dict)
                or obj.get("version") != _DELIVERY_VERSION
                or not isinstance(obj.get("queue"), list)
                or not isinstance(obj.get("receipts"), dict)
                or not isinstance(obj.get("intents"), dict)):
            raise DeliveryError(
                "delivery sidecar shape unrecognized; operator review "
                "required")
        return obj

    def _write(self, obj: dict[str, Any]) -> None:
        # Refuse, never silently truncate: a queue over the bound means the
        # bound was lowered under an existing file or the file was edited —
        # slicing here would silently drop receiptless publications.
        # (enqueue enforces the bound on the normal path.)
        if len(obj["queue"]) > self._max_queue:
            raise DeliveryError(
                "delivery queue exceeds %d entries; refusing to write "
                "(operator review required)" % self._max_queue)
        payload = json.dumps({
            "version": _DELIVERY_VERSION,
            "queue": obj["queue"],
            "receipts": obj["receipts"],
            "intents": obj["intents"],
        }, indent=1).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise DeliveryError(
                "delivery sidecar would exceed %d bytes; refusing to write"
                % _MAX_FILE_BYTES)
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DeliveryError(
                "delivery directory unavailable (%s)" %
                exc.__class__.__name__) from exc
        tmp = self._path.with_name(
            "%s.tmp-%d-%s" % (self._path.name, os.getpid(),
                              os.urandom(4).hex()))
        try:
            try:
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
            except OSError as exc:
                raise DeliveryError(
                    "delivery temp create failed (%s)" %
                    exc.__class__.__name__) from exc
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            except OSError as exc:
                raise DeliveryError(
                    "delivery sidecar write failed (%s)" %
                    exc.__class__.__name__) from exc
            finally:
                os.close(fd)
        except DeliveryError:
            # The owned temp (and ONLY the owned temp) is removed on any
            # failure; the sidecar path itself was never touched.
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise
        try:
            os.replace(str(tmp), str(self._path))
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(str(tmp))
            raise DeliveryError(
                "delivery replace failed (%s)" %
                exc.__class__.__name__) from exc

    # -- queue operations

    def enqueue(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Append one knowledge_link entry (metadata only).  Refuses at the
        queue bound — never silently dropped.  Idempotent per
        (namespace, target, key, locator) — the FULL identity: two
        namespaces publishing the same target/key/locator are two entries,
        never one.
        """
        required = ("namespace", "target", "key",
                    "version", "locator", "fullhash", "summary", "date")
        for field in required:
            if field not in entry:
                raise DeliveryError("queue entry missing %r" % field)
        stored = dict(entry)
        # A missing/empty op_id mints a fresh one here (one op id per
        # delivery attempt; retries of the same intent reuse the queued
        # entry's id through the idempotence check below).
        op_id = entry.get("op_id")
        stored["op_id"] = str(op_id) if op_id else new_op_id()
        stored["enqueued_at"] = _utc_now()
        stored.setdefault("attempts", 0)
        stored.setdefault("last_error", None)
        stored.setdefault("status", "queued")
        with self._locked():
            obj = self._read()
            for existing in obj["queue"]:
                if (existing.get("namespace") == stored.get("namespace")
                        and existing.get("target") == stored["target"]
                        and existing.get("key") == stored["key"]
                        and existing.get("locator") == stored["locator"]):
                    return existing
            if len(obj["queue"]) >= self._max_queue:
                raise DeliveryError(
                    "knowledge delivery queue is full (%d entries); flush "
                    "before enqueuing more (the refused capture stays "
                    "unpublished and can be re-captured once the queue "
                    "drains)" % self._max_queue)
            obj["queue"].append(stored)
            self._write(obj)
            return stored

    def pending(self) -> list[dict[str, Any]]:
        with self._locked():
            return list(self._read()["queue"])

    def remove(self, op_ids: list[str]) -> int:
        """Drop terminal entries from the queue (applied or rejected)."""
        if not op_ids:
            return 0
        wanted = set(op_ids)
        with self._locked():
            obj = self._read()
            before = len(obj["queue"])
            obj["queue"] = [e for e in obj["queue"]
                            if e.get("op_id") not in wanted]
            removed = before - len(obj["queue"])
            if removed:
                self._write(obj)
            return removed

    def note_attempt(self, op_ids: list[str], *, error: str | None) -> None:
        """Record an apply attempt (count + last content-free error) without
        removing the entries — the hub-unreachable/rollback pause path."""
        if not op_ids:
            return
        wanted = set(op_ids)
        with self._locked():
            obj = self._read()
            changed = False
            for entry in obj["queue"]:
                if entry.get("op_id") in wanted:
                    entry["attempts"] = int(entry.get("attempts", 0)) + 1
                    entry["last_error"] = error
                    changed = True
            if changed:
                self._write(obj)

    # -- receipts

    def record_receipt(self, receipt: Mapping[str, Any]) -> None:
        if not isinstance(receipt, dict):
            raise DeliveryError("receipt must be an object")
        # The op id is the correlation key (queue entry -> receipt) and is
        # validated FIRST, untransformed: every later refusal can name it.
        op_id = receipt.get("op_id")
        if (not isinstance(op_id, str) or not op_id
                or len(op_id) > _MAX_OP_ID_CHARS):
            raise DeliveryError(
                "receipt op_id must be a non-empty string of at most %d "
                "characters" % _MAX_OP_ID_CHARS)
        with self._locked():
            obj = self._read()
            stored = dict(receipt)
            stored["received_at"] = _utc_now()
            self._validate_receipt_fields(op_id, stored)
            obj["receipts"][op_id] = stored
            # Bound by insertion order (dicts preserve it): drop oldest.
            while len(obj["receipts"]) > self._max_receipts:
                obj["receipts"].pop(next(iter(obj["receipts"])))
            self._write(obj)

    @staticmethod
    def _validate_receipt_fields(op_id: str,
                                 stored: dict[str, Any]) -> None:
        """Bound receipt/op field sizes BEFORE the sidecar write.  The
        refusal is content-free: it names the op id, the field and the
        bound — never the offending value."""
        for key, value in stored.items():
            if (not isinstance(key, str)
                    or len(key) > _MAX_RECEIPT_KEY_CHARS):
                raise DeliveryError(
                    "receipt %r carries an invalid field name" % op_id)
            if value is None or isinstance(value, (bool, int, float)):
                continue
            if isinstance(value, str):
                if len(value) > _MAX_RECEIPT_FIELD_CHARS:
                    raise DeliveryError(
                        "receipt %r field %r exceeds %d characters" %
                        (op_id, key, _MAX_RECEIPT_FIELD_CHARS))
                continue
            raise DeliveryError(
                "receipt %r field %r is not a scalar (string, number or "
                "null)" % (op_id, key))

    def receipts(self) -> dict[str, dict[str, Any]]:
        with self._locked():
            return dict(self._read()["receipts"])

    # -- workflow intents (the fields the journal does not carry)

    @staticmethod
    def _intent_key(ref: str, namespace: str) -> str:
        """The FULL identity key: ``<namespace>|<ref>``.  The bare ref
        (knowledge:<target>:<key>) does not name a namespace, so keying by
        it alone let two namespaces' stakes overwrite each other."""
        return "%s|%s" % (namespace, ref)

    def _retained_intents(self, intents: dict[str, Any]) \
            -> dict[str, Any]:
        """State-aware retention for the bounded intent map (FB-014).

        At or under ``max_intents`` every intent is kept.  Over the bound,
        SETTLED intents (:data:`_INTENT_SETTLED_STATES`) are evicted first
        — oldest by dict insertion order — and an IN-FLIGHT intent (a live
        stake: "staked"/"pending"/"queued", or any unrecognized state,
        fail-closed) is NEVER evicted while a settled intent remains:
        evicting by raw insertion order dropped a fresh in-flight stake
        while a settled old intent survived (an update re-assigning an
        old key keeps its ORIGINAL insertion slot, so "oldest" is
        insertion order among settled keys only — an updated in-flight
        key is never a candidate while any settled key exists).  If the
        bound cannot be met without dropping an in-flight intent (the
        in-flight intents alone exceed it), the write is refused BEFORE
        any sidecar mutation — the caller's operation fails atomically
        and the file stays byte-identical."""
        if len(intents) <= self._max_intents:
            return intents
        settled = [key for key, record in intents.items()
                   if isinstance(record, dict)
                   and str(record.get("state") or "")
                   in _INTENT_SETTLED_STATES]
        in_flight = [key for key in intents if key not in set(settled)]
        if len(in_flight) > self._max_intents:
            raise DeliveryError(
                "intent bound %d would drop an IN-FLIGHT stake; refusing "
                "to write — drive intents to a settled state first"
                % self._max_intents)
        keep = self._max_intents - len(in_flight)
        # keep < len(settled) holds here (len(in_flight) <= max and the
        # total was over max); the NEWEST settled intents survive.
        drop = set(settled[:len(settled) - keep])
        return {key: record for key, record in intents.items()
                if key not in drop}

    def set_intent(self, *, ref: str, fields: Mapping[str, Any],
                   namespace: str = "") -> dict:
        _validate_ref(ref)
        _validate_intent_namespace(namespace)
        with self._locked():
            obj = self._read()
            record = dict(fields)
            record["ref"] = ref
            record["namespace"] = namespace
            record["updated_at"] = _utc_now()
            obj["intents"][self._intent_key(ref, namespace)] = record
            obj["intents"] = self._retained_intents(obj["intents"])
            self._write(obj)
            return record

    def get_intent(self, ref: str, namespace: str = "") \
            -> dict[str, Any] | None:
        _validate_ref(ref)
        _validate_intent_namespace(namespace)
        with self._locked():
            intents = self._read()["intents"]
            record = intents.get(self._intent_key(ref, namespace))
            if record is None:
                # Pre-namespace sidecar records were keyed by the bare ref
                # and carry no namespace field: keep honoring them so an
                # in-flight stake written before the fix survives.
                legacy = intents.get(ref)
                if isinstance(legacy, dict) and "namespace" not in legacy:
                    record = legacy
            return record

    def snapshot(self) -> dict[str, Any]:
        """A consistent copy of the whole sidecar (status rendering)."""
        with self._locked():
            obj = self._read()
            return {
                "queue": [dict(e) for e in obj["queue"]],
                "receipts": {k: dict(v) for k, v in
                             obj["receipts"].items()},
                "intents": {k: dict(v) for k, v in obj["intents"].items()},
            }


# ---------------------------------------------------------------------------
# hub capability + apply (the ONLY routes out of the queue)


def hub_capability_status(http: Any) -> tuple[str, str]:
    """Ask the hub whether it implements knowledge_link.

    Returns ``("supported", <zmem_version>)`` when ``hello`` advertises
    :data:`CAPABILITY`; ``("unsupported", detail)`` for a reachable hub
    without it (an old hub); ``("unreachable", reason)`` when hello failed.
    Never raises, never enqueues anything.
    """
    try:
        payload = http.hello()
    except Exception as exc:  # noqa: BLE001 - capability probe is fail-closed
        return "unreachable", exc.__class__.__name__
    if not isinstance(payload, dict) or payload.get("ok") is False:
        reason = (str(payload.get("reason"))
                  if isinstance(payload, dict) else "invalid-hello")
        return "unreachable", reason
    capabilities = payload.get("capabilities")
    version = str(payload.get("zmem_version") or "unknown")
    if not isinstance(capabilities, list) or CAPABILITY not in capabilities:
        return "unsupported", "hub %s does not advertise %s" % (
            version, CAPABILITY)
    return "supported", version


def flush_to_hub(delivery: KnowledgeDelivery, http: Any) -> dict[str, Any]:
    """Drain the knowledge queue to a CAPABLE hub; never any other route.

    Capability is re-checked before EVERY batch (rollback detection),
    including between batches of one flush: a hub that lost
    ``knowledge_link_v1`` pauses every entry pending — nothing is sent,
    nothing is dropped.  Receipts update the sidecar; committed/noop
    entries leave the queue, rejected entries leave with their receipt
    kept (the hub recorded the rejection against the op_id; a resend
    would replay it forever).  A whole-call DENIAL
    (:data:`NONRETRYABLE_DENIAL_REASONS` — scoped-token namespace /
    authorization) is terminal for the entries it covers; a mixed batch is
    split one op per call so only actually-denied entries go terminal.

    BOUNDED PER FLUSH: every queued op is handed to the hub AT MOST ONCE
    per flush invocation — a whole-call denial does not count (nothing was
    applied), but once a call may have applied an op it is never resent
    inside the same flush, split-denial path included.  A hub that answers
    without a receipt for an op therefore ENDS the flush with
    ``ok: false`` / ``reason: "no-receipt"`` and the entry stays queued
    for the NEXT flush — a receipt-omitting hub can never loop the caller.

    The caller gets an explicit summary: ``applied``/``rejected`` op ids
    and the ``pending`` count.
    """
    applied: list[str] = []
    rejected: list[dict[str, Any]] = []
    # Op ids already handed to a call that may have applied them THIS
    # flush: never resent within this invocation (bounded delivery).
    attempted: set[str] = set()

    def _pause(status: str, detail: str,
               remaining: list[dict[str, Any]]) -> dict[str, Any]:
        delivery.note_attempt([e["op_id"] for e in remaining],
                              error="hub-%s" % status)
        return {
            "ok": False,
            "reason": "hub-%s" % status,
            "detail": detail,
            "paused": True,
            "applied": applied,
            "rejected": rejected,
            "pending": len(delivery.pending()),
        }

    def _record_denial(entry: dict[str, Any], reason: str) -> None:
        """A whole-call authorization/namespace denial is NONRETRYABLE:
        the entry leaves the queue with a terminal rejected receipt (a
        resend would be denied forever)."""
        recorded = {
            "op_id": entry["op_id"],
            "target": entry["target"],
            "key": entry["key"],
            "status": "rejected",
            "link_status": None,
            "version": entry["version"],
            "row_id": None,
            "error": reason,
        }
        delivery.record_receipt(recorded)
        delivery.remove([entry["op_id"]])
        rejected.append(recorded)

    def _denial_reason(result: Any) -> str | None:
        reason = (result.get("reason")
                  if isinstance(result, dict) else None)
        return (reason if reason in NONRETRYABLE_DENIAL_REASONS
                else None)

    def _drain(batch: list[dict[str, Any]],
               result: dict[str, Any]) -> dict[str, Any] | None:
        """Process one apply answer's receipts; a failure dict (or None to
        continue draining)."""
        receipts = result.get("receipts")
        if not isinstance(receipts, list):
            delivery.note_attempt([e["op_id"] for e in batch],
                                  error="apply-failed")
            return {
                "ok": False,
                "reason": "apply-failed",
                "detail": "no receipts in hub answer",
                "applied": applied,
                "rejected": rejected,
                "pending": len(delivery.pending()),
            }
        by_op = {r.get("op_id"): r for r in receipts
                 if isinstance(r, dict)}
        missing: list[str] = []
        for entry in batch:
            receipt = by_op.get(entry["op_id"])
            if receipt is None:
                # The hub answered without this op: outcome unknown; the
                # entry stays queued for the NEXT flush — it was already
                # attempted THIS flush and is never resent here.
                delivery.note_attempt([entry["op_id"]],
                                      error="no-receipt")
                missing.append(entry["op_id"])
                continue
            status_value = str(receipt.get("status", ""))
            recorded = {
                "op_id": entry["op_id"],
                "target": entry["target"],
                "key": entry["key"],
                "status": status_value,
                "link_status": receipt.get("link_status"),
                "version": entry["version"],
                "row_id": receipt.get("canonical_id"),
                "error": receipt.get("error"),
            }
            delivery.record_receipt(recorded)
            if status_value in ("committed", "duplicate"):
                delivery.remove([entry["op_id"]])
                applied.append(entry["op_id"])
            else:
                # rejected/blocked: the hub recorded the verdict against
                # this op_id — keep the receipt, drop the entry.
                delivery.remove([entry["op_id"]])
                rejected.append(recorded)
        if missing:
            return {
                "ok": False,
                "reason": "no-receipt",
                "detail": "hub answer omitted receipt(s) for %d op(s): %s"
                          % (len(missing), ", ".join(missing)),
                "applied": applied,
                "rejected": rejected,
                "pending": len(delivery.pending()),
            }
        return None

    status, detail = hub_capability_status(http)
    while True:
        entries = [e for e in delivery.pending()
                   if e.get("op_id") not in attempted]
        if not entries:
            break
        if status != "supported":
            return _pause(status, detail, entries)
        batch = entries[:_FLUSH_BATCH]
        ops = [build_link_op(e) for e in batch]
        result = http.apply_knowledge_ops(ops)
        denial = _denial_reason(result)
        if denial is not None:
            # Whole-call denial (scoped token / authorization): nothing in
            # the batch was applied, so none of them count as attempted.
            # A MIXED batch is split one op per call so only the entries
            # actually denied go terminal — never indiscriminately; each
            # single call IS that op's one apply attempt this flush.
            if len(batch) == 1:
                _record_denial(batch[0], denial)
            else:
                for entry in batch:
                    attempted.add(entry["op_id"])
                    single = http.apply_knowledge_ops(
                        [build_link_op(entry)])
                    single_denial = _denial_reason(single)
                    if single_denial is not None:
                        _record_denial(entry, single_denial)
                        continue
                    if isinstance(single, dict) \
                            and single.get("ok") is True:
                        failure = _drain([entry], single)
                        if failure is not None:
                            return failure
                    else:
                        delivery.note_attempt([entry["op_id"]],
                                              error="apply-failed")
        elif not isinstance(result, dict) or result.get("ok") is not True:
            reason = ("hub-unreachable"
                      if isinstance(result, dict)
                      and result.get("reason") == "hub-unreachable"
                      else "apply-failed")
            attempted.update(e["op_id"] for e in batch)
            delivery.note_attempt([e["op_id"] for e in batch],
                                  error=reason)
            return {
                "ok": False,
                "reason": reason,
                "detail": (json.dumps(result)[:200]
                           if isinstance(result, dict) else
                           "non-object apply result"),
                "applied": applied,
                "rejected": rejected,
                "pending": len(delivery.pending()),
            }
        else:
            attempted.update(e["op_id"] for e in batch)
            failure = _drain(batch, result)
            if failure is not None:
                return failure
        # Rollback detection BETWEEN batches: a hub that lost the
        # capability mid-flush pauses the remaining (unattempted) entries
        # — the next batch is never sent to a hub that would reject it
        # into its applied_ops ledger.
        status, detail = hub_capability_status(http)
    remaining = delivery.pending()
    return {
        "ok": not remaining and not rejected,
        "reason": None if not remaining else "entries-pending",
        "applied": applied,
        "rejected": rejected,
        "pending": len(remaining),
    }
