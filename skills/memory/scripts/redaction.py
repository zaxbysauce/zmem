"""Dependency-free secret-like text redaction shared by capture surfaces.

Single pattern source (issue #180): every capture-policy surface — the write
path, the evidence writer, hook filtering, the read-time credential classifier,
and the correction queue — imports its patterns from HERE and nowhere else.

Span semantics (issue #180): patterns in ``_VALUE_SPAN_PATTERNS`` carry capture
group 1 around the SECRET VALUE only, so redaction preserves the surrounding
command syntax (``sshpass -p [REDACTED_SECRET] ssh host``). Every other pattern
keeps the historical whole-match replacement. A value that is already the
redaction marker is detected but left byte-identical, which makes redaction
idempotent on its own output. Counts are DETECTION counts, including
already-placeholder hits, never byte-change counts.
"""

from __future__ import annotations

import re


REDACTED_SECRET = "[REDACTED_SECRET]"

# Key=value credential shapes. The keyword prefix is deliberately UNANCHORED
# (no ``\b``): compound key names such as ``DB_PASSWORD=…`` / ``my_api_key=…``
# matched the pre-#180 pattern and must keep matching (plan-review round 2
# rejected a ``\b``-anchored rewrite for silently narrowing this detector).
# Group 1 captures the VALUE span for value-only replacement.
SECRET_CREDENTIAL_PATTERNS = [
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)\s*[:=]\s*(\S{8,})"),
    # Authorization headers occur in prompts, rendered fences, and operation
    # transcripts.  Keep this ahead of the generic token patterns so the whole
    # credential (rather than an arbitrary suffix) is replaced consistently.
    # The token alphabet contains several non-word characters.  A trailing
    # ``\b`` therefore backtracks before a terminal ``.``, ``_``, ``+``, ``/``,
    # ``~`` or ``-`` and leaks that character.  Require the next character to
    # be outside the credential alphabet so the greedy match consumes the
    # complete token while still respecting ordinary delimiters.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._+/~-]{16,}(?![A-Za-z0-9._+/~-])"),
    # PEM: prefer the FULL block (BEGIN..END, whole-match) so the body dies
    # with the header; the header-only shape remains as fallback for a
    # truncated paste. Both are non-capturing so no group logic can misfire.
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}\b"),
]
SECRET_GENERIC_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]

# Issue #180 command and URL credential shapes. Case-insensitivity is scoped
# with inline groups to the COMMAND NAME / URL SYNTAX only — option letters
# stay case-sensitive so ``sudo -s`` (shell) never matches ``sudo -S``.
# NOTE (cubic round, stated honestly): for ssh and psql the lowercase ``-p``
# IS the port flag, so ``ssh -p2222`` IS over-redacted here — that is the
# issue #180 matrix's explicit contract (positive p06 ``ssh -ppw180F``),
# accepted as fail-safe over-redaction and disclosed in SKILL.md; the case
# scoping only guarantees ``-P``-shaped tokens never match. Group 1 captures
# the value span in every entry.
SECRET_COMMAND_PATTERNS = [
    # sshpass -p <value> (standalone -p; any whitespace, incl. tabs)
    re.compile(r"(?i:\bsshpass\b)\s+-p\s+(\S+)"),
    # --password=<value> (attached form only; spaced/dangling are not matches)
    re.compile(r"(?i:--password=)(\S+)"),
    # ssh/mysql/psql -p<value> (attached form only, exactly these commands;
    # over-redacts ports for ssh/psql per the issue matrix)
    re.compile(r"(?i:\b(?:ssh|mysql|psql)\b)[ \t]+-p(\S+)"),
    # scheme://user:password@host — replace only the password
    re.compile(r"(?i:\b[a-z][a-z0-9+.-]*://)[^\s:/@]+:([^\s@]+)@"),
]

# Refusal-only detector (issue #180): no safe value span exists — the password
# arrives on stdin, not in the text — so the capture policy refuses the whole
# row (reason ``unredactable_secret``). The pure-redaction path still
# whole-match-replaces it so evidence/hook consumers count and mask the shape.
SECRET_REFUSAL_PATTERNS = [
    re.compile(r"(?i:\bsudo\b)\s+-S\b"),
]

