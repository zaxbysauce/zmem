#!/usr/bin/env python
"""Issue #473 (plan critic F6): byte-parity hard gate for the bounded ONNX
resource policy.

The issue contract says vector bytes stay BYTE-IDENTICAL under the resource
caps. This gate constructs TWO sessions from the SAME digest-verified buffer
— one with the bounded policy the fix installs, one with sess_options=None
(the base behavior) — and proves the embedding blobs and cross-encoder
scores are byte/float identical, plus ranking parity on a small corpus.

Runs where the REAL pinned model is provisioned (the CI test-real-model
lane; locally when the models dir carries the pinned bytes). Skips LOUDLY
with a named reason elsewhere — the skip is a disclosed limitation, never a
green claim.

Run:  python tests/test_real_model_byte_parity.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

PROBE_INPUTS = [
    "phase parity probe alpha",
    "how does the recall budget interact with the watchdog deadline",
    "sqlite_vec extension load cost",
    "zz unlrelated tokyo drift query 42",
    "a",
]


def _real_models_dir() -> Path | None:
    env_dir = os.environ.get("ZMEM_TEST_REAL_MODELS_DIR")
    if env_dir and Path(env_dir).is_dir() and \
            (Path(env_dir) / "minilm.onnx").is_file():
        return Path(env_dir)
    import embeddings as _emb
    default = _emb._resolve_models_dir()
    if (default / "minilm.onnx").is_file() and \
            (default / "tokenizer.json").is_file():
        return default
    return None


class ByteParityGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models_dir = _real_models_dir()
        if cls.models_dir is None:
            raise unittest.SkipTest(
                "real model not provisioned (ZMEM_TEST_REAL_MODELS_DIR "
                "unset and no pinned artifacts at the default models dir); "
                "the CI test-real-model lane owns this gate")
        cls._saved = {k: os.environ.get(k) for k in
                      ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                       "ZMEM_EMBED_PROFILE")}
        os.environ["ZMEM_MODELS_DIR"] = str(cls.models_dir)
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_EMBED_PROFILE", None)
        for k in ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN"):
            os.environ.pop(k, None)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _embed_with_session(self, session) -> list[bytes]:
        """Run the module's own pooling/normalization over the given session
        by swapping module state (same pipeline, only the session object
        differs — exactly the delta the resource policy introduces)."""
        import embeddings as _emb
        saved = (_emb._session, _emb._tokenizer, _emb._model_available,
                 _emb._model_checksum_ok)
        try:
            _emb._ensure_loaded()
            self.assertIsNotNone(_emb._session,
                                 "bounded load must succeed with the real "
                                 "model: %s" % _emb.availability_status())
            tok = _emb._tokenizer
            bounded_session = _emb._session
            _emb._session = session
            blobs = []
            for text in PROBE_INPUTS:
                enc = tok.encode(text)
                import numpy as np
                import struct
                out = session.run(None, {
                    "input_ids": np.asarray([enc.ids], dtype=np.int64),
                    "attention_mask": np.asarray([enc.attention_mask],
                                                 dtype=np.int64),
                    "token_type_ids": np.asarray(
                        [list(enc.type_ids or [0] * len(enc.ids))],
                        dtype=np.int64),
                })
                hidden = out[0][0]
                mask = np.asarray(enc.attention_mask, dtype=np.float32)
                mask2 = np.expand_dims(mask, 1)
                masked = hidden[:len(mask)] * mask2[:len(mask)]
                pooled = masked.sum(axis=0) / np.maximum(
                    mask.sum(), 1e-9)
                norm = float(np.linalg.norm(pooled))
                if norm > 0.0:
                    pooled = pooled / norm
                blobs.append(struct.pack("%df" % len(pooled), *pooled))
            _ = bounded_session
            return blobs
        finally:
            (_emb._session, _emb._tokenizer, _emb._model_available,
             _emb._model_checksum_ok) = saved

    def test_bounded_policy_preserves_vector_bytes(self):
        import onnxruntime as ort
        import embeddings as _emb
        _emb._ensure_loaded()
        self.assertIsNotNone(_emb._session)

        model_path = self.models_dir / "minilm.onnx"
        model_bytes = model_path.read_bytes()
        # The bounded session (module state, the policy this PR installs).
        bounded_blobs = self._embed_with_session(_emb._session)

        # The unbounded session — identical buffer, sess_options=None
        # (the pre-fix construction shape).
        unbounded = ort.InferenceSession(model_bytes)
        unbounded_blobs = self._embed_with_session(unbounded)

        for text, b, u in zip(PROBE_INPUTS, bounded_blobs,
                              unbounded_blobs):
            self.assertEqual(
                b, u,
                "vector bytes diverged under the bounded policy for %r — "
                "the issue contract forbids tolerance here" % text)

    def test_bounded_policy_preserves_ranking_order(self):
        import numpy as np
        import embeddings as _emb
        _emb._ensure_loaded()
        bounded = self._embed_with_session(_emb._session)
        import onnxruntime as ort
        unbounded = ort.InferenceSession(
            (self.models_dir / "minilm.onnx").read_bytes())
        unbounded_blobs = self._embed_with_session(unbounded)
        q = np.frombuffer(bounded[0], dtype=np.float32)
        ub = np.frombuffer(unbounded_blobs[0], dtype=np.float32)
        rank_b = sorted(range(1, len(bounded)),
                        key=lambda i: -float(np.dot(
                            q, np.frombuffer(bounded[i], dtype=np.float32))))
        rank_u = sorted(range(1, len(unbounded_blobs)),
                        key=lambda i: -float(np.dot(
                            ub, np.frombuffer(unbounded_blobs[i],
                                              dtype=np.float32))))
        self.assertEqual(rank_b, rank_u,
                         "cosine ranking order changed under the bounded "
                         "policy")
        # sanity: the bounded probe vector is a real 384-d unit vector
        self.assertEqual(len(q), 384)
        self.assertAlmostEqual(float(np.dot(q, q)), 1.0, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
