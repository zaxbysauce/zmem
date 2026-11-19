#!/usr/bin/env python
"""Issue #473 AC3: the cross-encoder session applies the SAME bounded
SessionOptions policy as the embedding session.

RED at base (the spec file is absent — frozen C3 wrapper prints
NEW_SURFACE_ABSENT); GREEN after the fix: ``cross_encoder._local_scorer``
constructs its ONNX session with the options from the ONE shared resolver
(``embeddings._resource_options``), so the two runtimes can never drift.

Run:  python tests/test_cross_encoder_resource_caps.py [substring ...]
      (positional substrings select tests; zero matched = failure)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import embeddings  # noqa: E402
from storelib import cross_encoder as ce  # noqa: E402


class _StubSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None
        self.execution_mode = None
        self.config_entries: list[tuple[str, str]] = []

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries.append((key, value))


class _CEStubBase(unittest.TestCase):
    """Shared fixture: profile pins patched to fixture bytes, stub
    onnxruntime (WITH SessionOptions + a recording InferenceSession) and
    tokenizers injected via sys.modules, models dir pinned to scratch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-cecaps-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_names = ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                     "ZMEM_DATA", "ZMEM_CROSS_ENCODER_MODEL",
                     "ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                     "ZMEM_ORT_DISABLE_SPIN")
        self._saved_env = {n: os.environ.get(n) for n in env_names}
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_DATA"] = str(self.tmp / "data")
        os.environ.pop("ZMEM_CROSS_ENCODER_MODEL", None)
        for k in ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN"):
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)

        import cross_encoder_profiles as profiles
        self._profiles = profiles
        self._entry = profiles.PROFILES[profiles.DEFAULT_PROFILE]
        self._saved_pins = (self._entry["sha256"],
                            self._entry.get("tokenizer_sha256"))

        self.model_bytes = b"ce-caps-fixture-model"
        self.tok_bytes = b'{"ce_caps": "fixture"}'
        models = self.tmp / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "mini_pair_scorer.onnx").write_bytes(self.model_bytes)
        (models / "tokenizer.json").write_bytes(self.tok_bytes)
        self._entry["sha256"] = hashlib.sha256(self.model_bytes).hexdigest()
        self._entry["tokenizer_sha256"] = hashlib.sha256(
            self.tok_bytes).hexdigest()

        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.SessionOptions = _StubSessionOptions

        class _Recorder:
            calls: list[dict] = []

            def __init__(self, model_bytes, sess_options=None):
                _Recorder.calls.append({"model_bytes": model_bytes,
                                        "sess_options": sess_options})

            def run(self, _names, _feeds):
                return [[[0.0]]]

        fake_ort.InferenceSession = _Recorder

        class _ExecMode:
            ORT_SEQUENTIAL = "ORT_SEQUENTIAL"

        fake_ort.ExecutionMode = _ExecMode
        self.recorder = _Recorder

        fake_tok = types.ModuleType("tokenizers")

        class _Tok:
            def __init__(self, *a, **k):
                pass

            @staticmethod
            def from_buffer(_buf):
                return _Tok()

            def enable_padding(self, length=128):
                pass

            def enable_truncation(self, max_length=128):
                pass

            def encode(self, _q, _t):
                class _Enc:
                    ids = [1]
                    attention_mask = [1]

                return _Enc()

        fake_tok.Tokenizer = _Tok

        self._modules_backup = {k: sys.modules.get(k)
                                for k in ("onnxruntime", "tokenizers",
                                          "numpy")}
        fake_np = types.ModuleType("numpy")
        sys.modules["onnxruntime"] = fake_ort
        sys.modules["tokenizers"] = fake_tok
        sys.modules["numpy"] = fake_np
        self.addCleanup(self._restore)

    def _restore_env(self):
        for n, v in self._saved_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _restore(self):
        self._entry["sha256"], token = self._saved_pins
        self._entry["tokenizer_sha256"] = token
        ce._scorer_fn = None
        ce._SCORER_CACHE.clear()
        for k, v in self._modules_backup.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class CESessionCarriesCaps(_CEStubBase):
    """AC3: the cross-encoder construction receives bounded options."""

    def test_construction_receives_shared_resolver_options(self):
        ce._scorer_fn = None
        ce._SCORER_CACHE.clear()
        self.recorder.calls.clear()
        scorer = ce._local_scorer()
        self.assertIsNotNone(scorer)
        self.assertEqual(len(self.recorder.calls), 1)
        opts = self.recorder.calls[0]["sess_options"]
        self.assertIsNotNone(
            opts, "the shared bounded SessionOptions must reach the "
                  "cross-encoder session")
        self.assertEqual(opts.intra_op_num_threads,
                         embeddings._default_intra_threads())
        self.assertEqual(opts.inter_op_num_threads, 1)
        spin_keys = sorted(k for k, _ in opts.config_entries)
        self.assertEqual(spin_keys,
                         ["session.inter_op.allow_spinning",
                          "session.intra_op.allow_spinning"])

    def test_env_override_reaches_cross_encoder_too(self):
        # ONE resolver: an override seen by the embedding session must be the
        # one the cross-encoder session gets (loader parity).
        os.environ["ZMEM_ORT_INTRA_THREADS"] = "2"
        ce._scorer_fn = None
        ce._SCORER_CACHE.clear()
        self.recorder.calls.clear()
        scorer = ce._local_scorer()
        self.assertIsNotNone(scorer)
        opts = self.recorder.calls[0]["sess_options"]
        self.assertEqual(opts.intra_op_num_threads, 2)

    def test_invalid_values_fall_back_in_ce_construction(self):
        # `invalid_values` selector leg: a garbage override must not leak an
        # unbounded/huge value into the CE session.
        os.environ["ZMEM_ORT_INTRA_THREADS"] = "banana"
        ce._scorer_fn = None
        ce._SCORER_CACHE.clear()
        self.recorder.calls.clear()
        scorer = ce._local_scorer()
        self.assertIsNotNone(scorer)
        opts = self.recorder.calls[0]["sess_options"]
        self.assertGreaterEqual(opts.intra_op_num_threads, 1)
        self.assertLessEqual(opts.intra_op_num_threads, 4)


def _run(selectors: list[str]) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (CESessionCarriesCaps,):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    if selectors:
        filtered = unittest.TestSuite()
        for test in suite:
            if any(sel in test.id() for sel in selectors):
                filtered.addTest(test)
        if filtered.countTestCases() == 0:
            print("SELECTOR-ERROR: %r matched zero tests" % (selectors,),
                  file=sys.stderr)
            return 3
        suite = filtered
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run([a for a in sys.argv[1:] if not a.startswith("-")]))
