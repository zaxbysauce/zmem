"""Single source of truth for the memory-store schema version.

Both ``store.py`` (the writer/migrator) and ``doctor.py`` (the health gate)
must agree on which schema version is "current". A stale copy in doctor.py
once caused every healthy v7 store to FAIL doctor's schema-version check
(issue #36, M11) because doctor hardcoded v5 while store shipped v7.

Keep this module dependency-free (stdlib only) and tiny so importing it has
no side effects — doctor.py can import it without pulling in store.py's
253 KB of writer logic, host-path resolution, or env-var parsing.
"""

from __future__ import annotations

# Bumped by store.py's migration machinery; doctor.py reads it to decide
# pass/warn/fail. Edit HERE and both consumers stay in sync.
SUPPORTED_SCHEMA_VERSION = 7

# The meta-table key under which the version is stored in the store.
SCHEMA_VERSION_KEY = "schema_version"
