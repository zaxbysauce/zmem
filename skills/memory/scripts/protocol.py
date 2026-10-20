"""Cross-process protocol identity for the zmem MCP hub (issue #285).

Single source of truth for the compatibility identity an authenticated
client negotiates with the hub via the ``hello`` tool, and for the
per-call ``client_protocol`` gate every other MCP tool applies. The Hermes
broker (issue #287, Workstream T PR 8) consumes these constants for its
negotiation loop; the hub imports this module through the same
checkout-precedence loader it uses for ``schema_meta``.

Keep this module dependency-free (stdlib only): ``mcp_server.py`` loads it
standalone via importlib, so it must never import storelib or anything
with environment or embedding side effects.

Version policy (the issue's constraint): same protocol major = compatible.
The hub supports the CURRENT major and (during a rolling-upgrade release)
the PREVIOUS major, expressed by ``SUPPORTED_PROTOCOL_MAJORS``. A client
whose declared major is outside that window is refused on writes and
interactive reads with the stable ``protocol_incompatible`` error, and
served the empty passive envelope with reason ``protocol-incompatible``
(fail open, distinguishable from ``empty-pool``).

An OMITTED ``client_protocol`` is a legacy client: treated as protocol 1
(the ``LEGACY_ABSENT_PROTOCOL_MAJOR`` window) for one release, then
refused. Two levers end the legacy window (PRR-008 — pick per cohort):
SURGICAL moves ``LEGACY_ABSENT_PROTOCOL_MAJOR`` outside the window (an
out-of-window sentinel like 0 refuses absent callers only, keeping
explicit protocol-1 clients); BLUNT drops major 1 from
``SUPPORTED_PROTOCOL_MAJORS`` (refuses absent AND explicit protocol-1
callers; an empty window tuple is honored by the hub loader).
"""

from __future__ import annotations

# The hub's MCP tool-protocol major. Protocol 1 covers every tool shape
# shipped through 0.109.x; a future breaking change to the tool contract
# (renamed/removed response keys, changed refusal semantics) bumps this.
PROTOCOL_MAJOR = 1

# Majors this hub accepts from a client. During a rolling upgrade the hub
# keeps the previous major for one release (N-1 support); today only
# protocol 1 exists, so the window is a single member. Keep this tuple
# ordered newest-first.
SUPPORTED_PROTOCOL_MAJORS = (1,)

# An omitted ``client_protocol`` means a legacy client that predates the
# handshake: treated as this major (protocol 1) for one release. This
# constant NAMES what an absent caller is — it stays 1 today. Two levers
# end the legacy window (PRR-008): SURGICAL = move this constant outside
# the window (a sentinel like 0; explicit protocol-1 clients keep working,
# absent callers are refused); BLUNT = remove 1 from
# SUPPORTED_PROTOCOL_MAJORS (refuses both cohorts; an empty window tuple
# is honored by the hub loader).
LEGACY_ABSENT_PROTOCOL_MAJOR = 1

# Version of the wire ENVELOPE schema this server emits: the selector
# envelope (results/count/omitted/reason/rendered family), the read
# result dicts, and the write result dicts. Additive keys do not bump
# this; removing or re-typing a key does. (Distinct from the STORE
# schema, which ``hello`` reports separately from schema_meta.)
EVENT_SCHEMA_VERSION = 1

# Stable id of the network capture policy every MCP write path runs:
# capture-mode ``auto`` (secret-like content redacted before persist),
# structured redaction warnings on the response, and the additive
# warning surfacing. If the policy's semantics ever change, change this
# id so clients can detect the difference.
CAPTURE_POLICY_ID = "network-auto-redact-v1"

# The stable error token for a refused write/interactive-read (clients
# branch on this exact string).
ERROR_PROTOCOL_INCOMPATIBLE = "protocol_incompatible"

# The passive empty-envelope reason for an incompatible client. Like
# ``INJECT_REASON_DISABLED`` in schema_meta, this names a boundary fact,
# not a retrieval/gate/budget fact, so it deliberately does NOT join the
# classifier's closed ``INJECT_SILENT_REASONS`` set.
INJECT_REASON_PROTOCOL_INCOMPATIBLE = "protocol-incompatible"


def protocol_supported(major: object) -> bool:
    """True iff ``major`` is a non-bool int inside the supported window.

    ``bool`` is an ``int`` subclass in Python but ``True`` is NOT protocol
    1 — a client that cannot type its own negotiation field fails the
    gate. Every non-int value (str, float, containers) is unsupported;
    raw-value normalization happens at the server gate, which receives
    them untyped by design.
    """
    if isinstance(major, bool) or not isinstance(major, int):
        return False
    return major in SUPPORTED_PROTOCOL_MAJORS
