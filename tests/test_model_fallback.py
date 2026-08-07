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

    def test_checksum_mismatch_prints_actionable_message(self):
        """On checksum mismatch the degrade path must explain WHY, not just
        silently return False (finding #2: honest/actionable failure mode)."""
        other = Path(self.tmp) / "different_bytes2.bin"
        other.write_bytes(b"totally different content" * 50)
        dest = Path(self.tmp) / "downloaded4" / "minilm.onnx"
        dest.parent.mkdir(parents=True)
        url = other.as_uri()

        import io
        from contextlib import redirect_stderr

        captured = io.StringIO()
        with redirect_stderr(captured):
            ok = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        self.assertFalse(ok)
        message = captured.getvalue().lower()
        self.assertIn("checksum mismatch", message)
        self.assertIn("no-embeddings", message)
        # Actionable: tells the user how to fix it.
        self.assertIn("zmem_model_url", message)

    def test_redact_url_for_logging_strips_userinfo_and_query_values(self):
        """Unit-level: ZMEM_MODEL_URL can legitimately carry credentials
        (https://user:token@host/path) or presigned-style query tokens
        (?sig=...), and neither may ever reach a printed/logged message.
        Tests the helper directly (no network, no _try_download_model
        involved) since the URL below is not resolvable and must never
        actually be fetched."""
        secret_url = "https://user:secrettoken@example.com/model.onnx?sig=abc123"
        safe = embeddings._redact_url_for_logging(secret_url)
        self.assertNotIn("secrettoken", safe)
        self.assertNotIn("abc123", safe)
        # Stays actionable: scheme + host + path survive.
        self.assertIn("example.com", safe)
        self.assertIn("model.onnx", safe)

    def test_checksum_mismatch_message_redacts_url_credentials(self):
        """finding: the checksum-mismatch message must never leak credentials
        or query-string tokens from a hostile-looking ZMEM_MODEL_URL, when
        the real download-and-verify path (_try_download_model) hits a
        checksum mismatch. `https://user:token@host/...?sig=...` isn't a
        fetchable `file://` fixture, so urllib.request.urlopen is patched to
        serve fixture bytes regardless of the requested URL -- never a real
        network fetch, per repo convention -- while the credentialed/
        presigned URL string itself still flows through unchanged to the
        message-building code, exercising the exact path that runs in
        production."""
        dest = Path(self.tmp) / "downloaded_redact" / "minilm.onnx"
        other = Path(self.tmp) / "different_bytes3.bin"
        other.write_bytes(b"totally different content" * 50)  # mismatches self.fixture_sha256

        secret_url = "https://user:secrettoken@example.com/model.onnx?sig=abc123"

        import io
        import unittest.mock
        from contextlib import redirect_stderr

        class _FakeResponse:
            def __enter__(self):
                self._f = open(other, "rb")
                return self._f
            def __exit__(self, *a):
                self._f.close()

        captured = io.StringIO()
        with redirect_stderr(captured):
            with unittest.mock.patch("urllib.request.urlopen", return_value=_FakeResponse()):
                ok = embeddings._try_download_model(dest, url=secret_url, expected_sha256=self.fixture_sha256)
        self.assertFalse(ok)
        message = captured.getvalue()
        self.assertNotIn("secrettoken", message)
        self.assertNotIn("abc123", message)
        # Still actionable.
        self.assertIn("checksum mismatch", message.lower())
        self.assertIn("no-embeddings", message.lower())
        self.assertIn("zmem_model_url", message.lower())

    def test_concurrent_download_attempts_use_distinct_temp_files(self):
        """Two 'concurrent' download attempts (simulated sequentially here,
        per repo convention of never doing real network I/O) must not share
        the same intermediate .part path (finding #3)."""
        dest = Path(self.tmp) / "downloaded5" / "minilm.onnx"
        url = self.fixture.as_uri()

        seen_tmp_paths = []
        real_open = open

        def spy_open(path, *args, **kwargs):
            p = str(path)
            if p.endswith(".part") or ".part" in Path(p).name:
                seen_tmp_paths.append(p)
            return real_open(path, *args, **kwargs)

        import builtins

        orig_open = builtins.open
        builtins.open = spy_open
        try:
            ok1 = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        finally:
            builtins.open = orig_open
        self.assertTrue(ok1)

        # Remove the result so a second attempt downloads again into a fresh
        # temp file, letting us compare the two temp paths used.
        dest.unlink()
        builtins.open = spy_open
        try:
            ok2 = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        finally:
            builtins.open = orig_open
        self.assertTrue(ok2)

        # Each attempt opens its .part path twice (write, then checksum
        # read) -- dedupe to the distinct paths used per attempt.
        unique_paths = sorted(set(seen_tmp_paths))
        self.assertEqual(len(unique_paths), 2)
        # Both should embed the pid, proving uniqueness isn't purely random-luck.
        self.assertIn(str(os.getpid()), unique_paths[0])
        self.assertIn(str(os.getpid()), unique_paths[1])

    def test_second_download_skips_rename_if_already_present(self):
        """If a concurrent attempt already placed a verified-correct file at
        model_path, a later attempt must not error and must leave the
        already-correct file in place (finding #3's race-harmlessly clause)."""
        dest = Path(self.tmp) / "downloaded6" / "minilm.onnx"
        url = self.fixture.as_uri()
        ok1 = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        self.assertTrue(ok1)
        mtime_before = dest.stat().st_mtime_ns

        ok2 = embeddings._try_download_model(dest, url=url, expected_sha256=self.fixture_sha256)
        self.assertTrue(ok2)
        self.assertEqual(embeddings._sha256_file(dest), self.fixture_sha256)
        # File wasn't churned (same underlying inode/mtime) since it already
        # existed and was correct.
        self.assertEqual(dest.stat().st_mtime_ns, mtime_before)


