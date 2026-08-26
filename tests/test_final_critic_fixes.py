#!/usr/bin/env python
"""Final-critic round (issue #63): production-path proofs.

Run:  python tests/test_final_critic_fixes.py   (no pytest; house convention)

These close the three gaps the final challenge surfaced:
1. cross_encoder._local_scorer's production closure must actually execute —
   proven with fake onnxruntime/tokenizers/numpy modules so no real deps or
   model files are needed anywhere;
2. the ZMEM_EMBED_PROFILE=fake write-path banner fires on `store.py add`;
3. flagless reembed still repairs live rows whose embeddings exist but whose
   memory_vec went empty (the `_has_any_embedding` return regression).
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import embed_profiles as ep  # noqa: E402


class FakeLocalScorerExecutes(unittest.TestCase):
    """Drives _local_scorer end-to-end against MOCKED heavy modules."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-ceprod-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {k: os.environ.get(k) for k in
                      ("ZMEM_CROSS_ENCODER_MODEL", "ZMEM_CROSS_ENCODER")}
        # fabricate a model pair; the mocked loaders accept anything readable
        models = self.tmp / "models"
        models.mkdir()
        (models / "ranker.onnx").write_bytes(b"dummy")
        (models / "tokenizer.json").write_text("[]")

        import types

        def array(data, dtype=None):
            return data if isinstance(data, list) and isinstance(data[0], float) \
                else [[1, 0] for _ in data]

        fake_np = types.ModuleType("numpy")
        fake_np.array = lambda data, dtype=None: (
            list(data) if not (isinstance(data, list) and data and
                               isinstance(data[0], list)) else data)

        class FakeSession:
            def __init__(self, path):
                assert str(path).endswith("ranker.onnx")

            def run(self, _, inputs):
                ids = inputs["input_ids"]
                return [[[0.9 if i == 0 else 0.1] for i in row] for row in ids]

        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.InferenceSession = FakeSession

        class FakeEnc:
            ids = [7, 0]
            attention_mask = [1, 0]

        class FakeTok:
            def __init__(self, path):
                pass

            @classmethod
            def from_file(cls, path):
                return cls(path)

            def encode(self, text):
                return FakeEnc()

            def enable_padding(self, length):
                pass

            def enable_truncation(self, max_length):
                pass

        fake_tok_mod = types.ModuleType("tokenizers")
        fake_tok_mod.Tokenizer = FakeTok

        self._modules_backup = {k: sys.modules.get(k)
                                for k in ("numpy", "onnxruntime", "tokenizers")}
        sys.modules["numpy"] = fake_np
        sys.modules["onnxruntime"] = fake_ort
        sys.modules["tokenizers"] = fake_tok_mod
        self.addCleanup(self._restore_modules)
        os.environ["ZMEM_CROSS_ENCODER_MODEL"] = str(models / "ranker.onnx")
        os.environ["ZMEM_CROSS_ENCODER"] = "1"

    def _restore_modules(self):
        for k, v in self._modules_backup.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_production_scorer_scores_without_name_error(self):
        from storelib.cross_encoder import _local_scorer, maybe_rerank, set_scorer

        set_scorer(None)  # force the PRODUCTION path, not an injected stub
        try:
            fn = _local_scorer()
            self.assertIsNotNone(
                fn, "with file+mocked deps present, scorer must build")
            rows = [{"content": "b text"}, {"content": "a text"}]
            out = maybe_rerank("q", rows)
            self.assertEqual(len(out), 2)
            self.assertEqual({r["content"] for r in out},
                             {"a text", "b text"})
        finally:
            set_scorer(None)


class FakeProfileAddBanner(unittest.TestCase):
    def test_add_under_fake_emits_placeholder_warning(self):
        tmp = tempfile.mkdtemp(prefix="zmem-banner-")
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = {k: os.environ.get(k) for k in
                 ("ZMEM_STORE", ep.PROFILE_ENV, "ZMEM_DATA",
                  "ZMEM_MODEL_AUTODOWNLOAD")}
        os.environ["ZMEM_STORE"] = str(Path(tmp) / "store.sqlite")
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

        try:
            sys.path.insert(0, str(SCRIPTS))
            from storelib.schema import connect, _prepare_store
            conn = connect()
            _prepare_store(conn)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                from storelib.write import add_memory
                add_memory(conn, namespace="user:t", type_="fact",
                           content="banner probe", confidence=0.9)
            conn.close()
            self.assertIn("PLACEHOLDER vectors", err.getvalue(),
                          "write under fake profile must warn the operator")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class FlaglessVecRepair(unittest.TestCase):
    def test_backfill_repairs_empty_memory_vec(self):
        """Pre-existing latent bug pinned by the final critic: when all live
        rows ALREADY have embeddings but memory_vec holds none, flagless
        reembed must populate it — not claim nothing-to-do."""
        tmp = tempfile.mkdtemp(prefix="zmem-repair-")
        self.addCleanup(shutil.rmtree, tmp, True)
        saved_store = os.environ.get("ZMEM_STORE")
        saved_profile = os.environ.get(ep.PROFILE_ENV)
        os.environ["ZMEM_STORE"] = str(Path(tmp) / "store.sqlite")
        os.environ.pop(ep.PROFILE_ENV, None)
        sys.path.insert(0, str(SCRIPTS))
        for m in list(sys.modules):
            if m.startswith("storelib"):
                del sys.modules[m]
        try:
            from storelib.schema import connect, _prepare_store
            from storelib import recall as R
            conn = connect()
            _prepare_store(conn)
            blob = struct.pack("<384f", *([0.25] * 384))
            for i in range(3):
                conn.execute(
                    "INSERT INTO memory(id,namespace,type,content,"
                    "confidence,signal,valid_from,ingestion_ts,taint,"
                    "embedding,embedding_model) VALUES (?,'user:t','fact',?,"
                    "0.9,'test','2026-01-01T00:00:00Z',"
                    "'2026-01-01T00:00:00Z','trusted_internal',?,"
                    "'minilm-onnx')",
                    (f"rep{i}", f"repair row {i}", blob))
            conn.commit()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = R.reembed_embeddings(conn)
            self.assertEqual(rc, 0)
            self.assertIn("populated vec0 for 3", out.getvalue(),
                          out.getvalue())
            cnt = conn.execute(
                "SELECT COUNT(*) FROM memory_vec").fetchone()[0]
            self.assertEqual(cnt, 3)
        finally:
            if saved_store is None:
                os.environ.pop("ZMEM_STORE", None)
            else:
                os.environ["ZMEM_STORE"] = saved_store
            if saved_profile is None:
                os.environ.pop(ep.PROFILE_ENV, None)
            else:
                os.environ[ep.PROFILE_ENV] = saved_profile


if __name__ == "__main__":
    unittest.main(verbosity=2)
