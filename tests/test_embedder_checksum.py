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
- doctor surfaces the verdict as JSON with the Xenova-vs-sentence-transformers
  `note`, and its recommendation names the restore path without inventing an
  unverified-load escape hatch.
"""

from __future__ import annotations

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
    (models_dir / "tokenizer.json").write_text("[]")  # deliberately broken
    import importlib

    saved = embeddings._MODEL_SHA256
    embeddings._MODEL_SHA256 = sha_pin
    # Full lazy-global reset — these five survive across tests otherwise.
    embeddings._model_available = None
    embeddings._model_checksum_ok = None
    embeddings._model_load_failed = False
    embeddings._session = None
    embeddings._tokenizer = None

    def restore():
        embeddings._MODEL_SHA256 = saved
        embeddings._model_available = None
        embeddings._model_checksum_ok = None
        embeddings._model_load_failed = False
        embeddings._session = None
        embeddings._tokenizer = None
    return restore


class TamperedModelRefusal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-tamper-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env_names = ("ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
                     ep.PROFILE_ENV)
        self._saved = {n: os.environ.get(n) for n in env_names}
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop(ep.PROFILE_ENV, None)
        self.addCleanup(self._restore_env)

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
        if r.returncode != 0 or not r.stdout.strip():
            self.fail(
                "doctor subprocess produced no JSON: "
                f"rc={r.returncode} stderr={r.stderr[-800:]!r} "
                f"stdout={r.stdout[:200]!r}"
            )
        report = json.loads(r.stdout)
        emb = next(
            c for c in report["checks"] if c["id"] == "embeddings"
        )
        self.assertEqual(emb["details"]["reason"], "model_checksum_mismatch")
        self.assertIs(emb["details"]["checksum_ok"], False)
        note = emb["details"].get("note", "")
        self.assertIn("ONNX", note)
        self.assertIn("sentence-transformers", note)
        recs = "\n".join(report["recommendations"])
        self.assertIn("FAILED its checksum pin", recs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
