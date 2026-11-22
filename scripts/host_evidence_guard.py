"""Host-gate evidence stdout_raw guard (issue #438 AC8, production seam).

The published host-gate evidence corpus under docs/host-gate-evidence/
must never retain raw recall memory in a capture's ``stdout_raw`` field.
The rule is NAME-INDEPENDENT for the payload rule: any ``*.json`` that
parses as a dict and carries a ``stdout_raw`` key must carry the fixed
REDACTED marker, whatever the capture is named. Launcher-capture names
additionally require a well-formed dict carrying the marker — a missing
key (e.g. ``{}``) or malformed JSON under a launcher-capture name is
unsafe. Malformed JSON under any other name is ignored (a
claude-run2-shaped file is JSON plus trailing text with no stdout_raw).

Issue #448 widens the guard with three additive surfaces, all stdlib-only
and parallel to ``unsafe_recall_paths`` (whose bytes/behavior are frozen
by tests/test_host_evidence_redaction.py):

- ``unsafe_incident_shape_paths(root, allowlist=None)``: flags any
  capture-shaped ``*.json`` dict (a ``remote_url`` key OR any string
  value containing ``://``) whose string values carry a credential
  key=value assignment matching the incident shape — the shape that
  leaked through refs/pull/402/head and that redaction.py's ``\\S{8,}``
  floor structurally missed. Values are NON-SPACE semantics: 4+ chars
  of ``[^\\s"']`` behind an optional single-or-double quote.
- ``unsafe_jsonl_transcript_paths(root, allowlist=None)``: scans
  ``*.jsonl`` transcripts line by line — a malformed line flags the
  file (fail-closed, .jsonl-only), and a fence open marker (live or
  neutralized) whose body carries no ``[REDACTED:`` notice flags the
  line. Memory-bearing text is walked from EVERY nested string carrier
  (payload.content[].text, payload.item.content[].text, state text
  carriers), never just top-level keys.
- the allowlist surface (``parse_allowlist`` / ``validate_allowlist``):
  a reviewed true-positive may be suppressed only by an entry pinning
  path + detector id + the file's CURRENT whole-file git blob sha +
  the zero-based hit ordinal + the total hit count — never a hash of
  the matched secret span (a span hash is structurally refused: the
  only hash field accepted is the whole-file blob identity).

Imported by tests/test_host_evidence_redaction.py (and the #448 check
files) as its enforcement seam (a test-imported module under scripts/
by design — there is no runtime caller; CI runs the importing test
files per the repo's direct-python loop); reports relative posix
filenames only, never evidence content.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

REDACTED = ("[REDACTED: raw recall output removed because it contained "
            "live credentials]")

LAUNCHER_CAPTURE_SUFFIXES = (
    "-launcher-recall.json",
    "-launcher-pretool-recall.json",
    "-launcher-session-start.json",
)

# --- issue #448: incident-shape + jsonl + allowlist surface ---

INCIDENT_SHAPE_DETECTOR_ID = "incident-credential-remote-url"
JSONL_RAW_RECALL_DETECTOR_ID = "jsonl-raw-recall"

# The incident credential shape: a key-family assignment whose VALUE is
# 4+ NON-SPACE characters (redaction.py's \S{8,} floor missed short and
# punctuation-bearing values; an alphabet whitelist would miss real
# punctuation — both are the defect). Quoted values are consumed; the
# value class deliberately INCLUDES quote characters so an embedded-quote
# value (TOKEN="ab'cd") still matches — the verdict is a human-review
# flag over capture-shaped files, where over-matching is the safe
# direction. The key family matches ANYWHERE (no word-boundary or
# lookbehind prefix): implementation-review round 1 proved a \b prefix
# silently excluded the common env-var shapes (GITHUB_TOKEN,
# AWS_SECRET_ACCESS_KEY), and the PR #451 feedback round proved the
# lookbehind replacement still excluded camelCase compounds
# (clientSecret, accessToken) and suffix compounds (SECRET_KEY,
# PRIVATE_KEY); cubic's review reached the same recommendation. A benign
# word containing a family substring (notatoken=x) now over-matches —
# accepted noise per the safe-direction rule.
_CREDENTIAL_KV_RE = re.compile(
    r"(?i)(?:token|api[_-]?key|secret(?:[_-]?key)?|password|passwd|pwd|"
    r"private[_-]?key|access[_-]?key|admin[_-]token)\s*[=:]\s*['\"]?([^\s]{4,})"
)

_FENCE_OPEN_MARKERS = (
    "<<<ZMEM_UNTRUSTED_FENCE>>>",
    "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>",
)
# Issue #511: genuine render fences carry a per-render token, so exact
# literals alone would silently stop matching every NEW render. The open
# side therefore matches by grammar (canonical, pre-neutralized, or
# token-bearing), mirroring the END-side prefix match below and the
# _FENCE_CLOSER_RE pattern in hooks/lib/zmem-session-start-payload.py.
_FENCE_OPEN_RE = re.compile(
    r"<<<ZMEM_UNTRUSTED_FENCE(?::[0-9A-Za-z]{1,32})?(_NEUTRALIZED)?>>>")
_FENCE_END_PREFIX = "<<<END_ZMEM_UNTRUSTED_FENCE"
_REDACTION_NOTICE = "[REDACTED:"

ALLOWLIST_ENTRY_KEYS = frozenset({
    "path", "detector", "file_blob_sha", "occurrence_position", "count",
})


class AllowlistError(ValueError):
    """A malformed, stale, or value-hash-shaped allowlist entry."""


def _is_launcher_capture(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in LAUNCHER_CAPTURE_SUFFIXES)


def _reject_duplicate_keys(pairs):
    """json object_pairs_hook: any duplicate key raises (issue #438
    review PRR-005 — last-wins parsing let a capture carry a raw
    stdout_raw followed by a REDACTED duplicate and pass the guard)."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key: %s" % key)
        out[key] = value
    return out


