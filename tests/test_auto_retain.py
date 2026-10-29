"""Evidence-gated automatic retention tests (issue #177, Workstream K PR 6).

Every store-touching test pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR/
ZMEM_MODEL_AUTODOWNLOAD before importing storelib; the operator's ~/.zmem is
never used.  Direct SQLite test connections execute PRAGMA foreign_keys=ON
and assert 1 before reading or writing; production code does not rely on
foreign-key enforcement.
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "retain"
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
DRIVER = REPO_ROOT / "hooks" / "lib" / "zmem-retain-process.py"

# --- module-level isolation pin (BEFORE any storelib import) ---------------
_SCRATCH = tempfile.mkdtemp(prefix="zmem-auto-retain-")
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _key in ("ZMEM_AUTO_RETAIN", "ZMEM_CAPTURE", "ZMEM_INJECT",
             "ZMEM_REFLECT"):
    os.environ.pop(_key, None)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
for _mod in [k for k in list(sys.modules)
             if k == "storelib" or k.startswith("storelib.")]:
    del sys.modules[_mod]

import storelib  # noqa: E402
from storelib import retain as retain_mod  # noqa: E402
from storelib.retain import (  # noqa: E402
    RETAIN_HOSTS, extract_candidates, normalize_hermes_messages,
    normalize_host_payload)
from correction_queue import MAX_QUEUE_SIZE, load_queue  # noqa: E402
from capture_quality import strip_zmem_fence  # noqa: E402

_STORE_PY = SCRIPTS / "store.py"

# Keys whose ambient values flip in-process behavior at call time.
_ENV_KEYS = ("ZMEM_AUTO_RETAIN", "ZMEM_CAPTURE", "ZMEM_INJECT",
             "ZMEM_REFLECT")


def _fresh_scratch():
    return tempfile.mkdtemp(prefix="zmem-retain-case-")


def _cli_env(scratch, **extra):
    """A clean child env pinned to a fresh scratch store."""
    env = dict(os.environ)
    for key in _ENV_KEYS:
        env.pop(key, None)
    env["ZMEM_DATA"] = scratch
    env["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
    env["ZMEM_MODELS_DIR"] = os.path.join(scratch, "missing-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.update({k: v for k, v in extra.items() if v is not None})
    for key, value in list(extra.items()):
        if value is None:
            env.pop(key, None)
    return env


def _run_cli(args, stdin_text="", env=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(_STORE_PY), *args],
        input=stdin_text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
        cwd=str(cwd or REPO_ROOT), timeout=60, check=False,
    )


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "zmem_retain_driver_177", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_retain_driver_177"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider():
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        "zmem_hermes_177", REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_hermes_177"] = module
    spec.loader.exec_module(module)
    return module


def _load_compat_hook():
    spec = importlib.util.spec_from_file_location(
        "zmem_compat_177",
        REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_compat_177"] = module
    spec.loader.exec_module(module)
    return module


def _fixture_rows(name):
    return [json.loads(line) for line in
            (FIXTURES / name).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _queue_items(scratch, namespace="project:retain"):
    path = Path(scratch) / "queue"
    if not path.is_dir():
        return []
    items = []
    for file in sorted(path.glob("*.json")):
        items.extend(json.loads(file.read_text(encoding="utf-8")))
    return items


class FakeRetainProcess:
    """Test double for the injected popen seam (issue #177 Design step 8)."""

    def __init__(self, args, stdout="", stderr="", timeout_on=None,
                 **popen_kwargs):
        self.args = args
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_on = timeout_on
        self.calls: list[str] = []
        self.stdin = io.StringIO()
        self.stdout_handle = io.StringIO()
        self.stderr_handle = io.StringIO()
        self._closed = False

    def communicate(self, input_text=None, timeout=None):
        self.calls.append("communicate:%s" % ("timeout" if timeout else "plain"))
        if self._timeout_on is not None and timeout is not None:
            raise subprocess.TimeoutExpired(
                cmd=self.args, timeout=self._timeout_on)
        self._communicate_input = input_text
        self._close_handles()
        return (self._stdout, self._stderr)

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append("wait")
        return 0

    def _close_handles(self):
        if not self._closed:
            for handle in (self.stdin, self.stdout_handle,
                           self.stderr_handle):
                try:
                    handle.close()
                except Exception:
                    pass
            self._closed = True


# ---------------------------------------------------------------------------
# RetentionFixtureTest
# ---------------------------------------------------------------------------

class RetentionFixtureTest(unittest.TestCase):
    """Byte-level oracle: extract_candidates reproduces expected-queue.json."""

    def test_session_a_matches_expected_queue(self):
        rows = _fixture_rows("session-a.jsonl")
        items = extract_candidates(rows)
        lines = [json.dumps(item, separators=(",", ":"),
                            ensure_ascii=False) for item in items]
        rendered = ("[\n    " + ",\n    ".join(lines) + "\n]\n")
        want = (FIXTURES / "expected-queue.json").read_text(encoding="utf-8")
        self.assertEqual(rendered, want)
        self.assertEqual(len(items), 4)
        self.assertEqual([i["class"] for i in items],
                         ["explicit", "decision", "correction", "outcome"])
        for item in items:
            self.assertEqual(item["signal"], "none")
            self.assertEqual(item["source"], "live-capture")
            self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
            self.assertRegex(item["retain_hash"], r"^[0-9a-f]{64}$")
            self.assertIn("evidence_ids", item)
            for legacy in ("schema_version", "message", "type", "patterns",
                           "confidence", "sentiment", "decay_days",
                           "timestamp", "session", "namespace", "host"):
                self.assertIn(legacy, item)


# ---------------------------------------------------------------------------
# RetentionAdapterTest
# ---------------------------------------------------------------------------

class RetentionAdapterTest(unittest.TestCase):

    def test_host_payloads_emit_expected_normalized_jsonl(self):
        generator = FIXTURES / "generate_payloads.py"
        wrapper = FIXTURES / "host-payloads.json"
        targets = {
            "claude": FIXTURES / "claude-payload.json",
            "codex": FIXTURES / "codex-payload.json",
            "zcode": FIXTURES / "zcode-payload.json",
        }
        gen = subprocess.run(
            [sys.executable, str(generator), str(wrapper),
             str(targets["claude"]), str(targets["codex"]),
             str(targets["zcode"])],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(REPO_ROOT), timeout=60, check=False)
        self.assertEqual(gen.returncode, 0, gen.stderr)

        outputs = []
        for host, target in targets.items():
            scratch = _fresh_scratch()
            result = _run_cli(
                ["retain-adapt", "--input", str(target),
                 "--namespace", "project:retain",
                 "--session", "adapter-session", "--host", host],
                env=_cli_env(scratch))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            outputs.append(result.stdout)
        combined = "".join(outputs)
        want = (FIXTURES / "expected-normalized.jsonl").read_text(
            encoding="utf-8")
        self.assertEqual(combined, want)

    def test_hermes_payloads_emit_expected_normalized_jsonl(self):
        provider_payload = json.loads(
            (FIXTURES / "hermes-provider-messages.json").read_text(
                encoding="utf-8"))
        provider_rows = normalize_hermes_messages(
            provider_payload["messages"], session="provider-session",
            namespace="project:retain", host="hermes-provider")
        compat_payload = json.loads(
            (FIXTURES / "hermes-compat-payload.json").read_text(
                encoding="utf-8"))
        compat_hook = _load_compat_hook()
        compat_rows = compat_hook._normalize_compatibility_messages(
            compat_payload, session_id="compat-session",
            namespace="project:retain")
        rows = provider_rows + compat_rows
        rendered = "".join(
            json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
            for row in rows)
        want = (FIXTURES / "expected-hermes-normalized.jsonl").read_text(
            encoding="utf-8")
        self.assertEqual(rendered, want)
        self.assertEqual([r["evidence_id"] for r in rows],
                         ["provider-1", "provider-3", "compat-1"])
        self.assertEqual(rows[1].get("runner_error"), "Permission denied")


# ---------------------------------------------------------------------------
# RetentionSafetyTest
# ---------------------------------------------------------------------------

class RetentionSafetyTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_untrusted_fence_is_not_candidate_text(self):
        fenced = ("<<<ZMEM_UNTRUSTED_FENCE>>>\nsecret-looking enclosed text\n"
                  "<<<END_ZMEM_UNTRUSTED_FENCE>>>")
        rows = [
            {"evidence_id": "f-1", "text": fenced,
             "timestamp": "2026-09-10T10:00:00Z", "session": "s",
             "namespace": "project:retain", "host": "claude", "kind": "user"},
            {"evidence_id": "f-2",
             "text": "Actually, run tests before merge",
             "timestamp": "2026-09-10T10:01:00Z", "session": "s",
             "namespace": "project:retain", "host": "claude", "kind": "user"},
        ]
        items = extract_candidates(rows)
        blob = json.dumps(items)
        # The complete fenced block and its enclosed text occur in NO queue
        # item; the fully-fenced row yields no candidate at all.
        self.assertEqual(len(items), 1)
        self.assertNotIn("secret-looking enclosed text", blob)
        self.assertNotIn("<<<END_ZMEM_UNTRUSTED_FENCE>>>", blob)
        # An unmatched marker remains unchanged by #123's strip_zmem_fence.
        text = "value <<<ZMEM_UNTRUSTED_FENCE>>> trailing"
        self.assertEqual(strip_zmem_fence(text), text)

    def test_capture_off_is_byte_passive(self):
        scratch = _fresh_scratch()
        row = json.dumps({
            "evidence_id": "e-1", "text": "Remember: use staged deploys",
            "timestamp": "2026-09-10T10:00:00Z", "session": "s",
            "namespace": "project:retain", "host": "claude",
            "kind": "explicit"}) + "\n"
        result = _run_cli(
            ["retain", "--namespace", "project:retain", "--session", "s",
             "--host", "claude"],
            stdin_text=row,
            env=_cli_env(scratch, ZMEM_CAPTURE="0", ZMEM_AUTO_RETAIN="1"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '{"count":0,"ok":true}\n')
        # Byte-passive: no store file, no queue file, nothing else created.
        self.assertEqual(os.listdir(scratch), [])

    def test_config_gate_and_env_override(self):
        explicit_row = json.dumps({
            "evidence_id": "e-1", "text": "Remember: use staged deploys",
            "timestamp": "2026-09-10T10:00:00Z", "session": "s",
            "namespace": "project:retain", "host": "claude",
            "kind": "explicit"}) + "\n"
        args = ["retain", "--namespace", "project:retain", "--session", "s",
                "--host", "claude"]

        def run(scratch, **extra):
            return _run_cli(args, stdin_text=explicit_row,
                            env=_cli_env(scratch, **extra))

        # config false (no config file): unset and exact 0 both count 0.
        for extra in ({}, {"ZMEM_AUTO_RETAIN": "0"}):
            scratch = _fresh_scratch()
            result = run(scratch, **extra)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '{"count":0,"ok":true}\n')
            self.assertEqual(_queue_items(scratch), [])

        # config true: unset follows config; exact 0 forces off; 1 forces on.
        def with_config(scratch, auto_retain):
            Path(scratch, "config.json").write_text(
                json.dumps({"auto_retain": auto_retain}), encoding="utf-8")

        scratch = _fresh_scratch(); with_config(scratch, True)
        result = run(scratch)
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')
        scratch = _fresh_scratch(); with_config(scratch, True)
        result = run(scratch, ZMEM_AUTO_RETAIN="0")
        self.assertEqual(result.stdout, '{"count":0,"ok":true}\n')
        self.assertEqual(_queue_items(scratch), [])
        scratch = _fresh_scratch(); with_config(scratch, False)
        result = run(scratch, ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')

        # invalid non-empty override: exact message, count 0, exit 0, and
        # ok:false so a checkpoint-v2 caller reads an operator error as a
        # failure rather than a successful no-op.
        for bad in ("yes", "true", "2", " "):
            scratch = _fresh_scratch()
            result = run(scratch, ZMEM_AUTO_RETAIN=bad)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '{"count":0,"ok":false}\n')
            self.assertIn("[zmem] retain: invalid ZMEM_AUTO_RETAIN",
                          result.stderr)

    def test_queue_write_failure_reports_partial_count(self):
        rows = _fixture_rows("session-a.jsonl")
        stdin_text = "".join(
            json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
            for row in rows)
        queue_dir = Path(_fresh_scratch())
        real_append = retain_mod._append_queue
        calls = {"n": 0}

        def flaky_append(namespace, item, queue_dir=None):
            calls["n"] += 1
            if calls["n"] == 3:
                return False
            return real_append(namespace, item, queue_dir=queue_dir)

        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        import storelib.cli as cli_mod
        with mock.patch.object(retain_mod, "_append_queue", flaky_append), \
                mock.patch.dict(os.environ, {"ZMEM_AUTO_RETAIN": "1"}), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), \
                mock.patch("correction_queue.resolve_queue_dir",
                           lambda: queue_dir), \
                redirect_stdout(stdout_buf), redirect_stderr(stderr_buf), \
                mock.patch.object(
                    sys, "argv",
                    ["store.py", "retain", "--input", "-",
                     "--namespace", "project:retain", "--session", "session-a",
                     "--host", "claude"]):
            with self.assertRaises(SystemExit) as caught:
                cli_mod.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(stdout_buf.getvalue(),
                         '{"count":2,"ok":false}\n')
        self.assertEqual(
            stderr_buf.getvalue(),
            "[zmem] retain: queue write failed; written=2; total=4\n")
        self.assertEqual(
            len(load_queue("project:retain", queue_dir=queue_dir)), 2)

    def test_correction_values_pre_boost_and_fp_gate(self):
        # (a) the frozen 0.55 lands for the fixture correction text.
        rows = [_fixture_rows("session-a.jsonl")[2]]
        items = extract_candidates(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["confidence"], 0.55)
        self.assertEqual(items[0]["decay_days"], 45)
        self.assertEqual(items[0]["patterns"], "actually")
        # (b) FP gate: the unpadded classifier verdict wins (no candidate).
        fp_rows = [{
            "evidence_id": "fp-1", "text": "actually, use the canary?",
            "timestamp": "2026-09-10T10:00:00Z", "session": "s",
            "namespace": "project:retain", "host": "claude", "kind": "user",
        }]
        self.assertEqual(extract_candidates(fp_rows), [])
        # (c) value agreement across padded and unpadded derivation.
        from corrections import MIN_SHORT_CORRECTION_LENGTH, detect_patterns
        text = "Actually, run tests before merge"
        unpadded = detect_patterns(text)
        padded = detect_patterns(
            text + " " * (MIN_SHORT_CORRECTION_LENGTH - len(text)))
        self.assertEqual(unpadded[0], "auto")
        self.assertEqual((padded[1], padded[3], padded[4]),
                         (unpadded[1], unpadded[3], unpadded[4]))
        self.assertEqual(padded[2], 0.55)

    def test_runner_outcome_reachable_without_kind(self):
        # Final-critic R1: the wired lanes normalize tool rows with
        # runner_error and NO kind; two same-error rows must aggregate into
        # exactly one outcome candidate with the full field contract.
        rows = [
            {"evidence_id": "r-1",
             "text": "Permission denied while running tests",
             "timestamp": "2026-09-10T10:03:00Z", "session": "s",
             "namespace": "project:retain", "host": "hermes-provider",
             "runner_error": "Permission denied"},
            {"evidence_id": "r-2",
             "text": "Permission denied while running tests",
             "timestamp": "2026-09-10T10:04:00Z", "session": "s",
             "namespace": "project:retain", "host": "hermes-provider",
             "runner_error": "Permission denied"},
        ]
        items = extract_candidates(rows)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["class"], "outcome")
        self.assertEqual(item["patterns"], "permission_denied")
        self.assertEqual(item["message"],
                         "Check filesystem/ownership permissions on the "
                         "target path")
        # The occurrence count drives REVIEW ORDER, not how sure we are the
        # classification is right, so confidence is a fixed honest value and
        # the ordering weight lives in its own field. It must also stay under
        # the 0.6 threshold clear_queue(drop_stale=True) uses.
        self.assertEqual(item["confidence"], 0.55)
        self.assertLess(item["confidence"], 0.6)
        self.assertEqual(item["review_priority"], 0.70)
        self.assertEqual(item["decay_days"], 90)
        self.assertEqual(item["timestamp"], "2026-09-10T10:03:00Z")
        self.assertEqual(item["evidence_ids"], ["r-1", "r-2"])


# ---------------------------------------------------------------------------
# RetentionQueueCapTest
# ---------------------------------------------------------------------------

class RetentionQueueCapTest(unittest.TestCase):

    def test_imported_cap_is_100_and_bounds_1000_events(self):
        self.assertEqual(MAX_QUEUE_SIZE, 100)
        queue_dir = Path(_fresh_scratch())
        from correction_queue import append_queue
        for index in range(1000):
            item = {
                "schema_version": 1, "id": "ev-%04d" % index,
                "message": "Remember: event %d" % index,
                "type": "lesson", "patterns": "remember:",
                "confidence": 0.9, "sentiment": "correction",
                "decay_days": 120,
                "timestamp": "2026-09-10T10:%02d:%02d" % (
                    (index // 60) % 60, index % 60),
                "session": "s", "namespace": "project:retain",
                "host": "claude", "source": "live-capture",
            }
            self.assertTrue(
                append_queue("project:retain", item, queue_dir=queue_dir))
        items = load_queue("project:retain", queue_dir=queue_dir)
        self.assertLessEqual(len(items), 100)


# ---------------------------------------------------------------------------
# RetentionLaneMatrixTest
# ---------------------------------------------------------------------------

class _DriverLaneCase(unittest.TestCase):
    """Shared harness: drive the stage driver in-process, count children."""

    _STORE_KEYS = ("ZMEM_DATA", "ZMEM_STORE", "ZMEM_MODELS_DIR",
                   "ZMEM_MODEL_AUTODOWNLOAD")

    def lane_scratch(self):
        scratch = _fresh_scratch()
        self.addCleanup(self._restore_env)
        return scratch

    def _restore_env(self):
        for key, value in getattr(self, "_lane_saved", {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_driver(self, *, host, payload_text=None, transcript_path=None,
                   env_extra=None):
        driver = _load_driver()
        calls = []
        real_run_retain = driver.run_retain

        def counting_run_retain(argv, input_text, **kwargs):
            calls.append(list(argv))
            return real_run_retain(argv, input_text, **kwargs)

        if not hasattr(self, "_lane_saved"):
            self._lane_saved = {k: os.environ.get(k)
                                for k in self._STORE_KEYS + _ENV_KEYS}
        stdin = io.StringIO(payload_text or "")
        argv = ["--host", host, "--session", "lane-session",
                "--namespace", "project:retain",
                "--transcript", transcript_path or "-"]
        for key in self._STORE_KEYS + _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env_extra or {})
        try:
            with mock.patch.object(driver, "run_retain", counting_run_retain), \
                    mock.patch.object(sys, "stdin", stdin):
                code = driver.main(argv)
        finally:
            pass
        return code, calls

    def lane_env(self, scratch, **extra):
        env = {
            "ZMEM_DATA": scratch,
            "ZMEM_STORE": os.path.join(scratch, "store.sqlite"),
            "ZMEM_MODELS_DIR": os.path.join(scratch, "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        }
        env.update(extra)
        return env


class RetentionLaneMatrixTest(_DriverLaneCase):

    def _enabled_payload(self, host):
        """A one-record raw payload whose text the extractor retains.

        The adapter-byte fixtures pin the RAW shapes (test_host_payloads_...);
        the lane tests feed texts that classify (the zcode/codex fixture
        texts carry no retainable marker, so the lanes use explicit-marked
        variants of the same records).
        """
        if host == "claude":
            record = {"type": "user", "isMeta": False, "uuid": "claude-lane",
                      "timestamp": "2026-09-10T11:00:00Z",
                      "message": {"content":
                                  "Remember: use staged deploys"}}
        elif host == "codex":
            record = {"id": "codex-lane", "timestamp":
                      "2026-09-10T11:01:00Z", "role": "user",
                      "content": "Remember: run tests before merge"}
        else:
            record = {"event_id": "zcode-lane", "ts":
                      "2026-09-10T11:02:00Z", "role": "user",
                      "text": "Remember: keep the canary green"}
        return json.dumps(record, separators=(",", ":"),
                          ensure_ascii=False) + "\n"

    def test_all_five_lanes_are_off_by_default(self):
        for host in ("claude", "codex", "zcode"):
            for extra in ({}, {"ZMEM_AUTO_RETAIN": "0"}):
                scratch = _fresh_scratch()
                result = _run_cli(
                    ["retain", "--namespace", "project:retain",
                     "--session", "s", "--host", host],
                    stdin_text=self._enabled_payload(host),
                    env=_cli_env(scratch, **extra))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '{"count":0,"ok":true}\n')
                self.assertEqual(os.listdir(scratch),
                                 [], "off lane created files (%s, %r)" % (
                                     host, extra))
        # hermes-provider leg: config off -> "" and zero writes anywhere.
        provider = _load_provider()
        instance = provider.ZmemMemoryProvider()
        instance._session_id = "lane-session"
        instance._namespace = "project:retain"
        provider_scratch = _fresh_scratch()
        saved_prov = {k: os.environ.get(k) for k in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        try:
            with mock.patch.dict(
                    os.environ,
                    {"ZMEM_DATA": provider_scratch,
                     "ZMEM_STORE": os.path.join(provider_scratch,
                                                "store.sqlite"),
                     "ZMEM_MODELS_DIR": os.path.join(provider_scratch,
                                                     "missing-models"),
                     "ZMEM_MODEL_AUTODOWNLOAD": "0"}):
                summary = instance.on_pre_compress(
                    [{"id": "m-1", "timestamp": "2026-09-10T12:00:00Z",
                      "role": "user", "content": "Remember: pin release gate",
                      "isMeta": False}])
        finally:
            for key, value in saved_prov.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(summary, "")
        self.assertEqual(_queue_items(provider_scratch), [])
        self.assertEqual(os.listdir(provider_scratch),
                         [], "off provider lane created files")
        # hermes-compat leg: the compat pair runs, the in-child gate holds,
        # and nothing is written.
        compat = _load_compat_hook()
        compat_scratch = _fresh_scratch()
        payload = json.loads(
            (FIXTURES / "hermes-compat-payload.json").read_text(
                encoding="utf-8"))
        saved_compat = {k: os.environ.get(k)
                        for k in ("ZMEM_DATA", "ZMEM_STORE",
                                  "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD")
                        + _ENV_KEYS}
        for key in saved_compat:
            os.environ.pop(key, None)
        try:
            os.environ.update({
                "ZMEM_DATA": compat_scratch,
                "ZMEM_STORE": os.path.join(compat_scratch, "store.sqlite"),
                "ZMEM_MODELS_DIR": os.path.join(compat_scratch,
                                                "missing-models"),
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
            })
            compat._retain_compatibility_payload(
                payload, session_id="compat-session",
                namespace="project:retain")
        finally:
            for key, value in saved_compat.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(_queue_items(compat_scratch), [])
        self.assertEqual(os.listdir(compat_scratch),
                         [], "off compat lane created files")

    def _assert_one_queued_item(self, scratch, require_no_store=True):
        items = _queue_items(scratch)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["signal"], "none")
        self.assertEqual(item["source"], "live-capture")
        self.assertRegex(item["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(item["retain_hash"], r"^[0-9a-f]{64}$")
        self.assertIn("evidence_ids", item)
        for legacy in ("schema_version", "message", "type", "patterns",
                       "confidence", "sentiment", "decay_days", "timestamp",
                       "session", "namespace", "host"):
            self.assertIn(legacy, item)
        if require_no_store:
            self.assertFalse(Path(scratch, "store.sqlite").exists(),
                             "retention must not create the store")

    def test_claude_stop_enabled(self):
        scratch = self.lane_scratch()
        code, calls = self.run_driver(
            host="claude", payload_text=self._enabled_payload("claude"),
            env_extra=self.lane_env(scratch, ZMEM_AUTO_RETAIN="1"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], "retain-adapt")
        self.assertIn("claude", calls[0])
        self.assertEqual(calls[1][2], "retain")
        self._assert_one_queued_item(scratch)

    def test_claude_session_end_enabled(self):
        scratch = self.lane_scratch()
        payload_path = Path(scratch) / "payload.json"
        payload_path.write_text(self._enabled_payload("claude"),
                                encoding="utf-8")
        code, calls = self.run_driver(
            host="claude", transcript_path=str(payload_path),
            env_extra=self.lane_env(scratch, ZMEM_AUTO_RETAIN="1"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self._assert_one_queued_item(scratch)

    def test_codex_stop_enabled(self):
        scratch = self.lane_scratch()
        code, calls = self.run_driver(
            host="codex", payload_text=self._enabled_payload("codex"),
            env_extra=self.lane_env(scratch, ZMEM_AUTO_RETAIN="1"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self._assert_one_queued_item(scratch)

    def test_zcode_stop_enabled(self):
        scratch = self.lane_scratch()
        code, calls = self.run_driver(
            host="zcode", payload_text=self._enabled_payload("zcode"),
            env_extra=self.lane_env(scratch, ZMEM_AUTO_RETAIN="1"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self._assert_one_queued_item(scratch)

    def test_hermes_provider_sync_turn_enabled(self):
        provider = _load_provider()
        instance = provider.ZmemMemoryProvider()
        instance._session_id = "lane-session"
        instance._namespace = "project:retain"
        # sync_turn consults the provider-home gate BEFORE spawning the
        # store child, so a default install pays nothing per turn. This lane
        # enables it explicitly; the off-by-default case is covered by
        # test_all_five_lanes_are_off_by_default.
        instance._config = {"auto_retain": True}
        scratch = self.lane_scratch()
        argvs = []
        real_run_store = provider._run_store

        def counting_run_store(args, *pos_args, **kwargs):
            argvs.append(list(args))
            return real_run_store(args, *pos_args, **kwargs)

        with mock.patch.object(provider, "_run_store", counting_run_store), \
                mock.patch.object(provider, "_now_iso_utc",
                                  lambda: "2026-09-10T12:00:00Z"):
            for key in self._STORE_KEYS + _ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(self.lane_env(scratch,
                                            ZMEM_AUTO_RETAIN="1"))
            try:
                result = instance.sync_turn(
                    "Remember: pin release gate", "ack",
                    session_id="lane-session")
            finally:
                pass
        self.assertIsNone(result)
        self.assertEqual(len(argvs), 1)
        self.assertEqual(argvs[0][:7], [
            "retain", "--input", "-", "--namespace", "project:retain",
            "--session", "lane-session"])
        self.assertIn("hermes-provider", argvs[0])
        # The governed-partial body after the retention stage legitimately
        # initializes the scratch store via training capture; the pure
        # retention lanes (driver, on_pre_compress, compat) carry the
        # no-store clause.
        self._assert_one_queued_item(scratch, require_no_store=False)

    def test_hermes_provider_on_pre_compress_enabled(self):
        provider = _load_provider()
        instance = provider.ZmemMemoryProvider()
        instance._session_id = "lane-session"
        instance._namespace = "project:retain"
        scratch = self.lane_scratch()
        payload = json.loads(
            (FIXTURES / "hermes-provider-messages.json").read_text(
                encoding="utf-8"))
        argvs = []
        real_run_store = provider._run_store

        def counting_run_store(args, *pos_args, **kwargs):
            argvs.append(list(args))
            return real_run_store(args, *pos_args, **kwargs)

        with mock.patch.object(provider, "_run_store", counting_run_store):
            for key in self._STORE_KEYS + _ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(self.lane_env(scratch,
                                            ZMEM_AUTO_RETAIN="1"))
            try:
                summary = instance.on_pre_compress(
                    payload["messages"],
                    evidence_messages=payload["messages"])
            finally:
                pass
        self.assertEqual(argvs[-1][0], "retain")
        self.assertIn("hermes-provider", argvs[-1])
        self.assertTrue(summary.startswith("Filed "), summary)
        self._assert_one_queued_item(scratch)

    def test_hermes_compatibility_routing_enabled(self):
        compat = _load_compat_hook()
        payload = json.loads(
            (FIXTURES / "hermes-compat-payload.json").read_text(
                encoding="utf-8"))
        argvs = []
        real_run = subprocess.run

        def counting_run(args, **kwargs):
            argvs.append(list(args))
            return real_run(args, **kwargs)

        scratch = self.lane_scratch()
        saved = {k: os.environ.get(k)
                 for k in self._STORE_KEYS + _ENV_KEYS}
        with mock.patch.object(compat.subprocess, "run", counting_run):
            for key in self._STORE_KEYS + _ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(self.lane_env(scratch,
                                            ZMEM_AUTO_RETAIN="1"))
            try:
                compat._retain_compatibility_payload(
                    payload, session_id="compat-session",
                    namespace="project:retain")
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        self.assertEqual(len(argvs), 2)
        self.assertEqual(argvs[0][2], "retain-adapt")
        self.assertIn("hermes-compat", argvs[0])
        self.assertEqual(argvs[1][2], "retain")
        self.assertIn("hermes-compat", argvs[1])
        self._assert_one_queued_item(scratch)
        items = _queue_items(scratch)
        self.assertEqual(items[0]["host"], "hermes-compat")
        self.assertEqual(items[0]["signal"], "none")


# ---------------------------------------------------------------------------
# HermesRetentionTest
# ---------------------------------------------------------------------------

class HermesRetentionTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_pre_compress_summary_is_at_most_400_tokens(self):
        provider = _load_provider()
        instance = provider.ZmemMemoryProvider()
        instance._session_id = "lane-session"
        instance._namespace = "project:retain"
        messages = [{"id": "m-%d" % i,
                     "timestamp": "2026-09-10T12:00:00Z",
                     "role": "user",
                     "content": "Remember: item %d" % i,
                     "isMeta": False} for i in range(50)]

        def scripted(count):
            def _run(args, *pos_args, **kwargs):
                if args and args[0] == "retain":
                    return {"ok": True,
                            "stdout": '{"count":%d,"ok":true}\n' % count,
                            "stderr": "", "returncode": 0}
                return {"ok": False, "stdout": "", "stderr": "",
                        "returncode": 1}
            return _run

        # zero-count path (store-side gate off / no candidates): "".
        with mock.patch.object(provider, "_run_store", scripted(0)):
            self.assertIsNone(instance.sync_turn(
                "Remember: pin release gate", "ack",
                session_id="lane-session"))
            self.assertEqual(instance.on_pre_compress(messages), "")
        # positive count: the exact one-line summary within the token bound.
        with mock.patch.object(provider, "_run_store", scripted(3)):
            summary = instance.on_pre_compress(
                messages, evidence_messages=messages)
        self.assertEqual(summary, "Filed 3 retention candidate(s).")
        sys.path.insert(0, str(SCRIPTS))
        from storelib.inject import estimate_tokens
        self.assertLessEqual(estimate_tokens(summary), 400)

        # A required-checkpoint child failure raises sanitized RuntimeError;
        # an ordinary failure returns "".
        def failing(args, *pos_args, **kwargs):
            return {"ok": False, "stdout": "", "stderr": "x",
                    "returncode": 2}

        with mock.patch.object(provider, "_run_store", failing):
            with self.assertRaises(RuntimeError):
                instance.on_pre_compress(
                    messages, require_checkpoint=True)
            self.assertEqual(
                instance.on_pre_compress(messages), "")

    def test_provider_timeout_kills_child(self):
        provider = _load_provider()
        fake = FakeRetainProcess(
            ["store.py", "retain"], timeout_on=0.05)
        result = provider._run_store(
            ["retain", "--input", "-", "--namespace", "n", "--session", "s",
             "--host", "hermes-provider"],
            input_text="{}\n", popen_factory=lambda *a, **k: fake,
            timeout_s=0.05)
        self.assertEqual(result["returncode"], 124)
        self.assertEqual(
            [c.split(":")[0] for c in fake.calls],
            ["communicate", "kill", "communicate", "wait"])


# ---------------------------------------------------------------------------
# ProcessBoundaryTest
# ---------------------------------------------------------------------------

class ProcessBoundaryTest(unittest.TestCase):

    def test_hook_timeout_kills_child(self):
        driver = _load_driver()
        fake = FakeRetainProcess(["x"], timeout_on=0.05)
        code, out, err = driver.run_retain(
            ["x"], "input", timeout_s=0.05,
            popen_factory=lambda *a, **k: fake)
        self.assertEqual(code, 124)
        self.assertEqual(
            [c.split(":")[0] for c in fake.calls],
            ["communicate", "kill", "communicate", "wait"])
        # Normal completion waits for the child.
        fake_ok = FakeRetainProcess(["x"], stdout="ok\n")
        code, out, err = driver.run_retain(
            ["x"], "input", timeout_s=1.0,
            popen_factory=lambda *a, **k: fake_ok)
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok\n")
        self.assertIn("wait", fake_ok.calls)

    def test_no_hook_sqlite_import_or_connect(self):
        """The retention surfaces stay subprocess-only (issue #177).

        The issue's repo-wide rg predicate matches six SANCTIONED
        pre-existing capture surfaces (zmem-capture-correction.sh,
        zmem-training-capture.py, zmem-postcompact.sh — the #47 capture
        work), so the guard runs scoped to the five RETENTION surfaces this
        issue owns; the predicate itself is the issue's exact alternation,
        evaluated portably in Python.
        """
        pattern = re.compile(
            r"import storelib|from storelib|sqlite3|_LEDGER_MOD|_OPS_TOKENS"
            r"|delivery_ledger|correction_queue")
        files = [
            REPO_ROOT / "hooks" / "zmem-reflect.sh",
            REPO_ROOT / "hooks" / "zmem-session-end.sh",
            REPO_ROOT / "hooks" / "lib" / "zmem-retain-process.py",
            REPO_ROOT / "hermes-plugin" / "__init__.py",
            REPO_ROOT / "hermes-plugin" / "hooks" / "zmem-hermes-reflect.py",
        ]
        hits = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append("%s:%d:%s" % (path, line_no, line.strip()))
        self.assertEqual(hits, [])


# ---------------------------------------------------------------------------
# RetentionRegressionTest — one discriminating pin per feedback-round fix.
#
# Each test below is written to FAIL if its fix is reverted. That is the
# property the round was missing: every previous assertion still passed when
# the behaviour it named was deleted.
# ---------------------------------------------------------------------------

def _row(**over):
    row = {
        "evidence_id": "r-1",
        "text": "Remember: use staged deploys",
        "timestamp": "2026-09-10T10:00:00Z",
        "session": "s1",
        "namespace": "project:retain",
        "host": "claude",
    }
    row.update(over)
    return json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"


class RetentionRegressionTest(unittest.TestCase):

    def _retain(self, scratch, stdin_text, **extra):
        return _run_cli(
            ["retain", "--namespace", "project:retain", "--session", "s1",
             "--host", "claude"],
            stdin_text=stdin_text, env=_cli_env(scratch, **extra))

    def test_rerun_is_idempotent(self):
        """Re-running the same evidence must not grow the queue.

        Reverts if: the id guard in write_candidates is removed.
        """
        scratch = _fresh_scratch()
        first = self._retain(scratch, _row(), ZMEM_AUTO_RETAIN="1")
        self.assertEqual(first.stdout, '{"count":1,"ok":true}\n')
        for _ in range(3):
            again = self._retain(scratch, _row(), ZMEM_AUTO_RETAIN="1")
            self.assertEqual(again.returncode, 0)
        items = _queue_items(scratch)
        self.assertEqual(len(items), 1, "queue grew on repeat runs")
        self.assertEqual(len({i["id"] for i in items}), 1)

    def test_non_ascii_survives_the_round_trip(self):
        """Non-ASCII text must round-trip through the stdin seam intact.

        Reverts if: either stdin read falls back to the locale codec.
        """
        scratch = _fresh_scratch()
        text = "Remember: café — 中文"
        result = self._retain(scratch, _row(text=text), ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')
        items = _queue_items(scratch)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["message"], text)

    def test_one_bad_line_does_not_kill_the_batch(self):
        """A malformed row costs that row, not the whole batch.

        Reverts if: the unguarded list comprehension comes back.
        """
        scratch = _fresh_scratch()
        payload = (
            _row(evidence_id="good-1")
            + '{"role":"user", BROKEN\n'
            + _row(evidence_id="good-2", text="Remember: keep the canary green")
        )
        result = self._retain(scratch, payload, ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '{"count":2,"ok":true}\n')
        self.assertIn("skipped malformed row", result.stderr)

    def test_line_separator_does_not_split_a_row(self):
        """U+2028 inside a message must not shred the whole batch.

        _normalize_text collapses whitespace by design, so the separator
        itself is expected to be gone; what must survive is the ENTIRE
        message as ONE candidate. Reverts if: raw.splitlines() is used
        instead of splitting on newlines only.
        """
        scratch = _fresh_scratch()
        # Explicit escapes: the point is that the payload really contains a
        # LINE SEPARATOR, which str.splitlines() would treat as a row break.
        text = ("Remember: use staged deploys \u2014 plan\u2028was"
                " cut short")
        result = self._retain(scratch, _row(text=text), ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')
        items = _queue_items(scratch)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["message"],
            "Remember: use staged deploys \u2014 plan was cut short")

    def test_string_auto_retain_false_stays_off(self):
        """A string auto_retain must NOT enable retention.

        Reverts if: the gate goes back to bare bool().
        """
        for value in ("false", "0", "no", 1, []):
            scratch = _fresh_scratch()
            Path(scratch, "config.json").write_text(
                json.dumps({"auto_retain": value}), encoding="utf-8")
            result = self._retain(scratch, _row())
            self.assertEqual(
                result.stdout, '{"count":0,"ok":true}\n',
                "non-boolean auto_retain %r opened the gate" % (value,))
            self.assertEqual(_queue_items(scratch), [])

    def test_boolean_auto_retain_true_opens_the_gate(self):
        """The positive case must keep working after the strictness fix."""
        scratch = _fresh_scratch()
        Path(scratch, "config.json").write_text(
            json.dumps({"auto_retain": True}), encoding="utf-8")
        result = self._retain(scratch, _row())
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')

    def test_digest_covers_the_stored_redacted_message(self):
        """A secret-shaped message must not leave a hash of the plaintext.

        Reverts if: the digest goes back to hashing pre-redaction text.
        """
        secret = "api_key=sk-ant-abcdefghijklmnopqrstuvwx"
        scratch = _fresh_scratch()
        result = self._retain(
            scratch, _row(text="Remember: %s" % secret), ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.stdout, '{"count":1,"ok":true}\n')
        item = _queue_items(scratch)[0]
        self.assertNotIn("sk-ant", item["message"])
        # The digest must be taken over what was PERSISTED. Before the fix it
        # hashed the plaintext, so this equality fails and the redaction was
        # bypassable by anyone holding the queue file.
        self.assertEqual(
            item["retain_hash"],
            hashlib.sha256(
                ("\0".join([item["namespace"], item["message"],
                            item["evidence_timestamp"]]))
                .encode("utf-8")).hexdigest(),
            "digest does not match the persisted (redacted) message")

    def test_claude_record_without_ismeta_is_retained(self):
        """A record with no isMeta key matches the live capture contract.

        Reverts if: the literal `isMeta is False` gate comes back.
        """
        scratch = _fresh_scratch()
        record = {"type": "user", "uuid": "claude-absent",
                  "timestamp": "2026-09-10T11:00:00Z",
                  "message": {"content": "Remember: use staged deploys"}}
        result = _run_cli(
            ["retain-adapt", "--input", "-", "--namespace",
             "project:retain", "--session", "s1", "--host", "claude"],
            stdin_text=json.dumps(record) + "\n", env=_cli_env(scratch))
        self.assertEqual(result.returncode, 0)
        self.assertIn("claude-absent", result.stdout)

    def test_claude_list_content_is_retained(self):
        """Block-form message content is the real Claude transcript shape.

        Reverts if: the string-only content check comes back.
        """
        scratch = _fresh_scratch()
        record = {"type": "user", "uuid": "claude-list",
                  "timestamp": "2026-09-10T11:00:00Z",
                  "message": {"content": [
                      {"type": "text", "text": "Remember: keep the canary"}]}}
        result = _run_cli(
            ["retain-adapt", "--input", "-", "--namespace",
             "project:retain", "--session", "s1", "--host", "claude"],
            stdin_text=json.dumps(record) + "\n", env=_cli_env(scratch))
        self.assertEqual(result.returncode, 0)
        self.assertIn("claude-list", result.stdout)

    def test_fractional_second_timestamp_is_accepted(self):
        """Real transcripts carry millisecond precision.

        Reverts if: the strict seconds-only timestamp regex comes back.
        """
        scratch = _fresh_scratch()
        record = {"type": "user", "uuid": "claude-frac",
                  "timestamp": "2026-09-10T11:00:00.123Z",
                  "message": {"content": "Remember: use staged deploys"}}
        result = _run_cli(
            ["retain-adapt", "--input", "-", "--namespace",
             "project:retain", "--session", "s1", "--host", "claude"],
            stdin_text=json.dumps(record) + "\n", env=_cli_env(scratch))
        self.assertEqual(result.returncode, 0)
        self.assertIn('"timestamp":"2026-09-10T11:00:00Z"', result.stdout)

    def test_hermes_compat_payload_shapes_are_unwrapped(self):
        """The real pre_llm_call shape has no `messages` key.

        Reverts if: only the synthetic {"messages": [...]} shape is unwrapped.
        """
        shapes = (
            {"user_message": "Remember: keep the canary green"},
            {"conversation_history": [
                {"role": "user", "content": "Remember: use staged deploys"},
                {"role": "assistant", "content": "ok"}]},
            {"prompt": "Remember: pin the release gate"},
        )
        for shape in shapes:
            scratch = _fresh_scratch()
            result = _run_cli(
                ["retain-adapt", "--input", "-", "--namespace",
                 "project:retain", "--session", "s1",
                 "--host", "hermes-compat"],
                stdin_text=json.dumps(shape) + "\n", env=_cli_env(scratch))
            self.assertEqual(result.returncode, 0, shape)
            self.assertNotEqual(result.stdout, "", "compat shape dropped: %s"
                                % sorted(shape))

    def test_compat_unwrap_does_not_eat_a_plain_record(self):
        """The unwrap is scoped to the Hermes lanes only.

        Reverts if: a Claude/ZCode record with a `text` field is misread.
        """
        scratch = _fresh_scratch()
        record = {"event_id": "zcode-plain", "ts": "2026-09-10T11:02:00Z",
                  "role": "user", "text": "Remember: keep the canary green"}
        result = _run_cli(
            ["retain-adapt", "--input", "-", "--namespace",
             "project:retain", "--session", "s1", "--host", "zcode"],
            stdin_text=json.dumps(record) + "\n", env=_cli_env(scratch))
        self.assertEqual(result.returncode, 0)
        self.assertIn("zcode-plain", result.stdout)

    def test_sync_turn_does_not_spawn_when_the_gate_is_off(self):
        """A default provider must not pay a subprocess per turn.

        Reverts if: the provider-home gate check is removed from sync_turn.
        """
        provider = _load_provider()
        instance = provider.ZmemMemoryProvider()
        instance._session_id = "lane-session"
        instance._namespace = "project:retain"
        scratch = _fresh_scratch()
        argvs = []
        real = provider._run_store

        def counting(args, *a, **kw):
            argvs.append(list(args))
            return real(args, *a, **kw)

        with mock.patch.object(provider, "_run_store", counting):
            for key in _ENV_KEYS:
                os.environ.pop(key, None)
            os.environ.update(_cli_env(scratch, ZMEM_AUTO_RETAIN="1"))
            self.addCleanup(os.environ.pop, "ZMEM_AUTO_RETAIN", None)
            instance.sync_turn("Remember: pin release gate", "ack",
                               session_id="lane-session")
        retain_calls = [a for a in argvs if a and a[0] == "retain"]
        self.assertEqual(retain_calls, [],
                         "sync_turn spawned retain with the gate off")


    def test_queue_write_survives_a_colon_namespace(self):
        """Every real namespace contains ':' -- illegal in a Windows filename.

        Reverts if: the lock name goes back to using the raw namespace, which
        made the lock impossible to create and silently dropped every
        candidate after the first on Windows.
        """
        scratch = _fresh_scratch()
        first = self._retain(scratch, _row(), ZMEM_AUTO_RETAIN="1")
        self.assertEqual(first.stdout, '{"count":1,"ok":true}\n')
        # A DIFFERENT candidate in the same namespace: if the lock could not
        # be taken, this second write is refused and the row is dropped.
        second = self._retain(
            scratch,
            _row(evidence_id="r-2", text="Remember: keep the canary green"),
            ZMEM_AUTO_RETAIN="1")
        # count reports THIS call's candidates, not the queue total.
        self.assertEqual(second.stdout, '{"count":1,"ok":true}\n')
        self.assertEqual(len(_queue_items(scratch)), 2)
        leftovers = [n for n in os.listdir(scratch)
                     if n.startswith(".zmem-retain-queue")]
        self.assertEqual(leftovers, [],
                         "lock file left behind: %s" % leftovers)

    def test_pasted_block_is_skipped_but_plain_preference_is_kept(self):
        """The paste guard must not eat legitimate user preferences.

        Reverts if: an over-broad marker list is restored (the first guard
        rejected 'commit message', 'the following is' and 'as stated in').
        """
        keep = [
            "Remember: write commit messages in the imperative mood",
            "Remember: the following is our deploy checklist for Fridays",
            "Remember: as stated in the team wiki, we pin minor versions",
            "Remember: use >>> for prompt chaining in python",
            "Remember: always run ```pnpm test``` before pushing",
            "Remember: prefer structured logging, even in short scripts",
        ]
        for text in keep:
            scratch = _fresh_scratch()
            result = self._retain(scratch, _row(text=text),
                                  ZMEM_AUTO_RETAIN="1")
            self.assertEqual(result.stdout, '{"count":1,"ok":true}\n',
                             "legitimate preference dropped: %s" % text[:48])
            self.assertEqual(len(_queue_items(scratch)), 1)

        scratch = _fresh_scratch()
        pasted = ("Remember: the project instruction file says verbatim: "
                  "never push to main")
        result = self._retain(scratch, _row(text=pasted),
                              ZMEM_AUTO_RETAIN="1")
        self.assertEqual(result.stdout, '{"count":0,"ok":true}\n')
        self.assertEqual(_queue_items(scratch), [])

    def test_compat_ids_are_stable_as_history_grows(self):
        """The same user text must keep one id as the history lengthens.

        Reverts if: the synthetic id goes back to keying on a position, which
        defeated the idempotency guard on the compat lane.
        """
        message = "Remember: always stage deploys"
        ids = set()
        for assistant_turns in (0, 1, 3):
            scratch = _fresh_scratch()
            shape = {"conversation_history": [
                {"role": "user", "content": message}]}
            for _ in range(assistant_turns):
                shape["conversation_history"].append(
                    {"role": "assistant", "content": "ack"})
            result = _run_cli(
                ["retain-adapt", "--input", "-", "--namespace",
                 "project:retain", "--session", "s1",
                 "--host", "hermes-compat"],
                stdin_text=json.dumps(shape) + "\n", env=_cli_env(scratch))
            self.assertEqual(result.returncode, 0)
            rows = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            ids.add(json.loads(rows[0])["evidence_id"])
        self.assertEqual(len(ids), 1, "compat evidence id is not stable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
