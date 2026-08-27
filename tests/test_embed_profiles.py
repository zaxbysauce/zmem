#!/usr/bin/env python
"""Issue #63, 8.2/8.5: embedding profile registry contract.

Run:  python tests/test_embed_profiles.py   (no pytest; house convention)

Covers:
- registry shape: exactly {minilm, fake}; every real profile carries
  hf_id/dim/sha256 (a name with empty sha256 is a stub — forbidden);
- unknown-profile refusal through resolve_active_profile;
- the `fake` embedder: determinism, content_norm equivalence classes,
  byte-level dim sanity (16 x float32 = 64), availability without any
  model files or third-party deps;
- embeddings.availability_status exposes profile/dim and stays consistent
  with is_available() under both profiles;
- no unverified-load escape hatch exists anywhere in the repo.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import embed_profiles as ep  # noqa: E402


def _save_env(*names):
    saved = {n: os.environ.get(n) for n in names}

    def restore():
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    return restore


class RegistryShape(unittest.TestCase):
    def test_exact_shipped_set(self):
        # set-equality surfaces silent registry growth (critic round C6)
        self.assertEqual(set(ep.PROFILES), {"minilm", "fake"})

    def test_minilm_is_complete_non_stub(self):
        m = ep.PROFILES["minilm"]
        self.assertEqual(m["hf_id"], "Xenova/all-MiniLM-L6-v2")
        self.assertEqual(m["dim"], 384)
        self.assertTrue(m["sha256"], "pinned sha256 must be non-empty")
        self.assertEqual(
            m["sha256"], "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5",
            "the pin is the Xenova ONNX export; changing it is a trust-root event",
        )

    def test_fake_profile_self_describing(self):
        f = ep.PROFILES["fake"]
        self.assertEqual(f["dim"], 16)
        self.assertIn("content_norm", f["notes"])
        self.assertEqual(ep.embedding_model_name("fake"), "fake")

    def test_marker_mapping(self):
        self.assertEqual(ep.embedding_model_name("minilm"), "minilm-onnx")
        self.assertEqual(ep.embedding_model_name("fake"), "fake")
        with self.assertRaises(Exception):
            ep.embedding_model_name("nomic")

    def test_documented_note_names_xenova_distinction(self):
        self.assertIn("ONNX", ep.MINILM_NOTES)
        self.assertIn("sentence-transformers", ep.MINILM_NOTES)


class ProfileResolution(unittest.TestCase):
    def setUp(self):
        self._restore = _save_env(ep.PROFILE_ENV)
        self.addCleanup(self._restore)
        os.environ.pop(ep.PROFILE_ENV, None)

    def test_unset_means_minilm(self):
        self.assertEqual(ep.resolve_active_profile(), "minilm")

    def test_empty_and_whitespace_mean_minilm(self):
        os.environ[ep.PROFILE_ENV] = "   "
        self.assertEqual(ep.resolve_active_profile(), "minilm")

    def test_case_insensitive_select(self):
        os.environ[ep.PROFILE_ENV] = "FAKE"
        self.assertEqual(ep.resolve_active_profile(), "fake")

    def test_unknown_refuses(self):
        os.environ[ep.PROFILE_ENV] = "nomic-embed-v2"
        with self.assertRaises(ep.ProfileError):
            ep.resolve_active_profile()
        # explicit environ dict variant must behave identically
        with self.assertRaises(ep.ProfileError):
            ep.resolve_active_profile({ep.PROFILE_ENV: "nope"})

    def test_active_dim_tracks_env(self):
        self.assertEqual(ep.active_dim(), 384)
        os.environ[ep.PROFILE_ENV] = "fake"
        self.assertEqual(ep.active_dim(), 16)


class FakeEmbedder(unittest.TestCase):
    def test_deterministic_and_canonicalized(self):
        a = ep.fake_embed("Hello World")
        b = ep.fake_embed("hello  world")     # same content_norm form
        c = ep.fake_embed("Goodbye World")
        self.assertEqual(a, b, "case/whitespace variants are identical")
        self.assertNotEqual(a, c)

    def test_blob_shape_16f32(self):
        blob = ep.fake_embed("some memory content here")
        self.assertEqual(len(blob), 64)
        vals = struct.unpack("<16f", blob)
        norm_sq = sum(v * v for v in vals)
        self.assertAlmostEqual(norm_sq, 1.0, places=5,
                               msg="vectors must be L2-normalized")

    def test_lexical_overlap_beats_disjoint(self):
        close_a = ep.fake_embed("sqlite wal checkpoint tuning")
        close_b = ep.fake_embed("wal checkpoint tuning sqlite")
        far = ep.fake_embed("kubernetes pod autoscaling limits")
        import math

        def cos(x, y):
            vx = struct.unpack("<16f", x)
            vy = struct.unpack("<16f", y)
            return sum(a * b for a, b in zip(vx, vy))

        # Relative form stays robust under signed-hash bucket collisions while
        # still proving lexical overlap carries real signal.
        self.assertGreater(cos(close_a, close_b), cos(close_a, far) + 0.2)

    def test_normalizer_matches_schema_meta_single_source(self):
        from schema_meta import normalize_content

        for text in ("  MiXeD   Case\n\ttext ", "trailing   "):
            self.assertEqual(
                ep.normalize_for_fake(text), normalize_content(text),
                "fake hashing MUST hash the exact canonical form",
            )


class AvailabilityWithoutModel(unittest.TestCase):
    """The fake profile needs NO onnxruntime/numpy/model files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-embprof-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self._restore = _save_env(
            ep.PROFILE_ENV, "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD")
        self.addCleanup(self._restore)

    def test_is_available_under_bogus_models_dir(self):
        os.environ[ep.PROFILE_ENV] = "fake"
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "does-not-exist")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        import embeddings

        embeddings._model_available = None  # reset cache for this process
        try:
            self.assertTrue(embeddings.is_available())
            st = embeddings.availability_status()
            self.assertTrue(st["available"])
            self.assertEqual(st["reason"], "ok")
            self.assertEqual(st["profile"], "fake")
            self.assertEqual(st["dim"], 16)
            self.assertEqual(embeddings.embed_text("anything"),
                             ep.fake_embed("anything"))
            self.assertIsNone(embeddings.embed_text("   "))
        finally:
            embeddings._model_available = None

    def test_status_unknown_profile_reports_raw_value(self):
        os.environ[ep.PROFILE_ENV] = "totally-bogus"
        import embeddings

        embeddings._model_available = None
        try:
            st = embeddings.availability_status()
            self.assertFalse(st["available"])
            self.assertEqual(st["reason"], "unknown_profile")
            self.assertEqual(st["profile"], "totally-bogus")
            self.assertIsNone(st["dim"])
        finally:
            embeddings._model_available = None


