#!/usr/bin/env python
"""Issue #63, 8.1: checksum pin hardening + operator-visible mismatch note.

Run:  python tests/test_embedder_checksum.py   (no pytest; house convention)

The trust-root mechanics under test:
- verify_checksum true/false semantics on small files (the pin is read at
  CALL time via the module attribute, which is what tests monkeypatch — that
  contract is pinned here too);
- the LOAD path refuses a tampered model BEFORE any ONNX/tokenizer parsing:
  garbage bytes with a valid-looking tokenizer must produce NO crash, an
  unavailable embed_text, and reason=model_checksum_mismatch;
- matching bytes pass the gate (ordering proof: checksum fires first);
- doctor surfaces the verdict as JSON with the mismatching artifact named
  (`checksum_artifact`), and its recommendation names that artifact's
  restore path without inventing an unverified-load escape hatch.
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import embeddings  # noqa: E402
import embed_profiles as ep  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ChecksumSemantics(unittest.TestCase):
    def setUp(self):
        # module-global hygiene regardless of alphabetical neighbors (_N3)
        embeddings._model_available = None
        embeddings._model_checksum_ok = None
        embeddings._model_load_failed = False

        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-cksum-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_match_true_mismatch_false_missing_false(self):
        f = self.tmp / "m.onnx"
        f.write_bytes(b"fake-onnx-payload")
        good = _sha(b"fake-onnx-payload")
        self.assertTrue(embeddings.verify_checksum(f, good))
        self.assertFalse(embeddings.verify_checksum(f, "0" * 64))
        self.assertFalse(
            embeddings.verify_checksum(self.tmp / "absent.onnx", good)
        )

    def test_default_expected_is_call_time_module_attr(self):
        """The documented monkeypatch seam (#36 M15): expected=None resolves
        _MODEL_SHA256 when verify_checksum RUNS, not at def time."""
        f = self.tmp / "m.onnx"
        f.write_bytes(b"x")
        orig = embeddings._MODEL_SHA256
        try:
            embeddings._MODEL_SHA256 = _sha(b"x")
            self.assertTrue(embeddings.verify_checksum(f))
            # flip AFTER binding expectations — still the call-time value
            embeddings._MODEL_SHA256 = "f" * 64
            self.assertFalse(embeddings.verify_checksum(f))
        finally:
            embeddings._MODEL_SHA256 = orig


def _provision(models_dir: Path, onnx_bytes: bytes, sha_pin: str):
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "minilm.onnx").write_bytes(onnx_bytes)
    tok_bytes = b"[]"  # deliberately broken for the load stage
    (models_dir / "tokenizer.json").write_bytes(tok_bytes)
    import importlib

    saved = embeddings._MODEL_SHA256
    saved_tok = embeddings._TOKENIZER_SHA256
    embeddings._MODEL_SHA256 = sha_pin
    # #418: the tokenizer pin is verified BEFORE construction now — patch it
    # to whatever bytes we wrote so the pre-existing tests exercise the LOAD
    # stage exactly as they did before the tokenizer gate existed.
    embeddings._TOKENIZER_SHA256 = _sha(tok_bytes)
    # Full lazy-global reset — these survive across tests otherwise.
    embeddings._model_available = None
    embeddings._model_checksum_ok = None
    embeddings._model_load_failed = False
    embeddings._checksum_failed_artifact = None
    embeddings._session = None
    embeddings._tokenizer = None

    def restore():
        embeddings._MODEL_SHA256 = saved
        embeddings._TOKENIZER_SHA256 = saved_tok
        embeddings._model_available = None
        embeddings._model_checksum_ok = None
        embeddings._model_load_failed = False
        embeddings._checksum_failed_artifact = None
        embeddings._session = None
        embeddings._tokenizer = None
    return restore


class TamperedModelRefusal(unittest.TestCase):
    """Model-absent-CI-safe by construction (zax round-2 follow-up): CI has no
    onnxruntime/tokenizers/numpy, so the checksum gate inside _ensure_loaded
    was unreachable there and three assertions reported the wrong reason.
    Deterministic STUB third-party modules are injected so the ordering proof
    (checksum fires BEFORE any parse/load) runs identically everywhere."""

    @staticmethod
    def expected_child_reason(has_runtime: bool) -> str:
        """Pure mapping used by every doctor-subprocess assertion: a runner
        WITH the third-party stack reaches the checksum gate (mismatch
        verdict observable); a dep-less runner honestly reports the import
        problem instead."""
        return ("model_checksum_mismatch" if has_runtime
                else "imports_missing")

    @classmethod
    def doctor_expected_reason(cls) -> str:
        """Host-truth default unless a subclass models another runner."""
        has_runtime = cls._host_has_runtime
        return cls.expected_child_reason(has_runtime)

    @classmethod
    def setUpClass(cls):
        import importlib.util

        def probe(name):
            try:
                return importlib.util.find_spec(name) is not None
            except (ValueError, ImportError):
                return False
        # resolve BEFORE setUp injects fake third-party modules — probes run
        # against the pristine interpreter state so host truth is accurate.
        # Stored under an explicit non-colliding name: subclasses may declare
        # their own `_host_has_runtime` to model other runners.
        if "_host_has_runtime" not in cls.__dict__:
            cls._host_has_runtime = all(
                probe(m)
                for m in ("onnxruntime", "tokenizers", "numpy"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-tamper-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_names = ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                     "ZMEM_DATA", ep.PROFILE_ENV)
        self._saved = {n: os.environ.get(n) for n in env_names}
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # PR-419 PRR-006/F-018: a degraded load writes the once-per-data-dir
        # sentinel through ZMEM_DATA — pin it to a scratch dir under this
        # test's temp root so the notice NEVER lands in the ambient data dir.
        os.environ["ZMEM_DATA"] = str(self.tmp / "data")
        os.environ.pop(ep.PROFILE_ENV, None)
        self.addCleanup(self._restore_env)
        self._inject_stub_modules()

    def _inject_stub_modules(self):
        """Minimal stand-ins so third-party imports succeed anywhere."""
        import types

        class _StubTokenizer:
            def __init__(self, *_a, **_k):
                pass

            @classmethod
            def from_file(cls, *_a, **_k):
                return cls()

            def enable_padding(self, length=128):
                pass

            def enable_truncation(self, max_length=128):
                pass

        fake_np = types.ModuleType("numpy")
        fake_ort = types.ModuleType("onnxruntime")
        fake_tok_mod = types.ModuleType("tokenizers")
        fake_tok_mod.Tokenizer = _StubTokenizer
        self._modules_backup = {k: sys.modules.get(k)
                                for k in ("onnxruntime", "tokenizers",
                                          "numpy")}
        sys.modules["onnxruntime"] = fake_ort
        sys.modules["tokenizers"] = fake_tok_mod
        sys.modules["numpy"] = fake_np
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        for k, v in self._modules_backup.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _restore_env(self):
        for n, v in self._saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def test_refuse_before_parse_on_mismatch(self):
        cleanup = _provision(
            self.tmp / "models",
            b"TAMPERED / WRONG BUILD BYTES",
            "a" * 64,
        )
        self.addCleanup(cleanup)
        embeddings._ensure_loaded()
        st = embeddings.availability_status()
        self.assertIsNone(embeddings._session)  # never constructed
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "model_checksum_mismatch")
        self.assertIs(st["checksum_ok"], False)
        self.assertIn("note", st)
        self.assertIsNone(embeddings.embed_text("anything"))

    def test_matching_gate_passes_ordering_proof(self):
        payload = b"CORRECT-BUILD-BYTES"
        cleanup = _provision(self.tmp / "models", payload, _sha(payload))
        self.addCleanup(cleanup)
        # Checksum PASSES (fires BEFORE load): the load itself then fails on
        # the deliberately-broken tokenizer and must be flagged distinctly.
        embeddings._ensure_loaded()
        st = embeddings.availability_status()
        self.assertEqual(st["checksum_ok"], True)
        self.assertNotEqual(st["reason"], "model_checksum_mismatch")

    def test_doctor_json_note_and_recommendation(self):
        cleanup = _provision(
            self.tmp / "models",
            b"WRONG-BUILD",
            "b" * 64,
        )
        self.addCleanup(cleanup)
        store_dir = self.tmp / "data"
        store_dir.mkdir()
        env = dict(os.environ)
        env["ZMEM_DATA"] = str(store_dir)
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "doctor.py"), "--format", "json"],
            capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS),
        )
        if not r.stdout.strip():
            self.fail(
                "doctor subprocess wrote no JSON to stdout: "
                f"rc={r.returncode} stderr={r.stderr[-800:]!r} "
                f"stdout-head={r.stdout[:200]!r}"
            )
        report = json.loads(r.stdout)
        emb = next(
            c for c in report["checks"] if c["id"] == "embeddings"
        )
        expected = type(self).expected_child_reason(
            getattr(type(self), "_host_has_runtime", True))
        if expected == "imports_missing":
            # Dep-less runner: the doctor CHILD cannot reach its checksum gate,
            # so it must honestly report the imports problem. The ordering
            # logic itself is covered by the stubbed tests above.
            self.assertEqual(emb["details"]["reason"], "imports_missing")
            return
        self.assertEqual(expected, "model_checksum_mismatch")
        self.assertEqual(emb["details"]["reason"], "model_checksum_mismatch")
        self.assertIs(emb["details"]["checksum_ok"], False)
        # #418: doctor's child-side probe hashes against the REAL pins (the
        # parent's test-patched pins do not cross the process boundary), so
        # the scratch minilm.onnx fails the real model pin FIRST and the
        # synthesized status names the artifact — the remediation keeps the
        # model wording (restore routes: release asset / ZMEM_MODEL_URL).
        self.assertEqual(emb["details"]["checksum_artifact"], "model")
        recs = "\n".join(report["recommendations"])
        self.assertIn("minilm.onnx FAILED its checksum pin", recs)
        self.assertNotIn("tokenizer.json FAILED", recs)


class TamperedModelRefusalDeplessDoctorBranch(TamperedModelRefusal):
    """Pins the dep-less CI branch of the doctor-subprocess expectations
    deterministically on every host. CI itself is the real instance of this
    variant; elsewhere the branch mapping is asserted as PURE LOGIC instead
    of dishonestly pretending a numpy-equipped local child lacks it."""

    _host_has_runtime = False  # model the CI runner explicitly

    @classmethod
    def doctor_expected_reason(cls):
        return cls.expected_child_reason(cls._host_has_runtime)

    def test_depless_mapping_is_pure_and_ci_faithful(self):
        self.assertEqual(self.expected_child_reason(True),
                         "model_checksum_mismatch")
        self.assertEqual(self.expected_child_reason(False),
                         "imports_missing")
        self.assertEqual(self.doctor_expected_reason(), "imports_missing")

    def test_doctor_json_note_and_recommendation(self):
        # The parent's subprocess expectations are host-truth-dependent; this
        # class covers the dep-less branch as pure logic above.
        raise unittest.SkipTest("dep-less mapping covered by "
                                "test_depless_mapping_is_pure_and_ci_faithful")


class TamperedTokenizerRefusal(unittest.TestCase):
    """#418 AC1: the minilm tokenizer is digest-pinned on the LOAD path from
    the same buffer the Tokenizer is constructed from. A tampered
    tokenizer.json refuses (fail-open degraded) with the artifact named for
    doctor; matching bytes construct via from_buffer, never from_file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-tokpin-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_names = ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                     "ZMEM_DATA", ep.PROFILE_ENV)
        self._saved = {n: os.environ.get(n) for n in env_names}
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # PR-419 PRR-006/F-018: degraded notices resolve through ZMEM_DATA —
        # point it at a fresh scratch dir under this temp root so the
        # sentinel never lands in the ambient data dir.
        os.environ["ZMEM_DATA"] = str(self.tmp / "data")
        os.environ.pop(ep.PROFILE_ENV, None)
        self.addCleanup(self._restore_env)
        import types

        good_model = b"CORRECT-MODEL-BYTES"

        class _StubTokenizer:
            constructed_from: list = []
            # F-004: record the EXACT buffer each construction receives so
            # the swap-after-verify test can prove construct-from-verified-
            # buffer (no verify-then-reread).
            received_buffers: list = []

            @classmethod
            def from_file(cls, *_a, **_k):
                cls.constructed_from.append("from_file")
                return cls()

            @classmethod
            def from_buffer(cls, buf):
                cls.constructed_from.append("from_buffer")
                cls.received_buffers.append(buf)
                return cls()

            def enable_padding(self, length=128):
                pass

            def enable_truncation(self, max_length=128):
                pass

        class _FakeSession:
            def __init__(self, model_bytes):
                assert isinstance(model_bytes, (bytes, bytearray))

        fake_np = types.ModuleType("numpy")
        fake_ort = types.ModuleType("onnxruntime")
        fake_ort.InferenceSession = _FakeSession
        fake_tok_mod = types.ModuleType("tokenizers")
        fake_tok_mod.Tokenizer = _StubTokenizer
        self._StubTokenizer = _StubTokenizer
        self._modules_backup = {k: sys.modules.get(k)
                                for k in ("onnxruntime", "tokenizers",
                                          "numpy")}
        sys.modules["onnxruntime"] = fake_ort
        sys.modules["tokenizers"] = fake_tok_mod
        sys.modules["numpy"] = fake_np
        self.addCleanup(self._restore_modules)

        # Model pin matches its bytes so the flow reaches the tokenizer gate.
        models = self.tmp / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "minilm.onnx").write_bytes(good_model)
        saved_model = embeddings._MODEL_SHA256
        saved_tok = embeddings._TOKENIZER_SHA256
        embeddings._MODEL_SHA256 = _sha(good_model)
        # Sub-tests overwrite the tokenizer file + pin.
        embeddings._model_available = None
        embeddings._model_checksum_ok = None
        embeddings._model_load_failed = False
        embeddings._checksum_failed_artifact = None
        embeddings._session = None
        embeddings._tokenizer = None

        def restore():
            embeddings._MODEL_SHA256 = saved_model
            embeddings._TOKENIZER_SHA256 = saved_tok
            embeddings._model_available = None
            embeddings._model_checksum_ok = None
            embeddings._model_load_failed = False
            embeddings._checksum_failed_artifact = None
            embeddings._session = None
            embeddings._tokenizer = None

        self.addCleanup(restore)

    def _restore_modules(self):
        for k, v in self._modules_backup.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _restore_env(self):
        for n, v in self._saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _write_tokenizer(self, payload: bytes, pin: str):
        (self.tmp / "models" / "tokenizer.json").write_bytes(payload)
        embeddings._TOKENIZER_SHA256 = pin
        embeddings._model_available = None
        embeddings._model_checksum_ok = None
        embeddings._model_load_failed = False
        embeddings._checksum_failed_artifact = None
        embeddings._session = None
        embeddings._tokenizer = None
        self._StubTokenizer.constructed_from = []
        self._StubTokenizer.received_buffers = []

    def test_tampered_tokenizer_refuses_fail_open(self):
        payload = b"WRONG-TOKENIZER-BYTES"
        self._write_tokenizer(payload, _sha(b"different-canonical-bytes"))
        embeddings._ensure_loaded()
        st = embeddings.availability_status()
        self.assertIsNone(embeddings._session)  # never constructed
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "model_checksum_mismatch")
        self.assertIs(st["checksum_ok"], False)
        # #418: the mismatching artifact is named — doctor text picks the
        # tokenizer remediation, never blaming minilm.onnx.
        self.assertEqual(st["checksum_artifact"], "tokenizer")
        self.assertEqual(self._StubTokenizer.constructed_from, [])
        self.assertIsNone(embeddings.embed_text("anything"))

    def test_matching_tokenizer_constructs_from_buffer(self):
        payload = b"CORRECT-TOKENIZER-BYTES"
        self._write_tokenizer(payload, _sha(payload))
        embeddings._ensure_loaded()
        st = embeddings.availability_status()
        self.assertEqual(st["reason"], "ok")
        self.assertEqual(st["checksum_ok"], True)
        self.assertEqual(self._StubTokenizer.constructed_from, ["from_buffer"])
        self.assertEqual(self._StubTokenizer.received_buffers, [payload])
        self.assertIsNotNone(embeddings._tokenizer)

    def test_constructs_from_verified_buffer_not_a_reread(self):
        # PR-419 F-004, falsifiable shape (final-critic round: the earlier
        # swap-after-load shape passed trivially under the exact mutation —
        # a loader that verifies buffer A then RE-READS before constructing
        # receives identical bytes from a stable file). The read seam is
        # mocked so the TOKENIZER's SECOND read returns different bytes:
        # read order in _ensure_loaded is [model, tokenizer] on the clean
        # path; a verify-then-reread loader consumes a third read (T2) and
        # constructs from it — this test FAILS under that mutation because
        # received_buffers becomes [T2], not [P1].
        from unittest import mock

        p1 = b"VERIFIED-TOKENIZER-PAYLOAD-P1"
        t2 = b"TAMPERED-SECOND-READ-T2"
        self._write_tokenizer(p1, _sha(p1))
        tok_file = self.tmp / "models" / "tokenizer.json"
        real_read_bytes = Path.read_bytes
        tok_pin = embeddings._TOKENIZER_SHA256

        def seam(path_self, *a, **k):
            data = real_read_bytes(path_self)
            # Intercept ONLY the tokenizer artifact's read (the model read
            # passes through): first tokenizer read returns the on-disk P1;
            # a second tokenizer read (the verify-then-reread mutation)
            # returns T2, whose digest mismatches nothing here — the bytes
            # reach from_buffer directly if the loader rereads.
            if path_self == tok_file and seam.calls:
                seam.calls.pop()
                return t2
            if path_self == tok_file:
                seam.calls.append(1)
            return data

        seam.calls = []
        with mock.patch.object(Path, "read_bytes", seam):
            embeddings._ensure_loaded()
        self.assertIsNotNone(embeddings._tokenizer)
        self.assertEqual(
            self._StubTokenizer.received_buffers, [p1],
            "from_buffer must receive EXACTLY the digest-verified P1 bytes; "
            "[T2] here means the loader re-read the file after verifying "
            "(the F-004 verify-then-reread window)")
        self.assertEqual(
            len(seam.calls), 1,
            "exactly one tokenizer read expected on the clean path "
            "(a verify-then-reread loader would consume the marker and "
            "construct from T2, failing the received_buffers assertion)")

    def test_unreadable_tokenizer_fails_open_with_artifact(self):
        # #418 OSError arm: a PRESENT tokenizer.json whose READ raises
        # (locked / permission-denied) must fail open — nothing raises out
        # of _ensure_loaded — with checksum_artifact="tokenizer". Mechanism
        # note: pathlib's read_bytes goes through io.open, so builtins.open
        # patching would NOT intercept it — Path.read_bytes is patched for
        # the one path instead. A DIRECTORY named tokenizer.json cannot
        # reach this arm (see the next test) because Path.is_file() is
        # False and the presence ladders classify it first.
        from unittest import mock

        p1 = b"TOKENIZER-BYTES-PRESENT-ON-DISK"
        self._write_tokenizer(p1, _sha(p1))
        tok_file = self.tmp / "models" / "tokenizer.json"
        real_read_bytes = Path.read_bytes

        def _locked_read_bytes(path_self):
            if str(path_self) == str(tok_file):
                raise PermissionError(13, "locked by another process")
            return real_read_bytes(path_self)

        with mock.patch.object(Path, "read_bytes", _locked_read_bytes):
            embeddings._ensure_loaded()  # must NOT raise
        st = embeddings.availability_status()
        self.assertIsNone(embeddings._session)  # never constructed
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "model_checksum_mismatch")
        self.assertIs(st["checksum_ok"], False)
        self.assertEqual(st["checksum_artifact"], "tokenizer")
        # The sentinel names the artifact too, and lands in the SCRATCH
        # ZMEM_DATA (never the ambient data dir).
        notice = Path(os.environ["ZMEM_DATA"]) / "embedding-degraded.notice"
        self.assertTrue(notice.is_file(), "degraded notice must be written")
        notice_text = notice.read_text(encoding="utf-8")
        # The sentinel keeps the #131 four-line BYTE CONTRACT (reason /
        # models_dir / profile / timestamp) — artifact attribution rides the
        # availability dict (asserted above) and the CLI JSON / write
        # warning, deliberately NOT the sentinel bytes.
        self.assertEqual(notice_text.splitlines()[0],
                         "reason=model_checksum_mismatch")
        self.assertEqual(len(notice_text.splitlines()), 4,
                         "#131 byte contract: exactly 4 lines")

    def test_tokenizer_replaced_by_directory_fails_open(self):
        # The on-disk degenerate shape from the same review round: the
        # tokenizer.json FILE replaced by a DIRECTORY of that name.
        # _ensure_loaded must not raise. HONEST classification: a directory
        # fails Path.is_file(), so both _check_available and
        # availability_status's presence ladder report it as
        # tokenizer_missing — the read-OSError arm (covered above via a
        # present-but-unreadable file) is unreachable by this shape by
        # construction.
        import shutil as _shutil

        p1 = b"TOKENIZER-BYTES-BEFORE-DIRECTORY-SWAP"
        self._write_tokenizer(p1, _sha(p1))
        tok_file = self.tmp / "models" / "tokenizer.json"
        tok_file.unlink()
        tok_file.mkdir()
        self.addCleanup(_shutil.rmtree, tok_file, True)
        embeddings._ensure_loaded()  # must NOT raise
        st = embeddings.availability_status()
        self.assertIsNone(embeddings._session)
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "tokenizer_missing")
        self.assertIs(st.get("checksum_artifact"), None)


