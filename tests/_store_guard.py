"""Test-harness store isolation guard (issue #244).

A test run must never resolve — let alone write — the operator's REAL
default store. This guard fires (exit 1) when any store path the resolution
chain can produce, explicitly env-named or not, lands inside the real
home's ``.zmem`` root while that root exists. Ephemeral environments (CI
runners, fresh containers) have no ``~/.zmem`` and nothing to protect, so
the guard stays silent there on purpose; the FIRST-WRITE case for test
pollution is owned by the write-time refusal in storelib/cli.py, which
carries no existence gate.

Opt out explicitly with ZMEM_ALLOW_REAL_STORE=1 (deliberate operator runs
against the live store, e.g. canary maintenance).

Imported for its side effect by every store-touching test file and by
conftest.py; ``--probe`` prints the resolution table for direct invocation
(the C7 acceptance check drives it).
"""
import os
import sys
from pathlib import Path

_OPT_OUT = "ZMEM_ALLOW_REAL_STORE"
_ENV_CANDIDATES = (
    "ZMEM_STORE",
    "ZMEM_DATA",
    "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA",
)


def _candidate_paths():
    """Every store path the resolution chain can produce (union), for the
    --probe report. Mirrors host.resolve_store_path's inputs WITHOUT
    importing it (import order in test files is not guaranteed to have the
    scripts dir on sys.path yet).
    """
    seen = set()
    for var in _ENV_CANDIDATES:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser()
        cand = p if p.suffix == ".sqlite" else p / "store.sqlite"
        key = str(cand).lower()
        if key not in seen:
            seen.add(key)
            yield cand
    home = Path(os.path.expanduser("~"))
    for legacy in (
        home / ".zcode" / "memory" / "store.sqlite",
    ):
        key = str(legacy).lower()
        if key not in seen:
            seen.add(key)
            yield legacy
    yield home / ".zmem" / "store.sqlite"


def _resolved_store_path():
    """The store path this environment WOULD use, by the same precedence
    host.resolve_store_path applies (explicit vars in order, else the home
    default). The guard judges THIS path — a scratch ZMEM_STORE/ZMEM_DATA
    must keep the run green even though the default candidate still exists
    inside the real root."""
    for var in _ENV_CANDIDATES:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser()
        return p if p.suffix == ".sqlite" else p / "store.sqlite"
    home = Path(os.path.expanduser("~"))
    return home / ".zmem" / "store.sqlite"


def check(fail_fast=True):
    """Return (ok, message). When fail_fast and not ok, exit 1 with the message."""
    if os.environ.get(_OPT_OUT, "").strip() == "1":
        return True, f"opt-out {_OPT_OUT}=1 set"
    home = Path(os.path.expanduser("~"))
    zmem_root = home / ".zmem"
    if not zmem_root.is_dir():
        return True, f"no real store root at {zmem_root} (ephemeral environment)"
    resolved = _resolved_store_path()
    try:
        rel = resolved.relative_to(zmem_root)
    except ValueError:
        return True, f"resolved store {resolved} is outside the real .zmem root"
    # Any path inside the real .zmem root — the store itself, backups,
    # subdirs — is the operator's live data.
    msg = (
        "tests/_store_guard: refusing to run against the REAL default "
        f"store area ({resolved} is inside {zmem_root}{os.sep}{rel}). "
        "Set ZMEM_STORE (a sqlite FILE path) or ZMEM_DATA (a directory) "
        f"to a scratch path, or set {_OPT_OUT}=1 to run deliberately "
        "against the live store."
    )
    if fail_fast:
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    return False, msg


if __name__ == "__main__":
    if "--probe" in sys.argv:
        ok, msg = check(fail_fast=False)
        print(f"store-guard probe: ok={ok} {msg}")
        print(f"resolved: {_resolved_store_path()}")
        print("candidates:")
        for c in _candidate_paths():
            print(f"  {c}")
        raise SystemExit(0 if ok else 1)
    check()

else:
    # Import-time guard: every `import _store_guard` line in a test file
    # runs this before the module body can touch a store.
    check()
