"""Embedding profile registry (issue #63, 8.2). Single source of truth for the
model/dim/hash facts of every shipped embedding profile.

Both ``embeddings.py`` (the loader), ``storelib/schema.py`` (the vec0 DDL dim),
``storelib/cli.py`` (profile selection + dim-mismatch refusal), and
``doctor.py`` (the embeddings_health check) must agree on these numbers. A
hard-coded literal in any one of them is the exact drift this module exists to
prevent — the same reasoning that moved MAX_CONTENT_CHARS/ALLOWED_* into
``schema_meta.py`` for the write path.

Keep this module dependency-free (stdlib only): doctor and test harnesses must
be able to import it without onnxruntime, numpy, or the writer stack.

Profiles shipped:
- ``minilm`` — the operator default: all-MiniLM-L6-v2 ONNX (the operator-box
  build pinned by sha256 — NOT the public Xenova HF export; see MINILM_NOTES),
  384-dim.
- ``fake``   — deterministic placeholder vectors for tests/CI that must run
  model-absent. Never for operator stores; doctor warns when a real store runs
  under it.

A third 2026 ONNX profile was evaluated (Qwen3-Embedding-0.6B /
nomic-embed-text-v2-moe) and deliberately OMITTED per the issue's rule: no
publicly published sha256 for a downloadable local ONNX artifact could be
verified at authoring time, and "a name with empty sha256 is a stub" is
forbidden here. Add one only with a personally verified (hf_id, dim, sha256).
"""

from __future__ import annotations

import hashlib
import os
import struct

from schema_meta import normalize_content

# Env var that selects the active profile. Empty/unset -> DEFAULT_PROFILE.
PROFILE_ENV = "ZMEM_EMBED_PROFILE"
DEFAULT_PROFILE = "minilm"

# The trust root for the default profile: sha256 of the original operator-box
# ONNX export of all-MiniLM-L6-v2, bundled/downloaded as minilm.onnx. Those
# bytes are preserved byte-exact in this repository's Git LFS history at commit
# 3240ea1a0cf3d972f6a7d472cee73abaddfc038d (skills/memory/models/minilm.onnx,
# 90,387,630 bytes) and served as the never-retargeted GitHub release asset
# `model-minilm-bbd7b466/minilm.onnx`. They are NOT the public Xenova
# HF-repo export (onnx/model.onnx oid
# 759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e,
# 90,387,606 bytes) — same checkpoint, different builds, different bytes; this
# pins an ONNX FILE, not the sentence-transformers PyTorch weights, and
# different exports of the same checkpoint hash differently by design.
# Mismatch therefore usually means "the wrong build got installed", never
# "disable verification".
# `ZMEM_MODEL_ALLOW_UNVERIFIED` does not exist and must not be added; there is
# deliberately also no env override for this constant itself (removed in #42's
# hardening round) so attacker-writable environment can never lower the trust
# root.
MINILM_SHA256 = "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5"

# Trust root for the checked-in tokenizer artifact (issue #418): sha256 of
# skills/memory/models/tokenizer.json preserved byte-exact in this
# repository's Git LFS history (711,661 bytes; the LFS oid IS the content
# digest, so `git lfs ls-files` cross-checks this constant). This pin covers
# the MINILM EMBEDDER's tokenizer load only — the cross-encoder
# mini-pair-scorer profile pins its OWN (different, fixture-pair) tokenizer
# as `tokenizer_sha256` in cross_encoder_profiles.py; two artifacts, two
# pins. Same laws as
# MINILM_SHA256: no env override, no unverified-load escape hatch; a mismatch
# means "the wrong tokenizer bytes got installed", never "disable
# verification". The load path verifies from the SAME buffer the Tokenizer is
# constructed from (Tokenizer.from_buffer), extending the #36 M15
# verify-then-reopen closure to the tokenizer.
TOKENIZER_SHA256 = "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0"

MINILM_NOTES = (
    "Published sha256 is of the original operator-box ONNX export of "
    "all-MiniLM-L6-v2 (preserved in this repo's LFS history at "
    "3240ea1a0cf3d972f6a7d472cee73abaddfc038d and served as the "
    "model-minilm-bbd7b466 release asset), not the public Xenova "
    "onnx/model.onnx export (oid "
    "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e) and "
    "not the sentence-transformers PyTorch weights — the builds are NOT "
    "byte-identical. A checksum_mismatch almost always means the wrong build "
    "was placed/downloaded; restore the pinned bytes from the "
    "model-minilm-bbd7b466 release asset (or point ZMEM_MODEL_URL at it) "
    "rather than disabling verification."
)

