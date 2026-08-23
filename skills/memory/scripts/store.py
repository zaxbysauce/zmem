"""ZMem semantic store — public CLI entrypoint (thin shim).

SPLIT (issue #57 / SOTA PR 2/10): all behaviour now lives in the ``storelib``
package under ``skills/memory/scripts/storelib/``. This file stays at
``skills/memory/scripts/store.py`` so every hook, MCP subprocess, Hermes
provider, SKILL.md and test that invokes ``store.py`` keeps working. It is a
behavior-identical re-export of the storelib API plus the argparse ``main()``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure this directory (which contains storelib/) is importable regardless of
# how store.py is launched (hook PATH, MCP subprocess, `python store.py`, ...).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storelib as _storelib  # noqa: E402
from storelib import main  # noqa: E402

# Re-derive env-derived paths (ZMEM_STORE / ZMEM_DATA / ...) at load time, as
# the pre-split module did at its own import. storelib is a shared process
# singleton, so without this its STORE_PATH/CORE_MD_PATH would stay at whatever
# env was current the first time storelib was imported (see storelib/_refresh_env_state).
_storelib._refresh_env_state()  # noqa: E402


def __getattr__(name):
    """Expose the storelib surface on `store` (same names as pre-split).

    Pre-split, `store._env_float`, `store.add_memory`, `store.CONFIDENCE_FLOOR`,
    ... all resolved on this one module. Post-split they live in storelib; a
    module __getattr__ keeps `import store; store.X` working for every name
    without enumerating them here.

    NOTE: a module-level `__setattr__` is NOT honoured by CPython (attribute
    writes always go to the module ``__dict__``), so assigning a mutable global
    on `store` (e.g. ``store.X = v`` or ``mock.patch.object(store, 'X', v)``)
    does NOT reach the submodule that reads ``X``. Code that patches such a
    global must target its owning submodule (e.g. ``storelib.consolidate.X``).
    Reads here forward live for the handful of mutable globals consumers mock.
    """
    return getattr(_storelib, name)



if __name__ == "__main__":
    main()
