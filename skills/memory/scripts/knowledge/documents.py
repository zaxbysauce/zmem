"""Document admission and immutable create/read for knowledge targets.

Content admission order (approved plan, 2026-11-13 — do not reorder):

1. SECRET SCAN FIRST, through the real capture-pattern source
   (:func:`redaction.redact_secret_like_text`).  Any detection REJECTS the
   publication (:class:`DocumentRejected`) — the companion never silently
   publishes altered content, and it never publishes a detected secret.
2. Newline normalization: CRLF and lone CR become LF; the text ends with
   exactly one terminal LF.
3. Hash the FULL canonical bytes (SHA-256), and derive the segmented
   digest form ``16hex-16hex-16hex-16hex`` used in locators — a raw 64-hex
   token would itself trip the generic secret scanner, so it never appears
   in a filename, a ref, or the journal.

Identity model:

- Logical identity: ``knowledge:<target-id>:<logical-key>`` where the key
  is ``/``-separated validated segments (slug or dashed-UUID each).
- Immutable version locator: ``<logical-key>/<segmented-digest>.md`` —
  deterministic, content-addressed, never overwritten.

Targets (types from :mod:`knowledge.config`, imported lazily so this
module never drags the storelib import chain by itself):

- ``MarkdownStore`` — exclusive ``O_CREAT|O_EXCL`` create under the
  configured root; symlinks/junctions (reparse points) on the note path,
  every ancestor, and the root itself are refused; create is verified by
  readback.
- ``KBClient`` — POST/GET against the exact allowlisted endpoint; never
  follows redirects; bounded response reads; bearer token from the
  configured ``credential_env``/``token_file`` (the value is never placed
  in an exception or log line); 409 or a timed-out create recovers via
  GET-and-compare — equal content is success, a mismatch is a conflict,
  and the client never PUTs, PATCHes, or renames.

No module here logs.  No secret value or note content is ever embedded in
an exception message.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from redaction import redact_secret_like_text

if TYPE_CHECKING:  # pragma: no cover
    from knowledge.config import KBHTTPTarget, MarkdownTarget, Target

__all__ = [
    "ConflictError",
    "CreateResult",
    "Document",
    "DocumentRejected",
    "IntegrityError",
    "KBClient",
    "MarkdownStore",
    "TargetError",
    "TransportError",
    "canonical_content",
    "content_digest",
    "logical_ref",
    "open_target",
    "parse_locator",
    "parse_segmented_digest",
    "prepare_document",
    "segment_digest",
    "validate_logical_key",
    "version_locator",
]

REF_PREFIX = "knowledge:"

# Bounded content: the companion publishes notes, not payloads.
MAX_CONTENT_BYTES = 4 * 1024 * 1024

# Logical keys: 1..8 '/'-separated segments.
MAX_KEY_SEGMENTS = 8
MAX_KEY_LENGTH = 128
MAX_REF_LENGTH = 256

# A slug segment: 1..31 chars lowercase, inner hyphens (kept < 32 so no
# segment can ever collide with the generic 32+ hex/alnum secret shapes).
_SLUG_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$")
# The one documented exception: a dashed lowercase UUID (36 chars) — its
# longest contiguous hex run is 12, so it is scanner-safe.
_UUID_SEGMENT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_SEGMENTED_DIGEST_RE = re.compile(
    r"^[0-9a-f]{16}-[0-9a-f]{16}-[0-9a-f]{16}-[0-9a-f]{16}$")

# Token file bounds: a bearer token is small; anything else is a mistake.
MAX_TOKEN_BYTES = 4096

_O_BINARY = getattr(os, "O_BINARY", 0)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class DocumentRejected(ValueError):
    """The authored content (or identity) is unfit for publication.

    The message describes the reason category and NEVER quotes the content
    or a secret value."""


class TargetError(RuntimeError):
    """A target-side failure.  Safe detail only: ids, statuses, paths —
    never note content and never a token."""


class ConflictError(TargetError):
    """The immutable version exists with DIFFERENT content.  The companion
    never overwrites: the caller must choose a new logical key or version."""


class TransportError(TargetError):
    """Network/protocol failure: unreachable endpoint, timeout, redirect,
    oversized response, or a non-recoverable status."""


class IntegrityError(TargetError):
    """A create succeeded at the target but the readback did not match the
    canonical digest."""


# ---------------------------------------------------------------------------
# content admission


def canonical_content(text: str) -> bytes:
    """Screen, normalize, and encode authored content.

    Scan FIRST with :func:`redaction.redact_secret_like_text`; ANY
    detection (count > 0, including already-redacted markers) raises
    :class:`DocumentRejected` — rejection over silent redaction, per the
    approved plan.  Survivors are normalized to LF with exactly one
    terminal newline and encoded UTF-8.  Content above
    :data:`MAX_CONTENT_BYTES` is rejected.
    """
    if not isinstance(text, str):
        raise DocumentRejected("content must be a string")
    _, detections = redact_secret_like_text(text)
    if detections:
        raise DocumentRejected(
            "content rejected: secret-like pattern detected (%d hit(s)); "
            "publication is refused rather than silently altered"
            % detections)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise DocumentRejected("content rejected: empty note")
    # Canonical form: LF line endings and exactly ONE terminal newline.
    normalized = normalized.rstrip("\n") + "\n"
    content = normalized.encode("utf-8")
    if len(content) > MAX_CONTENT_BYTES:
        raise DocumentRejected(
            "content rejected: %d bytes exceeds the %d byte limit"
            % (len(content), MAX_CONTENT_BYTES))
    return content


def content_digest(content: bytes) -> str:
    """Full SHA-256 over the canonical bytes, lowercase hex."""
    return hashlib.sha256(content).hexdigest()


def segment_digest(digest_hex: str) -> str:
    """``64-hex`` -> ``16hex-16hex-16hex-16hex`` (scanner-safe segments)."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest_hex):
        raise ValueError("digest must be 64 lowercase hex characters")
    return "-".join(digest_hex[i:i + 16] for i in range(0, 64, 16))


