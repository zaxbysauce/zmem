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
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd|pass|private[_-]?key)\s*[:=]\s*(\S{8,})"),
    # Authorization headers occur in prompts, rendered fences, and operation
    # transcripts.  Keep this ahead of the generic token patterns so the whole
    # credential (rather than an arbitrary suffix) is replaced consistently.
    # The token alphabet contains several non-word characters.  A trailing
    # ``\b`` therefore backtracks before a terminal ``.``, ``_``, ``+``, ``/``,
    # ``~`` or ``-`` and leaks that character.  Require the next character to
    # be outside the credential alphabet so the greedy match consumes the
    # complete token while still respecting ordinary delimiters.
    re.compile(r"(?i)(?<![A-Za-z0-9_])Bearer\s+[A-Za-z0-9._+/~-]{16,}(?![A-Za-z0-9._+/~-])"),
    # RFC 7617 Basic scheme (issue #502), TWO rules (review 4.5 r6):
    # 1. EXPLICIT AUTHORIZATION HEADER (`Authorization: Basic <val>`): the
    #    context is unambiguous, so the value matches UNCONDITIONALLY at 4+
    #    chars (the base64 minimum) — no length floor, no case requirement:
    #    mixed, all-uppercase, digit-bearing, AND pure-lowercase values all
    #    redact. The cost is fail-safe over-redaction of long prose inside a
    #    literal auth header ("Authorization: Basic authentication") — the
    #    SAME accepted fail-safe direction as the #180 matrix's ssh -p port
    #    precedent; a literal 12+ char word as a header VALUE carries no
    #    credential meaning anyway.
    # 2. BARE `Basic <val>` (no header prefix): unconditional at 8+ chars —
    #    the SAME value floor as the key=value credential rule (\S{8,}) —
    #    so EVERY value shape (mixed, all-uppercase, digit-bearing, AND
    #    pure-lowercase) redacts in bare context too (review 4.5 r6: the
    #    lowercase-only bare gap was rejected — no user deferral exists).
    #    The cost is fail-safe over-redaction of 8+ char words after bare
    #    "Basic " ("Basic authentication" is redacted) — the #180 ssh -p
    #    fail-safe direction; sub-8-char values survive (below ANY
    #    credential floor in the registry). The header rule runs FIRST in
    #    the registry, so header contexts are consumed before this rule is
    #    tried. Whole-match semantics like Bearer; the alphabet-boundary
    #    convention is copied verbatim.
    re.compile(r"(?i)(?<![A-Za-z0-9_])Authorization\s*:\s*Basic\s+[A-Za-z0-9._+/=~-]{4,}(?![A-Za-z0-9._+/=~-])"),
    re.compile(r"(?i:(?<![A-Za-z0-9_])Basic)\s+[A-Za-z0-9._+/=~-]{8,}(?![A-Za-z0-9._+/=~-])"),
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

# Issue #502 identifier-context calibration: a generic-pattern match whose
# preceding text (same line, up to 48 chars back, stopping at any caller-
# supplied field-join boundary) labels the run as a verifiable identifier
# (release/commit/object checksum contexts) is EXEMPT from redaction — the
# value is shape-identical to a secret but verifiable, and destroying it is
# the data-loss half of #502. The fail-safe net stays: an UNLABELED run still
# redacts (frozen corpus entry `unlabeled-hex-run`). Credential/command/
# value-span patterns are NEVER exempted — a Bearer token after the word
# "release" still dies.
_GENERIC_PATTERNS = frozenset(SECRET_GENERIC_PATTERNS)
_IDENTIFIER_CONTEXT_RE = re.compile(
    r"(?i)(?:\b(?:release|commit|revision|object|blob|digest|tag|md5|sha1|sha256|sha512)\b\s*[:=]?"
    r"|\bbuild\s+id\b)\s*$"
)