class DoctorArtifactAccurateRecommendations(unittest.TestCase):
    """#418: the model_checksum_mismatch remediation names the artifact that
    actually mismatched (checksum_artifact); absent checksum_artifact —
    doctor's own synthesized probe status — keeps the minilm.onnx wording."""

    def _recs(self, artifact):
        import doctor  # noqa: E402

        details = {
            "available": False,
            "reason": "model_checksum_mismatch",
            "checksum_ok": False,
            "note": "note-text",
        }
        if artifact is not None:
            details["checksum_artifact"] = artifact
        checks = [{
            "id": "embeddings",
            "status": "warn",  # the embeddings remediation notes gate on warn
            "summary": "s",
            "details": details,
        }]
        return "\n".join(doctor._recommendations(checks))

    def test_tokenizer_mismatch_names_tokenizer(self):
        recs = self._recs("tokenizer")
        self.assertIn("tokenizer.json FAILED its SHA-256 pin", recs)
        self.assertNotIn("minilm.onnx FAILED", recs)
        # #418 tokenizer-scoped remediation (PR-419 review): the restore
        # routes are the plugin-bundled copy and the upstream Xenova
        # tokenizer.json — no model provenance (MINILM_NOTES) interpolation.
        self.assertIn("bundled with your zmem plugin install", recs)
        self.assertIn(
            "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/"
            "main/tokenizer.json",
            recs,
        )
        self.assertNotIn("model-minilm-bbd7b466", recs)

    def test_model_mismatch_keeps_model_wording(self):
        recs = self._recs("model")
        self.assertIn("minilm.onnx FAILED its checksum pin", recs)
        self.assertNotIn("tokenizer.json FAILED", recs)

    def test_absent_artifact_defaults_to_model_wording(self):
        # doctor's separately synthesized probe status (doctor.py ~:938-944)
        # sets no checksum_artifact — the pre-#418 sentence remains.
        recs = self._recs(None)
        self.assertIn("minilm.onnx FAILED its checksum pin", recs)
        self.assertNotIn("tokenizer.json FAILED", recs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
