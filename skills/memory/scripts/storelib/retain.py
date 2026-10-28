"""Evidence-gated automatic retention (issue #177, Workstream K PR 6).

The SOLE automatic extractor.  It consumes normalized evidence rows and
writes REVIEW-QUEUE CANDIDATES only — never a memory row, never a store
write, never a ``valid_until`` mutation.  Everything durable stays behind
the review queue sidecar (``correction_queue``), exactly like the manual
capture path, and the whole surface is disabled unless the store-side
``auto_retain`` config gate or the ``ZMEM_AUTO_RETAIN`` override enables it.

Boundary rules pinned by tests/test_auto_retain.py::ProcessBoundaryTest:
this module is store-side; hooks and providers reach it only through the
``store.py retain`` / ``store.py retain-adapt`` subprocesses.
"""

from __future__ import annotations

import hashlib
import contextlib
import os
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

# corrections.py / correction_queue.py / capture_quality.py live in the
# PARENT directory of storelib/ (skills/memory/scripts).  Same bootstrap
# shape as storelib/mine.py.
try:
    from corrections import (
        MIN_SHORT_CORRECTION_LENGTH as _MIN_SHORT_CORRECTION_LENGTH,
        aggregate_errors as _aggregate_errors,
        classify_error_type as _classify_error_type,
        detect_patterns as _detect_patterns,
        is_harvest_paste as _is_harvest_paste,
        should_include_message as _should_include_message,
    )
    from correction_queue import append_queue as _append_queue
    from correction_queue import make_item as _make_item
    from capture_quality import strip_zmem_fence as _strip_zmem_fence
except ImportError:  # pragma: no cover - direct-module loads in tests
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from corrections import (  # type: ignore
        MIN_SHORT_CORRECTION_LENGTH as _MIN_SHORT_CORRECTION_LENGTH,
        aggregate_errors as _aggregate_errors,
        classify_error_type as _classify_error_type,
        detect_patterns as _detect_patterns,
        is_harvest_paste as _is_harvest_paste,
        should_include_message as _should_include_message,
    )
    from correction_queue import append_queue as _append_queue  # type: ignore
    from correction_queue import make_item as _make_item  # type: ignore
    from capture_quality import strip_zmem_fence as _strip_zmem_fence  # type: ignore

QueueItem = dict[str, Any]

# The closed host vocabulary (CLI --host choices and row lane stamps).
RETAIN_HOSTS = ("claude", "codex", "zcode", "deepseek",
                "hermes-provider", "hermes-compat")

# Canonical UTC timestamp shape every evidence row must carry.  Real host
# transcripts carry millisecond precision (Claude Code writes
# "2026-09-10T12:00:00.123Z"), so the fractional part is ACCEPTED on input and
# canonicalized by ``_normalize_ts`` before anything stores or sorts it.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_TS_CANON_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _normalize_ts(value: str) -> str | None:
    """Canonicalize a host timestamp to ``...THH:MM:SSZ`` or return None.

    Host transcripts disagree on sub-second precision, so the parser accepts a
    fractional part and truncates it.  The canonical form is what every
    downstream consumer (sorting, the queue ``_parse_iso_ts``, the stable-id
    hash) already assumes, so normalizing at the boundary keeps one shape in
    the store instead of two.
    """
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip()
    if _TS_CANON_RE.match(candidate):
        return candidate
    if not _TS_RE.match(candidate):
        return None
    return candidate[:19] + "Z"

# Decision rows only count as decisions when the normalized text carries an
# explicit rationale marker (issue #177 Design step 1).
_DECISION_MARKERS = ("because", "so that", "therefore", "rationale:", "reason:")

# Confidence for an aggregated runner-outcome row. Deliberately a fixed honest
# value: the occurrence count drives the REVIEW ORDER (review_priority), not
# how sure we are the classification is right. It sits under the 0.6 threshold
# that `clear_queue(drop_stale=True)` uses, so a low-signal outcome can age out
# instead of being immortalized by an ordering weight.
_OUTCOME_CONFIDENCE = 0.55


def _warn(message: str) -> None:
    """One sanitized stderr line, LF-only bytes on real pipes (with a text
    fallback for in-process redirected streams)."""
    line = "[zmem] retain: %s\n" % message
    buf = getattr(sys.stderr, "buffer", None)
    if buf is not None:
        buf.write(line.encode("utf-8"))
        buf.flush()
    else:
        sys.stderr.write(line)