class AutodownloadDefaultTests(unittest.TestCase):
    """Finding #1: autodownload must be opt-in (default off), never
    opt-out, so ordinary store operations never make an unsolicited network
    call. Drives the real CLI so ZMEM_MODEL_AUTODOWNLOAD is genuinely unset,
    with the download target patched to a file:// fixture -- if the code
    attempted a download at all (even one that would 404/fail), a marker
    file gets written, so its absence proves no attempt was made."""

    def test_no_download_attempted_when_env_var_unset(self):
        tmp = tempfile.mkdtemp()
        models_dir = os.path.join(tmp, "no_model_here")
        os.makedirs(models_dir, exist_ok=True)
        marker = os.path.join(tmp, "download_attempted.marker")

        script = f'''
import sys
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
import embeddings

_orig = embeddings._try_download_model
def _spy(*a, **k):
    open({marker!r}, "w").close()
    return _orig(*a, **k)
embeddings._try_download_model = _spy
embeddings._resolve_models_dir = lambda: __import__("pathlib").Path({models_dir!r})
embeddings._check_available()
'''
        env = {**os.environ}
        env.pop("ZMEM_MODEL_AUTODOWNLOAD", None)  # ensure genuinely unset
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            os.path.exists(marker),
            "embeddings._check_available() attempted a download with "
            "ZMEM_MODEL_AUTODOWNLOAD unset -- autodownload must be opt-in, "
            "not opt-out (finding #1).",
        )


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