def parse_segmented_digest(segmented: str) -> str:
    """Inverse of :func:`segment_digest`; raises ``ValueError`` otherwise."""
    if not isinstance(segmented, str) or not _SEGMENTED_DIGEST_RE.fullmatch(
            segmented):
        raise ValueError(
            "segmented digest must be four 16-hex groups joined by '-'")
    return segmented.replace("-", "")


# ---------------------------------------------------------------------------
# identity


def validate_logical_key(key: str) -> str:
    """Validate a ``/``-separated logical key (slug or dashed-UUID segments).

    Each segment is a 1..31 lowercase slug or a dashed lowercase UUID;
    1..8 segments, total length at most :data:`MAX_KEY_LENGTH`; no empty
    segment, no backslash, no ``..``, no leading/trailing slash.
    """
    if not isinstance(key, str):
        raise DocumentRejected("logical key must be a string")
    if not key or key.startswith("/") or key.endswith("/") or "\\" in key:
        raise DocumentRejected("logical key must be a non-empty relative "
                               "slash-separated path")
    if len(key) > MAX_KEY_LENGTH:
        raise DocumentRejected(
            "logical key exceeds %d characters" % MAX_KEY_LENGTH)
    segments = key.split("/")
    if len(segments) > MAX_KEY_SEGMENTS:
        raise DocumentRejected(
            "logical key has more than %d segments" % MAX_KEY_SEGMENTS)
    for segment in segments:
        if segment in ("", ".", ".."):
            raise DocumentRejected(
                "logical key has an empty or relative segment")
        if not (_SLUG_SEGMENT_RE.fullmatch(segment)
                or _UUID_SEGMENT_RE.fullmatch(segment)):
            raise DocumentRejected(
                "logical key segment %r must be a 1..31 char lowercase slug "
                "[a-z0-9-] or a dashed lowercase uuid" % segment)
    return key