class CliRefusesUnknownProfile(unittest.TestCase):
    """The guard refuses BEFORE touching the store (exit 2, fail-closed)."""

    def test_recall_with_bad_profile_exit_2(self):
        tmp = tempfile.mkdtemp(prefix="zmem-embprof-cli-")
        env = dict(os.environ)
        env["ZMEM_STORE"] = str(Path(tmp) / "store.sqlite")
        env[ep.PROFILE_ENV] = "not-a-profile"
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "store.py"), "recall",
                 "--query", "x"],
                capture_output=True, text=True, env=env, timeout=60,
                cwd=str(SCRIPTS),
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("unknown ZMEM_EMBED_PROFILE value", r.stderr + r.stdout)
        finally:
            __import__("shutil").rmtree(tmp, True)


class NoUnverifiedEscapeHatch(unittest.TestCase):
    """Repo-wide invariant: verification cannot be bypassed by env (#63 gate)."""

    def test_no_allow_unverified_anywhere(self):
        """No code may READ an ALLOW_UNVERIFIED-style env var. Doc/test
        mentions of the *absence* are fine; an actual environment fetch would
        be the hatch (issue #63 gate)."""
        r = subprocess.run(
            ["git", "grep", "-n", "-i", "-e", "ALLOW_UNVERIFIED",
             "--", "*.py", "*.js", "*.sh"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        self.assertEqual(r.returncode, 0,
                         f"scan must actually run: rc={r.returncode} "
                         f"stderr={r.stderr[:200]!r} (vacuous pass outside a "
                         "git checkout would hide real hatches)")
        import re as _re

        def is_hatch(ln):
            low = ln.lower()
            return ("getenv" in low and "allow_unverified" in low) or (
                "environ" in low and "allow_unverified" in low)

        hatch = [
            ln for ln in r.stdout.splitlines()
            if is_hatch(ln)
            and "test_embed_profiles.py" not in ln  # this scanner's own text
        ]
        self.assertEqual(hatch, [],
                         f"unverified-load hatch introduced: {hatch}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