def unsafe_recall_paths(root: Path) -> list:
    """Return filenames (relative posix) of unsafe captures under root.

    Never include raw evidence in the returned values.
    """
    unsafe = []
    for path in sorted(root.rglob("*.json")):
        name = path.name
        duplicate = False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"),
                                 object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError:
            payload = None
        except (ValueError, OSError):
            # A duplicate-key file is VALID-shaped JSON with hostile
            # parse semantics: unsafe under ANY name (a plain
            # JSONDecodeError under a non-launcher name stays ignored —
            # the claude-run2 JSON-plus-trailing-text shape).
            duplicate = True
            payload = None
        if duplicate or _is_launcher_capture(name):
            # Launcher-capture contract: a well-formed dict whose
            # stdout_raw IS the marker. A missing key is unsafe.
            safe = (not duplicate
                    and isinstance(payload, dict)
                    and payload.get("stdout_raw") == REDACTED)
        elif isinstance(payload, dict) and "stdout_raw" in payload:
            # Name-independent rule: any parsed dict that carries the
            # field must carry the marker.
            safe = payload["stdout_raw"] == REDACTED
        else:
            # Non-launcher name without the field: not this guard's
            # concern (malformed non-launcher JSON is ignored).
            safe = True
        if not safe:
            unsafe.append(path.relative_to(root).as_posix())
    return unsafe


def _iter_string_values(obj):
    """Yield every string value in a decoded JSON structure, depth-first,
    in deterministic order (dict insertion order, then list order). The
    transcript's memory text lives in nested carriers -- payload.content[]
    [].text, payload.item.content[].text, state text carriers -- never only
    at the top level."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_string_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_string_values(value)


def _is_capture_shaped(payload: dict) -> bool:
    """The incident shape's carrier: a dict with a remote_url key OR any
    string value carrying a live scheme:// URL."""
    if "remote_url" in payload:
        return True
    return any("://" in value for value in _iter_string_values(payload))


def _incident_hit_count(payload: dict) -> int:
    """One hit per credential-kv MATCH, in deterministic order (string-value
    walk order, then in-value match order)."""
    total = 0
    for value in _iter_string_values(payload):
        total += sum(1 for _ in _CREDENTIAL_KV_RE.finditer(value))
    return total


def _fence_body_unredacted(text: str) -> bool:
    """True when any fence open marker in text has a body carrying no
    [REDACTED: notice (class-level rule: every shipped notice starts with
    that prefix; notice-absence in a live/neutralized fence body is the
    defect signal).

    Issue #511: opens match by grammar (canonical, pre-neutralized, or
    token-bearing) — the exact-literal loop alone would silently stop
    matching every new render once fences carry per-render tokens."""
    start = 0
    while True:
        match = _FENCE_OPEN_RE.search(text, start)
        if match is None:
            return False
        body_start = match.end()
        end_at = text.find(_FENCE_END_PREFIX, body_start)
        body = (text[body_start:end_at] if end_at >= 0
                else text[body_start:])
        if _REDACTION_NOTICE not in body:
            return True
        start = body_start


