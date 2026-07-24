#!/usr/bin/env python
"""Embedding generation for ZMem using a bundled ONNX model.

Loads all-MiniLM-L6-v2 (384-dim) from the plugin models directory, generates
L2-normalized embeddings via ONNX Runtime. Runs fully offline once the model
is on disk — no network calls at embed time.

Optional dependency: if onnxruntime or tokenizers is not installed, or the model
file is missing (and cannot be lazy-downloaded — see `_try_download_model`),
all functions return None and callers should degrade gracefully to FTS5-only
recall / lexical-overlap clustering (see store.py `consolidate`).

The model file itself (skills/memory/models/minilm.onnx, ~90MB) is NOT
committed to git (Phase 10, PLAN.md §7-P10) — it's gitignored and either
already present on disk from a prior install, lazy-downloaded on first use
(opt-in only, via ZMEM_MODEL_AUTODOWNLOAD=1 — never automatic, to preserve
zero-cloud-dependency by default), or simply absent (degraded mode). A full
git-history rewrite to purge the 90MB blob already committed on this
branch's history is a disruptive, user-opt-in follow-up — NOT done here
(see PLAN.md).
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
from pathlib import Path

# --- Lazy globals (populated on first use) ---
_session = None
_tokenizer = None
_model_available: bool | None = None
_MODEL_DIM = 384

# sha256 of the minilm.onnx currently shipped/installed on the reference box
# (computed from the on-disk file at Phase-10 implementation time). Any
# downloaded replacement is verified against this before being trusted —
# on mismatch we discard it and fail open to no-embeddings, never load an
# unverified binary.
_MODEL_SHA256 = "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5"

# Default source for a lazy download when the model file is absent. This is
# the widely-used Xenova ONNX export of all-MiniLM-L6-v2 (same architecture/
# dim, but NOT guaranteed byte-identical to `_MODEL_SHA256` — different ONNX
# export toolchains/opsets produce different bytes for the same weights).
# Override with ZMEM_MODEL_URL to point at a build that matches the pinned
# checksum, or place the file manually at <models_dir>/minilm.onnx. Either
# way, a checksum mismatch never crashes — it just falls through to the
# no-embeddings path (see `_try_download_model`).
_DEFAULT_MODEL_URL = (
    "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
)


def _resolve_models_dir() -> Path:
    """Resolve the models directory.

    ZMEM_MODELS_DIR overrides (used by tests to point at an empty/missing
    dir without touching the real installed model). Defaults to the plugin's
    bundled models directory relative to this script.
    """
    override = os.environ.get("ZMEM_MODELS_DIR")
    if override:
        return Path(override)
    return Path(__file__).parent.parent / "models"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_checksum(path: Path, expected: str | None = None) -> bool:
    """True iff `path` exists and its sha256 matches `expected`. Never raises.

    `expected` defaults to the live value of the module-level `_MODEL_SHA256`
    (looked up at call time, NOT bound at function-definition time — this is
    what lets tests override it via monkeypatching the module attribute).
    """
    if expected is None:
        expected = _MODEL_SHA256
    try:
        return path.is_file() and _sha256_file(path) == expected
    except OSError:
        return False


def _try_download_model(
    model_path: Path, url: str | None = None, expected_sha256: str | None = None
) -> bool:
    """Best-effort lazy download of the model file with checksum verification.

    Fail-open by design: any exception, a network error, or a checksum
    mismatch returns False and leaves the caller to degrade to the
    no-embeddings path. Never raises, never leaves a partial/corrupt file at
    `model_path` (downloads to a uniquely-named .part sibling first — unique
    per attempt via pid + random suffix, so concurrent download attempts
    from separate processes never collide on the same intermediate file —
    only renamed into place after the checksum passes).
    """
    import urllib.request

    resolved_url = url or os.environ.get("ZMEM_MODEL_URL", _DEFAULT_MODEL_URL)
    # Unique per-attempt temp name (pid + random suffix) so two concurrent
    # download attempts (e.g. multiple hook processes racing on first use)
    # never write to the same intermediate file.
    tmp_path = model_path.with_suffix(
        f"{model_path.suffix}.{os.getpid()}.{os.urandom(4).hex()}.part"
    )
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(resolved_url, timeout=30) as resp:
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
        if not verify_checksum(tmp_path, expected_sha256):
            print(
                f"zmem: downloaded model checksum mismatch (url={resolved_url}) "
                "-- falling back to no-embeddings. The default download URL is "
                "not guaranteed to produce a build matching the pinned "
                "ZMEM_MODEL_SHA256; set ZMEM_MODEL_URL to a build matching the "
                "pinned checksum, or place the file manually at "
                f"{model_path}.",
                file=sys.stderr,
            )
            return False
        if not model_path.is_file():
            # Avoid unnecessary churn if another concurrent attempt already
            # completed successfully; both would be verified-correct copies
            # of the same nominal model, so either landing is fine.
            tmp_path.replace(model_path)
        return True
    except Exception:
        return False
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _check_available() -> bool:
    """Check if onnxruntime + tokenizers + model files are all present.

    If the model file is missing, attempts a lazy download (checksum-
    verified) ONLY when explicitly opted in via ZMEM_MODEL_AUTODOWNLOAD=1.
    This is opt-in, not opt-out: by default zmem never makes an unsolicited
    network call, to honor the project's local-first / zero-cloud-dependency
    promise. Download failure of any kind is silent and falls through to
    the no-embeddings path; it never raises.
    """
    global _model_available
    if _model_available is not None:
        return _model_available
    try:
        import onnxruntime  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        _model_available = False
        return False
    models_dir = _resolve_models_dir()
    model_path = models_dir / "minilm.onnx"
    tok_path = models_dir / "tokenizer.json"
    if not model_path.is_file() and os.environ.get("ZMEM_MODEL_AUTODOWNLOAD", "0") == "1":
        _try_download_model(model_path)
    _model_available = model_path.is_file() and tok_path.is_file()
    return _model_available


def _ensure_loaded():
    """Lazy-load the ONNX session and tokenizer. Called on first embed."""
    global _session, _tokenizer
    if _session is not None:
        return
    if not _check_available():
        return
    import onnxruntime as ort
    from tokenizers import Tokenizer

    models_dir = _resolve_models_dir()
    _session = ort.InferenceSession(str(models_dir / "minilm.onnx"))
    _tokenizer = Tokenizer.from_file(str(models_dir / "tokenizer.json"))
    _tokenizer.enable_padding(length=128)
    _tokenizer.enable_truncation(max_length=128)


def embed_text(text: str) -> bytes | None:
    """Generate a 384-dim L2-normalized embedding for the given text.

    Returns a packed float32 blob (1536 bytes) suitable for sqlite-vec, or
    None if the embedding infrastructure is unavailable.
    """
    if not text or not text.strip():
        return None
    _ensure_loaded()
    if _session is None or _tokenizer is None:
        return None

    import numpy as np

    encoded = _tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

    inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
    if len(_session.get_inputs()) > 2:
        inputs["token_type_ids"] = np.zeros_like(input_ids)

    outputs = _session.run(None, inputs)
    last_hidden = outputs[0]  # [1, seq_len, 384]

    # Mean pooling with attention mask
    mask = attention_mask[:, :, None].astype(np.float32)
    summed = (last_hidden * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-8, None)
    pooled = summed / counts

    # L2 normalize
    norm = np.linalg.norm(pooled, axis=1, keepdims=True)
    pooled = pooled / np.clip(norm, 1e-8, None)

    return struct.pack(f"{_MODEL_DIM}f", *pooled[0])


def cosine_similarity_from_blob(blob1: bytes, blob2: bytes) -> float:
    """Compute cosine similarity between two float32 blobs."""
    import numpy as np

    v1 = np.frombuffer(blob1, dtype=np.float32)
    v2 = np.frombuffer(blob2, dtype=np.float32)
    if len(v1) != len(v2):
        return 0.0
    return float(np.dot(v1, v2))


def is_available() -> bool:
    """Public API: check if embeddings are available without loading the model."""
    return _check_available()
