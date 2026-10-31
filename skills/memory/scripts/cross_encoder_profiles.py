"""Checked-in cross-encoder model profile registry (issue #125).

Stdlib-only, mirrors the `embed_profiles` registry pattern: one named
profile whose model AND tokenizer artifacts are pinned by exact SHA-256,
resolved through `resolve_profile()` on every load. Since issue #418 the
loader (`storelib.cross_encoder._local_scorer`) verifies the digest of THE
BUFFER it constructs each artifact from on the profile leg — no
verify-then-reopen window. `verify_profile_file()` remains a supported
helper/diagnostic API (path-level streaming verification, e.g. for doctor
style probes); it is no longer the load-path gate. Import-time validation is
the recurrence guardrail: a registry entry whose `sha256` OR
`tokenizer_sha256` is not a non-empty lowercase 64-hex digest refuses the
whole module (this is the demonstrated bite for the "unverified artifact
ingestion" defect class).

The default profile ships an EMPTY `url`: profile download stays disabled
until the operator supplies `ZMEM_CROSS_ENCODER_MODEL_URL`. No network
access happens at import time.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DEFAULT_PROFILE = "mini-pair-scorer"

# The mini-pair-scorer profile ships an artifact PAIR, and both halves are
# pinned (#418). HONEST IDENTITY of the model half (PR-419 review F-023):
# tests/fixtures/cross_encoder/mini_pair_scorer.onnx is 128 bytes of Git-LFS
# POINTER TEXT preserved as a plain blob (filter unset) — the pin be30078b…
# is the digest of those pointer bytes, not of any ONNX; the leg is
# fixture-only by construction and never executes a real model (a real
# onnxruntime parse of the pointer text fails closed). The tokenizer half
# (899 bytes, sha256 caef352a…) is the fixture-pair tokenizer, canonical
# record pinned by tests/test_cross_encoder.py::_expected_profile_record.
# PATH NOTE (review PRR-004/F-007): the profile resolves tokenizer.json
# BESIDE the model in the models dir — the SAME <models_dir>/tokenizer.json
# path the minilm embedder loads — so the two pins are mutually exclusive on
# one path by design; a deployment that provisions only the minilm pair
# fails this pin closed on the profile leg, and real reranking uses the
# operator-vouched explicit leg.
_TOKENIZER_SHA256 = "caef352a8affbe14f5235b5487dd766f2133ddfb70c2d59bfca5734496051cd7"

PROFILES: dict[str, dict] = {
    "mini-pair-scorer": {
        "hf_id": "zmem/mini-pair-scorer-fixture",
        "model_file": "mini_pair_scorer.onnx",
        "tokenizer_file": "tokenizer.json",
        "url": "",
        "sha256": "be30078bc29868074ddb09c8eefc8170594368fc82908d63988dd9e226c26993",
        "tokenizer_sha256": _TOKENIZER_SHA256,
    },
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_registry() -> None:
    """Refuse the module on any entry whose checksum is not an exact
    non-empty lowercase 64-hex digest. ValueError message names the profile
    and field so an operator sees which artifact is unpinned."""
    for name, entry in PROFILES.items():
        for field in ("sha256", "tokenizer_sha256"):
            digest = entry.get(field) or ""
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError(
                    f"invalid cross-encoder profile checksum: {name}.{field}")


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