def logical_ref(target_id: str, logical_key: str) -> str:
    """The reserved source ref: ``knowledge:<target>:<key>``.

    The assembled ref must additionally be scanner-clean IN PAYLOAD
    CONTEXT (review rounds): the grammar admits target ids like
    ``dev-token`` whose ref then reads ``knowledge:dev-token:<key>``.
    A bare-ref scan is not enough — the credential value-span pattern
    needs a non-whitespace run after the keyword, so a SHORT key passed
    the bare scan while the serialized pointer payload (compact JSON
    glues ``","locator":…`` straight onto the ref with no whitespace)
    still tripped the auto-mode detector, and auto ingest REDACTED the
    payload mid-JSON. Admission therefore screens the canonical ref with
    exactly the payload-context delimiter appended, using the real
    detector (no duplicate pattern list, no exemptions): a credential
    keyword before the ref grammar's ``:`` is rejected for EVERY key
    length, BEFORE any document create or pointer mint. Local manual
    writes and auto sync/import/rescan now refuse the same refs (parity),
    with a count-only message that never quotes matched text. The chunk
    grammar (slug/UUID keys, segmented digests) is scanner-safe by
    construction and always passes.
    """
    from knowledge.config import validate_target_id

    try:
        validate_target_id(target_id)
    except ValueError as exc:
        raise DocumentRejected("invalid target id: %s" % exc) from exc
    validate_logical_key(logical_key)
    ref = REF_PREFIX + target_id + ":" + logical_key
    if len(ref) > MAX_REF_LENGTH:
        raise DocumentRejected(
            "logical ref exceeds %d characters" % MAX_REF_LENGTH)
    # The exact bytes that follow the ref inside the canonical payload:
    # sorted-key compact JSON puts `,"locator":…` right after the
    # logical_id value. Screening ref+delimiter closes the short-key
    # bypass with the actual capture detector.
    _redacted_ctx, ctx_hits = redact_secret_like_text(ref + '","locator"')
    if ctx_hits:
        raise DocumentRejected(
            "logical ref rejected: secret-like pattern detected (%d hit(s)) "
            "in payload context; a reserved knowledge: ref must stay "
            "scanner-clean in every capture mode — a target id that places "
            "a credential keyword (token/secret/key/password/…) directly "
            "before the ref grammar's ':' is refused for every key length"
            % ctx_hits)
    return ref


def version_locator(logical_key: str, digest_hex: str) -> str:
    """Deterministic immutable locator: ``<key>/<segmented-digest>.md``."""
    validate_logical_key(logical_key)
    return "%s/%s.md" % (logical_key, segment_digest(digest_hex))


def parse_locator(locator: str) -> tuple[str, str]:
    """Split a locator into ``(logical_key, digest_hex)``.

    Raises ``ValueError`` for anything that is not exactly
    ``<valid-key>/<4x16hex>.md``.
    """
    if not isinstance(locator, str) or not locator.endswith(".md"):
        raise ValueError("locator must end with '.md'")
    stem = locator[:-3]
    key, sep, segmented = stem.rpartition("/")
    if not sep or not _SEGMENTED_DIGEST_RE.fullmatch(segmented):
        raise ValueError("locator must be '<logical-key>/<4x16hex>.md'")
    validate_logical_key(key)
    return key, parse_segmented_digest(segmented)


@dataclass(frozen=True)
class Document:
    """A fully admitted, immutable, content-addressed note."""

    target_id: str
    logical_key: str
    ref: str
    content: bytes
    digest: str
    segmented_digest: str
    locator: str


def prepare_document(target_id: str, logical_key: str, text: str) -> Document:
    """Admission pipeline: screen -> normalize -> hash -> identify."""
    content = canonical_content(text)
    digest = content_digest(content)
    ref = logical_ref(target_id, logical_key)
    key = validate_logical_key(logical_key)
    return Document(
        target_id=target_id,
        logical_key=key,
        ref=ref,
        content=content,
        digest=digest,
        segmented_digest=segment_digest(digest),
        locator=version_locator(key, digest),
    )


# ---------------------------------------------------------------------------
# filesystem safety (markdown target)