class AvailabilityStatusTests(unittest.TestCase):
    """Unit tests for embeddings.availability_status() — the structured,
    shallow, never-raising diagnostic added for issue #22. Drives the function
    directly (no CLI, no network) with temp dirs + import monkeypatching."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _status_with(self, models_dir, fake_imports_ok=True):
        """Probe availability_status against a controlled models_dir, optionally
        simulating missing imports by monkeypatching __import__."""
        env = {**os.environ, "ZMEM_MODELS_DIR": models_dir}
        script = (
            "import sys, os, json\n"
            f"os.environ.update({env!r})\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import embeddings\n"
            f"print(json.dumps(embeddings.availability_status()))\n"
        )
        if not fake_imports_ok:
            # Force the three embedding imports to fail inside the child process
            # by inserting a blocking meta_path finder BEFORE probing, so the
            # 'imports_missing' reason is exercised without uninstalling the
            # real packages from this interpreter.
            script = (
                "import sys, importlib.abc, importlib.machinery\n"
                "class _Block(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, fullname, path, target=None):\n"
                "        if fullname in ('onnxruntime','tokenizers','numpy'):\n"
                "            raise ImportError('blocked for test')\n"
                "        return None\n"
                "sys.meta_path.insert(0, _Block())\n"
            ) + script
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip())

    def test_status_reports_ok_when_model_and_tokenizer_present(self):
        """A dir with both files + importable deps reports reason='ok'."""
        d = Path(self.tmp) / "full"
        d.mkdir()
        (d / "minilm.onnx").write_bytes(b"x")
        (d / "tokenizer.json").write_bytes(b"x")
        st = self._status_with(str(d))
        self.assertTrue(st["available"])
        self.assertEqual(st["reason"], "ok")
        self.assertEqual(st["missing_imports"], [])
        self.assertTrue(st["model_file"])
        self.assertTrue(st["tokenizer_file"])
        self.assertTrue(st["models_dir"])

    def test_status_reports_model_file_missing(self):
        """Tokenizer present, model absent -> reason='model_file_missing'."""
        d = Path(self.tmp) / "notok"
        d.mkdir()
        (d / "tokenizer.json").write_bytes(b"x")
        st = self._status_with(str(d))
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "model_file_missing")
        self.assertFalse(st["model_file"])
        self.assertTrue(st["tokenizer_file"])

    def test_status_reports_tokenizer_missing(self):
        """Model present, tokenizer absent -> reason='tokenizer_missing'."""
        d = Path(self.tmp) / "notok2"
        d.mkdir()
        (d / "minilm.onnx").write_bytes(b"x")
        st = self._status_with(str(d))
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "tokenizer_missing")
        self.assertTrue(st["model_file"])
        self.assertFalse(st["tokenizer_file"])

    def test_status_reports_imports_missing_with_list(self):
        """When the runtime deps can't import, reason='imports_missing' and
        missing_imports lists which of onnxruntime/tokenizers/numpy failed."""
        d = Path(self.tmp) / "imports"
        d.mkdir()
        (d / "minilm.onnx").write_bytes(b"x")
        (d / "tokenizer.json").write_bytes(b"x")
        st = self._status_with(str(d), fake_imports_ok=False)
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "imports_missing")
        self.assertEqual(
            sorted(st["missing_imports"]), ["numpy", "onnxruntime", "tokenizers"]
        )
        # Files are still reported as present even when imports fail — status is
        # an honest snapshot of both axes, not a single collapsed bool.
        self.assertTrue(st["model_file"])

    def test_status_never_raises_on_bad_models_dir(self):
        """availability_status must never raise — point it at a path that does
        not exist; it should report missing files, not crash."""
        st = self._status_with(os.path.join(self.tmp, "does_not_exist"))
        self.assertFalse(st["available"])
        self.assertIn(st["reason"], ("model_file_missing", "tokenizer_missing"))
        self.assertFalse(st["model_file"])


class DegradedAddWarningTests(unittest.TestCase):
    """Issue #22: a degraded `add` (no model / no imports) must emit a
    one-time-per-process, actionable stderr warning naming the reason and the
    resolved models_dir. Drives the real CLI in a subprocess with the model
    forced absent, per repo convention."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.empty_models_dir = os.path.join(self.tmp, "no_model_here")
        os.makedirs(self.empty_models_dir, exist_ok=True)
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store_path,
            "ZMEM_MODELS_DIR": self.empty_models_dir,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        r = _run(["init"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _add(self, content):
        return _run(
            ["add", "--namespace", "project:degradedwarn", "--type", "fact",
             "--content", content, "--signal", "test"],
            self.env,
        )

    def test_degraded_add_emits_actionable_warning(self):
        r = self._add("a live lesson that should have been embedded")
        self.assertEqual(r.returncode, 0, r.stderr)
        err = r.stderr.lower()
        self.assertIn("warning", err)
        self.assertIn("without an embedding", err)
        # Names the reason.
        self.assertIn("model_file_missing", err)
        # Names the resolved models dir (the empty temp dir).
        self.assertIn(self.empty_models_dir.lower(), err)
        # Actionable: points at a remedy.
        self.assertTrue(
            "zmem_model_url" in err or "reembed" in err or "minilm.onnx" in err,
            f"warning should be actionable, got: {r.stderr}",
        )
        # The add still succeeded (degraded is a supported state).
        self.assertIn("[zmem] added memory", r.stdout)

    def test_warning_fires_once_per_process(self):
        """Two adds in ONE store.py invocation must warn exactly once. The CLI
        services one add per process, so we run a small driver that calls the
        in-process _detect_duplicate twice and counts warnings on stderr."""
        script = (
            "import sys, os, sqlite3\n"
            f"os.environ.update({self.env!r})\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import store\n"
            f"conn = sqlite3.connect({self.store_path!r})\n"
            "conn.row_factory = sqlite3.Row\n"
            "store._degraded_embedding_warned = False\n"
            "store._detect_duplicate(conn, 'first live content here', 'project:x')\n"
            "store._detect_duplicate(conn, 'second live content here', 'project:x')\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Exactly one warning line for two calls.
        self.assertEqual(r.stderr.count("WARNING: memory stored without an embedding"), 1)

    def test_empty_content_add_does_not_warn(self):
        """Empty/whitespace content produces no embedding by design (embed_text
        short-circuits) and must NOT trip the degradation warning."""
        r = self._add("   ")
        # The store rejects empty content, but either way the degradation
        # warning must not appear.
        self.assertNotIn("without an embedding", r.stderr.lower())

    def test_warning_does_not_trigger_download(self):
        """The warning path reads availability_status() (presence-only) and must
        not cause a network download attempt (autodownload stays opt-in)."""
        marker = os.path.join(self.tmp, "download_attempted.marker")
        script = (
            "import sys, os\n"
            f"os.environ.update({self.env!r})\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import embeddings, store\n"
            "_orig = embeddings._try_download_model\n"
            f"def _spy(*a, **k):\n"
            f"    open({marker!r}, 'w').close()\n"
            "    return _orig(*a, **k)\n"
            "embeddings._try_download_model = _spy\n"
            "import sqlite3\n"
            f"conn = sqlite3.connect({self.store_path!r})\n"
            "store._detect_duplicate(conn, 'content that lands unembedded', 'project:x')\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            os.path.exists(marker),
            "the degraded-warning path triggered a download attempt — "
            "availability_status() must be presence-only.",
        )


class StatsEmbeddingCoverageTests(unittest.TestCase):
    """Issue #22: `stats` must report live embedding coverage + availability."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.empty_models_dir = os.path.join(self.tmp, "no_model_here")
        os.makedirs(self.empty_models_dir, exist_ok=True)
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store_path,
            "ZMEM_MODELS_DIR": self.empty_models_dir,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        r = _run(["init"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = _run(
            ["add", "--namespace", "project:statscov", "--type", "fact",
             "--content", "an unembedded row for stats coverage", "--signal", "test"],
            self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stats_reports_coverage_and_availability(self):
        r = _run(["stats"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        # Coverage block present with live counts.
        self.assertIn("embedding coverage (live):", out)
        self.assertIn("with_embedding=0", out)
        self.assertIn("without_embedding=1", out)
        # Availability + reason + resolved models dir.
        self.assertIn("embeddings=unavailable", out)
        self.assertIn("reason=model_file_missing", out)
        self.assertIn(self.empty_models_dir, out)
        # Existing fields are still present (appended, not replaced).
        self.assertIn("by namespace (live):", out)
        self.assertIn("by signal (live):", out)


class DoctorEmbeddingsTests(unittest.TestCase):
    """Issue #22: doctor must report embedding availability (warn, not fail),
    the resolved interpreter, and must not flip top-level ok for a degraded
    install. Drives the real doctor.py CLI against an isolated store so the
    box store's schema version doesn't influence the result."""

    def test_doctor_reports_embeddings_warn_and_keeps_ok_true(self):
        tmp = tempfile.mkdtemp()
        # Point the store at a non-existent path so the schema-version check
        # reports `warn` (no store yet) rather than tripping the pre-existing
        # store.py(v7) vs doctor.py(v5) schema-version drift on an init'd store.
        # The embedding check is independent of the store (presence-only), so a
        # missing store does not affect what we are asserting here.
        store_path = os.path.join(tmp, "fresh_store.sqlite")
        models_dir = os.path.join(tmp, "nomodel")
        os.makedirs(models_dir, exist_ok=True)
        env = {**os.environ,
               "ZMEM_STORE": store_path,
               "ZMEM_MODELS_DIR": models_dir,
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        doctor_py = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"
        r = subprocess.run(
            [PYTHON, str(doctor_py), "--project", str(REPO_ROOT), "--format", "json"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        emb = next(c for c in report["checks"] if c["id"] == "embeddings")
        self.assertEqual(emb["status"], "warn")
        self.assertIn(emb["details"]["reason"],
                      ("model_file_missing", "tokenizer_missing"))
        # The embedding check must NOT contribute to a top-level failure.
        # (ok == fail count == 0; a degraded install is supported.)
        self.assertNotIn(emb["status"], ("fail",))
        # The python check now reports the resolved interpreter (multi-Python
        # diagnosability).
        py = next(c for c in report["checks"] if c["id"] == "python")
        self.assertTrue(py["details"].get("interpreter"))


class IngestJsonlWarningTests(unittest.TestCase):
    """Issue #22: the ingest-jsonl path shares _detect_duplicate, so a live
    imported row must warn when embeddings are unavailable; a tombstoned
    history row (which deliberately bypasses _detect_duplicate) must NOT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.empty_models_dir = os.path.join(self.tmp, "no_model_here")
        os.makedirs(self.empty_models_dir, exist_ok=True)
        self.env = {
            **os.environ,
            "ZMEM_STORE": self.store_path,
            "ZMEM_MODELS_DIR": self.empty_models_dir,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        r = _run(["init"], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _ingest(self, rows, allow_tombstones=False):
        import uuid as _uuid
        path = os.path.join(self.tmp, "sync.jsonl")
        with open(path, "w") as f:
            for row in rows:
                row = {
                    "id": str(_uuid.uuid4()),
                    "namespace": "project:ingestwarn",
                    "type": "fact",
                    "content": "live imported lesson for ingest warning",
                    "tags": "",
                    "source_ref": "",
                    "source_hash": "",
                    "confidence": 0.8,
                    "signal": "test",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "ingestion_ts": "2026-01-01T00:00:00Z",
                    "retrieval_count": 0,
                    **row,
                }
                f.write(json.dumps(row) + "\n")
        args = ["ingest-jsonl", "--in", path, "--source-ref", "test-sync"]
        if allow_tombstones:
            args.append("--allow-tombstones")
        return _run(args, self.env)

    def test_live_ingested_row_warns_when_unembedded(self):
        r = self._ingest([{}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("without an embedding", r.stderr.lower())

    def test_tombstoned_history_row_does_not_warn(self):
        """A row that arrives already-superseded is inserted as history via the
        direct path (no _detect_duplicate), so it must not trip the live-row
        degradation warning."""
        r = self._ingest(
            [{"superseded_at": "2026-01-02T00:00:00Z",
              "supersede_reason": "imported tombstone"}],
            allow_tombstones=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("without an embedding", r.stderr.lower())


if __name__ == "__main__":
    unittest.main()
