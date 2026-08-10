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

