"""Provider transport — compatibility shim over the shared fleet module.

Issue #287 lifted the transport implementation to
``skills/memory/scripts/fleet/transport.py`` so every harness adapter (the
T-08 broker, the Hermes provider, future adapters) shares one module.  This
file keeps the historical load path working: the Hermes plugin
(``hermes-plugin/__init__.py``) and the transport tests load THIS file by
path (the hyphenated plugin directory is not an importable package), so it
forwards the complete lifted surface — public names AND the underscore
names callers pin (``_empty_envelope``, ``_ENVELOPE_REQUIRED_KEYS``,
``_resolve_store_py``, ``_resolve_zmem_home``, ``_resolve_token``,
``_load_mcp_call``).  If the lifted module is missing (a checkout variant
that ships only the plugin), this module's import raises and the provider's
existing loader keeps its fail-open behavior (``_transport = None``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_LIFTED = (Path(__file__).resolve().parents[1]
           / "skills" / "memory" / "scripts" / "fleet" / "transport.py")
if not _LIFTED.is_file():
    # A copy-installed plugin (cp -r hermes-plugin <dir> alone, the layout
    # README documents) has no skills/ tree beside it. Fall back to
    # ZMEM_HOME (the documented remedy) before giving up, so the shim
    # raises FileNotFoundError only when there is truly no checkout to
    # load from.
    _home = (os.environ.get("ZMEM_HOME") or "").strip()
    if _home:
        _candidate = (Path(_home) / "skills" / "memory" / "scripts"
                      / "fleet" / "transport.py")
        if _candidate.is_file():
            _LIFTED = _candidate

_spec = importlib.util.spec_from_file_location("zmem_fleet_transport", _LIFTED)
_fleet = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("zmem_fleet_transport", _fleet)
_spec.loader.exec_module(_fleet)  # type: ignore[union-attr]

# Forward every attribute of the lifted module (public + private) so any
# historical import site or test pin keeps working against this path.
for _name in dir(_fleet):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_fleet, _name)

# Re-export the names static analyzers (and the tests) expect to find here.
TransportMode = _fleet.TransportMode
resolve_transport_mode = _fleet.resolve_transport_mode
resolve_deadline_s = _fleet.resolve_deadline_s
DeadlineExecutor = _fleet.DeadlineExecutor
McpHttp = _fleet.McpHttp
LocalSubprocess = _fleet.LocalSubprocess
_empty_envelope = _fleet._empty_envelope
_coerce_envelope = _fleet._coerce_envelope
_ENVELOPE_REQUIRED_KEYS = _fleet._ENVELOPE_REQUIRED_KEYS
_resolve_store_py = _fleet._resolve_store_py
_resolve_zmem_home = _fleet._resolve_zmem_home
_resolve_token = _fleet._resolve_token
_load_mcp_call = _fleet._load_mcp_call
_query_args = _fleet._query_args
