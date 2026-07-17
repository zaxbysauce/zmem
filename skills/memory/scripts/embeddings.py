#!/usr/bin/env python
"""Embedding generation for ZMem using a bundled ONNX model.

Loads all-MiniLM-L6-v2 (384-dim) from the plugin models directory, generates
L2-normalized embeddings via ONNX Runtime. Fully offline — zero network calls.

Optional dependency: if onnxruntime or tokenizers is not installed, or the model
file is missing, all functions return None and callers should degrade gracefully
to FTS5-only recall.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# --- Lazy globals (populated on first use) ---
_session = None
_tokenizer = None
_model_available: bool | None = None
_MODEL_DIM = 384


def _resolve_models_dir() -> Path:
    """Resolve the models directory relative to this script."""
    return Path(__file__).parent.parent / "models"


def _check_available() -> bool:
    """Check if onnxruntime + tokenizers + model files are all present."""
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