def is_identifier_context_match(
    text: str, match_start: int, boundary: int | None = None,
) -> bool:
    """True when the text before ``match_start`` labels the run as a benign,
    verifiable identifier (issue #502 calibration).

    The window is the same-line text ending at ``match_start`` (at most 48
    chars back). ``boundary``, when given, is a field-join offset (the
    content|source_ref|tags join in ``write._check_secrets``): the window
    never crosses it, so an identifier keyword ending one field can never
    exempt a value in the next field. ``None`` (the redaction loop's single
    texts) imposes no join.
    """
    if match_start <= 0:
        return False
    window_start = max(0, match_start - 48)
    if boundary is not None:
        window_start = max(window_start, boundary)
    prefix = text[window_start:match_start]
    if "\n" in prefix:
        prefix = prefix[prefix.rfind("\n") + 1:]
    # ``search`` + the ``$`` anchor: the keyword may sit anywhere in the
    # window as long as only optional ``:/=`` and whitespace separate it from
    # the match start (``match`` would wrongly demand it at the window's
    # beginning — "Deployed release <sha>" has leading prose).
    return bool(_IDENTIFIER_CONTEXT_RE.search(prefix))

# Issue #180 command and URL credential shapes. Case-insensitivity is scoped
# with inline groups to the COMMAND NAME / URL SYNTAX only — option letters
# stay case-sensitive so ``sudo -s`` (shell) never matches ``sudo -S``.
# NOTE (cubic round, stated honestly): for ssh and psql the lowercase ``-p``
# IS the port flag, so ``ssh -p2222`` IS over-redacted here — that is the
# issue #180 matrix's explicit contract (positive p06 ``ssh -ppw180F``),
# accepted as fail-safe over-redaction and disclosed in SKILL.md; the case
# scoping only guarantees ``-P``-shaped tokens never match. Group 1 captures
# the value span in every entry.
# Issue #535: the URL-scheme credential family is applied by a single-pass
# scanner, not a compiled regex. The pre-fix pattern (kept verbatim as the
# sentinel's display ``pattern`` so gate warnings stay byte-identical) was
# QUADRATIC on punctuated and scheme-chain input (12.3s for 'a.'*32768 at
# the 65,536-char capture cap): its unbounded scheme and password runs
# backtrack/scan O(n) per start position. No single compiled regex is both
# linear on chains and match-set-exact — a bounded password run fails open
# on long passwords, excluding ':' from the password class fails open on
# RFC-valid raw-colon passwords, and a class-mirroring run-start boundary
# loses '.https://'-family matches (plan-critic rounds 1-3, probe-verified).
# The scanner reproduces the regex's match decision EXACTLY (spans, outputs,
# detection counts, and the scoped-IGNORECASE Unicode folds — membership is
# delegated to the engine's own single-char patterns) and adds the memo a
# backtracking engine cannot express: a failed password scan proves no '@'
# exists before the next whitespace, so later attempts starting inside that
# region fail in O(1) — worst case one forward scan per whitespace-delimited
# region (linear; <=72ms worst observed at the cap vs 12.3s at base).
_URL_CREDENTIAL_PATTERN_SOURCE = (
    r"(?i:(?<![A-Za-z0-9_])[a-z][a-z0-9+.-]*://)[^\s:/@]+:([^\s@]+)@"
)
_URL_SCHEME_CHAR = re.compile(r"(?i)[a-z0-9+.-]")
_URL_LETTER = re.compile(r"(?i)[a-z]")
_URL_PREV = re.compile(r"(?i)[A-Za-z0-9_]")
_URL_USER_RUN = re.compile(r"[^\s:/@]+")
_URL_PW_RUN = re.compile(r"[^\s@]*")


class _URLCredentialMatch:
    """Match-protocol adapter for the URL credential scanner (``group``/
    ``span``/``start``/``end`` with the whole span = scheme start .. the
    terminating ``@`` exclusive-end, group 1 = the password span)."""

    __slots__ = ("_text", "_start", "_end", "_ps", "_pe")

    def __init__(self, text, start, end, ps, pe):
        self._text = text
        self._start = start
        self._end = end
        self._ps = ps
        self._pe = pe

    def group(self, group=0):
        if group == 0:
            return self._text[self._start:self._end]
        if group == 1:
            return self._text[self._ps:self._pe]
        raise IndexError("no such group")

    def span(self, group=0):
        if group == 0:
            return (self._start, self._end)
        if group == 1:
            return (self._ps, self._pe)
        raise IndexError("no such group")

    def start(self, group=0):
        return self.span(group)[0]

    def end(self, group=0):
        return self.span(group)[1]


