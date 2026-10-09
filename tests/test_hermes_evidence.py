"""Issue #164: `on_delegation` evidence — HermesEvidenceTest.

Deterministic, thread-free, model-absent.  The provider is loaded with the
Hermes ABC stubbed (tests/test_hermes_hooks.py pattern); unit-level cases
patch ``_run_store`` at the sanctioned seam (the issue's test contract), and
CLI-contract cases drive the real ``store.py`` subprocess against the scratch
store pinned below — never the operator's ``~/.zmem``.
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Store isolation BEFORE any import that might reach storelib or spawn a
# store subprocess (issue #61 hazard: storelib freezes STORE_PATH at import).
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-164-evidence-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "evidence"
_CHILD = "00000000-0000-4000-8000-000000000164"
_PARENT = "00000000-0000-4000-8000-000000000165"
_TASK_SHA = "b2d89077f3c1c86f9aec81e9e688b8ed902780ecc00333f33a839486ed8105ee"
_RESULT_SHA = "514bf87b606c5d7f24f179cf9b3b5bf1ccb20136386184b6bbfa207a078663cb"


def _load_provider():
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed."""
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_hermes_hooks.py)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        "zmem_hermes_164_evidence", REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_hermes_164_evidence"] = module
    spec.loader.exec_module(module)
    return module


def _run_store_cli(args, input_text=None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"),
         *args],
        input=input_text, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, cwd=str(REPO_ROOT))