def git_blob_sha(path: Path) -> str:
    """The file's whole-file identity: the git blob sha of the
    CRLF-NORMALIZED bytes (sha1(b"blob <len>\\0" + data with \\r\\n ->
    \\n)). Stdlib-only; no git subprocess. The normalization makes the
    identity PLATFORM-STABLE: a windows checkout with core.autocrlf=true
    materializes text files with CRLF, so raw-byte hashing produced a
    different sha per platform (the PR #451 CI round caught exactly
    that — ubuntu green, windows red on the pinned transcript literal);
    the normalized sha matches what git stores for the LF file and what
    both platforms compute. Same normalization concept as the release
    manifest's sha256-crlf-norm digests."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def parse_allowlist(text: str) -> list:
    """Parse an allowlist JSON document; refuse every malformed entry.

    Refusals (AllowlistError): non-list root; non-dict entry; any key
    outside ALLOWLIST_ENTRY_KEYS (a span-hash key is structurally refused);
    missing key; wrong types (path/detector non-empty str; file_blob_sha
    exactly 40 lowercase hex; occurrence_position int >= 0; count int >= 1
    -- bools are ints in Python and are refused); malformed JSON.
    """
    try:
        entries = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AllowlistError(
            "allowlist document is not valid JSON: %s" % exc) from None
    except ValueError as exc:
        # _reject_duplicate_keys refuses duplicate members (PR #451
        # feedback PRR-018: last-wins parsing let a dup-key document
        # through with only the surviving value visible).
        raise AllowlistError(
            "allowlist document has duplicate JSON keys: %s" % exc) from None
    if not isinstance(entries, list):
        raise AllowlistError("allowlist root must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise AllowlistError("allowlist entry must be an object")
        keys = frozenset(entry)
        if keys != ALLOWLIST_ENTRY_KEYS:
            unknown = sorted(keys - ALLOWLIST_ENTRY_KEYS)
            missing = sorted(ALLOWLIST_ENTRY_KEYS - keys)
            raise AllowlistError(
                "allowlist entry keys must be exactly %s (unknown: %s; "
                "missing: %s)"
                % (sorted(ALLOWLIST_ENTRY_KEYS), unknown, missing))
        if not isinstance(entry["path"], str) or not entry["path"]:
            raise AllowlistError(
                "allowlist path must be a non-empty string")
        if (not isinstance(entry["detector"], str)
                or not entry["detector"]):
            raise AllowlistError(
                "allowlist detector must be a non-empty string")
        sha = entry["file_blob_sha"]
        if (not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", sha)):
            raise AllowlistError(
                "file_blob_sha must be exactly 40 lowercase hex characters "
                "(the whole-file git blob identity -- a hash of a matched "
                "secret span is never accepted)")
        for field, floor in (("occurrence_position", 0), ("count", 1)):
            value = entry[field]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < floor):
                raise AllowlistError(
                    "allowlist %s must be an int >= %d (bools refused)"
                    % (field, floor))
    return entries


def validate_allowlist(entries, root: Path) -> None:
    """Refuse stale/unknown entries: the path must exist under root, the
    blob sha must equal the file's CURRENT whole-file identity, and the
    detector must be a known id. Raises AllowlistError on any refusal."""
    known = {INCIDENT_SHAPE_DETECTOR_ID, JSONL_RAW_RECALL_DETECTOR_ID}
    for entry in entries:
        if entry["detector"] not in known:
            raise AllowlistError(
                "unknown detector id: %s" % entry["detector"])
        path = root / entry["path"]
        if not path.is_file():
            raise AllowlistError(
                "allowlisted path does not exist: %s" % entry["path"])
        if git_blob_sha(path) != entry["file_blob_sha"]:
            raise AllowlistError(
                "stale allowlist entry (file_blob_sha does not match the "
                "CURRENT file identity): %s" % entry["path"])


def _is_exact_int(value) -> bool:
    """True for a real int — bool is an int subclass in Python and the
    allowlist contract refuses it (parse and scan agree, PR #451
    feedback PRR-006: a scan-level numeric comparison let count=true
    suppress a hit parse_allowlist refuses)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _suppressed_ordinals(rel: str, detector_id: str, path: Path,
                         total_hits: int, allowlist) -> set:
    """Ordinals suppressed by entries pinning (path, detector, the file's
    CURRENT blob sha, position, count == total). Any mismatch leaves the
    hit standing. Type rules mirror parse_allowlist exactly: position and
    count must be real ints (bools refused)."""
    if not allowlist:
        return set()
    current_sha = git_blob_sha(path)
    suppressed = set()
    for entry in allowlist:
        if (entry.get("path") == rel
                and entry.get("detector") == detector_id
                and entry.get("file_blob_sha") == current_sha
                and _is_exact_int(entry.get("count"))
                and entry.get("count") == total_hits):
            position = entry.get("occurrence_position")
            if _is_exact_int(position) and 0 <= position < total_hits:
                suppressed.add(position)
    return suppressed