class _URLCredentialScannerPattern:
    """Duck-typed pattern occupying the URL entry's registry slot. Every
    consumer that iterates the secret registries — the redaction loop's
    ``finditer`` + match protocol, write.py's capture gates and recall.py's
    classifiers via ``search`` + the sliceable ``pattern`` display string —
    gets scanner semantics at the exact sequence position, with no consumer
    changes (issue #535 plan-critic round 3: keeping the regex for the gates
    would preserve their quadratic DoS surface)."""

    pattern = _URL_CREDENTIAL_PATTERN_SOURCE

    def _matches(self, text):
        text = text or ""
        i = 0
        n = len(text)
        barrier = -1
        while True:
            q = text.find("://", i)
            if q < 0:
                break
            s = q
            while s > 0 and _URL_SCHEME_CHAR.fullmatch(text[s - 1]):
                s -= 1
            start = -1
            for pos in range(s, q):
                if (_URL_LETTER.fullmatch(text[pos])
                        and (pos == 0
                             or not _URL_PREV.fullmatch(text[pos - 1]))):
                    start = pos
                    break
            if start < 0:
                i = q + 3
                continue
            user = _URL_USER_RUN.match(text, q + 3)
            if user is None:
                i = q + 3
                continue
            colon = user.end()
            if colon >= n or text[colon] != ":":
                i = q + 3
                continue
            pw_start = colon + 1
            if pw_start < barrier:
                i = q + 3
                continue
            pw_end = _URL_PW_RUN.match(text, pw_start).end()
            if pw_end < n and text[pw_end] == "@" and pw_end > pw_start:
                yield (start, pw_end + 1, pw_start, pw_end)
                i = pw_end + 1
            else:
                if pw_end > barrier:
                    barrier = pw_end
                i = q + 3

    def finditer(self, text):
        for start, end, ps, pe in self._matches(text):
            yield _URLCredentialMatch(text, start, end, ps, pe)

    def search(self, text):
        for match in self.finditer(text):
            return match
        return None


_URL_CREDENTIAL_SCANNER = _URLCredentialScannerPattern()

