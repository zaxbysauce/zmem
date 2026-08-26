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
# v11 (issue #61): associative links (A-MEM lite) — the `memory_link` edge
# table + the `trust_score` column on `memory` (contradict −0.10 / corroborate
# +0.05, clamped [0,1]). Links are generated on every add/update, walked one
# hop at recall, inspectable via `links`/`contradict`, and round-tripped by
# export/ingest — not a dormant schema artifact.
SUPPORTED_SCHEMA_VERSION = 11

# The meta-table key under which the version is stored in the store.
SCHEMA_VERSION_KEY = "schema_version"

# Maximum content length accepted by every write path (CLI add, MCP add, local
# Hermes add, ingest). All surfaces reject content over this cap rather than
# silently truncating it (#36 M17 unified the cap; #37 L8 made the local Hermes
# path enforce it). Edit HERE and every consumer stays in sync.
MAX_CONTENT_CHARS = 65536

# Memory `type` enum. All write surfaces validate against this tuple.
# v9 (#59): `decision` and `constraint` are first-class shipped types.
ALLOWED_TYPES = ("fact", "lesson", "convention", "preference", "decision", "constraint")

# Memory `signal` enum. All write surfaces validate against this tuple. Ordered
# roughly by trustworthiness (test/compile/lint > reviewer/user > none).
ALLOWED_SIGNALS = ("test", "compile", "lint", "reviewer", "user", "none")

# Memory `taint` enum (issue #59, 4.7). The provenance/trust rank of a row's
# ORIGIN. There is deliberately NO fourth rank: an unknown taint value is
# refused at every write surface rather than coerced. Rank order is
# trusted_internal < untrusted_tool < untrusted_web (see TAINT_RANK). A row's
# taint is worst-of'd forward through lineage (update re-creation, consolidate
# absorb); a tombstone (supersede/invalidate) preserves the row's taint — a
# tombstone creates no new row, so there is no incoming taint to merge.
ALLOWED_TAINTS = ("trusted_internal", "untrusted_tool", "untrusted_web")

# Numeric rank for worst-of propagation. A worse taint strictly dominates a
# better one when two line items meet (update re-creation, consolidate absorb).
TAINT_RANK = {"trusted_internal": 0, "untrusted_tool": 1, "untrusted_web": 2}

# Entity `kind` enum (issue #60, 5.1). Deliberately small: five kinds cover
# the deterministic extractor's output without an LLM. `person` is NEVER
# auto-detected — it can only be created via an explicit `entity:person:Name`
# tag, and no code path auto-merges person entities (entity-merge is manual,
# --confirm-gated, and refuses kind mismatches).
ENTITY_KINDS = ("person", "project", "tool", "preference", "other")

# Signals that make a NEW write default to trusted_internal (human-authored /
# closeout / grounded evidence). Any other signal (`none` — an agent's
# self-opinion) defaults the new row to untrusted_tool. This is the single
# store-side derivation stored in storelib/write._default_taint_for_signal —
# the CLI and ingest surfaces share it directly. The REMOTE agent surfaces
# (Hermes/MCP) deliberately do NOT: plan M5 pins their default to an explicit
# untrusted_tool (an agent write is ungrounded self-opinion unless the caller
# claims more), and they pass an explicit --taint only when marking e.g. a
# web fetch (PR-review PRR-N wording fix).
TAINT_TRUSTED_SIGNALS = frozenset({"test", "compile", "lint", "reviewer", "user"})


def validate_taint(value: str) -> str:
    """Return `value` if it is a known taint rank, else raise ValueError.

    Every writer that accepts a user/remote-supplied taint must call this (or
    constrain to ALLOWED_TAINTS via argparse choices) FIRST — an unknown taint
    value is refused, never silently coerced (issue #59, 4.7).
    """
    if value not in TAINT_RANK:
        raise ValueError(
            f"taint must be one of: {', '.join(ALLOWED_TAINTS)}"
        )
    return value


def worse_taint(a: str, b: str) -> str:
    """The worse of two taint ranks (the least trustworthy provenance).

    A row's provenance is only as good as its least-trustworthy contributor:
    on update re-creation and consolidate absorb, the surviving row's taint is
    the worst of the two merged sources (issue #59, 4.7). Unknown inputs are
    impossible by construction (every writer validates first), but an unknown
    value degrades to the WORST rank for fail-closed safety rather than
    silently upgrading trust.
    """
    ra = TAINT_RANK.get(a, 2)
    rb = TAINT_RANK.get(b, 2)
    return a if ra >= rb else b

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
#    clear this floor to ride along with a high-signal match. The gate has no
#    type-relax branch (issue #59 ships `decision`/`constraint` as ordinary
#    types; they are judged only by signal/confidence like every other type).
INJECT_FLOOR_PROMPT_DEFAULT = 0.25
INJECT_FLOOR_RECENT_DEFAULT = 0.5
INJECT_FLOOR_GATE_NONE_DEFAULT = 0.4

# Signals considered GROUNDED (trusted) by the hook selective-inject
# gate (issue #58, 3.8): rows with these signals clear the PROMPT floor
# (0.25); rows with any other signal — in practice only `none`, the
# agent's self-opinion — must clear the tighter GATE_NONE floor (0.4).
# Matches the signal hierarchy: test/compile/lint > reviewer/user > none.
INJECT_GROUNDED_SIGNALS = frozenset({"test", "compile", "lint", "reviewer", "user"})

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