def unsafe_incident_shape_paths(root: Path, allowlist=None) -> list:
    """Return filenames (relative posix) of capture-shaped *.json dicts
    under root carrying the incident credential shape. Never include raw
    evidence in the returned values.

    Error semantics mirror the frozen ``unsafe_recall_paths`` guard
    (PR #451 feedback PRR-001): a duplicate-key document is VALID-shaped
    JSON with hostile parse semantics and is flagged under ANY name, and
    an undecodable file is flagged, not silently skipped; only an
    unparseable (JSONDecodeError) or unreadable (OSError) file is
    skipped, exactly like the frozen rule.
    """
    unsafe = []
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root).as_posix()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"),
                                 object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError:
            continue
        except OSError:
            continue
        except ValueError:
            # Duplicate keys (ValueError from the hook) and undecodable
            # bytes (UnicodeDecodeError subclasses ValueError): hostile
            # parse semantics -> unsafe under any name, matching the
            # frozen guard's duplicate branch.
            unsafe.append(rel)
            continue
        if not isinstance(payload, dict) or not _is_capture_shaped(payload):
            continue
        total = _incident_hit_count(payload)
        if not total:
            continue
        suppressed = _suppressed_ordinals(
            rel, INCIDENT_SHAPE_DETECTOR_ID, path, total, allowlist)
        if len(suppressed) < total:
            unsafe.append(rel)
    return unsafe


def _jsonl_line_offending(payload, line: str) -> bool:
    """One line's verdict under the transcript detector: a fence open
    marker (live or neutralized) whose body carries no [REDACTED: notice,
    OR the incident capture shape (a dict with a remote_url key OR any
    :// string value) carrying a credential key=value assignment
    (PR #451 feedback SH-A-001: the credential+URL shape was ungoverned in
    transcripts — the two detectors were extension-disjoint)."""
    for value in _iter_string_values(payload):
        if _fence_body_unredacted(value):
            return True
    if isinstance(payload, dict) and _is_capture_shaped(payload):
        return _incident_hit_count(payload) > 0
    return False


def _jsonl_offending_count(path: Path) -> int:
    """The transcript's offending-line count under the current detector
    semantics (the allowlist's `count` field for this file). A decode
    error counts the file as one offending unit (fail-closed, matching
    the scanner); an unreadable (OSError) file is skipped by the
    scanner, so this helper returns the fail-closed unit count 1 for it
    too — the caller pairing it with an allowlist entry never pairs
    with a file the scanner cannot read."""
    try:
        data = path.read_bytes()
    except OSError:
        return 1
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 1
    text = text.lstrip("\ufeff")
    offending = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            payload = json.loads(line,
                                 object_pairs_hook=_reject_duplicate_keys)
        except ValueError:
            offending += 1  # malformed or duplicate-key line: fail-closed
            continue
        if _jsonl_line_offending(payload, line):
            offending += 1
    return offending


def unsafe_jsonl_transcript_paths(root: Path, allowlist=None) -> list:
    """Return filenames (relative posix) of *.jsonl transcripts under
    root whose lines carry unredacted raw-recall content or the incident
    capture shape with a credential. Fail-closed on every unreadable
    line class (PR #451 feedback PRR-002/003): a MALFORMED line, a
    DUPLICATE-KEY line (last-wins parsing could hide an unredacted fence
    behind the surviving member — the #438 PRR-005 class), and a
    NON-UTF-8 file each flag rather than skip or crash; a UTF-8 BOM and
    raw U+2028/U+0085 inside JSON strings no longer split lines
    (split on \\n only, BOM stripped). Hit granularity: one hit per
    offending physical line."""
    unsafe = []
    for path in sorted(root.rglob("*.jsonl")):
        rel = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            unsafe.append(rel)  # fail-closed: an undecodable transcript
            continue            # is flagged, never skipped or raised
        text = text.lstrip("\ufeff")
        offending = 0
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                payload = json.loads(line,
                                     object_pairs_hook=_reject_duplicate_keys)
            except ValueError:
                # JSONDecodeError (malformed) and duplicate-key
                # ValueError are both fail-closed flags — last-wins
                # parsing must not let a surviving member hide a live
                # fence from the walk.
                offending += 1
                continue
            if _jsonl_line_offending(payload, line):
                offending += 1
        if not offending:
            continue
        suppressed = _suppressed_ordinals(
            rel, JSONL_RAW_RECALL_DETECTOR_ID, path, offending, allowlist)
        if len(suppressed) < offending:
            unsafe.append(rel)
    return unsafe
