"""SDK-native Bearer-token auth for the zmem MCP server.

Uses the ``mcp`` SDK's built-in ``TokenVerifier`` Protocol rather than custom
Starlette middleware. The SDK wires ``BearerAuthBackend`` +
``RequireAuthMiddleware`` automatically when ``FastMCP(auth=..., token_verifier=...)``
is constructed (verified in mcp/server/fastmcp/server.py:32-34, 217-224), and
this path stays correct as the SDK adds new protocol probe routes
(``.well-known/oauth-protected-resource`` etc.) that custom middleware would
silently miss.

The token is a single shared secret read from ``ZMEM_MCP_TOKEN`` (env or a file
path in ``ZMEM_MCP_TOKEN_FILE``). Comparison is constant-time
(``hmac.compare_digest``). This is intentionally simple static-token auth —
not OAuth — which matches zmem's local-first, single-operator model.

v13 (issue #65, 10.2): a token may carry an OPTIONAL namespace allow-list.
- ``ZMEM_MCP_TOKEN`` (env) is always an UNSCOPED operator token — full
  access, exactly the pre-v13 behavior.
- ``ZMEM_MCP_TOKEN_FILE`` accepts two formats:
  * a bare text file whose first non-whitespace character is NOT ``{`` —
    the whole (stripped) content is the token, UNSCOPED (pre-v13 behavior);
  * a JSON object file starting with ``{``: ``{"token": "...", "namespaces":
    ["project:a", "user:global"]}`` — a SCOPED token. ``namespaces`` absent
    or null means unscoped; when present it MUST be a non-empty list of
    valid namespace shapes (``project:*``, ``user:*``, ``fleet:*``, ``host:*``,
    ``agent:*``, ``domain:*``, or canonical ``user:global``; near-miss globals
    like ``global`` are refused). A file
    that STARTS with ``{`` but does not parse as such a JSON object is a
    hard startup error (exit 2) — never a silent fallback to bare-token
    mode, which would silently un-scope a scoped deployment.
Requests outside the allow-list fail closed with the stable error token
``namespace_not_allowed`` (see NamespaceDenied). No token rotation, CORS,
or rate limits — out of scope by design (issue #65).

vT (issue #281, Workstream T): a FLEET mode — ``ZMEM_MCP_TOKENS_FILE`` names
a JSON map of per-machine principals (token → {principal, host, harness,
namespaces?, capabilities?, revoked?}). The map wins over the legacy vars;
verification is per-token (sha256-digest keyed, so no entry-identity timing
leak) and returns an ``AccessToken`` whose ``client_id`` is the principal id,
letting the server resolve the CALLER's principal per request instead of a
startup-resolved config. Revocation is a map edit picked up on file mtime
change (checked at most once per second) or SIGHUP where the platform has it;
a reload that fails to parse keeps the previous map (a corrupt rewrite defers
pending revocations until a valid rewrite lands — logged, never silent).
Malformed maps are a hard startup error (exit 2), as ever.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import importlib.util
import os
import re
import stat as _stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier

# Stable error token for scope denials (issue #65, 10.2). A stable machine-
# readable constant, not a human message, so clients can branch on it.
NAMESPACE_NOT_ALLOWED = "namespace_not_allowed"
_MAX_TOKEN_NAMESPACES = 128
_MAX_TOKEN_NAMESPACE_UTF8_BYTES = 256
_MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES = 8192
# Fits below common 8 KiB HTTP header limits after the Authorization scheme,
# while leaving room for ordinary proxy/server header overhead.  It also makes
# the presented-token verification path bounded before any comparison
# (hmac.compare_digest in legacy single-token mode; a sha256 digest
# probe in map mode).
_MAX_BEARER_TOKEN_UTF8_BYTES = 4096

# Namespace grammar is loaded directly from the same selected checkout as the
# MCP store subprocess.  auth.py remains dependency-free and fails closed for
# noncanonical scopes if that checkout is incomplete or unreadable.
_SCHEMA_META_REL = Path("skills") / "memory" / "scripts" / "schema_meta.py"


def _load_namespace_validator():
    home = os.environ.get("ZMEM_HOME", "").strip()
    if home:
        path = Path(home).expanduser() / _SCHEMA_META_REL
    else:
        path = Path(__file__).resolve().parents[2] / _SCHEMA_META_REL
    try:
        spec = importlib.util.spec_from_file_location("zmem_schema_meta_auth", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        validator = getattr(mod, "is_valid_namespace", None)
        return validator if callable(validator) else None
    except Exception:  # malformed/unreadable selected validator fails closed
        return None


_NAMESPACE_VALIDATOR = _load_namespace_validator()


class NamespaceDenied(Exception):
    """A scoped token was used for a namespace outside its allow-list."""


@dataclass
class TokenConfig:
    """The single configured token + its optional namespace allow-list.

    ``namespaces`` is None for an UNSCOPED operator token (full access — the
    only pre-v13 mode) or a frozenset of namespace strings for a scoped one.
    """

    token: str
    namespaces: Optional[frozenset] = None
    source: str = "ZMEM_MCP_TOKEN"

    @property
    def scoped(self) -> bool:
        return self.namespaces is not None

    def check_namespace(self, namespace: Optional[str]) -> None:
        """Fail closed unless ``namespace`` is inside the allow-list.

        ``namespace=None`` means "no explicit namespace" — for a scoped token
        that is OUTSIDE the list (a read without --namespace spans every
        namespace; a write defaults to user:global): neither can be proven
        in-scope, so both are denied. Unscoped tokens allow everything.
        Raises NamespaceDenied; never returns a reason containing the token.
        """
        if self.namespaces is None:
            return
        ns = (namespace or "").strip()
        if ns in self.namespaces:
            return
        raise NamespaceDenied(
            f"{NAMESPACE_NOT_ALLOWED}: this token is scoped to "
            f"{len(self.namespaces)} namespace(s); pass an allowed "
            "namespace explicitly"
        )


def _valid_scope_namespace(ns: object) -> bool:
    if not isinstance(ns, str):
        return False
    v = ns.strip()
    if not v or v == "*":
        return False  # a scope of '*' is the unscoped case — not expressible
    if _NAMESPACE_VALIDATOR is None:
        # user:global is the one canonical scope understood without loading
        # the selected checkout's grammar; all other scopes fail closed.
        return v == "user:global"
    return bool(_NAMESPACE_VALIDATOR(v))


def _parse_token_file(raw: str, source: str) -> TokenConfig:
    """Parse ZMEM_MCP_TOKEN_FILE content per the pinned sniff rule (10.2)."""
    if not raw.lstrip().startswith("{"):
        tok = raw.strip()
        if not tok:
            _fail_config(f"{source} is empty")
        _validate_config_token(tok, source)
        return TokenConfig(token=tok, namespaces=None, source=source)
    try:
        obj = _json.loads(raw)
    except ValueError as exc:
        _fail_config(
            f"{source} starts with '{{' but is not valid JSON ({exc}); fix the "
            "file or remove the leading '{' if it is meant to be a bare token"
        )
    if not isinstance(obj, dict):
        _fail_config(f"{source} must be a JSON object with a 'token' field")
    tok = obj.get("token")
    if not isinstance(tok, str) or not tok.strip():
        _fail_config(f"{source} JSON object must carry a non-empty string 'token'")
    tok = tok.strip()
    _validate_config_token(tok, source)
    scopes = obj.get("namespaces")
    if scopes is None:
        return TokenConfig(token=tok, namespaces=None, source=source)
    if not isinstance(scopes, list) or not scopes:
        _fail_config(
            f"{source} 'namespaces' must be a NON-EMPTY list of namespace "
            "strings (omit the key entirely for an unscoped operator token)"
        )
    if len(scopes) > _MAX_TOKEN_NAMESPACES:
        _fail_config(
            f"{source} 'namespaces' exceeds the maximum of "
            f"{_MAX_TOKEN_NAMESPACES} entries"
        )
    cleaned: list[str] = []
    total_namespace_bytes = 0
    for ns in scopes:
        # Character length cheaply rejects pathological input before encoding;
        # UTF-8 byte length is the actual subprocess/SQLite safety contract.
        if not isinstance(ns, str) or len(ns) > _MAX_TOKEN_NAMESPACE_UTF8_BYTES:
            _fail_config(
                f"{source} 'namespaces' entries must be strings no longer than "
                f"{_MAX_TOKEN_NAMESPACE_UTF8_BYTES} UTF-8 bytes"
            )
        try:
            ns_bytes = len(ns.encode("utf-8"))
        except UnicodeEncodeError:
            _fail_config(f"{source} namespace entries must be valid UTF-8 text")
        if ns_bytes > _MAX_TOKEN_NAMESPACE_UTF8_BYTES:
            _fail_config(
                f"{source} 'namespaces' entries must be strings no longer than "
                f"{_MAX_TOKEN_NAMESPACE_UTF8_BYTES} UTF-8 bytes"
            )
        if (_NAMESPACE_VALIDATOR is None
                and isinstance(ns, str)
                and ns.strip() != "user:global"):
            _fail_config(
                f"{source} cannot load the shared namespace validator from the "
                "selected checkout; scoped namespace validation is unavailable"
            )
        if not _valid_scope_namespace(ns):
            _fail_config(
                f"{source} 'namespaces' entry {ns!r} is not a valid namespace "
                "shape (expected project:<name>, user:<name>, fleet:<name>, "
                "host:<name>, agent:<name>, domain:<name>, or user:global)"
            )
        normalized_ns = ns.strip()
        try:
            normalized_ns_bytes = normalized_ns.encode("utf-8")
        except UnicodeEncodeError:
            _fail_config(f"{source} namespace entries must be valid UTF-8 text")
        total_namespace_bytes += len(normalized_ns_bytes)
        if total_namespace_bytes > _MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES:
            _fail_config(
                f"{source} 'namespaces' exceeds the maximum aggregate of "
                f"{_MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES} UTF-8 bytes"
            )
        cleaned.append(normalized_ns)
    return TokenConfig(
        token=tok, namespaces=frozenset(cleaned), source=source
    )


def _fail_config(message: str) -> None:
    sys.stderr.write(f"zmem-mcp: {message}\n")
    sys.exit(2)


def _validate_config_token(token: str, source: str) -> None:
    """Reject oversized configured bearer material without echoing a secret."""
    if len(token) > _MAX_BEARER_TOKEN_UTF8_BYTES:
        _fail_config(
            f"{source} token exceeds the maximum of "
            f"{_MAX_BEARER_TOKEN_UTF8_BYTES} UTF-8 bytes"
        )
    try:
        token_bytes = token.encode("utf-8")
    except UnicodeEncodeError:
        _fail_config(f"{source} token must be valid UTF-8 text")
    if len(token_bytes) > _MAX_BEARER_TOKEN_UTF8_BYTES:
        _fail_config(
            f"{source} token exceeds the maximum of "
            f"{_MAX_BEARER_TOKEN_UTF8_BYTES} UTF-8 bytes"
        )


def load_token_config() -> TokenConfig:
    """Load the token (and optional namespace scope) from env or token file.

    Env ``ZMEM_MCP_TOKEN`` wins (always bare/unscoped); otherwise
    ``ZMEM_MCP_TOKEN_FILE`` is parsed per the sniff rule in the module
    docstring. Aborts startup (exit 2) on missing/empty/malformed config —
    an unusable auth config must be loud, not silent.
    """
    tok = os.environ.get("ZMEM_MCP_TOKEN", "").strip()
    if tok:
        _validate_config_token(tok, "ZMEM_MCP_TOKEN")
        return TokenConfig(token=tok, namespaces=None, source="ZMEM_MCP_TOKEN")
    tok_file = os.environ.get("ZMEM_MCP_TOKEN_FILE", "").strip()
    if tok_file:
        p = Path(tok_file).expanduser()
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            _fail_config(f"ZMEM_MCP_TOKEN_FILE ({tok_file}) unreadable: {exc}")
        return _parse_token_file(raw, f"ZMEM_MCP_TOKEN_FILE ({tok_file})")
    sys.stderr.write(
        "zmem-mcp: no token configured. Set ZMEM_MCP_TOKEN (or "
        "ZMEM_MCP_TOKEN_FILE) before starting the server — auth is "
        "mandatory for a network-exposed store.\n"
    )
    sys.exit(2)


def load_expected_token() -> str:
    """Load the expected Bearer token from env or token file.

    Backward-compatible wrapper over :func:`load_token_config` — returns just
    the secret. Aborts startup (exit 2) if neither is set OR the resolved
    value is empty — an empty token would let the server boot but reject
    every request (every Bearer comparison against ``""`` is False), leaving
    the operator with a silently-unusable server. Auth is mandatory for a
    network-exposed store.
    """
    return load_token_config().token


class StaticTokenVerifier(TokenVerifier):
    """Constant-time Bearer-token check against a single expected secret.

    Implements the SDK's ``TokenVerifier`` Protocol (one method,
    ``verify_token``). Returns an ``AccessToken`` on match, ``None`` on
    mismatch — the SDK's middleware turns None into a 401. Namespace
    scoping is NOT enforced here: verification happens before the tool call
    exists, so scope checks live at the tool layer closing over the one
    configured TokenConfig (mcp_server._guard_namespace).
    """

    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token

    async def verify_token(self, token: str) -> Optional[AccessToken]:  # noqa: D401
        if not isinstance(token, str) or not token:
            return None
        # Reject oversized Authorization credentials before comparing them. Do
        # not log the value or its length: both can be sensitive request data.
        if len(token) > _MAX_BEARER_TOKEN_UTF8_BYTES:
            return None
        try:
            token_bytes = token.encode("utf-8")
            expected_bytes = self._expected.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(token_bytes) > _MAX_BEARER_TOKEN_UTF8_BYTES:
            return None
        # Constant-time byte comparison supports valid non-ASCII UTF-8 tokens.
        if hmac.compare_digest(token_bytes, expected_bytes):
            return AccessToken(
                token=token,
                client_id="zmem-operator",
                scopes=["read", "write"],
                expires_at=None,
            )
        return None


# -- Workstream T: per-machine principals (issue #281) ------------------------

#: Capabilities an entry may declare; absent = the legacy behaviour of that
#: token kind (parsed and minted into AccessToken.scopes; namespace-allow-list
#: selection shipped in 0.105.0 (#283); capability enforcement itself remains
#: owned by the op queue (#288) — parse-only forward-compat metadata here,
#: disclosed in the issue #281 trace).
_ALLOWED_CAPABILITIES = frozenset(
    {"read", "passive", "write", "mutate", "admin"}
)
_MAX_PRINCIPALS = 256
_MAX_PRINCIPAL_ID_CHARS = 64
_MAX_PRINCIPAL_TAG_VALUE_CHARS = 128
_PRINCIPAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RELOAD_MIN_INTERVAL_S = 1.0
# Recognized fields of one token-map entry. Anything else is a hard
# exit-2 failure: a typo must never silently widen or keep access.
_PRINCIPAL_ENTRY_FIELDS = frozenset(
    {"principal", "host", "harness", "namespaces", "capabilities",
     "revoked"}
)


@dataclass(frozen=True)
class Principal:
    """One credential holder resolved from the token map (issue #281).

    Exposes the same guard surface as :class:`TokenConfig` (``scoped``,
    ``namespaces``, ``check_namespace``) so the server's guards consume
    either shape unchanged. ``host``/``harness`` are required at MAP LOAD
    (attribution integrity); the dataclass itself allows None so the legacy
    single-token mode can wrap its TokenConfig as one startup principal.
    """

    principal_id: str
    host: Optional[str] = None
    harness: Optional[str] = None
    namespaces: Optional[frozenset] = None
    capabilities: Optional[frozenset] = None
    revoked: bool = False
    source: str = "ZMEM_MCP_TOKENS_FILE"

    @property
    def scoped(self) -> bool:
        return self.namespaces is not None

    def check_namespace(self, namespace: Optional[str]) -> None:
        """Same contract and denial message as TokenConfig.check_namespace."""
        if self.namespaces is None:
            return
        ns = (namespace or "").strip()
        if ns in self.namespaces:
            return
        raise NamespaceDenied(
            f"{NAMESPACE_NOT_ALLOWED}: this token is scoped to "
            f"{len(self.namespaces)} namespace(s); pass an allowed "
            "namespace explicitly"
        )

    @classmethod
    def from_token_config(cls, cfg: "TokenConfig") -> "Principal":
        """Wrap the legacy single-token config as the one startup principal."""
        return cls(
            principal_id="zmem-operator",
            host=None,
            harness=None,
            namespaces=cfg.namespaces,
            capabilities=None,
            revoked=False,
            source=cfg.source,
        )


def deny_all_principal() -> Principal:
    """The fail-closed principal for map mode (issue #281).

    Scoped with an EMPTY allow-list: every explicit namespace, every
    defaulted namespace, and the implicit user:global union are all denied.
    Returned when the request auth context is missing, the client_id is
    unknown (e.g. the entry was removed by a map reload mid-session), or
    the resolved principal is revoked (``by_principal_id`` filters revoked
    entries, so a mid-session revocation denies at the authorization edge
    too) — a resolver bug must deny, never allow.
    """
    return Principal(
        principal_id="zmem-deny-all",
        host=None,
        harness=None,
        namespaces=frozenset(),
        capabilities=None,
        revoked=True,
        source="deny-all",
    )


def _fail_map(message: str) -> None:
    """Map-mode config failures use the same loud exit as token configs."""
    _fail_config(message)


def _validate_principal_tag_value(value: object, field: str, source: str) -> str:
    """Validate a host/harness value that will be rendered host:<v>/agent:<v>.

    Bounded length and a conservative id charset, self-contained (no shared
    validator dependency). When the shared namespace validator IS available,
    the rendered tag must also pass it — the tags must be reachable as
    host:*/agent:* namespace shapes.
    """
    if not isinstance(value, str):
        _fail_map(f"{source} entry field '{field}' must be a string")
    v = value.strip()
    if not v or len(v) > _MAX_PRINCIPAL_TAG_VALUE_CHARS:
        _fail_map(
            f"{source} entry field '{field}' must be 1.."
            f"{_MAX_PRINCIPAL_TAG_VALUE_CHARS} characters"
        )
    if not _PRINCIPAL_ID_RE.match(v):
        _fail_map(
            f"{source} entry field '{field}' must match "
            "[A-Za-z0-9][A-Za-z0-9._-]* (it is rendered as a host:/agent: tag)"
        )
    if _NAMESPACE_VALIDATOR is not None:
        rendered = f"{'host' if field == 'host' else 'agent'}:{v}"
        if not _NAMESPACE_VALIDATOR(rendered):
            _fail_map(
                f"{source} entry field '{field}' value {v!r} does not form a "
                "valid host:/agent: namespace shape"
            )
    return v


def _parse_principal_map(raw: str, source: str) -> dict:
    """Parse the token-map JSON into {sha256-hex: Principal}.

    Pure (no I/O) so load-time and reload-time validation cannot drift.
    Malformed input is a hard config failure (exit 2) — the same
    never-silently-unscope rule as the legacy token file. The returned dict
    also carries the principal-id index under the reserved ``__by_id__``
    key (never a sha256 hex digest, so it cannot collide with a token).
    """
    try:
        obj = _json.loads(raw)
    except RecursionError:
        # A nesting bomb raises RecursionError, which is NOT a ValueError —
        # without this the documented exit-2 config failure becomes a
        # traceback at startup.
        _fail_map(f"{source} is nested too deeply to parse")
    except ValueError as exc:
        _fail_map(f"{source} is not valid JSON ({exc})")
    if not isinstance(obj, dict):
        _fail_map(f"{source} must be a JSON object mapping tokens to principals")
    if not obj:
        _fail_map(
            f"{source} is empty — a map that authenticates nobody is a "
            "misconfiguration; unset the env var to use the legacy "
            "single-token mode"
        )
    if len(obj) > _MAX_PRINCIPALS:
        _fail_map(
            f"{source} exceeds the maximum of {_MAX_PRINCIPALS} principals"
        )
    entries: dict = {}
    by_id: dict = {}
    for token, entry in obj.items():
        if not isinstance(token, str) or not token.strip():
            _fail_map(f"{source} keys must be non-empty token strings")
        tok = token.strip()
        _validate_config_token(tok, source)
        if not isinstance(entry, dict):
            _fail_map(
                f"{source} entry for a token must be a JSON object with "
                "'principal', 'host' and 'harness' fields"
            )
        unknown = sorted(set(entry) - _PRINCIPAL_ENTRY_FIELDS)
        if unknown:
            # Fail loud on an unrecognized field: a typo'd `namespaces`
            # would silently widen the principal to unscoped, and a
            # typo'd `revoked` would leave a revoked credential live.
            # Never echo the outer key — it is the bearer token itself.
            _fail_map(
                f"{source} entry has unrecognized field(s) "
                f"{', '.join(repr(k) for k in unknown)}; allowed fields are "
                f"{', '.join(sorted(_PRINCIPAL_ENTRY_FIELDS))}"
            )
        pid = entry.get("principal")
        if not isinstance(pid, str) or not pid.strip():
            _fail_map(
                f"{source} entry field 'principal' must be a non-empty string"
            )
        pid = pid.strip()
        if len(pid) > _MAX_PRINCIPAL_ID_CHARS or not _PRINCIPAL_ID_RE.match(pid):
            _fail_map(
                f"{source} entry field 'principal' must match "
                f"[A-Za-z0-9][A-Za-z0-9._-]* and be at most "
                f"{_MAX_PRINCIPAL_ID_CHARS} characters"
            )
        host = _validate_principal_tag_value(entry.get("host"), "host", source)
        harness = _validate_principal_tag_value(
            entry.get("harness"), "harness", source)
        if pid in by_id:
            # Verification is per-token but request resolution is by
            # client_id: two entries sharing one id would let the second
            # token be guarded and stamped against the first's scope.
            _fail_map(
                f"{source} declares principal id {pid!r} more than once — "
                "principal ids must be unique across tokens"
            )
        scopes = entry.get("namespaces")
        namespaces = None
        if scopes is not None:
            if not isinstance(scopes, list) or not scopes:
                _fail_map(
                    f"{source} 'namespaces' must be a NON-EMPTY list of "
                    "namespace strings (omit the key for an unscoped "
                    "principal)"
                )
            if len(scopes) > _MAX_TOKEN_NAMESPACES:
                _fail_map(
                    f"{source} 'namespaces' exceeds the maximum of "
                    f"{_MAX_TOKEN_NAMESPACES} entries"
                )
            cleaned: list = []
            total_namespace_bytes = 0
            for ns in scopes:
                if not isinstance(ns, str) or len(ns) > _MAX_TOKEN_NAMESPACE_UTF8_BYTES:
                    _fail_map(
                        f"{source} 'namespaces' entries must be strings no "
                        f"longer than {_MAX_TOKEN_NAMESPACE_UTF8_BYTES} UTF-8 "
                        "bytes"
                    )
                try:
                    ns_bytes = ns.encode("utf-8")
                except UnicodeEncodeError:
                    _fail_map(
                        f"{source} namespace entries must be valid UTF-8 text")
                if len(ns_bytes) > _MAX_TOKEN_NAMESPACE_UTF8_BYTES:
                    _fail_map(
                        f"{source} 'namespaces' entries must be strings no "
                        f"longer than {_MAX_TOKEN_NAMESPACE_UTF8_BYTES} UTF-8 "
                        "bytes"
                    )
                if (_NAMESPACE_VALIDATOR is None
                        and isinstance(ns, str)
                        and ns.strip() != "user:global"):
                    _fail_map(
                        f"{source} cannot load the shared namespace validator "
                        "from the selected checkout; scoped namespace "
                        "validation is unavailable"
                    )
                if not _valid_scope_namespace(ns):
                    _fail_map(
                        f"{source} 'namespaces' entry {ns!r} is not a valid "
                        "namespace shape (expected project:<name>, "
                        "user:<name>, fleet:<name>, host:<name>, agent:<name>, "
                        "domain:<name>, or user:global)"
                    )
                normalized = ns.strip()
                total_namespace_bytes += len(normalized.encode("utf-8"))
                if total_namespace_bytes > _MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES:
                    _fail_map(
                        f"{source} 'namespaces' exceeds the maximum aggregate "
                        f"of {_MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES} UTF-8 "
                        "bytes"
                    )
                cleaned.append(normalized)
            namespaces = frozenset(cleaned)
        raw_caps = entry.get("capabilities")
        capabilities = None
        if raw_caps is not None:
            if not isinstance(raw_caps, list) or not raw_caps:
                _fail_map(
                    f"{source} 'capabilities' must be a NON-EMPTY list from "
                    "read, passive, write, mutate, admin (omit the key for "
                    "the legacy behaviour of this token kind)"
                )
            for cap in raw_caps:
                if not isinstance(cap, str) or cap not in _ALLOWED_CAPABILITIES:
                    _fail_map(
                        f"{source} 'capabilities' entries must be one of "
                        "read, passive, write, mutate, admin"
                    )
            capabilities = frozenset(raw_caps)
        revoked = entry.get("revoked", False)
        if not isinstance(revoked, bool):
            _fail_map(f"{source} entry field 'revoked' must be a boolean")
        principal = Principal(
            principal_id=pid,
            host=host,
            harness=harness,
            namespaces=namespaces,
            capabilities=capabilities,
            revoked=revoked,
            source=source,
        )
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        if digest in entries:
            # Two keys that normalize to the same secret (e.g. differing
            # only in surrounding whitespace) would silently shadow one
            # another — the later entry's scope and attribution win.
            _fail_map(
                f"{source} declares the same token more than once after "
                "normalization; entries must be unique"
            )
        entries[digest] = principal
        by_id[pid] = principal
    entries["__by_id__"] = by_id
    return entries


# -- principal-map file-permission contract (issue #406) -------------------
#
# The installers pin an owner-only shape at CREATION (install-hub.sh refuses
# any mode != 600; install-hub.ps1 refuses broad allow ACEs for Everyone /
# Users / Authenticated Users). These helpers extend that contract to LOAD
# time, RELOAD time, and doctor time: a map that drifted to group/world
# access after a correct install (umask-created replacement via mv/cp,
# chmod, ACL inheritance reset on a volume move) must refuse to serve its
# bearer entries instead of keeping answering on bearer possession — a
# locally readable map equals full principal impersonation for every local
# account on a multi-user hub.
#
# Kept in deliberate byte-parity with skills/memory/scripts/doctor.py's copy
# (the repo's parser-mirror precedent: doctor never imports server auth, and
# server auth never imports storelib); tests pin the parity.

# os.stat on NTFS synthesizes st_mode from only the readonly attribute, so
# S_IMODE is always one of these two shapes there regardless of the real
# DACL. A finer-grained mode on nt is therefore an authoritative stat (a
# POSIX-ish mount or a deliberate injection) and the mode leg applies to it;
# the synthesized shapes defer to the DACL leg.
_WINDOWS_SYNTHESIZED_MODES = frozenset({0o666, 0o444})

# Owner-relative Windows policy: an allow-ACE is exempt only when its SID is
# the file OWNER, SYSTEM (S-1-5-18), or Administrators (S-1-5-32-544) — what
# a locked-down install grants. Any other allow-ACE principal is exposure.
# NARROWED (implementation probe, issue #406): real Windows user-profile
# directories inherit allow-ACEs for machine-local SIDs (S-1-5-21-*) that do
# not resolve to any broad principal — per-app capability SIDs and sandbox
# groups on the SAME machine — so an owner-relative policy false-refuses
# every legitimate developer/CI map. The enforceable boundary that still
# matches the installers' refusal contract (install-hub.ps1) is the
# well-known BROAD principals; the verdict text says exactly that and never
# claims owner-only.
_NT_BROAD_SIDS = frozenset({"S-1-1-0", "S-1-5-32-545", "S-1-5-11"})

_ACCESS_ALLOWED_ACE_TYPE = 0x0


class MapPermissionRefused(SystemExit):
    """A principal map whose file permissions expose its bearer entries.

    A SystemExit subclass (exit 2, the map config-failure class) raised
    BEFORE PrincipalMap.force_reload's generic ``except SystemExit``
    keep-previous-map handler: a permission drift does not change file
    content, so keeping the previous map would keep serving exactly the
    credentials the file now exposes locally. The hub fails closed instead;
    the README documents the supervisor restart loop and the remedy.
    """

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


def _map_mode_problem(st) -> Optional[str]:
    """POSIX mode-bits verdict over a stat result (pure; parity-tested)."""
    imode = _stat.S_IMODE(st.st_mode)
    if not (imode & 0o077):
        return None
    if os.name == "nt" and imode in _WINDOWS_SYNTHESIZED_MODES:
        # Synthesized nt shape: the real answer is the DACL leg.
        return None
    return (f"mode 0{imode:o} grants group/world access "
            f"(installers enforce 0600/owner-only; chmod 0600 the file)")


def _nt_allow_ace_problem(owner_sid, ace_tuples):
    """Broad-allow-ACE policy over parsed (sid, ace_type) pairs.

    Pure function (parity-tested): an ACCESS_ALLOWED ace naming a
    well-known BROAD principal (Everyone / BUILTIN\\Users / Authenticated
    Users — install-hub.ps1's refusal list) is exposure. Denied/audit ACEs
    never grant access; machine-local SIDs are deliberately out of scope
    (see the narrowing note above — they do not make the file readable by
    OTHER local accounts' broad groups).
    """
    for sid, ace_type in ace_tuples:
        if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
            continue
        if sid in _NT_BROAD_SIDS:
            return (f"broad allow ACE grants {sid} (Everyone/Users/"
                    f"Authenticated Users) access — restrict the file to "
                    f"the hub service account")
    return None


def _nt_security_ace_tuples(path, fd):
    """(owner_sid, [(sid, ace_type), ...]) for the file's DACL, or raises.

    Uses the HANDLE of an already-open descriptor when given (the same
    object the caller will read — no check/use window), converting the CRT
    descriptor with msvcrt.get_osfhandle; falls back to a path query.
    """
    import ctypes
    import ctypes.wintypes

    advapi32 = ctypes.windll.advapi32
    OWNER_INFO = 0x00000001
    DACL_INFO = 0x00000004
    p_sd = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    dacl_present = ctypes.wintypes.BOOL()
    p_dacl = ctypes.c_void_p()
    defaulted = ctypes.wintypes.BOOL()

    if fd is not None:
        import msvcrt
        handle = msvcrt.get_osfhandle(fd)
        rc = advapi32.GetSecurityInfo(
            ctypes.wintypes.HANDLE(handle), 1,  # SE_FILE_OBJECT
            OWNER_INFO | DACL_INFO,
            ctypes.byref(owner), None, ctypes.byref(p_dacl), None,
            ctypes.byref(p_sd))
    else:
        rc = advapi32.GetNamedSecurityInfoW(
            ctypes.c_wchar_p(os.fspath(path)), 1,  # SE_FILE_OBJECT
            OWNER_INFO | DACL_INFO,
            ctypes.byref(owner), None, ctypes.byref(p_dacl), None,
            ctypes.byref(p_sd))
    if rc != 0 or not p_sd.value:
        raise OSError(f"GetSecurityInfo failed (winerror {rc})")

    def _sid_str(sid_addr):
        """SID string for a raw address, or raises."""
        buf = ctypes.c_wchar_p()
        try:
            if advapi32.ConvertSidToStringSidW(
                    ctypes.c_void_p(sid_addr), ctypes.byref(buf)) == 0:
                raise OSError("ConvertSidToStringSidW failed")
            value = buf.value
            if value is None:
                raise OSError("ConvertSidToStringSidW returned null")
            return value
        finally:
            addr = ctypes.cast(buf, ctypes.c_void_p).value
            if addr:
                ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(addr))

    try:
        owner_sid = _sid_str(owner.value) if owner.value else None
        ace_tuples = []
        if not advapi32.GetSecurityDescriptorDacl(
                p_sd, ctypes.byref(dacl_present), ctypes.byref(p_dacl),
                ctypes.byref(defaulted)):
            raise OSError("GetSecurityDescriptorDacl failed")
        if not dacl_present.value or not p_dacl.value:
            # NULL DACL = everyone-full; absent DACL = inherited (object's
            # protection is not owner-pinned). Either way: not owner-only.
            return owner_sid, None  # None marks NULL/absent DACL
        class _ACL_SIZE(ctypes.Structure):
            # ACL_SIZE_INFORMATION: three DWORDs; only AceCount is read.
            _fields_ = [("AceCount", ctypes.wintypes.DWORD),
                        ("AclBytesInUse", ctypes.wintypes.DWORD),
                        ("AclBytesFree", ctypes.wintypes.DWORD)]
        size_info = _ACL_SIZE()
        if not advapi32.GetAclInformation(
                p_dacl, ctypes.byref(size_info),
                ctypes.sizeof(_ACL_SIZE), 2):  # AclSizeInformation
            raise OSError("GetAclInformation failed")
        ace_ptr = ctypes.c_void_p()
        for i in range(size_info.AceCount):
            if advapi32.GetAce(p_dacl, i, ctypes.byref(ace_ptr)) == 0:
                raise OSError("GetAce failed")
            ace_addr = ace_ptr.value
            header = (ctypes.c_ubyte * 2).from_address(ace_addr)
            ace_type = header[0]
            # ACCESS_ALLOWED_ACE: 4-byte header + 4-byte mask, SID at +8.
            ace_tuples.append((_sid_str(ace_addr + 8), ace_type))
        return owner_sid, ace_tuples
    finally:
        if p_sd.value:
            ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(p_sd.value))


def _map_permission_problem(path, st, *, fd=None) -> Optional[str]:
    """Over-permissive principal-map verdict, or None when owner-only.

    POSIX mode leg first; on nt, synthesized stat shapes defer to the DACL
    leg (handle-based when fd is given). DACL inspection failures are
    REFUSED (fail-closed): skipping the only authoritative Windows check
    would serve the tokens precisely when their exposure is unknown.
    """
    problem = _map_mode_problem(st)
    if problem is not None:
        return problem
    if os.name != "nt":
        return None
    imode = _stat.S_IMODE(st.st_mode)
    if imode not in _WINDOWS_SYNTHESIZED_MODES:
        return None  # an authoritative finer-grained stat already decided
    try:
        owner_sid, ace_tuples = _nt_security_ace_tuples(path, fd)
    except (OSError, ImportError, AttributeError) as exc:
        return (f"could not inspect permissions ({type(exc).__name__}: "
                f"{exc}); refusing rather than serve an uninspectable map")
    if ace_tuples is None:
        return ("DACL is NULL/absent — grants everyone; restrict the file "
                "to the hub service account")
    return _nt_allow_ace_problem(owner_sid, ace_tuples)


def _open_map_checked(path):
    """Open the map and verify the OPEN object's permissions, atomically.

    open -> fstat -> permission verdict on the same handle that the caller
    reads from: the path-based stat verdict alone leaves a replace-after-
    check window. Returns (text, mtime_ns). Raises OSError/MapPermission-
    Refused via the caller on failure shapes.
    """
    with open(path, "r", encoding="utf-8") as fh:
        st = os.fstat(fh.fileno())
        problem = _map_permission_problem(path, st, fd=fh.fileno())
        if problem is not None:
            raise MapPermissionRefused(
                f"ZMEM_MCP_TOKENS_FILE ({path}) is over-permissive: "
                f"{problem}")
        return fh.read(), st.st_mtime_ns


class PrincipalMap:
    """The loaded token map with mtime-rate-limited reload (issue #281).

    ``entries`` maps sha256(token)-hex to Principal (plus the principal-id
    index under its reserved ``__by_id__`` key). Readers hold a reference to
    one parsed generation; reload atomically swaps in a new dict, so no
    reader ever observes a partial map.
    """

    _BY_ID = "__by_id__"

    def __init__(self, path, entries) -> None:
        self._path = path
        self._entries = entries
        try:
            self._mtime = os.stat(path).st_mtime_ns
        except OSError:
            self._mtime = None
        self._last_check = time.monotonic()

    @property
    def path(self):
        return self._path

    def _swap(self, entries, mtime) -> None:
        self._entries = entries
        self._mtime = mtime
        self._last_check = time.monotonic()

    def force_reload(self) -> bool:
        """Reload now (SIGHUP). Returns True when the map changed.

        A reload that cannot be read or parsed keeps the previous map and
        logs a fixed-class warning: an operator's corrupt rewrite must not
        brick auth for every live machine, but it DOES defer any pending
        revocation until a valid rewrite lands (disclosed in README).
        A PERMISSION refusal is different and fails closed (issue #406):
        a chmod does not change content, so keeping the previous map would
        keep serving exactly the credentials the file now exposes locally.
        """
        # Path-based pre-check (issue #406): the drift is detectable before
        # any read; the authoritative same-handle check runs inside
        # _open_map_checked below.
        try:
            _pre_st = os.stat(self._path)
        except OSError:
            _pre_st = None
        if _pre_st is not None:
            _problem = _map_permission_problem(self._path, _pre_st)
            if _problem is not None:
                sys.stderr.write(
                    "zmem-mcp: zmem_mcp_principal_map_permission_refused "
                    f"{self._path} {_problem}" + chr(10))
                raise MapPermissionRefused(
                    f"ZMEM_MCP_TOKENS_FILE ({self._path}) is "
                    f"over-permissive: {_problem}")
        try:
            raw, mtime = _open_map_checked(self._path)
            entries = _parse_principal_map(raw, self._path)
        except MapPermissionRefused as exc:
            sys.stderr.write(
                "zmem-mcp: zmem_mcp_principal_map_permission_refused "
                f"{self._path} {getattr(exc, 'message', exc)}" + chr(10))
            raise
        except SystemExit:
            # A RELOAD-time config failure (the exit-2 class _fail_config
            # raises, e.g. a corrupt rewrite) must NOT take the server
            # down or end every live session: keep the previous map and
            # log (README: a corrupt rewrite defers pending revocations
            # until a valid rewrite lands). STARTUP keeps the hard exit
            # via load_principal_map.
            sys.stderr.write(
                "zmem-mcp: zmem_mcp_principal_map_reload_failed "
                f"{self._path} (keeping previous map)" + chr(10)
            )
            self._last_check = time.monotonic()
            return False
        except Exception:
            sys.stderr.write(
                "zmem-mcp: zmem_mcp_principal_map_reload_failed "
                f"{self._path} (keeping previous map)\n"
            )
            self._last_check = time.monotonic()
            return False
        self._swap(entries, mtime)
        return True

    def maybe_reload(self) -> None:
        """Reload when the file mtime changed, at most once per second."""
        now = time.monotonic()
        if now - self._last_check < _RELOAD_MIN_INTERVAL_S:
            return
        self._last_check = now
        try:
            st = os.stat(self._path)
        except OSError as exc:
            # The map is unreadable (deleted, permissions, transient
            # share error): keep serving the previous generation, but say so.
            sys.stderr.write(
                "zmem-mcp: zmem_mcp_principal_map_stat_failed "
                f"{self._path} ({type(exc).__name__}) (keeping previous map)"
                + chr(10)
            )
            return
        # Any CHANGE must reload, not just a forward move: an mtime-preserving
        # restore (cp -p, rsync -t, a tar extraction, an NTP step backwards)
        # carries a revocation that would otherwise never be picked up, and a
        # coarse-granularity filesystem can land two edits in one tick.
        # st_mtime_ns keeps sub-second granularity where the FS provides it.
        mtime = st.st_mtime_ns
        # Issue #406: permission drift often does NOT bump mtime (chmod
        # changes ctime only), so the once-per-second stat also re-checks
        # the permission shape; a refusal escapes verify_token and ends the
        # process (fail-closed; README documents the supervisor behavior).
        _problem = _map_permission_problem(self._path, st)
        if _problem is not None:
            sys.stderr.write(
                "zmem-mcp: zmem_mcp_principal_map_permission_refused "
                f"{self._path} {_problem}" + chr(10))
            raise MapPermissionRefused(
                f"ZMEM_MCP_TOKENS_FILE ({self._path}) is over-permissive: "
                f"{_problem}")
        if self._mtime is not None and mtime == self._mtime:
            return
        self.force_reload()

    def lookup_token(self, token: str) -> Optional[Principal]:
        """Resolve a presented token to its principal (or None)."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        principal = self._entries.get(digest)
        if principal is None or principal.revoked:
            return None
        return principal

    def by_principal_id(self, pid: object) -> Optional[Principal]:
        """Resolve a client_id to its principal, or None if unknown OR revoked.

        The revoked check is the second half of revocation: lookup_token
        gates the VERIFY edge, this gates the AUTHORIZATION edge, so a
        principal revoked by a reload cannot keep its namespace allow-list
        on any path that resolves without re-verifying the token.
        """
        if not isinstance(pid, str):
            return None
        principal = self._entries.get(self._BY_ID, {}).get(pid)
        if principal is None or principal.revoked:
            return None
        return principal


def load_principal_map(path: Optional[str] = None) -> PrincipalMap:
    """Load the per-machine principal map (issue #281).

    ``ZMEM_MCP_TOKENS_FILE`` (or the explicit ``path``) must name a JSON
    object mapping tokens to principal entries. Malformed/missing/unreadable
    maps abort startup (exit 2) — never a silent fallback that would un-scope
    a fleet deployment.
    """
    tok_file = (path or os.environ.get("ZMEM_MCP_TOKENS_FILE", "")).strip()
    if not tok_file:
        _fail_map(
            "ZMEM_MCP_TOKENS_FILE is empty — set it to the principal map "
            "JSON path, or unset it to use the legacy single-token mode"
        )
    p = Path(tok_file).expanduser()
    try:
        st = os.stat(p)
    except OSError as exc:
        _fail_map(f"ZMEM_MCP_TOKENS_FILE ({tok_file}) unreadable: {exc}")
    # Issue #406: the map carries bearer entries for up to 256 machines;
    # a group/world-readable file must refuse startup, never serve.
    problem = _map_permission_problem(p, st)
    if problem is not None:
        _fail_map(f"ZMEM_MCP_TOKENS_FILE ({tok_file}) is over-permissive: "
                  f"{problem}")
    try:
        raw, _mtime = _open_map_checked(p)
    except MapPermissionRefused as exc:
        # exc.message already names the file and shape; no double prefix.
        _fail_map(getattr(exc, "message", str(exc)))
    except OSError as exc:
        _fail_map(f"ZMEM_MCP_TOKENS_FILE ({tok_file}) unreadable: {exc}")
    entries = _parse_principal_map(raw, f"ZMEM_MCP_TOKENS_FILE ({tok_file})")
    return PrincipalMap(p, entries)


class MapTokenVerifier(TokenVerifier):
    """Verify bearer tokens against the principal map (issue #281).

    Returns an ``AccessToken`` whose ``client_id`` is the matched
    principal's id (the SDK's middleware carries it into the request
    context, where the server resolves the caller's principal), or ``None``
    for unknown/revoked tokens (the SDK turns that into a 401). Reloads the
    map on mtime change at most once per second, so revocation takes effect
    without a restart.
    """

    def __init__(self, principal_map: PrincipalMap) -> None:
        self._map = principal_map

    async def verify_token(self, token: str) -> Optional[AccessToken]:  # noqa: D401
        if not isinstance(token, str) or not token:
            return None
        if len(token) > _MAX_BEARER_TOKEN_UTF8_BYTES:
            return None
        try:
            token_bytes = token.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(token_bytes) > _MAX_BEARER_TOKEN_UTF8_BYTES:
            return None
        self._map.maybe_reload()
        principal = self._map.lookup_token(token)
        if principal is None:
            return None
        scopes = (
            sorted(principal.capabilities)
            if principal.capabilities else ["read", "write"]
        )
        return AccessToken(
            token=token,
            client_id=principal.principal_id,
            scopes=scopes,
            expires_at=None,
        )


def resolve_token_auth():
    """Pick the auth mode (issue #281).

    ``ZMEM_MCP_TOKENS_FILE`` wins (the most specific fleet config); the
    legacy two keep their current relative order and exact behavior.
    Returns ``(verifier, principal_map, startup_principal)`` — in map mode
    ``principal_map`` is set and ``startup_principal`` is None (there is no
    single startup principal; every guard resolves per request); in legacy
    mode the map is None and the one startup principal wraps the config.
    """
    tokens_file = os.environ.get("ZMEM_MCP_TOKENS_FILE", "").strip()
    if tokens_file:
        principal_map = load_principal_map(tokens_file)
        return MapTokenVerifier(principal_map), principal_map, None
    cfg = load_token_config()
    return (
        StaticTokenVerifier(cfg.token),
        None,
        Principal.from_token_config(cfg),
    )
