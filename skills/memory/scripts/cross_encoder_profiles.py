"""Checked-in cross-encoder model profile registry (issue #125).

Stdlib-only, mirrors the `embed_profiles` registry pattern: one named
profile whose model artifact is pinned by exact SHA-256, resolved through
`resolve_profile()` on every load and verified by `verify_profile_file()`
before any byte of it reaches a runtime loader. Import-time validation is
the recurrence guardrail: a registry entry whose checksum is not a
non-empty lowercase 64-hex digest refuses the whole module (this is the
demonstrated bite for the "unverified artifact ingestion" defect class).

The default profile ships an EMPTY `url`: profile download stays disabled
until the operator supplies `ZMEM_CROSS_ENCODER_MODEL_URL`. No network
access happens at import time.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DEFAULT_PROFILE = "mini-pair-scorer"

PROFILES: dict[str, dict] = {
    "mini-pair-scorer": {
        "hf_id": "zmem/mini-pair-scorer-fixture",
        "model_file": "mini_pair_scorer.onnx",
        "tokenizer_file": "tokenizer.json",
        "url": "",
        "sha256": "be30078bc29868074ddb09c8eefc8170594368fc82908d63988dd9e226c26993",
    },
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_registry() -> None:
    """Refuse the module on any entry whose checksum is not an exact
    non-empty lowercase 64-hex digest. ValueError message names the profile
    and field so an operator sees which artifact is unpinned."""
    for name, entry in PROFILES.items():
        digest = entry.get("sha256") or ""
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise ValueError(
                f"invalid cross-encoder profile checksum: {name}.sha256")


_validate_registry()


def resolve_profile(name: str | None = None) -> dict:
    """Return the registry entry for `name` (default profile when None/empty).
    Unknown names raise ValueError("unknown cross-encoder profile: <name>")."""
    key = name if name else DEFAULT_PROFILE
    entry = PROFILES.get(key)
    if entry is None:
        raise ValueError(f"unknown cross-encoder profile: {key}")
    return entry


def profile_model_path(models_dir: Path, profile: dict) -> Path:
    """The profile's model file under `models_dir`."""
    return Path(models_dir) / profile["model_file"]


def verify_profile_file(path: Path, profile: dict) -> bool:
    """True iff `path` exists, is readable, and its SHA-256 equals the
    profile digest. Never raises; streams the file so large models do not
    need to fit in memory."""
    expected = (profile.get("sha256") or "").lower()
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected
