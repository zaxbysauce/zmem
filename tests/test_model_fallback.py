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


# Whether THIS interpreter has the embedding runtime installed. CI runs in a
# bare Python 3.11 without onnxruntime/tokenizers/numpy (the whole point of the
# degraded-mode suite), so tests that assert the file-level availability reasons
# ('ok' / 'model_file_missing' / 'tokenizer_missing') are only meaningful when
# the deps import — otherwise availability_status short-circuits to
# 'imports_missing' before the file checks. Behavior tests (the degraded warning,
# stats, doctor) accept either reason, since the degraded state is valid via
# both triggers (issue #22).
def _check_embedding_deps_importable() -> bool:
    for _mod in ("onnxruntime", "tokenizers", "numpy"):
        try:
            __import__(_mod)
        except Exception:
            return False
    return True


_EMBEDDING_DEPS_IMPORTABLE = _check_embedding_deps_importable()


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


class LoadPathChecksumTests(unittest.TestCase):
    """#36 M15: the LOAD path (not just the download path) must verify the
    model's checksum before onnxruntime executes it. An attacker able to write
    to ZMEM_MODELS_DIR could otherwise swap in an arbitrary ONNX binary. A
    mismatch must fail OPEN to the degraded no-embeddings path, never execute
    the unverified model."""

    def setUp(self):
        if not _EMBEDDING_DEPS_IMPORTABLE:
            self.skipTest("embedding deps not installed (CI) — load-path gate "
                          "needs importable onnxruntime to be meaningful")
        self.tmp = tempfile.mkdtemp()

    def test_tampered_model_degrades_and_does_not_load(self):
        """A model file present but with the WRONG checksum must NOT be loaded
        by _ensure_loaded; _model_available flips to False and embed_text
        returns None (degraded), never executing the tampered binary."""
        d = Path(self.tmp) / "tampered"
        d.mkdir()
        # A model file that exists but does NOT match the pinned checksum.
        (d / "minilm.onnx").write_bytes(b"tampered model bytes that are wrong" * 50)
        # tokenizer.json present so _check_available's is_file() passes (the
        # gate we are testing is the checksum, set in _ensure_loaded).
        (d / "tokenizer.json").write_bytes(b"{}")
        env = {**os.environ, "ZMEM_MODELS_DIR": str(d),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import embeddings\n"
            "import onnxruntime  # ensure load is attempted\n"
            # _check_available sets _model_available=True (both files present);
            # the LOAD-path gate must then reject it on checksum mismatch.
            "print('available_before=', embeddings._check_available())\n"
            "embeddings._ensure_loaded()\n"
            "print('session=', embeddings._session)\n"
            "print('available_after=', embeddings._model_available)\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The tampered model must NOT have been loaded into a session.
        self.assertIn("session= None", r.stdout, r.stdout + r.stderr)
        # And availability must have flipped to False (degraded).
        self.assertIn("available_after= False", r.stdout, r.stdout + r.stderr)

    def test_correct_checksum_model_loads(self):
        """A model file matching the pinned checksum IS loaded (the gate must
        not false-positive on a legitimate file). Only meaningful if the real
        minilm.onnx is present AND matches the pin; otherwise skip."""
        real_models = SCRIPTS_DIR.parent / "models"
        real_model = real_models / "minilm.onnx"
        if not real_model.is_file():
            self.skipTest("no real minilm.onnx on this box — skipping the "
                          "positive-load-path test")
        if not embeddings.verify_checksum(real_model):
            self.skipTest("on-disk minilm.onnx does not match the pinned "
                          "checksum (known: default builds differ) — the "
                          "positive path is exercised on the reference box")
        env = {**os.environ, "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import embeddings\n"
            "embeddings._ensure_loaded()\n"
            "print('session_loaded=', embeddings._session is not None)\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("session_loaded= True", r.stdout, r.stdout + r.stderr)

    def test_corrupt_tokenizer_fails_open_not_raises(self):
        """A load failure AFTER the checksum gate passes (e.g. onnxruntime
        can't parse a garbage model, or a corrupt tokenizer.json) must NOT
        raise through _ensure_loaded — it must fail OPEN to the degraded path,
        and availability_status must NOT report the model as healthy (cubic-1/2,
        reviewer NEEDS_REVISION).

        This genuinely exercises the post-checksum try/except: we write a fake
        model whose sha256 we patch into _MODEL_SHA256 (so the checksum gate
        PASSES), but whose bytes are garbage so InferenceSession raises → the
        new except branch fires."""
        d = Path(self.tmp) / "corrupttok"
        d.mkdir()
        fake_model = b"not a real onnx model " * 50
        (d / "minilm.onnx").write_bytes(fake_model)
        (d / "tokenizer.json").write_bytes(b"{}")  # present so _check_available passes
        fake_sha = embeddings._sha256_file(d / "minilm.onnx")
        env = {**os.environ, "ZMEM_MODELS_DIR": str(d),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        # Patch _MODEL_SHA256 so the checksum gate PASSES on the fake model —
        # forcing execution to reach the InferenceSession load (which fails).
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import embeddings\n"
            "import onnxruntime  # present so _check_available passes the import gate\n"
            f"embeddings._MODEL_SHA256 = {fake_sha!r}\n"  # checksum gate now passes
            "embeddings._ensure_loaded()\n"
            "print('raised= False')\n"
            "print('session=', embeddings._session)\n"
            "print('checksum_ok=', embeddings._model_checksum_ok)\n"
            "print('load_failed=', embeddings._model_load_failed)\n"
            "st = embeddings.availability_status()\n"
            "print('avail_reason=', st['reason'])\n"
            "print('avail_available=', st['available'])\n"
            "print('avail_checksum_ok=', st['checksum_ok'])\n"
            "print('avail_load_failed=', st['load_failed'])\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("raised= False", r.stdout, r.stdout + r.stderr)
        # The load failed (garbage model) → session not loaded.
        self.assertIn("session= None", r.stdout, r.stdout + r.stderr)
        # Checksum gate PASSED (so checksum_ok is None — never set True because
        # the load threw before that line) but load_failed is True. availability
        # must report the distinct model_load_failed reason, NOT ok.
        self.assertIn("load_failed= True", r.stdout, r.stdout + r.stderr)
        self.assertIn("avail_available= False", r.stdout, r.stdout + r.stderr)
        self.assertIn("avail_reason= model_load_failed", r.stdout,
                      r.stdout + r.stderr)
        self.assertNotIn("avail_reason= ok", r.stdout,
                         "load-failed model must not report reason=ok")


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
        if not _EMBEDDING_DEPS_IMPORTABLE:
            self.skipTest("embedding deps not installed in this interpreter "
                          "(CI runs without them) — file-level 'ok' reason "
                          "requires importable deps")
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
        """Tokenizer present, model absent -> reason='model_file_missing'
        (only reachable when the deps ARE importable; in a bare CI interpreter
        the imports_missing reason short-circuits first)."""
        if not _EMBEDDING_DEPS_IMPORTABLE:
            self.skipTest("embedding deps not installed in this interpreter "
                          "(CI runs without them) — the file-level reasons are "
                          "unreachable until imports succeed")
        d = Path(self.tmp) / "notok"
        d.mkdir()
        (d / "tokenizer.json").write_bytes(b"x")
        st = self._status_with(str(d))
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "model_file_missing")
        self.assertFalse(st["model_file"])
        self.assertTrue(st["tokenizer_file"])

    def test_status_reports_tokenizer_missing(self):
        """Model present, tokenizer absent -> reason='tokenizer_missing'
        (deps must be importable; see test_status_reports_model_file_missing)."""
        if not _EMBEDDING_DEPS_IMPORTABLE:
            self.skipTest("embedding deps not installed in this interpreter "
                          "(CI runs without them) — the file-level reasons are "
                          "unreachable until imports succeed")
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
        missing_imports lists which of onnxruntime/tokenizers/numpy failed.
        This case is the CI/bare-interpreter default and one of issue #22's
        two triggers — so it MUST be exercised regardless of whether the host
        interpreter has the deps."""
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

    def test_status_unavailable_when_deps_absent(self):
        """In a bare interpreter (e.g. CI) the status must report unavailable
        with reason='imports_missing', naming the missing modules — this is the
        default CI outcome and one of issue #22's triggers. Only runs when the
        host interpreter actually lacks the deps. Calls availability_status()
        IN-PROCESS (not via the subprocess helper, which would inherit a
        different interpreter state).

        Does NOT hard-code the full set of three: a partial install (e.g. only
        numpy missing, which also breaks onnxruntime's import) reports a subset.
        Asserts the reason is imports_missing, at least one module is reported,
        and every reported module is one of the three candidates."""
        if _EMBEDDING_DEPS_IMPORTABLE:
            self.skipTest("embedding deps ARE installed here; this asserts the "
                          "bare-interpreter (CI) behavior")
        d = Path(self.tmp) / "bare"
        d.mkdir()
        os.environ["ZMEM_MODELS_DIR"] = str(d)
        try:
            st = embeddings.availability_status()
        finally:
            os.environ.pop("ZMEM_MODELS_DIR", None)
        self.assertFalse(st["available"])
        self.assertEqual(st["reason"], "imports_missing")
        candidates = {"numpy", "onnxruntime", "tokenizers"}
        reported = set(st["missing_imports"])
        self.assertTrue(
            reported,
            "missing_imports should name at least one module when "
            "reason=imports_missing",
        )
        self.assertTrue(
            reported.issubset(candidates),
            f"reported modules {reported} should be a subset of the three "
            f"embedding candidates {candidates}",
        )

    def test_status_never_raises_on_bad_models_dir(self):
        """availability_status must never raise — point it at a path that does
        not exist; it should report unavailable, not crash. The reason depends
        on whether deps import (imports_missing in a bare interpreter, else a
        file reason)."""
        st = self._status_with(os.path.join(self.tmp, "does_not_exist"))
        self.assertFalse(st["available"])
        self.assertIn(st["reason"],
                      ("imports_missing", "model_file_missing", "tokenizer_missing"))
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
        # Names a reason — either trigger is valid (issue #22 has TWO:
        # missing model file when deps are present, OR missing imports in a bare
        # interpreter like CI). The point is the warning names A reason.
        self.assertTrue(
            "model_file_missing" in err or "imports_missing" in err
            or "tokenizer_missing" in err,
            f"warning should name an availability reason, got: {r.stderr}",
        )
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
        services one add per process, so we run a small in-process driver that
        calls add_memory() twice (the real insert path that fires the insert-site
        guard) and counts warnings on stderr."""
        script = (
            "import sys, os, sqlite3\n"
            f"os.environ.update({self.env!r})\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import store\n"
            f"conn = sqlite3.connect({self.store_path!r})\n"
            "conn.row_factory = sqlite3.Row\n"
            "store._degraded_embedding_warned = False\n"
            "store.add_memory(conn, namespace='project:oncewarn', type_='fact', "
            "content='first live content here', signal='test')\n"
            "store.add_memory(conn, namespace='project:oncewarn', type_='fact', "
            "content='second live content here distinct', signal='test')\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Exactly one warning line for two inserts.
        self.assertEqual(r.stderr.count("WARNING: memory stored without an embedding"), 1)

    def test_duplicate_add_does_not_consume_warning(self):
        """A duplicate add (exact-match dedup hit) inserts NO new row, so it must
        NOT consume the one-time-per-process warning — the next genuinely new
        unembedded row in the same process must still be surfaced. Regression
        guard for the fix that moved the warning from _detect_duplicate (which
        runs before dedup resolution) to the live-row insert sites.

        Discriminating sequence (fails on the pre-fix buggy code): PRE-SEED row A
        in a separate process, then in a FRESH process do add(A) [dup, must NOT
        warn] followed by add(B) [new, MUST warn]. Pre-fix, the dup-first add
        warned inside _detect_duplicate and consumed the flag, so add(B) was
        silent. Post-fix, the dup emits no warning and add(B) warns. The decisive
        assertion is that the dup add alone leaves the flag False (proven by the
        DUP_CONSUMED_FLAG marker) — which only holds post-fix."""
        # Pre-seed row A in its own process (it warns there, then the process
        # exits — irrelevant to the flag in the next process).
        self._add("a duplicate candidate row")
        # Fresh process: dup-first, then a genuinely new row. Emit a marker
        # AFTER the dup add (before the new add) showing whether the flag was set.
        script = (
            "import sys, os, sqlite3\n"
            f"os.environ.update({self.env!r})\n"
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
            "import store\n"
            f"conn = sqlite3.connect({self.store_path!r})\n"
            "conn.row_factory = sqlite3.Row\n"
            "store._degraded_embedding_warned = False\n"
            "# Duplicate of the pre-seeded row — inserts nothing, must NOT warn.\n"
            "store.add_memory(conn, namespace='project:degradedwarn', type_='fact', "
            "content='a duplicate candidate row', signal='test')\n"
            "# Probe the flag RIGHT AFTER the dup add, before the new add.\n"
            "sys.stderr.write('DUP_CONSUMED_FLAG=' + "
            "str(store._degraded_embedding_warned) + chr(10))\n"
            "# Genuinely new row — must still warn (flag was not consumed by the dup).\n"
            "store.add_memory(conn, namespace='project:degradedwarn', type_='fact', "
            "content='a genuinely new distinct row', signal='test')\n"
        )
        r = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The duplicate add must NOT have consumed the flag. Pre-fix, the dup
        # would have set the flag in _detect_duplicate (DUP_CONSUMED_FLAG=True),
        # and the new row B would be silent. Post-fix the flag stays False after
        # the dup, so B warns.
        self.assertIn(
            "DUP_CONSUMED_FLAG=False", r.stderr,
            "the duplicate add consumed the warning flag — pre-fix bug. "
            f"stderr: {r.stderr}",
        )
        # Exactly one warning total, attributable to row B's insert (not the dup).
        self.assertEqual(
            r.stderr.count("WARNING: memory stored without an embedding"), 1,
            f"expected exactly one warning (from the new row insert), got: {r.stderr}",
        )

    def test_empty_content_add_does_not_warn(self):
        """Empty/whitespace content produces no embedding by design (embed_text
        short-circuits) and must NOT trip the degradation warning."""
        r = self._add("   ")
        # The store rejects empty content, but either way the degradation
        # warning must not appear.
        self.assertNotIn("without an embedding", r.stderr.lower())

    def test_warning_does_not_trigger_download(self):
        """The warning path reads availability_status() (presence-only) and must
        not cause a network download attempt (autodownload stays opt-in).
        Exercises the real insert path (add_memory) that fires the warning."""
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
            "conn.row_factory = sqlite3.Row\n"
            "store.add_memory(conn, namespace='project:dltest', type_='fact', "
            "content='content that lands unembedded', signal='test')\n"
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
        # Availability + resolved models dir.
        self.assertIn("embeddings=unavailable", out)
        self.assertIn(self.empty_models_dir, out)
        # Names A reason — either trigger is valid (missing model file when deps
        # are present, OR missing imports in a bare CI interpreter).
        self.assertTrue(
            "reason=model_file_missing" in out
            or "reason=imports_missing" in out
            or "reason=tokenizer_missing" in out,
            f"stats should name an availability reason, got: {out}",
        )
        # Existing fields are still present (appended, not replaced).
        self.assertIn("by namespace (live):", out)
        self.assertIn("by signal (live):", out)


class DoctorEmbeddingsTests(unittest.TestCase):
    """Issue #22: doctor must report embedding availability (warn, not fail),
    the resolved interpreter, and must not flip top-level ok for a degraded
    install. Drives the real doctor.py CLI against an isolated store so the
    box store's schema version doesn't influence the result."""

    def test_doctor_reports_embeddings_warn_not_fail(self):
        """The embeddings check reports `warn` (NOT `fail`) when unavailable,
        names a reason + the resolved interpreter, and does not itself
        contribute to the fail count. We do NOT assert the overall returncode,
        because other env-specific checks (host surfaces, codex config) may
        legitimately fail in a bare CI box — the contract this test guards is
        that a degraded EMBEDDINGS state is advisory, not blocking."""
        tmp = tempfile.mkdtemp()
        # Point the store at a non-existent path so the schema-version check
        # reports `warn` (no store yet) rather than tripping the pre-existing
        # store.py(v7) vs doctor.py(v5) schema-version drift on an init'd store.
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
        # Doctor prints the JSON report regardless of overall ok/fail (other
        # env-specific checks may legitimately fail in a bare CI box). We parse
        # the report and assert about the EMBEDDINGS check specifically.
        self.assertTrue(r.stdout.strip(), f"doctor produced no JSON output: {r.stderr}")
        report = json.loads(r.stdout)
        emb = next(c for c in report["checks"] if c["id"] == "embeddings")
        # Degraded embeddings are advisory, never a hard failure.
        self.assertEqual(emb["status"], "warn")
        self.assertIn(emb["details"]["reason"],
                      ("model_file_missing", "tokenizer_missing", "imports_missing"))
        # The python check now reports the resolved interpreter (multi-Python
        # diagnosability).
        py = next(c for c in report["checks"] if c["id"] == "python")
        self.assertTrue(py["details"].get("interpreter"))

    def test_doctor_embeddings_check_does_not_fail_when_unavailable(self):
        """The embeddings check must be status `warn`, never `fail`, so a
        degraded install is not reported as broken. Run the embeddings check
        directly (in-process) so we isolate it from other env-specific checks
        that may legitimately fail in CI."""
        models_dir = os.path.join(tempfile.mkdtemp(), "nomodel")
        os.makedirs(models_dir, exist_ok=True)
        scripts_dir = str(REPO_ROOT / "skills" / "memory" / "scripts")
        lines = [
            "import sys, os, json",
            f"os.environ['ZMEM_MODELS_DIR'] = {models_dir!r}",
            "os.environ['ZMEM_MODEL_AUTODOWNLOAD'] = '0'",
            f"sys.path.insert(0, {scripts_dir!r})",
            "import doctor",
            "print(json.dumps(doctor._check_embeddings()))",
        ]
        r = subprocess.run([PYTHON, "-c", "\n".join(lines)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        check = json.loads(r.stdout.strip())
        self.assertNotEqual(check["status"], "fail",
                            f"embeddings check must not be 'fail' when unavailable "
                            f"(degraded is supported): {check}")
        self.assertIn(check["status"], ("warn", "pass"))


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
