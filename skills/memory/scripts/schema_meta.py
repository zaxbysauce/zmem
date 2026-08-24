"""Single source of truth for the memory-store schema version and the small set
of write-path constants that every write surface must agree on.

Both ``store.py`` (the writer/migrator) and ``doctor.py`` (the health gate)
must agree on which schema version is "current". A stale copy in doctor.py
once caused every healthy v7 store to FAIL doctor's schema-version check
(issue #36, M11) because doctor hardcoded v5 while store shipped v7.

The same drift problem applies to the content cap and the allowed type/signal
enums: the CLI (``store.py``), the MCP server (``hermes-plugin/server``), and
the local Hermes provider (``hermes-plugin/__init__.py``) all validate against
these, and a hard-coded literal in any one path creates the asymmetries logged
in issue #37 (L7/L8). Keep them here so every consumer imports the same value
rather than re-typing it.

Keep this module dependency-free (stdlib only) and tiny so importing it has
no side effects — doctor.py and the hermes provider can import it without
pulling in store.py's 253 KB of writer logic, host-path resolution, or
env-var parsing.
"""

from __future__ import annotations

# Bumped by store.py's migration machinery; doctor.py reads it to decide
# pass/warn/fail. Edit HERE and both consumers stay in sync.
SUPPORTED_SCHEMA_VERSION = 8

# The meta-table key under which the version is stored in the store.
SCHEMA_VERSION_KEY = "schema_version"

# Maximum content length accepted by every write path (CLI add, MCP add, local
# Hermes add, ingest). All surfaces reject content over this cap rather than
# silently truncating it (#36 M17 unified the cap; #37 L8 made the local Hermes
# path enforce it). Edit HERE and every consumer stays in sync.
MAX_CONTENT_CHARS = 65536

# Memory `type` enum. All write surfaces validate against this tuple.
ALLOWED_TYPES = ("fact", "lesson", "convention", "preference")

# Memory `signal` enum. All write surfaces validate against this tuple. Ordered
# roughly by trustworthiness (test/compile/lint > reviewer/user > none).
ALLOWED_SIGNALS = ("test", "compile", "lint", "reviewer", "user", "none")

# Inject-floor constants for hook surfaces (issue #58, 3.8). These three
# thresholds are intentionally distinct: each reflects a different surface's
# precision-vs-coverage tradeoff. They are env-overridable; see
# storelib/cli.py for the env names. Document all three in SKILL.md so
# operators understand which floor their hook currently applies.
#
#  - PROMPT (0.25): `recall` default. The default confidence floor for
#    FTS/vec recall. Anything below this is dropped before scoring.
#  - RECENT (0.5): `recent` default. The admin-pull floor for
#    SessionStart / subagent recall. Tighter because the surface is
#    "high-confidence recent material", not "query-best match".
#  - GATE_NONE (0.4): hook selective-inject gate. signal=none rows must
#    clear this floor to ride along with a high-signal match (the gate's
#    type-relax is empty until Phase 4 ships `decision`/`constraint`).
INJECT_FLOOR_PROMPT_DEFAULT = 0.25
INJECT_FLOOR_RECENT_DEFAULT = 0.5
INJECT_FLOOR_GATE_NONE = 0.4

# Signals considered "high-trust" by the hook selective-inject gate
# (issue #58, 3.8). Rows with these signals clear the PROMPT floor
# (0.25); rows with signal=none must clear GATE_NONE (0.4).
INJECT_HIGH_SIGNALS = frozenset({"test", "compile", "lint", "reviewer"})

# Env var names for the floors above. Centralised so the helper and the
# doc reference the same string.
INJECT_FLOOR_PROMPT_ENV = "ZMEM_INJECT_FLOOR_PROMPT"
INJECT_FLOOR_RECENT_ENV = "ZMEM_INJECT_FLOOR_RECENT"
INJECT_FLOOR_GATE_NONE_ENV = "ZMEM_INJECT_FLOOR_GATE_NONE"

# Vec0 KNN over-fetch factor (issue #58, 3.1). Default 8; the recall path
# over-fetches by this factor before namespace-filtering so a foreign
# namespace cannot dominate same-namespace slots. Consolidate uses its
# own escalation loop with a 500-row cap and is unaffected by this knob.
ZMEM_VEC_NS_OVERFETCH_DEFAULT = 8
ZMEM_VEC_NS_OVERFETCH_ENV = "ZMEM_VEC_NS_OVERFETCH"

