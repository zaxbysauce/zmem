#!/usr/bin/env python
"""Issue #473 AC1/AC2: bounded ONNX resource policy for the embedding session.

RED at base (the spec file is absent — the frozen C1/C2 wrappers print
NEW_SURFACE_ABSENT); GREEN after the fix: the embedding loader constructs its
ONNX session with an explicit ``SessionOptions`` carrying bounded thread
counts and an execution-spin policy derived from validated configuration,
invalid overrides fall back to the documented defaults and are reported in
the availability diagnostic instead of crashing or silently running
unbounded.

Run:  python tests/test_embedding_resource_caps.py [substring ...]
      (plain runner; positional substrings select test methods; a selector
      that matches ZERO tests fails the run — a vacuous pass is impossible)
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

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


class _StubSessionOptions:
    """Real-shaped SessionOptions stand-in: attribute-settable, spin-key
    recorder. Mirrors the surface embeddings._resource_options touches so the
    resolver is testable on CI boxes without onnxruntime."""

    def __init__(self):
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None
        self.execution_mode = None
        self.config_entries: list[tuple[str, str]] = []

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries.append((key, value))


def _stub_ort_with_recorder():
    """Build a fake onnxruntime module whose SessionOptions is the stub and
    whose InferenceSession records the sess_options kwarg."""
    fake = types.ModuleType("onnxruntime")

    class _Recorder:
        calls: list[dict] = []

        def __init__(self, model_bytes, sess_options=None):
            _Recorder.calls.append({"model_bytes": model_bytes,
                                    "sess_options": sess_options})

    fake.SessionOptions = _StubSessionOptions
    fake.InferenceSession = _Recorder

    class _ExecMode:
        ORT_SEQUENTIAL = "ORT_SEQUENTIAL"

    fake.ExecutionMode = _ExecMode
    return fake, _Recorder


class DefaultPolicyBounded(unittest.TestCase):
    """AC1: the resolved policy is bounded by default (never the ORT default
    of unbounded pools)."""

    def test_default_policy_is_bounded(self):
        saved = {k: os.environ.get(k) for k in
                 ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            pol = embeddings.resolve_resource_policy()
            cpus = os.cpu_count() or 1
            self.assertEqual(pol["intra_threads"], max(1, min(4, cpus)))
            self.assertLessEqual(pol["intra_threads"], 4)
            self.assertEqual(pol["inter_threads"], 1)
            self.assertTrue(pol["sequential"])
            self.assertTrue(pol["disable_spin"])
            self.assertEqual(pol["invalid"], [])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class EnvOverridesApplied(unittest.TestCase):
    """AC1: validated env overrides reach the constructed options."""

    def test_env_overrides_reach_options(self):
        fake_ort, _ = _stub_ort_with_recorder()
        saved = {k: os.environ.get(k) for k in
                 ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN")}
        try:
            os.environ["ZMEM_ORT_INTRA_THREADS"] = "2"
            os.environ["ZMEM_ORT_INTER_THREADS"] = "3"
            os.environ["ZMEM_ORT_DISABLE_SPIN"] = "0"
            opts = embeddings._resource_options(fake_ort)
            self.assertIsNotNone(opts)
            self.assertEqual(opts.intra_op_num_threads, 2)
            self.assertEqual(opts.inter_op_num_threads, 3)
            # disable_spin=0 -> no spin keys written
            self.assertEqual(
                [k for k, _ in opts.config_entries
                 if "allow_spinning" in k], [])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_spin_keys_both_written_when_disabled(self):
        fake_ort, _ = _stub_ort_with_recorder()
        saved = os.environ.get("ZMEM_ORT_DISABLE_SPIN")
        try:
            os.environ.pop("ZMEM_ORT_DISABLE_SPIN", None)
            opts = embeddings._resource_options(fake_ort)
            keys = sorted(k for k, _ in opts.config_entries)
            self.assertEqual(keys, ["session.inter_op.allow_spinning",
                                    "session.intra_op.allow_spinning"])
            values = {v for _, v in opts.config_entries}
            self.assertEqual(values, {"0"})
        finally:
            if saved is None:
                os.environ.pop("ZMEM_ORT_DISABLE_SPIN", None)
            else:
                os.environ["ZMEM_ORT_DISABLE_SPIN"] = saved


class InvalidValues(unittest.TestCase):
    """AC2 (`invalid_values` selector): invalid overrides fall back to the
    documented defaults and are REPORTED — never a crash, never silently
    unbounded."""

    ENV_NAMES = ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                 "ZMEM_ORT_DISABLE_SPIN")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_NAMES}
        # review TI-001: save/restore the lazy module flag (a sibling suite
        # sharing this process must not inherit our reset)
        self._saved_model_available = embeddings._model_available
        embeddings._model_available = None
        self.addCleanup(self._restore_module_state)

    def _restore_module_state(self):
        embeddings._model_available = self._saved_model_available

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_invalid_values_non_numeric_intra(self):
        os.environ["ZMEM_ORT_INTRA_THREADS"] = "banana"
        pol = embeddings.resolve_resource_policy()
        self.assertEqual(pol["intra_threads"],
                         embeddings._default_intra_threads())
        self.assertIn(["ZMEM_ORT_INTRA_THREADS", "banana"], pol["invalid"])

    def test_invalid_values_zero_and_negative(self):
        for raw in ("0", "-3"):
            os.environ["ZMEM_ORT_INTRA_THREADS"] = raw
            pol = embeddings.resolve_resource_policy()
            self.assertGreaterEqual(pol["intra_threads"], 1)
            self.assertIn(["ZMEM_ORT_INTRA_THREADS", raw], pol["invalid"])

    def test_invalid_values_absurd_cap(self):
        # "unbounded via a huge override" must be refused, not honored.
        os.environ["ZMEM_ORT_INTER_THREADS"] = "1000000"
        pol = embeddings.resolve_resource_policy()
        self.assertEqual(pol["inter_threads"], 1)
        self.assertIn(["ZMEM_ORT_INTER_THREADS", "1000000"], pol["invalid"])

    def test_invalid_values_non_boolean_spin(self):
        os.environ["ZMEM_ORT_DISABLE_SPIN"] = "maybe"
        pol = embeddings.resolve_resource_policy()
        self.assertTrue(pol["disable_spin"])
        self.assertIn(["ZMEM_ORT_DISABLE_SPIN", "maybe"], pol["invalid"])

    def test_invalid_values_availability_diagnostic(self):
        # availability_status() must NOT raise and must carry the invalid
        # pair in resource_options.invalid_overrides.
        os.environ["ZMEM_ORT_INTRA_THREADS"] = "banana"
        st = embeddings.availability_status()
        self.assertIn("resource_options", st)
        ro = st["resource_options"]
        self.assertIn(["ZMEM_ORT_INTRA_THREADS", "banana"],
                      ro["invalid_overrides"])
        # the fallback is the bounded default, not a crash sentinel
        self.assertLessEqual(ro["intra_op_num_threads"], 4)

    def test_invalid_values_never_raises(self):
        for raw in ("", "  ", "1.5", "None", "true", "0x10", "١٢٣"):
            os.environ["ZMEM_ORT_INTRA_THREADS"] = raw
            pol = embeddings.resolve_resource_policy()
            self.assertGreaterEqual(pol["intra_threads"], 1)
            self.assertLessEqual(pol["intra_threads"], 4)


class EnsureLoadedPassesOptions(unittest.TestCase):
    """AC1: the load path hands the bounded options to InferenceSession."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-caps-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_names = ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                     "ZMEM_DATA", "ZMEM_EMBED_PROFILE")
        self._saved_env = {n: os.environ.get(n) for n in env_names}
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_DATA"] = str(self.tmp / "data")
        os.environ.pop("ZMEM_EMBED_PROFILE", None)
        for k in ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN"):
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for n, v in self._saved_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def test_construction_receives_bounded_options(self):
        import hashlib
        model_bytes = b"caps-fixture-model-bytes"
        tok_bytes = b'{"caps": "fixture"}'
        models = self.tmp / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "minilm.onnx").write_bytes(model_bytes)
        (models / "tokenizer.json").write_bytes(tok_bytes)
        saved_model_sha = embeddings._MODEL_SHA256
        saved_tok_sha = embeddings._TOKENIZER_SHA256

        fake_tok = types.ModuleType("tokenizers")

        class _Tok:
            def __init__(self, *a, **k):
                pass

            @staticmethod
            def from_buffer(buf):
                return _Tok()

            def enable_padding(self, length=128):
                pass

            def enable_truncation(self, max_length=128):
                pass

        fake_tok.Tokenizer = _Tok
        fake_np = types.ModuleType("numpy")
        fake_ort, recorder = _stub_ort_with_recorder()

        module_backup = {k: sys.modules.get(k)
                         for k in ("onnxruntime", "tokenizers", "numpy")}
        old_state = (embeddings._session, embeddings._tokenizer,
                     embeddings._model_available, embeddings._model_checksum_ok,
                     embeddings._model_load_failed,
                     embeddings._checksum_failed_artifact)
        try:
            embeddings._MODEL_SHA256 = hashlib.sha256(model_bytes).hexdigest()
            embeddings._TOKENIZER_SHA256 = hashlib.sha256(tok_bytes).hexdigest()
            embeddings._session = None
            embeddings._tokenizer = None
            embeddings._model_available = None
            embeddings._model_checksum_ok = None
            embeddings._model_load_failed = False
            sys.modules["onnxruntime"] = fake_ort
            sys.modules["tokenizers"] = fake_tok
            sys.modules["numpy"] = fake_np

            embeddings._ensure_loaded()

            self.assertEqual(len(recorder.calls), 1,
                             "exactly one InferenceSession construction")
            opts = recorder.calls[0]["sess_options"]
            self.assertIsNotNone(opts,
                                 "the bounded SessionOptions must be passed")
            self.assertEqual(recorder.calls[0]["model_bytes"], model_bytes)
            self.assertGreaterEqual(opts.intra_op_num_threads, 1)
            self.assertLessEqual(opts.intra_op_num_threads, 4)
            self.assertEqual(opts.inter_op_num_threads, 1)
            spin_keys = sorted(k for k, _ in opts.config_entries)
            self.assertEqual(spin_keys,
                             ["session.inter_op.allow_spinning",
                              "session.intra_op.allow_spinning"])
        finally:
            embeddings._MODEL_SHA256 = saved_model_sha
            embeddings._TOKENIZER_SHA256 = saved_tok_sha
            (embeddings._session, embeddings._tokenizer,
             embeddings._model_available, embeddings._model_checksum_ok,
             embeddings._model_load_failed,
             embeddings._checksum_failed_artifact) = old_state
            for k, v in module_backup.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class RealRuntimeLeg(unittest.TestCase):
    """Where the real onnxruntime is importable, the resolver must produce a
    REAL SessionOptions with the bounded values (guards against a stub-only
    green). Skips loudly when the runtime is absent (CI base lane)."""

    def test_real_session_options_carry_caps(self):
        try:
            import onnxruntime as real_ort  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime not installed (real-model lane "
                          "covers this leg)")
        for k in ("ZMEM_ORT_INTRA_THREADS", "ZMEM_ORT_INTER_THREADS",
                  "ZMEM_ORT_DISABLE_SPIN"):
            os.environ.pop(k, None)
        opts = embeddings._resource_options(real_ort)
        self.assertIsNotNone(opts)
        self.assertGreaterEqual(opts.intra_op_num_threads, 1)
        self.assertLessEqual(opts.intra_op_num_threads, 4)
        self.assertEqual(opts.inter_op_num_threads, 1)
        self.assertEqual(str(opts.execution_mode),
                         "ExecutionMode.ORT_SEQUENTIAL")


def _run(selectors: list[str]) -> int:
    """Plain runner with the frozen selector contract: positional substrings
    select test methods (any test whose full id contains a selector runs);
    ZERO selected tests is a FAILURE, never a vacuous pass."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (DefaultPolicyBounded, EnvOverridesApplied, InvalidValues,
                EnsureLoadedPassesOptions, RealRuntimeLeg):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    if selectors:
        filtered = unittest.TestSuite()
        for test in suite:
            tid = test.id()
            if any(sel in tid for sel in selectors):
                filtered.addTest(test)
        selected = filtered.countTestCases()
        if selected == 0:
            print("SELECTOR-ERROR: %r matched zero tests" % (selectors,),
                  file=sys.stderr)
            return 3
        suite = filtered
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run([a for a in sys.argv[1:] if not a.startswith("-")]))
