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
from datetime import datetime, timedelta

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
    # ASCII-decimal only: `str.isdigit()` admits non-ASCII decimal digits
    # (silently coerced by int()) and superscripts (which pass isdigit and
    # then escape to int()'s raw message, echoing the operator's env value).
    if not (text.isascii() and text.isdecimal()) or int(text) < 1:
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
    date-only, naive (no offset), malformed, or non-UTC value is refused —
    "malformed" includes calendar-impossible components: the matched base is
    parsed with ``datetime.fromisoformat`` so ``2026-02-30``/``2026-13-01``/
    ``25:61:61`` are refused with the same pinned message rather than stored
    verbatim (the shape-only regex alone accepted them; issue #173 Design
    step 3 requires malformed values to raise).
    """
    if not isinstance(value, str):
        raise ValueError("inject_until must be UTC ISO-8601 with Z or +00:00 offset")
    match = _UTC_FRACTIONAL.match(value.strip())
    if match is None:
        raise ValueError("inject_until must be UTC ISO-8601 with Z or +00:00 offset")
    try:
        datetime.fromisoformat(match.group("base"))
    except ValueError:
        # Same pinned message as the shape refusals: the contract's error text
        # does not distinguish malformed sub-shapes.
        raise ValueError(
            "inject_until must be UTC ISO-8601 with Z or +00:00 offset"
        ) from None
    return f"{match.group('base')}{('.' + match.group('frac')) if match.group('frac') else ''}Z"


def compute_inject_until(expiry_class: str, *, now: str) -> str:
    """``now`` plus the class TTL, as a canonical UTC ``...Z`` string.

    ``now`` must already be normalized (``validate_inject_until``); the return
    is the same canonical form. The TTL is added to the WHOLE-SECOND part and
    the base's fractional digits (if any) are re-attached verbatim, so a
    caller-supplied fraction survives byte-identically instead of being
    truncated to datetime's microsecond precision (a whole-day addition never
    interacts with sub-second digits). A TTL/``now`` combination that
    overflows datetime range raises ``ValueError`` (never ``OverflowError``)
    so the CLI's ``except ValueError`` handlers print ``[zmem] <message>`` and
    exit 2 without a traceback (issue #173 Execution step 10).
    """
    base = validate_inject_until(now)
    if "." in base:
        whole, frac = base[:-1].split(".", 1)
        suffix = f".{frac}Z"
    else:
        whole, suffix = base[:-1], "Z"
    parsed = datetime.fromisoformat(whole)
    try:
        bumped = parsed + timedelta(days=state_expiry_days(expiry_class))
    except OverflowError as exc:
        raise ValueError(
            f"computed inject_until for class '{expiry_class}' is out of range"
        ) from exc
    return bumped.strftime("%Y-%m-%dT%H:%M:%SZ")[:-1] + suffix