SECRET_PATTERNS = (
    SECRET_CREDENTIAL_PATTERNS
    + SECRET_GENERIC_PATTERNS
    + SECRET_COMMAND_PATTERNS
    + SECRET_REFUSAL_PATTERNS
)

# The stable labels for the #180 shapes are the capture policy's refusal
# reasons (write.py: source_ref_secret_like / unredactable_secret /
# source_ref_unsafe_path) plus the shape names documented in SKILL.md's
# capture-policy table; no parallel label mapping is kept here.

# The key=value pattern is the ONE credential pattern with a captured VALUE
# span (group 1). Reference it by this named constant, never by list position:
# a positional slice like ``SECRET_CREDENTIAL_PATTERNS[:1]`` would silently
# change span semantics if the list were reordered (cubic review round).
KEYVALUE_VALUE_SPAN_PATTERN = SECRET_CREDENTIAL_PATTERNS[0]

# Identity membership: exactly the value-capturing patterns. A frozenset of
# pattern objects (re.Pattern hashes by identity) — never a "has group 1"
# heuristic, which would misfire on unrelated grouped patterns, and never a
# positional slice, which would silently flip span semantics when the
# credential list is reordered.
_VALUE_SPAN_PATTERNS = frozenset(
    (KEYVALUE_VALUE_SPAN_PATTERN,) + tuple(SECRET_COMMAND_PATTERNS))


def _value_span_replacement(match: "re.Match[str]") -> str:
    value = match.group(1)
    if value == REDACTED_SECRET:
        return match.group(0)
    start = match.start(1) - match.start(0)
    end = match.end(1) - match.start(0)
    return match.group(0)[:start] + REDACTED_SECRET + match.group(0)[end:]


def redact_secret_like_text(text: str) -> tuple[str, int]:
    """Replace secret-like VALUES with ``[REDACTED_SECRET]``.

    Value-span patterns rewrite only the captured value; everything else
    keeps the historical whole-match replacement. Returns
    ``(redacted_text, detection_count)``; the count includes matches whose
    value was already the marker (idempotent no-ops).
    """
    redacted = text or ""
    count = 0
    for pattern in SECRET_PATTERNS:
        if pattern in _VALUE_SPAN_PATTERNS:
            redacted, changed = pattern.subn(_value_span_replacement, redacted)
        else:
            redacted, changed = pattern.subn(REDACTED_SECRET, redacted)
        count += changed
    return redacted, count


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(
    r'''(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s"'<>]+|\\\\[^\s\\"'<>]+\\[^\s\\"'<>]+(?:\\[^\s"'<>]+)*)'''
)
_POSIX_ROOTS = r"Users|home|private|tmp|var|workspaces|workspace|mnt|opt|root"
_POSIX_PATH_RE = re.compile(
    rf'''(?<![A-Za-z0-9/:])/(?:{_POSIX_ROOTS})(?=/|\s|"|'|<|>|$)(?:/[^\s"'<>]+)*'''
)
_QUOTED_PATH_RE = re.compile(
    rf'''(?P<quote>["'])(?P<path>(?:[A-Za-z]:[\\/][^\r\n"']+|\\\\[^\s\\"']+\\[^\\\r\n"']+(?:\\[^\r\n"']+)*|/(?:{_POSIX_ROOTS})(?=/|\s|"|'|<|>|$)[^\r\n"']*))(?P=quote)'''
)


def redact_training_text(value: str) -> tuple[str, int]:
    """Apply secret, email, and filesystem path redaction for training data."""
    redacted, count = redact_secret_like_text(value)
    def replace_quoted(match: re.Match[str]) -> str:
        return f"{match.group('quote')}[REDACTED_PATH]{match.group('quote')}"

    redacted, changed = _QUOTED_PATH_RE.subn(replace_quoted, redacted)
    count += changed
    for pattern, replacement in (
        (_EMAIL_RE, "[REDACTED_EMAIL]"),
        (_WINDOWS_PATH_RE, "[REDACTED_PATH]"),
        (_POSIX_PATH_RE, "[REDACTED_PATH]"),
    ):
        redacted, changed = pattern.subn(replacement, redacted)
        count += changed
    return redacted, count
