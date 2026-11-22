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
#
# Boundary convention (issue #523): Python's ``\b`` is Unicode-aware, so a
# non-ASCII letter (é, ö, …) glued to a token end removes the boundary and the
# secret survives redaction on decoded-Unicode text. The distinctive token
# shapes below use explicit ASCII word-char lookarounds
# (``(?<![A-Za-z0-9_])`` / ``(?![A-Za-z0-9_])``) instead: identical to ``\b``
# for every ASCII neighbour (verified over 232,974 ASCII contexts), correct
# under non-ASCII adjacency, and parity with ECMAScript's ASCII-only ``\b``
# (opencode-plugin/index.mjs). Known gap, pinned by tests: under IGNORECASE
# the ASCII classes case-fold to also match U+0130/U+0131/U+017F/U+212A-class
# codepoints, so e.g. a U+212A-prefixed Bearer token still slips. The generic
# base64/hex patterns below deliberately KEEP ``\b`` — widening their
# boundaries would redact benign SHAs adjacent to non-ASCII text (issue #502
# over-redaction direction).
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
    re.compile(r"(?i)(?<![A-Za-z0-9_])Bearer\s+[A-Za-z0-9._+/~-]{16,}(?![A-Za-z0-9._+/~-])"),
    # PEM: prefer the FULL block (BEGIN..END, whole-match) so the body dies
    # with the header; the header-only shape remains as fallback for a
    # truncated paste. Both are non-capturing so no group logic can misfire.
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])xox[abprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])"),
    # These token alphabets include ``-``.  A trailing word boundary therefore
    # cannot consume a terminal hyphen (and the generic form can fail to match
    # altogether); use the same alphabet-boundary convention as Bearer above.
    re.compile(r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])sk-proj-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_])AKIA[0-9A-Z]{16}(?![A-Za-z0-9_])"),
    re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),
    re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"),
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
    re.compile(r"(?i:(?<![A-Za-z0-9_])sshpass(?![A-Za-z0-9_]))\s+-p\s+(\S+)"),
    # --password=<value> (attached form only; spaced/dangling are not matches)
    re.compile(r"(?i:--password=)(\S+)"),
    # ssh/mysql/psql -p<value> (attached form only, exactly these commands;
    # over-redacts ports for ssh/psql per the issue matrix)
    re.compile(r"(?i:(?<![A-Za-z0-9_])(?:ssh|mysql|psql)(?![A-Za-z0-9_]))[ \t]+-p(\S+)"),
    # scheme://user:password@host — replace only the password
    re.compile(r"(?i:(?<![A-Za-z0-9_])[a-z][a-z0-9+.-]*://)[^\s:/@]+:([^\s@]+)@"),
]

