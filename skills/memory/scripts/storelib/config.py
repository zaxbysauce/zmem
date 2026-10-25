"""Store-side operator configuration (issue #177).

One minimal, read-only reader for the store-side ``auto_retain`` gate.  The
gate file lives BESIDE the store (``<store_dir>/config.json``) — the same
home as the correction-queue sidecar — so config and queue always share a
data dir no matter which env chain (ZMEM_STORE / ZMEM_DATA / defaults)
resolved the store path.

This module deliberately knows nothing about the Hermes provider's own
``auto_retain`` config key (issue #161): that key lives in
``<hermes_home>/zmem/config.json`` and is consumed by the provider's
``auto_retain_enabled()``.  The store subprocess never reads provider home;
the two surfaces are separate by design (see skills/memory/SKILL.md).

Contract (issue #177 Design step 3):
- missing / zero-byte file            -> {}
- malformed JSON / non-object         -> {} + one stderr warning
- oversized file (> MAX_CONFIG_BYTES) -> {} + one stderr warning
- a leading BOM is NOT a failure case: the decode is utf-8-sig, so a
  BOM-prefixed but otherwise valid file is read normally and its values
  apply. That is leniency by design, not a warning.
- never migrates, never opens SQLite, never writes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_FILENAME = "config.json"
MAX_CONFIG_BYTES = 65536

_warned = False


def _store_dir() -> Path:
    """The directory holding the store file, from the store env chain.

    Resolved exactly like the correction-queue sidecar home
    (correction_queue.resolve_queue_dir): the parent of the store path, so
    the queue files and the config file can never drift apart.
    """
    try:
        from storelib.schema import STORE_PATH

        return Path(STORE_PATH).resolve().parent
    except Exception as exc:
        try:
            import host as _host_check
            if isinstance(exc, _host_check.StoreConfigError):
                # Issue #291: a REFUSED configuration (a directory variable
                # naming an existing file) must fail loudly — never fall
                # through to the env chain/cwd fallback below, which would
                # silently activate an unrelated config location.
                raise
        except ImportError:
            pass
        # Unresolvable store (host helpers unavailable) -> fall back to the
        # env chain the store itself uses, then cwd.  Read-only either way.
        store = os.environ.get("ZMEM_STORE", "").strip()
        if store:
            return Path(store).resolve().parent
        data = os.environ.get("ZMEM_DATA", "").strip()
        if data:
            return Path(data).expanduser().resolve()
        return Path.cwd()


def config_path() -> Path:
    """The gate config file path (``<store_dir>/config.json``)."""
    return _store_dir() / CONFIG_FILENAME


def load_config(path: Path | None = None) -> dict:
    """Read the store-side config object; fail-open to {}.

    A malformed, oversized, or non-object file yields {} with ONE stderr
    warning (per process) prefixed ``[zmem] config: ``.  A missing or
    zero-byte file yields {} silently — no config yet is the default-off
    state, not an error.
    """
    global _warned
    target = Path(path) if path is not None else config_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return {}
    if not raw:
        return {}
    if len(raw) > MAX_CONFIG_BYTES:
        if not _warned:
            print(
                "[zmem] config: file exceeds %d bytes; using {}"
                % MAX_CONFIG_BYTES,
                file=sys.stderr,
            )
            _warned = True
        return {}
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        obj = json.loads(text)
    except ValueError:
        if not _warned:
            print("[zmem] config: malformed JSON; using {}", file=sys.stderr)
            _warned = True
        return {}
    if not isinstance(obj, dict):
        if not _warned:
            print("[zmem] config: top level is not an object; using {}",
                  file=sys.stderr)
            _warned = True
        return {}
    return obj