FAKE_DIM = 16

PROFILES: dict[str, dict] = {
    "minilm": {
        "hf_id": "Xenova/all-MiniLM-L6-v2",
        "dim": 384,
        "sha256": MINILM_SHA256,
        "model_file": "minilm.onnx",
        "tokenizer_file": "tokenizer.json",
        "notes": MINILM_NOTES,
    },
    "fake": {
        "hf_id": "",
        "dim": FAKE_DIM,
        "sha256": "",
        "model_file": "",
        "tokenizer_file": "",
        "notes": (
            "Deterministic test embedder: signed token-bucket hash of the "
            "content_norm form of the text. No files, no network, no third-party "
            "deps. Vector quality is lexical overlap only — placeholders, not "
            "semantics."
        ),
    },
}

# embedding_model column marker per profile. The `memory.embedding_model`
# column was previously hardcoded to 'minilm-onnx' at four write sites;
# profile switches would silently mislabel rows, so every writer now derives
# the marker from HERE (issue #63 critic round C2).


def is_valid_profile(name: str) -> bool:
    return name in PROFILES


def get_profile(name: str) -> dict:
    """Return the registry entry for `name`; unknown names raise ProfileError."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ProfileError(
            f"unknown embedding profile {name!r} (valid: "
            f"{', '.join(sorted(PROFILES))})"
        ) from None


class ProfileError(ValueError):
    """Refusal-level error: the requested profile name is not registered."""


def resolve_active_profile(environ: dict | None = None) -> str:
    """Read PROFILE_ENV fresh on EVERY call (never cached — mirrors the
    call-time `_MODEL_SHA256` lookup contract so tests and long-lived servers
    observe env changes). Unknown values raise ProfileError; empty/unset means
    DEFAULT_PROFILE."""
    env = os.environ if environ is None else environ
    raw = (env.get(PROFILE_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_PROFILE
    get_profile(raw)  # validates; raises ProfileError on unknown
    return raw


def get_env_raw(environ: dict | None = None) -> str | None:
    """The raw PROFILE_ENV value, whatever it contains — for diagnostics that
    must show the operator's exact setting even when it is invalid."""
    env = os.environ if environ is None else environ
    return env.get(PROFILE_ENV)


def active_dim(environ: dict | None = None) -> int:
    return get_profile(resolve_active_profile(environ))["dim"]


def embedding_model_name(profile_name: str) -> str:
    """The `memory.embedding_model` marker written next to blobs produced by
    `profile_name`'s embedder. Real profiles are '<name>-onnx'; fake carries
    its own distinct marker so doctor/stats can always tell rows apart."""
    get_profile(profile_name)  # validate first
    if profile_name == DEFAULT_PROFILE:
        return "minilm-onnx"
    return f"{profile_name}-onnx" if PROFILES[profile_name]["sha256"] else profile_name


def normalize_for_fake(s: str) -> str:
    """Canonical form hashed by the fake embedder — the SAME normalization the
    content_norm column uses (single implementation lives in schema_meta)."""
    return normalize_content(s)


def fake_embed(text: str) -> bytes:
    """Deterministic FAKE_DIM-dim float32 blob from the canonical form of
    `text`.

    Signed feature hashing over tokens + adjacent-bigram features: sha256 of
    each feature picks both its bucket and sign, so cosine similarity between
    two texts reflects their canonical-token overlap — enough signal for
    hybrid/MMR/link tests to exercise real ranking paths without any model,
    files, or network. Deterministic across platforms and Python versions.
    """
    norm = normalize_for_fake(text)
    buckets = [0.0] * FAKE_DIM
    tokens = norm.split()
    feats = list(tokens)
    feats.extend(f"{a}_{b}" for a, b in zip(tokens, tokens[1:]))
    for feat in feats:
        h = hashlib.sha256(feat.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % FAKE_DIM
        buckets[idx] += 1.0 if (h[4] & 1) == 0 else -1.0
    norm_sq = sum(b * b for b in buckets)
    if norm_sq == 0.0:
        buckets[0] = 1.0
    else:
        inv = 1.0 / (norm_sq ** 0.5)
        buckets = [b * inv for b in buckets]
    return struct.pack(f"<{FAKE_DIM}f", *buckets)