SECRET_COMMAND_PATTERNS = [
    # sshpass -p <value> (standalone -p; any whitespace, incl. tabs)
    re.compile(r"(?i:(?<![A-Za-z0-9_])sshpass(?![A-Za-z0-9_]))\s+-p\s+(\S+)"),
    # --password=<value> (attached form only; spaced/dangling are not matches)
    re.compile(r"(?i:--password=)(\S+)"),
    # ssh/mysql/psql -p<value> (attached form only, exactly these commands;
    # over-redacts ports for ssh/psql per the issue matrix)
    re.compile(r"(?i:(?<![A-Za-z0-9_])(?:ssh|mysql|psql)(?![A-Za-z0-9_]))[ \t]+-p(\S+)"),
    # scheme://user:password@host — replace only the password (issue #535:
    # scanner sentinel, exact old match semantics, linear on adversarial
    # input; see _URLCredentialScannerPattern)
    _URL_CREDENTIAL_SCANNER,
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
    exempt_values: frozenset[str] = frozenset(),
) -> tuple[str, int, list[int]]:
    """Redact with the canonical pattern sequence and map selected positions.

    ``positions`` is intentionally caller-bounded.  Scan needs at most its
    retained match positions, so it avoids allocating provenance for every
    character of an otherwise valid 8 MiB record.  A position inside a
    redacted value maps to the replacement marker's start; callers therefore
    never search a marker that might itself contain their literal.

    ``exempt_values`` (issue #502 repair durability) suppresses detection AND
    redaction for GENERIC-pattern matches whose text is an exact verified
    value — the repair lane's row-scoped exemption. Credential/command/
    value-span patterns are never exempted by it.
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
            if (pattern in _GENERIC_PATTERNS
                    and (match.group(0) in exempt_values
                         or is_identifier_context_match(redacted, match.start()))):
                # Calibrated identifier or verified repair value: the match is
                # "not a secret" — the bytes pass through unchanged and the
                # detection count does not increase.
                replacement = match.group(0)
                counted = False
            else:
                replacement = (_value_span_replacement(match)
                               if pattern in _VALUE_SPAN_PATTERNS
                               else REDACTED_SECRET)
                counted = True
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
            if counted:
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


def redact_secret_like_text(
    text: str, exempt_values: frozenset[str] = frozenset(),
) -> tuple[str, int]:
    """Replace secret-like VALUES with ``[REDACTED_SECRET]``.

    Value-span patterns rewrite only the captured value; everything else
    keeps the historical whole-match replacement. Returns
    ``(redacted_text, detection_count)``; the count includes matches whose
    value was already the marker (idempotent no-ops). ``exempt_values``
    (issue #502 repair durability) suppresses detection AND redaction for
    GENERIC-pattern matches whose text is an exact verified value; the
    default keeps every caller's behavior byte-identical.
    """
    redacted, count, _positions = _redact_secret_like_text_with_positions(
        text, [], exempt_values=exempt_values)
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
# ``連絡先はuser@example.com`` → ``[REDACTED_EMAIL]``).
#
# Three structural properties make this pattern LINEAR, FAIL-CLOSED, and
# punctuation-rejecting together:
#   1. The leading boundary excludes the ENTIRE local-part char class
#      (``(?<![...%+-])``), so a match can only START at the beginning of a
#      maximal local-part run — the engine never retries inside a run. This is
#      what keeps punctuated adversarial input (``a.``*N) linear: without it the
#      engine restarts at every separator and scans the whole tail
#      (measured ~2s at 16k chars pre-fix).
#   2. The local-part quantifier is POSSESSIVE (``*+``): it consumes the whole
#      maximal run and does NOT backtrack to a shorter length. This is what
#      makes it FAIL-CLOSED on over-length local parts — a >64-char run with
#      separators (``a_``*32 + ``x@example.com``) is matched in full and
#      redacted, exactly as the pre-#523 base regex ``\b[A-Z0-9._%+-]+@…`` did.
#      A bounded ``{0,63}`` quantifier stops before the ``@`` on such a run and
#      FAILS OPEN (leaks the whole address); the possessive run-start form is
#      linear AND fail-closed. The RFC 5321 64-char bound is not enforced
#      character-wise — an over-length address is simply redacted in full
#      (invalid either way).
#   3. A word-char lookahead (``(?=[…]…[A-Za-z0-9_…])``) requires at least one
#      word char in the run, so punctuation-only local parts (``--@example.com``,
#      ``.@example.com``) are rejected — the PR's original intent, preserved.
#
# Behaviour: trailing non-ASCII adjacency now redacts
# (``user@example.comé`` → ``[REDACTED_EMAIL]é``); a leading delimiter is
# CONSUMED as part of the local part (``.joerg@…`` → ``[REDACTED_EMAIL]``),
# unlike the PR's earlier word-char-first form which preserved it — this is the
# one deliberate behaviour change vs the pre-review PR, and it matches the
# pre-#523 base regex (which also consumed the delimiter); non-Latin runs before
# an address survive (M-1). Under re.IGNORECASE the ASCII classes case-fold to
# also match U+0130/U+0131/U+017F/U+212A-class codepoints — a known, pinned gap.
# Verified linear at 8k/16k/32k adversarial inputs (pure word runs AND
# punctuated alternations) and fail-closed on over-length runs.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9_\u0300-\u036f\u00c0-\u024f.%+-])"
    r"(?=[A-Za-z0-9_\u0300-\u036f\u00c0-\u024f.%+-]*[A-Za-z0-9_\u0300-\u036f\u00c0-\u024f])"
    r"[A-Za-z0-9_\u0300-\u036f\u00c0-\u024f.%+-]*+"
    r"@"
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