def _is_reparse_point(path: Path) -> bool | None:
    """Reparse-point verdict for one path entry.

    Returns ``None`` when the path does not exist yet (the caller decides
    whether absence is expected — e.g. the note file before an exclusive
    create), ``True`` when the entry is a symlink/junction/reparse point
    or cannot be verified (fail closed), ``False`` when it is a plain
    stat-able entry.
    """
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return True
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    return bool(getattr(stat, "st_file_attributes", 0)
                & _FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_no_reparse_chain(final: Path, root: Path,
                             missing: str = "none") -> None:
    """Refuse when the note file, ANY ancestor up to the filesystem root,
    or the vault root is a symlink/junction/reparse point.  Walking past
    the vault root to the drive/filesystem root matches the store-side
    source-safety precedent (storelib.source.is_safe_regular_path): a
    junction ABOVE the configured vault root still carries every note
    write, and neither the root's own lstat nor the realpath containment
    check in _resolve catches it — only the full-ancestor walk does.

    ``missing`` says which components may legitimately not exist yet:
    ``"none"`` (read path — everything must exist), ``"file"`` (the note
    file itself is about to be created exclusively; every directory must
    already exist), or ``"any"`` (pre-mkdirs fast check; the directories
    are then created stepwise with per-step reparse checks and the chain
    is re-verified with ``"file"`` immediately before the create).
    Absence is only ever permitted BELOW the vault root (components the
    companion is about to create); every ancestor at or above the root
    must already exist.
    """
    if missing not in ("none", "file", "any"):
        raise AssertionError(missing)
    current = final
    at_final = True
    saw_root = False
    while True:
        verdict = _is_reparse_point(current)
        absent = verdict is None
        allowed_absent = (not saw_root
                          and (missing == "any"
                               or (missing == "file" and at_final)))
        if verdict is True or (absent and not allowed_absent):
            raise TargetError(
                "markdown target: refusing unsafe (reparse/absent) path "
                "component at %r" % str(current))
        if current == root:
            saw_root = True
        parent = current.parent
        if parent == current:
            # The filesystem root itself was verified by this iteration's
            # lstat; reaching it without having passed through the vault
            # root means the locator escaped (defense in depth).
            if not saw_root:
                raise TargetError(
                    "markdown target: note path escapes the vault root")
            break
        current = parent
        at_final = False


def _read_bounded(path: Path, max_bytes: int, what: str) -> bytes:
    fd = os.open(str(path), os.O_RDONLY | _O_BINARY)
    try:
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            raise TargetError(
                "%s exceeds the %d byte limit" % (what, max_bytes))
        return data
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# markdown target


class MarkdownStore:
    """Immutable create/read under one configured markdown root."""

    def __init__(self, target: "MarkdownTarget") -> None:
        from knowledge.config import MarkdownTarget as _MT

        if not isinstance(target, _MT):
            raise TypeError("MarkdownStore expects a MarkdownTarget")
        self._target = target
        self._root = Path(target.root)

    @property
    def target_id(self) -> str:
        return self._target.id

    def _resolve(self, locator: str) -> Path:
        parse_locator(locator)
        final = self._root.joinpath(*locator.split("/"))
        root_real = os.path.realpath(str(self._root))
        final_real = os.path.realpath(str(final))
        try:
            contained = os.path.commonpath([root_real, final_real]) == root_real
        except ValueError:
            contained = False
        if not contained:
            raise TargetError("markdown target: note path escapes the root")
        return final

    def _require_root(self) -> None:
        if not self._root.is_dir():
            raise TargetError(
                "markdown target %r: root %s is not an existing directory "
                "(the operator creates the vault root; the companion never "
                "does)" % (self._target.id, self._root))
        if _is_reparse_point(self._root):
            raise TargetError(
                "markdown target %r: root is a symlink/junction" %
                self._target.id)

    def _ensure_dirs(self, final: Path) -> None:
        """Create locator directories stepwise, refusing reparse points."""
        relative = final.relative_to(self._root)
        current = self._root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists():
                if not current.is_dir() or _is_reparse_point(current):
                    raise TargetError(
                        "markdown target: %r is not a real directory" %
                        str(current))
                continue
            try:
                os.mkdir(str(current))
            except FileExistsError:
                pass
            if not current.is_dir() or _is_reparse_point(current):
                raise TargetError(
                    "markdown target: %r is not a real directory" %
                    str(current))

    def create(self, document: Document) -> "CreateResult":
        """Exclusive immutable create with readback verification.

        An existing byte-identical version is SUCCESS
        (``existed_match=True``); existing different content is
        :class:`ConflictError`.  The file is never overwritten, renamed, or
        deleted.
        """
        if document.target_id != self._target.id:
            raise ValueError(
                "document targets %r, store is %r" %
                (document.target_id, self._target.id))
        self._require_root()
        final = self._resolve(document.locator)
        _assert_no_reparse_chain(final, self._root, missing="any")
        self._ensure_dirs(final)
        # Every directory exists now: verify the real chain immediately
        # before the exclusive create (narrows the TOCTOU window); only the
        # note file itself may still be absent.
        _assert_no_reparse_chain(final, self._root, missing="file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY
        try:
            fd = os.open(str(final), flags, 0o600)
        except FileExistsError:
            return self._existing_matches(document, final)
        except OSError as exc:
            raise TargetError(
                "markdown target %r: create failed at %r (%s)" %
                (self._target.id, str(final), exc.__class__.__name__)) from exc
        try:
            data = document.content
            offset = 0
            while offset < len(data):
                offset += os.write(fd, data[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        if self._read_verified(final, document.digest) is None:
            raise IntegrityError(
                "markdown target %r: readback digest mismatch for %r" %
                (self._target.id, document.locator))
        return CreateResult(locator=document.locator,
                            digest=document.digest,
                            existed_match=False)

    def _existing_matches(self, document: Document,
                          final: Path) -> "CreateResult":
        if self._read_verified(final, document.digest) is None:
            raise ConflictError(
                "markdown target %r: immutable version already exists with "
                "different content at %r (never overwritten)" %
                (self._target.id, document.locator))
        return CreateResult(locator=document.locator,
                            digest=document.digest,
                            existed_match=True)

    def _read_verified(self, final: Path, digest: str) -> bytes | None:
        if _is_reparse_point(final) or not final.is_file():
            return None
        data = _read_bounded(final, MAX_CONTENT_BYTES, "note file")
        return data if content_digest(data) == digest else None

    def read(self, locator: str) -> bytes:
        """Read an immutable version's canonical bytes."""
        self._require_root()
        final = self._resolve(locator)
        _assert_no_reparse_chain(final, self._root)
        if not final.exists():
            raise TargetError(
                "markdown target %r: no version at %r" %
                (self._target.id, locator))
        if _is_reparse_point(final) or not final.is_file():
            raise TargetError(
                "markdown target %r: %r is not a regular file" %
                (self._target.id, locator))
        return _read_bounded(final, MAX_CONTENT_BYTES, "note file")


# ---------------------------------------------------------------------------
# kb-http target


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KBClient:
    """Create/read immutable versions on one configured KB endpoint."""

    def __init__(self, target: "KBHTTPTarget",
                 env: Mapping[str, str] | None = None) -> None:
        from knowledge.config import KBHTTPTarget as _KT

        if not isinstance(target, _KT):
            raise TypeError("KBClient expects a KBHTTPTarget")
        self._target = target
        self._env = env
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect)

    @property
    def target_id(self) -> str:
        return self._target.id

    # -- token plumbing (the value never leaves except in the header)

    def _token(self) -> str:
        env_name = self._target.credential_env
        if env_name is not None:
            source = self._env if self._env is not None else os.environ
            value = source.get(env_name, "")
            token = value.strip()
            if not token:
                raise TargetError(
                    "kb target %r: credential env %r is unset or empty" %
                    (self._target.id, env_name))
        else:
            path = Path(str(self._target.token_file))
            try:
                raw = _read_bounded(path, MAX_TOKEN_BYTES, "token file")
            except OSError as exc:
                raise TargetError(
                    "kb target %r: token file unreadable (%s)" %
                    (self._target.id, exc.__class__.__name__)) from exc
            except TargetError:
                raise TargetError(
                    "kb target %r: token file exceeds %d bytes" %
                    (self._target.id, MAX_TOKEN_BYTES)) from None
            try:
                token = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                raise TargetError(
                    "kb target %r: token file is not valid UTF-8" %
                    self._target.id) from None
            if not token:
                raise TargetError(
                    "kb target %r: token file is empty" % self._target.id)
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise TargetError(
                "kb target %r: token exceeds %d bytes" %
                (self._target.id, MAX_TOKEN_BYTES))
        return token

    # -- request plumbing

    def _url(self, locator: str) -> str:
        encoded = urllib.parse.quote(locator, safe="/")
        return "%s/%s" % (self._target.endpoint, encoded)

    def _request(self, method: str, locator: str,
                 body: bytes | None = None) -> tuple[int, bytes]:
        """One bounded, redirect-refusing request.  Returns
        ``(status, body_bytes)``; redirects and network failures become
        :class:`TransportError`; response bodies are capped at the
        configured ``max_response_bytes``."""
        request = urllib.request.Request(
            self._url(locator), data=body, method=method)
        request.add_header("Authorization", "Bearer " + self._token())
        if body is not None:
            request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        limit = self._target.max_response_bytes
        try:
            with self._opener.open(request,
                                   timeout=self._target.timeout_s) as resp:
                status = resp.status
                payload = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                with exc:
                    payload = exc.read(limit + 1)
            except OSError:
                payload = b""
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError) as exc:
            raise TransportError(
                "kb target %r: %s to endpoint failed (%s)" %
                (self._target.id, method, exc.__class__.__name__)) from exc
        if 300 <= status < 400:
            raise TransportError(
                "kb target %r: endpoint answered HTTP %d; redirects are "
                "never followed" % (self._target.id, status))
        if len(payload) > limit:
            raise TransportError(
                "kb target %r: response exceeds the configured %d byte "
                "bound" % (self._target.id, limit))
        return status, payload

    def _get_content(self, locator: str) -> bytes | None:
        """GET one version's content; ``None`` means 404."""
        status, payload = self._request("GET", locator)
        if status == 404:
            return None
        if status in (401, 403):
            raise TargetError(
                "kb target %r: authorization refused (HTTP %d)" %
                (self._target.id, status))
        if status != 200:
            raise TransportError(
                "kb target %r: unexpected HTTP %d reading a version" %
                (self._target.id, status))
        try:
            obj = json.loads(payload.decode("utf-8"))
            content = obj["content"] if isinstance(obj, dict) else None
        except (ValueError, UnicodeDecodeError):
            content = None
        if not isinstance(content, str):
            raise TransportError(
                "kb target %r: malformed response body (expected a JSON "
                "object with a string 'content')" % self._target.id)
        data = content.encode("utf-8")
        if len(data) > MAX_CONTENT_BYTES:
            raise TransportError(
                "kb target %r: version content exceeds the %d byte limit" %
                (self._target.id, MAX_CONTENT_BYTES))
        return data

    # -- public operations

    def create(self, document: Document) -> "CreateResult":
        """POST ``{"mode": "create", "content": ...}`` then verify by GET.

        Success (2xx) is trusted only after the readback digest matches.  A
        409, or a create lost to a network failure, recovers via
        GET-and-compare: identical content is success (``existed_match``),
        different content is :class:`ConflictError`, no identical version
        after a lost create stays :class:`TransportError` (outcome
        unknown).  The client never PUTs, renames, or overwrites.
        """
        if document.target_id != self._target.id:
            raise ValueError(
                "document targets %r, client is %r" %
                (document.target_id, self._target.id))
        body = json.dumps({
            "mode": "create",
            "content": document.content.decode("utf-8"),
        }).encode("utf-8")
        try:
            status, _payload = self._request("POST", document.locator, body)
        except TransportError:
            existing = self._get_content(document.locator)
            matched = self._recovered(document, existing)
            if matched is not None:
                return matched
            raise TransportError(
                "kb target %r: create outcome unknown (network failure; "
                "recovery GET found no identical version at %r)" %
                (self._target.id, document.locator)) from None
        if status in (200, 201):
            existing = self._get_content(document.locator)
            if existing is None or content_digest(existing) != document.digest:
                raise IntegrityError(
                    "kb target %r: readback digest mismatch for %r" %
                    (self._target.id, document.locator))
            return CreateResult(locator=document.locator,
                                digest=document.digest,
                                existed_match=False)
        if status == 409:
            existing = self._get_content(document.locator)
            matched = self._recovered(document, existing)
            if matched is not None:
                return matched
            raise ConflictError(
                "kb target %r: endpoint reports a conflict and no identical "
                "version exists at %r" % (self._target.id, document.locator))
        if status in (401, 403):
            raise TargetError(
                "kb target %r: authorization refused (HTTP %d)" %
                (self._target.id, status))
        raise TransportError(
            "kb target %r: create answered HTTP %d" %
            (self._target.id, status))

    def _recovered(self, document: Document,
                   existing: bytes | None) -> "CreateResult | None":
        """GET-and-compare recovery: identical -> existed_match result;
        different -> ConflictError; absent -> None (outcome unknown)."""
        if existing is None:
            return None
        if content_digest(existing) != document.digest:
            raise ConflictError(
                "kb target %r: immutable version exists with different "
                "content at %r (never overwritten)" %
                (self._target.id, document.locator))
        return CreateResult(locator=document.locator,
                            digest=document.digest,
                            existed_match=True)

    def read(self, locator: str) -> bytes:
        """Read an immutable version's canonical bytes."""
        parse_locator(locator)
        content = self._get_content(locator)
        if content is None:
            raise TargetError(
                "kb target %r: no version at %r" %
                (self._target.id, locator))
        return content


# ---------------------------------------------------------------------------
# factory + result


def open_target(target: "Target",
                env: Mapping[str, str] | None = None) -> "Target":
    """Build the client for a configured target (type-dispatched)."""
    from knowledge.config import KBHTTPTarget, MarkdownTarget

    if isinstance(target, MarkdownTarget):
        return MarkdownStore(target)
    if isinstance(target, KBHTTPTarget):
        return KBClient(target, env=env)
    raise TypeError("unknown knowledge target kind: %r" % (target,))


@dataclass(frozen=True)
class CreateResult:
    """Outcome of an immutable create.

    ``existed_match`` is True when the exact version already existed
    (byte-identical) — success, never an overwrite."""

    locator: str
    digest: str
    existed_match: bool