def _compact_payload():
    obj = {
        "child_session_id": _CHILD,
        "parent_session_id": _PARENT,
        "result_sha256": _RESULT_SHA,
        "task_sha256": _TASK_SHA,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def _expected_argv():
    return [
        "evidence", "write",
        "--kind", "delegation",
        "--session-id", _PARENT,
        "--lane", "hermes-provider",
        "--moment", "subagent",
        "--ref-path", f"delegation:{_PARENT}:{_CHILD}",
    ]


def _ok_envelope_argv(args):
    return {"ok": True, "stdout": json.dumps({"ok": True, "id": str(uuid.uuid4())}) + "\n",
            "stderr": "", "returncode": 0}


class _Seam:
    """Record every _run_store call; replies are per-case."""

    def __init__(self, module, reply):
        self.calls = []
        self._module = module
        self._real = module._run_store
        self._reply = reply

    def _fake(self, args, *pos, **kwargs):
        self.calls.append({"argv": list(args),
                           "input_text": kwargs.get("input_text")})
        reply = self._reply
        if callable(reply):
            return reply(args, *pos, **kwargs)
        return reply

    def __enter__(self):
        self._module._run_store = self._fake
        return self

    def __exit__(self, *exc):
        self._module._run_store = self._real
        return False


class HermesEvidenceTest(unittest.TestCase):
    """Issue #164 acceptance: one hashed delegation evidence request,
    fail-open everywhere, zero memory rows."""

    @classmethod
    def setUpClass(cls):
        cls.module = _load_provider()

    def _provider(self):
        provider = self.module.ZmemMemoryProvider()
        provider.initialize(_PARENT, hermes_home=str(_SCRATCH))
        return provider

    def test_delegation_writes_one_evidence_row(self):
        fixture = json.loads((_FIXTURES / "delegation.json").read_text(
            encoding="utf-8"))
        expected_payload = (_FIXTURES / "delegation.expected.json").read_text(
            encoding="utf-8")
        provider = self._provider()
        with _Seam(self.module, _ok_envelope_argv) as seam:
            ret = provider.on_delegation(
                fixture["task"], fixture["result"],
                child_session_id=fixture["child_session_id"],
                parent_session_id=fixture["parent_session_id"])
        self.assertIsNone(ret)
        self.assertEqual(len(seam.calls), 1)
        call = seam.calls[0]
        self.assertEqual(call["argv"], _expected_argv())
        self.assertEqual(call["input_text"], expected_payload)
        payload = json.loads(call["input_text"])
        self.assertEqual(payload["task_sha256"], _TASK_SHA)
        self.assertEqual(payload["result_sha256"], _RESULT_SHA)
        self.assertEqual(payload["child_session_id"], _CHILD)
        self.assertEqual(payload["parent_session_id"], _PARENT)
        self.assertEqual(
            call["argv"][call["argv"].index("--kind") + 1], "delegation")
        # No raw task/result text ever crosses the subprocess boundary.
        self.assertNotIn(fixture["task"], call["input_text"])
        self.assertNotIn(fixture["result"], call["input_text"])

    def test_delegation_hashes_exact_utf8_values(self):
        for value in (
                "Delegate the exact UTF-8 task.",
                "Result with trailing space ",
                "unicode — ünïcödé 東京 🚀 end",
                "",
        ):
            self.assertEqual(
                self.module._delegation_hash(value),
                hashlib.sha256(value.encode("utf-8")).hexdigest(),
                msg=repr(value))

    def test_delegation_does_not_add_memory_row(self):
        proc = _run_store_cli(["init"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for i, content in enumerate((
                "seed lesson one for delegation projection",
                "seed lesson two for delegation projection")):
            proc = _run_store_cli([
                "add", "--namespace", "user:global", "--type", "lesson",
                "--content", content, "--source-ref", f"seed164:{i}"])
            self.assertEqual(proc.returncode, 0, proc.stderr)

        def projection():
            conn = sqlite3.connect(os.environ["ZMEM_STORE"])
            try:
                rows = conn.execute(
                    "SELECT * FROM memory ORDER BY id").fetchall()
                return json.dumps([list(r) for r in rows], default=str)
            finally:
                conn.close()

        before = projection()
        provider = self._provider()
        with _Seam(self.module, _ok_envelope_argv):
            ret = provider.on_delegation(
                "Delegate the exact UTF-8 task.",
                "Result with trailing space ",
                child_session_id=_CHILD, parent_session_id=_PARENT)
        self.assertIsNone(ret)
        self.assertEqual(projection(), before)

    def test_evidence_writer_failure_is_swallowed(self):
        provider = self._provider()
        with _Seam(self.module, lambda *a, **k: (_ for _ in ()).throw(
                OSError("writer failure"))) as seam:
            ret = provider.on_delegation(
                "task", "result", child_session_id=_CHILD,
                parent_session_id=_PARENT)
        self.assertIsNone(ret)
        self.assertEqual(len(seam.calls), 1)

        with _Seam(self.module, {"ok": False, "stdout": "not-json",
                                 "stderr": "boom", "returncode": 1}) as seam:
            ret = provider.on_delegation(
                "task", "result", child_session_id=_CHILD,
                parent_session_id=_PARENT)
        self.assertIsNone(ret)
        self.assertEqual(len(seam.calls), 1)

    def test_pre_compress_remains_untouched(self):
        provider = self._provider()
        self.assertEqual(provider.on_pre_compress([]), "")

    def test_flagged_cli_error_contract(self):
        proc = _run_store_cli(["init"])
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # Partial flags: exactly the shared exit-2 validation path, and the
        # payload is never echoed back.
        proc = _run_store_cli(
            ["evidence", "write", "--kind", "delegation"],
            input_text=_compact_payload())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("evidence write failed", proc.stderr)
        self.assertNotIn(_TASK_SHA, proc.stdout + proc.stderr)

        # Malformed payload (bad digest) with full flags: exit 2.
        bad = json.dumps({
            "child_session_id": _CHILD, "parent_session_id": _PARENT,
            "result_sha256": "NOT-A-DIGEST", "task_sha256": _TASK_SHA})
        proc = _run_store_cli(
            ["evidence", "write", "--kind", "delegation", "--session-id", _PARENT,
             "--lane", "hermes-provider", "--moment", "subagent",
             "--ref-path", f"delegation:{_PARENT}:{_CHILD}"],
            input_text=bad)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("NOT-A-DIGEST", proc.stdout + proc.stderr)

        # Forced writer failure: a BEFORE-INSERT trigger aborts write_evidence;
        # the flagged branch must answer exit 1 with the failure envelope.
        conn = sqlite3.connect(os.environ["ZMEM_STORE"])
        try:
            conn.execute(
                "CREATE TRIGGER refuse_delegation BEFORE INSERT ON evidence "
                "WHEN NEW.kind='delegation' BEGIN "
                "SELECT RAISE(ABORT, 'forced writer failure'); END;")
            conn.commit()
        finally:
            conn.close()
        proc = _run_store_cli(
            ["evidence", "write", "--kind", "delegation", "--session-id", _PARENT,
             "--lane", "hermes-provider", "--moment", "subagent",
             "--ref-path", f"delegation:{_PARENT}:{_CHILD}"],
            input_text=_compact_payload())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(
            proc.stdout.strip(), '{"ok":false,"error":"writer failure"}')

        # Legacy flagless mode keeps the bare-id stdout contract even when the
        # store would accept the row (envelope only ever belongs to flags).
        conn = sqlite3.connect(os.environ["ZMEM_STORE"])
        try:
            conn.execute("DROP TRIGGER refuse_delegation")
            conn.commit()
        finally:
            conn.close()
        legacy_row = {
            "session_id": "legacy-164", "lane": "hermes-provider",
            "moment": "pretool", "kind": "tool_call",
            "ts": "2026-10-09T00:00:00Z", "excerpt": "{\"legacy\":true}",
            "ref_path": "hermes://legacy/164", "ref_offset": None,
        }
        proc = _run_store_cli(
            ["evidence", "write"],
            input_text=json.dumps(legacy_row, separators=(",", ":")))
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn('"ok"', proc.stdout)
        self.assertRegex(proc.stdout.strip(), r"^[0-9a-fA-F-]{36}$")

    def test_empty_parent_skips_subprocess(self):
        provider = self.module.ZmemMemoryProvider()  # never initialized
        with _Seam(self.module, _ok_envelope_argv) as seam:
            ret = provider.on_delegation(
                "task", "result", child_session_id=_CHILD)
        self.assertIsNone(ret)
        self.assertEqual(seam.calls, [])

    def test_non_string_task_result_skips(self):
        provider = self._provider()
        with _Seam(self.module, _ok_envelope_argv) as seam:
            ret = provider.on_delegation(
                12345, "result", child_session_id=_CHILD,
                parent_session_id=_PARENT)
        self.assertIsNone(ret)
        self.assertEqual(seam.calls, [])

    def test_malformed_success_envelope_swallowed(self):
        provider = self._provider()
        for stdout in ("not-json{", '{"ok":true}', '{"ok":false,"id":"x"}'):
            with _Seam(self.module, {"ok": True, "stdout": stdout,
                                      "stderr": "", "returncode": 0}) as seam:
                ret = provider.on_delegation(
                    "task", "result", child_session_id=_CHILD,
                    parent_session_id=_PARENT)
            self.assertIsNone(ret)
            self.assertEqual(len(seam.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
