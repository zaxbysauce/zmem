"""Tests for Phase 10 (PLAN.md §7-P10): model out of git + graceful degradation.

Covers:
  - `embeddings.verify_checksum` / `_try_download_model`: checksum-verified
    lazy download, using a `file://` URL against a small local fixture — NEVER
    a real network fetch of the 90MB model. Confirms fail-open (no exception,
    no partial file left behind) on both a mismatched checksum and an
    unreachable URL.
  - `store.py`'s lexical (Jaccard) token-overlap helpers used by `consolidate`
    when embeddings are unavailable.
  - End-to-end model-absent behavior via the REAL store.py CLI: with
    ZMEM_MODELS_DIR pointed at an empty temp dir (no minilm.onnx) and
    ZMEM_MODEL_AUTODOWNLOAD=0 (no network), `recall` still returns rows via
    FTS5 and `consolidate` still merges near-duplicates via the lexical
    fallback — neither crashes.

Drives the REAL store.py CLI via subprocess against throwaway temp stores —
never the box store — and imports embeddings.py directly for the unit-level
checksum/download tests.

Run: python tests/test_model_fallback.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
PYTHON = sys.executable

sys.path.insert(0, str(SCRIPTS_DIR))
import embeddings  # noqa: E402
import store  # noqa: E402


def _run(args, env):
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        capture_output=True, text=True, env=env,
    )


class ChecksumAndDownloadTests(unittest.TestCase):
    """Unit tests for the lazy-download + checksum-verify helper. No network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fixture = Path(self.tmp) / "fake_model_source.bin"
        self.fixture.write_bytes(b"not a real onnx model, just fixture bytes\x00" * 100)
        self.fixture_sha256 = embeddings._sha256_file(self.fixture)

    def test_verify_checksum_match(self):
        self.assertTrue(embeddings.verify_checksum(self.fixture, self.fixture_sha256))

    def test_verify_checksum_mismatch(self):
        self.assertFalse(embeddings.verify_checksum(self.fixture, "0" * 64))

    def test_verify_checksum_missing_file(self):
        missing = Path(self.tmp) / "does_not_exist.bin"
        self.assertFalse(embeddings.verify_checksum(missing, self.fixture_sha256))

    def test_download_succeeds_and_verifies_with_matching_checksum(self):
        """file:// URL stands in for the network — no real HTTP fetch."""
        dest = Path(self.tmp) / "downloaded" / "minilm.onnx"
        url = self.fixture.as_uri()
        ok = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        self.assertTrue(ok)
        self.assertTrue(dest.is_file())
        self.assertEqual(embeddings._sha256_file(dest), self.fixture_sha256)
        # No leftover partial file.
        self.assertFalse(dest.with_suffix(dest.suffix + ".part").exists())

    def test_download_fails_open_on_checksum_mismatch(self):
        """A byte-for-byte different source must NOT be adopted, and must not
        raise or leave a partial/corrupt file at the destination."""
        other = Path(self.tmp) / "different_bytes.bin"
        other.write_bytes(b"totally different content" * 50)
        dest = Path(self.tmp) / "downloaded2" / "minilm.onnx"
        dest.parent.mkdir(parents=True)
        url = other.as_uri()
        # `other`'s bytes don't match `self.fixture_sha256` -> must fail open.
        ok = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        self.assertFalse(ok)
        self.assertFalse(dest.is_file())
        self.assertFalse(dest.with_suffix(dest.suffix + ".part").exists())

    def test_download_fails_open_on_unreachable_url(self):
        dest = Path(self.tmp) / "downloaded3" / "minilm.onnx"
        ok = embeddings._try_download_model(dest, url="file:///no/such/path/model.onnx")
        self.assertFalse(ok)
        self.assertFalse(dest.is_file())


class LexicalHelperTests(unittest.TestCase):
    """Unit tests for store.py's Jaccard token-overlap clustering helpers."""

    def test_tokenize_drops_stopwords_and_short_tokens(self):
        toks = store._lexical_tokens("The build is a fast one, and it is on time")
        self.assertNotIn("the", toks)
        self.assertNotIn("is", toks)
        self.assertNotIn("a", toks)
        self.assertIn("build", toks)
        self.assertIn("fast", toks)
        self.assertIn("time", toks)

    def test_similarity_identical_high(self):
        a = store._lexical_tokens("pytest tracebacks concise flag build pipeline")
        b = store._lexical_tokens("pytest tracebacks concise flag build pipeline")
        self.assertEqual(store._lexical_similarity(a, b), 1.0)

    def test_similarity_unrelated_low(self):
        a = store._lexical_tokens("pytest tracebacks concise flag build pipeline")
        b = store._lexical_tokens("database indexing strategies query planner")
        self.assertEqual(store._lexical_similarity(a, b), 0.0)

    def test_similarity_empty_is_zero(self):
        self.assertEqual(store._lexical_similarity(set(), {"x"}), 0.0)
        self.assertEqual(store._lexical_similarity(set(), set()), 0.0)


class ModelAbsentEndToEndTests(unittest.TestCase):
    """Drives the real CLI with the model path forced absent (ZMEM_MODELS_DIR
    pointed at an empty temp dir) and network downloads disabled. Confirms the
    Phase-10 guarantee: recall (FTS5) and consolidate (lexical fallback) both
    keep working, no crash, no embeddings involved."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.empty_models_dir = os.path.join(self.tmp, "no_model_here")
        os.makedirs(self.empty_models_dir, exist_ok=True)
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store_path,
            "ZMEM_MODELS_DIR": self.empty_models_dir,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",  # never hit the network in tests
        }
        r = _run(["init"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _add(self, content, confidence="0.9"):
        r = _run(
            ["add", "--namespace", "project:modelfallbacktest", "--type", "fact",
             "--content", content, "--signal", "test", "--confidence", confidence],
            self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_recall_works_without_model(self):
        self._add("Always run pytest with the --tb=short flag for concise tracebacks")
        r = _run(
            ["recall", "--query", "pytest tracebacks", "--namespace", "project:modelfallbacktest", "--json"],
            self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        results = json.loads(r.stdout)
        self.assertTrue(len(results) >= 1, f"expected FTS5 recall hit, got: {r.stdout}")
        self.assertIn("tb=short", results[0]["content"])
        # Sanity: added row was never embedded (no model to embed with).
        self.assertNotIn("embedded", r.stderr + r.stdout)

    def test_consolidate_merges_via_lexical_fallback_without_model(self):
        self._add("The build pipeline uses pytest with the --tb=short flag for concise tracebacks")
        self._add("The build pipeline uses pytest with the --tb=short flag for concise output", "0.85")
        self._add("Completely unrelated memory about database indexing strategies", "0.7")

        r = _run(["consolidate", "--namespace", "project:modelfallbacktest"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lexical", r.stderr.lower())
        self.assertIn("merged 1", r.stdout)

        r = _run(["list", "--namespace", "project:modelfallbacktest"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 3 added, 1 absorbed -> 2 live.
        live_lines = [l for l in r.stdout.splitlines() if l.startswith("[") and " live " in l]
        self.assertEqual(len(live_lines), 2, r.stdout)

    def test_no_crash_and_no_download_attempted(self):
        """With autodownload disabled and no model file, embeddings.is_available()
        must be False and nothing should attempt a network call (verified
        indirectly: the add/recall/consolidate calls above all return 0 with
        ZMEM_MODEL_AUTODOWNLOAD=0 and no model present)."""
        r = _run(
            ["add", "--namespace", "project:modelfallbacktest", "--type", "fact",
             "--content", "sanity check row", "--signal", "test"],
            self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
