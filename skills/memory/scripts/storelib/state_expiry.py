"""State-memory injection-expiry helpers (issue #173, schema v15).

A `state` memory declares HOW it expires (``expiry_class``) and, implicitly,
WHEN (``inject_until``, computed from the class TTL unless the caller supplies
an explicit timestamp). The class names one of ``STATE_EXPIRY_CLASSES``; its
default TTL in days and operator env override live in ``schema_meta``.

This module is pure: no store I/O, no ``now_iso()``. The caller owns the clock
and passes an already-normalized UTC ISO string to ``compute_inject_until`` so
a writer transaction stamps every row from one ``tx_now`` (the single-timestamp
invariant AC7 pins). Validation here is the ONLY place the exact contract error
strings are produced, so the direct writer, the CLI, and sync ingest all refuse
with identical text.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from schema_meta import STATE_EXPIRY_CLASSES, STATE_EXPIRY_ENV, STATE_EXPIRY_TTL_DAYS

# `YYYY-MM-DDTHH:MM:SS` with optional fractional seconds, then a UTC offset.
# Offset is captured separately so a non-UTC offset is rejected rather than
# silently coerced — a `+02:00` value is a bug in the caller, not a format to
# normalize.
_UTC_FRACTIONAL = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<off>Z|z|\+00:00)$"
)


def state_expiry_days(expiry_class: str) -> int:
    """TTL in days for a state expiry class, honoring its env override.

    The class is validated BEFORE the env read so an unknown class cannot probe
    an unrelated variable. A missing or empty override falls back to the class
    default; a present override must be a positive decimal integer.
    """
    if expiry_class not in STATE_EXPIRY_CLASSES:
        # Issue #173 Execution step 10 pins this direct-writer message exactly;
        # both writer paths (add's `_resolve_expiry` and update's
        # `compute_inject_until`) raise through here, so the pinned string
        # lives at the single choke point rather than being pre-checked twice.
        raise ValueError(
            "state expiry class must be one of "
            + ", ".join(STATE_EXPIRY_CLASSES)
        )
    env = STATE_EXPIRY_ENV[expiry_class]
    raw = os.environ.get(env, "")
    if raw is None or raw == "":
        return STATE_EXPIRY_TTL_DAYS[expiry_class]
    text = str(raw).strip()
    if not text.isdigit() or int(text) < 1:
        raise ValueError(
            f"invalid state expiry for class '{expiry_class}': {env} must be a "
            "positive decimal integer"
        )
    return int(text)


def validate_inject_until(value: str) -> str:
    """Normalize an explicit injection-expiry timestamp to canonical UTC Z form.

    Accepts ``YYYY-MM-DDTHH:MM:SSZ``, the same form with one or more fractional
    second digits, and either form with a ``+00:00`` offset. ``+00:00`` is
    rewritten to ``Z``; valid fractional seconds are preserved verbatim. A
    date-only, naive (no offset), malformed, or non-UTC value is refused.
    """
    if not isinstance(value, str):
        raise ValueError("inject_until must be UTC ISO-8601 with Z or +00:00 offset")
    match = _UTC_FRACTIONAL.match(value.strip())
    if match is None:
        raise ValueError("inject_until must be UTC ISO-8601 with Z or +00:00 offset")
    return f"{match.group('base')}{('.' + match.group('frac')) if match.group('frac') else ''}Z"


def compute_inject_until(expiry_class: str, *, now: str) -> str:
    """``now`` plus the class TTL, as a canonical UTC ``...Z`` string.

    ``now`` must already be normalized (``validate_inject_until``); the return
    is the same canonical form. ``fromisoformat`` (not ``strptime``, whose
    ``%Y-%m-%dT%H:%M:%SZ`` format string rejects the fractional seconds that
    ``validate_inject_until`` accepts) parses the base to an aware datetime, the
    TTL is added in UTC, and the result is re-serialized in canonical form.
    ``timespec="auto"`` keeps fractional digits when the base carried them and
    emits none when it did not, so a whole-second ``now`` (what ``now_iso()``
    produces, and what the writer's single-timestamp contract pins) round-trips
    byte-identically.
    """
    base = validate_inject_until(now)
    parsed = datetime.fromisoformat(base)
    return (
        parsed + timedelta(days=state_expiry_days(expiry_class))
    ).isoformat(timespec="auto").replace("+00:00", "Z")
