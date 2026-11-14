"""Reader for the optional ``knowledge`` object of the store config.json.

The knowledge companion (PR1) is OFF BY DEFAULT: no ``knowledge`` key in
``<store_dir>/config.json`` means ``enabled=False`` with zero targets and
the rest of the package is inert.  When the operator opts in, the same file
that already gates ``auto_retain`` (issue #177) carries one strict object:

    {
      "knowledge": {
        "enabled": true,
        "targets": {
          "notes":  {"kind": "markdown", "root": "D:/vault"},
          "fleet":  {"kind": "kb-http",
                     "endpoint": "https://hub.lan:8443/api/notes",
                     "credential_env": "ZMEM_KB_FLEET_TOKEN",
                     "timeout_s": 10,
                     "max_response_bytes": 1048576}
        }
      }
    }

Contract (approved plan, 2026-11-13):
- Read through the EXISTING ``storelib.config.load_config`` (fail-open {},
  BOM-tolerant, one stderr warning).  This module adds only the strict
  validation of the ``knowledge`` sub-object; it never migrates, never
  opens SQLite, never writes, never installs models.
- ``enabled`` must be a JSON boolean (strict) — anything else is a config
  error, not silently-off.
- Target ids are slugs: 1..24 chars, lowercase ``[a-z0-9-]``, no leading or
  trailing hyphen, unique (dict keys enforce that).
- Exactly two kinds.  ``markdown`` targets name ONE absolute root that the
  operator created (the package never creates the vault root).  ``kb-http``
  targets name ONE exact endpoint URL; https is the default and ``http://``
  is refused unless ``allow_http`` is explicitly ``true`` (a deliberate,
  disclosed LAN carve-out).  Credentials are EXPLICIT: exactly one of
  ``credential_env`` / ``token_file`` per target.  Token VALUES are never
  read here — only their named locations are recorded.
- Endpoint URLs carry no query, fragment, or userinfo; redirects are never
  followed at request time (documents.py), so the configured string is the
  only URL the client will ever touch.
- Unknown keys are errors, not silently ignored: a typo'd field must fail
  loudly at load, not half-enable a target.
- Error messages name the target id and the offending key, never a secret
  value, a token, or note content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Union

from storelib.config import load_config

__all__ = [
    "KBHTTPTarget",
    "KnowledgeConfig",
    "KnowledgeConfigError",
    "MarkdownTarget",
    "TARGET_ID_RE",
    "validate_target_id",
    "load_knowledge_config",
]


class KnowledgeConfigError(ValueError):
    """An invalid ``knowledge`` config object.  Safe to show an operator:
    the message names ids/keys, never credential values or note content."""


# 1..24 chars, lowercase, inner hyphens only.
TARGET_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bounds for kb-http knobs.  Defaults favour small, bounded requests.
DEFAULT_TIMEOUT_S = 10.0
MIN_TIMEOUT_S = 0.1
MAX_TIMEOUT_S = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
MIN_MAX_RESPONSE_BYTES = 256
MAX_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_KINDS = ("markdown", "kb-http")


def validate_target_id(slug: str) -> str:
    """Validate and return a target id slug (1..24, lowercase)."""
    if not isinstance(slug, str) or not TARGET_ID_RE.fullmatch(slug):
        raise KnowledgeConfigError(
            "knowledge target id %r is not a 1..24 char lowercase slug "
            "[a-z0-9-] with no leading/trailing hyphen" % (slug,))
    return slug


@dataclass(frozen=True)
class MarkdownTarget:
    """A local Markdown vault.  ``root`` must already exist and be a real
    directory (no symlink/junction) — enforced again at write time."""

    id: str
    kind: str = "markdown"
    root: Path = Path()

    def __post_init__(self) -> None:
        validate_target_id(self.id)


@dataclass(frozen=True)
class KBHTTPTarget:
    """A configured fleet knowledge-base HTTP endpoint.

    ``endpoint`` is the EXACT allowlisted URL (no query/fragment/userinfo);
    the client appends only the encoded note locator.  Exactly one of
    ``credential_env`` / ``token_file`` names where the bearer token lives —
    the value is read only at request time, never here, never logged.
    """

    id: str
    kind: str = "kb-http"
    endpoint: str = ""
    allow_http: bool = False
    credential_env: str | None = None
    token_file: Path | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        validate_target_id(self.id)


Target = Union[MarkdownTarget, KBHTTPTarget]


@dataclass(frozen=True)
class KnowledgeConfig:
    """The resolved, validated knowledge configuration.

    ``targets`` is a read-only mapping in config order.  ``env`` is the
    environment mapping the caller supplied for later credential lookups
    (``None`` means "consult ``os.environ`` at request time"); config
    LOADING never reads credential values.  ``source_path`` is the config
    file that was actually read, when known.
    """

    enabled: bool
    targets: Mapping[str, Target]
    env: Mapping[str, str] | None = None
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# validation helpers


def _req_bool(obj: dict, key: str, where: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise KnowledgeConfigError(
            "%s: %r must be a boolean true/false" % (where, key))
    return value


def _req_str(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeConfigError(
            "%s: %r must be a non-empty string" % (where, key))
    return value


def _no_extra_keys(obj: dict, allowed: frozenset, where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise KnowledgeConfigError(
            "%s: unknown key(s) %s (allowed: %s)"
            % (where, ", ".join(map(repr, extra)), ", ".join(sorted(allowed))))


def _abs_path(value: str, where: str, key: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise KnowledgeConfigError(
            "%s: %r must be an absolute path" % (where, key))
    if ".." in path.parts:
        raise KnowledgeConfigError(
            "%s: %r must not contain '..' components" % (where, key))
    return path


def _markdown_target(tid: str, obj: object) -> MarkdownTarget:
    where = "knowledge target %r (markdown)" % tid
    if not isinstance(obj, dict):
        raise KnowledgeConfigError("%s: must be an object" % where)
    _no_extra_keys(obj, frozenset({"kind", "root"}), where)
    root = _abs_path(_req_str(obj, "root", where), where, "root")
    return MarkdownTarget(id=tid, root=root)


def _kbhttp_target(tid: str, obj: object) -> KBHTTPTarget:
    import urllib.parse

    where = "knowledge target %r (kb-http)" % tid
    if not isinstance(obj, dict):
        raise KnowledgeConfigError("%s: must be an object" % where)
    _no_extra_keys(
        obj,
        frozenset({"kind", "endpoint", "allow_http", "credential_env",
                   "token_file", "timeout_s", "max_response_bytes"}),
        where)

    endpoint = _req_str(obj, "endpoint", where)
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        raise KnowledgeConfigError(
            "%s: endpoint scheme must be http or https" % where)
    if parts.scheme == "http" and obj.get("allow_http") is not True:
        raise KnowledgeConfigError(
            "%s: http endpoint requires explicit \"allow_http\": true "
            "(https is the default; plain http is a disclosed LAN-only "
            "carve-out)" % where)
    if not parts.hostname:
        raise KnowledgeConfigError("%s: endpoint host is empty" % where)
    if parts.username or parts.password:
        raise KnowledgeConfigError(
            "%s: endpoint must not embed userinfo" % where)
    if parts.query or parts.fragment:
        raise KnowledgeConfigError(
            "%s: endpoint must be an exact path, no query or fragment" % where)
    if not parts.path.startswith("/"):
        raise KnowledgeConfigError(
            "%s: endpoint path must start with '/'" % where)
    if parts.path.endswith("/"):
        raise KnowledgeConfigError(
            "%s: endpoint path must not end with '/' (the client appends "
            "'/<encoded locator>')" % where)
    # An explicit port must be a valid one: ``urlsplit``'s ``.port``
    # property itself raises ValueError for a non-numeric or out-of-range
    # port (e.g. ':99999'), so the access is guarded and the range is
    # checked explicitly — an invalid port is refused AT LOAD, never
    # carried to request time.
    try:
        port = parts.port
    except ValueError as exc:
        raise KnowledgeConfigError(
            "%s: endpoint port must be an integer in 1..65535" %
            where) from exc
    if port is not None and not 1 <= port <= 65535:
        raise KnowledgeConfigError(
            "%s: endpoint port must be in 1..65535" % where)

    allow_http = obj.get("allow_http", False)
    if not isinstance(allow_http, bool):
        raise KnowledgeConfigError("%s: 'allow_http' must be a boolean" % where)

    credential_env = obj.get("credential_env")
    token_file = obj.get("token_file")
    if credential_env is not None:
        if not isinstance(credential_env, str) or not _ENV_NAME_RE.fullmatch(
                credential_env):
            raise KnowledgeConfigError(
                "%s: 'credential_env' must be an environment variable name" %
                where)
    if token_file is not None:
        if not isinstance(token_file, str) or not token_file.strip():
            raise KnowledgeConfigError(
                "%s: 'token_file' must be a non-empty path string" % where)
        token_file = _abs_path(token_file, where, "token_file")
    if (credential_env is None) == (token_file is None):
        raise KnowledgeConfigError(
            "%s: exactly one of 'credential_env' or 'token_file' is "
            "required (no implicit credentials, no ambiguity)" % where)

    timeout = obj.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise KnowledgeConfigError(
            "%s: 'timeout_s' must be a number" % where)
    timeout = float(timeout)
    if not (MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S):
        raise KnowledgeConfigError(
            "%s: 'timeout_s' must be between %s and %s seconds"
            % (where, MIN_TIMEOUT_S, MAX_TIMEOUT_S))

    max_bytes = obj.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise KnowledgeConfigError(
            "%s: 'max_response_bytes' must be an integer" % where)
    if not (MIN_MAX_RESPONSE_BYTES <= max_bytes <= MAX_MAX_RESPONSE_BYTES):
        raise KnowledgeConfigError(
            "%s: 'max_response_bytes' must be between %d and %d"
            % (where, MIN_MAX_RESPONSE_BYTES, MAX_MAX_RESPONSE_BYTES))

    return KBHTTPTarget(
        id=tid,
        endpoint=endpoint,
        allow_http=allow_http,
        credential_env=credential_env,
        token_file=token_file,
        timeout_s=timeout,
        max_response_bytes=max_bytes,
    )


def _validate_knowledge_object(obj: object) -> tuple[bool, dict]:
    if not isinstance(obj, dict):
        raise KnowledgeConfigError(
            "knowledge: must be an object with 'enabled' and 'targets'")
    _no_extra_keys(obj, frozenset({"enabled", "targets"}), "knowledge")
    enabled = _req_bool(obj, "enabled", "knowledge")
    raw_targets = obj.get("targets")
    if raw_targets is None:
        raw_targets = {}
    if not isinstance(raw_targets, dict):
        raise KnowledgeConfigError("knowledge: 'targets' must be an object")
    return enabled, raw_targets


def _build_target(tid: object, spec: object) -> tuple[str, Target]:
    if not isinstance(tid, str):
        raise KnowledgeConfigError(
            "knowledge target id %r is not a string" % (tid,))
    validate_target_id(tid)
    if not isinstance(spec, dict):
        raise KnowledgeConfigError(
            "knowledge target %r: must be an object" % tid)
    kind = spec.get("kind")
    if kind not in _KINDS:
        raise KnowledgeConfigError(
            "knowledge target %r: 'kind' must be one of %s"
            % (tid, " or ".join(map(repr, _KINDS))))
    if kind == "markdown":
        return tid, _markdown_target(tid, spec)
    return tid, _kbhttp_target(tid, spec)


def load_knowledge_config(
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> KnowledgeConfig:
    """Load and strictly validate the ``knowledge`` config object.

    ``path`` defaults to the store-side config resolved by
    ``storelib.config`` (``<store_dir>/config.json``); ``env`` is captured
    on the result for later credential lookups by the HTTP client and is
    NOT consulted here.  Missing file / missing ``knowledge`` key is the
    default-off state (enabled=False, no targets, no error).  A present
    but invalid ``knowledge`` object raises :class:`KnowledgeConfigError`.
    """
    target_path = Path(path) if path is not None else None
    raw = load_config(target_path)
    source = target_path if target_path is not None else None
    knowledge = raw.get("knowledge")
    if knowledge is None:
        return KnowledgeConfig(enabled=False, targets=MappingProxyType({}),
                               env=env, source_path=source)
    enabled, raw_targets = _validate_knowledge_object(knowledge)
    targets: dict[str, Target] = {}
    for tid, spec in raw_targets.items():
        _, built = _build_target(tid, spec)
        targets[built.id] = built
    return KnowledgeConfig(enabled=enabled,
                           targets=MappingProxyType(targets),
                           env=env,
                           source_path=source)