def _stable_id(namespace: str, session: str, cls: str, text: str,
               evidence_ids: list[str]) -> str:
    payload = "\0".join(
        [namespace, session, cls, text, "\0".join(sorted(evidence_ids))]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _retain_hash(namespace: str, text: str, earliest_ts: str) -> str:
    payload = "\0".join([namespace, text, earliest_ts])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _correction_values(text: str) -> tuple[str, float, str, int] | None:
    """Classifier values for a correction row, pre-boost and FP-gated.

    The UNPADDED call is authoritative for the decision: its type (including
    the ``$``-anchored FALSE_POSITIVE_PATTERNS suppression) decides whether
    the row is a correction at all.  Only when it says "auto" are the values
    re-derived from a length-neutralized copy (padded with trailing spaces
    up to MIN_SHORT_CORRECTION_LENGTH when shorter): the capture path's
    short-message boost (+0.10) is a live-conversation heuristic that the
    frozen fixture contract deliberately does not apply to normalized
    evidence rows, and padding lands in the neutral band so no >150/>300
    reduction fires either.  patterns/sentiment are identical across both
    calls for every text the unpadded call accepts.
    """
    type_, patterns, confidence, sentiment, decay = _detect_patterns(text)
    if type_ != "auto":
        return None
    if len(text) < _MIN_SHORT_CORRECTION_LENGTH:
        padded = text + " " * (_MIN_SHORT_CORRECTION_LENGTH - len(text))
        _, p2, c2, s2, d2 = _detect_patterns(padded)
        return (p2 or patterns, c2, s2, d2)
    return (patterns, confidence, sentiment, decay)


def _normalize_text(text: str) -> str:
    return " ".join(str(text).split())


def _flatten_content(content: Any) -> Any:
    """Flatten a Claude transcript ``message.content`` to plain text.

    Real Claude Code user turns carry either a bare string or a list of
    content blocks; only the string form was accepted before, so every
    block-form turn was dropped.  Non-text blocks (tool_result, image) are
    skipped exactly as the live capture path's ``should_include_message``
    filters them downstream.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(part for part in parts if part)
    return content


def _looks_like_pasted_third_party(text: str) -> bool:
    """True when the text reads as quoted/pasted material, not a user assertion.

    ``_detect_patterns`` matches ``remember:`` UNANCHORED and the live capture
    path filters by message SHAPE only — never by provenance — so a pasted
    answer, a project instruction file, a CI log or a diff that merely
    CONTAINS the literal string was promoted to a ``class: explicit``
    candidate at confidence 0.90.

    The test is deliberately conservative, because the cost of a false
    negative here is silently losing the highest-confidence class the feature
    exists to catch. Only markers that are unambiguous evidence of a
    QUOTED BLOCK count — not ordinary words a user might type. ("commit
    message", "the following is" and "as stated in" all appear in perfectly
    legitimate preferences, so none of them are markers.) A length alone never
    rejects; a long turn that merely contains "remember:" is still the user
    stating a preference.

    This does not change the shared classifier, so the live capture path keeps
    its established behaviour — it only gates the automatic, whole-transcript
    extraction that this PR introduces.
    """
    return any(marker in text for marker in _PASTE_MARKERS)


# Unambiguous quoted-block evidence. Deliberately short: each entry can only
# appear when the message is quoting or transcribing foreign material.
# Fenced code is NOT a marker -- "Remember: always run ```pnpm test``` before
# pushing" is an ordinary developer preference, and dropping the highest
# confidence class silently is worse than keeping a false positive a reviewer
# can see and reject.
_PASTE_MARKERS = (
    "<<<zmem_untrusted_fence>>>", ">>>\n", "quoted from", "pasted from",
    "verbatim:", "[system]", "<system-reminder>", "-----BEGIN",
)


def _has_decision_marker(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _DECISION_MARKERS)


def extract_candidates(evidence_rows: Iterable[dict[str, Any]]) -> list[QueueItem]:
    """Classify normalized evidence rows into review-queue candidates.

    Invalid rows are skipped with one sanitized debug message each.  The
    returned list is sorted by ``(timestamp, id)`` and never contains a
    memory write, a store handle, or untrusted-fence content.
    """
    runners: list[dict[str, Any]] = []
    staged: list[QueueItem] = []

    for index, row in enumerate(evidence_rows or []):
        if not isinstance(row, dict):
            _warn("skipped invalid evidence row (index %d)" % index)
            continue
        evidence_id = row.get("evidence_id")
        text_raw = row.get("text")
        timestamp = row.get("timestamp")
        session = row.get("session")
        namespace = row.get("namespace")
        host = row.get("host")
        kind = row.get("kind")
        if not all(isinstance(v, str) and v for v in
                   (evidence_id, text_raw, timestamp, session, namespace, host)):
            _warn("skipped invalid evidence row (index %d)" % index)
            continue
        canonical_ts = _normalize_ts(timestamp)
        if canonical_ts is None:
            _warn("skipped invalid evidence row (index %d)" % index)
            continue
        timestamp = canonical_ts

        text = _normalize_text(_strip_zmem_fence(text_raw))
        if not text or not _should_include_message(text):
            continue
        # Issue #247 parity (PR #407 review PRR-003): the auto-retain lane
        # queues correction candidates exactly like the manual hook lane, so
        # a pasted harvest block must be gated here too — otherwise the same
        # multi-KB paste re-enters the queue through this lane.
        if _is_harvest_paste(text):
            continue

        cls: str | None = None
        legacy: tuple[str, str, float, str, int] | None = None
        evidence_ids = [evidence_id]

        detected_type, detected_patterns, detected_conf, detected_sent, detected_decay = (
            _detect_patterns(text)
        )

        if (isinstance(row.get("runner_error"), str) and row["runner_error"]
                and (kind == "runner" or kind is None or kind == "user"
                     or kind == "tool")):
            # Runner-signalled outcome: the non-empty runner_error IS the
            # signal.  Hand-fed CLI rows may carry kind="runner" (the
            # session-a fixture shape); every wired lane normalizes tool
            # rows with runner_error and NO kind field, so the branch keys
            # on the signal itself (final-critic R1).
            error_type, guideline = _classify_error_type(row["runner_error"])
            if error_type:
                runners.append({
                    "error_type": error_type,
                    "content": text,
                    "project_folder": session + ":" + namespace + ":" + host,
                    "suggested_guideline": guideline,
                    "session": session,
                    "namespace": namespace,
                    "host": host,
                    "evidence_id": evidence_id,
                    "timestamp": timestamp,
                })
            continue

        if kind == "explicit" or detected_type == "explicit":
            # Gate only the AUTOMATIC whole-transcript path: an unanchored
            # "remember:" inside pasted third-party material is not the user
            # asserting a standing preference.
            if _looks_like_pasted_third_party(text):
                cls = None
                legacy = None
            else:
                cls = "explicit"
                legacy = ("lesson", "remember:", 0.90, "correction", 120)
        elif kind == "decision" and _has_decision_marker(text):
            cls = "decision"
            legacy = ("decision", "decision", 0.70, "correction", 90)
        else:
            values = _correction_values(text)
            if values is not None:
                cls = "correction"
                legacy = ("constraint", values[0], values[1], values[2], values[3])

        if cls is None or legacy is None:
            continue

        staged.append(_build_item(
            cls=cls, legacy=legacy, text=text, evidence_ids=evidence_ids,
            timestamp=timestamp, session=session, namespace=namespace,
            host=host,
        ))

    for group in _aggregate_errors(runners, min_occurrences=2):
        error_type = group["error_type"]
        members = [r for r in runners if r["error_type"] == error_type
                   and r["project_folder"] == group["project_folder"]]
        earliest = min(m["timestamp"] for m in members)
        ids = sorted({m["evidence_id"] for m in members})
        message = str(group.get("suggested_guideline") or error_type)
        item = _build_item(
            cls="outcome",
            # `review_priority` is an ORDERING weight (corrections.py names it
            # that way precisely so the two are never confused, and the
            # closeout rubric says it "must not become the row's confidence").
            # It is carried as its own provenance field; `confidence` gets a
            # fixed honest value instead of a number that grows with the
            # occurrence count and silently exempts the row from the
            # `confidence < 0.6` stale prune.
            legacy=("lesson", error_type, _OUTCOME_CONFIDENCE,
                    "correction", 90),
            text=message, evidence_ids=ids, timestamp=earliest,
            session=members[0]["session"], namespace=members[0]["namespace"],
            host=members[0]["host"],
        )
        item["review_priority"] = float(group["review_priority"])
        staged.append(item)

    staged.sort(key=lambda item: (item["timestamp"], item["id"]))
    return staged


def _build_item(*, cls: str, legacy: tuple[str, str, float, str, int],
                text: str, evidence_ids: list[str], timestamp: str,
                session: str, namespace: str, host: str) -> QueueItem:
    type_, patterns, confidence, sentiment, decay_days = legacy
    item = _make_item(
        message=text,
        type_=type_,
        patterns=patterns,
        confidence=confidence,
        sentiment=sentiment,
        decay_days=decay_days,
        session=session,
        namespace=namespace,
        host=host,
        capture_mode="auto",
    )
    # Digest the text that was actually PERSISTED.  make_item(capture_mode=
    # "auto") redacts `message` when it finds a secret-shaped string, so
    # hashing the pre-redaction text stored a verifiable commitment to the
    # secret right beside the redaction that was meant to remove it.
    stored = item.get("message")
    stored_text = stored if isinstance(stored, str) else text
    # Replace the random uuid with the stable key and make_item's wall-clock
    # timestamp with the earliest normalized evidence timestamp, then add the
    # retention provenance fields (issue #177 Design step 2).
    item["id"] = _stable_id(namespace, session, cls, stored_text, evidence_ids)
    item["timestamp"] = timestamp
    item["evidence_timestamp"] = timestamp
    item["class"] = cls
    item["evidence_ids"] = list(evidence_ids)
    item["signal"] = "none"
    item["retain_hash"] = _retain_hash(namespace, stored_text, timestamp)
    return item


def _retention_enabled(*, environ: Mapping[str, str] | None = None,
                       config: Mapping[str, Any] | None = None) -> bool:
    """The single store-side retention gate.

    ``ZMEM_AUTO_RETAIN`` semantics (issue #177 Design step 3): unset (or
    empty) uses the config value; exact ``0`` forces off; exact ``1`` forces
    on; any other non-empty value raises ValueError (the CLI maps that to
    the invalid-override output).
    """
    if config is None:
        from storelib.config import load_config

        config = load_config()
    env = os.environ if environ is None else environ
    raw = env.get("ZMEM_AUTO_RETAIN")
    if raw is None or raw == "":
        return _config_gate_value(config)
    if raw == "0":
        return False
    if raw == "1":
        return True
    raise ValueError("invalid ZMEM_AUTO_RETAIN value")


def _config_gate_value(config: Mapping[str, Any]) -> bool:
    """Read ``auto_retain`` from config.json as a STRICT boolean.

    The gate decides whether untrusted conversation text is persisted, so it
    must fail closed on anything that is not an explicit boolean.  Bare
    ``bool()`` meant a config intended to DISABLE retention — the natural
    JSON-ification ``{"auto_retain": "false"}`` or ``"0"`` — silently ENABLED
    it, while the env override three lines above rejects the same values.
    """
    if "auto_retain" not in config:
        return False
    value = config["auto_retain"]
    if isinstance(value, bool):
        return value
    _warn("ignoring non-boolean auto_retain in config.json (retention stays off)")
    return False


def normalize_host_payload(payload: Iterable[dict[str, Any]], *, host: str,
                           session: str, namespace: str) -> list[dict[str, Any]]:
    """Normalize raw Claude/Codex/ZCode payload records into evidence rows.

    Each adapter maps its id field to ``evidence_id``, its content field to
    ``text``, and stamps the CLI provenance triple.  Records with missing
    required fields are skipped; a record carrying a conflicting provenance
    triple is skipped (defensive — every emitted row equals the CLI values).
    """
    rows: list[dict[str, Any]] = []
    if host in ("hermes-provider", "hermes-compat"):
        # The Hermes message schema (issue #177 Design step 4): the payload
        # is the message iterable; the shared Hermes normalizer owns the
        # role filter and id/timestamp mapping for both Hermes lanes.
        return normalize_hermes_messages(
            payload, session=session, namespace=namespace, host=host)
    for rec in payload or []:
        if not isinstance(rec, dict):
            continue
        if host == "deepseek":
            # dsh session record (issue #375): flat {"seq","type","data"};
            # user/message with a user-kind source -> user text,
            # tool/result with an error -> failure row.
            data = rec.get("data") if isinstance(rec.get("data"), dict) \
                else {}
            if rec.get("type") == "user/message":
                src = data.get("source") if isinstance(
                    data.get("source"), dict) else {}
                if src.get("kind") != "user":
                    continue
                evidence_id, timestamp, text = (
                    str(rec.get("seq")), "",
                    _flatten_content(data.get("message", {})))
                ok = bool(text) and len(text) >= 5
            elif rec.get("type") == "tool/result" and isinstance(
                    data.get("error"), dict):
                evidence_id, timestamp, text = (
                    "err-%s" % rec.get("seq"), "",
                    str(data["error"].get("message") or ""))
                ok = bool(text)
            else:
                continue
            if not ok:
                continue
            rows.append({
                "uuid": evidence_id, "timestamp": timestamp,
                "role": "user", "content": text,
            })
            continue
        if host == "claude":
            # Match the shape this repo's own proven Claude transcript reader
            # accepts (corrections.extract_user_messages): `isMeta` may be
            # ABSENT (it is absent in real transcripts) and `message.content`
            # may be a list of {"type":"text","text":...} blocks, not only a
            # bare string.  Requiring the literal `isMeta is False` plus a
            # string content matched only this PR's own synthetic fixture and
            # produced zero rows against a real transcript.
            content = rec.get("message", {}).get("content") if isinstance(
                rec.get("message"), dict) else None
            evidence_id, timestamp, text = (
                rec.get("uuid"), rec.get("timestamp"), _flatten_content(content),
            )
            ok = (
                rec.get("type") == "user"
                and not rec.get("isMeta", False)
                and isinstance(evidence_id, str) and evidence_id
                and isinstance(timestamp, str) and timestamp
                and isinstance(text, str) and text.strip()
            )
        elif host == "codex":
            evidence_id, timestamp, text = (
                rec.get("id"), rec.get("timestamp"), rec.get("content"),
            )
            ok = (
                rec.get("role") == "user"
                and isinstance(evidence_id, str) and evidence_id
                and isinstance(timestamp, str) and timestamp
                and isinstance(text, str) and text
            )
        elif host == "zcode":
            evidence_id, timestamp, text = (
                rec.get("event_id"), rec.get("ts"), rec.get("text"),
            )
            ok = (
                rec.get("role") == "user"
                and isinstance(evidence_id, str) and evidence_id
                and isinstance(timestamp, str) and timestamp
                and isinstance(text, str) and text
            )
        else:
            raise ValueError("unsupported host: %s" % host)
        if not ok:
            _warn("skipped payload record (missing required fields)")
            continue
        canonical_ts = _normalize_ts(timestamp)
        if canonical_ts is None:
            _warn("skipped payload record (invalid timestamp)")
            continue
        timestamp = canonical_ts
        if any(rec.get(key) is not None and rec.get(key) != expected
               for key, expected in (("session", session),
                                     ("namespace", namespace),
                                     ("host", host))):
            _warn("skipped payload record (provenance mismatch)")
            continue
        rows.append({
            "evidence_id": evidence_id,
            "text": text,
            "timestamp": timestamp,
            "session": session,
            "namespace": namespace,
            "host": host,
        })
    return rows


def normalize_hermes_messages(messages: Iterable[dict[str, Any]], *,
                              session: str, namespace: str,
                              host: str) -> list[dict[str, Any]]:
    """Normalize Hermes provider/compat messages into evidence rows.

    Schema: ``{id, timestamp, role in user|assistant|tool, content,
    isMeta: false[, runner_error on tool]}``.  Only an EXPLICIT ``isMeta:
    true`` discards a message — the field is simply absent from the real
    ``conversation_history`` entries the compat host sends, and requiring it
    to be present and boolean matched only this PR's own fixture.  ``user``
    and ``tool``-with-``runner_error`` are kept; ``assistant`` is discarded.
    """
    rows: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("isMeta", False) is True:
            continue
        role = msg.get("role")
        content = msg.get("content")
        evidence_id = msg.get("id")
        timestamp = msg.get("timestamp")
        if (role not in ("user", "assistant", "tool")
                or not isinstance(content, str)
                or not isinstance(evidence_id, str) or not evidence_id
                or not isinstance(timestamp, str)):
            _warn("skipped hermes message (missing field)")
            continue
        canonical_ts = _normalize_ts(timestamp)
        if canonical_ts is None:
            _warn("skipped hermes message (invalid timestamp)")
            continue
        timestamp = canonical_ts
        row: dict[str, Any] = {
            "evidence_id": evidence_id,
            "text": content,
            "timestamp": timestamp,
            "session": session,
            "namespace": namespace,
            "host": host,
        }
        if role == "assistant":
            continue
        if role == "tool":
            runner_error = msg.get("runner_error")
            if not isinstance(runner_error, str) or not runner_error:
                continue
            row["runner_error"] = runner_error
        rows.append(row)
    return rows


def write_candidates(candidates: list[QueueItem], *, namespace: str) -> tuple[int, int]:
    """Append NEW candidates to the review queue, once.

    Retention is the first caller of ``append_queue`` that re-runs on an
    unbounded cadence: every Stop, SessionEnd and compress re-reads the whole
    transcript and re-derives the same candidates.  ``append_queue`` is a bare
    load-append-write with no key scan, so without a guard here the queue grew
    by one full copy of the session per hook fire and the 100-item cap evicted
    real candidates.  ``_stable_id`` is deterministic for the same evidence, so
    skipping already-present ids makes the whole operation idempotent.

    The batch runs under the store's per-namespace lock (the same primitive
    ``append_queue``'s own docstring tells future callers to use) because Stop,
    SessionEnd and the Hermes background worker are concurrent writers of the
    same file.

    Returns ``(appended, landed, deferred)``:

    * ``appended`` — how many rows this call actually wrote.
    * ``landed``   — how many of ``candidates`` are in the queue afterwards,
      counting ones an earlier run already stored.  ``landed`` is what the
      caller reports, so a re-run is a successful no-op rather than a
      "queue write failed", and a candidate the 100-item cap evicted is never
      reported as written.
    * ``deferred`` — True when a live lock holder owned the namespace and
      nothing was attempted.  That is ordinary contention, not a failure: the
      next Stop re-derives the same candidates and the id guard makes that
      safe.  Reporting it as a write failure would escalate benign contention
      into a checkpoint-v2 host abort.
    """
    from correction_queue import load_queue  # type: ignore

    with _queue_write_lock(namespace) as acquired:
        if not acquired:
            return 0, 0, True
        present = {
            row.get("id")
            for row in load_queue(namespace)
            if isinstance(row, dict)
        }
        appended = 0
        for item in candidates:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in present:
                continue
            if not _append_queue(namespace, item):
                break
            if isinstance(item_id, str):
                present.add(item_id)
            appended += 1
        # Re-read so a candidate the cap evicted is reported as NOT landed.
        landed_ids = {
            row.get("id")
            for row in load_queue(namespace)
            if isinstance(row, dict)
        }
        landed = sum(
            1 for item in candidates
            if isinstance(item.get("id"), str) and item["id"] in landed_ids
        )
        return appended, landed, False


@contextlib.contextmanager
def _queue_write_lock(namespace: str):
    """Per-namespace exclusive lock around a batch queue write.

    ``append_queue`` is a bare load-append-write and its own docstring tells
    future callers to add a per-namespace O_EXCL lock; retention is that
    caller.  Yields False when a live holder owns the lock so the caller can
    fail open instead of writing concurrently.

    The namespace MUST be encoded before it becomes a filename: every real
    namespace contains ``:`` (``project:*`` / ``user:global``) and ``:`` is
    not a legal Windows path character, so an unencoded lock name could not be
    created or released and every write after the first was refused.
    ``encode_namespace`` is the same encoder the queue filename itself uses,
    so the two cannot drift.

    The wait is deliberately short: this runs inside a hook stage bounded to
    3s, and a long lock wait silently eats the whole budget.
    """
    token = None
    lock_name = None
    acquired = True
    try:
        from correction_queue import encode_namespace  # type: ignore
        from storelib.schema import _strict_acquire_lock  # type: ignore

        lock_name = "retain-queue-%s" % encode_namespace(namespace)
        token = _strict_acquire_lock(lock_name, 30.0, wait_seconds=0.5)
        acquired = token is not None
    except Exception:
        # Lock support unavailable: degrade to the unlocked single-process
        # path rather than failing the whole retention stage. This handler
        # deliberately covers ONLY the acquisition — wrapping the yield would
        # swallow an exception from the caller's body and re-yield, which
        # turns a real error into "generator didn't stop".
        acquired = True
    try:
        yield acquired
    finally:
        if token is not None and lock_name is not None:
            try:
                from storelib.schema import _release_named_lock  # type: ignore

                _release_named_lock(lock_name, token)
            except Exception:
                pass