# Refusal-only detector (issue #180): no safe value span exists — the password
# arrives on stdin, not in the text — so the capture policy refuses the whole
# row (reason ``unredactable_secret``). The pure-redaction path still
# whole-match-replaces it so evidence/hook consumers count and mask the shape.
SECRET_REFUSAL_PATTERNS = [
    re.compile(r"(?i:(?<![A-Za-z0-9_])sudo(?![A-Za-z0-9_]))\s+-S\b"),
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


def _redact_secret_like_text_with_positions(
    text: str,
    positions: list[int],
) -> tuple[str, int, list[int]]:
    """Redact with the canonical pattern sequence and map selected positions.

    ``positions`` is intentionally caller-bounded.  Scan needs at most its
    retained match positions, so it avoids allocating provenance for every
    character of an otherwise valid 8 MiB record.  A position inside a
    redacted value maps to the replacement marker's start; callers therefore
    never search a marker that might itself contain their literal.
    """
    redacted = text or ""
    mapped = list(positions)
    count = 0
    for pattern in SECRET_PATTERNS:
        chunks: list[str] = []
        ordered = sorted(enumerate(mapped), key=lambda item: item[1])
        next_mapped = [0] * len(mapped)
        target_index = 0
        cursor = 0
        delta = 0
        for match in pattern.finditer(redacted):
            replacement = (_value_span_replacement(match)
                           if pattern in _VALUE_SPAN_PATTERNS
                           else REDACTED_SECRET)
            while (target_index < len(ordered)
                   and ordered[target_index][1] < match.start()):
                index, position = ordered[target_index]
                next_mapped[index] = position + delta
                target_index += 1
            while (target_index < len(ordered)
                   and ordered[target_index][1] < match.end()):
                index, position = ordered[target_index]
                if replacement == match.group(0):
                    next_mapped[index] = position + delta
                elif pattern in _VALUE_SPAN_PATTERNS:
                    value_start, value_end = match.span(1)
                    replacement_start = match.start() + delta
                    if position < value_start:
                        next_mapped[index] = position + delta
                    elif position < value_end:
                        next_mapped[index] = replacement_start + (value_start - match.start())
                    else:
                        next_mapped[index] = (replacement_start + (value_start - match.start())
                                              + len(REDACTED_SECRET) + (position - value_end))
                else:
                    next_mapped[index] = match.start() + delta
                target_index += 1
            chunks.append(redacted[cursor:match.start()])
            chunks.append(replacement)
            cursor = match.end()
            delta += len(replacement) - (match.end() - match.start())
            count += 1
        while target_index < len(ordered):
            index, position = ordered[target_index]
            next_mapped[index] = position + delta
            target_index += 1
        if cursor:
            chunks.append(redacted[cursor:])
            redacted = "".join(chunks)
        mapped = next_mapped
    return redacted, count, mapped


def redact_secret_like_text(text: str) -> tuple[str, int]:
    """Replace secret-like VALUES with ``[REDACTED_SECRET]``.

    Value-span patterns rewrite only the captured value; everything else
    keeps the historical whole-match replacement. Returns
    ``(redacted_text, detection_count)``; the count includes matches whose
    value was already the marker (idempotent no-ops).
    """
    redacted, count, _positions = _redact_secret_like_text_with_positions(text, [])
    return redacted, count


# Email (issue #523): the pre-#523 pattern was ASCII-only in its local-part
# class AND used Unicode-aware ``\b`` at both ends, so a non-ASCII letter
# anywhere in or adjacent to the address defeated the match. The local part is
# tolerant of ASCII word chars, U+0300-U+036F combining marks (composed AND
# decomposed forms), and Latin Extended letters (U+00C0-U+024F — real
# international addresses like ``jörg@example.com``); the domain/TLD stay
# ASCII-literal (IDN domains remain a documented gap, mirroring the #502
# over-redaction caution). NON-Latin scripts are deliberately EXCLUDED from
# the local part: a CJK/Thai/ideographic run glued before an address is
# surrounding user text and must survive, not be consumed as the local part
# (review finding M-1: the earlier ``\w``-based class over-redacted
# ``連絡先はuser@example.com`` → ``[REDACTED_EMAIL]``). The leading boundary
# REQUIRES a local-part char first: this keeps non-matching non-ASCII text
# LINEAR (a punctuation-allowed prefix made the engine retry at every
# position — quadratic, measured ~2s at 16k chars) and preserves leading
# delimiters (``.joerg@…`` keeps its dot) while rejecting punctuation-only
# local parts (``--@example.com``). The local-part body is BOUNDED to {0,63}
# (RFC 5321 local-part limit): the unbounded quantifier was still QUADRATIC
# on punctuated adversarial input ("é."*N — a match could start after every
# separator and scan the whole tail; final-critic probe: 5.9s at 32k chars,
# inside the 65,536-byte capture cap). With the bound the same inputs run
# ~0.02s (linear); local parts over 64 chars are not valid addresses and no
# longer match (a >64-char dotted run gets only its valid tail redacted).
# Behaviour change: trailing non-ASCII adjacency now redacts
# (``user@example.comé`` → ``[REDACTED_EMAIL]é``). Under re.IGNORECASE the
# ASCII classes case-fold to also match U+0130/U+0131/U+017F/U+212A-class
# codepoints — a known, pinned gap. Verified ASCII-context-equal over 16,641
# predecessor/successor contexts and linear at 8k/16k/32k adversarial inputs
# (pure word runs AND punctuated alternations).
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9_\u0300-\u036f\u00c0-\u024f])"
    r"[A-Za-z0-9_\u0300-\u036f\u00c0-\u024f][A-Za-z0-9_\u0300-\u036f\u00c0-\u024f.%+-]{0,63}@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
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
